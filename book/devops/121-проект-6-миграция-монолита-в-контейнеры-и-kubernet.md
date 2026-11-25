# Проект №6: Миграция монолита в контейнеры и Kubernetes с zero-downtime и canary-выкаткой

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора», 9:40 утра:

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **Что написал:** «Портал перевозок опять лёг? У меня при сохранении заявки выскочило `502 Bad Gateway`, а у Петровой всё открылось. У нас отгрузка в 15:00, заявки не заходят.»
> **Срочность:** высокая

Заявка учебная, но ситуация — самая что ни на есть боевая. Портал учёта перевозок `vektor-track` (монолит на WEB01: Python-приложение + PostgreSQL + nginx) — единственная точка, через которую логисты оформляют перевозки. Каждый релиз = 5–10 минут простоя и пачка таких тикетов, потому что приложение перезапускается целиком.

Задача DevOps-инженера в такой ситуации — не «переписать всё на микросервисы за месяц», а аккуратно разрезать монолит: упаковать приложение в контейнер, поднять его в Kubernetes рядом со старым, начать пускать 10% трафика на новую версию, посмотреть метрики и только потом переключать всё. И чтобы при любом релизе ни один пользователь не увидел 502.

После этой главы ты будешь уметь: писать Dockerfile для legacy-приложения; описывать его в Kubernetes так, чтобы rolling update не ронял запросы; настраивать canary-выкатку через Argo Rollouts с автоматическим анализом метрик; откатывать релиз одной командой; и главное — понимать, где в этой схеме прячется база данных и почему она ломает все красивые планы.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Монолит vs контейнер.** Монолит — одно приложение, один процесс, один хост: упало — упало всё, обновление — целиком. Контейнер — это изоляция процесса через namespaces и cgroups ядра Linux: своя файловая система (слои образа), свои лимиты CPU/RAM, но общее ядро. Kubernetes добавляет оркестрацию: сам решает, где запустить контейнер, перезапускает упавшие, раскидывает трафик.

**Стратегия миграции — Strangler Fig («душитель»).** Мы не переключаем всё разом. Рядом со старым приложением поднимаем новое, ставим перед обоими прокси (nginx на WEB01 или Ingress) и постепенно переводим трафик. Старое приложение «удушается» по кусочкам, а не выключается в ночь на субботу.

```text
                     пользователи (WIN11-CL01, WIN10-CL02)
                                 │
                                 ▼
                    ┌────────────────────────────┐
                    │  nginx на WEB01 10.10.20.10│  HTTP 80/443
                    │  upstream: старый / новый  │
                    └───────┬────────────┬───────┘
                  вариант A │            │ вариант B (постепенно)
                            ▼            ▼
              ┌──────────────────┐   ┌─────────────────────────────┐
              │ МОНОЛИТ :8000    │   │ ingress-nginx в кластере     │
              │ systemd unit     │   │ track.corp.vektor.lab :30080 │
              └────────┬─────────┘   └──────────────┬──────────────┘
                       │                            ▼
                       │                 ┌──────────────────────┐
                       │                 │ Service track-api    │
                       │                 │ :80 → targetPort 8080│
                       │                 └────┬────────────┬────┘
                       │                      ▼            ▼
                       │              pod 1.0.0     pod 1.1.0 (canary)
                       │                      └─────┬──────┘
                       ▼                            ▼
              ┌──────────────────────────────────────────┐
              │ PostgreSQL 15 на WEB01, 127.0.0.1:5432   │
              │ (пока НЕ в кластере — данные тяжёлые)    │
              └──────────────────────────────────────────┘
```

**Почему получается zero-downtime.** В Kubernetes подами управляет Deployment/Rollout: он создаёт новый под, ждёт, пока тот пройдёт `readinessProbe`, добавляет его в Endpoints объекта Service и только потом убивает старый. Дальше всё решают четыре вещи:

