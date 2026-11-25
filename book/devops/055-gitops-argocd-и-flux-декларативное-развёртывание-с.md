# GitOps: ArgoCD и Flux — декларативное развёртывание, синхронизация, управление окружениями

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку от Ивана Иванова (WIN11-CL01, в сюжете — разработчик учебного приложения `vektor-web`):

> «Я вчера вечером руками поправил Deployment через `kubectl edit`, чтобы проверить фикс. Через три минуты мои изменения исчезли, реплики вернулись к единице. Кто-то лазит в кластер? Так же нельзя работать!»

Никто не лазил. Сработал GitOps-контроллер: он сверяет кластер с Git-репозиторием и стирает всё, чего в Git нет. Именно эта «магия» и пугает новичков, и одновременно является главной ценностью подхода.

До сих поговорили о том, как пайплайн собирает образ и как его выкатывать (уроки 48–54). Но там деплой делал CI-раннер: у него были креды от кластера, он «толкал» изменения внутрь. GitOps переворачивает схему: кластер сам тянет состояние из Git. После главы ты будешь уметь: ставить ArgoCD в кластер, описывать приложение как `Application`, разделять dev и prod через оверлеи Kustomize, читать статусы `Synced/OutOfSync/Healthy/Degraded`, настраивать self-heal и prune, откатывать деплой через `git revert`, а также понимать, чем Flux отличается от ArgoCD и когда что брать.

## 2. Теория «под капотом»

**GitOps** — это набор из четырёх принципов (сформулированы сообществом OpenGitOps):

1. **Declarative** — всё состояние описано декларативно (YAML-манифесты, Helm-чарты, Kustomize).
2. **Versioned and Immutable** — источник истины хранится в Git, у каждого состояния есть коммит и история.
3. **Pulled Automatically** — софт внутри кластера сам забирает изменения из Git (pull-модель).
4. **Continuously Reconciled** — контроллер постоянно сравнивает «что в Git» (desired state) с «что в кластере» (observed state) и приводит второе к первому.

Ключевое отличие от классического CI/CD: раньше раннер GitLab CI делал `kubectl apply` — это **push**-модель. Кластеру не нужны внешние креды, зато CI-системе нужен kubeconfig с правами. В GitOps CI заканчивается на «собрал образ → обновил тег в Git», а деплоем занимается агент **внутри** кластера со своими правами.

```text
   PUSH (как в уроке 49)                    PULL / GitOps (эта глава)
┌──────────────┐                      ┌──────────────┐
│ GitLab CI    │──kubectl apply──┐    │ GitLab CI    │──сборка образа──▶ HARBOR01
│ runner       │                 │    │ runner       │──коммит тега в Git──┐
└──────────────┘                 ▼    └──────────────┘                     │
                          ┌──────────┐                             ┌───────▼──────┐
   kubeconfig у раннера   │  k8s     │                             │ Git (GIT01)  │
   = большой риск         │ кластер  │                             │ gitops-stand │
                          └──────────┘                             └───────┬──────┘
                                                                           │ pull (HTTPS 443)
                                                                  ┌────────▼─────────┐
                                                                  │ ArgoCD / Flux    │
                                                                  │ в кластере       │
                                                                  └────────┬─────────┘
                                                                           │ apply через API 6443
                                                                  ┌────────▼─────────┐
                                                                  │ k8s кластер      │
                                                                  └──────────────────┘
```

**Архитектура ArgoCD** (namespace `argocd`):

- `argocd-server` — API + веб-интерфейс, порт 8080/443 внутри кластера, наружу обычно через Ingress;
- `argocd-repo-server` — клонирует Git-репозитории и рендерит манифесты (Kustomize/Helm/plain YAML), кэширует результат;
- `argocd-application-controller` — сравнивает рендер с кластером, синхронизирует, следит за health;
- `argocd-redis` — кэш;
- `argocd-dex-server` — опциональный SSO;
- `argocd-applicationset-controller` — генерирует Application'ы из шаблонов;
- CRD `Application`, `AppProject`, `ApplicationSet`.

