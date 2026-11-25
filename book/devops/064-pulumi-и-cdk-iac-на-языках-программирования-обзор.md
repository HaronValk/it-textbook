# Pulumi и CDK: IaC на языках программирования (обзор)

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4127.** От: `ivanov` (Иван Иванов, бухгалтерия). ВМ: WIN11-CL01. Текст: «Нам к пятнице нужен тестовый стенд витрины для показа: страничка-заглушка nginx в Kubernetes. И таких же стендов нужно три — бухгалтерии, HR и складу, с разным числом реплик. Руками `kubectl apply` делать не хочу: в прошлый раз всё сломали и никто не вспомнил, что нажимали». Срочность: средняя.

Вы уже прошли Terraform/OpenTofu и знаете: HCL — язык декларативный, без циклов «по-настоящему», без функций с проверкой типов, без нормальных юнит-тестов. Для трёх почти одинаковых стендов HCL решается модулем и `for_each` — и это нормально. Но как только появляется логика «если окружение prod, то 3 реплики и антиаффинити, иначе 1 и без него», «имя стенда собирается из отдела и номера», «пройти по списку отделов и создать по стенду», — многие команды хотят писать это обычным кодом: Python, TypeScript, Go, C#. Именно это и дают Pulumi и семейство CDK: вы описываете инфраструктуру на языке программирования, а движок приводит её к нужному состоянию.

В работе это встречается постоянно: в вакансиях DevOps всё чаще просят «Pulumi или CDK», платформенные команды пишут внутренние конструкторы (abstractions) поверх облачных API, а CI/CD-пайплайны генерируют окружения программно. После этой главы вы будете понимать, чем Pulumi отличается от Terraform архитектурно (а не «просто другой язык»), что такое CDK и почему это не движок, а «синтезатор», умеете поднять реальный стенд Pulumi-кодом в свой Kubernetes-кластер и объяснить на собеседовании, когда код лучше HCL, а когда — хуже.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Начнём с главного различия, которое путают почти все.

**Pulumi — это собственный движок управления состоянием**, как Terraform, только фронтенд — язык программирования. Он сам хранит state, сам считает diff, сам вызывает provider'ы.

**CDK — это «синтезатор», а не исполнитель.** Код на языке программирования превращается в артефакт, который исполняет *другой* движок:

```text
                    ┌──────────────────────────────────────────┐
                    │  CDK-приложение (TS / Python / Go / Java)│
                    │  constructs-дерево, циклы, классы        │
                    └───────────────────┬──────────────────────┘
                                        │ synth (генерация)
        ┌───────────────────────────────┼───────────────────────────────┐
        ▼                               ▼                               ▼
  AWS CDK                        CDK for Terraform (CDKTF)              cdk8s
  → cdk.out/*.template.json      → cdktf.out/.../cdk.tf.json           → dist/*.yaml
  → исполняет CloudFormation     → исполняет terraform / tofu          → исполняет kubectl/Argo CD
```

Отсюда важное следствие: **CDKTF наследует все проблемы и все плюсы Terraform** — тот же state, тот же план, те же провайдеры. Вы просто получаете «Terraform, написанный на TypeScript». AWS CDK наследует семантику CloudFormation (и её ограничения), cdk8s вообще не управляет состоянием — он генерирует YAML, а применяет его кто-то другой.

**Как работает Pulumi (упрощённо):**

```text
  pulumi up
      │
      ▼
  ┌─────────────────┐   ваш __main__.py запускается как обычная программа
  │ Language Host   │   (Python-интерпретатор / Node.js / Go-бинарь)
  │ (SDK + runtime) │
  └────────┬────────┘
           │ gRPC (локальный канал): RegisterResource("kubernetes:apps/v1:Deployment", ...)
           ▼
  ┌─────────────────┐        читает/пишет        ┌──────────────────────────┐
  │ Pulumi Engine   │◄──────────────────────────►│ State (checkpoint)       │
  │ diff + plan     │                            │ file:// | s3:// | Pulumi │
  └────────┬────────┘                            │ Cloud (HTTPS 443)        │
           │ gRPC: Check / Diff / Create / Update / Delete
           ▼
  ┌─────────────────────────────┐   HTTPS → :6443 (kube-apiserver)
  │ Provider plugin             │──────────────► Kubernetes API
  │ pulumi-resource-kubernetes  │   HTTPS → :443 (cloud API)
  └─────────────────────────────┘
```

