# Registry образов: Harbor, GitLab Container Registry, Nexus — установка, репликация, политики, подпись (Cosign)

## 1. Введение (зачем это нужно в реальной работе)

**Заявка #4187**
**Пользователь:** Петрова (HR), ВМ WIN10-CL02.
**Что написала:** «В HR-портале после обеда опять старая версия формы заявления на отпуск. Утром ИТ сказали, что обновили. Коллеги видят то новую, то старую — у кого как».
**Срочность:** средняя, но выглядит как «деплой не работает».

На самом деле деплой сработал трижды. Просто сборка перезаписала тег `1.4` в реестре образов, часть подов успела подтянуть старый слой, часть — новый, и никто не заметил, потому что тег — это просто стикер, который можно переклеить. Разбор этой заявки — ровно про тему главы.

Реестр образов (registry) — это то место, откуда любая Kubernetes-нода, любой docker-хост и любой CI-раннер забирают контейнеры. Пока реестр один и он «просто работает», о нём никто не думает. Как только появляется второй кластер, вторая команда, требование «образы не должны пропадать» и вопрос безопасника «а вы уверены, что в прод уехал именно тот образ, который собрали», — реестр становится инфраструктурным сервисом с политиками, репликацией, правами и подписями.

После этой главы ты должен уметь: поднять Harbor и объяснить, из каких компонентов он состоит; завести проект, робот-аккаунт, квоту; настроить ретеншн (retention) и иммутабельность тегов; сделать репликацию Harbor → GitLab Container Registry; подписать образ Cosign'ом и проверить подпись; понимать, когда Nexus удобнее, а когда — GitLab Registry «и так есть».

## 2. Теория «под капотом»

**Реестр = HTTP-сервис над хранилищем блобов.** Спецификация OCI Distribution (её реализует и Harbor, и GitLab Registry, и Nexus, и Docker Hub) описывает один и тот же API:

```text
GET  /v2/                                     → проверка «я живой» (обычно 401)
GET  /v2/<name>/tags/list                     → список тегов
GET  /v2/<name>/manifests/<tag|sha256:...>    → манифест (JSON: слои + config + digest)
HEAD /v2/<name>/manifests/<tag>               → только digest (дёшево, для проверок)
GET  /v2/<name>/blobs/sha256:<hash>           → сам слой (tar.gz)
POST /v2/<name>/blobs/uploads/                → загрузка слоя (chunked upload)
```

Ключевая идея: **слои адресуются по содержимому**. Digest `sha256:...` — это хеш манифеста, он меняется при любом изменении байтов. Тег — указатель на digest, и его можно переставить. Поэтому «деплой по тегу» — это деплой по изменяемой ссылке, а «деплой по digest» — по неизменяемой.

**Авторизация** в реестрах почти всегда одинаковая — Bearer token flow:

```text
клиент → GET /v2/ → 401 + WWW-Authenticate: Bearer realm=".../service/token",service="harbor-registry"
клиент → GET realm?service=...&scope=repository:vektor/print-agent:pull:push  (со своими кредами)
сервер → {"token": "..."}  (JWT с правами и сроком жизни)
клиент → GET /v2/... с Authorization: Bearer <token>
```

Поэтому `docker login` на самом деле ничего не «логинит»: он просто сохраняет креды в `~/.docker/config.json` (base64, без шифрования), а обмен на токен происходит при каждой операции. Токены короткоживущие, и рассинхрон часов между клиентом и реестром ломает вход — это частая причина «внезапно unauthorized».

Всё это — прикладной уровень (L7 модели OSI), обычно поверх TLS на 443. Docker по умолчанию требует HTTPS и отказывается работать с HTTP, если реестр не внесён в `insecure-registries` в `/etc/docker/daemon.json`.

**Harbor** — это не один процесс, а набор контейнеров под nginx:

```text
       UBU01 10.10.10.30                        HARBOR01 10.10.10.55
  ┌────────────────────┐                 ┌───────────────────────────────────┐
  │ docker/buildah     │  HTTPS :443     │ nginx  (portal UI + прокси /v2/)  │
  │ сборка → push ─────┼────────────────▶│  ├─ core        API /api/v2.0     │
  └────────────────────┘                 │  ├─ registry    OCI Distribution  │
                                         │  ├─ jobservice  репликация,       │
  ┌────────────────────┐                 │  │              retention, GC     │
  │ skopeo / crane     │  HTTPS :443     │  ├─ trivy       сканер CVE        │
  │ реестр → реестр ───┼────────────────▶│  ├─ postgresql  метаданные        │
  └────────────────────┘                 │  └─ redis       очередь/кэш       │
                                         │            │                      │
                                         │            ▼                      │
                                         │   /data/registry (blobs на диске) │
                                         └──────────────┬────────────────────┘
                                                        │ replication policy
                                                        │ (push, jobservice)
                                                        ▼
                                         GIT01 10.10.10.50
                                         GitLab Container Registry (:5050)
```