Юнит «приложения» — объект `Application`: где лежит (`repoURL` + `path` + `targetRevision`), куда ставить (`destination.server` + `namespace`), как синхронизировать (`syncPolicy`). Опрос Git по умолчанию раз в 3 минуты (параметр `timeout.reconciliation` в ConfigMap `argocd-cm`), плюс мгновенный вебхук из GitLab, если настроить.

**Flux** — набор независимых контроллеров в namespace `flux-system`: `source-controller` (тянет Git/OCI/Helm-репозитории в виде артефактов), `kustomize-controller` (применяет), `helm-controller` (ставит HelmRelease), `notification-controller` (алерты), плюс image-automation для автообновления тегов. CRD: `GitRepository`, `OCIRepository`, `Kustomization`, `HelmRelease`.

Состояния ArgoCD, которые надо знать наизусть: **Synced / OutOfSync** (совпадает ли кластер с Git) и **Healthy / Progressing / Degraded / Missing** (жив ли workload). `OutOfSync + Healthy` — кто-то руками изменил ресурс или в Git пришёл новый коммит.

**Управление окружениями** делается не отдельными кластерами, а структурой репозитория: каталоги `overlays/dev`, `overlays/prod` (Kustomize), отдельные `values-dev.yaml`/`values-prod.yaml` (Helm), либо ветки `dev`/`main`. Продвижение (promote) между окружениями — это merge request или тег, а не нажатие кнопки в Jenkins.

## 3. Аналогия из жизни

«Вектор» — логистическая компания, и на складе есть **эталонная накладная**: сколько чего должно лежать на полках. Кладовщик (контроллер) каждые три минуты обходит склад с этой накладной, сверяет полки и **сам** убирает лишнее и докладывает недостающее. Если кто-то ночью тайком переставил коробки — утром всё вернётся на место, потому что накладная главнее. Привезти новую накладную можно только одним способом: провести её через бюро (Git, pull request, ревью) — подписать и подшить в журнал, где видно, кто и когда менял. Никто не может «на словах» попросить кладовщика переставить товар — только через новую версию накладной. Аналогия перестаёт работать в одном месте: реальный кладовщик не умеет сам создавать новые стеллажи из ничего, а ArgoCD с флагом `CreateNamespace=true` спокойно создаст и namespace, и все объекты в нём с нуля.

## 4. Практика и команды

**1. Установка ArgoCD.** Ставим из официального манифеста.
```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```
`stable` — последняя стабильная ветка; для воспроизводимости в проде фиксируют версию: `.../argo-cd/v2.x.y/manifests/install.yaml` `[ПРОВЕРИТЬ актуальный номер версии]`. HA-вариант — `manifests/ha/install.yaml`.

**2. Проверка, что всё поднялось.**
```bash
kubectl -n argocd get pods
# argocd-application-controller-0   1/1  Running
# argocd-repo-server-...            1/1  Running
# argocd-server-...                 1/1  Running
```
Смотрим на `READY 1/1` и `Running`. `Pending` — обычно не хватает ресурсов или образа.

**3. Начальный пароль администратора.**
```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d; echo
# либо через CLI:
argocd admin initial-password -n argocd   # [ПРОВЕРИТЬ наличие в вашей версии]
```
Пароль лежит в Secret в base64 (это кодирование, не шифрование). После первого логина Secret лучше удалить.

**4. CLI и вход.**
```bash
curl -sSL -o argocd https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64
chmod +x argocd && sudo mv argocd /usr/local/bin/
kubectl -n argocd port-forward svc/argocd-server 8080:443
argocd login 127.0.0.1:8080 --username admin --insecure
```
`--insecure` — разрешает самоподписанный сертификат ArgoCD (на стенде нормально, в проде ставят нормальный TLS). Дальше `argocd account update-password`.

