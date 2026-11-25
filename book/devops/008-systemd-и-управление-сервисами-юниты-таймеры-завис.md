# systemd и управление сервисами: юниты, таймеры, зависимости, journald, sandboxing

## 1. Введение (зачем это нужно в реальной работе)

**Тикет № 4812.** Пользователь: ivanov. ВМ: UBU01. Что написал: «Вчера вы показывали тестовую страничку на 10.10.10.30:8080, сегодня открываю — соединение не устанавливается. Сервер я не выключал, ночью он точно перезагружался по обновлениям». Срочность: средняя.

Ты заходишь на UBU01 и видишь: скрипт запускали руками из терминала вчера, а после перезагрузки его никто не поднял. И это ровно тот момент, когда сисадмин говорит «нужно прописать в автозагрузку», а DevOps говорит «нужен нормальный unit-файл в Git, с рестартом, логами в journald и запретом лезть в /etc».

В Linux 2026 года systemd — это PID 1 почти в любом дистрибутиве: Ubuntu, Debian, Astra Linux, РЕД ОС, ALT. Он запускает все сервисы при загрузке, следит за ними, собирает их логи и умеет изолировать процесс от остальной системы. В DevOps это пересекается со всем: контейнеры в Kubernetes в итоге тоже описываются декларативно и имеют «health», CI-раннеры и агенты ставятся как systemd-сервисы, а таймеры systemd заменяют cron в бэкапах и ротациях. После этой главы ты должен уметь: написать unit с нуля, объяснить разницу между `start` и `enable`, разобраться, почему сервис упал, по `journalctl`, настроить таймер вместо cron и заставить сервис работать от непривилегированного пользователя в песочнице — и всё это положить в Git как воспроизводимый файл.

## 2. Теория «под капотом»

**systemd** — менеджер системы и служб: первый процесс в пространстве пользователя (PID 1), запускается ядром сразу после монтирования корня. Всё остальное в системе он так или иначе порождает сам.

**Юнит (unit)** — единица конфигурации. Типы, которые нужны на старте:

| Тип | Что описывает | Пример |
|-----|---------------|--------|
| `.service` | процесс-демон | `ssh.service`, наш `vektor-api.service` |
| `.timer` | расписание запуска другого юнита | `apt-daily.timer` |
| `.target` | группа/состояние системы | `multi-user.target` |
| `.socket` | сокет с активацией по подключению | `docker.socket` |
| `.mount`, `.path`, `.slice` | монтирование, слежение за файлом, группа по cgroups | — |

**Откуда берутся юниты и кто главнее.** systemd ищет файлы по трём путям, и приоритет именно такой (первый перекрывает второй, второй — третий):

```text
/etc/systemd/system      ← сюда кладём свои юниты и drop-in'ы (высший приоритет)
/run/systemd/system      ← генерируется во время работы (низший из «своих»)
/usr/lib/systemd/system  ← юниты из пакетов; в Ubuntu/Debian это /lib/systemd/system (симлинк)
```

Правило DevOps: **файлы из пакетов не редактируем**. Переопределяем через drop-in в `/etc/systemd/system/<unit>.d/*.conf`, а свои сервисы держим в Git и раскатываем как файл в `/etc/systemd/system/`.

**Структура unit-файла** — три секции: `[Unit]` (описание и зависимости), `[Service]` (как запускать: `Type`, `User`, `ExecStart`, `Restart`, песочница), `[Install]` (`WantedBy=` — в какой target «включиться» при `enable`).

**Type=** — важнейший параметр. `simple` (по умолчанию) — процесс остаётся в foreground, systemd считает сервис запущенным сразу. `forking` — процесс демонизируется сам (так делают старые демоны и nginx). `oneshot` — процесс отработал и завершился, это наш случай для скриптов. `notify` — процесс сам сообщает о готовности через сокет `/run/systemd/notify` (sd_notify). Ошибка в `Type` — самая частая причина «сервис запущен, но systemd думает иначе».

**Зависимости и порядок — это два разных механизма.** `Requires`/`Wants` — *что* подтянуть (Wants — мягко, падение не критично; Requires — жёстко). `After`/`Before` — *в каком порядке* запускать. `After` без `Wants` ничего не запускает, а `Wants` без `After` запускает параллельно. Классика для сетевого сервиса — `Wants=network-online.target` + `After=network-online.target`.

**Таймеры** — планировщик внутри systemd. Бывают `OnCalendar=` (по календарю, как cron) и `OnUnitActiveSec=`/`OnBootSec=` (монотонные, «через 5 минут после старта»). `Persistent=true` догоняет пропущенный запуск после выключения машины, `RandomizedDelaySec=` размазывает нагрузку — то, чего у cron из коробки нет.

