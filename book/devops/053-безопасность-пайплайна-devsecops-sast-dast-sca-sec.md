# Безопасность пайплайна (DevSecOps): SAST, DAST, SCA, secret scanning, SBOM и подпись артефактов

## 1. Введение (зачем это нужно в реальной работе)

Тикет в учебной системе «Вектор»:

> **Пользователь:** sidorov (по легенде лабы — дежурный разработчик сервиса `vektor/app`)
> **ВМ:** GIT01
> **Что написал:** «Пайплайн на ветке `main` стал красным после моих правок, а релиз 1.0.7 нужен сегодня. Я уже отключил шаг с проверкой секретов в `.gitlab-ci.yml`, но теперь падает что-то на подписи. Можно вообще убрать все эти security-джобы, они только мешают?»
> **Срочность:** высокая

Это ровно та ситуация, из-за которой компании теряют деньги и данные. Пайплайн — единственное место, где код, зависимости и образы проходят через одного «контролёра» автоматически и на каждом коммите. Если контролёра нет, то уязвимость в библиотеке или случайно закоммиченный пароль попадает в прод через 15 минут после `git push`, и узнают об этом не разработчики, а служба ИБ или, что хуже, клиент.

DevSecOps — это не «отдел безопасности пришёл и всё запретил», а перенос проверок влево (shift left): чем раньше найдена проблема, тем дешевле её починить. Ошибка на этапе проектирования стоит копейки, ошибка в проде — инцидент, простой и отчёт.

После этой главы ты будешь уметь: встроить в GitLab CI четыре класса проверок (SAST, SCA, secret scanning, DAST), собрать SBOM, подписать образ ключом cosign и сделать так, чтобы в кластер не мог попасть неподписанный образ. И главное — уметь объяснить разработчику, почему «убери security-джобы» — плохой ответ.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Все инструменты безопасности пайплайна делятся по **объекту проверки** и по **моменту проверки**. Объект — это исходный код, зависимости, контейнерный образ или работающее приложение. По моменту: статический анализ (ничего не запускаем) и динамический (запускаем и стучимся снаружи).

```text
        git push (GIT01, 10.10.10.50)
                │
   ┌────────────┴─────────────┐   СТАТИКА (не запускаем код)
   │ secret scanning (gitleaks) │  исходники + вся история коммитов
   │ SAST (semgrep)             │  код: SQLi, XSS, хардкод, опасные API
   │ SCA  (trivy fs / grype)    │  манифесты зависимостей → CVE, лицензии
   └────────────┬─────────────┘
                ▼  build → образ
   ┌──────────────────────────┐
   │ container scan (trivy image)│ ОС-пакеты + библиотеки ВНУТРИ образа
   │ SBOM (syft → CycloneDX)     │ «состав» образа, инвентаризация
   │ sign (cosign)               │ подпись digest, а не тега
   └────────────┬─────────────┘
                ▼
        HARBOR01 10.10.10.55 (реестр, OCI Registry API, HTTPS 443)
                ▼
        stage-окружение: WEB01 10.10.20.10 (DMZ, nginx)
   ┌──────────────────────────┐   ДИНАМИКА (приложение работает)
   │ DAST (OWASP ZAP baseline) │  заголовки, cookies, TLS, ошибки конфигурации
   └────────────┬─────────────┘
                ▼
   k8s-cp01/k8s-w01/k8s-w02 (10.10.10.61–63): admission control проверяет подпись
```

**SAST** (Static Application Security Testing) читает исходный код, строит AST/потоки данных и ищет небезопасные конструкции. Работает быстро, но не видит конфигурацию рантайма и даёт ложные срабатывания.

**SCA** (Software Composition Analysis) вообще не смотрит на твой код — он читает `package-lock.json`, `pom.xml`, `requirements.txt`, `go.mod` и сверяет версии с базами CVE (OSV, NVD). Именно SCA ловит Log4Shell-класс проблем: код у тебя свой, а дыра пришла из зависимости.

**Secret scanning** ищет ключи, токены, пароли в коде и в истории. Ключевое слово — «в истории»: удалить коммит недостаточно, объект остаётся в `.git` и у всех, кто уже сделал `git clone`.

**DAST** (Dynamic Application Security Testing) — сканер запускает HTTP-запросы к живому приложению на WEB01 и смотрит на ответы. Видит то, чего не видит SAST: реальные заголовки, cookie без `Secure`, открытые листинги каталогов.

**SBOM** (Software Bill of Materials) — «состав» артефакта в машиночитаемом виде, форматы CycloneDX и SPDX. Когда случается очередной инцидент, вопрос звучит так: «у нас это есть и где?». С SBOM ответ занимает минуту вместо трёх дней.

