# Планирование и ресурсы: requests/limits, QoS, affinity, taints/tolerations, HPA/VPA, PodDisruptionBudget

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4412**

- **Пользователь:** petrova (Мария Петрова, HR), ВМ WIN10-CL02
- **Что написал:** «Портал отпусков через раз открывается. Утром всё нормально, после обеда — белая страница и 502 Bad Gateway. К вечеру само проходит. Мне до пятницы согласовать график, очень срочно!»
- **Срочность:** средняя, но с дедлайном у пользователя

Портал живёт в твоём кластере на k8s-cp01/k8s-w01/k8s-w02, но ты как дежурный видишь только «502». Дальше начинается работа с планировщиком: кто-то из подов съел всю память ноды, kubelet выселил соседей, а у сервиса не было ни нормальных `requests`, ни `PodDisruptionBudget`, ни горизонтального масштабирования. Именно так выглядит 90% инцидентов «само то работает, то нет» в Kubernetes.

Планирование — это не про то, «как красиво написать YAML». Это про три вещи: предсказуемость (поды всегда находят место), изоляцию (один сервис не убивает соседей) и способность пережить пик нагрузки и плановое обслуживание. После этой главы ты должен уметь: осознанно ставить `requests` и `limits`, определять QoS-класс пода, раскладывать реплики по узлам через affinity и topology spread, помечать узлы taints и пускать на них нужные поды, настраивать HPA и VPA и защищать сервис от одновременного вывода всех подов через PDB.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Кто решает, куда едет под.** `kube-scheduler` (control plane, secure-порт 10259) смотрит в API-сервер на поды с пустым `.spec.nodeName`. Для каждого пода он прогоняет два этапа: **Filter** (отбрасывает узлы, которые физически не подходят — не хватает `requests`, не совпадают nodeSelector/affinity, taint без toleration, не проходят topology spread) и **Score** (ранжирует оставшиеся — по загрузке, по `preferred`-правилам, по образу уже в кэше). Победителю scheduler пишет `nodeName` (Bind). Дальше в игру вступает `kubelet` на узле (порт 10250): он просит CRI (containerd) запустить контейнер и через cgroups навешивает лимиты.

**requests vs limits — разные вещи для разных людей.**

| Параметр | Кто использует | Смысл |
|---|---|---|
| `requests.cpu/memory` | **scheduler** и kubelet при eviction | Гарантированная квота. Под не встанет на узел, где сумма requests уже не влезает |
| `limits.cpu` | cgroups (CFS quota) | Потолок. Превышение → **throttling** (под тормозит, но живёт) |
| `limits.memory` | cgroups (OOM killer) | Потолок. Превышение → **OOMKilled**, exit code 137, под перезапускается |

CPU — сжимаемый ресурс (можно «притормозить»), память — несжимаемый (только убить). Единицы: `1` = 1 vCPU, `100m` = 0.1 ядра; `128Mi`, `1Gi`. Есть ещё `ephemeral-storage` — лимит на запись в слой контейнера.

**QoS-классы** вычисляются автоматически из requests/limits:

- **Guaranteed** — у *всех* контейнеров `requests == limits` и заданы и CPU, и память.
- **Burstable** — задано хоть что-то, но не равны (самый частый класс).
- **BestEffort** — ничего не задано.

При нехватке памяти на узле kubelet выселяет (evict) поды в порядке: BestEffort → Burstable, превысившие свои requests → Guaranteed последними. Пороговые условия (`MemoryPressure`, `DiskPressure`, `PIDPressure`) видны в `kubectl describe node`; дефолтные значения вроде `memory.available<100Mi` зависят от версии — проверь в документации `[ПРОВЕРИТЬ]`.

**Где что живёт — схема:**

```text
                    ┌──────────────── kube-scheduler (10259) ───────────────┐
 kubectl apply ──►  │  Filter: resources / affinity / taints / spread       │
 API-сервер :6443   │  Score : least-allocated, preferred-rules, image-local│
                    └───────────────┬──────────────────────────────────────┘
                                    │ Bind (пишет spec.nodeName)
   ┌────────────────────────────────┼─────────────────────────────────┐
   ▼                                ▼                                 ▼
 k8s-w01 (taint?) ──kubelet:10250── k8s-w02 ──kubelet──   k8s-cp01 (NoSchedule)
   │ cgroups: cpu.max / memory.max (v2)
   │ QoS → порядок eviction при MemoryPressure
   └── metrics-server (агрегация через API :443) ──► HPA читает CPU/RAM
```

