# Мультиокружения: dev/stage/prod, promote-политики, управление конфигурациями и секретами

## 1. Введение (зачем это нужно в реальной работе)

**Тикет в helpdesk «Вектора»:**

> **Пользователь:** sidorov (Алексей Сидоров, склад)
> **ВМ:** WIN11-CL01
> **Что написал:** «Вчера в форме заявок на перевозку был новый баннер и поле "Приоритет", а сегодня всё по-старому. И ещё утром сайт открывался, а сейчас отдаёт 502. Нам отгружать, разберитесь.»
> **Срочность:** Высокая

Разбираем: часть 1 — Сидоров просто попал на другой контур. Новую форму показывали на тестовом стенде (`stage`), а боевой (`prod`) ещё никто не обновлял, потому что обновление туда не «промоутнули». Часть 2 — кто-то выкатил в прод образ, который не проходил проверку, и под не поднялся.

Это будни DevOps. Пока у вас одна среда, любой эксперимент — это риск уронить работающий сервис. Как только сред три (dev/stage/prod) и между ними есть управляемый «лифт» с правилами, вы можете катить часто и не бояться. После этой главы вы будете уметь: развернуть три окружения из одного репозитория, настроить политику продвижения артефакта (promote-политику), отделить конфигурацию от образа и разложить секреты так, чтобы они не лежали в Git открытым текстом.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ключевая идея, она же «build once, deploy many» (из 12-factor): **образ собирается ровно один раз**, а в следующие окружения продвигается **тот же самый образ, тот же digest** (sha256). Если вы пересобираете образ для прода — вы проверяли на stage одно, а катите другое.

Что вообще отличает окружения друг от друга:

1. **Инфраструктура** (сколько реплик, какие лимиты CPU/RAM, есть ли автомасштабирование).
2. **Конфигурация** (адреса внешних сервисов, уровни логов, фича-флаги).
3. **Секреты** (пароли, токены) — у каждого окружения свои.
4. **Данные** (в dev — синтетика, в prod — реальные персональные данные).
5. **Права доступа** (кто может деплоить).

Чего отличаться **не должно**: код и версия образа на момент проверки.

```text
           GIT01 (10.10.10.50) — GitLab CE
                     │  git push → pipeline
                     ▼
   ┌──────────────────────────────┐
   │ build: docker build + push   │
   └──────────────┬───────────────┘
                  │ образ + digest (sha256)
                  ▼
        HARBOR01 (10.10.10.55) — реестр, immutable tag
                  │
    ┌─────────────┼──────────────────────┐
    ▼             ▼                      ▼
 vektor-dev   vektor-stage          vektor-prod
 ns на k8s    ns на k8s             ns на k8s
 1 репл.      2 репл.               3 репл.
 авто         авто/smoke, потом      manual + approval,
 после merge  manual-кнопка          protected environment
    │             │                      │
    └──────► ArgoCD (argocd ns) следит за Git и синхронизирует ◄──┘
```

Порты, которые понадобятся: GitLab — 80/443 и 22 (SSH), Harbor — 443 (push/pull через `docker login`), API Kubernetes — 6443, kubelet — 10250, NodePort-диапазон — 30000–32767, UI ArgoCD — обычно через `kubectl port-forward`.

В Kubernetes «окружение» удобно делать **неймспейсом** (`vektor-dev`, `vektor-stage`, `vektor-prod`) — для учебного стенда этого достаточно. В бою prod часто выносят в отдельный кластер, чтобы падение dev не съело квоты прода. Запомните: **изоляция неймспейсом — это про имена и квоты, а не про безопасность**; по умолчанию поды из разных неймспейсов видят друг друга по сети.

Конфигурация приходит в под двумя путями: `ConfigMap` (несекретное) и `Secret` (секретное). Оба подключаются как переменные окружения (`envFrom`) или как файлы (volume). **Secret в Kubernetes — это не шифрование**, а всего лишь base64: `echo aGVsbG8= | base64 -d`. Поэтому «Secret лежит в Git в base64» = пароль в открытом виде.

Порядок приоритета источников конфигурации (упрощённо): значение по умолчанию в коде → переменная из `ConfigMap` → переменная из `Secret` (перекрывает) → то, что вручную вписали в манифест (так делать нельзя).

