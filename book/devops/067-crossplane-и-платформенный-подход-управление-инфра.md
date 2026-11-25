# Crossplane и платформенный подход: управление инфраструктурой через Kubernetes API

## 1. Введение (зачем это нужно в реальной работе)

Тикет в учебном трекере «Вектор»:

> **Пользователь:** Сидоров (разработчик, склад)
> **ВМ:** рабочая станция разработчика → кластер `k8s` (k8s-cp01)
> **Что написал:** «Мне на каждый тестовый стенд нужно руками писать Deployment, Service, Namespace и ждать, пока ты настроишь ingress. Уже третий раз забыли label `env`. Сделайте так, чтобы я одной командой получал окружение — мне не нужен твой Kubernetes, мне нужен мой тестовый стенд».
> **Срочность:** средняя.

Вот это «сделайте одну команду» — и есть платформенный подход. Платформенная команда (ты) один раз описывает, как выглядит правильное окружение, публикует это как обычный объект Kubernetes, а разработчик применяет `kubectl apply -f my-env.yaml` и получает всё готовое. Никакого доступа к Terraform-коду, никакого доступа к облаку — только узкий, безопасный API.

Crossplane — это инструмент, который позволяет так делать. Он ставится в Kubernetes и превращает его в универсальную панель управления: Kubernetes API становится API управления всей инфраструктурой, а не только подами. Ты после главы будешь понимать, из чего состоит Crossplane (провайдеры, функции, XRD, Composition, claim), как собрать свой «платформенный API» с нуля на стенде, как отлаживать тикет вида «мой ресурс в `Synced=False`» и как честно объяснить на собеседовании, когда Crossplane нужен, а когда хватит Terraform.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Идея в одном предложении.** Crossplane — это набор контроллеров внутри Kubernetes, которые работают по тому же циклу, что и обычный deployment-контроллер: «сравнить желаемое состояние (spec) с наблюдаемым (status) и дотянуть наблюдаемое до желаемого, бесконечно, раз в N секунд».

**Слои (снизу вверх).**

1. **Provider** — пакет, который приносит в кластер новые CRD и контроллеры для конкретной внешней системы: `provider-kubernetes` (управляет объектами самого Kubernetes), `provider-helm` (управляет Helm-релизами), `provider-vault`, community-провайдеры под Yandex Cloud / VK Cloud и т.п. Пакет — это OCI-образ, поэтому его можно зеркалировать в `HARBOR01`.
2. **Managed Resource (MR)** — CRD провайдера, описывающий один конкретный внешний объект (`Object`, `Release`, виртуалка, бакет). У каждого MR есть `spec.forProvider` (что хотим), `status.atProvider` (что видим), `status.conditions` (`Ready`, `Synced`), `metadata.annotations["crossplane.io/external-name"]` (имя объекта во внешней системе) и `spec.deletionPolicy` (`Delete`/`Orphan`) — аналог режимов удаления в Terraform.
3. **XRD (CompositeResourceDefinition)** — CRD, который ты публикуешь для своих клиентов. Это «схема твоего продукта»: `kind: XAppEnv`, `group: platform.vektor.lab`, поля `size`, `image`.
4. **Composition** — рецепт: из одного `XAppEnv` сделать N MR-ов (Namespace + Deployment + Service), с патчами и трансформациями полей.
5. **Claim (namespaced)** — то, что применяет разработчик. Claim — это «обёртка» над cluster-scoped composite resource (XR) в его namespace: разработчику не нужно прав на весь кластер.

> **Важно про версии.** В Crossplane v1 канон — «XRD + Composition + XR + Claim». В Crossplane v2 линия развития — namespaced composite resources и упрощение модели claims `[ПРОВЕРИТЬ]`. В лабораторной ниже используется модель v1 — она всё ещё основная в документации и в вакансиях; при установке смотри `kubectl get crds` и документацию к конкретной версии.

Схема стенда (DevOps-трек, кластер 10.10.10.61–.63):

```text
┌─────────────── k8s-cp01 / k8s-w01 / k8s-w02 (Kubernetes) ─────────────────┐
│                                                                            │
│  sidorov (разработчик)                    ты (платформа)                   │
│  kubectl apply -f AppEnv.yaml ──┐        храни.yml в GIT01, ревью          │
│                                 ▼                                          │
│                     kube-apiserver :6443  (HTTPS/REST, L7)                 │
│                                 │                                          │
│   ┌─────────────────────────────┴──────────────────────────────┐          │
│   │ namespace crossplane-system                                 │          │
│   │   deploy/crossplane           — ядро: XRD, Composition,     │          │
│   │                                  реконсиляция XR/claims      │          │
│   │   deploy/provider-kubernetes  — контроллер MR `Object`      │          │
│   │   deploy/provider-helm        — контроллер MR `Release`     │          │
│   │   deploy/function-patch-and-  — gRPC-сервис :9443           │          │
│   │     transform [ПРОВЕРИТЬ]       (компиляция Composition)    │          │
│   └─────────────────────────────────────────────────────────────┘          │
│                                 │                                          │
│        управляемые объекты: Namespace env-*, Deployment app, ...           │
│        (внешняя система может быть и вне кластера — облако, vSphere)       │
└────────────────────────────────────────────────────────────────────────────┘
```

