# Операторы и CRD: принцип работы, kubebuilder/operator-sdk (введение)

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»:

> **Пользователь:** ivanov (бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Портал согласования платежей опять показывает прошлую версию, вы писали, что выкатили новую. И вообще он после обеда сам перезапустился — я ничего не нажимал».
> **Срочность:** средняя

Вы идёте в кластер и обнаруживаете: Deployment, который обслуживает этот портал, никто не создавал руками — он появился сам, потому что кто-то применил объект `kind: WebApp`. И когда вы удалили под «на всякий случай», он вернулся. Это работает **оператор** — программа, которая живёт в кластере, читает ваши объекты и приводит реальность к тому, что в них написано.

Операторы — это то, чем Kubernetes-инженер занимается в реальной работе постоянно: 90% «платформенных» штук в кластере (cert-manager, ingress-контроллеры, Prometheus Operator, операторы баз данных, Deckhouse с его модулями) — это CRD + контроллер. Вы будете их ставить, обновлять, отлаживать, а иногда и писать. После этой главы вы должны: понимать, как Kubernetes расширяется новыми типами объектов, уметь прочитать чужой CRD и «увидеть» его схему, объяснить коллеге разницу между Helm-чартом и оператором, и собрать (scaffold) собственный минимальный оператор на kubebuilder.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**CRD (CustomResourceDefinition)** — это объект Kubernetes, который *регистрирует в API-сервере новый тип ресурса*. После `kubectl apply` CRD в кластере появляется, например, `webapps.platform.vektor.lab`, и с этого момента `kubectl get webapps` работает так же, как `kubectl get deployments`. Сам CRD — это декларация типа: группа (`group`), версия (`versions`), имена (`names`), область (`scope`: Namespaced/Cluster), OpenAPI-схема полей, включённые подресурсы (`status`, `scale`), дополнительные колонки в `kubectl get`.

**CR (Custom Resource)** — конкретный экземпляр этого типа: «хочу WebApp с образом nginx:1.27 и 2 репликами». API-сервер валидирует его по схеме CRD и кладёт в etcd. Всё. Сам по себе CR ничего не делает: API-сервер — это «умный шкаф», он не умеет запускать поды по вашим правилам.

**Контроллер** — процесс, который смотрит на объекты (watch) и делает шаги, чтобы состояние в кластере совпало с желаемым. **Оператор = CRD + контроллер(ы) с зашитой предметной логикой** (обычно + ServiceAccount, RBAC, иногда валидирующие webhook'и). Слово «оператор» ввёл CoreOS в 2016 году именно в смысле «упаковка знаний админа в код».

Ключевой цикл — **reconcile loop** (цикл согласования). Он не «реагирует на событие и забывает», а работает по принципу *level-triggered*: получает ключ `namespace/name`, заново читает объект из кэша, сравнивает с реальностью, делает один шаг, возвращает `Requeue` или ждёт следующего события. Поэтому reconcile обязан быть **идемпотентным**: выполнить его 1 раз или 10 раз подряд — результат один и тот же.

```
kubectl apply -f webapp.yaml
        │ HTTPS :6443
        ▼
┌──────────────────── kube-apiserver ─────────────────────┐
│ authn → authz (RBAC) → admission webhooks → schema      │
│ validation (structural schema) → PRUNING чужих полей    │
│ → запись в etcd: /registry/platform.vektor.lab/webapps/ │
└──────────────┬──────────────────────────────────────────┘
               │ watch (HTTP/2 stream)
               ▼
   Pod оператора (ns webapp-operator-system, Deployment)
   informer cache ─► workqueue ─► Reconcile(namespacedName)
                                    │ читает WebApp из кэша
                                    │ читает/создаёт Deployment
                                    ▼
                              kube-apiserver → etcd
                                    │
                        Deployment → ReplicaSet → Pod
```

Что важно знать про механику:

- **Порты:** API-сервер — 6443; ваш оператор отдаёт метрики (часто 8443 через kube-rbac-proxy), webhook-сервер — 9443. Внутри кластера всё по HTTPS.
- **informer/кэш:** контроллер не долбит API-сервер на каждый объект, а держит локальный кэш (List+Watch) и ходит в API только на запись. Отсюда типичный симптом «оператор не видит изменений» — рассинхрон кэша или отсутствие прав на `watch`.
- **ownerReferences + `Owns()`:** оператор ставит на созданный Deployment ссылку «мой владелец — WebApp demo». Kubernetes тогда сам удалит Deployment при удалении CR, а `Owns()` в `SetupWithManager` заставит оператор пересоздать Deployment, если кто-то его снёс.
- **status subresource:** `spec` — это «чего хочу» (пишет человек, оператор только читает), `status` — «что получилось» (пишет только оператор через `Status().Update`). Разделение защищает от гонок.
- **finalizers:** маркер «не удаляй объект, пока я не приберусь» (снять внешний ресурс, отдать DNS-запись). Забыли снять — объект навсегда висит в `Terminating`.
- **Конверсия версий:** если в CRD несколько версий (`v1alpha1`, `v1beta1`), между ними работает conversion webhook — знание полезно, когда читаете чужой CRD.
- **Валидация:** в `apiextensions.k8s.io/v1` схема обязана быть *structural*; неизвестные поля сервер **молча вырезает** (pruning). Это источник тикета из раздела 5.
- **Зрелость операторов** (Operator Capability Levels от Operator Framework): Basic Install → Seamless Upgrades → Full Lifecycle (бэкапы, восстановление) → Deep Insights (метрики/алерты) → Auto Pilot (масштабирование, автопочинка). Helm-чарт закрывает первый уровень, оператор целится в четвёртый-пятый.

## 3. Аналогия из жизни

CRD — это **новый бланк в МФЦ**. Вы приносите заполненную заявку «прошу выдать справку», кладёте в лоток — и всё, дальше ничего не происходит, пока за дело не возьмётся человек. Оператор — это **сотрудник окна**, прикреплённый к этому бланку: он берёт заявку, сверяет «что просят» с «что реально есть на складе», идёт и делает (печатает справку), потом ставит на заявке штамп «выполнено, 2 экземпляра» (это `status`). Раз в час он обходит склад и проверяет: если кто-то унёс напечатанное — печатает заново, потому что в заявке написано, что оно должно быть. Helm-чарт в этой метафоре — курьер, который один раз принёс коробку и уехал: дальше за коробкой никто не следит.

Где аналогия ломается: сотрудник МФЦ может пообещать, подумать, отказаться «по-человечески». Оператор не думает — он умеет только сравнивать и повторять; если в API нет поля, оператор о нём никогда не узнает.

## 4. Практика и команды

**1. Увидеть, какие типы вообще есть в кластере**

```bash
kubectl api-resources | grep -i webapp
kubectl api-resources --api-group=platform.vektor.lab
kubectl api-versions | grep platform
```

`api-resources` показывает `NAME / SHORTNAMES / APIVERSION / NAMESPACED / KIND`. Если после установки оператора (например, cert-manager) вы не видите `certificates`, значит CRD не встал — и `kubectl apply` манифеста с `kind: Certificate` упадёт с `no matches for kind`.

**2. Прочитать схему CRD до того, как что-то писать**

```bash
kubectl get crd webapps.platform.vektor.lab -o yaml | less
kubectl explain webapp.spec --recursive
kubectl explain webapp.spec.replicas
```

`kubectl explain` берёт данные из OpenAPI схемы CRD — это самый быстрый способ узнать, какие поля разрешены и какие у них типы/дефолты. В `-o yaml` смотрим блоки `versions[].schema.openAPIV3Schema`, `subresources`, `additionalPrinterColumns`.

**3. Пример CRD, написанный руками (чтобы понять структуру)**

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: webapps.platform.vektor.lab
spec:
  group: platform.vektor.lab
  scope: Namespaced
  names:
    kind: WebApp
    plural: webapps
    singular: webapp
    shortNames: [wa]
  versions:
    - name: v1alpha1
      served: true
      storage: true
      subresources:
        status: {}          # разрешаем отдельно писать status
      additionalPrinterColumns:
        - name: Image
          type: string
          jsonPath: .spec.image
        - name: Ready
          type: integer
          jsonPath: .status.readyReplicas
      schema:
        openAPIV3Schema:
          type: object
          required: [spec]
          properties:
            spec:
              type: object
              required: [image]
              properties:
                image:    { type: string }
                replicas: { type: integer, minimum: 0, default: 1 }
            status:
              type: object
              properties:
                readyReplicas: { type: integer }
```

`subresources.status` обязателен, иначе любой `kubectl apply` на CR будет затирать статус. `default` и `minimum` — это и есть «валидация схемой», без единой строчки Go.

**4. Применить CRD и CR**

```bash
kubectl apply -f crd.yaml
kubectl create namespace demo
kubectl apply -n demo -f - <<'EOF'
apiVersion: platform.vektor.lab/v1alpha1
kind: WebApp
metadata:
  name: demo
spec:
  image: nginx:1.27
  replicas: 2
EOF
kubectl get wa -n demo -o wide
```

Ожидаемый вывод — с вашими колонками `Image / Ready`. Если оператора ещё нет, `Ready` будет пустым: CR просто лежит в etcd.

**5. Написать статус от имени «оператора» вручную**

```bash
kubectl patch webapp demo -n demo --subresource=status --type=merge \
  -p '{"status":{"readyReplicas":2}}'
kubectl get webapp demo -n demo -o jsonpath='{.status.readyReplicas}{"\n"}'
```

Флаг `--subresource` появился в kubectl 1.24+ [ПРОВЕРИТЬ для вашей версии]. Без него правка `status` через `kubectl apply` будет проигнорирована.

**6. Скаффолдинг оператора на kubebuilder (на UBU01)**

```bash
curl -L -o kubebuilder "https://go.kubebuilder.io/dl/latest/$(go env GOOS)/$(go env ARCH)"
chmod +x kubebuilder && sudo mv kubebuilder /usr/local/bin/
mkdir -p ~/work/webapp-operator && cd ~/work/webapp-operator
kubebuilder init --domain vektor.lab --repo vektor.lab/webapp-operator
kubebuilder create api --group platform --version v1alpha1 --kind WebApp \
  --resource --controller
```

`--domain` даёт суффикс группы (`platform.vektor.lab`), `--repo` — путь Go-модуля (в реальной работе это URL репозитория на GIT01). Появятся `api/v1alpha1/webapp_types.go`, `internal/controller/webapp_controller.go`, `cmd/main.go`, каталог `config/` [ПРОВЕРИТЬ: в kubebuilder v3 контроллеры лежат в `controllers/`, в v4 — в `internal/controller/`].

**7. Сгенерировать манифесты и поставить CRD**

```bash
make generate      # deepcopy-функции
make manifests     # config/crd/bases/... + config/rbac/role.yaml
make install       # kubectl apply CRD в кластер
```

`make install` ставит **только CRD**. `controller-gen` собирает CRD из Go-структур и `+kubebuilder:...`-маркеров — руками YAML из пункта 3 больше писать не нужно.

**8. Запустить контроллер локально против кластера**

```bash
export KUBECONFIG=$HOME/.kube/config
make run           # запускает manager на UBU01, подключаясь к API-серверу
```

Удобно для разработки: код на ноутбуке, кластер рядом. В «бою» так не делают — оператор живёт подом в кластере.

**9. Кусок reconciler'а (сердце оператора)**

```go
func (r *WebAppReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var app platformv1alpha1.WebApp
	if err := r.Get(ctx, req.NamespacedName, &app); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: app.Name + "-web", Namespace: app.Namespace,
	}}
	op, err := controllerutil.CreateOrUpdate(ctx, r.Client, dep, func() error {
		replicas := app.Spec.Replicas
		dep.Spec.Replicas = &replicas
		dep.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{"app": app.Name}}
		dep.Spec.Template.ObjectMeta.Labels = map[string]string{"app": app.Name}
		dep.Spec.Template.Spec.Containers = []corev1.Container{{
			Name: "web", Image: app.Spec.Image, Ports: []corev1.ContainerPort{{ContainerPort: 80}},
		}}
		return controllerutil.SetControllerReference(&app, dep, r.Scheme) // ownerRef
	})
	if err != nil {
		return ctrl.Result{}, err
	}

	app.Status.ReadyReplicas = dep.Status.ReadyReplicas
	app.Status.ObservedGeneration = app.Generation
	if err := r.Status().Update(ctx, &app); err != nil {
		return ctrl.Result{}, err
	}
	log.FromContext(ctx).Info("reconciled", "op", op, "ready", app.Status.ReadyReplicas)
	return ctrl.Result{}, nil
}
```

`CreateOrUpdate` возвращает `created|updated|unchanged`. `SetControllerReference` вешает ownerRef, а в `SetupWithManager` нужен `Owns(&appsv1.Deployment{})` — тогда оператор проснётся, если Deployment удалили руками.

**10. RBAC-маркеры (иначе в кластере будет 403)**

```go
// +kubebuilder:rbac:groups=apps,resources=deployments,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=platform.vektor.lab,resources=webapps,verbs=get;list;watch
// +kubebuilder:rbac:groups=platform.vektor.lab,resources=webapps/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=platform.vektor.lab,resources=webapps/finalizers,verbs=update
```

`make manifests` превратит их в `config/rbac/role.yaml` → ClusterRole + RoleBinding для ServiceAccount оператора.

**11. Собрать образ и выкатить в кластер (как в бою)**

```bash
make docker-build docker-push IMG=10.10.10.55/devops/webapp-operator:v0.1.0
make deploy IMG=10.10.10.55/devops/webapp-operator:v0.1.0
kubectl get pods -n webapp-operator-system
kubectl logs -n webapp-operator-system deploy/webapp-operator-controller-manager -c manager --tail=50
```

Если Harbor отдаёт самоподписанный сертификат, containerd на k8s-узлах должен ему доверять (настраивали в уроке 34/39), иначе под уйдёт в `ImagePullBackOff` — разбор в уроке 45.

**12. Альтернатива: operator-sdk**

```bash
operator-sdk init --domain=vektor.lab --repo=vektor.lab/webapp-operator
operator-sdk create api --group=platform --version=v1alpha1 --kind=WebApp --resource --controller
# без Go: плагины helm/ansible
operator-sdk init --plugins=helm --domain=vektor.lab
```

Под капотом operator-sdk v1.x использует те же kubebuilder и controller-runtime; разница — дополнительные плагины (Helm/Ansible) и часть tooling [ПРОВЕРИТЬ актуальный статус проекта на 2026 год — команда Operator SDK заявляла о сближении с kubebuilder].

**13. Удаление CRD — самое опасное действие**

```bash
kubectl delete crd webapps.platform.vektor.lab
```

Удалит **все** CR этого типа и каскадом — ресурсы, которыми они владели. В бою так «чистят» кластер ровно один раз в карьере.

## 5. Лабораторная работа

### Предусловия

- Кластер из урока 34 (`kubectl get nodes` → `k8s-cp01`, `k8s-w01`, `k8s-w02` в `Ready`).
- UBU01 (10.10.10.30) с доступом по SSH, `kubectl`, настроенным на кластер, и доступом в интернет для `go install`/скачивания kubebuilder.
- HARBOR01 (10.10.10.55) — только для шага 12 (опционально).
- Windows-часть стенда не нужна.

### Снапшот

На каждой затрагиваемой ВМ: VMware Workstation → правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-044`. Или из консоли хоста:

```bash
vmrun -T ws snapshot "/vmware/k8s-cp01/k8s-cp01.vmx"   before-devops-044
vmrun -T ws snapshot "/vmware/k8s-w01/k8s-w01.vmx"     before-devops-044
vmrun -T ws snapshot "/vmware/k8s-w02/k8s-w02.vmx"     before-devops-044
vmrun -T ws snapshot "/vmware/UBU01/UBU01.vmx"         before-devops-044
```

Пути подставьте свои. Снапшот кластера с etcd — единственный способ быстро вернуть состояние API.

### Шаги

1. **UBU01.** Проверить инструменты: `kubectl get nodes`, `go version` (нужен Go 1.22+ для kubebuilder v4 [ПРОВЕРИТЬ под вашу версию]). Если Go нет: `sudo apt update && sudo apt install -y golang-go`, затем `go version`. *Ожидаемо:* три узла `Ready`, Go установлен.
2. **UBU01.** Поставить kubebuilder (команда из п.4.6) и проверить `kubebuilder version`. *Ожидаемо:* `Version: v4.x`.
3. **UBU01.** `mkdir -p ~/work/webapp-operator && cd $_ && kubebuilder init --domain vektor.lab --repo vektor.lab/webapp-operator`. *Ожидаемо:* «Next: define a resource with kubebuilder create api» и появившийся `go.mod`.
4. **UBU01.** `kubebuilder create api --group platform --version v1alpha1 --kind WebApp --resource --controller`. *Ожидаемо:* созданы `api/v1alpha1/webapp_types.go` и файл контроллера.
5. **UBU01.** В `webapp_types.go` добавить `WebAppSpec{Image string; Replicas int32}` и `WebAppStatus{ReadyReplicas int32; ObservedGeneration int64}`, маркеры `+kubebuilder:subresource:status` и `+kubebuilder:printcolumn`. *Ожидаемо:* файл компилируется (`go build ./...`).
6. **UBU01.** `make generate && make manifests`. Открыть `config/crd/bases/platform.vektor.lab_webapps.yaml`: убедиться, что схема, `subresources.status` и `additionalPrinterColumns` появились автоматически. *Ожидаемо:* CRD-манифест с вашими полями.
7. **UBU01.** `make install && kubectl get crd webapps.platform.vektor.lab` и `kubectl explain webapp.spec --recursive`. *Ожидаемо:* CRD в статусе `Established`, `explain` показывает `image` и `replicas`.
8. **UBU01.** `kubectl apply -n demo -f config/samples/platform_v1alpha1_webapp.yaml` (namespace создать заранее). *Ожидаемо:* `kubectl get webapp -n demo` показывает объект, `READY` пустой — контроллера ещё нет.
9. **UBU01.** Реализовать `Reconcile` (код из п.4.9) + RBAC-маркеры (п.4.10) + `Owns(&appsv1.Deployment{})` в `SetupWithManager`. `make generate manifests install`.
10. **UBU01, сессия №1 (tmux).** `make run`. *Ожидаемо:* в логах `Starting Controller... Starting workers`, затем `reconciled op=created ready=0`.
11. **UBU01, сессия №2.** `kubectl get deploy,po -n demo`, `kubectl get webapp demo -n demo -o wide`. *Ожидаемо:* Deployment `demo-web` с 2 репликами, у CR заполнен `READY`.
12. **UBU01.** Проверить саморемонт: `kubectl delete deploy demo-web -n demo`, через несколько секунд `kubectl get deploy -n demo`. *Ожидаемо:* Deployment вернулся (сработал `Owns`), в логах новый reconcile.
13. **UBU01.** Изменить желаемое: `kubectl scale` не нужен — правим CR: `kubectl patch webapp demo -n demo --type=merge -p '{"spec":{"replicas":3}}'`. *Ожидаемо:* через ~10 секунд три пода, `readyReplicas: 3`.
14. *(Опционально)* Шаг 11 из п.4 (сборка образа и `make deploy`) — если Harbor готов; остановить `make run` (Ctrl+C) перед этим, иначе два контроллера будут драться.
15. Записать в `stand-log.md` изменения (см. ниже).

