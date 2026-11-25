# Планировщики и оркестрация задач: Task Scheduler, cron, Jenkins/GitLab CI для рутины

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **Что написал:** «Каждое утро захожу на FS01 смотреть, сколько осталось места, и вручную чищу старые логи. Я бухгалтер, мне это не нужно. И вчера в `\\FS01\Buh` не сохранился файл — написало, что нет места. Сделайте, чтобы отчёт приходил сам и диск не забивался.»
> **Срочность:** средняя

Вот это и есть 90% работы L2: не «починить то, что сломалось», а «сделать так, чтобы это работало само и чтобы я узнал, если перестанет». Люди не должны быть таймером. Планировщик — это способ превратить регламент («проверить место», «сделать отчёт», «очистить логи», «запустить бэкап») в работающий механизм.

После этой главы ты должен уметь: ставить задачи в Windows Task Scheduler и в Linux cron/systemd timers, задавать им правильные права и окружение, читать логи их запусков, ловить ситуацию «задача есть, а результата нет», строить связку из нескольких заданий на нескольких серверах и понимать, в какой момент рутина перестаёт жить в планировщике и переезжает в CI (GitLab CI/Jenkins).

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Планировщик — это локальный сервис ОС (демон), который держит список заданий и по расписанию запускает новые процессы. Сам он по сети не ходит: задача = «описание того, что запустить, от чьего имени, когда и что делать с результатом».

**Windows Task Scheduler** — служба `Schedule` (отображаемое имя «Планировщик задач»), API через `taskschd.msc` / `schtasks.exe` / PowerShell-модуль `ScheduledTasks`. Хранит задания в `C:\Windows\System32\Tasks` (файлы XML) + реестр-индекс, описывает их в XML-определении:

```text
Task (задание)
├─ Triggers   — когда запускать (время, событие, вход, старт, простой)
├─ Actions    — что делать (exe/скрипт + аргументы, рабочий каталог)
├─ Principal  — от кого (учётка, уровень прав, тип входа: Password/S4U/Interactive)
├─ Settings   — таймаут, «не запускать второй экземпляр», «догнать пропущенное»
└─ Conditions — только при питании от сети, только при простое и т.п.
```

Ключевой момент: `Principal` определяет, в какой **сессии** (логин-сессии Windows) пойдёт задача. `Interactive` = только когда пользователь залогинен; `Password` = сохранённый пароль, работает без входа, имеет доступ к сети; `S4U` = без пароля, но **без доступа к сети**. Это источник половины «странных» падений.

**Linux cron** — демон `cron` (на Ubuntu 24.04 юнит `cron.service`) читает: пользовательские таблицы `crontab -e` (в `/var/spool/cron/crontabs/<user>`), `/etc/crontab`, `/etc/cron.d/*` и каталоги `/etc/cron.{hourly,daily,weekly,monthly}` (через `run-parts` и anacron). Плюс **systemd timers** — современная замена: юнит `.timer` в паре с `.service`, календарное выражение `OnCalendar`, `Persistent=true` (догнать пропущенное), интеграция с `journalctl`.

```text
cron:      демон ──> читает таблицы ──> при совпадении времени ──> fork/exec
           окружение МИНИМАЛЬНОЕ: PATH=/usr/bin:/bin, нет твоего .bashrc, нет маппленных дисков

systemd:   .timer (когда) ──> .service (что) ──> journald (лог и код возврата)
           + Persistent=true, + OnFailure=, + служебные зависимости
```

**Оркестрация (CI)** — следующий уровень: задача не «на этой машине по расписанию», а «в репозитории лежит описание пайплайна, сервер исполнения (runner/agent) забирает его и выполняет пошагово на нужной машине». CI = cron + Git + централизованный запуск + артефакты + история. GitLab CI и Jenkins — как раз такие серверы-исполнители.

```text
Git (репозиторий с .gitlab-ci.yml / Jenkinsfile)
      │  trigger: schedule / push / webhook
      ▼
CI-сервер (GitLab/Jenkins) ──> runner/agent ──> steps по порядку, exit code !== 0 = упало
      │                                    └──> artifacts, логи, уведомление
      ▼
уведомления (e-mail, Telegram, Zabbix)
```