1. `maxUnavailable: 0` + `maxSurge: 1` — сначала поднимаем новый под, потом гасим старый. Простоя по ёмкости нет.
2. `readinessProbe` — трафик не пойдёт в под, пока приложение не отвечает 200 на `/readyz`. `livenessProbe` — про другое: она перезапускает зависшее приложение, и её нельзя вешать на проверку внешней зависимости (БД), иначе получишь бесконечный CrashLoopBackOff.
3. `startupProbe` — даёт приложению время на «холодный старт» (у нас 40 секунд прогрева кэша), не мешая liveness.
4. `preStop` + graceful shutdown — самый недооценённый пункт. Когда под удаляется, kubelet сначала шлёт SIGTERM, но **kube-proxy и ingress не мгновенно убирают под из балансировки**. Поэтому в контейнере: `sleep 5` в preStop → приложение продолжает отвечать → эндпоинт уже вычищен → и только потом gunicorn получает SIGTERM и добивает текущие запросы. `terminationGracePeriodSeconds` должен быть больше `--graceful-timeout` gunicorn. Если это не настроить, часть пользователей получает 502 — ровно как в тикете выше.

**Canary release.** «Канарейка» — это выкатка на часть трафика (1 → 10 → 30 → 50 → 100%) с автоматической проверкой метрик между шагами. Ключевое отличие от rolling update: rolling — это про доступность, canary — про **качество** новой версии на реальном трафике. Уровни переключения трафика бывают разные:

- **L4, по количеству подов** (Rollout без trafficRouting): 10 подов, 1 канареечный = ~10% запросов. Грубо, но работает везде.
- **L7, через ingress** (Argo Rollouts + nginx Ingress): вес задаётся аннотацией на canary-Ingress, точность — проценты.

**Где боль.** База. Canary работает, только если обе версии приложения совместимы со схемой БД. Отсюда правило **expand/contract**: сначала добавляем новую колонку/таблицу (expand), пишем код, который умеет и так, и так, переключаем трафик, и только через релиз удаляем старое (contract). Никаких `DROP COLUMN` в том же релизе, что и код.

## 3. Аналогия из жизни

Представь, что ты меняешь колёса на ходу — но не на легковушке, а на составе из трёх вагонов (три реплики). Ты не останавливаешь поезд: отцепляешь один вагон, ставишь новый, проверяешь, что колёса крутятся (readiness), и только потом берёшься за следующий (maxUnavailable: 0). Canary — это когда ты сначала ставишь новое колесо на один вагон и едешь так десять километров, глядя на датчик вибрации (AnalysisTemplate). Завибрировало — откатываешь обратно одной кнопкой. А `preStop` — это сигнал «не садись в этот вагон, он сейчас на замену», который машинист подаёт до того, как отцепить вагон.

Аналогия перестаёт работать в одном месте: у поезда вагоны одинаковые, а у нас версии 1.0.0 и 1.1.0 одновременно смотрят в одну базу — и это ограничение никто не отменял.

## 4. Практика и команды

**1. Инвентаризация монолита (WEB01).** Прежде чем что-то контейнеризировать, надо понять, что там живёт.

```bash
sudo ss -lntp                                  # кто какие порты слушает
systemctl list-units --type=service --state=running | grep -Ei 'track|gunicorn|postgres|nginx'
sudo nginx -T | grep -nE 'server_name|listen|proxy_pass'
curl -s localhost:8000/healthz; echo
sudo -u postgres psql -tc "select version();"  # версия БД — от неё зависит образ
```

`ss -lntp` расшифровывается как sockets, listen, numeric, TCP, processes. Смотрим: приложение на `:8000`, PostgreSQL на `127.0.0.1:5432`, nginx на `:80`. Результат пишем в `stand-log.md` — это наша «карта местности».

**2. Dockerfile: multi-stage + non-root.**

