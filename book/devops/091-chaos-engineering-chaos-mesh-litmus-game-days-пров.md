# Chaos Engineering: Chaos Mesh, Litmus, game days, проверка отказоустойчивости

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Портал заявок открывается через раз, при нажатии „Отправить“ вылетает 502. Работа стоит, у меня закрытие месяца».
> **Срочность:** высокая

Дальше выясняется классика: за неделю до этого кластер Kubernetes обновили, и один из подов приложения перестал подниматься за приемлемое время. Никто не проверил, что будет с сервисом, если один под умрёт. Формально всё «работало» — до первого реального сбоя.

Chaos Engineering (инженерия хаоса) — это дисциплина, в которой вы **намеренно ломаете** систему, чтобы узнать о её слабых местах раньше пользователей. Не «уронить и посмотреть», а провести управляемый эксперимент: сформулировать гипотезу о нормальном поведении, ограничить зону поражения, измерить результат, откатить.

После главы читатель должен уметь: поставить Chaos Mesh в кластер стенда, написать и применить манифест эксперимента (pod-kill, сетевая задержка, стресс CPU, DNS-сбой), сделать то же самое через Litmus, спланировать и провести game day (учения) с гипотезой, abort-условием и постмортемом, и не положить при этом прод.

## 2. Теория «под капотом»

**Chaos Engineering — это эксперимент, а не тест.** У него есть обязательные элементы:

- **Steady state** (устойчивое состояние) — измеримый показатель нормы: доля успешных запросов, p95 задержки, число рестартов. Не «мне кажется, всё ок», а число из метрики (SLI из урока 87).
- **Гипотеза** — «если убить один под из двух, доля успешных запросов останется ≥ 99%».
- **Blast radius** (зона поражения) — что максимум может пострадать: один namespace, один под, 5% трафика.
- **Abort-условие** — при каком показателе эксперимент немедленно прекращается.

Место в общем процессе: нагрузочное тестирование (урок 90) отвечает на «выдержит ли под нагрузкой», chaos — «выдержит ли при отказе компонента», DR (урок 92) — «как восстановиться после катастрофы». Chaos работает между ними.

### Как это устроено внутри

**Chaos Mesh** (CNCF-проект, Apache 2.0) состоит из:

- **chaos-controller-manager** — обычный Deployment с CRD-контроллерами. CRD (Custom Resource Definition) — расширение API Kubernetes, которое добавляет новые типы объектов: `PodChaos`, `NetworkChaos`, `StressChaos`, `IOChaos`, `TimeChaos`, `DNSChaos`, `HTTPChaos`, `Schedule`, `Workflow`. Вы пишете YAML, контроллер его читает и приводит кластер в «сломанное» состояние.
- **chaos-daemon** — DaemonSet (по поду на каждую ноду), privileged, с доступом к сокету container runtime. Именно он залезает в network namespace контейнера через `nsenter` и вешает правила `tc netem` для задержек/потерь пакетов, `iptables` для партиции, cgroup для стресса.
- **chaos-dashboard** — веб-интерфейс, по умолчанию сервис `chaos-mesh-dashboard` в namespace `chaos-mesh` [ПРОВЕРИТЬ порт/имя сервиса в вашей версии].
- **chaos-dns-server** — отдельный DNS, нужен только для `DNSChaos`; CoreDNS переключается на него.

**LitmusChaos** (тоже CNCF) устроен иначе: оператор запускает эксперимент как **отдельный под** с нужными правами (RBAC), а результат описывается объектами `ChaosEngine` / `ChaosExperiment`. Плюс Litmus — большая библиотека готовых экспериментов (pod-delete, container-kill, node-drain, disk-fill, network-loss) с **probes**: проверками состояния до/во время/после эксперимента (httpProbe, cmdProbe, promProbe, k8sProbe). Chaos Mesh удобнее в декларативном описании сложных сценариев на уровне ядра, Litmus — когда нужен «готовый эксперимент из каталога».

