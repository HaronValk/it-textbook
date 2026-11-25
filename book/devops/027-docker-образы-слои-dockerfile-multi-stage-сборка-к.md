# Docker: образы, слои, Dockerfile, multi-stage сборка, кэширование и оптимизация размера

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4127.** Пользователь: Алексей Сидоров (склад). Где: рабочее место в офисе, сервис — контейнер на UBU01. Текст: «Форма приёма заявок на склад второй день открывается через раз, после обеда вообще “сайт недоступен”. Разработчик сказал: “пересобери образ, но учти — сборка идёт 12 минут, а образ почти гигабайт, у меня на ноуте места нет”». Срочность: высокая, склад не принимает поставки.

Дальше вы открываете `Dockerfile` и видите: `FROM golang:1.24`, `COPY . .`, `RUN go build`. Один слой с исходниками, поверх него один гигантский слой с компилятором и GOPATH — и так в каждом образе, который вы пушите в registry. Правка одной строки в коде пересобирает всё заново, потому что кэш слоёв ломается на `COPY . .`.

Это самая частая работа DevOps-инженера с Docker: не «запустить контейнер», а сделать образ **маленьким, быстрым на сборку, воспроизводимым и кэшируемым**. После этой главы вы будете: понимать, как Dockerfile превращается в слои; читать `docker history` и видеть, откуда взялись 900 МБ; писать multi-stage сборку, где в финальный образ попадает один бинарник; настраивать порядок инструкций так, чтобы ежедневные сборки шли секунды, а не минуты; и осознанно выбирать базовый образ под задачу.

## 2. Теория «под капотом»

**Образ — это не «файл образа».** По спецификации OCI (Open Container Initiative) образ состоит из трёх частей:

- **манифест** (`manifest.json`) — JSON, в котором перечислены digest конфига и digest всех слоёв;
- **конфиг** (`config.json`) — метаданные: `Env`, `Cmd`, `Entrypoint`, `User`, `ExposedPorts`, `Labels`, история;
- **слои** — набор tar-архивов (обычно gzip/zstd), каждый со своим digest `sha256:...`.

Digest образа — это хеш манифеста. Поэтому `nginx:1.27` и `nginx:1.27` через месяц могут быть разными образами (тег переехал), а `nginx@sha256:...` — всегда один и тот же. Тег — это указатель, digest — это идентичность.

**Слой = diff файловой системы.** Каждая инструкция `RUN`, `COPY`, `ADD` в Dockerfile создаёт новый слой: «что изменилось относительно предыдущего состояния». Инструкции `FROM`, `ENV`, `LABEL`, `WORKDIR`, `EXPOSE`, `USER`, `CMD`, `ENTRYPOINT`, `ARG` файловых слоёв не создают — они попадают в конфиг и в историю с размером 0 B (в `docker history` вы их увидите, но они «бесплатные»).

**Слои неизменяемы, а работают они через overlayfs.** При запуске контейнера слои образа становятся `lowerdir` (только для чтения), а сверху монтируется тонкий записываемый слой `upperdir`. Если процесс меняет файл из нижнего слоя, overlayfs не переписывает нижний слой — он копирует файл наверх (copy-on-write). Подробности про overlayfs, containerd и runc были в уроке 26, здесь нам важно следствие: **удаление файла в следующем слое не уменьшает образ** — файл остаётся в нижнем слое, а сверху появляется «белая метка» о его удалении.

**Кэш сборки.** BuildKit (движок сборки по умолчанию в Docker Engine 23+) вычисляет для каждой инструкции ключ кэша: digest родительского слоя + текст инструкции; для `COPY`/`ADD` — ещё и хеш содержимого копируемых файлов. Совпал ключ — слой берётся из кэша. Не совпал — слой пересобирается, и **все последующие инструкции тоже** (каскадная инвалидация). Это главная причина «медленных сборок».

**Multi-stage.** В одном Dockerfile может быть несколько `FROM`. Каждый `FROM` — отдельная стадия. Финальный образ собирается из последней стадии; всё, что было в промежуточных, в него не попадает — кроме того, что вы явно скопировали через `COPY --from=<стадия>`. Компилятор, исходники и кэш пакетов остаются «на стройплощадке».

```text
        Dockerfile                         Образ (то, что уезжает в registry)
        ----------                         ---------------------------------
FROM golang:1.24-alpine AS builder   ->   (в финальный образ НЕ попадает)
  RUN go build                       
                                      ->  config.json: Env, Entrypoint,
FROM scratch                              ExposedPorts, User
  COPY --from=builder /out/app /app   ->  layer 1: /app  (10 МБ)
  ENV APP_VERSION=0.2.0               ->  метаданные, 0 байт
  EXPOSE 8080                         ->  метаданные, 0 байт
  ENTRYPOINT ["/app"]                 ->  метаданные, 0 байт

manifest.json  ->  config.json (sha256:c...)  +  [layer1 (sha256:a...)]
digest образа  =   sha256(manifest.json)
```

