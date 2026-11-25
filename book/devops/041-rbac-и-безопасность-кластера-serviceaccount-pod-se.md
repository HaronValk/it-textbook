# RBAC и безопасность кластера: ServiceAccount, Pod Security Standards, OPA/Kyverno, Falco

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4127.** Пользователь: Иван Иванов (ему выдали доступ к кластеру — он пишет внутренний сервис для бухгалтерии). ВМ: k8s-cp01. Текст: «Я случайно удалил deployment в namespace prod, хотел в dev. Хорошо, что это тестовый стенд. Сделайте так, чтобы я туда вообще не мог ничего удалять, а лучше — только смотреть». Срочность: высокая.

Это не выдуманная ситуация — это ровно то, что происходит в первые же недели после того, как кластер перестаёт быть «моим личным на kind». Пока кластер один и админ один, `cluster-admin` есть у всех, кто до него дорвался, и это нормально. Как только доступ получает второй человек, CI-раннер или приложение — начинается настоящая работа по безопасности.

После этой главы ты будешь уметь: выдавать минимально необходимые права через Role/RoleBinding и проверять их командой `auth can-i`; делать ServiceAccount с короткоживущим токеном и без автоподмонтирования; включать Pod Security Standards на уровне namespace и понимать формулировки отказов; ставить политики валидации через Kyverno; ловить подозрительное поведение внутри контейнеров через Falco. И главное — ты будешь понимать, что это четыре разных слоя, а не одна «галочка безопасность».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Три ступени у ворот API-сервера.** Каждый запрос к `kube-apiserver` (порт 6443, HTTPS) проходит по цепочке. Если запрос не прошёл ступень — дальше он не идёт, и объект в etcd не попадает.

```text
kubectl / токен ServiceAccount / kubelet / контроллер
                    |
                    v
        [ kube-apiserver :6443 ]
                    |
   1) AUTHENTICATION — «кто ты?»
      клиентский x509-сертификат (CN=user, O=группа)
      | Bearer-токен ServiceAccount
      | OIDC (Яндекс ID, Keycloak, LDAP-коннектор)
      | сервисные: bootstrap-токены нод, webhook-токенаутентификация
                    |
   2) AUTHORIZATION — «что тебе можно?»
      RBAC (основной режим), Node, Webhook (это и есть OPA/Kyverno в режиме authz)
                    |
   3) ADMISSION — «а можно ли сделать это именно так?»
      MutatingAdmissionWebhook  → меняет объект (проставить лейблы, вставить sidecar)
      ValidatingAdmissionWebhook → запрещает (Kyverno, OPA Gatekeeper)
      PodSecurity Admission      → встроенный, читает лейблы namespace
                    |
   4) запись в etcd  →  kubelet на ноде (порт 10250)  →  контейнер
                    |
   5) RUNTIME-ЗАЩИТА — «а что он делает уже внутри?»
      Falco: eBPF / kernel module, syscall-поток, правила, алерты
```

**RBAC (Role-Based Access Control).** Четыре объекта, и путаница между ними — источник 90% ошибок:
- `Role` — набор правил **в одном namespace**. `ClusterRole` — набор правил на весь кластер (или на «кластерные» ресурсы: nodes, persistentvolumes, namespaces).
- `RoleBinding` — привязка «кто → к какой Role» в namespace. `ClusterRoleBinding` — привязка на весь кластер.

Хитрость: `ClusterRoleBinding` может ссылаться на `ClusterRole`, но **не** на `Role`. А `RoleBinding` может ссылаться и на `Role`, и на `ClusterRole` — во втором случае права ClusterRole действуют только внутри namespace биндинга. Это стандартный способ дать «просмотр» в один namespace.

Правило состоит из `apiGroups`, `resources`, `verbs` и опционально `resourceNames`. `apiGroups: [""]` — это core-группа (`pods`, `services`, `configmaps`). `apps` — деплойменты. Список групп и имён ресурсов смотрится командой `kubectl api-resources`.

