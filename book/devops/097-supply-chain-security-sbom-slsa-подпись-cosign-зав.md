# Supply Chain Security: SBOM, SLSA, подпись Cosign, зависимости, зеркала репозиториев

## 1. Введение (зачем это нужно в реальной работе)

Заявка в поддержку (GitLab issue на GIT01, проект `vektor/web`): «После обновления базового образа сборка выросла на 400 МБ, и в SBOM появился пакет, который мы не ставили. Релиз в пятницу. Что делать?» Срочность: высокая. Постановщик: `adm.student` (переслал из чата разработки).

Дальше выясняется классика: в `Dockerfile` стоит `FROM nginx:latest`, в `requirements.txt` — `requests` без версии, а `npm install` в пайплайне каждый раз тянет «что-нибудь свежее» из интернета. Никто не может ответить на простой вопрос: «что именно лежит в образе, который мы завтра выкатываем в прод, и откуда оно взялось?».

Supply chain security (безопасность цепочки поставки) — это ответ на этот вопрос. Мы не пишем весь код сами: 80–90% образа — чужие базовые слои, библиотеки, пакеты ОС. Значит, атаковать будут не ваш код, а то, что вы у кого-то взяли. Уже были event-stream (2018), dependency confusion (2021), ua-parser-js и colors (2021–2022), бэкдор в xz-utils CVE-2024-3094 (2024) — во всех случаях вредоносный код приехал «легальным» обновлением зависимости.

После главы вы должны уметь: собрать SBOM образа, подписать образ Cosign и проверить подпись перед деплоем, объяснить, что такое SLSA-уровни и provenance, зафиксировать зависимости так, чтобы сборка была воспроизводимой, и поднять зеркало реестра в Harbor, чтобы прод не зависел от доступности Docker Hub.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Что входит в цепочку поставки.** Всё, что превращает строчку кода в работающий контейнер: аккаунты разработчиков и их токены, Git-сервер, CI-раннеры, сторонние зависимости (npm, PyPI, apt), базовые образы, сам реестр, ключи и сертификаты подписи, а также система, которая решает «пускать или не пускать» артефакт в прод.

```text
разработчик        Git (GIT01)         CI-раннер           HARBOR01              k8s / прод
    |                  |                   |                    |                     |
    |-- git push ----> |                   |                    |                     |
    |                  |-- pipeline -----> |                    |                     |
    |                  |                   |-- build (база из кэша Harbor)            |
    |                  |                   |-- SBOM (syft)      |                     |
    |                  |                   |-- provenance       |                     |
    |                  |                   |-- cosign sign ---> | образ + .sig + .att |
    |                  |                   |                    |-- cosign verify --> |
    |                  |                   |                    |   (deploy gate)     |
```

**SBOM** (Software Bill of Materials, «перечень комплектующих ПО») — машиночитаемый список компонентов артефакта: имена, версии, лицензии, хеши, связи «кто кого тянет». Два основных формата: **SPDX** (Linux Foundation, стандарт ISO/IEC 5962:2021) и **CycloneDX** (OWASP, стандартизирован в ECMA-424 `[ПРОВЕРИТЬ]`). SBOM нужен не для красоты: без него вы не узнаете, что в вашемобразе лежит уязвимая версия openssl, пока не придёт CVE и не спросят «а мы затронуты?».

**SLSA** (Supply-chain Levels for Software Artifacts, произносится «салса», проект OpenSSF) — набор уровней зрелости сборки. В спецификации 1.0 трек Build разбит на три уровня `[ПРОВЕРИТЬ]`:

- **L1** — сборка автоматизирована, есть provenance (описание: кто, из какого коммита, каким скриптом собрал). Доказательство может быть подделано.
- **L2** — сборка идёт на хостед-платформе (GitLab CI, GitHub Actions), provenance подписан самим сервисом, история сборок сохраняется.
- **L3** — сборка изолирована, секреты недоступны скриптам сборки, provenance невозможно подделать даже владельцу проекта.

Рядом есть трек Source (защита репозитория: обязательные ревью, защищённые ветки) — уровни 1–4 `[ПРОВЕРИТЬ]`.

**Provenance** — тот самый «сертификат происхождения», оформленный как in-toto attestation: JSON-файл, подписанный сборщиком, где записано, какой коммит, какой билд-скрипт и какая платформа породили конкретный хеш образа.

