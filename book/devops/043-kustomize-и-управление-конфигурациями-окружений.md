# Kustomize и управление конфигурациями окружений

## 1. Введение (зачем это нужно в реальной работе)

Тикет в учебной поддержке «Вектора»:

> **Пользователь:** sidorov (Алексей Сидоров, склад — на стенде он же разработчик учебного приложения `vektor-web`)
> **ВМ:** UBU01
> **Что написал:** «Выкатили вчера новую версию в прод, а там включился debug, реплика одна вместо трёх и адрес в Ingress от dev. Кажется, я применил не тот файл. Можно сделать так, чтобы dev и prod не расползались?»
> **Срочность:** средняя

Проблема знакома всем, кто хоть раз держал больше одного окружения. Один и тот же Deployment отличается между dev и prod ровно тремя строчками: namespace, число реплик, тег образа. Но люди копируют папку `dev` в `prod`, правят копию руками, через месяц в проде оказывается забытый `LOG_LEVEL=debug`, а в dev — три реплики и лимиты из продакшена.

Kustomize решает это без шаблонизатора: у вас один «эталонный» набор YAML (base) и тонкие накладки на него (overlays), где написано только отличие. Никаких `{{ .Values }}`, никакой генерации строк — только обычный YAML, который можно читать и диффить.

После этой главы вы будете уметь: разложить манифесты на base и overlays, собрать конфигурацию без кластера (`kustomize build`), объяснить, откуда в именах ConfigMap берётся хэш, применять патчи двух видов, переиспользовать куски конфигурации через components и встроить всё это в GitOps. Это база для следующего урока про операторы и для реальной работы с Argo CD/Flux в проде.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Kustomize — это **клиентский рендерер YAML**. Он ничего не знает про кластер, не ходит в API-сервер (порт 6443) и не создаёт в кластере никаких своих объектов — ни CRD, ни контроллера. На вход подаётся каталог с файлом `kustomization.yaml`, на выходе — «плоский» многостраничный YAML, который потом применяется обычным `kubectl apply -f -`.

```text
Git-репозиторий k8s-configs
        │
        ├── base/                     «эталон» приложения
        │     kustomization.yaml
        │     deployment.yaml  service.yaml  ingress.yaml  index.html
        │
        └── overlays/
              ├── dev/   ──┐
              └── prod/  ──┤
                           │
              kustomize build overlays/dev
                           │
                           ▼
        итоговый YAML: namespace подставлен, хэши посчитаны,
        теги образов заменены, патчи применены
                           │
                           ▼
                  kubectl apply -f -   →  API-сервер :6443

Внутри одной сборки kustomize прогоняет конвейер:
  generators   configMapGenerator / secretGenerator
        ↓
  resources    base + файлы оверлея
        ↓
  transformers (порядок фиксирован внутри kustomize):
       patches → labels/annotations → namespace →
       namePrefix/nameSuffix → images → replicas → replacements
```

Ключевые понятия:

- **kustomization.yaml** — точка входа каталога. `apiVersion: kustomize.config.k8s.io/v1beta1`, `kind: Kustomization`. Поля: `resources`, `namespace`, `namePrefix`, `nameSuffix`, `labels`, `annotations`, `images`, `replicas`, `configMapGenerator`, `secretGenerator`, `generatorOptions`, `patches`, `components`, `replacements`, `helmCharts`.
- **base** — набор манифестов, который ничего не знает про окружение.
- **overlay** — каталог, который ссылается на base в `resources` и добавляет отличия.
- **Генераторы** (`configMapGenerator`) создают ConfigMap/Secret из файлов и литералов и добавляют к имени **хэш содержимого**, например `vektor-web-config-8m7b2c9d4f`. Ссылки на этот ConfigMap внутри Deployment/StatefulSet переписываются автоматически. Смысл: изменился конфиг → изменилось имя → изменился шаблон пода → Kubernetes сам сделал rolling update. Отключить можно через `generatorOptions.disableNameSuffixHash: true`.
- **Патчи двух видов**: *strategic merge patch* (тот же YAML, что и объект, только с нужными полями; списки объектов сливаются по ключу, например по `name` контейнера) и *JSON6902 patch* (список операций `add`/`replace`/`remove` по путям).
- **kubectl vs отдельный бинарник.** `kubectl kustomize` и `kubectl apply -k` используют версию kustomize, вкомпилированную в kubectl. Отдельный `kustomize` обновляется независимо, и версии могут расходиться — в CI это классический источник «у меня локально работает».

