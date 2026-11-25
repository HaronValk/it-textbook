# Централизованные логи: Graylog / ELK / Loki — развёртывание и анализ

## 1. Введение (зачем это нужно в реальной работе)

Тикет в HelpDesk: **«Иванов, WIN11-CL01: Полчаса не могу войти — пишет, что пароль неверный, а я его не менял. Вчера то же самое было. Срочно, закрываю период»**. Срочность — высокая.

Что делает L1: идёт к пользователю, сбрасывает пароль, закрывает заявку. Через день — снова. Что делает L2: не идёт никуда, а открывает Grafana, пишет `{host="dc01"} |= "4740"`, за 20 секунд видит событие блокировки учётки, в поле `CallerComputer` — `WIN11-CL01`, а рядом пачку событий 4625 с неверным паролем каждые 5 минут. Причина: сохранённый пароль в «Диспетчере учётных данных» на рабочей машине от старой учётки. Пользователь не виноват, сброс пароля не помог бы, а поиск причины по 6 серверам вручную занял бы час.

Централизованные логи — это когда все машины стенда отправляют свои журналы в одно место, где по ним можно искать, строить графики и ставить алерты. После этой главы ты будешь: понимать разницу между тремя стеками и осознанно выбирать один; развернуть Loki + Promtail + Grafana на MON01 и подключить к нему Linux-хосты и Windows; писать запросы LogQL; настроить retention (срок хранения), чтобы не убить диск; и главное — использовать логи как инструмент поиска корневой причины, а не как «где-то там лежит текстовый файл».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Логи — это прикладной уровень (L7 модели OSI). Но доставляются они транспортом: syslog по UDP/TCP 514 (RFC 3164 — старый формат, RFC 5424 — новый), GELF, HTTP/JSON, Beats, OTLP. Ключевое отличие логов от метрик (урок 90): **метрики мы опрашиваем (pull), логи нам присылают (push)**. Сервер логов не может «сходить» на 50 машин за журналами — агент сам отправляет их на приёмник.

Общая архитектура любого стека одинаковая:

```text
 [источники]              [агенты]                 [приёмник]           [хранилище]      [UI]
 DC01 (Event Log) ─┐
 WIN11-CL01 ───────┼──► Promtail / NXLog ──► Loki :3100 ──────► чанки+индекс ──► Grafana :3000
 UBU01 (journald) ─┤    Winlogbeat        ──► Logstash :5044 ──► Elasticsearch :9200 ──► Kibana :5601
 GW01  (nftables) ─┘    rsyslog/syslog-ng ──► Graylog :12201/:1514 ──► OpenSearch ──► Graylog Web :9000
```

Три стека отличаются **способом индексации**, и это определяет всё остальное:

| Стек | Что индексируется | RAM на стенде | Когда выбирают |
|---|---|---|---|
| **ELK** (Elasticsearch + Logstash + Kibana) | каждое слово в каждой строке (полнотекстовый индекс) | 8–16 ГБ | нужен поиск по содержимому, огромные объёмы, Kibana уже стандарт |
| **Graylog** (Graylog + OpenSearch/Elasticsearch + MongoDB) | тоже полный индекс, но Graylog — «умная надстройка»: готовый приём syslog/GELF, стримы, алерты, роли | 8 ГБ+ | классическая поддержка: сеть, syslog, Windows, журналы 1С; быстрый старт |
| **Loki** (Loki + Promtail/Alloy + Grafana) | только **метки** (labels); сам текст не индексируется, а сжимается в чанки | 1–2 ГБ | уже есть Grafana и Prometheus, экономно, Kubernetes, длинное хранение |

Loki-подход называется «индексируем метки, а не содержимое». Плюс: дёшево по диску и памяти. Минус: запрос `{job="syslog"}` без фильтра по меткам просканирует гигабайты — поэтому **метки (host, job, unit, env) — это святое**, а всё «переменное» (id запроса, имя пользователя, IP) в метки пихать нельзя: это взрывная кардинальность, Loki начнёт падать по памяти.