**Cosign и Sigstore.** Cosign — утилита, которая ставит и проверяет подпись у OCI-артефакта. Подпись не «вшивается» в образ, а лежит в реестре отдельным манифестом с тегом `sha256-<digest>.sig` (аттестации — `sha256-<digest>.att`). Отсюда два режима:

- **key-based**: у вас есть пара ключей, приватный ключ подписывает, публичный проверяет. Просто, но ключ надо где-то хранить и ротировать.
- **keyless**: Cosign получает OIDC-токен (например, от GitLab), идёт в **Fulcio** (CA, выдаёт сертификат на 10 минут, привязанный к вашей identity), подпись и сертификат пишутся в **Rekor** — прозрачный append-only лог на дереве Меркла. Проверяющий не имеет вашего ключа, но проверяет цепочку: подпись → сертификат → identity из OIDC → запись в Rekor.

Всё это ездит по HTTPS (443) через OCI Distribution API (реестр), API Fulcio и Rekor. Docker-реестр «по умолчанию» слушает 5000, но в бою — только 443 с TLS.

**Зависимости и зеркала.** Три классические атаки: **typosquatting** (пакет `reqeusts`), **dependency confusion** (внутренний пакет `vektor-utils` ищется в публичном npm — атакующий публикует своё `vektor-utils` с большей версией, и CI тянет его), **компрометация мейнтейнера** (вредоносный код выходит как обычный релиз). Защита: lockfile + хеши, приватные namespace (scopes), и **зеркала репозиториев** — pull-through cache в Harbor для Docker Hub, внутренние прокси для npm/pip/apt. Зеркало даёт три вещи: доступность (Docker Hub rate limit и блокировки), воспроизводимость (версия не исчезнет) и контроль (вы видите, что именно скачали). Обратная сторона: зеркало с правами записи — это точка подмены; кэш должен быть read-only по отношению к источнику.

## 3. Аналогия из жизни

«Вектор» возит грузы. Клиент просит доставить контейнер. Чтобы ему доверяли, нужны три вещи: **накладная** — что внутри (это SBOM), **сертификат происхождения** — с какого завода, по какой накладной, кто грузил (это provenance/SLSA), и **пломба** — пломба с уникальным номером, которую приёмщик сверяет с реестром пломб (это Cosign и Rekor). Дальше — **официальный склад-посредник** вместо случайных перекупщиков: вы не звоните на любой склад в интернете, а берёте груз у проверенного партнёра, который уже один раз всё привёз и записал (это зеркало репозитория).

Аналогия ломается в одном месте: пломбу на контейнере нельзя поставить незаметно задним числом, а подпись к образу можно приделать к любому — толк от неё появляется только тогда, когда приёмная сторона реально спрашивает пломбу. Если деплой не вызывает `cosign verify`, подпись бесполезна.

## 4. Практика и команды

Все команды — с UBU01 (10.10.10.30), если не сказано иное.

**1. Фиксация зависимостей по хешу в Dockerfile.**

```dockerfile
# было: FROM nginx:latest
FROM nginx@sha256:0c86dddac19a64d3a5a96c9d6b3e0e0c0e0f0a0b0c0d0e0f1011121314151617 as base
```

Тег — это указатель, который перезаписывается. Digest — хеш содержимого, его подменить нельзя. Смотреть digest: `docker buildx imagetools inspect <image>` или `crane digest <image>`; в выводе — строка `Digest: sha256:...`, её и копируем.

**2. Lockfile вместо «поставь что-нибудь».**

```bash
npm ci --ignore-scripts          # ставит строго из package-lock.json, не запускает postinstall
pip install --require-hashes -r requirements.txt
```

`npm ci` падает, если `package-lock.json` расходится с `package.json`, — это защита от «на моей машине работало». `--ignore-scripts` не даёт пакету выполнить произвольный код при установке. `pip-compile --generate-hashes requirements.in` (пакет `pip-tools`) генерирует requirements с хешами.

**3. SBOM через Syft.**

```bash
syft harbor.vektor.lab/vektor/web:1.4.2 -o cyclonedx-json=sbom.cdx.json
syft packages dir:. -o spdx-json=sbom.spdx.json
```

Ключ `-o <формат>=<файл>` — формат и путь; форматы: `cyclonedx-json`, `spdx-json`, `syft-json`, `table`. Аргументом может быть образ, директория, файл. Вывод в консоль (таблица): `NAME  VERSION  TYPE` — смотрим, нет ли незнакомых пакетов и пиннинга версий.

