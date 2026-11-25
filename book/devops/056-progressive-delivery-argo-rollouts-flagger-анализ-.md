# Progressive Delivery: Argo Rollouts, Flagger, анализ метрик при выкатке

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №DEV-2261**

> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01 / **Что написал:** «Открываю портал заявок — то работает нормально, то ошибка 502, и каждый раз разный дизайн страницы. Уже третий раз за час. Коллеги говорят, у них то же самое» / **Срочность:** высокая, блокирует закрытие месяца.

Ты выкатил новую версию портала полчаса назад «просто обновив образ», и половина пользователей попала на кривой релиз. Классическая ситуация: Rolling update (урок 54) гарантирует, что поды поднимутся, но не гарантирует, что новое приложение **не отдаёт 500 на каждом втором запросе**. Kubernetes не умеет смотреть на графики — он смотрит только на liveness/readiness.

Progressive Delivery закрывает этот разрыв: новая версия сначала получает маленькую долю трафика, автоматика снимает метрики (ошибки, задержки, бизнес-показатели), и если они выходят за порог — трафик возвращается на старую версию **без участия человека и без звонка от бухгалтерии**. После главы ты должен уметь: установить Argo Rollouts или Flagger в кластер стенда, описать стратегию канарейки как код в Git, написать AnalysisTemplate с PromQL, объяснить, чем `failureLimit` отличается от `consecutiveErrorLimit`, и почему `analysis` без правильного селектора метрик превращается в фикцию.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Progressive Delivery** = канареечный/поэтапный деплой + **автоматический анализ метрик** + автоматический откат. От просто «canary» (урок 54) отличается тем, что решение о продвижении принимает не человек, а контроллер по данным из системы мониторинга.

Два инструмента решают одну задачу с разных сторон:

| | Argo Rollouts | Flagger |
|---|---|---|
| Модель | Заменяет `Deployment` своим CRD `Rollout` | Работает **поверх существующего** `Deployment` |
| CRD | `Rollout`, `AnalysisTemplate`, `ClusterAnalysisTemplate`, `AnalysisRun`, `Experiment` | `Canary`, `MetricTemplate`, `AlertProvider` |
| Провайдеры метрик | Prometheus, Datadog, New Relic, Wavefront, CloudWatch, Graphite, InfluxDB, Kayenta, `web`, `job`, плагины | Prometheus, Datadog, Graphite, InfluxDB, New Relic, CloudWatch, Dynatrace, Stackdriver [ПРОВЕРИТЬ список] |
| Разделение трафика | `trafficRouting`: NGINX Ingress, Istio, Gateway API, ALB, Traefik, App Mesh, Ambassador, Apisix | Только через провайдер трафика: Istio, Linkerd, NGINX Ingress, Gateway API, Gloo, Traefik, Kuma, App Mesh, SMI [ПРОВЕРИТЬ актуальный список] |
| GUI | `kubectl argo rollouts dashboard` | нет, только Prometheus/Grafana и события |

Ключевая архитектурная деталь: **без `trafficRouting` «канарейка» работает по количеству подов**, а не по весам трафика. 1 под из 4 = примерно 25% запросов, но точность зависит от балансировки Service. Если нужен честный вес 5% — нужен mesh или ingress-контроллер.

Схема цикла анализа:

```text
       git push (новый образ)
                │
                ▼
   ┌────────────────────────────┐
   │  Rollout controller        │  ← следит за CRD Rollout
   │  (ns argo-rollouts)        │
   └───────┬────────────┬───────┘
           │            │
   создаёт │            │ создаёт
           ▼            ▼
  ReplicaSet       AnalysisRun ──HTTP──► Prometheus :9090
  (canary, 1 под)         │                    │
                          │            PromQL: error rate,
                          │            p95 latency, RPS
                          ▼                    │
              result >= 0.95 ? ────────────────┘
                   │да          │нет
                   ▼            ▼
            setWeight 50    abort → откат
                            на stable RS
```

Порты и уровни: Prometheus опрашивает приложения по HTTP (L7), kubelet — по 10250, API-сервер — 6443, метрики самого контроллера Argo Rollouts — 8090, dashboard — 3100 [ПРОВЕРИТЬ]. Всё, что мы анализируем, лежит на L7 — поэтому метрики есть только у HTTP/gRPC-сервисов или у экспортёров.