**Подпись артефактов** — криптографическое подтверждение, что образ собран именно твоим пайплайном и не изменён по дороге. Здесь работает стек Sigstore: `cosign` подписывает **digest** (sha256), а не тег. Тег — это указатель, его можно перевесить на другой образ; digest неизменен. Смежная тема — **SLSA** (Supply-chain Levels for Software Artifacts), уровни 1–4, описывающие зрелость защиты цепочки поставки `[ПРОВЕРИТЬ формулировки уровней на slsa.dev]`.

Порты на стенде: GitLab — 80/443 на GIT01, реестр Harbor — 443 на HARBOR01, ZAP ходит на WEB01 по 80/443. Если хочешь автоматически проверять подпись на входе в кластер — это делает admission-контроллер (Kyverno) через Kubernetes API 6443.

## 3. Аналогия из жизни

Представь завод, который выпускает коробки с товаром. **Secret scanning** — это турникет на входе: не пускает сотрудника с пропуском, забытым в кармане чужого пальто (и главное — помнит все прошлые смены). **SAST** — технолог, который читает чертёж изделия и говорит: «тут крепление на честном слове». **SCA** — входной контроль комплектующих: сами детали ты не производишь, но обязан знать, что поставщик привёз. **Container scan** — проверка уже собранной коробки на входе: не забыли ли внутри просроченный компонент. **SBOM** — накладная с полным перечнем содержимого коробки, чтобы при отзыве партии поставщиком найти все коробки за час. **Подпись** — пломба на коробке: получатель видит, что её запечатал именно твой цех, а не кто-то на складе. **DAST** — приёмщик, который уже в магазине открыл коробку, включил товар и попробовал им пользоваться.

Аналогия перестаёт работать в одном месте: пломба гарантирует, что коробку не вскрыли по дороге, но ничего не говорит о том, что внутри товар хороший — подписанный образ может содержать критическую уязвимость.

## 4. Практика и команды

Все команды ниже выполняются на GIT01 (10.10.10.50) в каталоге клона проекта `vektor/app` или внутри CI-джоб. Установка инструментов: `docker pull` нужных образов на GIT01, если Docker Hub доступен; из РФ он иногда медленный или недоступен — тогда ставь бинарники с GitHub Releases вручную или используй `shell`-executor раннера.

**1. Secret scanning — gitleaks**

```bash
gitleaks detect --source . --report-format json --report-path gitleaks.json --redact
```

`detect` — режим сканирования репозитория (в v8.19+ появились алиасы `gitleaks git` и `gitleaks dir` `[ПРОВЕРИТЬ актуальность в установленной версии]`). `--source .` — что сканируем. `--redact` — не печатать сам секрет в лог (в CI без этого секрет утечёт ещё раз, уже в лог джобы). `--report-format json` — чтобы отдать в артефакты. Код возврата 1 при находках — это и есть «ворота». Для подавления ложных срабатываний есть `--baseline-path` — файл с известными срабатываниями.

Типичный вывод:

```text
Finding:     AWS_SECRET_ACCESS_KEY="AKIA...wJal"
Secret:      REDACTED
RuleID:      aws-secret-access-key
File:        app/config.py
Line:        17
Commit:      9f3c1a2...
```

Смотрим: `RuleID` (что за правило), `File`/`Line` (где), `Commit` (когда попало и надо ли чистить историю).

**2. Проверка перед коммитом (pre-commit)**

```bash
gitleaks protect --staged --verbose
```

`protect --staged` смотрит только то, что уже в индексе — то есть ловит секрет до коммита, а не в пайплайне. Это дешевле всего: разработчик узнаёт о проблеме за 2 секунды.

**3. SAST — semgrep**

```bash
semgrep scan --config p/owasp-top-ten --config p/secrets --error --sarif -o semgrep.sarif .
```

`--config` можно указывать несколько раз; `p/...` — публичные наборы правил Semgrep Registry. `--error` — падать при находках (по умолчанию semgrep не возвращает ошибку). `--sarif -o файл` — формат SARIF, его понимают GitHub Code Scanning и GitLab. Вариант `--config auto` отправляет метаданные на сервер Semgrep и требует сети до `semgrep.dev` — в закрытом контуре используй локальный клон репозитория правил и путь к каталогу вместо `p/...` `[ПРОВЕРИТЬ имя репозитория правил в актуальной версии]`.

Типичный вывод:

```text
app/api.py
  42: cursor.execute("SELECT * FROM users WHERE id=" + uid)
     python.lang.security.audit.formatted-sql-query
     Severity: ERROR
```

Смотрим на путь, номер строки и `Severity`. ERROR — блокируем, WARNING — триажим.

**4. SCA и сканирование ФС — trivy**

```bash
trivy fs --scanners vuln,secret,misconfig \
  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 .
```

`fs` — сканировать каталог (манифесты зависимостей, Dockerfile, IaC). `--scanners vuln,secret,misconfig` — три сканера разом: CVE в зависимостях, секреты, ошибки конфигурации. `--ignore-unfixed` — не падать на CVE, для которых ещё нет исправленной версии (иначе пайплайн будет красным и починить нельзя). `--exit-code 1` — падать при находках.