```dockerfile
FROM python:3.12-slim AS build
WORKDIR /src
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
RUN useradd -u 10001 -m app
COPY --from=build /install /usr/local
WORKDIR /app
COPY --chown=app:app . .
USER 10001
EXPOSE 8080
CMD ["gunicorn","--bind","0.0.0.0:8080","--workers","2","--graceful-timeout","25","app:app"]
```

`--prefix=/install` собирает зависимости в отдельную папку, чтобы не тащить в финальный образ компиляторы. `USER 10001` — не root. `--graceful-timeout 25` должен быть меньше `terminationGracePeriodSeconds: 45`.

**3. Проверка образа до деплоя (без Docker на UBU01).**

```bash
kubectl create namespace vektor-track
kubectl -n vektor-track run track-smoke --restart=Never \
  --image=harbor.corp.vektor.lab/vektor/track-api:1.1.0 --port=8080
kubectl -n vektor-track port-forward pod/track-smoke 18080:8080 &
curl -s localhost:18080/version; curl -s localhost:18080/readyz; echo
kubectl -n vektor-track delete pod track-smoke
```

`--restart=Never` — одноразовый под. `port-forward` пробрасывает порт с твоего ПК (где запущен kubectl) в под. Проверяем `/version` (должна вернуться 1.1.0) и `/readyz`.

**4. Deployment с нулевым простоем.**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: track-api
  namespace: vektor-track
spec:
  replicas: 3
  minReadySeconds: 10
  progressDeadlineSeconds: 300
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  selector:
    matchLabels: {app: track-api}
  template:
    metadata:
      labels: {app: track-api}
    spec:
      terminationGracePeriodSeconds: 45
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels: {app: track-api}
      containers:
      - name: api
        image: harbor.corp.vektor.lab/vektor/track-api:1.0.0
        ports: [{containerPort: 8080, name: http}]
        env:
        - name: DB_HOST
          value: 10.10.20.10
        resources:
          requests: {cpu: 100m, memory: 128Mi}
          limits:   {cpu: 500m, memory: 512Mi}
        startupProbe:
          httpGet: {path: /healthz, port: http}
          periodSeconds: 2
          failureThreshold: 30
        readinessProbe:
          httpGet: {path: /readyz, port: http}
          periodSeconds: 5
          timeoutSeconds: 2
        livenessProbe:
          httpGet: {path: /healthz, port: http}
          periodSeconds: 10
          failureThreshold: 3
        lifecycle:
          preStop:
            exec: {command: ["sh","-c","sleep 10"]}
        securityContext:
          runAsNonRoot: true
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities: {drop: ["ALL"]}
```

`startupProbe` даёт до 60 секунд на старт (2×30) и блокирует liveness, пока приложение не поднялось. `topologySpreadConstraints` разбрасывает реплики по k8s-cp01/w01/w02 — чтобы падение одной ВМ не убило всё.

**5. Service, PDB, Ingress.**

```yaml
apiVersion: v1
kind: Service
metadata: {name: track-api, namespace: vektor-track}
spec:
  selector: {app: track-api}
  ports: [{name: http, port: 80, targetPort: 8080}]
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: track-api, namespace: vektor-track}
spec:
  minAvailable: 2
  selector:
    matchLabels: {app: track-api}
```

PDB не даст `kubectl drain` узла выключить больше одного пода сразу. `targetPort: 8080` — порт контейнера, `port: 80` — порт внутри кластера.

**6. Установка Argo Rollouts и плагина kubectl.**

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f \
  https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
# плагин kubectl (URL установщика бери из официальной страницы Installation) [ПРОВЕРИТЬ]
kubectl argo rollouts version
```

**7. Rollout вместо Deployment.**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata: {name: track-api, namespace: vektor-track}
spec:
  replicas: 4
  strategy:
    canary:
      stableService: track-api
      canaryService: track-api-canary
      trafficRouting:
        nginx:
          stableIngress: track-api
      steps:
      - setWeight: 10
      - pause: {duration: 2m}
      - analysis: {templates: [{templateName: success-rate}]}
      - setWeight: 30
      - pause: {duration: 3m}
      - setWeight: 50
      - pause: {}