Как считается результат: `AnalysisRun` каждые `interval` выполняет запрос, берёт первый элемент вектора (`result[0]`) и сравнивает с `successCondition`. Если условие не выполнено — инкремент `failureCount`; при `failureCount > failureLimit` весь rollout **абортится** (трафик на stable). `inconclusiveLimit` — сколько раз можно получить «непонятно что» (пустой ответ, NaN), `consecutiveErrorLimit` — сколько подряд ошибок запроса к Prometheus терпим.

## 3. Аналогия из жизни

Пекарня выпускает новый сорт хлеба. Директор не меняет весь ассортимент сразу: печёт **пробную партию** (10% полки), ставит рядом дегустатора с чек-листом («не меньше 90% покупателей берут повторно, жалоб не больше двух»), и только если цифры сходятся — расширяет партию до 50%, потом до 100%. Если дегустатор фиксирует массовые жалобы — пробная партия снимается с полки за пять минут, старая рецептура продолжает работать.

Аналогия ломается в одном месте: живой дегустатор может заметить «что-то не то» интуитивно, а AnalysisTemplate видит **только те метрики, которые ты ему дал**, и слепо пропустит сломанную бизнес-логику, если она не влияет на код ответа и время отклика.

## 4. Практика и команды

**1. Установка контроллера Argo Rollouts**

```bash
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
kubectl -n argo-rollouts get pods
```

`install.yaml` содержит namespace-scoped RBAC, CRD и Deployment контроллера. Смотрим: под `argo-rollouts-*` в статусе `Running`, `READY 1/1`.

```text
NAME                             READY   STATUS    RESTARTS
argo-rollouts-6b7f9c8d4f-x2kzp   1/1     Running   0
```

**2. Плагин kubectl (ставим на k8s-cp01)**

```bash
curl -LO https://github.com/argoproj/argo-rollouts/releases/latest/download/kubectl-argo-rollouts-linux-amd64
chmod +x kubectl-argo-rollouts-linux-amd64
sudo mv kubectl-argo-rollouts-linux-amd64 /usr/local/bin/kubectl-argo-rollouts
kubectl argo rollouts version
```

Если из РФ GitHub отдаёт медленно — качаем через зеркало или с GIT01, где уже есть нужные пакеты.

**3. Манифест Rollout со стратегией canary**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: demo
  namespace: demo
spec:
  replicas: 4
  revisionHistoryLimit: 3
  selector:
    matchLabels: {app: demo}
  template:
    metadata:
      labels: {app: demo}
    spec:
      containers:
        - name: demo
          image: 10.10.10.55/library/rollouts-demo:blue
          ports: [{containerPort: 8080}]
          resources:
            requests: {cpu: 50m, memory: 64Mi}
            limits: {cpu: 200m, memory: 128Mi}
  strategy:
    canary:
      steps:
        - setWeight: 25
        - pause: {duration: 2m}
        - analysis:
            templates: [{templateName: success-rate}]
        - setWeight: 50
        - pause: {duration: 5m}
        - setWeight: 100
```

`setWeight` — доля подов canary (без `trafficRouting`), `pause` — пауза, `analysis` — запуск проверки. `revisionHistoryLimit` ограничивает число ReplicaSet, иначе кластер зарастёт.

**4. Наблюдение за выкаткой**

```bash
kubectl argo rollouts get rollout demo -n demo --watch
```

```text
Name:            demo
Namespace:       demo
Status:          ॥ Paused
Strategy:        Canary
  Step:          2/6
  SetWeight:     25
  ActualWeight:  25
Images:          rollouts-demo:blue (stable)
                 rollouts-demo:green (canary)
Replicas:
  Desired: 4  Updated: 1  Ready: 4  Available: 4
```

Смотрим `Step`, `SetWeight`/`ActualWeight` и статус `Healthy → Paused → Progressing`.

**5. Обновление образа и ручное управление**

```bash
kubectl argo rollouts set image demo demo=10.10.10.55/library/rollouts-demo:green -n demo
kubectl argo rollouts promote demo -n demo     # пройти паузу досрочно
kubectl argo rollouts abort demo -n demo       # откат на stable
kubectl argo rollouts undo demo -n demo        # вернуть предыдущую ревизию
kubectl argo rollouts retry rollout demo -n demo
```

`abort` мгновенно переключает Service на stable ReplicaSet; `retry` перезапускает аборченный rollout после фикса.

**6. AnalysisTemplate с Prometheus**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: success-rate
  namespace: demo
spec:
  metrics:
    - name: success-rate
      interval: 1m
      count: 5
      failureLimit: 1
      inconclusiveLimit: 3
      successCondition: result[0] >= 0.95
      failureCondition: result[0] < 0.90
      provider:
        prometheus:
          address: http://prometheus.monitoring.svc.cluster.local:9090
          query: |
            sum(rate(http_requests_total{job="demo", code!~"5.."}[2m]))
            /
            clamp_min(sum(rate(http_requests_total{job="demo"}[2m])), 0.001)
```