Ключевые моменты, которые надо усвоить:

1. **Ваша программа не «применяет» инфраструктуру напрямую.** Она при запуске регистрирует *намерения* (ресурсы и их свойства) в движке. Движок сравнивает их со state и решает, что делать. Поэтому код должен быть детерминированным — про это будет отдельный разговор в разделе 6.
2. **State — это JSON-checkpoint.** В нём лежат URN ресурсов, их ID в облаке, входные/выходные свойства (в том числе секреты — зашифрованные). Без state Pulumi не знает, что уже создано.
3. **Провайдеры — отдельные бинарники-плагины**, они скачиваются с `get.pulumi.com` при первом использовании и кешируются в `~/.pulumi/plugins`. Версия плагина фиксируется в проекте — иначе «вчера работало, сегодня нет».
4. **Секреты.** В Pulumi есть понятие secret-значений: они шифруются ключом стека. Для self-managed бэкенда ключ выводится из пароля (`PULUMI_CONFIG_PASSPHRASE`), для Pulumi Cloud — управляется сервисом.
5. **Сеть тут — L7.** Вся работа идёт по HTTPS к API облака/кластера (443, 6443) и по локальному gRPC между движком и плагинами (localhost, динамический порт). Никаких «своих» протоколов Pulumi в сеть не изобретает.

По лицензиям: **CLI Pulumi — open source (Apache 2.0)**, бесплатен, и его можно использовать с self-managed backend (файл, S3-совместимое хранилище, в том числе MinIO) без оплаты. Платный — облачный сервис Pulumi Cloud для команд (SSO, RBAC, аудит) и self-hosted редакция. Из РФ `get.pulumi.com` и npm-реестры могут быть недоступны без прокси — учтите это одной строкой и используйте зеркала/офлайн-дистрибутивы, это легально. AWS CDK и cdk8s — Apache 2.0, CDKTF — MPL 2.0, но темп разработки CDKTF стоит проверить перед ставкой на него `[ПРОВЕРИТЬ статус проекта CDKTF в 2026]`.

## 3. Аналогия из жизни

Terraform HCL — это бумажный рецепт борща: «возьми 2 моркови, нарежь, вари 20 минут». Повар по такому рецепту тупой и идеально исполнительный: делает ровно написанное, и если вы попросите 30 порций — придётся исписать 30 строк.

Pulumi и CDK — это повар, который умеет программировать. Вы говорите: «для каждого из четырёх отделов свари бульон, если отдел — склад, добавь перца, а морковь нарежь столько, сколько гостей». Повар сам развернёт цикл, вызовет функцию «сварить бульон» и переиспользует её. Но — и это критично — **умный повар всё равно обязан сверяться с журналом кладовой (state)**: что уже стоит на плите, а что нет. Иначе он начнёт варить второй борщ поверх первого.

Аналогия перестаёт работать в одном месте: повар может «попробовать и поправить по вкусу», а движок IaC не пробует — он строго сравнивает желаемое с журналом и делает ровно diff.

## 4. Практика и команды

Все команды — для UBU01 (Ubuntu 24.04). Проект назовём `vektor-demo`, стек `dev`.

**1. Установка Pulumi CLI**

```bash
curl -fsSL https://get.pulumi.com | sh
export PATH="$HOME/.pulumi/bin:$PATH"
echo 'export PATH="$HOME/.pulumi/bin:$PATH"' >> ~/.bashrc
pulumi version
```

`curl -fsSL` — тихий режим, следование редиректам, ошибка при HTTP-сбое. Скрипт кладёт бинарь в `~/.pulumi/bin`. Ожидаемый вывод: `v3.x.y`. Смотрим: версия ≥ 3.x и что бинарь в PATH.

**2. kubectl и доступ к кластеру**

```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
mkdir -p ~/.kube && chmod 700 ~/.kube
# на k8s-cp01: sudo cat /etc/kubernetes/admin.conf  → скопировать сюда
nano ~/.kube/config && chmod 600 ~/.kube/config
kubectl get nodes
```

Смотрим: три ноды в статусе `Ready`. `admin.conf` — это cluster-admin, в боевой среде так делать нельзя (см. раздел 6).

**3. Логин в бэкенд состояния**

```bash
export PULUMI_CONFIG_PASSPHRASE='Lab#2026-vektor'
pulumi login --local
pulumi whoami -v
```