**5. Подключение Git-репозитория.**
```bash
argocd repo add http://10.10.10.50/vektor/gitops-stand.git \
  --username oauth2 --password <PERSONAL_ACCESS_TOKEN>
argocd repo list
```
Для GitLab по HTTPS вместо пароля — Personal/Project Access Token со scope `read_repository`; имя пользователя может быть любым непустым, часто пишут `oauth2`. В проде репозиторий объявляют декларативно Secret'ом с меткой `argocd.argoproj.io/secret-type: repository`.

**6. Application — единица деплоя.**
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: vektor-web-dev
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io   # удаление App удалит и ресурсы
spec:
  project: vektor
  source:
    repoURL: http://10.10.10.50/vektor/gitops-stand.git
    targetRevision: main
    path: apps/vektor-web/overlays/dev
  destination:
    server: https://kubernetes.default.svc    # локальный кластер
    namespace: vektor-web-dev
  syncPolicy:
    automated:
      prune: true        # удалять ресурсы, которых больше нет в Git
      selfHeal: true     # откатывать ручные правки
    syncOptions:
      - CreateNamespace=true
```
`targetRevision` — ветка, тег или коммит. `prune` + `selfHeal` = полный контроль Git над кластером.

**7. Создание и чтение статуса.**
```bash
argocd app create --upsert -f application.yaml
argocd app list
argocd app get vektor-web-dev
# Sync Status:   Synced to main (3f9c1a2)
# Health Status: Healthy
```
`--upsert` обновляет существующий объект. В выводе главное — две строки: `Sync Status` и `Health Status`.

**8. Diff, sync, ожидание.**
```bash
argocd app diff vektor-web-dev      # код возврата 1, если diff есть [ПРОВЕРИТЬ]
argocd app sync vektor-web-dev
argocd app wait vektor-web-dev --health --timeout 180
```
`diff` показывает, что именно разъедется; `sync` применяет; `wait` блокируется до Healthy — удобно в скриптах.

**9. История и откат.**
```bash
argocd app history vektor-web-dev
# ID  DATE                           REVISION
# 1   2026-02-09 18:02:11 +0300 MSK  91aa7de (main)
# 0   2026-02-10 12:31:04 +0300 MSK  3f9c1a2 (main)
argocd app rollback vektor-web-dev 1
```
`rollback` возвращает ревизию в кластер, но **Git остаётся с новым коммитом** — при следующем reconcile ArgoCD снова накатит `main`. В GitOps правильный откат — `git revert`, тогда история честная.

**10. Окружения одним объектом — ApplicationSet.**
```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: vektor-web, namespace: argocd}
spec:
  goTemplate: true
  generators:
    - list:
        elements:
          - {env: dev,  ns: vektor-web-dev}
          - {env: prod, ns: vektor-web-prod}
  template:
    metadata: {name: 'vektor-web-{{.env}}'}
    spec:
      project: vektor
      source:
        repoURL: http://10.10.10.50/vektor/gitops-stand.git
        targetRevision: main
        path: 'apps/vektor-web/overlays/{{.env}}'
      destination: {server: https://kubernetes.default.svc, namespace: '{{.ns}}'}
      syncPolicy: {automated: {prune: true, selfHeal: true}, syncOptions: [CreateNamespace=true]}
```
Генератор `list` — самый простой; бывает `git` (читает каталоги репозитория) и `pullRequest` (превью-окружение на каждый MR). `goTemplate` доступен в свежих 2.x `[ПРОВЕРИТЬ версию]`.

**11. Flux: установка и работа.**
```bash
curl -s https://fluxcd.io/install.sh | sudo bash     # CLI
flux check --pre
flux install                                         # контроллеры в flux-system
flux create source git gitops-stand \
  --url=http://10.10.10.50/vektor/gitops-stand.git --branch=main --interval=1m
flux create kustomization vektor-web-dev \
  --source=GitRepository/gitops-stand --path=./apps/vektor-web/overlays/dev \
  --prune=true --target-namespace=vektor-web-dev --interval=5m
flux get kustomizations
# NAME            REVISION      SUSPENDED  READY  MESSAGE
# vektor-web-dev  main/3f9c1a2  False      True   Applied revision: main/3f9c1a2
flux reconcile kustomization vektor-web-dev --with-source
```
`--with-source` заставляет сначала перечитать Git, потом применить — не ждать интервала. Аналог «Sync» в ArgoCD.

**12. Тот же смысл декларативно у Flux.**
```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata: {name: gitops-stand, namespace: flux-system}
spec:
  interval: 1m
  url: http://10.10.10.50/vektor/gitops-stand.git
  ref: {branch: main}
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata: {name: vektor-web-dev, namespace: flux-system}
spec:
  interval: 5m
  prune: true
  wait: true
  targetNamespace: vektor-web-dev
  sourceRef: {kind: GitRepository, name: gitops-stand}
  path: ./apps/vektor-web/overlays/dev
```
Версии API (`v1` vs `v1beta2`) зависят от релиза Flux — проверь `kubectl api-resources | grep toolkit` `[ПРОВЕРИТЬ]`.

## 5. Лабораторная работа

### Предусловия
Кластер из уроков 47/54: `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63), Ubuntu, `kubectl` и `flux`/`argocd` CLI на `k8s-cp01`. Работающий GitLab CE на `GIT01` (10.10.10.50) из урока 49 — там уже есть аккаунт `adm.student` и раннер (раннер в этой лабе **не нужен**: CI и GitOps — разные этапы). Windows-часть стенда не требуется.