Порты: Task Scheduler — RPC (135/TCP + динамика), GUI/`schtasks` к другой машине требует прав; CI-сервер обычно 80/443, агенты — своим транспортом (у Jenkins JNLP 50000/TCP [ПРОВЕРИТЬ], у GitLab Runner — HTTPS с проверкой задания).

## 3. Аналогия из жизни

Планировщик — это как будильник с запиской. Ты один раз ставишь будильник на 07:00 и кладёшь рядом записку «приготовить отчёт». Проснулся телефон — ты машинально сделал то, что в записке. Хочешь, чтобы это работало без тебя — нужен не будильник, а сотрудник: будильник будит не тебя, а сотрудника (учётная запись в задаче), у сотрудника должен быть ключ от серверной (права и пароль), и он должен уметь отчитаться «сделал» (код возврата, лог), иначе ты до вечера не узнаешь, что он проспал.

CI — это уже не будильник, а диспетчер: у него есть список заданий на день, он раздаёт их исполнителям, следит, что каждый вернул «готово», и если кто-то не вернул — звонит тебе.

Аналогия ломается в одном: будильник не умеет запускать второй экземпляр задачи, пока первый ещё бежит, а планировщик — умеет, и это надо специально запрещать.

## 4. Практика и команды

### 4.1. `schtasks` — быстро посмотреть и поставить задачу (Windows)

```cmd
schtasks /query /tn "Vektor\FS01-DiskReport" /v /fo LIST
schtasks /create /tn "Vektor\FS01-DiskReport" /tr "powershell.exe -NoProfile -File C:\Scripts\Vektor\DiskReport.ps1" /sc daily /st 07:00 /ru "VEKTOR\adm.student" /rp "Lab#2026-vektor" /rl HIGHEST
```

`/tn` — имя (слеш в имени создаёт папку задачи), `/tr` — что запускаем, `/sc daily` — тип расписания, `/st` — время старта, `/ru`/`/rp` — от кого и пароль, `/rl HIGHEST` — с повышенными правами. Дальше: `schtasks /run /tn "..."` — запустить сейчас, `/end` — прибить, `/change` — изменить, `/delete /tn "..." /f` — удалить. Смотреть надо `Статус`, `Время последнего запуска`, `Результат последнего запуска`.

> Безопасность: `/rp` в командной строке виден в списке процессов и остаётся в истории консоли. На стенде пароль учебный, в бою используй `Register-ScheduledTask` с `-Password` в виде `SecureString` или беспарольный `S4U`.

### 4.2. Регистрация задачи через PowerShell (правильный способ)

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
           -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Scripts\Vektor\DiskReport.ps1'
$trigger = New-ScheduledTaskTrigger -Daily -At 07:00
$set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
           -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
           -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$princ   = New-ScheduledTaskPrincipal -UserId "VEKTOR\adm.student" -LogonType Password -RunLevel Highest
Register-ScheduledTask -TaskName "FS01-DiskReport" -TaskPath "\Vektor\" -Action $action `
           -Trigger $trigger -Settings $set -Principal $princ -User "VEKTOR\adm.student" -Password "Lab#2026-vektor"
```

Ключевые параметры: `-StartWhenAvailable` (догнать пропущенный запуск, если ВМ была выключена), `-ExecutionTimeLimit` (по умолчанию задача висит до 3 суток — ставь свой лимит), `-MultipleInstances IgnoreNew` (не запускать второй экземпляр поверх первого), `-RunLevel Highest` (UAC не блокирует действия с правами админа).

### 4.3. Что реально произошло: `Get-ScheduledTaskInfo`

```powershell
Get-ScheduledTask -TaskPath "\Vektor\" | Select-Object TaskName, State
Get-ScheduledTask -TaskPath "\Vektor\" | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
```

```text
TaskName       LastRunTime          LastTaskResult   NextRunTime
FS01-DiskRe... 05.03.2026 07:00:05  0                06.03.2026 07:00:00
FS01-Cleanup   28.02.2026 07:00:01  267011           06.03.2026 07:00:00
```

Смотри `LastTaskResult`: `0` — успех; `267011` (`0x41303`) — «задача ещё не запускалась» [ПРОВЕРИТЬ по справке]; `267009` (`0x41301`) — выполняется прямо сейчас. `NumberOfMissedRuns` > 0 — задача не догнала пропуски (не включён `-StartWhenAvailable`).

### 4.4. Журнал Task Scheduler

GUI: Event Viewer → Applications and Services Logs → Microsoft → Windows → TaskScheduler → Operational → **Enable Log** (по умолчанию выключен!).

```powershell
wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
Get-WinEvent -LogName Microsoft-Windows-TaskScheduler/Operational -MaxEvents 30 |
    Select-Object TimeCreated, Id, Message
