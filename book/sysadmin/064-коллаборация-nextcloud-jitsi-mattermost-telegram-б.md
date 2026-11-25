# Коллаборация: Nextcloud, Jitsi, Mattermost, Telegram-боты, российские аналоги Teams/Slack

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку от Петровой (HR):

> **Пользователь:** Петрова Мария, HR
> **ВМ:** WIN10-CL02
> **Что написала:** «Мы согласовываем приказы с бухгалтерией и складом. Сейчас я отправляю файл по почте, Иванов правит, присылает обратно, я путаюсь в версиях `prikaz_final_final_2.docx`. И созвониться не можем: по телефону не слышно, а собрать всех в переговорке физически некогда — склад в другом конце здания. Можно сделать нормально: общая папка с версиями и видеозвонки с компьютера?»
> **Срочность:** средняя

Почта (урок 62) — это доставка сообщений, а не совместная работа. Как только в компании больше 20 человек, появляются три вещи, которых почта не умеет: **общий файл с версиями и правами**, **чат по рабочим вопросам** и **видеовстречи без разъездов**. Плюс четвёртая, самая важная для сисадмина: **интеграции и боты** — именно через них сервисы начинают сами создавать заявки, чинить, напоминать.

После этой главы вы будете уметь: развернуть Nextcloud (файлы + календарь + шаринг), Mattermost (корпоративный мессенджер) и Jitsi Meet (видеоконференции) на своём стенде, объяснить, почему Jitsi «падает на третьем участнике», написать Telegram-бота и посадить его под systemd, а также аргументированно выбрать между self-hosted open source и российскими商用 решениями из реестра — с оглядкой на 152-ФЗ.

## 2. Теория «под капотом»

Коллаборация — это четыре разных типа трафика, которые часто путают в одну кучу:

| Слой | Что делает | Протоколы и порты | Наш сервис |
|------|-----------|-------------------|------------|
| Файлы и календари | синхронизация, версии, шаринг | HTTPS 443, WebDAV, CalDAV/CardDAV | Nextcloud |
| Мессенджер | каналы, история, уведомления | HTTPS 443/8065, WebSocket (WS) поверх 443 | Mattermost |
| ВКС | сигналинг + медиа | HTTPS 443, XMPP (внутри), **UDP 10000** | Jitsi Meet |
| Боты и интеграции | события наружу/внутрь | HTTPS (webhook) или long polling | Telegram Bot API |

**Nextcloud** — PHP-приложение под Apache/nginx + СУБД (MariaDB/PostgreSQL) + Redis для кэша и блокировок файлов. Файлы лежат на диске, метаданные (версии, шары, права) — в БД. Клиенты (десктоп, мобильные) синхронизируются по **WebDAV**; логин в AD — через LDAP-приложение.

**Mattermost** — Go-бинарник + PostgreSQL. Браузер держит постоянное соединение через **WebSocket**, поэтому сообщения приходят мгновенно. Интеграции — **incoming/outgoing webhooks**, slash-команды и боты. Управление из CLI — `mmctl`.

**Jitsi Meet** — это не один процесс, а четыре контейнера:

```text
                    Браузер (WIN11-CL01)         Браузер (WIN10-CL02)
                            |  HTTPS 443 (сигналинг, SDP)   |
                            +--------------+----------------+
                                           |
                                  +--------v---------+
                                  | web (nginx)      |  раздаёт JS-клиент, проксирует
                                  +--------+---------+
                                           |
                            +--------------+---------------+
                            |                              |
                    +-------v------+               +-------v------+
                    | prosody      |               | jicofo       |  управляет конференцией
                    | XMPP, :5280  |<------------->| (focus)      |
                    +--------------+               +-------+------+
                                                           |
                                                   +-------v------+
                                                   | jvb          |  Jitsi Videobridge:
                                                   | UDP 10000    |<=== медиа (видео/звук) ===
                                                   +--------------+     идёт СЮДА, не через 443
```

Ключевая мысль: **сигналинг идёт по TCP 443, а медиа — по UDP 10000 в JVB**. Если UDP закрыт, Jitsi формально «работает», но на 3+ участниках начинается ад: мосты не находят друг друга, картинка рассыпается.

