# Docker Compose: многоконтейнерные приложения, сети, тома, healthchecks, профили

## 1. Введение (зачем это нужно в реальной работе)

**Заявка из внутренней очереди «Вектор»:**
> Пользователь: ivanov / ВМ: WIN11-CL01
> Что написал: «Открываю http://10.10.10.30:8080 — 502 Bad Gateway. Вчера отдавало JSON, сегодня нет. Мне для проверки выгрузки надо.»
> Срочность: средняя

До этого урока ты собирал контейнеры по одному: `docker run`, `docker build`, куча флагов `-p`, `-v`, `--network`. Как только сервисов становится больше одного (приложение + база + кэш + reverse-proxy), руками это превращается в ад: забыл сеть — контейнеры не видят друг друга, забыл том — данные умерли при пересоздании, перезагрузил хост — половина не поднялась. Docker Compose решает ровно эту боль: ты описываешь **всё приложение одним YAML-файлом**, коммитишь его в Git и одной командой поднимаешь или сносишь стенд целиком.

После этой главы ты должен уметь: писать `compose.yaml` для многоконтейнерного приложения; разделять сервисы на сети (что кому видно); отличать named volume от bind mount и понимать, где лежат данные; настраивать `healthcheck` и `depends_on` с условием готовности; прятать отладочные сервисы в `profiles`; диагностировать «502» за три команды и не терять базу при `down`.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Важно понять сразу: **Compose — это не оркестратор и не демон**. Это клиент (плагин к CLI `docker`, файл `docker-compose` из v1 устарел и снят с поддержки). Он читает YAML, подставляет переменные, превращает его в набор вызовов Docker Engine API через сокет `/var/run/docker.sock` — создаёт сети, тома, контейнеры. Сам он «на хосте» не живёт и после `up` ничего не контролирует: упавший контейнер Compose не поднимет (это делает `restart:`-политика самого демона, и только по коду выхода, а не по health).

Ключевые сущности Compose Specification:

- **project** — единица группировки. Имя берётся из `name:` в файле или из имени каталога; к нему добавляются префиксы: сеть `vektor-stack_default`, том `vektor-stack_pgdata`, контейнер `vektor-stack-app-1`. На контейнеры вешаются метки `com.docker.compose.project` / `.service` — по ним Compose их и находит.
- **services** — контейнеры. **networks** — пользовательские bridge-сети (в них работает встроенный DNS Docker на `127.0.0.11`, поэтому `app` резолвится по имени сервиса). **volumes** — именованные тома, живущие в `/var/lib/docker/volumes`. **secrets/configs** — монтирование файлов. **profiles** — метки, по которым сервис включается только явно.
- Файл по умолчанию — `compose.yaml` (или `compose.yml`, старый `docker-compose.yml` тоже читается). Поле `version:` больше не нужно: оно игнорируется и выдаёт warning.
- Переменные подставляются из `.env` в каталоге проекта и из окружения: `${WEB_PORT:-8080}`. Внутри YAML `$` — спецсимвол; чтобы передать его в контейнер буквально, пишут `$$`. В файлах, которые монтируются (например, конфиг nginx), подстановки нет вообще.

Схема учебного стека, который мы соберём:

```text
        хост UBU01 10.10.10.30
        ┌──────────────────────────────────────────────┐
 браузер│  :8080 (published)                            │
 ivanov │      │                                        │
────────┼──▶ [web: nginx]  ──сеть front──┐              │
        │                               ▼              │
        │                        [app: python]          │
        │                               │              │
        │                          сеть back (internal) │
        │                               ▼              │
        │                          [db: postgres]      │
        │                               │              │
        │                        том pgdata (named)    │
        │  [adminer]  профиль debug, только 127.0.0.1  │
        └──────────────────────────────────────────────┘
```

Порты: nginx 80/443, приложение 8000, PostgreSQL 5432, Redis 6379, adminer 8080 (внутри). С точки зрения модели OSI всё это L7: HTTP-трафик идёт по TCP, а «сеть» в Compose — это bridge L2-сегмент с NAT на хосте.

