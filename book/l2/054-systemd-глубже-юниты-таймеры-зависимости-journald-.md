# Systemd глубже: юниты, таймеры, зависимости, journald, диагностика падающих сервисов

## 1. Введение (зачем это нужно в реальной работе)

**Заявка.** Пользователь: ivanov (бухгалтерия) / ВМ: WIN11-CL01 / Что написал: «Второй день нет ночной выгрузки по очереди платежей. Раньше файл появлялся каждое утро, а сейчас пусто, мы не можем закрыть день» / Срочность: средняя.

На стенде выгрузку формирует скрипт на UBU01, который запускается ночью по расписанию. Значит, смотреть надо не на Windows-машину бухгалтера, а на Linux-сервер: жив ли юнит, сработал ли таймер, что написано в журнале.

В L1 вы уже видели `systemctl restart` и `journalctl -u`. В L2 этого мало: нужно понимать, **из чего состоит юнит**, **как systemd выстраивает зависимости**, **почему сервис падает с конкретным кодом** и **почему таймер «настроен», но не стреляет**. В реальной работе 80% инцидентов на Linux-сервере читаются по трём вещам: `systemctl status`, `journalctl -u` и `systemctl list-timers`. После этой главы вы сможете по одной заявке за 5–10 минут найти корневую причину, а не перезапускать сервис «на удачу».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**systemd** — это PID 1, первая программа, которую запускает ядро. Она поднимает всё остальное: сеть, SSH, cron-замену, журнал, getty. Она же следит за сервисами и перезапускает упавшие.

Единица управления — **юнит (unit)**. Юнит — это текстовый файл с описанием. Основные типы:

| Тип | Что описывает | Пример |
|---|---|---|
| `.service` | процесс-демон или одноразовая задача | `ssh.service` |
| `.socket` | сокет активации (сервис стартует по обращению) | `ssh.socket` |
| `.timer` | расписание запуска другого юнита | `apt-daily.timer` |
| `.target` | группа юнитов, «этап загрузки» | `multi-user.target` |
| `.mount` / `.automount` | точка монтирования | `-.mount` |
| `.path` | слежение за файлом/каталогом | `systemd-ask-password-wall.path` |
| `.slice` | группа процессов для лимитов ресурсов | `system.slice` |

**Где лежат юниты (важно для работы):**

```text
/lib/systemd/system/     ← то, что поставили пакеты (Debian/Ubuntu; на RHEL-семействе /usr/lib/systemd/system)
/run/systemd/system/     ← создано во время работы, пропадёт после ребута
/etc/systemd/system/     ← ВАШИ юниты и ваши переопределения. Высший приоритет.
```

Правило: файлы из `/lib/...` **не редактируют руками** — при обновлении пакета их перезапишут. Своё изменение оформляют как **drop-in**: `systemctl edit unit` создаёт `/etc/systemd/system/unit.d/override.conf`, который «накладывается» сверху. `systemctl edit --full unit` создаёт полную копию юнита в `/etc`.

**Структура юнита.** Три секции:

```ini
[Unit]                 # метаданные и зависимости
[Service]              # как именно запускать (только для .service)
[Install]              # что делать при systemctl enable
```

**Зависимости — самое неочевидное.** Есть два разных понятия, новички их путают:

- **Порядок** (`After=`, `Before=`) — «запусти меня позже». Сам по себе ничего не требует.
- **Требование** (`Requires=`, `Wants=`, `BindsTo=`, `PartOf=`, `Conflicts=`) — «мне нужен этот юнит».

`Wants` — мягкое: если зависимость не поднялась, мы всё равно стартуем. `Requires` — жёсткое: не поднялась зависимость — не стартуем мы (и падаем вместе с ней при остановке). `After` без `Requires`/`Wants` **не тянет** зависимость за собой — он только говорит «если она всё же стартует, то раньше меня».

Схема типичного сервиса в дереве загрузки:

```text
                     graphical.target            (на сервере отсутствует)
                            ▲ Wants
                     multi-user.target           ← «система в рабочем режиме»
                            ▲ WantedBy=
              ┌─────────────┴───────────────┐
              │   vektor-queue.service      │
              │ After=network-online.target │  ← только ПОРЯДОК
              │ Wants=network-online.target │  ← кто-то должен её поднять
              └─────────────▲───────────────┘
                            │
              network-online.target  →  NetworkManager-wait-online.service
              (вот это и ждёт реальной сети, network.target — не ждёт)
```

**Типы сервисов** (`Type=`): `simple` (по умолчанию, процесс не форкается — 90% случаев), `exec` (как simple, но считается запущенным после успешного exec), `oneshot` (сделал дело и вышел — для скриптов и таймеров), `forking` (классический демон, который уходит в фон и пишет pid-файл), `notify` (сам сообщает о готовности через sd_notify).