**Боты.** Telegram Bot API — обычный HTTPS JSON API на `api.telegram.org`. Два режима: **long polling** (`getUpdates` — бот сам опрашивает сервер, работает из-за NAT без белого IP) и **webhook** (`setWebhook` — Telegram сам стучится к вам, требует публичный HTTPS). В корпоративной сети почти всегда начинают с polling.

**Идентичность.** Правильный коллаборационный сервис не заводит своих паролей: Nextcloud — LDAP к DC01 (`ldap://10.10.10.10:389`, в проде — LDAPS 636), Mattermost — SAML/OAuth или LDAP, Jitsi — JWT-аутентификация через внешний провайдер. Иначе получаем второй зоопарк учёток.

**Российские аналоги Teams/Slack** (сравнивать по: реестр отечественного ПО, on-prem, интеграция с AD, ГОСТ-TLS/КриптоПро, цена): VK Teams (ранее Myteam), eXpress, Compass (Компас), TrueConf Server, IVA MCU / IVA Connect, VideoMost, Яндекс 360 для бизнеса (Диск + Телемост), МойОфис. Nextcloud/Mattermost/Jitsi — open source, их ставят «как есть», но в реестре их нет: для госструктур и КИИ это блокер. Наличие конкретной позиции в реестре **проверяйте на reestr.digital.gov.ru перед закупкой** `[ПРОВЕРИТЬ]`.

## 3. Аналогия из жизни

Представьте офис компании «Вектор». **Nextcloud** — это общий сейф с папками: у каждой папки свой замок (права), и сейф помнит каждую предыдущую редакцию документа. **Mattermost** — доска объявлений в коридоре, разбитая на секции (каналы): на «общую» пишут все, на «бухгалтерию» — только бухгалтерия. **Jitsi** — переговорная комната: вы открываете дверь по ссылке, а звук идёт не через дверь, а по отдельной трубе вентиляции (UDP 10000) — если трубу заткнули, люди в комнате есть, а слышно никого. **Бот** — курьер, который по расписанию бегает между сейфом и доской и переносит записки.

Аналогия ломается на том, что в реальном Jitsi «вентиляция» может быть не одна, а ещё и браузеры могут договориться напрямую (P2P) на двоих — и тогда третий участник внезапно оказывается один в комнате.

## 4. Практика и команды

**1. DNS-имена для сервисов (DC01, PowerShell).** Браузеру нужны имена, а не IP — иначе Nextcloud ругается на `trusted_domains`, а Jitsi не поднимет веб.

```powershell
Add-DnsServerResourceRecordA -Name collab -ZoneName corp.vektor.lab -IPv4Address 10.10.10.80 -CreatePtr
Add-DnsServerResourceRecordA -Name meet   -ZoneName corp.vektor.lab -IPv4Address 10.10.10.81
Resolve-DnsName collab.corp.vektor.lab
```

Вывод: `Name: collab.corp.vektor.lab, Type: A, IPAddress: 10.10.10.80`. Смотрим, что запись есть и указывает туда, куда думали.

**2. Проверка с клиента (WIN11-CL01, PowerShell).**

```powershell
Resolve-DnsName meet.corp.vektor.lab
Test-NetConnection collab.corp.vektor.lab -Port 8080
Test-NetConnection meet.corp.vektor.lab   -Port 443
```

`TcpTestSucceeded : True` — порт открыт. `-Port` проверяет только TCP; для UDP такого параметра нет, UDP проверяем на сервере через `ss` и `tcpdump`.

**3. Установка Docker (COLLAB01/JITSI01, bash).** Официальный репозиторий, не «скрипт из интернета».

```bash
sudo apt update && sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
| sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo docker run --rm hello-world
```

**4. Поднять Nextcloud (COLLAB01).**

```bash
mkdir -p /opt/nextcloud && cd /opt/nextcloud
cat > .env <<'EOF'
DB_ROOT_PASSWORD=Lab#2026-vektor
DB_PASSWORD=Lab#2026-vektor
EOF
chmod 600 .env
sudo docker compose up -d && sudo docker compose ps
```

`docker compose ps` — все сервисы должны быть `running (healthy)`; `db` часто стартует 20–40 секунд.

**5. `occ` — CLI-сердце Nextcloud.**