## 3. Аналогия из жизни

Compose-файл — это проект общежития от застройщика. `services` — квартиры: в каждой живёт один жилец-процесс, и у каждой прописано, что ему нужно (кому дверь в коридор, какие вещи он берёт с собой). `networks` — коридоры: в коридор `front` выходят входная дверь и приложение, а в `back` (внутренний, `internal: true`) имеет право ходить только приложение и база — и оттуда нет выхода на улицу. `volumes` — кладовки в подвале: снесли квартиру и построили заново, а кладовка с вещами осталась (`docker compose down` её не трогает, `down -v` — сносит). `healthcheck` — вахтёр с обходом: он не «видит свет в окне», а стучит и спрашивает пароль, то есть проверяет, что приложение реально готово отвечать. `depends_on` с `condition: service_healthy` — бригада не начинает работу, пока не подвезли бетон. `profiles` — гости, которых зовут только на праздник: adminer и дампер базы не мешают жить постоянно.

Где аналогия ломается: если жилец заболел, общежитие не переселяет его в другую квартиру и не вызывает врача — Compose вообще не следит за здоровьем после старта. Автоматическое «переселение» и самовосстановление — это уже Kubernetes.

## 4. Практика и команды

Все команды — на UBU01. На Windows с Docker Desktop всё то же самое, только пути и `sudo` другие.

**1. Версия и установка плагина.** Проверяем, что у тебя Compose v2 (команда через пробел, не через дефис).
```bash
docker compose version
# Docker Compose version v2.29.7  [ПРОВЕРИТЬ актуальную версию]
sudo apt install docker-compose-plugin   # если плагина нет
```
Если видишь `docker-compose` с версией 1.x — это старый Python-клиент, он снят с поддержки, переходи на v2.

**2. Валидация и рендер конфига.** Главная команда перед каждым `up`.
```bash
cd ~/vektor-stack
docker compose config -q          # только проверить, молча
docker compose config             # распечатать итоговый YAML со всеми подстановками
docker compose config --services  # список сервисов
```
Смотрим: подставился ли `${POSTGRES_PASSWORD}`, нет ли `null` в переменных, какой `name` у проекта.

**3. Запуск с ожиданием готовности.**
```bash
docker compose up -d --build --wait --wait-timeout 120
# [+] Running 4/4
#  ✔ Network vektor-stack_front  Created
#  ✔ Container vektor-stack-db-1    Healthy
#  ✔ Container vektor-stack-app-1   Healthy
#  ✔ Container vektor-stack-web-1   Started
```
`-d` — в фоне, `--build` — пересобрать образы, `--wait` — вернуться только когда все сервисы с healthcheck стали healthy (или упасть с ошибкой).

**4. Состояние: `ps` и `ls`.**
```bash
docker compose ps           # сервисы текущего проекта
docker compose ps -a        # включая остановленные
docker compose ls           # все проекты на хосте: NAME / STATUS / CONFIG FILES
```
В `ps` смотри колонки `STATUS` (`Up 2 minutes (healthy)` против `Exited (1)`) и `PORTS` (`0.0.0.0:8080->80/tcp`).

**5. Логи.**
```bash
docker compose logs -f --tail=100 app
docker compose logs --since 10m web
```
`-f` — следить, `--tail` — последние N строк, `--since` — по времени. Именно логи nginx покажут `connect() failed (111: Connection refused) while connecting to upstream` — то самое «почему 502».

**6. Выполнение команд внутри.**
```bash
docker compose exec app sh
docker compose exec db psql -U app -d vektor -c '\l'
docker compose run --rm app python -c "import socket; print(socket.gethostbyname('db'))"
```
`exec` — в уже работающий контейнер, `run` — одноразовый новый (с `--rm` удалится после), удобно для миграций и разовых скриптов.