```

Там видно, что задача стартовала, что действие завершилось и с каким кодом, и что задача была завершена по таймауту. Точные номера событий сверяй в справке [ПРОВЕРИТЬ].

### 4.5. `cron`: своя таблица (Linux)

```bash
crontab -l                 # посмотреть
crontab -e                 # редактировать (открывается редактор)
sudo crontab -u adm.student -l
```

Формат строки: `минута час день_месяца месяц день_недели команда`.

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
*/30 * * * * /usr/bin/flock -n /var/lock/vektor-health.lock /opt/vektor/scripts/health.sh >/dev/null 2>&1
```

`*/30` — каждые 30 минут. `MAILTO=""` — не заваливать локальную почту. `flock -n` — взять блокировку или не запускаться: защита от наложения запусков, если скрипт идёт дольше интервала.

### 4.6. `cron`: системные файлы и `/etc/cron.d`

```bash
sudo tee /etc/cron.d/vektor-health >/dev/null <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
*/30 * * * * adm.student /usr/bin/flock -n /var/lock/vektor-health.lock /opt/vektor/scripts/health.sh
EOF
sudo chmod 644 /etc/cron.d/vektor-health
```

В `/etc/cron.d` есть **поле пользователя** (в личном crontab его нет — там ты и есть пользователь). Файл должен принадлежать root и быть недоступен на запись группе/остальным, иначе cron его проигнорирует. В имени лучше без точек [ПРОВЕРИТЬ].

### 4.7. Где логи cron на Ubuntu 24.04

```bash
journalctl -u cron --since today
journalctl -u cron | grep -i vektor
```

Если в системе стоит rsyslog, дублируется в `/var/log/syslog` (строки с меткой `CRON`) [ПРОВЕРИТЬ состав пакетов UBU01]. Именно здесь видно «(CRON) info (No MTA installed, discarding output)» — сигнал, что вывод задачи ушёл в никуда.

### 4.8. systemd timer вместо cron

```ini
# /etc/systemd/system/vektor-report.service
[Unit]
Description=Vektor weekly report
[Service]
Type=oneshot
User=adm.student
ExecStart=/opt/vektor/scripts/weekly_report.sh
```

```ini
# /etc/systemd/system/vektor-report.timer
[Unit]
Description=Run Vektor weekly report

[Timer]
OnCalendar=Mon *-*-* 07:30:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vektor-report.timer
systemctl list-timers --all
systemd-analyze calendar "Mon *-*-* 07:30:00"
sudo systemctl start vektor-report.service   # проверить руками
journalctl -u vektor-report.service -n 50
```

`Persistent=true` — догнать пропущенный запуск после выключения, `RandomizedDelaySec` — размазать нагрузку. Плюсы против cron: лог в journald с кодом возврата, зависимости, `OnFailure=`, точный статус.

### 4.9. Проверка кода возврата — фундамент всей автоматизации

```bash
/opt/vektor/scripts/health.sh; echo "exit=$?"
```

```powershell
& C:\Scripts\Vektor\DiskReport.ps1
$LASTEXITCODE
```

`0` — успех, всё остальное — провал. Задача, которая «отработала», но вернула `1`, для планировщика успешна (он-то запустил процесс!), а для тебя — нет. Поэтому скрипты пишутся с `set -euo pipefail` (bash) и `$ErrorActionPreference='Stop'` + `exit 1` (PowerShell), а не «как получится».

### 4.10. CI-описание: как та же рутина выглядит в GitLab CI и Jenkins

```yaml
# .gitlab-ci.yml (фрагмент; реальный запуск — на GIT01, DevOps-трек)
stages: [check, report]

disk-check:
  stage: check
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
  script:
    - /opt/vektor/scripts/health.sh
  artifacts:
    when: always
    paths: [out/]
  retry: 1
  timeout: 30m
```