```bash
sudo docker compose exec -u www-data app php occ status
sudo docker compose exec -u www-data app php occ config:system:set trusted_domains 1 --value=collab.corp.vektor.lab
OC_PASS='Lab#2026-vektor' sudo docker compose exec -u www-data -e OC_PASS app php occ user:add --password-from-env ivanov
sudo docker compose exec -u www-data app php occ group:add Buh
sudo docker compose exec -u www-data app php occ group:adduser Buh ivanov
sudo docker compose exec -u www-data app php occ files:scan --all
```

`occ status` → `installed: true`, `versionstring: ...`, `maintenance: false`. Если `maintenance: true` — сервис в режиме обслуживания, вход закрыт. `files:scan` нужен, если файлы положили на диск руками мимо веб-интерфейса.

**6. Incoming webhook Mattermost (bash).**

```bash
curl -i -X POST -H 'Content-Type: application/json' \
  -d '{"text":"**Вектор:** бэкап FS01 завершён успешно"}' \
  http://collab.corp.vektor.lab:8065/hooks/abc123...
```

Ответ `ok` — сообщение ушло в канал. Ссылку webhook берём в System Console → Integrations → Incoming Webhooks. Это база для всех алертов: Zabbix/MON01 умеет дёргать такой URL.

**7. `mmctl` — управление Mattermost из консоли.**

```bash
sudo docker exec -it mattermost mmctl --local system status
sudo docker exec -it mattermost mmctl --local team create --name vektor --display-name "Вектор"
sudo docker exec -it mattermost mmctl --local user create --email ivanov@corp.vektor.lab --username ivanov --password 'Lab#2026-vektor'
```

`system status` → `Status: OK`, версия, число пользователей. Наличие `mmctl --local` в контейнере уточните по документации образа `[ПРОВЕРИТЬ]`; альтернатива — веб-интерфейс.

**8. Развернуть Jitsi (JITSI01, bash).**

```bash
git clone https://github.com/jitsi/docker-jitsi-meet.git /opt/jitsi && cd /opt/jitsi
cp env.example .env
./gen-passwords.sh
# в .env: PUBLIC_URL=https://meet.corp.vektor.lab, HTTP_PORT=80, HTTPS_PORT=443, TZ=Europe/Moscow
sudo docker compose up -d
sudo docker compose ps
sudo docker logs -f jvb 2>&1 | grep -iE "ice|advertise|candidate"
```

`gen-passwords.sh` генерирует пароли между контейнерами. В логах jvb ищем строку про `advertise` — для стенда в одной подсети её можно не задавать; если JVB «за NAT», нужен `JVB_ADVERTISE_IPS` `[ПРОВЕРИТЬ]` по документации.

**9. Проверить, что UDP 10000 реально слушается.**

```bash
sudo ss -lunp | grep 10000
sudo ufw allow 10000/udp && sudo ufw status numbered
sudo tcpdump -ni any udp port 10000 -c 20
```

`ss` → `UNCONN 0 0 0.0.0.0:10000 ... users:(("java",pid=...))`. `tcpdump` при подключении третьего участника должен показать пакеты с обеих сторон.

**10. Telegram-бот: ручная проверка API (bash).**

```bash
export BOT_TOKEN='123456:AA...'
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe"
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates" | head -c 500
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d chat_id=123456789 -d text='Проверка связи из Вектора'
```

`getMe` → `{"ok":true,"result":{"is_bot":true,"username":"vektor_lab_bot"}}`. `getUpdates` вернёт `{"ok":true,"result":[]}`, пока никто не написал боту; после `/start` появятся объекты `message`. `sendMessage` → `"ok":true`.