**Протоколы и порты.** Всё общение идёт через `kube-apiserver:6443`: контроллеры делают `LIST`/`WATCH` (HTTP/2, TLS), это слой L7 модели OSI. Провайдеры к внешним облакам ходят по HTTPS/443 наружу. Composition-функции — gRPC-сервисы внутри кластера (по умолчанию порт 9443) `[ПРОВЕРИТЬ]`. Отдельного «порта Crossplane» не существует — это и есть главная мысль: протокол управления один, Kubernetes API.

**Чем это отличается от Terraform (урок 61).** У Terraform есть файл состояния — единственный источник истины и боли при конфликтах. У Crossplane состояния нет: желаемое лежит в etcd кластера, наблюдаемое — в `status` каждого MR, а привязка к реальному объекту — через аннотацию `crossplane.io/external-name`. Зато есть постоянная реконсиляция: если кто-то поменял объект руками, Crossplane это заметит и вернёт как было (тот самый дрейф из урока 59, только побеждённый по умолчанию). По умолчанию период опроса провайдера — около минуты, меняется флагом `--poll-interval` `[ПРОВЕРИТЬ]`.

**Платформенный подход** — это не про инструмент, а про продукт: у внутренней команды появляется «золотой путь» (golden path) — готовый, безопасный, поддерживаемый способ получить окружение. Crossplane — один из способов такой путь реализовать. Альтернативы: Terraform-модули + пайплайн (урок 60–62), Helm-чарты, Backstage как портал поверх всего этого.

## 3. Аналогия из жизни

Представь столовую в бизнес-центре. Разработчик — сотрудник, который хочет обедать. Он не заходит на кухню, не крутит ручки на плите и не решает, какая мощность у конфорки: он видит **меню** (XRD) и делает **заказ** (claim) — «комплексный обед, размер большой» (`size: large`). Повар (Composition) по рецепту превращает заказ в набор действий: достать тарелку (Namespace), положить первое и второе (Deployment), добавить приборы (Service). Кладовщик (Provider) реально ходит на склад и приносит продукты; если продукт протух, кладовщик пишет в накладной «не смог» (`Synced=False`).

Ключевое: меню и рецепты меняет только кухня. Сотрудник не может заказать «борщ, но без свёклы, и чтобы кастрюля была моя личная» — а если и может, то только в рамках того, что кухня разрешила в меню. Именно это даёт платформенному подходу управляемость.

Аналогия перестаёт работать в одном месте: в столовой заказ выполняется один раз, а Crossplane перепроверяет соответствие заказа и блюда каждую минуту и до конца жизни кластера — и если кто-то утащил тарелку, он молча принесёт новую.

## 4. Практика и команды

### 4.1. Установка Crossplane через Helm

```bash
helm repo add crossplane-stable https://charts.crossplane.io/stable
helm repo update
helm search repo crossplane-stable/crossplane --versions | head -5
helm install crossplane crossplane-stable/crossplane \
  --namespace crossplane-system --create-namespace \
  --version <актуальная из списка выше>
```

`--create-namespace` создаёт namespace `crossplane-system`, `--version` фиксирует версию — в платформенной команде версии фиксируют всегда, иначе «вчера работало» превращается в инцидент. Без `--version` поставится последняя.

### 4.2. Проверка, что ядро живо

```bash
kubectl -n crossplane-system get pods
kubectl get crds | grep crossplane.io
kubectl api-resources | grep -i crossplane
```

Ожидаемый вывод (сокращённо):

```text
NAME                      READY   STATUS    RESTARTS
crossplane-6b9c…          1/1     Running   0
crossplane-rbac-manager…  1/1     Running   0

compositions.apiextensions.crossplane.io   2024-…
compositeresourcedefinitions…             2024-…
providerconfigs.kubernetes.crossplane.io  2024-…
```

Смотрим: `Running` у двух подов и наличие CRD `compositions` / `compositeresourcedefinitions`. Если под в `CrashLoopBackOff` — `kubectl -n crossplane-system logs deploy/crossplane`.

### 4.3. Провайдер: приносим умение управлять объектами Kubernetes

```yaml
# provider-kubernetes.yaml
apiVersion: pkg.crossplane.io/v1
kind: Provider
metadata:
  name: provider-kubernetes
spec:
  package: xpkg.upbound.io/crossplane-contrib/provider-kubernetes:v0.14.0  # [ПРОВЕРИТЬ] актуальную версию
```

```bash
kubectl apply -f provider-kubernetes.yaml
kubectl get providers
kubectl -n crossplane-system get pods -l pkg.crossplane.io/provider=provider-kubernetes
```