**4. Разбор SBOM через jq.**

```bash
jq -r '.components[] | "\(.name) \(.version)"' sbom.cdx.json | sort | head -20
jq '.components[] | select(.name=="openssl")' sbom.cdx.json
```

Ищем конкретный пакет и его версию. Это первое, что делают по CVE: «у нас openssl 3.0.11 или 3.0.13?».

**5. Генерация ключей Cosign.**

```bash
export COSIGN_PASSWORD='Lab#2026-vektor'
cosign generate-key-pair
ls -l cosign.key cosign.pub
# -rw------- cosign.key   -rw-r--r-- cosign.pub
```

`COSIGN_PASSWORD` — парольная фраза, которой шифруется приватный ключ. Публичный ключ (`cosign.pub`) раздаётся всем, кто проверяет; приватный — только CI. В нормальной среде он лежит в Vault (урок 96), а не в файле рядом с репозиторием.

**6. Подпись образа по digest.**

```bash
DIGEST=$(crane digest harbor.vektor.lab/vektor/web:1.4.2)
cosign sign --key cosign.key --yes "harbor.vektor.lab/vektor/web@${DIGEST}"
```

`--yes` — не спрашивать подтверждение (в интерактиве Cosign спрашивает `[ПРОВЕРИТЬ]`). Подпись уедет в реестр тегом `sha256-<digest>.sig`. Ожидаемый вывод: `Pushing signature to: harbor.vektor.lab/vektor/web`.

**7. Проверка подписи.**

```bash
cosign verify --key cosign.pub "harbor.vektor.lab/vektor/web@${DIGEST}"
```

Успех — JSON-массив `[{"critical":{...},"optional":{...}}]` и код возврата 0. Код 1 и `no matching signatures` — подписи нет или она от другого ключа. Именно эта команда ставится в деплой-гейт.

**8. Аттестация provenance.**

```bash
cat > provenance.json <<'EOF'
{
  "_type": "https://in-toto.io/Statement/v0.1",
  "predicateType": "https://slsa.dev/provenance/v0.2",
  "subject": [{"name": "harbor.vektor.lab/vektor/web", "digest": {"sha256": "PUT_DIGEST_HERE"}}],
  "predicate": {"builder": {"id": "gitlab-runner@GIT01"}, "buildType": "gitlab-ci"}
}
EOF
cosign attest --key cosign.key --type slsaprovenance --predicate provenance.json \
  "harbor.vektor.lab/vektor/web@${DIGEST}"
```

`--type` — тип предиката: `slsaprovenance`, `custom`, `spdx`, `cyclonedx`, `link`, `vuln`. Аттестация кладётся тегом `.att`. Формат in-toto в примере упрощён — это учебная заглушка, не валидный SLSA `[ПРОВЕРИТЬ]`.

**9. Проверка аттестации.**

```bash
cosign verify-attestation --key cosign.pub --type slsaprovenance \
  "harbor.vektor.lab/vektor/web@${DIGEST}" | jq -r '.payload' | base64 -d | jq .predicate
```

Вывод — base64-payload, поэтому распаковываем. Смотрим `predicate.builder.id`: если там ожидаемый раннер GIT01 — provenance от своего пайплайна.

**10. Что прикреплено к образу.**

```bash
cosign tree "harbor.vektor.lab/vektor/web:1.4.2"
cosign triangulate "harbor.vektor.lab/vektor/web:1.4.2"
```

`tree` показывает список подписей и аттестаций, `triangulate` — тег, под которым лежит подпись. Быстрый способ понять, «подписан ли образ вообще», не зная digest.

**11. Keyless-подпись и её проверка.**

```bash
cosign sign --yes "harbor.vektor.lab/vektor/web@${DIGEST}"        # в CI с OIDC-токеном
cosign verify \
  --certificate-identity-regexp 'https://gitlab.vektor.lab/*' \
  --certificate-oidc-issuer 'https://gitlab.vektor.lab' \
  "harbor.vektor.lab/vektor/web@${DIGEST}"
```

Без `--certificate-identity*` и `--certificate-oidc-issuer` `cosign verify` в keyless-режиме примет подпись любого аккаунта, который смог пройти OIDC, — это дыра, а не проверка. Точные значения issuer для GitLab CE `[ПРОВЕРИТЬ]`.