Путь образа по инфраструктуре:

```text
Dockerfile --docker build--> локальный store (containerd) --docker push--> registry:443
                                                                  (HARBOR01)
                                                                       |
                                                              docker pull / kubelet
                                                                       v
                                                    overlayfs: lowerdir = слои образа
```

Порты и протоколы: Docker API — локальный unix-сокет `/var/run/docker.sock`; общение с registry — HTTPS/443 (Registry HTTP API V2) плюс авторизация по токену (Bearer). Сборка по умолчанию идёт BuildKit-ом внутри демона, контекст сборки (`docker build ... .` — та самая точка в конце) отправляется демону целиком, поэтому папка `.git` на 200 МБ в контексте — это реальные минуты ожидания.

## 3. Аналогия из жизни

Представьте стройку. Каждый этап — фундамент, стены, крыша, отделка — фотографируется и сдаётся как отдельный акт. **Акты неизменяемы:** если в фундаменте нашли ошибку, вы не перерисовываете старый акт, а делаете новый этап «исправление фундамента» — и он ложится сверху. Отсюда правило «удалять мусор в том же `RUN`, где его создали»: выбросить мусор отдельным актом нельзя, он уже зафиксирован в предыдущем.

Кэш — это «если проект не менялся до третьего этапа, бригада начинает с четвёртого». Поменяли входную дверь (файл в `COPY`) — переделывать придётся всё, что делалось после неё. Поэтому двери ставят в конце, а тяжёлые работы (заливка фундамента = `apk add`, `pip install`, `go mod download`) — в начале, до копирования кода.

Multi-stage — это стройплощадка: леса, опалубка, бетономешалка, бытовки остаются на площадке, заказчику передают **только готовый дом**. Финальный образ со `scratch` — это дом без стройплощадки: чисто, компактно, но и переночевать строителям негде (отладить внутри нечем — нет `sh`).

Где аналогия ломается: у этапов стройки нет «хеша содержимого». А у слоёв есть — именно поэтому два разных образа, у которых совпал базовый слой, физически хранят один и тот же слой на диске и не скачивают его дважды.

## 4. Практика и команды

### 4.1. Что вообще за движок и чем он собирает

```bash
docker version
docker info --format '{{.ServerVersion}} | storage={{.Driver}} | cgroup={{.CgroupDriver}}'
```

`docker version` показывает клиент и сервер отдельно (бывает, что клиент новый, а демон старый — отсюда «неизвестные флаги»). `docker info` — драйвер хранения (ожидаем `overlay2`), версия сервера, корневой каталог.

```text
Client: Docker Engine 26.x
Server: Docker Engine 26.x
...
overlay2
```

Смотреть: `Storage Driver` = `overlay2` (на нормальном ядре Ubuntu 24.04 так и будет), версия сервера ≥ 23 — значит BuildKit по умолчанию включён. Точные номера версий в вашем `apt`-пакете `docker.io` проверяйте глазами, они меняются.

### 4.2. Список образов и их размер

```bash
docker image ls
docker image ls --filter dangling=true
docker image ls --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}'
```

`dangling=true` — «висячие» образы без тегов (остатки пересборок, `<none>`). Их можно удалять, они ни на что не ссылаются.

```text
REPOSITORY    TAG       IMAGE ID       CREATED         SIZE
vektor-app    good      a1b2c3d4e5f6   2 minutes ago   9.8MB
vektor-app    bad       0f1e2d3c4b5a   6 minutes ago   987MB
golang        1.24      ...            2 weeks ago     812MB
```

Смотреть: разница в порядке величин между `bad` и `good` — это и есть предмет главы.

### 4.3. Что внутри образа: inspect

```bash
docker image inspect vektor-app:good | jq '.[0] | {Architecture, Os, Layers: (.RootFS.Layers|length), Env: .Config.Env, Cmd: .Config.Cmd, Entrypoint: .Config.Entrypoint}'
docker image inspect vektor-app:good --format '{{.Architecture}}/{{.Os}} layers={{len .RootFS.Layers}}'
```

`jq` (разбирали в уроке 23) режет огромный JSON до нужного. Смотреть: сколько слоёв (`layers`), какая архитектура (`amd64` против `arm64` — классическая причина «exec format error» при переносе с ноутбука на сервер), какие `Env` и `Entrypoint` реально прописаны.