**journald** — демон `systemd-journald`, собирает stdout/stderr сервисов, ядро, syslog-сокет `/dev/log` и пишет в бинарный журнал. По умолчанию на Ubuntu `Storage=auto`: если каталога `/var/log/journal` нет — журнал живёт в `/run/log/journal` (в памяти) и пропадает после перезагрузки. Создал каталог — стал постоянным.

**Sandboxing** — набор директив в `[Service]`, которыми systemd собирает для процесса отдельные namespaces, монтирует корень read-only, вырезает capabilities и системные вызовы. Это не «настоящая» изоляция уровня контейнера, но дешёвая и почти бесплатная по трудозатратам защита. Оценку даёт `systemd-analyze security` (шкала 0–10, чем ниже — тем лучше; 9.6 — «голый» сервис, 2–4 — хорошо защищённый).

```text
                 ┌── kernel ──┐
                 │            │
              systemd (PID 1)
                 │  читает /etc/systemd/system/*.unit
                 │
      ┌──────────┼───────────────────────────┐
      │          │                           │
 default.target (что запускать при загрузке)
      │
 multi-user.target
      ├── ssh.service                (пакет)
      ├── vektor-api.service         (наш, After=network-online.target)
      └── cron.service
 timers.target
      ├── vektor-report.timer  ──(сработал)──► vektor-report.service (oneshot)
      └── apt-daily.timer

  stdout/stderr всех юнитов ──► systemd-journald ──► /run/log/journal (RAM)
                                                  └─► /var/log/journal (диск, если создан)
```

Порты тут ни при чём: journald и sd_notify работают через unix-сокеты. Сеть появляется только у самого сервиса — в нашем случае это TCP/8080 на UBU01.

## 3. Аналогия из жизни

Представь диспетчерскую аэропорта. **systemd** — старший диспетчер смены: он не летает сам, но решает, какие рейсы (юниты) сегодня выполняются, в каком порядке и что делать, если борт сломался (перезапустить три раза, потом сдаться и поднять флаг). **Зависимости** — стыковки: багаж не погрузят, пока не прилетел самолёт (`After`), но «нужен самолёт» и «самолёт должен быть раньше» — разные строчки в расписании. **Таймеры** — расписание рейсов: регулярные (каждый день в 03:00) и «через 20 минут после посадки предыдущего». **journald** — бортовой журнал и записи диспетчерской: всё в одном месте, с метками времени и приоритетами, и попробуй найди нужную строку без `-u`. **Sandboxing** — зона досмотра: пилоту выдали ровно тот пропуск, который нужен, и не пускают в серверную.

Где аналогия ломается: диспетчер умеет договариваться и переносить рейс, а systemd — нет. Если unit описан криво, он не «подстроится», а просто убьёт процесс и напишет в журнал код ошибки.

## 4. Практика и команды

Все команды — на **UBU01** (Ubuntu Server 24.04, 10.10.10.30), если не сказано иное.

**1. Посмотреть состояние сервиса.**

```bash
systemctl status ssh --no-pager
systemctl is-active ssh; systemctl is-enabled ssh; systemctl is-failed ssh
```

`status` показывает: активен ли (`active (running)`), с какого момента, PID, основной процесс, последние строки журнала и — самое ценное — код выхода при падении (`status=226/NAMESPACE`, `status=203/EXEC`). `is-active`/`is-enabled`/`is-failed` возвращают машинно-читаемый ответ и код возврата — удобно в скриптах. Смотреть в первую очередь на `Loaded:` (какой файл подхвачен и включён ли) и `Active:`.

**2. Что вообще есть в системе.**

```bash
systemctl list-units --type=service --state=running
systemctl list-unit-files --type=service | grep -E 'enabled|disabled' | head
systemctl --failed
```

`list-units` — что загружено *сейчас*; `list-unit-files` — что *установлено* на диске и включено ли в автозагрузку. `--failed` — первая команда при любом «после перезагрузки не работает».

**3. Посмотреть и переопределить unit из пакета.**

```bash
systemctl cat ssh
sudo systemctl edit ssh          # создаёт /etc/systemd/system/ssh.service.d/override.conf
sudo systemctl edit --full nginx # полная копия unit в /etc
```

`cat` показывает итоговый файл вместе со всеми drop-in'ами и путь `# /lib/systemd/system/ssh.service`. `edit` без `--full` создаёт drop-in — точечное изменение, которое переживёт обновление пакета.

