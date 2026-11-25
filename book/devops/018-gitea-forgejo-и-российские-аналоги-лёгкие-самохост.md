# Gitea/Forgejo и российские аналоги: лёгкие самохостинговые Git-решения

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку: «**Пользователь:** ivanov (Иван Иванов, бухгалтерия). **ВМ:** UBU01. **Что написал:** „Иван, привет. Мы с девочками из отчётов теперь пишем скрипты на Python, чтобы не сводить Excel руками. Нам нужен свой Git с веб-интерфейсом, желательно на 5–6 человек. Заходить на GIT01, где твой GitLab, нам страшно, да и права туда давать — ты сам говорил, что не надо. Есть что-то маленькое?“ **Срочность:** средняя».

И это классика. GitLab на GIT01 (урок 17) — отличный инструмент, но он требует 4–8 ГБ RAM и нормально живёт только когда его кто-то обслуживает. А «маленький Git для пяти человек» — это другая задача: один бинарник, SQLite, бэкап в zip, поднялся за полчаса. Именно поэтому в 2020-х массово распространились Gitea и его форк Forgejo: они закрывают 90 % потребностей небольшой команды, стоят копейки по ресурсам и управляются одним админом.

После этой главы ты будешь уметь: поставить Gitea/Forgejo из бинарника с systemd, настроить `app.ini` и секреты, создать админа и репозиторий через CLI и веб, настроить клонирование по SSH на нестандартном порту, сделать и проверить бэкап, и — главное — аргументированно выбирать между «лёгким» решением, GitLab и российскими платформами (GitFlic, GitVerse, SourceCraft), когда заказчик требует импортозамещение и реестр отечественного ПО.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Gitea написан на Go и с 2016 года развился из проекта Gogs. В 2022 году сообщество (кооператив Codeberg e.V.) сделало форк **Forgejo**, потому что не устраивала модель управления коммерческой Gitea Ltd. Код почти идентичен, отличается управление, лицензия (Forgejo — GPLv3, Gitea — MIT) и часть фич. Практически всё, что ниже, одинаково для обоих; различаются имена бинарника (`forgejo` / `gitea`), каталоги и названия раннеров CI.

Ключевая идея — **«один бинарник + база + каталог с репозиториями»**:

```text
        клиент (git / браузер)
                │
   ┌────────────┴─────────────┐
   │ HTTPS :3000              │ SSH :2222
   ▼                          ▼
┌──────────────────────────────────────────┐
│  forgejo (один процесс, Go)              │
│  ├─ HTTP-сервер: веб-UI + REST API       │
│  ├─ встроенный SSH-сервер (golang ssh)   │
│  ├─ внутренний cron (зеркала, очистки)   │
│  └─ вызывает системный `git` для операций│
└───────┬───────────────────────┬──────────┘
        │ метаданные            │ «голые» репозитории
        ▼                       ▼
  SQLite/PostgreSQL      /var/lib/forgejo/data/repositories/
  (users, issues, PR,     <owner>/<repo>.git  ← обычные bare-репы
   tokens, hooks)              │
                               ▼
                        LFS-объекты, аватары, логи
```

Что здесь важно понять:

- **Репозитории — это обычные bare-репозитории Git на диске.** Ты можешь зайти туда `git`-ом напрямую, скопировать каталог, отдать `git clone`. Это и есть «лёгкость»: нет прослойки вроде Gitaly, как в GitLab.
- **База хранит только метаданные**: пользователей, права, issues, merge request'ы, токены, хуки. Если база разъехалась с диском — репозиторий «есть», но в UI его нет. Поэтому бэкап всегда делается логически (`dump`), а не «скопировал каталог на живой сервис».
- **Слои**: HTTP/HTTPS и SSH — это L7 (прикладной уровень). Git по HTTPS работает поверх «умного» HTTP (`git-upload-pack`), по SSH — через `git-receive-pack`/`git-upload-pack` внутри SSH-сессии. Порт значения не имеет: SSH можно повесить на 2222, чтобы не занимать 22 у системного sshd.
- **`START_SSH_SERVER = true`** — Gitea/Forgejo поднимает свой SSH-сервер сам и сам управляет `authorized_keys` (ключи пользователей лежат в БД, а не в файле). Альтернатива — отдать SSH системному sshd и включить `AuthorizedKeysCommand`, но это сложнее; на учебном стенде берём встроенный.
- **CI**: и Gitea Actions, и Forgejo Actions совместимы по синтаксису с GitHub Actions (`.github/workflows/*.yml`), но выполняются **отдельным процессом-раннером** (`act_runner` у Gitea, `forgejo-runner` у Forgejo — точное текущее имя сверь в документации `[ПРОВЕРИТЬ]`). В отличие от GitLab CI, раннер не встроен в сам сервер.
- **Сравнение по ресурсам**: GitLab Omnibus — минимум 4 ГБ RAM (реально 8), Gitea/Forgejo — комфортно с 1 ГБ на 20–50 пользователей при SQLite [ПРОВЕРИТЬ по актуальной документации: цифры меняются от версии к версии].