```text
NAME                  INSTALLED   HEALTHY   PACKAGE
provider-kubernetes   True        True      xpkg.upbound.io/…:v0.14.0
```

Смотреть столбец `HEALTHY`. `INSTALLED=True, HEALTHY=False` — чаще всего образ не скачался (сеть/реестр). В РФ полезно зеркалировать пакет в `HARBOR01` и указать в `spec.package` адрес `harbor.vektor.lab/crossplane/provider-kubernetes:v0.14.0` — пакеты Crossplane это обычные OCI-образы.

### 4.4. ProviderConfig: от чьего имени провайдер действует

```yaml
apiVersion: kubernetes.crossplane.io/v1alpha1
kind: ProviderConfig
metadata:
  name: in-cluster
spec:
  credentials:
    source: InjectedIdentity
```

`InjectedIdentity` — провайдер использует свою ServiceAccount внутри кластера. Разбор: это аналог «профиля доступа» в Terraform-провайдере, только без файлов с ключами в git. Другие варианты — `Secret`/`None` [+ свой kubeconfig].

### 4.5. Функция компиляции Composition

```yaml
apiVersion: pkg.crossplane.io/v1
kind: Function
metadata:
  name: function-patch-and-transform
spec:
  package: xpkg.upbound.io/crossplane-contrib/function-patch-and-transform:v0.7.0  # [ПРОВЕРИТЬ]
```

```bash
kubectl get functions
```

Функция — это под, который по gRPC получает от ядра вход XR и возвращает список объектов. Именно она реализует привычные `patches`/`transforms`.

### 4.6. Публикуем свой API: XRD

```yaml
# xrd-appenv.yaml
apiVersion: apiextensions.crossplane.io/v1
kind: CompositeResourceDefinition
metadata:
  name: xappenvs.platform.vektor.lab
spec:
  group: platform.vektor.lab
  names:
    kind: XAppEnv          # cluster-scoped «продукт»
    plural: xappenvs
  claimNames:
    kind: AppEnv           # то, что видят разработчики (namespaced)
    plural: appenvs
  versions:
    - name: v1alpha1
      served: true
      referenceable: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              properties:
                size:
                  type: string
                  enum: [small, large]
                image:
                  type: string
              required: [size, image]
```

```bash
kubectl apply -f xrd-appenv.yaml
kubectl get xrd
kubectl api-resources | grep platform.vektor.lab
kubectl explain xappenv.spec
```

```text
NAME                            ESTABLISHED   OFFERED
xappenvs.platform.vektor.lab    True          True
```

`ESTABLISHED` — CRD создан, `OFFERED` — появился claim (`appenvs`). Если `OFFERED` пусто, смотри `kubectl describe xrd` → события: обычно ошибка в схеме или в `claimNames`. `kubectl explain` — лучший способ проверить, что твой API вообще документирован.

### 4.7. Composition: рецепт «из одного заказа — три объекта»

```yaml
# composition-appenv.yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: xappenvs-kubernetes
spec:
  compositeTypeRef:
    apiVersion: platform.vektor.lab/v1alpha1
    kind: XAppEnv
  mode: Pipeline
  pipeline:
    - step: patch-and-transform
      functionRef:
        name: function-patch-and-transform
      input:
        apiVersion: pt.fn.crossplane.io/v1beta1
        kind: Resources
        resources:
          - name: namespace
            base:
              apiVersion: kubernetes.crossplane.io/v1alpha1
              kind: Object
              spec:
                providerConfigRef: { name: in-cluster }
                forProvider:
                  manifest:
                    apiVersion: v1
                    kind: Namespace
                    metadata:
                      name: ""
                      labels: { app: "", size: "" }
            patches:
              - type: FromCompositeFieldPath
                fromFieldPath: metadata.name
                toFieldPath: spec.forProvider.manifest.metadata.name
                transforms:
                  - type: string
                    string: { type: Format, fmt: "env-%s" }
              - type: FromCompositeFieldPath
                fromFieldPath: metadata.name
                toFieldPath: spec.forProvider.manifest.metadata.labels.app
              - type: FromCompositeFieldPath
                fromFieldPath: spec.size
                toFieldPath: spec.forProvider.manifest.metadata.labels.size
            readinessChecks:
              - type: MatchString
                fieldPath: status.atProvider.manifest.status.phase  # [ПРОВЕРИТЬ]
                matchString: Active

          - name: deployment
            base:
              apiVersion: kubernetes.crossplane.io/v1alpha1
              kind: Object
              spec:
                providerConfigRef: { name: in-cluster }
                forProvider:
                  manifest:
                    apiVersion: apps/v1
                    kind: Deployment
                    metadata: { name: app, namespace: "", labels: { app: "" } }
                    spec:
                      replicas: 1
                      selector: { matchLabels: { app: app } }
                      template:
                        metadata: { labels: { app: app } }
                        spec:
                          containers:
                            - { name: app, image: "" }
            patches:
              - type: FromCompositeFieldPath
                fromFieldPath: metadata.name
                toFieldPath: spec.forProvider.manifest.metadata.namespace
                transforms:
                  - type: string
                    string: { type: Format, fmt: "env-%s" }
              - type: FromCompositeFieldPath
                fromFieldPath: spec.image
                toFieldPath: spec.forProvider.manifest.spec.template.spec.containers[0].image
              - type: FromCompositeFieldPath
                fromFieldPath: spec.size
                toFieldPath: spec.forProvider.manifest.spec.replicas
                transforms:
                  - type: map
                    map: { small: "1", large: "3" }
                  - type: convert
                    convert: { toType: int }   # [ПРОВЕРИТЬ] поддержку convert в вашей версии функции
```