**7. Проверка здоровья напрямую через Engine API.**
```bash
docker inspect -f '{{.State.Health.Status}}' $(docker compose ps -q app)
# healthy
docker inspect -f '{{json .State.Health}}' $(docker compose ps -q db) | jq '.Log[-1]'
```
`starting | healthy | unhealthy` — и последняя запись лога healthcheck: там видно, какая именно проверка провалилась.

**8. Профили.**
```bash
docker compose --profile debug up -d       # поднять web/app/db + adminer
docker compose --profile "*" up -d         # все профили сразу
docker compose config --profiles           # какие профили объявлены
```
Без `--profile` сервис с `profiles:` не создаётся вовсе — это способ не выставлять наружу отладку и не гонять тяжёлые сервисы на ноутбуке.

**9. Остановка — аккуратно с `-v`.**
```bash
docker compose stop          # остановить, контейнеры остаются
docker compose restart app   # перезапустить один сервис
docker compose down          # удалить контейнеры и сети, ТОМА ОСТАЮТСЯ
docker compose down -v       # удалить ещё и named volumes = потерять базу
docker compose down --remove-orphans  # добить контейнеры удалённых из YAML сервисов
```

**10. Тома и сети — что реально создалось.**
```bash
docker volume ls | grep vektor
# local     vektor-stack_pgdata
docker volume inspect vektor-stack_pgdata -f '{{.Mountpoint}}'
# /var/lib/docker/volumes/vektor-stack_pgdata/_data
docker network inspect vektor-stack_back -f '{{range .Containers}}{{.Name}} {{end}}'
# vektor-stack-app-1 vektor-stack-db-1
```
Если в сети `front` оказалась база — значит, ты перепутал сети и база видна лишним сервисам.

**11. Процессы и события.**
```bash
docker compose top
docker events --filter label=com.docker.compose.project=vektor-stack
```
`top` показывает PID процессов внутри контейнеров на хосте; `events` — поток `start/die/health_status` в реальном времени, полезно, когда контейнер «мигает».

**12. Безопасное опубликование порта.** Публикуй только то, что нужно, и лучше на loopback:
```bash
docker compose port web 80        # 0.0.0.0:8080
# в compose.yaml:  "127.0.0.1:8081:8080"  — доступно только с самого хоста
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **UBU01** (10.10.10.30), Ubuntu Server 24.04, доступ по SSH, пользователь с `sudo` (тот, что создан в главах про Linux), установлен Docker Engine из урока 27 и плагин `docker-compose-plugin`.
- Свободен порт 8080 на UBU01 (`ss -lntp | grep 8080` пусто).
- Windows-часть стенда для этой лабы не нужна — работаем только с Linux.
- Образы `nginx:1.27-alpine`, `postgres:16-alpine`, `python:3.12-slim` тянутся с Docker Hub. Если из РФ это не работает — сначала настрой зеркало/прокси-кэш по уроку 32, иначе лаба встанет на шаге загрузки.

### Снапшот
В VMware Workstation на ВМ **UBU01**: меню `VM → Snapshot → Take Snapshot`, имя — `before-devops-028`. Другие ВМ не затрагиваются, снапшот на них не нужен.

### Шаги

1. **Каталог проекта и Git.** На UBU01, под своим пользователем:
   ```bash
   mkdir -p ~/vektor-stack/{app,nginx} && cd ~/vektor-stack
   git init -b main
   ```
   Ожидаемо: `Initialized empty Git repository in /home/<user>/vektor-stack/.git/`.

2. **Код приложения** — `app/app.py`. Это стандартная библиотека Python, никаких `pip install`, поэтому сборка не зависит от интернета:
   ```python
   import json, os, socket, time
   from http.server import BaseHTTPRequestHandler, HTTPServer

   DB_HOST = os.getenv("DATABASE_HOST", "db")
   DB_PORT = int(os.getenv("DATABASE_PORT", "5432"))

   def db_ok() -> bool:
       try:
           with socket.create_connection((DB_HOST, DB_PORT), timeout=2):
               return True
       except OSError:
           return False

   def wait_for_db(attempts=10, delay=2.0):
       for i in range(1, attempts + 1):
           if db_ok():
               print(f"db reachable on attempt {i}", flush=True); return
           print(f"db not reachable, attempt {i}/{attempts}", flush=True); time.sleep(delay)
       raise SystemExit(1)

   class H(BaseHTTPRequestHandler):
       def do_GET(self):
           ok = db_ok()
           body = json.dumps({"service": "vektor-app", "db": ok}).encode()
           self.send_response(200 if (ok or self.path != "/health") else 503)
           self.send_header("Content-Type", "application/json")
           self.send_header("Content-Length", str(len(body)))
           self.end_headers(); self.wfile.write(body)

   if __name__ == "__main__":
       wait_for_db()
       HTTPServer(("0.0.0.0", 8000), H).serve_forever()
   ```

3. **Dockerfile** — `app/Dockerfile`:
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY app.py .
   EXPOSE 8000
   CMD ["python", "app.py"]
   ```
   Ожидаемо: файл на месте. Безопасность (non-root, distroless) — тема урока 30, здесь мы её осознанно пропускаем.

