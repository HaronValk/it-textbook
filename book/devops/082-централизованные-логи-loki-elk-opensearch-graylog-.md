# Централизованные логи: Loki, ELK/OpenSearch, Graylog, Vector/Fluent Bit — сбор, парсинг, ретеншн

## 1. Введение (зачем это нужно в реальной работе)

**Заявка из HelpDesk «Вектора»:**

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Вчера примерно в 15:40 минут пять не открывался корпоративный портал, я не успела отправить документы. В IT сказали „у нас всё работает“. Найдите, что происходило, мне нужно объяснение для руководства».
> **Срочность:** средняя

Ты идёшь на WEB01 смотреть `/var/log/nginx/error.log` — там пусто. Потом на UBU01 — там вообще другой сервис. Потом выясняется, что портал ходит через GW01, а на GW01 логи в другом формате и без времени в нужном поясе. Знакомо? Именно так выглядит работа без централизованных логов.

Логи — второй столп наблюдаемости (первый разбирали в уроке 77). Метрики говорят «стало плохо», логи говорят «почему именно». Проблема в том, что в инфраструктуре из 10–50 машин логи лежат в 50 разных местах, в трёх форматах, с разным временем и без ротации. Централизованный сбор решает три задачи: **свезти всё в одну точку**, **привести к общему формату (распарсить)** и **вовремя удалять (ретеншн)**, чтобы диск не кончился.

После этой главы ты должен уметь: поднять Loki и отправлять в него логи с Linux-хостов через Vector и Fluent Bit, писать запросы на LogQL и находить в них инцидент, настраивать ретеншн в Loki и OpenSearch, а также объяснить на собеседовании, чем Loki дешевле ELK и когда без ELK/OpenSearch не обойтись.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Любая система централизованного логирования состоит из трёх слоёв:

```text
СБОР (агенты)                ХРАНЕНИЕ + ИНДЕКС              ЧТЕНИЕ
┌───────────────┐           ┌────────────────────┐         ┌──────────────┐
│ UBU01         │           │  LOKI :3100        │         │ Grafana :3000│
│ journald      │──┐        │  ├ distributor     │         │ Explore,     │
│ rsyslog       │  │        │  ├ ingester        │◄──LogQL─┤ дашборды     │
├───────────────┤  │  push  │  ├ querier         │         └──────────────┘
│ WEB01 (DMZ)   │──┼───────►│  └ compactor       │
│ nginx+FLB     │  │        │  chunks → FS/S3    │         ┌──────────────┐
├───────────────┤  │        │  index  → только   │◄──DSL───┤ Kibana /     │
│ GIT01         │──┘        │           метки    │         │ Dashboards   │
│ docker logs   │           └────────────────────┘         └──────────────┘
└───────────────┘            OpenSearch :9200 (полный
                             обратный индекс по словам)
```

**Ключевое различие архитектур.** ELK (Elasticsearch + Logstash + Kibana) и его форк **OpenSearch** строят *полнотекстовый обратный индекс*: каждое слово каждой строки разбирается и кладётся в индекс. Это позволяет искать что угодно, но стоит дорого — индекс часто больше самих логов, а запись требует CPU и RAM. **Loki** (Grafana Labs) индексирует **только метки (labels)** — небольшой набор пар «ключ=значение» (job, host, level), а сам текст лога пачками (chunks) сжимает и кладёт в объектное хранилище (S3/MinIO) или на диск. Поиск по тексту идёт перебором внутри чанков по выбранному диапазону времени. Отсюда правило Loki: **чем меньше кардинальность меток, тем лучше**.

**Graylog** — отдельный зверь: Java-приложение, которое хранит данные в OpenSearch/Elasticsearch, а конфигурацию и метаданные — в MongoDB. Сверху даёт удобную веб-морду, потоки (streams), оповещения, разбор GELF. Порты: 9000 — веб и REST API, 12201 — GELF (UDP/TCP), 1514 — syslog (значение по умолчанию в свежих версиях; исторически 5140/514) `[ПРОВЕРИТЬ для вашей версии]`. Лицензия Graylog Open — SSPL `[ПРОВЕРИТЬ]`.