Разбор: `FromCompositeFieldPath` = «взять поле из XR → положить в MR»; `transforms` = преобразования по порядку (формат строки, map-таблица значений, приведение типа); `readinessChecks` задают порядок — Deployment поднимется только после того, как Namespace станет `Active`. Важно: в `base` мы заранее объявляем поля (`labels.size`, `replicas`, `containers[0].image`), чтобы патчу было что заменять.

```bash
kubectl apply -f composition-appenv.yaml
kubectl get composition
kubectl describe composition xappenvs-kubernetes | tail -20
```

В `describe` смотри `Type: Patch-and-Transform` и статус функции: если функция не готова, Composition не скомпилируется.

### 4.8. Заказ от разработчика (claim)

```yaml
apiVersion: platform.vektor.lab/v1alpha1
kind: AppEnv
metadata:
  name: sidorov-demo
  namespace: default
spec:
  compositionRef:
    name: xappenvs-kubernetes
  size: small
  image: harbor.vektor.lab/library/demo:1.0
```

```bash
kubectl apply -f appenv-sidorov.yaml
kubectl get appenvs -A
kubectl get xappenvs
kubectl get managed
kubectl get objects.kubernetes.crossplane.io
```

```text
NAMESPACE   NAME            SYNCED   READY
default     sidorov-demo    True     True

NAME                    SYNCED   READY   COMPOSITION
sidorov-demo-8xk2p      True     True    xappenvs-kubernetes

NAME                                  KIND        PROVIDERCONFIG   SYNCED  READY
sidorov-demo-8xk2p-…-namespace        Object      in-cluster       True    True
sidorov-demo-8xk2p-…-deployment       Object      in-cluster       True    True
```

`kubectl get managed` — «все управляемые ресурсы всех провайдеров», самая полезная команда в отладке. `SYNCED` — удалось ли применить желаемое, `READY` — соответствует ли внешний объект ожидаемому.

### 4.9. Отладка: где именно сломалось

```bash
kubectl describe appenv sidorov-demo | sed -n '/Conditions/,$p'
kubectl describe xappenv sidorov-demo-8xk2p | tail -30
kubectl describe object sidorov-demo-8xk2p-…-deployment | tail -30
kubectl -n crossplane-system logs deploy/crossplane --tail=50
kubectl -n crossplane-system logs deploy/provider-kubernetes-… --tail=50
```

Правило: идём сверху вниз — claim → XR (сработала ли Composition) → MR (дошло ли до внешней системы) → логи провайдера.

### 4.10. Пауза и удаление

```bash
kubectl annotate xappenv sidorov-demo-8xk2p crossplane.io/paused=true
kubectl patch object sidorov-demo-8xk2p-…-deployment --type=merge \
  -p '{"spec":{"deletionPolicy":"Orphan"}}'
kubectl delete appenv sidorov-demo
```

`paused=true` замораживает реконсиляцию (удобно при ручной отладке, но именно так появляется дрейф). `Orphan` при удалении заказа оставит реальный объект жить — в проде это иногда осознанный выбор («сначала перенеси данные»), и это надо согласовывать.

### 4.11. Что вообще есть в кластере

```bash
kubectl get providers,functions,xrds,compositions
kubectl get crds -l app.kubernetes.io/managed-by=crossplane   # [ПРОВЕРИТЬ] метку
```