### 4.4. История слоёв — главный диагностический инструмент

```bash
docker history vektor-app:bad
docker history vektor-app:bad --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' | head -n 12
```

`--no-trunc` показывает полный текст команды (без него — обрезанный с `...`), `--format` — только размер и команду.

```text
812MB  /bin/sh -c #(nop) ADD file:... in /        <- базовый golang:1.24
170MB  /bin/sh -c go build -o /app .
1.2kB  /bin/sh -c #(nop) COPY dir:... in /src
0B     /bin/sh -c #(nop) ENV APP_VERSION=0.1.0
```

Смотреть: первая строка — вес базового образа; ищите слои, где что-то «осталось» (кэш пакетов, исходники, компилятор). Всё, что весит сотни мегабайт и не нужно в рантайме, — кандидат на вынос в builder.

### 4.5. Сборка: базовые флаги

```bash
docker build -f Dockerfile.good -t vektor-app:good .
docker build -t vektor-app:good --build-arg APP_VERSION=0.3.0 .
docker build -t vektor-app:builder --target builder .
docker build --no-cache -t vektor-app:good .
docker build --progress=plain -t vektor-app:good . 2>&1 | tail -n 20
```

- `-f` — какой Dockerfile брать; `-t` — имя:тег;
- `.` в конце — **контекст сборки** (что демон видит вообще);
- `--build-arg` — передать значение `ARG` (не `ENV`!);
- `--target` — остановиться на указанной стадии (удобно, чтобы зайти внутрь builder и посмотреть, что там);
- `--no-cache` — игнорировать кэш (нужно, когда «кэш врёт», например после смены внешнего репозитория);
- `--progress=plain` — человекочитаемый лог BuildKit без «полосок», видно `CACHED` и `transferring context`.

```text
#8 [builder 3/5] COPY go.mod ./
#8 CACHED
#9 [builder 4/5] RUN go build -o /out/app .
#9 0.9 DONE 1.4s
#12 transferring context: 2.41kB 0.0s done
```

Смотреть: `CACHED` — слой взят из кэша; `transferring context` — сколько демон получил файлов.

### 4.6. `.dockerignore` — самый дешёвый способ ускорить сборку

```text
# .dockerignore
.git
.gitignore
*.md
Dockerfile*
docker-compose*.yml
secrets.env
**/__pycache__
**/node_modules
```

Файл лежит рядом с Dockerfile и вырезает мусор **из контекста сборки**. Без него `.git` и `node_modules` едут в демон и, если в Dockerfile написано `COPY . .`, ещё и попадают в образ. Проверка — по строке `transferring context` в `--progress=plain`.