**Расстановка по узлам.** `nodeSelector` — простейшее «метка = значение». `nodeAffinity` — тот же nodeSelector, но с операторами `In/NotIn/Exists` и двумя режимами: `required...` (жёстко) и `preferred...` (вес 1–100). `podAffinity`/`podAntiAffinity` — «хочу рядом с подом X» / «не хочу рядом», управляются через `topologyKey` (`kubernetes.io/hostname` = узел, `topology.kubernetes.io/zone` = зона). Современная замена антиаффинити — `topologySpreadConstraints` (максимальная неравномерность `maxSkew`).

**Taints и tolerations — зеркальная логика.** Taint вешается **на узел** и отталкивает поды: `NoSchedule` (новые не встанут), `PreferNoSchedule` (мягко), `NoExecute` (ещё и выгонит уже запущенные). Toleration — свойство **пода**, дающее право игнорировать конкретный taint. `cordon` — это просто `spec.unschedulable: true` у узла, а `drain` = cordon + eviction API (уважает PDB).

**Автомасштабирование и надёжность.** HPA (`autoscaling/v2`) каждые ~15 секунд считает `фактическое_потребление / requests` и меняет `replicas`. Важно: HPA считает **проценты от requests**, поэтому без requests он покажет `<unknown>`. VPA (`autoscaling.k8s.io/v1`, ставится отдельно из репозитория kubernetes/autoscaler) наоборот — рекомендует и подгоняет requests/limits, но требует перезапуска пода. PDB (`policy/v1`) ограничивает число **добровольных** нарушений (`minAvailable`/`maxUnavailable`) при drain и обновлениях.

## 3. Аналогия из жизни

Представь коворкинг. Ты арендуешь **рабочее место** — это `requests`: администратор физически не посадит на него другого, даже если ты ушёл пить кофе. **Розетка с автоматом** — это `limits`: тянешь больше — вырубает (CPU throttling), а если развалился и занял проход — охрана выводит (OOMKill). **QoS** — очередь на выход при пожаре: сначала выгоняют тех, кто сидит без брони (BestEffort), последними — тех, у кого место оплачено ровно под размер (Guaranteed). **Taint** — табличка «служебное помещение, вход по пропуску», а **toleration** — пропуск у тебя в кармане. **Affinity** — «хочу у окна», **anti-affinity** — «не хочу сидеть в одном зале с коллегой из моего же отдела». **HPA** — администратор добавляет стулья, когда в зале тесно. **PDB** — правило «во время ремонта в зале может отсутствовать не больше одного человека», чтобы офис продолжал работать.

Аналогия ломается в одном месте: CPU измеряется в миллисекундах кванта (`cpu.max` в cgroup v2), а не в «стульях», и под, которому не хватило места, не звонит администратору — он просто висит в `Pending` и ждёт, пока кто-нибудь не прочитает Events.

## 4. Практика и команды

**1. Найти поля в API, не угадывая YAML.**

```bash
kubectl explain deployment.spec.template.spec.containers.resources
kubectl explain pod.spec.topologySpreadConstraints
```
`kubectl explain` берёт документацию прямо из схемы CRD/API твоего кластера. Смотри `TYPE` (Quantity, string) и `REQUIRED`. Это быстрее, чем гуглить и надеяться на версию.

**2. Узнать, сколько реально можно запросить у узлов.**

```bash
kubectl get nodes -o custom-columns='NODE:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory,PODS:.status.allocatable.pods'
```
`allocatable` = capacity минус зарезервированное под систему (`system-reserved`, `kube-reserved`). Именно от allocatable, а не от capacity, считает планировщик. Типичный вывод для лабы: `k8s-w01  2  3Gi  110`.

**3. Создать рабочий Deployment с ресурсами и проверить QoS.**