Где в РФ это особенно полезно: маленький офис, филиал, изолированный контур, КИИ-сегмент без интернета, стенд разработки, где GitLab «не по размеру». И как промежуточный шаг перед выбором отечественной платформы.

## 3. Аналогия из жизни

GitLab — это бизнес-центр класса А. Своя управляющая компания, ресепшн, охрана, клининг, переговорные, кофейня, пропускная система. Отлично, если у тебя 300 сотрудников и есть служба эксплуатации. Если у тебя мастерская на пять человек — ты платишь за клининг и охрану и всё равно мёрзнешь, потому что инженера, который крутит настройки, нет.

Gitea — мастерская-лофт: стол, полка, нормальный замок на двери. Ты сам решаешь, где стоит стол. Forgejo — та же мастерская, но дом принадлежит не одному владельцу, а товариществу собственников: решения принимаются сообществом, код открыт и не может быть «перелицензирован» одним человеком.

Где аналогия ломается: **мастерскую всё равно кто-то должен обслуживать**. «Лёгкий» — это про ресурсы и сложность, а не про «настроил и забыл». Бэкапы, обновления, TLS-сертификаты и разбор инцидентов остаются на тебе.

## 4. Практика и команды

Стенд: `UBU01` (Ubuntu Server 24.04, 10.10.10.30). Всё ниже — под sudo, если не указано иное. Для Gitea замени `forgejo` → `gitea`, `/etc/forgejo` → `/etc/gitea`, `codeberg.org` → `dl.gitea.com`.

**1. Скачать бинарник и проверить контрольную сумму**

```bash
VER=9.0.3   # [ПРОВЕРИТЬ] актуальную версию на codeberg.org/forgejo/forgejo/releases
cd /tmp
curl -fLO "https://codeberg.org/forgejo/forgejo/releases/download/v${VER}/forgejo-${VER}-linux-amd64"
curl -fLO "https://codeberg.org/forgejo/forgejo/releases/download/v${VER}/forgejo-${VER}-linux-amd64.sha256"  # [ПРОВЕРИТЬ] точное имя файла
sha256sum -c "forgejo-${VER}-linux-amd64.sha256"
```

`-f` — падать с ошибкой на HTTP 4xx/5xx вместо скачивания HTML-страницы «404»; `-L` — следовать редиректам. Смотреть: строка должна кончиться на `OK`. **Контрольную сумму проверяй всегда** — ты качаешь исполняемый файл, который будет работать от системного пользователя.

**2. Системный пользователь и каталоги**

```bash
sudo useradd --system --shell /bin/bash --home /var/lib/forgejo --create-home git
sudo install -d -o git -g git /var/lib/forgejo/data /var/lib/forgejo/log /var/lib/forgejo/custom /etc/forgejo /var/backups/forgejo
```

Ожидаемо: `id git` показывает системного пользователя без пароля. Gitea/Forgejo не должен работать от root — это отдельный процесс с доступом к коду всех проектов.

**3. Установка и проверка версии**

```bash
sudo install -m 0755 /tmp/forgejo-${VER}-linux-amd64 /usr/local/bin/forgejo
sudo -u git HOME=/var/lib/forgejo /usr/local/bin/forgejo --version
```

Вывод вида `forgejo version 9.0.3 built with ...`. Если падает `command not found: git` — не установлен системный `git` (бинарник Gitea/Forgejo сам Git-операции не реализует, он вызывает `git`).

**4. Генерация секретов**

```bash
sudo -u git HOME=/var/lib/forgejo forgejo generate secret SECRET_KEY
sudo -u git HOME=/var/lib/forgejo forgejo generate secret INTERNAL_TOKEN
```