**12. Зеркало реестра в Harbor (proxy cache).**

```text
Harbor UI → Projects → New Project
  Project Name: dockerhub
  Access Level: Public
  [x] Proxy Cache
  Endpoint URL: https://hub.docker.com   (или https://registry-1.docker.io) [ПРОВЕРИТЬ]
```

После этого `docker pull 10.10.10.55/dockerhub/library/nginx@sha256:...` тянется через кэш Harbor. Проверка с UBU01:

```bash
skopeo --tls-verify=false inspect --format '{{.Digest}}' \
  docker://10.10.10.55/dockerhub/library/nginx:1.27
```

`--format` выводит digest манифеста — сверяем с тем, что ожидали. Второй pull с той же машины должен отработать быстрее и пережить отключение интернета.

**13. Деплой-гейт.**

```bash
#!/usr/bin/env bash
# /usr/local/bin/verify-before-deploy.sh
set -euo pipefail
IMAGE="$1"
DIGEST=$(crane digest "$IMAGE")
cosign verify           --key /etc/cosign/cosign.pub "$IMAGE@$DIGEST" >/dev/null
cosign verify-attestation --key /etc/cosign/cosign.pub --type slsaprovenance "$IMAGE@$DIGEST" >/dev/null
echo "OK: $IMAGE@$DIGEST подписан и имеет provenance"
```

`set -euo pipefail` — скрипт падает на любой ошибке, включая проверку подписи. Так supply chain security превращается из отчёта в технический контроль.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30) — Docker, git, доступ в интернет для скачивания `syft`/`cosign`/`crane` (или они уже есть с прошлых глав), пользователь с sudo.
- **HARBOR01** (10.10.10.55) — Harbor запущен, создан проект `vektor`, есть учётка с правами push `[ПРОВЕРИТЬ имя проекта в вашем стенде]`. TLS может быть самоподписанным — тогда нужен `--allow-insecure-registry` у cosign и `--tls-verify=false` у skopeo, в бою так нельзя.
- **GIT01** (10.10.10.50) — GitLab CE, проект `vektor/web`, зарегистрированный runner.
- Вход по имени `harbor.vektor.lab` требует записи в `/etc/hosts` на UBU01 и GIT01 или DNS-алиаса на DC01.

### Снапшот

На каждой затрагиваемой ВМ (UBU01, HARBOR01, GIT01) в VMware Workstation: `VM → Snapshot → Take Snapshot`, имя — **`before-devops-097`**.

### Шаги

1. **UBU01.** Проверить инструменты, при отсутствии — установить с релизов проектов `[ПРОВЕРИТЬ доступность GitHub из вашей сети; легальная альтернатива — внутреннее зеркало]`.
   ```bash
   cosign version && syft version && crane version
   ```
   Ожидаемо: три строки с версиями, ошибок нет.

2. **UBU01.** Создать рабочую папку и минимальный `Dockerfile` с пиннингом базы по digest.
   ```bash
   mkdir -p ~/supply-chain && cd ~/supply-chain && git init
   echo 'FROM nginx@sha256:<digest из шага 1>' > Dockerfile
   echo '<h1>vektor</h1>' > index.html
   ```
   Ожидаемо: `Dockerfile` содержит `@sha256:`, а не `latest`.

3. **UBU01.** Собрать и запушить образ в Harbor с тегом-коммитом.
   ```bash
   docker build -t harbor.vektor.lab/vektor/web:lab97 .
   docker login harbor.vektor.lab -u admin && docker push harbor.vektor.lab/vektor/web:lab97
   ```
   Ожидаемо: в Harbor в проекте `vektor` появился артефакт `web:lab97`.

4. **UBU01.** Сгенерировать SBOM из реестра (не из локального кэша — так честнее).
   ```bash
   DIGEST=$(crane digest harbor.vektor.lab/vektor/web:lab97)
   syft "harbor.vektor.lab/vektor/web@$DIGEST" -o cyclonedx-json=sbom.cdx.json
   jq '.components | length' sbom.cdx.json
   ```
   Ожидаемо: число компонентов (для nginx — десятки), файл `sbom.cdx.json` в папке.

