# Централизованные логи: Graylog, ELK/OpenSearch, Loki, хранение и ротация

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»:

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Сегодня утром при входе в систему писало “Не удаётся войти в систему”, пустило только с четвёртой попытки. Вчера вечером то же самое было. Разберитесь, меня что, взламывают?»
> **Срочность:** средняя

Вы идёте на WIN11-CL01, открываете «Просмотр событий» — и видите там три записи 4625 за утро. А что было вечером? А на DC01? А не ломился ли кто-то в этот аккаунт с UBU01 по сети? А что в это же время писал FS01, к которому ivanov ходит за папкой `Buh`? Руками по шести машинам это полтора часа беготни, и всё равно половину не увидите: журналы уже провернулись.

Централизованные логи решают ровно эту задачу: все события со всех машин едут в одно место, где их можно искать одним запросом по всем хостам и за любой период. В работе это встречается постоянно: разбор инцидентов с доступом, поиск «кто удалил файл», требования аудита и ФСТЭК хранить события безопасности, отладка «почему 1С падает по вечерам», проверка после обновления, разбор инцидента ИБ. После главы вы будете понимать, как устроен конвейер логов от источника до хранилища, чем отличаются Graylog, ELK/OpenSearch и Loki, сколько места это съедает, как настраивается ротация и retention и как за 10 минут по логам ответить на вопрос «что произошло».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Любая система централизованных логов — это конвейер из четырёх ступеней. Ломается он чаще всего не в середине, а на стыках.

```text
ИСТОЧНИКИ              ТРАНСПОРТ                ПРИЁМ/ОБРАБОТКА            ХРАНЕНИЕ/ПОИСК
┌────────────────┐
│ UBU01 journald │──┐
│ ASTRA01 rsyslog│──┤ syslog 1514/tcp           ┌──────────────┐
│ GW01   rsyslog │──┼──────────────────────────►│   Graylog    │──► OpenSearch/ES
│ WEB01  rsyslog │──┘  (в проде TLS 6514)       │  (парсинг,   │    индексы, шарды,
│ DC01  winlogbeat│──┐                          │   streams,   │    ILM/ISM-политика
│ FS01  winlogbeat│──┼── Beats/Lumberjack 5044 ►│   pipeline)  │    hot→warm→cold→delete
└────────────────┘  │                          └──────┬───────┘
                    │                                 └──► MongoDB (метаданные:
                    └── GELF 12201 / OTLP 4317,4318        inputs, users, streams)
```

**Источники.** На Linux это `systemd-journald` (бинарный журнал в `/run/log/journal` или `/var/log/journal`) и `rsyslog` (текстовые файлы в `/var/log`). На Windows — журналы событий (`Security`, `System`, `Application`), которые читает агент: Winlogbeat, Fluent Bit, NXLog, или встроенный механизм Windows Event Forwarding (WEF), где роль сборщика играет сам Windows Server. Плюс логи приложений — 1С, nginx, самописные скрипты.

**Транспорт.** Классика — syslog по RFC 5424: одна строка, где первые поля жёстко заданы и парсятся автоматически.

```text
<134>1 2026-02-12T09:14:03.123Z UBU01 sshd 4821 ID47 - Failed password for invalid user admin from 10.10.10.99
 │     │                        │     │    │    │      │  │
 │     │                        │     │    │    │      │  └─ текст сообщения
 │     │                        │     │    │    │      └─ structured data (нет)
 │     │                        │     │    │    └─ msgid
 │     │                        │     │    └─ procid
 │     │                        │     └─ app-name
 │     │                        └─ hostname
 │     └─ timestamp
 └─ priority = facility*8 + severity (134 = local0.info)
```

По умолчанию syslog летит по **UDP 514** — «выстрелил и забыл»: сообщение потерялось, никто не заметил. По **TCP 514/601** — с подтверждением, но без шифрования. По **TLS 6514** — шифрование и аутентификация сервера. Агенты Beats ходят на **5044**, GELF (формат Graylog) — на **12201**, OTLP из OpenTelemetry — на **4317** (gRPC) и **4318** (HTTP). В промышленных схемах между агентом и хранилищем ставят буфер — Kafka или Redis, чтобы при падении бэкенда логи не терялись.

**Приём и обработка.** Здесь три школы:

| | Graylog | ELK / OpenSearch | Loki |
|---|---|---|---|
| Что индексирует | поля сообщения (индекс делает OpenSearch/Elasticsearch) | все поля (полнотекстовый inverted index) | **только labels** (метки), текст — в сжатых чанках |
| Компоненты | Graylog + MongoDB + OpenSearch/ES | Elasticsearch/OpenSearch + Logstash/Fluent Bit + Kibana/Dashboards | Loki + агент (Promtail/Alloy/Fluent Bit) |
| Ресурсы | средние-высокие | высокие | низкие |
| Запросы | свой язык в UI, API | KQL/Lucene/DQL | LogQL (похож на PromQL) |
| Когда брать | syslog-хаб, готовый UI и алерты из коробки | полнотекстовый поиск, SIEM-задачи, сложная аналитика | Kubernetes и много источников с дешёвым хранением в S3 |