`count: 5` × `interval: 1m` = 5 минут анализа. `clamp_min(..., 0.001)` защищает от деления на ноль при отсутствии трафика. Имя метрики `http_requests_total` — **[ПРОВЕРИТЬ]** под свою сборку: сначала посмотри реальные имена.

**7. Проверка, что вообще отдаёт приложение**

```bash
kubectl -n demo port-forward pod/<pod> 8080:8080
curl -s localhost:8080/metrics | head -30
```

Ищем `# TYPE` строки — это и есть доступные имена и лейблы. Пока не увидел их своими глазами, PromQL писать бессмысленно.

**8. Прямой запрос к Prometheus API**

```bash
kubectl -n monitoring port-forward svc/prometheus-server 9090:80
curl -sG http://localhost:9090/api/v1/query \
  --data-urlencode 'query=sum(rate(http_requests_total{job="demo"}[2m]))'
```

Ответ `{"status":"success","data":{"result":[...]}}`. Если `result: []` — селектор не совпал, и AnalysisRun уйдёт в `Inconclusive`.

**9. Разбор AnalysisRun**

```bash
kubectl -n demo get analysisrun
kubectl -n demo describe analysisrun demo-<hash>-success-rate-<hash>
```

Смотрим `Phase: Successful | Failed | Inconclusive` и `Message` с фактическим значением `result[0]` — это главный источник правды при разборе «почему откатилось».

**10. Dashboard**

```bash
kubectl argo rollouts dashboard
```

Открывается на `localhost:3100`, показывает шаги, веса, графики AnalysisRun. Только для чтения, в прод-контуре обычно закрыт.

**11. Flagger: установка и Canary**

```bash
helm repo add flagger https://flagger.app
helm upgrade -i flagger flagger/flagger -n ingress-nginx \
  --set crd.create=true --set meshProvider=nginx
kubectl get canary -A
```

Флаг `meshProvider` указывает провайдер трафика, через который Flagger будет менять веса. Без провайдера Flagger работать не умеет — это его принципиальное отличие от Argo Rollouts.

```yaml
apiVersion: flagger.app/v1beta1
kind: Canary
metadata: {name: demo, namespace: demo}
spec:
  targetRef: {apiVersion: apps/v1, kind: Deployment, name: demo}
  service: {port: 80, targetPort: 8080}
  analysis:
    interval: 1m
    threshold: 5          # сколько провалов до отката
    maxWeight: 50
    stepWeight: 10
    metrics:
      - name: error-rate
        templateRef: {name: error-rate, namespace: demo}
        thresholdRange: {max: 1}   # [ПРОВЕРИТЬ] в старых версиях — threshold: 1
        interval: 1m
```

**12. MetricTemplate для Flagger**

```yaml
apiVersion: flagger.app/v1beta1
kind: MetricTemplate
metadata: {name: error-rate, namespace: demo}
spec:
  provider:
    type: prometheus
    address: http://prometheus.monitoring.svc.cluster.local:9090
  query: |
    100 - (
      sum(rate(http_requests_total{job="demo", code!~"5.."}[1m]))
      /
      clamp_min(sum(rate(http_requests_total{job="demo"}[1m])), 0.001)
    ) * 100
```