4. **Конфиг nginx** — `nginx/default.conf`:
   ```nginx
   server {
       listen 80;
       location / {
           proxy_pass http://app:8000;
           proxy_set_header Host $host;
       }
   }
   ```
   `$host` здесь не нужно экранировать: подстановка Compose работает только в `compose.yaml`, а это отдельный файл.

5. **`.env`** (в нём же ловушка с `#` — символ комментария, поэтому кавычки обязательны):
   ```bash
   WEB_PORT=8080
   POSTGRES_PASSWORD="Lab#2026-vektor-db"
   ```

6. **`compose.yaml`** в корне проекта:
   ```yaml
   name: vektor-stack

   services:
     web:
       image: nginx:1.27-alpine
       ports: ["${WEB_PORT:-8080}:80"]
       volumes: ["./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro"]
       depends_on:
         app: { condition: service_healthy }
       networks: [front]
       restart: unless-stopped

     app:
       build: ./app
       environment:
         DATABASE_HOST: db
         DATABASE_PORT: "5432"
       depends_on:
         db: { condition: service_healthy }
       healthcheck:
         test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
         interval: 10s
         timeout: 5s
         retries: 3
         start_period: 15s
       networks: [front, back]
       restart: unless-stopped

     db:
       image: postgres:16-alpine
       environment:
         POSTGRES_USER: app
         POSTGRES_DB: vektor
         POSTGRES_PASSWORD: "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}"
       volumes: ["pgdata:/var/lib/postgresql/data"]
       healthcheck:
         test: ["CMD-SHELL", "pg_isready -U app -d vektor"]
         interval: 5s
         timeout: 3s
         retries: 10
         start_period: 20s
       networks: [back]
       restart: unless-stopped

     adminer:
       image: adminer:latest
       profiles: [debug]
       ports: ["127.0.0.1:8081:8080"]
       depends_on:
         db: { condition: service_healthy }
       networks: [back, front]

   networks:
     front:
     back:
       internal: true

   volumes:
     pgdata:
   ```
   Пояснения: `internal: true` у `back` запрещает выход в интернет и не даёт сети шлюза — база изолирована; `:?` в переменной заставит Compose упасть, если пароль не задан, а не подставить пустую строку.

7. **Валидация и подъём.**
   ```bash
   docker compose config -q && echo OK
   docker compose up -d --build --wait
   docker compose ps
   ```
   Ожидаемо: `OK`, затем три сервиса в статусе `Up ... (healthy)`, `adminer` отсутствует (профиль не включён).

8. **Проверка сквозного трафика** — с UBU01 и с WIN11-CL01 (`http://10.10.10.30:8080`):
   ```bash
   curl -s http://127.0.0.1:8080 ; echo
   # {"service": "vektor-app", "db": true}
   ```
   Ожидаемо: JSON с `"db": true`. Если тут 502 — сразу смотри `docker compose ps` и `docker compose logs web`.

