# Alertmanager и политики оповещений: маршрутизация, подавление, эскалации, on-call

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очередь дежурной смены:

> **Пользователь:** Сидоров Алексей (склад, но сегодня дежурит по авариям) / **ВМ:** MON01
> **Что написал:** «Ночью в Telegram пришло 214 сообщений. Все про WEB01. Пока читал — проспал аварию на FS01. Разберитесь, что бывают только настоящие алерты, а не этот спам».
> **Срочность:** высокая.

Ситуация классическая. Prometheus умеет вычислять правила и «стрелять» алертами, но он ничего не знает про людей: кто дежурит, куда звонить, что делать с сотней однотипных сигналов за минуту и как не разбудить начальника из-за мигнувшего на 30 секунд пинга. За это отвечает отдельный сервис — **Alertmanager**. Он не собирает метрики и не считает PromQL, он занимается только доставкой: группирует, маршрутизирует, глушит, подавляет и повторяет.

После этой главы ты должен: поднять Alertmanager рядом с Prometheus, описать дерево маршрутов как код, настроить отправку в Telegram через webhook, научиться создавать silence'ы и inhibition-правила, объяснить на собеседовании, как работает дежурство on-call и эскалация, и понимать, чем PagerDuty отличается от «просто бота в чате».

## 2. Теория «под капотом»

Prometheus при каждом вычислении alerting-правила отправляет список активных алертов через HTTP POST на `/api/v1/alerts` (и `/api/v2/alerts`) Alertmanager'а. Соединение — **L7, HTTP/1.1, порт 9093** по умолчанию. Дальше внутри Alertmanager'а алерт проходит через конвейер:

```text
Prometheus (9090)
  alerting rules → state: inactive | pending (for:) | firing
        │  HTTP POST /api/v2/alerts  (все активные, каждые evaluation_interval)
        ▼
Alertmanager (9093)
  1. Dedup        — одинаковый набор меток = один алерт
  2. Inhibition   — не шлём, если есть «подавляющий» алерт
  3. Silencing    — не шлём, если есть активный silence
  4. Routing      — идём по дереву route, ищем receiver
  5. Grouping     — склеиваем алерты по group_by
  6. Notification — group_wait → отправка → group_interval → repeat_interval
        │
        ├─ webhook (Telegram-шлюз, SMTP, Mattermost)
        ├─ pagerduty_configs (Events API v2)
        └─ email / slack / opsgenie / victorops / wechat / webex
```

Ключевые понятия, которые спрашивают на собеседовании:

- **route** — дерево. У каждого узла свой набор matchers и свой receiver. Алерт идёт от корня вниз; по умолчанию останавливается на первом совпавшем дочернем узле, если не указан `continue: true`.
- **matchers** — фильтр по меткам. Современный синтаксис `severity="critical"`, `severity=~"critical|warning"`. Старые `match`/`match_re` устарели.
- **group_by** — по каким меткам склеивать алерты в одно уведомление. Обычно `alertname`, `instance`, `cluster`.
- **group_wait** — сколько ждать после первого алерта группы, чтобы дособрать остальные (обычно 30s).
- **group_interval** — минимальный интервал между уведомлениями по уже отправленной группе, если в ней появились новые алерты (5m).
- **repeat_interval** — как часто повторять уведомление, если алерт всё ещё горит (4h).
- **inhibition** — правило «если горит X, не шли Y». Задаётся `source_matchers`, `target_matchers` и `equal` (по каким меткам объекты должны совпадать).
- **silence** — ручное подавление по меткам с временным окном, автором и комментарием. Живёт в Alertmanager'е, а не в Prometheus.
- **HA-кластер** — несколько Alertmanager'ов общаются по **порту 9094** (gossip, memberlist) и дедуплицируют уведомления, чтобы при двух репликах не пришло два сообщения. Silence'ы тоже синхронизируются.

Есть ещё «мёртвая рука» — **Watchdog alert**: правило, которое горит всегда. Если оно перестало приходить, значит сломан весь конвейер мониторинга, а не «всё хорошо».

