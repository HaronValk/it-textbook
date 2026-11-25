# Platform Engineering: внутренняя платформа разработчика (IDP), Backstage, золотые пути, self-service

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку от Алексея Сидорова (склад), который на нашем DevOps-стенде играет роль первого «клиента» платформы:

> «Мне для складского сервиса нужен новый репозиторий, CI, образ в Harbor и деплой в кластер. Плюс чтобы документация и дешборд были в одном месте. Сколько ждать? Мне бы до пятницы собрать MVP».

И вот ты — единственный DevOps-инженер «Вектора» — сидишь и делаешь это руками: создаёшь проект в GitLab, копируешь `.gitlab-ci.yml` из старого репозитория (где он уже трижды форкнут), вписываешь переменные, заводишь проект в Harbor, правишь Dockerfile, потом объясняешь, почему пайплайн опять упал. Через полгода таких заявок пять команд делают одно и то же пятью разными способами, а ты не строишь платформу, а работаешь «человеком-API».

Platform Engineering — это ответ на это. Внутренняя платформа разработчика (IDP, Internal Developer Platform) — это **продукт для своих коллег-разработчиков**, который убирает ручную работу: разработчик заходит в портал, выбирает готовый шаблон («золотой путь»), нажимает кнопку — и получает репозиторий с линтерами, CI, сканерами, Dockerfile, манифестами, мониторингом и документацией. Портал тут — только витрина; суть в том, что за витриной лежат твои автоматизации.

После этой главы ты должен уметь: объяснить разницу между «платформой» и «порталом», поднять Backstage на стенде, завести Software Catalog через `catalog-info.yaml`, написать golden-path шаблон, который создаёт репозиторий в GitLab, и понимать, где у self-service границы безопасности.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ключевое разделение, которое на собеседовании проверяют первым делом:

- **IDP (платформа)** — это набор API, шаблонов, пайплайнов, инфраструктуры и процессов, которые дают разработчику self-service. Сюда входят: каталог сервисов, шаблоны репозиториев, CI-шаблоны, деплой (GitOps), секреты, наблюдаемость, политики.
- **Developer Portal** — это UI/фасад платформы. Backstage — именно портал (плюс каталог и scaffolder). Портал без платформы = красивая вики. Платформа без портала = набор скриптов, о которых никто не знает.

**Backstage** — open-source фреймворк портала разработчика, создан в Spotify, передан в CNCF (инкубирующий проект [ПРОВЕРИТЬ актуальный статус в cncf.io]). Это не готовый продукт, а конструктор на TypeScript/React + Node.js: фронтенд, бэкенд и набор плагинов. Ты собираешь из него свой портал.

Три кита Backstage:

1. **Software Catalog** — база сущностей (компоненты, системы, API, ресурсы, группы, пользователи). Источник правды — YAML-файлы `catalog-info.yaml`, лежащие в Git рядом с кодом сервиса. Бэкенд периодически их перечитывает. Это «инвентаризация» сервисов, которая не устаревает, потому что живёт в репозитории.
2. **Software Templates (Scaffolder)** — golden paths. Шаблон описывает шаги: взять скелет из Git, подставить параметры, создать репозиторий, зарегистрировать компонент в каталоге. Шаги — это «экшены» (`fetch:template`, `publish:github`, `catalog:register`, `gitlab:*` и т.д.).
3. **TechDocs** — документация как код: `mkdocs.yml` + `docs/*.md` в том же репозитории, отрисовывается прямо в портале.

Плюс плагины: Kubernetes (показывает поды сервиса), Argo CD/GitLab CI (статус деплоя), Grafana/Sentry (алерты), каталог API, cost-insights и т.д.

Схема стенда:

```text
Разработчик (браузер)
        │  HTTP :3000 (frontend)
        ▼
┌──────────────────────────────────────────────────────┐
│ GIT01 10.10.10.50 — Backstage                        │
│   frontend  React  :3000                             │
│   backend   Node   :7007                             │
│     ├─ catalog     ← catalog-info.yaml из GitLab     │
│     ├─ scaffolder  → GitLab API /api/v4              │
│     ├─ techdocs    ← docs/ + mkdocs.yml              │
│     └─ kubernetes  → k8s-cp01 :6443 (плагин)         │
│   БД: SQLite (dev) / PostgreSQL (prod)               │
└──────────────────────────────────────────────────────┘
      │                 │                  │
      ▼                 ▼                  ▼
 GitLab CE         HARBOR01           k8s-cp01
 10.10.10.50       10.10.10.55        10.10.10.61
 (репозитории,     (образы)           (кластер,
  runner, CI)                          деплой)
```

Порты и протоколы, которые надо помнить: Backstage — 3000/7007 по HTTP(S); GitLab API — `/api/v4` по 443/80; Kubernetes API — 6443; Harbor API — `/api/v2.0` по 443/80. Реестр образов, CI и кластер — те же, что в уроках 102 (импортозамещение, GitLab/Harbor) и 99 (безопасность K8s), мы их не переизобретаем, а **упаковываем в самообслуживание**.

Модель зрелости: сначала «тикет → делаю руками», потом «шаблон в GitLab (CI/CD Catalog)», потом «портал с шаблонами», потом «портал + политики + метрики платформы». Начинать надо с **тончайшей жизнеспособной платформы** (thinnest viable platform) — только то, что снимает реальную боль, а не 40 плагинов «чтобы было».

## 3. Аналогия из жизни

Представь строительный гипермаркет. Раньше, чтобы построить сарай, ты ехал на лесопилку за досками, потом на завод за гвоздями, потом искал того, кто знает, как правильно крепить. Теперь ты приезжаешь в один магазин: на входе стоит консультант, на стеллажах — готовые комплекты «гараж 3×6» с инструкцией, чертежами и списком всего нужного. Ты платишь и увозишь. Портал разработчика — это тот самый магазин с комплектами: каталог (что бывает), шаблоны (готовые комплекты с инструкцией) и консультант (ссылки на владельцев и документацию). А «золотой путь» — это комплект, который магазин проверил: он гарантированно собирается.

Аналогия перестаёт работать в одном месте: в магазине ты можешь купить что угодно и собрать по-своему, а на внутренней платформе за «собрать по-своему» отвечает служба безопасности и SRE — и они могут сказать «нет».

## 4. Практика и команды

**1. Создание приложения Backstage.** Официальный способ — генератор из npm.

```bash
cd ~/platform
npx @backstage/create-app@latest
# интерактивно спросит имя приложения: vektor-portal
```

`npx` скачивает пакет и запускает без глобальной установки. Внутри — каркас с примером каталога и одним шаблоном. Требования: Node.js 20 или 22 LTS [ПРОВЕРИТЬ актуальный диапазон в docs.backstage.io → «Getting Started»], Yarn 4 (генератор сам выставит `packageManager`).

**2. Запуск в dev-режиме.**

```bash
cd vektor-portal
yarn install
yarn dev
```

`yarn dev` поднимает одновременно фронтенд (`:3000`) и бэкенд (`:7007`). Типичный вывод:

```text
[0] Webpack ........ compiled successfully
[1] [backend]: Listening on :7007
[1] [catalog]: Catalog backend startup complete
```

Смотрим: нет ли красных `ERROR`, поднялся ли каталожный плагин. Проверка живости бэкенда:

```bash
curl -s http://127.0.0.1:7007/healthcheck
# {"status":"ok"}
```

**3. Конфиг приложения — `app-config.yaml` / `app-config.local.yaml`.** Локальные секреты — только в `app-config.local.yaml`, он в `.gitignore`.

```yaml
app:
  title: Портал разработчика Вектор
  baseUrl: http://10.10.10.50:3000

backend:
  baseUrl: http://10.10.10.50:7007
  listen:
    port: 7007

integrations:
  gitlab:
    - host: 10.10.10.50
      apiBaseUrl: http://10.10.10.50/api/v4
      baseUrl: http://10.10.10.50
      token: ${GITLAB_TOKEN}     # из переменной окружения, не в git

catalog:
  locations:
    - type: url
      target: http://10.10.10.50/platform/idp-demo/-/blob/main/catalog-info.yaml
      rules:
        - allow: [Component, System, API, Resource, Location, Group, User, Template]

techdocs:
  builder: local
  publisher:
    type: local
```

