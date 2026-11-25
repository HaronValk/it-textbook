# Контейнеры: Docker глубже — образы, сети, тома, docker-compose, registries

## 1. Введение (зачем это нужно в реальной работе)

Заявка в поддержку «Вектора»:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Здравствуйте! Внутренняя страница на `http://10.10.10.30:8080` не открывается, браузер пишет „сайт недоступен“. Вчера всё работало. Очень нужно до конца дня — сверяем реестр платежей к закрытию месяца.»
> **Срочность:** Средняя.

Дальше вы открываете браузер на WIN11-CL01, тыкаете `http://10.10.10.30:8080` — пусто. Заходите по SSH на UBU01, а там `docker ps` показывает не то, что ожидалось. Всё, дальше надо понимать, что такое слой образа, что такое опубликованный порт, где живут данные и почему контейнер может «умереть» от чужой ошибки.

В L1 вы уже наверняка встречали Docker «на уровне запуска»: `docker run`, `docker ps`, `docker logs`. На L2 этого мало. На L2 вы обслуживаете контейнерные сервисы: обновляете образы, разбираетесь, почему контейнер в рестарт-петле, почему данные пропали после `docker compose down`, почему из офисной сети не открывается порт, откуда тянется образ и что делать, если Docker Hub недоступен. Плюс Docker — прямой мост к DevOps: Dockerfile, Compose и работа с реестром — те же навыки, что нужны в пайплайнах (урок 98) и Kubernetes (урок 97).

После главы вы должны уметь: читать Dockerfile и понимать, как он превращается в слои; объяснять разницу между образом, контейнером и томом; создавать свои сети и публиковать порты; поднимать и пересобирать стек через Compose; поднимать локальный реестр, тегировать и пушить образы; диагностировать типовые поломки контейнера от заявки до закрытия тикета.

## 2. Теория «под капотом»

**Docker — это не виртуализация.** Виртуальная машина (в том числе ВМ в VMware Workstation) несёт внутри себя целое гостевое ядро. Контейнер — это **обычный процесс Linux**, которому ядро хоста подсунуло изолированное окружение: отдельные пространства имён (namespaces) для PID, сети, монтирований, IPC, UTS и пользователей, плюс ограничения (cgroups) на CPU/память. Ядро у всех контейнеров одно — хостовое. Отсюда: контейнер стартует за доли секунды, но «Linux в контейнере» — не отдельная ОС, и Windows-контейнеры на Linux-хосте не поедут (и наоборот).

**Кто там внутри.** `dockerd` (демон) → `containerd` (менеджер жизненного цикла) → `runc` (непосредственно создаёт процесс). Клиент `docker` общается с демоном через unix-сокет `/var/run/docker.sock`. Если этот сокет смонтировать в контейнер — процесс внутри получает права root на хосте. Это не «мелочь», это классическая дыра.

**Образ = стек слоёв + JSON-конфиг.** Каждая инструкция `RUN`, `COPY`, `ADD` создаёт новый read-only слой. Драйвер хранения по умолчанию — `overlay2`, слои физически лежат в `/var/lib/docker/overlay2`. При запуске контейнера сверху добавляется тонкий writable-слой (copy-on-write): всё, что процесс пишет в контейнерную ФС, умирает вместе с контейнером. Образы адресуются по содержимому (content-addressable), поэтому `image ID` — это sha256, а одинаковые слои от разных образов физически хранятся один раз.

```text
Образ vektor-web:1.0                     Контейнер vektor-web
┌─────────────────────────────┐          ┌──────────────────────────────┐
│ слой 4  COPY index.html     │          │ R/W слой (живёт до rm)       │ ← всё, что пишет процесс
├─────────────────────────────┤          ├──────────────────────────────┤
│ слой 3  EXPOSE 80           │   +   →  │ слой 4  (read-only)          │
├─────────────────────────────┤          ├──────────────────────────────┤
│ слой 2  ENV ...             │          │ слой 3  (read-only)          │
├─────────────────────────────┤          ├──────────────────────────────┤
│ слой 1  FROM nginx:alpine   │          │ слой 1..2 (read-only)        │
└─────────────────────────────┘          └──────────────────────────────┘
         registry хранит слои по digest, тег :1.0 — это просто указатель
```