## 3. Аналогия из жизни

Представь аварийно-диспетчерскую службу жилого района. В доме гаснет свет — от жильцов летят десятки звонков. Диспетчер не отправляет бригаду на каждый звонок: он видит, что все они про один адрес, и формирует **одну заявку** (группировка). Если уже известно, что в доме нет электричества целиком, он не принимает к исполнению отдельные жалобы «не работает лампочка в подъезде» — они всё равно следствие общей аварии (подавление). Если дом стоит на плановом ремонте, диспетчер пишет в журнале: «до 18:00 заявки по этому адресу не принимать» (silence). А если бригада не подтвердила выезд за 15 минут — звонок идёт старшему смены (эскалация).

Где аналогия ломается: диспетчер читает формулировки и понимает контекст, а Alertmanager — нет. Он смотрит только на метки, и если ты ошибся в matchers, он промолчит про настоящую аварию так же уверенно, как и про мусорную.

## 4. Практика и команды

**1. Проверка конфига перед запуском** — самая важная команда, делай её всегда до рестарта.

```bash
amtool check-config /opt/alertmanager/alertmanager.yml
```

```text
Checking '/opt/alertmanager/alertmanager.yml'  SUCCESS
Found:
 - global config: smtp_smarthost, resolve_timeout
 - 2 inhibitions
 - 4 receivers
 - 3 routes
```
Смотрим: `SUCCESS`, число inhibitions и receivers. Если в конфиге опечатка в имени receiver — здесь и увидишь.

**2. Прогон алерта по дереву маршрутов** — тест без реального срабатывания.

```bash
amtool config routes test --config.file=alertmanager.yml \
  --verify.receivers=telegram-critical \
  severity=critical instance=10.10.20.10:9100
```

```text
Matching routes:
[0] severity="critical" → receiver: telegram-critical
```
[ПРОВЕРИТЬ точный формат вывода в твоей версии — команда может печатать просто имя receiver]. Смысл: ты подставляешь набор меток и видишь, на какой receiver он попадёт. Прогоняй так каждое новое правило.

**3. Отправить синтетический алерт руками**

```bash
amtool --alertmanager.url=http://127.0.0.1:9093 alert add \
  alertname=TestAlert severity=warning instance=10.10.20.10:9100 \
  summary="Проверка канала оповещений"
```
Ожидаемо: сообщение в Telegram в течение `group_wait` (30s). Так проверяют канал, не дожидаясь реальной аварии.

**4. Посмотреть активные алерты**

```bash
amtool --alertmanager.url=http://127.0.0.1:9093 alert query
```
```text
Alertname   Starts At   Summary
TestAlert   ...         Проверка канала оповещений
```

**5. Silence — заглушить на время работ**

```bash
amtool --alertmanager.url=http://127.0.0.1:9093 silence add \
  alertname=InstanceDown instance=10.10.20.10:9100 \
  --duration=2h --author=sidorov --comment="Плановые работы на WEB01, заявка CHG-114"
```
`--duration` — обязателен, `--author` и `--comment` — не формальность: через месяц именно по ним поймут, кто и зачем заглушил аварию.

**6. Проверить и снять silence**

```bash
amtool silence query
amtool silence expire 4f8a1c9e-...   # ID из предыдущей команды
```
Silence ищется по ID или по меткам (`amtool silence query alertname=InstanceDown`).

**7. Перечитать конфиг без рестарта** (если включён `--web.enable-lifecycle`)

```bash
curl -X POST http://127.0.0.1:9093/-/reload
```
В systemd-юните то же самое делает `ExecReload=/bin/kill -HUP $MAINPID`.

**8. Отправить алерт напрямую в API** — пригодится для интеграций и отладки

```bash
curl -X POST http://127.0.0.1:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"ApiTest","severity":"info","instance":"test:9100"},
        "annotations":{"summary":"через API"}}]'
```

**9. Проверить, что Prometheus вообще видит Alertmanager.** В UI Prometheus: `Status → Runtime & Build Information`, блок Alertmanagers. Или