### Снапшот
В VMware Workstation для `k8s-cp01`, `k8s-w01`, `k8s-w02` (и `GIT01`, если будешь менять его настройки): VM → Snapshot → Take Snapshot, имя `before-devops-055`. Для кластера дополнительно полезно `sudo etcdctl snapshot save` `[ПРОВЕРИТЬ путь к сертификатам в вашем кластере]`, но на учебном стенде достаточно снапшота ВМ.

### Шаги

1. **k8s-cp01.** Проверь кластер: `kubectl get nodes -o wide` → три узла в статусе `Ready`. Если нет — сначала вернись к уроку про установку кластера.
2. **k8s-cp01.** `kubectl create namespace argocd` и `kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml`. Ожидаемо: десятки объектов `created`.
3. **k8s-cp01.** `kubectl -n argocd get pods -w` → все поды `Running`, `1/1`.
4. **k8s-cp01.** Достань пароль (команда из п.4.3), запусти `kubectl -n argocd port-forward svc/argocd-server 8080:443` в отдельном терминале, залогинься `argocd login 127.0.0.1:8080 --username admin --insecure`, смени пароль через `argocd account update-password` на `Lab#2026-vektor`. Ожидаемо: `'admin:login' logged in successfully`.
5. **GIT01 (браузер).** Создай проект `vektor/gitops-stand` (Visibility: Private). В Settings → Access Tokens создай токен с ролью Reporter и scope `read_repository` `[ПРОВЕРИТЬ названия пунктов меню в вашей версии GitLab]`, скопируй значение.
6. **k8s-cp01.** Склонируй репозиторий и создай структуру:
```text
gitops-stand/
├── apps/vektor-web/
│   ├── base/{deployment.yaml,service.yaml,kustomization.yaml}
│   └── overlays/dev/{kustomization.yaml}
│   └── overlays/prod/{kustomization.yaml}
└── argocd/{project.yaml,repo-secret.yaml,app-dev.yaml}
```
7. **k8s-cp01.** Заполни `base/deployment.yaml` (образ `nginx:stable-alpine`, `containerPort: 80`), `base/service.yaml` (ClusterIP, порт 80), `base/kustomization.yaml` со списком `resources`. Затем `overlays/dev/kustomization.yaml`:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: vektor-web-dev
resources: [../../base]
patches:
  - target: {kind: Deployment, name: vektor-web}
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 1
```
Для `prod` — то же с `namespace: vektor-web-prod` и `value: 3`.
8. **k8s-cp01.** `git add . && git commit -m "vektor-web: base + overlays dev/prod" && git push`. Ожидаемо: коммит в GitLab.
9. **k8s-cp01.** Примени `argocd/project.yaml` (AppProject `vektor`: `sourceRepos` — твой репозиторий, `destinations` — namespace `vektor-web-*`, `clusterResourceWhitelist` — `Namespace`), `repo-secret.yaml` (Secret с меткой `argocd.argoproj.io/secret-type: repository`, поля `url`, `username: oauth2`, `password: <токен>`) и `app-dev.yaml`. Проверь `argocd repo list` → `Successful`.
10. **k8s-cp01.** `argocd app get vektor-web-dev`. Ожидаемо: `Synced`, `Healthy`. Проверь `kubectl -n vektor-web-dev get deploy,svc,pods` — один под, Service на месте.
11. **k8s-cp01.** Проверка self-heal: `kubectl -n vektor-web-dev scale deploy/vektor-web --replicas=5`, затем `argocd app get vektor-web-dev --refresh` и через ~3 минуты `kubectl get deploy -n vektor-web-dev`. Ожидаемо: реплики вернулись к 1, в UI — событие `SelfHeal`.
12. **k8s-cp01.** Проверка prune: `kubectl -n vektor-web-dev create configmap manual-test --from-literal=a=1`, затем `argocd app sync vektor-web-dev --prune`. Ожидаемо: ConfigMap исчез — его нет в Git.
13. **k8s-cp01.** Примени ApplicationSet из п.4.10 (`argocd appset create` или `kubectl apply -f`). Ожидаемо: два Application — `vektor-web-dev` (1 реплика) и `vektor-web-prod` (3 реплики), namespace'ы созданы автоматически.
14. **Опционально, после верификации.** Поставь Flux (`flux install`, `flux check`) и повтори приложение в **другом** namespace (`vektor-flux-demo`), чтобы два контроллера не дрались за одни ресурсы. Сравни `flux get kustomizations` и `argocd app list`.

### Проверка
- [ ] `kubectl -n argocd get pods` — все компоненты `Running`.
- [ ] `argocd app list` содержит `vektor-web-dev` и `vektor-web-prod` в статусе `Synced/Healthy`.
- [ ] Ручное масштабирование откатывается контроллером.
- [ ] Ресурс, созданный руками в namespace приложения, удаляется при `--prune`.
- [ ] Изменение в Git (правка реплик в overlay) доезжает до кластера без `kubectl apply`.
- [ ] `git log` в `gitops-stand` содержит все изменения состояния — это и есть аудит-трейл.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини
**Пользователь:** ivanov / WIN11-CL01 (разработчик `vektor-web`)
**Что написал:** «В ArgoCD у приложения `vektor-web-dev` статус красный, `ComparisonError`, иконка Unknown. Мой фикс в `main` не доезжает до кластера. Выкатка стоит.»
**Срочность:** Высокая (блокирует проверку фикса).

**Как воспроизвести:** удали репозиторный Secret: `kubectl -n argocd delete secret -l argocd.argoproj.io/secret-type=repository`, затем `argocd app get vektor-web-dev --refresh`.

**Диагностика по шагам:**
1. `argocd app get vektor-web-dev` → смотрим поле `Message`: `repository not found` / `authentication required`.
2. `argocd repo list` → репозитория нет вовсе или статус `Failed`.
3. `kubectl -n argocd logs deploy/argocd-repo-server --tail=50` → `permission denied` / `401 Unauthorized`.
4. `kubectl -n argocd get secret -l argocd.argoproj.io/secret-type=repository` → пусто.
5. Проверяем в GitLab, не истёк ли токен (Settings → Access Tokens) `[ПРОВЕРИТЬ]`.

**Решение:** пересоздать Secret (или `argocd repo add` с новым токеном), затем `argocd app get vektor-web-dev --refresh` → `Synced/Healthy`.

**Ответ пользователю:** «Проблема была не в вашем коде и не в кластере — отвалились креды ArgoCD к Git. Починил, фикс на `main` уже в кластере, pod обновлён. Пожалуйста, не правьте ресурсы через `kubectl edit` — контроллер вернёт их обратно; изменения только через коммит в `gitops-stand`.»

**Закрытие:** тип «конфигурация», причина «истёк токен доступа к репозиторию», решение «пересоздан Secret репозитория», профилактика — календарный напоминатель о ротации токена и алерт на `OutOfSync > 15 минут`.

### Что изменилось на стенде
- `k8s-cp01/k8s-w01/k8s-w02`: в кластере установлен ArgoCD (namespace `argocd`) и, если делал шаг 14, Flux (namespace `flux-system`).
- `GIT01`: новый проект `vektor/gitops-stand` с каталогами `apps/vektor-web/base`, `apps/vektor-web/overlays/dev|prod`, `argocd/`; выпущен Project Access Token (scope `read_repository`).
- В кластере появились namespace `vektor-web-dev`, `vektor-web-prod` и ресурсы Deployment/Service `vektor-web`.
- Пароль `admin` ArgoCD сменён на `Lab#2026-vektor` (учебный стенд).
- Статус: **GitOps-контур работает**, CI-сборка образа в него пока не подключена (это следующая тема про мультиокружения).