- **core** — REST API и UI-логика: проекты, пользователи, политики.
- **registry** — тот самый distribution, который обслуживает `/v2/`.
- **jobservice** — фоновые задачи: репликация, сборка мусора (GC), сканирование, ретеншн.
- **trivy** — сканер уязвимостей (в Harbor включён по умолчанию).
- **postgresql/redis** — метаданные и очереди. Это важная деталь: **артефакты лежат на диске, а их описания — в БД**. Бэкапить нужно и то, и другое.

Политики Harbor, которые встречаются в жизни постоянно:

| Политика | Что делает | Типичный вопрос на работе |
|---|---|---|
| Tag immutability | запрещает перезапись тегов по маске (`1.*`, `**`) | «почему push отбило 412?» |
| Retention | удаляет старые артефакты (по числу/возрасту/дате pull) | «диск кончился» |
| Quota | лимит хранилища на проект | «один проект съел весь том» |
| Robot account | сервисная учётка для CI с правами pull/push | «где взять пароль для пайплайна» |
| Proxy cache | pull-through кэш к Docker Hub | (урок 32) |
| Replication | копирование в другой реестр по расписанию/событию | «нужен образ во втором кластере» |

**GitLab Container Registry** — тот же distribution, встроенный в Omnibus-пакет GitLab. Авторизация идёт через сам GitLab (логин/пароль, personal access token, deploy token, CI job token), права привязаны к проекту GitLab: путь образа = `registry/namespace/project`. Плюс — ничего не надо администрировать отдельно, минус — нет своего UI с политиками, ретеншн задаётся cleanup policy, а не «правилами Harbor».

**Nexus Repository OSS** — универсальный репозиторий: docker, maven, npm, apt, raw, helm. Для Docker нужен отдельный коннектор (порт) на каждый hosted/proxy/group-репозиторий и включённый realm `Docker Bearer Token Realm`. Репликации «из коробки» в OSS нет — обычно строят через proxy-репозитории и задачи [ПРОВЕРИТЬ наличие репликации в Pro]. Nexus выбирают, когда нужен один склад для всего: и образы, и maven-артефакты, и deb-пакеты.

**Cosign** (проект Sigstore) — подпись образов. Подпись хранится в том же реестре как OCI-артефакт: образ `vektor/print-agent@sha256:abc...` получает рядом тег `sha256-abc....sig` с манифестом подписи. Два режима: key-based (свой ключ `cosign.key`/`cosign.pub`) и keyless (OIDC-идентичность + Fulcio-сертификат + запись в Rekor). Подпись сама по себе ничего не запрещает — она становится контролем только когда её проверяют: в admission-контроллере (Kyverno `verifyImages`, Connaisseur, OPA Gatekeeper) или в CI перед деплоем.

## 3. Аналогия из жизни

Реестр — это библиотека чертежей на заводе. Каждый чертёж (bлоб) хранится в единственном экземпляре, потому что у него есть инвентарный номер, вычисленный из самого чертежа (digest): изменил хоть линию — номер стал другим. На полке лежит картонка с надписью «версия 1.4» (тег) — это просто ярлык, и любой сотрудник может переложить его на другой чертёж. Репликация — это обмен фондами с филиалом библиотеки по расписанию. Ретеншн — списание устаревших чертежей, чтобы стеллажи не лопнули. Иммутабельность тегов — приказ: «ярлыки из серии 1.* не перекладывать». Cosign — нотариальная печать на обороте: она ничего не запрещает, но приёмщик на входе в цех может посмотреть на неё и не пустить чертёж в производство.

Где аналогия ломается: печать можно подделать визуально, а подпись — нет, она криптографическая; и библиотека выдаёт чертёж только по читательскому билету, то есть без токена ты не получишь даже список тегов.

## 4. Практика и команды

**1. Проверить, что реестр отвечает и как он просит авторизоваться**

```bash
curl -sS -i https://10.10.10.55/v2/ | head -20
```

```text
HTTP/1.1 401 Unauthorized
Www-Authenticate: Bearer realm="https://10.10.10.55/service/token",service="harbor-registry"
```