Типичный вывод:

```text
express (package-lock.json)
  CVE-2024-29041  HIGH  Fixed in: 4.19.2
```

Смотрим: пакет, CVE, версия с фиксом. `Fixed in` есть → обновляем зависимость. Нет → фиксируем риск в реестре и ставим срок.

**5. Сканирование образа**

```bash
trivy image --insecure --severity HIGH,CRITICAL --exit-code 1 \
  10.10.10.55/vektor/app:1.0.7
```

`--insecure` — не проверять TLS-сертификат реестра (у нас в лабе самоподписанный, в проде так нельзя). Сканирует ОС-пакеты и библиотеки внутри образа. Тот же флаг `--exit-code` — это и есть gate.

**6. SBOM — syft**

```bash
syft dir:. -o cyclonedx-json=sbom.cdx.json
syft 10.10.10.55/vektor/app:1.0.7 -o spdx-json=sbom.spdx.json --from registry
```

`-o формат=файл` — формат вывода. `dir:.` — по локальному коду, `10.10.10.55/...` — по образу в реестре. Для образов в приватном реестре нужна авторизация: `SYFT_REGISTRY_AUTH_USERNAME`/`..._PASSWORD` `[ПРОВЕРИТЬ имена переменных окружения]` или `--registry-username/--registry-password`.

**7. Проверка SBOM на уязвимости — grype**

```bash
grype sbom:./sbom.cdx.json --fail-on high
```

`sbom:` — префикс «читай SBOM, а не образ». `--fail-on high` — порог падения. Grype и Syft — одна команда разработки (Anchore), форматы стыкуются напрямую. Смысл: у тебя остаётся SBOM, и через полгода, когда выйдет новая CVE, ты пересканируешь **исторические** SBOM без пересборки.

**8. Ключи cosign**

```bash
cosign generate-key-pair
ls -l cosign.key cosign.pub
```

Создаёт пару ключей; `cosign.key` зашифрован паролем (спросит интерактивно, в CI — через `COSIGN_PASSWORD`). `cosign.key` — секрет, `cosign.pub` — публичный, его можно коммитить и раздавать в кластер. В проде приватный ключ хранят не в CI-переменных, а в KMS/Vault или используют keyless-режим через OIDC `[ПРОВЕРИТЬ доступность публичного Sigstore из РФ — возможно, потребуется свой инстанс с Rekor]`.

**9. Подпись и проверка образа**

```bash
cosign sign --key cosign.key --yes \
  10.10.10.55/vektor/app@sha256:6b1e...c9
cosign verify --key cosign.pub --allow-insecure-registry \
  10.10.10.55/vektor/app@sha256:6b1e...c9
```

`--yes` — не спрашивать подтверждение. `--allow-insecure-registry` — HTTP/самоподписанный реестр (только лаба). Подписывается **digest**, синтаксис `repo@sha256:...`. `verify` печатает JSON с `critical.identity` и `optional` — важно, чтобы вернулся код 0 и в выводе был ожидаемый digest.

**10. Аттестация SBOM к образу**

```bash
cosign attest --key cosign.key --yes --type cyclonedx \
  --predicate sbom.cdx.json \
  10.10.10.55/vektor/app@sha256:6b1e...c9
cosign verify-attestation --key cosign.pub --type cyclonedx \
  --allow-insecure-registry 10.10.10.55/vektor/app@sha256:6b1e...c9
```

Аттестация — подписанное утверждение «вот SBOM именно для этого digest». `--type cyclonedx` `[ПРОВЕРИТЬ поддерживаемые типы предикатов в установленной версии cosign]`. Так SBOM не просто лежит рядом, а криптографически привязан к образу.

**11. DAST — OWASP ZAP baseline**

```bash
docker run --rm -v "$(pwd)/zap:/zap/wrk:rw" \
  ghcr.io/zaproxy/zap-stable zap-baseline.py \
  -t http://10.10.20.10 -r zap-report.html -J zap-report.json -I
```

`zap-baseline.py` — пассивное сканирование ( spider + passive rules), 1–3 минуты, безопасно для stage. `-t` — target, `-r`/`-J` — HTML и JSON отчёты, `-I` — не падать на предупреждениях (WARN), падать только на FAIL. Для аутентифицированного сканирования нужен контекст ZAP — это отдельная история; baseline без логина проверит заголовки, TLS, cookie, утечки в комментариях.

**12. Фрагмент `.gitlab-ci.yml`**