**ServiceAccount (SA)** — это «учётка для процесса», не для человека. До 1.24 у каждого пода был вечный Secret с токеном; сейчас токен — это projected volume с ограниченным TTL (по умолчанию ~1 час, ротация), и его подписывает API-сервер. Токен подмонтируется в под автоматически, если не выставить `automountServiceAccountToken: false`. Права SA — обычные RBAC-права через subject `kind: ServiceAccount`.

**Pod Security Standards (PSS)** — три встроенных профиля: `privileged` (ничего не запрещено), `baseline` (запрещает явно опасное: `hostNetwork`, `privileged: true`, `hostPath`), `restricted` (ещё требует `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`). Включается **лейблами namespace**, реализует это PodSecurity Admission — встроенный admission-плагин, ничего ставить не нужно. Три режима: `enforce` (блокировать), `audit` (писать в audit-лог), `warn` (показывать предупреждение клиенту).

**OPA Gatekeeper и Kyverno** — политики как код, но разного вкуса. OPA (Open Policy Agent) — универсальный движок политик на языке Rego; Gatekeeper — это OPA, встроенный в Kubernetes через CRD `ConstraintTemplate` + `Constraint`. Kyverno — движок, где политика пишется **на YAML**, без нового языка; умеет validate, mutate и generate. Для DevOps-команды Kyverno обычно быстрее внедряется, OPA/Gatekeeper — чаще встречается в крупных энтерпрайзах и в требованиях комплаенса.

**Falco** — это уже не про «пустить/не пустить», а про «что происходит». Он живёт DaemonSet'ом на каждой ноде, читает поток системных вызовов (через eBPF или загружаемый модуль ядра) и матчит их по правилам вида «в контейнере запущен shell», «запись в `/etc`», «исходящее соединение на нестандартный порт». Алерты уходят в stdout (и дальше в лог-систему — например, на MON01, если там уже стоит Zabbix/OpenSearch).

## 3. Аналогия из жизни

Представь бизнес-центр «Вектор» с проходной.

**Аутентификация** — турникет и охрана на входе: она смотрит паспорт или пропуск и решает, пускать ли тебя в здание вообще. Пропуск — это твой сертификат или токен.

**RBAC** — список, вывешенный у лифта: «Иванов: 3 и 4 этаж, только смотреть; Петрова: 5 этаж, можно входить и брать документы». Охрана на этаже сверяется со списком: ты можешь быть сотрудником компании, но на 7-й этаж тебе не надо.

**Admission-контроль (PSS/Kyverno)** — досмотр на входе в конкретное помещение: даже если тебе туда можно, тебя развернут с бензопилой в руках. И не потому, что ты плохой, а потому что в этом помещении бензопилы запрещены правилами.

**Falco** — камеры наблюдения внутри офиса. Они не мешают войти, но если человек с пропуском на 3-й этаж в 3 часа ночи лезет в серверную — служба безопасности об этом узнает.

Аналогия перестаёт работать в одном месте: охрана в бизнес-центре видит человека целиком и один раз, а Kubernetes проверяет каждый API-вызов заново и не знает, что было в предыдущем — состояние накапливается только внутри приложения, и именно поэтому runtime-контроль (Falco) нужен отдельно от RBAC.

## 4. Практика и команды

Все команды — с k8s-cp01 (10.10.10.61), `kubectl` уже настроен на кластер из урока 34. Namespace для примеров: `dev` и `prod` (создадим в лабораторной).

**1. Посмотреть, что вообще можно текущему пользователю.**

```bash
kubectl auth can-i --list
kubectl auth can-i --list -n prod
```
`--list` печатает таблицу «ресурсы / глаголы / источник прав». Типичный вывод для админа — `*.*` из `cluster-admin`. Смотрим на колонку `[namespace]` — если права пришли из namespaced Role, там будет имя namespace, если из cluster-wide — пусто.

**2. Проверить чужой доступ, не входя под этим пользователем.** Ключевое слово — `--as` (impersonation; работает, только если у тебя есть `impersonate`).

```bash
kubectl auth can-i get pods -n dev --as=system:serviceaccount:dev:bot-deployer
kubectl auth can-i delete deployments -n prod --as=system:serviceaccount:dev:bot-deployer
```
Ожидаемо: `yes` и `no`. Имя SA в RBAC всегда выглядит как `system:serviceaccount:<namespace>:<name>` — запомни этот формат, он понадобится в каждом биндинге.