**Агенты сбора:**
- **Fluent Bit** — написан на C, ~1 МБ, минимальный CPU, плагины входов/фильтров/выходов, конфиг в ini-формате. Стандарт для Kubernetes (часто как DaemonSet).
- **Vector** (Datadog) — на Rust, конфиг TOML/YAML, язык трансформаций **VRL** (Vector Remap Language), очень гибкий парсинг, есть API и `vector top`. Лицензия MPL 2.0.
- **Promtail** — родной агент Loki; Grafana Labs объявила о его выводе из эксплуатации в пользу **Grafana Alloy** `[ПРОВЕРИТЬ точные даты]`.
- **Logstash** — тяжёлый JVM-комбайн, нужен, когда требуется сложная обработка и куча плагинов.

**Протоколы и порты, которые надо помнить:** syslog — UDP/TCP **514**, TLS — **6514** (RFC 5424 — современный формат, RFC 3164 — старый BSD); Loki HTTP — **3100**; OpenSearch/Elasticsearch REST — **9200**, транспорт — **9300**; OpenSearch Dashboards / Kibana — **5601**; Fluent Bit forward — **24224**, его HTTP-метрики — **2020**; Vector API — **8686**; Graylog GELF — **12201**.

**Ретеншн** — политика удаления. В Loki это `retention_period` в `limits_config` плюс включённый **compactor**, который физически удаляет чанки. В OpenSearch/Elasticsearch — **ISM** (Index State Management) или **ILM**: индексы делятся по времени (`logs-2026.02.14`), старые переходят в состояния hot → warm → cold → delete. В Graylog — «index sets» с ротацией по времени или размеру. В любом случае снизу часто добавляют lifecycle-политику в S3, чтобы удаление работало даже при сбое приложения.

## 3. Аналогия из жизни

Представь библиотеку. **ELK** — это библиотека, где библиотекарь заранее прочитал каждую книгу и составил алфавитный указатель по каждому слову: найти страницу со словом «ошибка» — секунды, но на указатели уходит половина здания и зарплата трёх библиотекарей. **Loki** — библиотека, где на корешке написано только «полка: WEB01, год: 2026, жанр: nginx»: чтобы найти слово, придётся перелистать все книги с этой полки за нужную неделю, зато здание маленькое и дешёвое. **Graylog** — библиотека с готовой регистратурой, формулярами и автоматом оповещения читателей, но под капотом у неё всё равно стеллажи OpenSearch. **Агенты (Vector, Fluent Bit)** — курьеры, которые забирают дневники у каждого отдела и привозят в библиотеку, попутно переводя их на общий язык.

Аналогия перестаёт работать в одном месте: книги в библиотеке не меняются, а поток логов непрерывен — ярлыки на корешках приходится поддерживать постоянно, и одна неаккуратно добавленная метка (например, `request_id`) мгновенно превращает уютную библиотеку в бесконечный склад.

## 4. Практика и команды

### 4.1. Смотрим, что уже есть на хосте (UBU01)

```bash
# размер журнала journald и переход на постоянное хранение
sudo mkdir -p /var/log/journal
sudo systemctl restart systemd-journald
journalctl --disk-usage

# чтение журнала: юнит, период, формат
journalctl -u ssh --since "1 hour ago" --no-pager -o short-iso
journalctl -u nginx -o json --no-pager | head -n 2
```
`-u` — фильтр по systemd-юниту, `--since/--until` — диапазон, `-o` — формат вывода (`json` удобен для парсинга агентом), `--no-pager` — не открывать `less`.

В `/etc/systemd/journald.conf` ограничиваем размер, иначе журнал съест диск:
```ini
[Journal]
Storage=persistent
SystemMaxUse=2G
SystemKeepFree=1G
RateLimitIntervalSec=30s
RateLimitBurst=10000
```

### 4.2. Классика: rsyslog форвардит всё наверх

```bash
# /etc/rsyslog.d/50-vektor-remote.conf
# *.* action(type="omfwd" target="10.10.10.40" port="514" protocol="tcp"
#            action.resumeRetryCount="-1" queue.type="linkedList")

sudo systemctl restart rsyslog
sudo ss -lntup | grep 514        # на приёмнике: кто слушает порт
logger -t vektor-test "hello from ubu01"   # тестовая запись
```
`*.*` — все объекты всех уровней; `@@` (старый синтаксис) = TCP, `@` = UDP; `queue.type="linkedList"` — буфер в памяти на случай недоступности сервера. UDP-приёмник в проде плох: пакеты теряются при перегрузке.

### 4.3. Ротация на хосте (без неё агент не нужен — диск кончится раньше)