**Сети.** По умолчанию у Docker есть bridge `docker0` с подсетью `172.17.0.0/16`. Контейнер получает veth-пару, один конец в его namespace, второй — в бридже на хосте. Наружу — через iptables NAT (masquerade). Публикация порта `-p 8080:80` — это DNAT: трафик на `10.10.10.30:8080` переписывается на IP контейнера, порт 80. **Ключевой момент:** в *default* bridge контейнеры не резолвят друг друга по имени (только устаревший `--link`), в *user-defined* bridge — резолвят через встроенный DNS `127.0.0.11`. Поэтому в Compose всегда делают свою сеть.

```text
UBU01  10.10.10.30 (VMnet8), ядро Linux 6.8
┌───────────────────────────────────────────────────────────────┐
│ dockerd ─ containerd ─ runc        сокет /var/run/docker.sock │
│                                                               │
│  docker0 172.17.0.1/16        vektor-net 172.28.0.1/24        │
│      │                             │                          │
│  [web :80]                  [web :80]   [redis :6379]         │
│       └──────── iptables DNAT ─────────────┘                  │
│              :8080 → 172.28.0.x:80                            │
└───────────────────────────────────────────────────────────────┘
     ▲
     │ http://10.10.10.30:8080
 WIN11-CL01 (DHCP из пула DC01)
```

Драйверы сетей: `bridge` (обычный кейс), `host` (без изоляции, контейнер слушает порты хоста напрямую), `none`, `overlay` (между хостами, Swarm), `macvlan`. Плюс внешние плагины — их не трогаем.

**Тома.** Три способа дать контейнеру постоянные данные:
- **named volume** — управляется Docker, живёт в `/var/lib/docker/volumes/<имя>/_data`, переживает `rm` контейнера;
- **bind mount** — вы монтируете конкретный путь хоста (`-v /srv/vektor/web:/usr/share/nginx/html`), удобно для конфигов, но это прямая дырка в ФС хоста;
- **tmpfs** — в память, умирает вместе с контейнером.

Данные БД — только в volume. Код и конфиги — bind mount или внутрь образа.

**Реестр (registry).** Полное имя образа: `[registry/]namespace/repo:tag`. Без registry — Docker Hub (`docker.io`), порт 443. При `push` клиент сначала отправляет манифест, потом недостающие слои по digest. Локальный реестр `registry:2` по умолчанию слушает порт 5000 и работает по HTTP — Docker такому доверяет только после записи в `insecure-registries`. В продакшене так нельзя, там HTTPS + аутентификация (Harbor — урок 102).

Порты, которые надо помнить: 443 — Docker Hub/внешние реестры; 5000 — дефолт локального `registry:2`; **2375 и 2376 — удалённый Docker API. 2375 — без TLS и без аутентификации, это «root на хосте для всего интернета».** По умолчанию демон на TCP не слушает вообще, и это правильно.

## 3. Аналогия из жизни

Представьте склад «Вектора». **Образ** — это комплект коробок на стеллаже: коробка «базовый nginx», коробка «наши настройки», коробка «наша страница». Коробки опломбированы, менять их нельзя, и одна и та же коробка используется во всех заказах сразу (слои разделяются). **Контейнер** — это рабочее место, собранное из этих коробок, плюс журнал, куда кладовщик пишет всё, что делает: журнал лежит сверху, коробки не трогаются, а когда рабочее место разбирают — журнал выбрасывают вместе с ним. **Том** — сейф, который вынесли из помещения: помещение снесли, сейф стоит. **Реестр** — центральный склад снабжения, откуда привозят коробки и куда отправляют свои. **Сеть** — внутренняя телефонная линия: в обычном общем зале все зовут друг друга по номерам (IP), а в отдельном крыле (user-defined bridge) есть справочная, где ищут по имени.

Аналогия ломается в одном: контейнеры — не отдельные здания, а комнаты в одном доме с общим фундаментом (ядром), и трещина в фундаменте видна всем сразу.

## 4. Практика и команды

Все команды — на **UBU01** под пользователем с `sudo`, если не указано иное.

**1. Версия и состояние демона**

```bash
docker version
docker info
```

`docker version` покажет отдельно Client и Server — если Server не отвечает, значит демон лежит или нет прав на сокет. `docker info` — сводка: `Storage Driver: overlay2`, `Cgroup Version: 2`, `Docker Root Dir: /var/lib/docker`, число контейнеров и предупреждения. Смотреть сюда первым делом при «странном» поведении.

**2. Образы и их слои**

```bash
docker image ls
docker history vektor-web:1.0
docker image inspect nginx:alpine --format '{{.Config.Cmd}} {{.Config.ExposedPorts}}'
```