```yaml
stages: [test, build, scan, sign, dast]

variables:
  IMAGE: 10.10.10.55/vektor/app
  TRIVY_CACHE_DIR: "$CI_PROJECT_DIR/.trivy"

secret-scan:
  stage: test
  image: zricethezav/gitleaks:latest
  entrypoint: [""]
  script:
    - gitleaks detect --source . --redact --report-format json --report-path gitleaks.json
  artifacts:
    when: always
    paths: [gitleaks.json]

sast:
  stage: test
  image: semgrep/semgrep:latest
  entrypoint: [""]
  script:
    - semgrep scan --config p/owasp-top-ten --config p/secrets --error --sarif -o semgrep.sarif .
  artifacts:
    when: always
    paths: [semgrep.sarif]

sca:
  stage: test
  image: aquasec/trivy:latest
  entrypoint: [""]
  script:
    - trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL
        --ignore-unfixed --exit-code 1 .
```

`entrypoint: [""]` — критичная мелочь: у образов trivy, semgrep и gitleaks прописан `ENTRYPOINT` с именем бинарника. Без сброса GitLab выполнит `trivy trivy fs ...` и джоба упадёт с непонятной ошибкой. Проверяй `docker inspect --format '{{.Config.Entrypoint}}' image`.

```yaml
build:
  stage: build
  image: docker:27-cli
  script:
    - echo "$CI_REGISTRY_PASSWORD" | docker login 10.10.10.55 -u "$CI_REGISTRY_USER" --password-stdin
    - docker build -t "$IMAGE:$CI_COMMIT_SHORT_SHA" .
    - docker push "$IMAGE:$CI_COMMIT_SHORT_SHA"
    - D=$(docker inspect --format='{{index .RepoDigests 0}}' "$IMAGE:$CI_COMMIT_SHORT_SHA" | cut -d@ -f2)
    - echo "IMAGE_DIGEST=$D" > digest.env
  artifacts:
    reports: { dotenv: digest.env }

container-scan:
  stage: scan
  needs: [build]
  image: aquasec/trivy:latest
  entrypoint: [""]
  script:
    - trivy image --insecure --severity HIGH,CRITICAL --exit-code 1
        "$IMAGE@$IMAGE_DIGEST"

sbom:
  stage: scan
  needs: [build]
  image: anchore/syft:latest
  entrypoint: [""]
  script:
    - syft "$IMAGE@$IMAGE_DIGEST" -o cyclonedx-json=sbom.cdx.json
  artifacts:
    paths: [sbom.cdx.json]

sign:
  stage: sign
  needs: [container-scan, sbom]
  image: alpine:3.20
  script:
    - apk add --no-cache curl
    - curl -sSLo /usr/local/bin/cosign
        https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64
        && chmod +x /usr/local/bin/cosign
    - cosign sign --key "$COSIGN_KEY" --yes --allow-insecure-registry "$IMAGE@$IMAGE_DIGEST"
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'

dast:
  stage: dast
  needs: []
  image: ghcr.io/zaproxy/zap-stable
  script:
    - zap-baseline.py -t http://10.10.20.10 -J zap.json -I
  artifacts:
    when: always
    paths: [zap.json]
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: manual
```

Обрати внимание: `build` кладёт digest в `dotenv`-артефакт, и все следующие джобы работают с `@sha256:...`, а не с тегом. Это и есть главный приём — **всё дальше по цепочке прибито к digest**. Сборка образа (DinD vs Kaniko) подробно разобрана в уроке 58, здесь считаем, что она уже работает.

**13. Admission control на входе в кластер**

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-vektor-images
spec:
  validationFailureAction: Enforce
  rules:
    - name: check-signature
      match:
        any:
          - resources:
              kinds: [Pod]
      verifyImages:
        - imageReferences:
            - "10.10.10.55/vektor/*"
          attestors:
            - entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      <cosign.pub>
                      -----END PUBLIC KEY-----
```

`validationFailureAction: Enforce` — не просто предупредить, а отклонить создание пода. Схема Kyverno менялась между версиями `[ПРОВЕРИТЬ актуальный синтаксис verifyImages для установленной версии]`. В нормальной среде сначала ставят `Audit`, смотрят PolicyReport неделю, потом переводят в `Enforce`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Готовы GIT01 (10.10.10.50, GitLab CE + runner, есть проект `vektor/app` с Dockerfile из урока 49) и HARBOR01 (10.10.10.55, проект `vektor`, push-права у `adm.student`).
- WEB01 (10.10.20.10) развёрнут в DMZ и доступен с GIT01 по HTTP — маршрутизация через GW01 настроена на треке L2. Если WEB01 с GIT01 не пингуется, запускай `dast`-джобу на раннере, размещённом в DMZ, либо временно проверяй ZAP против контейнера nginx на самом GIT01, а в `stand-log.md` зафиксируй отклонение.
- Кластер k8s-cp01/w01/w02 (10.10.10.61–63) работает; Kyverno можно не ставить, шаг 12 тогда пропускается, а проверка подписи делается только через `cosign verify` в пайплайне.
- В GitLab у проекта `vektor/app` заведены CI/CD-переменные: `COSIGN_KEY` (тип **File**, содержимое `cosign.key`), `COSIGN_PASSWORD` (Masked), `CI_REGISTRY_USER`/`CI_REGISTRY_PASSWORD`.

### Снапшот

На GIT01, HARBOR01 и WEB01 (и на k8s-cp01, если ставишь Kyverno) в VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-053`. Имя одинаковое на всех ВМ — так проще откатывать одной командой в уме. Дополнительно закоммить текущее состояние `.gitlab-ci.yml` в GitLab, чтобы откат был и на уровне кода.