```bash
kubectl create namespace vector-app
kubectl -n vector-app create deployment checkout --image=nginx:1.27-alpine --replicas=4
kubectl -n vector-app set resources deployment/checkout \
  --requests=cpu=100m,memory=128Mi --limits=cpu=500m,memory=256Mi
kubectl -n vector-app get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.qosClass}{"\n"}{end}'
```
`set resources` патчит шаблон и запускает rolling update. `jsonpath` с `range` — универсальный способ вытащить поле из списка. Здесь увидишь `Burstable` (requests ≠ limits). Сделай `--limits` равными `--requests` — получишь `Guaranteed`.

**4. Посмотреть фактическое потребление (нужен metrics-server).**

```bash
kubectl top nodes
kubectl top pods -n vector-app --containers
```
Если видишь `error: Metrics API not available` — метрик-сервер не установлен. Для kubeadm-кластера:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```
Флаг `--kubelet-insecure-tls` отключает проверку сертификата kubelet — **только для лабы**; в проде kubelet-сертификаты подписывает нормальный CA. Образы тяни из зеркала, которое настроил в уроке 32.

**5. Понять, кто занял узел.**

```bash
kubectl describe node k8s-w01 | sed -n '/Allocated resources/,/Events/p'
```
Смотри блок `Allocated resources` (проценты requests от allocatable) и `Conditions` — `MemoryPressure: True` означает, что kubelet уже начал выселять поды.

**6. Пометить узел и повесить/снять taint.**

```bash
kubectl label node k8s-w01 workload=web
kubectl taint nodes k8s-w02 workload=batch:NoSchedule
kubectl taint nodes k8s-w02 workload=batch:NoSchedule-   # минус в конце = снять
kubectl get nodes -o custom-columns='NODE:.metadata.name,TAINTS:.spec.taints'
```
Taint-ключ `workload`, значение `batch`, эффект `NoSchedule`. После этого новые поды без toleration на k8s-w02 не поедут, а уже запущенные останутся (для выгона нужен `NoExecute`).

**7. Toleration + nodeAffinity в одном манифесте.**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: batch-report, namespace: vector-app}
spec:
  replicas: 1
  selector: {matchLabels: {app: batch-report}}
  template:
    metadata: {labels: {app: batch-report}}
    spec:
      tolerations:
      - key: workload
        operator: Equal
        value: batch
        effect: NoSchedule
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - {key: workload, operator: In, values: ["batch"]}
      containers:
      - name: job
        image: busybox:1.36
        command: ["sh","-c","sleep 3600"]
```
`operator: Equal` сравнивает значение, `Exists` игнорирует `value`. Обрати внимание: toleration лишь *разрешает* встать на узел, но не *требует* — требует как раз `nodeAffinity`.

**8. Разложить реплики по разным узлам.**

```yaml
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels: {app: checkout}
```
`maxSkew: 1` — разница между узлами не больше одного пода. `DoNotSchedule` — жёстко (риск `Pending`), `ScheduleAnyway` — мягко, планировщик просто штрафует перегруженные узлы.

**9. Обслуживание узла без паники.**

```bash
kubectl cordon k8s-w01
kubectl drain k8s-w01 --ignore-daemonsets --delete-emptydir-data
kubectl uncordon k8s-w01
```
`cordon` запрещает новые поды, `drain` аккуратно выселяет существующие через Eviction API (уважает PDB). Без `--ignore-daemonsets` drain ругнётся на DaemonSet-поды, `--delete-emptydir-data` разрешает удалить поды с `emptyDir`.

**10. HPA через kubectl и через манифест.**

```bash
kubectl -n vector-app autoscale deployment checkout --cpu-percent=60 --min=2 --max=6
kubectl -n vector-app get hpa checkout -o yaml   # посмотреть, что получилось
```

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: checkout, namespace: vector-app}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: checkout}
  minReplicas: 2
  maxReplicas: 6
  metrics:
  - type: Resource
    resource:
      name: cpu
      target: {type: Utilization, averageUtilization: 60}
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 0
      policies: [{type: Percent, value: 100, periodSeconds: 60}]
    scaleDown:
      stabilizationWindowSeconds: 300
      policies: [{type: Percent, value: 50, periodSeconds: 60}]