```

`trafficRouting.nginx` заставляет Rollouts на ходу править аннотации canary-Ingress. Точный набор полей зависит от версии — сверяйся с docs `[ПРОВЕРИТЬ]`. Без `trafficRouting` доля трафика считается по количеству подов.

**8. AnalysisTemplate по Prometheus.**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata: {name: success-rate, namespace: vektor-track}
spec:
  args: [{name: canary-version}]
  metrics:
  - name: success-rate
    interval: 30s
    count: 3
    successCondition: result[0] >= 0.99
    failureLimit: 1
    provider:
      prometheus:
        address: http://prometheus-server.monitoring.svc:9090  # [ПРОВЕРИТЬ] свой Service из урока 119
        query: |
          sum(rate(http_requests_total{version="{{args.canary-version}}",code!~"5.."}[2m]))
          /
          sum(rate(http_requests_total{version="{{args.canary-version}}"}[2m]))
```

`count: 3` — три замера, `failureLimit: 1` — после первого провала канарейка откатывается автоматически.

**9. Управление выкаткой.**

```bash
kubectl argo rollouts get rollout track-api -n vektor-track --watch
kubectl argo rollouts set image track-api -n vektor-track \
  api=harbor.corp.vektor.lab/vektor/track-api:1.1.0
kubectl argo rollouts promote track-api -n vektor-track          # пройти паузу
kubectl argo rollouts promote track-api -n vektor-track --full   # до конца
kubectl argo rollouts abort  track-api -n vektor-track           # стоп + откат
kubectl argo rollouts undo   track-api -n vektor-track           # предыдущая версия
```

`--watch` рисует дерево: ReplicaSet → поды, вес шага и статус анализа. Это главный экран при релизе.

**10. Кто реально готов принимать трафик.**

```bash
kubectl -n vektor-track get endpointslices \
  -l kubernetes.io/service-name=track-api -o wide
kubectl -n vektor-track get events --sort-by=.lastTimestamp | tail -20
```

В `ENDPOINTS` должны быть только поды в статусе Ready. Если список пуст — Service не находит поды (несовпадение `selector`/`labels`) или не проходит readiness.

**11. Нагрузочная проверка нулевого простоя (с UBU01).**

```bash
cat > /tmp/load-check.sh <<'EOF'
#!/usr/bin/env bash
CURL="curl -s -o /dev/null -m 3 -w %{http_code} --resolve track.corp.vektor.lab:30080:10.10.10.62"
declare -A codes
for i in $(seq 1 600); do
  c=$($CURL http://track.corp.vektor.lab:30080/api/v1/shipments || echo 000)
  codes[$c]=$(( ${codes[$c]:-0} + 1 ))
  sleep 0.1
done
for k in "${!codes[@]}"; do echo "$k -> ${codes[$k]}"; done
EOF
bash /tmp/load-check.sh
```

`--resolve host:port:ip` избавляет от зависимости от DNS стенда. `%{http_code}` — только код ответа. Идеальный результат — одна строка `200 -> 600`. Как альтернатива: `hey -z 60s -c 10 http://...` или `ab -n 2000 -c 20`.

## 5. Лабораторная работа

### Предусловия