**11. Бот как служба (COLLAB01, bash).**

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now vektor-bot
systemctl status vektor-bot --no-pager
journalctl -u vektor-bot -f -n 30
```

Ищем `Active: active (running)` и отсутствие `Traceback` в журнале. `Restart=always` в unit-файле спасает от падений на сетевых ошибках.

**12. Логи контейнеров и диск.**

```bash
sudo docker compose logs --tail=50 --timestamps app
df -h /var/lib/docker && docker system df
```

`docker system df` показывает, сколько занимают образы/контейнеры/кэш. Nextcloud и Mattermost растут файлами, а не образами — следите за разделом `/var`, а не только за Docker.

## 5. Лабораторная работа

### Предусловия

Стенд L2 готов: DC01 (AD DS, DNS, DHCP), FS01, WIN11-CL01 (ivanov), WIN10-CL02 (petrova), UBU01, ASTRA01, GW01, WEB01, MON01. Есть PVE01 (10.10.10.70) с вложенной виртуализацией.

На этом уроке **явно добавляются две новые ВМ** (на PVE01 или, если PVE01 ещё не поднят, — напрямую в VMware Workstation в сети VMnet8):

| Имя | ОС | Адрес | Роль |
|-----|----|-------|------|
| COLLAB01 | Ubuntu Server 24.04 LTS, 2 vCPU / 4 ГБ / 40 ГБ | 10.10.10.80 | Nextcloud + Mattermost (Docker) |
| JITSI01 | Ubuntu Server 24.04 LTS, 2 vCPU / 4 ГБ / 30 ГБ | 10.10.10.81 | Jitsi Meet (Docker) |

Почему 10.10.10.80/81: пул DHCP на DC01 — 10.10.10.100–150, а .80/.81 свободны и не пересекаются с существующими адресами. Если на домашнем ПК мало RAM — поднимайте сервисы по очереди, а не одновременно.

Нужны: учётки `ivanov`, `petrova`, `sidorov`, группы `GG-Buh`/`GG-HR`/`GG-Sklad`; интернет на ВМ (для `apt` и образов Docker). Telegram-бот создаётся **на вашем личном аккаунте** через @BotFather — это учебный бот, реальные данные компании в него не пишем.

### Снапшот

Перед началом — снапшот `before-sysadmin-064`:

- в VMware Workstation: выделить COLLAB01 и JITSI01 → **VM → Snapshot → Take Snapshot**, имя `before-sysadmin-064` (если ВМ ещё не существуют, снапшотятся хостовые `UBU01`, `DC01`, `WIN10-CL02`, `WIN11-CL01` — изменения в DNS и браузерных профилях);
- в Proxmox VE, если ВМ на нём: **Datacenter → VM → Snapshots → Take Snapshot**, имя `before-sysadmin-064`.

### Шаги

1. **Создать ВМ COLLAB01 и JITSI01.** На PVE01: **Create VM**, ISO `ubuntu-24.04-live-server-amd64.iso`, при установке создать пользователя `student`, галочку OpenSSH — поставить. Ожидаемый результат: обе ВМ грузятся, `ip -br a` показывает интерфейс.
2. **Настроить статические адреса (netplan, на каждой ВМ).** Имя интерфейса уточнить через `ip -br a` (`ens18`/`ens33`).

```yaml
network:
  version: 2
  ethernets:
    ens18:
      dhcp4: false
      addresses: [10.10.10.80/24]
      routes:
        - to: default
          via: 10.10.10.2
      nameservers:
        addresses: [10.10.10.10]