Ключевая мысль: **Graylog и ELK дорогие потому, что индексируют содержимое**. Loki сознательно индексирует только метки (`host`, `job`, `namespace`), а сам текст ищет по чанкам — поэтому он в разы дешевле, но запрос `{host="ubu01"} |= "error"` всегда сужает сначала по метке.

**Хранение.** Данные в OpenSearch/Elasticsearch лежат в **индексах**, индекс разбит на **шарды** (единица распределения) и **реплики** (копии). Индексы нельзя хранить вечно — диск кончится, и кластер уйдёт в read-only. Поэтому задают две вещи: **ротацию** (когда закрываем текущий индекс и начинаем новый — по времени, размеру или числу документов) и **retention** (сколько индексов/дней храним, что удаляем). В Elasticsearch это ILM, в OpenSearch — ISM, в Graylog — Index Set с rotation strategy и retention strategy. Плюс отдельная ротация на источнике (`logrotate`, лимиты journald, максимальный размер журнала Windows).

Наконец, где это в модели OSI: syslog — прикладной уровень (L7) поверх UDP/TCP (L4); Beats и GELF — тоже L7 поверх TCP. Поэтому «пропали логи» почти всегда означает либо L4 (порт/файрвол), либо L7 (неверный формат/парсер).

## 3. Аналогия из жизни

Представьте архив компании. Каждый отдел (сервер) ведёт свой дневник в общей тетради. Раз в сутки курьер собирает тетради и везёт в центральный архив, где документы раскладывают по коробкам (индексы), а на каждую коробку заводят карточку в картотеке: дата, отдел, тип документа (это и есть индекс). Картотека — самое дорогое в архиве: коробки можно сложить в подвал и забыть, а карточки должны быть под рукой, иначе ничего не найдёшь. Поэтому в архиве есть регламент: карточки за последнюю неделю — на стеллаже у входа (hot), за месяц — в соседней комнате (warm), за год — на складе в Подольске (cold), старше — в макулатуру (delete). А ещё у каждого отдела есть правило: тетрадь заканчивается — заводим новую (ротация на источнике), иначе курьер не увезёт.

Где аналогия перестаёт работать: в реальном архиве вы не сможете за секунду найти все документы, где встречается слово «отказано», а в Graylog или Kibana это обычный запрос — именно потому, что там «картотека» строится по содержимому, а не только по дате и отделу.

## 4. Практика и команды

**1. Смотрим, что вообще есть на источнике (journald).**

```bash
journalctl --disk-usage
journalctl -u ssh -p warning --since "2026-02-12 09:00" --until "2026-02-12 12:00" -o short-iso
journalctl -f -u nginx
```

`--disk-usage` — сколько журнал занимает; `-u` — фильтр по юниту; `-p warning` — приоритет и выше (0=emerg … 7=debug); `--since/--until` — период; `-o short-iso` — формат с ISO-таймстампом; `-f` — следить в реальном времени. Типичный вывод:

```text
-- Journal begins at 2026-02-10 08:12:44 MSK, ends at 2026-02-12 14:03:11 MSK. --
2026-02-12T09:14:03+0300 UBU01 sshd[4821]: Failed password for invalid user admin from 10.10.10.99 port 51544 ssh2
```

Смотрим `Failed password` и `from` — это первый шаг любого разбора «кто ломился».

**2. Ограничиваем журнал, чтобы не съел диск.**

```ini
# /etc/systemd/journald.conf
[Journal]
Storage=persistent
SystemMaxUse=2G
SystemMaxFileSize=200M
MaxRetentionSec=30day
Compress=yes
ForwardToSyslog=yes
```

```bash
sudo systemctl restart systemd-journald
sudo journalctl --vacuum-size=1G
```

`Storage=persistent` включает `/var/log/journal` (иначе журнал только в RAM и пропадает после ребута). `SystemMaxUse` — жёсткий потолок. `--vacuum-size` подрезает уже накопленное руками.

**3. Настраиваем отправку syslog на LOG01 (rsyslog, TCP).**

```text
# /etc/rsyslog.d/50-vektor-remote.conf
*.* action(type="omfwd"
           target="10.10.10.45" port="1514" protocol="tcp"
           TCP_Framing="octet-counted"
           action.resumeRetryCount="-1"
           queue.type="linkedList" queue.size="10000")
```

`protocol="tcp"` вместо UDP — чтобы не терять; `resumeRetryCount="-1"` — переподключаться бесконечно; `queue.type="linkedList"` — буфер в памяти на время недоступности сервера. В бою добавляют `StreamDriver="gtls" StreamDriverMode="1" StreamDriverAuthMode="x509/name"` и сертификаты. Проверка синтаксиса и применение:

```bash
sudo rsyslogd -N1          # проверка конфига, «rsyslogd: End of config validation run»
sudo systemctl restart rsyslog
sudo ss -lntup | grep 1514  # на приёмнике — слушает ли порт
```

**4. Тестовая отправка сообщения.**

```bash
logger -n 10.10.10.45 -P 1514 -T -t ubu01-test "hello vektor"
```

`-n` — сервер, `-P` — порт, `-T` — по TCP (без `-T` уйдёт UDP), `-t` — тег. Это самый быстрый способ проверить, что приёмник жив, не трогая конфиги.

**5. Ротация файловых логов на источнике (logrotate).**

```text
# /etc/logrotate.d/vektor-app
/var/log/vektor/app.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    dateext
    dateformat -%Y%m%d
    create 0640 root adm
}
```

```bash
sudo logrotate -d /etc/logrotate.d/vektor-app   # dry-run, показывает, что сделает
sudo logrotate -f /etc/logrotate.d/vektor-app   # выполнить принудительно
systemctl list-timers 'logrotate*'
```

`rotate 14` — держим 14 архивов; `compress` — gzip; `delaycompress` — не сжимать самый свежий; `copytruncate` — копировать и обнулять (нужно, если приложение не умеет переоткрывать файл; у этого режима есть окно, в котором можно потерять строки — в бою предпочитают `create` + `kill -USR1`). `-d` — отладка без изменений, ей пользуйтесь всегда перед `-f`.

**6. Windows: смотрим журналы и проверяем аудит.**

```powershell
wevtutil el                                   # список журналов
wevtutil gl Security                          # конфигурация: maxSize, retention
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625; StartTime=(Get-Date).AddHours(-2)} -MaxEvents 5 |
    Format-List TimeCreated, Id, Message
auditpol /get /category:*                     # какие категории аудита включены
```

4625 — неудачный вход, 4624 — успешный, 4740 — блокировка учётной записи, 4767 — разблокировка, 5140/5145 — доступ к сетевой папке. Если `auditpol` показывает «No Auditing» для нужной категории, никаких событий в журнале не будет — и это первое, что надо проверить, когда «в логах ничего нет».

**7. Ставим агент на Windows (Winlogbeat → Graylog Beats-инпут).**

```yaml
# C:\Program Files\Winlogbeat\winlogbeat.yml
winlogbeat.event_logs:
  - name: Security
    event_id: 4624, 4625, 4740, 4767, 4672
  - name: System
    level: error, critical
output.logstash:
  hosts: ["10.10.10.45:5044"]
```

```powershell
.\winlogbeat.exe test config
.\install-service-winlogbeat.ps1
Start-Service winlogbeat
Get-Service winlogbeat
```

Тест конфига до установки сервиса обязателен: иначе сервис будет молча падать в цикле.

**8. Поднимаем Graylog в Docker Compose (LOG01).**

```bash
sudo sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch.conf

openssl rand -hex 32                       # → GRAYLOG_PASSWORD_SECRET
echo -n 'Lab#2026-vektor' | sha256sum      # → GRAYLOG_ROOT_PASSWORD_SHA2
```

```yaml
# /opt/vektor-logs/docker-compose.yml
services:
  opensearch:
    image: opensearchproject/opensearch:2.17.1   # [ПРОВЕРИТЬ актуальный тег]
    environment:
      - discovery.type=single-node
      - bootstrap.memory_lock=true
      - "OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g"
      - plugins.security.disabled=true
    ulimits: { memlock: { soft: -1, hard: -1 } }
    volumes: [ "osdata:/usr/share/opensearch/data" ]

  mongodb:
    image: mongo:7.0
    volumes: [ "mongodata:/data/db" ]

  graylog:
    image: graylog/graylog:6.1                   # [ПРОВЕРИТЬ актуальный тег]
    depends_on: [ opensearch, mongodb ]
    environment:
      - GRAYLOG_PASSWORD_SECRET=<hex_из_openssl>
      - GRAYLOG_ROOT_PASSWORD_SHA2=<sha256_из_команды_выше>
      - GRAYLOG_HTTP_EXTERNAL_URI=http://10.10.10.45:9000/
      - GRAYLOG_ELASTICSEARCH_HOSTS=http://opensearch:9200
      - GRAYLOG_MONGODB_URI=mongodb://mongodb:27017/graylog
    ports:
      - "9000:9000"
      - "1514:1514/udp"
      - "1514:1514/tcp"
      - "5044:5044/tcp"
    volumes: [ "graylogdata:/usr/share/graylog/data" ]

volumes: { osdata: , mongodata: , graylogdata: }
```

```bash
cd /opt/vektor-logs && docker compose up -d
docker compose ps
curl -s http://127.0.0.1:9200/_cluster/health?pretty
```

`vm.max_map_count` — обязательное требование OpenSearch/Elasticsearch, без него контейнер падает. `plugins.security.disabled=true` **небезопасно** и допустимо только в закрытой лабе; в бою включают TLS и аутентификацию.

**9. Проверяем индексы и retention со стороны хранилища.**