- Кластер из урока 118: `k8s-cp01` 10.10.10.61, `k8s-w01` .62, `k8s-w02` .63 — Ready, ingress-nginx работает.
- `GIT01` 10.10.10.50 (GitLab CE + runner), `HARBOR01` 10.10.10.55 (Harbor), ArgoCD из урока 116.
- `WEB01` 10.10.20.10 — монолит `vektor-track` под systemd на `:8000`, nginx на `:80`, PostgreSQL локально. Доступа к приложению снаружи — нет, только через nginx.
- `MON01` 10.10.10.40 и Prometheus в кластере из урока 119.
- С `k8s-*` есть маршрут в DMZ через GW01 10.10.10.3. Проверка: `nc -zv 10.10.20.10 5432` из отладочного пода. Если не проходит — на GW01 в nftables разрешить `10.10.10.61-63 → 10.10.20.10:5432` (правило согласуй с сетевиком, у нас это учебный стенд).
- Если Argo Rollouts ещё не стоит — ставим на шаге 6.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-121`. Сделать на **WEB01, UBU01, k8s-cp01, k8s-w01, k8s-w02**. Для GIT01/HARBOR01 — тоже, если меняешь их настройки.

### Шаги

1. **WEB01.** Инвентаризация: `sudo ss -lntp`, `sudo nginx -T | grep proxy_pass`, `systemctl status vektor-track`. Ожидаемо: приложение на `:8000`, БД на `127.0.0.1:5432`, nginx проксирует `track.corp.vektor.lab` на монолит. Записать в `stand-log.md`.
2. **WEB01.** Зафиксировать контракт API: `curl -s localhost:8000/version` и список эндпоинтов. Дописать в приложение `/healthz` (живость), `/readyz` (готовность = есть коннект к БД), `/metrics` (счётчик `http_requests_total{code,version}`) и `/version`. Ожидаемо: все четыре отвечают 200.
3. **UBU01.** `git clone` репозитория `vektor-track` с GIT01, добавить `Dockerfile` из раздела 4. Закоммитить в ветку `feature/containerize`, открыть MR в GitLab. Ожидаемо: пайплайн из урока 116 собрал образ и запушил `harbor.corp.vektor.lab/vektor/track-api:1.1.0`.
4. **UBU01.** Smoke-тест образа через одноразовый под (команда №3 раздела 4). Ожидаемо: `/version` → `1.1.0`, `/readyz` → `{"db":"ok"}`.
5. **k8s-cp01.** `kubectl create namespace vektor-track` и `kubectl -n vektor-track create secret generic track-db --from-literal=password='Lab#2026-vektor'`. Ожидаемо: `kubectl get secret -n vektor-track` показывает `track-db`.
6. **k8s-cp01.** Установить Argo Rollouts и плагин (команда №6). Ожидаемо: `kubectl argo rollouts version` печатает версию.
7. **GIT01 → ArgoCD.** В gitops-репозитории `vektor-track-deploy` разложить `base/` (Deployment, Service, PDB, Ingress) и `rollout/`. ArgoCD подхватывает каталог. Ожидаемо: в ArgoCD приложение `vektor-track` — Synced/Healthy, 3 пода Ready, разбросаны по трём узлам (`kubectl get pods -o wide`).
8. **UBU01.** Переключить nginx на WEB01 на кластер: в `upstream track_backend` закомментировать `10.10.20.10:8000` и раскомментировать `10.10.10.62:30080`, `sudo nginx -t && sudo systemctl reload nginx`. Ожидаемо: `curl -sI http://track.corp.vektor.lab/version` с WIN11-CL01 возвращает 200 и версию `1.0.0`. Монолит оставить запущенным — это наш путь отката.
9. **UBU01.** Запустить `/tmp/load-check.sh` в одном терминале, во втором — `kubectl argo rollouts set image track-api api=...:1.1.0`. Ожидаемо: `200 -> 600`, ни одного 5xx, в `kubectl argo rollouts get rollout` видно прохождение шагов 10% → 30% → 50%.
10. **k8s-cp01.** Проверить PDB: `kubectl -n vektor-track describe pdb track-api` → `Allowed disruptions: 2`. Затем `kubectl drain k8s-w02 --ignore-daemonsets --delete-emptydir-data` и сразу `kubectl uncordon`. Ожидаемо: поды переехали, load-check не показал ошибок.
11. **k8s-cp01.** Остановить монолит: на WEB01 `sudo systemctl disable --now vektor-track`. Ожидаемо: портал работает, `5432` по-прежнему на WEB01. Записать в `stand-log.md`.