```

```bash
sudo netplan apply && ip -br a && ping -c2 10.10.10.10
```

Ожидаемо: адрес 10.10.10.80/81, шлюз 10.10.10.2, пинг до DC01 идёт. Для JITSI01 — адрес 10.10.10.81.
3. **Добавить DNS-записи на DC01** командой из п.4.1. С WIN10-CL02: `Resolve-DnsName collab.corp.vektor.lab` → 10.10.10.80.
4. **Поставить Docker на обе ВМ** командами из п.4.3. Ожидаемо: `hello-world` отработал, `docker compose version` печатает версию v2.x.
5. **Nextcloud.** На COLLAB01 создать `/opt/nextcloud/docker-compose.yml` (сервисы `db` = `mariadb:11.4`, `redis` = `redis:7-alpine`, `app` = `nextcloud:apache` с портом `8080:80`, томом `nc_data:/var/www/html` и переменными `MYSQL_HOST=db`, `MYSQL_DATABASE=nextcloud`, `MYSQL_USER=nextcloud`, `MYSQL_PASSWORD=${DB_PASSWORD}`, `REDIS_HOST=redis`, `NEXTCLOUD_TRUSTED_DOMAINS=collab.corp.vektor.lab` `[ПРОВЕРИТЬ]` по README официального образа), затем `sudo docker compose up -d`. Ожидаемо: через 30–60 с `http://collab.corp.vektor.lab:8080` открывает мастер установки.
6. **Пользователи и группа в Nextcloud.** Пройти мастер (создать админа `adm.student`), затем командами п.4.5 добавить `ivanov`, `petrova`, `sidorov`, группу `Buh`, включить `ivanov`. В веб-интерфейсе создать папку `Buh` и открыть доступ группе `Buh` (Файлы → папка → Share). Проверить: `ivanov` видит папку, `petrova` — нет.
7. **Mattermost.** На COLLAB01 поднять Mattermost team edition в Docker (порт 8065, PostgreSQL как отдельный сервис — берите официальный compose из документации `[ПРОВЕРИТЬ]`), открыть `http://collab.corp.vektor.lab:8065`, первый пользователь становится админом. Создать команду `Vektor`, каналы `obshchiy` и `buhgalteriya`, пригласить `ivanov` и `petrova`. Затем создать Incoming Webhook и отправить сообщение curl-ом из п.4.6 — оно должно появиться в канале.
8. **Jitsi.** На JITSI01 выполнить п.4.8: клонировать репозиторий, `cp env.example .env`, `./gen-passwords.sh`, в `.env` выставить `PUBLIC_URL=https://meet.corp.vektor.lab`, `HTTP_PORT=80`, `HTTPS_PORT=443`, `TZ=Europe/Moscow`, затем `sudo docker compose up -d`. Открыть `https://meet.corp.vektor.lab` — браузер ругнётся на самоподписанный сертификат (это нормально для лабы, см. урок 57 про внутренний УЦ), после «Всё равно перейти» появится стартовый экран Jitsi.
9. **Провести тестовую встречу.** На WIN11-CL01 и WIN10-CL02 открыть одну и ту же комнату `https://meet.corp.vektor.lab/vektor-test`, дать доступ к камере/микрофону. Затем на ASTRA01 (или в третьем браузере) зайти третьим. Ожидаемо: все трое видят друг друга, на JITSI01 растёт трафик `tcpdump -ni any udp port 10000`.
10. **Telegram-бот.** У @BotFather создать бота `vektor_lab_bot`, получить токен. На COLLAB01 в `/opt/vektor-bot/` положить `bot.py` (long polling: `getUpdates` в цикле, на `/start` отвечать приветствием, на `/status` — отдавать `docker compose ps`), создать отдельного пользователя `vektor-bot`, файл `bot.env` с токеном (`chmod 600`), unit-файл из п.4.11 и `systemctl enable --now vektor-bot`. Отправить боту `/start` — должен ответить. Опционально: по команде `/alert` бот дёргает Mattermost-webhook.
11. **Записать изменения в `stand-log.md`**: новые ВМ, адреса, DNS-записи, контейнеры, порты, бот.

### Проверка

- [ ] `ip -br a` на COLLAB01/JITSI01 показывает 10.10.10.80/81
- [ ] С WIN10-CL02 `Resolve-DnsName meet.corp.vektor.lab` возвращает 10.10.10.81
- [ ] `sudo docker compose ps` на обеих ВМ — все сервисы `running`
- [ ] `occ status` → `installed: true`, `maintenance: false`
- [ ] `ivanov` видит папку `Buh` в Nextcloud, `petrova` — нет
- [ ] Сообщение из curl появилось в канале Mattermost
- [ ] `sudo ss -lunp | grep 10000` показывает слушающий процесс jvb
- [ ] Конференция на 3 участника идёт со звуком и видео, `tcpdump` видит UDP
- [ ] `systemctl status vektor-bot` → `active (running)`, бот отвечает на `/start`
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

> **Пользователь:** Сидоров Алексей, склад (работает с WIN11-CL01)
> **ВМ:** WIN11-CL01
> **Что написал:** «Собираемся втроём на `meet.corp.vektor.lab/vektor-test`. Вдвоём с Петровой всё нормально. Как подключается Иванов — у всех пропадает картинка, звук рвётся, у меня всплывает “You are the only one in the meeting”. Вчера работало!»
> **Срочность:** высокая

**Как воспроизвести поломку (на JITSI01):**

```bash
sudo ufw deny 10000/udp
sudo ufw status numbered
```

**Диагностика по шагам — от простого к сложному:**

