# Bash и Python для системного администратора: автоматизация, API, мониторинг, парсинг логов

## 1. Введение (зачем это нужно в реальной работе)

Тикет в HelpDesk «Вектора»:

> **От:** ivanov (Бухгалтерия, WIN11-CL01)
> **Текст:** «Добрый день. Мне третий раз за неделю приходит письмо от Zabbix про ошибку авторизации на каком-то сервере, а что это значит — не понимаю. И ещё вопрос: вы можете сделать так, чтобы каждый понедельник мне приходил отчёт по журналу входов?»
> **Срочность:** низкая

Заявка бытовая, но она ровно про эту главу. Смотри: письма шлёт Zabbix — значит кто-то (ты) должен превратить сырой лог в цифру, которую можно мониторить. Отчёт по понедельникам — значит нужен скрипт плюс таймер. А «что это значит» — значит в отчёте должен быть человеческий текст, а не дамп журнала.

Чистый PowerShell у тебя уже есть из урока 72, но он не покрывает Linux-часть стенда: UBU01, MON01, WEB01, GW01. Там живут bash и Python. Кроме того, любой серьёзный продукт (Zabbix, GitLab, Proxmox, 1С через HTTP-сервисы) даёт REST API, и работать с ним удобнее из Python, чем из curl-однострочников.

После этой главы ты должен уметь: написать bash-скрипт, который не падает на первой же ошибке; распарсить лог journald и выдать CSV; сходить в REST API чужого продукта с токеном и таймаутом; оформить проверку так, чтобы её подхватил мониторинг; и поставить всё это на расписание systemd-таймером, а не «кроном из 1998-го».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Любая задача вида «посмотреть, посчитать, сообщить» раскладывается на пять слоёв. Держи схему — дальше мы просто будем ходить по ней слева направо.

```text
 ИСТОЧНИКИ              СБОР                    ТРАНСПОРТ             ХРАНЕНИЕ / АНАЛИЗ        ДЕЙСТВИЕ
┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌─────────────┐
│ journald     │   │ bash-скрипт      │   │ локально (pipe)  │   │ Zabbix + БД      │   │ триггер     │
│ auth.log     │──►│ python-скрипт    │──►│ HTTPS 443 (JSON) │──►│ CSV в /var/log   │──►│ письмо/     │
│ Zabbix API   │   │ на UBU01         │   │ SSH 22           │   │ stdout           │   │ Telegram    │
│ Proxmox API  │   │ (cron/timer)     │   │ (stdin/stdout)   │   │ лог скрипта      │   │ тикет в ITSM│
└──────────────┘   └──────────────────┘   └──────────────────┘   └──────────────────┘   └─────────────┘
```

**Bash** — это интерпретатор командной строки и одновременно язык, где всё есть текст. Скрипт запускается процессом-интерпретатором по shebang (`#!/usr/bin/env bash`), получает три стандартных потока (stdin/stdout/stderr) и возвращает **код возврата** — целое число от 0 до 255, где 0 значит «успех», а всё остальное — ошибка. Код возврата — это то, на чём держится и `set -e`, и проверки в мониторинге, и логика `if` в bash.

**Python** — полноценный язык общего назначения с батарейками: работа с JSON, HTTP, регулярками, CSV и SSH-библиотеками уже почти из коробки. Ключевая разница: bash склеивает чужие команды, Python делает работу сам, внутри процесса.

**Уровни, к которым это относится.** Всё это — прикладной уровень (L7 в модели OSI) плюс работа с локальными файлами. Сетевого «своего» протокола тут нет: когда скрипт идёт в API Zabbix, он говорит по HTTP/HTTPS с JSON-RPC (это обёртка над JSON — тело запроса содержит имя метода и параметры). Когда идёт по SSH — по протоколу SSH с ключом или паролем.

**Где живут логи в Linux.** У современных систем два механизма параллельно:
- **journald** — бинарный журнал systemd. Смотреть через `journalctl`, поля структурированные, есть JSON-вывод.
- **Syslog-файлы** (`/var/log/auth.log`, `/var/log/syslog`) — текстовые, через rsyslog. На Ubuntu Server 24.04 `auth.log` по умолчанию присутствует `[ПРОВЕРИТЬ: зависит от состава пакетов]`, но правильнее не завязываться только на него.

