# Python для DevOps: venv, poetry/uv, работа с файлами, процессами, REST API, парсинг логов

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди DevOps:

```
Автор: Сидоров Алексей (склад), ВМ WIN10-CL02 → сайт отчётов в DMZ
Тема: Страница отчётов по складу периодически отдаёт 502
Текст: «Открываю отчёт по приёмке — через раз ошибка 502 Bad Gateway.
       Вчера в 15:40 вообще полчаса не открывалось. Можно как-то понять,
       сколько раз это было и в какие часы?»
Срочность: средняя
```

Дежурный DevOps идёт на WEB01, видит nginx и груду строк в `/var/log/nginx/access.log`, и понимает: глазами это не считается. Нужен маленький скрипт, который раз в сутки вытащит из логов все ответы 5xx, сгруппирует по часам, положит отчёт в JSON и, если ошибок больше порога, заведёт issue в GitLab через REST API.

Python в DevOps — это «клей»: посчитать, распарсить, дёрнуть API, запустить утилиту, проверить, что сервис жив, сгенерировать конфиг. Ansible, AWS/GitLab/Harbor SDK, большинство exporter'ов и половина CI-скриптов — Python. После этой главы ты будешь уметь: поднимать изолированное окружение (`venv`, `uv`, `poetry`), читать файлы не убивая память, запускать внешние команды безопасно и проверяемо, ходить в REST API с таймаутами и ретраями и парсить логи регулярками так, чтобы результат можно было проверить. Написание CLI-утилит, упаковка и pytest — в уроке 21, здесь мы делаем рабочую основу.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Интерпретатор и пакеты.** `python3` — это бинарник-интерпретатор. Он ищет модули в списке путей `sys.path`: сначала текущая папка, потом стандартная библиотека (`/usr/lib/python3.12`), потом `site-packages` — куда `pip` кладёт сторонние пакеты. На Ubuntu 24.04 системный Python 3.12 помечен как *externally managed* (PEP 668): `apt` управляет пакетами `python3-*`, и если ты поставишь свой `requests` через системный `pip`, ты можешь сломать системные утилиты, которые от него зависят. Поэтому `pip install` без venv отвечает ошибкой `externally-managed-environment`. Вывод: **всё стороннее — только в venv или через `uv`**.

**Что такое venv физически.** Это папка (например `/opt/vektor/venv`) с копией/симлинками на интерпретатор, своим `bin/activate` (правит `PATH` и `VIRTUAL_ENV`) и своим `lib/python3.12/site-packages`. Активация — это просто правка переменных окружения в текущей оболочке; из cron она не работает, поэтому там вызывают абсолютный путь `/opt/vektor/venv/bin/python`.

**Менеджеры проектов.** `requirements.txt` — плоский список «что поставить». `pyproject.toml` (стандарт PEP 621) — метаданные проекта и зависимости; `poetry` и `uv` читают его и пишут **lock-файл** (`poetry.lock`, `uv.lock`) с точными версиями и хешами. Lock-файл коммитится в Git: он гарантирует, что на ноутбуке, в CI и на проде встанут одни и те же версии. `uv` (Astral) — быстрый менеджер на Rust, умеет и роль «замена pip+venv», и роль полноценного менеджера проекта; `poetry` — зрелый и привычный в командах.

**Процессы.** Скрипт может запускать другие программы: `subprocess.run(["systemctl", "is-active", "nginx"], capture_output=True, text=True)`. Ключевой момент: список аргументов вместо строки с `shell=True`. С `shell=True` строка уходит в `/bin/sh`, и подставленный извне текст (имя файла, ID из тикета) может выполнить чужую команду — это классическая инъекция.

**REST API.** HTTP поверх TCP, обычно 443 (TLS) или 80. Метод + путь + заголовки + тело. Авторизация — заголовок (`PRIVATE-TOKEN: <token>` для GitLab, `Authorization: Bearer <token>` для Harbor). Ответ — JSON, статус в первой строке (`200`, `201`, `401`, `429`, `5xx`). Для скрипта обязательны: `timeout`, проверка статуса (`raise_for_status()`), пагинация (GitLab отдаёт страницами, `?per_page=100&page=2`) и разумные ретраи на 429/5xx.