**4. Проверить синтаксис до запуска.**

```bash
systemd-analyze verify /etc/systemd/system/vektor-api.service
```

Парсер укажет на несуществующий `ExecStart=`, опечатку в директиве, неизвестный тип. Делать это **до** `daemon-reload` — экономит минуты.

**5. Управление жизненным циклом.**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vektor-api
sudo systemctl restart vektor-api
sudo systemctl reload-or-restart vektor-api
```

`daemon-reload` — перечитать файлы с диска (после правки unit'а обязателен, иначе «Warning: The unit file, source configuration file or drop-ins changed on disk»). `enable --now` = включить в автозагрузку *и* запустить сразу. `reload` посылает `SIGHUP` (если есть `ExecReload=`) — без обрыва соединений; `restart` убивает и поднимает заново. В боевой среде это разница между «моргнуло» и «уронили».

**6. Зависимости.**

```bash
systemctl list-dependencies vektor-api
systemctl list-dependencies --reverse ssh
systemctl show -p After -p Wants -p Requires vektor-api
```

`--reverse` — кто зависит от юнита (кого уронишь, если перезапустишь). `list-dependencies` показывает дерево до `basic.target` — полезно понять, почему сервис стартует раньше сети.

**7. Логи journald.**

```bash
journalctl -u vektor-api -n 50 --no-pager
journalctl -u vektor-api -f
journalctl -p err -b --since "1 hour ago"
journalctl -o json-pretty -n 1
```

`-u` — по юниту, `-f` — аналог `tail -f`, `-p err` — приоритет (0 emerg … 7 debug), `-b` — только текущая загрузка (`-b -1` — предыдущая), `--since/--until` — окно. `-o json-pretty` отдаёт структурированную запись: `_PID`, `_SYSTEMD_UNIT`, `PRIORITY` — из этого удобно грепать и парсить в CI.

**8. Место журнала и чистка.**

```bash
journalctl --disk-usage
sudo journalctl --vacuum-size=200M
sudo journalctl --vacuum-time=7d
```

Default-лимит в `journald.conf` — `SystemMaxUse=` около 10% диска `[ПРОВЕРИТЬ точное значение в вашей версии]`. На серверах с плотным потоком логов это первый кандидат на переполнение `/var`.

**9. Кто тормозит загрузку.**

```bash
systemd-analyze
systemd-analyze blame | head -10
systemd-analyze critical-chain vektor-api.service
```

`blame` — топ юнитов по времени инициализации, `critical-chain` — реальный путь по зависимостям. Классика: `network-online.target` ждёт DHCP 30 секунд.

**10. Таймеры.**

```bash
systemctl list-timers --all
systemd-analyze calendar '*:0/5'
sudo systemd-run --on-active=30 --unit=check-vektor /bin/true
```

`list-timers` показывает `NEXT`/`LAST`/`PASSED` — когда сработает в следующий раз. `systemd-analyze calendar` валидирует выражение и переводит его в человеческое время — обязательно проверять до продакшена. `systemd-run --on-active` создаёт одноразовый юнит «через 30 секунд» без файлов — удобно для теста.

**11. Оценка защищённости.**

```bash
systemd-analyze security vektor-api.service
```

Таблица выставленных и не выставленных директив с итоговым баллом (`→ Overall exposure level for vektor-api.service: 9.6 UNSAFE`). Цель для своего сервиса — уверенно ниже 5.

**12. Маскировка.**

```bash
sudo systemctl mask cron
systemctl is-enabled cron      # masked
sudo systemctl unmask cron
```

`mask` подменяет юнит симлинком на `/dev/null` — его нельзя запустить даже как зависимость. Это сильнее, чем `disable` (который лишь убирает симлинки автозагрузки).

GUI здесь нет и не будет: systemd управляется только из консоли, `/etc/systemd/system` — обычные текстовые файлы, а значит, они прекрасно живут в Git.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** — Ubuntu Server 24.04 LTS, `10.10.10.30`, доступ по SSH под своей учётной записью с `sudo`, установлен `python3` (идёт в комплекте с Ubuntu Server).
- **WEB01** (`10.10.20.10`, nginx) и **MON01** (`10.10.10.40`, Zabbix) — по желанию, только для чтения: посмотреть `systemctl status nginx` и `systemctl status zabbix-server` как примеры «чужих» юнитов. Ничего там не меняем.
- Windows-часть стенда (DC01, FS01) для этой лабораторной **не нужна** — трек DevOps на неё не опирается.
- Все команды выполняются на UBU01. Учебный пароль стенда — `Lab#2026-vektor`.