9. **Профиль debug.**
   ```bash
   docker compose --profile debug up -d
   docker compose ps
   docker compose port adminer 8080     # 127.0.0.1:8081
   ```
   Ожидаемо: появился `vektor-stack-adminer-1`. С Windows он недоступен — это задумано; ходи через туннель `ssh -L 8081:127.0.0.1:8081 <user>@10.10.10.30`.

10. **Проверка изоляции сетей.**
    ```bash
    docker compose exec web sh -c 'getent hosts db || echo "db не виден из web"'
    docker compose exec db sh -c 'ping -c1 -W1 8.8.8.8 || echo "наружу выхода нет"'
    ```
    Ожидаемо: `db не виден из web` (web только во `front`) и `наружу выхода нет` (сеть `back` internal). В ping из alpine-образа postgres утилиты может не быть — тогда проверь через `wget -T2 -q -O- http://example.com` или `nslookup` `[ПРОВЕРИТЬ наличие busybox-утилит в образе]`.

11. **Healthcheck в деле.**
    ```bash
    docker inspect -f '{{.State.Health.Status}}' $(docker compose ps -q app)   # healthy
    docker compose stop db
    sleep 30 && docker inspect -f '{{.State.Health.Status}}' $(docker compose ps -q app)
    # unhealthy
    docker compose start db
    ```
    Ожидаемо: при остановленной базе `/health` отдаёт 503, статус становится `unhealthy`. **Контейнер при этом никто не перезапустит** — политика `restart` смотрит на код выхода, а не на здоровье.

12. **Коммит и журнал.** Не коммить `.env`:
    ```bash
    printf '.env\n' > .gitignore
    git add . && git commit -m "feat: vektor-stack compose (web/app/db + debug profile)"
    ```
    И допиши в `stand-log.md` изменения из раздела ниже.

### Проверка
- [ ] `docker compose config -q` завершается без ошибок, в `docker compose config` нет пустых переменных.
- [ ] `docker compose ps` показывает `web`, `app`, `db` в статусе `Up (healthy)`.
- [ ] `curl -s http://10.10.10.30:8080` с WIN11-CL01 отдаёт JSON `{"service": "vektor-app", "db": true}`.
- [ ] `docker volume ls` содержит `vektor-stack_pgdata`.
- [ ] `docker network inspect vektor-stack_back` показывает только `app` и `db`.
- [ ] Без `--profile` контейнера `adminer` не существует; с `--profile debug` — существует и слушает `127.0.0.1:8081`.
- [ ] `docker compose down` + `up -d` — данные в базе на месте (том не удалён).
- [ ] В Git закоммичены `compose.yaml`, `app/`, `nginx/`, а `.env` — нет.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Открываю http://10.10.10.30:8080 — 502 Bad Gateway. Вчера работало, я ничего не трогал.»
> **Срочность:** средняя

**Воспроизводим.** Сервис `app` упал и не вернулся — типичная ситуация после перезагрузки хоста или рестарта базы: `depends_on` работает только в момент `docker compose up`, демон при старте контейнеров порядок не соблюдает.
```bash
docker compose stop db
docker compose restart app       # app подождёт базу 20 с и выйдет с кодом 1
docker compose start db          # база вернулась, но app так и лежит
curl -si http://127.0.0.1:8080 | head -1
# HTTP/1.1 502 Bad Gateway
```

**Диагностика, строго по порядку:**
1. `curl -si http://127.0.0.1:8080 | head -1` — 502 отдаёт nginx, значит сам nginx жив.
2. `docker compose ps -a` — видим `vektor-stack-app-1  Exited (1)`. Круг сузился до приложения.
3. `docker compose logs --tail=50 app` — в логе `db not reachable, attempt 10/10` → приложение не дождалось базу и завершилось само. Это не баг кода, это отсутствие ретраев «навсегда».
4. `docker compose logs --tail=20 web` — `connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://172.x.x.x:8000"`. Подтверждает: 502 — это «некуда подключиться», а не «медленно».
5. `docker inspect -f '{{.State.Health.Status}}' $(docker compose ps -q db)` — `healthy`, база в порядке, дело только в `app`.