```bash
# /etc/logrotate.d/vektor
# /var/log/vektor/*.log {
#     daily
#     rotate 14
#     compress
#     delaycompress
#     missingok
#     notifempty
#     create 0640 root adm
# }

sudo logrotate -d /etc/logrotate.d/vektor    # dry-run, ничего не делает
sudo logrotate -f /etc/logrotate.d/vektor    # принудительно прокрутить
```
`-d` — режим отладки (показывает, что сделал бы), `-f` — force. В проде `-d` — первый шаг любой диагностики ротации.

### 4.4. Loki: проверка и запросы

```bash
curl -s http://10.10.10.40:3100/ready          # ожидаем "ready"
curl -s http://10.10.10.40:3100/metrics | head -n 20

logcli --addr=http://10.10.10.40:3100 labels
logcli --addr=http://10.10.10.40:3100 query '{job="nginx"}' --since=1h --limit=20
logcli --addr=http://10.10.10.40:3100 series '{job=~".+"}'
```
`/ready` — readiness-проба (в k8s её и вешают). `logcli` — CLI к Loki API; `--since`, `--limit`, `--addr` (или переменная `LOKI_ADDR`).

Запросы **LogQL** (в Grafana → Explore → источник Loki):
```logql
{host="web01"} |= "502"
{job="nginx"} | json | status >= 500
sum by (status) (count_over_time({job="nginx"} | json [5m]))
rate({host="ubu01"} |= "Failed password" [1m])
topk(3, sum by (host) (count_over_time({job=~".+"}[5m])))
```
`{...}` — селектор по меткам (обязателен), `|=` — подстрока, `| json` / `| logfmt` — парсинг, `| status >= 500` — фильтр по распарсенному полю, `rate()`/`count_over_time()` — превращение логов в метрику для графика или алерта.

### 4.5. Vector на UBU01: сбор и парсинг

```toml
# /etc/vector/vector.toml
data_dir = "/var/lib/vector"

[api]
enabled = true
address = "127.0.0.1:8686"

[sources.ubu01_logs]
type = "file"
include = ["/var/log/syslog", "/var/log/auth.log"]
read_from = "beginning"

[transforms.prepare]
type = "remap"
inputs = ["ubu01_logs"]
source = '''
.parsed = parse_syslog(.message) ?? {}
.host = "ubu01"
.appname = to_string(.parsed.appname) ?? "unknown"
.severity = to_string(.parsed.severity) ?? "info"
'''

[sinks.loki]
type = "loki"
inputs = ["prepare"]
endpoint = "http://10.10.10.40:3100"
encoding.codec = "json"
labels.job = "syslog"
labels.host = "{{ host }}"
labels.level = "{{ severity }}"
```

```bash
sudo vector validate --no-environment /etc/vector/vector.toml
sudo systemctl restart vector
sudo vector top                  # живой TUI: события, ошибки, throughput
```
`parse_syslog` — VRL-функция разбора RFC 3164/5424, `?? {}` — «если не получилось, верни пустой объект» (без `??` событие упадёт с ошибкой). `labels.*` в Loki-sink поддерживают шаблоны `{{ поле }}`. Никогда не делай меткой `appname` из用户-данных или `request_id` — кардинальность взорвётся.

### 4.6. Fluent Bit на WEB01 (DMZ): nginx access.log

```ini
# /etc/fluent-bit/parsers.conf
[PARSER]
    Name        nginx_combined
    Format      regex
    Regex       ^(?<remote_addr>[^ ]*) - (?<remote_user>[^ ]*) \[(?<time_local>[^\]]*)\] "(?<method>\S+) (?<path>[^ ]*) (?<protocol>[^"]*)" (?<status>\d+) (?<body_bytes_sent>\d+) "(?<http_referer>[^"]*)" "(?<http_user_agent>[^"]*)"$
    Time_Key    time_local
    Time_Format %d/%b/%Y:%H:%M:%S %z
```

```ini
# /etc/fluent-bit/fluent-bit.conf
[SERVICE]
    Flush        5
    Log_Level    info
    Parsers_File /etc/fluent-bit/parsers.conf
    HTTP_Server  On
    HTTP_Listen  0.0.0.0
    HTTP_Port    2020

[INPUT]
    Name            tail
    Path            /var/log/nginx/access.log
    Parser          nginx_combined
    Tag             web01.nginx
    Read_from_Head  On
    DB              /var/lib/fluent-bit/tail.db

[FILTER]
    Name    modify
    Match   web01.*
    Add     host web01

[OUTPUT]
    Name          loki
    Match         web01.*
    Host          10.10.10.40
    Port          3100
    Labels        job=nginx,host=web01
    Line_Format   json
```