**Логи.** nginx пишет строку на запрос; формат `combined` состоит из пробело-разделённых полей с кавычками. Парсить надо регуляркой с именованными группами и `re.compile()` (компиляция один раз). Файл в 5 ГБ нельзя читать `readlines()` — только построчно, это поток, память O(1).

```text
   UBU01 (10.10.10.30)                      DMZ 10.10.20.0/24
 ┌──────────────────────────┐    SSH/22   ┌──────────────────────┐
 │ /opt/vektor/venv         │◄── scp ─────│ WEB01 10.10.20.10    │
 │   └ bin/python (3.12)    │   логи      │  nginx :80/:443      │
 │ /opt/vektor/scripts/     │             │  /var/log/nginx/*.log│
 │   parse_nginx.py         │             └──────────────────────┘
 │ /opt/vektor/logs/*.log   │
 └───────────┬──────────────┘
             │ HTTPS/HTTP API :443|:80, заголовок PRIVATE-TOKEN
             ▼
 ┌──────────────────────────┐
 │ GIT01 10.10.10.50        │  GitLab CE, /api/v4/...
 └──────────────────────────┘

 Пакеты: PyPI (HTTPS/443) → venv/uv → uv.lock/poetry.lock → Git
```

## 3. Аналогия из жизни

Представь кухню ресторана. Системный Python — это **общественная кухня здания**: плиты общие, и если ты переставишь кастрюли, у соседей сгорит обед (сломается `apt`). `venv` — **отдельная персональная кухня**: свои ножи, свои специи, соседям не мешаешь. `pyproject.toml` — **меню**, где написано, какие блюда ты вообще делаешь; lock-файл — **закупочная накладная с точными артикулами и партиями**: «мука марки X, партия 42» — чтобы завтра блюдо вышло ровно таким же. `subprocess` — **подрядчик**, которому ты звонишь и просишь что-то сделать, а не делаешь сам (и которому надо называть задание по пунктам, а не давать право переписать всю кухню — это и есть разница между списком аргументов и `shell=True`). REST API — **окно выдачи**: ты называешь блюдо и показываешь пропуск (токен), тебе отвечают «готово» (200) или «нет такого» (404). Парсинг логов — **разбор чеков за день**: их много, они одинакового формата, и ты выписываешь на доску только то, что нужно (сколько позиций, какие официанты, где минус). Аналогия ломается на безопасности: повар, испортивший соус, не сможет украсть ключи от сейфа, а скрипт с токеном в коде — вполне.

## 4. Практика и команды

**1. Проверить, что и где стоит**

```bash
python3 --version
which -a python3
```
```text
Python 3.12.3
/usr/bin/python3
```
`--version` — версия; `which -a` — все интерпретаторы в `PATH` (полезно, когда «а у меня другой Python»). Смотреть: одна ли версия, не висит ли лишний из `~/.local/bin`.

**2. Поставить инструменты (Ubuntu 24.04)**

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
```
`python3-venv` обязателен: без него `python3 -m venv` падает с `ensurepip is not available`.

**3. Создать venv и активировать**

```bash
python3 -m venv /opt/vektor/venv
source /opt/vektor/venv/bin/activate
python -c "import sys; print(sys.prefix)"
```
```text
/opt/vektor/venv
```
`sys.prefix` — корень окружения. Если он равен `/usr`, ты НЕ в venv. Выход — `deactivate`.

**4. Поставить пакет и зафиксировать зависимости**

```bash
python -m pip install --upgrade pip
python -m pip install "requests==2.32.3"
python -m pip freeze > /opt/vektor/requirements.txt
```
`freeze` — список установленного в формате требований. Смотреть: не попали ли лишние пакеты (их потом никто не отличит от нужных).

**5. Убедиться, что PEP 668 работает (и это хорошо)**

```bash
/usr/bin/python3 -m pip install requests
```
```text
error: externally-managed-environment
```
Так система защищает себя. Правильный путь — venv.

**6. uv: быстрое окружение и проект**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # [ПРОВЕРИТЬ] актуальный способ установки
uv venv /opt/vektor/.venv
uv pip install requests
uv init stand-ops && cd stand-ops
uv add requests
uv sync
uv run python -c "import requests; print(requests.__version__)"
```
`uv venv` — окружение; `uv add` — добавить зависимость в `pyproject.toml` и `uv.lock`; `uv sync` — привести окружение к lock-файлу; `uv run` — запустить в окружении без активации.