```
`averageUtilization: 60` — это 60% от requests, а не от лимита и не от ядра. `behavior` управляет скоростью: вверх быстро, вниз с 5-минутным окном стабилизации, чтобы не «дёргаться».

**11. PodDisruptionBudget.**

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: checkout-pdb, namespace: vector-app}
spec:
  minAvailable: 2
  selector:
    matchLabels: {app: checkout}
```

```bash
kubectl -n vector-app get pdb
kubectl -n vector-app describe pdb checkout
```
В выводе смотри `ALLOWED DISRUPTIONS` — сколько подов можно вывести прямо сейчас. Если там `0`, drain зависнет.

**12. VPA (только рекомендации).**

```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata: {name: checkout-vpa, namespace: vector-app}
spec:
  targetRef: {apiVersion: apps/v1, kind: Deployment, name: checkout}
  updatePolicy: {updateMode: Off}
```
`updateMode: Off` — VPA только пишет рекомендации, ничего не перезапускает. Смотреть: `kubectl -n vector-app describe vpa checkout-vpa` (блок `Recommendation`). VPA — не встроенный объект, CRD ставятся из репозитория kubernetes/autoscaler `[ПРОВЕРИТЬ актуальный манифест установки]`. Режимы `Initial`, `Recreate`, а также `InPlaceOrRecreate` в новых версиях `[ПРОВЕРИТЬ]`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34 поднят: **k8s-cp01** (10.10.10.61), **k8s-w01** (10.10.10.62), **k8s-w02** (10.10.10.63), все в `Ready`.
- CNI Calico из урока 37 работает, CoreDNS отвечает.
- Есть доступ к GIT01 (10.10.10.50) — манифесты этой лабы коммитим туда, в репозиторий `k8s-lab`.
- Если metrics-server не установлен — шаг 4 из раздела 4 (это kubeadm; на k3s он встроен).
- Узлы не должны быть сильно загружены: проверь `kubectl get nodes` и `kubectl top nodes`.

### Снапшот

Останавливаем все три ВМ кластера (этак чище для etcd, чем снапшот на живой системе), затем в VMware Workstation: **VM → Snapshot → Take Snapshot**, имя `before-devops-040`, на каждой из **k8s-cp01, k8s-w01, k8s-w02**. После этого включаем обратно и ждём `kubectl get nodes` → все `Ready`.

### Шаги

1. **k8s-cp01.** Проверяем ресурсы: `kubectl get nodes -o custom-columns='NODE:.metadata.name,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'`. Ожидаемо: allocatable меньше capacity (например, 2 CPU / 3Gi). Запиши цифры — от них зависит, влезут ли 4 реплики.

2. **k8s-cp01.** `kubectl create namespace vector-app`. Ожидаемо: `namespace/vector-app created`.

3. **k8s-cp01.** Создаём Deployment `checkout` (nginx:1.27-alpine, 4 реплики) и через `kubectl set resources` ставим `requests=cpu=100m,memory=128Mi`, `limits=cpu=500m,memory=256Mi`. Ожидаемо: `kubectl -n vector-app get pods` показывает 4 пода `Running`, QoS-класс `Burstable`.

4. **k8s-cp01.** `kubectl -n vector-app get pods -o wide` — смотрим, как поды распределились по k8s-w01/k8s-w02. Они, скорее всего, свалены на один узел (least-allocated не гарантирует равномерность при малом числе реплик).

5. **k8s-cp01.** Ставим `topologySpreadConstraints` (`maxSkew: 1`, `kubernetes.io/hostname`, `ScheduleAnyway`) в манифест и применяем. Ожидаемо: после rollout поды разъехались 2/2.

6. **k8s-cp01.** `kubectl taint nodes k8s-w02 workload=batch:NoSchedule` и сразу запускаем `kubectl -n vector-app scale deployment checkout --replicas=6`. Ожидаемо: новые поды едут только на k8s-w01, часть может уйти в `Pending`, если на w01 кончились requests.

7. **k8s-cp01.** `kubectl -n vector-app describe pod <pending-под>` → в `Events` строка вида `0/3 nodes are available: 1 node(s) had untolerated taint {workload: batch}, 2 Insufficient cpu`. Это и есть результат работы Filter-этапа.