```groovy
// Jenkinsfile (декларативный пайплайн)
pipeline {
  agent any
  triggers { cron('H 7 * * 1-5') }
  options { timeout(time: 30, unit: 'MINUTES') }
  stages {
    stage('Check')  { steps { sh '/opt/vektor/scripts/health.sh' } }
    stage('Report') { steps { sh '/opt/vektor/scripts/report.sh' } }
  }
  post { failure { echo 'Упало — уведомить L2' } }
}
```

Маппинг на нашу рутину: `stages` = порядок шагов, `exit code` шага = провал пайплайна, `artifacts` = то, что нужно сохранить, `schedule`/`cron('H ...')` = аналог crontab, `H` в Jenkins — хеш, чтобы задания не стартовали все в одну минуту. Расписания GitLab задаются в UI: **Build → Pipeline schedules**.

### 4.11. «Репетиция пайплайна» без CI-сервера

```bash
#!/usr/bin/env bash
set -euo pipefail
STAGE="init"
trap 'echo "[$(date -Is)] FAILED at stage: $STAGE" | tee -a /opt/vektor/logs/pipeline.log; exit 1' ERR
STAGE="check";  /opt/vektor/scripts/health.sh
STAGE="report"; /opt/vektor/scripts/weekly_report.sh
echo "[$(date -Is)] OK"
```

Так ты получаешь поведение CI (этапы, падение на первом провале, лог) уже на L2, а на DevOps-треке тот же скрипт просто вызывается из пайплайна.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 в полном составе: DC01 (10.10.10.10, AD/DNS/DHCP), FS01 (10.10.10.20, файлы и печать, папки `\\FS01\Obshchee`, `\\FS01\Buh`, `\\FS01\HR`), UBU01 (10.10.10.30, SSH), MON01 (10.10.10.40, Zabbix), WIN11-CL01 и WIN10-CL02 в домене. Нужны: доменная учётка `adm.student` (админ), папка `\\FS01\Obshchee\Reports` (создать в шаге 1), скрипт из урока 82 (отчёт по неактивным учёткам) и навык работы с Ansible из урока 86.

GitLab CE и Jenkins на стенде L2 **нет** — машина GIT01 (10.10.10.50) появляется только на DevOps-треке. Поэтому CI в этой лабе разбирается как чтение конфигурации и локальная репетиция пайплайна (шаг 11); настоящий прогон `.gitlab-ci.yml` будет на GIT01 позже.

### Снапшот

На всех затрагиваемых ВМ — DC01, FS01, UBU01, MON01:

VMware Workstation → правый клик по ВМ → **Snapshot → Take Snapshot** → имя `before-l2-087` → описание «до лабы 87, планировщики».

### Шаги

1. **FS01.** Создать каталоги и папку отчётов, выдать права:
   ```powershell
   New-Item -ItemType Directory -Path C:\Scripts\Vektor -Force
   New-Item -ItemType Directory -Path \\FS01\Obshchee\Reports -Force
   ```
   Ожидаемо: папка `Reports` видна в `\\FS01\Obshchee` у всех. Скопировать туда две короткие версии скриптов: `DiskReport.ps1` (свободное место `Get-Volume` + топ-5 крупных файлов в логах → CSV в `\\FS01\Obshchee\Reports`) и `Cleanup.ps1` (удалить файлы старше 30 дней в `C:\Logs\Vektor`, в конце `exit 0`).

2. **FS01.** Проверить скрипты руками, до планировщика:
   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Scripts\Vektor\DiskReport.ps1
   Get-ChildItem \\FS01\Obshchee\Reports | Sort-Object LastWriteTime -Descending | Select-Object -First 3
   ```
   Ожидаемо: CSV появился, время свежее. Если не работает руками — в планировщик нести рано.

3. **FS01.** Включить журнал планировщика: `wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true`. Ожидаемо: `Get-WinEvent -LogName Microsoft-Windows-TaskScheduler/Operational -MaxEvents 1` не ругается.

4. **FS01.** Зарегистрировать задачу `Vektor\FS01-DiskReport` командами из п. 4.2 (ежедневно 07:00, `-RunLevel Highest`, пароль `Lab#2026-vektor`). Проверить: `Get-ScheduledTask -TaskPath "\Vektor\"` → `State = Ready`.