1. Воспроизвести: зайти в комнату двумя браузерами (работает — это P2P), затем третьим (ломается). Зафиксировать симптом.
2. Проверить, что JVB вообще слушает: `sudo ss -lunp | grep 10000`. Если пусто — проблема в контейнере (`docker compose ps`, `docker logs jvb`), а не в фаерволе.
3. Посмотреть логи моста: `sudo docker logs --tail=100 jvb | grep -iE "error|candidate|ice"`. Типичный признак — сообщения об отсутствии ICE-кандидатов или таймауты.
4. Проверить фаервол: `sudo ufw status numbered` → в списке есть `DENY IN 10000/udp` (в нашем случае мы её сами создали).
5. Проверить трафик: `sudo tcpdump -ni any udp port 10000 -c 20` и попросить третьего подключиться. Пакетов нет → медиа блокируется.
6. Дополнительно исключить маршрутизацию: `ping meet.corp.vektor.lab` и `Test-NetConnection meet.corp.vektor.lab -Port 443` с клиента — TCP работает, значит проблема строго в UDP.

**Решение:**

```bash
sudo ufw delete deny 10000/udp
sudo ufw allow 10000/udp
sudo ufw status numbered
sudo tcpdump -ni any udp port 10000 -c 20
```

**Ответ пользователю:** «Алексей, проблема была на стороне сервера конференций: после вчерашних работ закрылся UDP-порт, через который идёт видео и звук. Вдвоём вы работали напрямую, поэтому и казалось, что всё в порядке. Порт открыт, проверил конференцию на трёх участниках — звук и видео в норме. Если повторится — сразу пишите, посмотрим логи моста.»

**Закрытие заявки:** категория «Коллаборация / ВКС», решение «открыт UDP 10000 в ufw на JITSI01», в постмортеме зафиксировать, что правило фаервола не попало в конфигурацию после перезагрузки — добавить в Ansible/чек-лист.

### Что изменилось на стенде

- Добавлены ВМ: **COLLAB01** (10.10.10.80), **JITSI01** (10.10.10.81), обе Ubuntu Server 24.04, статическая адресация, DNS = 10.10.10.10.
- На DC01 в зоне `corp.vektor.lab` появились A-записи `collab` и `meet`.
- На COLLAB01 работает Docker: Nextcloud (`:8080`, MariaDB, Redis), Mattermost (`:8065`, PostgreSQL), служба `vektor-bot`.
- На COLLAB01 в Nextcloud заведены пользователи `ivanov`, `petrova`, `sidorov`, группа `Buh`, папка `Buh` расшарена на группу.
- В Mattermost: команда `Vektor`, каналы `obshchiy`, `buhgalteriya`, Incoming Webhook.
- На JITSI01 работает Jitsi Meet (`:80`/`:443`, UDP 10000), комната `vektor-test`, самоподписанный сертификат.
- Создан учебный Telegram-бот `vektor_lab_bot`, токен в `/opt/vektor-bot/bot.env` (600).

### Откат

1. Docker-стек: `cd /opt/nextcloud && sudo docker compose down -v` (ключ `-v` удаляет тома — данные Nextcloud пропадут, это ожидаемо).
2. DNS: на DC01 `Remove-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name collab -RRType A -Force` (аналогично `meet`).
3. Полный откат: в VMware/Proxmox выбрать снапшот `before-sysadmin-064` → **Revert**. ВМ, созданные после снапшота, удалить через **Delete VM** (не «Remove from inventory»).
4. Записать факт откатa в `stand-log.md`, чтобы следующая глава не искала несуществующие сервисы.

## 6. Ошибки новичков и безопасность