**3. Создать ServiceAccount и получить для него токен.**

```bash
kubectl create namespace dev
kubectl create namespace prod
kubectl create serviceaccount bot-deployer -n dev
kubectl create token bot-deployer -n dev --duration=1h
```
`create token` печатает JWT. `--duration` ограничивает время жизни (максимум задаётся флагом `--service-account-max-token-expiration` API-сервера). Дальше такой токен можно подставить в `curl -H "Authorization: Bearer ..."` или в kubeconfig.

**4. Role с минимальными правами.**

```yaml
# dev-bot-role.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: deployer
  namespace: dev
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "create", "patch"]
```
Здесь нет `delete` и нет `secrets` — это и есть принцип минимальных привилегий. `pods/log` — подресурс, он указывается через слэш и требует отдельного правила.

**5. Привязать Role к ServiceAccount.**

```bash
kubectl apply -f dev-bot-role.yaml
kubectl create rolebinding bot-deployer-binding \
  --role=deployer \
  --serviceaccount=dev:bot-deployer \
  -n dev
kubectl describe rolebinding bot-deployer-binding -n dev
```
В `describe` смотрим блок `Subjects` (kind/name/namespace) и `Role` с `Kind: Role`. Если namespace у subject не тот — права просто не сработают, без ошибки.

**6. Убедиться, что прав ровно столько, сколько нужно.**

```bash
kubectl auth can-i --list -n dev --as=system:serviceaccount:dev:bot-deployer
```
В выводе должны быть только `pods`, `pods/log`, `deployments` и нужные глаголы. Если видишь `secrets` — значит биндинг указывает не на ту Role.

**7. Включить Pod Security Standards на namespace.**

```bash
kubectl label ns dev pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/warn=restricted \
  pod-security.kubernetes.io/audit=restricted --overwrite
kubectl label ns prod pod-security.kubernetes.io/enforce=restricted --overwrite
kubectl get ns dev --show-labels
```
Паттерн «в dev мягко, в prod жёстко» — рабочий компромисс при внедрении. `warn=restricted` покажет разработчику предупреждение, но не заблокирует под — это способ подготовить команду к переключению `enforce`.

**8. Проверить, что PSS реально блокирует.**

```bash
kubectl run bad --image=nginx -n prod
```
Ожидаемый отказ примерно такой (формулировка зависит от версии кластера):

```text
Error from server (Forbidden): pods "bad" is forbidden: violates PodSecurity
"restricted:latest": allowPrivilegeEscalation != false (container "bad" must set
securityContext.allowPrivilegeEscalation=false), unrestricted capabilities ...
```
Читай текст ошибки по пунктам — он сам говорит, что поправить в манифесте.

**9. Правильный под для `restricted`.**

```yaml
# prod-ok-pod.yaml
apiVersion: v1
kind: Pod
metadata:
  name: ok-pod
  namespace: prod
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: app
    image: busybox:1.36
    command: ["sleep", "3600"]
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: ["ALL"]
```
`automountServiceAccountToken: false` — на этом поде токен API вообще не нужен, значит, и монтировать его незачем (украдут файл — нечего красть).

**10. Kyverno: поставить и написать политику.**

```bash
helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update
helm install kyverno kyverno/kyverno -n kyverno --create-namespace
```
Детально Helm разбираем в уроке 42 — здесь достаточно, что чарт ставит контроллер и CRD. Версии чарта и совместимость с версией кластера `[ПРОВЕРИТЬ]` в документации Kyverno.

```yaml
# policy-require-team-label.yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-team-label
spec:
  validationFailureAction: Audit   # для Kyverno >=1.12可以用 rules[].validate.failureAction [ПРОВЕРИТЬ]
  background: true
  rules:
    - name: check-team-label
      match:
        any:
          - resources:
              kinds: ["Pod"]
      validate:
        message: "Обязателен лейбл team=<имя команды>"
        pattern:
          metadata:
            labels:
              team: "?*"
```

