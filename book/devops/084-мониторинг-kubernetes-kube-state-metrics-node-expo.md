# Мониторинг Kubernetes: kube-state-metrics, node-exporter, cAdvisor, kube-prometheus-stack

## 1. Введение (зачем это нужно в реальной работе)

```
Пользователь: дежурный инженер смены «Вектора», ВМ k8s-cp01 / Grafana
Что написал: «Открыл дашборд Kubernetes / Nodes — панели CPU, RAM и диска пустые.
              Метрики подов при этом рисуются. Через 20 минут показываем нагрузку
              на кластер руководителю.»
Срочность: высокая
```

Кластер Kubernetes — это десятки объектов, которые живут своей жизнью: поды перезапускаются, ноды уходят в NotReady, деплойменты не добирают реплики, диски на нодах тихо заполняются. Без мониторинга вы узнаёте об этом от пользователей, а не от системы. Проблема в том, что «мониторить Kubernetes» — это не один источник метрик, а четыре разных, и у каждого своя зона ответственности: железо ноды, контейнеры, состояние объектов API и само приложение. Если их перепутать, вы будете либо строить графики по мусору (двойной счёт CPU), либо искать причину падения пода там, где её физически нет.

После этой главы вы будете уметь: различать node-exporter, cAdvisor, kube-state-metrics и metrics-server; читать сырые метрики kubelet через API; ставить kube-prometheus-stack через Helm и держать конфигурацию в Git; писать PromQL по контейнерам и объектам кластера; добавлять свои ServiceMonitor и PrometheusRule. Всё это — база, на которой дальше строятся SLO-дашборды и блэкбокс-проверки.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

В Kubernetes метрики приходят из четырёх независимых мест, и Prometheus просто скрейпит их по HTTP.

```text
                 k8s-cp01 10.10.10.61 (control plane)
        ┌──────────────────────────────────────────────────┐
        │  kube-apiserver :6443                            │
        └───────────────▲──────────────────────────────────┘
                        │ watch/list (RBAC, read-only)
        ┌───────────────┴──────────────────┐
        │ kube-state-metrics  :8080        │  СОСТОЯНИЕ ОБЪЕКТОВ
        │ (Deployment, ns monitoring)      │  (реплики, фазы, рестарты)
        └───────────────▲──────────────────┘
                        │ scrape /metrics
   ┌────────────────────┴──────────────────────────────────────┐
   │  Prometheus (ns monitoring, :9090)                        │
   └───▲───────────────────▲───────────────────▲───────────────┘
       │ :9100             │ :10250            │ :10250
┌──────┴───────┐   ┌───────┴────────┐   ┌──────┴──────────────┐
│ node-exporter│   │ kubelet        │   │ kubelet             │
│ DaemonSet    │   │ /metrics       │   │ /metrics/cadvisor   │
│ hostNetwork  │   │ (свои метрики) │   │ (РЕСУРСЫ КОНТЕЙНЕРОВ)│
│ на каждой    │   └────────────────┘   └─────────────────────┘
│ ноде: .61-63 │
└──────────────┘
```

**node-exporter** — отдельный процесс (в кластере — DaemonSet, по поду на каждую ноду), который читает `/proc` и `/sys` хоста и отдаёт метрики «железа»: `node_cpu_seconds_total`, `node_memory_MemAvailable_bytes`, `node_filesystem_avail_bytes`, `node_load1`, `node_network_receive_bytes_total`. Он не знает ничего про Kubernetes. Чтобы видеть реальный хост, а не сетевой namespace пода, его запускают с `hostNetwork: true` и `hostPID: true` и монтируют `/proc`, `/sys`, `/`.