```text
                 ┌──────────────────────────────────────────┐
   вы (kubectl)  │  Chaos Mesh: controller-manager          │
   YAML/CRD  ───►│  читает PodChaos/NetworkChaos/StressChaos│
                 └───────────────┬──────────────────────────┘
                                 │ gRPC (по умолчанию 31767 [ПРОВЕРИТЬ])
                 ┌───────────────▼──────────────────────────┐
                 │  chaos-daemon (DaemonSet, privileged)    │
                 │  на каждой ноде                          │
                 └───┬──────────────┬───────────────┬───────┘
                     │ nsenter      │ tc netem      │ cgroup
                     ▼              ▼               ▼
              kill pod /      задержка,       стресс CPU/
              io delay        потери,         памяти поду
                              partition
   Слои OSI:  L7 (HTTPChaos) → L4/L3 (NetworkChaos) → процесс/под (PodChaos, StressChaos) →
              нода (PhysicalMachineChaos) → время (TimeChaos)
```

Наблюдаемость обязательна: без метрик (kube-state-metrics, cAdvisor, blackbox — уроки 84–85) эксперимент превращается в гадание.

**Game day** (день учений) — организационная надстройка: заранее объявленное окно, участники, гипотеза, чек-лист, наблюдатель, abort-условие, постмортем без поиска виноватых (формат — урок 88). В отличие от «постоянного хаоса» в проде (chaos monkey), game day — редкое контролируемое событие.

## 3. Аналогия из жизни

Представьте учебную пожарную тревогу в бизнес-центре: людей не выводят через горящий коридор, а перекрывают один пролёт, замеряют, за сколько все спустятся по лестнице, и смотрят, где образовалась пробка. После — разбирают: третья дверь была закрыта, её нужно разблокировать.

Где аналогия ломается: в учениях МЧС никто не поджигает реальный серверный зал, а в chaos engineering вы действительно убиваете реальный под — просто заранее знаете, что реплик две, и что вторую вы не тронете.

## 4. Практика и команды

Все команды — с UBU01 (10.10.10.30), где лежат `kubectl` и `helm`, настроенные на кластер `k8s-cp01`/`k8s-w01`/`k8s-w02`.

**1. Установка Chaos Mesh**

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update
helm install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock
```

`--set chaosDaemon.runtime` и `socketPath` — критично: daemon должен знать, где сокет containerd, иначе pod-kill работать не будет. Для Docker было бы `docker` и `/var/run/docker.sock`. Актуальную версию чарта смотрите `helm search repo chaos-mesh` [ПРОВЕРИТЬ актуальную версию и поддерживаемые версии Kubernetes].

Ожидаемый вывод (сокращённо):

```text
NAME: chaos-mesh
NAMESPACE: chaos-mesh
STATUS: deployed
```

**2. Проверка компонентов и CRD**

```bash
kubectl get pods -n chaos-mesh
kubectl get crd | grep chaos-mesh
```

```text
chaos-controller-manager-7d9f8c6b5-x2k4l   1/1   Running   0   3m
chaos-daemon-8hqbz                          1/1   Running   0   3m
chaos-daemon-p6m2t                          1/1   Running   0   3m
chaos-daemon-z9w7c                          1/1   Running   0   3m
chaos-dashboard-6b7c9f5d4-lq8xn             1/1   Running   0   3m
```

Смотрим: `chaos-daemon` ровно столько, сколько нод (3). Если 0 — проблема с privileged-политикой PodSecurity.

**3. Полигон: namespace и тестовое приложение**

```bash
kubectl create namespace chaos-lab
kubectl -n chaos-lab create deployment vektor-web \
  --image=10.10.10.55/library/nginx:stable --replicas=2
kubectl -n chaos-lab expose deployment vektor-web --port=80
kubectl -n chaos-lab get pods -o wide
```

Образ тянем из своего Harbor (HARBOR01, 10.10.10.55) — Docker Hub из РФ может быть недоступен [ПРОВЕРИТЬ путь проекта в вашем Harbor].

**4. PodChaos: убить под**

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: kill-one-web
  namespace: chaos-lab
spec:
  action: pod-kill
  mode: one
  selector:
    namespaces:
      - chaos-lab
    labelSelectors:
      app: vektor-web
  gracePeriod: 0
```