Прелесть journald в том, что он даёт JSON: `journalctl -o json` возвращает по объекту на строку, и это можно читать через `jq` или `json.loads()`.

**Расписание.** Два варианта: классический `cron` (минимальное окружение, `PATH` отличается от интерактивного — реальная и частая причина «у меня руками работает, а из крона нет») и **systemd-таймеры** (юнит `.timer` + `.service`, журналирование через `journalctl -u`, зависимости от сети, `Persistent=true` — догонит пропущенный запуск после выключения ВМ). На стенде с ВМ, которые ты регулярно гасишь, `Persistent=true` — то, что нужно.

**Мониторинг: как скрипт становится проверкой.** Классический протокол плагинов Nagios (и совместимых: Icinga, Zabbix agent-плагины) — это просто «текст в stdout + код возврата»: 0 = OK, 1 = WARNING, 2 = CRITICAL, 3 = UNKNOWN. Zabbix с **внешними скриптами** и `UserParameter` устроен иначе: ненулевой код возврата = элемент «Not supported» (ошибка сбора), а значение берётся из stdout. Об этом — в разделе 6, это классическая ловушка `[ПРОВЕРИТЬ актуальное поведение своей версии Zabbix]`.

**API — что надо знать минимум.** REST — это когда метод HTTP (GET/POST/PUT/DELETE) и URL говорят, что сделать, а тело — JSON. Аутентификация: либо заголовок `Authorization: Bearer <токен>`, либо сессионный логин-метод (у Zabbix это `user.login`, но токены удобнее и переживают перезапуск `[ПРОВЕРИТЬ статус user.login в твоей версии]`). Обязательно: **таймаут** на любой запрос и **ограничение частоты** (rate limit) на стороне сервера.

## 3. Аналогия из жизни

Представь ремонтную бригаду. **Bash** — это мастер с мультитулом: пришёл, посмотрел, подтянул гайку, прикрутил полку. Быстро, дёшево, инструмент в кармане. Ошибся — просто переделал. **Python** — это инженер, который сначала рисует чертёж, потом заказывает деталь на станке: дольше, дороже, зато деталь можно повторить сто раз и она будет одинаковой, а не «примерно такой».

Дальше: **API** — это окошко приёмной, куда ты подаёшь бланк (JSON) и получаешь ответ по форме; а **логи** — это вахтенный журнал, куда всё записывают от руки, и твоя задача — научиться выдёргивать из него нужные строки, не перечитывая весь журнал.

Аналогия перестаёт работать в одном месте: bash и Python — не два разных «человека», они прекрасно работают вместе, один скрипт вызывает другой, и ремонтная бригада тут же превращается в отдел из двух сотрудников, которые друг друга подменяют.

## 4. Практика и команды

Ниже — минимальный рабочий набор приёмов. Все примеры — для стенда «Вектор».

**1. Строгий режим bash.** Заголовок любого скрипта, который будет работать без присмотра.

```bash
#!/usr/bin/env bash
set -euo pipefail      # -e: падать на ошибке; -u: падать на необъявленной переменной;
                       # -o pipefail: пайплайн падает, если упала любая его часть
IFS=$'\n\t'
```

Без этого скрипт «проглотит» ошибку и отработает как успех. Это худшее, что бывает с автоматизацией.

**2. Разбор аргументов через `getopts`.**

```bash
DAYS=1; TOP=5
while getopts "d:t:" opt; do
  case "$opt" in
    d) DAYS="$OPTARG" ;;
    t) TOP="$OPTARG" ;;
    *) echo "usage: $0 [-d days] [-t top]" >&2; exit 64 ;;
  esac
done
```

`OPTARG` — значение флага. `>&2` — вывод в stderr, чтобы ошибка не попала в stdout как «данные». Ненулевой код выхода — обязателен.

**3. Замок, чтобы не запустилось два экземпляра.**