### Откат
VMware Workstation → правый клик по `k8s-cp01` → Snapshots → `before-devops-055` → Revert (аналогично для `.62`/`.63`). Если откатывать весь стенд не хочется — удали только контур:
```bash
kubectl delete -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl delete ns argocd vektor-web-dev vektor-web-prod vektor-flux-demo --ignore-not-found
flux uninstall --silent   # если ставил Flux
```
Проект и токен в GitLab удаляются вручную. Не забудь пометить в `stand-log.md`, какой вариант отката выбран.

## 6. Ошибки новичков и безопасность

1. **`prune: true` включён «на всякий случай» с первого дня.** → Удаление объекта из Git молча сносит Service или PVC в проде. → Сначала гонять на dev, включать `prune` осознанно, начинать без него.
2. **`selfHeal` без объяснения команде.** → Люди правят кластер через `kubectl edit`, теряют правки и пишут тикеты. → Договорённость «кластер read-only для людей», доступ на изменение — через Git.
3. **Тег образа `:latest`.** → Git не фиксирует версию, реконсиляция не видит разницы, откат невозможен. → Пиновать версию или digest.
4. **Один Git-репозиторий без структуры.** → dev и prod перемешаны, promote — ручное копирование. → Каталоги-оверлеи или отдельные ветки; prod меняется только через MR.
5. **Креды Git внутри манифестов в открытом виде.** → Токен утекает в историю навсегда. → Secret в кластере, Sealed Secrets / SOPS / External Secrets (подробно в уроке 57).
6. **ArgoCD установлен с дефолтным паролем и открыт в интернет.** → Компромисс кластера за минуты. → Сменить пароль сразу, закрыть SSO/RBAC, доступ только через VPN/Ingress с авторизацией.
7. **Права ArgoCD «cluster-admin на всё».** → Агент может снести что угодно. → AppProject с ограниченными `destinations` и `sourceRepos`.