Чего Kustomize **не** делает: не хранит состояние релиза. У Helm есть `helm history` и `helm rollback`, у Kustomize — только Git. Откат = `git revert` + повторный `apply`.

## 3. Аналогия из жизни

Представьте квартиру от застройщика. Планировка, стены, проводка, сантехника — это **base**. Она одинаковая для всех трёх квартир в доме, и менять её в каждой отдельно — идиотизм.

Дальше приходят три бригады с **дизайн-проектами** (overlays). В московской квартире бригада ставит тёплые полы и меняет обои в спальне — и пишет в проекте ровно две строчки правок. В сочинской — кондиционер и плитку на балконе. Никто не перерисовывает всю квартиру заново: все правки накладываются на один и тот же чертёж застройщика. А если в базовом проекте застройщик поменяет проводку — это автоматически приедет во все три квартиры, что и есть главная ценность.

Аналогия ломается на двух вещах: оверлей может не только добавить, но и **удалить** кусок базы (`$patch: delete`), а хэш-суффиксы в именах ConfigMap — это уже чисто инженерная механика Kubernetes, которой в ремонте квартир нет вообще.

## 4. Практика и команды

**1. Сборка без кластера.** Главная команда: посмотреть, что реально уедет в кластер.

```bash
kubectl kustomize overlays/dev > /tmp/dev.yaml
kustomize build overlays/dev | grep -E "name:|namespace:|replicas:" | head -20
```

`kustomize build <dir>` — рендер каталога. `kubectl kustomize` — то же самое встроенной версией. Смотреть: появился ли `namespace`, посчитался ли хэш у ConfigMap, сколько реплик.

**2. Каркас kustomization.yaml.** Поля, которые нужны в 90% случаев:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:      # файлы, каталоги и другие kustomization
  - deployment.yaml
  - ../../base
namespace: vektor-dev
namePrefix: dev-          # ссылки внутри объектов переписываются
labels:
  - pairs:
      app.kubernetes.io/environment: dev
images:
  - name: 10.10.10.55/vektor/web
    newTag: 1.1.0
replicas:
  - name: vektor-web
    count: 3
```

`namespace` и `namePrefix` применяются ко всем ресурсам сборки. `labels` с `includeSelectors: true` дополнительно залезет в `selector` — туда лезть стоит только если объект ещё не создан, иначе поле неизменяемо.

**3. diff перед применением.** Обязательная привычка.

```bash
kubectl diff -k overlays/dev
kubectl apply -k overlays/dev --dry-run=server
```

`kubectl diff` делает server-side dry-run и печатает разницу — то, чего не хватает `apply`. Код возврата 1 означает «есть изменения», это не ошибка.

**4. Применение и удаление.**

```bash
kubectl apply -k overlays/prod
kubectl delete -k overlays/dev
kubectl rollout status deployment/vektor-web -n vektor-dev
```

`-k` = `--kustomize`, альтернатива `-f`. `delete -k` удаляет ровно то, что перечислено в сборке (namespace, если он не описан как ресурс, останется).

**5. Генератор ConfigMap и хэш.**

```bash
kubectl get cm -n vektor-dev
# NAME                          DATA   AGE
# vektor-web-config-8m7b2c9d4f  2      30s
# vektor-web-html-5c4d3f2a1b    1      30s
```

Хэш (10 символов) считается от содержимого. Ваш хэш будет другим — это нормально. Отключается так:

```yaml
generatorOptions:
  disableNameSuffixHash: true
```

**6. Патчи: стратегический и JSON6902.**

```yaml
patches:
  - path: patch-resources.yaml        # strategic merge, имя берётся из файла
  - target:
      kind: Deployment
      name: vektor-web
    patch: |-
      - op: replace
        path: /spec/replicas
        value: 5