Каждая команда печатает одну длинную base64-строку. `SECRET_KEY` — подпись cookie и токенов, `INTERNAL_TOKEN` — аутентификация внутренних API. **Не храни их в Git.** При компрометации — меняешь, все сессии разлогиниваются.

**5. Файл конфигурации `/etc/forgejo/app.ini`**

```ini
APP_NAME = Vektor Git
RUN_USER = git
WORK_PATH = /var/lib/forgejo

[server]
PROTOCOL         = http
DOMAIN           = 10.10.10.30
HTTP_ADDR        = 0.0.0.0
HTTP_PORT        = 3000
ROOT_URL         = http://10.10.10.30:3000/
SSH_DOMAIN       = 10.10.10.30
SSH_PORT         = 2222
START_SSH_SERVER = true
LFS_START_SERVER = true

[database]
DB_TYPE = sqlite3
PATH    = /var/lib/forgejo/data/forgejo.db

[repository]
ROOT           = /var/lib/forgejo/data/repositories
DEFAULT_BRANCH = main

[security]
INSTALL_LOCK  = true
SECRET_KEY    = <вставь вывод generate secret SECRET_KEY>
INTERNAL_TOKEN = <вставь вывод generate secret INTERNAL_TOKEN>

[service]
DISABLE_REGISTRATION = true

[log]
MODE  = console
LEVEL = Info
```

Разбор: `ROOT_URL` — адрес, который подставляется в ссылки и вебхуки (если он неверный, клонирование из UI отдаёт битую ссылку — самая частая жалоба); `SSH_PORT` — порт в clone-URL, `START_SSH_SERVER` — встроенный SSH; `INSTALL_LOCK = true` — блокирует веб-мастер первичной установки (иначе любой, кто дошёл до порта 3000, создаст себе админа); `DISABLE_REGISTRATION` — закрытая регистрация.

**6. Юнит systemd `/etc/systemd/system/forgejo.service`**

```ini
[Unit]
Description=Forgejo (Vektor Git)
After=network.target

[Service]
Type=simple
User=git
Group=git
WorkingDirectory=/var/lib/forgejo
Environment=HOME=/var/lib/forgejo
ExecStart=/usr/local/bin/forgejo web --config /etc/forgejo/app.ini
Restart=always
RestartSec=2
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now forgejo
systemctl status forgejo --no-pager
```

Смотреть: `Active: active (running)` и отсутствие `level=error` в последних строках. Логи — `journalctl -u forgejo -n 50 --no-pager`.

**7. Проверка портов и health-эндпоинта**

```bash
sudo ss -lntp | grep -E ':(3000|2222)\b'
curl -s http://127.0.0.1:3000/api/healthz; echo
curl -s http://127.0.0.1:3000/api/v1/version; echo
```

Ожидаемо: две строки LISTEN на `forgejo`, `{"status":"pass"}` и JSON с версией. `/api/healthz` — то, что потом повесишь на мониторинг в MON01 (урок про Zabbix).

**8. Создание администратора из CLI**

```bash
sudo -u git HOME=/var/lib/forgejo forgejo admin user create \
  --username adm.student --password 'Lab#2026-vektor' \
  --email adm.student@corp.vektor.lab --admin \
  --must-change-password=false
```

`--admin` даёт права администратора сайта; `--must-change-password=false` — чтобы учебная учётка сразу работала (в реале оставь `true`). Проверка: `forgejo admin user list`.

**9. Клонирование по SSH на нестандартном порту**

```bash
git clone ssh://git@10.10.10.30:2222/adm.student/vektor-scripts.git
cd vektor-scripts && git remote -v
```

Ключевое: в схеме `ssh://` порт указывается через двоеточие после хоста, а путь — абсолютный (начинается со `/`). Альтернатива на один раз:

```bash
GIT_SSH_COMMAND='ssh -p 2222 -i ~/.ssh/id_ed25519' git clone git@10.10.10.30:adm.student/vektor-scripts.git
```

Проверка соединения без Git: `ssh -p 2222 -T git@10.10.10.30` → `Hi there, adm.student! You've successfully authenticated...`.

**10. Бэкап и его проверка**

```bash
sudo -u git HOME=/var/lib/forgejo forgejo dump --config /etc/forgejo/app.ini \
  --file /var/backups/forgejo/forgejo-$(date +%F).zip
unzip -l /var/backups/forgejo/forgejo-$(date +%F).zip | head -30
```

