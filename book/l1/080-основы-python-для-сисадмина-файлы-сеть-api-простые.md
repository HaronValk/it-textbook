# Основы Python для сисадмина: файлы, сеть, API, простые утилиты

## 1. Введение (зачем это нужно в реальной работе)

Заявка в `C:\Support\Tickets`, 10:42, срочность «средняя»:

> **Пользователь:** Петрова М. (HR), ВМ WIN10-CL02.
> **Что написала:** «Каждое утро мне нужно вручную проверять, что наши общие папки на FS01 доступны, и записывать это в отчёт. Я делаю это уже месяц, трачу 15 минут. Можно как-то автоматизировать? И ещё: у меня на рабочем столе накопилось 300 файлов из почты, надо разложить по датам, но руками это ад.»
> **Срочность:** средняя.

Петрова просит ровно то, для чего сисадмин и L1 берут Python: автоматизировать рутину, которую руками делать глупо. Проверить доступность папок — это одна сетевая проверка. Разложить 300 файлов по датам — это цикл по каталогу и `os.rename`. Обе задачи на Python решаются в 20–40 строк, читаются через полгода и не ломаются, когда Петрова нажмёт «Enter» не там.

Python в 2026 году — это де-факто «клей» для инфраструктуры: на нём пишут скрипты мониторинга, парсер логов, обёртки над API (Zabbix, GLPI, 1С, облака), Ansible-модули и половину DevOps-инструментов. Для L1 это ещё и язык, на котором можно за 10 минут сделать утилиту, которую потом используют все. После этой главы ты будешь уметь: читать и писать файлы, ходить по каталогам, делать HTTP-запросы к API, проверять доступность хостов и портов, собирать из этого маленькие утилиты с аргументами командной строки и понятным выводом.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Python — интерпретируемый язык. Ты пишешь `.py`-файл, интерпретатор `python3` читает его построчно, компилирует в байткод и выполняет. Никакой компиляции «в exe» для запуска не нужно: на UBU01 достаточно `python3 script.py`. Python 3.12+ идёт в составе Ubuntu Server 24.04 «из коробки», отдельно ставить не надо.

Ключевые для сисадмина модули стандартной библиотеки (то есть уже установлены, `pip install` не нужен):

| Модуль | Зачем |
|--------|-------|
| `os`, `pathlib` | файлы, каталоги, пути, переменные окружения |
| `shutil` | копирование, перемещение, удаление каталогов |
| `subprocess` | запуск внешних команд (`ping`, `ssh`, `systemctl`) |
| `socket` | TCP/UDP-соединения, проверка портов, DNS-резолв |
| `urllib.request` | HTTP-запросы без сторонних библиотек |
| `json` | разбор ответов API |
| `argparse` | аргументы командной строки (`--host`, `--port`) |
| `logging` | журнал работы скрипта |
| `datetime` | даты, время, форматирование |

Сторонние библиотеки (`requests`, `paramiko`, `psutil`) удобнее, но требуют `pip install` и доступа в интернет. На стенде интернет есть через NAT (VMnet8, шлюз 10.10.10.2), но для учебных утилит мы обойдёмся стандартной библиотекой — она работает всегда и на любом сервере, даже без интернета.

Как Python встраивается в работу сисадмина:

```text
   ┌────────────────────────────────────────────────────────────┐
   │  Твоя утилита на Python (UBU01 или WIN11-CL01)             │
   │                                                            │
   │  argparse  →  логика  →  вывод (stdout / файл / лог)       │
   │                │                                           │
   │      ┌─────────┼──────────┬─────────────┐                  │
   │      ▼         ▼          ▼             ▼                  │
   │   файлы    socket     urllib       subprocess              │
   │  (os,      (TCP-      (HTTP к      (ping, ssh,             │
   │  pathlib)  connect)   API)         systemctl)              │
   └──────┬─────────┬──────────┬─────────────┬──────────────────┘
          │         │          │             │
          ▼         ▼          ▼             ▼
       локальный  порт 80/443  REST API   внешняя команда
       диск       на хосте     (JSON)     в ОС
```

Протоколы, которые ты будешь трогать:

- **TCP** — `socket.create_connection((host, port), timeout=3)` открывает TCP-соединение. Если порт открыт — успех, если закрыт — `ConnectionRefusedError`, если хост недоступен — `socket.timeout` или `OSError`. Это основа проверки «жив ли сервис».
- **DNS** — `socket.gethostbyname("dc01.corp.vektor.lab")` резолвит имя в IP через системный резолвер (на UBU01 — `/etc/resolv.conf`, на WIN11-CL01 — DNS-клиент Windows).
- **HTTP/HTTPS** — `urllib.request.urlopen("http://...")` делает GET-запрос, возвращает объект с `.status`, `.read()`. Для API обычно нужен JSON: `json.loads(response.read())`.
- **ICMP** — из Python напрямую не отправишь без root и сырых сокетов, поэтому проверку «пингуется ли хост» делают через `subprocess.run(["ping", "-c", "1", host])`.

## 3. Аналогия из жизни

Python — это швейцарский армейский нож сисадмина. В нём есть маленькие лезвия на каждый день (открыть файл, пройтись по каталогу), есть отвёртка (сокет — проверить порт), есть штопор (HTTP-запрос к API), и есть отделение, куда можно доложить свои инструменты (`pip install`). Нож не заменяет молоток (Bash для однострочников, PowerShell для Windows-специфики), но когда нужно «и там, и там, и с сохранением результата в файл» — ты тянешься к нему. Аналогия перестаёт работать в одном месте: нож не умеет сам решать, что делать — всю логику «если порт закрыт, напиши в лог и заверши с кодом 1» ты пишешь руками.

## 4. Практика и команды

Все примеры — на UBU01 (Ubuntu Server 24.04, `adm.student`, SSH с WIN11-CL01). Python 3 уже стоит.

**1. Проверить версию и запустить первую строку.**

```bash
python3 --version
python3 -c "print('hello, vektor')"
```

`--version` покажет что-то вроде `Python 3.12.3` (точная цифра зависит от образа). `-c` — выполнить строку без файла. Удобно для быстрых проверок.

**2. Прочитать файл и посчитать строки.**

```bash
python3 - <<'PY'
from pathlib import Path
p = Path("/var/log/syslog")
if p.exists():
    lines = p.read_text(errors="replace").splitlines()
    print(f"{p}: {len(lines)} строк")
else:
    print(f"{p}: нет файла")
PY
```

`Path.read_text()` читает весь файл в строку. `errors="replace"` — чтобы не падать на бинарных байтах. `splitlines()` режет по переводам строк. Вывод примерно: `/var/log/syslog: 8421 строк`. Для больших файлов лучше `for line in p.open():` — читает построчно, не грузит всё в память.

**3. Пройтись по каталогу и найти файлы по маске.**

```bash
python3 - <<'PY'
from pathlib import Path
root = Path("/var/log")
for f in sorted(root.glob("*.log")):
    print(f.name, f.stat().st_size, "байт")
PY
```

`glob("*.log")` — файлы в каталоге по маске. `stat().st_size` — размер. Вывод примерно: `alternatives.log 12345 байт`. Для рекурсивного поиска — `rglob("*.log")`.

**4. Проверить TCP-порт на хосте.**

```bash
python3 - <<'PY'
import socket
for host, port in [("10.10.10.10", 53), ("10.10.10.10", 3389), ("10.10.10.30", 22)]:
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"{host}:{port} открыт")
    except OSError as e:
        print(f"{host}:{port} закрыт ({e.__class__.__name__})")
PY
```

`create_connection` открывает TCP и сразу закрывает (через `with`). `timeout=3` — не висеть вечно. На стенде DC01 (10.10.10.10) слушает 53 (DNS) и 3389 (RDP), UBU01 (10.10.10.30) — 22 (SSH). Вывод примерно: `10.10.10.10:53 открыт`, `10.10.10.10:3389 открыт`, `10.10.10.30:22 открыт`.

**5. Резолв имени в IP.**

```bash
python3 - <<'PY'
import socket
for name in ["dc01.corp.vektor.lab", "ubu01.corp.vektor.lab", "no-such-host.corp.vektor.lab"]:
    try:
        print(name, "→", socket.gethostbyname(name))
    except socket.gaierror as e:
        print(name, "→ ошибка:", e)
PY
```

`gethostbyname` использует системный резолвер. На UBU01 в `/etc/resolv.conf` должен быть `nameserver 10.10.10.10` (это настраивалось в L1 №34). Вывод примерно: `dc01.corp.vektor.lab → 10.10.10.10`, `ubu01.corp.vektor.lab → 10.10.10.30`, `no-such-host.corp.vektor.lab → ошибка: [Errno -2] Name or service not known`.

