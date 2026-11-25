# Сканирование: Trivy, Grype, Semgrep, SonarQube, Gitleaks, OWASP ZAP в пайплайне

## 1. Введение (зачем это нужно в реальной работе)

**Тикет:** «Пользователь: adm.student / ВМ: GIT01 / Что написал: "Вчера вечером в репозиторий `portal` прилетел мерж, сегодня пайплайн встал на этапе `image_scan`. Релиз встал. Разберись, что там — CVE или ложное срабатывание, и почему это не поймали раньше" / Срочность: высокая».

Сканеры — это то, чем DevOps-инженер занимается буквально каждый день: настраивает этапы в пайплайне, читает отчёты, объясняет команде, почему билд красный, и договаривается, какие находки блокируют релиз, а какие едут в бэклог. Хорошая новость: сами инструменты несложные, у каждого один-два основных режима запуска. Сложная часть — политика: где поставить гейт, какой порог severity выбрать, что делать с ложными срабатываниями и как не превратить безопасность в «пайплайн, который всегда красный, поэтому его отключили».

После этой главы ты будешь уметь: поставить шесть разных сканеров в GitLab CI на стенде «Вектор», читать их отчёты (табличные, JSON, SARIF), настраивать fail-пороги, вести файлы исключений и объяснять разработчику, что именно он сломал. И главное — понимать, что каждый сканер видит только свой срез проблемы, и «чистый скан» не значит «безопасно».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Сканеры в CI раскладываются по этапам жизненного цикла. Важно понимать, что они анализируют и на каких данных работают, потому что именно от этого зависит скорость и точность.

| Класс | Что анализирует | Инструменты | Где в пайплайне |
|---|---|---|---|
| Secret scanning | текст файлов и историю git | Gitleaks | до всего остального, 5–15 сек |
| SAST | исходный код (AST, паттерны, dataflow) | Semgrep, SonarQube | после checkout, до сборки |
| SCA | манифесты и lock-файлы зависимостей | Trivy fs, Grype | там же, где SAST |
| Контейнерный скан | слои образа, пакеты внутри | Trivy image, Grype | после build, до push |
| IaC/misconfig | Dockerfile, Helm, k8s-манифесты, Terraform | Trivy config | параллельно SCA |
| DAST | работающее приложение по HTTP | OWASP ZAP | после деплоя на stage |

Как это работает внутри. SCA-сканер не «понимает» код: он читает `package-lock.json`, `requirements.txt`, `go.sum`, `pom.xml`, вытаскивает список «пакет + версия», определяет дистрибутив/экосистему и сверяет с базой уязвимостей. Базы: NVD (nvd.nist.gov), GitHub Advisory (GHSA), трекеры дистрибутивов (Debian/Ubuntu/Alpine security trackers). Trivy и Grype отличаются именно источниками и логикой матчинга: у Trivy сильная поддержка дистрибутивных пакетов (apt/apk внутри образа), у Grype — свой движок матчинга и удобная работа с готовым SBOM.

SAST работает иначе: строит дерево разбора и ищет паттерны или потоки данных (source → sink). Semgrep — это по сути «grep по AST» с правилами на YAML, поэтому он быстрый и легко расширяется своими правилами. SonarQube — тяжёлый сервер с индексом кода, историей метрик и Quality Gate; он анализирует глубже, но требует своего инстанса и базы.

DAST ничего не знает о коде: он ходит по HTTP и смотрит на ответы — заголовки, cookie, отражения параметров, доступные пути. Baseline-режим ZAP делает только пассивный анализ (плюс обход сайта пауком), full-scan добавляет активные атаки и может уронить приложение.

Формат обмена результатами — **SARIF** (Static Analysis Results Interchange Format, стандарт OASIS). GitLab, GitHub и большинство сканеров умеют его, поэтому в пайплайне удобно складывать все отчёты в один артефакт и смотреть в UI.

```text
GIT01 (10.10.10.50), GitLab CE + runner
  ┌──────────────────────────────────────────────────────────────┐
  │ stage: secrets   gitleaks detect            (5 сек)          │
  │ stage: sast      semgrep --config=...       (30–90 сек)      │
  │                  sonar-scanner -> :9000     (2–5 мин)        │
  │ stage: sca       trivy fs / config, grype   (30–60 сек)      │
  │ stage: build     docker build -> portal:sha                  │
  │ stage: image_scan trivy image (гейт)        (40–90 сек)      │
  │                  grype sbom:sbom.json                        │
  │ stage: dast      zap-baseline -> WEB01      (2–5 мин)        │
  └──────────────────────────────────────────────────────────────┘
        │ push                        │ pull              │ HTTP
        ▼                             ▼                   ▼
   HARBOR01 10.10.10.55          k8s-cp01 .61        GW01 10.10.20.1
   (реестр образов)              k8s-w01 .62               │
                                 k8s-w02 .63        WEB01 10.10.20.10
```