**Решение.**
```bash
docker compose up -d --wait      # Compose пересоздаст вышедший контейнер
docker compose ps
docker compose logs --tail=5 app # "db reachable on attempt 1"
```
Чтобы это не повторялось: убедиться, что у `app` стоит `restart: unless-stopped`, а в коде ретраи продолжаются дольше, чем стартует база (в проде — бесконечно, плюс readiness-проба). Для теста можно временно увеличить `attempts` до 60.

**Ответ пользователю:** «Сервис приложения не поднялся после перезапуска базы. Восстановил, проверьте, пожалуйста, http://10.10.10.30:8080 — должно отдавать JSON. Чтобы не повторялось, добавил автоперезапуск и увеличил ожидание базы.» **Закрытие:** статус Resolved, в комментарии — ссылка на коммит с изменением `compose.yaml`.

### Что изменилось на стенде
- **UBU01 (10.10.10.30):** установлен плагин `docker-compose-plugin` (если не был); каталог `~/vektor-stack` — Git-репозиторий (ветка `main`); запущен проект Compose **`vektor-stack`** с сервисами `web`/`app`/`db`; порт **8080** опубликован на хост.
- **Сети Docker на UBU01:** `vektor-stack_front` (bridge), `vektor-stack_back` (internal).
- **Тома:** `vektor-stack_pgdata` — данные PostgreSQL (`POSTGRES_USER=app`, `POSTGRES_DB=vektor`).
- **Профиль `debug`:** сервис `adminer`, публикация `127.0.0.1:8081` (по умолчанию выключен).
- **Учётные данные стенда:** пароль БД `Lab#2026-vektor-db` только в локальном `.env` (в Git не попадает).
- Следующие главы (29–32, registry, k8s) могут опираться на этот образ `vektor-stack_app` как на готовый артефакт для публикации в Harbor.

### Откат
1. Погасить проект: `cd ~/vektor-stack && docker compose --profile "*" down -v` (том тоже удалится — данные базы не нужны, стенд учебный).
2. В VMware Workstation на UBU01: `VM → Snapshot → Snapshot Manager` → выбрать `before-devops-028` → **Revert**. Запустить ВМ.
3. Проверить: `docker compose ls` пусто, `docker volume ls` без `vektor-stack_*`. Каталог `~/vektor-stack` исчезнет вместе со снапшотом — если код нужен, сначала запушь его в GitLab на GIT01 (когда он у тебя появится).

## 6. Ошибки новичков и безопасность

1. **`docker compose down -v` на стенде с данными** → сносит named volumes, база пустая и «сама не восстановится». → Перед `down` всегда спрашивай себя, где лежит состояние; для боевого — `down` без `-v` и отдельная процедура бэкапа (`pg_dump` в том/на бэкап-сервер).
2. **`depends_on` без healthcheck** → контейнеры стартуют по порядку, но приложение падает на «connection refused», потому что база ещё инициализируется. → Всегда `condition: service_healthy` + healthcheck на зависимом сервисе (и ретраи в самом приложении).
3. **Вера в то, что Compose «сам поднимет»** → после падения процесса или перезагрузки хоста стек лежит. → `restart: unless-stopped` + понимание, что health-based рестартов в Engine нет, а порядок `depends_on` при рестарте демона не соблюдается.
4. **`ports: "5432:5432"` у базы** → PostgreSQL торчит на `0.0.0.0` в офисную сеть 10.10.10.0/24. → Внутренним сервисам `ports` не нужны вообще (хватает `expose`/общей сети); если очень нужно — `127.0.0.1:5432:5432` и SSH-туннель.
5. **Пароль прямо в `compose.yaml` и коммит** → секрет в истории Git навсегда. → `.env` + `.gitignore`; для чего посерьёзнее — `secrets:` (в Compose это просто файлы, не хранилище секретов) или внешний Vault.
6. **Забыл том под данные** → данные пишутся в writable layer контейнера и исчезают при `up --force-recreate`. → Для состояния — только named volume или bind mount, проверка `docker volume ls`.
7. **Сервис с профилем включён «на всякий случай»** → adminer/отладка с доступом к базе висит постоянно. → Профили `debug`/`ops` включаются явно и только на время.
8. **`image: adminer:latest`** → непредсказуемое обновление и сломанная совместимость. → Пиновать теги (`adminer:4.8.1` `[ПРОВЕРИТЬ актуальный тег]`), обновлять осознанно.