```

Второй вариант — JSON6902, операции по путям. Учтите: списки без ключа слияния (например `spec.rules` у Ingress) стратегический патч заменяет **целиком**, а не сливает поэлементно.

**7. Замена образа и реплик без правки Deployment.**

```bash
kustomize edit set image 10.10.10.55/vektor/web:1.2.0
kustomize edit set replicas vektor-web=3
```

`edit` меняет сам `kustomization.yaml` на диске — удобно в CI-скриптах обновления тега.

**8. components — переиспользуемые куски.**

```yaml
# components/metrics-sidecar/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1alpha1
kind: Component
patches:
  - path: sidecar.yaml
```

```yaml
# в оверлее
components:
  - ../../components/metrics-sidecar
```

Отличие от base: component нельзя применить сам по себе, он только «примешивается» к сборке. Идеален для отладочных sidecar-контейнеров, которые нужны в dev, но не в prod.

**9. Остальные подкоманды edit.**

```bash
kustomize edit add resource service.yaml
kustomize edit set namespace vektor-prod
kustomize edit add patch --path patch.yaml --kind Deployment --name vektor-web  # [ПРОВЕРИТЬ] набор флагов в вашей версии
kustomize edit fix   # миграция устаревших полей (bases, commonLabels)
```

**10. Ограничение на выход из каталога и проверка версий.**

```bash
kustomize build --load-restrictor LoadRestrictionsNone overlays/dev
kustomize version
kubectl version --client
```

По умолчанию kustomize запрещает ссылаться на файлы выше корня kustomization — защита от случайного «подтягивания» чужих манифестов. `LoadRestrictionsNone` снимает запрет; в CI так делать не стоит без причины. Версии `kustomize` из бинарника и из kubectl могут отличаться — фиксируйте версию в пайплайне.

**11. Helm-чарты внутри Kustomize (alpha).**

```yaml
helmCharts:
  - name: nginx
    repo: https://charts.bitnami.com/bitnami   # доступность репозитория из РФ [ПРОВЕРИТЬ]
    releaseName: web
    valuesInline:
      replicaCount: 2
```

```bash
kustomize build --enable-helm overlays/dev
```

Флаг `--enable-helm` обязателен, `helm` должен быть в `PATH`. Функция alpha: для продакшена чаще берут Helm отдельно, а Kustomize оставляют на патчинг.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из уроков 33–34: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63) в статусе Ready.
- `kubectl`, настроенный на работу с кластером (на UBU01 или GIT01). Если не настроен: скопируйте `/etc/kubernetes/admin.conf` с `k8s-cp01` в `~/.kube/config` на UBU01.
- Установлен standalone-бинарник `kustomize` v5.x (скачать релиз с GitHub или `go install sigs.k8s.io/kustomize/kustomize/v5@latest`). Если не получается — всю лабу можно пройти на `kubectl kustomize`.
- Ingress-контроллер nginx из урока 38 работает.
- На HARBOR01 (10.10.10.55) есть проект `vektor` и образ `vektor/web:1.0.0`. Если нет — запушьте любой nginx-образ под этим именем или подставьте `nginx:1.27` (доступность Docker Hub из РФ [ПРОВЕРИТЬ]; легальная альтернатива — свой Harbor, что и есть правильный путь).
- Репозиторий `k8s-configs` на GIT01 (10.10.10.50). Если его нет — создайте пустой проект и работайте локально, коммит сделаете на шаге 9.

Всё делаем от учётки `adm.student`, пароль стенда `Lab#2026-vektor`. Это учебный стенд: пароли и `insecure-registry` здесь допустимы, в боевой среде — нет.

### Снапшот

В VMware Workstation на **k8s-cp01, k8s-w01, k8s-w02, GIT01, HARBOR01**: меню VM → Snapshot → Take Snapshot, имя `before-devops-043`. Галочку «Snapshot the virtual machine's memory» снимите — снапшот с памятью на живом кластере даёт неприятные сюрпризы при откате.

### Шаги

1. **UBU01.** Проверьте инструменты:
   ```bash
   kubectl get nodes
   kustomize version
   ```
   Ожидаемо: три узла в статусе `Ready`, версия kustomize `v5.x.x`.

2. **UBU01.** Создайте скелет репозитория:
   ```bash
   mkdir -p ~/k8s-configs/{base,components/metrics-sidecar,overlays/dev,overlays/prod}
   cd ~/k8s-configs
   ```
   Ожидаемо: пустое дерево каталогов.