Обязательный элемент любого стека, о котором все забывают: **единое время**. У каждой записи есть timestamp. Loki по умолчанию отбрасывает записи «из прошлого» (`reject_old_samples_max_age`, обычно 168 ч) и ругается `entry too far in the future`, если часы на источнике убежали. Поэтому NTP/chrony и `timedatectl` — не формальность, а условие работоспособности логов.

Порты, которые надо помнить наизусть:

| Порт | Что |
|---|---|
| 514 UDP/TCP | syslog (приём в Graylog/rsyslog) |
| 12201 TCP/UDP | GELF (формат Graylog) |
| 5044 | Beats (Winlogbeat/Filebeat → Logstash) |
| 9200 / 5601 / 9000 | Elasticsearch / Kibana / Graylog Web+API |
| 3100 / 9096 | Loki HTTP API / gRPC |
| 9080 | Promtail (веб-страница `/targets`) |
| 3000 | Grafana |

## 3. Аналогия из жизни

Компания «Вектор» — логистическая. Представь сортировочный центр. Каждый водитель (сервер) привозит накладные (логи) в один хаб. В **ELK** кладовщик заранее выписывает карточку на **каждое слово** в каждой накладной — найти что угодно можно мгновенно, но картотека занимает три комнаты и зарплата кладовщика огромная. В **Graylog** то же самое, но с удобной приёмной: накладные принимают в стандартных форматах, сразу сортируют по потокам и шлют бригадиру SMS при аварии. В **Loki** кладовщик не читает накладные вообще: он пишет на коробке три наклейки (host, job, unit) и ставит коробку на полку; когда нужен конкретный случай — берёшь коробки с нужной наклейкой и **листаешь вручную**. Дешево, быстро, но если наклеек нет — ищешь по всему складу.

Где аналогия ломается: на реальном складе коробка лежит на одной полке, а лог-запись может одновременно иметь несколько меток и попадать в разные «полки» — плюс метки проставляет агент, и он же может ошибиться.

## 4. Практика и команды

Дальше — реальные команды под Loki (наш основной стек) плюс эквиваленты для ELK/Graylog, чтобы ты не растерялся, если на работе стоит «Элка».

**1. Живой ли приёмник.** Самая первая проверка — как `ping`, только для логов.
```bash
curl -s http://10.10.10.40:3100/ready          # Loki
curl -s http://10.10.10.40:3100/metrics | grep -E 'loki_(request_duration_seconds_count|distributor_)' | head
```
Ожидаем `ready`. В `/metrics` смотрим счётчики приёма и ошибок — точные имена метрик зависят от версии `[ПРОВЕРИТЬ]`.

**2. Какие метки вообще есть.** Если тут пусто — значит агенты ничего не прислали.
```bash
curl -s http://10.10.10.40:3100/loki/api/v1/labels
# {"status":"success","data":["host","job","unit"]}
curl -s 'http://10.10.10.40:3100/loki/api/v1/label/host/values'
# {"status":"success","data":["dc01","gw01","mon01","ubu01"]}
```

**3. Запрос через API без Grafana** — так делают скрипты и дежурные плейбуки.
```bash
curl -s -G 'http://10.10.10.40:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={host="ubu01"} |= "Failed password"' \
  --data-urlencode 'limit=5' \
  --data-urlencode "start=$(date -d '-1 hour' +%s)000000000" \
  --data-urlencode "end=$(date +%s)000000000"
```
`start/end` — наносекунды, поэтому `000000000`. `|=` — подстрока, `|~` — регулярка, `!=` — исключить.

**4. logcli — «grep по всем серверам сразу».** Ставится отдельным бинарником (в пакет loki не входит) `[ПРОВЕРИТЬ]`.
```bash
export LOKI_ADDR=http://10.10.10.40:3100
logcli query '{job="systemd-journal"} |= "error"' --since=2h --limit=20
logcli labels host
```