**Политика перезапуска:** `Restart=no|on-failure|always|on-abnormal`, `RestartSec=`. Защита от «карусели» — **StartLimitIntervalSec/StartLimitBurst** (по умолчанию 5 стартов за 10 секунд). Если превышено, юнит встаёт в `failed` с `start-limit-hit` и больше не пытается — лечится `systemctl reset-failed`.

**Таймеры.** `.timer` — это расписание для одноимённого `.service`. Два вида:
- **монотонные**: `OnBootSec=`, `OnUnitActiveSec=`, `OnStartupSec=` — «через N после события»;
- **календарные**: `OnCalendar=*-*-* 03:00:00`, `Mon..Fri *-*-* 09:00`, `*:0/15` (каждые 15 минут).

Полезные ключи: `Persistent=true` (если система была выключена в момент срабатывания — догнать при загрузке), `RandomizedDelaySec=` (размазать одновременный запуск парка машин), `AccuracySec=` (по умолчанию 1 минута — systemd специально не будит диск точно в 03:00:00).

**journald** — подсистема сбора логов. Приложение пишет в stdout/stderr, systemd ловит это и передаёт в `systemd-journald`, который складывает записи в бинарный журнал с индексированными полями (`_PID`, `_UID`, `_SYSTEMD_UNIT`, `_COMM`, `PRIORITY`).

```text
приложение (stdout/stderr)
        │
        ▼
 systemd-journald ──► /run/log/journal   (RAM: после ребута пусто)
        │          └► /var/log/journal   (диск: нужно Storage=persistent/auto + каталог)
        ▼
 journalctl   +   rsyslog (если стоит) → /var/log/syslog
```

Плюс журнала: структурированные поля, фильтры по юниту/загрузке/приоритету, ротация по размеру и времени. Минус: бинарный формат, без `journalctl` не прочитать, и по умолчанию **не гарантирует** хранение между загрузками.

## 3. Аналогия из жизни

Представьте прораба на большой стройке. У него есть наряды (юниты): «сварщик», «бетономешалка», «сторож». В каждом наряде написано, что делать, кем работать (User=), что считать успехом и что делать, если работник упал (Restart=). В наряде же указано «начинать после того, как подвезут бетон» — это `After`, порядок; и «бетоновоз мне обязателен» (`Requires`) или «желателен, но обойдусь» (`Wants`). Прораб не рассуждает: если написано «обойдусь», он запустит сварщика и без бетона, а потом удивляться не надо. Отдельный будильник (таймер) говорит, когда дёрнуть работника с конкретным нарядом, а вахтённый журнал (journald) фиксирует каждую строчку, которую работник крикнул, — с временем, номером загрузки смены и тем, кто именно кричал.

Аналогия ломается в одном: у прораба есть здравый смысл, а systemd выполняет ровно то, что написано в файле, — и если вы перепутали `After` и `Requires`, он не догадается, он просто сделает не то.

## 4. Практика и команды

**1. `systemctl status` — читаем поля, а не только «active»**

```bash
systemctl status vektor-queue.service --no-pager
```

```text
● vektor-queue.service - Vektor queue worker (demo)
     Loaded: loaded (/etc/systemd/system/vektor-queue.service; enabled; preset: enabled)
    Drop-In: /etc/systemd/system/vektor-queue.service.d
             └─override.conf
     Active: failed (Result: exit-code) since Tue 2026-02-10 03:00:04 MSK; 2s ago
    Process: 4411 ExecStart=/opt/vektor/bin/queue-worker.sh (code=exited, status=203/EXEC)
   Main PID: 4411 (code=exited, status=203/EXEC)
```

Смотрим: `Loaded` — откуда взят файл и включён ли; `Drop-In` — есть ли переопределения; `Active` + `Result`; строка `Process` с `code=` и `status=` (это и есть код выхода systemd); `Main PID`. Коды, которые надо узнавать в лицо: `203/EXEC` — не найден/не исполняется файл из `ExecStart`; `217/USER` — нет такого пользователя; `200/CHDIR` — нет `WorkingDirectory`; `1/FAILURE` — скрипт отработал с кодом 1.

**2. `systemctl cat` и `systemctl edit` — где лежит и как править**

```bash
systemctl cat vektor-queue.service     # покажет и основной файл, и все drop-in'ы
sudo systemctl edit vektor-queue.service   # создать override.conf
sudo systemctl daemon-reload               # ОБЯЗАТЕЛЬНО после правки юнита
```

`cat` — единственный надёжный способ увидеть итоговый вид юнита: файлов может быть три, и вы не помните, что в каком.

**3. Состояния юнитов: `list-units`, `list-unit-files`, `is-enabled`**

