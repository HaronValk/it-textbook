# Python для администратора: работа с API, SSH (paramiko), парсинг логов, отчёты, Telegram-уведомления

## 1. Введение (зачем это нужно в реальной работе)

Заявка в почте: **Пользователь:** `ivanov` (бухгалтерия). **ВМ:** `WIN11-CL01`. **Что написал:** «Здравствуйте! Утром минут 20 не открывалась 1С, сейчас вроде работает. Проблема повторяется третий день, никто не знает, во сколько именно всё ломалось. Посмотрите, пожалуйста». **Срочность:** средняя.

Вы открываете Zabbix на MON01 — там графики, но по nginx в DMZ и по свободному месту на UBU01 данных нет, а `auth.log` никто не смотрел с прошлого месяца. Руками лазить по пяти серверам каждый день — не вариант. Вот тут и появляется Python: один скрипт спрашивает API мониторинга, второй заходит по SSH на серверы и снимает показатели, третий читает логи, четвёртый собирает из этого отчёт и отправляет его в Telegram.

После главы вы будете уметь: поднимать изолированное окружение Python, дёргать JSON-API через `requests`, ходить по SSH через `paramiko`, парсить логи регулярками, собирать CSV/Markdown-отчёты, отправлять уведомления в Telegram и запускать всё это по расписанию так, чтобы оно не сломалось в самый неподходящий момент.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Python здесь — «клей». PowerShell (урок 81) хорош внутри Windows и AD, Bash (урок 83) — для быстрой склейки команд на Linux. Python берут там, где нужны **структурированные данные** (JSON), **много машин сразу** и **нормальный текст отчёта**. На стенде «Вектора» скрипты живут на UBU01 (10.10.10.30): там Ubuntu Server 24.04 и Python 3.12 из коробки.

Три канала, с которыми мы работаем:

**1. HTTP API.** Почти всё современное ПО (Zabbix, GitLab, 1С, облака) даёт API — программный интерфейс. Запрос — обычный HTTP: метод (`POST`), URL, заголовки, тело в JSON. Ответ — HTTP-код (`200`, `401`, `500`) и JSON. Zabbix использует не «чистый REST», а **JSON-RPC**: в теле всегда есть `jsonrpc`, `method`, `params`, `id`, а ошибки приходят не только кодом, но и объектом `error` внутри JSON с кодом 200. Это важно: `raise_for_status()` тут не спасёт, надо смотреть тело.

**2. SSH.** Протокол прикладного уровня поверх TCP/22 (`paramiko` реализует SSH-2). Внутри три слоя: транспорт (шифрование, обмен ключами), аутентификация (пароль, ключ, `known_hosts` — проверка отпечатка сервера), соединение (каналы: команда, шелл, SFTP). `exec_command` открывает канал, выполняет команду и возвращает три потока — stdin/stdout/stderr, как обычный процесс.

**3. Логи.** `/var/log/auth.log`, `/var/log/nginx/access.log` — текстовые файлы, которые ротирует `logrotate` (переименовывает в `auth.log.1`, `auth.log.2.gz`). Скрипт должен это учитывать, иначе в понедельник он «ничего не найдёт».

```text
                 UBU01 10.10.10.30  (python3.12 + venv)
   ┌──────────────┬───────────────┬──────────────┬───────────────┐
   │ api_zabbix.py│ ssh_health.py │ parse_auth.py│  notify.py    │
   └──────┬───────┴───────┬───────┴──────┬───────┴───────┬───────┘
          │ HTTPS/HTTP    │ SSH TCP/22   │ файл          │ HTTPS 443
          │ L7 JSON-RPC   │ L7 SSH-2     │ локально      │ L7 JSON
          v               v              v               v
   MON01 10.10.10.40   WEB01 10.10.20.10  /var/log/     api.telegram.org
   /api_jsonrpc.php    nginx, sshd        auth.log      (нужен DNS наружу)
                       ^
                       └── идём через GW01 10.10.10.3 (маршрут в DMZ)
```

DNS для `api.telegram.org` UBU01 получает от DC01 (10.10.10.10) — если DC01 не форвардит внешние зоны, имя не разрешится, и это первое, что надо проверить, а не «Телеграм заблокировали».

## 3. Аналогия из жизни