**6. HTTP-запрос к API и разбор JSON.**

```bash
python3 - <<'PY'
import json, urllib.request
url = "https://api.github.com/repos/python/cpython"
try:
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
        print("status:", r.status)
        print("stars:", data.get("stargazers_count"))
except Exception as e:
    print("ошибка:", e)
PY
```

`urlopen` делает GET. `json.loads(r.read())` превращает байты в словарь. `data.get("stargazers_count")` — безопасно достаёт поле. Вывод примерно: `status: 200`, `stars: 63000` (число меняется). GitHub API без токена ограничен 60 запросами в час с одного IP — для учебных целей хватает. Если GitHub недоступен из РФ — используй любой другой публичный API или локальный сервис (например, `http://10.10.10.10` — IIS на DC01, если роль установлена; на стенде до L1 №80 её нет, поэтому для лабораторной мы обойдёмся без внешнего API).

**7. Запустить внешнюю команду и получить вывод.**

```bash
python3 - <<'PY'
import subprocess
r = subprocess.run(["ping", "-c", "1", "-W", "2", "10.10.10.10"],
                   capture_output=True, text=True)
print("код возврата:", r.returncode)
print(r.stdout.splitlines()[-2] if r.stdout else "нет вывода")
PY
```

`subprocess.run` запускает команду. `capture_output=True` — перехватить stdout/stderr. `text=True` — вернуть строки, а не байты. `returncode == 0` — успех. Вывод примерно: `код возврата: 0` и строка статистики `1 packets transmitted, 1 received, 0% packet loss`. На стенде ICMP разрешён на всех ВМ (правило в брандмауэре), поэтому ping до DC01 проходит.

**8. Аргументы командной строки через `argparse`.**

```bash
cat > /tmp/checkhost.py <<'PY'
import argparse, socket, sys

def check(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def main():
    ap = argparse.ArgumentParser(description="Проверка TCP-порта")
    ap.add_argument("host")
    ap.add_argument("-p", "--port", type=int, default=22)
    ap.add_argument("-t", "--timeout", type=float, default=3.0)
    args = ap.parse_args()
    ok = check(args.host, args.port, args.timeout)
    print(f"{args.host}:{args.port} {'открыт' if ok else 'закрыт'}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
PY
python3 /tmp/checkhost.py 10.10.10.30 -p 22
python3 /tmp/checkhost.py 10.10.10.10 -p 3389
echo "код: $?"
```

`argparse` сам делает `--help`, проверяет типы, ругается на лишние аргументы. `sys.exit(0/1)` — правильный код возврата, чтобы скрипт можно было использовать в `if` и в cron. Вывод: `10.10.10.30:22 открыт`, `10.10.10.10:3389 открыт`, `код: 0`.

**9. Записать результат в файл и в лог.**

```bash
python3 - <<'PY'
import logging
from pathlib import Path
logging.basicConfig(filename="/tmp/check.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logging.info("проверка запущена")
Path("/tmp/result.txt").write_text("ok\n")
logging.info("результат записан")
PY
cat /tmp/check.log
```

`logging` пишет в файл с датой и уровнем. `Path.write_text` создаёт/перезаписывает файл. Вывод `cat` примерно: `2026-01-15 10:42:01,123 INFO проверка запущена` и вторая строка. В боевой среде лог — это то, по чему ты потом разбираешь, почему скрипт в 3 часа ночи упал.

**10. На Windows (WIN11-CL01) — то же самое через PowerShell.**

```powershell
python --version
python -c "import socket; print(socket.gethostbyname('dc01.corp.vektor.lab'))"
```

Если Python на WIN11-CL01 не установлен — поставь с python.org (Windows installer, «Add python.exe to PATH»), это бесплатно. Вывод: `Python 3.12.x` и `10.10.10.10`. Дальше все примеры работают так же, только пути Windows (`C:\Support\Tickets`).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- UBU01 (10.10.10.30, Ubuntu Server 24.04, `adm.student`, SSH включён) — работает, доступен по SSH с WIN11-CL01.
- DC01 (10.10.10.10) — работает, отвечает на ping, слушает 53 (DNS) и 3389 (RDP). Домен `corp.vektor.lab` развёрнут (L1 №42), DNS-зона `corp.vektor.lab` есть (L1 №34).
- WIN11-CL01 (10.10.10.51, DHCP с L1 №33) — работает, вход `VEKTOR\ivanov` или `adm.student`.
- FS01 (10.10.10.20) — работает, общие папки `\\FS01\Obshchee`, `\\FS01\Buh`, `\\FS01\HR` (L1 №46).
- На UBU01 установлен `python3` (входит в Ubuntu Server 24.04). Проверить: `python3 --version`.
- На WIN11-CL01 Python может отсутствовать — если так, установить с python.org (Windows installer, «Add python.exe to PATH»).