5. **FS01.** Прогнать задачу вручную и снять результат:
   ```powershell
   Start-ScheduledTask -TaskName "FS01-DiskReport" -TaskPath "\Vektor\"
   Start-Sleep 15
   Get-ScheduledTask -TaskPath "\Vektor\" | Get-ScheduledTaskInfo | Select TaskName, LastRunTime, LastTaskResult
   ```
   Ожидаемо: `LastTaskResult = 0`, в `\\FS01\Obshchee\Reports` новый файл.

6. **FS01.** Второй задачей `Vektor\FS01-Cleanup` поставить очистку логов: ежедневно 06:45, `-MultipleInstances IgnoreNew`, `-ExecutionTimeLimit (New-TimeSpan -Minutes 30)`. Ожидаемо: две задачи в папке `\Vektor\`, у второй `State = Ready`.

7. **DC01.** Задача `Vektor\DC01-InactiveUsers`: раз в неделю (понедельник 07:15) запускать отчёт по неактивным учёткам из урока 82, экспорт в `\\FS01\Obshchee\Reports\inactive-<дата>.csv`. Зарегистрировать через `Register-ScheduledTask`, `Start-ScheduledTask`, убедиться, что CSV появился и `LastTaskResult = 0`. Важно: отчёт пишется **по UNC-пути**, а не на маппленный диск.

8. **UBU01.** Скрипт мониторинга `/opt/vektor/scripts/health.sh`: проверяет доступность `10.10.10.10:389`, `10.10.10.20:445`, `10.10.10.40:10050` (`nc -z -w2`), пишет строку в `/opt/vektor/logs/health.log` и возвращает `exit 1`, если хоть одна проверка провалилась. Сделать `sudo chown root:root`, `chmod 755`, прогнать руками: `echo $?` → `0`.

9. **UBU01.** Поставить cron-задание каждые 30 минут через `/etc/cron.d/vektor-health` (п. 4.6) с `flock`. Проверить через 2–3 минуты: `journalctl -u cron --since "10 min ago"` содержит запуск, в `health.log` появились новые строки.

10. **UBU01.** Поставить systemd timer `vektor-report.timer` на неделю (п. 4.8) для скрипта `/opt/vektor/scripts/weekly_report.sh` (собирает факты: сколько отчётов в `/mnt/obshchee/Reports`, статус health за 7 дней). Смонтировать шару: `sudo apt install -y cifs-utils` и
    ```bash
    sudo mount -t cifs //FS01/Obshchee /mnt/obshchee -o username=adm.student,vers=3.0
    ```
    Проверить: `systemctl list-timers --all` показывает `vektor-report.timer` с датой следующего запуска; `sudo systemctl start vektor-report.service` → `systemctl status` = `Result=success`.

11. **UBU01.** Прочитать `.gitlab-ci.yml` и `Jenkinsfile` из п. 4.10, выписать в `stand-log.md` соответствие: наш `health.sh` ↔ stage `Check`, `weekly_report.sh` ↔ stage `Report`, cron 30 мин ↔ `cron('H 7 * * 1-5')`/Pipeline schedules. Затем создать `/opt/vektor/scripts/pipeline-local.sh` (п. 4.11), сломать этап `report` (временно передать несуществующий путь) и убедиться, что скрипт падает на нужном этапе с понятным сообщением и `exit 1`.

12. **UBU01/FS01.** Собрать «сводку дежурного»: скрипт `check-jobs.sh` проверяет, что в `/mnt/obshchee/Reports` есть файлы за последние 24 часа и что в `health.log` нет строк `FAIL` за сутки; на Windows — `Get-ScheduledTask -TaskPath "\Vektor\" | Get-ScheduledTaskInfo` и фильтр `LastTaskResult -ne 0`. Записать в `stand-log.md`: список задач, время, машина, отчёт, SLO «должно быть каждые N часов».

### Проверка