### 4.7. Кэш-монтирования: `RUN --mount=type=cache`

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.24-alpine AS builder
WORKDIR /src
COPY go.mod ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go mod download
```

`--mount=type=cache` даёт папке **постоянный** кэш между сборками, но **не** кладёт её содержимое в слой образа. Это лучше, чем «положить кэш и потом удалить»: удаление всё равно оставит мегабайты в нижнем слое.

```bash
docker builder prune --filter until=24h --force
docker system df -v
```

Первое чистит кэш сборки старше суток (не образы!), второе показывает, сколько места занимают образы, контейнеры, тома и кэш сборки по отдельности.

### 4.8. Отладка содержимого образа

```bash
docker run --rm -it --entrypoint /bin/sh vektor-app:bad
docker create --name dbg vektor-app:good
docker cp dbg:/app ./app.debug
docker rm dbg
file ./app.debug
```

`--entrypoint /bin/sh` перебивает `ENTRYPOINT` образа и даёт шелл (если он есть). В `scratch` шелла нет — поэтому содержимое достают через `docker create` + `docker cp` (работает и на не запускавшемся контейнере) и смотрят `file`/`ldd`. Точный вывод `file` зависит от сборки бинарника — нам важно увидеть `statically linked` против `dynamically linked`.

### 4.9. Вес «по проводу»: save/load

```bash
docker save vektor-app:good | gzip -9 | wc -c
docker save vektor-app:bad  | gzip -9 | wc -c
docker load < vektor-app.good.tar.gz
```

`docker save` отдаёт tar со **всеми** слоями образа — так оценивают, сколько реально уедет в registry и сколько потом скачает нода. `docker load` — обратная операция; `docker export` (для контейнера) и `docker import` — другое, там теряется история слоёв, не путайте.

### 4.10. Multi-arch (задел на будущее)

```bash
docker buildx ls
docker buildx build --platform linux/amd64,linux/arm64 -t vektor-app:good .
```

`buildx` — плагин к BuildKit для мультиплатформенных сборок (нужен `qemu-user-static`/бинарь-эмуляция `[ПРОВЕРИТЬ]` в вашей версии). В пакете `docker.io` из репозитория Ubuntu плагин `docker-buildx-plugin` может отсутствовать — проверьте `docker buildx version`; полноценный сценарий с пушем в registry — в уроке 31.

### 4.11. Уборка (осторожно)

```bash
docker image prune -a --filter until=72h
docker system prune --volumes
```

Первое удаляет **все** образы, на которые не ссылается ни один контейнер, старше 72 часов. Второе — ещё и сети, остановленные контейнеры и тома. На учебном стенде нормально, на общем сервере — сначала `docker system df -v` и понимание, кто чем пользуется.

## 5. Лабораторная работа

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS) — доступ по SSH, пользователь с `sudo`.
- Docker Engine установлен. Если ещё нет (по уроку 26 вы могли только разбирать теорию) — минимальная установка:

```bash
sudo apt update
sudo apt install -y docker.io file curl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"   # затем перелогиниться
docker version
```

`docker.io` из репозитория Ubuntu — легальный свободный пакет; версия там может быть старше, чем на download.docker.com, и плагин `buildx` может отсутствовать. Если `docker pull` упирается в недоступность Docker Hub из РФ — это тема урока 32 (зеркала и локальный прокси-кэш), там же решение; на время лабы можно использовать уже имеющиеся на UBU01 базовые образы (`docker image ls`).
- Windows-часть стенда (DC01, FS01, WIN-клиенты) для этой лабы **не нужна** — и это нормально для DevOps-трека.
- Ведётся `stand-log.md`. Снапшот делается в VMware Workstation на UBU01.

### Снапшот

В VMware Workstation при выключенной или включённой ВМ UBU01: `VM → Snapshot → Take Snapshot`, имя — **`before-devops-027`**.

Если предпочитаете командную строку (пути подставьте свои):

```bash
vmrun -T ws snapshot "C:\VMs\UBU01\UBU01.vmx" before-devops-027
```

Формат команд `vmrun` в разных версиях Workstation может отличаться — сверяйтесь с `vmrun --help` `[ПРОВЕРИТЬ]`.

### Шаги

1. **UBU01.** Проверить движок: `docker info --format '{{.ServerVersion}} {{.Driver}}'`. Ожидаемо: версия сервера и `overlay2`.

2. **UBU01.** Создать проект:

```bash
mkdir -p ~/docker-lab/vektor-app && cd ~/docker-lab/vektor-app
git init
```

3. **UBU01.** Положить приложение — маленький HTTP-сервис на Go (стандартная библиотека, внешних зависимостей нет, значит сеть для модулей не нужна):

```go
// main.go
package main

import (
	"fmt"
	"net/http"
	"os"
)

func main() {
	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "Vektor warehouse form, version %s\n", os.Getenv("APP_VERSION"))
	})
	fmt.Println("listening on :8080")
	http.ListenAndServe(":8080", nil)
}
```

```text
// go.mod
module vektor/warehouse-form

go 1.24
```

Версию `go` в `go.mod` и тег образа `golang` берите согласованными и актуальными на hub.docker.com `[ПРОВЕРИТЬ]`.

4. **UBU01.** Создать `.dockerignore` (содержимое — из раздела 4.6) и «плохой» Dockerfile:

```dockerfile
# Dockerfile.bad
FROM golang:1.24
WORKDIR /src
COPY . .
RUN go build -o /app .
ENV APP_VERSION=0.1.0
EXPOSE 8080
CMD ["/app"]
```

5. **UBU01.** Собрать «плохой» образ и замерить:

```bash
time docker build -f Dockerfile.bad -t vektor-app:bad .
docker image ls | grep vektor-app
```

Ожидаемо: скачивание базового образа `golang:1.24` (сотни мегабайт, время зависит от канала) и итоговый размер **≈800 МБ–1 ГБ**.

6. **UBU01.** Запустить и проверить, что сервис живой:

```bash
docker run -d --name vektor-app-bad -p 8081:8080 vektor-app:bad
curl -s localhost:8081/ ; curl -s localhost:8081/healthz
```

Ожидаемо: `Vektor warehouse form, version 0.1.0` и `ok`.

7. **UBU01.** Посмотреть, из чего состоит образ:

```bash
docker history vektor-app:bad --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' | head -n 8
```

Ожидаемо: огромный первый слой (база golang), второй — результат `go build`, дальше мелочь и нули.

8. **UBU01.** Эксперимент с кэшем на «плохом» файле: поменять `0.1.0` на `0.1.1` (в `main.go` поменяйте текст ответа, например `version %s (v2)`) и пересобрать:

```bash
time docker build -f Dockerfile.bad -t vektor-app:bad .
```

Ожидаемо: `CACHED` почти нигде после `COPY . .` — пересобирается и копирование, и компиляция. Обратите внимание: сам `go build` для такой мелочи быстрый, но на реальном проекте именно этот слой и «съедает» минуты.

9. **UBU01.** Написать «хороший» Dockerfile:

```dockerfile
# syntax=docker/dockerfile:1
# Dockerfile.good