`--local` — файловый бэкенд, state в `~/.pulumi`. Альтернатива для команды: `pulumi login 's3://vektor-pulumi-state?region=ru-central1&endpoint=...'` (S3-совместимое хранилище). `whoami -v` показывает backend URL — убедитесь, что там `file://`, а не `https://api.pulumi.com`.

**4. Создание проекта из шаблона**

```bash
mkdir -p ~/pulumi-vektor && cd ~/pulumi-vektor
pulumi new kubernetes-python --name vektor-demo --stack dev --yes
ls -la
```

`--name` — имя проекта, `--stack` — сразу создать стек, `--yes` — без интерактивных вопросов. Появятся `Pulumi.yaml` (описание проекта), `Pulumi.dev.yaml` (конфиг стека), `__main__.py` (программа), `requirements.txt` и venv. Шаблон содержит пример деплоймента nginx — **замените `__main__.py` целиком** своим кодом, содержимое шаблона от версии к версии меняется.

**5. Параметры стека (конфиг вместо хардкода)**

```bash
pulumi config set replicas 2
pulumi config set image nginx:1.27-alpine
pulumi config set --secret demoToken 'test-token-ne-iz-proda'
pulumi config
```

`--secret` шифрует значение ключом стека (тем самым `PULUMI_CONFIG_PASSPHRASE`). В `Pulumi.dev.yaml` секрет будет лежать как `secure: v1:...`. Смотрим: `replicas` и `image` открытым текстом, `demoToken` — зашифрован.

**6. Программа на Python (`__main__.py`)**

```python
import pulumi
import pulumi_kubernetes as k8s

config = pulumi.Config()
replicas = int(config.require("replicas"))
image = config.get("image") or "nginx:1.27-alpine"
app = "vektor-demo"

ns = k8s.core.v1.Namespace("vektor-demo-ns",
                           metadata=k8s.meta.v1.ObjectMetaArgs(name=app))

cm = k8s.core.v1.ConfigMap("vektor-demo-cm",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="vektor-demo-config", namespace=app),
    data={"INDEX_HTML": "<h1>Vektor demo stand</h1>"})

deploy = k8s.apps.v1.Deployment("vektor-demo-deploy",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="vektor-demo", namespace=app),
    spec=k8s.apps.v1.DeploymentSpecArgs(
        replicas=replicas,
        selector=k8s.meta.v1.LabelSelectorArgs(match_labels={"app": app}),
        template=k8s.core.v1.PodTemplateSpecArgs(
            metadata=k8s.meta.v1.ObjectMetaArgs(labels={"app": app}),
            spec=k8s.core.v1.PodSpecArgs(
                containers=[k8s.core.v1.ContainerArgs(
                    name="web", image=image,
                    ports=[k8s.core.v1.ContainerPortArgs(container_port=80)])]))))

svc = k8s.core.v1.Service("vektor-demo-svc",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="vektor-demo", namespace=app),
    spec=k8s.core.v1.ServiceSpecArgs(
        type="NodePort",
        selector={"app": app},
        ports=[k8s.core.v1.ServicePortArgs(port=80, target_port=80, node_port=30080)]))

pulumi.export("namespace", app)
pulumi.export("url", "http://10.10.10.61:30080")
```

Здесь `config.require("replicas")` возвращает строку, поэтому `int(...)`. `pulumi.export` создаёт **stack output** — то, что потом читают другие стеки и пайплайны.

**7. Preview — план изменений**

```bash
pulumi preview
```

```text
     Type                              Name                 Plan
 +   pulumi:pulumi:Stack               vektor-demo-dev      create
 +   ├─ kubernetes:core/v1:Namespace   vektor-demo-ns       create
 +   ├─ kubernetes:core/v1:ConfigMap   vektor-demo-cm       create
 +   ├─ kubernetes:apps/v1:Deployment  vektor-demo-deploy   create
 +   └─ kubernetes:core/v1:Service     vektor-demo-svc      create

Resources:
    + 5 to create
```

Символы: `+` create, `~` update, `+-` replace (удалить и создать), `-` delete. Первый запуск скачает плагин `pulumi-resource-kubernetes`. Смотреть надо на **количество replace**: `+-` на stateful-ресурсе — это потенциальная потеря данных.

**8. Применение и проверка**