**Безопасность.** Compose-файл — это описание инфраструктуры, и оно попадает в Git целиком: не пиши туда пароли, токены, приватные ключи. Bind-mount `./nginx/default.conf:ro` — всегда `:ro` там, где контейнер не должен писать. Не монтируй `docker.sock` внутрь контейнеров (это root на хосте). Не запускай `docker compose` от root в `/root` — получишь файлы, принадлежащие root, и потеряешь контроль. И помни главное ограничение: Compose — **один хост**. Ни HA, ни самовосстановления, ни раскатки по узлам он не даёт; в боевой среде за это отвечает Kubernetes, а Compose остаётся инструментом локальной разработки, тестовых стендов и небольших внутренних сервисов.

## 7. Вопросы на собеседовании (5)

**1. «Чем `depends_on` с `condition: service_healthy` отличается от обычного `depends_on` и почему healthcheck должен проверять готовность, а не факт запуска?»**
Ответ: обычный `depends_on` гарантирует только порядок *запуска* контейнеров, и всё; приложение стартует, когда база ещё принимает соединения через 10–20 секунд. Длинная форма с `condition: service_healthy` заставляет Compose ждать статус `healthy` зависимости (а `service_completed_successfully` — успешного завершения одноразового job'а, например миграций). Healthcheck же должен дергать реальный endpoint или внутреннюю проверку (`pg_isready`, `/health`), а не `ps` процесса: процесс жив, но соединения не принимает — типовой случай при инициализации или деградации.
*Что хочет услышать интервьюер:* понимание, что «запущен» ≠ «готов», и что оркестрация старта — не самовосстановление.
*Красный флаг:* «depends_on гарантирует, что база работает» без оговорок и без упоминания рестартов.

**2. «Как контейнеры одного проекта находят друг друга по имени? Что под капотом?»**
Ответ: Compose создаёт пользовательскую bridge-сеть (по умолчанию `<project>_default`, у нас — именованные `front`/`back`), подключает к ней контейнеры и задаёт им сетевые алиасы по имени сервиса. В контейнерах прописан DNS `127.0.0.11` — встроенный DNS-резолвер Docker, который отвечает адресами контейнеров этой сети. Поэтому `proxy_pass http://app:8000` работает. В дефолтной сети `bridge` (если запускать через `docker run` без `--network`) такого DNS нет — только `--link` (устарел) или IP.
*Что хочет услышать интервьюер:* `127.0.0.11`, user-defined bridge, алиасы, изоляция сетей.
*Красный флаг:* «через /etc/hosts прописывается» как основной механизм.

**3. «Named volume vs bind mount: когда что и что будет с данными при `down`, `down -v` и пересоздании контейнера?»**
Ответ: bind mount — конкретный путь хоста (`./nginx/default.conf`), удобно для конфигов и разработки, но зависит от ФС и прав хоста; named volume — каталог в `/var/lib/docker/volumes/<project>_<name>`, управляется Docker, переносим между хостами через `docker volume` и быстрее на macOS/Windows. При `up --force-recreate` данные в обоих сохраняются; при `down` контейнеры удаляются, тома и bind-пути остаются; при `down -v` named volumes удаляются, bind-пути — нет, потому что они не принадлежат Docker. Данные в writable layer контейнера теряются при любом пересоздании.
*Что хочет услышать интервьюер:* что состояние всегда вне контейнера и что `-v` — это удаление данных.
*Красный флаг:* «тома удаляются при `down`» или «в контейнере хранить нормально».

**4. «Зачем нужны profiles? Приведи рабочий пример.»**
Ответ: профиль — метка на сервисе, который без явного `--profile <name>` не создаётся вообще. Применяют для отладочных инструментов (adminer, pgadmin), разовых job'ов (миграции, `pg_dump`), опциональных сервисов (мониторинг, кэш), чтобы один и тот же `compose.yaml` работал и на ноутбуке разработчика, и на стенде. Пример: `adminer` с `profiles: [debug]` запускается только `docker compose --profile debug up -d`; `--profile "*"` включает все. Плюс это безопаснее: отладочная панель с доступом к базе не висит постоянно.
*Что хочет услышать интервьюер:* что профили — про управление составом окружения одним файлом, а не про «ещё один способ запуска».
*Красный флаг:* путаница с `env_file` или утверждение, что профили что-то изолируют по сети.

**5. «У нас сервис на Compose. Когда пора в Kubernetes и что мы теряем, оставаясь на Compose?»**
Ответ: Compose — один хост: нет HA (упал хост — лежит всё), нет планировщика, нет горизонтального масштабирования и rolling update без простоя, нет самовосстановления по health (только `restart` по коду выхода), нет встроенных секретов и RBAC. Kubernetes даёт реплики, readiness/liveness-пробы, rolling-обновления, распределение по узлам. Переход оправдан, когда нужны отказоустойчивость, автомасштабирование или несколько команд на общем кластере; для локальной разработки, CI-стендов и небольших внутренних сервисов Compose дешевле. Черновик манифестов можно получить через `kompose convert` `[ПРОВЕРИТЬ актуальность инструмента в разделе kubernetes.io]`, но руками обычно чище.
*Что хочет услышать интервьюер:* трезвую оценку границ инструмента, а не «Compose — прошлый век».
*Красный флаг:* «Compose — это тот же Kubernetes, только проще» или «перейдём в k8s, там всё само заработает».

## 8. Источники (официальная документация)

- **docs.docker.com** — раздел «Docker Compose»: статьи «Compose file reference», «Compose specification», «Control startup and shutdown order in Compose», «Specify a Docker Compose file», «Compose profiles». Поисковые фразы: `docker compose file reference depends_on condition`, `compose healthcheck start_period`, `docker compose profiles`.
- **docs.docker.com** — «Docker Engine»: статьи «HEALTHCHECK instruction» и «Volumes» (разница named volume / bind mount). Поиск: `dockerfile healthcheck`, `docker volumes bind mounts difference`.
- **compose-spec.io** (и репозиторий `compose-spec/compose-spec` на GitHub) — актуальная спецификация Compose: какие поля обязательны, какие расширения Docker-only. Поиск: `compose specification services top-level elements`.
- **kubernetes.io** — раздел «Migrate to Kubernetes» / статья про `kompose`. Поиск: `kompose convert docker compose to kubernetes`.
- **nginx.org** — `ngx_http_proxy_module`, директивы `proxy_pass` и `proxy_set_header`. Поиск: `nginx proxy_pass upstream name resolution`.
- **hub.docker.com** — официальные страницы образов `postgres`, `nginx`, `python` (переменные `POSTGRES_*`, healthcheck-примеры). Поиск: `postgres docker official image environment variables`.
- Доступность Docker Hub из РФ и настройка зеркал — тема урока 32; официальной гарантии стабильного доступа нет, легальная альтернатива — собственный прокси-кэш/реестр (Harbor на HARBOR01) `[ПРОВЕРИТЬ актуальные правила доступа]`.