401 здесь — правильный ответ, а не ошибка. Из заголовка ты узнаёшь адрес token-сервиса. Если приходит `404` — скорее всего ты попал в UI, а не в реестр, или не тот порт/схема.

**2. Логин сервисной учёткой**

```bash
docker login 10.10.10.55 -u 'robot$vektor+ci-push' -p "$HARBOR_ROBOT_SECRET"
```

`-p` лучше не оставлять в истории — использовать `--password-stdin`. Креды оседают в `~/.docker/config.json` в base64. Имя робота в Harbor всегда вида `robot$<проект>+<имя>`.

**3. Разметить и запушить образ**

```bash
docker tag vektor/print-agent:1.0.0 10.10.10.55/vektor/print-agent:1.0.0
docker push 10.10.10.55/vektor/print-agent:1.0.0
```

```text
The push refers to repository [10.10.10.55/vektor/print-agent]
5f70bf18a086: Layer already exists
a1b2c3d4e5f6: Pushed
1.0.0: digest: sha256:9c1f... size: 1573
```

`Layer already exists` — это дедупликация по digest: слой уже есть в хранилище, повторно не заливается.

**4. Узнать неизменяемую ссылку**

```bash
docker inspect --format '{{index .RepoDigests 0}}' 10.10.10.55/vektor/print-agent:1.0.0
crane digest --insecure 10.10.10.55/vektor/print-agent:1.0.0   # [ПРОВЕРИТЬ] расположение флага --insecure в вашей версии crane
```

Именно digest, а не тег, идёт в Kubernetes-манифест, если нужна воспроизводимость.

**5. Получить токен вручную и посмотреть теги через API**

```bash
TOKEN=$(curl -s -u 'robot$vektor+ci-push:'"$HARBOR_ROBOT_SECRET" \
  "https://10.10.10.55/service/token?service=harbor-registry&scope=repository:vektor/print-agent:pull" \
  | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://10.10.10.55/v2/vektor/print-agent/tags/list | jq
```

```json
{"name":"vektor/print-agent","tags":["1.0.0","latest","sha256-9c1f....sig"]}
```

Так диагностируют «а образ вообще доехал?» без Docker.

**6. Управлять Harbor через API v2.0**

```bash
# создать приватный проект
curl -sk -u admin:"$HARBOR_ADMIN_PASSWORD" -X POST https://10.10.10.55/api/v2.0/projects \
  -H 'Content-Type: application/json' \
  -d '{"project_name":"vektor","public":false}'

# создать робот-аккаунт с pull+push на проект (в ответе будет secret)
curl -sk -u admin:"$HARBOR_ADMIN_PASSWORD" -X POST https://10.10.10.55/api/v2.0/projects/vektor/robots \
  -H 'Content-Type: application/json' \
  -d '{"name":"ci-push","duration":90,"level":"project","permissions":[{"kind":"repository","namespace":"vektor","access":[{"resource":"repository","action":"pull"},{"resource":"repository","action":"push"}]}]}'

# список артефактов проекта
curl -sk -u admin:"$HARBOR_ADMIN_PASSWORD" \
  "https://10.10.10.55/api/v2.0/projects/vektor/repositories?page=1&page_size=20" | jq
```

`duration` — срок жизни робота в днях, `level: project` ограничивает его одним проектом. Точные имена полей сверяй со Swagger своего Harbor: `https://10.10.10.55/devcenter-api-2.0` [ПРОВЕРИТЬ путь к Swagger в вашей версии].

**7. Копировать образы между реестрами без Docker**

```bash
skopeo inspect --tls-verify=false \
  --creds "robot\$vektor+ci-push:$HARBOR_ROBOT_SECRET" \
  docker://10.10.10.55/vektor/print-agent:1.0.0 | jq '.Digest, .Labels'

skopeo copy --all --src-tls-verify=false --dest-tls-verify=false \
  docker://10.10.10.55/vektor/print-agent:1.0.0 \
  docker://10.10.10.50:5050/vektor/print-agent:1.0.0
```

`--all` переносит все платформы мультиарх-манифеста. `skopeo copy` — ручной аналог того, что Harbor делает политикой репликации.

**8. Установка и обслуживание Harbor на HARBOR01**

```bash
cd /opt/harbor
cp harbor.yml.tmpl harbor.yml && vi harbor.yml
# hostname: 10.10.10.55
# http: port: 80        (https в лабе закомментирован — только для стенда!)
# harbor_admin_password: <сменить сразу после установки>
# data_volume: /data
./install.sh --with-trivy
docker compose ps
docker compose logs -f --tail=50 core
```