## 3. Аналогия из жизни

Представьте авиакомпанию. **dev** — это тренажёр: можно сколько угодно ронять самолёт, никто не пострадает, но и летать на тренажёре пассажиров нельзя. **stage** — испытательный полёт: тот же борт, та же заправка, те же приборы, но на борту только экипаж и инженеры, а маршрут повторяет реальный. **prod** — регулярный рейс с пассажирами: тут ошибка стоит дорого, поэтому допуск подписывает отдельный человек, а не тот, кто крутил гайки.

Promote-политика — это процедура допуска: один и тот же борт (образ) проходит тренажёр, потом облёт, и только потом получает разрешение на рейс. Никто не «пересобирает» самолёт заново перед вылетом.

Секреты — это коды доступа к сейфу: они лежат в сейфе (Vault/Secret-хранилище), а не на доске объявлений в раздевалке (Git).

Где аналогия ломается: самолёт после рейса возвращается в исходное состояние сам, а деплой в prod нужно уметь откатывать вручную — и это отдельный навык.

## 4. Практика и команды

### 4.1. Три неймспейса и квоты

```bash
kubectl create namespace vektor-dev
kubectl create namespace vektor-stage
kubectl create namespace vektor-prod
kubectl label namespace vektor-dev env=dev
kubectl create quota dev-quota \
  --hard=count/deployments.apps=10,requests.cpu=2,requests.memory=2Gi \
  -n vektor-dev
```

`create quota` ограничивает dev, чтобы эксперименты не съели кластер. `count/deployments.apps` — синтаксис «ресурс/группа», считает именно Deployment'ы. Смотрим: `kubectl -n vektor-dev describe quota` — в выводе `Used / Hard`.

### 4.2. Отрендерить оверлей до применения

```bash
kubectl kustomize deploy/overlays/prod | less
kubectl diff -k deploy/overlays/prod
```

`kubectl kustomize <dir>` собирает итоговый манифест из `base` + `overlay`, ничего не применяя. `kubectl diff -k` показывает разницу между тем, что в Git, и тем, что в кластере. Это ваш главный инструмент перед prod-деплоем: **сначала diff, потом apply**.

### 4.3. Пиннинг версии образа

```bash
kustomize edit set image 10.10.10.55/vektor/app=10.10.10.55/vektor/app@sha256:<digest>
# или без установленного kustomize — правим строку newTag в kustomization.yaml вручную
```

`edit set image` меняет блок `images:` в `kustomization.yaml`. Digest надёжнее тега: тег можно перезаписать, digest — нет.

### 4.4. Секрет из переменной окружения (без записи на диск)

```bash
kubectl create secret generic app-secret \
  --from-literal=DB_PASSWORD="$DB_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -n vektor-prod -f -
```

`--dry-run=client` генерирует YAML локально, `-o yaml` печатает его, пайп передаёт в `apply`. Так `kubectl create secret` можно вызывать повторно — он не падает с «already exists». Ключ `--from-file=` берёт значение из файла, `--from-env-file=` — из файла формата `KEY=VALUE`.

### 4.5. Секрет для pull из приватного Harbor

```bash
kubectl create secret docker-registry harbor-cred \
  --docker-server=10.10.10.55 \
  --docker-username="$HARBOR_USER" \
  --docker-password="$HARBOR_PASSWORD" \
  -n vektor-prod
```

Тип `kubernetes.io/dockerconfigjson`. Ссылка в манифесте: `imagePullSecrets: - name: harbor-cred`.

### 4.6. Проверить, что реально пришло в под

```bash
kubectl -n vektor-prod get secret app-secret -o jsonpath='{.data}' | jq 'keys'
kubectl -n vektor-prod exec deploy/app -- env | sort | grep -E 'APP_ENV|LOG_LEVEL'
```

Первая строка показывает **только имена ключей**. `jq 'keys'` — чтобы не вывести значения в терминал и в логи сессии. Вторая — что переменные доехали до процесса (не выводите так секреты на боевом стенде).

### 4.7. Раскатка и откат