Порты, которые понадобятся: GitLab — 443/80 на GIT01, реестр Harbor — 443 на HARBOR01, SonarQube — 9000 на GIT01, HTTP-трафик ZAP — 80 на WEB01 через GW01. Внешний трафик нужен только для обновления баз уязвимостей (Trivy тянет БД с ghcr.io, Grype — с своей реплики).

## 3. Аналогия из жизни

Представь диспансеризацию. Терапевт задаёт вопросы (secret scanning — быстрый осмотр, ловит очевидное: пароль в открытом виде). Анализ крови показывает состав изнутри (SCA — «что у тебя внутри»: версии пакетов). Рентген просвечивает структуру (SAST — видит устройство кода, но не видит, что происходит в момент работы). УЗИ смотрят на работающий орган в динамике (DAST — приложение уже запущено, мы смотрим на его поведение). И есть главврач, который сводит все заключения в один вердикт и решает, пускать ли на смену (Quality Gate в SonarQube).

Два вывода из аналогии. Первый: ни один метод не видит всего, поэтому и делают шесть исследований, а не одно. Второй: у любого метода есть «тени» — затемнение на рентгене не всегда опухоль, а VULNERABLE в Trivy не всегда реальная дыра в твоём коде (пакет может быть не задействован). Отсюда — обязательная ручная триажная стадия. Где аналогия ломается: в медицине повторное обследование стоит денег и времени пациента, а в CI каждый лишний скан — это минуты простоя раннера и ожидания разработчика, поэтому здесь инженерные компромиссы жёстче.

## 4. Практика и команды

Все команды выполняются на **GIT01 (10.10.10.50)**, сканеры ставятся на раннер. Для лабораторных — только бесплатные/OSS-версии.

**1. Trivy: скан образа с гейтом.**

```bash
trivy image \
  --severity HIGH,CRITICAL \
  --ignore-unfixed \
  --exit-code 1 \
  --format table \
  "$HARBOR_HOST/vektor/portal:1.0.0"
```

`--severity` фильтрует по уровню (NEGLIGIBLE/LOW/MEDIUM/HIGH/CRITICAL), `--ignore-unfixed` убирает CVE, на которые нет исправленной версии (иначе гейт нельзя закрыть вообще), `--exit-code 1` — вернуть код 1 при находках (по умолчанию всегда 0!). Смотреть: имя пакета, установленная версия, версия с фиксом.

```text
portal:1.0.0 (debian 12.5)
Total: 14 (HIGH: 9, CRITICAL: 5)
┌────────────┬────────────────┬──────────┬───────────────────┐
│  Library   │ Vulnerability  │ Severity │ Fixed Version     │
├────────────┼────────────────┼──────────┼───────────────────┤
│ libssl3    │ CVE-2024-XXXXX │ CRITICAL │ 3.0.13-1~deb12u1  │
└────────────┴────────────────┴──────────┴───────────────────┘
```

**2. Trivy: скан файловой системы (SCA по манифестам).**

```bash
trivy fs --scanners vuln,secret --severity HIGH,CRITICAL .
```

Здесь `--scanners` явно перечисляет, что включить: `vuln` (зависимости), `secret` (утечки), `misconfig`. По умолчанию `misconfig` не включён — частая причина «а почему Trivy не нашёл мой небезопасный Dockerfile».

**3. Trivy: конфигурации и IaC.**

```bash
trivy config --severity HIGH,CRITICAL --exit-code 1 deploy/
```

Проверяет Dockerfile, Helm-чарты, k8s-манифесты, Terraform по встроенным политикам (Rego/встроенные checks). Типичная находка: `KSV001` — контейнер запускается без `runAsNonRoot`.

**4. Trivy: экспорт SBOM (мост к уроку 97).**

```bash
trivy image --format cyclonedx --output sbom.json "$IMAGE"
trivy image --format spdx-json  --output sbom.spdx.json "$IMAGE"
```