Представьте офисное здание, по которому бегает курьер — это ваш скрипт. У курьера есть три способа получить информацию. Первый — **окно справок** (API): курьер подаёт бланк строго по форме, где написано «кто я», «что прошу» и «номер заявки», и получает ответ тоже по форме; если бланк заполнить неверно, окно не молчит, а возвращает листок с текстом ошибки. Второй — **служебный вход с ключом** (SSH): курьер прикладывает пропуск, вахтёр сверяет его с журналом выданных пропусков (`known_hosts`), и только потом пускает; если курьер махнул рукой «да пускай любого» — это `AutoAddPolicy`. Третий — **вахтёрская книга** (лог-файл): курьер листает записи и считает, сколько раз кто пытался войти. А Telegram — это доска объявлений у лифта, куда курьер вешает короткую записку, чтобы бригадир увидел результат, не дожидаясь курьера лично.

Аналогия ломается на ротации: в реальности вахтёр в конце месяца уносит книгу в архив и заводит новую, а курьер об этом не знает и продолжает читать пустые страницы.

## 4. Практика и команды

**1. Изолированное окружение (PEP 668).** В Ubuntu 24.04 `pip install` в системный Python запрещён — получите `error: externally-managed-environment`. Только venv.

```bash
python3 --version
sudo apt install -y python3-venv python3-pip
python3 -m venv ~/venv-admin
source ~/venv-admin/bin/activate
pip install requests paramiko
pip freeze > ~/scripts/requirements.txt
```

`python3 -m venv` создаёт каталог с собственным интерпретатором и `site-packages`; `source .../activate` подменяет `PATH` и `python` в текущем шелле. Вывод: `Python 3.12.3`, затем `Successfully installed ...`. Смотреть: версию (не 2.7!) и что приглашение шелла началось с `(venv-admin)`. `pip freeze` фиксирует точные версии — без него через полгода скрипт «внезапно» сломается на обновлении библиотеки.

**2. Проверка импортов и форматирование JSON.**

```bash
python3 -c "import requests, paramiko; print(requests.__version__, paramiko.__version__)"
echo '{"a":{"b":[1,2]}}' | python3 -m json.tool
```

`-c` — выполнить строку кода. `python3 -m json.tool` — встроенный «красивый печататель» JSON; незаменим, когда API вернул одну длинную строку.

**3. GET-запрос через `requests`.**

```python
import requests
r = requests.get("http://10.10.10.40/", timeout=10)
r.raise_for_status()
print(r.status_code, r.elapsed, len(r.text))
```

`timeout=10` — обязателен: без него скрипт может висеть вечно. `raise_for_status()` бросает исключение на 4xx/5xx. `r.elapsed` — время ответа, удобно писать в отчёт как метрику. Типичный вывод: `200 0:00:00.043 15234`.

**4. Zabbix API: версия без авторизации, затем логин и данные.**

```python
import requests
URL = "http://10.10.10.40/api_jsonrpc.php"
H = {"Content-Type": "application/json-rpc"}

def zbx(method, params, auth=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    if auth:
        body["auth"] = auth   # Zabbix 6.x. В 7.0 поле auth убрано —
                              # токен идёт заголовком Authorization: Bearer <token> [ПРОВЕРИТЬ версию]
    r = requests.post(URL, json=body, headers=H, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]

print(zbx("apiinfo.version", {}))                     # работает без токена
token = zbx("user.login", {"user": "Admin", "password": "..."})
print(len(zbx("host.get", {"output": ["hostid", "host"]}, auth=token)))
```

`apiinfo.version` — единственный метод, которому не нужна авторизация, с него всегда начинают: он подтверждает, что URL и сеть в порядке. `user.login` возвращает токен-строку. `host.get` с `output` — какие поля вернуть. Вывод: `['6.0.30']`, затем число хостов. Смотреть: совпадает ли версия API с той, под которую вы писали (в 7.x сменилась авторизация).

**5. `paramiko`: команда на удалённом хосте.**