- [ ] `Get-ScheduledTask -TaskPath "\Vektor\"` на FS01 и DC01 показывает 3 задачи в состоянии `Ready`.
- [ ] У каждой задачи `LastTaskResult = 0`, `LastRunTime` — свежий, `NumberOfMissedRuns = 0`.
- [ ] В `\\FS01\Obshchee\Reports` лежат отчёты: `disk-*.csv`, `inactive-*.csv`.
- [ ] `journalctl -u cron` на UBU01 показывает запуски `health.sh` не реже раза в 30 минут.
- [ ] `systemctl list-timers --all` показывает `vektor-report.timer`, а `journalctl -u vektor-report.service` — успешный прогон.
- [ ] `pipeline-local.sh` при рабочей конфигурации возвращает `0` и пишет `OK`, при сломанном этапе — `FAILED at stage: report` и `exit 1`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **ВМ:** FS01
> **Что написал:** «Опять не могу сохранить файл в `\\FS01\Buh` — пишет, что на диске нет места. Отчёт за ночь мне в папку тоже не пришёл. Срочность: высокая, закрываем месяц.»
> **Срочность:** высокая

**Как воспроизвести поломку.** На FS01 в оснастке `taskschd.msc` открыть `Task Scheduler Library → Vektor → FS01-Cleanup` → Properties → General → переключить радиокнопку на **Run only when user is logged on** (или, что то же самое, `Set-ScheduledTask` с `-LogonType Interactive`). Затем создать мусор, чтобы диск действительно забился: `fsutil file createnew C:\Logs\Vektor\junk.dat 3000000000` (осторожно с размером; достаточно забить свободное место на 90%+). Завершить сеанс `adm.student` на FS01. Задача, требующая интерактивного входа, больше не запускается.

**Диагностика по шагам.**

1. Подтвердить симптом: `Get-Volume C | Select DriveLetter, SizeRemaining, Size` — места почти нет. Проблема пользователя реальна, но это следствие.
2. `Get-ScheduledTask -TaskPath "\Vektor\" | Get-ScheduledTaskInfo` → у `FS01-Cleanup` `LastRunTime` — несколько дней назад, `LastTaskResult = 267011` (`0x41303`, «ещё не запускалась») [ПРОВЕРИТЬ].
3. `Get-ScheduledTask -TaskName "FS01-Cleanup" | Select -Expand Principal | Format-List UserId, LogonType` → `LogonType: Interactive`. Вот причина.
4. Проверить, что сам скрипт живой: `Start-ScheduledTask` (в интерактивной сессии под `adm.student`) → `LastTaskResult = 0`, файлы чистятся.
5. Посмотреть журнал `Microsoft-Windows-TaskScheduler/Operational` за период — есть события «задача не запущена, пользователь не залогинен».
6. Smoke-тест альтернативы: `schtasks /change /tn "Vektor\FS01-Cleanup" /ru "VEKTOR\adm.student" /rp "Lab#2026-vektor"` и убедиться без входа в систему, что задача отработала.

**Решение.** Вернуть `-LogonType Password` (`Set-ScheduledTask` с новым `Principal`), сразу прогнать `Start-ScheduledTask` и проверить `LastTaskResult = 0` и освободившееся место. Дополнительно: добавить в `Cleanup.ps1` порог «удалять, если свободно < 15%», и поставить на UBU01 в `check-jobs.sh` проверку `LastTaskResult` с уведомлением — чтобы такое ловилось не через месяц, а на следующее утро.

> В бою: задача очистки должна бежать под сервисной учёткой с минимально нужными правами и с мониторингом результата. Учётка `adm.student` с паролем в задаче — только для стенда.

**Ответ пользователю:** «Иван, добрый день. Диск освободили, сохранять в `\\FS01\Buh` снова можно. Причина: ночная задача очистки перестала запускаться после изменения настроек — она была переключена в режим "только при входе пользователя", а на сервере вход никто не выполняет. Вернули режим "выполнять без входа", прогнали вручную, отчёт за ночь доложен в `\\FS01\Obshchee\Reports`. Дополнительно поставили ежедневную проверку, что задача отработала — если снова не запустится, мы узнаем в тот же день, а не через месяц.»

**Закрытие заявки.** Категория «Файловый сервер / Дисковое пространство», корневая причина — «изменение настроек задачи (LogonType)», решение — «восстановлен Principal + добавлен контроль результата», привязка к статье базы знаний «Регламентные задачи FS01».

### Что изменилось на стенде