8. **k8s-cp01.** Возвращаем `--replicas=4`, разворачиваем `batch-report` из раздела 4 (toleration + nodeAffinity) — он должен встать **именно** на k8s-w02. Проверяем: `kubectl -n vector-app get pod -o wide | grep batch`.

9. **k8s-cp01.** Устанавливаем HPA: `kubectl -n vector-app autoscale deployment checkout --cpu-percent=60 --min=2 --max=6`, затем `kubectl -n vector-app describe hpa checkout` — колонка `TARGETS` должна показывать реальные проценты (например `3%/60%`), а не `<unknown>`.

10. **k8s-cp01.** Генерируем нагрузку: `kubectl -n vector-app run load --image=busybox:1.36 --restart=Never -- sh -c "while true; do wget -q -O- http://checkout:80/ >/dev/null; done"` (запустить 3–4 таких пода). Через 2–3 минуты `kubectl -n vector-app get hpa -w` должен показать рост `REPLICAS`. Если CPU не растёт — увеличь число нагрузочных подов.

11. **k8s-cp01.** Создаём `checkout-pdb` (`minAvailable: 2`). `kubectl -n vector-app get pdb` → `ALLOWED DISRUPTIONS` больше нуля.

12. **k8s-cp01.** Убираем нагрузку (`kubectl -n vector-app delete pod load`), затем `kubectl drain k8s-w01 --ignore-daemonsets --delete-emptydir-data`. Ожидаемо: поды переезжают на k8s-w02, drains завершается, PDB не блокирует. Затем `kubectl uncordon k8s-w01`.

13. **k8s-cp01 + GIT01.** Кладём все манифесты в `~/k8s-lab/40/` на GIT01, коммитим с сообщением `lesson40: resources, spread, hpa, pdb`. Заполняем `stand-log.md`.

### Проверка

- [ ] `kubectl -n vector-app get pods` — 4 пода `Running`, все `Ready`
- [ ] QoS-класс подов `checkout` — `Burstable` (или `Guaranteed`, если ты уравнял requests и limits)
- [ ] `kubectl -n vector-app get pods -o wide` — реплики распределены по k8s-w01 и k8s-w02
- [ ] `kubectl describe node k8s-w02` содержит taint `workload=batch:NoSchedule`
- [ ] `batch-report` запущен именно на k8s-w02
- [ ] `kubectl -n vector-app describe hpa checkout` — `TARGETS` не `<unknown>`
- [ ] При нагрузке `REPLICAS` в HPA выросло и потом вернулось (с задержкой ~5 минут)
- [ ] `kubectl -n vector-app get pdb` — `ALLOWED DISRUPTIONS: 2`
- [ ] `kubectl drain k8s-w01` завершился без ошибок, узел вернулся через `uncordon`
- [ ] В `stand-log.md` записаны namespace, deployment, taint, метки, HPA, PDB

### Сломай и почини

**Заявка #4471**

- **Пользователь:** petrova (Мария Петрова, HR), ВМ WIN10-CL02
- **Что написала:** «Портал отпусков опять не открывается, 502 Bad Gateway. Началось примерно в 14:10, до этого весь день работал. Вы же что-то обновляли?»
- **Срочность:** высокая

**Как воспроизвести поломку (k8s-cp01):**

```bash
kubectl -n vector-app set resources deployment/checkout \
  --requests=memory=64Mi --limits=memory=32Mi
kubectl -n vector-app rollout status deployment/checkout --timeout=60s
```
(Сначала API может отклонить limits < requests — тогда ставь оба значения: `--requests=memory=16Mi --limits=memory=32Mi`. Смысл один: память урезана ниже реальной потребности nginx.)

**Диагностика, по порядку:**