```bash
sudo fluent-bit -c /etc/fluent-bit/fluent-bit.conf --dry-run
curl -s http://127.0.0.1:2020/api/v1/metrics | head -n 30
```
`tail` + `DB` хранит позицию в файле (без него после рестарта логи перечитаются дважды). `--dry-run` проверяет конфиг без запуска. HTTP-порт 2020 даёт метрики `fluentbit_output_errors_total`, `fluentbit_output_retries_total` — первое, что смотрим, если «логи не доходят».

### 4.7. OpenSearch и Graylog: команды «соседнего мира»

```bash
# какие индексы есть и сколько занимают
curl -s -u admin:'Lab#2026-vektor' 'http://10.10.10.40:9200/_cat/indices?v&s=store.size:desc'

# политика ретеншна (ISM): 30 дней и удаление
# PUT _plugins/_ism/policies/logs-30d
# { "policy": { "description": "30d then delete", "default_state": "hot",
#   "states": [
#     { "name": "hot", "actions": [],
#       "transitions": [ { "state_name": "delete", "conditions": { "min_index_age": "30d" } } ] },
#     { "name": "delete", "actions": [ { "delete": {} } ], "transitions": [] } ],
#   "ism_template": [ { "index_patterns": ["logs-*"], "priority": 100 } ] } }
```

```bash
# отправка GELF-сообщения в Graylog (проверка приёма)
echo -n '{"version":"1.1","host":"ubu01","short_message":"test gelf from ubu01","level":6,"_env":"prod"}' \
  | nc -u -w1 10.10.10.40 12201
```
Пользовательские поля в GELF обязаны начинаться с `_`, иначе Graylog их отбросит.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **MON01** (10.10.10.40): Ubuntu Server 24.04, Grafana из урока 79 уже стоит и отвечает на `:3000`. Свободно минимум 20 ГБ на `/var`.
- **UBU01** (10.10.10.30): Ubuntu 24.04, SSH работает, есть `/var/log/syslog` и `/var/log/auth.log`.
- **WEB01** (10.10.20.10): Ubuntu + nginx, `/var/log/nginx/access.log` пишется.
- **GW01** (10.10.10.3 / 10.10.20.1): если DMZ отделена, разрешить `10.10.20.10 → 10.10.10.40:3100/tcp`. Если WEB01 и MON01 в одной сети — шаг пропустить.
- Время на всех ВМ синхронизировано (`timedatectl` → `System clock synchronized: yes`). Разные часы — причина №1 «а почему в Grafana пусто».
- Пароль стенда `Lab#2026-vektor`; `auth_enabled: false` в Loki — **только для лабы**.

### Снапшот
В VMware Workstation на **MON01**, **UBU01**, **WEB01**: `VM → Snapshot → Take Snapshot`, имя — `before-devops-082`. Закомментировать в `stand-log.md` одной строкой.

### Шаги

1. **MON01. Ставим Loki из репозитория Grafana.**
   ```bash
   sudo mkdir -p /etc/apt/keyrings
   wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
   echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
   sudo apt-get update && sudo apt-get install -y loki logcli
   ```
   Ожидаем: пакет `loki` установлен, юнит `loki.service` есть `[ПРОВЕРИТЬ имена пакета и файла конфига в вашей версии]`. Альтернатива без репозитория — бинарник с GitHub Releases (версию взять актуальную, не выдумывать).

2. **MON01. Кладём конфиг `/etc/loki/config.yml`.**
   ```yaml
   auth_enabled: false

   server:
     http_listen_port: 3100
     grpc_listen_port: 9096

   common:
     instance_addr: 127.0.0.1
     path_prefix: /var/lib/loki
     storage:
       filesystem:
         chunks_directory: /var/lib/loki/chunks
         rules_directory: /var/lib/loki/rules
     replication_factor: 1
     ring:
       kvstore:
         store: inmemory

   schema_config:
     configs:
       - from: 2020-10-24
         store: tsdb
         object_store: filesystem
         schema: v13
         index:
           prefix: index_
           period: 24h

   limits_config:
     retention_period: 168h
     reject_old_samples: true
     reject_old_samples_max_age: 168h

   compactor:
     working_directory: /var/lib/loki/compactor
     compaction_interval: 10m
     retention_enabled: true
     retention_delete_delay: 2h
     delete_request_store: filesystem

   analytics:
     reporting_enabled: false
   ```
   `retention_period: 168h` — 7 суток. `delete_request_store` обязателен, когда `retention_enabled: true`.

