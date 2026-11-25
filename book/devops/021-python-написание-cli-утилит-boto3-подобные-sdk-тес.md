# Python: CLI-утилиты, SDK в стиле boto3, тесты на pytest и упаковка

## 1. Введение (зачем это нужно в реальной работе)

Заявка в поддержку:

> **Пользователь:** sidorov (разработчик, доступ к GIT01)
> **ВМ:** GIT01 / UBU01
> **Что написал:** «Каждый раз, когда чистим старые образы в HARBOR01, я руками кликаю по веб-морде и путаюсь в проектах. И ещё: pipeline падает на деплое, потому что Harbor лежит, а мы узнаём об этом в самый последний момент. Сделайте нормальную утилиту, чтобы из терминала и из CI можно было проверить реестр и получить список проектов.»
> **Срочность:** средняя

Дальше вы почти всегда будете делать одно и то же: брать чужой HTTP API и превращать его в удобную команду, которую можно вызвать руками и из пайплайна. Разница между «скриптом на коленке» и нормальной утилитой ровно в четырёх вещах: понятный CLI с кодами возврата, слой SDK с ретраями и ошибками, тесты без реальной сети и упаковка, которую можно поставить одной командой. После главы вы соберёте `vkt` — маленькую утилиту для стенда «Вектор», — покроете её pytest-тестами, соберёте wheel и прогоните через GitLab CI. Это ровно то, что просят на собеседовании, когда говорят «напишите небольшой инструмент и покажите, как тестируете».

## 2. Теория «под капотом»

Любая такая утилита — слоёный пирог. Важно, чтобы слои не протекали друг в друга: CLI не знает про HTTP, SDK не печатает в консоль.

```text
  argv / env / CI-переменные
            │
            ▼
 ┌──────────────────────────────┐
 │ CLI-слой (argparse)          │  парсинг, валидация, формат вывода,
 │ src/vektor/cli.py            │  коды возврата, stdout vs stderr
 └──────────────┬───────────────┘
                │ вызовы методов
                ▼
 ┌──────────────────────────────┐
 │ SDK-слой (HarborClient)      │  endpoint, auth, таймауты, ретраи,
 │ src/vektor/sdk/client.py     │  пагинация, модели, исключения
 └──────────────┬───────────────┘
                │ HTTP/1.1 + JSON + TLS (L7)
                ▼
 ┌──────────────────────────────┐
 │ REST API HARBOR01:443        │  /api/v2.0/...
 └──────────────────────────────┘
```

Что именно значит «SDK в стиле boto3»:

- **Client + Config.** У boto3 есть `boto3.client(...)` и `botocore.config.Config(...)`, куда выносят `endpoint_url`, `retries`, `timeout`, `verify`. У нас аналог — параметры конструктора `HarborClient`.
- **Цепочка получения учётных данных.** У boto3 это env → файл профиля → IAM-роль. У нас попроще: `--username/--password` → переменные окружения `VKT_USER`/`VKT_PASSWORD`. Принцип тот же: код не должен знать, откуда взялся секрет.
- **Пагинаторы.** API отдаёт данные страницами (`page`, `page_size`), SDK сам крутит цикл и отдаёт генератор. Утилита не думает про страницы.
- **Ретраи.** Повторяем только транспортные ошибки и `429/500/502/503/504`, с экспоненциальной задержкой и ограничением попыток. `4xx` (кроме 429) **не** ретраим — это наша ошибка, а не сетевая.
- **Исключения из кодов ответа.** Не `Exception("HTTP 401")`, а `HarborAuthError`, чтобы CLI мог вернуть отдельный код возврата.

Модель pytest, которую надо держать в голове: тест — это обычная функция `test_*`, фикстура — это «заготовка», которую pytest подставляет по имени, а `assert` переписывается так, чтобы при падении показать значения. Ключевой приём: **мокать на границе транспорта**. `httpx.MockTransport` подменяет сеть целиком, при этом весь ваш код — ретраи, пагинация, разбор JSON — выполняется по-настоящему.