### Проверка

- [ ] `kubectl get crd webapps.platform.vektor.lab` → `Established`
- [ ] `kubectl get webapp -n demo -o wide` показывает колонки `Image` и `Ready`
- [ ] `kubectl get deploy demo-web -n demo -o yaml` содержит `ownerReferences` на WebApp
- [ ] `kubectl delete deploy demo-web -n demo` → Deployment пересоздан без ваших действий
- [ ] `kubectl patch ... replicas=3` → реально три пода, `status.readyReplicas=3`
- [ ] `kubectl explain webapp.spec` показывает ваши поля
- [ ] `kubectl get clusterrole | grep webapp-operator` — RBAC сгенерирован, а не написан руками

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Портал согласования платежей показывает старую версию, вы говорили, что выкатили 1.27. Я вчера менял в кластере конфиг — ничего не поменялось».
> **Срочность:** средняя

**Как воспроизвести поломку.** В CR сделать опечатку в имени поля — ровно то, что делает человек, спешащий в 18:40:

```bash
kubectl patch webapp demo -n demo --type=merge -p '{"spec":{"imag":"nginx:1.27"}}'
```

`imag` в схеме нет → API-сервер **молча вырезает** поле (pruning), `kubectl` отвечает «patched», ничего не меняется.

**Диагностика по порядку:**