3. **MON01. Запуск и проверка готовности.**
   ```bash
   sudo systemctl enable --now loki
   curl -s http://127.0.0.1:3100/ready      # ожидаем "ready"
   sudo ss -lntp | grep 3100
   ```
   Если `ready` не приходит дольше минуты — `journalctl -u loki -n 50 --no-pager`.

4. **UBU01. Ставим Vector и пишем конфиг** из раздела 4.5 в `/etc/vector/vector.toml`, затем:
   ```bash
   sudo vector validate --no-environment /etc/vector/vector.toml
   sudo systemctl enable --now vector
   sudo vector top
   ```
   Ожидаем: в `vector top` счётчик у sink `loki` растёт, ошибок нет.

5. **UBU01. Генерируем события для проверки.**
   ```bash
   logger -t vektor-test "lab 82 hello from ubu01"
   sudo -u nobody ssh baduser@127.0.0.1 2>/dev/null || true   # или просто неудачный ssh
   ```
   Ожидаем: на MON01 в Loki появятся записи с меткой `host="ubu01"`.

6. **WEB01. Ставим Fluent Bit и конфиг** из раздела 4.6, затем генерируем трафик:
   ```bash
   sudo mkdir -p /var/lib/fluent-bit
   sudo fluent-bit -c /etc/fluent-bit/fluent-bit.conf --dry-run
   sudo systemctl enable --now fluent-bit
   for i in $(seq 1 5); do curl -s -o /dev/null http://127.0.0.1/; done
   curl -s -o /dev/null http://127.0.0.1/no-such-page     # даст 404
   ```
   Ожидаем: 6 новых строк в access.log, они уехали в Loki с `job="nginx"`.

7. **MON01. Подключаем Loki к Grafana как код.**
   ```yaml
   # /etc/grafana/provisioning/datasources/loki.yaml
   apiVersion: 1
   datasources:
     - name: Loki
       type: loki
       access: proxy
       url: http://10.10.10.40:3100
       jsonData:
         maxLines: 1000
   ```
   ```bash
   sudo systemctl restart grafana-server
   ```
   Ожидаем: в Grafana → Connections → Data sources появился Loki, кнопка «Save & test» зелёная.

8. **Grafana → Explore → Loki.** Выполняем по очереди запросы из 4.4. Ожидаем: `{job="nginx"}` отдаёт строки, `sum by (status) (count_over_time({job="nginx"} | json [5m]))` рисует график с 200 и 404.

9. **Проверяем ретеншн.** На MON01:
   ```bash
   sudo journalctl -u loki --since "15 min ago" --no-pager | grep -i -E 'retention|compact' | head
   ```
   Ожидаем: в логах compactor видны запуски; чанки старше 168 ч будут помечены к удалению в течение `retention_delete_delay` (2 ч).

10. **Собираем «картину инцидента»** — один дашборд в Grafana с тремя панелями: график `rate({job="nginx"}[1m])`, таблица `{host="web01"} |= " 50"`, таблица `{host="ubu01"} |= "Failed password"`. Сохраняем JSON дашборда в Git-репозиторий стенда (`git commit`).

### Проверка
- [ ] `curl http://10.10.10.40:3100/ready` возвращает `ready`
- [ ] В Grafana есть datasource Loki и он проходит Save & test
- [ ] `{host="ubu01"}` отдаёт событие `vektor-test`
- [ ] `{job="nginx"}` отдаёт строки с WEB01, поле `status` парсится (`| json | status=404` работает)
- [ ] `vector top` на UBU01 не показывает ошибок sink
- [ ] `curl -s http://127.0.0.1:2020/api/v1/metrics` на WEB01 отдаёт метрики без роста `output_errors_total`
- [ ] В `/etc/loki/config.yml` `retention_period` задан и `retention_enabled: true`
- [ ] `stand-log.md` обновлён

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01) — через службу поддержки
> **ВМ:** WEB01 / MON01
> **Что написал:** «Портал снова отдаёт 502 с 10:15. В Grafana по nginx за это время пусто, хотя раньше всё было. Логи сломались?»
> **Срочность:** высокая