`dump` собирает базу (в виде SQL-дампа), каталог `data/`, конфиг и `custom/`. Смотреть в листинге: есть `forgejo-db.sql`, `data.zip`, `app.ini`. **Бэкап без проверки восстановлением — не бэкап.**

**11. Диагностика: `doctor`**

```bash
sudo -u git HOME=/var/lib/forgejo forgejo doctor check --all --config /etc/forgejo/app.ini
```

Проверяет права на каталоги, конфиг, git-версию, хуки, таймзоны. Набор подкоманд зависит от версии `[ПРОВЕРИТЬ]`; часть проблем чинится флагом `--run`. Первое, что запускаешь при «а вчера работало».

**12. Альтернатива: развёртывание в Docker Compose (черновик)**

```yaml
services:
  forgejo:
    image: codeberg.org/forgejo/forgejo:9   # [ПРОВЕРИТЬ] актуальный тег
    restart: always
    ports: ["3000:3000", "2222:22"]
    environment:
      - USER_UID=1000
      - USER_GID=1000
      - FORGEJO__database__DB_TYPE=sqlite3
      - FORGEJO__server__ROOT_URL=http://10.10.10.30:3000/
    volumes:
      - /srv/forgejo/data:/data
      - /srv/forgejo/config:/etc/forgejo
```

Смысл тот же, но конфиг можно переопределять переменными окружения `FORGEJO__<секция>__<ключ>`. В проде обязателен бэкап volume'ов и пиннинг конкретного тега, а не `:latest`.

**GUI-пути (если делаешь мышкой):** создание пользователя — `Site Administration → Identity & Access → User Accounts → Create User`; SSH-ключ — `Settings → SSH / GPG Keys → Add Key`; бэкап-политика — `Site Administration → Configuration → Repository` (там же `doctor` кнопкой в новых версиях `[ПРОВЕРИТЬ]`).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- `UBU01` (10.10.10.30, Ubuntu Server 24.04 LTS) — включена, доступ по SSH, пользователь с sudo, 2 vCPU / 2 ГБ RAM / ≥10 ГБ свободно.
- `GIT01` (10.10.10.50, GitLab из урока 17) — существует и работает. **Мы её не трогаем**, только сравниваем на словах.
- Учётки стенда: `adm.student` и пароль `Lab#2026-vektor`. Windows-часть (DC01, WIN11-CL01) может отсутствовать — на лабораторную не влияет.
- Клиент с `git` (сама UBU01 подойдёт) и SSH-ключ `~/.ssh/id_ed25519` (или создаём заново `ssh-keygen -t ed25519`).

Проверка ресурсов: `free -h && df -h / && nproc`.

### Снапшот

В VMware Workstation на ВМ **UBU01**: меню `VM → Snapshot → Take Snapshot…`, имя ровно `before-devops-018`. Галочку «Snapshot the virtual machine's memory» **сними** — снимок диска консистентнее и меньше весит, сервис всё равно ещё не установлен. `GIT01` снапшот не нужен, мы её не меняем.

### Шаги