Быстрый обзор всей платформы в одном экране — пригодятся, когда вернёшься к стенду через месяц.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер Kubernetes из DevOps-трека готов: `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63), `kubectl` настроен на управляющем узле. Если кластера ещё нет — вернись к уроку про установку кластера и развёртывание kubeadm, без него лабораторная не имеет смысла.
- `GIT01` (10.10.10.50) — там создаёшь репозиторий `platform/crossplane` для всех манифестов этой главы.
- `HARBOR01` (10.10.10.55) — опционально, для зеркалирования пакетов `xpkg.upbound.io`.
- Helm 3 установлен на `k8s-cp01`, с узлов есть доступ в интернет через `10.10.10.2` (NAT VMnet8).
- Ты — администратор кластера. Учётка `adm.student` для этой лабы не нужна: работаем в Linux.

### Снапшот

В VMware Workstation — снапшот **всех трёх узлов кластера**:

```text
k8s-cp01 → VM → Snapshot → Take Snapshot → имя: before-devops-067
k8s-w01  → то же самое
k8s-w02  → то же самое
```

Снапшот `k8s-cp01` делай **первым**, узлы воркеров можно после, но помни: откат только control-plane без воркеров оставит «висящие» объекты — откатывать надо все три.

### Шаги

1. **k8s-cp01.** `helm repo add crossplane-stable https://charts.crossplane.io/stable && helm repo update`. Ожидаемо: `"crossplane-stable" has been added`. Если таймаут — проверь `ping 10.10.10.2` и DNS.
2. **k8s-cp01.** `helm install crossplane crossplane-stable/crossplane -n crossplane-system --create-namespace`. Ожидаемо: `STATUS: deployed`. Затем `kubectl -n crossplane-system get pods` → два пода `Running`.
3. **k8s-cp01.** Создай в репозитории файл `provider-kubernetes.yaml` (раздел 4.3), `kubectl apply -f` его. Ожидаемо через 1–2 минуты: `kubectl get providers` → `INSTALLED True, HEALTHY True`.
4. **k8s-cp01.** Примени `ProviderConfig in-cluster` (4.4) и `Function function-patch-and-transform` (4.5). Проверь: `kubectl get functions` → `INSTALLED True, HEALTHY True`. Здесь частая заминка — функция ставится дольше провайдера, подожди.
5. **k8s-cp01.** Примени `xrd-appenv.yaml` (4.6). Проверь `kubectl get xrd` → `ESTABLISHED True, OFFERED True`. Теперь `kubectl api-resources | grep platform.vektor.lab` должен показать **два** ресурса: `xappenvs` и `appenvs`.
6. **k8s-cp01.** Примени `composition-appenv.yaml` (4.7). Проверь `kubectl describe composition xappenvs-kubernetes` → в конце блока `Status` нет ошибок функции.
7. **k8s-cp01.** Примени claim `appenv-sidorov.yaml` (4.8) в namespace `default`. Ожидаемо: `kubectl get appenvs -A` → `SYNCED True READY True` в течение ~1 минуты.
8. **k8s-cp01.** Убедись, что инфраструктура реально появилась: `kubectl get ns env-sidorov-demo`, `kubectl -n env-sidorov-demo get deploy,po`. Ожидаемо: namespace `env-sidorov-demo` с метками `app=sidorov-demo`, `size=small`, и Deployment `app` с 1 подом.
9. **k8s-cp01.** Проверь масштабирование через API платформы: измени claim — `kubectl patch appenv sidorov-demo --type=merge -p '{"spec":{"size":"large"}}'`. Ожидаемо: через ~минуту `kubectl -n env-sidorov-demo get deploy app` → `READY 3/3`, метка `size=large` на namespace тоже обновилась.
10. **k8s-cp01.** Проверь дрейф (это ключевой момент главы): руками `kubectl -n env-sidorov-demo scale deploy app --replicas=1`. Подожди 1–2 минуты. Ожидаемо: Crossplane вернёт `3`. То же самое произойдёт, если удалить namespace руками — он пересоздастся.
11. **k8s-cp01.** Закоммить все манифесты в `GIT01` в репозиторий `platform/crossplane`, тегом/коммитом зафиксируй состояние. Ожидаемо: `git log --oneline -1` показывает твой коммит; в README репозитория — короткая инструкция «как заказать окружение» для разработчика.
12. **k8s-cp01.** Запиши в `stand-log.md`: установлен Crossplane (версия), провайдер, функция, группа API `platform.vektor.lab/v1alpha1`, пример claim.

### Проверка

- [ ] `kubectl -n crossplane-system get pods` — все поды `Running`, без рестартов.
- [ ] `kubectl get providers` и `kubectl get functions` — `HEALTHY True`.
- [ ] `kubectl get xrd` — `ESTABLISHED True, OFFERED True`.
- [ ] `kubectl api-resources | grep platform.vektor.lab` показывает и `xappenvs`, и `appenvs`.
- [ ] `kubectl get appenvs -A` — `READY True`.
- [ ] `kubectl get managed` показывает два `Object` в состоянии `READY True`.
- [ ] В кластере есть namespace `env-sidorov-demo` с Deployment из 3 реплик.
- [ ] Ручной `scale` до 1 откатывается Crossplane обратно до 3.
- [ ] Манифесты лежат в `GIT01`, в `stand-log.md` есть запись.

### Сломай и почини

> **Пользователь:** Сидоров (разработчик)
> **ВМ:** k8s-cp01 (работает через `kubectl` со своей машины)
> **Что написал:** «Создал второй стенд `AppEnv/sidorov-test2` — namespace появился, а Deployment нет. У первого стенда то же самое: поды пропали. Ничего не трогал. Срочность: высокая, показывать демо через час».
> **Срочность:** высокая.