1. `kubectl get webapp demo -n demo -o yaml | sed -n '/spec:/,/status:/p'` — видим `image: nginx:1.25`, поля `imag` **нет вообще**. Первый вывод: правка не сохранилась.
2. `kubectl explain webapp.spec --recursive` — сверяем: допустимо `image`, `replicas`. `imag` не существует.
3. `kubectl get deploy demo-web -n demo -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'` — реально работает старое, то есть оператор не виноват.
4. `kubectl logs -n webapp-operator-system deploy/webapp-operator-controller-manager -c manager --tail=30` (или окно `make run`) — нет ни одного reconcile с новым образом. Подтверждение: изменения до API не дошли.
5. `kubectl get events -n demo --sort-by=.lastTimestamp | tail` — пусто по WebApp.

**Решение:**

```bash
kubectl patch webapp demo -n demo --type=merge -p '{"spec":{"image":"nginx:1.27"}}'
kubectl rollout status deploy/demo-web -n demo
```

**Ответ пользователю:** «Разобрался: правка была с опечаткой в имени поля, API её отбросил, поэтому версия не менялась. Исправил, образ 1.27 раскатан, портал обновлён — проверьте, пожалуйста, Ctrl+F5». На будущее — добавить в CI валидацию манифестов по схеме CRD (например, `kubeconform` с каталогом CRD-схем [ПРОВЕРИТЬ актуальный источник схем]) и писать `kubectl apply --validate=strict` [ПРОВЕРИТЬ, ловит ли ваша версия kubectl неизвестные поля у CR].