### Снапшот

На хосте, в PowerShell:

```powershell
vmrun -T ws snapshot "C:\Lab\vektor\VMs\UBU01\UBU01.vmx" before-l1-080
vmrun -T ws snapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-080
```

WIN11-CL01 зашифрована (vTPM), поэтому для неё нужен пароль шифрования:

```powershell
vmrun -T ws -vp "Lab#2026-vektor" snapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-080
```

[ПРОВЕРИТЬ синтаксис `-vp` в твоей версии VMware Workstation.] DC01 и FS01 не трогаем — мы их только опрашиваем по сети, изменений нет.

### Шаги

1. **На WIN11-CL01** открой PowerShell, подключись к UBU01:
    ```powershell
    ssh adm.student@10.10.10.30
    ```
    Ожидаемо: приглашение `adm.student@ubu01:~$`.

2. **На UBU01** проверь Python:
    ```bash
    python3 --version
    ```
    Ожидаемо: `Python 3.12.x`.

3. **На UBU01** создай рабочую папку и первый скрипт — проверка TCP-портов:
    ```bash
    mkdir -p ~/scripts && cd ~/scripts
    cat > check_ports.py <<'PY'
    import argparse, socket, sys

    def check(host, port, timeout):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def main():
        ap = argparse.ArgumentParser(description="Проверка TCP-портов")
        ap.add_argument("host")
        ap.add_argument("-p", "--ports", default="22,53,3389,445",
                        help="список портов через запятую")
        ap.add_argument("-t", "--timeout", type=float, default=3.0)
        args = ap.parse_args()
        ports = [int(x) for x in args.ports.split(",")]
        bad = 0
        for p in ports:
            ok = check(args.host, p, args.timeout)
            print(f"{args.host}:{p:<5} {'открыт' if ok else 'закрыт'}")
            if not ok:
                bad += 1
        sys.exit(0 if bad == 0 else 1)

    if __name__ == "__main__":
        main()
    PY
    ```
    Ожидаемо: файл создан, ошибок нет.

4. **На UBU01** запусти проверку DC01:
    ```bash
    python3 check_ports.py 10.10.10.10
    ```
    Ожидаемо примерно: `10.10.10.10:22 закрыт`, `10.10.10.10:53 открыт`, `10.10.10.10:3389 открыт`, `10.10.10.10:445 открыт`. SSH на DC01 не настроен — порт 22 закрыт, это нормально. Код возврата будет 1 (есть закрытые порты) — проверь `echo $?`.

5. **На UBU01** запусти проверку FS01 (порт 445 — SMB):
    ```bash
    python3 check_ports.py 10.10.10.20 -p 445,3389
    ```
    Ожидаемо: `10.10.10.20:445 открыт`, `10.10.10.20:3389 открыт`. Если 445 закрыт — проверь, что FS01 включена и брандмауэр не блокирует SMB (на стенде правило SMB включено по умолчанию при роли «Файловый сервер»).

6. **На UBU01** напиши утилиту «разложить файлы по датам» — та самая задача Петровой:
    ```bash
    cat > sort_by_date.py <<'PY'
    import argparse, shutil
    from datetime import datetime
    from pathlib import Path

    def main():
        ap = argparse.ArgumentParser(description="Разложить файлы по папкам YYYY-MM")
        ap.add_argument("src", help="исходный каталог")
        ap.add_argument("--dry-run", action="store_true", help="только показать")
        args = ap.parse_args()
        src = Path(args.src)
        if not src.is_dir():
            raise SystemExit(f"нет каталога: {src}")
        moved = 0
        for f in src.iterdir():
            if not f.is_file():
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            dst_dir = src / mtime.strftime("%Y-%m")
            dst = dst_dir / f.name
            if args.dry_run:
                print(f"{f.name} -> {dst_dir.name}/")
                continue
            dst_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(dst))
            moved += 1
        print(f"перемещено: {moved}")

    if __name__ == "__main__":
        main()
    PY
    ```
    Ожидаемо: файл создан.