```bash
curl -s http://10.10.10.40:9090/api/v1/alertmanagers | head
```
Если список пуст — Prometheus не знает, куда слать, и никакие правила не дадут уведомлений.

**10. Узнать `chat_id` для Telegram-бота**

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getUpdates" | grep -o '"chat":{"id":-[0-9]*'
```
[ПРОВЕРИТЬ формат ответа — структура JSON может отличаться]. Практичнее: написать боту в чат любое сообщение и посмотреть `getUpdates`.

**11. Посмотреть логи и статус сервиса**

```bash
systemctl status alertmanager --no-pager
journalctl -u alertmanager -n 50 --no-pager | grep -i -E 'error|warn'
```
Типичная ошибка в логах: `Notify for alerts failed` — значит получатель недоступен (бот, SMTP, PagerDuty).

**12. GUI.** Всё то же доступно в браузере: `http://10.10.10.40:9093` → вкладки **Alerts**, **Silences** (кнопка *New Silence*), **Status** (там же текущий конфиг и форма тестового алерта).

## 5. Лабораторная работа

### Предусловия

- **MON01** (10.10.10.40, Ubuntu Server 24.04) — с Prometheus из урока 78 на порту 9090, Docker установлен, есть исходящий доступ в интернет (нужен для Telegram API).
- **GIT01** (10.10.10.50) — GitLab CE из более раннего урока, доступен проект `monitoring`.
- **UBU01** (10.10.10.30) и **WEB01** (10.10.20.10) — с `node_exporter` на 9100 и правилом `InstanceDown` из урока 78.
- Свой Telegram-бот, созданный через **@BotFather**, и приватный чат «Дежурство Вектор». Токен держим только в `.env` на MON01, **не в Git**.
- Если интернета на MON01 нет — замени Telegram на `email_configs` (SMTP-релей) или `webhook_configs` на локальный Mattermost; логика главы не меняется.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-080`. Сделать на **MON01** и **GIT01**.

### Шаги

1. **GIT01, в GitLab.** В проекте `monitoring` создай каталоги `alertmanager/` и `alertmanager/runbooks/`. Цель: конфиг живёт в Git, а не «я поправил руками на сервере».
2. **MON01.** Клонируй репозиторий и создай структуру:
   ```bash
   sudo mkdir -p /opt/monitoring && sudo chown $USER /opt/monitoring
   git clone git@10.10.10.50:monitoring/monitoring.git /opt/monitoring/repo
   cd /opt/monitoring/repo && ls alertmanager/
   ```
   Ожидаемо: пустой каталог `alertmanager/`.
3. **MON01, установка Alertmanager.** Скачай релиз с github.com/prometheus/alertmanager (версию `0.27.x` [ПРОВЕРИТЬ актуальную]) и распакуй:
   ```bash
   cd /tmp
   wget https://github.com/prometheus/alertmanager/releases/download/v0.27.0/alertmanager-0.27.0.linux-amd64.tar.gz
   tar xzf alertmanager-0.27.0.linux-amd64.tar.gz
   sudo install -m 0755 alertmanager-0.27.0.linux-amd64/alertmanager /usr/local/bin/
   sudo install -m 0755 alertmanager-0.27.0.linux-amd64/amtool /usr/local/bin/
   sudo useradd --no-create-home --shell /usr/sbin/nologin prometheus || true
   sudo mkdir -p /opt/alertmanager/data && sudo chown -R prometheus:prometheus /opt/alertmanager
   alertmanager --version
   ```
   Ожидаемо: печатается версия.
4. **MON01.** Создай `/opt/alertmanager/alertmanager.yml` (кладём в Git как `alertmanager/alertmanager.yml`):
   ```yaml
   global:
     resolve_timeout: 5m

   route:
     receiver: 'telegram-oncall'
     group_by: ['alertname', 'instance']
     group_wait: 30s
     group_interval: 5m
     repeat_interval: 4h
     routes:
       - matchers:
           - severity = "critical"
         receiver: 'telegram-critical'
         group_wait: 10s
         repeat_interval: 1h
       - matchers:
           - severity = "info"
         receiver: 'null'

   receivers:
     - name: 'null'
     - name: 'telegram-oncall'
       webhook_configs:
         - url: 'http://127.0.0.1:8060/telegram'
           send_resolved: true
     - name: 'telegram-critical'
       webhook_configs:
         - url: 'http://127.0.0.1:8061/telegram'
           send_resolved: true

   inhibit_rules:
     - source_matchers:
         - severity = "critical"
       target_matchers:
         - severity =~ "warning|info"
       equal: ['alertname', 'instance']
     - source_matchers:
         - alertname = "InstanceDown"
       target_matchers:
         - alertname =~ ".*Down|.*Unreachable"
       equal: ['instance']
   ```
   Ожидаемо: файл создан. `send_resolved: true` — чтобы приходило и «всё прошло», иначе дежурный не знает, закрывать ему инцидент или нет.
5. **MON01.** Проверь конфиг: `amtool check-config /opt/alertmanager/alertmanager.yml`. Ожидаемо: `SUCCESS`, 2 inhibitions, 3 receivers.
6. **MON01.** Подними Telegram-шлюзы. Создай `/opt/monitoring/docker-compose.yml`:
   ```yaml
   services:
     dialer-oncall:
       image: timonwong/prometheus-webhook-dialer:latest
       restart: unless-stopped
       command:
         - --telegram.bot-token=${TG_BOT_TOKEN}
         - --telegram.chat-id=${TG_CHAT_ONCALL}
       ports: ["127.0.0.1:8060:8060"]
     dialer-critical:
       image: timonwong/prometheus-webhook-dialer:latest
       restart: unless-stopped
       command:
         - --telegram.bot-token=${TG_BOT_TOKEN}
         - --telegram.chat-id=${TG_CHAT_CRITICAL}
       ports: ["127.0.0.1:8061:8060"]
   ```
   Флаги `--telegram.bot-token` / `--telegram.chat-id` и порт по умолчанию 8060 — [ПРОВЕРИТЬ по README образа] — у форка могут отличаться. Рядом положи `.env` (права `chmod 600`), добавь `.env` в `.gitignore`. Запуск:
   ```bash
   cd /opt/monitoring && docker compose up -d && docker compose ps
   ```
   Ожидаемо: два контейнера в статусе `running`.
7. **MON01.** systemd-юнит `/etc/systemd/system/alertmanager.service`:
   ```ini
   [Unit]
   Description=Prometheus Alertmanager
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=prometheus
   Group=prometheus
   ExecStart=/usr/local/bin/alertmanager \
     --config.file=/opt/alertmanager/alertmanager.yml \
     --storage.path=/opt/alertmanager/data \
     --web.listen-address=0.0.0.0:9093 \
     --cluster.listen-address=0.0.0.0:9094 \
     --web.external-url=http://10.10.10.40:9093 \
     --web.enable-lifecycle
   ExecReload=/bin/kill -HUP $MAINPID
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now alertmanager && systemctl is-active alertmanager
   ```
   Ожидаемо: `active`. UI открывается на `http://10.10.10.40:9093`.
