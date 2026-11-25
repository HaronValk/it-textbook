# Zero Trust, mTLS и Service Mesh: Istio/Linkerd (обзор), идентичность сервисов

## 1. Введение (зачем это нужно в реальной работе)

Тикет в понедельник утром:

```
Пользователь: Сидоров Алексей (склад)
ВМ: WIN10-CL02 (работает под своей учётной записью, пока Петрова на больничном)
Что написал: «Открываю "Отгрузку" — страница грузится, а список заказов не появляется,
выдаёт "Сервис недоступен". В пятницу всё работало. Остальные разделы открываются.»
Срочность: высокая, склад не отгружает
```

Вы открываете кластер и видите: поды `Running`, `READY 2/2`, CPU в норме, в логах приложения — тишина. А в понедельник никто ничего не деплоил. Просто в пятницу вечером коллега «навёл порядок в безопасности» и включил в Kubernetes политики авторизации между сервисами.

Именно так эта тема и приходит в реальную работу: не как «поставить Istio», а как «половина сервисов отвалилась после включения политик». После этой главы вы должны уметь: объяснить, что такое Zero Trust и почему «доверять потому, что мы в одной подсети» больше не работает; показать, как выглядит mTLS между подами и откуда берётся идентичность сервиса; поднять service mesh в учебном кластере, проверить, что трафик реально шифруется, и ограничить, кто с кем может общаться; и главное — диагностировать поломку «приложение живо, но получает 403».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Zero Trust** — модель, в которой ни один запрос не считается доверенным только потому, что он пришёл «изнутри» сети. Формально она описана в NIST SP 800-207. Ключевые компоненты:

- **PDP (Policy Decision Point)** — «мозг», который решает: можно или нельзя. В mesh это control plane (istiod или контроллеры Linkerd).
- **PEP (Policy Enforcement Point)** — «руки», которые физически пропускают или блокируют трафик. Это sidecar-прокси рядом с каждым подом.
- **PIP (Policy Information Point)** — откуда берутся данные для решения: кто ты (сертификат, ServiceAccount), с какого адреса, куда идёшь.

Разница с моделью периметра: раньше был «замок и ров» — внутри доверяем всем. Zero Trust говорит: каждый сервис доказывает, кто он, при **каждом** соединении, а решение принимается на основе идентичности, а не IP-адреса.

**mTLS (mutual TLS)** — TLS, где сертификат предъявляют **обе** стороны. В обычном TLS клиент проверяет сервер (это вы делаете каждый раз, когда видите замок в браузере). В mTLS сервер тоже требует сертификат от клиента и проверяет, что тот подписан доверенным CA. Итог: шифрование канала + взаимная аутентификация + защита от подмены (MITM).

**Идентичность сервиса.** В Kubernetes «личность» пода — это его ServiceAccount. Mesh превращает её в криптографическую: CA внутри control plane выдаёт поду короткоживущий X.509-сертификат (SVID), где в Subject Alternative Name записан идентификатор:

```text
Istio:    spiffe://cluster.local/ns/shop/sa/vektor-web      (URI SAN, формат SPIFFE)
Linkerd:  vektor-web.shop.serviceaccount.identity.linkerd.cluster.local  (DNS SAN)
```

SPIFFE (Secure Production Identity Framework for Everyone) — открытый стандарт таких идентификаторов; реализация с автоматической ротацией называется SPIRE. Сертификаты живут недолго (у Istio по умолчанию 24 часа [ПРОВЕРИТЬ актуальное значение], у Linkerd тоже порядка суток) и ротируются автоматически — вручную ничего продлевать не надо. Ключи держатся **в памяти** прокси и раздаются по протоколу SDS (Secret Discovery Service), на диск в под они не пишутся.

**Service mesh** — это два слоя:

```text
                CONTROL PLANE (PDP)
   istiod  /  linkerd-destination + identity + proxy-injector
   • CA: выдаёт SVID каждому поду, ротирует
   • раздаёт конфиг: маршруты, политики mTLS, авторизацию
                    │  xDS (gRPC)        │  SDS (сертификаты)
                    ▼                    ▼
                DATA PLANE (PEP)  — sidecar-прокси в каждом поде
   ┌─────────────────┐    mTLS     ┌─────────────────┐
   │ pod vektor-web  │            │ pod vektor-api  │
   │ app :80         │            │ app :80         │
   │  ▲ localhost    │            │  ▲ localhost    │
   │  │ iptables     │            │  │              │
   │ proxy (Envoy)   │◄──────────►│ proxy (Envoy)   │
   └─────────────────┘  TLS 1.3   └─────────────────┘
        порты Istio: 15001 (outbound), 15006 (inbound),
                     15000 (admin), 15021 (health), 15090 (метрики)
```