7. **На UBU01** создай тестовые файлы и прогони утилиту в режиме `--dry-run`:
    ```bash
    mkdir -p /tmp/inbox && cd /tmp/inbox
    for i in 1 2 3; do echo "test $i" > "file$i.txt"; done
    touch -d "2025-11-15" file1.txt
    touch -d "2025-12-01" file2.txt
    touch -d "2026-01-10" file3.txt
    python3 ~/scripts/sort_by_date.py /tmp/inbox --dry-run
    ```
    Ожидаемо примерно: `file1.txt -> 2025-11/`, `file2.txt -> 2025-12/`, `file3.txt -> 2026-01/`. Файлы на месте.

8. **На UBU01** прогони без `--dry-run` и проверь результат:
    ```bash
    python3 ~/scripts/sort_by_date.py /tmp/inbox
    find /tmp/inbox -type f | sort
    ```
    Ожидаемо: `перемещено: 3`, затем `find` покажет `/tmp/inbox/2025-11/file1.txt`, `/tmp/inbox/2025-12/file2.txt`, `/tmp/inbox/2026-01/file3.txt`.

9. **На UBU01** напиши утилиту «отчёт о доступности» — проверка списка хостов и запись в лог:
    ```bash
    cat > availability.py <<'PY'
    import argparse, logging, socket
    from pathlib import Path

    def main():
        ap = argparse.ArgumentParser(description="Отчёт о доступности хостов")
        ap.add_argument("--hosts-file", required=True)
        ap.add_argument("--port", type=int, default=445)
        ap.add_argument("--log", default="/tmp/availability.log")
        args = ap.parse_args()
        logging.basicConfig(filename=args.log, level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        hosts = [l.strip() for l in Path(args.hosts_file).read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
        up = 0
        for h in hosts:
            try:
                with socket.create_connection((h, args.port), timeout=3):
                    logging.info("%s:%s OK", h, args.port)
                    print(f"{h}: OK")
                    up += 1
            except OSError as e:
                logging.warning("%s:%s FAIL %s", h, args.port, e.__class__.__name__)
                print(f"{h}: FAIL")
        print(f"итого: {up}/{len(hosts)} доступны")

    if __name__ == "__main__":
        main()
    PY
    cat > hosts.txt <<'TXT'
    # серверы Вектора
    10.10.10.10
    10.10.10.20
    10.10.10.30
    TXT
    python3 availability.py --hosts-file hosts.txt --port 445
    ```
    Ожидаемо примерно: `10.10.10.10: OK`, `10.10.10.20: OK`, `10.10.10.30: FAIL` (на UBU01 нет SMB), `итого: 2/3 доступны`. Лог в `/tmp/availability.log`.

10. **На UBU01** проверь лог:
    ```bash
    cat /tmp/availability.log
    ```
    Ожидаемо примерно: три строки с датой, `INFO` для OK и `WARNING` для FAIL.

11. **На WIN11-CL01** (в PowerShell, не в SSH) проверь, что Python работает и там:
    ```powershell
    python --version
    python -c "import socket; print(socket.gethostbyname('dc01.corp.vektor.lab'))"
    ```
    Ожидаемо: `Python 3.12.x` и `10.10.10.10`. Если Python не установлен — установи с python.org и повтори.

12. **На WIN11-CL01** скопируй скрипт с UBU01 и запусти локально:
    ```powershell
    scp adm.student@10.10.10.30:~/scripts/check_ports.py C:\Support\check_ports.py
    python C:\Support\check_ports.py 10.10.10.20 -p 445
    ```
    Ожидаемо: `10.10.10.20:445 открыт`. Скрипт одинаково работает на Linux и Windows — в этом и сила Python.

### Проверка

- [ ] `python3 --version` на UBU01 показывает 3.12.x.
- [ ] `check_ports.py 10.10.10.10` показывает 53 и 3389 открытыми.
- [ ] `check_ports.py 10.10.10.20 -p 445` показывает 445 открытым.
- [ ] `sort_by_date.py /tmp/inbox --dry-run` показывает три папки по месяцам.
- [ ] После прогона без `--dry-run` файлы лежат в `/tmp/inbox/2025-11/`, `/tmp/inbox/2025-12/`, `/tmp/inbox/2026-01/`.
- [ ] `availability.py --hosts-file hosts.txt --port 445` показывает 2/3 доступны.
- [ ] `/tmp/availability.log` содержит строки с датой и уровнями INFO/WARNING.
- [ ] На WIN11-CL01 `python --version` работает, `check_ports.py` запускается.