**Воспроизводим поломку** (на WEB01):
```bash
sudo sed -i 's/Port          3100/Port          3101/' /etc/fluent-bit/fluent-bit.conf
sudo systemctl restart fluent-bit
```
Теперь агент стучится в несуществующий порт — новые логи не приходят, старые в Grafana остаются (и это сбивает с толку).

**Диагностика по порядку:**
1. Grafana Explore: `{job="nginx"}` за последний час → строк нет. Значит проблема не в Grafana, а в доставке.
2. На MON01: `curl -s http://127.0.0.1:3100/ready` → `ready`. Loki жив, ищем на стороне агента.
3. На WEB01: `systemctl status fluent-bit` → `active (running)`, что уже подозрительно (падает не он).
4. `journalctl -u fluent-bit -n 30 --no-pager` → ошибки вида `could not connect to 10.10.10.40:3101` / `connection refused`. Гипотеза: неверный endpoint.
5. `nc -vz 10.10.10.40 3100` с WEB01 → `succeeded`; `nc -vz 10.10.10.40 3101` → `refused`. Сетевых проблем нет, ошибка в конфиге.
6. `grep -n 3101 /etc/fluent-bit/fluent-bit.conf` → находим строку.

**Решение:**
```bash
sudo sed -i 's/Port          3101/Port          3100/' /etc/fluent-bit/fluent-bit.conf
sudo fluent-bit -c /etc/fluent-bit/fluent-bit.conf --dry-run
sudo systemctl restart fluent-bit
curl -s -o /dev/null http://127.0.0.1/   # свежая строка
```
Проверяем в Grafana, что строки пошли. Заодно смотрим, есть ли `output_retries_total` — Fluent Bit буферизует, часть логов за окно сбоя может доехать после исправления.

**Ответ пользователю:** «Проблема была в агенте сбора логов на веб-сервере: после правки конфигурации он отправлял данные на закрытый порт. Сбор восстановлен в 10:47, часть событий за период 10:15–10:47 восстановлена из буфера агента. Сам 502 разбираем отдельно — по восстановленным логам видно код ответа upstream».

**Закрытие заявки:** категория «Мониторинг/логирование», в решении — ссылка на коммит с исправлением конфига в Git и запись в `stand-log.md`. Отдельно завести задачу «добавить алерт на `fluentbit_output_errors_total > 0`».

### Что изменилось на стенде
- **MON01**: установлен и запущен Loki (`:3100`), конфиг `/etc/loki/config.yml` (filesystem storage, ретеншн 168 ч), установлен `logcli`; в Grafana провижинится datasource `Loki`.
- **UBU01**: установлен Vector (`/usr/local/bin/vector`), конфиг `/etc/vector/vector.toml`, юнит `vector.service`, читает `/var/log/syslog` и `/var/log/auth.log`, шлёт в Loki с метками `job=syslog`, `host=ubu01`, `level`.
- **WEB01**: установлен Fluent Bit, конфиги `/etc/fluent-bit/fluent-bit.conf` и `parsers.conf`, HTTP-метрики на `:2020`, отправляет nginx access в Loki с метками `job=nginx`, `host=web01`.
- **Сеть**: при необходимости — разрешающее правило на GW01 `10.10.20.10 → 10.10.10.40:3100/tcp`.
- **Grafana**: дашборд «Vektor / Logs overview» (JSON в Git).

### Откат
1. VMware Workstation → нужная ВМ → `Snapshot Manager` → `before-devops-082` → **Revert** (на MON01, UBU01, WEB01 по очереди; ВМ выключить перед откатом).
2. Если откат не нужен, а сломалась только одна машина: `sudo systemctl disable --now fluent-bit` / `vector` / `loki` и удалить соответствующие конфиги.
3. Записать в `stand-log.md` факт отката и причину.

## 6. Ошибки новичков и безопасность