```bash
exec 9>/var/lock/vektor-auth-report.lock
flock -n 9 || { echo "already running" >&2; exit 0; }
```

`&&` здесь не годится — `flock` с `-n` возвращает ошибку, если замок занят. Такой паттерн спасает от «два отчёта одновременно и оба пишут в один CSV».

**4. Парсинг journald без JSON — быстро и грубо.**

```bash
journalctl --since "-1 days" -u ssh -o cat | grep -c 'Failed password' || true
```

`-u ssh` — только SSH-демон, `-o cat` — печатать лишь текст сообщения (без даты и хоста, удобно парсить). `|| true` — потому что `grep -c` при нуле совпадений вернёт код 1, а с `set -e` скрипт умрёт.

**5. Топ IP по неудачным входам — awk, sort, uniq.**

```bash
journalctl --since "-1 days" -u ssh -o cat \
  | awk '/Failed password/ {for(i=1;i<=NF;i++) if($i=="from") print $(i+1)}' \
  | sort | uniq -c | sort -rn | head -n 5
```

Типичный вывод:
```text
     14 10.10.10.100
      3 10.10.10.31
```
`awk` ищет слово `from` и печатает следующее поле — адрес. Смотреть: если в топе адреса из твоей же подсети 10.10.10.0/24 — это почти наверняка забытый ключ у коллеги, а не атака.

**6. journald как JSON + `jq`.**

```bash
journalctl -u ssh --since "-24h" -o json \
  | jq -r 'select(.MESSAGE | test("Failed password")) | ._HOSTNAME + " " + .MESSAGE' | head
```

Поля JSON — с подчёркиванием (`_HOSTNAME`, `_PID`, `SYSLOG_IDENTIFIER`) `[ПРОВЕРИТЬ набор полей на своей версии systemd]`. `jq -r` — без кавычек вокруг строк.

**7. REST API через curl (Zabbix на MON01).**

```bash
curl -s -m 10 'http://10.10.10.40/api_jsonrpc.php' \
  -H 'Content-Type: application/json-rpc' \
  -H "Authorization: Bearer $ZBX_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}'
```

Ответ: `{"jsonrpc":"2.0","result":"7.0.0","id":1}` `[ПРОВЕРИТЬ версию]`. `-m 10` — таймаут 10 секунд. Без таймаута скрипт может висеть вечно.

**8. Изолированное окружение Python (PEP 668).**

```bash
sudo apt install -y python3-venv
python3 -m venv /opt/vektor/venv
/opt/vektor/venv/bin/pip install -q requests
```

На Ubuntu 24.04 `pip install` в системный Python запрещён («externally-managed-environment»). Ставить `--break-system-packages` — плохая привычка, которая однажды сломает систему. Только venv.

**9. HTTP-запрос из Python с таймаутом и повтором.**

```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
session.mount("http://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])))
r = session.post(API, json=payload, timeout=(3.05, 10),
                 headers={"Authorization": f"Bearer {token}"})
r.raise_for_status()
data = r.json()
```

`timeout=(connect, read)` — два числа: сколько ждать соединения и сколько ответа. `raise_for_status()` превращает HTTP-ошибку в исключение, чтобы она не прошла молча.

**10. Логирование вместо `print`.**

```python
import logging
logging.basicConfig(
    filename="/var/log/vektor/zabbix-report.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
logging.info("collected %d problems", len(problems))
```

Плюс: у Python-скрипта, работающего по таймеру, `stdout` уходит в journal, а свой файл удобнее читать и ротировать `logrotate`. Только не забудь про ротацию — иначе через год скрипт съест диск.

**11. Юниты для расписания.**

```ini
# /etc/systemd/system/vektor-report.service
[Unit]
Description=Vektor daily security report
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=student
EnvironmentFile=/etc/vektor/report.env
ExecStart=/opt/vektor/scripts/run_daily.sh
```

```ini
# /etc/systemd/system/vektor-report.timer
[Unit]
Description=Run Vektor daily report at 07:00

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vektor-report.timer
systemd-analyze calendar "*-*-* 07:00:00"   # проверить, что выражение понято
systemctl list-timers --all | grep vektor
```