5. **UBU01.** Сгенерировать ключи Cosign и зафиксировать права на приватный ключ.
   ```bash
   export COSIGN_PASSWORD='Lab#2026-vektor'
   cosign generate-key-pair
   chmod 600 cosign.key
   ```
   Ожидаемо: `cosign.key` и `cosign.pub`, права `600` на приватном.

6. **UBU01.** Подписать образ по digest и проверить подпись.
   ```bash
   cosign sign --key cosign.key --yes "harbor.vektor.lab/vektor/web@$DIGEST"
   cosign verify --key cosign.pub "harbor.vektor.lab/vektor/web@$DIGEST"
   ```
   Ожидаемо: `Pushing signature to: ...`, затем JSON с `critical.image.docker-manifest-digest`.

7. **UBU01.** Создать `provenance.json`, прикрепить аттестацию и проверить её.
   ```bash
   sed -i "s/PUT_DIGEST_HERE/${DIGEST#sha256:}/" provenance.json
   cosign attest --key cosign.key --type slsaprovenance --predicate provenance.json \
     "harbor.vektor.lab/vektor/web@$DIGEST"
   cosign verify-attestation --key cosign.pub --type slsaprovenance \
     "harbor.vektor.lab/vektor/web@$DIGEST" | jq -r '.payload' | base64 -d | jq .predicate
   ```
   Ожидаемо: распакованный `predicate` с `builder.id`.

8. **GIT01.** Добавить в `.gitlab-ci.yml` стадии сборки и подписи. Приватный ключ — **файловая** CI-переменная `COSIGN_KEY` (Settings → CI/CD → Variables → Type: File), в репозиторий его не коммитим.
   ```yaml
   stages: [build, sign]
   variables:
     IMAGE: harbor.vektor.lab/vektor/web

   build-image:
     stage: build
     image: docker:27
     services: [docker:27-dind]
     script:
       - docker login -u "$HARBOR_USER" -p "$HARBOR_PASS" harbor.vektor.lab
       - docker build -t "$IMAGE:$CI_COMMIT_SHORT_SHA" .
       - docker push "$IMAGE:$CI_COMMIT_SHORT_SHA"

   sbom-and-sign:
     stage: sign
     image: alpine:3.20
     needs: [build-image]
     script:
       - apk add --no-cache cosign syft crane jq   # [ПРОВЕРИТЬ] наличие пакетов в репозитории Alpine
       - DIGEST=$(crane digest "$IMAGE:$CI_COMMIT_SHORT_SHA")
       - syft "$IMAGE@$DIGEST" -o cyclonedx-json=sbom.cdx.json
       - cosign sign --key "$COSIGN_KEY" --yes "$IMAGE@$DIGEST"
     artifacts:
       paths: [sbom.cdx.json]
   ```
   Ожидаемо: пайплайн зелёный, в артефактах лежит `sbom.cdx.json`, в Harbor — `.sig`.

9. **HARBOR01.** Включить proxy cache для Docker Hub (см. п. 4.12) и переключить `FROM` в `Dockerfile` на `10.10.10.55/dockerhub/library/nginx@sha256:...`.
   Ожидаемо: сборка проходит без прямого обращения к Docker Hub.

10. **UBU01.** Поставить деплой-гейт и проверить его на подписанном и неподписанном образе.
    ```bash
    sudo install -m 755 verify-before-deploy.sh /usr/local/bin/
    /usr/local/bin/verify-before-deploy.sh "harbor.vektor.lab/vektor/web:$DIGEST"
    ```
    Ожидаемо: `OK: ... подписан и имеет provenance` на подписанном; ненулевой код возврата на теге без подписи.

11. **UBU01.** Записать изменения в `stand-log.md` (см. следующий раздел).

### Проверка

- [ ] `cosign verify --key cosign.pub <image>@<digest>` возвращает 0 и JSON.
- [ ] `cosign verify-attestation --type slsaprovenance` возвращает 0.
- [ ] `jq '.components | length' sbom.cdx.json` больше 10.
- [ ] В Harbor у артефакта видны теги `sha256-<digest>.sig` и `sha256-<digest>.att`.
- [ ] В пайплайне GIT01 артефакт `sbom.cdx.json` сохранён.
- [ ] `verify-before-deploy.sh` падает на неподписанном образе (проверили намеренно).
- [ ] `Dockerfile` не содержит `:latest`.
- [ ] Pull из `10.10.10.55/dockerhub/library/nginx` работает.