Модель упаковки: `pyproject.toml` по PEP 621 описывает метаданные, PEP 517/518 — как собирать. Собираются два артефакта: **wheel** (`.whl` — готовый к установке архив пакета) и **sdist** (`.tar.gz` — исходники). `[project.scripts]` создаёт консольную команду: при установке pip генерирует маленький исполняемый скрипт, который импортирует указанную функцию. Версию держим по semver (урок 15): сломали совместимость CLI — мажор.

## 3. Аналогия из жизни

Представьте ресторан. **CLI** — это официант: он единственный говорит с гостем на его языке, принимает заказ в удобной форме (`vkt project list --json`), приносит блюдо красиво оформленным и уходит, сообщив, всё ли в порядке (код возврата). **SDK** — кухня: она знает, как реально готовить, умеет переспросить поставщика, если тот не ответил с первого раза (ретраи), и умеет подавать блюдо порциями, пока гость не наестся (пагинация). **HTTP API Harbor** — поставщик продуктов: единственный, кто реально знает, что есть на складе. **Тесты** — дегустация на кухне с муляжами продуктов: повара не надо ждать, пока приедет настоящая машина, но логика приготовления проверяется целиком. **Упаковка** — фасовка и доставка: пока блюдо не упаковано в wheel и не поставлено командой `pipx install`, его никто, кроме вас, не приготовит.

Аналогия ломается в одном месте: повара нельзя заменить муляжом и проверить, а HTTP-сервер можно — и именно поэтому `httpx.MockTransport` работает, а «тестовый ресторан» нет.

## 4. Практика и команды

**1. Окружение и установка в режиме разработки**

```bash
cd ~/work/vektor-cli
python3 -m venv .venv && source .venv/bin/activate
python -V                      # Python 3.12.x на Ubuntu 24.04
pip install -e ".[dev]"
```

`-e` (editable) ставит пакет «ссылкой»: правки в `src/` сразу видны без переустановки. `.[dev]` — ставит и `optional-dependencies.dev` (pytest, ruff, mypy). Смотреть в выводе: строка `Successfully installed ... vektor-cli-0.1.0`.

**2. `pyproject.toml` — метаданные, entry point, настройки pytest**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "vektor-cli"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27,<1.0"]     # [ПРОВЕРИТЬ] актуальную верхнюю границу

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov>=5", "ruff>=0.5", "mypy>=1.10"]

[project.scripts]
vkt = "vektor.cli:main"                 # имя команды = модуль:функция

[tool.hatch.build.targets.wheel]
packages = ["src/vektor"]

[tool.pytest.ini_options]
pythonpath = ["src"]                    # чтобы pytest видел пакет без установки
testpaths = ["tests"]
addopts = "-q --strict-markers"
markers = ["slow: долгие тесты"]
```

**3. Скелет SDK: ошибки, модели, клиент с ретраями**

```python
# src/vektor/sdk/client.py (сокращённо)
import time
from dataclasses import dataclass
from typing import Any, Iterator
import httpx

RETRY_STATUS = {429, 500, 502, 503, 504}

class VektorError(Exception): ...