**12. Проверка скриптов линтером до запуска.**

```bash
sudo apt install -y shellcheck
shellcheck /opt/vektor/scripts/auth_report.sh
```

```bash
/opt/vektor/venv/bin/pip install -q ruff
/opt/vektor/venv/bin/ruff check /opt/vektor/scripts/zabbix_report.py
```

`shellcheck` ловит неэкранированные переменные, неверные сравнения и `for f in $(ls)` — те ошибки, которые руками искать больно.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — доступна, SSH пускает, есть `sudo`. Это наш «сервер автоматизации».
- **MON01** (10.10.10.40, Zabbix из трека L2) — веб-интерфейс открывается, есть учётная запись администратора Zabbix.
- **DC01, FS01** — не нужны в этой лабораторной (к ним вернёмся в уроке 75 про Ansible).
- **PVE01** — не нужен, но API у него есть (`https://10.10.10.70:8006/api2/json`) `[ПРОВЕРИТЬ]`, это хорошее продолжение самостоятельного задания.
- На UBU01 заведена твоя учётная запись — в примерах она называется `student`. Если в твоём стенде имя другое, подставь своё.

Если Zabbix ещё не поднят — сначала пройди соответствующий шаг трека L2, без API лабораторная не имеет смысла.

### Снапшот

В VMware Workstation на **UBU01** и **MON01**: `VM → Snapshot → Take Snapshot`, имя `before-sysadmin-073`.

```text
vmrun -T ws snapshot "C:\VMs\UBU01\UBU01.vmx" before-sysadmin-073
vmrun -T ws snapshot "C:\VMs\MON01\MON01.vmx" before-sysadmin-073
```

Путь к `vmrun` — `C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe`, вайртуальные пути подставь свои `[ПРОВЕРИТЬ]`.

### Шаги

1. **UBU01.** Создаём каталоги и права.
   ```bash
   sudo mkdir -p /opt/vektor/scripts /var/log/vektor /etc/vektor
   sudo chown -R student:student /opt/vektor/scripts /var/log/vektor
   sudo chmod 750 /var/log/vektor /etc/vektor
   ```
   Ожидаемый результат: каталоги есть, владелец — `student`.

2. **UBU01.** Добавляем себя в группу чтения журнала, иначе `journalctl` покажет только свои процессы.
   ```bash
   sudo usermod -aG systemd-journal student
   newgrp systemd-journal
   ```
   Проверка: `journalctl -u ssh -n 3` выводит строки без ошибки доступа.

3. **UBU01.** Создаём `/opt/vektor/scripts/auth_report.sh` с заголовком из приёма 1, разбором аргументов (приём 2), замком (приём 3) и подсчётом (приёмы 4–5). В конце — запись CSV:
   ```bash
   printf '%s,%s\n' "$(date -Iseconds)" "$FAILS" >> /var/log/vektor/auth-failures.csv
   ```
   Результат: файл сохранён, `chmod +x` стоит.

4. **UBU01.** Прогоняем линтер и запускаем руками.
   ```bash
   shellcheck /opt/vektor/scripts/auth_report.sh
   /opt/vektor/scripts/auth_report.sh -d 2 -t 3
   cat /var/log/vektor/auth-failures.csv
   ```
   Ожидаемый результат: строки CSV с ISO-датой и числом неудачных входов. Если ноль — это нормально (стенд учебный), но строка должна быть.

5. **UBU01.** Ставим venv и `requests` (приём 8). Проверяем:
   ```bash
   /opt/vektor/venv/bin/python3 -c "import requests; print(requests.__version__)"
   ```

6. **UBU01.** Проверяем доступ к API Zabbix без токена.
   ```bash
   curl -s -m 10 http://10.10.10.40/api_jsonrpc.php \
     -H 'Content-Type: application/json-rpc' \
     -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}'
   ```
   Ожидаемый результат: JSON с версией. Если `apiinfo.version` требует авторизации в твоей версии — увидишь ошибку, это тоже результат `[ПРОВЕРИТЬ]`.