```bash
kubectl apply -f policy-require-team-label.yaml
kubectl get clusterpolicy
kubectl get policyreport -n dev          # отчёты о нарушениях [ПРОВЕРИТЬ имя CRD в вашей версии]
```
`Audit` — сначала смотрим, сколько нарушений в кластере, и только потом переключаем на `Enforce`. Ставить `Enforce` на живом кластере сразу — верный способ остановить деплой в пятницу.

**11. Falco: поставить и добавить своё правило.**

```bash
helm repo add falcosecurity https://falcosecurity.github.io/charts
helm repo update
helm install falco falcosecurity/falco -n falco --create-namespace \
  --set driver.kind=modern_ebpf
```
`driver.kind=modern_ebpf` — если сборка модуля ядра не заводится на вложенной виртуализации (частая история в VMware Workstation). Требуется ядро 5.8+; на Ubuntu 24.04 это выполняется `[ПРОВЕРИТЬ под конкретную версию Falco]`.

```yaml
# falco-values.yaml
customRules:
  vektor-rules.yaml: |-
    - rule: Vektor shell in container
      desc: В контейнере запущен интерактивный shell
      condition: >
        spawned_process and container and proc.name in (bash, sh, ash, dash)
      output: >
        Shell в контейнере (user=%user.name ns=%k8s.ns.name pod=%k8s.pod.name cmd=%proc.cmdline)
      priority: WARNING
      tags: [container, shell]
```
Ключ `customRules` в чарте Falco `[ПРОВЕРИТЬ]` — он менялся между мажорными версиями, сверься с values.yaml чарта.

**12. Посмотреть алерты Falco.**

```bash
kubectl get pods -n falco
kubectl logs -n falco -l app.kubernetes.io/name=falco --tail=30
kubectl exec -it ok-pod -n prod -- /bin/sh    # спровоцирует алерт, если shell есть в образе
```
В логах ищем строку с `Vektor shell in container` и полями `ns=` / `pod=`. Это и есть runtime-детект: RBAC такое не поймает — право на `exec` у тебя есть.

**13. Мелочь, которая экономит часы: как узнать структуру полей.**