1. **Высококардинальные метки в Loki** (`request_id`, `user`, `path`, `status`). → Каждая уникальная комбинация = новый поток; Loki падает по памяти, Grafana еле открывает список лейблов. → В метки только `job`, `host`, `env`, `level`; всё остальное — в тело лога и достаётся через `| json`.
2. **Логировать всё подряд, включая debug, навсегда.** → 500 ГБ в S3 и счёт за облако. → Уровень логирования через переменные окружения, ретеншн 7–30 дней, отдельные политики для аудита.
3. **Нет ротации на хостах.** → `/var/log` переполняется, падает сначала rsyslog, потом сервис. → `logrotate` + `SystemMaxUse` в journald + алерт на свободное место.
4. **Разные часы на агентах и сервере.** → События «из будущего» отбрасываются (`reject_old_samples`), инцидент не собирается в таймлайн. → chrony/systemd-timesyncd на всех ВМ, проверять `timedatectl`.
5. **Многострочные логи (stack trace Java/Python) разбиваются на строки.** → В Loki невозможно прочитать исключение целиком. → Настраивать multiline-парсер в Fluent Bit (`multiline.parser`) или склеивать в Vector, либо писать JSON-лог с `traceback` одним полем.
6. **`auth_enabled: false` и открытый порт 3100 на весь мир.** → Кто угодно читает и пишет логи. → В лабе допустимо, в проде — reverse-proxy с аутентификацией (Grafana/Loki с tenant, TLS), сетевые политики, доступ только из подсети мониторинга.
7. **Логи уходят по syslog/UDP через DMZ в открытом виде.** → Перехват и подмена. → TCP + TLS (порт 6514), mTLS между агентом и приёмником.

### Безопасность
Никогда не отправляй в централизованное хранилище пароли, токены, номера карт, полные паспортные данные и содержимое куков. Такие данные попадают в логи случайно — через `Authorization`-заголовок, тело запроса, строку подключения к БД. В нормальной среде на агенте ставят маскирование (в Vector — VRL `replace` по регулярке, в Fluent Bit — фильтр `modify`/`lua`), а доступ к самому хранилищу ограничивают ролями: разработчик видит логи своего сервиса, а не всей компании. Для персональных данных в РФ действует 152-ФЗ: логи с ПДн — это тоже обработка ПДн, со сроками хранения и требованиями к защите. `[ПРОВЕРИТЬ актуальные требования с юристом/ИБ]`

## 7. Вопросы на собеседовании

**1. Чем Loki отличается от ELK/OpenSearch и когда вы выберете каждый?**
Ответ: Loki индексирует только метки, тело лога хранит сжатыми чанками в объектном хранилище; поиск по тексту — перебор по выбранному диапазону. ELK/OpenSearch строят полнотекстовый обратный индекс, поэтому дают быстрый произвольный поиск и агрегации, но дороже в 3–5 раз по CPU/RAM/диску. Loki берут, когда нужен дешёвый сбор большого объёма логов и уже есть Grafana; OpenSearch/ELK — когда нужен полнотекстовый поиск, сложные агрегации, аудит с длинным хранением,SIEM-сценарии или Graylog поверх него.
*Что хочет услышать:* понимание компромисса «индекс vs стоимость» и привязку к сценарию, а не «Loki лучше».
*Красный флаг:* «Loki — это просто облегчённый Elasticsearch» или «в Loki можно так же быстро искать по любому слову за год».

**2. Как бороться с высококардинальностью меток в Loki?**
Ответ: правило — набор меток фиксирован и мал (`job`, `host`, `env`, `level`, `namespace`). Всё динамическое (ID запроса, URL, статус, user_id) — поля в JSON-теле; парсим на этапе запроса через `| json` / `| logfmt`, фильтруем `| field="value"`. Уже накопленные «плохие» потоки вычищают сменой конфига агента и ожиданием ретеншна либо принудительным удалением через API `delete` в Loki. Дополнительно помогает `structured metadata` в Loki 3.x — метаданные, не входящие в индекс потока `[ПРОВЕРИТЬ синтаксис в вашей версии]`.
*Что хочет услышать:* конкретные примеры плохих/хороших меток и понимание, что метка = измерение потока.
*Красный флаг:* «просто добавлю все поля в метки, так искать удобнее».

**3. Как устроен ретеншн в Loki и в OpenSearch?**
Ответ: Loki — `limits_config.retention_period` (глобально или per-tenant), плюс обязательно `compactor.retention_enabled: true` и `delete_request_store`; compactor периодически помечает чанки старше срока и удаляет их после `retention_delete_delay`. Важно: retention работает только для закрытых (не активных) чанков. OpenSearch/Elasticsearch — индексы по времени + ISM (в OpenSearch) / ILM (в Elastic) с переходами hot→delete по `min_index_age`, либо удаление через curator/cron. В обоих случаях снизу полезна lifecycle-политика на бакете S3.
*Что хочет услышать:* что retention — это конфиг приложения **и** политика хранилища, и что без compactor в Loki настройка бесполезна.
*Красный флаг:* «настроил retention_period, значит всё удалится» (без compactor — нет).