FROM golang:1.24-alpine AS builder
WORKDIR /src
COPY go.mod ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY *.go ./
ENV CGO_ENABLED=0
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -ldflags="-s -w" -o /out/app .

FROM scratch
ARG APP_VERSION=0.2.0
ENV APP_VERSION=$APP_VERSION
COPY --from=builder /out/app /app
EXPOSE 8080
USER 10001:10001
ENTRYPOINT ["/app"]
```

10. **UBU01.** Собрать «хороший» образ и сравнить:

```bash
time docker build -f Dockerfile.good -t vektor-app:good .
docker image ls | grep vektor-app
docker history vektor-app:good --format '{{.Size}}\t{{.CreatedBy}}'
```

Ожидаемо: размер **единицы–десятки мегабайт** (бинарник + метаданные), в истории 2–3 записи с ненулевым размером.

11. **UBU01.** Проверить работу и кэш после правки кода:

```bash
docker run -d --name vektor-app-good -p 8080:8080 vektor-app:good
curl -s localhost:8080/ ; curl -s localhost:8080/healthz
# правим текст в main.go, затем:
time docker build --progress=plain -f Dockerfile.good -t vektor-app:good .
```

Ожидаемо: `CACHED` на стадиях `builder` до `COPY *.go`, пересобирается только компиляция и финальный `COPY`.

12. **UBU01.** Сравнить вес «по проводу» и записать результат:

```bash
docker save vektor-app:good | gzip -9 | wc -c
docker save vektor-app:bad  | gzip -9 | wc -c
docker system df -v
```

Ожидаемо: разница в десятки–сотни раз. Внести в `stand-log.md`.

### Проверка

- [ ] `docker image ls` показывает `vektor-app:bad` (сотни МБ) и `vektor-app:good` (единицы–десятки МБ).
- [ ] `curl localhost:8080/healthz` возвращает `ok`, `curl localhost:8080/` — версию `0.2.0`.
- [ ] `docker history vektor-app:good` содержит не больше 3 записей с ненулевым размером.
- [ ] Повторная сборка `vektor-app:good` без правок кода идёт с `CACHED` по всем слоям.
- [ ] `docker image inspect vektor-app:good --format '{{.Architecture}}/{{.Os}} layers={{len .RootFS.Layers}}'` — `amd64/linux`, слоёв 1–2.
- [ ] В `stand-log.md` зафиксированы размеры обоих образов и время сборки до/после.
- [ ] Файлы проекта лежат в Git (`git status` → закоммичено).

### Сломай и почини

**Пользователь:** Алексей Сидоров, склад
**ВМ:** рабочее место в офисе; сервис — контейнер на UBU01
**Что написал:** «Форма приёма заявок вообще не открывается, браузер пишет “сайт недоступен”. Вчера работало.»
**Срочность:** высокая — склад не принимает поставки.

**Как воспроизвести поломку (на UBU01):** имитируем «неудачный рефакторинг». В `Dockerfile.good` меняем стадию builder с `golang:1.24-alpine` на `golang:1.24` и убираем строку `ENV CGO_ENABLED=0`:

```dockerfile
FROM golang:1.24 AS builder
# ... остальное как было, ENV CGO_ENABLED=0 удалён
FROM scratch
```

```bash
docker build -f Dockerfile.good -t vektor-app:good .
docker rm -f vektor-app-good
docker run -d --name vektor-app-good -p 8080:8080 vektor-app:good
```

Ожидаемое поведение: контейнер не стартует, CLI выдаёт ошибку вида `exec /app: no such file or directory` — хотя файл `/app` в образе есть. Точный текст зависит от версии runc `[ПРОВЕРИТЬ]`, но смысл один: не запускается исполняемый файл.

**Диагностика по шагам:**

1. `docker ps -a --filter name=vektor-app-good` — статус не `Up`, а `Created`/`Exited`. Значит проблема до приложения, в самом образе.
2. `docker logs vektor-app-good` — пусто. Приложение не успело ничего напечатать: бинарь не запустился. Это отсекает версии «ошибка в коде» и «не тот порт».
3. `docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.Config.Entrypoint}} {{.Config.Env}}' vektor-app-good` — проверяем, что `Entrypoint` вообще `[/app]` и что в `Env` есть `APP_VERSION`.
4. Достать бинарь наружу и посмотреть на него:

```bash
docker rm -f dbg 2>/dev/null
docker create --name dbg vektor-app:good
docker cp dbg:/app ./app.debug
docker rm dbg
file ./app.debug
ldd ./app.debug || true
```

Ожидаемо: `ELF 64-bit LSB executable, x86-64, dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2`. **Вот причина:** в `scratch` нет ни glibc, ни динамического линкера — запускать нечем.

5. Проверить гипотезу «битая сборка vs отсутствие libc»: собрать промежуточную стадию и зайти в неё — `docker build -f Dockerfile.good --target builder -t vektor-app:builder . && docker run --rm -it --entrypoint sh vektor-app:builder` — файл `/out/app` на месте и запускается (собран под Debian-окружение).

**Решение:** вернуть статическую сборку — вернуть `ENV CGO_ENABLED=0` в стадию builder (и/или вернуть `-alpine` базу):

```bash
docker build -f Dockerfile.good -t vektor-app:good .
docker rm -f vektor-app-good
docker run -d --name vektor-app-good -p 8080:8080 vektor-app:good
curl -s localhost:8080/healthz
```

Проверка: `file ./app.debug` после пересборки показывает `statically linked`. Если статическая сборка невозможна (CGO нужен библиотеке), финальную стадию делают `debian:12-slim` (≈75 МБ) вместо `scratch` — это честный компромисс, а не «поставить `glibc` в scratch наколкой».

**Что написать пользователю:** «Сервис восстановлен. Причина — ошибка в конфигурации сборки образа: в контейнер попадал бинарник, который не может запуститься в минимальном окружении. Исправили сборку, добавили шаг проверки запуска контейнера после деплоя. Форма доступна, повторите попытку».

**Закрытие заявки:** категория «инцидент, деплой», в решении — ссылка на коммит с фиксом, в `stand-log.md` — запись «причина: динамическая линковка бинарника при `FROM scratch`; профилактика: проверка `file`/smoke-тест в пайплайне».

### Что изменилось на стенде

- **UBU01**: установлен `docker.io` (если не был установлен ранее), пользователь в группе `docker`.
- **UBU01**: каталог `~/docker-lab/vektor-app` с `main.go`, `go.mod`, `.dockerignore`, `Dockerfile.bad`, `Dockerfile.good`, git-репозиторий.
- **UBU01**: образы `vektor-app:bad`, `vektor-app:good` (и, возможно, `vektor-app:builder`), кэш сборки BuildKit.
- **UBU01**: контейнер `vektor-app-good` на порту 8080 хоста (и, до уборки, `vektor-app-bad` на 8081).
- Порты 8080/8081 на UBU01 заняты — учитывать в следующих лабах (в частности, при работе с Compose в уроке 28).
- Запись в `stand-log.md` с размерами образов и временем сборки.

### Откат

В VMware Workstation: `VM → Snapshot → Snapshot Manager` → выбрать `before-devops-027` → `Go To` (ВМ будет выключена в состоянии снапшота). Командой:

```bash
vmrun -T ws revertToSnapshot "C:\VMs\UBU01\UBU01.vmx" before-devops-027
```

Если откатывать всю ВМ не хочется, достаточно убрать только артефакты лабы:

```bash
docker rm -f vektor-app-bad vektor-app-good dbg 2>/dev/null
docker rmi -f vektor-app:bad vektor-app:good vektor-app:builder 2>/dev/null
docker builder prune -f
```

Docker Engine при этом остаётся — он понадобится в уроке 28.

## 6. Ошибки новичков и безопасность

1. **`COPY . .` перед установкой зависимостей.** → Каждая правка любого файла (даже `README.md`) сбрасывает кэш и заново тянет зависимости: сборка 10 минут вместо 15 секунд. → Правильно: сначала `COPY go.mod`/`requirements.txt`/`package.json`, потом установка, и только потом `COPY` кода. И `.dockerignore`.

2. **Удаление мусора отдельным слоем.** → `RUN apt-get install ...`, затем `RUN rm -rf /var/lib/apt/lists/*` — образ не уменьшился ни на байт: файлы зафиксированы в нижнем слое. → Правильно: `RUN apt-get update && apt-get install -y --no-install-recommends ... && rm -rf /var/lib/apt/lists/*` одной инструкцией.