```python
import paramiko
c = paramiko.SSHClient()
c.load_system_host_keys()
c.set_missing_host_key_policy(paramiko.RejectPolicy())
c.connect("10.10.10.30", username="USER", key_filename="/home/USER/.ssh/id_ed25519",
          timeout=10, banner_timeout=10, auth_timeout=10)
_, out, err = c.exec_command("df -h / | tail -1", timeout=15)
code = out.channel.recv_exit_status()
print(code, out.read().decode().strip(), err.read().decode().strip())
c.close()
```

`load_system_host_keys()` подхватывает `~/.ssh/known_hosts`; по умолчанию политика и так `RejectPolicy`, `AutoAddPolicy` надо включать руками — не включайте. `timeout` на `connect` — сетевой, на `exec_command` — на выполнение. `recv_exit_status()` возвращает код возврата (без него вы не отличите «команда упала» от «пустой вывод»). Вывод: `0 /dev/sda2 24G 9.1G 14G 40% /`.

**6. `paramiko` + SFTP: забрать файл с сервера.**

```python
sftp = c.open_sftp()
for a in sftp.listdir_attr("/var/log/nginx")[:5]:
    print(a.filename, a.st_size)
sftp.get("/var/log/nginx/access.log", "/tmp/web01-access.log")
sftp.close()
```

`listdir_attr` отдаёт объекты с размером и mtime — удобно для отчёта «сколько весит лог». `get`/`put` — скачать/загрузить.

**7. Регулярки и `Counter`: топ IP по неудачным логинам.**

```python
import re
from collections import Counter
PAT = re.compile(r"Failed password for (?:invalid user )?(?P<user>\S+) "
                 r"from (?P<ip>\d{1,3}(?:\.\d{1,3}){3})")
ip = Counter()
with open("/var/log/auth.log", errors="replace") as f:
    for line in f:
        m = PAT.search(line)
        if m:
            ip[m["ip"]] += 1
print(ip.most_common(5))
```

Строка в логе: `sshd[1234]: Failed password for invalid user admin from 10.10.10.99 port 51234 ssh2`. `(?P<имя>...)` — именованная группа, читается как `m["ip"]`. `(?:...)` — группа без захвата. `errors="replace"` спасает от падения на битой кодировке. Вывод: `[('10.10.10.99', 42), ...]` — 42 попытки за минуту с одного адреса это уже brute-force, эскалация в ИБ.

**8. CSV-отчёт.**

```python
import csv
with open("report.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["host", "check", "rc", "value"])
    w.writeheader()
    w.writerow({"host": "WEB01", "check": "nginx", "rc": 0, "value": "active"})
```

`newline=""` — иначе на Windows-клиенте получите пустые строки между записями. CSV открывается в Excel — это то, что просит руководитель в 90% случаев.

**9. Telegram: отправка сообщения через Bot API.**

```python
import os, requests
r = requests.post(
    f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
    json={"chat_id": os.environ["TG_CHAT"], "text": text,
          "parse_mode": "HTML", "disable_web_page_preview": True},
    timeout=10)
r.raise_for_status()
print(r.json()["ok"])
```

Токен даёт `@BotFather` (`/newbot`), `chat_id` узнаёте из `https://api.telegram.org/bot<TOKEN>/getUpdates` после того, как напишете боту. Лимит текста — 4096 символов, длинное режьте на части. Ответ: `{"ok": true, "result": {...}}`.

**10. `argparse`: параметры вместо «правки кода перед запуском».**

```bash
python3 report.py --hosts UBU01,WEB01 --days 7 --dry-run
```

`--hosts` со `type=lambda s: s.split(",")`, `--days` с `type=int, default=1`, `--dry-run` через `action="store_true"`. `-h` печатает справку автоматически.

**11. `logging` вместо `print`.**

```python
import logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    filename="/home/USER/logs/report.log")
logging.info("start, hosts=%s", hosts)
logging.exception("WEB01 unreachable")   # внутри except — пишет traceback
```

В cron `print` уходит в никуда, а лог с временем и уровнем остаётся.

**12. Запуск по расписанию (детали — урок 87).**

```bash
0 7 * * * /home/USER/venv-admin/bin/python /home/USER/scripts/daily_report.py >> /home/USER/logs/cron.log 2>&1
```