`image ls` — имя, тег, `IMAGE ID`, `SIZE` (размер верхнего слоя + общий). `history` — какая инструкция Dockerfile дала какой слой; огромный слой `RUN apt install` сразу видно. `inspect --format` вытаскивает поля из JSON-конфига, удобно для скриптов.

**3. Сборка образа**

```bash
cd ~/vektor-web
cat Dockerfile
docker build -t vektor-web:1.0 .
docker build -t vektor-web:1.1 --no-cache .
```

`-t` — имя и тег, `.` — контекст сборки (что отправляется демону). Кэш переиспользует слои, пока не изменилась инструкция и входные файлы; `--no-cache` сбрасывает. Держите `COPY` **после** установки зависимостей, иначе кэш будет инвалидироваться на каждой правке кода.

**4. Запуск контейнера**

```bash
docker run -d --name vektor-web -p 8080:80 \
  --restart unless-stopped \
  --memory 128m --cpus 0.5 \
  vektor-web:1.0
```

`-d` — в фоне, `--name` — предсказуемое имя, `-p host:container` — публикация, `--restart unless-stopped` — поднимать после падения/перезагрузки хоста, `--memory`/`--cpus` — лимиты (без них один контейнер съест всю ВМ). Порт публикуется через iptables — по факту он открыт на `10.10.10.30` для всей VMnet8.

**5. Список, логи, вход внутрь**

```bash
docker ps -a
docker logs --tail 50 -f vektor-web
docker exec -it vektor-web sh
```

`ps -a` показывает и остановленные — их `STATUS` (`Exited (1) 3 seconds ago`, `Restarting (1) ...`) говорит больше всего. `logs -f` — поток логов stdout/stderr, `--tail` ограничивает. `exec -it` — зайти в живой контейнер; если процесс уже упал, нужен `docker run --rm -it --entrypoint /bin/sh <image>`.

**6. Разбор конфигурации**

```bash
docker inspect vektor-web --format '{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}'
docker inspect vektor-web --format '{{json .NetworkSettings.Ports}}'
```

`ExitCode` — почему процесс умер, `RestartCount` — сколько раз перезапускался (растёт → рестарт-петля). `NetworkSettings.Ports` — фактический маппинг `8080→80/tcp`. `docker inspect` без `--format` выдаёт весь JSON — ищите `Mounts`, `Networks`, `Config.Env`.

**7. Ресурсы и место**

```bash
docker stats --no-stream
docker system df -v
```

`stats` — CPU, память, сеть, блоки по каждому контейнеру (аналог `top`). `system df -v` — сколько занимают образы, контейнеры, тома и build cache по отдельности. Классика: «место на диске кончилось» — а это старые образы и тома.

**8. Сети**

```bash
docker network ls
docker network create --driver bridge --subnet 172.28.0.0/24 vektor-net
docker network inspect vektor-net --format '{{json .Containers}}'
docker network connect vektor-net vektor-web
```

`ls` покажет `bridge`, `host`, `none` и ваш `vektor-net`. `inspect` — какие контейнеры в сети и их IP. `connect`/`disconnect` цепляют живой контейнер в сеть без перезапуска.

**9. Тома**

```bash
docker volume create redis-data
docker volume ls
docker volume inspect redis-data
docker volume prune
```

`inspect` даёт `Mountpoint` — реальный путь на хосте (`/var/lib/docker/volumes/redis-data/_data`), оттуда и берётся бэкап. `prune` удаляет **неиспользуемые** тома безвозвратно — только после проверки `docker system df -v`.

**10. Docker Compose**

```bash
docker compose version
docker compose config
docker compose up -d
docker compose ps
docker compose logs -f web
docker compose down
```

`compose version` — плагин v2 (`docker compose`, через пробел), а не устаревший `docker-compose`. `config` валидирует файл и печатает итоговую конфигурацию (видно подставленные переменные и сети) — делайте это перед `up`. `up -d` создаёт сеть, тома и контейнеры; если образ/конфиг изменились — пересоздаёт контейнер (короткий простой!). `down` удаляет контейнеры и сеть, **но не named volumes**; `down -v` — с ними.

**11. Работа с реестром**

```bash
docker run -d -p 5000:5000 --name registry registry:2
docker tag vektor-web:1.0 10.10.10.30:5000/vektor-web:1.0
docker push 10.10.10.30:5000/vektor-web:1.0
curl -s http://10.10.10.30:5000/v2/_catalog
docker pull 10.10.10.30:5000/vektor-web:1.0
```