```bash
pulumi up --yes
kubectl -n vektor-demo get deploy,pods,svc -o wide
kubectl -n vektor-demo get endpoints vektor-demo
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.61:30080
```

`--yes` — авто-подтверждение (в проде так нельзя, см. раздел 6). Ожидаем `2/2` реплики, endpoints с двумя IP, код `200`.

**9. Изменение параметра — идемпотентность в действии**

```bash
pulumi config set replicas 3
pulumi preview          # покажет ~ update только на Deployment
pulumi up --yes
pulumi stack output
```

**10. Дрейф и refresh**

```bash
kubectl -n vektor-demo scale deploy vektor-demo --replicas=0
pulumi preview          # покажет ~ update deployment (0 → 2)
pulumi up --yes         # вернёт как в коде
pulumi refresh          # отдельная команда: синхронизировать state с реальностью
```

**11. Инспекция state**

```bash
pulumi stack export > state-dev.json
jq '.deployment.resources | length' state-dev.json
jq -r '.deployment.resources[].type' state-dev.json | sort | uniq -c
```

Смотрим: 5 ресурсов, типы `kubernetes:*` и `pulumi:pulumi:Stack`. Этот файл — ваш бэкап и материал для разбора инцидентов.

**12. Импорт существующего ресурса**

```bash
kubectl create namespace vektor-legacy
pulumi import kubernetes:core/v1:Namespace vektor-legacy-ns vektor-legacy
```

Синтаксис: `<тип> <логическое имя> <ID в провайдере>`. Для Kubernetes ID = имя объекта в кластере. Pulumi допишет ресурс в state и в код (сгенерирует фрагмент).

**13. Тот же стенд в TypeScript (сравните объём)**

```ts
import * as pulumi from "@pulumi/pulumi";
import * as k8s from "@pulumi/kubernetes";

const cfg = new pulumi.Config();
const replicas = cfg.requireNumber("replicas");

const ns = new k8s.core.v1.Namespace("vektor-demo-ns", {
  metadata: { name: "vektor-demo" },
});

const deploy = new k8s.apps.v1.Deployment("vektor-demo-deploy", {
  metadata: { name: "vektor-demo", namespace: "vektor-demo" },
  spec: {
    replicas,
    selector: { matchLabels: { app: "vektor-demo" } },
    template: {
      metadata: { labels: { app: "vektor-demo" } },
      spec: { containers: [{ name: "web", image: "nginx:1.27-alpine" }] },
    },
  },
}, { dependsOn: [ns] });
```

**14. AWS CDK — команды «синтезатора»**

```bash
npm install -g aws-cdk
cdk init app --language=typescript
cdk synth            # → cdk.out/*.template.json (CloudFormation)
cdk diff
cdk bootstrap aws://<account_id>/<region>
cdk deploy
```

`cdk synth` **ничего не создаёт** — только генерирует CloudFormation-шаблон. Смотреть надо в `cdk.out/`: если там не то, что вы ждали, — деплой не спасёт. AWS в РФ — история с оплатой и доступностью аккаунта; для учёбы держите CDK на уровне синтеза шаблонов.

**15. CDKTF и cdk8s**

```bash
npm install -g cdktf-cli
cdktf init --template=typescript --local
cdktf synth          # → cdktf.out/stacks/<stack>/cdk.tf.json
cdktf deploy

npm install -g cdk8s-cli
cdk8s init python-app
cdk8s synth          # → dist/*.yaml
kubectl apply -f dist/
```

`cdktf synth` выдаёт обычный Terraform JSON — его можно кормить `terraform plan`/`tofu plan` и вашими политиками из урока 65. `cdk8s synth` выдаёт YAML — то, что потом применит Argo CD.

## 5. Лабораторная работа

### Предусловия

- Стенд DevOps готов: `UBU01` (10.10.10.30, вход по SSH), кластер `k8s-cp01` / `k8s-w01` / `k8s-w02` (10.10.10.61–.63) в рабочем состоянии, `GIT01` (10.10.10.50) — по желанию для шага с Git.
- На `UBU01` есть `curl`, `sudo`, `python3`, `jq`. `kubectl` и Pulumi **не нужны заранее** — ставим в лабе.
- Кластеру нужен доступ к реестру образов. Если интернета нет — заранее залейте `nginx` в `HARBOR01` (10.10.10.55, проект `library`) и укажите `pulumi config set image 10.10.10.55/library/nginx:1.27-alpine` `[ПРОВЕРИТЬ имя проекта в вашем Harbor]`.
- Windows-часть стенда (DC01/FS01) не требуется — трек DevOps на неё не опирается.

