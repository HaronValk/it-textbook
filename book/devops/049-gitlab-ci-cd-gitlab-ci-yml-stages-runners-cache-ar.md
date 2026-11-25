# GitLab CI/CD: .gitlab-ci.yml, stages, runners, cache, artifacts, include, rules, environments

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в техподдержку:**

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Открываю внутренний портал http://10.10.20.10/vektor-portal/ — там версия от прошлой недели. Мы просили поправить форму, правки не приехали. Срочность: средняя».
> **Статус:** открыта

Ты идёшь на GIT01 (10.10.10.50), открываешь GitLab и видишь: последний pipeline зелёный, все джобы прошли, но джоба `deploy:stage` в пайплайне вообще отсутствует. Кто-то поправил `rules` в `.gitlab-ci.yml`, и пайплайн «успешно» перестал выкатывать код. Ровно та ситуация, из-за которой CI/CD в реальной работе — это не «настроил один раз», а постоянное чтение чужого YAML.

После этой главы ты должен уметь: читать и писать `.gitlab-ci.yml` с нуля; понимать, как pipeline проходит от `git push` до выкатки; регистрировать и диагностировать runner'ы; осознанно выбирать между `cache` и `artifacts`; переиспользовать конфиг через `include`/`extends`; управлять запуском джоб через `rules`; фиксировать выкатки через `environments`. Всё это — база, на которой дальше строятся GitOps, DevSecOps и мультиокружения.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