```bash
systemctl list-units --type=service --state=running
systemctl list-units --state=failed
systemctl list-unit-files --state=enabled | head -20
systemctl is-enabled vektor-report.timer; systemctl is-active vektor-report.timer
```

Состояния `list-unit-files`: `enabled`, `disabled`, `static` (нельзя включить, его тянет кто-то другой), `masked` (ссылкой на `/dev/null` — запуск запрещён полностью), `indirect`, `generated`. `is-active` и `is-enabled` возвращают код возврата — удобно в скриптах.

**4. Зависимости: кто кого тянет**

```bash
systemctl list-dependencies vektor-queue.service
systemctl list-dependencies vektor-queue.service --reverse
systemctl show vektor-queue.service -p Wants -p Requires -p After -p Before
systemd-analyze critical-chain vektor-queue.service
```

`--reverse` показывает, кто зависит от вас (важно перед остановкой сервиса). `critical-chain` — самый длинный путь по времени до юнита, сразу видно, что задержало старт:

```text
vektor-queue.service +1.210s
└─network-online.target @2.001s
  └─NetworkManager-wait-online.service @1.870s +131ms
```

**5. `systemd-analyze verify` — проверить юнит до запуска**

```bash
sudo systemd-analyze verify /etc/systemd/system/vektor-queue.service
```

Молчание = синтаксис в порядке. Ловит опечатки в путях, неизвестные ключи, отсутствующих пользователей.

**6. `journalctl -u` — журнал конкретного сервиса**

```bash
journalctl -u vektor-queue.service -b --no-pager -n 30
journalctl -u vektor-report.service --since today --until "10 min ago"
journalctl -u vektor-queue.service -f            # хвост в реальном времени
journalctl -p err -b --no-pager                  # все ошибки с текущей загрузки
```

Ключи: `-u` юнит, `-b` текущая загрузка (`-b -1` — предыдущая, работает только при постоянном журнале), `-n` строк, `--since/--until`, `-p` приоритет (`err`, `warning`), `-f` follow, `-x` с пояснениями, `-o short-iso|json-pretty`.

**7. Структурированные поля и вывод в JSON**

```bash
journalctl -u vektor-queue.service -n 1 -o json-pretty
journalctl _PID=4411 -b --no-pager
journalctl -k -b | tail          # ядро, аналог dmesg
```

Поля `_PID`, `_COMM`, `_SYSTEMD_UNIT` позволяют найти все сообщения процесса, даже если он писал не через юнит.

**8. Обслуживание журнала: размер, ротация, постоянное хранение**

```bash
journalctl --disk-usage
sudo journalctl --vacuum-time=14d
ls -ld /var/log/journal
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
```

Без каталога `/var/log/journal` журнал живёт только в RAM. Лимиты задаются в `/etc/systemd/journald.conf` (`Storage=`, `SystemMaxUse=`, `RuntimeMaxUse=`). На Ubuntu серверный журнал обычно уже постоянный — проверьте `ls -ld /var/log/journal` [ПРОВЕРИТЬ на своём стенде].

**9. Политика перезапуска и счётчики**

```bash
systemctl show vektor-queue.service -p Restart -p RestartSec -p NRestarts -p Result -p ExecMainStatus -p User
systemctl reset-failed vektor-queue.service
```

`NRestarts` — сколько раз systemd уже перезапускал. Если растёт — сервис в цикле, лечим причину, а не счётчик.

**10. Таймеры**

```bash
systemctl list-timers --all
systemd-analyze calendar "*:0/2"
systemd-analyze calendar "*-*-* 03:00:00"
systemctl enable --now vektor-report.timer
```

`list-timers` отдаёт `NEXT`, `LAST`, `PASSED`, `UNIT`, `ACTIVATES`. Если `NEXT` = `n/a` — таймер не активен или формат даты не распознан. `systemd-analyze calendar` — обязательная проверка выражения до того, как ставить его в юнит:

```text
  Original form: *:0/2
Normalized form: *-*-* *:00/2:00
    Next elapse: Tue 2026-02-10 14:12:00 MSK
       From now: 1min 12s left
```

**11. Одноразовый запуск без создания файла**

```bash
sudo systemd-run --on-active=30s --unit=vektor-test /usr/bin/echo hello-from-timer
journalctl -u vektor-test --no-pager
```

`systemd-run` создаёт временный юнит/таймер «на лету» — удобно для проверки гипотез без мусора в `/etc`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- UBU01 (`10.10.10.30`, Ubuntu Server 24.04 LTS) включена, есть вход по SSH/консоли с учёткой, у которой `sudo`. SSH и сеть настраивали в уроках 51–53.
- DC01, FS01 и Windows-клиенты не нужны — работаем только на UBU01.
- Свободно минимум 1 ГБ на `/` и `/var`. Проверьте `df -h /` и `sudo timedatectl` — если время сильно врёт, таймеры будут вести себя странно; поправьте `sudo timedatectl set-ntp true`.
- Каталог `/srv/vektor/reports` на стенде — учебный аналог папки, из которой бухгалтерия забирает утреннюю выгрузку. Доставку на FS01 в этой главе не настраиваем (это урок 58).