### Проверка

- [ ] `kubectl -n vektor-track get rollout track-api` → `HEALTHY`, версия `1.1.0`.
- [ ] `kubectl -n vektor-track get endpointslices -l kubernetes.io/service-name=track-api` содержит 4 адреса (или 3, если rollout в процессе).
- [ ] `/tmp/load-check.sh` во время релиза дал только коды `200`.
- [ ] `kubectl -n vektor-track get pods -o wide` — поды на разных узлах.
- [ ] `curl -s http://track.corp.vektor.lab/version` с WIN11-CL01 → `1.1.0`.
- [ ] `kubectl -n vektor-track describe pdb track-api` → `minAvailable: 2`.
- [ ] В `stand-log.md` записаны: namespace, имя Rollout, версия образа, что монолит выключен.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «После обеда портал перевозок при сохранении заявки выдаёт 502, иногда просто крутится и всё. У Петровой открывается. У нас отгрузка в 15:00, срочно.»
> **Срочность:** высокая

**Воспроизводим поломку.** В `Rollout` убираем `preStop` и ставим `terminationGracePeriodSeconds: 5`, а в образе `1.2.0` меняем `CMD` на `--graceful-timeout 60`. Затем под нагрузкой:

```bash
kubectl argo rollouts set image track-api -n vektor-track api=harbor.corp.vektor.lab/vektor/track-api:1.2.0
bash /tmp/load-check.sh     # увидим 200 -> 570, 502 -> 30
```

**Диагностика по порядку:**

1. Уточняем у пользователя: ошибка только при сохранении (POST) или на всех страницах? Это делит проблему на «приложение» и «балансировщик».
2. `kubectl -n ingress-nginx logs -l app.kubernetes.io/name=ingress-nginx --tail=100 | grep -E ' 50[0-9] '` — видим, что 502 генерирует ingress, а не приложение.
3. `kubectl -n vektor-track get pods -w` — во время релиза поды уходят в `Terminating` и исчезают за 5 секунд вместо 45.
4. `kubectl -n vektor-track get events --sort-by=.lastTimestamp | tail` — ищем `Unhealthy`, `Killing`.
5. `kubectl -n vektor-track logs deploy/track-api --previous` — пусто, приложение не падало. Значит, запросы рвутся на завершении.
6. Вывод: gunicorn не успевает добить in-flight запросы, kube-proxy уже убрал под из Endpoints, но keep-alive соединения живы. Классика.

**Решение:** вернуть `preStop: sleep 10`, `terminationGracePeriodSeconds: 45`, в образе `--graceful-timeout 25`. Откатить плохую версию:

```bash
kubectl argo rollouts undo track-api -n vektor-track
```

Перезапустить load-check — должен дать `200 -> 600`.

**Ответ пользователю:** «Иван, проблему нашли: при обновлении портала часть запросов обрывалась на стороне сервера. Сейчас откатили обновление и поправили настройки остановки сервиса. Проверьте, пожалуйста, сохранение заявки, и напишите, если повторится — я подниму логи по вашему логину».

**Закрытие заявки:** категория «Веб-приложение / доступность», приоритет «Высокий», в решении — ссылка на MR с фиксом и запись в постмортеме: «причина — отсутствие graceful shutdown, обнаруживается только под нагрузкой, добавлен шаг load-check в чек-лист релиза».

### Что изменилось на стенде

- Новый namespace `vektor-track` в кластере; Secret `track-db`.
- Установлен Argo Rollouts в namespace `argo-rollouts` + плагин kubectl.
- Rollout `track-api` (4 реплики, canary 10/30/50), Service `track-api`, `track-api-canary`, Ingress `track-api` на `track.corp.vektor.lab`, PDB `minAvailable: 2`.
- Образы в Harbor: `vektor/track-api:1.0.0`, `1.1.0`.
- nginx на WEB01 проксирует портал на `10.10.10.62:30080` вместо монолита.
- Сервис `vektor-track` на WEB01 остановлен и убран из автозапуска (но не удалён).
- PostgreSQL остаётся на WEB01 `127.0.0.1:5432`, доступ для подов через 10.10.20.10.