`tag` не копирует образ, а даёт второе имя. `push` уйдёт в HTTP-реестр только после разрешения:

```json
{
  "insecure-registries": ["10.10.10.30:5000"],
  "live-restore": true
}
```

`/etc/docker/daemon.json`, затем `sudo systemctl restart docker`. `_catalog` — список репозиториев, простейшая проверка, что реестр жив.

**12. Проверка со стороны Windows**

```powershell
Test-NetConnection 10.10.10.30 -Port 8080
curl.exe -I http://10.10.10.30:8080
Invoke-WebRequest http://10.10.10.30:8080 -UseBasicParsing | Select-Object StatusCode
```

`Test-NetConnection` отделит «порт закрыт/не отвечает» от «порт открыт, но приложение отдаёт ошибку». `-I` — только заголовки, быстро.

**13. Уборка (осторожно)**

```bash
docker system prune          # остановленные контейнеры, висячие образы, build cache, сети
docker system prune -a --volumes   # ОПАСНО: + все неиспользуемые образы и тома
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L1 готов: DC01 (AD DS, DNS, DHCP), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01, домен `corp.vektor.lab`.
- **UBU01** — Ubuntu Server 24.04 LTS, `10.10.10.30`, доступ по SSH, есть интернет через VMnet8 NAT (шлюз `10.10.10.2`).
- **WIN11-CL01** — рабочее место ivanov, адрес по DHCP из пула DC01, нужен браузер для проверки порта.
- Docker на стенде ещё не установлен — ставим в этой лабораторной. Это единственное изменение: **новых ВМ не добавляем**, локальный реестр поднимаем контейнером на UBU01. Harbor и его роль появятся позже, в уроке 102.
- Логин на UBU01 — ваш локальный пользователь, член группы `sudo`. В примерах он обозначен как `$USER`.

Дистрибутив Docker CE качается с `download.docker.com`. Из РФ доступность может быть нестабильной — легальная альтернатива: пакет `docker.io` из штатного репозитория Ubuntu (версия старее, Compose ставится отдельно) либо корпоративное зеркало. Пиратские сборки и «активаторы» — нет.

### Снапшот

В VMware Workstation: выключить/приостановить затрагиваемые ВМ → правый клик по вкладке ВМ → **Snapshot → Take Snapshot** → имя `before-l2-096`.

Машины: **UBU01** и **WIN11-CL01** (вторую — на случай, если будем менять сетевые настройки; изменения минимальные). Если предпочитаете CLI: `vmrun snapshot "<путь к .vmx>" before-l2-096` [ПРОВЕРИТЬ путь к vmrun в вашей версии Workstation].

### Шаги

1. **UBU01.** Устанавливаем Docker Engine из официального репозитория:
   ```bash
   sudo apt update && sudo apt install -y ca-certificates curl gnupg
   sudo install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
   sudo chmod a+r /etc/apt/keyrings/docker.gpg
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt update
   sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   ```
   **Ожидаемо:** пакеты установлены, `systemctl is-active docker` → `active`.

2. **UBU01.** Права пользователю и проверка:
   ```bash
   sudo usermod -aG docker $USER
   newgrp docker
   docker run --rm hello-world
   docker info | grep -E 'Storage Driver|Cgroup Version|Docker Root Dir'
   ```
   **Ожидаемо:** «Hello from Docker!», `overlay2`, `2`, `/var/lib/docker`. (В реальной среде в группу `docker` пускают не всех: членство в ней = root.)

3. **UBU01.** Создаём проект:
   ```bash
   mkdir -p ~/vektor-web && cd ~/vektor-web
   printf '<h1>Vektor: внутренняя страница учета</h1>\n' > index.html
   cat > Dockerfile <<'EOF'
   FROM nginx:alpine
   COPY index.html /usr/share/nginx/html/index.html
   EXPOSE 80
   EOF
   docker build -t vektor-web:1.0 .
   docker history vektor-web:1.0
   ```
   **Ожидаемо:** сборка из 2–3 шагов, в `history` верхний слой — `COPY index.html`.

4. **UBU01.** Проверяем кэш слоёв:
   ```bash
   printf '<h1>Vektor: страница учета v2</h1>\n' > index.html
   docker build -t vektor-web:1.1 .
   ```
   **Ожидаемо:** строка `CACHED` напротив `FROM`/`COPY` базовых слоёв, пересобирается только слой с `index.html`.

5. **UBU01.** Запускаем и публикуем порт:
   ```bash
   docker run -d --name vektor-web -p 8080:80 --restart unless-stopped --memory 128m vektor-web:1.1
   docker ps
   curl -I http://127.0.0.1:8080
   ```
   **Ожидаемо:** контейнер `Up`, `curl` отдаёт `HTTP/1.1 200 OK`, `Server: nginx/...`.

6. **WIN11-CL01.** Открываем браузер: `http://10.10.10.30:8080`. **Ожидаемо:** страница «Vektor: страница учета v2». Если не открывается — `Test-NetConnection 10.10.10.30 -Port 8080` и проверка, что у UBU01 в `ufw status` порт не заблокирован (на учебном стенде ufw обычно выключен — но в бою так нельзя).