1. **UBU01.** `sudo apt update && sudo apt -y install git curl unzip sqlite3 ufw`. Ожидаемо: `git --version` → 2.43.x.
2. **UBU01.** Создать пользователя `git` и каталоги (команда из п. 4.2). Ожидаемо: `ls -ld /var/lib/forgejo/data` показывает владельца `git`.
3. **UBU01.** Скачать бинарник Forgejo нужной версии и проверить `sha256sum -c`. Ожидаемо: `OK`.
4. **UBU01.** Установить в `/usr/local/bin/forgejo`, проверить `forgejo --version`. Ожидаемо: вывод версии.
5. **UBU01.** Сгенерировать `SECRET_KEY` и `INTERNAL_TOKEN` (п. 4.4). Сохранить в файл `/root/forgejo-secrets.txt` с `chmod 600` (в проде — в хранилище секретов, не в файл на том же хосте).
6. **UBU01.** Создать `/etc/forgejo/app.ini` из п. 4.5, подставив секреты, `sudo chown root:git /etc/forgejo/app.ini && sudo chmod 640 /etc/forgejo/app.ini`. Ожидаемо: `sudo -u git cat /etc/forgejo/app.ini | head` читается.
7. **UBU01.** Создать юнит systemd, `systemctl daemon-reload && systemctl enable --now forgejo`. Ожидаемо: `Active: active (running)`, в `journalctl -u forgejo -n 30` есть `Listen: http://0.0.0.0:3000` и `SSH server listening on :2222` (формулировка версии-зависима `[ПРОВЕРИТЬ]`).
8. **UBU01.** `ss -lntp | grep -E ':(3000|2222)'` и `curl -s http://127.0.0.1:3000/api/healthz`. Ожидаемо: два LISTEN + `{"status":"pass"}`.
9. **UBU01.** Создать админа `adm.student` через CLI (п. 4.8). Ожидаемо: `forgejo admin user list` показывает строку с `adm.student` и `admin=true`.
10. **Клиент.** Открыть в браузере `http://10.10.10.30:3000`, войти как `adm.student`. Проверить, что веб-мастер установки **не** показывается (сработал `INSTALL_LOCK`).
11. **Клиент.** `Settings → SSH / GPG Keys → Add Key`, вставить публичный ключ. Ожидаемо: ключ в списке с отпечатком.
12. **Клиент.** В UI: `+ → New Repository`, владелец `adm.student`, имя `vektor-scripts`, галочка «Initialize repository» (README). Ожидаемо: репозиторий с одним коммитом.
13. **Клиент.** `git clone ssh://git@10.10.10.30:2222/adm.student/vektor-scripts.git`, добавить файл, `git add/commit/push`. Ожидаемо: `main -> main` и файл виден в веб-интерфейсе.
14. **UBU01.** Сделать `forgejo dump` в `/var/backups/forgejo/` и посмотреть листинг архива. Ожидаемо: внутри `app.ini`, дамп БД, `data.zip`.
15. **UBU01.** Поставить ежедневный бэкап: `sudo crontab -u root -e` → `30 2 * * * /usr/bin/sudo -u git HOME=/var/lib/forgejo /usr/local/bin/forgejo dump --config /etc/forgejo/app.ini --file /var/backups/forgejo/forgejo-$(date +\%F).zip`. Обрати внимание на экранирование `\%` в cron.

### Проверка

- [ ] `systemctl is-enabled forgejo` → `enabled`, `systemctl is-active forgejo` → `active`
- [ ] `curl -s http://10.10.10.30:3000/api/healthz` → `{"status":"pass"}`
- [ ] `ss -lntp` показывает 3000 и 2222, оба — процесс `forgejo`
- [ ] Веб-интерфейс открывается, мастер установки не появляется
- [ ] `forgejo admin user list` содержит `adm.student` с правами админа
- [ ] `ssh -p 2222 -T git@10.10.10.30` возвращает приветствие с твоим логином
- [ ] `git clone` + `git push` по SSH работают, коммит виден в UI
- [ ] `forgejo dump` создан за последние сутки, `unzip -l` показывает `app.ini` и данные
- [ ] В `/var/lib/forgejo/data/` нет файлов, принадлежащих `root`

### Сломай и почини

**Заявка**

> **Пользователь:** petrova (Мария Петрова, HR) — но обращается к нашему Git-серверу на UBU01
> **ВМ:** UBU01
> **Что написала:** «Вчера спокойно клонировала репозиторий по SSH, сегодня — `ssh: connect to host 10.10.10.30 port 2222: Connection refused`. В браузере сайт открывается, ключ я не меняла. Помогите, у нас релиз документации сегодня.»
> **Срочность:** средняя (работа стоит, прод не затронут)

**Как воспроизвести**

```bash
sudo ufw allow 3000/tcp
sudo ufw --force enable
sudo ufw status numbered
```

Политика ufw по умолчанию — `deny (incoming)`. Порт 3000 разрешён, 2222 — нет.

**Диагностика по порядку (от процесса к сети, не наоборот)**

1. Сервис жив? `systemctl status forgejo` → `active (running)`. Значит, дело не в падении.
2. Порт слушается? `sudo ss -lntp | grep 2222` → `LISTEN 0 4096 0.0.0.0:2222 ... users:(("forgejo"...))`. Слушается — процесс не виноват.
3. Локально работает? `ssh -p 2222 -T git@127.0.0.1` → приветствие. Значит, приложение исправно, проблема в фильтрации между хостами.
4. `sudo ufw status verbose` → в списке только `3000/tcp`. Вот и причина.
5. Подтверждение: `sudo dmesg | tail -20` → строки `[UFW BLOCK]` с `DPT=2222`. (Альтернатива: `sudo tcpdump -ni any port 2222` — видно SYN без ответа.)
6. Логи приложения `journalctl -u forgejo -n 50` — чисто, ошибок нет. Это ожидаемо: до приложения пакет не дошёл.