**7. poetry: то же самое, но с историей**

```bash
python3 -m pip install --user poetry
poetry new stand-ops && cd stand-ops
poetry add requests
poetry install
poetry run python -c "import requests; print(requests.__version__)"
```
`poetry add` правит `pyproject.toml` и `poetry.lock`; `poetry install` ставит ровно по lock. Версии poetry быстро меняются — команды сверяй с документацией `[ПРОВЕРИТЬ]`.

**8. Чтение файла через pathlib (безопасно по памяти)**

```python
from pathlib import Path
log = Path("/opt/vektor/logs/access.log")
for line in log.open(encoding="utf-8", errors="replace"):
    if " 502 " in line:
        print(line.rstrip())
```
`open()` в цикле `for` отдаёт строку за строкой — память не растёт. `errors="replace"` спасает от битых байтов.

**9. Запуск внешней команды**

```python
import subprocess
res = subprocess.run(["systemctl", "is-active", "nginx"],
                     capture_output=True, text=True, timeout=10)
print(res.returncode, res.stdout.strip())
```
`capture_output` — собрать stdout/stderr; `text=True` — строки вместо байтов; `timeout` — не висеть вечно; `check=True` (если нужен) — исключение на ненулевом коде. `returncode` смотреть всегда: `systemctl` вернёт `0` только для активного юнита.

**10. GET в REST API с таймаутом**

```python
import requests
r = requests.get("http://10.10.10.50/api/v4/projects",
                 headers={"PRIVATE-TOKEN": TOKEN},
                 params={"per_page": 100, "page": 1},
                 timeout=(3, 10))
r.raise_for_status()
for p in r.json():
    print(p["id"], p["path_with_namespace"])
```
`timeout=(connect, read)` — обязателен, иначе скрипт повиснет на мертвом сервисе; `raise_for_status()` — исключение на 4xx/5xx; `params` — кодирование query-строки. Смотреть на заголовок `X-Next-Page` для пагинации.

**11. POST: создать issue**

```bash
curl -sS -X POST "http://10.10.10.50/api/v4/projects/vektor%2Fstand-ops/issues" \
  -H "PRIVATE-TOKEN: $GIT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"5xx на WEB01 за сутки: 37","description":"Отчёт во вложении"}'
```
`%2F` — закодированный `/` в пути проекта. Ответ `201` — issue создан; `401` — плохой токен; `400` — тело не по схеме. В Python то же самое: `requests.post(url, headers=..., json={...}, timeout=10)`.

**12. Разбор лога регуляркой и подсчёт**

```bash
python - <<'PY'
import re, collections
LINE = re.compile(r'^(?P<ip>\S+).*?"(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) ')
c = collections.Counter()
for line in open("/opt/vektor/logs/access.log", encoding="utf-8", errors="replace"):
    m = LINE.match(line)
    if m and m["status"].startswith("5"):
        c[m["path"]] += 1
print(c.most_common(5))
PY
```
Именованные группы (`?P<status>`) читаются глазами лучше, чем `group(3)`. `Counter.most_common(n)` — топ-N. Пути по GUI здесь нет: всё в терминале.

## 5. Лабораторная работа

### Предусловия
- **UBU01** (10.10.10.30): Ubuntu Server 24.04, SSH-доступ, `sudo`. Здесь живёт venv, скрипты и скачанные логи.
- **WEB01** (10.10.20.10): nginx в DMZ, доступ по SSH с UBU01 (ключ из урока 12), логи в `/var/log/nginx/`.
- **GIT01** (10.10.10.50): GitLab CE из урока 17, есть проект `vektor/stand-ops`, ты владелец. Если проект не создан — создай через веб-интерфейс.
- Токен GitLab: Settings → Access Tokens, scope `api`, срок 30 дней. Токен — только в файл с правами `600`.
- Windows-часть стенда не нужна.