1. `kubectl -n vector-app get pods` — видим `CrashLoopBackOff` и `RESTARTS` в десятках. Это уже сужает поиск: под запускается и падает, а не висит в `Pending`.
2. `kubectl -n vector-app get endpoints checkout` — эндпоинтов нет или мало. Отсюда и 502 на ingress: бэкенду некуда проксировать.
3. `kubectl -n vector-app describe pod <pod>` → блок `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`. 137 = 128 + 9 (SIGKILL от OOM killer). Причина найдена.
4. `kubectl -n vector-app logs <pod> --previous` — логи предыдущего запуска; часто там пусто, потому что процесс убили до записи, и это нормально.
5. `kubectl -n vector-app get deploy checkout -o jsonpath='{.spec.template.spec.containers[0].resources}'` — сверяем с тем, что было: `limits.memory: 32Mi`.
6. `kubectl -n vector-app rollout history deployment/checkout` — видим ревизию, которой соответствует время 14:10.

**Решение:**

```bash
kubectl -n vector-app rollout undo deployment/checkout
kubectl -n vector-app rollout status deployment/checkout
kubectl -n vector-app get pods
```
Альтернатива — вернуть корректные значения через `kubectl set resources ... --limits=memory=256Mi`. После отката поды поднимаются, эндпоинты наполняются, 502 исчезает.

**Ответ пользователю:** «Мария, добрый день. Причина найдена: наш сервис портала оставался без памяти после обновления конфигурации в 14:10, контейнеры падали по OOM. Конфигурацию откатили, портал снова работает. Проверьте, пожалуйста, вход и напишите, если что-то ещё не открывается».

**Закрытие заявки:** категория «Kubernetes / ресурсы», в описании — «limits.memory ниже фактического потребления → OOMKilled → нет эндпоинтов → 502. Откат ревизии». Отдельной задачей — настроить алерт на `restart count` и `OOMKilled` (вернёмся к этому в уроке 45 про диагностику).

### Что изменилось на стенде

- Namespace `vector-app` на кластере k8s-cp01/k8s-w01/k8s-w02.
- Deployment `checkout` (nginx:1.27-alpine, 4 реплики, requests 100m/128Mi, limits 500m/256Mi), Service `checkout`.
- `topologySpreadConstraints` по `kubernetes.io/hostname`, `maxSkew: 1`.
- Deployment `batch-report` с toleration и nodeAffinity на метку `workload=batch`.
- Taint на **k8s-w02**: `workload=batch:NoSchedule`; метка `workload=web` на **k8s-w01**.
- HPA `checkout` (min 2, max 6, CPU 60%), PDB `checkout-pdb` (`minAvailable: 2`).
- Возможно, установлен metrics-server в `kube-system`.
- Репозиторий `k8s-lab` на GIT01, каталог `40/` с манифестами; запись в `stand-log.md`.

### Откат

- **Точечно, без снапшота:** `kubectl delete namespace vector-app`, `kubectl taint nodes k8s-w02 workload=batch:NoSchedule-`, `kubectl label node k8s-w01 workload-`.
- **Полный откат:** выключить k8s-cp01, k8s-w01, k8s-w02, в VMware Workstation на каждой выбрать **Snapshot → Snapshot Manager → before-devops-040 → Revert**, включить снова. Откатывай **все три узла вместе**: если откатить только cp01, etcd разойдётся с состоянием воркеров, и кластер придётся чинить.

## 6. Ошибки новичков и безопасность

1. **`limits` без `requests`.** → Планировщик считает под «невесомым», набивает узел под завязку, потом все упираются в limits и деградируют. Правильно: `requests` — по реальному p50 потребления, `limits` — с запасом 2–3× (CPU) или +20–30% (память).
2. **Memory limit «на глаз», ниже реального.** → `OOMKilled` (exit 137), CrashLoopBackOff, 502 у пользователей. Правильно: снимать профиль через `kubectl top`/VPA-рекомендации и ставить лимит выше пикового потребления.
3. **Очень низкий CPU limit.** → Постоянный throttling: приложение живо, но отвечает по 2 секунды, и в логах ничего нет. Правильно: либо CPU-limit заметно выше requests, либо (часто в проде) CPU-limit вообще не ставить, оставив только requests.
4. **HPA без `requests`.** → `<unknown>` в TARGETS, масштабирования нет. Правильно: сначала requests, потом HPA; и помнить, что HPA не поможет, если поды некуда ставить (`Pending` из-за нехватки ресурсов — это уже cluster autoscaler).
5. **HPA и VPA на одном и том же ресурсе.** → Они борются: VPA меняет requests, HPA пересчитывает проценты, реплики скачут. Правильно: HPA на CPU, VPA — на память (или VPA в режиме `Off` как источник рекомендаций).
6. **`minAvailable` равен числу реплик.** → `ALLOWED DISRUPTIONS: 0`, `kubectl drain` висит навсегда, обновление кластера встаёт. Правильно: `minAvailable = replicas - 1` или `maxUnavailable: 1`.
7. **`required`-антиаффинити при 3 репликах и 2 воркерах.** → Третья реплика вечно в `Pending`. Правильно: `preferred` или `topologySpreadConstraints` со `ScheduleAnyway`.
8. **Taint на всех узлах сразу.** → Новые поды, включая CoreDNS после рестарта, уходят в `Pending`, кластер теряет DNS. Правильно: сначала проверить, что у системных DaemonSet есть нужные tolerations, и taint'ить по одному узлу.