### Снапшот

На UBU01:

```bash
sudo poweroff
```

В VMware Workstation: выделить UBU01 → меню **VM → Snapshot → Take Snapshot** → имя `before-l2-054` → OK. Затем включить ВМ. Если снимаете на работающей ВМ, поставьте галку с памятью — но для учебного стенда надёжнее выключенная.

### Шаги

1. **Разведка (UBU01).** Посмотреть, как выглядят «родные» юниты:
   ```bash
   systemctl list-units --type=service --state=running | head -15
   systemctl status ssh --no-pager
   systemctl cat ssh | head -25
   ```
   Ожидаемо: список из ~15–20 сервисов, у `ssh.service` видно `Loaded: ... enabled` и путь `/lib/systemd/system/ssh.service`.

2. **Создать служебную учётку и каталоги:**
   ```bash
   sudo groupadd --system svc-queue
   sudo useradd --system --gid svc-queue --no-create-home --shell /usr/sbin/nologin svc-queue
   sudo install -d -o svc-queue -g svc-queue \
     /opt/vektor/bin /srv/vektor/queue/incoming /srv/vektor/queue/processed /srv/vektor/reports
   ```
   Ожидаемо: `id svc-queue` показывает системного пользователя с UID < 1000 и оболочкой nologin.

3. **Написать скрипт-обработчик** `/opt/vektor/bin/queue-worker.sh`:
   ```bash
   sudo tee /opt/vektor/bin/queue-worker.sh >/dev/null <<'EOF'
   #!/usr/bin/env bash
   set -euo pipefail
   QUEUE=/srv/vektor/queue/incoming
   DONE=/srv/vektor/queue/processed
   echo "queue-worker: старт, каталог $QUEUE"
   while true; do
     for f in "$QUEUE"/*.json; do
       [ -e "$f" ] || continue
       mv "$f" "$DONE/"
       echo "processed: $(basename "$f")"
     done
     sleep 15
   done
   EOF
   sudo chmod 755 /opt/vektor/bin/queue-worker.sh
   ```
   Ожидаемо: файл создан, `bash -n` на него не ругается.

4. **Создать юнит** `/etc/systemd/system/vektor-queue.service`:
   ```ini
   [Unit]
   Description=Vektor queue worker (demo)
   After=network-online.target local-fs.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=svc-queue
   Group=svc-queue
   WorkingDirectory=/srv/vektor
   ExecStart=/opt/vektor/bin/queue-worker.sh
   Restart=on-failure
   RestartSec=5s
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```
   Проверить и запустить:
   ```bash
   sudo systemd-analyze verify /etc/systemd/system/vektor-queue.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now vektor-queue.service
   systemctl status vektor-queue.service --no-pager
   ```
   Ожидаемо: `active (running)`, `Main PID` есть, в `CGroup:` видны `queue-worker.sh` и `sleep 15`.

5. **Проверить, что сервис реально работает:** положить задание и посмотреть журнал.
   ```bash
   sudo -u svc-queue touch /srv/vektor/queue/incoming/order-1001.json
   journalctl -u vektor-queue.service -f
   ```
   Через ~15 секунд в журнале появится `processed: order-1001.json`. Выйти из `-f` — `Ctrl+C`. Файл должен лежать в `/srv/vektor/queue/processed`.