Flagger сравнивает результат с `thresholdRange` (в процентах ошибок), а не с долей успеха — легко перепутать знак и «заваливать» хорошие релизы.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер k8s-cp01 (10.10.10.61), k8s-w01 (10.10.10.62), k8s-w02 (10.10.10.63) — поднят, `kubectl get nodes` показывает 3 узла `Ready`.
- GIT01 (10.10.10.50) — репозиторий `vektor/k8s-demo`, куда мы коммитим манифесты (логика из урока 55).
- HARBOR01 (10.10.10.55) — образы тянем оттуда; если Harbor настроен как pull-through cache Docker Hub, используй его FQDN из своей конфигурации.
- Prometheus в кластере **отсутствует** — ставим его на шаге 4 (мониторинг на MON01 смотрит на ВМ, а не внутрь кластера, для k8s-метрик он не подходит).
- Ресурсы: минимум 2 vCPU / 4 ГБ RAM на k8s-w01 и k8s-w02, иначе Prometheus не влезет.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-devops-056`. Снимаем на k8s-cp01, k8s-w01, k8s-w02 и GIT01. Для кластера снапшоты делаем практически одновременно (проще всего — при остановленных ВМ либо заранее приняв, что etcd после отката может потребовать восстановления из бэкапа).

### Шаги

1. **k8s-cp01.** `kubectl get nodes -o wide` и `kubectl get pods -A | grep -v Running`. Ожидаем 3 узла `Ready` и отсутствие падающих подов. Если что-то не так — чиним до начала.
2. **k8s-cp01.** Устанавливаем контроллер: `kubectl create namespace argo-rollouts` + `kubectl apply -n argo-rollouts -f .../install.yaml`. Ожидаем под `argo-rollouts` в `Running`.
3. **k8s-cp01.** Ставим плагин `kubectl-argo-rollouts` (команды из п.4.2), проверяем `kubectl argo rollouts version`. Ожидаем вывод версии без ошибки «unknown command».
4. **k8s-cp01.** Ставим Prometheus: `helm repo add prometheus-community https://prometheus-community.github.io/helm-charts`, затем `helm upgrade -i prometheus prometheus-community/prometheus -n monitoring --create-namespace --set alertmanager.enabled=false --set server.persistentVolume.enabled=false`. Ожидаем `kubectl -n monitoring get pods` → `prometheus-server` в `Running`.
5. **k8s-cp01.** Создаём `demo` namespace и деплоим демо-приложение обычным Deployment с 2 репликами. Ждём `Available`. Проверяем `curl` до сервиса изнутри кластера (`kubectl run tmp --rm -it --image=curlimages/curl -- curl http://demo.demo.svc`). Ожидаем HTTP 200 и цветную страницу.
6. **k8s-cp01.** Смотрим метрики: `kubectl -n demo port-forward pod/<pod> 8080:8080` и `curl -s localhost:8080/metrics`. Записываем реальные имена метрик и лейблов в `stand-log.md`. **Ожидаемый результат:** непустой список `# TYPE`.
7. **k8s-cp01.** Настраиваем скрейпинг: добавляем в Helm-values Prometheus `extraScrapeConfigs` с job `demo`, указывающим на поды по Service-дискавери, либо ставим ServiceMonitor, если используешь kube-prometheus-stack. Проверяем в UI Prometheus (`port-forward` на 9090) наличие target `demo` в статусе `UP`.
8. **k8s-cp01.** Пишем `AnalysisTemplate success-rate` (п.4.6) с **реальными** именами метрик из шага 6, коммитим в `vektor/k8s-demo` вместе с `Rollout`. `kubectl apply -f`. Ожидаем `kubectl -n demo get rollout` → `demo`.
9. **k8s-cp01.** Применяем Rollout (он заменит Deployment — удали старый Deployment, иначе два контроллера будут драться за одни поды). Ожидаем `kubectl argo rollouts get rollout demo -n demo` → `Status: ✔ Healthy`.
10. **k8s-cp01.** Запускаем выкатку: `kubectl argo rollouts set image demo demo=<registry>/rollouts-demo:green -n demo`, затем `kubectl argo rollouts get rollout demo -n demo --watch`. Ожидаем: `Step 1/6`, `SetWeight 25`, затем `Paused` на время анализа.
11. **k8s-cp01.** Во втором терминале: `kubectl -n demo get analysisrun -w`. Ожидаем появление AnalysisRun и смену `Phase` на `Successful` через ~5 минут, после чего rollout идёт дальше.
12. **k8s-cp01.** Проверяем откат: `kubectl argo rollouts abort demo -n demo`. Ожидаем `Status: ✖ Degraded`, `ActualWeight: 0`, все поды — старая версия `blue`.
13. **k8s-cp01.** Дописываем изменения в `stand-log.md` и коммитим все манифесты в GIT01.

### Проверка

