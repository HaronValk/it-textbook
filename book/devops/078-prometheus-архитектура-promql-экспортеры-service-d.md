# Prometheus: архитектура, PromQL, экспортеры, service discovery, recording/alerting rules

## 1. Введение (зачем это нужно в реальной работе)

**Тикет в helpdesk «Вектора»:**

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «После обеда 1С и файловая шара отвечают по 10–15 секунд, утром всё летало. Вы говорили, что серверы под мониторингом — почему я узнаю о проблеме раньше вас?»
> **Срочность:** средняя

Пока ты работал на L1/L2, мониторинг означал «зайти на сервер и посмотреть `htop`». С ростом инфраструктуры (а на DevOps-треке у тебя уже GIT01, HARBOR01, WEB01 в DMZ и три узла Kubernetes) такой подход умирает: серверов больше, чем времени, а проблема обычно проявляется раньше, чем пользователь напишет в чат.

Prometheus — де-факто стандарт сбора метрик в облачно-нативной среде. Он стоит внутри почти каждого кластера Kubernetes, его язык PromQL спрашивают на собеседованиях на любую DevOps-позицию, а формат метрик `metric_name{label="value"} 123` стал универсальным: его отдают nginx, Postgres, Node.js, Java, сам Kubernetes.

После этой главы ты будешь уметь: поднимать Prometheus и экспортеры, описывать цели скрейпа через файловый service discovery, писать запросы PromQL (включая `rate` и `histogram_quantile`), строить recording-правила, которые ускоряют дашборды, и alerting-правила, которые срабатывают раньше, чем зазвонит телефон. Всё — в виде YAML в Git.

## 2. Теория «под капотом»

### Pull-модель

Ключевое отличие Prometheus от Zabbix/Agent-based систем: он **сам ходит к цели** (pull), а не ждёт, что цель пришлёт данные (push). Каждые `scrape_interval` (по умолчанию 15 секунд) Prometheus делает HTTP GET на `http://<target>/metrics`, получает текстовый список метрик и складывает их в свою базу.

```text
                        ┌───────────────────────────────────────┐
                        │      MON01  10.10.10.40               │
                        │  ┌─────────────────────────────────┐  │
                        │  │ Prometheus Server               │  │
                        │  │  ┌──────────┐  ┌─────────────┐  │  │
   ┌──────────────┐     │  │  │Retrieval │─▶│ TSDB (WAL + │  │  │
   │ UBU01 .30    │◀────┼──┼──┤ (scrape) │  │ 2h-блоки)   │  │  │
   │ node_exp:9100│ HTTP│  │  └──────────┘  └──────┬──────┘  │  │
   └──────────────┘     │  │                       │         │  │
   ┌──────────────┐     │  │  ┌────────────────────▼──────┐  │  │
   │ GIT01 .50    │◀────┼──┼──┤ Rule Engine (evaluation)  │  │  │
   │ node_exp:9100│     │  │  │  recording + alerting     │  │  │
   └──────────────┘     │  │  └────────────┬──────────────┘  │  │
   ┌──────────────┐     │  │               │                 │  │
   │ WEB01 .20.10 │◀────┼──┼──┐            ▼                 │  │
   │ (DMZ, GW01)  │     │  │  │   ┌─────────────────┐        │  │
   └──────────────┘     │  │  │   │  Alertmanager   │──▶ TG  │  │
                        │  │  │   │      :9093      │        │  │
                        │  │  │   └─────────────────┘        │  │
                        │  └──┼──────────────────────────────┘  │
                        │     │  HTTP UI / API :9090            │
                        └─────┴──────────────────────────────────┘
```

### Из чего состоит

| Компонент | Порт по умолчанию | Роль |
|---|---|---|
| Prometheus Server | 9090 | скрейпинг, TSDB, PromQL, HTTP API и UI, движок правил |
| node_exporter | 9100 | метрики Linux (CPU, RAM, диски, сеть) |
| blackbox_exporter | 9115 | проверки «снаружи»: ICMP, HTTP, TCP (урок 85) |
| Pushgateway | 9091 | приём push от короткоживущих задач |
| Alertmanager | 9093 | маршрутизация и подавление алертов (урок 80) |

### Модель данных

Одна точка — это **имя метрики + набор лейблов + время + значение**. Имя метрики технически тоже лейбл `__name__`.

```text
node_filesystem_avail_bytes{device="/dev/sda2",fstype="ext4",instance="10.10.10.30:9100",job="node",mountpoint="/"} 41234567168
```

