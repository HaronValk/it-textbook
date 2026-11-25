# Контейнеры для сисадмина: Docker, Podman, compose, registries, безопасность

## 1. Введение (зачем это нужно в реальной работе)

Через день после того, как ты соберёшь стенд из этой главы, в тикете появится такое:

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **Что написал:** «Здравствуйте! Вчера вы показывали страницу с инструкцией по печати — http://ctr01.corp.vektor.lab:8080. Сегодня утром она не открывается, а мне до 11:00 нужно распечатать приказ. Срочно!»
> **Срочность:** средняя

Дальше ты будешь выяснять, что контейнер не поднялся после перезагрузки сервера, потому что у сервиса не было политики перезапуска. Это типичная контейнерная заявка — и типичная причина, по которой сисадмин вообще должен знать Docker.

Контейнеры в 2026 году — это не «модно у разработчиков», а рабочий инструмент инфраструктурщика: на них крутятся внутренние сервисы (вики, реестры, мониторинг, прокси, Git, CI-раннеры), ими пользуются для быстрой проверки гипотез («а как ведёт себя nginx с этой конфигурацией?»), и почти любой DevOps-собес начинается с вопроса про разницу между контейнером и виртуалкой. После этой главы ты будешь уметь: поставить Docker Engine и Podman на Linux-хост, поднять сервис одной командой и целым стеком через compose, держать собственный реестр образов внутри сети, настроить автозапуск и, главное, понимать, где контейнеры дают тебе дырку в безопасности и как её закрыть.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Контейнер — это не виртуалка.** Виртуалка эмулирует железо и запускает своё ядро. Контейнер — это обычный процесс (или группа процессов) на твоём же хосте, которому ядро Linux подсовывает иллюзию отдельной машины. Иллюзию делают две подсистемы ядра:

- **namespaces** — изоляция видимости. `pid` (своя нумерация процессов), `net` (свои интерфейсы и порты), `mnt` (своя файловая система), `uts` (своё имя хоста), `ipc`, `user` (свой root внутри, не root снаружи), `cgroup`.
- **cgroups v2** — ограничение ресурсов: сколько CPU, памяти, дискового I/O может съесть группа процессов.

Файловая система контейнера собирается из **слоёв образа** через драйвер `overlay2`: слои только для чтения + тонкий записываемый слой сверху. Поэтому 20 контейнеров из образа nginx занимают место одного образа.

**Образ** — это не «архив», а набор: слои (tar-ы), манифест (список слоёв + хеши) и конфиг (какая команда запускается, какие порты, какие переменные). Всё описано открытыми спецификациями **OCI**: image-spec, runtime-spec, distribution-spec. Именно поэтому образ, собранный Docker, запускается Podman-ом и наоборот.

Стек Docker на Linux:

```text
 ┌───────────────────── CTR01 (Ubuntu 24.04, 10.10.10.35) ─────────────────────┐
 │                                                                             │
 │  sysadm ──docker/compose──► dockerd ──► containerd ──► runc                 │
 │                              │                          │                   │
 │                     /var/run/docker.sock                ▼                   │
 │                     (HTTP API, root-эквивалент)   процессы контейнера        │
 │                              │                    namespaces + cgroups      │
 │                              ▼                                              │
 │                    iptables/nftables DNAT: 8080→172.18.0.2:80, 5000→...     │
 └─────────────────────────────────────────────────────────────────────────────┘
```

- `dockerd` — демон, слушает Unix-сокет `/var/run/docker.sock`. **Кто имеет доступ к сокету — тот root на хосте.**
- `containerd` — менеджер жизненного цикла контейнеров и образов.
- `runc` — низкоуровневый рантайм: непосредственно создаёт namespaces, монтирует слои, применяет seccomp/AppArmor и делает `execve`.

**Сети.** По умолчанию контейнер попадает в bridge-сеть `docker0` и получает адрес вида 172.17.0.0/16. Наружу порт публикуется через DNAT в iptables/nftables (`-p 8080:80`). В user-defined сети (в compose она создаётся автоматически) работает встроенный DNS: контейнеры видят друг друга **по имени сервиса**, а не по IP. Это уровень L3/L4 модели OSI; сами сервисы внутри — уже L7 (HTTP, SSH и т.д.).