```bash
kubectl -n vektor-stage rollout status deployment/app --timeout=120s
kubectl -n vektor-prod rollout undo deployment/app
kubectl -n vektor-prod rollout history deployment/app
```

`rollout status` ждёт готовности и возвращает ненулевой код, если не дождался — идеально для CI. `rollout undo` возвращает предыдущую ревизию ReplicaSet.

### 4.8. ArgoCD: что видит GitOps-контроллер

```bash
kubectl -n argocd get applications
argocd app diff vektor-prod
argocd app sync vektor-prod
argocd app rollback vektor-prod 3
```

`diff` — расхождение Git и кластера, `sync` — принудительная синхронизация, `rollback <N>` — откат к истории ревизий приложения. `[ПРОВЕРИТЬ]` номер ревизии смотрится в `argocd app history`.

### 4.9. Джоба с окружением и ручным гейтом

```yaml
deploy-prod:
  stage: promote
  image: alpine/git:latest
  environment:
    name: prod
    url: https://app.vektor.lab
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: manual
      allow_failure: false
  resource_group: prod
  script:
    - ./ci/promote.sh prod
```

`when: manual` + `allow_failure: false` — блокирующая кнопка (пайплайн ждёт человека). `resource_group: prod` запрещает два параллельных деплоя в прод. `environment: name: prod` связывает джобу с окружением и разрешает защиту через **Settings → CI/CD → Protected environments** (тогда запускать может только разрешённая роль). `[ПРОВЕРИТЬ]` точные названия пунктов меню в вашей версии GitLab.

### 4.10. Секреты в Git «по-взрослому»

```bash
sops -e -i secrets/prod.enc.yaml     # шифрует файл на месте, ключ — age/KMS
kubeseal --format yaml < secret.yaml > sealed-secret.yaml
```

`sops` (репозиторий переехал с mozilla на **getsops**) шифрует значения, оставляя ключи читаемыми — такие файлы можно коммитить. `kubeseal` (Sealed Secrets) шифрует под конкретный кластер. Оба варианта — компромисс; канонический ответ для прода — **External Secrets Operator** + Vault: в Git лежит ссылка на секрет, значение живёт в хранилище.

## 5. Лабораторная работа

### Предусловия

- Кластер `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63) из уроков 55–56 — работает, `kubectl get nodes` показывает три узла `Ready`.
- ArgoCD установлен (урок 55): `kubectl -n argocd get pods` — все `Running`. Если нет — вернитесь к уроку 55.
- `GIT01` (10.10.10.50) — GitLab CE, репозиторий `vektor/vektor-app`, зарегистрированный раннер с тегом `docker`.
- `HARBOR01` (10.10.10.55) — проект `vektor`, право push/pull.
- В репозитории есть работающее приложение в каталоге `app/` (Dockerfile + код из уроков 51–52).
- `stand-log.md` открыт.

### Снапшот

Имя: `before-devops-057`. Снимаем на **всех** затрагиваемых ВМ: `k8s-cp01`, `k8s-w01`, `k8s-w02`, `GIT01`, `HARBOR01`.

Кластер — это согласованная система из трёх узлов. Правильный порядок: выключить гостевые ОС всех трёх узлов → снять снапшот на каждой ВМ в VMware Workstation (`VM → Snapshot → Take Snapshot`) → включить. Снапшот запущенного etcd-кластера «на горячую» может дать несогласованное состояние. `[ПРОВЕРИТЬ]` в вашей версии Workstation пункт меню может называться иначе.

### Шаги

1. **k8s-cp01.** Проверяем, что всё живо: `kubectl get nodes` и `kubectl -n argocd get applications`. Ожидаем 3 узла `Ready` и список приложений без `Unknown`.
2. **k8s-cp01.** Создаём три неймспейса и квоту для dev (команды из 4.1). Ожидаем `kubectl get ns | grep vektor` → три строки.
3. **k8s-cp01 (или локально).** В репозитории создаём структуру:

```text
vektor-app/
├── .gitlab-ci.yml
├── app/
└── deploy/
    ├── base/
    │   ├── kustomization.yaml
    │   ├── deployment.yaml
    │   └── service.yaml
    └── overlays/
        ├── dev/{kustomization.yaml,config.env}
        ├── stage/{kustomization.yaml,config.env}
        └── prod/{kustomization.yaml,config.env}