### Снапшот

В VMware Workstation для **каждой** ВМ: `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01` → правый клик по ВМ → **Snapshot → Take Snapshot** → имя `before-devops-064`.

Важно: снапшот работающего etcd-кластера не гарантирует консистентность. В бою для Kubernetes делают отдельный дамп etcd (`etcdctl snapshot save`) и снапшот диска на остановленной ВМ. Здесь, на учебном стенде, допустимо снимать на работающих ВМ — просто помните, что это компромисс.

### Шаги

1. **UBU01.** Установите Pulumi: `curl -fsSL https://get.pulumi.com | sh`, добавьте `~/.pulumi/bin` в PATH. Ожидаемо: `pulumi version` печатает `v3.x.y`.
2. **UBU01.** Установите kubectl по инструкции из раздела 4 (шаг 2). Ожидаемо: `kubectl version --client` работает.
3. **k8s-cp01 → UBU01.** Скопируйте `admin.conf` в `~/.kube/config`, `chmod 600`. Затем на UBU01: `kubectl get nodes`. Ожидаемо: 3 ноды `Ready`.
4. **UBU01.** `export PULUMI_CONFIG_PASSPHRASE='Lab#2026-vektor'`, `pulumi login --local`, `pulumi whoami -v`. Ожидаемо: backend `file://~/.pulumi`.
5. **UBU01.** Создайте проект: `mkdir -p ~/pulumi-vektor && cd ~/pulumi-vektor && pulumi new kubernetes-python --name vektor-demo --stack dev --yes`. Ожидаемо: появились `Pulumi.yaml`, `Pulumi.dev.yaml`, `__main__.py`, venv.
6. **UBU01.** Замените `__main__.py` кодом из раздела 4 (шаг 6). Задайте конфиг: `pulumi config set replicas 2`, `pulumi config set image nginx:1.27-alpine`, `pulumi config set --secret demoToken 'test-token'`. Ожидаемо: `pulumi config` показывает три ключа, `demoToken` помечен как secret.
7. **UBU01.** `pulumi preview`. Ожидаемо: план `+ 5 to create`, ни одного `+-`. Первый запуск скачивает плагин Kubernetes (1–2 минуты).
8. **UBU01.** `pulumi up --yes`. Ожидаемо: `5 created`, вывод двух stack output.
9. **UBU01.** Проверьте кластер: `kubectl -n vektor-demo get deploy,pods,svc -o wide`, `kubectl -n vektor-demo get endpoints vektor-demo`, `curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.61:30080`. Ожидаемо: `2/2` пода `Running`, endpoints с двумя адресами, HTTP `200`.
10. **UBU01.** `pulumi config set replicas 3`, `pulumi preview` (ровно одно `~ update`), `pulumi up --yes`. Ожидаемо: 3 пода, в плане не было ни одного `delete`.
11. **UBU01.** `pulumi stack export > state-dev.json`, `jq '.deployment.resources | length' state-dev.json`. Ожидаемо: число ресурсов в state (стек + 4 объекта).
12. **GIT01 (по желанию).** Создайте в GitLab проект `vektor-demo-iac`, закоммитьте `Pulumi.yaml`, `Pulumi.dev.yaml`, `__main__.py`, `requirements.txt`, `.gitignore` (venv, `state-dev.json`). Убедитесь, что секрет в `Pulumi.dev.yaml` лежит как `secure:`. Ожидаемо: репозиторий с историей, ни одного открытого пароля.
13. **UBU01.** Запишите изменения в `stand-log.md`.

### Проверка

- [ ] `pulumi version` и `kubectl version --client` работают на UBU01.
- [ ] `pulumi whoami -v` показывает локальный backend (`file://`).
- [ ] `pulumi config` показывает `replicas`, `image`, и `demoToken` как secret.
- [ ] `kubectl -n vektor-demo get deploy` → `3/3` (после шага 10).
- [ ] `curl http://10.10.10.61:30080` возвращает `200`.
- [ ] `pulumi stack output` печатает `namespace` и `url`.
- [ ] В `state-dev.json` есть ровно 4 объекта Kubernetes и стек.
- [ ] В Git (если делали) нет файлов с открытым `demoToken`.

### Сломай и почини