**Решение**

```bash
sudo ufw allow 2222/tcp
sudo ufw status numbered
ssh -p 2222 -T git@10.10.10.30
```

Затем — гигиена: `sudo ufw default deny incoming`, `sudo ufw allow from 10.10.10.0/24 to any port 3000 proto tcp`, `sudo ufw allow from 10.10.10.0/24 to any port 2222 proto tcp`, чтобы сервис не торчал в DMZ (10.10.20.0/24).

**Ответ пользователю**

> Мария, добрый день. Нашли причину: вчера на сервере включили файрвол для закрытия лишних портов, и порт для Git по SSH (2222) остался закрытым. Сайт работает по другому порту (3000), поэтому он открывался. Порт открыли, ограничили доступ только офисной подсетью. Проверьте, пожалуйста, `git clone` ещё раз — у меня тест прошёл. Если снова ошибка, пришлите полный текст вывода `ssh -vvv -p 2222 git@10.10.10.30`.

**Закрытие заявки**: тип «изменение конфигурации», корневая причина «включение ufw без аудита правил», действие «правило добавлено, firewall-политика задокументирована в stand-log.md», профилактика — чек-лист «перед включением файрвола перечисли все публикуемые порты сервиса».

### Что изменилось на стенде

- **UBU01 (10.10.10.30)**: установлен `forgejo` в `/usr/local/bin/`, юнит `forgejo.service` (enabled), конфиг `/etc/forgejo/app.ini`.
- Пользователь ОС `git`, каталоги `/var/lib/forgejo/{data,log,custom}`, `/var/backups/forgejo/`.
- Порты: **3000/tcp** (web + API), **2222/tcp** (Git over SSH, встроенный SSH-сервер).
- База SQLite: `/var/lib/forgejo/data/forgejo.db`; репозитории: `/var/lib/forgejo/data/repositories/`.
- Учётка администратора Gitea/Forgejo: `adm.student` (пароль стенда), email `adm.student@corp.vektor.lab`.
- Репозиторий `adm.student/vektor-scripts` с одним коммитом в `main`.
- ufw включён, разрешены 3000 и 2222 только из 10.10.10.0/24.
- Cron-задача ежедневного `forgejo dump` в 02:30.
- **GIT01 не менялся** — GitLab остаётся как «тяжёлая» платформа для сравнения.

### Откат

- Быстро и полностью: VMware Workstation → `VM → Snapshot → Snapshot Manager`, выбрать `before-devops-018` → `Go To`. ВМ вернётся к состоянию без Forgejo, ufw и репозитория.
- Точечно, без отката снапшота: `sudo systemctl disable --now forgejo`, `sudo rm /etc/systemd/system/forgejo.service && sudo systemctl daemon-reload`, `sudo rm -rf /var/lib/forgejo /etc/forgejo /var/backups/forgejo`, `sudo userdel git`.
- **Важно**: после успешной лабораторной стенд остаётся в новом состоянии (Forgejo — часть стенда DevOps-трека). Откат — только если что-то сломалось необратимо. Изменения фиксируй в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **`INSTALL_LOCK = false` и порт 3000 наружу** → кто угодно открывает `/install` и создаёт себе администратора, дальше — доступ к коду и токенам. → Всегда ставь `INSTALL_LOCK = true` после создания первого админа, а до этого не публикуй порт.
2. **`ROOT_URL` указывает на `localhost` или неверный порт** → в UI генерируются битые clone-ссылки, вебхуки уходят «в никуда», письма содержат неправильные адреса. → `ROOT_URL` должен побайтово совпадать с адресом, по которому заходят пользователи (включая порт и завершающий `/`).
3. **Сервис запущен от root** → любая уязвимость в веб-приложении сразу даёт root на хосте и доступ ко всем репозиториям организации. → Только пользователь `git`, только systemd-юнит с `User=git`.
4. **Бэкап «скопировал каталог на живой сервис»** → SQLite копируется в момент записи, получаешь битую базу; репозитории разъезжаются с метаданными. → Только `forgejo dump` (или `sqlite3 ... ".backup"` при остановленном сервисе) и **регулярная проверка восстановлением на отдельной ВМ**.
5. **SQLite «на вырост» для 100+ человек** → блокировки при параллельной записи, «database is locked», медленный поиск. → До ~20–50 активных пользователей SQLite ок, дальше — PostgreSQL (переключается в `app.ini` + `forgejo migrate` `[ПРОВЕРИТЬ]`).
6. **Обновление без чтения release notes и без снапшота** → несовместимые миграции БД, откат невозможен. → Порядок: снапшот/бэкап → release notes → обновление → `doctor check --all` → smoke-тест.
7. **Нет `[mailer]`** → «Забыл пароль» не работает, приглашения не уходят, вся поддержка — руками через CLI. → Настрой SMTP сразу, даже на маленьком стенде.
8. **Регистрация открыта** → спам-аккаунты и мусорные репозитории. → `DISABLE_REGISTRATION = true`, пользователей заводит админ или LDAP.