class HarborError(VektorError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code

class HarborAuthError(HarborError): ...

@dataclass(frozen=True)
class Project:
    id: int
    name: str
    repo_count: int

class HarborClient:
    def __init__(self, endpoint: str, username: str | None = None,
                 password: str | None = None, *, verify: bool = True,
                 timeout: float = 10.0, max_attempts: int = 3,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._max_attempts = max_attempts
        self._client = httpx.Client(
            base_url=endpoint.rstrip("/"), verify=verify, timeout=timeout,
            transport=transport,                       # ← точка подмены в тестах
            auth=(username, password) if username else None,
            headers={"Accept": "application/json"},
        )

    def __enter__(self): return self
    def __exit__(self, *exc): self._client.close()

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._client.request(method, path, **kw)
            except httpx.TransportError as exc:
                last = exc
            else:
                if resp.status_code not in RETRY_STATUS:
                    self._raise_for_status(resp)
                    return resp
                last = HarborError(resp.status_code, resp.text[:200])
            if attempt < self._max_attempts:
                time.sleep(0.5 * 2 ** (attempt - 1))
        raise VektorError(f"{path}: не удалось за {self._max_attempts} попыток ({last})")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        msg = resp.text[:300]
        if resp.status_code in (401, 403):
            raise HarborAuthError(resp.status_code, msg)
        raise HarborError(resp.status_code, msg)

    def list_projects(self, page_size: int = 50) -> Iterator[Project]:
        page = 1
        while True:
            batch = self._request("GET", "/api/v2.0/projects",
                                  params={"page": page, "page_size": page_size}).json()
            if not batch:
                return
            for raw in batch:
                yield Project(raw["project_id"], raw["name"], raw.get("repo_count", 0))
            if len(batch) < page_size:
                return
            page += 1
```

Ключевые решения: `transport` в конструкторе (иначе тесты невозможны без сети), таймаут по умолчанию (иначе job висит вечно), `time.sleep` вынесен так, чтобы тест мог его подменить.

**4. CLI на argparse с подкомандами**

```python
# src/vektor/cli.py (сокращённо)
import argparse, dataclasses, json, os, sys
from .sdk.client import HarborAuthError, HarborClient, VektorError

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vkt", description="Утилита стенда «Вектор»")
    p.add_argument("--endpoint", default=os.environ.get("VKT_ENDPOINT", "http://10.10.10.55"))
    p.add_argument("--username", default=os.environ.get("VKT_USER"))
    p.add_argument("--password", default=os.environ.get("VKT_PASSWORD"))
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--version", action="version", version="vkt 0.1.0")
    sub = p.add_subparsers(dest="group", required=True)
    sub.add_parser("health")
    project = sub.add_parser("project")
    psub = project.add_subparsers(dest="action", required=True)
    lst = psub.add_parser("list")
    lst.add_argument("--min-repos", type=int, default=0)
    return p

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with HarborClient(args.endpoint, args.username, args.password,
                          verify=not args.insecure) as c:
            if args.group == "health":
                print(json.dumps(c._request("GET", "/api/v2.0/health").json())
                      if args.as_json else "Harbor: ok")
                return 0
            items = [p for p in c.list_projects() if p.repo_count >= args.min_repos]
            if args.as_json:
                print(json.dumps([dataclasses.asdict(p) for p in items],
                                 ensure_ascii=False, indent=2))
            else:
                for p in items:
                    print(f"{p.name:<20} repos={p.repo_count}")
            return 0
    except HarborAuthError as exc:
        print(f"ошибка аутентификации: {exc}", file=sys.stderr); return 3
    except VektorError as exc:
        print(f"ошибка: {exc}", file=sys.stderr); return 1
```

**5. Сначала — сырой API, потом код.** Всегда смотрим, что реально отдаёт сервер:

```bash
curl -s -u admin:'Lab#2026-vektor' \
  'http://10.10.10.55/api/v2.0/projects?page=1&page_size=2' -D - | head -30
```

```text
HTTP/1.1 200 OK
Content-Type: application/json
[{"project_id":1,"name":"library","repo_count":3,...}]
```
Смотреть: код ответа, заголовки (`X-Total-Count` — [ПРОВЕРИТЬ], у Harbor пагинация иногда отдаёт его), структуру JSON. По этим полям и пишем `Project`.

**6. Запуск утилиты и `jq`**

```bash
vkt --endpoint http://10.10.10.55 --username robot\$vektor --password "$HARBOR_PW" project list
vkt --json project list | jq -r '.[] | select(.repo_count > 5) | .name'
vkt health; echo "exit=$?"
```

```text
library              repos=3
vektor-app           repos=7
```
Смотреть: данные идут в **stdout** (чтобы работал `jq`), ошибки — в **stderr**, код возврата отдельный на каждый класс проблем (0/1/3).

**7. Тесты SDK: мок на транспорте**

```python
# tests/test_client.py
import httpx, pytest
from vektor.sdk.client import HarborAuthError, HarborClient

def make_client(handler, **kw):
    return HarborClient("http://harbor.test", "u", "p",
                        transport=httpx.MockTransport(handler), **kw)

def test_pagination():
    pages = {1: [{"project_id": i, "name": f"p{i}", "repo_count": 1} for i in (1, 2)],
             2: [{"project_id": 3, "name": "p3", "repo_count": 0}]}
    client = make_client(lambda r: httpx.Response(200, json=pages[int(r.url.params["page"])]))
    assert [p.name for p in client.list_projects(page_size=2)] == ["p1", "p2", "p3"]

def test_auth_error():
    client = make_client(lambda r: httpx.Response(401, json={"message": "unauthorized"}))
    with pytest.raises(HarborAuthError):
        client.list_projects()

def test_retry_on_503(monkeypatch):
    monkeypatch.setattr("vektor.sdk.client.time.sleep", lambda _: None)
    calls = {"n": 0}
    def handler(r):
        calls["n"] += 1
        return httpx.Response(503, text="down") if calls["n"] < 3 else httpx.Response(200, json=[])
    list(make_client(handler, max_attempts=3).list_projects())
    assert calls["n"] == 3
```

**8. Запуск тестов**

```bash
pytest                                    # всё
pytest -x -vv tests/test_client.py         # остановиться на первом падении
pytest -k "retry or auth" --lf             # только упавшие в прошлый раз
pytest -m "not slow"                       # без интеграционных
```

```text
.....                                                              [100%]
5 passed in 0.42s
```
Смотреть: количество тестов, время, и что ни один тест не пошёл в сеть (если пошёл — упадёт или замедлится).

**9. Покрытие**

```bash
pytest --cov=vektor --cov-report=term-missing --cov-fail-under=80
```

```text
Name                      Stmts   Miss  Cover   Missing
-------------------------------------------------------
src/vektor/cli.py            48      4    92%   61-64
src/vektor/sdk/client.py     76     10    87%   88-96
TOTAL                       124     14    89%
```
Смотреть колонку `Missing`: там обычно ветки обработки ошибок — их и надо дописать.

**10. Линтеры**

```bash
ruff check src tests --fix && ruff format src tests
mypy src
```
`ruff` — линтер и форматтер на Rust, `mypy` — статическая проверка типов. В CI гоняем без `--fix`.

**11. Сборка wheel и sdist**

```bash
python -m build            # или: uv build
ls -1 dist/
unzip -l dist/vektor_cli-0.1.0-py3-none-any.whl | head
```

```text
dist/vektor_cli-0.1.0-py3-none-any.whl
dist/vektor_cli-0.1.0.tar.gz
```
Смотреть: что `tests/` и `.venv` НЕ попали в wheel и что внутри есть `entry_points.txt`.

**12. Установка как обычной программы**

```bash
pipx install ./dist/vektor_cli-0.1.0-py3-none-any.whl
which vkt && vkt --version
pipx list
```
`pipx` ставит CLI-утилиты в отдельные venv и линкует бинарь в `~/.local/bin`. Смотреть: `vkt --version` должен отработать без активированного venv.

## 5. Лабораторная работа

### Предусловия

- **UBU01** (10.10.10.30): Ubuntu Server 24.04, SSH, `python3 --version` → 3.12, установлены `git`, `pipx` (`sudo apt install -y pipx` [ПРОВЕРИТЬ имя пакета в 24.04]), `curl`, `jq`.
- **HARBOR01** (10.10.10.55): Harbor CE, доступен по `http://10.10.10.55`. Логин `admin`, пароль — тот, что задавали при установке (на стенде — единый учебный). Если Harbor на стенде ещё не поднят, пройдите главу про контейнерные реестры; временной заменой может быть API GitLab на GIT01 (`http://10.10.10.50/api/v4/version` с `PRIVATE-TOKEN`).
- **GIT01** (10.10.10.50): GitLab CE + зарегистрированный runner с тегом `docker` (урок 17). Если runner'а нет — шаги 10–11 пропустите, CI разберёте отдельно.
- В `~/work` пусто, `git config user.name/user.email` заданы.

### Снапшот

На хосте, при выключенных или работающих ВМ (VMware Workstation: VM → Snapshot → Take Snapshot):

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-021
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\GIT01\GIT01.vmx" before-devops-021
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\HARBOR01\HARBOR01.vmx" before-devops-021
```

Пути подставьте свои. Проверить: `vmrun -T ws listSnapshots "D:\VMs\UBU01\UBU01.vmx"`.

### Шаги

1. **UBU01.** `mkdir -p ~/work/vektor-cli && cd ~/work/vektor-cli && git init`. Создайте структуру: `src/vektor/__init__.py`, `src/vektor/__main__.py`, `src/vektor/cli.py`, `src/vektor/sdk/__init__.py`, `src/vektor/sdk/client.py`, `tests/`.
   *Ожидаемо:* `tree -L 3` показывает каркас.
2. **UBU01.** Скопируйте `pyproject.toml` из раздела 4 (п.2), код клиента (п.3) и CLI (п.4). В `__main__.py`:
   ```python
   import sys
   from .cli import main
   sys.exit(main())
   ```
   *Ожидаемо:* файлы на месте, `git status` показывает новые.
3. **UBU01.** `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`.
   *Ожидаемо:* `Successfully installed ... vektor-cli-0.1.0`. Ошибка «Unable to determine which files to ship» — не хватает `[tool.hatch.build.targets.wheel]`.
4. **UBU01.** Проверьте сырой API: `curl -s -u admin:'Lab#2026-vektor' http://10.10.10.55/api/v2.0/projects -D - | head -20`.
   *Ожидаемо:* `200 OK` и JSON-массив проектов. 401 — неверный пароль; `Connection refused` — Harbor не поднят.
5. **UBU01.** `export VKT_USER=admin VKT_PASSWORD='Lab#2026-vektor'`, затем `vkt --endpoint http://10.10.10.55 health` и `vkt project list`.
   *Ожидаемо:* `Harbor: ok` и список проектов строками.
6. **UBU01.** `vkt --json project list | jq -r '.[].name'` и отдельно `vkt health; echo "exit=$?"`.
   *Ожидаемо:* чистый JSON-массив от `jq`, `exit=0`. Если в stdout попали логи — они смешаются с JSON, это баг.
7. **UBU01.** Намеренно сломайте пароль: `VKT_PASSWORD=wrong vkt health; echo "exit=$?"`.
   *Ожидаемо:* сообщение в stderr и `exit=3`, а не трейсбек.
8. **UBU01.** Создайте `tests/test_client.py` (раздел 4, п.7) и `tests/test_cli.py` — тест CLI через подмену `cli.HarborClient` заглушкой (`monkeypatch.setattr(cli, "HarborClient", FakeClient)`), проверяющий, что `--json` печатает JSON и что код возврата при `HarborAuthError` равен 3. Запустите `pytest -q`.
   *Ожидаемо:* все тесты зелёные, время < 1 с.
9. **UBU01.** `pytest --cov=vektor --cov-report=term-missing --cov-fail-under=80`, затем `ruff check src tests && mypy src`.
   *Ожидаемо:* покрытие ≥ 80 %, линтеры без ошибок.
10. **UBU01.** `python -m build && pipx install ./dist/vektor_cli-0.1.0-py3-none-any.whl`. Выйдите из venv (`deactivate`) и запустите `vkt --version`.
    *Ожидаемо:* install successful, `vkt --version` → `vkt 0.1.0` без активированного окружения.
11. **UBU01 → GIT01.** `git add -A && git commit -m "feat(vkt): cli для Harbor на httpx"`, создайте проект в GitLab, `git remote add origin http://10.10.10.50/<группа>/vektor-cli.git && git push -u origin main`, `git tag v0.1.0 && git push --tags`.
    *Ожидаемо:* код в GitLab, тег виден в Tags.
12. **GIT01.** Добавьте `.gitlab-ci.yml` (стадии `lint`/`test`/`build`, в `before_script` — `pip install -e ".[dev]"`, в `build` — `python -m build` + smoke `vkt --version`, `artifacts: paths: [dist/]`), положите пароль Harbor в Settings → CI/CD → Variables как **masked** `VKT_PASSWORD`. Запустите pipeline.
    *Ожидаемо:* три зелёные стадии, артефакт `dist/` скачивается. Если runner — shell-executor, уберите `image:` из job'ов.
13. **UBU01.** Допишите в `stand-log.md` изменения из блока ниже.

### Проверка

- [ ] `vkt --version` работает без активированного venv (установлен через pipx).
- [ ] `vkt project list` отдаёт проекты, `vkt --json project list | jq` не падает.
- [ ] `vkt health; echo $?` → правильный код (0 — успех, 3 — аутентификация).
- [ ] `pytest -q` зелёный и не обращается к 10.10.10.55 (проверьте: остановите Harbor — тесты всё равно проходят).
- [ ] `pytest --cov-fail-under=80` не падает.
- [ ] `ruff check` и `mypy src` без ошибок.
- [ ] `unzip -l dist/vektor_cli-0.1.0-py3-none-any.whl` не содержит `tests/` и `.venv/`.
- [ ] Pipeline в GitLab зелёный, артефакт `dist/` скачивается.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** sidorov (разработчик)
> **ВМ:** GIT01 (runner) / UBU01
> **Что написал:** «Pipeline красный на стадии build: `ModuleNotFoundError: No module named 'vektor.command_line'`. Локально `vkt` работает, тесты проходят. Перезапускал дважды.»
> **Срочность:** средняя (деплой не заблокирован, но ветка main красная)

Воспроизводим: в `pyproject.toml` поменяйте `vkt = "vektor.cli:main"` на `vkt = "vektor.command_line:main"`, закоммитьте, запустите pipeline — стадия `build` упадёт на smoke-шаге `vkt --version`.

Диагностика по порядку:

1. Читаем лог job'а сверху вниз: трейсбек возникает **на старте консольного скрипта**, до нашего кода — значит дело в точке входа, а не в логике.
2. Локально проверяем гипотезу: `python -c "from vektor.command_line import main"` → `ModuleNotFoundError`. Значит модуль называется `vektor.cli`.
3. Смотрим, что реально записано в собранный артефакт: `unzip -p dist/vektor_cli-0.1.0-py3-none-any.whl 'vektor_cli-0.1.0.dist-info/entry_points.txt'` → строка `vkt = vektor.command_line:main`. Вот источник.
4. Проверяем, что остальные тесты проходили потому, что импортируют модуль напрямую, а не через entry point — то есть тесты дыру не ловили.
5. Чиним: правим `[project.scripts]`, поднимаем версию до `0.1.1` (важно: pipx кэширует по версии), собираем заново.
6. Переустанавливаем: `pipx install --force ./dist/vektor_cli-0.1.1-py3-none-any.whl`, проверяем `vkt --version`.
7. Добавляем в `.gitlab-ci.yml` в стадию `build` шаг `pip install dist/*.whl && vkt --version` — теперь такая ошибка не пройдёт. Коммит, push, pipeline зелёный.

Ответ пользователю: «Причина в опечатке в `[project.scripts]`: утилита после установки искала модуль `vektor.command_line` вместо `vektor.cli`. Тесты это не ловили, потому что не проверяли консольную команду. Исправлено в v0.1.1, в CI добавлен smoke-тест `vkt --version` после сборки — теперь пайплайн краснеет сразу». Заявку закрываем со ссылкой на merge request.

### Что изменилось на стенде

- **UBU01:** создан `~/work/vektor-cli` (пакет `vektor`, `src`-layout), venv `.venv`, установлен `pipx` и утилита `vkt` (wheel версии из лабы), в `~/.local/bin` появился симлинк `vkt`.
- **GIT01:** новый проект `<группа>/vektor-cli`, ветка `main`, тег `v0.1.0`, настроен CI/CD: стадии `lint`/`test`/`build`, masked-переменная `VKT_PASSWORD`.
- **HARBOR01:** данных не меняли, использовались `admin` и API `/api/v2.0/projects`, `/api/v2.0/health`.
- **Сеть:** исходящие HTTP-запросы UBU01 → 10.10.10.55 (порт по факту установки: 80 или 443).
- **Журнал:** `stand-log.md` содержит дату, снапшот `before-devops-021` и перечень выше.

### Откат

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-021
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\GIT01\GIT01.vmx" before-devops-021
```

Точечно, без отката всей ВМ: `pipx uninstall vektor-cli`, `deactivate` + `rm -rf ~/work/vektor-cli/.venv`, в GitLab — удалить проект (Settings → General → Advanced → Delete project). Помните: откат снапшота GIT01 уносит и проект, и настройки CI-переменных — сначала решите, нужен ли он вам как портфолио.

## 6. Ошибки новичков и безопасность

1. **Логи и данные в одном потоке.** `print("debug")` в stdout → `vkt --json | jq` падает с «Invalid numeric literal». → Данные всегда в stdout, диагностика в stderr, для отладки — `-v`/`--debug`.
2. **Возврат 0 при ошибке.** `except Exception: pass` → CI зелёный, деплой сломан. → Отдельные коды возврата (0/1/3), `raise` вместо проглатывания, тест на каждый код.
3. **Тесты ходят в реальный Harbor.** Флаки в CI, падение при недоступном стенде, «у меня работает». → Мок на транспорте (`httpx.MockTransport`); интеграционные тесты — отдельно, с маркером `slow` и запуском по расписанию.
4. **Ретраи на всё подряд.** Повтор на 401/404 → шторм запросов и маскировка ошибки конфигурации. → Ретраить только 429/5xx/транспорт, с backoff, jitter и лимитом попыток.
5. **Секреты в коде, в git и в командной строке.** `--password` виден в `ps` и в истории bash, `.env` улетает в репозиторий. → Только env и masked/protected-переменные CI, файл с правами `600`, в проде — robot-аккаунт с минимальными правами вместо `admin`.
6. **`pip install` без фиксации версий.** «Вчера работало, сегодня нет». → Lock-файл (`uv.lock`/`poetry.lock`), диапазоны вида `>=0.27,<1.0`, кэш pip в CI.
7. **Публикация лишнего.** `__pycache__`, `.venv`, `tests` внутри wheel раздувают артефакт. → `src`-layout + `[tool.hatch.build.targets.wheel] packages`, `.gitignore`.
8. **Запрос без таймаута.** Job висит до общего таймаута раннера. → `timeout` по умолчанию в клиенте, разумные значения (5–30 с), тест на таймаут.

**Безопасность.** В лабе Harbor открыт по HTTP без TLS и мы логинимся `admin` — так делать только на изолированном стенде. В боевой среде: только HTTPS с `verify=True`, отдельный robot-аккаунт с правами «только чтение» для утилиты, никаких паролей в аргументах командной строки и в логах (`httpx` на уровне DEBUG печатает заголовки — не включайте его в CI без фильтра), аудит действий утилиты, ротация токена по расписанию.

## 7. Вопросы на собеседовании

**1. «Чем хорошая CLI-утилита отличается от скрипта, и что для вас значит "готова к CI"?»**
Ответ: скрипт решает задачу один раз, утилита — многократно и предсказуемо. Готова к CI значит: неинтерактивна, параметры через флаги и env, данные в stdout, ошибки в stderr, осмысленные коды возврата (0 — успех, отдельные коды — auth/сеть/не найдено), таймауты и ретраи с лимитом, `--json` для машинной обработки, `--dry-run` для опасных операций, версия в `--version` и в артефакте.
*Интервьюер хочет услышать:* что вы думали про потребителя-автоматику, а не только про «работает у меня».
*Красный флаг:* «ну вы же просто запускаете и смотрите глазами».

**2. «Как бы вы спроектировали SDK-обёртку над REST API?»**
Ответ: тонкий клиент + конфиг. Конструктор принимает `endpoint`, креды, `timeout`, `verify`, `max_attempts`, транспорт (для тестов). Клиент не печатает и не логирует секреты. Ошибки маппятся из HTTP-кодов в иерархию исключений. Пагинация внутри SDK отдаётся генератором. Ретраи — только для идемпотентных запросов и только для 429/5xx, с экспоненциальной задержкой. Всё, что возвращается наружу, — типизированные модели (dataclass), а не сырые `dict`, чтобы CLI не знал про поля API.
*Интервьюер хочет услышать:* разделение слоёв, тестируемость через внедрение транспорта, идемпотентность и осознанные ретраи.
*Красный флаг:* «оберну всё в функцию с `requests.get` и `except: pass`».

**3. «Как тестировать код, который ходит в HTTP API?»**
Ответ: мокать на границе транспорта, а не на уровне наших методов. `httpx.MockTransport` подменяет сеть, весь наш код (ретраи, пагинация, разбор) выполняется по-настоящему. Тесты на успех, 4xx, 5xx с ретраем, таймаут, битый JSON, пагинацию через две страницы. Отдельно — контрактные тесты против живого стенда с маркером `slow`, чтобы ловить изменения API. Секрет: проверять не только happy path, но и код возврата CLI.
*Интервьюер хочет услышать:* понимание, что мок должен быть как можно ниже по стеку, и что тесты не должны зависеть от сети.
*Красный флаг:* «поднимаю Harbor в каждом тесте» или «мокаю свой же клиент целиком» — тогда тестируется мок.

**4. «Что такое wheel и sdist, зачем `pyproject.toml` и что делает `[project.scripts]`?»**
Ответ: sdist — архив исходников, из него собирают wheel; wheel — готовый к установке пакет, pip ставит его без сборки. `pyproject.toml` по PEP 621 хранит метаданные и зависимости, по PEP 517/518 — описывает backend сборки (у нас hatchling). `[project.scripts]` создаёт консольный скрипт: pip генерирует обёртку, импортирующую указанную функцию; отсюда и ошибки вида «No module named» после установки, хотя локально всё работало.
*Интервьюер хочет услышать:* понимание пути «исходники → артефакт → установленная команда» и типичных мест, где он рвётся.
*Красный флаг:* «wheel — это просто zip, какая разница» без понимания entry points.

**5. «Покрытие 80 % — достаточный показатель качества?»**
Ответ: нет, это индикатор, а не цель. 100 % покрытия при отсутствии проверок граничных случаев бесполезны; тест без `assert` тоже даёт покрытие. Полезнее смотреть на покрытие веток обработки ошибок, на наличие тестов на ретраи, таймауты и неверные коды, и держать `--cov-fail-under` как защиту от регрессии, а не как KPI.
*Интервьюер хочет услышать:* трезвость и понимание, что тестировать надо риски, а не строчки.
*Красный флаг:* «гонюсь за 100 %» или «покрытие вообще не нужно».

## 8. Источники (официальная документация)

- **docs.python.org** — разделы «argparse — Parser for command-line options», «venv — Creation of virtual environments», «dataclasses», «importlib.metadata». Поиск: `python argparse subparsers`, `python venv documentation`.
- **packaging.python.org** — «Python Packaging User Guide»: «Writing your pyproject.toml», «Entry points specification», «Packaging Python Projects». Поиск: `pyproject.toml project scripts entry point`, `python wheel vs sdist`.
- **peps.python.org** — PEP 517, PEP 518, PEP 621, PEP 440 (спецификаторы версий). Поиск: `PEP 621 project metadata`.
- **docs.pytest.org** — «Get Started», «Fixtures», «Monkeypatching/mocking modules and environments», «Configuration» (опция `pythonpath`). Поиск: `pytest monkeypatch`, `pytest pythonpath ini option`.
- **www.python-httpx.org** — «Clients», «MockTransport» [ПРОВЕРИТЬ точное название раздела], «Timeouts», «Authentication». Поиск: `httpx MockTransport`, `httpx client timeout`.
- **boto3.amazonaws.com** (документация boto3/botocore) — «Configuration» (`Config`, retries, timeouts), «Paginators», «Error handling». Поиск: `botocore config retries mode standard`, `boto3 paginators`.
- **goharbor.io** — «Harbor API v2.0 Swagger» (эндпоинты `/api/v2.0/projects`, `/api/v2.0/health`). Поиск: `harbor api v2.0 swagger projects`.
- **docs.astral.sh/ruff** — «The Ruff Linter», «Configuration». Поиск: `ruff configuration pyproject`.
- **mypy.readthedocs.io** — «Getting started», «Type hints cheat sheet». Поиск: `mypy getting started`.
- **pipx.pypa.io** — «Installation», «Docs» (`pipx install`, `--force`, `reinstall`). Поиск: `pipx install wheel file`.
- **docs.gitlab.com** — «CI/CD pipelines», «Masked variables», «CI/CD artifacts». Поиск: `gitlab ci masked variable`, `gitlab ci artifacts paths`.