8. **MON01.** Пропиши Alertmanager в Prometheus (`/etc/prometheus/prometheus.yml`) и перечитай конфиг:
   ```yaml
   alerting:
     alertmanagers:
       - static_configs:
           - targets: ['127.0.0.1:9093']
         timeout: 10s
   ```
   ```bash
   sudo promtool check config /etc/prometheus/prometheus.yml
   curl -X POST http://127.0.0.1:9090/-/reload
   ```
   Ожидаемо: `SUCCESS`, в Prometheus UI `Status → Runtime` Alertmanager виден.
9. **MON01.** Прогони маршруты: `amtool config routes test --config.file=/opt/alertmanager/alertmanager.yml severity=critical instance=10.10.20.10:9100` → должен вести на `telegram-critical`; тот же тест с `severity=warning` → `telegram-oncall`; с `severity=info` → `null`.
10. **MON01.** Синтетический алерт: `amtool --alertmanager.url=http://127.0.0.1:9093 alert add alertname=TestAlert severity=critical instance=10.10.20.10:9100`. Ожидаемо: через ~10 с сообщение в чате «Дежурство Вектор» (критичный шлюз).
11. **MON01.** Silence: `amtool --alertmanager.url=http://127.0.0.1:9093 silence add alertname=TestAlert --duration=10m --author=adm.student --comment="Тест silence"`. Повтори шаг 10 — второго сообщения быть не должно, а в UI во вкладке **Silences** появится запись, у алерта — метка «silenced».
12. **GIT01/MON01.** Закоммить `alertmanager.yml`, `docker-compose.yml`, `runbooks/` (без `.env`), запушь, допиши изменения в `stand-log.md`.