`action: pod-kill` — резкое удаление (аналог `kubectl delete pod --grace-period=0`); `pod-failure` — под «зависает» на `duration` и не перезапускается; `container-kill` — убивает конкретный контейнер. `mode: one` — только один под; варианты `all`, `fixed: N`, `fixed-percent: "50"`, `random-max-percent`. `gracePeriod: 0` эмулирует аварийное завершение без graceful shutdown.

```bash
kubectl apply -f kill-one-web.yaml
kubectl -n chaos-lab get podchaos
kubectl -n chaos-lab describe podchaos kill-one-web
```

```text
NAME            ACTION     MODE   AGE
kill-one-web    pod-kill   one    10s
```

В `describe` смотрите `Status.Experiment.DesiredPhase: Finished` — эксперимент одноразовый.

**5. NetworkChaos: задержка между сервисами**

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: delay-web
  namespace: chaos-lab
spec:
  action: delay
  mode: one
  selector:
    namespaces: [chaos-lab]
    labelSelectors:
      app: vektor-web
  delay:
    latency: "300ms"
    jitter: "50ms"
    correlation: "50"
  direction: both
  duration: "2m"
```

`latency`/`jitter`/`correlation` — параметры ядерного `tc netem`. `direction: to|from|both`. Обязательно указывайте `duration`, иначе правило останется навсегда (см. «Сломай и почини»). Другие action: `loss`, `duplicate`, `corrupt`, `partition` (полная изоляция — база для уроков 93 про кворум и split-brain), `bandwidth`.

**6. StressChaos: давление на CPU и память**

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: StressChaos
metadata:
  name: cpu-stress-web
  namespace: chaos-lab
spec:
  mode: one
  selector:
    namespaces: [chaos-lab]
    labelSelectors: {app: vektor-web}
  stressors:
    cpu:
      workers: 2
      load: 80
    memory:
      workers: 1
      size: "128MB"
  duration: "60s"
```

`workers` — число процессов-стрессоров, `load` — процент загрузки одного ядра. Начинайте с `mode: one`, чтобы не выесть ноду целиком.

**7. DNSChaos: сервис «не находит» соседа**

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: DNSChaos
metadata:
  name: dns-error
  namespace: chaos-lab
spec:
  action: error          # или random
  mode: all
  patterns: ["vektor-api.chaos-lab.svc.cluster.local"]
  selector:
    namespaces: [chaos-lab]
  duration: "1m"
```

Требует установки с DNS-сервером (`--set dnsServer.create=true`) и переключения CoreDNS [ПРОВЕРИТЬ в документации вашей версии].

**8. Schedule: регулярный хаос по расписанию**

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: Schedule
metadata:
  name: kill-every-10m
  namespace: chaos-lab
spec:
  schedule: "@every 10m"
  historyLimit: 3
  concurrencyPolicy: Forbid
  type: PodChaos
  podChaos:
    action: pod-kill
    mode: one
    selector:
      namespaces: [chaos-lab]
      labelSelectors: {app: vektor-web}
```

`concurrencyPolicy: Forbid` не даёт запустить новый эксперимент, пока не закончился предыдущий. Это и есть «обезьяна» в стиле chaos monkey, но с ограничением по namespace.

**9. Пауза всех экспериментов**

```bash
kubectl annotate networkchaos delay-web -n chaos-lab \
  experiment.chaos-mesh.org/pause=true --overwrite
```

Аннотация `experiment.chaos-mesh.org/pause` ставит эксперименты на паузу, не удаляя их [ПРОВЕРИТЬ синтаксис для вашей версии]. В реальном инциденте это первый шаг: «заморозить хаос, потом разбираться».

**10. Litmus: установка и запуск эксперимента**

```bash
helm repo add litmuschaos https://litmuschaos.github.io/litmus-helm/
helm install litmus litmuschaos/litmus --namespace litmus --create-namespace
kubectl get pods -n litmus
```

[ПРОВЕРИТЬ имя чарта, состав компонентов и статус ChaosCenter/Portal в актуальной версии Litmus — в 3.x состав заметно менялся.]