```bash
kubectl explain pod.spec.securityContext
kubectl explain deployment.spec.template.spec.containers.securityContext
kubectl api-resources | grep -i rbac
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34 поднят: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63). Все `Ready`:
  ```bash
  kubectl get nodes
  kubectl get pods -A
  ```
- На `k8s-cp01` есть `kubectl` и `helm`. Если Helm ещё не ставился — он будет подробно в уроке 42; здесь достаточно `curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash` `[ПРОВЕРИТЬ актуальность скрипта]`.
- Свободно ~8 ГБ RAM на кластере сверх текущего: Kyverno и Falco — это ещё несколько подов.
- Windows-часть стенда не нужна — весь урок только на Linux.

### Снапшот

В VMware Workstation — снапшот **всех трёх** узлов кластера с именем `before-devops-041`:

VM → Snapshot → Take Snapshot → имя `before-devops-041` (для `k8s-cp01`, `k8s-w01`, `k8s-w02`).

Если снимаешь при работающих ВМ — включи «Snapshot the virtual machine's memory» или, что чище, сначала `sudo shutdown -h now` на всех трёх, потом снапшот, потом включить. Etcd на холодном снапшоте переживает откат спокойнее.

### Шаги

1. **k8s-cp01.** Создай namespace и сервисный аккаунт.
   ```bash
   kubectl create namespace dev
   kubectl create namespace prod
   kubectl create serviceaccount bot-deployer -n dev
   ```
   Ожидаемо: `namespace/dev created`, `serviceaccount/bot-deployer created`.

2. **k8s-cp01.** Примени Role и RoleBinding из раздела 4 (пункты 4–5). Проверь:
   ```bash
   kubectl auth can-i --list -n dev --as=system:serviceaccount:dev:bot-deployer
   ```
   Ожидаемо: только `pods`, `pods/log`, `deployments` с ограниченными глаголами. Скопируй вывод в `stand-log.md`.

3. **k8s-cp01.** Проверь негативный сценарий — это и есть суть RBAC:
   ```bash
   kubectl auth can-i delete deployments -n dev  --as=system:serviceaccount:dev:bot-deployer
   kubectl auth can-i get pods   -n prod        --as=system:serviceaccount:dev:bot-deployer
   ```
   Ожидаемо: `no` и `no`. Заявка #4127 закрыта по сути: удалять бот не может.

4. **k8s-cp01.** Включи PSS: `dev` — `baseline` + предупреждения `restricted`, `prod` — `restricted` (команды из раздела 4, пункт 7). Проверь:
   ```bash
   kubectl get ns dev prod --show-labels
   ```

5. **k8s-cp01.** Попробуй нарушить политику в `prod`:
   ```bash
   kubectl run bad --image=nginx -n prod
   ```
   Ожидаемо: `Error from server (Forbidden): ... violates PodSecurity "restricted:latest"`. Сфотографируй/скопируй текст — это типовое сообщение, которое придёт в тикете.

6. **k8s-cp01.** Задеплой корректный под из раздела 4 (пункт 9):
   ```bash
   kubectl apply -f prod-ok-pod.yaml
   kubectl get pod ok-pod -n prod
   ```
   Ожидаемо: `Running`.

7. **k8s-cp01.** Проверь, что предупреждение (warn) в `dev` не блокирует, но говорит:
   ```bash
   kubectl run warn-me --image=nginx -n dev
   ```
   Ожидаемо: под создаётся, но в stderr приходит `Warning: would violate PodSecurity "restricted:latest": ...`.

8. **k8s-cp01.** Поставь Kyverno и примени политику `require-team-label` (раздел 4, пункт 10). Сначала в `Audit`:
   ```bash
   kubectl get pods -n kyverno
   kubectl apply -f policy-require-team-label.yaml
   kubectl get clusterpolicy
   ```
   Ожидаемо: поды `kyverno-*` в `Running` (их 3–4), политика `Ready=True`.

9. **k8s-cp01.** Проверь реакцию на под без лейбла:
   ```bash
   kubectl run no-label --image=busybox:1.36 -n dev -- sleep 3600
   kubectl get policyreport -n dev
   ```
   В `Audit` под создастся, в PolicyReport появится запись о нарушении. Переключи политику на `Enforce`, удали под и повтори — теперь создание должно упасть на admission-е с текстом из `validate.message`.

10. **k8s-cp01.** Поставь Falco (раздел 4, пункт 11) и добавь правило `Vektor shell in container`:
    ```bash
    helm upgrade --install falco falcosecurity/falco -n falco -f falco-values.yaml --set driver.kind=modern_ebpf
    kubectl get pods -n falco
    ```
    Ожидаемо: по одному поду на каждую ноду (DaemonSet, 3 штуки) в `Running`.

11. **k8s-cp01.** Спровоцируй алерт и найди его в логах:
    ```bash
    kubectl exec -it ok-pod -n prod -- /bin/sh -c "exit 0"
    kubectl logs -n falco -l app.kubernetes.io/name=falco --tail=50 | grep -i "Vektor shell"
    ```
    Ожидаемо: строка с `ns=prod pod=ok-pod`. (Если в образе нет `sh` — используй под с busybox.)

12. **k8s-cp01.** Запиши всё в `stand-log.md`: какие namespace, SA, Role, политики и чарты появились, какие лейблы висят на namespace, что упало и почему.

### Проверка

- [ ] `kubectl get ns dev prod` — оба `Active`.
- [ ] `kubectl auth can-i --list -n dev --as=system:serviceaccount:dev:bot-deployer` показывает только разрешённое, без `delete` и без `secrets`.
- [ ] `kubectl auth can-i delete deployments -n prod --as=system:serviceaccount:dev:bot-deployer` → `no`.
- [ ] На `dev` лейбл `pod-security.kubernetes.io/enforce=baseline`, на `prod` — `=restricted`.
- [ ] `kubectl run bad --image=nginx -n prod` падает с `violates PodSecurity "restricted"`.
- [ ] `ok-pod` в `prod` в статусе `Running`.
- [ ] `kubectl get clusterpolicy` показывает `require-team-label` без ошибок.
- [ ] `kubectl get pods -n falco` — 3 пода по числу нод, все `Running`.
- [ ] В логах Falco есть правило с `Vektor shell in container`.
- [ ] В `stand-log.md` зафиксированы SA `bot-deployer`, Role `deployer`, лейблы namespace, Kyverno, Falco.

### Сломай и почини

**Пользователь:** Иван Иванов (`ivanov`). **ВМ:** k8s-cp01 (через его ноутбук, kubeconfig с токеном SA). **Что написал:** «Раньше из-под своего бота я мог смотреть поды в dev: `kubectl get pods -n dev`, а сейчас `Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:dev:bot-deployer" cannot list resource "pods" in API group "" in the namespace "dev"`. Ничего не трогал!». **Срочность:** высокая (сломан CI).