Как это работает по шагам: приложение в поде обращается на `localhost:8080` → iptables (его настраивает init-контейнер `istio-init` или `linkerd-init`) перехватывает соединение и отдаёт прокси → прокси открывает наружу mTLS-соединение с прокси второй стороны → тот расшифровывает, проверяет сертификат и политику → отдаёт приложению через localhost. Приложение про TLS вообще не знает: код не меняется.

**Аутентификация ≠ авторизация.** Это главная мысль главы. mTLS отвечает на вопрос «кто ты». Авторизация отвечает «что тебе можно». В Istio за первое отвечает `PeerAuthentication` (режимы `STRICT`, `PERMISSIVE`, `DISABLE`), за второе — `AuthorizationPolicy` (`ALLOW`/`DENY`/`AUDIT`, правила по `principals`, namespace, методам HTTP). В Linkerd — ресурсы `Server`, `MeshTLSAuthentication`, `AuthorizationPolicy`. Включённый STRICT mTLS **ничего не запрещает**: любой под в меше по-прежнему ходит куда угодно.

**Istio vs Linkerd** (оба работают на L4 и L7):

| | Istio | Linkerd |
|---|---|---|
| Прокси | Envoy (C++), тяжелее | linkerd-proxy на Rust, легче по CPU/RAM |
| Функциональность | очень широкая: маршрутизация, fault injection, ingress/egress gateway, ambient-режим без sidecar | сознательно узкая: mTLS, ретраи, таймауты, авторизация, метрики |
| Сложность | высокая, много CRD | низкая, ставится одной командой |
| Когда выбирать | нужны L7-маршруты, canary, gateway, тонкие политики | нужен быстрый «mTLS для всех» с минимумом обслуживания |

Для домашнего стенда на VMware вложенный кластер ограничен по ресурсам — поэтому лабораторная ниже на Linkerd, а Istio показан в командах. Понимание обеих моделей на собеседовании ценится выше, чем знание одной.

## 3. Аналогия из жизни

Представьте бизнес-центр, где сидит «Вектор». Старая модель — вахтёр на входе: показал пропуск один раз, дальше ходи по всем этажам, заходи в любой кабинет, бери документы. Если пропуск потерян или его передали другу — никто не заметит.

Zero Trust — это система, где **на каждой двери** стоит считыватель. Пропуск (сертификат) выдают в едином бюро пропусков (control plane) на сутки, в нём написано, из какого вы отдела (`spiffe://.../sa/vektor-web`). Каждая дверь (sidecar) проверяет: пропуск настоящий, подпись бюро, не истёк, и есть ли у этого отдела право входить именно сюда. mTLS — это когда и вы проверяете бейдж охранника, а не только он ваш: иначе можно случайно рассказать секреты самозванцу, который поставил свою дверь.

Плюс к этому в бюро есть журнал: кто, куда и когда ходил — это телеметрия mesh.

Аналогия ломается на L7: охранник не смотрит, что вы несёте в сумке и каким способом (GET или DELETE) стучитесь в дверь, — а `AuthorizationPolicy` в Istio умеет разрешать только `GET /catalog` и запрещать `DELETE`.

## 4. Практика и команды

Все команды — с UBU01 или GIT01, где настроен `kubectl` на кластер `k8s-cp01`. Если работаете из PowerShell на Windows, помните: `curl` там — алиас `Invoke-WebRequest`, вызывайте `curl.exe`. Бинарники `istioctl.exe` и `linkerd.exe` существуют, логика та же.

### 4.1. Проверка предварительных условий (Linkerd)

```bash
linkerd check --pre
```

`--pre` — только проверки до установки: есть ли доступ к API, хватает ли прав, не заняты ли порты, поддерживается ли версия Kubernetes. Смотрим на последнюю строку:

```text
Status check results are √
```

Любой `×` — ставить нельзя, читаем подсказку под строкой.

### 4.2. Установка control plane

```bash
linkerd install --crds | kubectl apply -f -
linkerd install | kubectl apply -f -
linkerd check
```