**5. Что видит агент.** Promtail поднимает веб-морду на 9080 — там список файлов и journal-юнитов, которые он реально читает.
```bash
curl -s http://10.10.10.40:9080/targets | head -20
# или в браузере: http://10.10.10.40:9080/targets
```
Ищи свой файл в списке. Если его нет — проблема в `scrape_configs`, а не в Loki.

**6. Службы и их журналы.** 90% проблем — тут.
```bash
systemctl status loki promtail --no-pager
journalctl -u promtail -n 50 --no-pager
journalctl -u loki -n 50 --no-pager
```
Типовые строки: `permission denied` (агент не может читать файл), `connection refused` (неверный адрес Loki), `400 Bad Request ... too far in the future` (часы).

**7. Тестовая запись «от себя».** Проверка сквозняком: источник → агент → Loki → Grafana.
```bash
logger -t vector-test "hello from ubu01 $(date -Is)"
```
Через 5–10 секунд в Grafana: `{host="ubu01"} |= "vector-test"`.

**8. Метрики из логов (LogQL metric query).** Это уже не текст, а график — основа алертов.
```logql
sum by (host) (count_over_time({job="syslog"} |~ "(?i)failed|error|crit" [5m]))
sum(count_over_time({host="dc01"} |= "4625" [10m]))
```
`[5m]` — окно, `count_over_time` — сколько строк в окне. Именно так ставится алерт «всплеск ошибок».

**9. Windows-сторона.** Смотрим журнал локально и генерируем событие для теста.
```powershell
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625,4740; StartTime=(Get-Date).AddHours(-2)} -MaxEvents 5 |
  Format-List TimeCreated, Id, Message
wevtutil gl Security                    # параметры журнала: размер, ротация
eventcreate /T ERROR /ID 999 /L APPLICATION /D "vector test event"
```
`4625` — неудачный вход, `4740` — учётка заблокирована (пишется на контроллере домена). `eventcreate` даёт «своё» событие, чтобы проверить доставку.

**10. Диск и retention.** Самая частая авария «логи есть, но сервер лёг».
```bash
du -sh /var/lib/loki/chunks /var/lib/loki/index 2>/dev/null
df -h /var/lib
journalctl --disk-usage
```
Если чанки растут быстрее, чем чистит компактор — режь `retention_period` и `ingestion_rate_mb`.

**11. Эквиваленты для ELK/Graylog.**
```bash
curl -s 'http://10.10.10.40:9200/_cluster/health?pretty'   # status: green/yellow/red, unassigned_shards
curl -s 'http://10.10.10.40:9200/_cat/indices?v'           # размеры индексов
```
В Kibana поиск идёт в **Discover**, в Graylog — в **Search**; синтаксис там другой (Lucene/DSL: `event_id:4625 AND winlog_computer:DC01`), но логика та же: сузить время → сузить поле → прочитать сообщение.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **MON01** (10.10.10.40, Ubuntu 24.04) — работает, SSH доступен, есть Grafana (ставили на уроке 90; если Grafana у тебя на другой машине — подставляй её адрес). Дай MON01 **минимум 4 ГБ RAM** (Zabbix + Prometheus + Grafana + Loki), иначе Loki будет убит OOM-killer.
- **UBU01** (10.10.10.30) и **GW01** (10.10.10.3) — работают, доступ по SSH.
- **DC01** (10.10.10.10) — Windows Server 2022 с AD DS, доступ под `VEKTOR\adm.student`, пароль `Lab#2026-vektor`.
- Свободно ~10 ГБ на диске MON01.

### Снапшот
В VMware Workstation на MON01, UBU01 и GW01 (и на DC01, если будешь делать шаг 9): **VM → Snapshot → Take Snapshot**, имя `before-l2-091`. Если хочется из CLI на Linux-гостях — снапшоты VMware делаются только с хоста, из гостя никак.