### Шаги

1. **GIT01.** Сгенерируй ключи: `mkdir -p ~/ci-keys && cd ~/ci-keys && cosign generate-key-pair`. Ожидаемо: появляются `cosign.key` и `cosign.pub`, пароль — учебный `Lab#2026-vektor`. Публичный ключ скопируй в репозиторий как `cosign.pub`.
2. **GIT01.** Загрузи приватный ключ в GitLab: **Settings → CI/CD → Variables → Add variable**, key `COSIGN_KEY`, type **File**, value — содержимое `cosign.key`, flags Protected + Masked. Ожидаемо: переменная появляется в списке без отображения значения.
3. **GIT01.** Создай ветку `feature/security` и добавь в проект файл `app/config.py` со строкой `AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"` (это **пример из документации AWS**, не действующий ключ — так и должно быть, мы тренируемся). Ожидаемо: локально всё компилируется, ничего не подозрительно.
4. **GIT01.** Добавь в `.gitlab-ci.yml` джобы `secret-scan`, `sast`, `sca` из раздела 4 и запушь ветку. Ожидаемо: пайплайн **красный**, `secret-scan` падает, в артефакте `gitleaks.json` есть `RuleID: aws-access-token`, путь `app/config.py`.
5. **GIT01.** Убедись, что проблема не только в рабочем дереве: выполни `git log -p -- app/config.py | grep -n AKIA`. Ожидаемо: ключ виден в истории. Сделай вывод: «ротация обязательна, история чистится отдельно». Удали строку из файла, закоммить, запушь. Ожидаемо: `secret-scan` зелёный, но в отчёте по истории ключ всё ещё находится — зафиксируй это в `stand-log.md`.
6. **GIT01.** Добавь уязвимую зависимость: в `package.json` пропиши `"lodash": "4.17.15"` (в этой версии есть известные CVE `[ПРОВЕРИТЬ актуальный список в NVD]`), запусти `sca`. Ожидаемо: trivy печатает таблицу с CVE и `Fixed in`, джоба красная. Обнови до актуальной версии — джоба зелёная.
7. **GIT01.** Собери образ и запушь в HARBOR01 (джоба `build`), проверь, что артефакт `digest.env` содержит `IMAGE_DIGEST=sha256:...`. Ожидаемо: в Harbor в проекте `vektor` появился тег с коротким SHA коммита.
8. **GIT01/HARBOR01.** Запусти `container-scan` и `sbom`. Скачай артефакт `sbom.cdx.json`, открой, найди в нём `metadata.component` и список `components`. Ожидаемо: JSON на несколько тысяч строк, в компонентах видны пакеты базового образа (ubuntu/debian).
9. **GIT01.** Выполни джобу `sign` на ветке `main`. Ожидаемо: в Harbor у образа появляется подпись (вкладка артефактов: рядом с образом — дополнительный OCI-артефакт `sha256-....sig`). Локально проверь: `cosign verify --key cosign.pub --allow-insecure-registry 10.10.10.55/vektor/app@sha256:<digest>`. Ожидаемо: JSON с `critical.identity.docker-reference` и код 0.
10. **GIT01.** Запусти `dast` вручную против WEB01. Ожидаемо: отчёт `zap.json` с секцией `site`, списком `alerts`; часть — WARN (например, отсутствие `Content-Security-Policy`), FAIL быть не должно, если nginx настроен из урока L2.
11. **k8s-cp01.** Примени Kyverno-политику из раздела 4 (с `validationFailureAction: Audit` на первый прогон). Ожидаемо: `kubectl get clusterpolicy` показывает `verify-vektor-images`, а `kubectl get policyreport -A` — результаты проверки.
12. **k8s-cp01.** Попробуй запустить неподписанный образ: `kubectl run bad --image=nginx:1.27 --restart=Never`. Ожидаемо: в `Audit` под создаётся, но в PolicyReport — FAIL; после перевода в `Enforce` тот же запуск отклоняется с сообщением о непроверенной подписи.
13. **GIT01.** Запиши в `stand-log.md`: добавлены джобы `secret-scan`, `sast`, `sca`, `container-scan`, `sbom`, `sign`, `dast`; создана пара ключей cosign; в Harbor включено хранение подписей; на кластере — политика Kyverno.