1. **Ошибка:** открыть Nextcloud/Mattermost по IP и удивиться «Access through untrusted domain». **К чему:** сервис недоступен, пользователи в панике. **Правильно:** сразу заводить DNS-имя и `trusted_domains`, а IP использовать только для отладки.
2. **Ошибка:** оставить Jitsi без аутентификации «на пять минут» и забыть. **К чему:** любой, кто узнал ссылку, заходит в вашу комнату (и в лобби-режиме можно висеть и слушать). **Правильно:** `ENABLE_AUTH` + JWT, `ENABLE_LOBBY`, ограничение по домену.
3. **Ошибка:** хранить `bot.env`, `.env` Jitsi и webhook-URL в git. **К чему:** токен утекает, бот начинает рассылать спам от имени компании. **Правильно:** `.gitignore`, `chmod 600`, отдельный сервисный аккаунт, ротация токена через @BotFather (`/revoke`).
4. **Ошибка:** гонять корпоративную переписку и документы через Telegram-бота. **К чему:** данные россиян уходят за границу — прямой конфликт с 152-ФЗ; для КИИ (187-ФЗ) это вообще недопустимо. **Правильно:** Telegram — только уведомления без персональных данных, чувствительное — в self-hosted Mattermost.
5. **Ошибка:** бэкапить Nextcloud копированием каталога при работающей БД. **К чему:** «битые» файлы и рассинхрон метаданных, восстановление невозможно. **Правильно:** `occ maintenance:mode --on` → дамп БД → копия `data` → `maintenance:mode --off` (подробнее — урок 61).
6. **Ошибка:** обновлять Nextcloud через несколько мажорных версий сразу. **К чему:** миграция БД падает, откат только из бэкапа. **Правильно:** только пошагово (major → следующий major) и сначала на тестовой копии.
7. **Ошибка:** дать боту права администратора Mattermost «чтобы точно работало». **К чему:** компрометация токена = компрометация всего мессенджера. **Правильно:** бот — обычный пользователь + webhook в один канал.
8. **Ошибка:** считать, что «TCP 443 открыт — значит Jitsi работает». **К чему:** звонки на 3+ участниках разваливаются, а мониторинг зелёный. **Правильно:** мониторить UDP 10000 отдельно (урок про Zabbix на MON01) и проверять синтетической конференцией.

**Безопасность.** В боевой среде нельзя: публиковать `:8065` и `:8080` напрямую в интернет без TLS и reverse proxy (это отдельная тема — урок 67); использовать самоподписанные сертификаты для внешних пользователей; ставить форк/Jitsi «из случайного образа» без проверки источника; хранить пароли БД в `docker-compose.yml` в открытом виде (используйте `.env` + секреты, а в идеале — Vault из урока 56). На стенде пароль `Lab#2026-vektor` допустим только потому, что стенд изолирован.

## 7. Вопросы на собеседовании

**1. «Компания хочет корпоративный мессенджер. Что выберете — self-hosted Mattermost или Telegram, и почему?»**

Ответ: сначала требования. Если есть персональные данные россиян и/или объект КИИ — Telegram отпадает: данные хранятся за пределами РФ, нет контроля над инфраструктурой, нет DLP. Mattermost team edition self-hosted даёт контроль, LDAP/AD-интеграцию, webhooks, но требует администрирования (обновления, бэкапы, HA). Telegram уместен как канал уведомлений без чувствительных данных. Промежуточный вариант — российские решения (VK Teams, eXpress, Compass): on-prem, в реестре `[ПРОВЕРИТЬ]`, поддержка ГОСТ-TLS, но платно.
*Что хочет услышать:* выбор из требований (152-ФЗ, КИИ, ИБ), а не «что мне нравится», плюс понимание стоимости владения.
*Красный флаг:* «Telegram удобнее, возьмём его» без упоминания данных и регуляторики.

**2. «Почему Jitsi нормально работает на двоих, а на троих разваливается?»**

Ответ: на двоих браузеры могут установить P2P-соединение напрямую. С третьим участником включается JVB — медиа идёт через мост по UDP 10000. Если UDP закрыт фаерволом, неверно указан `JVB_ADVERTISE_IPS` или JVB за NAT, ICE-кандидаты не находятся, участники «не видят» друг друга. Диагностика: `ss -lunp | grep 10000`, логи jvb, `tcpdump udp port 10000`, проверка ufw/security groups.
*Что хочет услышать:* понимание разделения сигналинга (443/TCP) и медиа (10000/UDP), знание про ICE.
*Красный флаг:* «надо просто добавить участников в настройках» или «перезагрузить сервер».

**3. «Как организовать единый вход в Nextcloud и Mattermost через Active Directory?»**