7. **MON01 (веб-интерфейс).** Создаём API-токен: `Administration → API tokens → Create API token` `[ПРОВЕРИТЬ путь для твоей версии Zabbix]`. Имя — `vektor-report`, срок — 90 дней, права — только чтение (роль `User` и доступ к нужным хостам). Копируем токен: он показывается один раз.

8. **UBU01.** Кладём секреты в файл окружения.
   ```bash
   sudo tee /etc/vektor/report.env >/dev/null <<'EOF'
   ZBX_URL=http://10.10.10.40/api_jsonrpc.php
   ZBX_TOKEN=вставь_токен_сюда
   EOF
   sudo chown root:root /etc/vektor/report.env
   sudo chmod 600 /etc/vektor/report.env
   ```
   Важно: файл читает systemd (от root) и передаёт переменные процессу — поэтому `600 root:root` работает, хотя сервис бежит от `student`. В боевой среде — Vault или Ansible Vault (урок 75), а не текстовый файл.

9. **UBU01.** Создаём `/opt/vektor/scripts/zabbix_report.py`: чтение `ZBX_URL`/`ZBX_TOKEN` из окружения, функция `api(method, params)`, вызовы `problem.get` (активные проблемы) и `host.get` (список хостов), запись в `/var/log/vektor/zabbix-report.csv` через модуль `csv`, логирование (приём 10), `argparse` с флагом `--days`. Запускаем:
   ```bash
   set -a; . /etc/vektor/report.env; set +a
   /opt/vektor/venv/bin/python3 /opt/vektor/scripts/zabbix_report.py --days 1
   ```
   Ожидаемый результат: CSV с колонками `host,severity,clock,name`, строки соответствуют тому, что видно в `Monitoring → Problems`.

10. **UBU01.** Пишем проверку `/opt/vektor/scripts/check_auth_failures.py`. Логика: считает число `Failed password` за сутки; режим по умолчанию печатает **только число** и всегда выходит с кодом 0 (для Zabbix, `UserParameter`); с флагом `--nagios` печатает `AUTH_FAILURES WARNING - 7 failed logins` и выходит с кодом 1 или 2 при превышении порогов 5 и 20.
    ```bash
    /opt/vektor/venv/bin/python3 /opt/vektor/scripts/check_auth_failures.py; echo "exit=$?"
    /opt/vektor/venv/bin/python3 /opt/vektor/scripts/check_auth_failures.py --nagios; echo "exit=$?"
    ```
    Ожидаемый результат: в первом случае — число и `exit=0`, во втором — строка Nagios-формата и код 1 (если неудачных входов больше 5).