SBOM (Software Bill of Materials) — спецификация состава образа в формате CycloneDX или SPDX. Дальше его сканируют Grype/Dependency-Track и подписывают Cosign. Это правильный способ «сканировать один раз, проверять много раз».

**5. Grype: скан по готовому SBOM.**

```bash
grype sbom:./sbom.json -o table --fail-on high
```

`-o` — формат вывода (`table`, `json`, `sarif`, `cyclonedx-json`), `--fail-on high` — код возврата 1 при находке уровня high и выше. Grype умеет и напрямую `grype dir:.` или `grype "$IMAGE"`, но по SBOM быстрее и не требует docker-демона.

**6. Gitleaks: поиск секретов.**

```bash
gitleaks detect --source . --redact -v \
  --report-format sarif --report-path gitleaks.sarif
```

`--source .` — каталог (по умолчанию сканируется и история git), `--redact` маскирует сам секрет в выводе (обязательно, иначе секрет утечёт в логи CI), `-v` — подробный вывод. Exit code 1 при находках. В новых версиях есть подкоманды `gitleaks git` и `gitleaks dir` [ПРОВЕРИТЬ актуальность `detect` в вашей версии].

**7. Gitleaks: свой конфиг с безопасным allowlist.**

```toml
# .gitleaks.toml
[extend]
useDefault = true

[[rules]]
id = "vektor-internal-token"
description = "Внутренний токен Вектор"
regex = '''vkr_[0-9a-f]{32}'''
tags = ["token", "vektor"]

[allowlist]
description = "Тестовые фикстуры и примеры"
paths = [
  '''tests/fixtures/.*''',
  '''docs/examples/.*''',
]
regexes = ['''vkr_0{32}''']
```

`useDefault = true` наследует встроенный набор правил, `[[rules]]` добавляет свои, `allowlist` — исключения по путям и регуляркам. Правило: в allowlist идёт либо заведомо фейковый секрет, либо путь фикстуры. Реальный секрет — только в Vault (урок 96), никогда в конфиге.

**8. Semgrep: SAST без аккаунта.**

```bash
semgrep --config=p/owasp-top-ten \
  --error --severity=ERROR \
  --sarif --output=semgrep.sarif \
  --exclude=tests --exclude=node_modules .
```

`--config=p/...` — набор правил из публичного реестра Semgrep (`p/owasp-top-ten`, `p/secrets`, `p/ci`), `--config=auto` подбирает правила по языкам [ПРОВЕРИТЬ: требует сетевого доступа к реестру], `--error` — вернуть 1 при находках (без него код всегда 0), `--severity` фильтрует. Свои правила кладутся в `.semgrep.yml` или каталог `rules/`.

**9. SonarQube: анализ проекта со сканером.**

```properties
# sonar-project.properties
sonar.projectKey=vektor_portal
sonar.projectName=vektor portal
sonar.sources=src
sonar.exclusions=**/node_modules/**,**/*.test.js
sonar.sourceEncoding=UTF-8
sonar.qualitygate.wait=true
```

```bash
sonar-scanner \
  -Dsonar.host.url=http://10.10.10.50:9000 \
  -Dsonar.token="$SONAR_TOKEN"
```

`sonar.qualitygate.wait=true` заставляет пайплайн ждать вердикта Quality Gate и падать, если он красный — без этой строки шаг «прошёл» ещё до окончания анализа. `sonar.token` — вместо устаревшего `sonar.login` [ПРОВЕРИТЬ в вашей версии]. Server: SonarQube Community Build, ставится Docker-контейнером на GIT01, интерфейс `http://10.10.10.50:9000`.

**10. OWASP ZAP: пассивный baseline-скан.**

```bash
docker run --rm -t ghcr.io/zaproxy/zap-stable \
  zap-baseline.py \
  -t http://10.10.20.10/ \
  -r zap-report.html \
  -J zap-report.json \
  -I
```

`zap-baseline.py` — обход сайта + пассивные правила, без атакующих запросов (безопасно даже для stage). `-t` — цель, `-r`/`-J` — HTML и JSON отчёты [ПРОВЕРИТЬ флаг `-J`], `-I` — не падать на предупреждениях уровня WARN (иначе гейт по умолчанию ругается почти на любой сайт). Коды выхода: 0 — чисто, 1 — есть новые алерты, 2 — ошибка запуска. Активный вариант — `zap-full-scan.py`, только по своим стендам и с письменного согласия владельца.