### Шаги

1. **MON01. Проверить ресурсы и время.** `free -h`, `df -h /`, `timedatectl`. Ожидаем: RAM свободно ≥1 ГБ, время `System clock synchronized: yes`. Если нет — `sudo timedatectl set-ntp true`, без синхронного времени логи «уедут».

2. **MON01. Добавить репозиторий Grafana и поставить Loki + Promtail.**
```bash
sudo mkdir -p /etc/apt/keyrings
wget -q -O - https://apt.grafana.com/gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | \
  sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt update && sudo apt install -y loki promtail
systemctl status loki --no-pager
```
Ожидаем: пакеты установлены, служба `loki` в состоянии `active`. Имена пакетов и путь к ключу сверяй с актуальной инструкцией Grafana `[ПРОВЕРИТЬ]`.

3. **MON01. Правка конфига Loki** — `/etc/loki/config.yml`. Минимум: один экземпляр (`auth_enabled: false`), filesystem-хранилище, retention 7 дней, лимиты приёма.
```yaml
auth_enabled: false
server:
  http_listen_port: 3100
  grpc_listen_port: 9096
common:
  instance_addr: 10.10.10.40
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
    - from: 2024-04-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
limits_config:
  retention_period: 168h
  ingestion_rate_mb: 4
  ingestion_burst_size_mb: 8
compactor:
  working_directory: /var/lib/loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
```
`retention_period: 168h` = 7 дней. `store: tsdb`/`schema: v13` актуальны для Loki 3.x, сверь с документацией своей версии `[ПРОВЕРИТЬ]`. После правки: `sudo systemctl restart loki && curl -s localhost:3100/ready` → `ready`.

4. **MON01. Promtail: свои логи + journald.** `/etc/promtail/config.yml`:
```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0
positions:
  filename: /var/lib/promtail/positions.yaml
clients:
  - url: http://10.10.10.40:3100/loki/api/v1/push
scrape_configs:
  - job_name: varlogs
    static_configs:
      - targets: [localhost]
        labels:
          job: varlogs
          host: mon01
          __path__: /var/log/*.log
  - job_name: journal
    journal:
      json: false
      max_age: 12h
      path: /var/log/journal
      labels:
        job: systemd-journal
        host: mon01
    relabel_configs:
      - source_labels: ['__journal__systemd_unit']
        target_label: 'unit'
```
`positions.yaml` — «закладка», докуда файл прочитан (при перезапуске агент не дублирует всё заново). `relabel_configs` достаёт имя юнита из journald в метку. Точные имена полей journal-конфига сверь `[ПРОВЕРИТЬ]`. Запуск: `sudo systemctl enable --now promtail`. Если в `journalctl -u promtail` видишь `permission denied` на `/var/log/syslog` — служба работает не от root, добавь её пользователя в группу `adm`: `sudo usermod -aG adm promtail && sudo systemctl restart promtail` `[ПРОВЕРИТЬ пользователя юнита]`.

5. **Grafana → источник данных.** Браузер: `http://10.10.10.40:3000` → **Connections → Data sources → Add data source → Loki**, URL `http://10.10.10.40:3100`, **Save & test** → «Data source successfully connected». (Можно и файлом-провижинингом в `/etc/grafana/provisioning/datasources/`, но вручную нагляднее.)

6. **Первый поиск.** **Explore** → datasource Loki → Label browser → `job=varlogs` → **Run query**. Ожидаем строки логов MON01. Затем усложни: `{job="systemd-journal"} |= "error"` и `{host="mon01"} |~ "sshd|sudo"`.

7. **UBU01 → Loki.** Те же шаги 2 и 4, но в конфиге: `clients: - url: http://10.10.10.40:3100/loki/api/v1/push` и метка `host: ubu01`. Проверка: `logger -t vector-test "hello from ubu01"`, через 10 секунд в Grafana `{host="ubu01"} |= "vector-test"` → строка найдена.