### Проверка

- [ ] `amtool check-config` возвращает `SUCCESS`.
- [ ] В Prometheus UI в блоке Alertmanagers указан `10.10.10.40:9093`.
- [ ] `amtool config routes test` для `severity=critical` ведёт на `telegram-critical`, для `warning` — на `telegram-oncall`.
- [ ] Синтетический алерт дошёл в оба чата в соответствии с severity.
- [ ] Пришло сообщение с пометкой **Resolved** после `amtool alert add` с истёкшим TTL / снятия алерта.
- [ ] Silence создаётся, отображается в UI и реально блокирует уведомление.
- [ ] Inhibition: при одновременных `InstanceDown` (critical) и `NodeLoadHigh` (warning) на одном `instance` в чат уходит только critical.
- [ ] Конфиг в Git, `.env` — не в Git (`git log --stat | grep .env` пусто).

### Сломай и почини

> **Пользователь:** Сидоров Алексей (дежурный) / **ВМ:** MON01
> **Что написал:** «После обеда в чат дежурства прилетело 187 сообщений про WEB01 и UBU01, лента умерла. До этого всё было нормально».
> **Срочность:** высокая.

**Воспроизводим поломку сами** (делай в конце лабы, потом откатишься снапшотом):

```bash
cd /opt/monitoring/repo/alertmanager
cp alertmanager.yml alertmanager.yml.bak
# 1) убираем inhibition-правила и 2) ставим group_by по одному alertname, 3) repeat_interval 1m
sed -i '/^inhibit_rules:/,$d' alertmanager.yml
sed -i "s/group_by: \['alertname', 'instance'\]/group_by: ['alertname']/" alertmanager.yml
sed -i 's/repeat_interval: 4h/repeat_interval: 1m/' alertmanager.yml
amtool check-config alertmanager.yml            # конфиг «валиден» — и это ловушка
sudo cp alertmanager.yml /opt/alertmanager/ && curl -X POST http://127.0.0.1:9093/-/reload
```
Затем на UBU01 погаси `node_exporter` (`sudo systemctl stop node_exporter`) и на WEB01 тоже — получишь шторм.

**Диагностика по порядку:**

1. `journalctl -u alertmanager -n 100 | grep -c 'Notify'` — видим десятки отправок за минуту. Уже понятно, что дело не в Telegram.
2. `diff alertmanager.yml.bak alertmanager.yml` — сразу видно три правки: нет `inhibit_rules`, изменён `group_by`, `repeat_interval: 1m`. Это и есть причина.
3. `amtool config routes test --config.file=alertmanager.yml alertname=InstanceDown severity=critical instance=10.10.20.10:9100` — убеждаемся, что маршрут сам по себе корректен (не в нём дело).
4. `amtool alert query` — видим, что каждый хост даёт свой алерт: группировка по одному `alertname` без `instance` всё равно склеила бы их, но `repeat_interval: 1m` превращает это в спам.
5. Проверяем, не виноват ли Prometheus: `curl -s http://127.0.0.1:9090/api/v1/rules | grep '"name"'` — правила те же, что вчера. Значит, Prometheus ни при чём.

**Решение:**