**Пользователь:** `ivanov` (бухгалтерия, ВМ WIN11-CL01)
**Что написал:** «Стенд vektor-demo, который вы вчера поднимали, не открывается — браузер пишет «сайт недоступен». Нам в 15:00 показывать директору.»
**Срочность:** высокая

**Как воспроизвести поломку** (имитируем «коллегу, который освобождал ресурсы»):

```bash
kubectl -n vektor-demo scale deployment vektor-demo --replicas=0
```

**Диагностика по порядку (не перескакивать):**

1. `kubectl get ns vektor-demo` — namespace на месте? (Если удалён — это не дрейф, а разрушение, идём в «Откат».)
2. `kubectl -n vektor-demo get deploy,rs,pods,svc,endpoints` — видим `deploy 0/0` и **пустые** endpoints у сервиса. Это ключ: Service жив, а трафика нет.
3. `kubectl -n vektor-demo describe svc vektor-demo` — в `Endpoints` пусто.
4. `curl -v http://10.10.10.61:30080` — connection refused/timed out. Симптом совпал с жалобой.
5. `pulumi preview` — Pulumi показывает `~ kubernetes:apps/v1:Deployment vektor-demo-deploy update` (0 → 3). Значит код и state целы, а в кластере дрейф. **Это и есть ответ на «кто виноват».**

**Решение:**

```bash
pulumi up --yes
kubectl -n vektor-demo get pods -w      # ждём 3 Running, Ctrl+C
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.61:30080
```

Почему не `kubectl scale --replicas=3`: руками вернуть — быстро, но дрейф останется «зашитым» в реальность, а следующий `pulumi up` всё равно приведёт к состоянию из кода. Чинить надо через инструмент, который владеет этим объектом.

**Ответ пользователю:** «Стенд vektor-demo восстановлен и проверен: http://10.10.10.61:30080 отвечает 200, в кластере 3 реплики в статусе Running. Причина — ручное изменение количества реплик в обход IaC. Чтобы не повторилось: правки в namespace `vektor-demo` вносим только через Pulumi, а в пайплайн добавляем `pulumi preview` как обязательную проверку дрейфа.» 

**Закрытие заявки:** тег `iac-drift`, запись в `stand-log.md`, короткая заметка «ручные правки в управляемом namespace запрещены» — пригодится на уроке 67 про платформенный подход.

### Что изменилось на стенде

- **UBU01**: установлены Pulumi CLI (`~/.pulumi/bin`) и `kubectl`; есть `~/.kube/config` (cluster-admin, учебный); локальный state-бэкенд `~/.pulumi`; проект `~/pulumi-vektor` (стек `dev`) и файл `state-dev.json`.
- **Кластер k8s**: namespace `vektor-demo`; ConfigMap `vektor-demo-config`; Deployment `vektor-demo` (3 реплики); Service `vektor-demo` типа NodePort на порту `30080` (нода `10.10.10.61`).
- **GIT01 (опционально)**: проект `vektor-demo-iac` с кодом стека.
- **Пароль-парольная фраза стенда для Pulumi**: `Lab#2026-vektor` (учебная, только для стенда).

### Откат

- **Мягкий откат (оставить инструменты):** `cd ~/pulumi-vektor && pulumi destroy --yes`, затем `pulumi stack rm dev --yes`. Кластер чист, UBU01 с инструментами остаётся.
- **Если state потерян/испорчен:** удалить namespace руками (`kubectl delete ns vektor-demo`), затем `pulumi stack rm dev --yes` и создать стек заново.
- **Полный откат:** выключить `UBU01`, `k8s-cp01`, `k8s-w01`, `k8s-w02` → **Snapshot Manager → `before-devops-064` → Restore**. Стенд вернётся к состоянию до лабы, включая отсутствие namespace.

## 6. Ошибки новичков и безопасность