```

4. **base.** В `deployment.yaml` образ указываем **без тега**: `image: 10.10.10.55/vektor/app`. В `envFrom` — `configMapRef: name: app-config` и `secretRef: name: app-secret`. Ожидаем: манифест универсален, ничего специфичного для окружения.
5. **overlays/dev/kustomization.yaml:**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: vektor-dev
resources:
  - ../../base
images:
  - name: 10.10.10.55/vektor/app
    newTag: bootstrap
replicas:
  - name: app
    count: 1
configMapGenerator:
  - name: app-config
    envs:
      - config.env
```

и `config.env`: `APP_ENV=dev`, `LOG_LEVEL=debug`. Для stage — `count: 2`, `LOG_LEVEL=info`; для prod — `count: 3`, `APP_ENV=prod`, `LOG_LEVEL=warn`. Ожидаем: `kubectl kustomize deploy/overlays/prod` показывает 3 реплики и `namespace: vektor-prod`.

6. **Коммит и пуш.** `git add -A && git commit -m "deploy: base + три оверлея" && git push`. Ожидаем: зелёный пайплайн сборки образа в Harbor.
7. **Bootstrap секретов.** В GitLab: **Settings → CI/CD → Variables**, создаём `DB_PASSWORD` (флаги *Mask variable* и *Protect variable*), `HARBOR_USER`, `HARBOR_PASSWORD`, `PROMOTE_TOKEN` (Project Access Token с правом `write_repository`, тоже masked+protected). Ожидаем: значения в UI отображаются как `********`.
8. **ArgoCD Applications.** Создаём по одному Application на окружение, `path: deploy/overlays/dev|stage|prod`, `syncPolicy.automated` только для dev. Ожидаем: `argocd app list` — три приложения, dev `Synced`, prod `OutOfSync` (если оверлей ещё не готов).
9. **Пайплайн.** В `.gitlab-ci.yml` описываем `build` (сборка+пуш, `docker inspect --format='{{index .RepoDigests 0}}'` для digest) и шаблон `.promote`, который коммитит новый `newTag` в нужный оверлей:

```yaml
.promote:
  stage: promote
  image: alpine/git:latest
  before_script:
    - apk add --no-cache sed
  script:
    - git clone --depth 1 "http://gitlab-ci-token:${PROMOTE_TOKEN}@10.10.10.50/vektor/vektor-app.git" repo
    - cd repo
    - 'sed -i "s/newTag: .*/newTag: ${CI_COMMIT_SHORT_SHA}/" deploy/overlays/${ENV}/kustomization.yaml'
    - git -c user.email=ci@vektor.lab -c user.name=ci commit -am "promote ${ENV} to ${CI_COMMIT_SHORT_SHA}"
    - git push

promote-dev:
  extends: .promote
  variables: { ENV: dev }
  environment: { name: dev }
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'

promote-stage:
  extends: .promote
  variables: { ENV: stage }
  environment: { name: stage }
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: manual
      allow_failure: false

promote-prod:
  extends: .promote
  variables: { ENV: prod }
  environment: { name: prod }
  resource_group: prod
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: manual
      allow_failure: false
```

10. **Прогон.** Пушим коммит в `main`. Ожидаем: `build` зелёный, `promote-dev` выполнился автоматически, ArgoCD подхватил коммит и через ~1–3 минуты в `vektor-dev` новый под.
11. **Проверка конфигурации.** `kubectl -n vektor-dev get cm` — имя `app-config-<hash>`. Меняем `LOG_LEVEL` в `deploy/overlays/dev/config.env`, коммитим, ждём синка. Ожидаем: хеш в имени ConfigMap сменился, под пересоздался сам.
12. **Промоут в stage.** Нажимаем кнопку `promote-stage`. Ожидаем: ArgoCD синхронизировал `vektor-stage`, `kubectl -n vektor-stage get deploy app` — образ с тегом = короткий SHA.
13. **Прод.** `argocd app diff vektor-prod` — читаем diff глазами. Затем `promote-prod` → approve. Ожидаем: 3 реплики в `vektor-prod`, `kubectl rollout status` завершился успешно.
14. **Запись в `stand-log.md`.** Что появилось: неймспейсы, оверлеи, Applications в ArgoCD, переменные в GitLab.