- FS01: каталог `C:\Scripts\Vektor\{DiskReport.ps1, Cleanup.ps1}`; папка `\\FS01\Obshchee\Reports`; задачи `Vektor\FS01-DiskReport` (ежедневно 07:00) и `Vektor\FS01-Cleanup` (ежедневно 06:45), обе под `VEKTOR\adm.student`; включён журнал `Microsoft-Windows-TaskScheduler/Operational`.
- DC01: задача `Vektor\DC01-InactiveUsers` (еженедельно, понедельник 07:15), пишет CSV в `\\FS01\Obshchee\Reports`.
- UBU01: `/opt/vektor/scripts/{health.sh, weekly_report.sh, pipeline-local.sh, check-jobs.sh}`; `/opt/vektor/logs/`; `/etc/cron.d/vektor-health`; юниты `vektor-report.service` и `vektor-report.timer`; установлен `cifs-utils`, шара FS01 смонтирована в `/mnt/obshchee` (без записи в `/etc/fstab` — до следующей лабы).
- Никаких новых машин, адресов и учёток не появилось.

### Откат

VMware Workstation → выбрать ВМ (FS01, DC01, UBU01, MON01) → **Snapshot Manager** → `before-l2-087` → **Restore**. После отката включить ВМ и проверить `Get-ScheduledTask -TaskPath "\Vektor\"` (должно быть пусто) и `ls /etc/cron.d/vektor-health` (файла нет). Перед откатом закрыть все сеансы к затронутым серверам, а в `stand-log.md` дописать строку «откат к before-l2-087, причина».

## 6. Ошибки новичков и безопасность

1. **Задача в режиме «только при входе пользователя»** → не запускается никогда, потому что на сервере никто не логинится → `-LogonType Password` или `S4U` (если сеть не нужна) и контроль `LastTaskResult`.
2. **Сохранённый пароль + его смена** → задача молча падает с `0x8007052E` [ПРОВЕРИТЬ] → сервисная учётка с неистекающим паролем и отдельная запись в базе знаний, плюс алерт по коду возврата.
3. **Маппленный диск или относительный путь** → в неинтерактивной сессии диска `Z:` не существует, `C:\Scripts` — это не тот каталог → всегда абсолютные пути и UNC.
4. **Результат никто не проверяет** → «задача есть» ≠ «задача работает». Нужен сторожевой скрипт и алерт (Zabbix на MON01, Telegram из урока 84).
5. **Наложение запусков** → два экземпляра пишут в один файл, получаешь битый CSV → `-MultipleInstances IgnoreNew` и `flock -n` в cron.
6. **Тяжёлая задача в 09:00** → конкуренция с бэкапом и с рабочим днём пользователей → окна обслуживания, `RandomizedDelaySec`.
7. **Секреты в аргументах и в crontab** → видны в `ps aux`, в истории, в логах CI → переменные окружения с ограниченными правами, защищённые/маскированные переменные CI.
8. **Нет ротации логов заданий** → сама автоматизация забивает диск → `logrotate` на Linux, `-NoClobber`/ротация в PowerShell, контроль размера в `DiskReport.ps1`.

### Безопасность

Планировщик — это механизм **постоянного выполнения кода**, поэтому он же любимый инструмент закрепления в системе (MITRE ATT&CK T1053, Scheduled Task/Job). В боевой среде: не хранить пароли в тексте задач и скриптов, не давать задачам `Highest`/root «на всякий случай», ограничить, кто имеет право менять `C:\Windows\System32\Tasks` и `/etc/cron.d` (иначе любой админ станет «вечным» админом), включать аудит создания/изменения задач (4624/4698/4699 в Security-журнале [ПРОВЕРИТЬ]), проверять новые задачи в рамках реагирования на инцидент (урок 79). В CI: секреты — только в защищённых и маскированных переменных, runner — не на машине с прод-доступами без ограничений, пайплайны из форков не получают секретов.

## 7. Вопросы на собеседовании

**1. Задача Task Scheduler отработала успешно, но отчёта нет. Твои действия?**
Ответ: начать с `Get-ScheduledTaskInfo` — `LastRunTime` и `LastTaskResult`; если `0`, смотреть, что именно сделал скрипт: рабочий каталог, относительные пути, права учётки Principal, доступность UNC-шары, ExecutionPolicy и то, что скрипт не «проглотил» ошибку (`exit 0` при `try/catch`). Дальше — журнал TaskScheduler/Operational и собственный лог скрипта. Вывод: код возврата 0 у планировщика означает только «процесс запустился», а не «работа сделана правильно».
*Что хочет услышать:* понимание разницы «задача запустилась» и «задача сделала работу», плюс конкретный порядок проверки.
*Красный флаг:* «пересоздам задачу» без диагностики и «поставлю запуск каждую минуту».