### Снапшот
В VMware Workstation на **UBU01**, **WEB01** и **GIT01** сделай снапшот с именем `before-devops-020` (VM → Snapshot → Take Snapshot). Для GIT01 — если он критичен: снапшот при остановленной ВМ, иначе консистентность БД GitLab не гарантирована.

### Шаги

1. **UBU01.** `sudo mkdir -p /opt/vektor/{scripts,logs} && sudo chown -R $USER:$USER /opt/vektor`. Ожидаемо: каталоги есть, `ls -ld /opt/vektor/logs` показывает твоего владельца.
2. **UBU01.** `python3 --version` → `3.12.x`; `sudo apt install -y python3-venv` если ещё не стоит.
3. **UBU01.** `python3 -m venv /opt/vektor/venv && source /opt/vektor/venv/bin/activate && python -m pip install -U pip requests`. Ожидаемо: приглашение с `(venv)`, `pip list` показывает `requests`.
4. **UBU01.** `pip freeze > /opt/vektor/requirements.txt && cat /opt/vektor/requirements.txt`. Ожидаемо: `requests==2.32.x`, `urllib3`, `certifi`, `charset-normalizer`, `idna`.
5. **UBU01.** Стяни логи: `scp <user>@10.10.20.10:/var/log/nginx/access.log /opt/vektor/logs/access-$(date +%F).log`. Ожидаемо: файл непустой, `wc -l` > 0. Если логов мало — на WEB01 сделай `for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1/nope; done` чтобы получить 404-записи, а 5xx появятся позже сам.
6. **UBU01.** Создай `/opt/vektor/scripts/parse_nginx.py`:

```python
#!/usr/bin/env python3
import json, re, sys, collections
from datetime import datetime
from pathlib import Path

LINE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>\S+) [^"]*" '
    r'(?P<status>\d{3}) (?P<size>\d+|-)'
)

def parse(path: Path):
    by_hour = collections.Counter()
    by_path = collections.Counter()
    total = errors = 0
    for line in path.open(encoding="utf-8", errors="replace"):
        m = LINE.match(line)
        if not m:
            continue
        total += 1
        if int(m["status"]) < 500:
            continue
        errors += 1
        try:
            ts = datetime.strptime(m["time"], "%d/%b/%Y:%H:%M:%S %z")
        except ValueError:
            continue
        by_hour[ts.strftime("%Y-%m-%d %H:00")] += 1
        by_path[m["path"]] += 1
    return {"file": str(path), "total": total, "errors_5xx": errors,
            "by_hour": dict(by_hour), "top_paths": by_path.most_common(5)}

if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/vektor/logs/access.log")
    result = parse(src)
    print(json.dumps(result, ensure_ascii=False, indent=2))
```

7. **UBU01.** Запусти: `/opt/vektor/venv/bin/python /opt/vektor/scripts/parse_nginx.py /opt/vektor/logs/access-*.log > /opt/vektor/logs/report-$(date +%F).json`. Ожидаемо: JSON с `total`, `errors_5xx`, `by_hour`. Проверь `json.tool`: `python -m json.tool report-*.json > /dev/null && echo OK`.
8. **UBU01.** Токен: `install -m 600 /dev/null /opt/vektor/.env` и впиши `GIT_TOKEN=glpat-...`, затем `chmod 600 /opt/vektor/.env`. Проверь `ls -l /opt/vektor/.env` → `-rw-------`.
9. **UBU01.** Проверь API вручную: `curl -sS -H "PRIVATE-TOKEN: $GIT_TOKEN" "http://10.10.10.50/api/v4/projects?membership=true&per_page=5" | head -c 400`. Ожидаемо: JSON-массив проектов, включая `vektor/stand-ops`, и виден её `id`.
10. **UBU01.** Добавь в скрипт отправку issue, если `errors_5xx > 0`:

```python
import os, requests
def create_issue(project_id: int, report: dict) -> str:
    token = os.environ["GIT_TOKEN"]
    r = requests.post(
        f"http://10.10.10.50/api/v4/projects/{project_id}/issues",
        headers={"PRIVATE-TOKEN": token},
        json={"title": f"5xx на WEB01: {report['errors_5xx']}",
              "description": "```json\n" + json.dumps(report, indent=2) + "\n```"},
        timeout=(3, 10),
    )
    r.raise_for_status()
    return r.json()["web_url"]
```
Запуск: `set -a; . /opt/vektor/.env; set +a; /opt/vektor/venv/bin/python /opt/vektor/scripts/parse_nginx.py ...`. Ожидаемо: в GitLab появился issue со ссылкой.
11. **UBU01.** Поставь в cron: `crontab -e` и строку `15 7 * * * cd /opt/vektor && . ./.env && ./venv/bin/python scripts/parse_nginx.py logs/access-$(date +\%F).log >> logs/cron.log 2>&1`. Обрати внимание: `%` в crontab экранируется как `\%`, а интерпретатор — абсолютный путь.
12. **UBU01.** Проверь, что cron-запуск работает сейчас: `env -i /bin/sh -c 'cd /opt/vektor && . ./.env && ./venv/bin/python scripts/parse_nginx.py logs/access-2026-XX-XX.log | head -3'`. Ожидаемо: тот же JSON, что руками. Это имитация «пустого» окружения cron.

### Проверка
- [ ] `sys.prefix` внутри venv указывает на `/opt/vektor/venv`
- [ ] `/opt/vektor/requirements.txt` содержит точные версии и лежит в Git-репозитории проекта
- [ ] Скрипт возвращает валидный JSON с ненулевым `total`
- [ ] `errors_5xx` совпадает с ручным подсчётом: `grep -c '" 5' access-*.log`
- [ ] `/opt/vektor/.env` имеет права `600` и не попал в Git
- [ ] issue в `vektor/stand-ops` создан и содержит отчёт
- [ ] Запуск через `env -i` не падает

### Сломай и почини

```
Пользователь: Алексей Сидоров (склад) → дежурный DevOps
ВМ: WEB01 (страница отчётов), скрипт на UBU01
Что написал: «Каждое утро смотрю сводку по ошибкам склада, сегодня пусто.
             Вчера вечером 502 точно были — я сам видел. Ничего не трогал.»
Срочность: средняя (до 12:00 нужно понять, были ли ошибки за вчера)
```

**Как воспроизвести поломку.** На UBU01 убери venv из уравнения: в cron замени `./venv/bin/python` на `python3` и запусти `env -i /bin/sh -c 'python3 /opt/vektor/scripts/parse_nginx.py /opt/vektor/logs/access.log'`.

**Диагностика по шагам.**
1. Ручной запуск: `/opt/vektor/venv/bin/python scripts/parse_nginx.py ...` — работает? Значит дело не в скрипте, а в окружении.
2. `tail -50 /opt/vektor/logs/cron.log` — видим `ModuleNotFoundError: No module named 'requests'`.
3. `journalctl -u cron --since "today"` — подтверждаем, что cron вообще стартовал (иначе проблема в самом расписании).
4. `env -i /bin/sh -c 'which python3; python3 -c "import sys; print(sys.prefix)"'` — видим `/usr` вместо venv. Причина найдена: cron не выполняет `activate`, а `PATH` у него минимальный.
5. Проверить, что переменная `GIT_TOKEN` тоже не доезжает (симптом «тихо ничего не делает»).

**Решение.** В crontab — только абсолютные пути: `SHELL=/bin/bash`, `PATH=/usr/local/bin:/usr/bin:/bin`, вызов `/opt/vektor/venv/bin/python`, явный `. /opt/vektor/.env`, обязательный редирект `>> logs/cron.log 2>&1` (иначе ошибки уходят в почту, которую никто не читает). Дополнительно `flock -n /tmp/parse_nginx.lock` — чтобы два запуска не наложились. Для systemd-таймера (предпочтительнее) то же самое делается через `Environment=` и `ExecStart=`.

**Ответ пользователю:**
> Алексей, добрый день. Отчёт не сформировался из-за ошибки в окружении планировщика — скрипт запускался системным Python без нужных библиотек, поэтому тихо падал. Исправил путь к интерпретатору и включил логирование ошибок. Данные за вчера не потеряны: сейчас пересчитаю и пришлю сводку по 502 с разбивкой по часам. Если снова увидишь 502 — пришлите примерное время, это ускорит поиск.