6. **Сделать drop-in и убедиться, что он применяется:**
   ```bash
   sudo systemctl edit vektor-queue.service
   ```
   В открывшемся редакторе вписать:
   ```ini
   [Service]
   Environment=VEKTOR_MODE=lab
   RestartSec=10s
   ```
   Далее:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart vektor-queue.service
   systemctl cat vektor-queue.service | tail -8
   systemctl show vektor-queue.service -p Environment -p RestartSec
   ```
   Ожидаемо: в `cat` видна секция `Drop-In`, `Environment=VEKTOR_MODE=lab`, `RestartSec=10s`.

7. **Посмотреть зависимости:**
   ```bash
   systemctl list-dependencies vektor-queue.service
   systemctl list-dependencies vektor-queue.service --reverse
   systemd-analyze critical-chain vektor-queue.service
   ```
   Ожидаемо: в дереве `multi-user.target` ← `vektor-queue.service` ← `network-online.target`; в `--reverse` — только `multi-user.target`; `critical-chain` показывает, сколько заняла сеть.

8. **Сделать отчёт и таймер.** Скрипт `/opt/vektor/bin/queue-report.sh`:
   ```bash
   sudo tee /opt/vektor/bin/queue-report.sh >/dev/null <<'EOF'
   #!/usr/bin/env bash
   set -euo pipefail
   OUT=/srv/vektor/reports
   STAMP=$(date +%F)
   {
     echo "date,in_queue,processed"
     echo "$STAMP,$(find /srv/vektor/queue/incoming -maxdepth 1 -name '*.json' | wc -l),$(find /srv/vektor/queue/processed -maxdepth 1 -name '*.json' | wc -l)"
   } > "$OUT/queue-$STAMP.csv"
   echo "report written: $OUT/queue-$STAMP.csv"
   EOF
   sudo chmod 755 /opt/vektor/bin/queue-report.sh
   ```
   Юнит `vektor-report.service`:
   ```ini
   [Unit]
   Description=Vektor queue report (oneshot)

   [Service]
   Type=oneshot
   User=svc-queue
   Group=svc-queue
   ExecStart=/opt/vektor/bin/queue-report.sh
   ```
   Юнит `vektor-report.timer` (пока с интервалом в 2 минуты, чтобы увидеть срабатывание):
   ```ini
   [Unit]
   Description=Daily Vektor queue report

   [Timer]
   OnCalendar=*:0/2
   Unit=vektor-report.service

   [Install]
   WantedBy=timers.target
   ```
   ```bash
   sudo systemd-analyze verify /etc/systemd/system/vektor-report.timer
   sudo systemd-analyze calendar "*:0/2"
   sudo systemctl daemon-reload
   sudo systemctl enable --now vektor-report.timer
   systemctl list-timers vektor-report.timer --no-pager
   ```
   Ожидаемо: в `list-timers` есть строка с вашим таймером, `NEXT` — ближайшая чётная минута, `ACTIVATES` = `vektor-report.service`.

9. **Дождаться срабатывания** (2–3 минуты) и проверить:
   ```bash
   journalctl -u vektor-report.service -b --no-pager | tail -10
   ls -l /srv/vektor/reports
   ```
   Ожидаемо: запись `report written: /srv/vektor/reports/queue-2026-02-10.csv` и сам файл с двумя строками CSV.

10. **Перевести таймер на нормальное расписание 03:00** с защитой от пропусков:
    ```ini
    [Timer]
    OnCalendar=*-*-* 03:00:00
    Persistent=true
    RandomizedDelaySec=5min
    Unit=vektor-report.service
    ```
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl restart vektor-report.timer
    systemd-analyze calendar "*-*-* 03:00:00"
    systemctl list-timers --all --no-pager
    ```
    Ожидаемо: `NEXT` — ближайшие 03:00 (со сдвигом до 5 минут из-за `RandomizedDelaySec`), `LAST` — недавнее тестовое срабатывание.

11. **Включить постоянный журнал и ограничить его размер:**
    ```bash
    journalctl --disk-usage
    ls -ld /var/log/journal || sudo mkdir -p /var/log/journal
    sudo systemd-tmpfiles --create --prefix /var/log/journal
    sudo systemctl restart systemd-journald
    journalctl --disk-usage
    ```
    Ожидаемо: `--disk-usage` печатает размер (десятки–сотни МБ); при необходимости ограничить — в `/etc/systemd/journald.conf` ставим `SystemMaxUse=500M` → `sudo systemctl restart systemd-journald`. Всё записать в `stand-log.md`.

### Проверка

- [ ] `systemctl status vektor-queue.service` → `active (running)`, `Main PID` не пустой.
- [ ] `systemctl cat vektor-queue.service` показывает секцию `Drop-In` и `Environment=VEKTOR_MODE=lab`.
- [ ] В `journalctl -u vektor-queue.service` есть строка `processed: order-1001.json`.
- [ ] `systemctl list-dependencies vektor-queue.service --reverse` содержит `multi-user.target`.
- [ ] `systemctl is-enabled vektor-queue.service vektor-report.timer` → дважды `enabled`.
- [ ] `systemctl list-timers vektor-report.timer` → `NEXT` заполнен, `ACTIVATES` = `vektor-report.service`.
- [ ] В `/srv/vektor/reports` лежит CSV с сегодняшней датой и заголовком `date,in_queue,processed`.
- [ ] `journalctl --disk-usage` выдаёт число, а `ls -ld /var/log/journal` — существующий каталог.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Заявка.** Пользователь: ivanov (бухгалтерия) / ВМ: WIN11-CL01 / Что написал: «Третий день нет утренней выгрузки в папке отчётов. Раньше к 03:05 всегда была, теперь пусто. Закрытие месяца завтра» / Срочность: высокая.