3. **UBU01.** Напишите `base/`: `deployment.yaml` (образ `10.10.10.55/vektor/web:1.0.0`, порт 80, монтирование ConfigMap `vektor-web-html` в `/usr/share/nginx/html`, `envFrom` из `vektor-web-config`), `service.yaml` (ClusterIP, порт 80), `ingress.yaml` (host `web.corp.vektor.lab`, `ingressClassName: nginx`), `index.html` с текстом «vektor-web: base» и `base/kustomization.yaml`:
   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   resources:
     - deployment.yaml
     - service.yaml
     - ingress.yaml
   configMapGenerator:
     - name: vektor-web-html
       files:
         - index.html
     - name: vektor-web-config
       literals:
         - APP_ENV=base
         - LOG_LEVEL=info
   ```
   Ожидаемо: пять файлов в `base/`.

4. **UBU01.** Соберите base и посмотрите результат:
   ```bash
   kubectl kustomize base | head -40
   ```
   Ожидаемо: два ConfigMap с хэш-суффиксами, в Deployment ссылки на них уже с хэшем.

5. **UBU01.** Создайте `overlays/dev`: `kustomization.yaml` с `namespace: vektor-dev`, `resources: [../../base]`, `labels` (`app.kubernetes.io/environment: dev`), `images` → `newTag: 1.1.0-dev`, `configMapGenerator` для `vektor-web-config` с `behavior: merge` и литералами `APP_ENV=dev`, `LOG_LEVEL=debug`; отдельный `index.html` для dev; патч `patch-resources.yaml` (уменьшенные лимиты) и `patch-replicas.yaml` (`spec.replicas: 2`) с `target: {kind: Deployment, name: vektor-web}`.
   Ожидаемо: каталог `overlays/dev` с четырьмя файлами.

6. **UBU01.** Соберите dev и проверьте отличия:
   ```bash
   kubectl kustomize overlays/dev | grep -E "namespace:|replicas:|image:|APP_ENV|LOG_LEVEL"
   ```
   Ожидаемо: `namespace: vektor-dev`, `replicas: 2`, тег `1.1.0-dev`, значения `dev`/`debug`.

7. **UBU01.** Создайте namespaces и примените dev:
   ```bash
   kubectl create namespace vektor-dev
   kubectl diff -k overlays/dev | head -30
   kubectl apply -k overlays/dev
   kubectl get deploy,cm,ingress -n vektor-dev
   ```
   Ожидаемо: Deployment `vektor-web` с 2/2 подами, два ConfigMap с хэшами, Ingress на `web.corp.vektor.lab`.

8. **UBU01.** Сделайте `overlays/prod`: `namespace: vektor-prod`, `replicas: 3` через поле `replicas`, `images` → `newTag: 1.0.0`, `configMapGenerator` merge с `APP_ENV=prod`, `LOG_LEVEL=warn`, патч ресурсов с боевыми лимитами, патч Ingress на хост `web.corp.vektor.lab` (без `dev.`). Примените:
   ```bash
   kubectl create namespace vektor-prod
   kubectl apply -k overlays/prod
   kubectl get deploy -n vektor-dev -n vektor-prod 2>/dev/null || kubectl get deploy -A | grep vektor-web
   ```
   Ожидаемо: в `vektor-dev` 2 пода, в `vektor-prod` 3, хэши ConfigMap в окружениях разные.

9. **UBU01.** Закоммитьте в GitLab:
   ```bash
   git init && git add . && git commit -m "base + dev/prod overlays"
   git remote add origin http://10.10.10.50/vektor/k8s-configs.git
   git push -u origin master
   ```
   Ожидаемо: репозиторий виден в веб-интерфейсе GIT01.

10. **UBU01.** Смена версии через `edit`, как это делает CI:
    ```bash
    cd overlays/prod && kustomize edit set image 10.10.10.55/vektor/web:1.0.1
    git diff kustomization.yaml
    kubectl apply -k . && kubectl rollout status deployment/vektor-web -n vektor-prod
    ```
    Ожидаемо: в диффе одна строка, поды в prod переехали на новый тег. Не забудьте вернуть `1.0.0` или оставить как есть — это новое состояние стенда.

### Проверка

- [ ] `kubectl kustomize base` выдаёт YAML без ошибок и с хэшами в именах ConfigMap.
- [ ] `kubectl kustomize overlays/dev` содержит `namespace: vektor-dev` и `replicas: 2`.
- [ ] `kubectl kustomize overlays/prod` содержит `namespace: vektor-prod` и `replicas: 3`.
- [ ] В кластере два namespace: `vektor-dev` и `vektor-prod`, в каждом Deployment `vektor-web` в статусе Ready.
- [ ] Хэши ConfigMap в dev и prod различаются, значения `APP_ENV` тоже.
- [ ] В `vektor-dev` приложение отдаёт «dev»-страницу, в prod — базовую.
- [ ] `kubectl get ingress -A` показывает разные хосты для dev и prod.
- [ ] Репозиторий `k8s-configs` запушен на GIT01.

### Сломай и почини

> **Пользователь:** sidorov (Алексей Сидоров, разработчик `vektor-web`)
> **ВМ:** UBU01
> **Что написал:** «Поставил в dev три реплики вместо двух, применил — а подов всё равно два. И никакой ошибки не было, как так?»
> **Срочность:** средняя

**Воспроизводим поломку.** В `overlays/dev/kustomization.yaml` поменяйте в патче реплик `count`/значение на 3 и **одновременно** испортите селектор: `target.name: vektor_web` вместо `vektor-web`. Затем:

```bash
kubectl apply -k overlays/dev
kubectl get deploy -n vektor-dev
```

Ожидаемо: команда завершится успешно, реплик останется 2, ошибки нет. Kustomize молча игнорирует патч, чей `target` ни с чем не совпал.

**Диагностика по порядку:**

1. Смотрим, что реально собралось: `kubectl kustomize overlays/dev | grep -A1 replicas` — если там по-прежнему 2, проблема точно на стороне рендера, а не кластера.
2. Проверяем сам патч-файл: `cat overlays/dev/patch-replicas.yaml` — значение `3` на месте.
3. Проверяем `target` в `kustomization.yaml` и сравниваем с именем Deployment в `base/deployment.yaml` — находим `vektor_web`.
4. Убеждаемся, что нет второго патча, который перетирает результат: `grep -n "replicas" -r overlays/dev`.
5. Проверяем, не забыт ли `behavior: merge` у генераторов (частая вторая причина «ничего не поменялось»).

**Решение:** вернуть `name: vektor-web`, выполнить `kubectl diff -k overlays/dev` (должны увидеть `replicas: 2 → 3`), затем `kubectl apply -k overlays/dev` и `kubectl rollout status deployment/vektor-web -n vektor-dev`.

**Ответ пользователю:** «Имя в селекторе патча было с опечаткой, Kustomize такие патчи молча пропускает. Исправил, diff показал изменение реплик, применил, сейчас 3/3 Ready. На будущее: перед `apply` смотрим `kubectl diff -k`, он бы сразу показал пустой диф.»

**Закрытие заявки:** тип «ошибка конфигурации», решение описано, в `stand-log.md` записано «dev: replicas=3».

### Что изменилось на стенде

- Новые namespace: `vektor-dev`, `vektor-prod`.
- В них: Deployment `vektor-web`, Service `vektor-web`, Ingress `vektor-web`, ConfigMap `vektor-web-html-<hash>` и `vektor-web-config-<hash>`.
- Репозиторий `k8s-configs` на GIT01 (10.10.10.50) со структурой `base/`, `components/metrics-sidecar/`, `overlays/dev`, `overlays/prod`.
- На UBU01 (10.10.10.30) установлен standalone `kustomize`.
- Ingress-хосты: `web.corp.vektor.lab` (prod) и `dev.web.corp.vektor.lab` (dev) — при необходимости добавьте записи в DNS на DC01 или в `/etc/hosts` на UBU01.

### Откат

1. `kubectl delete -k overlays/dev` и `kubectl delete -k overlays/prod`, затем `kubectl delete namespace vektor-dev vektor-prod`.
2. Если нужно откатить и кластер: в VMware Workstation выберите снапшот `before-devops-043` на k8s-cp01, k8s-w01, k8s-w02 и восстановите **все три одновременно** — иначе etcd на cp01 будет помнить узлы, которых уже нет.
3. GIT01 и HARBOR01 откатываются тем же снапшотом. Каталог `~/k8s-configs` на UBU01 можно оставить — он вне кластера и пригодится дальше.

## 6. Ошибки новичков и безопасность

1. **Копировать папку окружения вместо overlay.** → Через месяц dev и prod расходятся на 40 строк, и никто не помнит, какая версия «правильная». → Один base, оверлеи содержат только отличия, все правки идут через Git.
2. **Патч с неверным `target`.** → Применяется без ошибок и ничего не меняет; вы уверены, что задеплоили. → Всегда `kubectl diff -k` перед `apply`, в CI — сравнение `kustomize build` с ожидаемым.
3. **`disableNameSuffixHash: true` «чтобы имена были красивые».** → ConfigMap обновился, а поды не перезапустились: приложение читает старую конфигурацию. → Оставьте хэши включёнными; если имя фиксировано, добавляйте аннотацию с контрольной суммой и `kubectl rollout restart`.
4. **Править объекты в кластере руками (`kubectl edit`).** → Следующий `apply -k` затрёт правки, а причина инцидента останется в чьей-то памяти. → Только через Git; ручные правки — лишь для аварийной диагностики.
5. **`namePrefix` в оверлее, который ссылается на уже существующие объекты.** → Получаются два комплекта ресурсов, старый продолжает жить. → Префиксы — для новых окружений, а не для «переезда».
6. **`includeSelectors: true` в `labels` на живом Deployment.** → `selector` неизменяем, `apply` падает с ошибкой. → Метки в селекторы добавляйте только при создании объекта.
7. **Разные версии kustomize локально и в CI.** → «У меня работает, в пайплайне нет». → Фиксируйте версию бинарника в образе раннера и печатайте `kustomize version` в логе.

**Безопасность.** В боевой среде нельзя: держать в репозитории `secretGenerator` с паролями в открытом виде (используйте Sealed Secrets/SOPS из урока 36); запускать `kustomize build` с `--load-restrictor LoadRestrictionsNone` в CI (позволяет подтянуть манифесты из неожиданного места); давать CI-раннеру cluster-admin — он должен уметь применять только свою сборку. И помните: `kubectl apply -k` не проверяет, что вы применяете именно то окружение — палец, набранный `overlays/prod` вместо `overlays/dev`, защищается только диффом и ревью.

## 7. Вопросы на собеседовании

**1. Чем Kustomize отличается от Helm и что вы выберете?**

Ответ: Helm — шаблонизатор плюс менеджер пакетов и релизов: `values.yaml`, Go-шаблоны, `helm history`/`rollback`, версионированные чарты в репозитории. Kustomize — рендерер без шаблонов: чистый YAML, base + overlays, патчи, никакого состояния в кластере. Helm удобен, когда вы распространяете готовый продукт наружу; Kustomize — когда у вас своё приложение и 2–5 окружений. Часто их комбинируют: чарт вендора рендерится через `helmCharts`, сверху накладываются патчи Kustomize. Для GitOps с Argo CD Kustomize удобнее, потому что итог детерминирован и хорошо дифится.

*Что хочет услышать интервьюер:* что вы понимаете отсутствие состояния и роли, а не просто «Kustomize моднее».
*Красный флаг:* «Kustomize умеет всё то же, что Helm» или «Helm устарел».

**2. Как вы организуете конфигурацию для dev/staging/prod?**

Ответ: `base/` — полный набор манифестов приложения, одинаковый для всех сред. `overlays/dev|stage|prod` ссылаются на base в `resources` и содержат только отличия: `namespace`, `namePrefix` (если нужно), `replicas`, `images` с тегом, `configMapGenerator` с `behavior: merge` для переменных среды, патчи на ресурсы и Ingress-хост. Общее, что встречается в двух и более средах, выношу в `components/`. Всё в одном репозитории, изменения только через PR, тег образа обновляется CI-командой `kustomize edit set image`.

*Что хочет услышать интервьюер:* структуру каталогов, отсутствие копипаста, обновление тега автоматикой.
*Красный флаг:* «отдельная ветка на каждое окружение» без обсуждения минусов или «копирую папку».

**3. Что такое стратегический merge patch и чем он отличается от JSON6902?**

Ответ: strategic merge patch — это YAML той же структуры, что и объект, с указанием только изменяемых полей. Для списков Kubernetes знает ключи слияния (у контейнеров — `name`), поэтому элементы сливаются по имени, а не заменяются целиком; поддерживаются директивы `$patch: delete` и `$patch: replace`. JSON6902 — массив операций `add`/`remove`/`replace`/`copy`/`move`/`test` с путями JSON Pointer. JSON6902 точнее и умеет удалять элементы списка по индексу, но менее читаем. По умолчанию беру strategic merge, JSON6902 — когда нужно удалить элемент из списка или заменить конкретное поле в массиве без переписывания всего элемента.

*Что хочет услышать интервьюер:* понимание ключей слияния и того, что списки без ключа заменяются целиком.
*Красный флаг:* «это одно и то же» или «JSON-патч работает только с JSON-файлами».

**4. Как заставить поды перезапуститься при изменении ConfigMap?**

Ответ: три способа. Первый и основной — `configMapGenerator`: имя ConfigMap получает хэш содержимого, ссылка в Deployment переписывается автоматически, шаблон пода меняется → rolling update запускается сам. Второй — аннотация вида `checksum/config: <sha256>` в `spec.template.metadata.annotations`, которую подставляет CI или Helm. Третий — ручной `kubectl rollout restart deployment/vektor-web`. Важный нюанс: если ConfigMap смонтирован как файл, kubelet обновит его в поде с задержкой (порядка минуты), но если он подключён через `env`/`envFrom`, приложение не увидит новых значений без перезапуска. И ещё: `behavior: merge` у генератора работает только если базовый ConfigMap есть в той же сборке.

*Что хочет услышать интервьюер:* хэш-суффикс как основной механизм и разницу между volume и env.
*Красный флаг:* «Kubernetes сам перезапускает поды при изменении ConfigMap».

**5. Как Kustomize встраивается в GitOps и какие подводные камни?**

Ответ: Argo CD и Flux поддерживают Kustomize нативно. Argo CD берёт `path` до каталога с `kustomization.yaml`, умеет `kustomize.images` и `kustomize.buildOptions`, рендерит в момент синхронизации и показывает diff против кластера. Flux использует CRD `Kustomization` с полями `path`, `patches`, `images` и `postBuild.substitute` для подстановки переменных. Подводные камни: версия kustomize внутри Argo CD/Flux должна совпадать с той, что у вас локально (иначе разный результат рендера); удалённые источники ресурсов (git/URL) в Kustomize считаются устаревшими, манифесты должны лежать рядом [ПРОВЕРИТЬ формулировку для вашей версии]; секреты нельзя держать в plain text — только Sealed Secrets/SOPS/External Secrets; при использовании `helmCharts` нужно включать `--enable-helm` в билд-опциях контроллера.

*Что хочет услышать интервьюер:* связка «рендер одинаковый везде» и безопасность секретов.
*Красный флаг:* «Argo CD сам разберётся с версиями» — это ровно источник «у меня локально другой манифест».

## 8. Источники (официальная документация)

- **kustomize.io** — официальный сайт проекта. Разделы «Getting Started», «Kustomization», «Guides» (там же про overlays, generators, patches).
- **kubernetes.io** — статья «Declarative Management of Kubernetes Objects Using Kustomize» в разделе Tasks → Managing Kubernetes Objects. Поиск: `site:kubernetes.io kustomize declarative management`.
- **github.com/kubernetes-sigs/kustomize** — репозиторий проекта, каталоги `docs/` и `api/` (справочник по полям kustomization), раздел Releases (какая версия вкомпилирована в kubectl — смотрите в release notes).
- **kubectl.docs.kubernetes.io** — старая документация по kubectl и Kustomize, раздел «Kustomize» [ПРОВЕРИТЬ актуальность, ресурс постепенно переезжает на kustomize.io].
- **argo-cd.readthedocs.io** — раздел «Kustomize» (User Guide → Kustomize). Поиск: `argo cd kustomize images buildOptions`.
- **fluxcd.io** — документация Flux, раздел «Kustomization» (fluxcd.io/flux/components/kustomize/). Поиск: `flux kustomization patches postBuild substitute`.
- **kubernetes.io** — страница «JSON patch / Strategic merge patch» в API concepts. Поиск: `kubernetes strategic merge patch JSON patch documentation`.