### Снапшот

В VMware Workstation для ВМ **UBU01** (и для WEB01/MON01, если будешь туда заходить): меню `VM → Snapshot → Take Snapshot…`, имя — **`before-devops-008`**. Снапшот делается при выключенной или работающей ВМ, но при работающей в него попадёт и содержимое ОЗУ — для этой лабы достаточно дискового состояния, поэтому лучше выключить UBU01 через `sudo poweroff`, снять снапшот, включить.

### Шаги

1. **Разведка.** На UBU01: `systemctl --failed` и `systemd-analyze blame | head -5`. Ожидаемо: `0 loaded units listed`, в blame — `systemd-networkd-wait-online` или `cloud-init`. Запиши время загрузки из `systemd-analyze` — пригодится для сравнения.

2. **Создать системного пользователя под сервис.**

   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin vektor-svc
   id vektor-svc
   ```

   Ожидаемо: `uid=…(vektor-svc) gid=…(vektor-svc) groups=…` без домашнего каталога. Сервис не должен работать от root.

3. **Подготовить каталоги и «приложение».**

   ```bash
   sudo install -d -o root -g root -m 0755 /opt/vektor-api
   sudo install -d -o vektor-svc -g vektor-svc -m 0750 /var/lib/vektor-api
   echo '<h1>Vektor API test service — lesson 08</h1>' | sudo tee /opt/vektor-api/index.html
   ```

   Ожидаемо: `/opt/vektor-api/index.html` доступен на чтение всем, `/var/lib/vektor-api` — только владельцу (там будут логи отчёта).

4. **Написать unit.**

   ```bash
   sudo tee /etc/systemd/system/vektor-api.service >/dev/null <<'EOF'
   [Unit]
   Description=Vektor API test service (Ubuntu 24.04, lesson 08)
   After=network-online.target
   Wants=network-online.target
   StartLimitIntervalSec=30
   StartLimitBurst=5

   [Service]
   Type=simple
   User=vektor-svc
   Group=vektor-svc
   WorkingDirectory=/opt/vektor-api
   ExecStart=/usr/bin/python3 -m http.server 8080 --bind 10.10.10.30 --directory /opt/vektor-api
   Restart=on-failure
   RestartSec=3
   StandardOutput=journal
   StandardError=journal
   NoNewPrivileges=yes
   PrivateTmp=yes
   PrivateDevices=yes
   ProtectSystem=strict
   ProtectHome=yes
   ProtectKernelTunables=yes
   ProtectKernelModules=yes
   ProtectControlGroups=yes
   RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
   ReadWritePaths=/var/lib/vektor-api
   CapabilityBoundingSet=
   LockPersonality=yes

   [Install]
   WantedBy=multi-user.target
   EOF
   ```

   Обрати внимание: `StartLimit*` живут в `[Unit]`, а не в `[Service]` — частая ошибка.

5. **Проверить синтаксис и запустить.**

   ```bash
   systemd-analyze verify /etc/systemd/system/vektor-api.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now vektor-api
   systemctl status vektor-api --no-pager
   ```

   Ожидаемо: `Loaded: loaded (/etc/systemd/system/vektor-api.service; enabled; preset: enabled)`, `Active: active (running)`, в логе — `Serving HTTP on 10.10.10.30 port 8080`.

6. **Проверить снаружи.**

   ```bash
   curl -sS http://10.10.10.30:8080/ ; ss -tlnp | grep 8080
   ```

   Ожидаемо: HTML-заголовок из шага 3. `ss` покажет слушающий сокет, но `-p` для чужого процесса без root имени не покажет — это нормально.

7. **Проверить рестарт по падению.**

   ```bash
   sudo systemctl show -p MainPID vektor-api
   sudo kill -9 $(systemctl show -p MainPID --value vektor-api)
   sleep 6; systemctl status vektor-api --no-pager
   ```

   Ожидаемо: в журнале `Main process exited, code=killed, status=9/KILL`, затем `Scheduled restart job, restart counter is at 1`, сервис снова `active (running)`, PID другой. `Restart=on-failure` не срабатывает на `systemctl stop` — это разные вещи и это правильно.

8. **Логи.**

   ```bash
   journalctl -u vektor-api -n 30 --no-pager
   journalctl -u vektor-api -f      # Ctrl+C для выхода
   ```

   Ожидаемо: все строки с меткой `vektor-api` и `/usr/bin/python3`. Убедись, что сообщения видны без `StandardOutput=journal` ты бы их тоже увидел — journald перехватывает stdout/stderr по умолчанию.

9. **Таймер: скрипт отчёта.**

   ```bash
   sudo tee /usr/local/bin/vektor-report.sh >/dev/null <<'EOF'
   #!/usr/bin/env bash
   set -euo pipefail
   ts="$(date -Is)"
   up="$(cut -d. -f1 /proc/uptime)"
   use="$(df -h / | awk 'NR==2{print $5}')"
   printf '%s uptime=%ss disk_root=%s\n' "$ts" "$up" "$use" >> /var/lib/vektor-api/report.log
   EOF
   sudo chmod 0755 /usr/local/bin/vektor-report.sh
   ```

10. **Юнит и таймер отчёта.**

    ```bash
    sudo tee /etc/systemd/system/vektor-report.service >/dev/null <<'EOF'
    [Unit]
    Description=Vektor stand report (oneshot)

    [Service]
    Type=oneshot
    User=vektor-svc
    Group=vektor-svc
    ExecStart=/usr/local/bin/vektor-report.sh
    NoNewPrivileges=yes
    PrivateTmp=yes
    ProtectSystem=strict
    ProtectHome=yes
    ReadWritePaths=/var/lib/vektor-api
    EOF

    sudo tee /etc/systemd/system/vektor-report.timer >/dev/null <<'EOF'
    [Unit]
    Description=Run vektor-report every 5 minutes

    [Timer]
    OnCalendar=*:0/5
    Persistent=true
    RandomizedDelaySec=20

    [Install]
    WantedBy=timers.target
    EOF

    systemd-analyze calendar '*:0/5'
    sudo systemctl daemon-reload
    sudo systemctl enable --now vektor-report.timer
    sudo systemctl start vektor-report.service
    systemctl list-timers vektor-report.timer --no-pager
    sudo cat /var/lib/vektor-api/report.log
    ```

    Ожидаемо: `systemd-analyze calendar` печатает `Normalized form: *-*-* *:00/5:00`, `list-timers` показывает `NEXT` в пределах 5 минут, а `report.log` уже содержит одну строку после ручного `start`.

11. **Зависимости и оценка песочницы.**

    ```bash
    systemctl list-dependencies vektor-api --no-pager | head -20
    systemd-analyze security vektor-api.service --no-pager | tail -5
    ```

    Ожидаемо: в дереве есть `network-online.target` и `basic.target`; итоговый балл у нашего unit'а — примерно `4.x` и `OK`/`MEDIUM` (если балл всё ещё `9.x` — проверь, что `daemon-reload` прошёл и файл действительно тот).

12. **Зафиксировать в журнале стенда.** Дописать в `stand-log.md` изменения (см. следующий подраздел).

### Проверка

- [ ] `systemctl is-enabled vektor-api` → `enabled`, `systemctl is-active vektor-api` → `active`
- [ ] `curl -sS http://10.10.10.30:8080/` отдаёт HTML из `/opt/vektor-api/index.html`
- [ ] `ps -o user,pid,cmd -C python3` показывает пользователя `vektor-svc`, не `root`
- [ ] после `kill -9` сервис поднимается сам, в `journalctl` есть `Scheduled restart job`
- [ ] `systemctl list-timers vektor-report.timer` показывает `NEXT` и корректный `ACTIVATES`
- [ ] `/var/lib/vektor-api/report.log` пополняется минимум одной строкой за 5 минут
- [ ] `systemd-analyze security vektor-api.service` даёт балл ниже 5
- [ ] `journalctl -u vektor-api -b` возвращает записи за текущую загрузку
- [ ] `systemd-analyze verify` не выдаёт ошибок
- [ ] в `stand-log.md` появилась запись о новой лабораторной