### Сломай и почини

**Заявка:**

> **Пользователь:** Петрова М. (HR), ВМ WIN10-CL02.
> **Что написала:** «Ваш скрипт `availability.py` перестал работать. Запускаю — пишет `итого: 0/3 доступны`, хотя вчера всё было зелёное. Мне нужен отчёт для руководства к 15:00.»
> **Срочность:** высокая.

**Как воспроизвести поломку** (на UBU01, от `adm.student`):

```bash
sudo ufw enable
sudo ufw default deny outgoing
```

`ufw` на UBU01 в этом состоянии стенда не настроен (в L1 №39 разбирали теорию, но не включали), поэтому команды включат файрвол с политикой «запретить исходящие». Теперь любой TCP-коннект из Python будет падать с `OSError` (таймаут или `Permission denied`). Это ровно то, что видит Петрова: все хосты FAIL.

**Диагностика по шагам:**

1. Запусти скрипт вручную и посмотри на ошибку:
    ```bash
    python3 ~/scripts/availability.py --hosts-file hosts.txt --port 445
    ```
    Ожидаемо: `10.10.10.10: FAIL`, `10.10.10.20: FAIL`, `10.10.10.30: FAIL`, `итого: 0/3 доступны`.

2. Проверь, что сеть вообще жива — ping до DC01:
    ```bash
    ping -c 1 10.10.10.10
    ```
    Ожидаемо: ping проходит (ICMP разрешён на всех ВМ). Значит, проблема не в сети, а в чём-то локальном.

3. Проверь TCP вручную через `nc` (если установлен) или через Python:
    ```bash
    python3 -c "import socket; socket.create_connection(('10.10.10.10', 53), timeout=3)"
    ```
    Ожидаемо: `OSError` или `TimeoutError`. TCP не идёт, хотя ICMP идёт — типичный признак локального файрвола.

4. Посмотри статус файрвола:
    ```bash
    sudo ufw status verbose
    ```
    Ожидаемо: `Status: active`, `Default: deny (incoming), deny (outgoing)`.

5. Посмотри правила:
    ```bash
    sudo ufw status numbered
    ```
    Ожидаемо: пусто или только стандартные правила, исходящие запрещены.

**Решение:**

```bash
sudo ufw default allow outgoing
sudo ufw reload
python3 ~/scripts/availability.py --hosts-file hosts.txt --port 445
```

Ожидаемо: снова `2/3 доступны` (UBU01 без SMB — это норма). Если Петровой нужен только отчёт, файрвол можно оставить включённым с разрешёнными исходящими — это правильная настройка. Если файрвол на UBU01 не нужен вообще (учебный стенд), можно `sudo ufw disable`, но в боевой среде так не делают.

**Ответ пользователю:**

> Мария, добрый день. Проблема была не в скрипте, а в настройках файрвола на сервере, где он запускается: исходящие соединения были запрещены, поэтому скрипт не мог достучаться до серверов. Исправил, отчёт снова формируется, 2 из 3 хостов доступны (третий — сам сервер, на нём нет SMB, это норма). Прикладываю свежий лог. Если повторится — сразу пишите, разберёмся за 5 минут.

**Закрытие заявки:** категория «Скрипты и автоматизация», решение «исправлена политика ufw (default allow outgoing)», время — 15 минут, приоритет высокий → закрыто после подтверждения от Петровой.

### Что изменилось на стенде

- На UBU01 созданы файлы: `~/scripts/check_ports.py`, `~/scripts/sort_by_date.py`, `~/scripts/availability.py`, `~/scripts/hosts.txt`.
- На UBU01 создан каталог `/tmp/inbox` с подкаталогами `2025-11`, `2025-12`, `2026-01` и файлами внутри.
- На UBU01 создан `/tmp/availability.log`.
- На UBU01 в ходе «Сломай и почини» включался `ufw`; после решения — `default allow outgoing`, `ufw` остаётся `active` (если решил оставить) или `disabled` (если отключил). Зафиксируй в `stand-log.md`, что именно.
- На WIN11-CL01 скопирован `C:\Support\check_ports.py`; при необходимости установлен Python с python.org.
- Роли, домен, учётки, адреса — без изменений.