**Безопасность.** В боевой среде нельзя: оставлять поды без limits вообще (один «шумный сосед» съедает ноду — это фактически DoS для всех остальных, поэтому в реальных кластерах namespace закрывают `ResourceQuota` и `LimitRange`); ставить `--kubelet-insecure-tls` у metrics-server (отключает проверку TLS, открывает путь к подмене метрик); выполнять `kubectl drain` без PDB на проде в час пик; запускать VPA в автоматическом режиме на stateful-сервисах без понимания, что он перезапускает поды; давать разработчикам право менять `requests`/`limits` в prod-namespace без ревью — именно так и получился инцидент из этой главы. Sysctl- и cgroup-настройки, влияющие на QoS, должны проходить через Git и review, а не через `kubectl edit`.

## 7. Вопросы на собеседовании

**1. Чем `requests` отличается от `limits` и что происходит, когда под превышает каждый из них?**

`requests` — гарантия для планировщика и база для расчёта eviction: под не встанет на узел, где нет свободной квоты. `limits` — потолок, который обеспечивает kubelet через cgroups. CPU при превышении лимита не убивается, а тормозится CFS-квотой (throttling). Память несжимаема: выход за `limits.memory` → OOM killer, контейнер получает SIGKILL, exit code 137, `kubectl describe pod` показывает `Reason: OOMKilled`. Если `limits` не задан вовсе, под может занять всю память узла, и тогда kubelet начнёт выселять поды по правилам QoS.

*Что хочет услышать интервьюер:* чёткое разделение «scheduler vs cgroups», понимание, что CPU и память ведут себя по-разному, и знание exit code 137 / throttling.
*Красный флаг:* «limits — это просто максимум, requests — минимум, разницы особо нет» или путаница, кто именно применяет лимиты.

**2. Какие есть QoS-классы и как kubelet выбирает, кого выселять при нехватке памяти?**

Три класса: **Guaranteed** (у всех контейнеров requests == limits и заданы и CPU, и память), **Burstable** (что-то задано, но не равно), **BestEffort** (ничего не задано). При `MemoryPressure` kubelet сначала выселяет BestEffort, затем Burstable, чьё фактическое потребление превышает requests (сортировка по величине превышения), и последними — Guaranteed. Внутри одного класса приоритет может корректироваться `PriorityClass`.

*Что хочет услышать интервьюер:* порядок eviction и понимание, зачем вообще нужен Guaranteed для критичных сервисов.
*Красный флаг:* «Guaranteed выселяется первым, потому что у него больше ресурсов» — полное непонимание.

**3. В чём разница между taints/tolerations и nodeAffinity? Кто кого отталкивает?**

Логика зеркальная. Taint вешается на узел и **отталкивает** поды; toleration у пода даёт право этот taint игнорировать, но не обязывает под туда ехать. nodeAffinity — наоборот, свойство пода, которое **притягивает** его к узлам с нужными метками (`required` — жёстко, `preferred` — с весом). На практике их сочетают: taint на узле защищает его от чужих подов, toleration + nodeAffinity гарантируют, что нужный под встанет именно туда. Отдельно стоит `topologySpreadConstraints` — современный способ обеспечить равномерность, который лучше справляется с задачей, чем `podAntiAffinity`.