**cAdvisor** (container advisor) — сборщик статистики по cgroups. В современных версиях он **встроен в kubelet** и не ставится отдельно: kubelet отдаёт его метрики на своём защищённом порту **10250** по пути `/metrics/cadvisor`. Именно оттуда берутся `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `container_fs_usage_bytes`. Исторически cAdvisor жил отдельным демоном на порту 4194, но этот режим давно убран `[ПРОВЕРИТЬ для вашей версии Kubernetes]`.

**kube-state-metrics (KSM)** — принципиально другой зверь. Он ничего не измеряет, а через API-сервер читает объекты кластера и превращает их в метрики: `kube_deployment_status_replicas_available`, `kube_pod_container_status_restarts_total`, `kube_node_status_condition`, `kube_pod_container_resource_limits`. Это ответ на вопрос «что задумано и в каком статусе», а не «сколько CPU съедено». Порты: **8080** — метрики, **8081** — собственная телеметрия процесса.

**metrics-server** — четвёртый, часто путаемый компонент. Он не имеет отношения к Prometheus: это сервер resource metrics API, который отдаёт цифры для `kubectl top` и Horizontal Pod Autoscaler. В kube-prometheus-stack его нет.

**kube-prometheus-stack** — Helm-чарт из репозитория `prometheus-community`, который собирает всё вместе: Prometheus Operator (CRD `ServiceMonitor`, `PodMonitor`, `PrometheusRule`, `Probe`), сам Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics и набор готовых правил и дашбордов. Вся настройка скрейпинга описывается Kubernetes-манифестами и лежит в Git — это и есть «мониторинг как код».

## 3. Аналогия из жизни

Представьте завод. **node-exporter** — это счётчики на вводе в здание: сколько электричества, воды и газа потребил цех целиком. **cAdvisor** — счётчики на каждом отдельном станке: сколько конкретно взял этот пресс и этот конвейер. **kube-state-metrics** — диспетчер в конторе: он не меряет ничего, но у него есть журнал нарядов, и он точно знает, что станков по плану пять, работают четыре, один в ремонте, а один простаивает без сырья. Prometheus — курьер, который раз в 30 секунд обходит все три источника и записывает показания в журнал, а Grafana — это витрина с графиками для начальника.

Аналогия перестаёт работать в одном месте: диспетчер KSM пересказывает документы, а не реальность. Если в API написано «реплик 3», он честно отдаст 3 — даже если фактически работают два пода и они падают каждые пять минут. Поэтому метрики состояния и метрики ресурсов нужно смотреть вместе.

## 4. Практика и команды

**1. Метрики контейнеров напрямую из kubelet.** Не нужен ни Prometheus, ни port-forward — `kubectl` проксирует запрос через API-сервер с вашей аутентификацией.

```bash
kubectl get --raw "/api/v1/nodes/k8s-w01/proxy/metrics/cadvisor" | head -20
# HELP container_cpu_usage_seconds_total [ALPHA] Cumulative cpu time consumed in seconds.
# TYPE container_cpu_usage_seconds_total counter
# container_cpu_usage_seconds_total{container="kube-proxy",...} 4211.23
```

Смотреть: список `container`, `namespace`, `pod`. Если видите `container="POD"` — это служебный sandbox-контейнер, его метрики обычно отбрасывают.

**2. Собственные метрики kubelet.** Тот же приём, другой путь — `/metrics` (работа самого kubelet) и `/metrics/resource` (агрегированные ресурсы, набор зависит от версии `[ПРОВЕРИТЬ]`).

```bash
kubectl get --raw "/api/v1/nodes/k8s-w01/proxy/metrics" | grep -E "^kubelet_(node_name|running_pod_count)"
kubelet_node_name{node="k8s-w01"} 1
kubelet_running_pod_count{node="k8s-w01"} 14
```

**3. node-exporter после установки чарта** (под hostNetwork, поэтому доступен по IP ноды):

```bash
curl -s http://10.10.10.62:9100/metrics | grep -E "^node_(load1|memory_MemAvailable_bytes)"
node_load1 0.42
node_memory_MemAvailable_bytes 3.4021e+09
```

**4. `kubectl top` — это НЕ Prometheus.**

```bash
kubectl top nodes
# NAME      CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%
# k8s-cp01  320m          8%     2100Mi          27%
```

Если команда падает с `Metrics API not available` — не установлен metrics-server, это отдельный компонент (ставится отдельно, в чарт не входит). Цифры здесь «мгновенные» и без истории.

**5. Подготовка Helm-репозитория.**

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm search repo prometheus-community/kube-prometheus-stack
helm show values prometheus-community/kube-prometheus-stack > values-baseline.yaml
```

`helm show values` — способ увидеть все параметры без чтения исходников. Файл `values-baseline.yaml` в Git не коммитим, он нужен только для справки.

**6. Values-файл (лежит в Git на GIT01).**