### Сломай и почини

**Заявка.** Пользователь: ivanov / ВМ: UBU01 / Что написал: «Тестовая страничка на 8080 опять не открывается, вчера всё работало. Мне к 15:00 нужно показать». / Срочность: высокая.

**Как воспроизвести поломку** (делаем сами, чтобы потренироваться):

```bash
sudo rm -rf /var/lib/vektor-api
sudo systemctl restart vektor-api
systemctl status vektor-api --no-pager
```

Сервис уходит в `failed` со `status=226/NAMESPACE`, в журнале — `Failed to set up mount namespacing: No such file or directory`.

**Диагностика по порядку:**

1. `curl -sS -m 3 http://10.10.10.30:8080/` — убедиться, что проблема не на стороне клиента: `Connection refused` = сервис не слушает.
2. `systemctl --failed` и `systemctl status vektor-api --no-pager` — читаем `Active: failed` и код выхода. Код — главная улика.
3. `journalctl -u vektor-api -n 40 --no-pager` — ищем первую ошибку после `Started`. Здесь это `mount namespacing`.
4. `systemctl cat vektor-api` — смотрим, какие директивы реально применены (не забыл ли кто-то drop-in).
5. `ls -ld /var/lib/vektor-api` — каталога нет. Директива `ReadWritePaths=` требует, чтобы путь **существовал на момент старта**, иначе systemd не может собрать mount namespace → `226/NAMESPACE`. Это не баг, это документированное поведение.
6. `systemd-analyze verify /etc/systemd/system/vektor-api.service` — для порядка, синтаксис тут в норме.