Главное: **абсолютный путь к интерпретатору из venv** и перенаправление `2>&1` в файл. У cron свой `PATH` и своё окружение — половина «не работает из крона» лечится именно этим.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
Нужны: **UBU01** (10.10.10.30, Ubuntu 24.04, SSH, ваш пользователь, доступ в интернет через NAT), **MON01** (10.10.10.40, Zabbix с известным паролем `Admin`), **WEB01** (10.10.20.10, nginx, SSH) и **GW01** (10.10.10.3 / 10.10.20.1) как маршрутизатор в DMZ. Все три появились на предыдущих уроках L2 (мониторинг, маршрутизация, DMZ). Если WEB01 ещё нет — пройдите сначала главу про GW01 и DMZ, без неё шаг 6 пропустите и работайте только с UBU01. **WIN11-CL01** пригодится на шаге 7, чтобы «насорить» в логах (OpenSSH-клиент в Windows 11 есть из коробки).

### Снапшот
В VMware Workstation на каждой затрагиваемой ВМ: меню **VM → Snapshot → Take Snapshot**, имя `before-l2-084`. Для UBU01, MON01, WEB01 — обязательно; DC01 и GW01 меняться не должны, но снапшот DC01 не помешает.

### Шаги

1. **UBU01.** Подготовьте окружение (команды из п.4.1). Ожидаемо: `Python 3.12.3`, приглашение `(venv-admin)`, `pip freeze` показывает `requests` и `paramiko` с версиями.

2. **UBU01.** Секреты — в файл, не в код:
   ```bash
   mkdir -p ~/.config/vektor ~/scripts ~/logs ~/reports
   cat > ~/.config/vektor/env <<'EOF'
   export ZBX_URL="http://10.10.10.40/api_jsonrpc.php"
   export ZBX_USER="Admin"
   export ZBX_PASS="<пароль Zabbix>"
   export SSH_USER="<ваш пользователь на UBU01/WEB01>"
   export TG_TOKEN="<токен>"
   export TG_CHAT="<chat_id>"
   EOF
   chmod 600 ~/.config/vektor/env
   source ~/.config/vektor/env
   ```
   Ожидаемо: `ls -l` показывает `-rw-------`. Пароли в код не пишем (урок 76).

3. **UBU01.** Скрипт `~/scripts/api_zabbix.py` из п.4.4 (URL берите из `os.environ["ZBX_URL"]`). Запуск: `python3 ~/scripts/api_zabbix.py`. Ожидаемо: `Строка версии API` и число активных проблем. Если `RuntimeError` с полем `error` — читайте `data["error"]["data"]`, там текст от Zabbix.

4. **UBU01.** Маршрут в DMZ и проверка SSH руками:
   ```bash
   sudo ip route add 10.10.20.0/24 via 10.10.10.3
   ssh -o BatchMode=yes $SSH_USER@10.10.20.10 "hostname; systemctl is-active nginx"
   ```
   Ожидаемо: `web01` и `active`. Если `No route to host` — проблема в маршруте или nftables на GW01. Прописать маршрут постоянно через netplan — задача урока про GW01.

5. **UBU01.** Скрипт `~/scripts/ssh_health.py` из п.4.5 для двух целей — UBU01 и WEB01; проверки: `uptime -p`, `df -h / | tail -1`, а на WEB01 ещё `systemctl is-active nginx`. Запуск. Ожидаемо: по строке на каждую проверку, `rc=0`, у WEB01 `active`.

6. **UBU01.** Скрипт `~/scripts/parse_auth.py` из п.4.7 по `/var/log/auth.log`. Ожидаемо: «Всего неудачных попыток: N» и топ-5 IP.

7. **WIN11-CL01.** «Насорим» в логах, чтобы парсер было что считать: в PowerShell
   ```powershell
   for ($i=0; $i -lt 5; $i++) { ssh baduser@10.10.10.30 exit }
   ```
   На запрос пароля вводите любой мусор. Ожидаемо: на UBU01 в `auth.log` появляются `Failed password for invalid user baduser from 10.10.10.100` (адрес WIN11-CL01 из DHCP-пула — сверьте фактический). Повторный запуск парсера показывает рост счётчика.

8. **UBU01.** Скрипт `~/scripts/report.py`: собирает результаты шагов 3, 5, 6 и пишет Markdown в `~/reports/report-$(date +%F).md` (таблица «хост / проверка / результат») плюс `report.csv`. Ожидаемо: файл, который открывается в редакторе и читается человеком.