*Что хочет услышать интервьюер:* понимание направления логики и того, что toleration сам по себе ничего не гарантирует.
*Красный флаг:* «toleration притягивает под на узел» — типичная ошибка.

**4. Как работает HPA и почему память — плохая метрика для автомасштабирования?**

HPA каждые ~15 с получает метрики (metrics-server или custom metrics API) и сравнивает фактическое потребление с `requests`, вычисляя `desiredReplicas = ceil(currentReplicas × currentMetric / targetMetric)` с допуском 10%. То есть процент считается от requests, и без requests HPA покажет `<unknown>`. Память плоха в качестве метрики, потому что большинство рантаймов её не возвращают ОС: график памяти растёт ступенькой и не падает после снятия нагрузки, поэтому HPA будет масштабировать вверх и никогда вниз. Правильно: HPA по CPU или по прикладным метрикам (RPS, длина очереди), а память — через VPA.

*Что хочет услышать интервьюер:* формула/логика расчёта, требование requests, понимание ограничений памяти и что HPA не лечит `Pending`.
*Красный флаг:* «HPA добавляет подам память» (это VPA) или «HPA масштабирует, если подов не хватает на узлах».

**5. Что такое PodDisruptionBudget и от чего он не защищает?**

PDB ограничивает число **добровольных** нарушений: `minAvailable` или `maxUnavailable` для подов, выбранных селектором. Его соблюдают Eviction API, `kubectl drain`, обновление узлов в managed-кластерах. PDB **не защищает** от аварий: падение узла, OOMKill, eviction по MemoryPressure — это involuntary disruptions, и PDB на них не влияет. Кроме того, PDB не создаёт реплики: если реплика одна и `minAvailable: 1`, drain просто зависнет навсегда.

*Что хочет услышать интервьюер:* различие voluntary/involuntary и понимание, что PDB — не замена репликам и антиаффинити.
*Красный флаг:* «PDB гарантирует, что сервис всегда доступен» — нет, он гарантирует только аккуратность при плановых работах.

## 8. Источники (официальная документация)

- **kubernetes.io**, раздел *Concepts → Configuration → «Resource Management for Pods and Containers»* — requests/limits, единицы, ephemeral storage. Поиск: `kubernetes resource management for pods and containers`.
- **kubernetes.io**, *«Pod Quality of Service Classes»* — точные критерии Guaranteed/Burstable/BestEffort и порядок eviction. Поиск: `kubernetes pod quality of service classes`.
- **kubernetes.io**, *«Node-pressure Eviction»* — пороги kubelet, `MemoryPressure`/`DiskPressure`, сортировка подов. Поиск: `kubernetes node-pressure eviction`.
- **kubernetes.io**, *«Assigning Pods to Nodes»* и *«Pod Topology Spread Constraints»* — nodeAffinity, podAffinity/AntiAffinity, spread. Поиск: `kubernetes assigning pods to nodes`.
- **kubernetes.io**, *«Taints and Tolerations»* и *«Safely Drain a Node»* — эффекты taint, cordon/drain. Поиск: `kubernetes taints and tolerations`.
- **kubernetes.io**, *«Horizontal Pod Autoscaling»* — autoscaling/v2, `behavior`, алгоритм и допуск. Поиск: `kubernetes horizontal pod autoscaling`.
- **kubernetes.io**, *«Disruptions»* и *«Specifying a PodDisruptionBudget»* — voluntary/involuntary, `policy/v1`. Поиск: `kubernetes pod disruption budget`.
- **github.com/kubernetes/autoscaler** (`vertical-pod-autoscaler/`) — установка VPA, `updateMode`, компоненты recommender/updater/admission-controller. Поиск: `kubernetes autoscaler vertical pod autoscaler`. Точные режимы и манифесты установки версии-зависимы — `[ПРОВЕРИТЬ]` по README репозитория.
- **github.com/kubernetes-sigs/metrics-server** — установка и флаг `--kubelet-insecure-tls`. Поиск: `metrics-server kubernetes-sigs installation`.
- **kubernetes.io**, *«Resource Quotas»* и *«Limit Ranges»* — ограничения на namespace, чтобы история из этой главы не повторилась. Поиск: `kubernetes resource quotas limit ranges`.