```bash
curl -s 'http://127.0.0.1:9200/_cat/indices?v&s=index&h=index,health,docs.count,store.size'
```

```text
index                     health docs.count store.size
graylog_0                 green     184223      1.2gb
graylog_1                 green      92110      604mb
```

Растёт число индексов и `store.size` — проверяйте retention. Если кластер отвечает `status: red`, а записи не идут — почти всегда кончился диск и сработал блок `cluster.blocks.read_only`.

**10. Loki: конфиг и запросы (для сравнения).**

```yaml
# /etc/loki/config.yml — однобинарный режим
auth_enabled: false
server: { http_listen_port: 3100 }
common:
  path_prefix: /var/lib/loki
  storage: { filesystem: { chunks_directory: /var/lib/loki/chunks, rules_directory: /var/lib/loki/rules } }
  replication_factor: 1
  ring: { kvstore: { store: inmemory } }
schema_config:
  configs:
    - { from: 2026-01-01, store: tsdb, object_store: filesystem, schema: v13, index: { prefix: index_, period: 24h } }
limits_config: { retention_period: 720h }
compactor: { working_directory: /var/lib/loki/compactor, retention_enabled: true, delete_request_store: filesystem }
```

```logql
{host="ubu01"} |= "Failed password"
sum by (host) (count_over_time({job="syslog"} |= "Failed password" [5m]))
```

Первая строка — поиск подстроки в потоке; вторая — счётчик по хостам за 5 минут (основа алерта «брутфорс»). `|=` — содержит, `!=` — не содержит, `|~` — regex. Версии схемы и поля `delete_request_store` помечаю `[ПРОВЕРИТЬ]` под вашу версию Loki.

**11. Ищем в Graylog через API (для скриптов и автоматизации).**

```bash
curl -s -u admin:'Lab#2026-vektor' -H 'Accept: application/json' \
  'http://10.10.10.45:9000/api/system' | head -40
```

API нужен, когда надо вытащить события в скрипт, в тикет или в отчёт. Точные пути эндпоинтов поиска зависят от версии — `[ПРОВЕРИТЬ]` в документации Graylog.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 собран полностью: DC01 (10.10.10.10), FS01 (10.10.10.20), WIN11-CL01, WIN10-CL02, UBU01 (10.10.10.30), ASTRA01 (10.10.10.31), GW01 (10.10.10.3 / 10.10.20.1), WEB01 (10.10.20.10), MON01 (10.10.10.40). PVE01 (10.10.10.70) уже есть с трека сисадмина.

Тема требует машины, которой в таблице стенда нет, — добавляем явно:

| Имя | ОС | Адрес | Роль |
|-----|----|-------|------|
| LOG01 | Ubuntu Server 24.04 LTS | 10.10.10.45 | сервер централизованных логов (Graylog + OpenSearch + MongoDB в Docker) |

Ресурсы LOG01: 4 vCPU, 8 ГБ RAM, 100 ГБ диск, адаптер в VMnet8. Можно разместить её как ВМ на PVE01 — тогда она живёт на 10.10.10.45 в той же сети. Локальный пользователь — тот же, под которым вы работали на UBU01.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-sysadmin-084`. Снять на **LOG01** (сразу после установки ОС и настройки сети), **DC01**, **UBU01**, **ASTRA01**, **GW01**, **WEB01**. Если LOG01 ещё не существует — сначала шаги 1–2, потом снапшот.

### Шаги

1. **LOG01.** Установить Ubuntu Server 24.04, задать hostname `LOG01`, статический адрес:
   ```yaml
   # /etc/netplan/00-installer-config.yaml
   network:
     ethernets:
       ens33: { dhcp4: false, addresses: [10.10.10.45/24], gateway4: 10.10.10.2, nameservers: { addresses: [10.10.10.10] } }
     version: 2
   ```
   ```bash
   sudo netplan apply && hostnamectl
   ```
   Ожидаемо: `ping 10.10.10.10` и `ping 10.10.10.30` проходят, SSH работает. На DC01 (DNS) добавить A-запись `LOG01 → 10.10.10.45` через `dnsmgmt.msc`.

2. **LOG01.** Снапшот `before-sysadmin-084` (см. выше). Дальше — установка Docker:
   ```bash
   sudo apt update && sudo apt install -y docker.io docker-compose-v2
   sudo usermod -aG docker $USER && newgrp docker
   docker version && docker compose version
   ```
   Ожидаемо: обе версии печатаются, `docker ps` не требует sudo.

3. **LOG01.** Подготовить хост под OpenSearch:
   ```bash
   sudo sysctl -w vm.max_map_count=262144
   echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch.conf
   sudo mkdir -p /opt/vektor-logs && cd /opt/vektor-logs
   openssl rand -hex 32
   echo -n 'Lab#2026-vektor' | sha256sum
   ```
   Ожидаемо: получены две строки; вторая — sha256 пароля `Lab#2026-vektor` (64 hex-символа). Пароль учебный: в бою для admin Graylog и для БД будут разные случайные пароли в менеджере секретов.