```yaml
# values-kube-prom.yaml
grafana:
  adminPassword: "Lab#2026-vektor"   # только для стенда!
  service:
    type: ClusterIP
prometheus:
  prometheusSpec:
    retention: 7d
    scrapeInterval: 30s
    resources:
      requests: { cpu: 200m, memory: 512Mi }
      limits:   { cpu: "1",  memory: 2Gi }
alertmanager:
  config:
    route:
      receiver: "null"
      group_by: ["namespace", "alertname"]
    receivers:
      - name: "null"
```

**7. Установка и проверка шаблона.**

```bash
helm template kube-prom prometheus-community/kube-prometheus-stack \
  -n monitoring -f values-kube-prom.yaml > /tmp/render.yaml   # посмотреть, что создастся
helm install kube-prom prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f values-kube-prom.yaml
kubectl -n monitoring get pods,svc
```

`helm template` — обязательный шаг: он показывает итоговые манифесты и ловит ошибки в values до применения.

**8. PromQL: CPU контейнеров.**

```promql
sum by (namespace, pod) (
  rate(container_cpu_usage_seconds_total{container!="", container!="POD", image!=""}[5m])
)
```

Почему фильтры: у cAdvisor есть агрегированные ряды уровня пода с пустым `container` — без фильтра вы сложите одно и то же дважды и получите завышенные цифры.

**9. PromQL: память, рестарты и состояние объектов.**

```promql
# working set — то, что реально занимает память и не вытесняется
sum by (namespace, pod) (container_memory_working_set_bytes{container!=""})

# рестарты за час (counter — нужен increase!)
sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[1h])) > 0

# деплойменты, у которых доступных реплик меньше желаемых
kube_deployment_status_replicas_available / kube_deployment_spec_replicas < 1

# ноды не в статусе Ready
kube_node_status_condition{condition="Ready", status="true"} == 0
```

**10. Свой ServiceMonitor (шаблон для приложения с `/metrics`).**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: vektor-app
  namespace: monitoring
  labels:
    release: kube-prom        # ВАЖНО: метка, по которой Prometheus Operator ищет SM
spec:
  selector:
    matchLabels:
      app: vektor-app
  endpoints:
    - port: http-metrics
      path: /metrics
      interval: 30s
```

**11. Своё правило алерта.**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: vektor-k8s-alerts
  namespace: monitoring
  labels:
    release: kube-prom
spec:
  groups:
    - name: vektor.pods
      rules:
        - alert: PodRestartingOften
          expr: increase(kube_pod_container_status_restarts_total[30m]) > 3
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "Под {{ $labels.namespace }}/{{ $labels.pod }} часто перезапускается"
```

**12. Диагностика «почему таргет не скрейпится».**

```bash
kubectl -n monitoring get servicemonitors,podmonitors,prometheusrules
kubectl -n monitoring get endpoints kube-prom-prometheus-node-exporter   # пусто → нет подов/меток
kubectl -n monitoring get svc kube-prom-prometheus-node-exporter --show-labels
```

В GUI Prometheus: **Status → Targets** (какие таргеты UP/DOWN) и **Status → Configuration** (что реально попало в scrape_configs). Имена сервисов зависят от release name — всегда сверяйтесь с выводом `kubectl get svc`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из прошлых глав DevOps-трека поднят: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), все в статусе Ready.
- На `k8s-cp01` есть `kubectl` с доступом к кластеру и `helm` v3.
- На `GIT01` (10.10.10.50) — ваш инфраструктурный репозиторий, куда вы коммитите манифесты с прошлых глав.
- Машины `MON01` (Zabbix) не трогаем: централизованный Prometheus и федерация — темы уроков 81 и 86.

### Снапшот

В VMware Workstation сделайте снапшот `before-devops-084` на **k8s-cp01, k8s-w01, k8s-w02**. Кластер меняется целиком, откат по одной ноде бессмысленен.

```text
VM → Snapshot → Take Snapshot… → имя: before-devops-084
```

### Шаги

1. **k8s-cp01.** Проверить состояние кластера:
   ```bash
   kubectl get nodes -o wide
   kubectl get pods -A | grep -v Running
   ```
   Ожидаемо: три ноды `Ready`, «лишних» подов нет (кроме Completed-джобов).

2. **k8s-cp01.** Посмотреть сырые метрики cAdvisor через API:
   ```bash
   kubectl get --raw "/api/v1/nodes/k8s-w01/proxy/metrics/cadvisor" | grep -c container_cpu_usage_seconds_total
   ```
   Ожидаемо: число в сотнях-тысячах строк. Если `Forbidden` — у вашей учётной записи нет прав на `nodes/proxy` `[ПРОВЕРИТЬ]`.