3. **`docker commit` как способ «собрать» образ.** → Получается невоспроизводимый образ без истории и без Dockerfile: через месяц никто не знает, что внутри. → Правильно: только Dockerfile (или Buildah/Kaniko, урок 29) и Git.

4. **`latest` вместо версии и digest.** → `FROM ubuntu` / `image: latest` в один день приносит другую версию с другими библиотеками, и «ничего не меняли, а сломалось». → Правильно: `ubuntu:24.04`, а для критичного — `@sha256:...`; обновление версии — отдельный коммит.

5. **Секреты в `ARG`/`ENV`.** → `ARG GITHUB_TOKEN` виден в `docker history --no-trunc` и в метаданных слоя; токен утекает в registry вместе с образом. → Правильно: `RUN --mount=type=secret` (BuildKit) для сборки, а в рантайме — переменные окружения/секреты оркестратора, не образ.

6. **Гигантский build context.** → `.git`, `node_modules`, iso-образы в контексте: сборка идёт минуты даже при полном кэше. → Правильно: `.dockerignore` + дисциплина «в контексте только то, что нужно сборке».

7. **`docker system prune --volumes` на общем хосте.** → Сносит тома с данными и образы, которые использовали другие. → Правильно: сначала `docker system df -v`, удаление по фильтрам (`until=`, по конкретному `IMAGE ID`), а в CI — отдельный «чистильщик» с согласованными правилами.

8. **Отладка через `--privileged` и `--net=host` «чтобы быстрее». → Контейнер получает права хоста, и вы теряете весь смысл изоляции. → Правильно: разбираться с конкретной проблемой (порты, capabilities, права на файлы); про `--cap-drop`, non-root и read-only FS — урок 30.

### Безопасность

- **Не встраивайте в образ ничего секретного**: ни ключи, ни `.env`, ни внутренние пароли. Слой — это tar, который лежит в registry и у каждого, кто имеет pull. Даже если вы «удалили» файл следующим слоем, он там остался.
- **`FROM scratch` — не «секьюрность»**: да, меньше поверхности атаки, но вы теряете возможность отладки, а не-root `USER` и правильные capabilities всё равно надо настраивать отдельно (урок 30).
- **Базовые образы — тоже код**: тянуть `FROM randomuser/app:latest` из непонятного источника = запустить чужой код с правами вашего процесса. Доверенные базовые образы + фиксированный digest + скан уязвимостей (Trivy/Grype — урок 30).
- **В лабе** мы запускаем контейнер с публикацией порта на 0.0.0.0 хоста (`-p 8080:8080`) — на стенде нормально, в бою такой порт должен открываться только через балансировщик/ingress, а не «наружу со всех интерфейсов».
- **Учебный пароль стенда** `Lab#2026-vektor` — только для стенда. В Dockerfile-примерах и в `.env` реальных проектов паролей быть не должно: только secret-механизмы.

## 7. Вопросы на собеседовании

**Вопрос 1. Почему при изменении одной строки кода образ иногда пересобирается 15 минут, а иногда 10 секунд? Как работает кэш слоёв?**

Ответ: кэш BuildKit ключуется по digest родительского слоя и тексту инструкции, а для `COPY`/`ADD` — ещё и по хешу содержимого файлов. Если изменился файл, попавший в `COPY`, то этот слой и все последующие пересобираются — инвалидация каскадная. Поэтому тяжёлые операции ставят до копирования кода: `COPY go.mod` → `go mod download` → `COPY *.go` → `go build`. Тогда правка кода пересобирает только компиляцию. В CI дополнительно используют `--cache-to/--cache-from type=registry`, чтобы кэш переживал разные раннеры.
*Что хочет услышать интервьюер:* понимание каскадной инвалидации и практику «сначала зависимости, потом код».
*Красный флаг:* «Docker кэширует всё автоматически» или «надо всегда собирать с `--no-cache`» (это лечит симптом, а не причину).

**Вопрос 2. Что такое multi-stage сборка и что попадает в финальный образ?**

Ответ: несколько стадий `FROM` в одном Dockerfile. Каждая стадия собирается отдельно; в итоговый образ попадает только последняя, плюс то, что явно скопировано `COPY --from=<стадия>`. Компиляторы, исходники, кэш пакетов и промежуточные артефакты остаются в стадиях builder и в образ не попадают. Побочный эффект: в минимальном финальном образе (`scratch`, distroless) нет шелла и стандартных утилит, поэтому отладка идёт через `docker create`/`docker cp` или через отдельную debug-стадию.
*Что хочет услышать интервьюер:* что вы понимаете границу «builder vs runtime» и умеете её использовать осознанно, включая минусы.
*Красный флаг:* «multi-stage уменьшает образ за счёт сжатия» — это про размер базового образа, а не про стадии.