7. **UBU01.** Своя сеть и резолв по имени:
   ```bash
   docker network create --driver bridge --subnet 172.28.0.0/24 vektor-net
   docker run -d --name cache --network vektor-net redis:7-alpine
   docker network connect vektor-net vektor-web
   docker run --rm -it --network vektor-net alpine sh -c "apk add --no-cache bind-tools >/dev/null && nslookup cache"
   ```
   **Ожидаемо:** `nslookup` возвращает IP контейнера `cache` в `172.28.0.x`. Повторите то же в сети `bridge` — имя не резолвится.

8. **UBU01.** Том и сохранность данных:
   ```bash
   docker volume create redis-data
   docker run -d --name cache2 --network vektor-net -v redis-data:/data redis:7-alpine
   docker exec -it cache2 redis-cli set key1 "vektor"
   docker rm -f cache2
   docker run -d --name cache3 --network vektor-net -v redis-data:/data redis:7-alpine
   docker exec -it cache3 redis-cli get key1
   docker volume inspect redis-data
   ```
   **Ожидаемо:** `"vektor"` — данные пережили удаление контейнера; в `inspect` виден `Mountpoint`.

9. **UBU01.** Compose-стек:
   ```bash
   cd ~/vektor-web && cat > compose.yaml <<'EOF'
   name: vektor
   services:
     web:
       image: vektor-web:1.1
       ports:
         - "8080:80"
       networks: [vektor-net]
       restart: unless-stopped
     cache:
       image: redis:7-alpine
       networks: [vektor-net]
       volumes:
         - redis-data:/data
   networks:
     vektor-net:
       driver: bridge
   volumes:
     redis-data:
   EOF
   docker rm -f vektor-web cache
   docker compose config
   docker compose up -d
   docker compose ps
   curl -I http://127.0.0.1:8080
   ```
   **Ожидаемо:** `config` печатает разрешённый YAML; `ps` — два сервиса `running`; стек отвечает на 8080. (Redis без пароля — только для стенда; в бою `requirepass` и сеть без выхода наружу.)

10. **UBU01.** Локальный реестр:
    ```bash
    docker run -d -p 5000:5000 --name registry registry:2
    curl -s http://127.0.0.1:5000/v2/_catalog
    sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
    { "insecure-registries": ["10.10.10.30:5000"], "live-restore": true }
    EOF
    sudo systemctl restart docker
    docker tag vektor-web:1.1 10.10.10.30:5000/vektor-web:1.1
    docker push 10.10.10.30:5000/vektor-web:1.1
    curl -s http://10.10.10.30:5000/v2/_catalog
    ```
    **Ожидаемо:** первый `_catalog` — `{"repositories":[]}`, после `push` — `{"repositories":["vektor-web"]}`. `systemctl restart docker` останавливает контейнеры; с `unless-stopped` они поднимутся сами (с `live-restore` демон их вообще не трогает) — проверьте `docker compose ps`.

11. **UBU01.** Лимиты и гигиена:
    ```bash
    docker stats --no-stream
    docker system df -v
    docker image prune
    ```
    **Ожидаемо:** у `web` потребление памяти в пределах 128 МБ, в `df -v` видно, что занимает место. Ради практики: `docker stop cache` → пользовательская страница продолжает работать, потому что она не зависит от кэша (проверьте `curl -I`).

12. **Запись в `stand-log.md`** (на любом хосте): что установлено, какие имена/порты/тома появились. Без этой записи через месяц вы будете гадать, откуда на 8080 чужой nginx.