4. **LOG01.** Положить `docker-compose.yml` из раздела 4 (пункт 8), подставив секреты, и поднять:
   ```bash
   docker compose up -d
   docker compose ps
   curl -s http://127.0.0.1:9200/_cluster/health?pretty
   ```
   Ожидаемо: три контейнера `running`, кластер `"status" : "green"` (или `yellow` для single-node). Если OpenSearch в рестарте — почти всегда `vm.max_map_count`.

5. **LOG01, веб-интерфейс.** Открыть `http://10.10.10.45:9000/`, войти `admin` / `Lab#2026-vektor`. Создать вход: **System → Inputs → Select input → Syslog UDP → Launch new input**, указать Title `vektor-syslog-udp`, Bind address `0.0.0.0`, Port `1514`. Ожидаемо: инпут в состоянии **RUNNING**, кнопка **Show received messages** открывается.

6. **UBU01.** Настроить пересылку syslog (пункт 3 раздела 4), проверить и отправить тест:
   ```bash
   sudo rsyslogd -N1
   sudo systemctl restart rsyslog
   logger -n 10.10.10.45 -P 1514 -T -t ubu01-test "hello vektor"
   ```
   В Graylog в поиске набрать `source:ubu01` (или `message:"hello vektor"`). Ожидаемо: сообщение найдено в течение 5–10 секунд, в поле `source` — `UBU01`.

7. **ASTRA01, GW01, WEB01.** Повторить п.6 тем же файлом `/etc/rsyslog.d/50-vektor-remote.conf`, указав в сообщении тега имя хоста. На ASTRA01 пакет `rsyslog` может называться так же, но проверьте `systemctl status rsyslog` — в Astra Linux служба может быть уже запущена с другим набором правил `[ПРОВЕРИТЬ по вашей сборке]`. Ожидаемо: в Graylog в поиске `source:(ASTRA01 OR GW01 OR WEB01)` есть сообщения от всех трёх.

8. **LOG01.** Создать Beats-инпут: **System → Inputs → Beats → Port 5044**, Title `vektor-beats`. Ожидаемо: инпут RUNNING.

9. **DC01.** Установить Winlogbeat (официальный MSI с сайта Elastic), заменить `winlogbeat.yml` на конфиг из п.7 раздела 4, выполнить `test config`, установить сервис, запустить:
   ```powershell
   .\winlogbeat.exe test config
   .\install-service-winlogbeat.ps1
   Start-Service winlogbeat
   Get-Service winlogbeat
   ```
   Ожидаемо: сервис `Running`; в Graylog в течение минуты появляются сообщения с `source:DC01`.

10. **LOG01.** Настроить ротацию и хранение: **System → Indices → default index set → Edit**. Rotation strategy: `Index time`, period `P1D`. Retention strategy: `Delete`, `Max number of indices: 14`. Сохранить. Ожидаемо: в списке индексов появился сегодняшний `graylog_0` (или `graylog_<N>`), а старые будут удаляться при превышении 14.

11. **LOG01.** Создать stream для событий безопасности: **Streams → Create Stream**, Title `Windows-Security`, rule — поле `source` соответствует regex `^(DC01|FS01|WIN.*)$`. Затем проверить, что сообщения попали в стрим (**Streams → Windows-Security → Messages**). Ожидаемо: счётчик сообщений растёт.

12. **UBU01.** Настроить ротацию файлового лога: создать `/var/log/vektor/app.log`, положить конфиг из п.5 раздела 4, прогнать dry-run и принудительную ротацию:
    ```bash
    sudo mkdir -p /var/log/vektor && echo test | sudo tee /var/log/vektor/app.log
    sudo logrotate -d /etc/logrotate.d/vektor-app
    sudo logrotate -f /etc/logrotate.d/vektor-app
    sudo ls -l /var/log/vektor/
    ```
    Ожидаемо: появился `app.log-20260212` (сжатый — после следующего прогона из-за `delaycompress`), новый `app.log` создан с правами `0640 root adm`.

### Проверка

- [ ] `ping 10.10.10.45` с UBU01 и DC01 проходит, `LOG01` резолвится в DNS.
- [ ] `docker compose ps` на LOG01 — три контейнера `running`, `curl .../_cluster/health` отдаёт `green`/`yellow`.
- [ ] `sudo ss -lntup | grep -E '1514|5044|9000'` на LOG01 показывает слушающие порты.
- [ ] В Graylog поиск `source:ubu01` возвращает и старые, и тестовые сообщения.
- [ ] В Graylog есть сообщения минимум от 5 источников: `UBU01`, `ASTRA01`, `GW01`, `WEB01`, `DC01`.
- [ ] Index set показывает rotation `P1D` и retention `Delete / 14`.
- [ ] На UBU01 после `logrotate -f` в `/var/log/vektor/` два файла: `app.log` и `app.log-YYYYMMDD`.
- [ ] Запись в `stand-log.md` добавлена.