3. **k8s-cp01.** Создать каталог под мониторинг в репозитории и положить `values-kube-prom.yaml` из раздела 4. Закоммитить.

4. **k8s-cp01.** `helm repo add` + `helm template` (команды из п.7 раздела 4). Ожидаемо: в `/tmp/render.yaml` есть `kind: Prometheus`, `kind: ServiceMonitor`, `kind: DaemonSet` (node-exporter).

5. **k8s-cp01.** `helm install kube-prom ... -n monitoring --create-namespace -f values-kube-prom.yaml`. Ожидаемо: `STATUS: deployed`. Затем:
   ```bash
   kubectl -n monitoring get pods
   ```
   Ожидаемо через 2–3 минуты: `prometheus-kube-prom-prometheus-0` 2/2 Running, `kube-prom-grafana-*` 3/3, `kube-prom-kube-state-metrics-*` 1/1, `kube-prom-prometheus-node-exporter-*` — **три** пода (по одному на ноду).

6. **k8s-cp01.** Проверить node-exporter напрямую по IP нод:
   ```bash
   for ip in 10.10.10.61 10.10.10.62 10.10.10.63; do
     echo -n "$ip: "; curl -s http://$ip:9100/metrics | grep -c '^node_'
   done
   ```
   Ожидаемо: три ненулевых числа. Если пусто — проверьте `hostNetwork: true` у DaemonSet.

7. **k8s-cp01.** Проверить kube-state-metrics через port-forward:
   ```bash
   kubectl -n monitoring port-forward svc/kube-prom-kube-state-metrics 8080:8080 &
   curl -s http://127.0.0.1:8080/metrics | grep -E "^kube_(node_info|pod_info)" | head -5
   ```
   Ожидаемо: строки с `node="k8s-cp01"`, `namespace="monitoring"` и т.п.

8. **k8s-cp01.** Открыть Prometheus и убедиться, что таргеты живы:
   ```bash
   kubectl -n monitoring port-forward svc/kube-prom-kube-prometheus-prometheus 9090:9090
   ```
   В браузере `http://127.0.0.1:9090` → **Status → Targets**. Ожидаемо: `node-exporter`, `kube-state-metrics`, `kubelet`, `cadvisor` — все **UP**.

9. **k8s-cp01.** Выполнить в Prometheus четыре запроса из п.8–9 раздела 4 (CPU, память, рестарты, реплики). Ожидаемо: CPU подов — в ядрах (0.001–0.05 в простое), рестарты — пусто или нули.

10. **k8s-cp01.** Открыть Grafana:
    ```bash
    kubectl -n monitoring port-forward svc/kube-prom-grafana 3000:80
    ```
    Логин `admin`, пароль из values. **Dashboards → Browse**: найти папку с дашбордами Kubernetes, открыть «Compute Resources / Cluster» и «Nodes». Ожидаемо: графики с данными по трём нодам.

11. **k8s-cp01.** Применить свой `PrometheusRule` из п.11 раздела 4:
    ```bash
    kubectl apply -f vektor-k8s-alerts.yaml
    ```
    В Prometheus → **Alerts** убедиться, что правило `PodRestartingOften` появилось в состоянии `inactive`.

12. **k8s-cp01.** Проверить, что правило подхватилось именно через оператор (а не через `prometheus.yml`): `kubectl -n monitoring get prometheusrules`. Ожидаемо: `vektor-k8s-alerts`.

13. **k8s-cp01.** Закоммитить `values-kube-prom.yaml` и `vektor-k8s-alerts.yaml` в репозиторий на GIT01, запушить.

14. **k8s-cp01.** Записать изменения в `stand-log.md`.

### Проверка

- [ ] `kubectl -n monitoring get pods` — все поды `Running`, node-exporter ровно 3 пода.
- [ ] `curl http://10.10.10.61:9100/metrics` (и .62, .63) отдаёт метрики `node_*`.
- [ ] В Prometheus **Status → Targets** все таргеты `UP`, включая `cadvisor` и `kube-state-metrics`.
- [ ] Запрос `kube_node_status_condition{condition="Ready", status="true"}` возвращает 3 ряда со значением `1`.
- [ ] Запрос `sum by (namespace) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))` не содержит namespace `monitoring` со завышенным в разы CPU (признак двойного счёта).
- [ ] В Grafana открывается дашборд по нодам и на нём видны все три узла.
- [ ] `kubectl -n monitoring get prometheusrules` показывает ваше правило, и оно видно в **Alerts**.
- [ ] Values и правило лежат в Git на GIT01.