1. **State на локальной ВМ без бэкапа.** Потеря `~/.pulumi` = Pulumi «забыл» всё, следующий `up` попытается создать дубли и упадёт на конфликте имён. → Бэкенд в S3-совместимом хранилище (MinIO на стенде), `pulumi stack export` по расписанию, бэкап — часть пайплайна.
2. **Секреты без `--secret`.** `pulumi config set dbPassword ...` пишет открытым текстом в `Pulumi.dev.yaml`, который уезжает в Git и в state. → Всегда `--secret`, а `PULUMI_CONFIG_PASSPHRASE` — в Vault/CI-переменных, не в репозитории (урок 66).
3. **Недетерминированный код.** `datetime.now()`, `uuid4()`, `random` в именах ресурсов, чтение файлов и `os.system("kubectl apply ...")` из программы. Программа «намерений» должна выдавать одно и то же при каждом запуске, иначе вечный diff и дрейф. → Детерминированные имена из конфига, никаких побочных эффектов внутри `__main__.py`.
4. **`pulumi up --yes` в прод и в CI на каждый коммит.** Нет preview — нет шанса заметить `+-` (replace) и снос базы. → `preview` в MR, `up` только после approve, политики (CrossGuard / OPA) на запрет опасных типов, как в уроке 65.
5. **Игнорирование дрейфа.** Ручные `kubectl edit` живут месяцами, `refresh` никто не запускает, реальность и код расходятся. → Регулярный `pulumi preview/refresh`, запрет ручных правок, reconciliation-джоб в GitLab CI.
6. **`pulumi destroy` на общем стеке.** Один стек на весь кластер → destroy сносит namespace вместе с чужими данными. → Отдельный стек на окружение, `pulumi.ResourceOptions(protect=True)` на критичных ресурсах, `retain_on_delete` там, где данные важнее чистоты.
7. **Незафиксированные версии провайдера и SDK.** `pip install pulumi-kubernetes` без пина → «вчера работало». → `requirements.txt` с точной версией, `package-lock.json` для TS, коммит lock-файлов.
8. **Кодовая база, которую некому поддерживать.** Команда из трёх админов без Python получает «магию», которую боится трогать. → Выбор инструмента по команде: 50 строк HCL лучше 200 строк «умного» Python, который никто не читает.

**Безопасность (что нельзя в боевой среде):**

- Держать `admin.conf` от кластера в домашнем каталоге и копировать его в CI. Правильно — отдельный ServiceAccount с RBAC, ограниченный нужным namespace, и короткоживущий токен.
- Хранить `PULUMI_ACCESS_TOKEN` / пароль бэкенда в открытом виде в GitLab CI-переменных без маскирования и без защиты ветки.
- Считать state «безопасным файлом»: в нём бывают пароли, ключи, сертификаты. Шифрование бэкенда и ограничение доступа к нему — обязательны.
- Давать CI-раннеру права создавать IAM-пользователей и роли уровня аккаунта. Ограничивайте scope провайдера и используйте отдельный «деплой-аккаунт».

## 7. Вопросы на собеседовании

**1. «Чем Pulumi отличается от Terraform? Когда выберете Pulumi?»**