```yaml
apiVersion: litmuschaos.io/v1alpha1
kind: ChaosEngine
metadata:
  name: web-pod-delete
  namespace: chaos-lab
spec:
  engineState: active
  appinfo:
    appns: chaos-lab
    applabel: "app=vektor-web"
    appkind: deployment
  chaosServiceAccount: litmus-admin
  experiments:
    - name: pod-delete
      spec:
        components:
          env:
            - name: TOTAL_CHAOS_DURATION
              value: "30"
            - name: CHAOS_INTERVAL
              value: "10"
            - name: FORCE
              value: "false"
```

`chaosServiceAccount` — ServiceAccount с правами на удаление подов в целевом namespace [ПРОВЕРИТЬ, создаётся ли `litmus-admin` автоматически чартом].

**11. Смотрим результат эксперимента в кластере и метриках**

```bash
kubectl -n chaos-lab get events --sort-by=.lastTimestamp | tail -20
kubectl -n chaos-lab get pods -w
```

```bash
# рестарты подов за последние 10 минут (нужен kube-state-metrics, урок 84)
curl -sG http://10.10.10.40:9090/api/v1/query [ПРОВЕРИТЬ адрес/порт Prometheus на стенде] \
  --data-urlencode 'query=sum(increase(kube_pod_container_status_restarts_total{namespace="chaos-lab"}[10m]))'
```

**12. Уборка**

```bash
kubectl -n chaos-lab delete podchaos,networkchaos,stresschaos,dnschaos,schedule --all
kubectl get networkchaos -A          # должно быть пусто
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
Стенд DevOps: кластер `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63) в рабочем состоянии; `MON01` (10.10.10.40) с Prometheus/Grafana; `HARBOR01` (10.10.10.55) с образом nginx; `GIT01` (10.10.10.50) для манифестов. На UBU01 — `kubectl` и `helm`. Windows-часть стенда не нужна.

### Снапшот
В VMware Workstation на каждой затрагиваемой ВМ: `VM → Snapshot → Take Snapshot`, имя **`before-devops-091`** для `k8s-cp01`, `k8s-w01`, `k8s-w02`. Снимайте все три: откат одной ноды из трёх рассинхронизирует etcd [ПРОВЕРИТЬ, что quorum позволяет одновременно остановить одну ноду; при 3 нодах — да].

### Шаги

1. **Здоровье кластера.** На UBU01: `kubectl get nodes` и `kubectl get pods -A | grep -v -E "Running|Completed"`. Ожидаем: 3 ноды `Ready`, пустой список проблемных подов. Если нет — сначала лечим кластер.
2. **Установка Chaos Mesh** (команда 1 из раздела 4). Ожидаем `STATUS: deployed`.
3. **Проверка daemon-подов** (команда 2). Ожидаем три `chaos-daemon` в `Running`, CRD `podchaos.chaos-mesh.org` и др. в списке.
4. **Полигон.** Создаём namespace `chaos-lab` и `vektor-web` с 2 репликами (команда 3). Ожидаем 2 пода `Running`.
5. **Baseline (steady state).** Замеряем норму в течение 30 секунд:
   ```bash
   kubectl -n chaos-lab run check --rm -it --restart=Never --image=curlimages/curl \
     -- sh -c 'for i in $(seq 1 30); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" http://vektor-web/; sleep 1; done'
   ```
   Ожидаем: все `200`, время ~0.00x с. Записываем в `stand-log.md`.