### Проверка

- [ ] `kubectl get ns` содержит `vektor-dev`, `vektor-stage`, `vektor-prod`.
- [ ] `kubectl -n vektor-prod get deploy app -o jsonpath='{.spec.replicas}'` → `3`.
- [ ] Тег образа в `vektor-prod` совпадает с тегом в `vektor-stage` (один и тот же артефакт).
- [ ] `kubectl -n vektor-dev get cm app-config-* -o yaml | grep LOG_LEVEL` → `debug`, в prod — `warn`.
- [ ] `kubectl -n vektor-prod get secret app-secret` существует, ключ `DB_PASSWORD` на месте.
- [ ] `argocd app list` — все три `Synced / Healthy`.
- [ ] В GitLab у `promote-prod` есть отметка окружения `prod` и он не запускается без нажатия.
- [ ] `stand-log.md` обновлён.

### Сломай и почини

> **Пользователь:** sidorov (склад, WIN11-CL01)
> **Что написал:** «Открываю форму заявок — "Сервис временно недоступен". Утром работало. Нам отгружать сегодня.»
> **Срочность:** Высокая

**Как воспроизвести поломку.** В GitLab: **Settings → CI/CD → Variables** → у `DB_PASSWORD` снимаем галочку *Protect variable*. Теперь в джобе, запущенной с незащищённой ветки/тега, переменная приходит **пустой**. Запускаем пайплайн деплоя в prod. Секрет создаётся без ключа `DB_PASSWORD` (или приложение стартует с пустым паролем и не поднимается).

**Диагностика по порядку:**

```bash
kubectl -n vektor-prod get pods                      # CrashLoopBackOff / 0/1 Ready?
kubectl -n vektor-prod describe pod <pod>            # в Events: Back-off restarting
kubectl -n vektor-prod logs <pod> --previous         # приложение пишет про пустой пароль
kubectl -n vektor-prod get secret app-secret -o jsonpath='{.data}' | jq 'keys'
kubectl -n vektor-prod get deploy app -o yaml | grep -A3 envFrom
argocd app diff vektor-prod                          # Git и кластер не расходятся?
```

Порядок именно такой: сначала «жив ли под» (2 секунды), потом «почему умер» (`describe`/`logs`), и только потом лезем в конфигурацию. Если `argocd app diff` пустой — проблема не в Git, а в том, чего в Git нет: в секрете.

**Решение.** Возвращаем галочку *Protect variable*, перезапускаем джобу, затем:

```bash
kubectl -n vektor-prod rollout restart deployment/app
kubectl -n vektor-prod rollout status deployment/app
```

**Что написать пользователю:** «Алексей, добрый день. Причина — ошибка в обновлении сервиса, исправлена. Форма заявок снова работает, проверьте, пожалуйста, и напишите, если что-то ещё не так. Приносим извинения за простой.»

**Закрытие заявки:** категория «Сбой сервиса», в решении — причина (пустая защищённая переменная при деплое), что сделано, и профилактика: добавить в пайплайн проверку `test -n "$DB_PASSWORD" || exit 1` перед созданием секрета.

### Что изменилось на стенде

- Kubernetes: неймспейсы `vektor-dev`, `vektor-stage`, `vektor-prod`, ResourceQuota `dev-quota`.
- Репозиторий `vektor/vektor-app`: каталог `deploy/` с `base` и тремя оверлеями.
- ArgoCD: три Application (`vektor-dev`, `vektor-stage`, `vektor-prod`).
- GitLab: переменные `DB_PASSWORD`, `HARBOR_USER`, `HARBOR_PASSWORD`, `PROMOTE_TOKEN`; Project Access Token.
- В кластере: Secret `app-secret` в каждом неймспейсе, Secret `harbor-cred`.
- Пайплайн: стадии `build` и `promote`, ручные гейты на stage и prod.

### Откат