- [ ] `kubectl -n argo-rollouts get pods` — контроллер `Running`.
- [ ] `kubectl argo rollouts version` — версия выводится.
- [ ] `kubectl -n monitoring get pods` — `prometheus-server` `Running`, target `demo` в статусе `UP`.
- [ ] `kubectl -n demo get analysisrun` — есть хотя бы один AnalysisRun с `Phase: Successful`.
- [ ] В `curl -s localhost:8080/metrics` видны реальные имена метрик, и они совпадают с PromQL в AnalysisTemplate.
- [ ] `kubectl argo rollouts get rollout demo -n demo` после `set image` показывает `Step 2/6` и `SetWeight: 25`.
- [ ] После `abort` все поды — предыдущей ревизии, `ActualWeight: 0`.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01 / **Что написал:** «Портал заявок опять моргает: то новая страница, то старая. И на новой половина кнопок отдаёт 502» / **Срочность:** высокая.
> *Если Windows-части стенда нет — заявка приходит от коллеги в мессенджере, суть та же.*

**Как воспроизвести поломку.** В AnalysisTemplate поменяй `job="demo"` на несуществующий `job="demo-app"`, закоммить и выкати новую версию:

```bash
kubectl argo rollouts set image demo demo=<registry>/rollouts-demo:orange -n demo
kubectl -n demo get analysisrun
```

**Диагностика по шагам.**

1. `kubectl argo rollouts get rollout demo -n demo` — видим, что rollout застрял на `Step 2/6`, `Status: ॥ Paused`.
2. `kubectl -n demo get analysisrun` — фаза `Inconclusive` или `Error`, не `Failed`. Значит, дело не в приложении, а в запросе.
3. `kubectl -n demo describe analysisrun <name>` — в `Message` будет `no data` / `empty result` и текст запроса.
4. `kubectl -n monitoring port-forward svc/prometheus-server 9090:80` и в UI Prometheus → Graph выполняем тот же PromQL. Пусто → селектор неверный.
5. Проверяем реальные лейблы: `curl -sG localhost:9090/api/v1/query --data-urlencode 'query=count by (job) ({__name__=~".*_total",namespace="demo"})'` — видим фактическое имя job.
6. Смотрим `inconclusiveLimit` в AnalysisTemplate: пока счётчик не исчерпан, rollout будет висеть, а не откатываться. Это опасное состояние — трафик уже частично на канарейке.

**Решение.** Правим `job` на фактический, добавляем защиту от пустого результата (`or vector(0)` или явный `failureCondition` на отсутствие данных), коммитим, применяем:

```bash
kubectl apply -f analysis-template.yaml
kubectl argo rollouts abort demo -n demo
kubectl argo rollouts retry rollout demo -n demo
```

**Ответ пользователю:** «Иван Иванов, проблема подтверждена: новая версия портала не прошла автоматическую проверку и была откачена на стабильную. Сейчас у всех одна и та же версия, 502 быть не должно. Проверьте, пожалуйста, Ctrl+F5. Если повторится — напишите время и что именно нажимали».

**Закрытие заявки:** категория «релиз/деплой», причина «некорректный селектор метрик в AnalysisTemplate → анализ не смог подтвердить качество», действие «исправлен PromQL, добавлена защита от пустых данных», в `stand-log.md` — запись про фикс.

### Что изменилось на стенде