**Как воспроизвести поломку (на UBU01):**

```bash
sudo systemctl disable --now vektor-report.timer
sudo mv /srv/vektor/reports /srv/vektor/reports-old
```

Первое — «коллега пересоздавал юнит и не включил таймер обратно», второе — каталог отчётов переименовали при уборке. Две независимые причины: таймер не стреляет, а если запустить руками — скрипт падает.

**Диагностика по порядку:**

1. Что вообще происходит с таймером:
   ```bash
   systemctl list-timers --all --no-pager | grep vektor
   systemctl status vektor-report.timer --no-pager
   ```
   Видим `Active: inactive (dead)` и `Loaded: ... disabled`. Причина №1 найдена.

2. Что в журнале за сегодня:
   ```bash
   journalctl -u vektor-report.service --since today --no-pager
   ```
   Пусто — сервис сегодня не запускался. Подтверждает, что дело в таймере, а не в скрипте.

3. Проверяем, запускается ли сервис вообще, руками:
   ```bash
   sudo systemctl start vektor-report.service
   systemctl status vektor-report.service --no-pager
   ```
   `Active: failed (Result: exit-code)`, `status=1/FAILURE`. Причина №2.

4. Читаем, что именно сказал скрипт:
   ```bash
   journalctl -u vektor-report.service -n 20 --no-pager -o short-iso
   ```
   В хвосте: `.../queue-report.sh: line 8: /srv/vektor/reports/queue-2026-02-10.csv: No such file or directory`. Каталога нет — вот и корень.

5. Сверяем конфиг с реальностью:
   ```bash
   systemctl cat vektor-report.service vektor-report.timer
   ls -ld /srv/vektor
   ```
   В юните путь `/srv/vektor/reports`, в каталоге — только `reports-old`. Всё сходится.

6. Чиним:
   ```bash
   sudo install -d -o svc-queue -g svc-queue /srv/vektor/reports
   sudo systemctl reset-failed vektor-report.service
   sudo systemctl start vektor-report.service
   sudo systemctl enable --now vektor-report.timer
   systemctl list-timers vektor-report.timer --no-pager
   ls -l /srv/vektor/reports
   ```
   Ожидаемо: сервис `inactive (dead)` с `Result: success` (oneshot так и должен выглядеть), файл появился, `NEXT` — завтра 03:00.

7. Убираем мусор и фиксируем: `sudo rm -rf /srv/vektor/reports-old` (убедившись, что внутри нет нужных файлов), запись в `stand-log.md`.

**Ответ пользователю:** «Иван, добрый день. Разобрались: ночной скрипт формирования выгрузки не запускался — сбилось расписание, плюс пропал каталог, куда он пишет. Расписание восстановили, выгрузку за сегодня сформировали вручную, файл на месте. Завтрашняя придёт штатно к 03:05. Поставили контроль на неуспешные сервисы, чтобы такое ловилось сразу, а не через три дня. Заявку закрываю, если что — пишите.»

**Закрытие заявки:** в резолюции — «таймер `vektor-report.timer` был в состоянии disabled, каталог `/srv/vektor/reports` отсутствовал; восстановлены каталог с правами `svc-queue`, таймер включён, отчёт сформирован вручную, проверено `list-timers`». Отдельной задачей — алерт на `systemctl is-failed` (в уроке про мониторинг).

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** создан системный пользователь `svc-queue` и группа `svc-queue`.
- Каталоги: `/opt/vektor/bin`, `/srv/vektor/queue/incoming`, `/srv/vektor/queue/processed`, `/srv/vektor/reports`.
- Скрипты: `/opt/vektor/bin/queue-worker.sh`, `/opt/vektor/bin/queue-report.sh`.
- Юниты: `/etc/systemd/system/vektor-queue.service` (`enabled`), `/etc/systemd/system/vektor-report.service` (`static`, запускается таймером), `/etc/systemd/system/vektor-report.timer` (`enabled`, 03:00 ежедневно).
- Drop-in: `/etc/systemd/system/vektor-queue.service.d/override.conf` (`Environment=VEKTOR_MODE=lab`, `RestartSec=10s`).
- Журнал systemd переведён в постоянный режим (`/var/log/journal`, лимит `SystemMaxUse=500M` — если ставили).
- Тестовый файл `order-1001.json` перемещён в `processed`; в `/srv/vektor/reports` есть CSV за текущую дату.

### Откат

```bash
sudo systemctl disable --now vektor-queue.service vektor-report.timer
```

Затем в VMware Workstation на UBU01: **VM → Snapshot → Snapshot Manager** → выбрать `before-l2-054` → **Go To** (или **Revert**). ВМ вернётся к состоянию до лабораторной: юнитов, пользователя `svc-queue` и каталогов не будет. Не забудьте после отката записать в `stand-log.md`, что стенд откатан, — иначе через неделю будете думать, что сервис пропал сам.