**Вопрос 3. В чём разница `ARG` и `ENV` и почему нельзя передавать секреты через `--build-arg`?**

Ответ: `ARG` — переменная времени сборки, доступна в инструкциях Dockerfile, но не в рантайме контейнера; `ENV` — переменная, попадающая в конфиг образа и видимая процессу в контейнере. Значения `ARG` остаются в истории сборки (видны в `docker history --no-trunc` и в метаданных), поэтому токен, переданный `--build-arg`, утекает в registry вместе с образом. Для секретов на этапе сборки используют BuildKit secrets (`RUN --mount=type=secret,id=...`), для рантайма — secrets-механизмы платформы.
*Что хочет услышать интервьюер:* что вы отличаете «параметр сборки» от «конфигурации рантайма» и знаете, что история сборки — публична.
*Красный флаг:* «`ARG` не сохраняется, можно спокойно пихать пароль».

**Вопрос 4. Образ 1 ГБ, приложение — один бинарник. Как уменьшите его в 10–100 раз?**

Ответ: multi-stage сборка; статическая сборка (`CGO_ENABLED=0`) и `FROM scratch`/distroless; минимальная база (`alpine`, `-slim`) вместо полной ОС; удаление мусора в том же `RUN`; `--no-install-recommends` и очистка кэша пакетов; `.dockerignore`, чтобы в контекст и образ не попали `.git`, тесты и документация; флаги линкера `-s`/`-w` для Go (символы и debug-инфо); `--mount=type=cache` для кэшей сборки, а не «положить и удалить». Дальше — измерять: `docker image ls`, `docker history`, `docker save | gzip | wc -c`.
*Что хочет услышать интервьюер:* набор приёмов + привычку измерять и проверять, а не «сделал alpine и стало хорошо».
*Красный флаг:* «поставлю `latest` на alpine и всё» — без учёта libc и совместимости.

**Вопрос 5. Какой базовый образ выберете: `alpine`, `slim` или `distroless`? В чём подвох `alpine`?**

Ответ: зависит от рантайма. `alpine` — 5–10 МБ, но musl libc вместо glibc: бинарники с CGO/glibc-зависимостями и некоторые Python-колёса требуют пересборки, а resolver DNS и работа с локалями отличаются. `slim` (Debian) — компромисс: glibc, привычные пакеты, ~70–130 МБ. `distroless` — нет шелла и пакетного менеджера, минимальная поверхность атаки, но отладка сложнее и нужен статический бинарник. Для Go со `CGO_ENABLED=0` — `scratch`/`distroless`; для Python/Node — обычно `slim`; `alpine` — когда есть готовые musl-совместимые артефакты.
*Что хочет услышать интервьюер:* понимание компромисса «размер vs совместимость vs отлаживаемость», а не религию «alpine всегда лучше».
*Красный флаг:* «alpine — это просто маленький Ubuntu».

## 8. Источники (официальная документация)

- **docs.docker.com** — раздел *Reference → Dockerfile reference* (все инструкции, `ARG` vs `ENV`, `COPY --from`); поиск: `dockerfile reference docker docs`.
- **docs.docker.com** — раздел *Build → Building best practices* / *Best practices for writing Dockerfiles*; поиск: `best practices writing dockerfiles`.
- **docs.docker.com** — раздел *Build → Build cache* (как считается кэш, `--mount=type=cache`, `--cache-to/--cache-from`); поиск: `docker build cache buildkit`.
- **docs.docker.com** — *Reference → CLI → docker build*, *docker image*, *docker history*; поиск: `docker build cli reference`.
- **docs.docker.com** — *Reference → Dockerfile frontend / BuildKit `RUN --mount`*; поиск: `dockerfile run mount type cache syntax`.
- **opencontainers.org** — спецификация *OCI Image Format Specification* (манифест, конфиг, слои, digest); поиск: `oci image spec manifest config layers`.
- **hub.docker.com** — официальные образы `golang`, `alpine`, `debian`, `python`, `distroless` (теги, размеры, варианты `-slim`/`-alpine`); поиск: `golang official image docker hub`.
- **github.com** — репозитории `moby/buildkit` и `docker/buildx` (README, документация по кэшу и multi-platform); поиск: `buildkit github multi-platform build`.

Конкретные номера версий пакетов, тегов образов и точные тексты ошибок runc в 2026 году проверяйте на этих ресурсах — они меняются, и в тексте они помечены как ориентировочные.