- **k8s-cp01/w01/w02:** namespace `argo-rollouts` (контроллер + CRD), namespace `monitoring` (Prometheus), namespace `demo` (Rollout `demo`, AnalysisTemplate `success-rate`, AnalysisRun'ы).
- **k8s-cp01:** установлен бинарь `/usr/local/bin/kubectl-argo-rollouts`.
- **GIT01:** репозиторий `vektor/k8s-demo` содержит `rollout.yaml`, `analysis-template.yaml`, `service.yaml`.
- **HARBOR01:** используется как источник образов (при необходимости — pull-through cache).
- ArgoCD из урока 55 теперь синхронизирует `Rollout` вместо `Deployment` в `demo`.

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-056` → **Restore**. Восстанавливаем k8s-w01, k8s-w02 и k8s-cp01 (все три — иначе etcd и состояние подов разойдутся). После загрузки: `kubectl get nodes`, `kubectl get pods -A`. Если etcd не поднялся — восстанавливаем кластер по методике из главы про установку (kubeadm reset + повторный join), это быстрее, чем чинить рассинхрон.

## 6. Ошибки новичков и безопасность

1. **Ошибка:** `interval: 10s`, `count: 3` → **последствия:** окно анализа 30 секунд, Prometheus ещё не успел собрать данные, откат «на всякий случай» на ровном месте → **правильно:** окно анализа ≥ 3–5 минут, `interval` кратен scrape-интервалу Prometheus.
2. **Ошибка:** метрика без лейбла версии (считаем общий error rate по всем подам) → **последствия:** старая версия «разбавляет» ошибки новой, плохой релиз проходит → **правильно:** считать метрики отдельно по `canary`-подам или по `destination_workload`, либо использовать mesh-метрики с разделением по `reporter`.
3. **Ошибка:** нет защиты от деления на ноль и пустого ответа → **последствия:** `NaN` трактуется как inconclusive, rollout висит вечно, кластер в подвешенном состоянии → **правильно:** `clamp_min(..., 0.001)` + явный `failureCondition` на отсутствие данных + разумный `inconclusiveLimit`.
4. **Ошибка:** `failureLimit: 10` «чтобы не падало зря» → **последствия:** анализ фактически отключён, решение об откате никогда не принимается → **правильно:** 0–1 при коротком окне, и всегда есть `failureCondition`.
5. **Ошибка:** удалили `Deployment` и оставили `Rollout` с тем же Service без `selector` → **последствия:** 502 у всех пользователей, Service без эндпоинтов → **правильно:** сначала готовим Service с общими лейблами, потом переключаем контроллер, всё через Git.
6. **Ошибка:** `kubectl argo rollouts abort` забыли после разбора инцидента → **последствия:** следующий `set image` продолжит старую аборченную ревизию → **правильно:** после фикса `retry` или явный `undo` до чистой ревизии, состояние проверять в `stand-log.md`.
7. **Ошибка:** анализ только по CPU/памяти → **последствия:** утечка памяти не видна за 5 минут, а «зелёный» релиз ломает бизнес-логику → **правильно:** технические guardrail-метрики (error rate, p95) **плюс** хотя бы одна бизнес-метрика.

**Безопасность.** В боевой среде нельзя: держать dashboard Argo Rollouts открытым наружу (это полный контроль над деплоем без аутентификации); хранить `address` Prometheus и токены в открытом виде прямо в AnalysisTemplate — используй Secret и `valueFrom`; давать контроллеру `cluster-admin` (в `install.yaml` RBAC уже минимально необходимый). Также помни: `abort` — не «безопасная кнопка», он переключает прод-трафик; в зрелой практике такие действия идут через PR в Git, а не руками в кластер.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем Progressive Delivery отличается от канареечного деплоя, который мы обсуждали в стратегиях деплоя?**

Ответ: канареечный деплой — это механизм **перераспределения трафика** между версиями. Progressive Delivery добавляет к нему автоматическую верификацию: контроллер сам снимает метрики с канареечной версии, сравнивает с порогами и принимает решение продвигать или откатывать. То есть ключевое отличие — **автоматическое решение по данным, а не по расписанию или ручному подтверждению**. Плюс появляются артефакты: AnalysisRun с историей проверок, аудит «почему откатились».

*Что хочет услышать интервьюер:* что кандидат понимает разницу между «механикой трафика» и «контуром принятия решения», знает про метрики и автоматический откат.
*Красный флаг:* «это то же самое, просто модное слово».

**Вопрос 2. Как Argo Rollouts решает, что пора откатываться? Что произойдёт, если Prometheus недоступен?**

Ответ: AnalysisRun выполняет запрос каждые `interval`, всего `count` раз. Результат сравнивается с `successCondition`; при непопадании растёт `failureCount`, при `failureCount > failureLimit` rollout абортится и трафик уходит на stable. Если запрос к Prometheus падает с ошибкой — растёт `consecutiveErrorCount` (порог `consecutiveErrorLimit`), а если ответ пустой/непарсируемый — `inconclusiveCount` (`inconclusiveLimit`). Исчерпание любого из лимитов ведёт к аборту; недоступность Prometheus — это тоже повод не выкатывать, а не повод пропустить.

*Что хочет услышать интервьюер:* различение `failureLimit`, `inconclusiveLimit` и `consecutiveErrorLimit`, понимание fail-safe поведения.
*Красный флаг:* «при недоступности мониторинга выкатка продолжится, чтобы не блокировать релизы».

**Вопрос 3. Argo Rollouts или Flagger — что выберете и почему?**

Ответ: зависит от того, есть ли уже mesh/ingress и сколько свободы в манифестах. Flagger не требует менять `Deployment`, он работает поверх и хорошо ложится в уже существующий GitOps-репозиторий с обычными Deployment'ами; но он **обязательно** требует провайдера трафика (Istio, Linkerd, NGINX Ingress и т.п.) и его логика анализа завязана на цикл Canary. Argo Rollouts — самодостаточный контроллер с CRD `Rollout`, богаче по шагам/экспериментам, есть dashboard и удобный kubectl-плагин, умеет работать даже без mesh (по количеству подов). Если проект с нуля и хочется максимум контроля над шагами — Rollouts; если уже есть Istio и не хочется трогать манифесты приложений — Flagger.

*Что хочет услышать интервьюер:* что выбор обоснован инфраструктурой, а не вкусом; знание, что Flagger без провайдера трафика не работает.
*Красный флаг:* «Flagger лучше, потому что он от Flux».

**Вопрос 4. Какие метрики вы возьмёте для анализа канарейки и что делать при маленьком трафике?**

Ответ: технические — доля 5xx (error rate), p95/p99 latency, saturation (CPU/throttling), доля рестартов; бизнесовые — конверсия, число заказов/платежей в минуту, число успешных вызовов внешнего API. При малом трафике абсолютные счётчики бесполезны: используем относительные доли за окно, увеличиваем окно анализа, добавляем синтетический load-test webhook (у Flagger — loadtester, у Rollouts — `job`-провайдер), и обязательно ставим минимальный порог по числу запросов: если `rate < N`, анализ должен быть `Inconclusive`, а не «успех».

*Что хочет услышать интервьюер:* понимание статистической незначимости, наличие guardrail- и бизнес-метрик, идея синтетической нагрузки.
*Красный флаг:* «достаточно смотреть на CPU и статус подов».

**Вопрос 5. У приложения нет метрик и Prometheus нет. Как построить Progressive Delivery?**

Ответ: три варианта. Первый — провайдер `web` в Argo Rollouts: контроллер сам дергает HTTP-эндпоинт, проверяет код ответа и вытаскивает значение по JSONPath; годится для health- и smoke-проверок. Второй — `job`-провайдер: запускаем Job с интеграционным/e2e-тестом против канареечного Service и смотрим на код завершения. Третий — **сначала закрыть наблюдаемость**: поднять Prometheus + экспортёры (nginx-prometheus-exporter, blackbox-exporter) и только потом включать автоматический анализ. Правильный ответ — третий, но с оговоркой, что первые два позволяют стартовать уже сегодня.

*Что хочет услышать интервьюер:* что кандидат не блокируется от отсутствия инструмента, но и не строит канарейку на одном `curl /healthz`.
*Красный флаг:* «без метрик progressive delivery невозможен, ждём три месяца».

## 8. Источники (официальная документация)

- **Argo Rollouts** — `argo-rollouts.readthedocs.io` (также `argoproj.github.io`). Разделы: *Getting Started*, *Analysis & AnalysisTemplate*, *Traffic Management*, *Canary Deployments*. Поиск: `argo rollouts analysis template prometheus`.
- **Flagger** — `docs.flagger.app`. Разделы: *How it works*, *Canary analysis*, *MetricTemplate*, *Deployment strategies*. Поиск: `flagger canary metric template prometheus thresholdRange`.
- **Prometheus** — `prometheus.io`. Разделы: *Querying basics*, *HTTP API*, *Configuration*. Поиск: `prometheus http api instant query curl`.
- **Kubernetes** — `kubernetes.io`. Раздел: *Custom Resources / Custom Resource Definitions*, *Deployments*. Поиск: `kubernetes custom resource definition concepts`.
- **Helm** — `helm.sh`. Раздел: *Using Helm*, `values` override. Поиск: `helm upgrade install set values namespace`.
- **NGINX Ingress Controller** — `kubernetes.github.io` (документация ingress-nginx). Раздел: *Canary Deployments (Annotations)*. Поиск: `nginx ingress canary annotations weight`. [ПРОВЕРИТЬ актуальный домен документации ingress-nginx]
- **Grafana / визуализация AnalysisRun** — `grafana.com`. Раздел: *Prometheus data source*. Поиск: `grafana dashboard argo rollouts analysisrun`.