`install --crds` — сначала CRD (описания новых типов ресурсов), потом сам control plane. `linkerd check` без `--pre` проверяет уже работающую установку: поднялись ли поды, есть ли валидные сертификаты, доступна ли identity-служба. Ожидаемо: в namespace `linkerd` три пода `linkerd-identity`, `linkerd-destination`, `linkerd-proxy-injector` в состоянии `Running`.

### 4.3. Включение sidecar-инъекции

```bash
kubectl get deploy -n shop -o yaml | linkerd inject - | kubectl apply -f -
kubectl get pods -n shop
```

`linkerd inject -` читает манифест со stdin, добавляет в поды init- и sidecar-контейнер и отдаёт результат в stdout. В выводе `kubectl get pods` появится `READY 2/2` вместо `1/1` — это и есть признак меша. Альтернатива для новых подов — аннотация на namespace: `kubectl annotate ns shop linkerd.io/inject=enabled`.

### 4.4. Проверка, что трафик реально шифруется

```bash
linkerd viz stat deploy -n shop
linkerd viz tap deploy/vektor-api -n shop
```

`viz stat` показывает по колонкам `TLS` и `AUTHORIZED` процент защищённых запросов:

```text
NAME         MESHED   SUCCESS   RPS   LATENCY_P95   TLS    AUTHORIZED
vektor-api      1/1   100.00%   1.4           3ms   100%          100%
```

`tap` — живой сниффер (как tcpdump, но на уровне L7 и без расшифровки в файл). В строке запроса ищем `tls=true`:

```text
req id=0:1 proxy=in src=10.244.1.15:51234 dst=10.244.2.7:8080 tls=true :method=GET :path=/
```

Если `tls=false` — под не в меше либо запрос идёт мимо прокси.

### 4.5. Посмотреть сертификат и идентичность пода

```bash
linkerd identity -n shop -l app=vektor-web   # точный набор флагов [ПРОВЕРИТЬ]
```

Вывод — разобранный X.509: `Issuer`, `Validity`, `Serial Number` и, главное, SAN с идентичностью вида `vektor-web.shop.serviceaccount.identity.linkerd.cluster.local`. Живёт такой сертификат около 24 часов и продлевается сам.

### 4.6. Разрешить трафик только от конкретного сервиса

```yaml
apiVersion: policy.linkerd.io/v1beta3
kind: Server
metadata:
  name: vektor-api-http
  namespace: shop
spec:
  podSelector:
    matchLabels:
      app: vektor-api
  port: http
  proxyProtocol: HTTP/1        # допустимые значения [ПРОВЕРИТЬ]
---
apiVersion: policy.linkerd.io/v1alpha1
kind: MeshTLSAuthentication
metadata:
  name: web-identity
  namespace: shop
spec:
  identities:
    - "vektor-web.shop.serviceaccount.identity.linkerd.cluster.local"
---
apiVersion: policy.linkerd.io/v1alpha1
kind: AuthorizationPolicy
metadata:
  name: allow-only-web
  namespace: shop
spec:
  targetRef:
    group: policy.linkerd.io
    kind: Server
    name: vektor-api-http
  requiredAuthenticationRefs:
    - group: policy.linkerd.io
      kind: MeshTLSAuthentication
      name: web-identity
```

`Server` описывает порт пода, который мы защищаем. `MeshTLSAuthentication` — список разрешённых идентичностей. `AuthorizationPolicy` связывает их: «на этот Server пускать только тех, кто прошёл эту аутентификацию». Как только для порта создан `Server`, политика по умолчанию становится «запрещено всё, что не разрешено явно».

### 4.7. Проверить, какие политики применились

```bash
linkerd viz authz -n shop
kubectl get server,authorizationpolicy,meshtlsauthentication -n shop
```

`viz authz` печатает таблицу «Server → его политики → сколько запросов разрешено/запрещено». Это первое, что открывают при разборе «а почему 403».

### 4.8. Istio: установка и проверка

```bash
istioctl install --set profile=default -y
kubectl label namespace shop istio-injection=enabled --overwrite
kubectl rollout restart deploy -n shop
istioctl analyze -A
```

`profile=default` — рабочий профиль (в `demo` включено лишнее для учебных демо). `analyze` — статический линтер конфигурации: находит конфликты политик, обращения к несуществующим хостам, забытые аннотации. В норме: `No validation issues found`.

### 4.9. Istio: включить строгий mTLS

```bash
kubectl explain peerauthentication.spec.mtls.mode
```

Показывает встроенную документацию по полю — быстрее, чем гуглить. Дальше манифест:

```yaml
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: shop
spec:
  mtls:
    mode: STRICT
```

`STRICT` — принимать только mTLS-трафик. `PERMISSIVE` — принимать и mTLS, и обычный (переходный режим). `DISABLE` — не требовать. Включать STRICT можно только когда все поды в namespace имеют sidecar, иначе они мгновенно получат отказ.

### 4.10. Istio: разрешить только свой сервис

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: allow-web-to-api
  namespace: shop
spec:
  selector:
    matchLabels:
      app: vektor-api
  action: ALLOW
  rules:
    - from:
        - source:
            principals:
              - "cluster.local/ns/shop/sa/vektor-web"
      to:
        - operation:
            methods: ["GET", "POST"]
            paths: ["/api/*"]
```

`principals` — SPIFFE-идентичность без префикса `spiffe://`. `action: ALLOW` при наличии хотя бы одной ALLOW-политики означает «всё, что не разрешено, — запрещено».

### 4.11. Istio: статус синхронизации и сертификаты

```bash
istioctl proxy-status
istioctl proxy-config secret <pod> -n shop
istioctl x describe pod <pod> -n shop
```

`proxy-status` — таблица подов с колонками `CDS / LDS / EDS / RDS`: если везде `SYNCED`, прокси получил конфиг. `proxy-config secret` показывает выпущенный сертификат: `VALID CERT`, серийный номер, даты `NOT BEFORE / NOT AFTER`. `x describe pod` — сводка: попал ли под под политику STRICT и какие authorization policies на него действуют.

### 4.12. Идентичность на уровне Kubernetes (основа всего)

```bash
kubectl create token vektor-web -n shop --duration=1h
kubectl auth can-i --list --as=system.serviceaccount:shop:vektor-web
```

Первая команда выпускает короткоживущий токен ServiceAccount (тот самый механизм, на котором строится доверие к workload). Вторая показывает, что этому ServiceAccount разрешено в RBAC: `--as` подменяет пользователя без реального входа. Обычно сервису не нужно почти ничего — и это правильно.

### 4.13. Проверка произвольного TLS-эндпоинта

```bash
openssl s_client -connect vektor-api.shop.svc:8080 -showcerts </dev/null
```

Показывает цепочку сертификатов сервера. Внутрь меша вы так не попадёте (там mTLS и прокси ждёт сертификат клиента), но для внешних балансировщиков и ingress — рабочий инструмент.

## 5. Лабораторная работа

### Предусловия

- Готовый кластер из урока 99: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63). Минимум 2 vCPU и 4 ГБ RAM на ноду — иначе mesh не влезет.
- `kubectl` с рабочим kubeconfig на UBU01 (10.10.10.30).
- HARBOR01 (10.10.10.55) доступен, в нём есть проект `library` с образом `nginx:1.27-alpine`. Если образа нет — сначала урок 97 (зеркала репозиториев), без рабочего зеркала в РФ образы могут не тянуться.
- Namespace `shop` с двумя сервисами. Если его нет — создайте из манифеста ниже (он же пойдёт в Git).

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: shop
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: vektor-web, namespace: shop}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: vektor-api, namespace: shop}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: vektor-api, namespace: shop}
spec:
  replicas: 1
  selector: {matchLabels: {app: vektor-api}}
  template:
    metadata: {labels: {app: vektor-api}}
    spec:
      serviceAccountName: vektor-api
      containers:
        - name: api
          image: 10.10.10.55/library/nginx:1.27-alpine
          ports: [{name: http, containerPort: 80}]
---
apiVersion: v1
kind: Service
metadata: {name: vektor-api, namespace: shop}
spec:
  selector: {app: vektor-api}
  ports: [{name: http, port: 8080, targetPort: http}]
```

Аналогично `vektor-web` (свой ServiceAccount и Deployment). Плюс клиентский под для проверок:
`kubectl -n shop run client --image=10.10.10.55/library/curl:8.10.1 --command -- sleep 3600` [ПРОВЕРИТЬ наличие образа].

Если кластер ещё не поднят — вернитесь к уроку о развёртывании Kubernetes, эта лабораторная без него невозможна.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-100` — на `k8s-cp01`, `k8s-w01`, `k8s-w02` (обязательно), а также на `GIT01` и `HARBOR01`, если будете коммитить манифесты и заливать образы. Пока ВМ работает, снапшот памяти можно не делать — достаточно состояния диска, но кластер после отката придётся перезапустить.

### Шаги