**11. Сборка этапов в GitLab CI.**

```yaml
stages: [secrets, sast, sca, build, image_scan, dast]

variables:
  IMAGE: "$HARBOR_HOST/vektor/portal:$CI_COMMIT_SHORT_SHA"
  TRIVY_CACHE_DIR: "$CI_PROJECT_DIR/.cache/trivy"

gitleaks:
  stage: secrets
  image: zricethezav/gitleaks:latest   # [ПРОВЕРИТЬ тег]
  script:
    - gitleaks detect --source . --redact -v --report-format sarif --report-path gitleaks.sarif
  artifacts: { when: always, paths: [gitleaks.sarif] }

semgrep:
  stage: sast
  image: semgrep/semgrep:latest        # [ПРОВЕРИТЬ тег]
  script:
    - semgrep --config=p/owasp-top-ten --error --sarif --output=semgrep.sarif .
  artifacts: { when: always, paths: [semgrep.sarif] }

trivy_fs:
  stage: sca
  image: aquasec/trivy:latest          # [ПРОВЕРИТЬ тег]
  script:
    - trivy fs --scanners vuln,secret --severity HIGH,CRITICAL --exit-code 1 .

image_scan:
  stage: image_scan
  script:
    - trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$IMAGE"
    - trivy image --format cyclonedx --output sbom.json "$IMAGE"
    - grype sbom:sbom.json --fail-on high
  artifacts: { when: always, paths: [sbom.json] }

zap_baseline:
  stage: dast
  allow_failure: true
  script:
    - docker run --rm -t ghcr.io/zaproxy/zap-stable zap-baseline.py -t http://10.10.20.10/ -J zap.json -I
  artifacts: { when: always, paths: [zap.json] }
```

Ключевое: самые быстрые и дешёвые сканы — первыми (секреты, затем SAST/SCA параллельно), гейты на образ — до push в Harbor, DAST — после деплоя на stage и сначала в режиме `allow_failure`, пока не откалиброван.

**12. Файл исключений и политика.**

```text
# .trivyignore — только с комментарием и сроком
# 2026-04-01, согласовано: adm.student. CVE в libwebp не достигается: библиотека не используется
CVE-2023-4863

# .grype.yaml
ignore:
  - vulnerability: CVE-2023-4863
    reason: "не эксплуатируется, ждём обновления базового образа"
```

Каждое исключение — с датой, автором и причиной. Исключение без срока жизни через полгода становится дырой, о которой никто не помнит.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GIT01** (10.10.10.50): GitLab CE + runner, есть репозиторий проекта `portal` с `Dockerfile` и минимальным приложением (если репозитория ещё нет — создай его пустым и добавь `Dockerfile` на `node:14-alpine` [ПРОВЕРИТЬ доступность тега] и любой `package.json`, чтобы было что сканировать). Раннер в режиме `docker` executor, имеет доступ к docker.sock.
- **HARBOR01** (10.10.10.55): проект `vektor`, учётная запись для push, есть `HARBOR_HOST` и `HARBOR_USER`/`HARBOR_PASSWORD` в CI/CD Variables GitLab (masked).
- **WEB01** (10.10.20.10): nginx в DMZ, доступен с GIT01 через GW01 (10.10.20.1).
- Учётная запись домена `adm.student`, пароль стенда `Lab#2026-vektor` — только для этого стенда.
- Если модуль CI/CD ещё не пройден — вернись к главам про GitLab CI на GIT01.

### Снапшот

В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot, имя `before-devops-098`. Снять на **GIT01** и **HARBOR01** (на WEB01 изменения не планируются, но снапшот `before-devops-098` сделать и там — пригодится для DAST).

### Шаги

1. **GIT01, shell.** Ставим сканеры на раннер. Trivy — из официального репозитория пакетов или бинарём с редиректом; Grype, Gitleaks, Semgrep — бинарями в `/usr/local/bin`.
   ```bash
   sudo apt-get install -y wget gnupg
   wget -qO - https://aquasecurity.github.io/trivy-repo/deb/public.key | sudo gpg --dearmor -o /usr/share/keyrings/trivy.gpg
   echo "deb [signed-by=/usr/share/keyrings/trivy.gpg] https://aquasecurity.github.io/trivy-repo/deb stable main" | sudo tee /etc/apt/sources.list.d/trivy.list
   sudo apt-get update && sudo apt-get install -y trivy
   trivy --version
   ```
   *Ожидаемо:* печатается версия Trivy. Если github/ghcr из РФ недоступны — скачай релизы через зеркало или собери локальный кэш БД на машине с доступом и перенеси (`trivy image --download-db-only` + каталог кэша); это законно и штатно [ПРОВЕРИТЬ актуальный способ переноса кэша].