### Сломай и почини

> **Пользователь:** команда разработки (постановщик — `adm.student`, переслал из чата)
> **ВМ:** GIT01, проект `vektor/web`
> **Что написал:** «Пайплайн зелёный, подпись в логах есть. Но деплой-скрипт на UBU01 говорит `no matching signatures`. Вчера тот же образ проходил. Ничего не меняли!»
> **Срочность:** высокая, релиз вечером

**Как воспроизвести поломку.** На UBU01 пересобрать образ с тем же тегом и запушить:

```bash
docker build -t harbor.vektor.lab/vektor/web:lab97 .
docker push harbor.vektor.lab/vektor/web:lab97
```

Тег тот же, digest — новый. Старая подпись осталась висеть на старом digest.

**Диагностика по порядку.**

1. Что говорит проверка и на что она смотрит:
   ```bash
   cosign verify --key cosign.pub harbor.vektor.lab/vektor/web:lab97
   # Error: no matching signatures
   ```
2. Текущий digest и что было подписано:
   ```bash
   crane digest harbor.vektor.lab/vektor/web:lab97
   cosign tree harbor.vektor.lab/vektor/web:lab97
   ```
   `tree` покажет пусто — подпись привязана к другому digest.
3. Есть ли подпись у старого digest: в Harbor → проект `vektor` → артефакт `web` → список тегов. Тег `sha256-<старый>.sig` есть, `sha256-<новый>.sig` — нет. Вывод: образ пересобрали, подпись «отвалилась».
4. Проверить, не подменён ли ключ: `sha256sum cosign.pub` на UBU01 и на раннере должны совпадать.
5. Проверить, что деплой-гейт вообще вызывается: `grep -n cosign verify /usr/local/bin/verify-before-deploy.sh`.

**Решение.** Подписывать не тег, а digest сразу после push, и в деплой передавать digest, а не тег. В пайплайне — блок `sbom-and-sign` из шага 8 (он именно так и делает). Для уже собранного образа:

```bash
DIGEST=$(crane digest harbor.vektor.lab/vektor/web:lab97)
cosign sign --key cosign.key --yes "harbor.vektor.lab/vektor/web@$DIGEST"
cosign verify --key cosign.pub "harbor.vektor.lab/vektor/web@$DIGEST"
```

Дополнительно: перейти на иммутабельные теги (`lab97-<git-sha>` и запрет перезаписи в Harbor) `[ПРОВЕРИТЬ точное название настройки "Immutable tags" в вашей версии Harbor]`.

**Ответ пользователю:** «Причина найдена: образ пересобрали с тем же тегом, digest изменился, а подпись осталась на старом digest — это ожидаемое поведение, тег не является идентификатором содержимого. Подписал актуальный digest и поправил пайплайн: теперь подпись ставится по digest сразу после push, а деплой принимает только digest. Проверено на стенде, релиз можно катить».

**Закрытие заявки:** статус Closed, в комментарий — digest, команда проверки и ссылка на MR с правкой `.gitlab-ci.yml`.

### Что изменилось на стенде

- **UBU01:** установлены `cosign`, `syft`, `crane`; каталог `~/supply-chain` с `Dockerfile`, `sbom.cdx.json`, `provenance.json`; создана пара ключей `cosign.key`/`cosign.pub`; в `/usr/local/bin` — `verify-before-deploy.sh`.
- **HARBOR01:** в проекте `vektor` артефакт `web:lab97` с тегами `.sig` и `.att`; создан проект `dockerhub` с включённым proxy cache.
- **GIT01:** в проекте `vektor/web` — стадии `build`/`sign`, переменные `COSIGN_KEY` (File), `HARBOR_USER`, `HARBOR_PASS`.
- **Ключи:** `cosign.pub` разложен на UBU01 и (при необходимости) на узлы, которые проверяют подпись.

### Откат

VMware Workstation → выбрать ВМ (UBU01, затем HARBOR01, затем GIT01) → `VM → Snapshot → Snapshot Manager` → снапшот `before-devops-097` → **Revert To**. Машины вернутся к состоянию до лабораторной: инструменты, ключи, проекты и артефакты исчезнут. Запись в `stand-log.md` откатом не удаляется — допишите строку «откатено к before-devops-097, причина: …».

## 6. Ошибки новичков и безопасность