### Сломай и почини

> **Пользователь:** petrova (HR)
> **ВМ:** WIN10-CL02
> **Что написал:** «Не могу зайти в систему — пишет, что учётная запись заблокирована. Я пароль не меняла!»
> **Срочность:** высокая

**Как воспроизвести поломку.** На WIN10-CL02 от имени другого пользователя несколько раз подряд ввести неверный пароль для `VEKTOR\petrova` (например, в окне «Запуск от имени другого пользователя» или через `runas /user:VEKTOR\petrova cmd` с неверным паролем). Пять-семь попыток хватит, если порог блокировки в домене стандартный (смотреть `net accounts` на DC01). Дополнительно — «вторая поломка»: на UBU01 убрать из `/etc/rsyslog.d/50-vektor-remote.conf` строку `target` (или опечататься в адресе), перезапустить rsyslog и убедиться, что логи с UBU01 перестали приходить.

**Диагностика по шагам.**

1. В Graylog: `source:DC01 AND "4740"` за последний час. Находим событие блокировки — время и `TargetUserName: petrova`. Значит, порог попыток исчерпан.
2. Ищем причину: `source:(DC01 OR WIN10-CL02) AND "4625"` за тот же период. Смотрим `TargetUserName`, `IpAddress`, `WorkstationName`, `LogonType`, `SubStatus`.
3. Читаем коды: `0xC000006A` — неверный пароль, `0xC0000234` — учётка заблокирована, `0xC000006D` — неверное имя или пароль. Если `LogonType 2` и `WorkstationName = WIN10-CL02` — человек просто несколько раз ошибся на своей машине. Если `LogonType 3` и `IpAddress = 10.10.10.99` — уже повод для инцидента ИБ.
4. Проверяем, нет ли автоматики со старым паролем: на WIN10-CL02 `Get-ScheduledTask | Where-Object State -eq 'Ready'`, а также «Диспетчер учётных данных» → сохранённые пароли и старые сетевые диски на `\\FS01\`.
5. Проверяем, что логи не врут по времени: `timedatectl` на UBU01 и `w32tm /query /status` на DC01. Если часы разъехались — корреляция по времени бессмысленна.
6. Проверяем «вторую поломку»: в Graylog поиск `source:UBU01` за последние 15 минут пуст → идём на UBU01: `systemctl status rsyslog`, `sudo rsyslogd -N1`, `journalctl -u rsyslog -n 30`. Находим ошибку в конфиге.

**Решение.** `Unlock-ADAccount -Identity petrova` на DC01 (или GUI **Active Directory Users and Computers → найти пользователя → Unlock account**). Починить конфиг rsyslog на UBU01, `rsyslogd -N1`, `systemctl restart rsyslog`, проверить тестовым `logger`. Записать и то, и другое в тикет.

**Что написать пользователю.** «Мария, добрый день. Учётную запись разблокировал, можно входить. По журналам видно, что с вашей машины (WIN10-CL02) сегодня в 09:12–09:14 было несколько попыток входа с неверным паролем — похоже на опечатку или включённый Caps Lock. Пароль не менялся. Если снова заблокируется — позвоните, посмотрим вместе, скорее всего старый пароль сохранён в сетевом диске или в 1С».

**Закрытие заявки.** Категория «Доступы / блокировка учётной записи», решение — разблокировка, корневая причина — серия неудачных входов, ссылки на события 4740/4625 в Graylog приложить в комментарий. Отдельным тикетом — «исправлена пересылка логов на UBU01» (это уже инцидент мониторинга, не пользователя).

### Что изменилось на стенде

- Добавлена ВМ **LOG01**, Ubuntu Server 24.04, `10.10.10.45`, DNS-запись в зоне `corp.vektor.lab`.
- На LOG01: Docker + Compose, стек в `/opt/vektor-logs` (OpenSearch 2.x, MongoDB 7, Graylog 6.x), тома `osdata`, `mongodata`, `graylogdata`.
- Graylog: пользователь `admin` (пароль стенда), инпуты `vektor-syslog-udp` (1514/udp+tcp), `vektor-beats` (5044/tcp), index set с rotation `P1D` и retention `14` индексов, stream `Windows-Security`.
- UBU01, ASTRA01, GW01, WEB01: файл `/etc/rsyslog.d/50-vektor-remote.conf`, пересылка на `10.10.10.45:1514/tcp`.
- DC01: установлен Winlogbeat (журналы Security + System, события 4624/4625/4740/4767/4672), сервис `winlogbeat` в автозапуске.
- UBU01: файл `/etc/logrotate.d/vektor-app` и каталог `/var/log/vektor/`.
- Порог блокировки учётных записей в домене проверен (`net accounts`), учётка `petrova` разблокирована.

### Откат

Если что-то пошло не так: выключить LOG01, в VMware Workstation **Snapshot Manager → before-sysadmin-084 → Restore** на LOG01, DC01, UBU01, ASTRA01, GW01, WEB01. Альтернатива без отката всей ВМ: на LOG01 `cd /opt/vektor-logs && docker compose down` (тома сохранятся), на источниках удалить `/etc/rsyslog.d/50-vektor-remote.conf` и перезапустить rsyslog, на DC01 `Stop-Service winlogbeat; .\uninstall-service-winlogbeat.ps1`. После любого откката — запись в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Нет синхронизации времени.** → События с разных хостов не сопоставляются, разбор инцидента превращается в гадание. → Настраивайте chrony/NTP (`w32tm` на Windows), приучайтесь проверять время раньше, чем смотреть логи.
2. **Отправка всего по syslog UDP 514 «потому что проще».** → Пакеты теряются при всплеске нагрузки и при недоступности приёмника, содержимое открыто любому в сети. → TCP + TLS, буфер (`queue.type="linkedList"`) или Kafka.
3. **Ротация только на источнике либо только в бэкенде.** → Либо диск сервера забит старыми `.log`, либо OpenSearch уходит в read-only и логи перестают приниматься. → Настраивайте обе стороны и мониторьте свободное место и `_cluster/health`.
4. **«Храним всё вечно».** → Через 3–6 месяцев кончился диск, индексы читаются только на удаление, приём логов встал. → Retention по политике (например, hot 7 дней, warm 30, архив 90–365 в S3/MinIO, дальше delete) и расчёт объёма заранее.
5. **Сырые логи без нормализации.** → Поля называются по-разному у разных источников, поиск `user:petrova` не находит ничего. → Единая схема полей (ECS или соглашение внутри команды), парсинг в pipeline/Logstash с самого начала.
6. **Один узел Elasticsearch/OpenSearch без реплик и бэкапов.** → Упал диск — потеряли историю. → Минимум реплика 1 на кластере из 2+ узлов, снапшоты в S3/MinIO, проверка восстановления раз в квартал.
7. **Логи собирают, но никто не следит за самим пайплайном.** → «В логах тихо» воспринимают как «всё хорошо», хотя агент умер неделю назад. → Метрика «сообщений в минуту по источнику», алерт на нулевой поток, heartbeat-тест со всех хостов.
8. **Нет фильтрации чувствительных данных.** → В логах оказываются пароли в открытом виде, номера карт, персональные данные. → Маскирование на источнике или в pipeline, ограничение доступа к порталу логов по ролям.

**Безопасность (что нельзя в боевой среде).** Нельзя открывать Graylog/Kibana/OpenSearch в интернет без VPN и аутентификации; нельзя отключать `plugins.security` у OpenSearch и оставлять 9200 доступным из офисной сети; нельзя давать право записи в syslog-приёмник всем подряд — иначе злоумышленник «затрёт» или подделает логи; нельзя хранить логи бессрочно без политики — это и риск утечки, и нарушение 152-ФЗ; требования к регистрации событий безопасности (приказ ФСТЭК России № 17) влияют на сроки хранения — уточняйте по вашей модели угроз `[ПРОВЕРИТЬ]`. Портал логов должен быть отдельным сегментом/VLAN с ограниченным доступом, а сам сервер логов — не в одном сегменте с атакующей целью.

## 7. Вопросы на собеседовании

**1. Чем Loki отличается от Elasticsearch/OpenSearch и когда вы его выберете?**
Ответ: Loki индексирует только метки (`host`, `job`, `app`), а текст сообщений хранит сжатыми чанками в объектном хранилище и ищет перебором. Поэтому он в разы дешевле по CPU/RAM/диску и идеален для Kubernetes с большим количеством подов, где важны метки. Elasticsearch/OpenSearch индексируют все поля: поиск по произвольному содержимому быстрее, аналитика богаче, но ресурсов нужно на порядок больше. Выбор: Loki — когда источников много, а запросы простые (найти по поду, по хосту, по слову); ELK/OpenSearch или Graylog — когда нужен полнотекстовый поиск, SIEM-корреляция и сложные отчёты.
*Что хочет услышать интервьюер:* понимание компромисса «индексируем всё ↔ индексируем метки», а не заучивание списка фич.
*Красный флаг:* «Loki — это просто лёгкий Elasticsearch» или «Loki не умеет алертить».

**2. Как вы построите отказоустойчивый конвейер логов?**
Ответ: агент на источнике → локальный буфер (rsyslog disk-assisted queue или файловый буфер Fluent Bit) → транспорт с подтверждением (TCP/TLS, Beats, Kafka) → приёмник в HA (2+ узла Graylog за балансировщиком, OpenSearch минимум 3 узла с репликой 1, MongoDB replica set) → снапшоты в объектное хранилище → мониторинг самого пайплайна с алертом на нулевой поток. Ключевая мысль: лог-пайплайн — такая же прод-система, как почта, у неё есть SLA.
*Что хочет услышать:* буфер на каждом стыке, отсутствие single point of failure, контроль «а живы ли логи вообще».
*Красный флаг:* «поставим один сервер Graylog и всё».

**3. Как посчитать объём хранилища под логи и настроить retention?**
Ответ: считаем поток: источники × событий/сек × средний размер события. Пример: 50 источников × 200 сообщений/мин × 500 байт ≈ 10 000 сообщений/мин ≈ 14,4 млн/сутки ≈ 7 ГБ/сутки «сырых»; с накладными расходами индекса ×1,3–1,5 → ~10 ГБ/сутки. При хранении 30 дней — ~300 ГБ, при реплике 1 — вдвое больше, плюс запас 30%. Дальше: ротация индексов по времени (1 день), retention по числу индексов или по ILM/ISM-политике (hot → warm → cold → delete), архивирование в S3/MinIO, обязательно резерв под рост и мониторинг свободного места с алертом на 70/85%.
*Что хочет услышать:* цифры и формулу, а не «посмотрим по факту».
*Красный флаг:* «настроим retention на 365 дней, место найдётся».

**4. Сотрудник говорит: «Меня взломали, входили в мою учётку». Ваши действия по логам?**
Ответ: сначала зафиксировать время и аккаунт. Далее по централизованным логам: 4625 (неудачные входы) с `IpAddress`, `WorkstationName`, `LogonType`, `SubStatus`; 4624 с `LogonType` (2 — локально, 3 — по сети, 10 — RDP) и `IpAddress`; 4740/4767 (блокировка/разблокировка); 4672 (выдача спецпривилегий); на файловом сервере — 5140/5145 и, если включён объектный аудит, 4663. Сопоставить с логами GW01 (соединения), VPN и почты. Проверить, не создавались ли новые учётки (4720), не менялись ли группы (4728/4732). Параллельно — изолировать машину/учётку по регламенту, не дожидаясь конца расследования. Итог — таймлайн в тикете и передача в ИБ.
*Что хочет услышать:* порядок «зафиксировать → собрать события по ID → построить таймлайн → изолировать → эскалировать», знание конкретных Event ID.
*Красный флаг:* «посмотрю на его компьютере в просмотре событий» — без централизованных логов и без фиксации времени.

**5. Почему не стоит отправлять все логи по syslog UDP 514 и что использовать вместо?**
Ответ: UDP не гарантирует доставку и порядок, не имеет подтверждений и шифрования — при всплеске или перезагрузке приёмника сообщения теряются молча, а содержимое видно любому в сети (в логах бывают логины, имена файлов, иногда токены). Вместо этого: syslog over TCP с TLS (6514) и буфером, Beats/Lumberjack на 5044, GELF по TCP/TLS, OTLP, а на границе — Kafka как durable-буфер. Если UDP всё же нужен (старое железо), то только внутри отдельного management-VLAN и с пониманием, что часть логов теряется.
*Что хочет услышать:* осознание разницы между «удобно» и «гарантированно и безопасно».
*Красный флаг:* «UDP быстрее, а логи не критичны».

## 8. Источники (официальная документация)

- **Graylog** — `graylog.org`, раздел документации (go2docs.graylog.org): «Graylog Docker installation», «Inputs», «Index Sets and rotation», «Streams», «Pipeline rules». Поиск: `graylog docker compose opensearch install`.
- **Elastic** — `elastic.co/guide`: «Index Lifecycle Management», «Winlogbeat quick start», «Elasticsearch cluster health API». Поиск: `elasticsearch index lifecycle management policy`.
- **OpenSearch** — `opensearch.org/docs`: «Install and configure OpenSearch», «Index State Management», «Cluster health». Поиск: `opensearch index state management ISM`.
- **Grafana Loki** — `grafana.com/docs/loki`: «Get started», «Configuration reference», «LogQL», «Retention and compactor». Поиск: `loki single binary config retention`.
- **Promtail / Grafana Alloy** — `grafana.com/docs`: «Promtail configuration», «Alloy». Поиск: `promtail scrape config labels`.
- **systemd journald** — `freedesktop.org` (man-страницы systemd): «journald.conf», «journalctl». Поиск: `journald.conf SystemMaxUse`.
- **rsyslog** — `rsyslog.com/doc`: «omfwd module», «TLS and certificates», «Queues». Поиск: `rsyslog omfwd tls queue`.
- **RFC 5424 (syslog)** — `datatracker.ietf.org`, документ RFC 5424. Поиск: `RFC 5424 syslog protocol`.
- **logrotate** — man-страницы `man7.org` / `linux.die.net`: «logrotate(8)», «logrotate.conf(5)». Поиск: `logrotate copytruncate dateext`.
- **Microsoft Learn** — `learn.microsoft.com`: «Windows Event Forwarding», «wevtutil», «Get-WinEvent», «Audit logon events». Поиск: `wevtutil query security event 4625`.
- **ФСТЭК России** — `fstec.ru`: приказ № 17 о требованиях к защите информации (раздел о регистрации событий безопасности). Сроки хранения логов под вашу модель угроз уточнять отдельно `[ПРОВЕРИТЬ]`.