9. **UBU01.** Telegram: в `@BotFather` → `/newbot` → имя → токен; напишите боту любое сообщение и откройте `https://api.telegram.org/bot<TOKEN>/getUpdates`, скопируйте `chat.id` в `TG_CHAT`. Скрипт `notify.py` из п.4.9 отправляет первые 10 строк отчёта. Ожидаемо: сообщение в чате, `True` в выводе.

10. **UBU01.** Единый `~/scripts/daily_report.py` с `argparse` (`--dry-run`, `--days`, `--no-notify`) и `logging`. Проверьте `--dry-run`, затем поставьте в cron строку из п.4.12. Ожидаемо: после `crontab -e` в `~/logs/cron.log` через минуту (если временно поставить `* * * * *`) появляется запись с датой.

11. **UBU01.** Запишите в `stand-log.md`: «UBU01: venv ~/venv-admin, скрипты ~/scripts/{api_zabbix,ssh_health,parse_auth,report,notify,daily_report}.py, env-файл ~/.config/vektor/env (600), cron 0 7 * * *, статический маршрут в DMZ через GW01».

### Проверка
- [ ] `python3 -c "import requests, paramiko"` внутри venv проходит без ошибок.
- [ ] `api_zabbix.py` возвращает версию API и список проблем без исключений.
- [ ] `ssh_health.py` показывает по UBU01 и WEB01 `rc=0`, у WEB01 `nginx active`.
- [ ] `parse_auth.py` после шага 7 увеличивает счётчик для 10.10.10.100.
- [ ] `~/reports/report-<дата>.md` и `report.csv` существуют и непустые.
- [ ] В Telegram пришло сообщение с текстом отчёта.
- [ ] `crontab -l` содержит абсолютный путь `/home/USER/venv-admin/bin/python` и `>> ... 2>&1`.
- [ ] В `stand-log.md` появилась запись о новых файлах и cron.

### Сломай и почини

**Заявка. Пользователь:** `ivanov` (бухгалтерия). **ВМ:** `WIN11-CL01`. **Что написал:** «Утренний отчёт по серверам в чат ИТ с понедельника не приходит — мне его пересылают для сверки доступности 1С, а теперь нечего пересылать. Сами серверы вроде работают. Срочность: средняя».

**Как воспроизвести.** На UBU01 откройте `crontab -e` и замените интерпретатор на системный: `/usr/bin/python3 /home/USER/scripts/daily_report.py`. Сохраните, дождитесь запуска.

**Диагностика по шагам.** (1) `crontab -l` — видим `/usr/bin/python3`. (2) `tail -50 ~/logs/cron.log` — там `ModuleNotFoundError: No module named 'requests'`. (3) Проверяем гипотезу: `/usr/bin/python3 ~/scripts/daily_report.py` руками — та же ошибка; `~/venv-admin/bin/python ~/scripts/daily_report.py` — работает. Вывод: cron запускает системный Python, где библиотек нет. (4) Попутно проверяем, что cron вообще жив: `systemctl status cron`, `timedatectl` (часовой пояс!). (5) Проверяем, что не «ложная тревога»: `grep` по логу Zabbix — проблем нет, серверы в порядке. (6) Отдельно смотрим, не ротировался ли `auth.log` (`ls -l /var/log/auth.log*`), иначе через неделю повторится та же история с парсером.

**Решение.** Вернуть в cron venv-интерпретатор; обернуть запуск в `run_report.sh` с `#!/bin/bash`, `set -euo pipefail`, `source /home/USER/venv-admin/bin/activate`; добавить в конец скрипта проверку «если предыдущий успешный запуск был больше 25 часов назад — прислать в Telegram отдельную строку `STALE`»; писать в лог `logging.exception` при любом падении. В нормальной среде cron-джобы ставят под мониторинг (Zabbix `system.run`/trapper-элемент или проверка «heartbeat-файла»), чтобы падение скрипта было видно до жалобы пользователя — здесь мы делаем это вручную.