- **Подписывать тег, а не digest** → после пересборки подпись «отваливается», деплой падает или, хуже, проходит проверка старого артефакта → всегда резолвить тег в digest (`crane digest`) и подписывать `image@sha256:...`.
- **Приватный ключ в репозитории или в обычной CI-переменной** → компрометация ключа = возможность подписать что угодно от вашего имени, а отзыв требует перевыпуска всего → файловая переменная как минимум, Vault/KMS как норма; ключ с ограниченным сроком жизни.
- **`latest` в `FROM` и версии без lockfile** → сборка невоспроизводима, «вчера работало» не воспроизводится, dependency confusion проходит незамеченным → digest в `FROM`, `npm ci`, `--require-hashes` для pip, lockfile в Git.
- **Keyless-проверка без `--certificate-identity` и `--certificate-oidc-issuer`** → валидной считается подпись любого пользователя любого OIDC-провайдера → всегда указывать ожидаемую identity и issuer.
- **SBOM ради галочки: сгенерировали и выбросили** → через месяц по CVE нечего анализировать → SBOM как артефакт пайплайна с retention, плюс регулярная перегенерация для уже выпущенных образов.
- **`--insecure-ignore-tlog`, `--allow-insecure-registry`, `--tls-verify=false` в постоянной конфигурации** → отключается весь смысл прозрачного лога и TLS → только как временная мера в лабе, с записью в `stand-log.md` и датой снятия.
- **Публичное зеркало с правами записи или зеркало «на доверии»** → подмена пакета в кэше выглядит как обычная сборка → кэш read-only к источнику, подпись пакетов (`signed-by` для apt), аудит того, что реально скачано.
- **Внутренние пакеты без reserved scope** → dependency confusion: атакующий публикует `vektor-utils` в публичном реестре с версией 99.0.0 → приватные scopes (`@vektor/...`), `.npmrc` с явным registry, запрет fallback на публичный реестр для внутренних имён.

### Безопасность

В боевой среде нельзя: хранить приватный ключ подписи в Git; подписывать вручную с рабочей машины (подпись должна рождаться в CI, где фиксируется provenance); использовать самоподписанные сертификаты реестра вне лабы; принимать в кластер образы без проверки подписи «потому что так быстрее»; включать proxy cache с анонимным pull из интернета на прод-контуре без фильтрации. Учебный пароль `Lab#2026-vektor` — только для стенда: в реальной работе он был бы нарушением парольной политики, а `COSIGN_PASSWORD` в открытом виде в истории shell — утечкой.

## 7. Вопросы на собеседовании

**1. Что такое SBOM и зачем он нужен, если у нас уже есть сканер уязвимостей?**

Ответ: SBOM — структурированный перечень компонентов артефакта с версиями, лицензиями и хешами, в стандартном формате (SPDX или CycloneDX). Сканер отвечает на вопрос «что плохого в этом образе сейчас», SBOM — на вопрос «из чего образ состоит», и он остаётся полезным, когда появляется новая CVE через полгода после релиза: пересканировать можно сам SBOM, не пересобирая образ. Плюс SBOM нужен для лицензионного комплаенса и для передачи заказчику — в РФ это всё чаще требование при поставке.

*Что хочет услышать:* что SBOM — это данные, а не отчёт; что он генерируется на сборке и хранится как артефакт; что форматы стандартные, а не «свой JSON».

*Красный флаг:* «SBOM = вывод `docker history`» или «это то же самое, что отчёт Trivy».

**2. Объясните уровни SLSA. Чем provenance отличается от SBOM?**

Ответ: SLSA — уровни зрелости процесса сборки. Build L1 — сборка автоматизирована и порождает provenance, но доказательство можно подделать; L2 — сборка на хостед-платформе, provenance подписан сервисом, история хранится; L3 — изолированная сборка, где даже владелец проекта не может подделать provenance. Provenance — подписанное утверждение о том, *как* артефакт появился: коммит, билд-скрипт, платформа, входные материалы. SBOM описывает *что внутри*, provenance — *откуда и как это сделано*. Одно без другого неполно.

*Что хочет услышать:* понимание, что SLSA — про процесс, а не про уязвимости; что уровни повышают доверие к утверждению, а не «качество кода».

*Красный флаг:* «SLSA — это сертификат, который выдают» или путаница уровней с количеством проверок безопасности.

**3. Как работает keyless-подпись в Cosign и почему ей можно доверять?**