### Проверка

- [ ] `docker info` на UBU01 показывает `overlay2` и `Docker Root Dir: /var/lib/docker`.
- [ ] `docker image ls` содержит `vektor-web:1.0` и `vektor-web:1.1`, `docker history` показывает слой `COPY index.html`.
- [ ] `http://10.10.10.30:8080` открывается в браузере на WIN11-CL01.
- [ ] `docker run --rm -it --network vektor-net alpine ... nslookup cache` возвращает IP.
- [ ] `docker volume ls` содержит `redis-data`, ключ `key1` читается после пересоздания контейнера.
- [ ] `docker compose ps` — оба сервиса `running`, `docker compose config` валиден.
- [ ] `curl -s http://10.10.10.30:5000/v2/_catalog` возвращает `vektor-web`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

```text
Пользователь: ivanov (Иван Иванов, бухгалтерия)
ВМ:           WIN11-CL01
Что написал:  «Внутренняя страница http://10.10.10.30:8080 не открывается — „сайт недоступен“.
               Утром работала. Сверяем реестр платежей, нужна сегодня.»
Срочность:    Средняя (блокирует часть работы)
```

**Воспроизводим поломку на UBU01** (имитация «коллега тестировал и забыл»):

```bash
cd ~/vektor-web && docker compose down
docker run -d --name test-nginx -p 8080:80 nginx:alpine
docker compose up -d
```

Последняя команда упадёт с ошибкой вида `driver failed programming external connectivity ... Bind for 0.0.0.0:8080 failed: port is already allocated`. Сайт лежит.

**Диагностика по шагам:**

1. WIN11-CL01: `Test-NetConnection 10.10.10.30 -Port 8080` → `TcpTestSucceeded: False`. Значит, проблема не в браузере и не в DNS, а на стороне UBU01.
2. UBU01: `docker ps -a` → есть `test-nginx` (`Up`), а `vektor-web` либо отсутствует, либо `Exited`. Уже почти ответ.
3. `docker compose ps` → пусто: стек не поднят.
4. `docker logs --tail 20 test-nginx` → обычные логи nginx, никаких ошибок. Приложение исправно, но оно **чужое** — это не наш `vektor-web`.
5. `ss -lntp | grep ':8080'` → порт занят процессом `docker-proxy`/`nginx`.
6. `docker inspect test-nginx --format '{{.Config.Image}} {{.Name}}'` → `nginx:alpine /test-nginx` — подтверждаем, что это не сервис «Вектора».
7. `docker compose up -d` вручную → сообщение `port is already allocated` — корневая причина зафиксирована.

**Решение:**

```bash
docker rm -f test-nginx
docker compose up -d
docker compose ps
curl -I http://127.0.0.1:8080
```

Проверяем с WIN11-CL01 в браузере — страница на месте.

**Ответ пользователю:** «Иван, добрый день. Причина — на сервере порт 8080 занял тестовый контейнер, наш сервис из-за этого не запустился. Освободил порт, сервис поднят, проверил: страница `http://10.10.10.30:8080` открывается, данные на месте. Если снова пропадёт — сразу пишите, разберусь.»

**Закрытие заявки:** категория «Внутренний веб-сервис / контейнеры», корневая причина «конфликт опубликованного порта, тестовый контейнер вне регламента», решение «удалён посторонний контейнер, стек перезапущен», время в работе. В постмортеме (урок 92) — пункт «порты, занятые контейнерами, фиксировать в CMDB» (урок 93).

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен Docker Engine (CE) + плагины `buildx`, `compose`; пользователь добавлен в группу `docker`; создан `/etc/docker/daemon.json` с `insecure-registries` и `live-restore`.
- **UBU01:** каталог проекта `~/vektor-web` (`index.html`, `Dockerfile`, `compose.yaml`).
- **UBU01:** образы `vektor-web:1.0`, `vektor-web:1.1`, `nginx:alpine`, `redis:7-alpine`, `registry:2`, `alpine`.
- **UBU01:** сеть `vektor-net` (172.28.0.0/24), тома `redis-data`, контейнеры стека `vektor` (`web`, `cache`) и `registry`.
- **UBU01:** опубликованы порты **8080** (страница «Вектора») и **5000** (локальный реестр, только учебный, без TLS).
- **WIN11-CL01:** изменений в системе нет, использовался как клиент для проверки.
- Запись в `stand-log.md` сделана.

### Откат