**Решение:**

```bash
sudo install -d -o vektor-svc -g vektor-svc -m 0750 /var/lib/vektor-api
sudo systemctl restart vektor-api
systemctl is-active vektor-api          # active
curl -sS http://10.10.10.30:8080/       # HTML
```

**Ответ пользователю:** «Иван, добрый день. Страничка снова доступна: http://10.10.10.30:8080/. Причина — пропал служебный каталог, сервис не мог стартовать в изолированном окружении. Восстановил каталог, сервис поднялся и теперь запускается автоматически при перезагрузке. Если снова упадёт — я получу уведомление, но на всякий случай пишите в тикет.»

**Закрытие заявки:** в комментарии — код ошибки (`226/NAMESPACE`), причина, действие, проверка (`curl` 200), и предложение: добавить в GitLab CI джобу `systemd-analyze verify` для всех unit-файлов и пост-деплой проверку `curl`-ом. Именно это отличает DevOps-ответ от «перезапустил, работает».

### Что изменилось на стенде

- UBU01: создан системный пользователь `vektor-svc` (`--system`, без home, shell `nologin`).
- UBU01: каталоги `/opt/vektor-api` (root:root 0755, файл `index.html`) и `/var/lib/vektor-api` (vektor-svc:vektor-svc 0750, файл `report.log`).
- UBU01: юниты `/etc/systemd/system/vektor-api.service`, `/etc/systemd/system/vektor-report.service`, `/etc/systemd/system/vektor-report.timer`; первый и третий — `enabled`.
- UBU01: скрипт `/usr/local/bin/vektor-report.sh`.
- UBU01: TCP/8080 слушает сервис `vektor-api`, доступен с `10.10.10.10`/`10.10.10.20`-сегмента.
- UBU01: таймер `vektor-report.timer` пишет строку в `/var/lib/vektor-api/report.log` каждые 5 минут.
- Снапшот `before-devops-008` существует на UBU01.

### Откат

- Частичный (только эта лаба): `sudo systemctl disable --now vektor-api vektor-report.timer`, затем `sudo rm /etc/systemd/system/vektor-api.service /etc/systemd/system/vektor-report.service /etc/systemd/system/vektor-report.timer /usr/local/bin/vektor-report.sh`, `sudo rm -rf /opt/vektor-api /var/lib/vektor-api`, `sudo systemctl daemon-reload`, `sudo systemctl reset-failed`, `sudo userdel vektor-svc`.
- Полный: выключить UBU01, в VMware Workstation `VM → Snapshot → Snapshot Manager`, выбрать `before-devops-008` → `Go to` (или `Revert`). Учесть, что откатятся и все изменения стенда, сделанные после снапшота.
- После отката — запись в `stand-log.md` об откате с причиной.

## 6. Ошибки новичков и безопасность

- **Забыть `daemon-reload` после правки unit.** → systemd продолжает работать со старой версией в памяти, ты правишь файл и не понимаешь, почему ничего не меняется. → Всегда `sudo systemctl daemon-reload` сразу после создания/правки файла, и только потом `restart`.
- **Путать `start` и `enable`.** → `start` поднимает сейчас, но после перезагрузки сервис не встанет (наш исходный тикет ровно об этом). → Проверка: `systemctl is-enabled <unit>` должно быть `enabled`; в лабе используем `enable --now`.
- **Редактировать `/lib/systemd/system/*.service` напрямую.** → Изменения молча затираются при обновлении пакета, и в самый неподходящий момент сервис возвращается к дефолту. → Только `systemctl edit` (drop-in) или копия в `/etc/systemd/system`.
- **Сервис от root «потому что так работает».** → Любая уязвимость в приложении = полный контроль над машиной. → `User=`/`Group=` с системным пользователем, `CapabilityBoundingSet=`, `NoNewPrivileges=yes`.
- **`Restart=always` с `Type=oneshot` или с сервисом, падающим мгновенно.** → Бесконечный цикл рестартов, забитый журнал, нагрузка на CPU. → `Restart=on-failure` + `RestartSec=` + `StartLimitIntervalSec`/`StartLimitBurst` в `[Unit]`.
- **Логи в файл `/var/log/app.log` через `>>` в `ExecStart`.** → Нет ротации, диск переполняется, логи не связаны с юнитом. → Писать в stdout/stderr (`StandardOutput=journal` по умолчанию), а journald настроить через `SystemMaxUse=` и `journalctl --vacuum-*`.
- **Неверный `Type=`.** → `Type=forking` для скрипта, который не форкается: systemd ждёт родителя и через таймаут объявляет `failed`, хотя процесс жив. → `simple` для foreground-процессов, `oneshot` для скриптов, `notify` только если приложение реально умеет sd_notify.
- **Ставить `OnCalendar` в UTC, думая, что это локальное время.** → Отчёт уходит не в 03:00, а в 06:00 по Москве. → Проверять `systemd-analyze calendar` и `timedatectl`; для «календарных» задач, которые должны догоняться после выключения, — `Persistent=true`.