Ответ: CI получает OIDC-токен от провайдера идентичности, Cosign идёт с ним в Fulcio, получает короткоживущий сертификат, привязанный к identity (репозиторий, ветка, workflow), подписывает образ и пишет запись в Rekor — прозрачный append-only лог. Проверяющий не имеет ключа: он проверяет цепочку подпись → сертификат → identity → запись в Rekor. Доверие держится на Fulcio как CA и Rekor как публичном журнале: постфактум подпись не подделать и не удалить незаметно. Обязательно проверять `--certificate-identity` и `--certificate-oidc-issuer`, иначе проверка бессмысленна.

*Что хочет услышать:* три компонента (Fulcio, Rekor, OIDC) и понимание, что проверять надо идентичность, а не просто наличие подписи.

*Красный флаг:* «keyless — значит без проверки» или «Cosign хранит приватный ключ на сервере Sigstore».

**4. Что такое dependency confusion и как от неё защититься?**

Ответ: атака на менеджеры пакетов, которые сначала смотрят в приватный реестр, а при неудаче — в публичный. Если внутренний пакет `vektor-utils` не зарезервирован в публичном реестре, атакующий публикует свой `vektor-utils` с более высокой версией — резолвер выберет его. Защита: приватные scopes (`@vektor/*`), явный registry в `.npmrc`/`pip.conf` без fallback, lockfile с проверкой источника и хеша, внутреннее зеркало с запретом проксировать внутренние имена во внешние реестры.

*Что хочет услышать:* понимание механики версий и источников, а не только «мы используем Nexus».

*Красный флаг:* «достаточно поставить firewall» — атака проходит по обычному HTTPS на 443.

**5. Зачем DevOps-инженеру зеркало репозиториев и какие у него риски?**

Ответ: зеркало/pull-through cache решает три задачи: доступность (Docker Hub rate limits, блокировки, недоступность публичных реестров из РФ), воспроизводимость (артефакт не исчезнет из публичного реестра) и контроль (единая точка, где видно, что скачивается). Риски: зеркало становится точкой подмены, особенно если оно с правами записи; кэш может «залипнуть» на устаревшей версии; один упавший Harbor останавливает сборки. Меры: read-only к источнику, подпись пакетов и образов, TLS с внутренним CA, регулярная очистка и мониторинг кэша, резервный путь на случай падения.

*Что хочет услышать:* баланс «доступность/воспроизводимость против новой точки отказа и новой точки атаки».

*Красный флаг:* «зеркало нужно только чтобы обойти блокировки» — без упоминания контроля и целостности.

## 8. Источники (официальная документация)

- **SLSA** — slsa.dev: раздел «Specification» (уровни Build и Source), «Provenance». Поиск: `SLSA specification build levels provenance`.
- **Sigstore / Cosign** — docs.sigstore.dev: разделы «Cosign», «Signing containers», «Keyless signing», «Verifying». Поиск: `sigstore cosign keyless verify attestation`.
- **CycloneDX** — cyclonedx.org: «Specification», «Authoritative guide to SBOM». Поиск: `CycloneDX specification JSON schema`.
- **SPDX** — spdx.dev: «Specifications», ISO/IEC 5962:2021. Поиск: `SPDX specification ISO 5962`.
- **Syft** — github.com/anchore/syft (README и docs): форматы вывода, работа с образами. Поиск: `anchore syft output formats spdx cyclonedx`.
- **Harbor** — goharbor.io: раздел «Configuring a Proxy Cache», «Immutable tags». Поиск: `Harbor proxy cache project configuration`.
- **in-toto** — in-toto.io: «Attestation framework», «Statement and predicate». Поиск: `in-toto attestation statement predicate SLSA provenance`.
- **GitLab Docs** — docs.gitlab.com: «CI/CD variables», «OIDC / ID tokens», «Container registry». Поиск: `GitLab CI ID tokens OIDC documentation`. Точные имена issuer/OIDC-эндпоинтов для вашей версии GitLab CE — `[ПРОВЕРИТЬ]`.
- **OpenSSF** — openssf.org: «Securing the software supply chain», «SLSA», «Scorecard». Поиск: `OpenSSF supply chain security guide`.
- **NIST SSDF** — csrc.nist.gov: SP 800-218, «Secure Software Development Framework». Поиск: `NIST SP 800-218 SSDF`.