**Как воспроизвести поломку.** От лица платформенной команды «почистили лишнее» и удалили функцию:

```bash
kubectl delete function function-patch-and-transform
kubectl apply -f appenv-sidorov-test2.yaml   # новый claim
```

**Диагностика по шагам.**

1. `kubectl get appenvs -A` — видим `SYNCED False READY False` у обоих. Значит проблема не в одном заказе, а в платформе: ядро не может скомпилировать Composition.
2. `kubectl describe appenv sidorov-test2 | tail -20` — в `Conditions` ищем `Reason` и `Message`. Типично: `ReconcileError`, текст про ненайденную функцию.
3. `kubectl get compositions` — смотрим `READY`. Если `False` — уже половина ответа.
4. `kubectl describe composition xappenvs-kubernetes | tail -30` — в статусе/событиях будет что-то вида `function "function-patch-and-transform" not found` `[ПРОВЕРИТЬ формулировку в вашей версии]`.
5. `kubectl get functions` — пусто. Причина найдена: удалён объект `Function`.
6. `kubectl -n crossplane-system logs deploy/crossplane --tail=100 | grep -i function` — подтверждение в логах ядра.
7. `kubectl -n env-sidorov-demo get all` — убеждаемся, что старый namespace и Deployment на месте (поды «пропали» из-за того, что Deployment не пересоздался после удаления, либо разработчик смотрел не туда — проверяем фактами, а не словами).

**Решение.**

```bash
kubectl apply -f function-patch-and-transform.yaml   # из репозитория на GIT01
kubectl get functions -w                             # ждём HEALTHY True
kubectl get xappenvs -w                              # ждём READY True
kubectl -n env-sidorov-demo get deploy,po
kubectl -n env-sidorov-test2 get deploy,po           # появился после починки
```

Дальше — вывод, который и есть смысл платформенного подхода: **никто не переделывал заказы**. Разработчик не менял свои манифесты, ты не «пересоздавал окружения» — восстановление одного объекта платформы привело к автоматическому пересозданию всех ресурсов у всех клиентов. Это надо написать в тикете.

**Ответ пользователю:**

> Алексей, причина найдена: на стороне платформы был удалён компонент, который превращает заказ `AppEnv` в реальные объекты Kubernetes. Восстановили, оба стенда (`sidorov-demo`, `sidorov-test2`) снова в состоянии `READY`. Твои манифесты менять не нужно. Проверь, пожалуйста, `kubectl -n env-sidorov-test2 get deploy` — должен быть 1 под. Если через 5 минут не появится — открой тикет заново со ссылкой на этот.

**Закрытие заявки:** категория «платформа / Crossplane», в решении — что именно удалили, время восстановления, и **отдельным пунктом** — организационный вывод: удаление `Function`/`Provider`/`Composition` в общем кластере должно идти только через git-ревью, а `kubectl delete` на платформенные объекты стоит закрыть RBAC-ом. Запись в `stand-log.md` обязательна.

### Что изменилось на стенде

- На `k8s-cp01` (кластер `10.10.10.61–.63`) установлен Crossplane в namespace `crossplane-system`.
- Установлены: провайдер `provider-kubernetes`, `ProviderConfig/in-cluster`, функция `function-patch-and-transform`.
- Появилась собственная группа API `platform.vektor.lab/v1alpha1` с ресурсами `XAppEnv` (cluster-scoped) и `AppEnv` (namespaced).
- Существует Composition `xappenvs-kubernetes` (Namespace + Deployment).
- Существуют заказы: `default/AppEnv sidorov-demo`, `default/AppEnv sidorov-test2`; созданы namespaces `env-sidorov-demo`, `env-sidorov-test2`.
- На `GIT01` создан репозиторий `platform/crossplane` с манифестами XRD, Composition, Provider, ProviderConfig, Function и примером claim.
- В `stand-log.md` добавлена запись о платформенном API и версиях компонентов.

### Откат

Полный откат — снапшот `before-devops-067` на всех трёх узлах (Power off → Snapshot Manager → Restore). Если откат нежелателен, а нужно просто убрать за собой:

```bash
kubectl delete appenvs -A --all
kubectl delete xappenvs --all
kubectl delete compositions,xrds --all
kubectl delete functions,providers --all
helm -n crossplane-system uninstall crossplane
kubectl get crd | grep -E 'crossplane.io|platform.vektor.lab' | awk '{print $1}' | xargs kubectl delete crd
kubectl delete ns env-sidorov-demo env-sidorov-test2 crossplane-system
```

Порядок важен: сначала заказы и XR, потом XRD, потом helm, потом CRD. Если удалить CRD первыми, Kubernetes удалит все объекты этих типов разом, и ты потеряешь понимание, что происходило. Удаление CRD — операция, которую в боевом кластере делают только с бэкапом etcd.

## 6. Ошибки новичков и безопасность