GitLab CI/CD состоит из трёх частей: **сервер GitLab** (хранит репозиторий, парсит `.gitlab-ci.yml`, создаёт pipeline), **coordinator** (внутри GitLab — раздаёт джобы runner'ам) и **runner** (отдельный процесс, который реально выполняет команды).

```text
разработчик                GIT01 (10.10.10.50)                     runner
   |                             |                                    |
   | git push (22 или 443) ----> | GitLab читает .gitlab-ci.yml       |
   |                             | строит pipeline: stages -> jobs    |
   |                             |                                    |
   |                             | <--- long polling (443), runner    |
   |                             |      спрашивает "есть работа?"     |
   |                             | ----> job + CI_JOB_TOKEN ---------> |
   |                             |                                    | executor:
   |                             |                                    | docker run
   |                             | <--- streaming trace (лог) -------- |
   |                             | <--- artifacts: PUT /api/v4/... --- |
   |                             |      cache остаётся у runner'а     |
```

Ключевые свойства архитектуры:

- **Runner сам стучится наружу.** Входящие порты ему не нужны, только исходящий HTTPS к GitLab (443, в лабе — 80). Поэтому runner можно поставить за NAT, в другой сети, в облаке.
- **Выбор runner'а — по тегам.** У джобы есть `tags`, у runner'а — список тегов. Если пересечения нет, джоба висит в статусе `pending` с сообщением «no runners that match all of the job's tags».
- **Пайплайн — это граф.** По умолчанию джобы идут по стадиям: все джобы стадии `test` ждут завершения всех джоб стадии `build`. Ключевое слово `needs` ломает этот барьер и строит DAG (направленный ациклический граф) — джоба стартует, как только готовы её прямые зависимости.
- **`cache` и `artifacts` — разные вещи.** Artifacts GitLab забирает к себе и отдаёт другим джобам и человеку. Cache runner хранит у себя и использует только для ускорения.

| | `cache` | `artifacts` |
|---|---|---|
| Где живёт | у runner'а (локальный диск, S3, GCS) | в GitLab |
| Гарантия | нет, может исчезнуть | да, пока не истечёт `expire_in` |
| Зачем | ускорить (зависимости, pip/npm) | передать результат (tar.gz, отчёт тестов) |
| Кто скачивает | следующий job с тем же `cache:key` | джобы следующих стадий, пользователь, API |
| Видно в UI | нет | да, кнопка Browse/Download |

- **`include`** подмешивает YAML из другого файла — локального, из другого проекта, из официального шаблона. Это способ не копировать 50 одинаковых строк в каждый репозиторий.
- **`rules`** решает, попадёт ли джоба в пайплайн и с какими параметрами. Правила проверяются сверху вниз, **срабатывает первое совпавшее**, остальные игнорируются. Нет совпадений — джобы нет в пайплайне, и это не ошибка.
- **`environment`** — запись в GitLab «этот деплой-джоб выкатил вот такую версию вот на это окружение». Появляется в Operate → Environments с историей выкаток и кнопкой отката на предыдущий деплой.

## 3. Анализ из жизни

Представь кухню ресторана. Официант приносит заказ (это `git push`) — шеф читает техкарту блюда (это `.gitlab-ci.yml`) и раздаёт задачи поварам (runner'ы): кто-то режет овощи (стадия `build`), кто-то варит (стадия `test`), кто-то собирает тарелку (`deploy`). Каждый повар работает на своей станции с тегом «гриль» или «холодный цех» — если заказ повесили на «гриль», а такого повара сегодня нет, заказ висит и никто его не готовит, пока не выйдет нужный повар.

Заготовки (бульон, соус) стоят в холодильнике — это `cache`: удобно, ускоряет, но холодильник могут разморозить, и тогда всё равно варить заново. А готовое блюдо на подносе, которое несёт официант, — это `artifacts`: его нельзя «потерять», оно уходит гостю и записывается в чек.

Аналогия перестаёт работать в одном месте: на кухне повар видит весь заказ целиком, а runner получает только одну джобу и ничего не знает про остальные, кроме того, что ему явно передали через artifacts.

## 4. Практика и команды

**1. Регистрация runner'а (executor docker).** Токен берётся в UI: Admin (или Settings → CI/CD → Runners) → New instance runner → теги `docker, vektor`.

```bash
sudo gitlab-runner register \
  --non-interactive \
  --url "http://10.10.10.50" \
  --token "glrt-XXXXXXXXXXXXXXXXXXXX" \
  --executor "docker" \
  --docker-image "alpine:3.20" \
  --description "git01-docker" 
```

`--token` — новый authentication token (начинается с `glrt-`). В старых версиях использовался `--registration-token`; он объявлен устаревшим, в свежих версиях GitLab регистрация по нему не работает `[ПРОВЕРИТЬ версию вашего GitLab]`. `--executor` — чем выполнять: `docker` (контейнер на джобу), `shell` (прямо в ОС runner'а), `kubernetes`. Ожидаемый вывод: `Runner registered successfully. Feel free to start it, but if it's running already the config should be automatically reloaded!`

**2. Файл конфигурации runner'а** — `/etc/gitlab-runner/config.toml`:

```toml
concurrent = 2
check_interval = 3

[[runners]]
  name = "git01-docker"
  url = "http://10.10.10.50"
  token = "glrt-XXXXXXXXXXXXXXXXXXXX"
  executor = "docker"
  [runners.docker]
    image = "alpine:3.20"
    volumes = ["/cache"]
    pull_policy = ["if-not-present"]
    privileged = false
```

`concurrent` — сколько джоб runner выполняет параллельно (главный тормоз на слабом ПК). `check_interval` — как часто опрашивать GitLab, по умолчанию 3 секунды. `volumes = ["/cache"]` — тот самый «холодильник», общий для джоб. `privileged = false` — контейнер без root-возможностей; включать `true` стоит только если реально нужен Docker-in-Docker.

**3. Диагностика runner'а.**

```bash
sudo systemctl status gitlab-runner
sudo gitlab-runner list
sudo gitlab-runner verify
sudo journalctl -u gitlab-runner -n 50 --no-pager
```

`list` показывает зарегистрированные runner'ы и путь к конфигу. `verify` проверяет, что токен ещё действителен и GitLab отвечает. В `journalctl` ищи `Checking for jobs...` (опрос идёт) и `Job succeeded` — если опроса нет, runner не видит GitLab (сеть, DNS, сертификат).

**4. Минимальный `.gitlab-ci.yml`.**

```yaml
stages: [build, test]

build:app:
  stage: build
  image: alpine:3.20
  script:
    - echo "собираем $CI_PROJECT_NAME @ $CI_COMMIT_SHORT_SHA"
```

`stages` задаёт порядок; джоба без `stage` попадает в `test` по умолчанию. Проверка синтаксиса — Build → Pipeline editor → вкладка Validate, либо через API:

```bash
curl --request POST --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  --data-urlencode "content@.gitlab-ci.yml" \
  "http://10.10.10.50/api/v4/ci/lint"
```

`[ПРОВЕРИТЬ точный путь эндпоинта lint в вашей версии]`. Ответ — JSON с `valid: true` или списком ошибок с номерами строк.

**5. Переиспользование: `default` + `extends`.**

```yaml
default:
  image: python:3.12-slim
  tags: [docker, vektor]
  before_script:
    - python -V

.python-test:
  before_script:
    - pip install -r requirements-dev.txt

test:unit:
  extends: .python-test
  script:
    - python -m pytest --junitxml=report.xml
```

`default` применяется ко всем джобам, у которых нет своего значения. `.python-test` — скрытая джоба (начинается с точки, в пайплайн не попадает), она только шаблон. `extends` делает глубокое слияние: скаляры и словари мержатся, **массивы заменяются целиком** — это главная ловушка (унаследованный `script` затрётся своим).

**6. Кэш зависимостей.**

```yaml
variables:
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"

cache:
  key:
    files: [requirements-dev.txt]
  paths: [.cache/pip]
  policy: pull-push
```

`key:files` считает хеш от указанных файлов: поменялся `requirements-dev.txt` — новый кэш, не поменялся — переиспользуется старый. `policy: pull` ставится джобам, которым кэш нужен только на чтение (экономит время на выгрузке). Ограничение: до двух файлов в `key:files` `[ПРОВЕРИТЬ]`.

**7. Артефакты и отчёты.**

```yaml
build:package:
  stage: build
  script:
    - mkdir -p dist && tar czf dist/portal.tar.gz app public
  artifacts:
    name: "portal-$CI_COMMIT_REF_SLUG-$CI_COMMIT_SHORT_SHA"
    paths: [dist/]
    expire_in: 1 week
```

`name` — имя zip-архива в UI, поддерживает переменные. `expire_in` — срок жизни, по умолчанию 30 дней (настраивается на инстансе). `never` отключает автоочистку — так делать не стоит даже в лабе, кроме случаев, когда артефакт и есть релиз.

**8. Передача отчёта тестов в GitLab.**

```yaml
test:unit:
  script:
    - python -m pytest --junitxml=report.xml
  artifacts:
    when: always
    reports:
      junit: report.xml
    paths: [report.xml]
    expose_as: 'JUnit report'
```

`when: always` — забрать отчёт даже при падении тестов, иначе в UI не будет разбора упавших кейсов. `reports:junit` — не просто файл, а разобранный отчёт во вкладке Tests пайплайна. `expose_as` даёт ссылку прямо в MR.

**9. `needs` вместо ожидания всей стадии.**

```yaml
deploy:stage:
  stage: deploy
  needs: ["build:package", "test:unit"]
  dependencies: ["build:package"]
  script:
    - ls -la dist/
```

`needs` запускает джобу сразу после своих зависимостей, не дожидаясь остальных джоб стадии. `dependencies` сужает, **какие именно** артефакты скачать (по умолчанию скачиваются все артефакты всех предыдущих стадий — это частая причина медленных пайплайнов).

**10. `workflow:rules` и `include`.**

```yaml
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'

include:
  - local: 'ci/common.yml'
  - project: 'vektor/ci-templates'
    ref: v1.2.0
    file: '/templates/python.yml'
```

`workflow:rules` решает, создавать ли пайплайн вообще (если ни одно правило не совпало — пайплайна нет, и это не ошибка). `include:local` — файл в том же репозитории. `include:project` — файл из другого проекта; `ref` указывай тегом или коммитом, а не `main`, иначе чужая правка молча сломает твой пайплайн. Есть ещё `include:template` (официальные шаблоны GitLab) и `include:component` (CI-компоненты) `[ПРОВЕРИТЬ актуальные пути шаблонов и версию с компонентами]`.

**11. `rules` — управление запуском.**

```yaml
job:mr-only:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'

job:main-or-tag:
  rules:
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'
    - if: '$CI_COMMIT_TAG'

job:docs-change:
  rules:
    - changes: [docs/**/*]
      when: on_success
    - when: never
```

Сравнивай ветку с `$CI_DEFAULT_BRANCH`, а не с жёстким `main` — иначе после переименования ветки джоба тихо исчезнет. `when: never` — явный запрет. `changes` на ветке сравнивает с предыдущим коммитом, в MR — с целевой веткой.

**12. Окружения.**

```yaml
deploy:prod:
  stage: deploy
  rules:
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'
      when: manual
      allow_failure: false
  environment:
    name: prod
    url: http://10.10.20.10/vektor-portal/
  resource_group: prod
  script:
    - echo "выкатываем $CI_COMMIT_SHORT_SHA"
```

`environment:name` создаёт окружение в Operate → Environments. `when: manual` делает джобу кнопкой; по умолчанию manual-джоба не блокирует пайплайн (`allow_failure: true`), `allow_failure: false` превращает её в обязательный шлагбаум. `resource_group` не даёт двум деплоям в одно окружение выполняться одновременно.

**13. Проверка через API.**

```bash
curl --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "http://10.10.10.50/api/v4/projects/1/pipelines?ref=main&per_page=3"
```

Быстрый способ посмотреть статусы без браузера — удобно в скриптах и на слабом ПК, где UI тормозит.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GIT01** (10.10.10.50) — Ubuntu Server, установлен GitLab CE (omnibus), доступен по `http://10.10.10.50`, есть root-пароль. Пакет `gitlab-runner` установлен, но не зарегистрирован.
- **WEB01** (10.10.20.10) — nginx, доступен по HTTP из DMZ. С GIT01 до WEB01 есть маршрут через GW01 (10.10.10.3 → 10.10.20.1). Если маршрута нет — сначала тема про маршрутизацию, дальше лаба не поедет.
- **UBU01** (10.10.10.30) — рабочая Linux-машина, откуда ты делаешь `git push` и генерируешь ключ.
- Учётные записи на Linux — те же, под которыми ты работаешь на UBU01/WEB01.
- Docker на GIT01 установлен (нужен для executor `docker`). Если Docker Hub из РФ недоступен по rate limit — настрой mirror в `/etc/docker/daemon.json` или используй `image: docker.io/library/python:3.12-slim` из кэша `[ПРОВЕРИТЬ доступность в вашей сети]`.
- Если GitLab или runner ещё не установлены — это тема установки GitLab; без них лаба не имеет смысла.

### Снапшот

На GIT01 и WEB01 (VMware Workstation → VM → Snapshot → Take Snapshot):

```text
имя снапшота: before-devops-049
```

### Шаги

1. **GIT01.** Проверь, что GitLab жив: `sudo gitlab-ctl status` и `curl -I http://10.10.10.50/users/sign_in`. Ожидаемо: все сервисы `run`, HTTP 200/302.
2. **GitLab UI.** Создай группу `vektor` и в ней проект `vektor-portal` (Visibility: Private). Ожидаемо: пустой репозиторий с подсказками по `git clone`.
3. **GitLab UI.** Admin → CI/CD → Runners → New instance runner. Теги: `docker`, `vektor`. Скопируй токен `glrt-...`. Ожидаемо: runner в статусе «never contacted».
4. **GIT01.** Зарегистрируй runner командой из п.4.1 (с `--url http://10.10.10.50`). Ожидаемо: `Runner registered successfully`, в UI статус зелёный.
5. **GIT01.** `sudo systemctl restart gitlab-runner && sudo gitlab-runner verify`. Ожидаемо: `is alive`.
6. **UBU01.** Сгенерируй ключ для деплоя: `ssh-keygen -t ed25519 -f ~/.ssh/vektor_portal_deploy -N ""`. Публичную часть положи на WEB01: `ssh-copy-id -i ~/.ssh/vektor_portal_deploy.pub <твой_пользователь>@10.10.20.10`. Ожидаемо: `ssh -i ~/.ssh/vektor_portal_deploy <ты>@10.10.20.10` заходит без пароля.
7. **WEB01.** `sudo mkdir -p /var/www/html/vektor-portal && sudo chown $USER:$USER /var/www/html/vektor-portal`. Ожидаемо: каталог есть, ты в нём можешь писать.
8. **GitLab UI → Settings → CI/CD → Variables.** Добавь `SSH_PRIVATE_KEY` (тип **File**, содержимое — приватный ключ из п.6, Visibility: Masked) и `DEPLOY_USER` (твой пользователь на WEB01). Ожидаемо: две переменные в списке.
9. **UBU01.** Клонируй проект и положи файлы: `app/main.py` (`def version(): return "1.0"`), `tests/test_main.py` (проверка `version() == "1.0"`), `requirements-dev.txt` (`pytest`), `public/index.html`, `ci/common.yml` с содержимым:

```yaml
.common:
  before_script:
    - echo "common: pipeline=$CI_PIPELINE_ID job=$CI_JOB_NAME ref=$CI_COMMIT_REF_NAME"
```

10. **GitLab UI.** Создай второй проект `vektor/ci-templates`, в ветке `main` файл `templates/python.yml`:

```yaml
.python-test:
  image: python:3.12-slim
  before_script:
    - pip install -r requirements-dev.txt
```

Ожидаемо: файл виден в репозитории.

11. **UBU01.** Положи `.gitlab-ci.yml`:

```yaml
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'

default:
  image: python:3.12-slim
  tags: [docker, vektor]

stages: [build, test, deploy]

variables:
  PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip"

cache:
  key:
    files: [requirements-dev.txt]
  paths: [.cache/pip]

include:
  - local: 'ci/common.yml'
  - project: 'vektor/ci-templates'
    ref: main
    file: '/templates/python.yml'

build:package:
  stage: build
  extends: .common
  script:
    - mkdir -p dist
    - tar czf dist/portal.tar.gz app public
    - echo "$CI_COMMIT_SHORT_SHA" > dist/VERSION
  artifacts:
    name: "portal-$CI_COMMIT_REF_SLUG-$CI_COMMIT_SHORT_SHA"
    paths: [dist/]
    expire_in: 1 week

test:unit:
  stage: test
  extends: .python-test
  needs: [build:package]
  script:
    - python -m pytest --junitxml=report.xml
  artifacts:
    when: always
    reports:
      junit: report.xml
    paths: [report.xml]
    expire_in: 1 week

test:smoke:
  stage: test
  script:
    - test -f dist/portal.tar.gz

deploy:stage:
  stage: deploy
  needs: [build:package, test:unit]
  dependencies: [build:package]
  resource_group: stage
  environment:
    name: stage
    url: http://10.10.20.10/vektor-portal/
    on_stop: stop:stage
  rules:
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'
  before_script:
    - apt-get update -qq && apt-get install -y -qq openssh-client rsync
    - mkdir -p ~/.ssh && chmod 700 ~/.ssh
    - cp "$SSH_PRIVATE_KEY" /tmp/id && chmod 600 /tmp/id
    - ssh-keyscan -H 10.10.20.10 >> ~/.ssh/known_hosts 2>/dev/null
  script:
    - rsync -az -e "ssh -i /tmp/id" dist/portal.tar.gz "$DEPLOY_USER@10.10.20.10:/var/www/html/vektor-portal/"
    - ssh -i /tmp/id "$DEPLOY_USER@10.10.20.10" "cd /var/www/html/vektor-portal && tar xzf portal.tar.gz"

deploy:prod:
  stage: deploy
  needs: [deploy:stage]
  environment:
    name: prod
    url: http://10.10.20.10/vektor-portal/
  rules:
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'
      when: manual
      allow_failure: false
  script:
    - echo "прод-выкатка $CI_COMMIT_SHORT_SHA (учебно, тот же каталог)"

stop:stage:
  stage: deploy
  environment:
    name: stage
    action: stop
  rules:
    - if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'
      when: manual
  script:
    - echo "окружение stage остановлено (учебно)"
```

12. **UBU01.** `git add -A && git commit -m "ci: pipeline" && git push -u origin main`. Ожидаемо: в Build → Pipelines появляется pipeline, джобы `build:package` → `test:unit` / `test:smoke` → `deploy:stage`.
13. **GitLab UI.** Открой `test:unit` → вкладка Tests (разобрался JUnit), вернись в pipeline → Browse artifacts у `build:package` → скачай `dist/`. Ожидаемо: архив с `portal.tar.gz` и `VERSION`.
14. **Проверка выкатки.** С UBU01: `curl -s http://10.10.20.10/vektor-portal/VERSION`. Ожидаемо: SHA последнего коммита.
15. **GitLab UI.** Operate → Environments → `stage` и `prod`. Ожидаемо: у `stage` — история деплоя, у `prod` — кнопка Run для `deploy:prod`. Нажми её, посмотри статус.

### Проверка

- [ ] `sudo gitlab-runner list` на GIT01 показывает `git01-docker`, `verify` отвечает `is alive`.
- [ ] Pipeline на `main` зелёный, все джобы в статусе `passed`.
- [ ] В логе `build:package` есть строка из `.common` — значит `include:local` и `extends` сработали.
- [ ] В логе `test:unit` есть `pip install` и строка pytest с `1 passed`.
- [ ] Вкладка Tests у `test:unit` показывает один кейс.
- [ ] Артефакт `portal-...zip` скачивается и содержит `portal.tar.gz` и `VERSION`.
- [ ] `curl http://10.10.20.10/vektor-portal/VERSION` отдаёт SHA последнего коммита.
- [ ] В Operate → Environments есть `stage` со статусом `available` и `prod` с ручной джобой.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Портал по адресу http://10.10.20.10/vektor-portal/ показывает старую версию, правки не приезжают третий день. Срочность: средняя»

**Как воспроизвести поломку.** На UBU01 открой `.gitlab-ci.yml`, в джобе `deploy:stage` поменяй правило на `- if: '$CI_COMMIT_BRANCH == "master"'`, закоммить и запушь в `main`. Пайплайн станет зелёным, но деплой-джобы в нём не будет.

**Диагностика по порядку:**

1. С UBU01: `curl -s http://10.10.20.10/vektor-portal/VERSION` — видим старый SHA. Проблема на стороне выкатки, а не nginx.
2. GitLab → Repository → Commits: последний коммит в `main` есть, SHA другой. Значит код в репозитории новее, чем на WEB01.
3. Build → Pipelines для `main`: последний pipeline `passed`. Коммит совпадает с последним.
4. Открываем граф пайплайна — **джобы `deploy:stage` нет вообще**. Это принципиальный признак: не «упала», а «не создана» → виноваты `rules`/`workflow`, а не скрипт.
5. Repository → `.gitlab-ci.yml` → History: видим коммит «fix rules», где ветка заменена на `master`.
6. Build → Pipeline editor → Validate → Simulate pipeline с `main`: `deploy:stage` не появляется. Подтверждаем причину без запуска.

**Решение.** Вернуть `$CI_DEFAULT_BRANCH` (или `main`), закоммитить, дождаться нового пайплайна, убедиться, что `deploy:stage` появился и прошёл, повторить `curl`. Дополнительно — не допускать рецидива: вынести ветку в переменную `DEPLOY_BRANCH` и сверять с `$CI_DEFAULT_BRANCH`.

**Ответ пользователю:**

> Иван, добрый день. Правки в репозитории были, но автоматическая выкатка на портал не запускалась из-за ошибки в конфигурации пайплайна — деплой-джоба не создавалась вовсе. Исправил конфигурацию, выкатка прошла, проверьте пожалуйста страницу (Ctrl+F5). Причина записана, повтор не ожидается.

**Закрытие заявки:** статус «Решено», в комментарий — ссылка на pipeline и на коммит с исправлением. Запись в `stand-log.md`.

### Что изменилось на стенде

- GIT01: зарегистрирован runner `git01-docker`, теги `docker, vektor`, executor `docker`, `concurrent = 2`.
- GitLab: группа `vektor`, проекты `vektor-portal` и `vektor/ci-templates` (ветка `main`).
- GitLab: CI/CD variables `SSH_PRIVATE_KEY` (File, masked) и `DEPLOY_USER`.
- UBU01: ключевая пара `~/.ssh/vektor_portal_deploy{,.pub}`.
- WEB01: каталог `/var/www/html/vektor-portal`, в `~/.ssh/authorized_keys` добавлен публичный ключ деплоя.
- GitLab: окружения `stage` и `prod` с историей выкаток.
- Артефакты пайплайна хранятся 1 неделю.

### Откат

VMware Workstation → выбери ВМ → Snapshot Manager → `before-devops-049` → Revert. Откатывать нужно **обе** ВМ (GIT01 и WEB01), иначе получишь GitLab без ключа на WEB01 или наоборот. После откока удали на UBU01 локальную копию репозитория или переключи её на новый remote, чтобы не пушить в несуществующий проект.

## 6. Ошибки новичков и безопасность

- **Путать `cache` и `artifacts`** → деплой падает на «нет файла», потому что кэш — не гарантия и живёт только у одного runner'а. Правильно: то, без чего следующая джоба не работает, — всегда `artifacts`.
- **Смешивать `only/except` и `rules` в одном файле** → пайплайн ведёт себя непредсказуемо, часть джоб не создаётся. Правильно: только `rules`, `only/except` — устаревший синтаксис.
- **Жёсткий `main` в `rules`** → после переименования ветки деплой тихо исчезает (ровно наша поломка). Правильно: `$CI_DEFAULT_BRANCH`.
- **`needs` без `dependencies`** → джоба тянет все артефакты всех стадий, пайплайн раздувается. Правильно: явно указывать, что нужно.
- **`when: manual` без `allow_failure: false`** → прод-деплой «не обязателен», пайплайн зелёный, а на проде старая версия. Правильно: для прода — `allow_failure: false`.
- **`include:project` с `ref: main`** → чужой коммит в шаблонах ломает все пайплайны сразу. Правильно: тег или SHA.
- **Кэш с ключом без `files`** → все ветки делят один кэш и получают чужие зависимости. Правильно: `key:files` по lock-файлу.
- **`shell` executor на боевом сервере** → любой, кто может пушить в репозиторий, выполняет команды от пользователя `gitlab-runner` на этой машине. Правильно: `docker` executor без `privileged`, отдельный хост.

**Безопасность.** Ключ деплоя в CI-переменной — учебное упрощение: `Masked` скрывает значение в логах, но не защищает от того, кто имеет доступ к настройкам проекта. В боевой среде — короткоживущие токены, OIDC-федерация с облаком, Vault/secret manager, `Protected`-переменные только для защищённых веток. Никогда не печатай переменные целиком (`echo $SSH_PRIVATE_KEY`), не храни секреты в `.gitlab-ci.yml` и не ставь `privileged = true` без реальной необходимости — это фактически root на хосте. `ssh-keyscan` в лабе отключает проверку подлинности хоста по сути; в продакшене known_hosts должен быть зафиксирован заранее.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Чем `cache` отличается от `artifacts` и что вы используете для передачи собранного бинарника в деплой?**

Ответ: `cache` — механизм ускорения, хранится у runner'а (локально или в S3), не гарантирован, может быть общим для джоб с одинаковым ключом, в UI не виден. `artifacts` — результат работы, GitLab его забирает к себе, он доступен следующим стадиям, скачивается человеком и через API, живёт до `expire_in`. Для бинарника в деплой — только `artifacts`, с `needs`/`dependencies` для точечной передачи. Для зависимостей (pip/npm/maven) — `cache` с ключом по lock-файлу.
*Что хочет услышать:* чёткое разделение «ускорение vs результат» и понимание, что кэш не гарантирован.
*Красный флаг:* «это одно и то же, просто разные названия» или «бинарник в кэш, так быстрее».

**2. Джоба висит в `pending`. Ваши действия?**

Ответ: смотрю сообщение на странице джобы — обычно «no runners that match all of the job's tags» или «no runners online». Дальше: Settings → CI/CD → Runners (или Admin → Runners) — есть ли активный runner с нужными тегами, не истёк ли токен, не отключён ли runner для проекта. На хосте runner'а: `systemctl status gitlab-runner`, `gitlab-runner verify`, `journalctl -u gitlab-runner`, `concurrent` в `config.toml` — если все слоты заняты, джоба тоже ждёт. Проверяю сеть до GitLab и валидность сертификата.
*Что хочет услышать:* системный порядок «сообщение → UI → хост → сеть», а не «перезапустить runner».
*Красный флаг:* сразу «перезагружу сервер GitLab».

**3. Как работают `rules` и почему первое совпадение важно?**

Ответ: `rules` — упорядоченный список условий (`if`, `changes`, `exists`, `variables`), проверяется сверху вниз, срабатывает первое совпавшее, остальные игнорируются; атрибуты (`when`, `allow_failure`, `variables`) берутся из него. Если ни одно не совпало и нет финального `when: never`/`when: always` — джоба в пайплайн не попадёт, и это не ошибка. Поэтому общие правила ставят ниже, специфичные выше, а `when: never` — явно.
*Что хочет услышать:* «первое совпадение выигрывает» и «джоба может просто не создаться».
*Красный флаг:* «правила объединяются по И» или путаница с `only/except`.

**4. Как вы переиспользуете CI-конфигурацию между десятком репозиториев?**

Ответ: три уровня. Внутри одного файла — `extends` и YAML-анкоры/`!reference`. Между проектами — `include:project` с фиксированным `ref` (тег/SHA), шаблоны в отдельном репозитории `ci-templates`. Совсем общее — CI-компоненты (versioned, с inputs) или официальные шаблоны GitLab. Плюс `default` для общих `image`/`tags`. Главное правило: шаблоны версионируются, `ref: main` запрещён.
*Что хочет услышать:* понимание версионирования шаблонов и границ переиспользования.
*Красный флаг:* «копирую .gitlab-ci.yml в каждый проект».

**5. Что такое `environment` и как с его помощью откатиться?**

Ответ: `environment` — декларация окружения в GitLab: имя, URL, история деплоев, кто и какой SHA выкатил. Джоба с `environment` создаёт запись в Operate → Environments, поддерживает `on_stop`/`action: stop`, `auto_stop_in`, `resource_group` для сериализации. Откат: в окружении есть список деплоев — можно перезапустить джобу на предыдущем коммите (Re-deploy) или запустить пайплайн на старом SHA. Это «ручной» откат; автоматический — уже отдельная тема (canary, rollback по метрикам, Argo Rollouts).
*Что хочет услышать:* что environment — это учёт и история, а не магия, и что откат возможен через повторный деплой известного SHA.
*Красный флаг:* «environment сам откатывает приложение».

## 8. Источники (официальная документация)

- **GitLab Docs** — домен `docs.gitlab.com`. Раздел «CI/CD YAML reference» — полный список ключевых слов. Поиск: `gitlab ci yaml reference`.
- **GitLab Docs** — «CI/CD pipelines»: stages, `needs`, DAG. Поиск: `gitlab ci cd pipeline architecture stages needs`.
- **GitLab Docs** — «Runners»: executors, registration, `config.toml`, tags. Поиск: `gitlab runner executors register config.toml`.
- **GitLab Docs** — «Caching in GitLab CI/CD». Поиск: `gitlab ci caching key files policy`.
- **GitLab Docs** — «Job artifacts» и «Unit test reports». Поиск: `gitlab ci artifacts reports junit expire_in`.
- **GitLab Docs** — «Include» и «CI/CD components». Поиск: `gitlab ci include project ref component`.
- **GitLab Docs** — «Choose when jobs run» (`rules`, `workflow:rules`). Поиск: `gitlab ci rules workflow rules if changes`.
- **GitLab Docs** — «Environments and deployments». Поиск: `gitlab ci environments deployments on_stop resource_group`.
- **GitLab Docs** — «API resources: CI Lint, Pipelines». Поиск: `gitlab api ci lint pipelines endpoint` `[ПРОВЕРИТЬ точный путь эндпоинта для вашей версии]`.