6. **Гипотеза + game day документ.** В репозитории на GIT01 создаём `gamedays/91-vektor-web.md`: гипотеза («при убийстве 1 пода из 2 доля успешных запросов ≥ 99%»), blast radius (`namespace chaos-lab`), abort-условие («доля ошибок > 5% две минуты подряд → `kubectl delete` всех chaos-объектов»), окно, участники.
7. **Эксперимент 1: pod-kill.** `kubectl apply -f kill-one-web.yaml`. Параллельно в другом терминале — цикл `curl` из шага 5. Ожидаем: 1–2 ошибки в момент убийства, под пересоздаётся, `kubectl get pods` показывает новый AGE.
8. **Эксперимент 2: сетевая задержка.** `kubectl apply -f delay-web.yaml`, снова меряем `time_total`. Ожидаем рост p95 примерно на 300 мс у затронутого пода. Проверяем на дашборде в Grafana, что SLO-панель «поехала», но не пробила abort-условие.
9. **Эксперимент 3: стресс CPU.** `kubectl apply -f cpu-stress-web.yaml` на 60 секунд. Ожидаем рост CPU пода в `kubectl top pod -n chaos-lab`; под не должен быть `OOMKilled` (если OOM — вы нашли реальную проблему: нет limits).
10. **Уборка и сверка с baseline.** Команда 12 из раздела 4, затем повторный цикл `curl`. Ожидаем возврат к `200` и прежнему времени ответа.
11. **Фиксация.** Коммит манифестов и протокола в Git, запись в `stand-log.md`.

### Проверка
- [ ] `kubectl get pods -n chaos-mesh` — все компоненты `Running`
- [ ] `kubectl get crd | grep chaos-mesh` показывает минимум `podchaos`, `networkchaos`, `stresschaos`, `schedule`
- [ ] После pod-kill сервис продолжил отвечать `200` (гипотеза подтверждена) либо зафиксирован провал
- [ ] В Grafana видны изменения latency/рестартов в окне эксперимента
- [ ] `kubectl get networkchaos -A` пусто после уборки
- [ ] В `stand-log.md` записаны: baseline, результат трёх экспериментов, вывод по гипотезе
- [ ] Game day документ закоммичен на GIT01

### Сломай и почини

> **Пользователь:** sidorov (склад), ВМ WIN10-CL02
> **Что написал:** «Портал заявок еле шевелится, форма грузится по 3–4 секунды, раньше открывалось мгновенно. Началось после обеда».
> **Срочность:** средняя

**Как воспроизвести.** Примените `delay-web.yaml`, предварительно **удалив строку `duration`**. Chaos Mesh воспримет это как «без ограничения по времени» — правило `tc netem` останется в netns пода навсегда, даже после окончания game day. Все считают, что «учения закончились», а задержка живёт.

**Диагностика по порядку.**
1. `kubectl -n chaos-lab get networkchaos` — есть ли активный объект (главный признак).
2. `kubectl get pods -n chaos-lab -o wide` — не рестартовали ли поды, совпадает ли время начала проблемы с временем подачи манифеста.
3. `kubectl -n chaos-lab describe networkchaos delay-web` — смотрим `Duration` (пусто) и `Status.Experiment.Phase` (`Injected`).
4. Изнутри пода: `kubectl -n chaos-lab exec <pod> -- tc qdisc show` [ПРОВЕРИТЬ наличие `tc` в образе; в nginx-образе его может не быть] — увидите `netem delay 300ms`.
5. Логи `kubectl -n chaos-mesh logs deploy/chaos-controller-manager | grep -i delay` — подтверждение инжекта.
6. Только после этого идём в Grafana и сверяем графики latency по namespace.

**Решение.**
```bash
kubectl -n chaos-lab delete networkchaos delay-web
kubectl -n chaos-lab exec <pod> -- tc qdisc show    # правило исчезло
```
Затем — повторный `curl`-цикл, что время вернулось к базовому.

**Ответ пользователю:** «Задержка вызвана некорректно завершённым тестом отказоустойчивости в тестовом контуре, на ваши данные это не влияло. Проблему устранили, проверили скорость загрузки формы — около 0.2 с. Если снова заметите замедление, напишите в эту же заявку».

**Закрытие:** категория «конфигурация тестового контура», в постмортем game day — пункт «все эксперименты обязаны иметь `duration` либо `Schedule`; проверка `kubectl get networkchaos -A` добавлена в чек-лист завершения учений».

### Что изменилось на стенде
- Namespace `chaos-mesh`: установлен Chaos Mesh (controller-manager, 3× chaos-daemon, dashboard, dns-server).
- Namespace `chaos-lab`: Deployment `vektor-web` (2 реплики, образ из HARBOR01), Service `vektor-web`.
- В репозитории на GIT01 — каталог `gamedays/`: манифесты `kill-one-web.yaml`, `delay-web.yaml`, `cpu-stress-web.yaml`, `kill-every-10m.yaml`, протокол `91-vektor-web.md`.
- Все chaos-объекты удалены, кластер в исходном состоянии; зафиксирован baseline latency и error rate.
- Опционально: установлен Litmus в namespace `litmus`.