Полный откат — снапшот `before-l2-096` в VMware Workstation: правый клик по UBU01 → **Snapshot Manager** → выбрать `before-l2-096` → **Restore**. WIN11-CL01 не менялся, его можно не откатывать.

Частичный откат без снапшота:

```bash
cd ~/vektor-web && docker compose down -v
docker rm -f registry test-nginx 2>/dev/null
docker network rm vektor-net
docker image rm vektor-web:1.0 vektor-web:1.1 10.10.10.30:5000/vektor-web:1.1
sudo apt purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo rm -f /etc/docker/daemon.json
```

Помните: `docker compose down -v` уничтожает данные в томах. На учебном стенде это нормально, в бою — только после бэкапа.

## 6. Ошибки новичков и безопасность

1. **Тег `latest` в продакшене** → невозможно понять, какая версия реально запущена, откат наугад. → Явные теги (`:1.1`) и, для критичного, digest (`@sha256:...`).
2. **Секреты в Dockerfile или `compose.yaml`** (`ENV DB_PASSWORD=...`, `docker build --build-arg`) → пароль навсегда в слое образа и в реестре, виден через `docker history`/`inspect`. → Переменные окружения из файла с правами `600`, секрет-хранилище (урок 102).
3. **`docker system prune -a --volumes` «чтобы место освободить»** → сносит образы и тома вместе с данными БД. → Сначала `docker system df -v`, удалять точечно, тома — только с бэкапом.
4. **Bind mount `-v /var/run/docker.sock:/var/run/docker.sock`** → контейнер управляет демоном, то есть получает root на хосте. → Не монтировать; если очень надо — read-only и понимание риска.
5. **Нет лимитов `--memory`/`--cpus`** → один контейнер с утечкой памяти роняет всю ВМ (урок 95 про capacity planning не зря). → Лимиты + мониторинг (Zabbix/Prometheus из уроков 89–90).
6. **Публикация порта `-p 5432:5432` «на всякий случай»** → БД торчит в офисную сеть без шифрования. → Публиковать только нужное, остальное — через внутреннюю сеть Docker.
7. **Правка файлов внутри контейнера через `exec`** → при следующем `up -d` всё исчезнет, «пропали настройки». → Всё через образ/Dockerfile или volume/конфиг-файл на хосте.
8. **Игнорирование `.dockerignore`** → в контекст сборки улетают `.git`, `node_modules`, бэкапы; образ раздувается до гигабайт. → `.dockerignore` в каждом проекте.

**Безопасность.** В боевой среде нельзя: слушать Docker API на `0.0.0.0:2375` (это shell на хосте без пароля); держать реестр по HTTP без аутентификации и с доступом из всех сетей; запускать контейнеры от root без необходимости (используйте `USER` в Dockerfile и `--read-only`, `--cap-drop` по возможности); хранить образы с секретами в публичном реестре. На стенде `insecure-registries` и открытый порт 5000 — учебное упрощение: в реальности нужен TLS и авторизация (Harbor, урок 102).

## 7. Вопросы на собеседовании

**1. Чем образ отличается от контейнера и почему контейнеры такие лёгкие?**

Ответ: образ — неизменяемый набор read-only слоёв плюс JSON-конфиг с командой запуска, переменными и портами. Контейнер — запущенный из образа процесс с добавленным сверху writable-слоем (copy-on-write), своими namespaces (PID, net, mnt, uts, ipc, user) и ограничениями cgroups. Лёгкие потому, что нет гостевого ядра и нет копирования ФС: ядро общее (хостовое), слои разделяются между всеми контейнерами, запуск — это старт процесса, а не загрузка ОС.
*Что хочет услышать интервьюер:* понимание namespaces/cgroups и того, что контейнер — процесс, а не мини-ВМ.
*Красный флаг:* «контейнер — это лёгкая виртуалка со своим ядром».

**2. Named volume или bind mount — что и когда?**

Ответ: named volume управляется Docker, лежит в `/var/lib/docker/volumes`, переносим между окружениями, переживает удаление контейнера, безопаснее — это выбор для данных БД и состояния приложений. Bind mount — конкретный путь хоста, нужен для конфигов, логов, разработки; минус — жёсткая привязка к структуре хоста и риск случайно дать контейнеру доступ к чувствительным каталогам. tmpfs — для временных данных в памяти.
*Что хочет услышать интервьюер:* что данные БД не держат в writable-слое контейнера и что volumes надо бэкапить.
*Красный флаг:* «данные можно хранить в контейнере, они же не удаляются».