### Сломай и почини

```
Пользователь: дежурный инженер смены, ВМ k8s-cp01 / Grafana
Что написал: «На дашборде Kubernetes / Nodes панели CPU, RAM и диска пустые.
              Панели подов и деплойментов при этом живые. Смотрю уже 10 минут.»
Срочность: высокая
```

**Как воспроизвести поломку (на k8s-cp01):**

```bash
kubectl -n monitoring delete svc kube-prom-prometheus-node-exporter
```

Сервис удалён, поды node-exporter живы, но у ServiceMonitor нет эндпоинтов.

**Диагностика по порядку — от «кто виноват» к «почему»:**

1. Поды живы? `kubectl -n monitoring get pods -l app.kubernetes.io/name=prometheus-node-exporter` → 3 Running. Значит проблема не в exporter'ах.
2. Сервис есть? `kubectl -n monitoring get svc | grep node-exporter` → пусто. Первая зацепка.
3. Эндпоинты: `kubectl -n monitoring get endpoints kube-prom-prometheus-node-exporter` → `Error from server (NotFound)`. Подтверждает: объекта нет.
4. Prometheus: **Status → Targets** → таргет `node-exporter` в состоянии `DOWN` с ошибкой резолва/подключения. Логично: нет DNS-имени сервиса.
5. Проверяем, что метки ServiceMonitor на месте: `kubectl -n monitoring get servicemonitor kube-prom-prometheus-node-exporter -o yaml | grep -A5 selector`. Сравниваем с `--show-labels` у остальных сервисов — понимаем, какие метки должны быть.
6. Проверяем, что Prometheus вообще видит наши ServiceMonitor'ы (по метке `release`): `kubectl -n monitoring get servicemonitor --show-labels`.

**Решение:** вернуть сервис декларативно, а не «руками через `kubectl create`»:

```bash
helm upgrade kube-prom prometheus-community/kube-prometheus-stack \
  -n monitoring -f values-kube-prom.yaml
kubectl -n monitoring get endpoints kube-prom-prometheus-node-exporter
```

Через 30–60 секунд (`scrapeInterval: 30s`) в Targets всё снова `UP`, панели оживают.

**Ответ пользователю:** «Пропал Service, через который Prometheus собирал метрики нод. Восстановили из Helm-релиза — панели Nodes снова с данными, история за период простоя не пишется, но новые точки уже идут. Проверьте дашборд.»

**Закрытие заявки:** в комментарии — что удалён Service (человеческая ошибка или неудачный `kubectl apply`), как восстановили и что добавили в чек-лист «не удалять объекты в namespace monitoring вручную».

### Что изменилось на стенде

- Namespace `monitoring` на кластере k8s-cp01/w01/w02.
- Helm-релиз `kube-prom` (kube-prometheus-stack): Prometheus `:9090`, Alertmanager `:9093`, Grafana `:3000` (внутри пода), Prometheus Operator.
- DaemonSet node-exporter, по поду на каждой ноде, `hostNetwork`, порт **9100** на 10.10.10.61/.62/.63.
- Deployment kube-state-metrics, порт **8080**.
- PrometheusRule `vektor-k8s-alerts` в namespace `monitoring`.
- Файлы `values-kube-prom.yaml`, `vektor-k8s-alerts.yaml` в Git на GIT01.
- Retention Prometheus — 7 дней, хранилище `emptyDir` (данные теряются при пересоздании пода).

### Откат

```text
VM → Snapshot Manager → before-devops-084 → Revert
```

Откатывать все три ноды. Если нужен мягкий откат без снапшота:

```bash
helm -n monitoring uninstall kube-prom
kubectl delete namespace monitoring
```

При этом CRD оператора (`kubectl get crd | grep monitoring.coreos.com`) остаются — удаляйте их только осознанно: они кластерные и затронут другие релизы, если те появятся.

## 6. Ошибки новичков и безопасность