### Проверка

- [ ] В `.gitlab-ci.yml` пять и более стадий, `build` публикует `digest.env` как `reports: dotenv`.
- [ ] `secret-scan` падает на тестовом ключе AWS и зелёный после удаления строки.
- [ ] `sast` находит хотя бы одно правило OWASP Top Ten в демонстрационном коде (проверь на специально добавленной конкатенации SQL).
- [ ] `sca` печатает CVE с колонкой `Fixed in` и падает при `--exit-code 1`.
- [ ] `sbom` производит валидный CycloneDX: `jq '.bomFormat' sbom.cdx.json` → `"CycloneDX"`.
- [ ] `cosign verify` возвращает код 0 для подписанного digest.
- [ ] `cosign verify` возвращает ненулевой код для **тега** без digest, если подпись навешивалась на другой digest.
- [ ] `zap-baseline.py` отработал и создал `zap.json` с непустым `site`.
- [ ] В Harbor у образа виден подписанный артефакт.
- [ ] В `stand-log.md` записаны все изменения.

### Сломай и почини

> **Пользователь:** sidorov (дежурный разработчик)
> **ВМ:** GIT01
> **Что написал:** «Пайплайн зелёный до шага `sign`, а на выкатке в stage падает с `no matching signatures`. Образ тот же, тег `1.0.7`, я его только перезалил после фикса опечатки. Подпись же была!»
> **Срочность:** высокая, релиз через час

**Как воспроизвести поломку.** На GIT01 пересобери образ с тем же тегом и запушь его вручную, минуя пайплайн:

```bash
cd ~/app && echo "# hotfix" >> README.md
docker build -t 10.10.10.55/vektor/app:1.0.7 .
docker push 10.10.10.55/vektor/app:1.0.7
```

Тег `1.0.7` теперь указывает на **новый** digest, подпись осталась на старом.

**Диагностика по шагам.**

1. Проверяем, что именно ломается: `cosign verify --key cosign.pub --allow-insecure-registry 10.10.10.55/vektor/app:1.0.7` — ошибка `no matching signatures`.
2. Смотрим digest тега: `docker manifest inspect 10.10.10.55/vektor/app:1.0.7 | jq -r '.config.digest'` `[ПРОВЕРИТЬ поле для схемы манифеста v2]` или проще `docker inspect --format='{{index .RepoDigests 0}}'`.
3. Смотрим, на какой digest есть подпись: `cosign triangulate --allow-insecure-registry 10.10.10.55/vektor/app:1.0.7` покажет, где cosign искал подпись `[ПРОВЕРИТЬ флаг для insecure-реестра в triangulate]`. Сравниваем digest из шага 2 и подписанный digest из лога джобы `sign`.
4. Вывод: digest разошёлся. Причина — перезаливка тега в обход пайплайна. Это не баг cosign, это баг процесса.

**Решение.** Прогнать полный пайплайн на новом коммите, чтобы образ прошёл `container-scan`, `sbom` и `sign` и подпись легла на актуальный digest; затем деплоить строго `repo@sha256:...`. Дополнительно: включить в Harbor immutable tags для релизных тегов (запрет перезаписи) `[ПРОВЕРИТЬ название настройки в твоей версии Harbor — Tag Immutability Rules]`, чтобы поломка не повторялась.

**Ответ пользователю:** «Подпись была на предыдущем digest, ты перезалил тег `1.0.7` новым образом — подпись к нему не относится и не могла относиться. Перезапусти пайплайн на коммите с фиксом, я включил запрет перезаписи релизных тегов, и деплой теперь идёт по digest. С этого момента перезаливка тега вручную не сработает — только через CI».

**Закрытие заявки:** категория «CI/CD, подпись артефактов», решение — пересборка через пайплайн + immutable tags, в `stand-log.md` строка «в Harbor включены Tag Immutability Rules для `vektor/app:1.*`».

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** в `.gitlab-ci.yml` проекта `vektor/app` добавлены джобы `secret-scan`, `sast`, `sca`, `build` (с dotenv-digest), `container-scan`, `sbom`, `sign`, `dast`; созданы ключи `~/ci-keys/cosign.key` и `cosign.pub`; в GitLab добавлены CI/CD-переменные `COSIGN_KEY` (File) и `COSIGN_PASSWORD` (Masked).
- **HARBOR01 (10.10.10.55):** в проекте `vektor` хранятся образы с подписями cosign; включены правила неизменяемости тегов. Учётка — `adm.student`, учебный пароль стенда.
- **k8s-cp01 (10.10.10.61):** установлен Kyverno, применена ClusterPolicy `verify-vektor-images` (в `Audit`, при желании — `Enforce`).
- **WEB01 (10.10.20.10):** без изменений в конфигурации, но появился потребитель — сканер ZAP с GIT01.
- **Новые артефакты в репозитории:** `cosign.pub`, отчёты `gitleaks.json`, `semgrep.sarif`, `sbom.cdx.json`, `zap.json` (в артефактах пайплайна).