1. **Ставить Crossplane в боевой кластер «посмотреть».** → Провайдеры получают права на создание объектов во всём кластере; ошибка в Composition легко создаёт сотни ресурсов. → Сначала отдельный кластер/namespace-скоуп, потом прод, и обязательно через git + ревью.
2. **Путать claim и XR.** → Разработчик правит `XAppEnv` напрямую (а у него нет на это прав или он ломает общий объект), заявки перестают быть изолированными. → Клиентам — только `AppEnv` в их namespace, `XAppEnv` — cluster-scoped и только для платформенной команды.
3. **Патчить поля, которых нет в `base`.** → Composition применяется, но MR остаётся с пустым/дефолтным значением, и ты полчаса ищешь, почему `image` не подставился. → В `base` заранее объявляй все поля, которые будешь патчить, и проверяй через `kubectl get object -o yaml`.
4. **Не задать `providerConfigRef`.** → MR в состоянии `Synced=False`, в событиях — про отсутствующий ProviderConfig. → Всегда указывать `spec.providerConfigRef.name` явно, даже если он «единственный».
5. **Хранить kubeconfig/ключи облака в git рядом с Composition.** → Утечка = полный доступ к инфраструктуре. → Провайдеры должны работать через `InjectedIdentity` или IRSA/Workload Identity, а секреты подключать по схеме из урока 66.
6. **Считать, что Crossplane «не управляет» объектом, потому что «я так захотел».** → Ручные правки откатываются, люди думают, что «платформа глючит». → Либо `managementPolicies`/`deletionPolicy: Orphan` осознанно и с комментарием в git, либо не трогать руками.
7. **Не фиксировать версии провайдеров и функций.** → «Вчера работало»: обновился minor-релиз, поменялось поведение патчей. → `spec.package` с тегом, обновление — отдельным PR и отдельным тестом.
8. **Обещать разработчикам «любой ресурс по запросу».** → Платформа превращается в свалку, никто не понимает, что где живёт. → Меню (XRD) должно быть маленьким и покрывать 80% кейсов; остальное — через тикет с явным решением.

### Безопасность

- На стенде ты даёшь `provider-kubernetes` права через `InjectedIdentity` и, скорее всего, на весь кластер. **В боевой среде так нельзя**: провайдер должен работать с минимально достаточными RBAC-правами в рамках namespace-скоупа, а в идеале — с отдельным кластером-платформой.
- Учебный пароль стенда `Lab#2026-vektor` в манифестах не появляется вообще — и это правильно; если появится, считай это инцидентом.
- `crossplane.io/paused=true` и `deletionPolicy: Orphan` — «стоп-краны», которые в проде включаются только по регламенту: забытый `paused` = дрейф, который никто не заметит.
- Отключать сетевые политики или давать провайдеру `cluster-admin` «чтобы быстрее заработало» — типичный антипаттерн; вместо этого сузь API (меньше полей в XRD), а не расширяй права.

## 7. Вопросы на собеседовании

**1. Чем Crossplane отличается от Terraform и когда вы выберете Crossplane?**

Ответ: Terraform — CLI с файлом состояния и запуском «по требованию», лучший инструмент, когда нужно разово/по пайплайну создать инфраструктуру и когда важна поддержка экзотических провайдеров. Crossplane — контроллер в Kubernetes, работает по reconciliation loop, без отдельного state-файла (желаемое в etcd, наблюдаемое в `status` MR, привязка через `crossplane.io/external-name`), с постоянным устранением дрейфа и с возможностью выдать разработчику узкий self-service API (claim). Crossplane выбирают, когда есть Kubernetes, есть много однотипных окружений, нужна мультитенантность и «платформа как продукт»; Terraform — когда инфраструктуры мало, она меняется редко или Kubernetes-кластера нет. На практике они часто живут вместе: Terraform (или Pulumi) создаёт кластеры, Crossplane управляет тем, что внутри них.
*Что хочет услышать интервьюер:* понимание state vs reconciliation, что Crossplane — не «Terraform, но модный», и что выбор зависит от контекста.
*Красный флаг:* «Crossplane полностью заменяет Terraform, Terraform устарел».

**2. Объясните связку XRD → Composition → Claim. Зачем три уровня, а не один?**

Ответ: XRD публикует новый CRD — это контракт/меню платформы (`XAppEnv` + namespaced claim `AppEnv`). Composition — рецепт, как из одного XR собрать набор managed resources (Namespace, Deployment, Helm Release) с патчами, трансформациями и readiness-checks. Claim — namespaced представление XR: разработчик в своём namespace создаёт `AppEnv`, ядро создаёт cluster-scoped `XAppEnv` и привязывает их, а права разработчика не выходят за его namespace. Три уровня дают разделение ролей: клиент описывает «что мне нужно» маленькой схемой, платформа решает «как это сделать», и одно изменение Composition мгновенно применяется ко всем заказам.
*Что хочет услышать интервьюер:* понимание абстракции и разграничения доступа, а не просто список CRD.
*Красный флаг:* «Claim — это то же самое, что XR, просто в namespace» без объяснения прав и жизненного цикла.