**Воспроизводим поломку намеренно:**

```bash
kubectl delete rolebinding bot-deployer-binding -n dev
kubectl auth can-i list pods -n dev --as=system:serviceaccount:dev:bot-deployer
# → no
```

**Диагностика по шагам:**

1. Читаем текст ошибки: там уже написано `cannot list resource "pods" ... in the namespace "dev"`. Это RBAC, а не аутентификация — токен валиден, прав нет.
2. Проверяем, что SA существует и это он, а не одноимённый пользователь:
   ```bash
   kubectl get sa -n dev
   kubectl auth can-i --list -n dev --as=system:serviceaccount:dev:bot-deployer
   ```
3. Смотрим, есть ли привязки вообще и на что они указывают:
   ```bash
   kubectl get role,rolebinding -n dev
   kubectl describe rolebinding -n dev
   ```
   Если список пуст — биндинг удалён. Если не пуст — сверяем subjects: namespace SA и имя.
4. Проверяем, что Role на месте и в ней нужный глагол:
   ```bash
   kubectl describe role deployer -n dev
   ```
5. Поднимаемся уровнем выше и исключаем `ClusterRoleBinding`-и, которые могли «перебить картину»:
   ```bash
   kubectl get clusterrolebinding | grep -i bot
   ```
6. Смотрим audit-лог API-сервера, если включён (`--audit-policy-file`), — там будет видно, кто удалил биндинг и когда. На учебном стенде обычно проще посмотреть историю в Git, если манифесты лежат в GIT01.

**Решение** — восстановить биндинг из Git (а не руками в кластере):

```bash
kubectl apply -f dev-bot-rolebinding.yaml
kubectl auth can-i list pods -n dev --as=system:serviceaccount:dev:bot-deployer
# → yes
```

Если манифеста в Git нет — создать заново командой из пункта 5 раздела 4.

**Ответ пользователю:** «Иван, это была RBAC-привязка: доступ отвязался, токен и аккаунт в порядке. Восстановил из Git, `kubectl get pods -n dev` работает. Чтобы не повторилось, заведу алерт на изменение rolebinding'ов в dev — если ещё раз пропадут права, узнаем раньше тебя».

**Закрытие заявки:** тип — «доступ», причина — «удалён RoleBinding», связка с коммитом в Git, в `stand-log.md` — запись «восстановлен RoleBinding bot-deployer-binding, добавлено правило в git-репозиторий манифестов».

### Что изменилось на стенде

- Namespace `dev` (лейблы `enforce=baseline`, `warn=restricted`, `audit=restricted`) и `prod` (`enforce=restricted`).
- ServiceAccount `dev/bot-deployer`.
- Role `dev/deployer` (pods, pods/log, deployments) и RoleBinding `dev/bot-deployer-binding`.
- Под `prod/ok-pod` (эталон корректного `restricted`-пода).
- Helm-релиз `kyverno` в namespace `kyverno` + ClusterPolicy `require-team-label`.
- Helm-релиз `falco` в namespace `falco` + custom-правило `Vektor shell in container`.
- `stand-log.md` дополнен записью об уроке 41.

### Откат

Если Kyverno в `Enforce` заблокировал что-то нужное — сначала смягчи, потом разбирайся:

```bash
kubectl patch clusterpolicy require-team-label --type merge \
  -p '{"spec":{"validationFailureAction":"Audit"}}'
```