### Откат
Если кластер после экспериментов нестабилен: сначала `kubectl delete podchaos,networkchaos,stresschaos,dnschaos,schedule --all -A`, затем, если не помогло, — в VMware Workstation `VM → Snapshot → Snapshot Manager` на **всех трёх** нодах `k8s-cp01`, `k8s-w01`, `k8s-w02`, выбрать `before-devops-091` и `Go To`. Откатывать одну ноду нельзя — получите рассинхрон etcd; если сомневаетесь, сначала проверьте `kubectl get nodes` и `kubectl -n kube-system logs etcd-k8s-cp01 | tail`.

## 6. Ошибки новичков и безопасность

1. **Хаос без гипотезы и метрики.** → «Сломали, вроде поднялось» — результата нет. → Сначала baseline и SLI, потом эксперимент.
2. **Запуск в prod без blast radius.** → Один `mode: all` роняет весь сервис. → Всегда `mode: one`/`fixed-percent`, отдельный namespace, метка `chaos: allowed` на целевых объектах.
3. **Эксперимент без `duration`.** → Правило остаётся навсегда (см. выше). → `duration` или `Schedule`, плюс проверка после уборки.
4. **pod-kill на StatefulSet с одной репликой БД.** → Потеря данных/долгий рестарт. → Для stateful-компонентов сначала изучайте кворум и репликацию (урок 93).
5. **StressChaos на всю ноду.** → Уезжает kubelet и control plane, кластер не отвечает. → Стресс только на поды с `mode: one` и `duration`.
6. **Хаос во время реального инцидента.** → Диагностика невозможна. → Правило: на время инцидента все эксперименты ставятся на паузу (аннотация `pause`), это первое действие дежурного.
7. **Учения без постмортема.** → Найденные слабости не превращаются в задачи. → Каждый game day заканчивается списком action items с владельцами и сроками.
8. **Забыли про probes в Litmus.** → Непонятно, эксперимент «прошёл» или «уронил». → Настраивайте `httpProbe`/`promProbe` до и после.

**Безопасность.** `chaos-daemon` — privileged-под с доступом к сокету container runtime, то есть фактически root на каждой ноде кластера. В боевой среде это недопустимо без: (а) отдельного кластера или namespace для chaos; (б) строгого RBAC — писать chaos-объекты может только узкая группа SRE, не все разработчики; (в) NetworkPolicy и аудита API-сервера; (г) ограничения `physicalMachineChaos` — он трогает хосты. Litmus-эксперименты запускаются подами с правами на удаление подов — ServiceAccount `litmus-admin` не должен существовать в prod-namespace. Никогда не давайте chaos-инструментам доступ к кластеру с боевыми персональными данными без письменного согласования.

## 7. Вопросы на собеседовании

**1. Чем chaos engineering отличается от нагрузочного тестирования?**
Ответ: нагрузочное тестирование отвечает на вопрос «выдержит ли система пиковую нагрузку» — оно про ёмкость и узкие места, его метрика — RPS/латентность под нагрузкой. Chaos engineering отвечает на «как поведёт себя система при отказе компонента» — убивает под, вносит задержку, роняет ноду. Общее у них одно: и то и другое делается по гипотезе и с измерением, и оба проводятся до прода, а не после инцидента.
*Что хочет услышать интервьюер:* понимание, что chaos — про failure-режимы, а не про производительность.
*Красный флаг:* «это одно и то же, просто с отказами».

**2. Что такое blast radius и как вы ограничите его в Chaos Mesh?**
Ответ: blast radius — максимальный ущерб, который эксперимент может нанести. Ограничиваю пятью способами: отдельный namespace и метка на целевых объектах в `selector`; `mode: one` или `fixed-percent` вместо `all`; обязательный `duration`; запуск в staging-кластере (или отдельном кластере на том же стенде); abort-условие по метрике и готовность удалить объект одной командой. Плюс аннотация `pause` как стоп-кран для всех экспериментов.
*Что хочет услышать интервьюер:* перечисление конкретных механизмов, а не «аккуратно».
*Красный флаг:* «запускаем в проде на всех подах, ночью, когда мало пользователей».