```bash
sudo cp alertmanager.yml.bak /opt/alertmanager/alertmanager.yml
amtool check-config /opt/alertmanager/alertmanager.yml
curl -X POST http://127.0.0.1:9093/-/reload
# поднимаем экспортёры обратно
ssh adm.student@10.10.10.30 'sudo systemctl start node_exporter'
```
Плюс отдельно, чтобы шторм не повторился: на MON01 в Alertmanager добавить правило-предохранитель и в Prometheus — `for: 2m` у `InstanceDown`, чтобы одиночный флап не стрелял.

**Ответ пользователю:**

> Алексей, разобрался. Причина — правка конфига Alertmanager: были удалены правила подавления и интервал повтора снижен до 1 минуты, поэтому один и тот же сбой по двум хостам повторялся каждую минуту. Конфиг восстановлен из резервной копии, проверен `amtool check-config`, экспортёры на UBU01 и WEB01 снова работают. Сейчас в чат приходят только критичные алерты, warning подавляется. На будущее: любые правки `alertmanager.yml` — только через merge request в GitLab и с прогоном `amtool config routes test`.

**Закрытие заявки:** категория «Мониторинг / оповещения», решение — «откат некорректного конфига, восстановление сервиса», привязка к CHG-номеру, в описании — ссылка на коммит с фиксом.

### Что изменилось на стенде

- **MON01**: установлен Alertmanager (бинарь `/usr/local/bin/alertmanager`), systemd-юнит `alertmanager.service`, конфиг `/opt/alertmanager/alertmanager.yml`, данные в `/opt/alertmanager/data`, UI на `http://10.10.10.40:9093`, кластерный порт 9094.
- **MON01**: два контейнера Telegram-шлюза на `127.0.0.1:8060` и `127.0.0.1:8061`, файл `/opt/monitoring/docker-compose.yml` и `/opt/monitoring/.env` (в Git не попадает).
- **MON01**: Prometheus знает об Alertmanager через `alerting.alertmanagers` в `/etc/prometheus/prometheus.yml`.
- **GIT01**: в проекте `monitoring` появился каталог `alertmanager/` (конфиг, compose, runbooks) — с этого момента политика оповещений версионируется.
- **Telegram**: создан бот, два чата — «Дежурство» (on-call) и «Дежурство+тимлид» (critical).
- Канал оповещений считается рабочим; в следующих уроках (Kubernetes, логи, трейсы) алерты будут уходить в этот же Alertmanager.

### Откат

VMware Workstation → правый клик по ВМ **MON01** → **Snapshot Manager** → выбрать `before-devops-080` → **Restore**. То же для **GIT01**, если коммит с конфигом нужно отменить полностью. После отката: `sudo systemctl status alertmanager` (сервис должен отсутствовать), при необходимости вернуть Prometheus к прежнему конфигу, удалить контейнеры `docker compose down` — если откат делаешь без снапшота. Запись в `stand-log.md` поменять на «изменения откачены снапшотом before-devops-080».

## 6. Ошибки новичков и безопасность

1. **Держать `alertmanager.yml` только на сервере.** → После случайной правки нечего откатывать, история изменений потеряна. → Конфиг в Git, деплой через CI, `amtool check-config` в пайплайне.
2. **Забыть `group_by: ['instance']`.** → Все хосты склеиваются в одно сообщение «InstanceDown» и непонятно, что именно упало. → Всегда включай в `group_by` идентифицирующие метки: `alertname`, `instance`, `cluster`.
3. **`repeat_interval: 1m` «чтобы не пропустить».** → Шторм уведомлений, дежурный перестаёт читать чат — это называется alert fatigue, и это прямая дорога к пропущенной аварии. → 1h–4h для критики, 12–24h для warning.
4. **Заглушить silence'ом «всё» (`--duration=720h`).** → Через две недели никто не помнит, почему мониторинг молчит. → Silence на конкретные метки, с `--author`, `--comment` и коротким TTL.
5. **Считать, что алерт в Telegram = on-call.** → Ночью сообщение в чате не разбудит, у него нет подтверждения и эскалации. → Нужен канал с ack/эскалацией (PagerDuty-класс) либо регламент дежурства и дублирование звонком.
6. **Отправлять в один чат всё: и critical, и info.** → Информационные шумы маскируют аварию. → Отдельные маршруты и отдельные чаты, `info` — в `null` или в ленту.
7. **Не настроить Watchdog.** → Если Prometheus или Alertmanager умерли, тишина выглядит как «всё хорошо». → Правило `alertname=Watchdog` с `expr: vector(1)` и отдельный получатель, который проверяет, что «пульс» есть.
8. **Хранить токен бота в репозитории.** → Утечка = спам от твоего бота и потеря канала. → `.env` с правами 600, `.gitignore`, при утечке — `/revoke` у @BotFather.