**Реестр (registry).** Сервер, отдающий образы по HTTP API v2 (`/v2/`, `/v2/<name>/manifests/<tag>`). Публичный — Docker Hub (HTTPS, 443, rate limits для анонимов). Корпоративный — свой: Docker Registry (`registry:2`), Harbor, Nexus. Для внутренней сети стенда мы поднимем `registry:2` на порту 5000 по HTTP — **только для лабы**, в продакшене обязательны TLS и аутентификация.

**Podman** — тот же OCI, но без демона: каждый контейнер — прямой потомок процесса `podman`, изоляция та же, но нет постоянно работающего root-демона, и rootless-режим (запуск от обычного пользователя через user namespaces) включён по умолчанию. Автозапуск — через systemd: Quadlet-юниты или `podman generate systemd` [ПРОВЕРИТЬ: `generate systemd` помечен устаревшим в новых версиях, актуальный путь — Quadlet].

## 3. Аналогия из жизни

Морской контейнер. До него груз перевозили как попало: ящики, бочки, мешки — каждый раз перекладывали вручную, и каждый порт был головной болью. Придумали стандартный стальной ящик: внутри может быть что угодно, а снаружи — одни и те же крепления, поэтому его одинаково берёт любой кран, любое судно, любая платформа. Так и Docker-образ: внутри твой nginx с конфигами и зависимостями, а снаружи — стандартный интерфейс OCI, и неважно, на каком хосте его запускать.

Где аналогия ломается: у морского контейнера стенки стальные и свои. У контейнера Docker стенок нет — все «жильцы» пользуются **одним ядром хоста**, и уязвимость ядра или ошибка в настройке прав пробивает перегородки сразу для всех.

## 4. Практика и команды

**1. Проверка установки и состояния демона.**

```bash
docker version            # клиент + сервер (если сервер не отвечает — демон не запущен)
docker info               # драйвер хранилища (overlay2), cgroup v2, Security Options
systemctl status docker
sudo usermod -aG docker sysadm   # после этого перелогиниться
```

В `docker info` смотри строки `Storage Driver`, `Cgroup Version`, `Security Options` (должны быть `apparmor` и `seccomp`) и `Docker Root Dir` (`/var/lib/docker`).

**2. Запуск контейнера.**

```bash
docker run -d --name vektor-web -p 8080:80 --restart unless-stopped nginx:alpine
```

`-d` — в фоне, `--name` — имя, `-p host:container` — публикация порта, `--restart unless-stopped` — поднимать после ребута, пока не остановили руками. `nginx:alpine` — образ и тег; без тега подставится `latest`, чего в работе избегают.

**3. Список и логи.**

```bash
docker ps -a
docker logs --tail 50 -f vektor-web
```

```text
CONTAINER ID   IMAGE          STATUS          PORTS                  NAMES
7f3a1c9b2e10   nginx:alpine   Up 2 hours      0.0.0.0:8080->80/tcp   vektor-web
```

Смотри колонку `STATUS`: `Up` — работает, `Exited (1)` — упал и нужно читать логи, `Restarting` — цикл падений.

**4. Зайти внутрь и посмотреть глазами.**

```bash
docker exec -it vektor-web sh
# внутри:
ps aux; ip a; cat /etc/nginx/nginx.conf; exit
```

`exec` запускает процесс в уже работающем контейнере. `-it` — интерактивно с TTY. В alpine нет bash — только `sh`.

**5. Инспекция без входа внутрь.**

```bash
docker inspect --format '{{.State.Status}} {{.State.Health.Status}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' vektor-web
```

```text
running healthy 172.18.0.2
```

`inspect` отдаёт JSON; `--format` с шаблоном Go вытаскивает нужное поле. Так удобно снимать IP и статус healthcheck в скриптах и отчётах.

**6. Потребление ресурсов и место на диске.**

```bash
docker stats --no-stream
docker system df
```