**Безопасность.** В боевой среде нельзя: давать CI-раннеру полный kubeconfig (вся суть GitOps — убрать эту связку), хранить plaintext-секреты в Git, ставить ArgoCD/Flux в namespace по умолчанию без NetworkPolicy, разрешать `prune` на кластерные ресурсы (`Namespace`, `CRD`, `PersistentVolume`) без whitelist. На стенде мы осознанно используем `--insecure` и учебный пароль — в реальности это TLS с валидным сертификатом и корпоративный SSO.

## 7. Вопросы на собеседовании

**1. Чем GitOps отличается от «CI-пайплайн делает `kubectl apply`»?**
Ответ: это push- против pull-модели. В push CI-раннер имеет креды от кластера и сам толкает манифесты — креды живут вне кластера, дрейф не отслеживается. В GitOps агент внутри кластера тянет Git и постоянно реконсилит, поэтому ловит ручные изменения (`selfHeal`), удаляет лишнее (`prune`) и даёт аудит в истории коммитов. CI заканчивается на сборке образа и обновлении тега в Git.
*Что хочет услышать:* pull vs push, креды внутри кластера, continuous reconciliation, Git как single source of truth.
*Красный флаг:* «GitOps — это просто Helm-чарты в Git».

**2. Что произойдёт, если руками удалить Deployment из namespace под управлением ArgoCD с `selfHeal: true`?**
Ответ: application-controller на следующем цикле сверки (по умолчанию до ~3 минут) увидит `Missing`/`OutOfSync` и воссоздаст ресурс из Git. Если удалить целиком namespace — при `CreateNamespace=true` он будет создан заново, но данные (PVC) не восстановятся.
*Что хочет услышать:* понимание интервала реконсиляции и того, что GitOps восстанавливает конфигурацию, а не данные.
*Красный флаг:* «Ничего не произойдёт, GitOps только для деплоя новых версий».