### Безопасность

В боевой среде нельзя: оставлять сервис под root и с полным набором capabilities; держать секреты в `Environment=SECRET=...` в unit-файле (`systemctl show` и `/proc/<pid>/environ` их покажут любому с доступом); отключать hardening-директивы «потому что приложение не стартует», не разобравшись, — почти всегда это значит, что приложение лезет туда, куда ему не надо. Правильно: секреты — в `EnvironmentFile=` с правами `0600` и владельцем-сервисом либо через `LoadCredential=`; изоляция — по принципу «сначала `ProtectSystem=strict` + `ReadWritePaths=` на минимум, потом добавляем послабления по одному»; аудит — `systemd-analyze security` в CI как регрессионный тест (упал балл ниже порога — пайплайн красный). И помни про учебный пароль `Lab#2026-vektor` — на реальном стенде так нельзя, пароли не хранятся в открытом виде в заметках и unit-файлах.

## 7. Вопросы на собеседовании

**1. Чем `systemctl enable` отличается от `systemctl start` и что делает `daemon-reload`?**

`start` — запустить юнит прямо сейчас, в текущей сессии. `enable` — создать симлинки в `.wants`/`.requires` целевого target'а, чтобы юнит запускался при загрузке; текущее состояние процесса это не меняет. `daemon-reload` — перечитать с диска все unit-файлы, пересобрать граф зависимостей; нужен после любой правки файлов, иначе systemd работает со старым кэшем в памяти. `daemon-reload` не перезапускает сервисы — это отдельный `restart`. На практике чаще всего `systemctl enable --now <unit>`.

*Что хочет услышать интервьюер:* чёткое разделение «состояние автозагрузки» vs «состояние процесса» vs «кэш конфигурации» и понимание, что после правки unit нужны два действия, а не одно.

*Красный флаг:* «enable запускает сервис», «daemon-reload перезапускает сервисы», путаница между `restart` и `reload`.

**2. Как systemd определяет порядок запуска? Чем `Wants` отличается от `After`?**

Это два ортогональных механизма. `Wants=`/`Requires=` — про *притягивание*: какие юниты нужно запустить вместе с этим (Wants — мягко, ошибка не фатальна; Requires — жёстко, падение роняет зависимый). `After=`/`Before=` — про *порядок*: кто раньше, если оба уже в очереди. `After` сам никого не запускает. Классическая связка для сетевого сервиса: `Wants=network-online.target` + `After=network-online.target`. Проверять — `systemctl list-dependencies` и `systemctl show -p After -p Wants`.

*Что хочет услышать интервьюер:* понимание, что systemd запускает максимально параллельно и порядок задаётся явно; что `Requires` без `After` даёт гонку; что `network.target` ≠ `network-online.target`.

*Красный флаг:* «After= запускает сервис до», «Requires и After — одно и то же», путаница между `target` и `service`.

**3. Зачем нужны systemd-таймеры, если есть cron?**

Плюсы: логи и код возврата каждого запуска автоматически в journald (`journalctl -u foo.service`); зависимости и тот же механизм песочницы, что у сервисов; `Persistent=true` догоняет пропущенный запуск после простоя; `RandomizedDelaySec=` размазывает нагрузку по парку машин; монотонные расписания (`OnBootSec=`, `OnUnitActiveSec=`) вместо «календарного», которым cron не умеет; таймеры видны в `systemctl list-timers`. Минусы: чуть больше файлов (нужны `.timer` + `.service`) и менее привычный синтаксис. Граница: если у команды уже стандарт на cron и Ansible-роль — ломать его ради принципа не нужно.