**Закрытие заявки.** Пересчитать отчёт за прошлые сутки, приложить `report-*.json`, отдельным issue в `vektor/stand-ops` завести задачу «перенести парсер на systemd timer с логированием», закрыть тикет со ссылкой на issue.

### Что изменилось на стенде
- UBU01: создан `/opt/vektor/venv` (Python 3.12 + requests), `/opt/vektor/requirements.txt`.
- UBU01: `/opt/vektor/scripts/parse_nginx.py` — парсер access.log с отправкой issue.
- UBU01: `/opt/vektor/logs/` — копии логов WEB01 и JSON-отчёты; `/opt/vektor/.env` (600) с `GIT_TOKEN`.
- UBU01: запись в crontab пользователя (7:15 ежедневно).
- GIT01: проект `vektor/stand-ops`, созданный через API issue с первым отчётом.
- Записать всё это в `stand-log.md`.

### Откат
VM → Snapshot Manager → `before-devops-020` → Revert, на UBU01, WEB01 и GIT01. После отката убедись, что `crontab -l` пуст, а проект `vektor/stand-ops` в GitLab не остался «полупустым» — если снапшот GIT01 не делался, удали ветку/проект вручную либо оставь как часть стенда и зафиксируй в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **`sudo pip install` в системный Python** → конфликт с пакетами `apt`, сломанные системные утилиты, `externally-managed-environment`. → Всегда venv/uv, системный Python не трогать.
2. **`.venv` в Git** → репозиторий на сотни мегабайт и битые пути. → `.gitignore` с `.venv/`, `__pycache__/`, `*.pyc`, `.env`.
3. **Зависимости без фиксации версий** → через месяц «на проде другая версия, у меня работает». → `requirements.txt` с `==` или lock-файл, оба в Git.
4. **`requests.get()` без `timeout`** → скрипт висит до TCP-таймаута ОС (минуты), в cron копятся процессы. → `timeout=(connect, read)`, ретраи только на 429/5xx, ограниченное число попыток.
5. **`subprocess.run(cmd, shell=True)` с внешними данными** → инъекция команд. → Список аргументов, `shell=False`, валидация входных значений.
6. **`open().readlines()` на большом логе** → OOM-killer (ты уже видел его в уроке 13). → Построчная итерация, генераторы, `Counter` вместо списков.
7. **Парсинг «на глазок» через `split()`** → ломается на пути с пробелом или нестандартном user-agent. → Регулярка с именованными группами + `tests/` (подробно в уроке 21).
8. **Токен в коде или в Git-истории** → утечка доступа к GitLab/облаку. → `.env` с правами `600`, переменные окружения, токены с минимальным scope и сроком, ротация при подозрении.

**Безопасность.** В боевой среде нельзя: держать токены в репозитории (даже в приватном — история не чистится сама), запускать скрипты от `root` без нужды, складывать `access.log` с IP пользователей в общедоступную папку, ставить пакеты без проверки источника (`pip install --index-url` на левое зеркало = произвольный код на сервере). В лабе мы сознательно держим токен в файле на диске и ограничиваемся `600` — в продакшене это делают через secret-хранилище (Vault, GitLab CI-переменные, Kubernetes Secret), а не файлом рядом со скриптом. Установка `uv` через `curl | sh` в лабе допустима, но в компании такой паттерн требует проверки контрольной суммы или установки из пакета.

## 7. Вопросы на собеседовании

**1. Зачем нужен venv, если есть Docker?**
Ответ: venv изолирует **Python-зависимости** на уровне процесса и работает без Docker — на хосте, в CI-раннере, в cron, в контейнере тоже. Docker изолирует всё приложение целиком вместе с ОС-слоем, но внутри контейнера всё равно нужен свой site-packages. Это разные уровни: venv — про версии библиотек, контейнер — про упаковку и доставку артефакта. На сервере, где нельзя ставить Docker (например, по требованиям безопасности), venv остаётся единственным способом не сломать системный Python.
*Что хочет услышать:* понимание, что это не «или-или», и что venv не отменяет контейнеры.
*Красный флаг:* «venv — устаревшая штука, все давно в докере».