## 6. Ошибки новичков и безопасность

- **Править юнит и не делать `daemon-reload`.** → systemd работает со старым описанием, вы правите файл и не понимаете, почему «ничего не меняется», а `status` показывает прежнюю строку `ExecStart`. → После любой правки в `/etc/systemd/system` — `sudo systemctl daemon-reload`, потом `restart`.
- **Редактировать файл в `/lib/systemd/system`.** → Изменение исчезнет при обновлении пакета, а до этого будет сюрпризом для коллеги. → Только drop-in через `systemctl edit`.
- **Путать `enable` и `start`.** → Сервис работает до ребута и «внезапно» не поднимается после перезагрузки. Отдельный случай: включили `.service`, а надо было `.timer` — таймер в `disabled`, расписание мертво. → `systemctl enable --now`, а для таймеров включать именно `.timer`.
- **Считать, что `After=network.target` даёт рабочую сеть.** → Скрипт стартует раньше, чем появился адрес, и падает на первом же запросе. → `Wants=network-online.target` + `After=network-online.target` (и включённый `*-wait-online.service`).
- **`Type=simple` для скрипта, который форкается,** или `Type=forking` для обычного цикла. → systemd теряет главный процесс: сервис «активен», хотя внутри уже всё умерло, либо падает сразу после старта. → `simple` — если процесс не уходит в фон; `forking` — только для настоящих демонов с pid-файлом.
- **`Restart=always` без ограничений для падающего сервиса.** → Бесконечный цикл перезапусков, забитый журнал, `start-limit-hit`, а причина так и не найдена. → `on-failure` + `RestartSec`, следить за `NRestarts`, при необходимости `reset-failed` **после** устранения причины.
- **Смотреть только `/var/log/syslog`.** → Там нет части вывода сервисов (особенно если rsyslog настроен иначе), нет кодов выхода и полей. → Первичный источник — `journalctl -u <unit> -b`.
- **Забыть про маску.** → Кто-то сделал `systemctl mask`, потом полдня ищут, почему сервис «не найден» при старте. → `systemctl is-enabled <unit>` покажет `masked`; снимается `systemctl unmask`.

### Безопасность

- Юнит с `User=root` — это полный доступ ко всему серверу для любого бага в скрипте. В бою всегда заводите сервисного пользователя (`User=svc-queue`) и добираете права точечно. На стенде мы так и сделали — это правильный пример, а не упрощение.
- **Не храните пароли в `Environment=` и в тексте юнита.** `systemctl show -p Environment` покажет их любому, у кого есть доступ к D-Bus, а сам файл читается всеми. В бою — `EnvironmentFile=` с правами `600` и владельцем root, либо `systemd-creds`/внешний vault.
- Журнал может содержать чувствительные данные (токены в URL, содержимое ошибок SQL). В бою ограничивайте доступ к `/var/log/journal` и не отключайте `Storage=persistent` «чтобы не текло» без понимания.
- Отключение rate limit журнала (`RateLimitBurst`) глобально ради «чтобы всё писалось» — путь к забитому диску и падению самого журнала. Правильнее — увеличить лимит только проблемному юниту через drop-in.
- Всё, что вы делаете на стенде с `sudo` и отключёнными ограничениями (например, `ProtectSystem=no`), в боевой среде требует согласования. Переход к hardening юнитов (`NoNewPrivileges=`, `ProtectSystem=strict`, `PrivateTmp=`) — тема урока 55.

## 7. Вопросы на собеседовании

**1. Чем `Requires` отличается от `After`? И `Requires` от `Wants`?**

Ответ: `After`/`Before` — только порядок запуска, они не тянут зависимость и не роняют юнит, если та не поднялась. `Requires` — жёсткое требование: не стартовала зависимость — не стартуем мы; при остановке зависимости останавливаемся. `Wants` — мягкое: попытаться запустить, но при неудаче продолжить. На практике для сети используют `Wants=network-online.target` + `After=network-online.target`, потому что жёсткий `Requires` на сеть может заблокировать загрузку при её проблемах.

*Что хочет услышать интервьюер:* чёткое разделение «порядок ≠ зависимость» и понимание, что дефолтные зависимости systemd уже добавляет сам.
*Красный флаг:* «After запускает то, что указано» — это неверно и сразу видно.

**2. Сервис работал, после ребута не поднялся. Ваши действия по шагам?**