11. **UBU01.** Создаём `run_daily.sh` (обёртка: `set -euo pipefail`, запускает оба скрипта, при ошибке пишет в stderr), затем юниты `.service` и `.timer` из приёма 11.
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable --now vektor-report.timer
    sudo systemctl start vektor-report.service     # ручной прогон
    systemctl status vektor-report.service --no-pager
    journalctl -u vektor-report.service -n 20 --no-pager
    systemctl list-timers vektor-report.timer
    ```
    Ожидаемый результат: сервис `inactive (dead)` с кодом `0/SUCCESS` после ручного запуска, таймер в списке с датой следующего срабатывания.

12. **UBU01.** Дописываем в `stand-log.md`: какие скрипты созданы, какие пути, где лежит токен, какие юниты включены.

### Проверка

- [ ] `/opt/vektor/scripts/auth_report.sh` проходит `shellcheck` без ошибок уровня error.
- [ ] `/var/log/vektor/auth-failures.csv` содержит минимум две строки после двух запусков.
- [ ] `apiinfo.version` отвечает по HTTP с UBU01.
- [ ] `zabbix-report.csv` заполнен и совпадает с картиной в веб-интерфейсе Zabbix.
- [ ] `/etc/vektor/report.env` имеет права `600 root:root`.
- [ ] `check_auth_failures.py` в двух режимах даёт ожидаемые коды выхода.
- [ ] `systemctl list-timers` показывает `vektor-report.timer` с временем 07:00.
- [ ] `journalctl -u vektor-report.service` содержит строки лога твоего скрипта.

### Сломай и почини

> **Пользователь:** дежурный L1 (тикет создан по обращению ivanov)
> **ВМ:** UBU01 / MON01
> **Что написал:** «Zabbix орёт: `AUTH_FAILURES` в статусе WARNING третий день подряд, а в утреннем отчёте пусто. Иван спрашивает, что происходит. Разберись, пожалуйста.»
> **Срочность:** средняя

**Как воспроизвести поломку.** Две независимые причины — специально, чтобы потренировать порядок диагностики. На UBU01 переименуй venv:

```bash
sudo mv /opt/vektor/venv /opt/vektor/venv_old
```

Теперь сервис падает с кодом 127 (`ExecStart` указывает на несуществующий путь). Вторая причина — ложное срабатывание: проверка считает строки `Failed password`, а на UBU01 есть старая задача бэкапа с устаревшим SSH-ключом, которая каждую ночь делает 7 неудачных попыток. Порог WARNING = 5, отсюда вечный WARNING.

**Диагностика по шагам.**
1. `systemctl status vektor-report.service --no-pager` — видим `status=127` и путь, которого нет. Причина №1 найдена.
2. `journalctl -u vektor-report.service -n 50 --no-pager` — подтверждаем: `No such file or directory`.
3. Возвращаем venv: `sudo mv /opt/vektor/venv_old /opt/vektor/venv`, `sudo systemctl start vektor-report.service`.
4. `tail -5 /var/log/vektor/auth-failures.csv` — видим, что отчёт снова пишется, но число неудачных входов стабильно 7. Значит мониторинг не врёт, он честно сообщает о проблеме.
5. `journalctl -u ssh --since "-24h" -o cat | grep 'Failed password' | head -3` — смотрим на строки. Видим один и тот же `user=backup`, интервал ровно раз в сутки.
6. Сопоставляем с владельцем: `sudo grep -rl 'backup@' /etc/cron*` или `systemctl list-timers | grep -i backup` — находим источник.
7. Смотрим на сам ключ: `sudo ssh-keygen -lf /home/backup/.ssh/id_ed25519.pub` и `sudo cat /home/backup/.ssh/authorized_keys` на целевом хосте — ключ не совпадает (устарел после переустановки).

**Решение.** Обновляем ключ бэкапной задачи, проверяем, что попыток стало 0. Порог WARNING не трогаем — он был прав.

**Ответ пользователю:**
> «Разобрались. Причин было две: во-первых, сломался скрипт формирования отчёта (переименовали окружение Python, поправили), во-вторых — сама тревога была настоящей: старая задача резервного копирования стучалась устаревшим SSH-ключом. Ключ заменили, отчёт снова приходит по расписанию в 07:00, отдельных ошибок авторизации на сервере нет. Заявку закрываю, если в понедельник отчёт не придёт — пишите сразу.»

**Закрытие заявки:** в комментарии — что сделано, какие файлы менялись (`/opt/vektor/venv`, ключ `/home/backup/.ssh/`), и что проверено (таймер активен, CSV пишется).

### Что изменилось на стенде

- **UBU01:** созданы `/opt/vektor/scripts/auth_report.sh`, `zabbix_report.py`, `check_auth_failures.py`, `run_daily.sh`; создан venv `/opt/vektor/venv` с `requests`; созданы `/var/log/vektor/auth-failures.csv`, `/var/log/vektor/zabbix-report.csv`, `/var/log/vektor/zabbix-report.log`; создан `/etc/vektor/report.env` (`600 root:root`); учётка `student` добавлена в группу `systemd-journal`; включены юниты `vektor-report.service` и `vektor-report.timer`.
- **MON01:** создан API-токен Zabbix `vektor-report` с правами только на чтение.
- Расписание: ежедневно в 07:00 (с разбросом до 5 минут), догоняющий запуск включён.

### Откат

```text
VM → Snapshot Manager → before-sysadmin-073 → Restore Snapshot
```

на UBU01 и MON01. Восстановление вернёт состояние «до скриптов» — не забудь, что токен Zabbix на MON01 при откате исчезнет, а сервисные юниты на UBU01 пропадут. Если откат не нужен, но нужно снять автоматизацию — `sudo systemctl disable --now vektor-report.timer` и удалить юниты вручную.

## 6. Ошибки новичков и безопасность

1. **Скрипт без `set -euo pipefail`.**
   → Ошибка внутри пайплайна проглатывается, скрипт рапортует «успех», в CSV пишется мусор, а мониторинг молчит.
   → Всегда строгий режим; если конкретная команда имеет право падать — оборачивай в `if` или `|| true` осознанно.

2. **Парсинг вывода `ls` вместо глобов.**
   → Ломается на пробелах и переводах строк в именах файлов.
   → `for f in /path/*.conf; do ... done` или `find ... -print0 | xargs -0`.

3. **Нет таймаута на HTTP-запрос.**
   → Скрипт висит бесконечно, таймер накапливает запуски, замок блокирует всё навсегда.
   → `timeout=` в `requests`, `-m` в curl, плюс `flock`.

4. **Пароли и токены прямо в скрипте или в git.**
   → Утечка при первом же показе кода коллеге; токен живёт в истории git вечно.
   → Переменные окружения, `EnvironmentFile` с правами `600`, Vault/Ansible Vault в боевой среде.

5. **Путаница между «данными» и «сообщением об ошибке».**
   → Мониторинг получает в stdout текст ошибки и принимает его за значение.
   → Данные — только stdout, диагностика — только stderr, код возврата отражает статус.

6. **Zabbix-проверка возвращает nagios-код.**
   → Внешний скрипт Zabbix с ненулевым кодом даёт «Not supported», а не WARNING `[ПРОВЕРИТЬ поведение своей версии]`.
   → Для Zabbix — всегда код 0 и число в stdout; nagios-формат — только для Nagios-совместимых систем.

7. **Нет ротации собственных логов и CSV.**
   → Через полгода `/var/log` переполнен, падает журналирование, а вместе с ним и всё остальное.
   → `logrotate`-конфиг или `RotatingFileHandler`; CSV — по месяцу на файл.

8. **Cron с относительными путями.**
   → «Из консоли работает, из крона — нет»: другой `PATH`, другой `HOME`, нет venv.
   → systemd-таймеры с `ExecStart` по абсолютному пути или явное указание окружения в crontab.

**Безопасность.** На стенде MON01 работает по HTTP без TLS, и токен летит по сети открытым текстом — в боевой среде так нельзя, только HTTPS с проверкой сертификата (и `verify=True`, а не `verify=False`). Скрипту не нужен `root`: сервис запускается от `student`, а sudo-права выдаются точечно на конкретные команды. Не отключай SELinux/AppArmor и файрвол ради того, чтобы «скрипт заработал», — сначала разберись, что именно блокируется. И не давай API-токену права администратора Zabbix: для отчёта достаточно роли только на чтение.

## 7. Вопросы на собеседовании

**1. «Bash или Python — как выбираете инструмент?»**
*Ответ:* bash — когда задача это склейка готовых команд, файловые операции, запуск по расписанию, до ~100–150 строк. Python — когда есть структурированные данные (JSON/CSV), HTTP API, сложная логика, обработка ошибок, регулярки, тесты. Признак, что пора переписывать: появились словари в bash-ассоциативных массивах, разбор JSON через grep и трёхзначные коды выхода.
*Что хочет услышать:* критерий, а не религия; понимание, что оба живут вместе, и bash-обёртка вокруг Python — норма.
*Красный флаг:* «bash вообще не нужен, всё на Python» или «Python на сервере не ставят».

**2. «Скрипт для мониторинга возвращает значение — как вы его спроектируете?»**
*Ответ:* значение в stdout, диагностика в stderr, код возврата по контракту конкретной системы (для Nagios 0/1/2/3, для Zabbix-внешнего скрипта — 0 и число). Обязательно таймаут внутри и снаружи, отсутствие интерактивности, устойчивость к отсутствию источника данных (не падать, а отдавать UNKNOWN), отдельный режим `--verbose` для отладки руками.
*Что хочет услышать:* понимание, что проверка — это контракт, а не «скрипт, который что-то печатает».
*Красный флаг:* «печатаю всё подряд, пусть мониторинг разбирается».

**3. «Как обеспечите идемпотентность и безопасность скрипта, который крутится по таймеру?»**
*Ответ:* идемпотентность — замок `flock`, проверка «уже сделано» перед изменением, дописывание, а не перезапись вслепую. Безопасность — запуск от непривилегированного пользователя, секреты в `EnvironmentFile` с правами `600` или в Vault, ограничение прав API-токена, логирование без секретов, понятный exit code.
*Что хочет услышать:* «повторный запуск ничего не ломает» и «секретов в коде и в логах нет».
*Красный флаг:* «положу пароль в скрипт, он же только для внутренней сети».

**4. «Логи ротируются и сжимаются. Как не сломать парсер?»**
*Ответ:* не привязываться к одному файлу: читать journald (`journalctl --since`, `-o json`) как основной источник, файлы — как резерв. Указывать явные временные границы, а не `tail -n 1000`. Учитывать сжатые `*.gz` (`zgrep`). Не парсить строку целиком регуляркой «на всё», а вытаскивать конкретные поля. Для структурированных логов — JSON и `jq`/`json.loads`.
*Что хочет услышать:* что журнал — поток, а не файл с фиксированным именем.
*Красный флаг:* «читаю `/var/log/messages` и грепаю по дате в формате `Jan 5`».

**5. «Нужно отдать в Zabbix метрику своего сервиса. С чего начнёте?»**
*Ответ:* сначала определю, что именно болит у пользователя (доступность, время ответа, размер очереди), потом выберу способ: `UserParameter` на агенте для локальной метрики, внешний скрипт с UBU01 для удалённой, `zabbix_sender` для push из скрипта. Дальше — триггер с порогом и понятным описанием, потом уже дашборд. Проверю, что при недоступности источника элемент становится «Not supported», а не молча отдаёт 0 — иначе тревоги не будет.
*Что хочет услышать:* «сначала вопрос зачем, потом метрика, потом триггер» и понимание разницы между «нет данных» и «ноль».
*Красный флаг:* сразу «поставлю Zabbix-агент и включу все шаблоны подряд».

## 8. Источники (официальная документация)

- **GNU Bash** — gnu.org, раздел «Bash Reference Manual» (главы про `set`, `getopts`, `trap`, редиректы). Поиск: `bash reference manual set builtin`.
- **Python** — docs.python.org, «The Python Tutorial» и «Library Reference» (`argparse`, `csv`, `logging`, `subprocess`, `re`, `json`). Поиск: `python 3 argparse tutorial`, `python logging cookbook`.
- **Requests** — requests.readthedocs.io, «Quickstart» и «Advanced Usage» (таймауты, retry, сессии). Поиск: `requests advanced usage retries`.
- **systemd** — freedesktop.org, man-страницы `systemd.timer`, `systemd.service`, `systemd.exec`, а также `systemd-analyze` (calendar). Поиск: `systemd timer OnCalendar man`.
- **Zabbix** — zabbix.com/documentation, разделы «API» (`host.get`, `problem.get`, токены) и «External checks» / «User parameters». Поиск: `zabbix api host.get`, `zabbix external checks exit code` `[ПРОВЕРИТЬ актуальный синтаксис для твоей версии]`.
- **Proxmox VE** — pve.proxmox.com, раздел «Proxmox VE API» (аутентификация, API-токены). Поиск: `proxmox ve api tokens`.
- **Ubuntu Server** — ubuntu.com/server/docs, разделы про пакеты, systemd и управление пользователями. Поиск: `ubuntu server docs systemd service`.
- **ShellCheck** — shellcheck.net, вики с описанием кодов ошибок (`SC2086`, `SC2046`). Поиск: `shellcheck SC2086`.
- **PEP 668** — peps.python.org, «Externally Managed Environments» (почему `pip install` в системный Python запрещён). Поиск: `PEP 668 externally managed environments`.
- **logrotate** — man-страница `logrotate(8)` на дистрибутивном зеркале; поиск: `logrotate man page configuration`.