1. **Быстро, только приложение:** `kubectl -n vektor-prod rollout undo deployment/app`.
2. **Через GitOps:** `argocd app rollback vektor-prod <N>` или revert-коммит в Git.
3. **Полный откат стенда:** выключить `k8s-cp01`, `k8s-w01`, `k8s-w02`, `GIT01`, `HARBOR01` → в VMware для каждой `Snapshot Manager → before-devops-057 → Restore` → включить. Восстанавливать **все три узла кластера**, иначе etcd разъедется.

## 6. Ошибки новичков и безопасность

1. **Ошибка:** держать dev и prod в одном неймспейсе «чтобы проще».
   **Приводит:** тестовый под с утечкой памяти роняет прод; случайный `kubectl delete` бьёт по боевому.
   **Правильно:** отдельные неймспейсы минимум, отдельный кластер для прода в реальной эксплуатации.

2. **Ошибка:** пересобирать образ для каждого окружения.
   **Приводит:** на stage проверяли digest A, в prod поехал digest B. Регресс, который «на тесте не воспроизводился».
   **Правильно:** один build, продвижение по digest.

3. **Ошибка:** тег `latest`.
   **Приводит:** невозможно сказать, что именно работает; `rollout undo` откатывает на «тот же latest».
   **Правильно:** immutable-тег (короткий SHA) + пиннинг по digest в prod.

4. **Ошибка:** секреты в `ConfigMap` или в `values.yaml` в Git.
   **Приводит:** утечка при первом же клонировании репозитория; секрет навсегда остаётся в истории Git.
   **Правильно:** Vault/ESO, SOPS или защищённые переменные CI; при компрометации — ротация, а не `git rm`.

5. **Ошибка:** разные конфиги, но общая внешняя зависимость (dev смотрит в prod-БД).
   **Приводит:** тестовый прогон удаляет/портит боевые данные.
   **Правильно:** отдельные БД на окружение; в dev — обезличенная копия, а не дамп прода.

6. **Ошибка:** всем разработчикам — право деплоя в prod.
   **Приводит:** «кто-то что-то нажал» в пятницу вечером.
   **Правильно:** protected environment + обязательный approve + `resource_group`.

7. **Ошибка:** печатать секреты в логи джобы (`echo $TOKEN`, `set -x` с секретами).
   **Приводит:** секрет уезжает в артефакты и в историю пайплайна, доступную всей команде.
   **Правильно:** `set +x` вокруг чувствительных блоков, `masked`-переменные, проверка логов.

### Безопасность

- **Secret в Kubernetes — не шифрование.** В etcd он лежит base64; включите шифрование etcd (`EncryptionConfiguration`) — `[ПРОВЕРИТЬ]` детали под вашу версию.
- **Персональные данные (152-ФЗ).** Копировать реальные данные сотрудников «Вектора» в dev нельзя. Обезличивание — не пожелание, а требование; хранение и обработка ПДн — на территории РФ.
- **В лаборатории мы храним `PROMOTE_TOKEN` в переменных GitLab** — это приемлемый учебный минимум. В бою токен живёт в Vault с TTL и ротацией, а не «вечно».
- **Не отключайте RBAC и NetworkPolicy ради удобства.** В учебном стенде мы не настраивали NetworkPolicy — на боевом это обязательный элемент изоляции окружений.

## 7. Вопросы на собеседовании

**1. Чем окружения отличаются друг от друга и что в них должно быть одинаковым?**

Ответ: отличаются инфраструктурой (реплики, лимиты), конфигурацией, секретами, данными и правами доступа. Одинаковым должен быть **артефакт**: один и тот же образ по digest проходит dev → stage → prod, меняется только обвязка. Это принцип «build once, deploy many». Конфигурация приходит извне (12-factor, п. III), а не зашита в образ.

*Что хочет услышать интервьюер:* что вы понимаете разницу между «собрать заново» и «продвинуть» и не путаете окружение с копией кода.

*Красный флаг:* «На stage собираем отдельно, чтобы быстрее» или «окружения отличаются только переменной ENV».

**2. Как выстроить promote-политику?**

Ответ: dev — автомат на каждый merge в main. stage — автомат после smoke-тестов либо кнопка. prod — только вручную, с обязательным approve, защищённым окружением и `resource_group`, чтобы не было параллельных раскаток. Везде: пиннинг по digest, `kubectl diff`/`argocd app diff` перед применением, `rollout status` как критерий успеха, автоматический откат по неуспешному health-check.