### Откат

В VMware Workstation: правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-053` → **Restore Snapshot** на GIT01, HARBOR01, WEB01 и k8s-cp01. Если откатываешь только код — `git revert` мерж-коммита с security-джобами; если только кластер — `kubectl delete clusterpolicy verify-vektor-images`. Ключи cosign после отката стенда считаются скомпрометированными для целей лабы: удали `~/ci-keys` и переменные `COSIGN_KEY`/`COSIGN_PASSWORD` из проекта.

## 6. Ошибки новичков и безопасность

1. **Подписывать тег, а не digest.** → После любой перезаливки проверка падает или, хуже, проходит на чужом образе. → Всегда `repo@sha256:...`, digest передавать через `dotenv`-артефакт.
2. **`--exit-code 0` или `allow_failure: true` на security-джобах «чтобы не мешало».** → Пайплайн зелёный, уязвимости едут в прод, отчёты никто не читает. → Порог задаётся политикой (`--severity HIGH,CRITICAL --ignore-unfixed`), а не отключением проверки.
3. **Удалить коммит с секретом и считать проблему решённой.** → Секрет остался в истории, в форках и в клонах у коллег; он уже утёк. → Сначала **ротация** ключа, потом чистка истории (`git filter-repo`) и включение pre-commit.
4. **Хранить `cosign.key` в репозитории или в незащищённой переменной.** → Компрометация ключа = возможность подписать любой образ и протащить его в кластер. → File-переменная + Protected + Masked, в проде — KMS/Vault или keyless.
5. **Ставить admission-политику сразу в `Enforce`.** → В один день перестают запускаться чужие поды, включая системные компоненты, и «кладётся» кластер. → Сначала `Audit`, анализ PolicyReport, whitelist для системных namespace, потом `Enforce`.
6. **Гонять DAST full-scan по проду.** → Сканер реально ломает формы, удаляет записи, создаёт мусорные данные; для prod — только passive/baseline. → Активное сканирование — на stage, с тестовыми данными.
7. **Игнорировать ложные срабатывания без процесса.** → Через месяц 400 «красных» находок, которые все привыкли игнорировать. → Baseline-файл, allowlist с обязательным сроком и владельцем, тикет на каждый accepted risk.
8. **Не хранить SBOM.** → При новой CVE («а у нас log4j есть?») начинается ручной обход репозиториев на несколько дней. → SBOM — артефакт с retention не меньше срока жизни образа, плюс пересканирование исторических SBOM по расписанию.

### Безопасность

В боевой среде нельзя: держать приватный ключ подписи в CI-переменных без ограничения по веткам (`Protected`), использовать самоподписанные TLS и флаг `--insecure` в сканерах, хранить тестовый ключ AWS из примеров в реальном коде, отключать сканирование на `main` «на время релиза». И помни: подпись образа подтверждает авторство, но **не** отсутствие уязвимостей — это две разные проверки, и нужны обе.

## 7. Вопросы на собеседовании

**1. Чем SAST отличается от DAST и SCA и почему нельзя обойтись одним инструментом?**

Ответ: SAST анализирует исходный код без запуска — быстро, на каждом коммите, видит проблемные конструкции и потоки данных, но не видит рантайм и даёт false positives. DAST работает по запущенному приложению снаружи — видит реальные заголовки, cookie, ошибки конфигурации, но медленный, требует окружения и не показывает, где в коде ошибка. SCA вообще не смотрит на код, а сверяет версии зависимостей с базами CVE — единственный, кто ловит уязвимость, пришедшую из чужой библиотеки. Пересечение покрытия частичное, поэтому в пайплайне нужны все три, плюс сканирование образа и secret scanning.

*Что хочет услышать интервьюер:* понимание, что классы проверок дополняют друг друга, и что инструмент — не самоцель, а место в пайплайне (быстрые проверки раньше, медленные позже).

*Красный флаг:* «достаточно SAST, он же код читает» или путаница SCA с container scanning.

**2. Разработчик закоммитил пароль в публичный репозиторий и через 10 минут удалил коммит. Твои действия?**

Ответ: считаю секрет скомпрометированным с момента пуша. Первое — ротация (отзыв токена, смена пароля) в течение минут, потому что боты сканируют публичные репозитории автоматически. Второе — оценить ущерб по логам использования. Третье — чистка истории (`git filter-repo --invert-paths --path <файл>`), force-push, переустановка у всех коллег, инвалидация форков и кэшей CI. Четвёртое — процессные меры: pre-commit hook с gitleaks, secret scanning в пайплайне, ротация по расписанию.

*Что хочет услышать интервьюер:* «сначала ротация, потом история» и понимание, что удаление коммита ничего не решает.

*Красный флаг:* «удалил коммит — вопрос закрыт» или «перепишу историю и всё».

**3. Что такое SBOM, зачем он нужен и как ты им пользуешься при инциденте?**

Ответ: SBOM — машиночитаемый перечень компонентов артефакта с версиями и идентификаторами (purl, CPE) в форматах CycloneDX или SPDX. Генерируется на этапе сборки (syft), хранится как артефакт и подписывается/аттестуется к digest образа. При инциденте типа Log4Shell запрос превращается из «обойди 60 репозиториев» в «прогони grype по всем историческим SBOM и получи список затронутых сервисов за минуты». Плюс SBOM закрывает требования регуляторов и заказчиков по прозрачности цепочки поставки.

*Что хочет услышать интервьюер:* формат, момент генерации, привязка к digest, сценарий «новая CVE → пересканирование старых SBOM».

*Красный флаг:* «SBOM — это список пакетов из `npm ls`» или «мы его генерируем, но нигде не храним».

**4. Как гарантировать, что в кластер попадёт только подписанный образ?**

Ответ: цепочка из трёх частей. В пайплайне собираем образ, сканируем, генерируем SBOM и подписываем **digest** приватным ключом (`cosign sign`). В реестре храним подпись рядом с образом (Harbor это умеет как OCI-артефакт) и запрещаем перезапись релизных тегов. В кластере ставим admission-контроллер (Kyverno, Sigstore Policy Controller) с публичным ключом: любой Pod с образом из нашего реестра без валидной подписи отклоняется. Плюс деплой-манифесты ссылаются на digest, а не на тег. Дополнительно — контроль доступа к ключу (KMS/Vault) и, в идеале, keyless-подпись через OIDC, чтобы ключ вообще не существовал как файл.

*Что хочет услышать интервьюер:* «подпись digest + верификация на входе в кластер + защита ключа», а не только `cosign sign`.

*Красный флаг:* «подписываем тег» или «проверяем подпись только в пайплайне» — тогда обход через прямой `docker push` остаётся открытым.

**5. Security-проверки замедляют пайплайн и дают много ложных срабатываний. Как ты балансируешь?**

Ответ: разношу проверки по скорости: secret scanning и линтеры — pre-commit и первый stage (секунды), SAST/SCA — параллельно на MR, container scan и SBOM — после сборки, DAST — на stage-окружении по расписанию и перед релизом. Гейты настраиваю по политике: блокирую CRITICAL/HIGH с доступным фиксом (`--ignore-unfixed`), остальное — в отчёт. Ложные срабатывания подавляю baseline-файлами и allowlist с владельцем и сроком, а не отключением джобы. Раз в квартал пересматриваю правила и пороги по метрикам: сколько находок, сколько реально починено, сколько времени пайплайн стоит в красном.

*Что хочет услышать интервьюер:* инженерный подход — приоритизация, автоматизация триажа, метрики, отсутствие «выключим проверку».

*Красный флаг:* «поставим `allow_failure: true` на всё security» или «пусть ИБ разбирается, мы только отчёты отдаём».

## 8. Источники (официальная документация)

- **GitLab Docs** — `docs.gitlab.com`, раздел «Application security» (SAST, Secret Detection, Dependency Scanning, Container Scanning) и «CI/CD pipelines». Поиск: `gitlab docs secret detection CI`.
- **Semgrep Docs** — `semgrep.dev`, разделы «Getting started» и «Rule syntax». Поиск: `semgrep ci scan config rules`.
- **Trivy Docs** — `trivy.dev` (ранее `aquasecurity.github.io/trivy`) `[ПРОВЕРИТЬ актуальный домен]`, разделы «Scanner» и «SBOM». Поиск: `trivy fs scan scanners vuln secret misconfig`.
- **Syft / Grype Docs** — `github.com/anchore`, README и раздел «Supported formats». Поиск: `syft cyclonedx output grype sbom`.
- **Sigstore / cosign Docs** — `docs.sigstore.dev`, разделы «Signing containers» и «Keyless». Поиск: `cosign sign verify attest image digest`.
- **OWASP ZAP** — `zaproxy.org`, раздел «ZAP – Baseline Scan» и «Automate». Поиск: `zap baseline scan docker github action`.
- **CycloneDX** — `cyclonedx.org`, раздел «Specification». Поиск: `cyclonedx json specification components`.
- **SLSA** — `slsa.dev`, разделы «Specification» и «Levels». Поиск: `slsa security levels supply chain`.
- **Kyverno** — `kyverno.io`, раздел «Verify Images». Поиск: `kyverno verifyImages cosign policy`.
- **NIST SSDF (SP 800-218)** — `csrc.nist.gov`, «Secure Software Development Framework». Поиск: `nist sp 800-218 ssdf`.
- **CISA SBOM** — `cisa.gov`, раздел «SBOM». Поиск: `cisa sbom minimum elements`.