`install.sh` генерирует `docker-compose.yml` из шаблона и поднимает стек. `docker compose ps` должен показать все сервисы в состоянии `running`/`healthy`.

**9. GitLab Container Registry: включение**

```ruby
# /etc/gitlab/gitlab.rb на GIT01
registry_external_url 'http://10.10.10.50:5050'   # [ПРОВЕРИТЬ] порт по умолчанию зависит от external_url
registry['enable'] = true
registry['storage'] = {
  'filesystem' => { 'rootdirectory' => '/var/opt/gitlab/gitlab-rails/shared/registry' }
}
```

```bash
gitlab-ctl reconfigure
gitlab-ctl status registry
docker login 10.10.10.50:5050 -u "$GITLAB_USER" -p "$GITLAB_TOKEN"
```

Без TLS Docker откажется пушить, пока адрес не внесён в `insecure-registries` на клиенте (в лабе — можно, в бою — нет).

**10. Nexus OSS как docker-реестр (сравнение)**

```bash
docker run -d --name nexus -p 8081:8081 -p 8082:8082 \
  -v nexus-data:/nexus-data sonatype/nexus3
```

UI: `http://10.10.10.30:8081` (пароль admin — в `/nexus-data/admin.password`). Дальше: **Security → Realms → Docker Bearer Token Realm → Active**, **Repositories → Create → docker (hosted)**, имя `docker-hosted`, HTTP-порт `8082`.

```bash
docker login 10.10.10.30:8082 -u admin
docker tag vektor/print-agent:1.0.0 10.10.10.30:8082/print-agent:1.0.0
docker push 10.10.10.30:8082/print-agent:1.0.0
```

Схема пути отличается от Harbor: репозиторий определяется портом-коннектором, а не первым сегментом имени.

**11. Подпись и проверка Cosign**

```bash
export COSIGN_PASSWORD='Lab#2026-vektor'
cosign generate-key-pair

DIGEST=$(crane digest --insecure 10.10.10.55/vektor/print-agent:1.0.0)
cosign sign --key cosign.key --tlog-upload=false --allow-insecure-registry \
  "10.10.10.55/vektor/print-agent@${DIGEST}"

cosign verify --key cosign.pub --allow-insecure-registry \
  "10.10.10.55/vektor/print-agent@${DIGEST}"
cosign tree --allow-insecure-registry "10.10.10.55/vektor/print-agent@${DIGEST}"
cosign triangulate 10.10.10.55/vektor/print-agent:1.0.0
```

`--tlog-upload=false` отключает запись в публичный Rekor (на стенде без интернета он не нужен) [ПРОВЕРИТЬ имя флага в вашей версии cosign]. `cosign tree` покажет подписи и SBOM, `triangulate` — имя тега, под которым лежит подпись.

**12. Проверить, что политика реально работает**

```bash
# ожидаем отказ, если на проект навешена иммутабельность тегов
docker push 10.10.10.55/vektor/print-agent:1.0.0
```

```text
denied: requested access to the resource is denied
```

или 

```text
412 Precondition Failed
```

Разные коды — разные причины: `denied`/`unauthorized` — права и токены, `412`/`DENIED` от Harbor — политика иммутабельности. Это первое, что нужно различать в диагностике.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **HARBOR01** (10.10.10.55, Ubuntu Server 24.04) — Docker Engine + Compose plugin и Harbor 2.x. Если Harbor ещё не стоит: скачай offline-installer с github.com/goharbor/harbor/releases, распакуй в `/opt/harbor`, настрой `harbor.yml` (hostname `10.10.10.55`, http), `./install.sh --with-trivy`.
- **UBU01** (10.10.10.30) — Docker (или Podman), а также `skopeo`, `crane`, `cosign`, `jq`. Docker настроен на `insecure-registries` для `10.10.10.55` и `10.10.10.50:5050`.
- **GIT01** (10.10.10.50) — GitLab CE с включённым Container Registry (см. п. 9 практики), есть проект `vektor/print-agent`.
- Образ `vektor/print-agent:1.0.0` собран на UBU01 в уроке 27. Если его нет — собери любой минимальный (например, из `alpine` с `CMD ["sleep","infinity"]`) и назови так же.
- Windows-часть стенда не нужна.

### Снапшот

На **HARBOR01**, **UBU01**, **GIT01**: VMware Workstation → вкладка виртуалки → **VM → Snapshot → Take Snapshot**, имя `before-devops-031`. Останавливать ВМ не требуется (снапшот с памятью — по желанию; без памяти он меньше и быстрее).