```text
TYPE            TOTAL   ACTIVE   SIZE     RECLAIMABLE
Images          5       3        812MB    310MB (38%)
Containers      4       2        1.2MB    0B (0%)
Local Volumes   3       2        45MB     12MB (26%)
```

`docker images`/`system df` — первое, что проверяют при «на сервере кончилось место». Чистка: `docker image prune`, `docker builder prune`; `docker system prune -a --volumes` удаляет и данные томов — осторожно.

**7. Тег и отправка образа в свой реестр.**

```bash
docker tag nginx:alpine 10.10.10.35:5000/vektor/nginx:1.0
docker push 10.10.10.35:5000/vektor/nginx:1.0
curl -s http://10.10.10.35:5000/v2/_catalog
```

```text
{"repositories":["vektor/nginx"]}
```

`_catalog` — эндпоинт API v2 со списком репозиториев. Для HTTP-реестра (не HTTPS) на удалённом хосте нужен `insecure-registries` в `/etc/docker/daemon.json`:

```json
{
  "insecure-registries": ["10.10.10.35:5000"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```

После правки — `sudo systemctl restart docker`. Это **только для учебного стенда**: HTTP-реестр означает передачу образов открытым текстом.

**8. Compose: описание стека файлом.**

```yaml
name: vektor-web

services:
  web:
    image: nginx:alpine
    container_name: vektor-web
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - ./html:/usr/share/nginx/html:ro
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost/"]
      interval: 30s
      timeout: 3s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks: [front]

  registry:
    image: registry:2
    container_name: vektor-registry
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - registry-data:/var/lib/registry
    networks: [front]

networks:
  front:

volumes:
  registry-data:
```

Важно: ключ `version:` в Compose v2 **устарел** и не нужен. `restart`, `healthcheck` и лимиты логов задаются на уровне сервиса.

**9. Управление стеком.**

```bash
docker compose config          # проверка и нормализация YAML, подстановка переменных
docker compose up -d           # создать/обновить
docker compose ps              # состояние сервисов
docker compose logs -f web     # логи конкретного сервиса
docker compose down            # остановить и удалить контейнеры и сеть (тома остаются)
docker compose down -v         # ...и удалить тома = потерять данные реестра
```

`docker compose config` — самый недооценённый шаг: он парсит файл и показывает итоговую конфигурацию до запуска.

**10. Podman без демона и автозапуск через systemd.**

```bash
sudo apt install -y podman
podman run -d --name podman-web -p 8081:80 nginx:alpine
podman ps
```

Rootless-автозапуск (Quadlet), файл `~/.config/containers/systemd/podman-web.container`:

```ini
[Unit]
Description=Vektor test web (rootless)

[Container]
Image=nginx:alpine
ContainerName=podman-web
PublishPort=8081:80

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user start podman-web.service
loginctl enable-linger sysadm    # чтобы user-сервисы жили без активной сессии
```

[ПРОВЕРИТЬ: для сгенерированных Quadlet-юнитов `systemctl --user enable` обычно не требуется — включение задаётся секцией `[Install]`; детали смотри в актуальной документации Podman.]

**11. Проверка с Windows-клиента.**

```powershell
Resolve-DnsName ctr01.corp.vektor.lab
Test-NetConnection ctr01.corp.vektor.lab -Port 8080
```

`Resolve-DnsName` покажет A-запись с DC01, `Test-NetConnection` — `TcpTestSucceeded : True`. Это первый шаг любой заявки «не открывается».

**12. Сканирование образа на уязвимости.**

```bash
docker run --rm aquasec/trivy:latest image nginx:alpine
```

Смотри секцию `Total: N (CRITICAL: x, HIGH: y)` и список CVE. Если trivy не может достать образ через сокет — добавь `--image-src remote` [ПРОВЕРИТЬ флаг в текущей версии].

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Готовый стенд L2: DC01 (10.10.10.10, AD DS + DNS), WIN11-CL01, UBU01 (10.10.10.30, Ubuntu 24.04, SSH). PVE01 не нужен — не трогаем.