**Закрытие заявки:** категория «Kubernetes / конфигурация», решение — «исправлено имя поля в CR», запись в `stand-log.md`.

### Что изменилось на стенде

- **UBU01:** установлен kubebuilder (`/usr/local/bin/kubebuilder`), установлен Go; создан проект `~/work/webapp-operator` (Go-модуль `vektor.lab/webapp-operator`).
- **Кластер k8s (cp01/w01/w02):** зарегистрирован CRD `webapps.platform.vektor.lab` (v1alpha1, Namespaced, subresource status, колонки `Image`/`Ready`); добавлен namespace `demo`; в нём CR `demo` и Deployment `demo-web` с ownerRef.
- **Кластер (если шаг 14 выполнен):** namespace `webapp-operator-system`, ServiceAccount/Role/RoleBinding оператора, Deployment `webapp-operator-controller-manager`, образ `10.10.10.55/devops/webapp-operator:v0.1.0` в реестре HARBOR01 (проект `devops`).
- Windows-часть стенда не затрагивалась.

### Откат

VMware Workstation → **Snapshot Manager** → выбрать `before-devops-044` → **Revert**. Или `vmrun -T ws revertToSnapshot "/vmware/k8s-cp01/k8s-cp01.vmx" before-devops-044` для каждого узла. Важно откатывать **все** узлы кластера: если откатить только UBU01, CRD останется в etcd. Точечно без снапшота: `kubectl delete ns demo webapp-operator-system`, `kubectl delete crd webapps.platform.vektor.lab` (помните: это удаляет все CR типа).