**Ответ пользователю:** «Добрый день! Причина найдена: задача в планировщике запускалась с неверным окружением Python, скрипт падал с ошибкой и уведомление не уходило. Серверы при этом работали штатно. Исправил запуск, добавил контроль, что отчёт ушёл; проверил на тестовом запуске — утренний отчёт за сегодня придёт в ближайшие минуты. Если завтра к 8:00 его не будет — сразу напишите».

**Закрытие заявки:** категория «Автоматизация / плановые задачи», решение — «некорректный интерпретатор в cron, заменён на venv; добавлен watchdog и логирование», привязка к записи в `stand-log.md`.

### Что изменилось на стенде
- **UBU01:** установлены `python3-venv`, `python3-pip`; окружение `~/venv-admin` с `requests` и `paramiko`; каталоги `~/scripts`, `~/logs`, `~/reports`; скрипты `api_zabbix.py`, `ssh_health.py`, `parse_auth.py`, `report.py`, `notify.py`, `daily_report.py`; файл секретов `~/.config/vektor/env` (права 600); статический маршрут `10.10.20.0/24 via 10.10.10.3`; запись в `crontab` пользователя.
- **MON01:** без изменений в конфигурации, но появился внешний потребитель API Zabbix (учётка `Admin` в скрипте — в проде нужна отдельная read-only учётка).
- **WEB01:** без изменений, доступен по SSH с UBU01 через GW01.
- **Секреты:** токен Telegram-бота и пароль Zabbix хранятся только в env-файле на UBU01; в `stand-log.md` сами значения не пишем.

### Откат
VMware Workstation → выбрать ВМ → **VM → Snapshot → Snapshot Manager** → выбрать `before-l2-084` → **Go to**. Откатывать нужно все три ВМ (UBU01, MON01, WEB01) до общего состояния, иначе маршрут или cron останутся «висеть». После отката удалите строку из `stand-log.md` или допишите пометку «откат до before-l2-084». Если откат не нужен, но скрипты мешают — достаточно `crontab -e` и закомментировать строку.

## 6. Ошибки новичков и безопасность

1. **`pip install` в системный Python** → на Ubuntu 24.04 ошибка `externally-managed-environment`, а в обход (через `--break-system-packages`) — сломанные системные утилиты. → Всегда venv.
2. **Пароль или токен прямо в коде** → попадает в бэкап, в чат, в Git (урок 85). → `os.environ` + файл с `chmod 600`, в проде — Vault (урок 76).
3. **`paramiko.AutoAddPolicy()`** → скрипт доверяет любому серверу с тем же IP, MITM проходит незамеченным. → `load_system_host_keys()` + `RejectPolicy`, ключи в `known_hosts`.
4. **`verify=False` в `requests`** и API по HTTP «чтобы работало» → данные и токены идут открытым текстом. → `verify=True`, в лабе HTTP допустим только внутри изолированной сети, в проде — TLS.
5. **Парсинг лога «целиком» без учёта ротации** → после `logrotate` скрипт читает пустой файл и рапортует «всё хорошо». → Учитывать `auth.log.1`, читать `*.gz` через `gzip`, или ставить хук в `logrotate`.
6. **Отправка персональных данных в Telegram** → ФИО, логины, IP сотрудников уходят за границу, это вопрос 152-ФЗ. → В уведомления только имена хостов, метрики и агрегаты; для корпоративных нужд — self-hosted Mattermost/Rocket.Chat.
7. **`except Exception: pass`** → скрипт «работает», просто ничего не делает. → Логировать `logging.exception`, возвращать ненулевой код возврата.
8. **Запуск от root «на всякий случай»** → скрипт с багом может снести не то. → Отдельный пользователь, `sudo` только на конкретные команды через `sudoers`.

**Безопасность.** В лаборатории мы храним пароль Zabbix в env-файле и используем учётку `Admin` — в боевой среде так нельзя: под API создаётся отдельный сервисный пользователь с правами только на чтение, пароль ставится в Vault с ротацией, все запросы идут по HTTPS. Токен Telegram-бота — это полноценный доступ к боту: утечка = спам от вашего имени. Скрипты, которые ходят по SSH на десятки машин, в реальности почти всегда заменяют Ansible (урок 86) — там нет разбросанных по коду паролей и есть идемпотентность.

## 7. Вопросы на собеседовании