2. **GIT01.** Устанавливаем Grype, Gitleaks, Semgrep тем же способом (install-скрипты или бинарь в `/usr/local/bin`), проверяем `grype version`, `gitleaks version`, `semgrep --version`.
3. **GIT01.** Поднимаем SonarQube Community Build в Docker Compose (отдельный каталог `/opt/sonar`, ~2 ГБ RAM, обязательный sysctl).
   ```bash
   sudo sysctl -w vm.max_map_count=262144
   docker run -d --name sonarqube -p 9000:9000 \
     -v sonar_data:/opt/sonarqube/data \
     sonarqube:community
   ```
   *Ожидаемо:* через 1–2 минуты `http://10.10.10.50:9000` открывается, логин `admin`; **сразу смени пароль**, затем создай токен для CI. [ПРОВЕРИТЬ тег образа `sonarqube:community` и требование внешней БД для Community Build]
4. **GitLab UI.** В проекте `portal` → Settings → CI/CD → Variables: добавь `SONAR_TOKEN`, `SONAR_HOST_URL=http://10.10.10.50:9000`, `HARBOR_HOST`, `HARBOR_USER`, `HARBOR_PASSWORD` (masked, protected).
5. **Репозиторий.** Добавь `.gitlab-ci.yml` из раздела 4 (пункт 11), `sonar-project.properties`, `.gitleaks.toml`, `.trivyignore`. Коммит и push в ветку `main`.
6. **Первый прогон.** Смотри Pipeline: `gitleaks` (секунды), `semgrep`, `trivy_fs` — параллельно, `build` собирает образ и пушит в HARBOR01, `image_scan` сканирует, `zap_baseline` бьёт по WEB01.
   *Ожидаемо:* `gitleaks` зелёный, `image_scan` **красный** — в `node:14-alpine` полно CRITICAL/HIGH.
7. **Разбор отчёта.** Скачай артефакт `sbom.json` и job-лог `image_scan`. Выпиши 3 самые критичные CVE, посмотри в Trivy, какая версия их фиксит.
8. **Починка.** Поменяй базовый образ на актуальный (`node:22-alpine` [ПРОВЕРИТЬ актуальный LTS-тег]), пересобери. Если гейт всё ещё красный — убедись, что не «лечишь» его добавлением всего в `.trivyignore`.
9. **Grype.** В job `image_scan` добавь `grype sbom:sbom.json --fail-on high` — сравни количество находок с Trivy. Расхождение нормально: разные базы и логика матчинга.
10. **SonarQube.** Прогони `sonar-scanner`, открой проект в UI, посмотри Quality Gate и раздел Security Hotspots. Настрой в UI условие Quality Gate: `Blocker Violations = 0`.
11. **ZAP.** Открой `zap.json` из артефактов, найди алерты по заголовкам (`X-Content-Type-Options`, `Content-Security-Policy`). Добавь в nginx на WEB01 `add_header X-Content-Type-Options nosniff;`, перезапусти nginx, перезапусти пайплайн — алерт должен исчезнуть.
12. **Журнал.** Запиши в `stand-log.md`: новые пакеты на GIT01, контейнер SonarQube на 9000, этапы пайплайна, файлы политик, изменения nginx на WEB01.

### Проверка

- [ ] `trivy --version`, `grype version`, `gitleaks version`, `semgrep --version` выполняются на GIT01.
- [ ] `http://10.10.10.50:9000` открывается, пароль `admin` сменён, токен создан.
- [ ] Пайплайн в GitLab проходит все 6 стадий.
- [ ] Первый прогон `image_scan` упал на HIGH/CRITICAL, после смены базового образа — зелёный.
- [ ] Артефакты `sbom.json`, `gitleaks.sarif`, `semgrep.sarif`, `zap.json` скачиваются из пайплайна.
- [ ] `grype` по SBOM выдаёт список, отличный от Trivy (и это объяснимо).
- [ ] В `.trivyignore` нет записей без комментария и даты.
- [ ] В `stand-log.md` появилась запись за урок 098.

### Сломай и почини