Полный откат — выключить `k8s-cp01`, `k8s-w01`, `k8s-w02` и восстановить снапшот `before-devops-041` на всех трёх (VM → Snapshot → Snapshot Manager → `before-devops-041` → Revert). ВАЖНО: откатывать надо **все узлы**, иначе etcd на cp01 будет помнить поды, которых нет на воркерах, и кластер будет ругаться на «висящие» объекты.

## 6. Ошибки новичков и безопасность

1. **«Дам ClusterRoleBinding на cluster-admin, потом сузим».** → Одалживание cluster-admin становится постоянным, потому что «потом» не наступает; любая уязвимость в поде = полный контроль над кластером. → Начинай с `view`/`edit` в namespace и расширяй по факту отказа.
2. **RoleBinding с `kind: User` вместо `kind: ServiceAccount`.** → Права «есть», но не у того субъекта; диагностика мучительная. → Всегда проверяй `kubectl auth can-i --as=system:serviceaccount:<ns>:<name>`.
3. **`resourceNames` на `list`/`watch`.** → **Не работает**: `list` возвращает коллекцию, и API-сервер не может отфильтровать по имени. → `resourceNames` только с `get`/`update`/`delete`; для `list` — отдельный namespace или `label selector` в приложении.
4. **Автомонтирование токена «на всякий случай» во все поды.** → Токен с правами SA лежит в файле внутри контейнера; при RCE его забирают первым делом. → `automountServiceAccountToken: false` по умолчанию, включать точечно.
5. **PSS сразу в `enforce=restricted` на все namespace.** → Деплой встал, ночные алерты, откат «на всё». → Сначала `warn`+`audit` на 1–2 недели, отчёт по нарушениям, потом `enforce`.
6. **Политика Kyverno в `Enforce` без тестов.** → Блокирует прод-релиз из-за забытого лейбла. → Прогонять политику в `Audit` на dev, тестировать `kyverno test` (CLI) до применения.
7. **Считать, что Falco «защищает».** → Falco только детектирует и пишет в лог; без реакции (алерт в мониторинг, рантайм-реакция) он бесполезен. → Прокинуть алерты на MON01/Astra-мониторинг и договориться, кто читает.
8. **RBAC руками через `kubectl create`.** → Права не версионируются, при пересоздании кластера теряются, диффа нет. → Все Role/RoleBinding/Policy — в Git на GIT01, применение только через пайплайн.

**Безопасность (что нельзя в боевой среде):** не выдавать `cluster-admin` сервисным аккаунтам CI и не хранить их kubeconfig в открытом виде в репозитории; не использовать `hostPath` и `privileged: true` для «удобного» доступа к железу — это обход всех PSS сразу; не оставлять `system:masters` группу в kubeconfig разработчиков; не полагаться только на RBAC — RBAC не видит, что делает контейнер с уже выданными правами; не отключать audit-лог «чтобы не жрал диск» — без него инцидент не размотать. Отдельно про наш стенд: все токены и `kubectl create token` здесь учебные, в реальности токены SA живут минуты-часы и не пересылаются в чатах.

## 7. Вопросы на собеседовании

**1. «Чем Role отличается от ClusterRole, а RoleBinding от ClusterRoleBinding?»**
Role — правила в одном namespace, ClusterRole — на весь кластер либо на кластерные ресурсы (nodes, PV, namespaces). RoleBinding связывает субъекта с правами в одном namespace и может ссылаться как на Role, так и на ClusterRole (тогда права ClusterRole действуют только в этом namespace). ClusterRoleBinding даёт права на весь кластер и ссылаться на Role не может.
*Что хочет услышать интервьюер:* что ты понимаешь трюк «RoleBinding + ClusterRole» для выдачи общих прав в один namespace.
*Красный флаг:* «RoleBinding только для Role, ClusterRoleBinding только для ClusterRole» — формально второе верно, но неполно, и видно, что с RBAC не работали.