### Шаги

1. **HARBOR01.** `cd /opt/harbor && docker compose ps` — все сервисы `running`/`healthy`. Если контейнер `registry` в `restarting` — смотри `docker compose logs registry`, обычно это права на `/data` или занятый порт.
2. **HARBOR01.** Проверь `grep -E 'hostname|port|data_volume' harbor.yml` — `hostname: 10.10.10.55`, `data_volume: /data`. Запомни: HTTPS выключен — это только для стенда.
3. **HARBOR01, UI** `http://10.10.10.55`, вход `admin` / пароль из `harbor.yml`. Смени пароль админа. Создай проект `vektor`, **Public** сними, quota — 5 GiB.
4. **HARBOR01, UI.** Проект `vektor` → **Configuration** → **Tag Immutability** → добавь правило с маской `1.*` (и `**` для `release-*` по вкусу). Затем **Retention rules** → «retain the most recently pushed 3 artifacts» → **Dry Run**, посмотри, что будет удалено, потом сохрани.
5. **HARBOR01, UI.** Проект `vektor` → **Robot Accounts** → **New Robot Account**: имя `ci-push`, expiration 90 дней, права `pull` + `push` на репозитории проекта. Скопируй секрет — он показывается один раз.
6. **UBU01.** `export HARBOR_ROBOT_SECRET='...'`, затем `docker login 10.10.10.55 -u 'robot$vektor+ci-push' --password-stdin <<< "$HARBOR_ROBOT_SECRET"`. Ожидается `Login Succeeded`.
7. **UBU01.** `docker tag vektor/print-agent:1.0.0 10.10.10.55/vektor/print-agent:1.0.0`, `docker push 10.10.10.55/vektor/print-agent:1.0.0`. Ожидается строка с `digest: sha256:...`. Затем `crane digest --insecure 10.10.10.55/vektor/print-agent:1.0.0` — тот же digest.
8. **HARBOR01, UI.** Открой артефакт `1.0.0` → вкладка **Vulnerabilities**. Если сканирование не запускалось — **Scan**. Ожидается список CVE (у `alpine`-базы их немного) или `No vulnerabilities`.
9. **HARBOR01, UI.** **Administration → Registries → New Endpoint**: provider `Docker Registry`, URL `http://10.10.10.50:5050`, креды GitLab-пользователя или deploy token. **Test Connection** → `OK`. Затем **Replications → New Policy**: name `to-gitlab`, source `local`, destination — созданный endpoint, mode `push-based`, trigger `Manual`, фильтр по проекту `vektor`, галочка «delete remote resources when locally deleted». **Save & Run** → статус `Succeeded`.
10. **GIT01.** В проекте `vektor/print-agent` → **Deploy → Container Registry** появился тег `1.0.0`. Через API: `GET /api/v4/projects/:id/registry/repositories` возвращает репозиторий с тегом.
11. **UBU01.** `export COSIGN_PASSWORD='Lab#2026-vektor' && cosign generate-key-pair`. Подпиши по digest: `cosign sign --key cosign.key --tlog-upload=false --allow-insecure-registry "10.10.10.55/vektor/print-agent@sha256:..."`. Проверь: `cosign verify --key cosign.pub --allow-insecure-registry "..."` → `The signatures were verified against the specified public key`.
12. **HARBOR01, UI.** В репозитории `vektor/print-agent` появился артефакт с тегом `sha256-....sig`. Открой его — это манифест подписи, не образ.
13. **UBU01.** Попробуй перезаписать `1.0.0`: собери образ с изменённым `CMD`, сделай `docker push 10.10.10.55/vektor/print-agent:1.0.0`. Ожидается отказ по политике иммутабельности. Зафиксируй текст ошибки.
14. **UBU01 (опционально, сравнение).** Подними Nexus по п. 10 практики, создай `docker-hosted` на порту 8082, включи Docker Bearer Token Realm, запушь тот же образ туда. Сравни путь образа и поведение авторизации.
15. **Запись в `stand-log.md`.** Внеси всё из раздела «Что изменилось на стенде».

### Проверка