Тема требует машины сверх таблицы, поэтому **явно добавляем** в стенд:

| Имя | ОС | Адрес | Роль |
|-----|----|-------|------|
| CTR01 | Ubuntu Server 24.04 LTS | 10.10.10.35 (статика) | контейнерный хост: Docker Engine, compose, локальный реестр |

Локальный пользователь на Linux-хостах — `sysadm`. Реестр Harbor (HARBOR01) не создаём: он появится на треке DevOps.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-sysadmin-078` на **UBU01** и на **CTR01** (после её создания). На DC01 — тоже, если будешь менять DNS-зону.

### Шаги

1. **Создать CTR01.** VMware Workstation → New Virtual Machine → Ubuntu Server 24.04 ISO, 2 vCPU, 2 ГБ RAM, 20 ГБ диск, сеть **VMnet8**. Установить ОС.
   *Ожидаемо:* система загружается, вход по `sysadm`.
2. **Настроить сеть и имя на CTR01.**

```bash
sudo hostnamectl set-hostname ctr01
ip -br a                     # узнать имя интерфейса, обычно ens33
sudo nano /etc/netplan/50-cloud-init.yaml
```

```yaml
network:
  version: 2
  ethernets:
    ens33:
      dhcp4: false
      addresses: [10.10.10.35/24]
      routes:
        - to: default
          via: 10.10.10.2
      nameservers:
        addresses: [10.10.10.10]