Ответ: «Terraform — собственный движок с декларативным HCL и state. Pulumi — тоже собственный движок со state и провайдерами, но программа пишется на общем языке (Python, TypeScript, Go, C#) и исполняется как обычная программа через language host. Плюсы: циклы, условия, классы, переиспользование через библиотеки, настоящие юнит-тесты с моками, типизация. Минусы: нужен навык программирования у команды, код легко превратить в неподдерживаемую «программу на 2000 строк», меньше людей на рынке. Выберу Pulumi, когда логика сложная и параметризуемая (много похожих окружений, вычисляемые конфиги), когда команда уже пишет на Python/TS и когда нужны тесты инфраструктуры как кода. Останусь на Terraform/OpenTofu, если нужен максимально стандартный, легко передаваемый между подрядчиками код и готовые модули из реестра».

*Что хочет услышать интервьюер:* понимание, что оба — движки со state, а различие в фронтенде и в требованиях к команде.
*Красный флаг:* «Pulumi — это облачный сервис, надо платить» или «Pulumi не хранит state».

**2. «Как Pulumi понимает, что изменилось? Что будет, если потерять state?»**

Ответ: «При запуске программа регистрирует в движке набор ресурсов с их свойствами. Движок берёт предыдущий checkpoint (state), вызывает у провайдера Check/Diff по каждому ресурсу и строит план: create / update / replace / delete. Затем применяет. State хранит URN, физические ID и свойства. Если state потерян, движок считает, что ничего не существует, и попытается создать ресурсы заново — упрётся в конфликт имён либо создаст дубли. Восстановление — из бэкапа (`pulumi stack export`), а для существующих объектов — `pulumi import <тип> <имя> <id>`».

*Что хочет услышать интервьюер:* понимание state, refresh/diff и процедуры восстановления через import.
*Красный флаг:* «state не нужен, Pulumi смотрит в облако» — путает drift detection с отсутствием состояния.

**3. «Что такое CDK и чем AWS CDK отличается от Pulumi?»**

Ответ: «CDK — это подход «инфраструктура как конструктор»: код на языке программирования синтезирует артефакт, который исполняет другой движок. AWS CDK генерирует CloudFormation-шаблон, CDKTF — Terraform JSON, cdk8s — YAML для Kubernetes. Pulumi же не синтезирует чужой формат: у него свой движок, свой state и свои провайдеры, работающие по gRPC. Поэтому в CDKTF все проблемы state и плана наследуются от Terraform, а в AWS CDK вы ограничены возможностями и семантикой CloudFormation».

*Что хочет услышать интервьюер:* чёткое разделение «синтезатор vs движок» — это главный маркер, что человек реально разбирался.
*Красный флаг:* «CDK — это то же самое, что Pulumi, только от Amazon».

**4. «Как в Pulumi работать с секретами?»**

Ответ: «На уровне конфига — `pulumi config set --secret`, значение шифруется ключом стека и в `Pulumi.<stack>.yaml` лежит как `secure:`. Ключ для self-managed бэкенда выводится из `PULUMI_CONFIG_PASSPHRASE`, для Pulumi Cloud управляется сервисом. В коде — `config.require_secret(...)`, такие значения не попадают в открытом виде в логи и output'ы. Для командной работы пароль/токен живут в Vault или CI-переменных, а не в репозитории. Плюс шифрование самого бэкенда state — в state секреты тоже присутствуют».

*Что хочет услышать интервьюер:* разделение «шифрование значения в конфиге» и «защита бэкенда state», плюс упоминание внешнего хранилища секретов.
*Красный флаг:* «Pulumi сам всё шифрует, ничего делать не надо».

**5. «Как тестировать инфраструктурный код на языке программирования?»**

Ответ: «Три уровня. Юнит-тесты: в Pulumi есть механизм моков (`pulumi.runtime.set_mocks`), программа выполняется без реального облака, проверяются свойства ресурсов. Статический анализ и политики: `preview` в MR как обязательный чек, policy-as-code (Pulumi CrossGuard, OPA/Conftest для синтезированных шаблонов) на запрет публичных бакетов, широких RBAC и replace у stateful-ресурсов. Интеграционные тесты: поднять окружение в отдельном стеке/аккаунте, прогнать smoke-проверки, погасить. И обязательный `pulumi preview --diff` в ревью — самый дешёвый тест».

*Что хочет услышать интервьюер:* что человек не ограничивается «задеплоил и посмотрел», а строит пирамиду тестов.
*Красный флаг:* «тестировать IaC не нужно, главное — чтобы apply прошёл».

## 8. Источники (официальная документация)

- **pulumi.com** — раздел *Documentation*: статьи *Pulumi Programming Model*, *Pulumi Kubernetes Provider*, *Managing State & Backends*, *Secrets*. Поиск: `pulumi programming model`, `pulumi login local backend`, `pulumi config secrets`, `pulumi import`.
- **pulumi.com/registry** — реестр провайдеров и пакетов; ищите `kubernetes` и `aws`. Поиск: `pulumi registry kubernetes`.
- **kubernetes.io** — *Documentation → Concepts → Workloads* (Deployment, Service, NodePort) и *Tasks*. Поиск: `kubernetes NodePort service type`.
- **docs.aws.amazon.com** — *AWS Cloud Development Kit (AWS CDK) v2 Developer Guide*: разделы *Getting started*, *Synth*, *Constructs*. Поиск: `aws cdk v2 developer guide synth`.
- **cdk8s.io** — *Documentation*: *Getting started*, *cdk8s synth*. Поиск: `cdk8s getting started python`.
- **developer.hashicorp.com** — документация Terraform (пригодится для сравнения и для CDKTF). Поиск: `terraform state purpose`.
- **CDK for Terraform** — отдельный сайт проекта (домен проверить: `cdktf.io` / раздел в блоге HashiCorp) `[ПРОВЕРИТЬ актуальный адрес и статус разработки]`. Поиск: `cdk for terraform getting started typescript`.
- **gitlab.com / docs.gitlab.com** — *CI/CD pipelines*, переменные и protected-ветки, для шага с репозиторием. Поиск: `gitlab ci protected variables masked`.