- [ ] `docker compose ps` на HARBOR01 — все сервисы подняты, `registry` и `core` без рестартов.
- [ ] `curl -i https://10.10.10.55/v2/` отдаёт 401 с `Www-Authenticate: Bearer realm=...`.
- [ ] Проект `vektor` приватный; без логина `docker pull 10.10.10.55/vektor/print-agent:1.0.0` падает с `unauthorized`.
- [ ] Робот `robot$vektor+ci-push` может пушить, но не может пушить в другой проект.
- [ ] Тег `1.0.0` защищён иммутабельностью: повторный push отклонён.
- [ ] Ретеншн в режиме dry-run показывает ожидаемый список на удаление, боевой запуск удаляет именно его.
- [ ] Репликация в GitLab: статус задачи `Succeeded`, в GitLab Registry виден `1.0.0` с тем же digest.
- [ ] `cosign verify` с правильным `cosign.pub` — успех; с чужим ключом — ошибка.
- [ ] В Harbor есть артефакт `sha256-....sig`, привязанный к digest образа.

### Сломай и почини

**Заявка #4190**
**Пользователь:** CI-пайплайн `build` проекта `vektor/print-agent`
**ВМ:** GIT01 (runner)
**Что написал:** «Job failed: `ERROR: unauthorized: authentication required` при push в Harbor. Локально с ноутбука пушится. Релиз сегодня, помогите».
**Срочность:** высокая.

**Как воспроизвести поломку.** На HARBOR01 в UI проекта `vektor` → **Robot Accounts** → удали робота `ci-push` и создай нового с тем же именем (секрет будет другой; кнопки «перевыпустить секрет» в некоторых версиях нет — [ПРОВЕРИТЬ] для твоей версии). В CI-переменной GitLab `HARBOR_ROBOT_SECRET` оставь старое значение. Пайплайн начнёт падать ровно с этой ошибкой.

**Диагностика по порядку:**

1. Воспроизведи руками на UBU01: `docker login 10.10.10.55 -u 'robot$vektor+ci-push'` с секретом из CI-переменной. Если `401 Unauthorized` — проблема в кредах, а не в сети. Если `connection refused`/timeout — сеть, TLS, порт.
2. `curl -i https://10.10.10.55/v2/` — есть ли вообще `WWW-Authenticate`. Нет — не тот адрес/схема.
3. Harbor UI → Robot Accounts: существует ли робот, не истёк ли срок, есть ли права `push`.
4. GitLab → **Settings → CI/CD → Variables**: не `Protected` ли переменная, если job идёт в незащищённой ветке (тогда она просто не подставится, и логин уйдёт с пустым паролем — а сообщение будет то же).
5. Проверь часы на GIT01 и UBU01: `timedatectl`, `chronyc tracking`. Разбег больше пары минут ломает JWT-токены.
6. На HARBOR01: `docker compose logs core | grep -i unauthorized | tail -20` — подтвердит, что запрос доходил и отбит на авторизации.
7. Отдели вторую возможную причину: `412`/`DENIED` в логе — это иммутабельность тега, а не креды.

**Решение.** Создай нового робота (или перевыпусти секрет), обнови переменную `HARBOR_ROBOT_SECRET` в GitLab CI как **Masked** и при необходимости **Protected**, перезапусти job. Проверь, что push проходит и что повторный push того же тега отбивается уже политикой, а не авторизацией.

**Ответ пользователю:** «Причина — устаревший секрет сервисной учётки реестра: робот-аккаунт `robot$vektor+ci-push` был пересоздан, старый секрет стал невалиден. Секрет в CI-переменных обновлён, пайплайн перезапущен и зелёный. На будущее: секреты реестра ротируем через переменные CI, при ротации предупреждаем за день».

**Закрытие заявки:** категория «CI/CD — доступ к реестру», root cause «ротация сервисной учётки без обновления секрета в CI», профилактика — чек-лист ротации + алерт на долю 401 от реестра.

### Что изменилось на стенде

- **HARBOR01 (10.10.10.55):** Harbor 2.x, пароль admin изменён; проект `vektor` (приватный, quota 5 GiB); правила Tag Immutability (`1.*`) и Retention (3 последних артефакта); робот-аккаунт `robot$vektor+ci-push` (pull+push, 90 дней); replication endpoint на GIT01 и политика `to-gitlab` (push-based, manual); в репозитории `vektor/print-agent` — артефакт `1.0.0` и подпись `sha256-....sig`.
- **UBU01 (10.10.10.30):** в `~/.docker/config.json` — креды для 10.10.10.55; в `~/cosign/` (или текущем каталоге) — `cosign.key`/`cosign.pub` с паролем `Lab#2026-vektor`; установлены `skopeo`, `crane`, `cosign`. Опционально: контейнер `nexus` (порты 8081/8082, том `nexus-data`) с docker-hosted репозиторием `docker-hosted`.
- **GIT01 (10.10.10.50):** включён Container Registry (`registry_external_url http://10.10.10.50:5050`), в проекте `vektor/print-agent` появился образ `1.0.0`.
- **Пароли:** единственный учебный пароль стенда `Lab#2026-vektor` (в бою так нельзя).