## 6. Ошибки новичков и безопасность

1. **Оператор там, где хватает ConfigMap/Helm.** → недели разработки, лишний компонент в кластере, который надо патчить и мониторить. → Сначала спросите: «моя задача — выдать манифесты один раз или постоянно удерживать состояние и внешние ресурсы?» Второе — оператор, первое — Helm/Kustomize (уроки 42–43).
2. **Reconcile, написанный «по событиям», а не по состоянию.** Логика «если событие Add — создать, если Update — обновить» → после рестарта оператора или отката кэша состояние разъезжается. → Каждый reconcile начинает с `r.Get` и приводит к desired state целиком, без памяти о прошлом.
3. **`Update` вместо `Status().Update`.** → статус то затирается, то не пишется вовсе («Err: object has been modified»). → Отдельный `status` subresource и `r.Status().Update`.
4. **Тестировали под `make run` с админским kubeconfig.** → в кластере под своим ServiceAccount оператор ловит `forbidden: cannot create resource "deployments"`. → Всегда проверяйте `kubectl auth can-i create deployments -n demo --as=system:serviceaccount:webapp-operator-system:webapp-operator-controller-manager` и держите RBAC-маркеры в коде.
5. **`make install` забыли после изменения типов.** → CRD в кластере старый, новые поля вырезаются pruning'ом, вы час ищете баг в Go. → Правило: изменил `_types.go` → `make manifests install`, и только потом `make run`.
6. **Удалили CRD «почистить». **→ Каскадом уехали все CR и созданные ими Deployment/Service/PVC. → Удаляйте CR в namespace, а не CRD; CRD сносится только осознанно.
7. **Финализатор добавили, снимать не научили.** → объект вечно в `Terminating`, `kubectl delete --force` не помогает (финализатор убирается только патчем `metadata.finalizers`). → В коде: `if !deletionTimestamp.IsZero() { cleanup(); removeFinalizer(); return }`.
8. **Храните пароли в spec CR и думаете, что «это же Kubernetes».** → CR лежит в etcd открытым текстом, и RBAC на `webapps` часто выдают всем подряд. → Секреты — в Secret/External Secrets (урок 36), в CR — только `secretRef`.