**3. Что будет, если инженер руками изменит объект, созданный Crossplane?**

Ответ: провайдер заметит расхождение на следующей итерации reconcile (по умолчанию порядка минуты, флаг `--poll-interval`) и вернёт объект к состоянию, описанному в MR. То же с удалением: объект пересоздастся, если `deletionPolicy: Delete` и он управляется. Если нужно временно заморозить — аннотация `crossplane.io/paused=true` на XR/claim, но это создаёт дрейф и в проде требует регламента. Если объект нужно оставить жить после удаления заказа — `deletionPolicy: Orphan`, осознанно.
*Что хочет услышать интервьюер:* что кандидат понимает: постоянная реконсиляция — главная сила и главная ловушка Crossplane.
*Красный флаг:* «Crossplane ничего не заметит» или «придётся запускать `terraform apply` руками».

**4. Где Crossplane хранит состояние и как «подцепить» уже существующий объект?**

Ответ: отдельного файла состояния нет — описания лежат в etcd в виде CR, наблюдаемое состояние — в `status` каждого MR (`atProvider`, `conditions`), а соответствие реальному объекту — через аннотацию `crossplane.io/external-name`. Импорт/адопция существующего объекта: создать MR с нужным именем и проставить `crossplane.io/external-name` реального объекта, провайдер при следующей реконсиляции подхватит его и начнёт управлять. Плюс — нет проблемы «state-файл потерялся/заблокирован»; минус — etcd надо бэкапить так же серьёзно, как state-файл Terraform.
*Что хочет услышать интервьюер:* что кандидат не переносит terraform-привычки механически и понимает цену «state в etcd».
*Красный флаг:* «У Crossplane есть `terraform.tfstate`» или «состояние вообще не нужно».

**5. Что такое platform engineering и какие риски у платформенного подхода?**

Ответ: platform engineering — это когда внутренняя команда делает для продуктовых команд self-service платформу с «золотыми путями»: готовые окружения, CI/CD-шаблоны, наблюдаемость, безопасность по умолчанию, — чтобы разработчик не заводил тикет на каждый namespace. Инструменты: Crossplane, Helm-чарты, Backstage как портал, Argo CD, политики (OPA/Conftest из урока 65). Риски: платформа становится «бутылочным горлышком» (все ждут одну команду); абстракция протекает — разработчику нужен нестандартный случай, и он уходит в обход; слишком много свободы в API (тот же «любой ресурс по запросу») — получается свалка; платформа без документации и метрик использования просто не приживается. Поэтому меню должно быть маленьким, изменение — через git-ревью, а метрики adoption и time-to-first-environment — обязательны.
*Что хочет услышать интервьюер:* продуктовое мышление и понимание организационной части, а не только YAML.
*Красный флаг:* «Мы просто поставили Crossplane, и теперь у нас платформа».

## 8. Источники (официальная документация)

- **docs.crossplane.io** — основной сайт документации Crossplane. Разделы: «What is Crossplane?», «Composition», «Composite Resource Definitions», «Providers», «Functions». Поисковые фразы: `crossplane composition patch and transform`, `crossplane composition functions`.
- **docs.crossplane.io** — раздел про провайдеры и ProviderConfig, а также «Crossplane vs Terraform» / «FAQ». Поиск: `crossplane provider kubernetes ProviderConfig InjectedIdentity`.
- **github.com/crossplane-contrib/provider-kubernetes** — README и `examples/` провайдера: точный синтаксис `Object`, `status.atProvider.manifest`, поддерживаемые поля. Поиск: `provider-kubernetes Object manifest example`.
- **github.com/crossplane-contrib/function-patch-and-transform** — README с полным списком патчей, трансформаций (`map`, `string`, `convert`, `math`) и `readinessChecks`. Поиск: `function-patch-and-transform readinessChecks`.
- **kubernetes.io** — «Custom Resources» и «CustomResourceDefinitions», «Operator pattern»: база, без которой Composition не читается. Поиск: `kubernetes custom resource definition structural schema`.
- **xpkg.upbound.io** — реестр пакетов Crossplane (провайдеры и функции), актуальные теги версий. Поиск: `xpkg.upbound.io crossplane-contrib provider-kubernetes`.
- **cncf.io** — карточка проекта Crossplane в CNCF (статус проекта и governance) `[ПРОВЕРИТЬ актуальный статус]`. Поиск: `Crossplane CNCF project`.
- **harbor.io / goharbor.io** — документация Harbor про прокси-кэш и репликацию OCI-артефактов: как зеркалировать пакеты Crossplane внутрь периметра `[ПРОВЕРИТЬ раздел про proxy cache для OCI]`. Поиск: `harbor proxy cache OCI artifacts`.