**2. Чем `requirements.txt` отличается от `pyproject.toml` и lock-файла?**
Ответ: `requirements.txt` — плоский список для `pip`, без метаданных проекта. `pyproject.toml` — стандартный манифест (PEP 621): имя, версия, зависимости, entry points, настройки линтеров. Lock-файл (`poetry.lock`, `uv.lock`) фиксирует **полное транзитивное дерево** с хешами — воспроизводимая сборка. В Git обычно коммитят `pyproject.toml` + lock, а `requirements.txt` генерируют из lock для тех, кто ставит через `pip`.
*Что хочет услышать:* «воспроизводимость» и «транзитивные зависимости».
*Красный флаг:* «lock-файл не нужен, у нас же `>=` в зависимостях».

**3. Почему `subprocess.run(..., shell=True)` опасен?**
Ответ: строка уходит в `/bin/sh`, поэтому метасимволы (`;`, `|`, `$(...)`, бэктики) интерпретируются оболочкой. Если в строку попадает пользовательский ввод — имя файла из тикета, ID из API — это command injection. Правильно: список аргументов (`["tar", "-czf", path, src]`), `shell=False` по умолчанию, `check=True`/явная проверка `returncode`, `timeout`, `capture_output=True, text=True` для разбора вывода.
*Что хочет услышать:* конкретный пример инъекции и практику передачи списка.
*Красный флаг:* «`shell=True` удобнее, я так всегда пишу».

**4. Как сделать надёжный вызов REST API из скрипта?**
Ответ: базовый URL и токен — из переменных окружения; `timeout` на connect и read; `raise_for_status()`; ретраи с экспоненциальной задержкой только для 429/5xx и сетевых ошибок, без ретраев на 4xx (кроме 429); учёт пагинации (`X-Next-Page` в GitLab, `Link` в GitHub); логирование request-id; идемпотентность при повторах POST (уникальный ключ в теле или проверка, не создан ли объект); ограничение rate limit и уважение к `Retry-After`.
*Что хочет услышать:* таймауты, ретраи, пагинация, идемпотентность.
*Красный флаг:* «Просто `requests.get` в цикле `while True`».

**5. Как распарсить лог в 20 ГБ, не съев память?**
Ответ: читать потоком — `for line in f` или `subprocess.Popen(...).stdout` — это O(1) по памяти. Не собирать все строки в список; агрегировать на лету (`collections.Counter`, словари по часам). Предкомпилировать регулярку через `re.compile` с именованными группами, не матчить лишние строки. Для больших объёмов — `gzip.open`, `mmap` при случайном доступе, а в пределе — log-шипер (Vector, Fluent Bit, Promtail) и агрегатор (Loki, Elasticsearch), а не собственный скрипт.
*Что хочет услышать:* «стриминг и агрегация на лету», понимание границ применимости своего скрипта.
*Красный флаг:* «`f.readlines()`, потом `re.findall` по всему тексту».

## 8. Источники (официальная документация)

- docs.python.org — разделы: «venv — Creation of virtual environments», «subprocess — Subprocess management», «pathlib — Object-oriented filesystem paths», «re — Regular expression operations», «collections — Container datatypes», «json — JSON encoder and decoder». Поиск: `python venv tutorial`, `python subprocess run capture_output`.
- packaging.python.org — «Python Packaging User Guide», разделы про `pyproject.toml`, зависимостями и lock-файлами `[ПРОВЕРИТЬ актуальный путь раздела]`.
- docs.astral.sh — документация `uv`: установка, `uv venv`, `uv add`, `uv sync`, `uv run`. Поиск: `uv python package manager docs`.
- python-poetry.org — «Poetry documentation», разделы Basic usage и Managing dependencies.
- requests.readthedocs.io — «Requests: HTTP for Humans», разделы Quickstart и Advanced (timeouts, sessions, retries).
- docs.gitlab.com — «GitLab API», разделы «REST API»: issues, projects, authentication, pagination. Поиск: `gitlab api create issue rest`.
- nginx.org — «ngx_http_log_module», директивы `log_format` и `access_log` (формат `combined`).
- 12factor.net — «The Twelve-Factor App», раздел «Config» (почему настройки в переменных окружения, а не в коде).