**4. Fluent Bit, Vector, Promtail, Alloy, Logstash — как выбирать агент?**
Ответ: критерии — потребление ресурсов, язык обработки, экосистема, поддержка источников. Fluent Bit — минимальный footprint, стандарт для k8s DaemonSet, конфиг ini. Vector — богатый парсинг на VRL, TOML/YAML, хорош как агрегатор и роутер между несколькими приёмниками. Promtail — уходит, для новых инсталляций Grafana рекомендует Alloy (собирает и метрики, и логи, умеет Prometheus-совместимый scrape). Logstash — когда нужна огромная библиотека плагинов и JVM-окружение уже есть, но он тяжёлый. На практике: Fluent Bit/Alloy на узлах, Vector как агрегатор/маршрутизатор в центре.
*Что хочет услышать:* осознание разделения «агент на хосте» и «агрегатор», знание про deprecated Promtail.
*Красный флаг:* «ставлю Logstash на каждую ВМ» без обсуждения ресурсов.

**5. Логи сервиса не доходят до хранилища. Ваши действия по шагам?**
Ответ: сначала сузить слой — (1) проверить приёмник: `/ready`, порт, место на диске, ошибки в логах Loki; (2) проверить агент: `systemctl status`, его собственные логи, метрики (`fluentbit_output_errors_total`, `vector_errors_total`), retry-очередь; (3) проверить сеть: `nc -vz host port`, файрвол между зонами, MTU/VPN; (4) проверить источник: пишется ли файл вообще, правильный ли путь в конфиге, не сменился ли формат лога (парсер перестал матчиться); (5) проверить время и `reject_old_samples`; (6) только потом — Grafana/датасорс (не тот URL, не та метка в селекторе). Фиксируем причину в тикете и добавляем алерт, чтобы в следующий раз узнать раньше пользователя.
*Что хочет услышать:* методичный разбор по слоям и упоминание мониторинга самого пайплайна.
*Красный флаг:* «перезапущу всё подряд» без диагностики или сразу «переустановлю Loki».

## 8. Источники (официальная документация)

- **Grafana Loki** — `grafana.com/docs/loki`. Разделы: «Configuration reference» (`limits_config`, `compactor`), «LogQL», «Storage». Поиск: `loki retention_period compactor retention_enabled`, `logql query examples`.
- **Grafana Alloy** — `grafana.com/docs/alloy`. Раздел: «Introduction / Migrate from Promtail». Поиск: `promtail deprecated alloy migration`.
- **Vector (Datadog)** — `vector.dev/docs`. Разделы: «Configuration», «Sources / file», «Transforms / remap (VRL)», «Sinks / loki». Поиск: `vector vrl parse_syslog`, `vector loki sink labels`.
- **Fluent Bit** — `docs.fluentbit.io`. Разделы: «Configuration», «Inputs / tail», «Parsers», «Outputs / Loki», «Monitoring (HTTP API)». Поиск: `fluent bit loki output line_format`.
- **OpenSearch** — `opensearch.org/docs`. Разделы: «Index State Management», «Index management», «REST API reference». Поиск: `opensearch ism policy delete index after days`.
- **Elastic** — `elastic.co/guide`. Разделы: «Index lifecycle management», «Logstash input beats». Поиск: `elasticsearch ilm policy hot delete phase`. `[ПРОВЕРИТЬ доступность ресурса из РФ; легальная альтернатива — OpenSearch, Apache 2.0]`
- **Graylog** — `graylog.org/docs` (или `docs.graylog.org`). Разделы: «GELF», «Index model / index sets», «Streams». Поиск: `graylog gelf input index set rotation`.
- **systemd journald / journalctl** — `freedesktop.org` (man-страницы `journald.conf`, `journalctl`). Поиск: `journald.conf SystemMaxUse Storage persistent`.
- **rsyslog** — `rsyslog.com/doc`. Раздел: «omfwd module». Поиск: `rsyslog omfwd tcp queue linkedList`.
- **logrotate** — man-страница `logrotate` на `man7.org`. Поиск: `logrotate.conf rotate compress postrotate`.
- **RFC 5424 (syslog)** — `datatracker.ietf.org`. Поиск: `RFC 5424 syslog protocol`.