**Заявка.** «Пользователь: adm.student (дежурный по CI) / ВМ: GIT01 / Что написал: "Деплой встал: пайплайн красный на `gitleaks`. В логе секрет замазан, но job падает. В репозиторий прилетел коммит от подрядчика с конфигом. Релиз обещали клиенту сегодня" / Срочность: высокая».

**Воспроизведение.** В репозитории `portal` создай файл `config/harbor.env` с фейковым, но правдоподобным секретом вида `HARBOR_PASSWORD=Lab#2026-vektor` (учебный пароль стенда — то, что реально ловят сканеры), закоммить и запушь.

**Диагностика по порядку:**

1. Job-лог `gitleaks`: смотри `RuleID`, `File`, `Commit`. Если `File` — новый конфиг, а `Commit` — свежий, идём дальше.
2. `git log --oneline -5` и `git show <sha> --stat` — что именно и кем добавлено.
3. Локально: `gitleaks detect --source . --redact -v` на GIT01 — убедиться, что находка воспроизводится вне CI (значит, это не артефакт runner'а).
4. Проверь, не является ли это фикстурой: если путь `tests/fixtures/**` — правильное решение добавить путь в `allowlist`, но **не** отключать правило целиком.
5. Проверь, утёк ли секрет: запускался ли деплой с этим коммитом. Если да — секрет считается скомпрометированным.

**Решение:** удалить файл из рабочего дерева, добавить `config/*.env` в `.gitignore`, переписать историю (`git filter-repo` или BFG) [ПРОВЕРИТЬ установку `git filter-repo` в вашем дистрибутиве], force-push с предупреждением команды, **ротировать пароль** в Harbor и перенести его в Vault (урок 96). Для тестового примера — добавить путь в `allowlist` с комментарием.

**Ответ пользователю:** «Секрет в репозитории — блокирующая находка, гейт сработал правильно. Пароль от Harbor уже ротирую, из истории git удалю в течение часа, ключи раздам через Vault. Пайплайн станет зелёным после force-push; предупреди всех, у кого локальные копии ветки — им нужен re-fetch».

**Закрытие:** в тикете — ссылка на job, на коммит с фиксом, запись в `stand-log.md`, и отдельная задача в бэклог «включить pre-commit hook gitleaks на рабочих машинах разработчиков».

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** установлены Trivy, Grype, Gitleaks, Semgrep, `sonar-scanner`; запущен контейнер SonarQube Community Build, UI `http://10.10.10.50:9000`, том `sonar_data`; изменён `vm.max_map_count`.
- **GitLab:** в проекте `portal` добавлены `.gitlab-ci.yml` (стадии `secrets/sast/sca/build/image_scan/dast`), `.gitleaks.toml`, `.trivyignore`, `sonar-project.properties`; CI/CD-переменные `SONAR_TOKEN`, `SONAR_HOST_URL`, `HARBOR_HOST`, `HARBOR_USER`, `HARBOR_PASSWORD`.
- **HARBOR01 (10.10.10.55):** в проекте `vektor` появились образы `portal:<sha>`; пароль учётной записи push-пользователя ротирован.
- **WEB01 (10.10.20.10):** в конфиг nginx добавлен заголовок `X-Content-Type-Options: nosniff`.
- **Новых ВМ нет.** Адреса, домен `corp.vektor.lab` и учётные записи не менялись.

### Откат

VMware Workstation → правый клик по ВМ → Snapshot Manager → выбери `before-devops-098` → Revert. Откатывать на GIT01 и HARBOR01 (на HARBOR01 откат уберёт образы, запушенные после снапшота). Если снапшот не снимался: `docker rm -f sonarqube && docker volume rm sonar_data`, вернуть `.gitlab-ci.yml` через `git revert`, удалить пакеты сканеров. Git-история после `filter-repo` снапшотом не откатывается — только перезаписью ветки из бэкапа.

## 6. Ошибки новичков и безопасность

1. **Сканеры только на релизе.** → Уязвимость живёт в коде месяцами, фикс стоит в 10 раз дороже. → Ставь secrets/SAST/SCA на каждый merge request, image_scan — на каждый билд.
2. **Забыть `--exit-code 1` / `--error` / `--fail-on`.** → Скан «проходит», отчёт никто не читает, находки копятся. → Всегда явно задавай порог падения и проверяй его на намеренно уязвимом коммите.
3. **Лечить красный гейт исключениями.** → `.trivyignore` на 200 строк = сканер отключён. → Исключение только с причиной, автором и датой; пересматривать раз в квартал.
4. **Скан `latest`.** → Непонятно, что именно уехало в прод, результаты невоспроизводимы. → Тег = git SHA, сканируется ровно тот образ, который пушится.
5. **`allow_failure: true` на всё подряд.** → Пайплайн зелёный, дырки в проде. → Временный `allow_failure` только для нового сканера на 2–4 недели калибровки, с задачей в бэклоге.
6. **Доверие «нулю находок».** → «Trivy чист» ≠ «безопасно»: логические дыры, broken access control, уязвимости без CVE не ловятся. → Сканеры дополняют код-ревью и тесты, а не заменяют их.
7. **Секреты в артефактах.** → Отчёты с `--redact` забыли, токены уехали в открытый артефакт. → `--redact` по умолчанию, артефакты с находками — с ограниченным сроком хранения.
8. **DAST по проду без разрешения.** → Активный скан — это атака; можно уронить сервис и нарушить закон. → Активные сканы — только по своим стендам и с письменного согласования.

**Безопасность.** Всё, что мы делаем в лабораторной, безопасно, потому что цель — свой стенд `10.10.20.10`. В боевой среде: (1) ZAP full-scan без письменного согласия владельца системы — это уже инцидент, а не «тестирование»; (2) `--config=auto`, `semgrep ci` и облачные SaaS-сканеры отправляют код наружу — для кода с персональными данными это вопрос 152-ФЗ, поэтому в «Векторе» правильный выбор — self-hosted SonarQube и Semgrep CE с локальными правилами; (3) SonarQube с дефолтным `admin/admin` в сети — это подарок сканерам; (4) базы уязвимостей и образы тянутся из внешних реестров — в реальной работе их зеркалируют в свой Harbor, а не открывают GIT01 в интернет.

## 7. Вопросы на собеседовании

**1. Чем SAST, SCA и DAST отличаются и где им место в пайплайне?**

**Ответ.** SAST анализирует исходный код без запуска — ищет опасные конструкции и потоки данных (SQL-инъекция, небезопасный deserialization). SCA смотрит на зависимости: берёт версии пакетов из lock-файлов и сверяет с базами уязвимостей. DAST работает с уже запущенным приложением по HTTP и видит то, чего не видно в коде — конфигурацию сервера, заголовки, поведение авторизации. Порядок в пайплайне: секреты → SAST/SCA (параллельно, на каждый MR) → сборка → скан образа до push → DAST после деплоя на stage. DAST в прод-пайплайн не ставят: активные проверки могут изменить данные.

*Что хочет услышать интервьюер:* что ты понимаешь разные источники данных у инструментов и строишь пайплайн по принципу «дешевле и быстрее — раньше». *Красный флаг:* «DAST сканирует код, SAST — сайт» (перепутаны местами) или уверенность, что одного инструмента достаточно.

**2. Как решаешь, что сборка должна упасть, а что — только предупреждение?**

**Ответ.** Гейт строю по трём фильтрам: severity (обычно блокируют CRITICAL и HIGH), fixable (`--ignore-unfixed`, иначе гейт невозможно закрыть) и достижимость/эксплуатируемость (есть ли уязвимый код в реальном пути выполнения, есть ли публичный эксплойт). Дальше — политика: секреты блокируют всегда; CRITICAL в образе блокирует; HIGH без фикса — тикет с дедлайном; MEDIUM и ниже — бэклог. Плюс процесс исключений: файл ignore с причиной, автором и датой пересмотра, согласование с владельцем сервиса. И обязательная калибровка: новый сканер первые недели в режиме `allow_failure`, чтобы команда привыкла, а гейт не блокировал всё подряд.

*Что хочет услышать интервьюер:* наличие политики и процесса исключений, а не «включаю всё на максимум». *Красный флаг:* «всегда падаем на LOW» — так гейт отключат через неделю.

**3. Что такое SBOM и как его использовать при сканировании?**

**Ответ.** SBOM — машиночитаемый список компонентов артефакта с версиями и лицензиями, обычно CycloneDX или SPDX. Генерирую на этапе сборки (`trivy image --format cyclonedx`), подписываю Cosign и храню как артефакт релиза. Дальше сканирование отделяется от сборки: Grype или Dependency-Track проверяют SBOM по свежей базе хоть каждый день — когда выходит новая CVE, я узнаю об этом без пересборки, просто пересканировав SBOM. Это же основа для ответа на вопрос «что у нас в проде уязвимо» за минуту, а не за неделю.

*Что хочет услышать интервьюер:* связь SBOM с непрерывным мониторингом уязвимостей и supply chain. *Красный флаг:* «SBOM — это отчёт Trivy» (путает формат скана и спецификацию).

**4. Разработчик закоммитил и запушил секрет. Твои действия?**

**Ответ.** Секрет считаю скомпрометированным сразу: сначала ротация в источнике (Harbor, БД, API), потом всё остальное. Дальше — удалить из рабочего дерева, добавить путь в `.gitignore`, вычистить историю (`git filter-repo`), force-push с предупреждением команды, проверить, не использовался ли секрет на деплое (логи). Затем — профилактика: правило в Gitleaks, pre-commit hook, секреты только через Vault с динамическими учётками. Урок: сканер поймал — хорошо, но если секрет уже уехал, скан сам по себе ничего не исправил.

*Что хочет услышать интервьюер:* «сначала ротация, потом чистка истории». *Красный флаг:* «просто удалю коммит и всё» — секрет остаётся в истории и в форках.

**5. Сканеры замедляют пайплайн. Как ускоряешь?**

**Ответ.** Разношу по стадиям и запускаю параллельно через `needs: []`; самый дешёвый скан (Gitleaks) — первым, чтобы дешёвый отказ случился раньше. Кэширую: базу Trivy и Grype через `TRIVY_CACHE_DIR` и volume раннера, слои docker — через `--cache-from`. На MR гоняю быстрый инкрементальный набор (изменённые файлы, diff-скан Semgrep по `--baseline-commit`), полный скан — на `main` и по расписанию. DAST — только на stage и не на каждый коммит, а на merge в основную ветку. Сонар — с `sonar.qualitygate.wait`, но без полного переиндексирования там, где хватает инкрементального анализа.

*Что хочет услышать интервьюер:* понимание, что безопасность и скорость балансируются архитектурой пайплайна, а не отключением проверок. *Красный флаг:* «уберу сканеры, чтобы пайплайн был быстрым».

## 8. Источники (официальная документация)

- **trivy.dev** — официальная документация Trivy (Aqua Security). Разделы: *Docs → Scanners → Image / Filesystem / Configuration*, *Docs → Supply Chain → SBOM*. Поиск: `trivy image ignore-unfixed exit-code`, `trivy cyclonedx sbom`.
- **github.com/anchore/grype** — репозиторий и README Grype (Anchore). Раздел *Usage* и *Configuration*. Поиск: `grype sbom fail-on output formats`.
- **semgrep.dev** — сайт Semgrep. Разделы *Docs → Running rules* и *Rule registry*. Поиск: `semgrep config auto ci exit code sarif`.
- **docs.sonarsource.com** — документация SonarQube. Разделы *Server installation → Docker*, *Analyzing source code → SonarScanner*, *Quality gates*. Поиск: `sonarqube community build docker sysctl max_map_count`, `sonar.qualitygate.wait`. [ПРОВЕРИТЬ] актуальное название редакции Community Build и ограничения по анализу веток.
- **github.com/gitleaks/gitleaks** — README и раздел *Configuration* (`useDefault`, `allowlist`). Поиск: `gitleaks detect redact sarif config allowlist`. [ПРОВЕРИТЬ] изменения структуры конфига и подкоманд в последних версиях.
- **zaproxy.org** — сайт OWASP ZAP. Разделы *Documentation → ZAP in Docker* (baseline/full/API scan) и *Automate → Baseline scan*. Поиск: `zap-baseline.py docker exit codes`, `zap full scan authentication context`.
- **docs.gitlab.com** — документация GitLab CI/CD. Разделы *CI/CD YAML reference*, *CI/CD security scanning* (встроенные шаблоны GitLab). Поиск: `gitlab ci needs parallel stages security scanning`. [ПРОВЕРИТЬ] — какие сканеры GitLab включает штатно в вашей версии CE.
- **owasp.org** — OWASP Top Ten и OWASP ASVS, для выбора наборов правил. Поиск: `OWASP Top Ten 2021`.
- **nvd.nist.gov** и **github.com/advisories** — первоисточники данных об уязвимостях, для ручной проверки находок. Поиск: `CVE ID lookup NVD`.
- **docs.oasis-open.org** — спецификация SARIF. Поиск: `SARIF v2.1.0 OASIS specification`.