Четыре типа метрик:
- **counter** — только растёт (`node_cpu_seconds_total`, `http_requests_total`). К ним применяют `rate()`.
- **gauge** — произвольное значение туда-сюда (`node_memory_MemAvailable_bytes`, `up`).
- **histogram** — распределение в бакетах (`*_bucket`, `*_sum`, `*_count`), из него считают перцентили.
- **summary** — квантили, посчитанные на стороне клиента (в PromQL не агрегируются, поэтому в проде предпочитают histogram).

### Цепочка скрейпа и service discovery

Скрейп-конфиг описывает **job** — логическую группу целей. Цели откуда-то берутся: статикой (`static_configs`) или автоматически через **service discovery** — `file_sd` (файлы JSON/YAML), `dns_sd`, `kubernetes_sd`, `consul_sd`. До скрейпа Prometheus присваивает каждой цели служебные лейблы `__address__`, `__scheme__`, `__metrics_path__`, `__meta_*` (для SD), а **relabeling** позволяет их переписать или отбросить цель.

### Где это в модели OSI

Prometheus работает на прикладном уровне (L7): HTTP поверх TCP. Сетевой доступ нужен только в одну сторону — от Prometheus к экспортеру на порт 9100. Если между ними маршрутизатор (GW01 между офисной сетью и DMZ), правило файрвола открывается **на стороне цели**.

### Правила

Движок правил раз в `evaluation_interval` прогоняет выражения:
- **recording rule** — считает выражение и сохраняет результат как новую метрику (ускоряет тяжёлые запросы дашбордов);
- **alerting rule** — считает выражение и, если оно истинно дольше `for`, переводит алерт в состояние `pending` → `firing` и отдаёт в Alertmanager.

## 3. Аналогия из жизни

Представь обходчика на заводе. У него есть маршрутный лист (scrape_config), и он лично обходит посты (targets): заходит на каждый, снимает показания счётчиков и записывает в журнал (TSDB). Если пост закрыт — он так и помечает: «не дошёл» (метрика `up = 0`). Отдельный аналитик (rule engine) раз в 15 минут просматривает журнал и делает выжимки (recording rules), а если видит, что давление в котле растёт — пишет заявку диспетчеру (Alertmanager). Посты сами никогда не звонят: если обходчик не пришёл — никто и не заметит.

Аналогия ломается на короткоживущих задачах: обходчик приходит раз в 15 секунд, а cron-задача живёт 3 секунды и умирает. Для них и нужен Pushgateway — «почтовый ящик», куда такие задачи кладут показания, а обходчик забирает их оттуда.

## 4. Практика и команды

Все команды — для MON01 (10.10.10.40), Ubuntu Server 24.04.

**1. Скачать и распаковать Prometheus**

```bash
VER=3.5.0   # [ПРОВЕРИТЬ] актуальный номер на github.com/prometheus/prometheus/releases
cd /tmp
curl -LO https://github.com/prometheus/prometheus/releases/download/v${VER}/prometheus-${VER}.linux-amd64.tar.gz
tar xzf prometheus-${VER}.linux-amd64.tar.gz
ls prometheus-${VER}.linux-amd64/
```

В архиве: `prometheus`, `promtool`, `prometheus.yml`, `consoles/`. `promtool` — утилита проверки конфигов и правил, ей ты будешь пользоваться постоянно.

**2. Создать системного пользователя и каталоги**

```bash
sudo useradd --no-create-home --shell /usr/sbin/nologin prometheus
sudo mkdir -p /etc/prometheus/rules /etc/prometheus/targets /var/lib/prometheus
sudo cp prometheus-${VER}.linux-amd64/{prometheus,promtool} /usr/local/bin/
sudo chown -R prometheus:prometheus /etc/prometheus /var/lib/prometheus
```

Ключи `useradd`: `--no-create-home` — не создавать домашний каталог, `--shell nologin` — запретить логин. Сервис не должен бежать под root.

**3. Проверить конфиг перед запуском**

```bash
promtool check config /etc/prometheus/prometheus.yml
# SUCCESS: /etc/prometheus/prometheus.yml is valid prometheus config file syntax
```

Проверяет синтаксис плюс все файлы правил из `rule_files`. Эту команду ставят в CI — чтобы битый YAML не доехал до прода.

**4. Запуск вручную (для отладки)**

```bash
sudo -u prometheus /usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.listen-address=:9090 \
  --web.enable-lifecycle
```

`--storage.tsdb.path` — где лежит TSDB, `--web.enable-lifecycle` — разрешает `POST /-/reload` без рестарта, `--web.listen-address` — адрес UI/API. Если порт 9090 занят (его любит Cockpit) — получишь `bind: address already in use`.