### Откат

В PowerShell на хосте:

```powershell
vmrun -T ws revertToSnapshot "C:\Lab\vektor\VMs\UBU01\UBU01.vmx" before-l1-080
vmrun -T ws -vp "Lab#2026-vektor" revertToSnapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-080
```

[ПРОВЕРИТЬ синтаксис `-vp` в твоей версии.] После отката все скрипты и `/tmp/inbox` исчезнут, `ufw` вернётся в состояние до лабораторной.

## 6. Ошибки новичков и безопасность

1. **Ошибка:** хардкодить IP и пароли в скрипте. **К чему приводит:** при смене адреса (например, DHCP в L1 №33 выдал другой) скрипт ломается, а пароль утекает в Git. **Как правильно:** аргументы через `argparse`, конфиг в файле, пароли — в переменных окружения или в защищённом хранилище (на стенде — просто не коммить).

2. **Ошибка:** `except:` без указания исключения. **К чему приводит:** скрипт «молча» глотает любую ошибку, включая `KeyboardInterrupt`, и ты не понимаешь, почему он не работает. **Как правильно:** ловить конкретные исключения (`OSError`, `socket.timeout`, `json.JSONDecodeError`) и логировать их.

3. **Ошибка:** читать весь лог в память через `read_text()`. **К чему приводит:** на файле в 2 ГБ скрипт съедает всю RAM и падает по OOM. **Как правильно:** `for line in open(path):` — построчно.

4. **Ошибка:** `subprocess.run(cmd, shell=True)` с подстановкой пользовательских данных. **К чему приводит:** инъекция команд (`; rm -rf /`). **Как правильно:** передавать список аргументов `["ping", "-c", "1", host]` без `shell=True`.

5. **Ошибка:** забыть `timeout` в `socket.create_connection`. **К чему приводит:** скрипт висит минутами на недоступном хосте. **Как правильно:** всегда `timeout=3` (или разумное значение).

6. **Ошибка:** запускать скрипт от `root`, потому что «так проще». **К чему приводит:** любая ошибка в скрипте — потенциальное удаление системных файлов. **Как правильно:** запускать от обычного пользователя, `sudo` — только там, где реально нужно (например, чтение `/var/log/auth.log`).

7. **Ошибка:** не проверять код возврата внешней команды. **К чему приводит:** скрипт считает, что `ping` сработал, хотя он вернул 1. **Как правильно:** `if r.returncode != 0: ...`.

8. **Ошибка:** коммитить `__pycache__`, `.venv`, логи и токены в Git. **К чему приводит:** мусор в репозитории, утечка секретов. **Как правильно:** `.gitignore` с `__pycache__/`, `*.log`, `.env`.

### Безопасность

- **Не запускай скрипты из интернета без чтения.** `curl ... | python3` — это выполнение произвольного кода с правами твоего пользователя. Скачай, прочитай, потом запускай.
- **Не храни пароли в скриптах и в Git.** На стенде пароль `Lab#2026-vektor` — учебный, но привычка должна быть правильной: в боевой среде пароль в коде = инцидент ИБ.
- **Не отключай файрвол «чтобы работало».** В лабораторной мы включили `ufw` и разрешили исходящие — это правильная настройка. `ufw disable` на боевом сервере — грубое нарушение.
- **Не давай скрипту больше прав, чем нужно.** Если скрипт только читает лог — он не должен работать от `root`.
- **Логируй, но не логируй секреты.** В `logging.info` не должно попадать содержимое токенов и паролей.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**Вопрос 1. Зачем сисадмину Python, если есть Bash и PowerShell?**

Как её задают: «У нас Linux и Windows, зачем ещё Python?»

Ответ: Bash хорош для однострочников и склейки команд, PowerShell — для Windows-специфики (AD, WMI, реестр). Python выигрывает там, где нужна логика: разбор JSON от API, работа с файлами и каталогами, кроссплатформенность (один скрипт работает и на Linux, и на Windows), нормальная обработка ошибок и тесты. На стенде я делал утилиту проверки портов — она одинаково запускается на UBU01 и на WIN11-CL01, а на Bash пришлось бы писать две версии.

Что хочет услышать интервьюер: понимание, что язык — инструмент под задачу, а не религия.