1. **UBU01.** `kubectl get nodes -o wide` → три ноды в статусе `Ready`.
2. **GIT01.** В репозитории создайте каталог `mesh/` и положите туда `apps.yaml` из предусловий и `authz-linkerd.yaml` из п. 4.6. Закоммитьте: `git add mesh/ && git commit -m "mesh: mTLS + authz for shop"`. Ожидаемо: `2 files changed`.
3. **UBU01.** Примените приложение: `kubectl apply -f apps.yaml` → `namespace/shop created`, `deployment.apps/vektor-api created`, `service/vektor-api created`.
4. **UBU01.** `linkerd check --pre` → все проверки `√`.
5. **UBU01.** `linkerd install --crds | kubectl apply -f -` затем `linkerd install | kubectl apply -f -` → появляются CRD и namespace `linkerd`.
6. **UBU01.** `linkerd check` → `Status check results are √`. Если висит `waiting for check to complete` — control plane ещё стартует, подождите минуту.
7. **UBU01.** `linkerd viz install | kubectl apply -f -` и `linkerd viz check` → установлен `linkerd-viz` (Prometheus + дашборд). Проверка: `kubectl get pods -n linkerd-viz` → все `Running`.
8. **UBU01.** `kubectl get deploy -n shop -o yaml | linkerd inject - | kubectl apply -f -` → `deployment.apps/vektor-api configured (meshed)`.
9. **UBU01.** `kubectl get pods -n shop` → `READY 2/2` у `vektor-api` и `vektor-web`. Клиентский под тоже пересоздайте с инъекцией (или запустите его уже после шага 8 — тогда он получит sidecar автоматически).
10. **UBU01.** `kubernetes get svc -n shop` (опечатка специально не нужна — просто `kubectl get svc -n shop`), запомните ClusterIP `vektor-api`. Из клиентского пода: `kubectl -n shop exec -it client -- curl -s -o /dev/null -w "%{http_code}\n" http://vektor-api:8080` → `200`.
11. **UBU01.** `linkerd viz stat deploy -n shop` → в колонках `TLS` и `AUTHORIZED` стоит `100%`.
12. **UBU01.** `linkerd viz tap deploy/vektor-api -n shop` в одном терминале, во втором повторите curl → в выводе tap строка с `tls=true`. Это и есть доказательство mTLS.
13. **UBU01.** `kubectl apply -f mesh/authz-linkerd.yaml` → созданы `Server`, `MeshTLSAuthentication`, `AuthorizationPolicy`.
14. **UBU01.** Из клиентского пода снова curl → `403`. Из пода `vektor-web`: `kubectl -n shop exec deploy/vektor-web -- curl -s -o /dev/null -w "%{http_code}\n" http://vektor-api:8080` → `200`. Разница — ровно в идентичности, которую предъявляет прокси.
15. **UBU01.** `linkerd viz authz -n shop` → у `vektor-api-http` видна политика `allow-only-web`, счётчик `DENIED` растёт, `ALLOWED` — только от web.
16. **Запись в журнал.** Допишите в `stand-log.md` (см. ниже) и закоммитьте манифесты в Git.

### Проверка

- [ ] В namespace `linkerd` три пода control plane в `Running`, `linkerd check` зелёный.
- [ ] Все поды `shop` имеют `READY 2/2` (sidecar инжектирован).
- [ ] `linkerd viz stat deploy -n shop` показывает `TLS 100%`.
- [ ] `linkerd viz tap` показывает `tls=true` для запросов к `vektor-api`.
- [ ] Запрос от `client` получает `403`, от `vektor-web` — `200`.
- [ ] `linkerd viz authz -n shop` показывает применённую политику.
- [ ] Манифесты лежат в Git, в `stand-log.md` появилась запись.

### Сломай и почини

```
Пользователь: Сидоров Алексей (склад)
ВМ: WIN10-CL02 (под своей учётной записью на время отсутствия Петровой)
Что написал: «Раздел "Отгрузка" не открывается — "Сервис недоступен".
Остальные страницы работают. Началось после обеда.»
Срочность: высокая
```

**Как воспроизвести.** В `mesh/authz-linkerd.yaml` поменяйте идентичность на несуществующий ServiceAccount:

```yaml
  identities:
    - "vektor-front.shop.serviceaccount.identity.linkerd.cluster.local"
```

Примените (`kubectl apply -f mesh/authz-linkerd.yaml`) и дёрните API из `vektor-web`.