**Безопасность (в бою так нельзя):**

- **Оператор — это привилегированный код.** Он обычно имеет права писать в кластер; при `cluster-admin` у ServiceAccount компромисс образа оператора = компромисс кластера. Давайте минимальный RBAC по namespace и проверяйте образы (подпись cosign / доверенный реестр; в РФ — свой Harbor вместо Docker Hub, доступность которого не гарантирована).
- **Валидирующий webhook с `failurePolicy: Fail` — единая точка отказа.** Упал под webhook'а — перестали создаваться объекты (иногда и поды). Всегда держите минимум 2 реплики, `namespaceSelector`, короткий `timeoutSeconds`, и заранее продумайте процедуру снятия webhook'а.
- **CRD расширяет API-поверхность.** Новая группа = новый объект для аудита, новых прав, новых CR. Заведите правило: любой новый CRD проходит ревью (кто владелец, какие права, какие finalizers).
- **Учебные пароли стенда** (`Lab#2026-vektor`) и незашифрованные секреты в CR — только для лаборатории.

## 7. Вопросы на собеседовании

**1. Чем CRD отличается от CR и что происходит в кластере после `kubectl apply -f crd.yaml`?**

**Ответ.** CRD — объект, который регистрирует в API-сервере новый тип (`kind`) в новом API-group: сервер начинает отдавать его в discovery, хранить в etcd, валидировать по OpenAPI-схеме и обслуживать `kubectl get/describe`. CR — экземпляр этого типа, обычная запись в etcd с `apiVersion: <group>/<version>`. CRD — это «класс», CR — «объект». После установки CRD сам по себе ничего не запускается: нужен контроллер, который читает CR и создаёт реальные ресурсы.

**Что хочет услышать интервьюер:** что вы понимаете разделение «расширение API» и «логика»; что CRD без контроллера бесполезен.

**Красный флаг:** «CRD — это то же самое, что CR»; «оператор создаётся автоматически при установке CRD».

**2. Что такое оператор и чем он принципиально отличается от Helm-чарта?**

**Ответ.** Helm-чарт — шаблонизатор: рендерит манифесты и один раз применяет их; дальше он ничего не знает о состоянии. Оператор — постоянно работающий контроллер с reconcile-циклом и предметной логикой: он умеет обновлять версии с миграцией, делать бэкапы, реагировать на сбои, выставлять статус и условия. По Capability Levels оператор целится в Full Lifecycle/Deep Insights/Auto Pilot, чарт закрывает Basic Install. Технически оператор почти всегда ставится *тем же* Helm-чартом — они не конкуренты, а разные слои.

**Что хочет услышать интервьюер:** понимание «declarative apply vs continuous reconciliation», знание, что оператор = CRD + controller + RBAC.

**Красный флаг:** «оператор — это большой Helm-чарт»; «оператор нужен всегда, он же современнее».

**3. Почему reconcile должен быть идемпотентным и что такое level-triggered подход?**

**Ответ.** Контроллер получает не «событие изменения», а ключ объекта и заново вычисляет целевое состояние; события могут дублироваться, теряться, приходить пачками, а после рестарта оператора кэш перечитывается целиком. Поэтому каждое исполнение обязано приводить систему к desired state независимо от предыдущего: сравнили, создали/обновили, записали status, вернули `Result{}`. Ошибку возвращаем — controller-runtime сам сделает ретрай с экспоненциальной задержкой. Для «подождать внешнего условия» — `Requeue: true` или `RequeueAfter`.