**3. Почему в user-defined bridge контейнеры видят друг друга по имени, а в default bridge — нет?**

Ответ: в user-defined сети Docker поднимает встроенный DNS-сервер `127.0.0.11`, который знает имена/алиасы контейнеров этой сети. В дефолтном `docker0` такого DNS нет, и остаётся только резолв по IP или устаревший `--link`. Плюс user-defined сеть даёт изоляцию: контейнеры других сетей не видны, тогда как default bridge общий для всех.
*Что хочет услышать интервьюер:* что вы понимаете DNS внутри Docker и почему в Compose сервисы обращаются друг к другу по имени сервиса.
*Красный флаг:* «Docker сам как-то решает» без упоминания DNS.

**4. Что произойдёт при `docker compose up -d`, если изменился образ или `compose.yaml`?**

Ответ: Compose сравнит желаемое состояние с текущим и пересоздаст (recreate) затронутые контейнеры: старый останавливается и удаляется, новый создаётся из нового образа/конфига. Данные в named volumes сохраняются, данные в writable-слое — теряются. Это простой сервиса на время пересоздания, поэтому в продакшене — окно изменений и, для критичных сервисов, несколько реплик за прокси. `docker compose ps` покажет новый `CREATED`.
*Что хочет услышать интервьюер:* понимание, что `up -d` — это не «перезапустить», а «привести к декларированному состоянию», и что это изменение в терминах change-management.
*Красный флаг:* «`up -d` ничего не трогает, если контейнер уже запущен».

**5. Контейнер постоянно перезапускается. Ваши действия?**

Ответ: `docker ps -a` → смотрю `STATUS` и `RestartCount`; `docker inspect --format '{{.State.ExitCode}}'` → код выхода (1 — ошибка приложения, 137 — OOM-kill или SIGKILL, 143 — SIGTERM); `docker logs --tail 200` → текст ошибки; проверяю переменные окружения, наличие тома, доступность зависимостей (БД, DNS), лимиты памяти (`--memory` слишком мал → 137). Если логов нет — `docker run --rm -it --entrypoint /bin/sh <image>` и запускаю команду вручную. Дальше либо чиню конфиг/образ, либо откатываюсь на предыдущий тег.
*Что хочет услышать интервьюер:* дисциплина диагностики и знание кода 137/OOM, а не «перезапущу и посмотрю».
*Красный флаг:* «перезапущу контейнер несколько раз, вдруг поднимется».

## 8. Источники (официальная документация)

- **docs.docker.com** — раздел «Docker Engine / Install / Install on Ubuntu» (установка, репозиторий, ключи). Поиск: `docker docs install docker engine ubuntu`.
- **docs.docker.com** — «Get started / What is an image», «Build / Dockerfile reference», «Build best practices» (слои, кэш, multi-stage). Поиск: `dockerfile reference best practices`.
- **docs.docker.com** — «Engine / Networking overview» и «Network drivers» (bridge, host, none, overlay; встроенный DNS). Поиск: `docker networking overview bridge driver`.
- **docs.docker.com** — «Engine / Storage / Volumes» (named volume vs bind mount, `--mount`, бэкап томов). Поиск: `docker volumes documentation`.
- **docs.docker.com** — «Compose / Compose file reference» и «Compose CLI reference» (`up`, `config`, `down`, `depends_on`). Поиск: `docker compose file reference`.
- **docs.docker.com** — «Registry / Deploy a registry server» (локальный `registry:2`, `insecure-registries`, TLS). Поиск: `docker registry deploy registry server`.
- **hub.docker.com** — официальные страницы образов `nginx`, `redis`, `registry` (переменные, пути томов). Поиск: `docker hub official image nginx`.
- **docs.docker.com** — «Engine / Security» (rootless, `--cap-drop`, `--read-only`, почему сокет опасен). Поиск: `docker engine security`.
- **learn.microsoft.com** — статьи про WSL2 и Docker Desktop, если позже понадобится контейнерная разработка на Windows-рабочем месте (лицензия Docker Desktop для компаний — отдельная тема) `[ПРОВЕРИТЬ актуальные условия лицензии]`. Поиск: `docker desktop license terms windows containers`.
- **ubuntu.com** — Server documentation, раздел про пакеты и `systemd` (`systemctl restart docker`, `live-restore`). Поиск: `ubuntu server documentation systemd services`.