Точные имена полей `integrations.gitlab` (например, `apiBaseUrl`, `baseUrl`) стоит сверить с docs.backstage.io → «Integrations → GitLab» — [ПРОВЕРИТЬ] под свою версию.

**4. Описание компонента — `catalog-info.yaml`.**

```yaml
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: sklad-api
  description: Сервис складских остатков
  annotations:
    backstage.io/techdocs-ref: dir:.
    backstage.io/kubernetes-id: sklad-api
  tags: [python, sklad]
  links:
    - url: http://10.10.10.55/harbor/projects/sklad
      title: Образы в Harbor
spec:
  type: service
  lifecycle: production
  owner: group:default/gg-sklad
  system: sklad
```

`kind` — тип сущности; `owner` — ссылка на группу-владельца (`group:default/<имя>`, namespace `default` по умолчанию); `annotations` — машинные подсказки плагинам: TechDocs читать локально из `docs/`, Kubernetes-плагин искать ресурсы по label `app=sklad-api`.

**5. Группа и система — чтобы у сервиса был владелец.**

```yaml
apiVersion: backstage.io/v1alpha1
kind: Group
metadata:
  name: gg-sklad
spec:
  type: team
  profile:
    displayName: Команда склада
  children: []
---
apiVersion: backstage.io/v1alpha1
kind: System
metadata:
  name: sklad
spec:
  owner: group:default/gg-sklad
```

Смотрим: если владелец не найден, портал покажет предупреждение `owner not found` — частая причина «каталог вроде есть, но некрасивый».

**6. Золотой путь — шаблон.**

```yaml
apiVersion: scaffolder.backstage.io/v1beta3
kind: Template
metadata:
  name: golden-path-service
  title: Новый сервис (золотой путь)
  description: Репозиторий + CI + образ в Harbor + каталог + TechDocs
spec:
  owner: group:default/gg-it
  type: service
  parameters:
    - title: Параметры сервиса
      required: [name, owner]
      properties:
        name:
          type: string
          pattern: '^[a-z][a-z0-9-]{2,30}$'
        owner:
          type: string
          ui:field: OwnerPicker
  steps:
    - id: fetch
      name: Сгенерировать файлы из скелета
      action: fetch:template
      input:
        url: ./skeleton
        values:
          name: ${{ parameters.name }}
          owner: ${{ parameters.owner }}
    - id: publish
      name: Создать репозиторий в GitLab
      action: publish:gitlab
      input:
        repoUrl: 'http://10.10.10.50?owner=platform&repo=${{ parameters.name }}'
        defaultBranch: main
    - id: register
      name: Зарегистрировать в каталоге
      action: catalog:register
      input:
        repoContentsUrl: ${{ steps.publish.output.repoContentsUrl }}
        catalogInfoPath: /catalog-info.yaml
  output:
    links:
      - title: Репозиторий
        url: ${{ steps.publish.output.remoteUrl }}
```

`pattern` не даёт создать репозиторий с пробелами и кириллицей в имени. Экшен `publish:gitlab` приходит из модуля `@backstage/plugin-scaffolder-backend-module-gitlab` — **проверь, что он есть в твоей версии** [ПРОВЕРИТЬ: docs.backstage.io → «Built-in actions» / «GitLab»]. Если его нет, репозиторий создаётся отдельным шагом через GitLab API (`gitlab:project:create`-подобный или свой экшен), а шаблон продолжает генерировать файлы.

**7. Техдокс — минимум.**

```yaml
# mkdocs.yml в корне репозитория сервиса
site_name: sklad-api
nav:
  - Главная: index.md
plugins:
  - techdocs-core
```

`techdocs-core` — обязательный плагин, иначе Backstage не отрендерит страницу. Файл `docs/index.md` — обычный Markdown.