Красный флаг: «Python лучше всех, Bash не нужен».

**Вопрос 2. Как проверить, что сервис на хосте доступен, из Python?**

Как её задают: «Напиши проверку, что порт 445 на FS01 открыт».

Ответ: `socket.create_connection((host, port), timeout=3)` в блоке `try/except OSError`. Если соединение открылось — порт слушается, если `ConnectionRefusedError` — порт закрыт, если `socket.timeout` — хост недоступен или фильтруется. Важно ставить `timeout`, иначе скрипт зависнет. На стенде я так проверял 445 на FS01 (10.10.10.20) и 53/3389 на DC01 (10.10.10.10).

Что хочет услышать интервьюер: понимание TCP, таймаутов и различия между «закрыт» и «недоступен».

Красный флаг: «просто пингануть» — ping проверяет ICMP, а не сервис.

**Вопрос 3. Как сделать HTTP-запрос к API и разобрать ответ?**

Как её задают: «Как из Python дернуть REST API и достать поле из JSON?»

Ответ: `urllib.request.urlopen(url, timeout=10)` возвращает объект с `.status` и `.read()`. Дальше `json.loads(response.read())` — получаем словарь, достаём поле через `.get("key")`. Для POST — `urllib.request.Request(url, data=..., headers=..., method="POST")`. Сторонняя `requests` удобнее, но требует `pip install`; стандартная библиотека работает везде.

Что хочет услышать интервьюер: понимание HTTP, JSON и того, что API — это обычный текстовый протокол поверх TCP.

Красный флаг: «не знаю, я только curl использую».

**Вопрос 4. Как разложить файлы по папкам в зависимости от даты?**

Как её задают: «У пользователя 300 файлов на рабочем столе, надо разложить по месяцам».

Ответ: `pathlib.Path(src).iterdir()` — обход файлов, `f.stat().st_mtime` — время изменения, `datetime.fromtimestamp(...).strftime("%Y-%m")` — имя папки, `shutil.move` — перемещение. Обязательно режим `--dry-run`, чтобы сначала показать, что будет сделано, и только потом двигать. На стенде я так делал `sort_by_date.py` для Петровой.

Что хочет услышать интервьюер: аккуратность (dry-run), понимание `pathlib` и `shutil`, безопасность (не удалять, а перемещать).

Красный флаг: «просто `os.rename` в цикле без проверок».

**Вопрос 5. Как сделать скрипт удобным для коллег?**

Как её задают: «Коллега будет запускать твой скрипт, что нужно сделать?»

Ответ: аргументы через `argparse` с `--help`, разумные значения по умолчанию, понятные сообщения об ошибках, код возврата 0/1 через `sys.exit`, логирование через `logging` в файл, `--dry-run` для опасных операций. Плюс короткий README с примером запуска. На стенде `check_ports.py` и `availability.py` сделаны именно так.

Что хочет услышать интервьюер: понимание, что скрипт — это продукт для людей, а не для тебя одного.

Красный флаг: «запускать `python script.py` без аргументов и править код под каждую задачу».

## 8. Источники (официальная документация)

- **Python.org** (домен `python.org`) — раздел «The Python Tutorial», главы «Input and Output», «Errors and Exceptions», «Brief Tour of the Standard Library». Поиск: `python tutorial pathlib`, `python tutorial socket`.
- **Python.org** — «Library Reference»: модули `pathlib`, `socket`, `urllib.request`, `json`, `argparse`, `logging`, `subprocess`, `shutil`. Поиск: `python pathlib documentation`, `python socket create_connection`.
- **Python.org** — «Python Setup and Usage», раздел про установку на Windows. Поиск: `python windows installer add to path`.
- **Ubuntu Documentation** (домен `ubuntu.com`) — раздел «Ubuntu Server», «Package management» (для `apt install python3-...`). Поиск: `ubuntu server python3`.
- **VMware Workstation Documentation** (домен `vmware.com` или `techdocs.broadcom.com` после смены владельца) — раздел про `vmrun` и снапшоты. Поиск: `vmrun snapshot revertToSnapshot`. [ПРОВЕРИТЬ актуальный домен документации VMware в 2026 году.]
- **Microsoft Learn** (домен `learn.microsoft.com`) — раздел «PowerShell» и «Windows Package Manager» (для установки Python на WIN11-CL01). Поиск: `powershell python install winget`.