8. **GW01 → Loki.** Ставим Promtail (Debian 12 — тот же репозиторий Grafana), метка `host: gw01`. Дополнительно забери логи nftables, если они пишутся в файл `/var/log/nftables.log` (настройка логирования nftables — тема урока про GW01; если файла нет, просто логируй `/var/log/syslog`). Ожидаем в Grafana `{host="gw01"}`.

9. **DC01 (Windows) → Loki.** Установи Promtail для Windows (архив с GitHub releases Grafana `[ПРОВЕРИТЬ]`) в `C:\ProgramData\promtail`. Конфиг `promtail-windows.yaml`:
```yaml
server:
  http_listen_port: 9080
  grpc_listen_port: 0
positions:
  filename: C:\ProgramData\promtail\positions.yaml
clients:
  - url: http://10.10.10.40:3100/loki/api/v1/push
scrape_configs:
  - job_name: windows-security
    windows_events:
      use_incoming_timestamp: true
      bookmark_path: C:\ProgramData\promtail\bookmark-security.xml
      eventlog_name: Security
      xpath_query: '*[System[(EventID=4624 or EventID=4625 or EventID=4740)]]'
      labels:
        job: windows-security
        host: dc01
```
`windows_events` читает журнал Windows Events через API; `bookmark_path` — тот же «закладка», чтобы после перезапуска не перечитывать журнал целиком. Фильтр `xpath_query` оставь минимальным: тянуть весь Security-журнал — это мегабайты в минуту. Имена полей проверь по докам Promtail `[ПРОВЕРИТЬ]`; легальные альтернативы агенту — **NXLog CE** и **Grafana Alloy** (Promtail Grafana переводит в режим поддержки в пользу Alloy `[ПРОВЕРИТЬ]`). Запусти как службу: `promtail-windows-amd64.exe -config.file=... ` и через `sc.exe create`/NSSM — в постоянную службу. Ожидаем в Grafana `{host="dc01"}` → события 4624 при логине.

10. **Алерт.** Grafana → **Alerting → Alert rules → New alert rule**, запрос:
```logql
sum(count_over_time({host="dc01"} |= "4625" [10m])) > 5
```
Condition — `IS ABOVE 5`, evaluation every 1m, contact point — тот же, что в уроке 90. Ожидаем: после шага 11 (генерация неудачных входов) алерт переходит в **Firing**. Правило живёт в Grafana, а не в Loki — это нормально и удобно, если Grafana уже есть.

11. **Проверка end-to-end с Windows.** На WIN11-CL01 (под `ivanov`, пароль `Lab#2026-vektor`):
```cmd
net use \\DC01\IPC$ /user:VEKTOR\ivanov WrongPass
```
3–5 раз. Ожидаем на DC01 события 4625, а в Grafana — строки `{host="dc01"} |= "4625"` с полем `Account Name: ivanov` и `Caller Computer Name: WIN11-CL01`.

12. **Журнал стенда.** Записать в `stand-log.md`: MON01 получил роли Loki + Promtail; агенты Promtail на UBU01, GW01, DC01; метки `host/job`; retention 7 дней; datasource Loki в Grafana; алерт на 4625.

### Проверка
- [ ] `curl -s http://10.10.10.40:3100/ready` → `ready`
- [ ] `/loki/api/v1/label/host/values` содержит `mon01`, `ubu01`, `gw01`, `dc01`
- [ ] `logger -t vector-test` с UBU01 находится в Grafana за 1 минуту
- [ ] `{host="gw01"}` возвращает строки, время в них совпадает с реальным (±1 мин)
- [ ] `{host="dc01"} |= "4624"` даёт события входов после шага 9
- [ ] Алерт на 4625 существует и срабатывает после шага 11
- [ ] `df -h /var/lib` на MON01 — заполнение < 70%

### Сломай и почини