*Что хочет услышать интервьюер:* не «cron — прошлый век», а конкретные эксплуатационные преимущества: логирование, зависимости, persistent, рандомизация — и понимание, когда миграция не оправдана.

*Красный флаг:* «cron небезопасен» без объяснений, незнание `Persistent=true`, уверенность, что таймер сам по себе запускает команду (нужен парный `.service`).

**4. Сервис из пакета надо немного изменить. Как это сделать правильно и почему не через правку файла в `/usr/lib/systemd/system`?**

Через drop-in: `sudo systemctl edit <unit>` создаёт `/etc/systemd/system/<unit>.d/override.conf`, который накладывается поверх пакетного файла; при обновлении пакета твои изменения сохранятся. Правка `/usr/lib/systemd/system/*` затирается при обновлении и делает состояние системы невоспроизводимым. Проверить итог — `systemctl cat <unit>` (показывает все фрагменты по порядку) и `systemctl show`. После — `daemon-reload` и `restart`. Если нужно переопределить `ExecStart=` — в drop-in сначала пустая строка `ExecStart=`, потом новое значение (списки в systemd аккумулируются).

*Что хочет услышать интервьюер:* знание приоритета путей `/etc` > `/run` > `/usr/lib`, понимание drop-in и того, что список директив вроде `ExecStart` требует сброса пустой строкой.

*Красный флаг:* «правлю файл в /lib и делаю бэкап», незнание `systemctl cat`, попытка сделать `systemctl edit` без последующего `daemon-reload`.

**5. Как ограничить сервис и как убедиться, что ограничения работают?**

Базовый набор: `User=`/`Group=`, `NoNewPrivileges=yes`, `ProtectSystem=strict` + `ReadWritePaths=` на минимум, `ProtectHome=yes`, `PrivateTmp=yes`, `PrivateDevices=yes`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`, `ProtectControlGroups=yes`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `CapabilityBoundingSet=` (пусто), `LockPersonality=yes`, при возможности `DynamicUser=yes`. Проверка — `systemd-analyze security <unit>`: балл и построчный список того, что не выставлено; цель — уверенно ниже 5. Второй уровень проверки — реальный тест: сервис должен стартовать, писать только в разрешённый каталог и падать при попытке записи в `/etc`. В CI это превращается в регрессионную проверку.

*Что хочет услышать интервьюер:* системный подход «наименьшие привилегии → измеримая метрика → тест», а не заученный список директив; понимание, что `ProtectSystem=strict` требует `ReadWritePaths` для существующих каталогов.

*Красный флаг:* «sandbox в systemd не работает, нужен Docker», отключение ограничений при первой ошибке вместо разбора причины, отсутствие метрики/проверки.

## 8. Источники (официальная документация)

- **freedesktop.org** — официальная документация проекта systemd (раздел systemd man pages). Статьи: `systemd.unit`, `systemd.service`, `systemd.timer`, `systemd.exec` (там же все директивы sandboxing), `systemd.time` (синтаксис `OnCalendar`), `journald.conf`. Поиск: `systemd.service man page`, `systemd.exec sandboxing directives`.
- **systemd.io** — сайт проекта systemd, разделы про юниты и `systemd-analyze`. Поиск: `systemd.io units`.
- **ubuntu.com** — документация Ubuntu Server, раздел про управление службами и systemd `[ПРОВЕРИТЬ точное название раздела для 24.04]`. Поиск: `Ubuntu Server docs systemd service management`.
- **docs.redhat.com** — Red Hat Enterprise Linux, раздел «Managing systemd» / «Configuring basic system settings»: хорошо структурированное описание типов юнитов, таймеров и drop-in `[ПРОВЕРИТЬ актуальное название раздела]`. Поиск: `RHEL managing systemd units`.
- **wiki.archlinux.org** — статья `systemd` и `systemd/Timers`: не вендорская, но очень точная и с примерами; полезна как справочник по синтаксису таймеров. Поиск: `Arch Wiki systemd timers`.
- **man7.org** — зеркало man-страниц Linux, удобно для `systemd.*`, `journalctl`, `systemctl`. Поиск: `man7 journalctl`.
- Локально на UBU01 всегда под рукой: `man systemd.service`, `man systemd.timer`, `man systemd.exec`, `man journalctl`, `man systemd-analyze`.

Все ссылки даны на домены и названия разделов; конкретные URL не приводятся намеренно — структура сайтов меняется, а поиск по указанным фразам находит актуальную страницу за один шаг.