### Откат

VMware Workstation → выбери ВМ → **VM → Snapshot → Snapshot Manager** → `before-devops-031` → **Go To**. Повтори для HARBOR01, UBU01 и GIT01. Помни: откат вернёт и данные внутри ВМ (проекты Harbor, образы в GitLab, ключи Cosign на UBU01) — поэтому снапшот делается до лабораторной, а не после. Если откатываешь только HARBOR01, а UBU01 оставляешь с ключами — подписи будут ссылаться на несуществующие артефакты, это нормально для учебного стенда, просто перепушь образ.

## 6. Ошибки новичков и безопасность

1. **Пушить только `latest`.** → В кластере одновременно живут разные версии, откат невозможен, непонятно, что в проде. → Всегда семантический тег + деплой по digest (`image@sha256:...`).
2. **Ходить в реестр под `admin` из CI.** → Утечка переменной = полный контроль над реестром. → Робот-аккаунт с минимальными правами на один проект и сроком жизни.
3. **Включить HTTP «потому что проще».** → Креды и образы идут открытым текстом, MITM подменяет слои. → TLS с внутренним CA, в клиентах — `insecure-registries` только для учебного стенда.
4. **Иммутабельность «включим потом».** → Именно так и появляется заявка Петровой. → Маска `1.*`/`**` на релизные теги с первого дня проекта.
5. **Ретеншн без dry-run.** → Правило «оставить 3» удаляет единственный рабочий образ ветки. → Сначала **Dry Run**, потом сохранение; плюс правило «always retain tag `prod-*`».
6. **Считать репликацию бэкапом.** → В Harbor у репликации есть опция удаления на приёмнике; при удалении источника образ уедет и с GIT01. → Репликация ≠ бэкап; бэкап — это дамп БД Harbor + снапшот тома `/data`.
7. **Подписать и успокоиться.** → Подпись без верификации не влияет ни на что. → `cosign verify` в CI перед деплоем и проверка в admission-контроллере (Kyverno `verifyImages`) в кластере.
8. **Хранить `cosign.key` в Git рядом с проектом.** → Компрометация ключа подписи = можно подписать что угодно. → Ключ в секрет-хранилище (Vault, CI protected variable, KMS), пароль — отдельно; keyless-режим как альтернатива.

**Безопасность.** В боевой среде нельзя: оставлять дефолтный `Harbor12345`; открывать UI реестра в интернет без WAF/ограничения по IP; давать роботам права `system`-уровня; использовать один ключ Cosign на все команды; делать реестр анонимно-читаемым, если в образах есть внутренние артефакты (конфиги, токены в слоях). Отдельно помни: удалённый из реестра тег не удаляет блоб сразу — сборка мусора (GC) делается отдельной задачей jobservice, и до неё данные физически лежат на диске.

## 7. Вопросы на собеседовании

**1. Чем тег отличается от digest и почему в проде деплоят по digest?**

Тег — изменяемый указатель, хранится в метаданных реестра, его можно переставить на другой манифест. Digest — SHA-256 от содержимого манифеста, вычисляется из байтов и потому неизменяем. Деплой по digest гарантирует, что на ноде окажется ровно тот образ, который прошёл ревью, скан и подпись; тег оставляют для читаемости. Дополнительно digest защищает от race condition: между «проверили тег» и «нода скачала тег» содержимое могло измениться.

*Что хочет услышать интервьюер:* понимание content-addressable storage и воспроизводимости деплоя.
*Красный флаг:* «digest — это хеш тега» или «разницы нет, docker сам разберётся».

**2. Как устроена авторизация в Docker Registry API?**

Bearer token flow: неавторизованный запрос к `/v2/` возвращает 401 с заголовком `WWW-Authenticate: Bearer realm=...,service=...`; клиент идёт на `realm` с базовыми кредами и параметром `scope` (`repository:<name>:pull,push`), получает короткоживущий JWT, дальше ходит с ним. У Harbor token-сервис — `/service/token`, у GitLab — свой эндпоинт. Именно поэтому при рассинхроне часов появляется `unauthorized`, а `docker login` не проверяет права до первой реальной операции.

*Что хочет услышать интервьюер:* что человек читал спецификацию, а не только видел ошибки.
*Красный флаг:* «Docker Registry использует basic auth» без уточнений.