**2. «Пользователь жалуется на 403 Forbidden. Как диагностируешь?»**
Сначала читаю текст: `cannot <verb> resource "<res>" in API group "<group>" in the namespace "<ns>"` — там уже есть ответ. Дальше `kubectl auth can-i <verb> <resource> --as=<subject> -n <ns>` и `kubectl auth can-i --list --as=<subject> -n <ns>`. Проверяю биндинги `kubectl get rolebinding,clusterrolebinding -A`, сверяю subjects (имя, namespace, kind), затем `kubectl describe role`. Если всё на месте — смотрю, не перекрывает ли что-то admission (webhook Kyverno/OPA), и только потом audit-лог.
*Что хочет услышать интервьюер:* порядок «ошибка → can-i → биндинги → роль → admission/audit», без метаний.
*Красный флаг:* «перезапущу под» или «выдам cluster-admin и посмотрю, заработает ли».

**3. «Чем Pod Security Standards отличаются от OPA Gatekeeper и Kyverno?»**
PSS — встроенный admission-плагин с тремя фиксированными профилями (`privileged`/`baseline`/`restricted`), включается лейблами namespace, своих правил писать нельзя. Gatekeeper и Kyverno — расширяемые движки политик: Gatekeeper на Rego с CRD ConstraintTemplate/Constraint, Kyverno на YAML с validate/mutate/generate. Их берут, когда нужно «своё» правило: обязательные лейблы, запрещённые реестры образов, ограничение `:latest`.
*Что хочет услышать интервьюер:* что PSS и движки политик дополняют друг друга, а не заменяют.
*Красный флаг:* «Kyverno заменяет PSS» — обычно означает, что человек не включал ни то, ни другое.

**4. «Что делает ServiceAccount и как устроен его токен в современных версиях?»**
SA — идентичность для подов. До 1.24 токен лежал в Secret'е бессрочно; сейчас это projected volume с bound service account token: подписанный JWT с ограниченным TTL, аудиторией и привязкой к объекту пода, ротация. Отключается через `automountServiceAccountToken: false`. Права SA управляются RBAC через subject `system:serviceaccount:<ns>:<name>`.
*Что хочет услышать интервьюер:* что старые вечные токены — известный риск, и что ты умеешь их не монтировать.
*Красный флаг:* «токен лежит в Secret и живёт вечно» как описание нормы.

**5. «Зачем Falco, если есть RBAC и политики?»**
RBAC и admission работают до запуска: кто ты, что тебе можно, соответствует ли манифест правилам. Falco работает после — читает syscall'ы контейнеров через eBPF/модуль ядра и ловит аномалии в поведении: shell внутри контейнера, запись в `/etc`, чтение `/etc/shadow`, исходящее соединение наружу, запуск пакетного менеджера. Это runtime-детект: он не блокирует, а сигнализирует, и его ценность равна качеству реакции на алерты.
*Что хочет услышать интервьюер:* понимание «prevent vs detect» и что Falco без алертинга бесполезен.
*Красный флаг:* «Falco блокирует атаки» — он детектор, блокировка это уже про seccomp/AppArmor/runtime-реакцию.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Using RBAC Authorization», «Service Accounts», «Pod Security Standards» и «Pod Security Admission». Поиск: `kubernetes RBAC role rolebinding`, `kubernetes pod security standards restricted`.
- **kubernetes.io** — «Audit» / «Audit Logging» (для разбора инцидентов с правами). Поиск: `kubernetes audit policy file`.
- **kyverno.io** — разделы «Installation», «Writing policies / Validate», «Policy Reports». Поиск: `kyverno validate policy pattern`, `kyverno policy report`.
- **open-policy-agent.github.io/gatekeeper** — «Concepts: Constraint Templates», «How to…». Поиск: `gatekeeper constraint template rego example`.
- **falco.org** — «Installation (Helm)», «Rules», «Falco drivers / modern eBPF». Поиск: `falco helm chart driver modern ebpf`, `falco rule condition spawned_process container`.
- **helm.sh** — «Using Helm» / «Chart values» (пригодится для 42-го урока и для установки Kyverno/Falco). Поиск: `helm install values file`.
- **github.com/kubernetes-sigs/security-profiles-operator** `[ПРОВЕРИТЬ]` — если пойдёшь дальше в seccomp/AppArmor-профили на уровне кластера.
- **cve.org / nvd.nist.gov** — для отслеживания CVE на версии Kubernetes и admission-контроллеров `[ПРОВЕРИТЬ актуальные записи]`.