**Диагностика по порядку:**

1. `kubectl get pods -n shop` — поды `Running 2/2`, рестартов нет. Значит, приложение живо, дело в сети/политике. Уже половина гипотез отпала.
2. `kubectl -n shop exec deploy/vektor-web -- curl -v http://vektor-api:8080` → `HTTP/1.1 403 Forbidden` и заголовок от прокси вида `l5d-proxy-error` [ПРОВЕРИТЬ точный текст]. Код `403`, а не `502/503` — значит, соединение установилось, но прокси-получатель отказал в авторизации.
3. `kubectl -n shop exec deploy/vektor-web -- curl -v http://vektor-api:8080` при **удалённой** политике (`kubectl delete -f mesh/authz-linkerd.yaml`) → `200`. Гипотеза «сломалась авторизация» подтверждена.
4. `linkerd viz authz -n shop` → у `vektor-api-http` политика есть, но `ALLOWED` = 0, `DENIED` растёт.
5. `linkerd viz tap deploy/vektor-api -n shop` → видно `tls=true`, но `:status=403`. Шифрование работает, отказ — на уровне policy.
6. Сверяем идентичность: `linkerd identity -n shop -l app=vektor-web` даёт `vektor-web.shop.serviceaccount.identity.linkerd.cluster.local`, а в `MeshTLSAuthentication` написано `vektor-front...`. Вот и причина — опечатка/копипаста.
7. Правим `identities`, `kubectl apply -f`, повторяем curl → `200`.

**Почему пользователь видел 503, а не 403.** Веб-приложение `vektor-web` получило от API `403`, не смогло собрать страницу и вернуло браузеру свою «Сервис недоступен» — типичная картина: наружу уходит 5xx, а настоящая причина — внутренний 403 в меше. Поэтому первым делом смотрим не логи фронта, а `tap` и `authz`.

**Ответ пользователю:** «Алексей, проблема не в вашем рабочем месте и не в данных — сбой в связке внутренних сервисов склада. Причину нашёл, исправил конфигурацию доступа, "Отгрузка" должна открываться. Если через 5 минут всё ещё не грузится — обновите страницу с Ctrl+F5 и напишите мне».

**Закрытие заявки:** категория «Сервисы / доступ между сервисами», решение «исправлена идентичность в политике авторизации», корневая причина «ручное редактирование политики без ревью». Действие: манифесты меняются только через merge request в GitLab на GIT01 — тогда опечатку поймает ревью.

### Что изменилось на стенде

- Кластер k8s: установлен Linkerd (namespace `linkerd`: `linkerd-identity`, `linkerd-destination`, `linkerd-proxy-injector`) и `linkerd-viz`.
- Namespace `shop`: поды `vektor-web`, `vektor-api` с sidecar-прокси (`2/2`), клиентский под `client`.
- ServiceAccount `vektor-web`, `vektor-api` в namespace `shop` — теперь это идентичности сервисов.
- Объекты политик: `Server/vektor-api-http`, `MeshTLSAuthentication/web-identity`, `AuthorizationPolicy/allow-only-web`.
- GIT01: в репозитории появился каталог `mesh/` (`apps.yaml`, `authz-linkerd.yaml`).
- HARBOR01: без изменений (использован существующий проект `library`).
- `stand-log.md`: запись о включённом mTLS и о том, что трафик web→api ограничен политикой.

### Откат