### Откат

1. На WEB01 вернуть nginx на монолит: раскомментировать `10.10.20.10:8000`, `sudo nginx -t && sudo systemctl reload nginx`, затем `sudo systemctl enable --now vektor-track`.
2. В VMware Workstation: правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-121` → **Restore**. Сделать на всех ВМ, где снимал.
3. Если Rollouts ставился впервые и не нужен — `kubectl delete namespace argo-rollouts`, `kubectl delete namespace vektor-track`.

## 6. Ошибки новичков и безопасность

1. **`livenessProbe` проверяет БД** → при недоступности базы Kubernetes перезапускает все поды по кругу, превращая деградацию в полный отказ. Правильно: живость — «процесс отвечает», готовность — «могу обслуживать».
2. **Нет `preStop` и graceful shutdown** → 1–3% 502 при каждом релизе, которые никто не замечает, кроме бухгалтерии. Правильно: `sleep` в preStop + `--graceful-timeout` < `terminationGracePeriodSeconds`.
3. **`maxUnavailable: 1` при `replicas: 1`** → простой гарантирован. Правильно: минимум 3 реплики, `maxUnavailable: 0`.
4. **Образ `:latest`** → невозможно откатиться и непонятно, что работает. Правильно: иммутабельные теги по версии/хешу коммита.
5. **Контейнер под root и с `readOnlyRootFilesystem: false`** → компрометация приложения = компрометация узла. Правильно: `runAsNonRoot`, `drop: [ALL]`, read-only корень, запись только в `emptyDir`.
6. **Canary без анализа** → это просто медленный rolling update. Правильно: AnalysisTemplate по success rate/latency, `failureLimit: 1`.
7. **Несовместимое изменение схемы БД вместе с релизом** → канареечные поды падают, откат уже не поможет (колонку-то удалили). Правильно: expand/contract.
8. **Канарейка «на глазок» по логам** → пропускаешь регресс, который виден только в метриках (например, p99 latency вырос в 3 раза при нулевом 5xx).

**Безопасность.** В боевой среде нельзя: держать пароль БД в `Secret` без шифрования и без Vault (см. урок 120); оставлять монолит запущенным «на всякий случай» после переключения — забытый сервис с устаревшими зависимостями это дыра; открывать PostgreSQL из кластера в DMZ без NetworkPolicy и без TLS; давать CI-раннеру `cluster-admin`. В лабе пароль `Lab#2026-vektor` лежит в открытом манифесте — это допустимо только на стенде.

## 7. Вопросы на собеседовании

**1. «Как добиться zero-downtime при деплое в Kubernetes?»**
Ответ: минимум 3 реплики, `maxUnavailable: 0`/`maxSurge: 1`, readinessProbe по реальной готовности, startupProbe для медленного старта, `preStop` с задержкой, graceful shutdown по SIGTERM, `terminationGracePeriodSeconds` больше времени дренажа, PDB, реплики на разных узлах. Плюс проверка под нагрузкой, а не «на глаз».
*Что хочет услышать интервьюер:* что ты знаешь про гонку между удалением пода из Endpoints и SIGTERM — это главный маркер.
*Красный флаг:* «поставим replicas: 2 и всё будет ок» без единого слова про probes и graceful shutdown.

**2. «Чем canary отличается от blue-green и rolling update, когда что применять?»**
Ответ: rolling — про доступность, трафик перетекает по мере готовности подов, откат медленный; blue-green — два окружения, переключение мгновенное, откат тоже, но двойная стоимость ресурсов; canary — часть трафика на новую версию с автоматическим анализом метрик, ловит регрессы качества, но требует совместимости с БД и хороших метрик. Canary — для высоконагруженных сервисов с SLO, blue-green — для редких релизов и быстрого отката, rolling — по умолчанию.
*Что хочет услышать интервьюер:* привязку стратегии к риску и к зрелости наблюдаемости.
*Красный флаг:* «canary всегда лучше» без упоминания анализа и БД.