1. **Считать, что `kubectl top` = мониторинг.** → Нет истории, нет алертов, нет разреза по namespace за неделю. → Для истории ставим Prometheus + kube-state-metrics, `kubectl top` оставляем для быстрой проверки.
2. **Считать CPU контейнеров без фильтров.** `sum(rate(container_cpu_usage_seconds_total[5m]))` без `container!=""` → двойной счёт: агрегированные ряды пода плюс ряды контейнеров. → Всегда `container!="", container!="POD"`.
3. **Путать kube-state-metrics и cAdvisor.** → Ищут «сколько CPU ест под» в KSM или «сколько реплик должно быть» в cAdvisor и не находят. → Ресурсы — cAdvisor/node-exporter, состояние объектов — KSM.
4. **Запускать node-exporter как Deployment или без hostNetwork.** → Метрики контейнера пода вместо хоста; при нескольких репликах часть нод без метрик вообще. → Только DaemonSet, `hostNetwork: true`, `hostPID: true`, монтирование `/proc`, `/sys`.
5. **«Мой ServiceMonitor не подхватывается».** По умолчанию чарт ставит `serviceMonitorSelectorNilUsesHelmValues: true`, и Prometheus выбирает только ServiceMonitor'ы с меткой `release: <release-name>`. → Либо ставим метку, либо явно настраиваем селектор в values.
6. **Ждать метрик от etcd, scheduler, controller-manager «из коробки».** На kubeadm-кластерах они часто слушают `127.0.0.1`, и таргеты висят `DOWN`. → В values этих компонентов надо указать реальные адреса или отключить их (`kubeEtcd.enabled: false` и т.д.) `[ПРОВЕРИТЬ под свой кластер]`.
7. **Хранить 90 дней метрик на трёх нодах по 4 ГБ RAM.** → Prometheus съедает память и падает по OOM. → Retention 7d на стенде, `limits` в values, долгосрочное хранение — через Thanos/VictoriaMetrics (урок 81).
8. **Алерт `up == 0` без `for`.** → Флаппинг и «алерт-шторм» на каждое обновление DaemonSet. → `for: 5m` минимум и подавление в Alertmanager (урок 80).

**Безопасность.** Prometheus, Grafana и Alertmanager в этой лабе доступны только через `port-forward` — это правильно, наружу их выставлять нельзя: у Prometheus нет аутентификации, у Grafana в бою должен быть SSO или хотя бы сильный пароль и HTTPS за ingress. Пароль `Lab#2026-vektor` из values в Git — учебный; в реальном репозитории секреты кладутся в Secret/Sealed Secrets/SOPS, а не открытым текстом. kube-state-metrics имеет cluster-wide read на объекты API — не расширяйте его права «на всякий случай» и не выдавайте его ServiceAccount прав на секреты. И помните: `kubectl get --raw` на `nodes/proxy` фактически даёт доступ к внутренним эндпоинтам kubelet — в командных кластерах это право дают ограниченному кругу лиц.

## 7. Вопросы на собеседовании

**1. Чем отличаются метрики node-exporter, cAdvisor и kube-state-metrics и что вы будете смотреть каждыми?**

Ответ: node-exporter — состояние хоста: CPU, память, диски, сеть, load average; он живёт вне Kubernetes-логики. cAdvisor встроен в kubelet и отдаёт потребление контейнеров через cgroups: `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`. kube-state-metrics читает API-сервер и отдаёт состояние объектов: сколько реплик доступно, сколько рестартов, в какой фазе под, какие requests/limits. Первые два отвечают «сколько потрачено», третий — «что задумано и что по факту».

*Что хочет услышать:* что вы понимаете разделение «ресурсы» vs «состояние» и назовёте хотя бы по две реальные метрики из каждой группы.
*Красный флаг:* «это всё одно и то же, kube-state-metrics собирает метрики подов».

**2. Как посчитать CPU пода в PromQL и почему нельзя брать `container_cpu_usage_seconds_total` напрямую?**

Ответ: это counter — берём `rate(...[5m])`. Обязательны фильтры `container!=""` (иначе плюсуются агрегированные ряды уровня пода) и `container!="POD"` (sandbox-контейнер). Итог: `sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!="", container!="POD"}[5m]))`. Если нужны ядра относительно лимита — делим на `kube_pod_container_resource_limits{resource="cpu"}` из KSM.