*Что хочет услышать интервьюер:* что prod защищён процессом, а не «дисциплиной людей».

*Красный флаг:* «У нас всё автоматом в прод, тесты же есть» — без гейтов и отката.

**3. Где хранить конфигурацию и где секреты?**

Ответ: конфигурация — в Git, в оверлеях/чартах, версионируется вместе с кодом. Секреты — **не в Git в открытом виде**: либо внешнее хранилище (Vault, Yandex/VK Cloud Secret Manager) через External Secrets Operator, либо шифрование в репозитории (SOPS+age, Sealed Secrets), либо защищённые переменные CI, если окружение небольшое. В Kubernetes — ConfigMap для несекретного, Secret для секретного, помня, что Secret не шифрует.

*Что хочет услышать интервьюер:* знание хотя бы одного промышленного решения и понимание, что base64 — не защита.

*Красный флаг:* «Пароли в .env, он же в .gitignore» — как единственный механизм.

**4. Чем External Secrets Operator отличается от Sealed Secrets?**

Ответ: Sealed Secrets шифрует секрет под конкретный кластер криптоключом контроллера — файл можно коммитить, но он не переносим на другой кластер и ротация требует пересоздания. ESO вообще не хранит значение в Git: в манифесте лежит ссылка на секрет в Vault/облачном хранилище, контроллер тянет его в кластер и обновляет. ESO лучше для ротации и аудита, Sealed Secrets проще в старте.

*Что хочет услышать интервьюер:* понимание компромисса «простота vs управляемость» и что вы не тащите значение секрета в репозиторий.

*Красный флаг:* «ESO хранит секреты в Git» — путаница в базовой архитектуре.

**5. Как откатить прод-деплой и как понять, что пора?**

Ответ: `kubectl rollout undo` или `argocd app rollback` (или revert-коммит, если GitOps — предпочтительно, Git остаётся источником правды). Триггеры: неуспешный `rollout status`, падение health-check/readiness, всплеск 5xx или latency в мониторинге (MON01/Zabbix), алерт из Progressive Delivery (урок 56). Решение принимается по метрикам, а не по ощущениям; после отката — разбор и запись в постмортем.

*Что хочет услышать интервьюер:* что откат — отработанная процедура с критериями, а не импровизация.

*Красный флаг:* «Откатим руками через SSH, поправив файл на сервере» — расхождение с Git и потерянное состояние.

## 8. Источники (официальная документация)

- **kubernetes.io** — разделы *ConfigMaps*, *Secrets*, *Good practices for Kubernetes Secrets*, *Resource quotas*, *Declarative Management of Kubernetes Objects Using Kustomize*. Поиск: `kubernetes secrets good practices`.
- **12factor.net** — *III. Config*, *V. Build, release, run*, *X. Dev/prod parity*. Поиск: `12 factor app config`.
- **docs.gitlab.com** — *CI/CD variables*, *Environments and deployments*, *Protected environments*, *Resource groups*, *Project access tokens*. Поиск: `gitlab ci protected environments`.
- **argoproj.github.io** (Argo CD) — *Applications*, *Sync policies*, *App of apps / ApplicationSet*. Поиск: `argo cd application sync policy`.
- **kustomize.io** — *Kustomization*, *Images*, *ConfigMapGenerator*. Поиск: `kustomize images newTag`.
- **external-secrets.io** — *Introduction*, *SecretStore / ExternalSecret*. Поиск: `external secrets operator getting started`.
- **github.com/getsops/sops** (ранее mozilla/sops) — *SOPS README*, раздел про `.sops.yaml`. Поиск: `sops age encryption`.
- **github.com/bitnami-labs/sealed-secrets** — *Sealed Secrets README*, `kubeseal`. Поиск: `kubeseal sealed secrets`.
- **docs.docker.com** — *docker inspect*, *Registry authentication*. Поиск: `docker inspect RepoDigests`.

Версии компонентов (ArgoCD, GitLab, kustomize, kubectl в образах CI) в стенде — `[ПРОВЕРИТЬ]` по факту выпуска; перед лабораторной сверяйтесь с `--version`.