**Что хочет услышать интервьюер:** понимание, что оператор — не скрипт «на событие», а вечный цикл сверки; что ретраи и повторные reconcile — норма.

**Красный флаг:** «считаю состояние в памяти между вызовами»; «использую `sleep` внутри Reconcile»; «на любое событие делаю `kubectl delete && create`».

**4. Зачем нужны subresource `status`, ownerReferences и finalizers?**

**Ответ.** `status` отделяет «что просят» (spec, пишет человек/CI) от «что получилось» (status, пишет только оператор) — иначе `kubectl apply` затирал бы статус, а оператор конфликтовал с пользователем. `ownerReferences` дают каскадное удаление и «мусорку»: удалили CR — уехали его Deployment/Service; а `Owns()` в `SetupWithManager` заставляет оператор восстановить дочерний объект, если его удалили руками. `finalizers` держат объект в `Terminating`, пока оператор не приберёт внешние ресурсы (DNS, облачный балансировщик, бэкап) — и обязан снять финализатор явно.

**Что хочет услышать интервьюер:** что вы писали или хотя бы читали такой код, а не только ставили чужие операторы.

**Красный флаг:** «финализатор можно убрать `--force`»; «status можно писать обычным Update».

**5. Kubebuilder или Operator SDK — что выберете и как отлаживать оператор в кластере?**

**Ответ.** Оба строятся на controller-runtime; kubebuilder — «чистый» Go-скаффолд от kubernetes-sigs, operator-sdk добавляет плагины Helm/Ansible (когда команда не пишет на Go) и часть tooling, плюс интеграцию с OLM. Для Go-операторов в 2026 логично kubebuilder (или operator-sdk с Go-плагином — почти одно и то же) [ПРОВЕРИТЬ актуальный статус проектов]. Отладка: локально `make run` с `KUBECONFIG` для быстрого цикла; в кластере — `kubectl logs -n <ns> deploy/<name>-controller-manager -c manager`, `kubectl describe <cr>`, `kubectl get events --field-selector involvedObject.kind=<Kind>`, `kubectl auth can-i --as=system:serviceaccount:...` для RBAC, метрики controller-runtime (reconcile_errors_total, workqueue_depth) в Prometheus/MON01.

**Что хочет услышать интервьюер:** практический опыт отладки (логи, события, RBAC, метрики), понимание, что 80% проблем оператора — права и кэш, а не Go.

**Красный флаг:** «отлаживаю только `fmt.Println`»; «не знаю, что у оператора есть ServiceAccount».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Extend Kubernetes»: статьи «Custom Resources», «Extend the Kubernetes API with CustomResourceDefinitions», «Operator pattern», «API Conventions». Поиск: `kubernetes custom resource definition structural schema pruning`.
- **book.kubebuilder.io** — «Quick Start», «Project Layout», «CronJob Tutorial», «Multiversion Tutorial», раздел про markers и `make manifests`. Поиск: `kubebuilder book quick start cronjob tutorial`.
- **sdk.operatorframework.io** — документация Operator SDK: Go/Helm/Ansible plugins, migration guide. Поиск: `operator sdk go plugin tutorial`.
- **operatorframework.io** — «Operator Capability Levels» / maturity model. Поиск: `operator capability levels`.
- **pkg.go.dev (sigs.k8s.io/controller-runtime)** — пакеты `pkg/client`, `pkg/reconcile`, `pkg/controller/controllerutil` (`CreateOrUpdate`, `SetControllerReference`). Поиск: `controller-runtime controllerutil CreateOrUpdate`.
- **github.com/kubernetes-sigs/kubebuilder** — релизы, release notes (важно: в v4 изменился layout проекта) [ПРОВЕРИТЬ актуальную мажорную версию].
- **kubernetes.io → Concepts → Extend Kubernetes → Compute, Storage and Networking Extensions** — про conversion и admission webhooks. Поиск: `kubernetes admission webhook conversion webhook crd`.
- Российский контекст: **deckhouse.io** — документация Deckhouse Kubernetes Platform, раздел про модули (каждый модуль — это CR + оператор), пригодится к уроку 47. Поиск: `deckhouse модули custom resources`.