*Что хочет услышать:* понимание counter vs rate и типичной ловушки двойного счёта.
*Красный флаг:* «просто `sum(container_cpu_usage_seconds_total)`» без rate и фильтров.

**3. Зачем Prometheus Operator и ServiceMonitor, если есть `scrape_configs`?**

Ответ: ServiceMonitor — CRD, которая описывает, какие Service и по какому порту/пути скрейпить. Оператор следит за этими объектами и генерирует конфиг Prometheus сам, перезагружая его без ручного рестарта. Плюс: конфигурация мониторинга живёт рядом с приложением в Git, ревьюится в MR и не требует доступа к файлу `prometheus.yml` на сервере. Цена — ещё один слой абстракции: если ServiceMonitor не попал в селектор Prometheus (по метке `release`), таргета не будет, и это надо уметь диагностировать.

*Что хочет услышать:* «декларативность и GitOps вместо ручного конфига» + знание, что селектор по меткам — источник типовых проблем.
*Красный флаг:* «ServiceMonitor — это то же самое, что Service».

**4. Нода ушла в NotReady. Какими метриками вы будете разбираться?**

Ответ: сначала `kube_node_status_condition{condition="Ready", status="true"} == 0` — подтверждаем проблему из KSM. Дальше смотрим, жив ли таргет этой ноды: `up{job="node-exporter"}` — если 0, проблема на уровне сети/хоста (машина недоступна, упал kubelet, переполнен диск). Если таргет жив, а нода NotReady — смотрим `node_filesystem_avail_bytes` (диск заполнен — классика), `node_memory_MemAvailable_bytes`, `node_load1`, `kubelet_running_pod_count`. Параллельно `kubectl describe node` и события — метрики показывают «что», события и логи kubelet — «почему».

*Что хочет услышать:* порядок «подтвердил → локализовал слой (хост/сеть/kubelet) → пошёл в логи».
*Красный флаг:* «перезагружу ноду» без единого запроса.

**5. Как поймать CrashLoopBackOff и почему cAdvisor один этого не покажет?**

Ответ: рестарты видны только в KSM — `increase(kube_pod_container_status_restarts_total[30m]) > 3`, плюс `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1`. cAdvisor покажет лишь короткие всплески потребления и обрывы рядов, потому что контейнер живёт секунды. Дальше — `kubectl logs --previous`, `kubectl describe pod` и алерт с `for: 10m`, чтобы не срабатывать на одиночный рестарт при деплое.

*Что хочет услышать:* знание конкретной метрики KSM и того, что метрики состояния — единственный надёжный признак CrashLoop.
*Красный флаг:* «посмотрю в Grafana глазами».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Metrics for Kubernetes system components» (эндпоинты kubelet `/metrics`, `/metrics/cadvisor`, `/metrics/resource`); раздел «Resource metrics pipeline». Поиск: `kubernetes kubelet metrics endpoints cAdvisor`.
- **github.com/kubernetes/kube-state-metrics** — README и каталог `docs/` со списком всех метрик и требованием RBAC. Поиск: `kube-state-metrics exposed metrics list`.
- **github.com/google/cadvisor** — README, раздел про storage/metrics. Поиск: `cadvisor container metrics cgroups v2`.
- **github.com/prometheus-community/helm-charts** — чарт `kube-prometheus-stack`: README с параметрами values и FAQ. Поиск: `kube-prometheus-stack values grafana adminPassword serviceMonitorSelectorNilUsesHelmValues`.
- **prometheus.io** — раздел «Configuration» (`scrape_configs`, relabeling) и «Querying basics» (PromQL, `rate`, `increase`). Поиск: `prometheus promql rate increase counter`.
- **github.com/prometheus-operator/prometheus-operator** — документация по CRD `ServiceMonitor`, `PodMonitor`, `PrometheusRule`. Поиск: `prometheus operator ServiceMonitor selector documentation`.
- **prometheus.io/download** (репозиторий node_exporter) — README с коллекторами и флагами. Поиск: `node_exporter collectors flags hostNetwork kubernetes`.
- **grafana.com** — раздел Dashboards, поиск готовых дашбордов Kubernetes. Поиск: `grafana dashboards kubernetes cluster node exporter`.

Версии чарта, точные имена сервисов и набор эндпоинтов kubelet зависят от версии Kubernetes и релиза чарта — сверяйтесь с `helm show values` и `kubectl get svc` на своём стенде, а не с чужими скриншотами.