**8. Проверка каталога через API.**

```bash
curl -s 'http://10.10.10.50:7007/api/catalog/entities?filter=kind=component' | jq -r '.[].metadata.name'
# sklad-api
# example-website
```

`filter=kind=component` — фильтр по полю сущности. Если компонента нет — проблема в `catalog.locations` или в правах токена, а не в UI.

**9. Проверка токена GitLab до всякого Backstage.**

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  'http://10.10.10.50/api/v4/projects?membership=true' | jq -r '.[].path_with_namespace'
# platform/idp-demo
```

Если здесь пусто/401 — Backstage тоже не прочитает. Токену нужен scope `read_api` (для чтения) и `api` (для создания репозиториев шаблоном).

**10. Проверки качества в репозитории портала.**

```bash
yarn tsc          # проверка типов
yarn lint         # линт
yarn test         # тесты
yarn backstage-cli versions:bump   # обновить зависимости Backstage
```

Запускай перед каждым коммитом в репозиторий портала — это обычный TypeScript-проект, и он должен проходить те же проверки, что и остальной код.

**11. GitLab как «золотой путь без портала».** Если портал ещё не готов, самый дешёвый шаг — CI/CD-компоненты GitLab (CI/CD Catalog, доступен в свежих версиях GitLab CE [ПРОВЕРИТЬ версию в docs.gitlab.com]):

```yaml
# .gitlab-ci.yml сервиса
include:
  - component: 10.10.10.50/platform/ci-templates/build@1.0
    inputs:
      image_name: sklad-api
```

Смысл тот же: разработчик не пишет пайплайн с нуля, а подключает версионированный шаблон платформенной команды.

**12. Деплой портала в кластер (когда dev-режима мало).**

```bash
kubectl -n platform create secret generic backstage-secrets \
  --from-literal=GITLAB_TOKEN="$GITLAB_TOKEN"
kubectl -n platform get deploy,pods
```

В боевом варианте Backstage собирается в Docker-образ (`yarn build` + `yarn build-image`), ставится в k8s с PostgreSQL вместо SQLite и закрывается ingress'ом с SSO. Путь и имя образа сверяй с docs.backstage.io → «Deployment → Docker / Kubernetes» — [ПРОВЕРИТЬ].

## 5. Лабораторная работа

### Предусловия

- **GIT01 (10.10.10.50)** — GitLab CE + runner работают, есть доступ под своим админским аккаунтом GitLab (заводили в уроке 102). На нём же поднимаем Backstage.
- **HARBOR01 (10.10.10.55)** — Harbor работает, создан проект `sklad` (или используем `library`).
- **k8s-cp01 (10.10.10.61)** — кластер из урока 102 в рабочем состоянии (для проверки плагина/деплоя; если кластер не поднят — шаги 10–11 пропускаем, остальное работает).
- Ресурсы GIT01: минимум 4 ГБ свободной RAM и 5 ГБ диска. Если GitLab съел всё — временно выключи runner-контейнеры, либо поднимай Backstage на **UBU01 (10.10.10.30)** тем же способом (тогда все URL ниже замени на 10.10.10.30).
- Windows-часть стенда (DC01) не нужна — на DevOps-треке её может не быть.

### Снапшот

В VMware Workstation на **GIT01** и **HARBOR01**: меню VM → Snapshot → Take Snapshot, имя `before-devops-103`. Тот же снапшот — на k8s-cp01, если будешь трогать кластер.

### Шаги

1. **GIT01.** Проверь ресурсы: `free -h`, `df -h /`, `nproc`. Ожидаемо: ≥4 ГБ доступно. Если меньше — освободи память или переходи на UBU01.
2. **GIT01.** Поставь Node.js 20 LTS:
   ```bash
   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
   sudo apt install -y nodejs
   node -v && corepack enable
   ```
   Ожидаемо: `v20.x.x`. Если репозиторий NodeSource из РФ недоступен — скачай официальный тарбол с nodejs.org и распакуй в `/usr/local` (легально, бесплатно).
3. **GIT01.** Сгенерируй приложение:
   ```bash
   mkdir -p ~/platform && cd ~/platform
   npx @backstage/create-app@latest
   ```
   Имя: `vektor-portal`. Ожидаемо: дерево файлов + автоматический `yarn install` без ошибок.
4. **GIT01.** Запусти `cd vektor-portal && yarn dev`. Открой в браузере с хоста `http://10.10.10.50:3000`. Ожидаемо: стартовая страница Backstage с демо-каталогом (там уже есть `example-website`).
5. **GIT01, GitLab UI.** Создай группу `platform` и проект `idp-demo` (private). Добавь в корень `main` файл `catalog-info.yaml` из §4.4 (замени owner на `group:default/gg-it`, если групп ещё нет в каталоге) и `docs/index.md` + `mkdocs.yml`.
6. **GIT01, GitLab UI.** Создай Project/Personal Access Token со scope `read_api` (+ `api`), положи его в переменную окружения:
   ```bash
   echo 'export GITLAB_TOKEN="glpat-xxxxxxxx"' >> ~/.bashrc && source ~/.bashrc
   echo ${GITLAB_TOKEN:+token-set}
   ```
   Ожидаемо: `token-set`. Токен **не** коммитим в Git.