**1. «У API нет SDK. Как будете с ним работать?»**
Ответ: выясню тип — REST или JSON-RPC; найду в документации базовый URL, метод аутентификации и формат ошибок. Дальше `requests` с `timeout`, проверка `raise_for_status()`, для JSON-RPC — дополнительно разбор поля `error` в теле. Начну с самого дешёвого метода (`apiinfo.version`, `/health`), чтобы отделить проблемы сети от проблем авторизации. Секреты — из env, не из кода.
*Что хочет услышать:* системный подход, а не «попробую наугад».
*Красный флаг:* «поставлю `verify=False`, потом разберёмся» и хардкод пароля.

**2. «Как хранить секреты в скриптах?»**
Ответ: в коде — никогда. Переменные окружения или файл с правами `600` вне репозитория; в команде — Vault/Ansible Vault, ротация, отдельные сервисные учётки с минимальными правами, `.gitignore` для env-файлов. Плюс маскирование секретов в логах.
*Что хочет услышать:* понимание, что это процесс, а не одна команда.
*Красный флаг:* «положу в переменную в скрипте, там же никто не смотрит».

**3. «Чем опасен `AutoAddPolicy` в paramiko?»**
Ответ: он принимает любой host key, то есть скрипт не проверяет, тот ли это сервер. При подмене (ARP/DNS spoofing) MITM получает и команды, и, при неудачной настройке, учётные данные. Правильно: `load_system_host_keys()` + `RejectPolicy`, ключи заранее в `known_hosts`, при массовом парке — раскатка ключей через Ansible или централизованный `ssh_known_hosts`.
*Что хочет услышать:* понимание проверки подлинности сервера, а не только «пароль шифруется».
*Красный флаг:* «внутри сети это неважно».

**4. «Логи ротируются. Как парсить, чтобы не терять данные?»**
Ответ: учитывать `logrotate` — читать `auth.log`, затем `auth.log.1`, архивы через `gzip`; либо читать по смещению (inode + offset) и сбрасывать позицию при смене inode; либо не парсить файлы самим, а отдавать в централизованный сборщик (rsyslog/journald → SIEM). Обязательно фиксировать, за какой период данные реально прочитаны, и попадать этим в отчёт.
*Что хочет услышать:* опыт, что «grep по файлу» — не мониторинг.
*Красный флаг:* «прочитаю файл целиком, там всегда всё есть».

**5. «Скрипт работает руками, но не работает из cron. Ваши действия?»**
Ответ: cron даёт минимальное окружение — свой `PATH`, нет `~/.bashrc`, другой рабочий каталог. Проверю: абсолютные пути к интерпретатору и файлам, venv вместо системного Python, перенаправление `stdout/stderr` в лог, пользователя от чьего имени идёт задача, `systemctl status cron`, часовой пояс `timedatectl`, кавычки/спецсимволы (`%` в cron экранируется). Дальше — обёртка на bash с `set -euo pipefail` и запись кода возврата.
*Что хочет услышать:* знание конкретных отличий окружения cron.
*Красный флаг:* «попробую перезагрузить сервер».

## 8. Источники (официальная документация)

- **docs.python.org** — разделы «venv — Creation of virtual environments», «re — Regular expression operations», «csv», «argparse», «logging». Поиск: `python venv tutorial`, `python re named groups`.
- **requests.readthedocs.io** — «Quickstart», «Authentication», «Timeouts». Поиск: `python requests timeout raise_for_status`.
- **docs.paramiko.org** — «Client», «SFTP», «Host keys». Поиск: `paramiko SSHClient exec_command host keys`.
- **zabbix.com/documentation** — раздел «API» (JSON-RPC, `user.login`, `host.get`, `problem.get`). Поиск: `zabbix api jsonrpc user.login problem.get`. Детали авторизации в 7.x `[ПРОВЕРИТЬ]` под вашу версию.
- **core.telegram.org/bots/api** — «Making requests», `sendMessage`, `getUpdates`. Поиск: `telegram bot api sendmessage`.
- **ubuntu.com** — разделы про Python и PEP 668 (`externally-managed-environment`). Поиск: `ubuntu 24.04 externally managed environment pip`.
- **nginx.org/en/docs** — «Module ngx_http_log_module» (формат `access.log`, если будете парсить его). Поиск: `nginx log format combined`.