### Безопасность

В боевой среде **нельзя**: держать Git-сервер на HTTP без TLS (креды и токены уходят открытым текстом — на стенде мы так делаем только потому, что трафик в изолированной сети; в реальности ставь nginx с TLS, уроки 10–11); открывать 3000/2222 в интернет без VPN/bastion (урок 12); хранить `SECRET_KEY` в Git или в заметках; выдавать всем права администратора «чтобы не мелочиться»; использовать один общий аккаунт на команду. Минимум для прода: HTTPS, 2FA у админов (Gitea/Forgejo поддерживают TOTP), отдельные deploy-токены вместо паролей, `fail2ban` на SSH-порт, ограничение доступа по подсетям и регулярный аудит списка администраторов.

## 7. Вопросы на собеседовании

**1. Чем Gitea/Forgejo отличается от GitLab и как выбрать?**

Ответ: Это разные весовые категории. Gitea/Forgejo — один Go-бинарник, SQLite/PostgreSQL, bare-репозитории прямо на диске, деплой за полчаса, комфортно с 1–2 ГБ RAM. GitLab — набор сервисов (Rails, Sidekiq, Gitaly, Redis, Postgres), требует 4–8 ГБ RAM и нормальной эксплуатации, зато даёт зрелые merge request'ы, встроенный CI/CD с богатым синтаксисом, сканирование безопасности, управление зависимостями, продвинутые права. Выбор: до ~50 разработчиков и без сложных пайплайнов — Gitea/Forgejo; нужна корпоративная интеграция, SAST/DAST, сложные CI-пайплайны, аудит — GitLab. Плюс отдельный критерий — кто и сколько людей будет это эксплуатировать: «лёгкое» решение экономит RAM, но не время админа.
*Что хочет услышать*: понимание, что «лёгкий» ≠ «бесплатный по трудозатратам», и что выбор делается по числу людей, требованиям CI и наличию эксплуатанта.
*Красный флаг*: «Gitea лучше, потому что бесплатная» — обе бесплатны в базовых редакциях, и это не аргумент.

**2. Что и как вы бэкапите на self-hosted Gitea/Forgejo?**

Ответ: Логический дамп командой `forgejo dump` — он собирает SQL-дамп базы, каталог `data/` (репозитории, LFS, аватары), `custom/` и `app.ini`. Отдельно — секреты из хранилища секретов. Ключевое: (1) бэкап по расписанию и с retention; (2) хранение **вне** этого хоста; (3) регулярная проверка восстановлением на отдельной ВМ — разворачиваем дамп, поднимаем сервис, проверяем, что клонируется репозиторий и открывается история; (4) фиксируем RPO/RTO. Если база внешняя (PostgreSQL) — плюс `pg_dump`/WAL-архив. Снапшот ВМ — не замена логическому бэкапу, он не даёт восстановить отдельный репозиторий.
*Что хочет услышать*: «бэкап проверяется восстановлением», «дамп, а не копирование каталога на живом сервисе».
*Красный флаг*: «делаю снапшот VMware раз в месяц и хватает».

**3. Как организовать аутентификацию в Gitea/Forgejo в компании?**

Ответ: Три уровня. (1) Источники аутентификации: LDAP/Active Directory (`[security]`/Admin → Authentication Sources) или OAuth2/OIDC — тогда учётки живут в AD, а сервис только проверяет. (2) Локальные аккаунты оставляем для аварийного админа (break-glass) — иначе падение AD блокирует доступ к коду. (3) Внутри — 2FA (TOTP), SSH-ключи вместо паролей для Git, отдельные access-токены для CI с минимальными правами и TTL. Плюс маппинг групп AD → команд/организаций, чтобы права не раздавали руками.
*Что хочет услышать*: интеграция с корпоративным каталогом + обязательный аварийный локальный админ + токены для автоматизации.
*Красный флаг*: «сделаем всем один общий пароль» или «AD не нужен, заведём локальных пользователей на 100 человек».