7. **GIT01.** Добавь блоки `integrations.gitlab` и `catalog.locations` из §4.3 в `app-config.local.yaml` внутри `vektor-portal`. Перезапусти `yarn dev`.
8. **GIT01.** Открой `http://10.10.10.50:3000/catalog`. Ожидаемо: компонент `sklad-api` появился, у него есть владелец и ссылка на Harbor.
9. **GIT01.** Создай шаблон: `templates/golden-path-service/template.yaml` (из §4.6) и папку `skeleton/` с файлами `catalog-info.yaml`, `README.md`, `.gitlab-ci.yml`, `mkdocs.yml`, `docs/index.md`, `k8s/deployment.yaml`. Зарегистрируй шаблон через UI «Create → Register Existing Component», указав URL на файл.
10. **GIT01.** В UI: Create → «Новый сервис (золотой путь)» → имя `sklad-api-test`, owner `gg-it` → Run. Ожидаемо: репозиторий `platform/sklad-api-test` создан, файлы на месте, компонент зарегистрирован в каталоге, в логах `yarn dev` — успешные шаги `fetch`/`publish`/`register`.
11. **GIT01 + GitLab.** В проекте `sklad-api-test` заведи CI/CD Variables `HARBOR_USER`, `HARBOR_PASSWORD` (masked). Запусти пайплайн. Ожидаемо: job собирает образ и пушит его в `10.10.10.55`.
12. **GIT01.** Проверь API каталога (`curl` из §4.8) и вкладку Docs у компонента. Запиши результат в `stand-log.md`.

### Проверка

- [ ] `curl -s http://127.0.0.1:7007/healthcheck` возвращает `{"status":"ok"}`.
- [ ] В Catalog виден компонент `sklad-api` с владельцем и ссылкой.
- [ ] `curl .../api/catalog/entities?filter=kind=component` содержит `sklad-api`.
- [ ] В списке шаблонов есть `golden-path-service`.
- [ ] Шаблон создал новый репозиторий в GitLab и зарегистрировал компонент.
- [ ] У нового компонента есть вкладка Docs с отрендеренным Markdown.
- [ ] В GitLab зелёный пайплайн, образ виден в Harbor.
- [ ] Токен GitLab отсутствует в `git status` / в истории коммитов.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

```text
Пользователь: sidorov (Алексей Сидоров) — первый клиент платформы
ВМ: GIT01 (10.10.10.50), Backstage
Что написал: «Запустил golden path, репозиторий создался,
но в каталоге компонента нет — пишет "Entity not found".
Мне до обеда надо показать демо.»
Срочность: средняя (есть обходной путь — задеплоить руками)
```

**Воспроизводим поломку.** На GIT01 убери токен из окружения и перезапусти бэкенд:

```bash
unset GITLAB_TOKEN
# в терминале с yarn dev: Ctrl+C, затем снова yarn dev
```

Проект `platform/sklad-api-test` — private, поэтому бэкенд получает 401 и не может прочитать `catalog-info.yaml`.

**Диагностика по порядку.**

1. **Что видит пользователь:** `Catalog → sklad-api-test` → `Entity not found`. Проверяем API, а не UI: `curl -s '.../api/catalog/entities?filter=kind=component' | jq -r '.[].metadata.name'` — компонента нет.
2. **Логи бэкенда** (терминал `yarn dev`): ищем строку вида `Unable to read location http://10.10.10.50/platform/.../catalog-info.yaml, 401 Unauthorized`. Это диагноз на 90%.
3. **Проверяем переменную в том же шелле, где живёт бэкенд:** `echo ${GITLAB_TOKEN:+token-set}`. Частая причина — токен экспортирован в другом окне/через `sudo`, а `yarn dev` запущен в старом.
4. **Проверяем сам токен напрямую:** `curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" 'http://10.10.10.50/api/v4/projects?membership=true'`. 401 — токен мёртв/отозван; 200, но нужного проекта нет — проблема в правах.
5. **GitLab UI:** User Settings → Access Tokens — scope, срок действия, не отозван ли.
6. **Проверяем `app-config.local.yaml`:** `host: 10.10.10.50` совпадает с хостом в URL каталога (не `gitlab.local`, не `localhost`), `token: ${GITLAB_TOKEN}` именно так, в фигурных скобках.

**Решение.** Выпустить новый токен со scope `read_api` + `api`, экспортировать в окружение, перезапустить `yarn dev`, в UI нажать Refresh у локации (или подождать цикл обновления каталога). Проверить: компонент в API и в UI.

**Ответ пользователю:** «Портал не читал твой репозиторий, потому что истёк токен доступа к GitLab. Токен обновил, компонент `sklad-api-test` в каталоге виден, ссылки и документация на месте. Демо можно показывать. Если снова пропадёт — пиши, посмотрю логи бэкенда».

**Закрытие заявки:** категория «платформа/портал», причина «истёк срок действия токена интеграции», действие «ротация токена + проверка каталога». Отдельным тикетом на себя — завести напоминание о ротации токенов (или перейти на токен через Vault из урока 96, а не в переменной окружения).

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** установлен Node.js 20 LTS; развёрнуто приложение Backstage `~/platform/vektor-portal`; заняты порты 3000 (frontend) и 7007 (backend); в GitLab появились группа `platform` и проекты `idp-demo`, `sklad-api-test`.
- **GitLab:** создан access-token для интеграции (scope `read_api`/`api`), CI-переменные `HARBOR_USER`, `HARBOR_PASSWORD` в проекте-скелете.
- **HARBOR01 (10.10.10.55):** в проекте появился образ, собранный пайплайном из golden-path шаблона.
- **Каталог Backstage:** сущности `sklad-api`, `group:default/gg-it`, `group:default/gg-sklad`, шаблон `golden-path-service`.
- **Персона sidorov** используется как «первый клиент платформы» (отдельной учётки разработчика на стенде нет).

### Откат

В VMware Workstation на GIT01 и HARBOR01: VM → Snapshot Manager → выбрать `before-devops-103` → Revert. Машины вернутся к состоянию до установки Node.js и Backstage; GitLab-проекты `platform/*` и образ в Harbor исчезнут вместе со снапшотом. Если откатываешь только Backstage — достаточно остановить `yarn dev` и удалить `~/platform/vektor-portal`, но каталог-состояние в GitLab так не откатится.

## 6. Ошибки новичков и безопасность