Ответ: создать сервисную учётку в AD с минимальными правами (только чтение каталога), настроить LDAP-коннектор: хост `10.10.10.10`, порт 389 (в проде — LDAPS 636 либо StartTLS), base DN `DC=corp,DC=vektor,DC=lab`, bind DN сервисной учётки, фильтр пользователей и групп (`memberOf=CN=GG-IT,...`). Для Mattermost — SAML/LDAP. Пароль сервисной учётки — в хранилище секретов, ротация по расписанию (урок 56). Обязательно проверить маппинг групп: `GG-Buh` → роль/команда.
*Что хочет услышать:* сервисная учётка, LDAPS, маппинг групп, ротация пароля.
*Красный флаг:* «заведём админа домена в bind DN» или «отключим TLS, внутри же сеть».

**4. «Какие российские аналоги Teams/Slack рассмотрите и по каким критериям сравните?»**

Ответ: VK Teams, eXpress, Compass, TrueConf (ВКС), IVA MCU/IVA Connect, VideoMost, Яндекс 360 (Диск + Телемост), МойОфис. Критерии: наличие в реестре отечественного ПО, on-premise развёртывание, интеграция с AD/LDAP, поддержка ГОСТ-TLS и КриптоПро, ОС-клиенты (включая Astra Linux и РЕД ОС), масштабирование и HA, стоимость лицензий и поддержки, план миграции данных (экспорт истории, файлов), требования ФСТЭК к ИБ-функциям (DLP, аудит).
*Что хочет услышать:* системный список критериев и упоминание реестра/импортозамещения (урок 59).
*Красный флаг:* «всё отечественное плохое» или «поставим первое из поиска».

**5. «Как обновлять коллаборационный сервис без длительного простоя и с возможностью отката?»**

Ответ: отдельный тестовый инстанс на копии данных; снапшот ВМ/БД; чтение release notes (для Nextcloud — обязательный шаг по мажорным версиям, для Mattermost — ESR-релизы `[ПРОВЕРИТЬ]`); `occ maintenance:mode --on`; бэкап БД + `data`; обновление; проверка по чек-листу (вход, шаринг, webhook); вывод из maintenance. Откат — revert снапшота или восстановление БД. Окно обновления — вне рабочих часов, пользователей предупредить за день.
*Что хочет услышать:* «бэкап до, проверка после, откат возможен», упоминание maintenance mode.
*Красный флаг:* «обновлю вечером, авось прокатит».

## 8. Источники (официальная документация)

- **docs.nextcloud.com** — раздел «Nextcloud Administrator Manual», подразделы «Installing» и «OCC command reference». Поиск: `nextcloud occ command reference`, `nextcloud docker installation`.
- **hub.docker.com** — страница официального образа Nextcloud (переменные `MYSQL_*`, `NEXTCLOUD_TRUSTED_DOMAINS`) `[ПРОВЕРИТЬ актуальные имена переменных перед лабой]`.
- **docs.mattermost.com** — разделы «Mattermost documentation», «Install Mattermost server», «mmctl Command Line Tool», «Incoming webhooks». Поиск: `mattermost docker compose install`, `mmctl local`.
- **jitsi.github.io/handbook** — «Jitsi Meet Handbook», раздел «Docker» (docker-jitsi-meet quick start, `.env`, `gen-passwords.sh`, `JVB_ADVERTISE_IPS`). Поиск: `jitsi docker quick start`, `jitsi jvb port 10000`.
- **core.telegram.org/bots/api** — «Telegram Bot API» (методы `getMe`, `getUpdates`, `sendMessage`, `setWebhook`). Поиск: `telegram bot api getupdates long polling`.
- **docs.docker.com** — «Install Docker Engine on Ubuntu», «Docker Compose». Поиск: `install docker engine ubuntu apt repository`.
- **learn.microsoft.com** — «Add-DnsServerResourceRecordA», «Resolve-DnsName», «Test-NetConnection». Поиск: `Add-DnsServerResourceRecordA PowerShell`.
- **reestr.digital.gov.ru** — реестр российского ПО (проверка наличия VK Teams, eXpress, Compass, TrueConf, IVA, VideoMost перед закупкой) `[ПРОВЕРИТЬ актуальность домена реестра]`.
- **152-ФЗ, 187-ФЗ** — тексты на **pravo.gov.ru** (раздел «Законодательство России»). Поиск: `152-ФЗ персональные данные локализация`.