**3. «Как канареечный релиз уживается с изменениями схемы БД?»**
Ответ: только через обратно-совместимые изменения — expand/contract. Расширение схемы отдельным релизом (новая колонка nullable, новая таблица), код читает/пишет в оба варианта, затем переключение трафика, потом отдельным релизом удаление старого. Миграции — отдельный Job/шаг пайплайна, а не `initContainer` в каждом поде (иначе N подов гоняют одну миграцию).
*Что хочет услышать интервьюер:* понимание, что канарейка не спасает от несовместимой схемы.
*Красный флаг:* «накатим миграцию при старте приложения, Alembic сам разберётся».

**4. «Как бы ты мигрировал монолит в контейнеры?»**
Ответ: инвентаризация (порты, зависимости, состояние на диске), выделение stateless-части, контейнеризация без изменения кода, запуск рядом по паттерну Strangler Fig, переключение трафика на прокси постепенно, вынос состояния (БД, файлы) отдельно, потом декомпозиция на сервисы по бизнес-границам. На каждом шаге — метрики и путь отката.
*Что хочет услышать интервьюер:* поэтапность и наличие плана отката, а не «перепишем на микросервисы».
*Красный флаг:* «монолит всегда плохо, надо сразу разбить на 20 сервисов».

**5. «Что делать, если канареечный анализ даёт ложные срабатывания?»**
Ответ: смотреть на объём выборки (при малом трафике 30 секунд окна недостаточно — нужен `count`/`interval` больше или переключение по подам), на чувствительность порога (0.99 на 10 запросах бессмысленно), на метрики-источники (сравнивать canary с baseline, а не с абсолютом), на «шумные» периоды (бэкапы, batch-джобы). Ослабить порог можно только после того, как понял причину.
*Что хочет услышать интервьюер:* системный подход к метрикам и понимание статистической погрешности.
*Красный флаг:* «просто отключу анализ, он мешает катить».

## 8. Источники (официальная документация)

- **kubernetes.io** — разделы «Deployments» (RollingUpdate, maxUnavailable/maxSurge), «Configure Liveness, Readiness and Startup Probes», «Pod Lifecycle» (termination, preStop), «Pod Disruption Budgets», «Topology Spread Constraints». Поиск: `kubernetes zero downtime rolling update probes`.
- **argo-rollouts.readthedocs.io** — «Canary Deployments», «Analysis & AnalysisTemplate», «Traffic Routing: NGINX». Поиск: `argo rollouts canary analysis prometheus`.
- **github.com/argoproj/argo-rollouts** — страница Installation (актуальный URL `install.yaml` и установка плагина kubectl) `[ПРОВЕРИТЬ]`.
- **prometheus.io** — «Querying basics», функции `rate`, `sum`. Поиск: `promql rate success rate query example`.
- **martinfowler.com** — статья «StranglerFigApplication». Поиск: `strangler fig application martin fowler`.
- **12factor.net** — «The Twelve-Factor App» (разделы Config, Logs, Disposability — про graceful shutdown). Поиск: `twelve factor app disposability`.
- **docs.docker.com** — «Multi-stage builds», «Dockerfile reference» (`USER`, `COPY --chown`). Поиск: `dockerfile multi-stage build slim`.
- **docs.gunicorn.org** — «Signals» / `graceful-timeout`, `--workers`. Поиск: `gunicorn graceful shutdown timeout SIGTERM`.
- **kubernetes.github.io/ingress-nginx** — «Annotations» (canary-аннотации). Поиск: `nginx ingress canary annotations`.
- **goharbor.io** — «Working with images», проекты и права. Поиск: `harbor project robot account pull`.