**Безопасность.** В лабе мы слушаем Alertmanager на `0.0.0.0:9093` без аутентификации — любой в сети может создать silence и заглушить аварию. В боевой среде Alertmanager ставят за обратный прокси с аутентификацией (basic-auth/OIDC) или в закрытый сегмент, API наружу не публикуют, а webhook-получатели проверяют, что запрос пришёл действительно от Alertmanager. Отдельно: секреты (токен бота, `routing_key` PagerDuty) — в Vault/защищённых переменных CI, не в Git и не в открытом `.env` на общем сервере.

**Про РФ-реалии 2026.** PagerDuty, Opsgenie и Slack могут быть недоступны для оплаты/использования из РФ. Легальные варианты: self-hosted Grafana OnCall [ПРОВЕРИТЬ актуальный статус open-source версии], корпоративный Mattermost/Rocket.Chat с ботом, Telegram как основной канал, плюс ITSM-системы с дежурствами и эскалацией (Naumen Service Desk, SimpleOne [ПРОВЕРИТЬ наличие нужного модуля]). Принцип один: канал должен уметь подтверждение и эскалацию, а не только «отправить текст».

## 7. Вопросы на собеседовании

**1. «Чем Alertmanager отличается от alerting rules в Prometheus?»**
**Ответ:** Prometheus вычисляет правила и хранит состояние алерта (`inactive` / `pending` / `firing`), но не занимается доставкой. Alertmanager — отдельный сервис, который принимает алерты по HTTP, дедуплицирует их, подавляет (inhibition), глушит (silences), группирует и маршрутизирует по дереву `route`, а затем отправляет в receiver'ы с повторами по `repeat_interval`. Он не умеет считать PromQL. Prometheus обнаружит, что Alertmanager недоступен, только по метрике `prometheus_notifications_errors_total`.
**Что хочет услышать интервьюер:** чёткое разделение ответственности «вычислил / доставил» и знание, что за доставку отвечает отдельный компонент.
**Красный флаг:** «Alertmanager считает алерты по правилам» или «это плагин Prometheus».