> **Пользователь:** Иван Иванов (`ivanov`, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Третий раз за неделю не могу войти — пишет, что пароль неверный, хотя я его не менял. Приходится звонить, вы сбрасываете, через день опять. У меня отчёт, срочно!»
> **Срочность:** Высокая

**Как воспроизвести поломку (намеренно):**
1. На DC01 под `adm.student`: `Stop-Service promtail; Set-Service promtail -StartupType Disabled` — имитируем «после обновления служба не поднялась».
2. На WIN11-CL01 пять раз подряд: `net use \\DC01\IPC$ /user:VEKTOR\ivanov WrongPass` — генерируем 4625 и, если в домене настроена политика блокировки, 4740. (Если политика блокировки не настроена — события 4740 не будет, диагностируй по 4625.)

**Диагностика по порядку:**
1. Grafana: `{host="dc01"}` за последний час → **пусто**. Отличаем «нет события» от «нет доставки»: пусто по всем событиям = сломана доставка, а не «всё хорошо».
2. `{host="ubu01"}` → строки есть. Значит Loki и Grafana живы, проблема на стороне источника.
3. На DC01: `Get-Service promtail` → `Stopped`, `StartType: Disabled`. Вот и причина «слепоты».
4. `Set-Service promtail -StartupType Automatic; Start-Service promtail`, смотрим лог агента — пошли ли записи.
5. Снова в Grafana: `{host="dc01"} |= "4740"` → находим событие, в `Caller Computer Name` — `WIN11-CL01`, `Account Name` — `ivanov`.
6. Рядом `{host="dc01"} |= "4625"` → серия попыток каждые ~5 минут с `Status: 0xc000006a` (неверный пароль) с той же машины.
7. На WIN11-CL01: `control keymgr.dll` → в «Учётных данных Windows» находим сохранённую запись `VEKTOR\ivanov` со старым паролем (типовая причина — старый RDP/сетевой ресурс в «Диспетчере учётных данных» или миссия с сохранённым паролем). Удаляем запись, перелогиниваемся, сбрасываем блокировку в AD (ADUC → учётка → Unlock account / `Unlock-ADAccount -Identity ivanov`).

**Ответ пользователю:** «Иван, проблема не в вас и не в вашем пароле: на рабочей машине было сохранено старое подключение к сетевой папке со старым паролем — оно каждые 5 минут пыталось войти и блокировало учётку. Сохранённую запись удалил, блокировку снял, вход работает. Если снова повторится — не меняйте пароль, позвоните, посмотрим за 5 минут».

**Закрытие заявки:** категория «Доступ / Блокировка учётной записи», решение — «удалены устаревшие сохранённые учётные данные на рабочей машине», корневая причина — «сохранённые креды после плановой смены пароля», в комментарий — ссылка на события 4740/4625 в Grafana (время, машина). Отдельной заявкой — включить автозапуск службы Promtail на DC01.

### Что изменилось на стенде
- **MON01 (10.10.10.40):** добавлены Loki (HTTP 3100, gRPC 9096) и Promtail (9080); хранилище `/var/lib/loki`; retention 168 ч; datasource `Loki` в Grafana; alert rule на 4625.
- **UBU01 (10.10.10.30), GW01 (10.10.10.3):** установлен Promtail, метки `host=ubu01` / `host=gw01`, `job=syslog`.
- **DC01 (10.10.10.10):** установлен Promtail для Windows, читает Security (4624/4625/4740), метки `host=dc01`, `job=windows-security`.
- **Grafana:** новый datasource и правило алертинга; порт 3000 остаётся единственной точкой входа для просмотра логов.

### Откат
VMware Workstation → выбрать ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-l2-091` → **Go to**. Повторить на MON01, UBU01, GW01, DC01. Учти: откат MON01 вернёт Grafana к состоянию до датасорса, но не тронет машины, где снапшот не откатывали, — поэтому откатывай все четыре.

## 6. Ошибки новичков и безопасность

1. **Пихать всё в метки.** `user="ivanov"`, `request_id="abc123"` в labels → тысячи потоков, Loki съедает память и падает. → Метки только с малым числом значений: `host`, `job`, `env`, `unit`.
2. **Забыть про retention.** Диск заполняется за месяц, и падает не только Loki, но и Zabbix на той же ВМ. → `retention_period` + мониторинг `df` (урок 89/90) + алерт на 80%.
3. **Собирать «всё».** Логи уровня DEBUG со всех сервисов = гигабайты в день и счёт за диск. → Уровень INFO и выше, фильтр по `xpath_query`/`__path__`.
4. **Не синхронизировать время.** Часы убежали — записи отвергаются (`too far in the future`) или ищутся «не в том окне». → `timedatectl`, chrony, проверка в чек-листе.
5. **Ставить Loki на 1 ГБ RAM рядом с Zabbix.** OOM-killer убивает то один, то другой сервис, и «мониторинг врёт». → Планировать RAM заранее, минимум 4 ГБ на такую ВМ.
6. **Хранить в логах пароли и ПДн.** Токены, номера карт, пароли в URL — типовое. → Маскирование на агенте (stages в Promtail / grok в Logstash), а не «потом почистим».
7. **Думать, что ELK/Loki заменяет бэкап.** Логи — это данные, но не эталон конфигурации. → Логи не отменяют бэкап БД и конфигов.
8. **Искать причину в одном логе.** Смотри корреляцию: время события на DC01 ↔ время на клиенте ↔ время на FS01. Один лог почти никогда не даёт полной картины.

**Безопасность.** На стенде мы ставим Loki с `auth_enabled: false` и Grafana с дефолтным паролем — **в боевой среде так нельзя**: любой в сети прочитает логи, а в логах бывают учётки и внутренние адреса. В продакшене: аутентификация (Grafana с LDAP/AD, `X-Scope-OrgID` для Loki), TLS между агентом и приёмником, сегментация сети (приёмник логов — отдельный VLAN), регулярная ротация учётных данных, разграничение доступа по командам (в Graylog — роли и стримы, в Kibana — Spaces). И отдельно: не открывай 3100/9200/5044 в интернет — это классическая точка входа для атакующих, которые ищут открытые Elasticsearch.

## 7. Вопросы на собеседовании

**1. Чем Loki отличается от ELK и когда вы выберете Loki?**
Ответ: ELK/Elasticsearch индексирует содержимое — полнотекстовый поиск, но дорого по CPU/RAM/диску. Loki индексирует только метки, а текст сжимает в чанки и ищет по нему перебором в рамках выбранных меток — дешево и быстро на запись, медленнее на «поиск по всему». Loki выбирают, когда уже есть Grafana/Prometheus, когда объёмы большие, а бюджета мало, когда нужен Kubernetes (Promtail/Alloy) и когда типовой запрос — «покажи логи этого хоста за час».
*Что хочет услышать:* понимание «индексируем метки, а не содержимое» и компромисса цена/скорость поиска.
*Красный флаг:* «Loki — это просто лёгкая версия ELK», «Loki тоже всё индексирует».

**2. Почему агент не отправляет логи, а Loki отвечает 400?**
Ответ: чаще всего три причины — рассинхронизация времени (запись старше `reject_old_samples_max_age` или в будущем), неправильный формат/пустой timestamp, превышение лимитов приёма (`ingestion_rate_mb` → 429). Смотрим `journalctl -u promtail`, `/targets`, `timedatectl`, конфиг клиента.
*Что хочет услышать:* алгоритм «проверяем агент → сеть → время → лимиты», а не «перезапустил, заработало».
*Красный флаг:* «Просто увеличь retention и ingestion_rate» без разбора причины.

**3. Как вы будете искать причину блокировки учётной записи с помощью централизованных логов?**
Ответ: на DC01 включаем сбор Security-журнала, ищем 4740 (блокировка, поле Caller Computer Name) и парные 4625 (неудачный вход, статус `0xc000006a` — неверный пароль), смотрим частоту и время; по имени машины находим источник, проверяем сохранённые учётные данные/службы/планировщик с устаревшим паролем. Проверяем корреляцию с плановой сменой пароля.
*Что хочет услышать:* знание Event ID и умение идти от симптома к корневой причине, а не сбрасывать пароль.
*Красный флаг:* «Сброшу пароль и посмотрю», «пользователь сам виноват».

**4. Что такое кардинальность меток и почему это важно в Loki?**
Ответ: число уникальных комбинаций значений метки. Каждая новая комбинация = новый «поток» (stream), и Loki держит его в памяти. Метки с уникальными значениями (id запроса, IP, имя файла) создают тысячи потоков → рост памяти и деградация. Правильно: метки — только с малым фиксированным набором значений, остальное — в текст записи и фильтр в запросе.
*Что хочет услышать:* понимание, что метка — это индекс, и индекс должен быть маленьким.
*Красный флаг:* «Чем больше меток, тем удобнее искать».

**5. Как обеспечить, чтобы диски не закончились из-за логов?**
Ответ: политика хранения (`retention_period` + компактор в Loki, ILM/шаблоны индексов в Elasticsearch, индекс-ротация в Graylog), лимиты приёма, фильтрация на агенте (не собирать DEBUG), мониторинг заполнения диска и алерт на 80%, периодическая проверка, что retention реально работает (а не просто прописан).
*Что хочет услышать:* «retention + лимиты + мониторинг + проверка», связка с capacity planning.
*Красный флаг:* «Дисков много, хватит».

## 8. Источники (официальная документация)

- **grafana.com** — раздел Grafana Loki: «Loki documentation», «Send logs», «LogQL»; поиск: `grafana loki logql query examples`, `loki retention config compactor`, `promtail scrape config journal`.
- **grafana.com** — раздел про агенты: «Promtail», «Grafana Alloy»; поиск: `promtail windows_events configuration`, `promtail deprecated alloy migration` `[ПРОВЕРИТЬ статус Promtail]`.
- **grafana.com** — «Grafana Alerting» (правила и contact points); поиск: `grafana alerting logql alert rule`.
- **docs.graylog.org** — «Graylog documentation»: установка, inputs (GELF, Syslog), streams, alerts, sizing; поиск: `graylog install ubuntu opensearch requirements`, `graylog inputs gelf udp`.
- **elastic.co/guide** — «Elasticsearch Guide», «Kibana Guide», «Logstash / Beats»; поиск: `elastic index lifecycle management ilm`, `winlogbeat install windows service`.
- **learn.microsoft.com** — «Windows Event Log», `wevtutil`, `Get-WinEvent`, аудит событий 4624/4625/4740; поиск: `windows security event 4740 caller computer name`, `wevtutil command line reference`.
- **documentation.ubuntu.com** — «Ubuntu Server Guide»: systemd-journald, rsyslog, chrony; поиск: `ubuntu journald persistent storage`, `rsyslog forward to remote server`.
- **opensearch.org/docs** — «OpenSearch documentation» (легальная альтернатива Elasticsearch под Apache 2.0, если с лицензией Elastic есть вопросы); поиск: `opensearch index state management`.
- **nxlog.co/documentation** `[ПРОВЕРИТЬ актуальность CE-версии]` — бесплатный агент для Windows-журналов; поиск: `nxlog community edition windows eventlog to syslog`.
- Лицензии: Loki — AGPLv3 `[ПРОВЕРИТЬ]`, Elasticsearch/Kibana — Elastic License 2.0 / SSPL (не OSI-open-source, базовый уровень бесплатен), Graylog Open — SSPL `[ПРОВЕРИТЬ]`, OpenSearch — Apache 2.0. Если скачивание Docker-образов из РФ затруднено — используй официальные apt-репозитории и GitHub releases, а не сторонние «сборки-репаки».