1. **Строить портал раньше боли.** → Ставим Backstage, три недели красивый UI, а разработчики всё равно пишут тебе в личку. → Сначала автоматизируй 2–3 самых частых запроса (репозиторий, CI, деплой), потом оборачивай в портал.
2. **Портал = платформа.** → В портале есть кнопка «создать сервис», но за ней ручной тикет в поддержку. → Портал должен вызывать API, а не человека.
3. **Секреты в `app-config.yaml` в git.** → Токен GitLab с правами `api` утекает в историю репозитория. → `app-config.local.yaml` в `.gitignore`, переменные окружения, в проде — Vault/Secrets Manager.
4. **Один супертокен на всё.** → Компрометация токена = доступ ко всем проектам GitLab. → Отдельный сервисный аккаунт, минимальные scope, ротация, раздельные токены на чтение и запись.
5. **Сущности без владельца.** → Через месяц в каталоге 80 компонентов, и никто не знает, чьи они. → `spec.owner` обязателен в шаблоне; регулярный отчёт по «сиротам».
6. **Шаблон без проверок.** → Golden path генерирует репозиторий без линтеров и сканеров, и «золотой путь» становится «грязным путём». → В скелет сразу: `.gitlab-ci.yml` с Trivy/Semgrep/Gitleaks (урок 98), Dockerfile с non-root, манифесты с ресурсами и NetworkPolicy (урок 99).
7. **Слишком много плагинов сразу.** → Портал падает/тормозит, обновления ломаются. → 5–7 плагинов, которые реально используются, остальное — по заявке.
8. **Портал без аутентификации.** → Любой в офисе видит внутренние репозитории и владельцев сервисов. → В проде только SSO (GitLab/OIDC), `guest`-провайдер выключен, портал не смотрит в интернет.

### Безопасность

- **Dev-режим Backstage не для продакшена.** `yarn dev` не имеет нормальной аутентификации и запускается от твоего пользователя. В боевой среде — сборка образа, k8s, ingress с TLS, SSO и RBAC.
- **Self-service = автоматическое создание ресурсов.** Шаблон с правами `api` может создать репозиторий, job и образ по нажатию кнопки. Значит, нужны guardrails: разрешённые имена/неймспейсы, лимиты, обязательные проверки, а для критичных действий — approval.
- **Данные о сотрудниках и сервисах.** SaaS-порталы (Port, Cortex, Roadie) удобны, но для компании с 152-ФЗ стоит оценить, куда уезжает каталог и данные сотрудников; self-hosted Backstage или Deckhouse — легальная и часто более уместная альтернатива.
- **«Широкие права ради лабы».** В этой лабораторной мы дали токену scope `api`. В реальной среде так делать нельзя: отдельный сервисный пользователь, минимальные права, ротация и журнал аудита.

## 7. Вопросы на собеседовании

**1. Что такое внутренняя платформа разработчика и чем она отличается от developer portal?**

Ответ: IDP — это совокупность self-service возможностей (каталог, шаблоны, CI/CD-шаблоны, GitOps-деплой, секреты, наблюдаемость, политики) плюс команда, которая ими владеет как продуктом. Портал (Backstage, Port, Deckhouse UI) — фасад и точка входа: каталог сервисов, документация, кнопки запуска шаблонов. Портал без автоматизаций — витрина; автоматизации без портала — скрипты, о которых никто не знает.
*Что хочет услышать интервьюер:* чёткое разделение «фасад vs возможности» и понимание, что платформа — продукт с пользователями и метриками.
*Красный флаг:* «Backstage — это и есть внутренняя платформа».

**2. Что такое golden path и почему это не «единственный разрешённый путь»?**

Ответ: golden path (paved road) — проверенный, поддерживаемый платформенной командой маршрут: шаблон репозитория + CI + безопасность + деплой + документация, с которым разработчик получает всё нужное за минуты. Это путь с наименьшим сопротивлением, а не запрет: уйти с него можно, но тогда команда сама отвечает за поддержку. Ценность — в снижении когнитивной нагрузки и в том, что «правильно» становится проще, чем «неправильно».
*Что хочет услышать интервьюер:* что golden path — про UX и снижение cognitive load, а не про запреты; что есть escape hatch.
*Красный флаг:* «Все обязаны использовать только шаблон, иначе блокируем доступ».

**3. Как Backstage узнаёт о сервисах? Что такое `catalog-info.yaml`?**