**3. Как настроить репликацию Harbor → GitLab и чем она отличается от proxy cache?**

Репликация: в Administration → Registries создаётся endpoint (провайдер `Docker Registry`, URL GitLab, креды), затем Replications → New Policy с режимом push-based или pull-based, фильтрами по имени/тегу/метке и триггером (manual / scheduled cron / on push). Задания выполняет jobservice, статус виден в UI. Proxy cache — другое: проект-кэш, который при `docker pull` прозрачно тянет образ из внешнего реестра (Docker Hub) и отдаёт локально; он не копирует по политике, а кэширует по факту обращения и не работает как «передай мой образ в другой кластер».

*Что хочет услышать интервьюер:* различение «синхронизация между своими реестрами» и «кэш внешнего источника».
*Красный флаг:* «это одно и то же, оба тянут образы».

**4. Как работает подпись Cosign и как заставить кластер её проверять?**

Cosign считает digest образа и подписывает его приватным ключом (или keyless: получает короткоживущий сертификат от Fulcio по OIDC-идентичности и пишет событие в Rekor). Подпись публикуется в тот же реестр как OCI-артефакт — по умолчанию тег `sha256-<digest>.sig`. Проверка — `cosign verify --key cosign.pub image@sha256:...`. Сама подпись ничего не блокирует: чтобы она стала контролем, нужен admission-контроллер с политикой (Kyverno `verifyImages` с `attestors`, Connaisseur, Gatekeeper/OPA), который отклоняет поды с неподписанными или чужими образами. Плюс проверка в CI до деплоя.

*Что хочет услышать интервьюер:* понимание, что подпись = аутентичность, а enforcement — отдельный слой.
*Красный флаг:* «подписал образ — значит он безопасен» (подпись не говорит об отсутствии CVE).

**5. Harbor, GitLab Container Registry или Nexus — что выберешь и почему?**

Зависит от задач. GitLab Registry — когда всё в GitLab и нужен минимум администрирования: права наследуются от проекта, есть cleanup policy. Harbor — когда нужен полноценный реестр с политиками: иммутабельность тегов, ретеншн с dry-run, квоты, роботы, сканирование Trivy, репликация между площадками, прокси-кэш, аудит. Nexus — когда нужен единый склад артефактов разных типов (docker + maven + npm + raw), но за репликацию и часть корпоративных функций придётся платить или строить обходные пути. На практике часто Harbor как основной реестр + GitLab Registry как «рядом с пайплайном».

*Что хочет услышать интервьюер:* выбор по требованиям (политики, мультиплощадочность, типы артефактов), а не «мне нравится вот этот».
*Красный флаг:* «Nexus лучше, потому что он платный» или отсутствие критериев вообще.

## 8. Источники (официальная документация)

- **goharbor.io** — Harbor Docs: разделы *Installation and Configuration Guide*, *Working with projects* (Tag Immutability, Retention, Quotas), *Replication*, *Robot Accounts*, *Vulnerability Scanning*, *API 2.0 / Swagger*.
  Поиск: `Harbor docs tag immutability retention replication policy`.
- **docs.gitlab.com** — раздел *Container Registry*: *Enable the Container Registry*, *Authenticate with the container registry*, *Reduce container registry storage* (cleanup policies), *Container Registry API*.
  Поиск: `GitLab container registry administration cleanup policy`.
- **help.sonatype.com** — Nexus Repository 3: разделы *Docker Repositories*, *Realms* (Docker Bearer Token Realm), *Repository Management*.
  Поиск: `Nexus Repository 3 docker hosted repository connector port`.
- **docs.sigstore.dev** и репозиторий **github.com/sigstore/cosign** — *Cosign* разделы: *Signing containers*, *Verifying signatures*, *Keyless*, *Signing with hardware tokens*.
  Поиск: `cosign sign verify key tlog-upload image digest`.
- **opencontainers.org** и **distribution.github.io** (Distribution / OCI Distribution Specification) — *API*, *Pulling*, *Pushing*, *Token authentication*.
  Поиск: `OCI distribution spec token authentication www-authenticate`.
- **kubernetes.io** — *Images* (образы по digest, `imagePullSecrets`), **kyverno.io** — *Verify Images* (политики проверки подписи в кластере).
  Поиск: `Kyverno verifyImages cosign attestors`.
- Версии Harbor, GitLab, Nexus и cosign, а также точные имена флагов и портов по умолчанию — сверяй с changelog и release notes конкретной версии, они меняются [ПРОВЕРИТЬ].