**4. Как в Gitea/Forgejo устроен CI и чем отличается от GitLab CI?**

Ответ: Actions — синтаксис совместим с GitHub Actions, workflow лежат в `.github/workflows/*.yml`, но исполняет их отдельный процесс-раннер (`act_runner` / `forgejo-runner`, имя зависит от форка и версии `[ПРОВЕРИТЬ]`), который регистрируется на сервере токеном и может работать в Docker или на хосте. Сервер сам джобы не запускает. Отличия от GitLab CI: нет такого количества готовых шаблонов и компонентов, меньше фич (например, окружения и защищённые переменные проще), меньше «из коробки» сканеров безопасности. Для типовых задач (линт, тесты, сборка образа, пуш в HARBOR01, деплой в k8s) возможностей хватает; раннеры нужно ставить и обновлять самому — как и GitLab Runner, впрочем.
*Что хочет услышать*: понимание, что раннер — отдельная сущность, и что совместимость с GitHub Actions не 100 %.
*Красный флаг*: «Actions работают прямо в сервере, раннер не нужен».

**5. Российские аналоги: что смотреть при выборе?**

Ответ: Сначала — требования. (1) Форма: облако или on-premise (для КИИ и госкомпаний чаще только on-prem). (2) Наличие в реестре российского ПО Минцифры и пригодность под требования регуляторов (КИИ, 152-ФЗ, хранение кода в РФ). (3) Лицензия и стоимость владения. (4) Совместимость: импорт репозиториев из GitLab/GitHub, поддержка Git LFS, API, CI, LDAP/AD. (5) Поддержка вендора и SLA. В РФ на слуху **GitFlic** (gitflic.ru — есть self-hosted версия, в реестре `[ПРОВЕРИТЬ]`), **GitVerse** от Сбера (gitverse.ru, есть корпоративные варианты `[ПРОВЕРИТЬ]`), **SourceCraft** от Яндекса (sourcecraft.dev, on-prem-вариант `[ПРОВЕРИТЬ]`). Плюс «нейтральный» путь — Gitea/Forgejo на своём железе: открытый код, никаких лицензионных рисков, но формально это не отечественное ПО, и для некоторых заказчиков это блокер.
*Что хочет услышать*: умение начать с требований и реестра, а не с названий; понимание, что «импортозамещение» — это отдельный критерий выбора, а не свойство кода.
*Красный флаг*: уверенные заявления про конкретные цены, версии и наличие в реестре без проверки; советы ставить «крякнутую» корпоративную версию.

## 8. Источники (официальная документация)

- **forgejo.org** — раздел Documentation: Installation → «Installation from binary», «Configuration Cheat Sheet» (все параметры `app.ini`), «Backup and restore», «Command Line». Поиск: `forgejo binary install systemd app.ini`.
- **codeberg.org/forgejo/forgejo** — страница Releases: актуальные версии, имена файлов, контрольные суммы. Поиск: `forgejo releases codeberg`.
- **docs.gitea.com** (и **about.gitea.com**) — Gitea Documentation: «Installation from binary», «Configuration Cheat Sheet», «Backup and Restore», «Command Line», «Gitea Actions». Поиск: `gitea configuration cheat sheet app.ini`.
- **dl.gitea.com** — официальное зеркало релизов Gitea с `.sha256` и подписями `.asc`. Поиск: `gitea download releases dl.gitea.com`.
- **gitflic.ru** — раздел с документацией и описанием вариантов поставки (облако / on-premise) `[ПРОВЕРИТЬ структуру раздела]`.
- **gitverse.ru** — документация платформы, раздел о корпоративных/self-hosted решениях `[ПРОВЕРИТЬ]`.
- **sourcecraft.dev** — документация Яндекс SourceCraft, раздел о вариантах развёртывания `[ПРОВЕРИТЬ]`.
- **reestr.digital.gov.ru** — реестр российского ПО Минцифры; поиск по названию продукта `[ПРОВЕРИТЬ точный домен реестра]`.
- **nginx.org** — раздел Documentation → «Reverse proxy» (для публикации Gitea/Forgejo через nginx с TLS; см. также уроки 10–11).
- **man7.org** — `man systemd.unit`, `man systemd.service`; **ubuntu.com/server/docs** — раздел про ufw и базовую настройку сервера.