Ответ: через Software Catalog. Сущности (`Component`, `System`, `API`, `Resource`, `Group`, `User`, `Template`) описываются YAML-файлами `catalog-info.yaml` и лежат в Git рядом с кодом. В `app-config.yaml` в `catalog.locations` перечислены процессоры, которые эти файлы читают (URL GitLab, файл, GitHub), бэкенд периодически их перечитывает. `owner`, `system`, `dependsOn`, `providesApis` формируют граф зависимостей; `annotations` (`backstage.io/techdocs-ref`, `backstage.io/kubernetes-id`) подключают плагины.
*Что хочет услышать интервьюер:* «источник правды — Git, каталог не заполняется руками в UI».
*Красный флаг:* «Каталог ведётся вручную в админке/Excel».

**4. Как сделать self-service безопасным?**

Ответ: guardrails вместо запретов. Шаблоны параметризованы и валидированы (`pattern`, enum); создавать можно только в разрешённых группах/неймспейсах; в скелете сразу линтеры, сканеры (Trivy, Semgrep, Gitleaks), non-root образы, resource limits и NetworkPolicy; секреты — из Vault/CI-переменных, не в коде; сервисные токены с минимальными scope и ротацией; для критичных действий (прод-деплой, доступ к секретам) — approval и аудит; периодический обзор «сирот» в каталоге.
*Что хочет услышать интервьюер:* shift-left + политики как код + минимальные привилегии, и что безопасность встроена в шаблон, а не прикручена после.
*Красный флаг:* «Дадим разработчикам админский токен, потом разберёмся».

**5. Как понять, что платформа приносит пользу?**

Ответ: метрики adoption (сколько сервисов создано через шаблоны, доля репозиториев с golden-path CI), скорость (time from idea to first deploy, lead time, частота деплоев, MTTR — DORA), снижение нагрузки на платформенную команду (сколько ручных тикетов на провижининг закрыто), удовлетворённость (опрос разработчиков, NPS) и качество (доля пайплайнов со сканерами, число инцидентов из-за конфигурации). Плюс правило «тончайшей жизнеспособной платформы»: каждую фичу проверяем на реальную боль, а не на «звучит круто».
*Что хочет услышать интервьюер:* продуктовый подход и измеримость, а не «мы поставили Backstage».
*Красный флаг:* «Мы просто поставили портал, метрик нет, но все довольны».

## 8. Источники (официальная документация)

- **docs.backstage.io** — основной источник правды по Backstage. Разделы: «Getting Started», «Software Catalog», «Software Templates», «TechDocs», «Integrations → GitLab», «Deployment → Docker / Kubernetes», «Permissions». Поиск: `backstage software template golden path`, `backstage catalog-info.yaml owner`, `backstage integrations gitlab`.
- **backstage.io** — обзор проекта, блог, ссылки на CNCF. Поиск: `backstage developer portal overview`.
- **cncf.io** — статус Backstage в CNCF и материалы по platform engineering. Поиск: `CNCF platforms white paper`, `CNCF TAG App Delivery platform engineering`.
- **teamtopologies.com** — концепция platform team, cognitive load, Team Topologies. Поиск: `team topologies platform team as a product`.
- **docs.gitlab.com** — CI/CD Catalog и components (облегчённый golden path без портала), GitLab API. Поиск: `gitlab ci cd catalog components`, `gitlab api create project`.
- **deckhouse.io** — Deckhouse Kubernetes Platform как российская альтернатива со встроенным порталом/UI; уточни актуальный состав модулей [ПРОВЕРИТЬ]. Поиск: `deckhouse platform developer portal`.
- **kubernetes.io** — документация по развёртыванию сервисов и `kubectl`. Поиск: `kubernetes deployment create secret`.
- **nodejs.org** — LTS-версии Node.js и официальные сборки. Поиск: `node js lts release schedule`.
- Понятие «thin viable platform» и «paved road» чаще встречается в статьях и докладах, чем в официальной документации — точные первоисточники проверяй отдельно [ПРОВЕРИТЬ].