**3. Как организовать dev/stage/prod в GitOps-репозитории?**
Ответ: три подхода — репозиторий на окружение; ветка на окружение; один репозиторий с каталогами `overlays/dev|stage|prod` на Kustomize (или `values-<env>.yaml` для Helm). Продвижение — merge request из dev в prod-каталог или тег, ревью и CI-проверки на diff. Плюс отдельные кластеры/namespace и разные права.
*Что хочет услышать:* promote через MR, изоляция секретов и прав, отсутствие «ручного копирования».
*Красный флаг:* «Прод — это отдельная ветка, которую мы мержим вручную по пятницам».

**4. ArgoCD или Flux — что выберете и почему?**
Ответ: ArgoCD — централизованный control plane с UI, мультикластерностью и готовым RBAC/SSO, удобен командам, где нужен «пульт» и наглядность; Flux — набор легковесных контроллеров, всё через CRD и CLI, лучше ложится в «всё как код» и в связку с HelmRelease/image-automation. Часто решает наличие UI для поддержки и привычки команды.
*Что хочет услышать:* оба декларативны и реконсилят, разница в UX, архитектуре и экосистеме; выбор обоснован, а не «модно».
*Красный флаг:* «Flux устарел» или «ArgoCD — единственный настоящий GitOps».

**5. Как хранить секреты в GitOps?**
Ответ: plaintext в Git — никогда. Варианты: Sealed Secrets (шифрует в Git, расшифровывает контроллер в кластере), SOPS + age/KMS (шифрует значения, ключ вне Git), External Secrets Operator (тянет из Vault/облачного хранилища). Плюс RBAC на репозиторий и ротация.
*Что хочет услышать:* понимание, что Git публичен внутри компании, и разделение «конфиг в Git, секрет — в хранилище».
*Красный флаг:* «Положим в приватный репозиторий, туда никто не смотрит».

## 8. Источники (официальная документация)

- **Argo CD** — домен `argo-cd.readthedocs.io` `[ПРОВЕРИТЬ: документация также публикуется на argo-cd.dev]`. Разделы: Getting Started, Declarative Setup, Application Specification, Sync Options, AppProject, ApplicationSet, Private Repositories. Поиск: `argocd application syncPolicy selfHeal prune`, `argocd declarative repository secret`.
- **Flux** — домен `fluxcd.io`. Разделы: Get Started, Core Concepts, GitRepository, Kustomization, Guides → Flux bootstrap for GitLab/GitHub. Поиск: `flux bootstrap gitlab`, `flux gitrepository kustomization reconcile`.
- **OpenGitOps** — домен `opengitops.dev` `[ПРОВЕРИТЬ]`. Раздел Principles. Поиск: `OpenGitOps principles`.
- **Kubernetes** — `kubernetes.io`, разделы Declarative Management / Kustomize / ConfigMaps and Secrets. Поиск: `kustomize overlays patches`.
- **Kustomize** — `kustomize.io`, раздел Guides → Patches and Overlays. Поиск: `kustomize overlay patch replicas`.
- **GitLab** — `docs.gitlab.com`, разделы Project access tokens, CI/CD. Поиск: `gitlab project access token read_repository`.
- **Flux / Argo CD CLI reference** — соответствующие разделы на тех же доменах (`argocd app sync`, `flux reconcile`). Поиск: `argocd cli command reference`, `flux cli reference`.