**2. Скрипт работает руками, а из cron — нет. Почему?**
Ответ: cron запускает процесс в минимальном окружении: другой `PATH`, нет `.bashrc`, нет текущего каталога, нет переменных, нет MTA для вывода. Скрипт находит бинарник в интерактиве (например, `ansible` из `/usr/local/bin`) и не находит в cron. Лечится: полные пути к бинарникам, `PATH=` в crontab, `cd` в начало скрипта, перенаправление вывода в лог, `set -euo pipefail`, проверка через `journalctl -u cron`. Отдельный классический случай — незаэкранированный `%` в crontab.
*Что хочет услышать:* знание про окружение cron и умение воспроизводить условия cron вручную (`env -i /bin/bash скрипт`).
*Красный флаг:* «cron глючит» и «перезапущу cron».

**3. cron или systemd timer — что выберешь и почему?**
Ответ: timer даёт лог в journald с кодом возврата, `Persistent=true` (догнать пропущенное), зависимости, `OnFailure=`, `RandomizedDelaySec`, точное управление через `systemctl`. cron проще и привычнее, работает везде, удобен для пользовательских таблиц. На Ubuntu 24.04 для новых задач я выберу timer, а cron оставлю там, где он уже используется или где нужна совместимость с другими дистрибутивами.
*Что хочет услышать:* осознанный выбор с критериями, а не «cron старый».
*Красный флаг:* «timer — это только для systemd-сервисов» или неспособность назвать `OnCalendar`/`Persistent`.

**4. Чем CI (GitLab CI/Jenkins) лучше cron для рутины и когда его брать?**
Ответ: CI даёт версионирование описания задачи в Git, ревью изменений, централизованный запуск на нескольких машинах, артефакты, ретраи, таймауты, историю прогонов и уведомления «из коробки». Брать его стоит, когда заданий много, они меняются, их надо запускать на разных серверах или связывать в цепочки. Для одиночной задачи «чистить логи на FS01» CI — оверкилл: лишняя точка отказа и сетевая зависимость.
*Что хочет услышать:* «cron — локально и просто, CI — когда нужны Git, ревью, несколько сред и артефакты», без религии.
*Красный флаг:* «CI всегда лучше» или «CI — это только для разработчиков».

**5. Как ты убедишься, что регламент из десяти задач реально выполняется?**
Ответ: единая точка контроля — сторожевой скрипт, который проверяет не «задача включена», а «есть свежий результат»: возраст файлов-артефактов, `LastTaskResult` у всех задач в `\Vektor\`, свежие строки в логах cron/timer. Результат — метрика в Zabbix на MON01 с триггером и уведомлением. Плюс регламент с окнами обслуживания и запись в базе знаний.
*Что хочет услышать:* контроль по результату, а не по факту наличия задачи; мониторинг и алертинг.
*Красный флаг:* «раз в месяц захожу и смотрю глазами».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — разделы про Task Scheduler: «Task Scheduler», «schtasks commands», «ScheduledTasks module» (PowerShell). Поиск: `schtasks create syntax`, `Register-ScheduledTask example`, `New-ScheduledTaskSettingsSet`.
- **learn.microsoft.com** — «Task Scheduler Operational log», коды возврата планировщика. Поиск: `Task Scheduler operational log event ids` `[ПРОВЕРИТЬ]`.
- **man7.org** — `crontab(5)` и `cron(8)`. Поиск: `crontab 5 man page`.
- **freedesktop.org** — `systemd.timer`, `systemd.time` (синтаксис `OnCalendar`). Поиск: `systemd.timer man page OnCalendar`.
- **docs.gitlab.com** — «CI/CD pipelines», «Pipeline schedules», «`.gitlab-ci.yml` reference». Поиск: `gitlab ci yml reference`, `gitlab pipeline schedules`.
- **jenkins.io/doc** — «Pipeline», «Pipeline Syntax», «cron trigger syntax». Поиск: `jenkins declarative pipeline triggers cron`.
- **attack.mitre.org** — техника T1053 «Scheduled Task/Job» (зачем планировщик интересен атакующим). Поиск: `MITRE ATT&CK T1053`.