В VMware Workstation: правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-100` → **Go to**. Сделайте это на `k8s-cp01`, `k8s-w01`, `k8s-w02` (и на `GIT01`/`HARBOR01`, если их трогали), затем включите машины и дождитесь `kubectl get nodes` → `Ready`.

Мягкий вариант без отката ВМ: `linkerd uninstall | kubectl delete -f -` (снесёт control plane), затем `kubectl rollout restart deploy -n shop` — иначе sidecar останется в подах и без control plane будет мешать трафику. Манифесты из Git удаляйте отдельным коммитом, историю не переписывайте.

## 6. Ошибки новичков и безопасность

1. **Включить STRICT mTLS, не проверив sidecar у всех подов.** → Поды без прокси и внешние клиенты (Prometheus, cronjob, отладка через `curl` с ноутбука) мгновенно получают отказ. → Сначала `PERMISSIVE`, посмотрите `linkerd viz stat` / `istioctl x describe pod`: `TLS` должен быть 100%, и только потом STRICT.
2. **Считать, что mTLS = авторизация.** → «Трафик шифруется» не значит «ходить можно только своим»: любой под в меше с валидным сертификатом ходит куда угодно. → Всегда добавляйте `AuthorizationPolicy` следом за `PeerAuthentication`.
3. **Ставить mesh на `kube-system` и все namespace сразу.** → Риск сломать DNS, ingress, kubelet-пробы; отладка резко усложняется. → Инкрементально: один namespace, один сервис, проверка, следующий.
4. **Хардкодить идентичности без единого шаблона.** → Опечатка в строке `vektor-front...` тихо ломает прод (см. «Сломай и почини»). → Манифесты только через Git и ревью, идентичности — из переменных в шаблоне Helm/Kustomize.
5. **Забыть про срок жизни корневого CA.** → Trust anchor, сгенерированный CLI по умолчанию, живёт ограниченное время [ПРОВЕРИТЬ для вашей версии]; просроченный root CA кладёт всю связность меша разом. → Хранить root и issuer в Vault PKI или cert-manager (урок 96), поставить алерт на дату истечения, провести учение по ротации.
6. **Думать, что mesh защищает от взломанного пода.** → Если злоумышленник выполнил код внутри `vektor-web`, он использует его валидную идентичность и легальный mTLS — политика его пропустит. → Идентичность ограничивает **сервис**, а не скомпрометированный процесс: нужны runtime-защита и Pod Security из урока 99, минимальные ServiceAccount и NetworkPolicy в дополнение.
7. **Не мониторить сам mesh.** → Отказы авторизации выглядят как «приложение тупит», а не как ошибка безопасности. → Дашборды на `403` от прокси, алерт на `TLS < 100%`, регулярный `linkerd check` / `istioctl analyze` в CI.
8. **Складывать ключи CA и kubeconfig в Git «чтобы не потерять».** → Компрометация CA = компрометация всех идентичностей кластера. → Секреты только в Vault/Kubernetes Secret с шифрованием etcd, в Git — `gitleaks` в пайплайне (урок 98).

**Безопасность.** В боевой среде mesh — это часть периметра безопасности: root CA меша равнозначен ключу от домена. Никогда не отключайте mTLS (`DISABLE`) «чтобы быстрее отладить» на проде — отлаживайте в отдельном namespace. Не выдавайте разработчикам `cluster-admin` «для тестов политик»: моделируйте `kubectl auth can-i --as`. В этой лабораторной мы для простоты поставили один control plane и не настраивали HA — в продакшене control plane ставится в 2–3 репликах на разных нодах.

## 7. Вопросы на собеседовании

**1. Что такое Zero Trust и чем он отличается от модели периметра?**

Ответ: модель, в которой доверие не выдаётся по признаку «находится внутри сети». Каждый запрос аутентифицируется и авторизуется заново на основе идентичности, устройства и контекста (NIST SP 800-207). Есть PDP (принимает решение), PEP (исполняет), PIP (поставляет данные). В Kubernetes реализуется через идентичность ServiceAccount → SPIFFE-сертификат → mTLS → авторизационную политику; сетевые политики и RBAC — дополняют, но не заменяют. Отличие от периметра: компрометация одной машины внутри больше не даёт свободный доступ ко всему.

*Что хочет услышать интервьюер:* что вы понимаете «идентичность вместо IP» и знаете разделение PDP/PEP, а не просто пересказали лозунг.
*Красный флаг:* «это когда ставят файрвол и VPN» — это про периметр, а не про Zero Trust.

**2. Откуда в mesh берётся идентичность пода и как работает mTLS?**

Ответ: идентичность — ServiceAccount пода, превращённый CA control plane в короткоживущий X.509 SVID (Istio — SPIFFE URI `spiffe://cluster.local/ns/shop/sa/vektor-web`, Linkerd — DNS SAN `vektor-web.shop.serviceaccount.identity.linkerd.cluster.local`). Сертификат раздаётся прокси по SDS, ключ хранится в памяти, TTL порядка суток, ротация автоматическая. Прокси-инициатор открывает TLS 1.3, обе стороны предъявляют сертификаты, проверяется цепочка до trust anchor и SAN; дальше прокси-получатель сверяет SAN с `AuthorizationPolicy` и применяет L7-правила.

*Что хочет услышать интервьюер:* связку ServiceAccount → SPIFFE → SVID → ротация → проверка на стороне получателя.
*Красный флаг:* «сертификаты лежат в секрете Kubernetes» — в mesh они в памяти прокси, иначе какой смысл в авто-ротации.