**3. Как вы поймёте, что эксперимент прошёл успешно?**
Ответ: до эксперимента фиксирую steady state — SLI (доля успешных запросов, p95) и записываю в документ. Во время эксперимента смотрю те же метрики на дашборде и в `kubectl get pods/events`. Эксперимент успешен, если гипотеза подтвердилась: например, при убийстве одного пода доля `5xx` не превысила 1%, а p95 не вырос выше SLO. Если метрика пробила abort-условие — эксперимент останавливаю и считаю это найденным дефектом, а не «неудачным тестом».
*Что хочет услышать интервьюер:* что критерий задан заранее и измерим.
*Красный флаг:* «смотрю глазами на графики, вроде норм».

**4. Chaos Mesh или Litmus — что выберете и почему?**
Ответ: зависит от задачи. Chaos Mesh — Kubernetes-native, всё описывается CRD (PodChaos, NetworkChaos, StressChaos, HTTPChaos, TimeChaos), есть Workflow для последовательных сценариев и Schedule для регулярных, инжект идёт на уровне ядра через privileged DaemonSet — хорошо для точных сетевых и временных сбоев. Litmus — большая библиотека готовых экспериментов с probes (http, cmd, prom, k8s) и удобен, когда нужен каталог «из коробки» и проверки-ассерты. На практике часто ставят Chaos Mesh для сетевых/стресс-сценариев и Litmus для pod/container/nodу-экспериментов с проверками.
*Что хочет услышать интервьюер:* сравнение по архитектуре и по задаче, а не «тот лучше».
*Красный флаг:* «Litmus — это то же самое, разницы нет».

**5. Как встроить game day в процесс эксплуатации?**
Ответ: game day планируется заранее: гипотеза, затронутые сервисы, окно (не в пик и не в freeze-период из урока 94), участники и роли, abort-условие, дежурный в курсе. В день учений: фиксируем baseline, проводим эксперимент, наблюдаем, при пробитии abort — останавливаем. После — постмортем без поиска виноватых (формат урока 88), список action items с владельцами, задачи в трекер, и через месяц повторная проверка, что слабое место закрыто. Результаты складываем в Git рядом с манифестами.
*Что хочет услышать интервьюер:* что это процесс с до и после, а не разовое «поломали».
*Красный флаг:* «проводим, когда скучно, без предупреждения дежурного».

## 8. Источники (официальная документация)

- **chaos-mesh.org** — раздел «User Guide»: типы экспериментов (PodChaos, NetworkChaos, StressChaos, DNSChaos, HTTPChaos, TimeChaos), Workflow и Schedule, «Installation» (Helm, требования к runtime). Поиск: `chaos mesh installation helm containerd`.
- **chaos-mesh.org** — раздел «Run a Chaos Experiment», примеры манифестов и `kubectl get podchaos`. Поиск: `chaos mesh pod-kill example`.
- **litmuschaos.io** — разделы «Getting Started», «ChaosHub», «Probes», документация по `ChaosEngine` и `ChaosExperiment`. Поиск: `litmuschaos chaosengine pod-delete example`.
- **kubernetes.io** — «Custom Resources» и «DaemonSet» (для понимания CRD и chaos-daemon). Поиск: `kubernetes custom resource definition`.
- **github.com/chaos-mesh/chaos-mesh** и **github.com/litmuschaos/litmus** — README, `docs/`, каталог примеров; там же матрица поддерживаемых версий Kubernetes `[ПРОВЕРИТЬ актуальные релизы]`.
- **sre.google** — книга «Site Reliability Engineering», глава «Postmortem Culture» и раздел про тестирование отказоустойчивости (ищите раздел про DiRT — Disaster Recovery Testing). Поиск: `google sre book disaster recovery testing`.
- **principlesofchaos.org** — манифест принципов chaos engineering (гипотеза, steady state, blast radius).