Ответ: `systemctl status <unit>` — смотрю `Loaded` (enabled/disabled), `Active`, код выхода; `systemctl is-enabled`; `systemctl list-units --state=failed`; `journalctl -u <unit> -b` — что писал при старте; `systemd-analyze critical-chain <unit>` — не задержала ли его зависимость; `systemctl show -p Wants -p After -p Result`. Если `start-limit-hit` — `reset-failed` после устранения причины. Частые корни: юнит не был `enabled`, монтирование из `fstab` не поднялось (и `local-fs.target` в failed), сеть не дождалась адреса, у сервиса нет прав на каталог после перезагрузки.

*Что хочет услышать интервьюер:* порядок «статус → включён ли → журнал → зависимости», а не «перезапустил и заработало».
*Красный флаг:* сразу `systemctl restart` без чтения журнала — привычка, которая скрывает причину.

**3. Где лежат юнит-файлы и как правильно изменить юнит, поставленный пакетом?**

Ответ: `/lib/systemd/system` (вендор, на RHEL — `/usr/lib/systemd/system`), `/run/systemd/system` (runtime), `/etc/systemd/system` (админ, высший приоритет). Менять вендорный файл напрямую нельзя — перезапишется. Правильно: `systemctl edit <unit>` → drop-in в `/etc/systemd/system/<unit>.d/override.conf`, затем `daemon-reload` и `restart`. Если нужна полная копия — `systemctl edit --full`.

*Что хочет услышать интервьюер:* знание приоритета каталогов и привычки не трогать вендорные файлы.
*Красный флаг:* «правлю в /lib через nano, потом daemon-reload» — так делать нельзя.

**4. Зачем нужны systemd-таймеры, если есть cron? Когда таймер хуже?**

Ответ: таймеры дают журналирование в общий journald, зависимости от других юнитов, монотонные расписания (`OnBootSec`, `OnUnitActiveSec`), `Persistent=true` для догона пропущенных запусков, `RandomizedDelaySec` для размазывания нагрузки по парку, лимиты ресурсов через cgroup и `systemctl status` как единый инструмент. Хуже: синтаксис `OnCalendar` незнаком, отладка через `systemd-analyze calendar`, для простого «каждую минуту дёрнуть curl» cron короче и привычнее, и часть legacy-скриптов исторически на cron.

*Что хочет услышать интервьюер:* понимание, что таймер — не «cron 2.0», а часть системы управления сервисами, и умение назвать конкретные плюсы.
*Красный флаг:* «таймеры всегда лучше, cron устарел» без аргументов.

**5. Сервис падает с `status=203/EXEC`. Что это значит и что проверять?**

Ответ: systemd не смог выполнить `ExecStart`. Причины по частоте: файла нет по указанному пути; путь относительный (в `ExecStart` нужен абсолютный); нет бита исполнения; сломан shebang (`#!/usr/bin/env bash` при отсутствии `/usr/bin/env`); SELinux/AppArmor блокирует запуск; `ExecStart` указывает на каталог. Проверяю `systemctl cat`, `ls -l <путь>`, `file <путь>`, `journalctl -u <unit> -n 30 -x`. Смежные коды: `217/USER` — нет пользователя из `User=`, `200/CHDIR` — нет `WorkingDirectory`.

*Что хочет услышать интервьюер:* что вы читаете код ошибки, а не гадаете, и знаете пару соседних кодов.
*Красный флаг:* «наверное, прав не хватает» без проверки пути и бита исполнения.

## 8. Источники (официальная документация)

- **systemd.io** — раздел «Documentation» / «System and Service Manager»; поисковая фраза: `systemd unit file documentation`.
- **freedesktop.org/wiki/Software/systemd** — архив официальной wiki проекта, разделы про юниты, таймеры и journald; поиск: `systemd journald persistent storage`.
- **Справочные страницы man** (ставятся с пакетом systemd, смотреть локально `man ...`): `systemd.unit(5)`, `systemd.service(5)`, `systemd.timer(5)`, `systemd.exec(5)`, `systemd.kill(5)`, `journalctl(1)`, `journald.conf(5)`, `systemd-analyze(1)`, `systemd-run(1)`. Поиск: `man systemd.timer`.
- **man7.org** — онлайн-копии тех же man-страниц; раздел `System Manager` / `System and Service Manager`; поиск: `man7 systemd.service`.
- **ubuntu.com/server/docs** — раздел про сервис-менеджмент и логи; поиск: `ubuntu server docs systemd journald`.
- **documentation.suse.com / docs.redhat.com** — разделы «Managing systemd services» и «Viewing logs with journalctl» (полезно для RHEL-совместимых сред, с которыми встретитесь в РФ-инфраструктуре); поиск: `redhat managing services with systemctl`.
- Особенности Astra Linux в части systemd и журналирования [ПРОВЕРИТЬ] — смотреть официальную документацию «РусБИТех» / `wiki.astralinux.ru`, раздел про службы и журналы; поиск: `Astra Linux systemctl журнал документация`.