**2. «Объясните разницу между group_wait, group_interval и repeat_interval».**
**Ответ:** `group_wait` — задержка перед первой отправкой новой группы, чтобы дособрать «родственные» алерты (30s). `group_interval` — минимальный интервал между отправками по уже известной группе, когда в ней появились новые алерты (5m). `repeat_interval` — через сколько повторять неизменившееся уведомление, если алерт всё ещё firing (4h). Все три задаются на уровне route и наследуются дочерними маршрутами.
**Что хочет услышать:** понимание, что эти параметры защищают от шторма и одновременно гарантируют, что алерт не потеряется.
**Красный флаг:** путаница `group_interval` и `repeat_interval` либо «repeat_interval — это retry, если Telegram не ответил» (нет, retry — это внутренние механизмы receiver'а).

**3. «Как работает inhibition и приведите пример?»**
**Ответ:** В `inhibit_rules` задаются `source_matchers` (что должно гореть), `target_matchers` (что при этом не отправлять) и `equal` — список меток, которые должны совпасть у source и target. Классика: `source_matchers: [severity="critical"]`, `target_matchers: [severity=~"warning|info"]`, `equal: ['alertname','instance']` — warning по тому же объекту и тому же алерту не отправляется, пока горит critical. Второй пример: `HostDown` подавляет все алерты с тем же `instance`, потому что если хост лежит, все его сервисы «упали» за компанию. Подавленный алерт виден в UI с пометкой inhibited, но уведомление не уходит.
**Что хочет услышать:** понимание `equal` — самая частая ошибка на практике.
**Красный флаг:** «inhibition удаляет алерт» или «это то же самое, что silence, только автоматически» без уточнения, что silence ручной и по меткам времени.

**4. «Где хранятся silence'ы и что будет при рестарте, если Alertmanager в HA-кластере?»**
**Ответ:** Silence'ы хранятся в `--storage.path` (обычно `data/`), в файле `silences` на диске. При рестарте одиночного инстанса они восстанавливаются оттуда. В HA-кластере инстансы общаются по порту 9094 (gossip на memberlist) и синхронизируют silence'ы и состояние уведомлений — так что при отказе одного узла второй продолжает дежурство, а дублей сообщений не будет. Поэтому HA требует и одинакового конфига, и доступа по 9094 между узлами.
**Что хочет услышать:** понимание, что HA Alertmanager — это про дедупликацию и отказоустойчивость, а не про «два бота в чат».
**Красный флаг:** «silence'ы хранятся в Prometheus» или «в HA придёт два уведомления, это нормально».

**5. «Как вы построите on-call и эскалацию для команды из 5 человек?»**
**Ответ:** Ротация на неделю (первичный дежурный + вторичный), график в Git или в системе on-call (PagerDuty-класс либо self-hosted аналог). Каждый алерт — critical или warning: critical будит сразу и требует подтверждения (ack) за N минут, иначе эскалация на вторичного, затем на тимлида. Warning — только в рабочий чат, без звонка. Обязательные атрибуты алерта: `severity`, `team`, `runbook_url` в аннотациях. Метрики качества — MTTA (время до подтверждения) и MTTR (время до восстановления), их разбирают на постмортеме. Алерт без runbook'а и без действия не заводим — иначе alert fatigue. Для России: если PagerDuty недоступен, тот же процесс на self-hosted системе on-call плюс Telegram как транспорт.
**Что хочет услышать:** процесс, а не инструмент: ротация, ack, эскалация, runbook, метрики, разбор инцидентов.
**Красный флаг:** «заведу чат в Telegram, все туда пишут» — без эскалации и подтверждения.

## 8. Источники (официальная документация)

- **prometheus.io** — раздел *Alerting → Alertmanager*: обзор, `route`, `receivers`, `inhibit_rules`, примеры конфигурации. Поиск: `prometheus alertmanager configuration`.
- **github.com/prometheus/alertmanager** — README и `docs/`: `amtool` (проверка конфига, тест маршрутов, silence'ы), HA-кластеризация. Поиск: `prometheus alertmanager amtool silence add`.
- **prometheus.io** — *Configuration → alerting* (секция `alerting: alertmanagers:` в `prometheus.yml`). Поиск: `prometheus config alerting alertmanagers`.
- **github.com/timonwong/prometheus-webhook-dialer** [ПРОВЕРИТЬ актуальность репозитория и флаги] — шлюз Alertmanager → Telegram. Поиск: `prometheus-webhook-dialer telegram alertmanager`.
- **core.telegram.org** — *Bot API*: `sendMessage`, `getUpdates`, создание бота через @BotFather. Поиск: `telegram bot api sendMessage`.
- **grafana.com/docs** — раздел *Grafana OnCall / IRM* как self-hosted альтернатива PagerDuty [ПРОВЕРИТЬ текущий статус open-source версии]. Поиск: `grafana oncall self-hosted escalation`.
- **pagerduty.com** — *Developer docs → Events API v2* (совместимость с `pagerduty_configs` в Alertmanager) [ПРОВЕРИТЬ доступность сервиса и оплаты из РФ]. Поиск: `pagerduty events api v2 alertmanager`.