**5. Основной конфиг `prometheus.yml`**

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    env: vektor-lab

rule_files:
  - /etc/prometheus/rules/*.yml

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['10.10.10.40:9090']

  - job_name: node
    static_configs:
      - targets:
          - '10.10.10.40:9100'
          - '10.10.10.30:9100'
          - '10.10.10.50:9100'
          - '10.10.20.10:9100'
        labels:
          team: infra
```

`scrape_interval` — как часто ходим за метриками, `evaluation_interval` — как часто считаем правила, `external_labels` — метки, которые добавляются при отправке данных наружу (remote write, федерация).

**6. Проверить, что экспортер отдаёт метрики**

```bash
curl -s http://10.10.10.30:9100/metrics | grep -E '^(node_load1|node_boot_time_seconds)'
# node_boot_time_seconds 1.7612345e+09
# node_load1 0.08
```

Формат exposition: `# HELP`, `# TYPE`, затем `имя{лейблы} значение`. Если `curl` вернул `Connection refused` — сервис не запущен, если висит — файрвол.

**7. Проверить состояние целей через API**

```bash
curl -s http://10.10.10.40:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, instance: .scrapeUrl, health: .health, err: .lastError}'
```

`.health` = `up`/`down`, `.lastError` покажет текст ошибки скрейпа. Без `jq`: `| python3 -m json.tool`.

**8. PromQL: базовые запросы в UI (вкладка Graph)**

```promql
up                                   # 1 = скрейп успешен, 0 = нет
up{job="node"} == 0                  # кто лежит
count by (job) (up == 1)             # сколько целей живо в каждом job
100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100
node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes * 100
```

`rate(x[5m])` — средняя скорость роста counter'а за 5 минут, в секунду. Окно бери минимум в 4 раза больше `scrape_interval`, иначе получишь рваный график: для 15s это 1m, но лучше `[5m]`.

**9. PromQL: диски и перцентили**

```promql
100 * node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
    / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"} < 15

histogram_quantile(0.95,
  sum by (le) (rate(prometheus_http_request_duration_seconds_bucket[5m]))
)
```

Первое — «диск заполнен больше чем на 85%». Второе — 95-й перцентиль времени ответа API самого Prometheus. `histogram_quantile` работает только с `_bucket` и обязательным `le` в `by(...)`.

**10. Service discovery через файл**

```yaml
  - job_name: node
    file_sd_configs:
      - files:
          - /etc/prometheus/targets/*.json
        refresh_interval: 30s
```

`/etc/prometheus/targets/node-lab.json`:

```json
[
  {
    "targets": ["10.10.10.40:9100", "10.10.10.30:9100", "10.10.10.50:9100"],
    "labels": {"zone": "office", "team": "infra"}
  },
  {
    "targets": ["10.10.20.10:9100"],
    "labels": {"zone": "dmz", "team": "infra"}
  }
]
```

Prometheus перечитывает файлы сам, без рестарта и без reload. Так удобно делать, когда список целей генерирует Ansible или CI.

**11. Recording rule**

`/etc/prometheus/rules/node-recording.yml`:

```yaml
groups:
  - name: node-recording
    interval: 30s
    rules:
      - record: instance:node_cpu_utilisation:rate5m
        expr: 100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100
      - record: instance:node_fs_avail_percent
        expr: 100 * node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
              / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}
```

Соглашение об именах: `level:metric:operation` (`instance:node_cpu_utilisation:rate5m`) — сразу видно, что это запись, а не сырая метрика.

**12. Alerting rule**

`/etc/prometheus/rules/node-alerts.yml`:

```yaml
groups:
  - name: node-alerts
    rules:
      - alert: NodeExporterDown
        expr: up{job="node"} == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Экспортер {{ $labels.instance }} недоступен"
          description: "Prometheus не может скрейпить {{ $labels.instance }} 5 минут."

      - alert: DiskWillFillIn4Hours
        expr: predict_linear(node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}[1h], 4*3600) < 0
        for: 10m
        labels:
          severity: critical
```

`for: 5m` — «истинно 5 минут подряд, тогда firing», защита от мигания. `{{ $labels.X }}` и `{{ $value }}` — шаблоны в аннотациях.

**13. Проверить правила и перезагрузить конфиг**

```bash
promtool check rules /etc/prometheus/rules/*.yml
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://10.10.10.40:9090/-/reload
# 200
curl -s http://10.10.10.40:9090/api/v1/rules | jq '.data.groups[].rules[] | {name, health: .health}'
```

`POST /-/reload` работает только с `--web.enable-lifecycle`. Ответ не 200 — смотри логи `journalctl -u prometheus`.

## 5. Лабораторная работа

### Предусловия

- **MON01** 10.10.10.40, Ubuntu Server 24.04, доступ по SSH, есть Zabbix с L2 (порт 9090 может быть занят Cockpit — проверим).
- **UBU01** 10.10.10.30 и **GIT01** 10.10.10.50 — доступны, SSH работает.
- **WEB01** 10.10.20.10 — в DMZ, маршрут до неё идёт через **GW01** (10.10.10.3 / 10.10.20.1).
- На GIT01 есть GitLab CE, у тебя есть учётка и проект `monitoring/prometheus`.
- Windows-часть стенда для этой главы не нужна.

### Снапшот

В VMware Workstation на **MON01**, **UBU01**, **WEB01**, **GIT01**: меню *VM → Snapshot → Take Snapshot*, имя `before-devops-078`. Или из командной строки хоста:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "C:\VMs\MON01\MON01.vmx" before-devops-078
```

### Шаги

1. **MON01.** Проверить занятость порта: `sudo ss -tlnp | grep -E ':9090|:9100'`. Ожидаемо: пусто. Если 9090 занят Cockpit — либо `sudo systemctl disable --now cockpit.socket`, либо в юните `--web.listen-address=:9091` (тогда во всех командах ниже меняешь порт).
2. **MON01.** Скачать и распаковать Prometheus (команды 1–2 из раздела 4). Ожидаемо: в `/usr/local/bin/` появились `prometheus` и `promtool`, пользователь `prometheus` создан.
3. **Все четыре хоста.** Поставить node_exporter: скачать архив с github.com/prometheus/node_exporter/releases, распаковать, `sudo cp node_exporter /usr/local/bin/`, создать юнит `/etc/systemd/system/node_exporter.service`:

   ```ini
   [Unit]
   Description=Prometheus Node Exporter
   After=network-online.target
   [Service]
   User=node_exporter
   Group=node_exporter
   Type=simple
   ExecStart=/usr/local/bin/node_exporter
   Restart=always
   [Install]
   WantedBy=multi-user.target
   ```

   Затем `sudo systemctl daemon-reload && sudo systemctl enable --now node_exporter`. Ожидаемо: `active (running)`, `curl -s localhost:9100/metrics | wc -l` даёт сотни строк.
4. **UBU01, GIT01, WEB01.** Открыть 9100 только для MON01:
   ```bash
   sudo ufw allow from 10.10.10.40 to any port 9100 proto tcp
   ```
   Ожидаемо: `sudo ufw status` показывает правило. На WEB01 дополнительно убедиться, что GW01 пропускает трафик из 10.10.10.0/24 в DMZ.
5. **MON01.** Проверить доступность всех экспортеров с Prometheus-сервера:
   ```bash
   for t in 10.10.10.40 10.10.10.30 10.10.10.50 10.10.20.10; do
     echo -n "$t: "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 http://$t:9100/metrics
   done
   ```
   Ожидаемо: четыре раза `200`. Любой другой код — стоп, дальше идти смысла нет.
6. **MON01.** Положить `prometheus.yml` в редакции со `static_configs` (команда 5), выполнить `promtool check config`. Ожидаемо: `SUCCESS`. Запустить вручную (команда 4), убедиться, что в логе нет `level=error`, нажать Ctrl+C.
7. **MON01.** Создать юнит `/etc/systemd/system/prometheus.service` с `ExecStart` из команды 4, `Restart=always`, `User=prometheus`, `WantedBy=multi-user.target`. `sudo systemctl daemon-reload && sudo systemctl enable --now prometheus`. Проверить:
   ```bash
   curl -s http://10.10.10.40:9090/-/ready    # Prometheus Server is Ready.
   curl -s http://10.10.10.40:9090/-/healthy  # Prometheus Server is Healthy.
   ```
8. **MON01.** Открыть `http://10.10.10.40:9090/targets`. Ожидаемо: 5 целей (`prometheus` + 4 `node`), все `UP`. Выполнить `up`, потом `up == 0` — второй запрос пустой. Записать в `stand-log.md`: «MON01 — Prometheus 3.x на :9090, node_exporter на 4 хостах».
9. **MON01.** Перевести job `node` на файловый service discovery (команда 10): заменить `static_configs` на `file_sd_configs`, создать `node-lab.json`, `promtool check config`, `curl -X POST .../-/reload`. Ожидаемо: цели те же, но у `10.10.20.10:9100` появился лейбл `zone="dmz"`. Проверить запросом `count by (zone) (up)` — должно быть `dmz 1`, `office 3`.
10. **MON01.** Добавить оба файла правил (команды 11–12), `promtool check rules`, `POST /-/reload`. Открыть вкладку *Alerts*: два правила в состоянии `inactive`. Проверить, что recording-метрика появилась: запрос `instance:node_cpu_utilisation:rate5m` возвращает 4 series.
11. **MON01 → UBU01.** Сломать и посмотреть: `sudo systemctl stop node_exporter` на UBU01. В течение 15–30 секунд `up{instance="10.10.10.30:9100"}` становится `0`, на вкладке *Alerts* правило `NodeExporterDown` переходит в `pending` с обратным отсчётом. Через 5 минут — `firing`, метрика `ALERTS{alertname="NodeExporterDown",alertstate="firing"}` = 1.
12. **GIT01.** Закоммитить `prometheus.yml`, `node-lab.json`, оба файла правил и юниты в проект `monitoring/prometheus`, тег `v0.1.0`. Правила и конфиг теперь версионируются, а не живут только на сервере.

### Проверка

- [ ] `curl http://10.10.10.40:9090/-/ready` возвращает `Prometheus Server is Ready.`
- [ ] На `/targets` ровно 5 целей, health у всех `UP`
- [ ] `up{job="node"} == 0` до поломки пуст, `count by (zone) (up)` даёт `office 3`, `dmz 1`
- [ ] `instance:node_cpu_utilisation:rate5m` возвращает 4 series
- [ ] `promtool check config` и `promtool check rules` — оба `SUCCESS`
- [ ] На вкладке *Alerts* два правила, оба `inactive`
- [ ] После `systemctl stop node_exporter` на UBU01 алерт доходит до `firing` за ~5 минут
- [ ] Конфиги и правила закоммичены в GitLab на GIT01

### Сломай и почини

> **Пользователь:** Алексей Сидоров (sidorov, склад), ВМ WIN11-CL01 → UBU01
> **Что написал:** «Вчера складской сервис на UBU01 не открывался минут двадцать, мы вам звонили. Вы говорили, что всё под мониторингом — почему я узнал о проблеме раньше, чем вы? Разберитесь, пожалуйста.»
> **Срочность:** средняя

**Воспроизведение поломки.** На UBU01: `sudo systemctl stop node_exporter`. Дополнительно сымитируй вторую ошибку — «забыли подключить файл алертов»: убери `node-alerts.yml` из каталога `rules/` (или переименуй в `node-alerts.yml.bak`) и сделай `POST /-/reload`. Prometheus продолжит работать, метрики соберёт, но алертов не будет — типичная и очень обидная ситуация.

**Диагностика по шагам:**

1. **Prometheus → Status → Targets.** Цель `10.10.10.30:9100` в состоянии `DOWN`, в колонке Error — `context deadline exceeded` или `connection refused`. Уже понятно, что дело в цели, а не в самом Prometheus.
2. **Ресурсы самого Prometheus.** `curl -s http://10.10.10.40:9090/api/v1/query?query=up | jq '.data.result[] | select(.value[1]=="0")'` — убедиться, что упала только одна цель, а не все (если все — проблема в сети MON01 или в дисках TSDB).
3. **Сетевая доступность.** С MON01: `curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 http://10.10.10.30:9100/metrics`. `000` = соединения нет, `200` = сеть в порядке и проблема в конфиге скрейпа.
4. **Состояние сервиса на цели.** На UBU01: `systemctl status node_exporter --no-pager` и `journalctl -u node_exporter -n 30 --no-pager`. Ожидаемо: `inactive (dead)` либо `failed` с причиной в логе.
5. **Последний рубеж — почему не пришёл алерт.** Вкладка *Alerts* пуста → смотрим `curl -s http://10.10.10.40:9090/api/v1/rules | jq '.data.groups'` — `[]`. Значит, файл правил не подхвачен.
6. **Проверка конфига.** `promtool check config /etc/prometheus/prometheus.yml` — покажет, если какой-то `rule_files` не найден (`SUCCESS` при отсутствии файлов тоже возможен, поэтому проверяй именно список в API).

**Решение:** вернуть `node-alerts.yml` на место, `promtool check rules`, `POST /-/reload`, проверить в API, что группа `node-alerts` появилась и правило в `pending`. Поднять сервис: `sudo systemctl start node_exporter && sudo systemctl enable node_exporter`. Дождаться `up == 1` и `ALERTS{alertname="NodeExporterDown"}` → пусто.

**Ответ пользователю:** «Алексей, спасибо за сигнал. Мы разобрались: сервис мониторинга на UBU01 останавливался, а правило оповещения не было загружено — поэтому мы не узнали сразу. Сейчас сервис поднят, правило восстановлено, проверка идёт каждые 15 секунд, алерт срабатывает через 5 минут недоступности. Добавили проверку конфигов в обязательный чек-лист при изменениях.»

**Закрытие заявки:** категория «Мониторинг», приоритет средний, в решении — ссылка на коммит с исправленным `prometheus.yml` в GitLab и запись в `stand-log.md`. Отдельным пунктом завести задачу «настроить доставку алертов в Telegram через Alertmanager» — это урок 80.

### Что изменилось на стенде

- **MON01 (10.10.10.40):** установлены Prometheus (порт 9090, `--web.enable-lifecycle`, TSDB в `/var/lib/prometheus`) и node_exporter (9100). Каталоги `/etc/prometheus/{rules,targets}`. Сервисы включены в автозапуск.
- **UBU01 (10.10.10.30), GIT01 (10.10.10.50), WEB01 (10.10.20.10):** node_exporter на 9100, ufw разрешает 9100 только с 10.10.10.40.
- **GIT01:** новый проект `monitoring/prometheus`, тег `v0.1.0`.
- **Цели скрейпа:** job `prometheus` (self, 10.10.10.40:9090) и job `node` через `file_sd` из `/etc/prometheus/targets/node-lab.json`.
- **Правила:** `node-recording.yml` (2 записи), `node-alerts.yml` (2 алерта, `NodeExporterDown` с `for: 5m`).
- **Порты:** 9090/tcp и 9100/tcp — новые открытые порты в стенде.
- **Задолженность на будущее:** Alertmanager не настроен, доставка уведомлений отсутствует (урок 80).

### Откат

Если что-то пошло не так на шаге N — VMware Workstation, меню *VM → Snapshot → Snapshot Manager*, выбрать `before-devops-078`, *Go To*. Откатывать нужно **все четыре** ВМ, иначе Prometheus будет помнить цели, которых уже нет. После отката запустить ВМ и проверить `systemctl status prometheus node_exporter` — состояние сервисов откатывается вместе с диском. Если откат нежелателен (например, жалко уже написанные правила), минимальный путь назад: `sudo systemctl disable --now prometheus node_exporter`, `sudo rm -rf /etc/prometheus /var/lib/prometheus /usr/local/bin/prometheus /usr/local/bin/promtool`, удалить юниты и `daemon-reload`, закрыть порт правилом ufw `delete`.

## 6. Ошибки новичков и безопасность

1. **Окно `rate()` меньше интервала скрейпа** → график рваный, `rate()` возвращает пустоту. → Бери окно минимум 4×`scrape_interval`: для 15s это `[1m]`, практично `[5m]`.
2. **`rate()` к gauge** → бессмысленные цифры. → `rate`/`increase` только для счётчиков (`*_total`), для gauge — `avg_over_time`, `max_over_time` или прямое значение.
3. **Лейблы с высокой кардинальностью** → `user_id`, `request_id`, `url` с query-параметрами в лейблах дают миллионы series, TSDB съедает RAM и диск, Prometheus падает по OOM. → Держи число series в сотнях тысяч; идентификаторы — в логи и трейсы, не в метрики.
4. **Алерт без `for`** → алерт срабатывает на каждом кратковременном всплеске, дежурный перестаёт на них реагировать (alert fatigue). → `for` минимум 2–5 минут для warning.
5. **Правило существует, но не подключено** → на вкладке *Alerts* пусто, и это неочевидно. → После каждого изменения `promtool check config` + проверка `api/v1/rules`; в CI — тест `promtool test rules`.
6. **Правка `prometheus.yml` прямо на сервере, без Git** → через месяц никто не знает, почему конфиг такой. → Всё в GitLab, деплой через CI или Ansible, сервер — расходник.
7. **Скрейп всего подряд «на всякий случай»** → тысячи целей, растущий диск, падение производительности. → Собирай метрики под конкретные SLO и алерты (урок 77).
8. **Один Prometheus на всё, без резерва** → падает MON01, история метрик потеряна. → Для стенда допустимо, в проде — внешнее хранилище и реплики (урок 81).

### Безопасность

- **`/metrics` без аутентификации.** Prometheus по умолчанию не умеет basic auth на стороне сервера, а экспортеры отдают метрики всем. В лабе мы ограничили 9100 файрволом — в проде обязательно сетевые политики, mTLS или reverse proxy. Голый node_exporter в интернете — утечка информации о версиях ядра, дисках и нагрузке.
- **UI Prometheus в интернет.** Порт 9090 не имеет аутентификации и позволяет выполнять произвольные запросы — это ещё и способ выесть память тяжёлым `{__name__=~".+"}`. Только внутренняя сеть или `--web.external-url` за аутентифицирующим прокси.
- **`--web.enable-lifecycle`** разрешает любому, кто дотянулся до порта, перезагрузить конфиг. В лабе — удобно, в проде — либо не включать, либо закрывать сетью.
- **Секреты в `prometheus.yml`.** Пароли для `basic_auth` целей и токены хранятся в открытом виде. В проде — Vault/Secrets Manager, файлы с правами 0640 и владельцем `prometheus`.
- **Широкие права сервиса.** `prometheus` и `node_exporter` в лабе работают непривилегированными пользователями — так и должно быть. Запуск от root превращает любую уязвимость экспортера в полный захват хоста.

## 7. Вопросы на собеседовании

**1. Почему Prometheus использует pull, а не push? В чём минусы и когда pull не работает?**

**Ответ.** Pull даёт три вещи: Prometheus сам знает список целей и их состояние (метрика `up` — бесплатный health-check), легко отлаживать («зайди curl'ом на /metrics и посмотри, что отдаёт»), нет нужды открывать входящий порт на каждом приложении — достаточно, чтобы Prometheus мог до него дойти. Минусы: цели за NAT/файрволом недоступны, нужен service discovery, а короткоживущие задачи (CI-джобы, cron, batch) живут меньше интервала скрейпа и не попадают в выборку. Для них — Pushgateway: задача пушит метрику, Prometheus скрейпит Pushgateway. Плюс есть agent mode (только скрейп и remote write) для edge-площадок, где Prometheus физически не может достучаться.

**Что хочет услышать интервьюер:** что ты понимаешь модель, знаешь про `up`, Pushgateway и agent mode, не считаешь pull «единственно правильным».

**Красный флаг:** «push лучше, потому что быстрее» или незнание, что делать с batch-джобами.

---

**2. Что делает `rate()` и почему нельзя брать окно `[30s]` при `scrape_interval: 15s`?**

**Ответ.** `rate(counter[окно])` считает средний прирост счётчика в секунду за окно: берёт первое и последнее значение в окне, делит разницу на длительность, и корректно обрабатывает сброс счётчика (рестарт процесса) — при обнаружении уменьшения значения считает, что произошёл reset. Для вычисления нужно минимум два сэмпла в окне. При `scrape_interval: 15s` окно `[30s]` даёт всего два сэмпла на границе — если один скрейп пропущен или попал на границу окна, `rate()` вернёт пустоту или скачок. Практическое правило: окно ≥ 4 интервалов скрейпа. Для 15s — `[1m]` минимум, `[5m]` для графиков. Ещё нюанс: `rate` экстраполирует на границы окна, `irate` берёт два последних сэмпла и показывает мгновенную скорость — для графиков `rate`, для «что происходит прямо сейчас» в алертах — иногда `irate`.

**Что хочет услышать интервьюер:** понимание, что counter и `rate` — парные сущности, и что частота скрейпа влияет на выбор окна.

**Красный флаг:** «`rate` работает с любыми метриками» или незнание про reset.

---

**3. Как работает service discovery и зачем нужен relabeling?**

**Ответ.** SD-механизм автоматически получает список целей из источника: `file_sd` (JSON/YAML-файл, обновляется по `refresh_interval`), `dns_sd` (SRV/A-записи), `kubernetes_sd` (роли pod/service/endpoints/node через API), `consul_sd`, `ec2_sd`. Каждая найденная цель получает служебные лейблы: `__address__`, `__scheme__`, `__metrics_path__`, `__param_*` и `__meta_*` — метаданные источника (namespace, pod name, теги). Relabeling — это фильтр-конвейер над этими лейблами до скрейпа: `keep`/`drop` отбрасывают цели (`keep` по `__meta_kubernetes_pod_annotation_prometheus_io_scrape="true"`), `replace` переносит `__meta_*` в обычные лейблы, `labelmap` массово переименовывает. Служебные лейблы с `__` удаляются после переименования. Отдельно есть `metric_relabel_configs` — то же самое, но для уже полученных метрик: дропать шумные серии до записи в TSDB, экономить диск.

**Что хочет услышать интервьюер:** разницу между `relabel_configs` (до скрейпа, про цели) и `metric_relabel_configs` (после, про метрики) — это классический разделительный вопрос.

**Красный флаг:** путаница «relabeling фильтрует метрики» без уточнения, какой именно конфиг.

---

**4. Зачем нужны recording rules, если можно написать тот же PromQL в Grafana?**

**Ответ.** Три причины. Первая — производительность: тяжёлое выражение с `rate` по сотням series, посчитанное в дашборде, выполняется на каждый рефреш каждой панели у каждого пользователя; recording rule считает его один раз за `interval` и кладёт готовую метрику в TSDB, дашборд читает её мгновенно. Вторая — переиспользование: одна запись `instance:node_cpu_utilisation:rate5m` используется в десяти панелях и в алертах, логика не расползается по копипасте. Третья — алерты: сложное выражение в `alerting rule` проще отладить, если оно уже проверено как recording rule. Минусы: записанные series тоже занимают место и кардинальность, правило надо версионировать в Git и проверять `promtool`, а ошибка в записи тихо ломает все дашборды сразу. Конвенция имён — `level:metric:operations`, например `job:http_requests:rate5m`.

**Что хочет услышать интервьюер:** понимание trade-off «нагрузка на запросы vs место в TSDB», а не просто «чтобы быстрее».

**Красный флаг:** «recording rules нужны, чтобы не писать запросы» — без осознания цены.

---

**5. Target отдаёт HTTP 200, но метрики не те, что ожидались. Как отличить «сервис лежит» от «сервис работает, но метрика пропала»?**

**Ответ.** Это два разных сигнала, и лечатся они по-разному. `up == 0` означает, что скрейп не удался: соединение не установилось, таймаут или HTTP-код не 2xx — то есть проблема с сетью, файрволом или самим процессом экспортера. `up == 1`, но нужной метрики нет — процесс жив, а вот бизнес-функция не работает: например, nginx отдаёт `stub_status`, но upstream-ы все легли, или приложение не инициализировало счётчик. Для второго случая используется `absent()`: `absent(rate(http_requests_total{job="api"}[5m]))` вернёт 1, если series вообще отсутствует — это алерт «метрика исчезла». Также полезно ставить алерт на `up == 1` и одновременно `absent(...)` для критичных метрик. И третье измерение — `scrape_duration_seconds` и `scrape_samples_scraped`: если цель отвечает, но число сэмплов резко упало, значит экспортер частично сломан.

**Что хочет услышать интервьюер:** что ты не сводишь мониторинг к `up`, а различаешь уровни отказа — «не достучались», «достучались, но пусто», «данные есть, но неверные».

**Красный флаг:** «если `up == 1`, значит всё хорошо» — это самая частая причина пропущенных инцидентов.

## 8. Источники

- **prometheus.io** — раздел *Documentation*: «Getting started», «Configuration» (полный справочник по `scrape_configs`, `relabel_configs`, `alerting`), «Querying basics» и «Query functions» (`rate`, `histogram_quantile`, `predict_linear`, `absent`). Поиск: `prometheus configuration scrape_config`, `promql functions rate`.
- **prometheus.io** — раздел *Docs → Visualization → Alerting rules* и *Recording rules*: синтаксис `groups`, `for`, шаблоны `$labels`. Поиск: `prometheus alerting rules for clause`.
- **prometheus.io** — *Docs → Instrumenting → Exporters and integrations* (список официальных и community-экспортеров) и *Best practices* (именование метрик, кардинальность, `rate` и окна). Поиск: `prometheus naming best practices`, `prometheus exporters list`.
- **github.com/prometheus** — репозитории `prometheus`, `node_exporter`, `alertmanager`: раздел *Releases* (актуальные версии и ассеты для linux-amd64) и README с флагами запуска. Поиск: `node_exporter releases linux-amd64`.
- **prometheus.io** — *Docs → Command-line flags* и *HTTP API* (`/api/v1/query`, `/api/v1/rules`, `/api/v1/targets`, `/-/reload`, `/-/ready`). Поиск: `prometheus http api query endpoint`.
- **kubernetes.io** — раздел про `kubernetes_sd_configs` и service discovery [ПРОВЕРИТЬ актуальный путь в документации], пригодится на уроке 84. Поиск: `prometheus kubernetes service discovery roles`.
- **prometheus.io** — *Docs → Storage*: retention, блоки, WAL, `--storage.tsdb.retention.time`. Поиск: `prometheus storage retention tsdb`.