**3. Istio или Linkerd — что выберете и почему?**

Ответ: зависит от задачи. Linkerd — тонкий Rust-прокси, минимальный оверхед, ставится одной командой, закрывает mTLS/ретраи/таймауты/авторизацию/метрики. Istio — Envoy, огромная функциональность: L7-маршрутизация, canary и fault injection, ingress/egress gateway, ambient-режим без sidecar. Для небольшой команды без отдельного платформенного инженера — Linkerd; для крупной платформы с L7-требованиями — Istio. Смотреть надо также на потребление ресурсов на ноду и на наличие экспертизы; плюс в РФ — вопрос доступности образов и зеркал.

*Что хочет услышать интервьюер:* осознанный выбор по критериям, а не «Istio — стандарт».
*Красный флаг:* «Istio всегда лучше, потому что функций больше» без упоминания сложности и стоимости эксплуатации.

**4. Аутентификация и авторизация в mesh — в чём разница и какими объектами управляются?**

Ответ: аутентификация — «кто ты»: `PeerAuthentication` со `STRICT/PERMISSIVE/DISABLE` в Istio, mTLS-идентичность в Linkerd. Авторизация — «что можно»: `AuthorizationPolicy` в Istio с `principals`, namespace, методами и путями; в Linkerd — `Server` + `MeshTLSAuthentication` + `AuthorizationPolicy`. Важно: STRICT mTLS сам по себе никого не ограничивает, а ALLOW-политика включает режим deny-by-default для выбранных подов.

*Что хочет услышать интервьюер:* что вы не путаете шифрование с ограничением доступа и знаете про deny-by-default.
*Красный флаг:* «включил STRICT — теперь безопасно».

**5. Сервис не в меше (или снаружи кластера) — как ему достучаться до пода со STRICT mTLS?**

Ответ: никак напрямую — в `STRICT` plaintext-трафик отклоняется. Варианты: (1) переходный `PERMISSIVE` на время миграции; (2) завести клиента в меш (инъекция sidecar / ambient); (3) пройти через ingress/egress gateway меша, который терминирует внешний TLS и внутри идёт по mTLS с корректной идентичностью gateway; (4) для health-проверок kubelet — включить переписывание проб (`rewriteAppHTTPProbe`). Выбор зависит от того, кто клиент: внешняя система, job без sidecar или другой кластер.

*Что хочет услышать интервьюер:* понимание, что mesh — не «магический слой», а явная граница доверия, и у неё есть входные точки.
*Красный флаг:* «просто отключу mTLS для этого namespace» — так доверие не строится.

## 8. Источники (официальная документация)

- **NIST** — `csrc.nist.gov`. Специальная публикация SP 800-207 «Zero Trust Architecture». Поиск: `NIST SP 800-207 zero trust architecture`.
- **Istio** — `istio.io`. Разделы: «Concepts → Security» (mTLS, идентичность, SDS), справочник по `PeerAuthentication` и `AuthorizationPolicy`, «Ambient Mode». Поиск: `istio security concepts mutual TLS`, `istio authorization policy principals`.
- **Linkerd** — `linkerd.io`. Разделы: «Automatic mTLS», «Authorization policy», «linkerd check», «Debugging 403s» [ПРОВЕРИТЬ точное название раздела]. Поиск: `linkerd automatic mTLS`, `linkerd authorization policy Server MeshTLSAuthentication`.
- **SPIFFE/SPIRE** — `spiffe.io`. Разделы: «SPIFFE ID», «X.509-SVID», «Workload API». Поиск: `SPIFFE ID specification`, `X.509 SVID spiffe`.
- **Kubernetes** — `kubernetes.io`. Разделы: «Managing Service Accounts», «Service Account Token Volume Projection», «Authorization Overview». Поиск: `kubernetes service account token volume projection`, `kubernetes RBAC authorization`.
- **IETF** — `datatracker.ietf.org`. RFC 8446 (TLS 1.3), RFC 5280 (X.509). Поиск: `RFC 8446 TLS 1.3`, `RFC 5280 X.509 certificate profile`.
- **Envoy** — `envoyproxy.io`. Раздел «Architecture overview», «TLS / Secret discovery service». Поиск: `envoy SDS secret discovery service`.
- **CNCF** — `cncf.io`. Обзоры проекта и ландшафта service mesh [ПРОВЕРИТЬ актуальность конкретного раздела]. Поиск: `CNCF service mesh landscape`.