```

```bash
sudo netplan apply && ip -br a && ping -c2 10.10.10.10
```

*Ожидаемо:* адрес 10.10.10.35, пинг до DC01 идёт.
3. **Добавить A-запись в DNS.** На **DC01** в PowerShell от администратора:

```powershell
Add-DnsServerResourceRecordA -Name "ctr01" -ZoneName "corp.vektor.lab" -IPv4Address "10.10.10.35"
```

*Ожидаемо:* на WIN11-CL01 `Resolve-DnsName ctr01.corp.vektor.lab` возвращает 10.10.10.35.
4. **Установить Docker Engine из официального репозитория Docker.**

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

*Ожидаемо:* `docker version` показывает и клиент, и сервер. Если `download.docker.com` недоступен из сети стенда, легальная альтернатива — пакет `docker.io` из репозитория Ubuntu (версия старше, compose отдельным пакетом `docker-compose-v2` [ПРОВЕРИТЬ наличие в 24.04]).
5. **Права и проверка.**

```bash
sudo usermod -aG docker sysadm     # перелогиниться
docker run --rm hello-world
```

*Ожидаемо:* сообщение `Hello from Docker!`. Помни: группа `docker` = root.
6. **Первый сервис.** Подготовить каталог и страницу:

```bash
mkdir -p ~/vektor-web/html && cd ~/vektor-web
echo '<h1>Vektor: инструкция по печати</h1>' > html/index.html
docker run -d --name vektor-web -p 8080:80 -v ~/vektor-web/html:/usr/share/nginx/html:ro nginx:alpine
curl -s http://localhost:8080
```

*Ожидаемо:* в ответе HTML с заголовком Vektor.
7. **Настроить daemon.json** (insecure-registry для HTTP-реестра + ротация логов по умолчанию), перезапустить демон.

```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "insecure-registries": ["10.10.10.35:5000"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
docker ps
```

*Ожидаемо:* демон активен, контейнер `vektor-web` **не** поднялся автоматически (у него нет `--restart`) — это нормально и понадобится в разделе «Сломай и почини».
8. **Перевести сервис на compose.** Удалить ручной контейнер, создать `compose.yaml` из раздела 4 (пункт 8):

```bash
docker rm -f vektor-web
nano compose.yaml
docker compose config
docker compose up -d
docker compose ps
```

*Ожидаемо:* два сервиса `running`, у `web` статус `healthy` через ~30 секунд.
9. **Проверить реестр и отправить образ.**

```bash
curl -s http://10.10.10.35:5000/v2/_catalog
docker tag nginx:alpine 10.10.10.35:5000/vektor/nginx:1.0
docker push 10.10.10.35:5000/vektor/nginx:1.0
curl -s http://10.10.10.35:5000/v2/_catalog
```

*Ожидаемо:* до push — `{"repositories":[]}`, после — `{"repositories":["vektor/nginx"]}`.
10. **Проверить с рабочей станции.** На WIN11-CL01: открыть `http://ctr01.corp.vektor.lab:8080`, затем `Test-NetConnection ctr01.corp.vektor.lab -Port 5000`.
    *Ожидаемо:* страница открывается, оба порта доступны.
11. **Проверить безопасность контейнера** (см. также раздел 6):

```bash
docker run --rm --user 1000:1000 --read-only --cap-drop=ALL --security-opt=no-new-privileges:true --tmpfs /tmp alpine:3.20 sh -c 'id; touch /etc/test 2>&1'
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock docker:cli ps
```

*Ожидаемо:* `id` показывает uid=1000; попытка записи в `/etc` — `Read-only file system`; вторая команда показывает контейнеры хоста (то есть контейнер получил управление Docker = root хоста).
12. **Podman на UBU01.**

```bash
sudo apt install -y podman
podman run -d --name podman-web -p 8081:80 nginx:alpine
podman ps && curl -s http://localhost:8081 | head -n 3
```

Создать Quadlet-юнит (раздел 4, пункт 10), выполнить `systemctl --user daemon-reload && systemctl --user start podman-web.service`.
*Ожидаемо:* `podman ps` показывает контейнер от пользователя `sysadm` (rootless), порт 8081 отвечает.
13. **Записать изменения в `stand-log.md`.**

### Проверка

- [ ] CTR01 отвечает по 10.10.10.35, имя хоста `ctr01`, DNS-запись в зоне `corp.vektor.lab` есть
- [ ] `docker version` показывает и клиент, и сервер; `docker info` — драйвер `overlay2`, `cgroup v2`, `apparmor`, `seccomp`
- [ ] `docker compose ps` — оба сервиса `running`, `web` в состоянии `healthy`
- [ ] `curl http://localhost:8080` отдаёт страницу, `http://ctr01.corp.vektor.lab:8080` открывается с WIN11-CL01
- [ ] `curl http://10.10.10.35:5000/v2/_catalog` показывает `vektor/nginx`
- [ ] `docker compose logs web` не содержит ошибок, логи ротируются (`max-size`)
- [ ] `docker run --rm --read-only ...` не даёт писать в `/etc`
- [ ] На UBU01 `podman ps` работает от `sysadm` без sudo, Quadlet-сервис запускается через `systemctl --user`
- [ ] В `stand-log.md` записаны CTR01, её адрес, установленное ПО, имя compose-проекта и тома

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **Что написал:** «Страница http://ctr01.corp.vektor.lab:8080 не открывается. Вчера работала. До 11:00 надо распечатать приказ!»
> **Срочность:** средняя

**Воспроизводим поломку** (на CTR01):

```bash
docker compose down                 # имитация «после перезагрузки/ручных действий»
docker run -d --name old-nginx -p 8080:80 nginx:alpine   # забытый контейнер держит порт
sudo systemctl restart docker
docker compose up -d
docker compose ps
```

Теперь `web` не может занять порт 8080 (его держит `old-nginx`), а политика перезапуска не спасает.

**Диагностика по порядку:**

1. С клиента: `Test-NetConnection ctr01.corp.vektor.lab -Port 8080` → `TcpTestSucceeded : False`. Значит, проблема не в DNS и не в браузере, а в сервисе.
2. На CTR01: `ss -lntp | grep 8080` → порт слушает **другой** процесс/контейнер.
3. `docker ps -a` → видим и `vektor-web` в статусе `Restarting` или `Exited`, и посторонний `old-nginx` на 8080.
4. `docker compose logs --tail 50 web` → `bind() to 0.0.0.0:8080 failed (98: Address already in use)`.
5. `docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' vektor-web` → `no` (если политики нет — контейнер и не должен был подняться).

**Решение:**

```bash
docker rm -f old-nginx
docker compose up -d
docker compose ps
curl -s http://localhost:8080 | head -n 3
```

И обязательно — устранить причину: в `compose.yaml` у сервиса `web` должно быть `restart: unless-stopped` (в файле из лабораторной оно уже есть; если ты его убрал — верни). Проверить: `sudo systemctl restart docker && sleep 10 && docker compose ps`.

**Ответ пользователю:** «Иван Иванов, добрый день. Страница восстановлена, проверил — открывается. Причина: после перезагрузки сервера контейнер не поднялся автоматически, а порт был занят старым экземпляром. Настроил автозапуск сервиса, повторов быть не должно. Если ещё раз увидите ошибку — пришлите скриншот, я посмотрю логи.»

**Закрытие заявки:** категория «Внутренние сервисы / контейнеры», решение — «конфликт портов + отсутствие restart policy», в примечании — ссылка на коммит в репозитории конфигураций, куда ты положил `compose.yaml` (глава 74).

### Что изменилось на стенде

- Новая ВМ **CTR01** (Ubuntu Server 24.04), 10.10.10.35, статическая адресация, шлюз 10.10.10.2, DNS 10.10.10.10, имя `ctr01`.
- В зоне `corp.vektor.lab` на DC01 — A-запись `ctr01` → 10.10.10.35.
- На CTR01 установлены: Docker Engine (docker-ce, containerd, buildx, compose plugin), локальный пользователь `sysadm` в группе `docker`.
- `/etc/docker/daemon.json`: `insecure-registries` (10.10.10.35:5000) и ротация логов 10m×3.
- Compose-проект `vektor-web`: сервисы `web` (8080→80) и `registry` (5000), named volume `registry-data`, каталог `~/vektor-web/html` на хосте.
- В реестре `10.10.10.35:5000` лежит образ `vektor/nginx:1.0`.
- На UBU01 установлен Podman, работает rootless-контейнер `podman-web` (8081→80) и Quadlet-юнит `podman-web.service` у пользователя `sysadm`, включён linger.
- Использованные порты стенда: 8080, 8081, 5000 (занять другими сервисами нельзя).

### Откат

1. На CTR01: `docker compose down -v`, `podman` — на UBU01: `systemctl --user stop podman-web.service && podman rm -f podman-web`.
2. VMware Workstation: правый клик по UBU01 → **Snapshot Manager** → выбрать `before-sysadmin-078` → **Restore**.
3. CTR01 в снапшоте не нужна — если хочешь убрать её полностью: остановить ВМ и **Delete from disk**.
4. Убрать DNS-запись на DC01:

```powershell
$rec = Get-DnsServerResourceRecord -ZoneName "corp.vektor.lab" -Name "ctr01"
Remove-DnsServerResourceRecord -ZoneName "corp.vektor.lab" -InputObject $rec -Force
```

## 6. Ошибки новичков и безопасность

- **`--privileged`, чтобы «заработало»** → контейнер получает все capabilities и доступ к устройствам хоста; изоляция исчезает полностью. → Правильно: добавлять конкретно нужное (`--cap-add=NET_BIND_SERVICE`) или настроить сервис так, чтобы привилегии не требовались.
- **Монтирование `-v /var/run/docker.sock:...` «чтобы контейнер управлял Docker»** → любой процесс внутри получает root на хосте (мы это видели в шаге 11). → Правильно: не монтировать сокет; для сборки — buildx/kaniko, для запуска — CI-раннер на хосте.
- **Добавить всех в группу `docker`** → членство в группе `docker` эквивалентно root. → Правильно: rootless Docker/Podman для разработчиков, ограниченный доступ через `sudo` с правилами.
- **`:latest` и `docker compose pull` в проде** → внезапно приезжает новая версия, стек ложится, откатываться некуда. → Правильно: фиксированные теги (или digest `image@sha256:...`), обновление через ревью в Git (главы 74–76).
- **Данные внутри контейнера** → `docker compose down`/пересоздание контейнера = потеря данных. → Правильно: named volumes или bind-mount на отдельный диск, плюс бэкап тома.
- **`docker system prune -a --volumes` «для очистки»** → удаляет используемые образы и данные томов, включая реестр. → Правильно: сначала `docker system df`, точечно `docker image prune`, `docker builder prune`; `--volumes` — только осознанно.
- **Публикация Docker API на 0.0.0.0:2375** → любой в сети управляет твоим хостом без пароля. → Правильно: только Unix-сокет; если TCP — TLS-взаимная аутентификация на 2376 и файрвол.
- **Логи без ротации** → забитый диск и лежащий хост через месяц. → Правильно: `log-opts max-size/max-file` в `daemon.json` глобально.

**Безопасность (отдельно):**

- Все контейнеры делят **одно ядро**. Уязвимость ядра ( privilege escalation, container escape) пробивает всех сразу; поэтому обновление ядра хоста — часть контейнерной гигиены.
- **Порты, опубликованные Docker, обходят ufw.** Демон вставляет правила DNAT раньше цепочек ufw, поэтому «я закрыл 8080 файрволом» не работает. Ограничивай публикацию привязкой к адресу (`"10.10.10.35:8080:80"`) и правилами в цепочке `DOCKER-USER`.
- Rootless (Podman по умолчанию, rootless Docker) — базовый уровень защиты: компромисс контейнера даёт атакующему только права обычного пользователя.
- Образы — из доверенных источников: свой реестр с TLS и аутентификацией (htpasswd-базовый вариант для `registry:2`), подпись/сканер (Trivy), pinned digest. `insecure-registries` и HTTP-реестр — **только стенд**.
- Секреты не хранят в `compose.yaml` и не передают через `-e` в истории shell: используй файлы с правами 600, Docker secrets, Ansible Vault (глава 75) или внешний менеджер секретов.
- В РФ-контуре учитывай: Docker Engine (Apache-2.0) бесплатен, а **Docker Desktop** для крупных компаний требует платной подписки — на серверах он и не нужен. Если доступ к `download.docker.com`/Docker Hub нестабилен, легальные варианты: собственный Harbor/Nexus, российские облачные реестры контейнеров (Яндекс Облако, VK Cloud, Selectel — актуальность и условия [ПРОВЕРИТЬ]), Podman/containerd из репозиториев дистрибутива. На Astra Linux и РЕД ОС контейнерный стек ставят из штатных репозиториев [ПРОВЕРИТЬ состав пакетов для конкретной версии].

## 7. Вопросы на собеседовании

**1. Чем контейнер отличается от виртуальной машины и что это значит для безопасности?**

Ответ: ВМ виртуализирует железо и запускает собственное ядро — граница изоляции проходит по гипервизору. Контейнер — это процесс на хосте, изолированный ядром через namespaces и ограниченный cgroups, с файловой системой из слоёв overlay2. Контейнер стартует за секунды и не тратит RAM на второе ядро, поэтому на хосте их десятки. Обратная сторона: ядро общее, значит, его уязвимость или ошибка в capabilities/`--privileged`/монтировании сокета рушит изоляцию для всех контейнеров. Отсюда требования: rootless, drop capabilities, read-only FS, seccomp/AppArmor, обновление ядра, отсутствие доступа к docker.sock изнутри.

*Что хочет услышать:* чёткое «namespaces+cgroups vs гипервизор», упоминание shared kernel как источника риска и конкретные меры защиты.
*Красный флаг:* «контейнер — это лёгкая виртуалка» и отсутствие любого упоминания безопасности.

**2. Как вы обеспечите, чтобы сервис в контейнере поднимался после перезагрузки сервера?**

Ответ: На Docker — `restart: unless-stopped` (или `always`) в compose-файле плюс `systemctl enable docker`; проверка — `docker inspect --format '{{.HostConfig.RestartPolicy.Name}}'`. Для systemd-нативных сценариев — Quadlet-юнит у Podman или unit с `ExecStart=/usr/bin/docker compose up -d` и `Type=oneshot`/`RemainAfterExit=yes`. Обязательно healthcheck, чтобы «контейнер Up» не означал «сервис отвечает». И проверять на реальной перезагрузке, а не «на глаз».

*Что хочет услышать:* restart policy + автозапуск демона + healthcheck + фактическая проверка после reboot.
*Красный флаг:* «Docker сам поднимает» без понимания политики перезапуска.

**3. Почему доступ к `/var/run/docker.sock` — это root, и что делать, если приложению нужно управлять контейнерами?**

Ответ: Docker API через сокет позволяет создавать контейнеры с произвольными параметрами, включая монтирование `/` хоста и `--privileged`, то есть даёт полный контроль над машиной. Значит, любой контейнер с этим сокетом — потенциальный root. Варианты: не давать приложению управлять Docker вообще; использовать специализированный прокси с ограничением эндпоинтов; вынести сборку в отдельный агент; перейти на rootless; для CI — эфемерные раннеры в отдельной ВМ.

*Что хочет услышать:* понимание, что сокет = полный контроль, и практичные альтернативы.
*Красный флаг:* «ну просто примонтирую сокет, так удобнее».

**4. Чем Podman отличается от Docker и когда вы выберете Podman?**

Ответ: Тот же OCI-стандарт образов и рантайм (runc/crun), но Podman — daemonless: контейнер запускается как дочерний процесс CLI, нет постоянно работающего root-демона, rootless — режим по умолчанию, нативная интеграция с systemd (Quadlet/`generate systemd`), есть поддержка pods (группа контейнеров с общим сетевым namespace, как в Kubernetes). Docker удобнее в экосистеме разработки (compose, buildx, Docker Desktop), Podman — на серверах, в системах с требованием минимизировать root-сервисы и в RHEL-подобных дистрибутивах. Ограничение Podman: некоторые сценарии `docker compose` требуют `podman compose`/`podman-docker`, поведение отличается [ПРОВЕРИТЬ актуальное состояние].

*Что хочет услышать:* daemonless, rootless, systemd, OCI-совместимость, обоснование выбора под задачу.
*Красный флаг:* «Podman — это форк Docker» или «между ними нет разницы».

**5. Как организовать внутренний реестр образов и как обеспечить доверие к образам?**

Ответ: Разворачиваем Harbor (или `registry:2` как минимум) в закрытом сегменте, только TLS, аутентификация, RBAC по проектам, регулярное зеркалирование нужных внешних образов (pull-through cache), сканирование Trivy в CI, фиксация образов по digest, политика «в прод — только из своего реестра», ротация учётных данных, бэкап хранилища реестра. Плюс журналирование и контроль, кто и что пушит.

*Что хочет услышать:* TLS + auth + RBAC + сканирование + digest + запрет прямого pull из интернета в проде.
*Красный флаг:* «подниму registry без пароля на 5000 и всё».

## 8. Источники (официальная документация)

- **docs.docker.com** — разделы: «Install Docker Engine on Ubuntu», «Post-installation steps for Linux», «Compose file reference», «Docker daemon configuration (`daemon.json`)», «Configure logging drivers». Поиск: `docker engine install ubuntu apt repository`, `docker daemon.json insecure-registries`.
- **docs.docker.com** — «Registry» / «Deploy a registry server». Поиск: `docker registry 2 configuration htpasswd tls`.
- **docs.podman.io** — разделы про rootless, `podman run`, Quadlet (`podman-systemd.unit`). Поиск: `podman quadlet container unit file`, `podman rootless`.
- **opencontainers.org** — спецификации image-spec, runtime-spec, distribution-spec. Поиск: `OCI image spec layers manifest`.
- **trivy.dev** — документация сканера (раздел Image scanning). Поиск: `trivy image scan usage`.
- **kubernetes.io** — раздел «Concept: Containers» (для перехода к главе 79). Поиск: `kubernetes containers overview`.
- Всё, что касается поведения пакетов в Ubuntu 24.04 (`docker-compose-v2`), состава репозиториев Astra Linux/РЕД ОС и актуальности российских облачных реестров, — уточняй в официальной документации вендора и помечай как `[ПРОВЕРИТЬ]` в своих заметках.
