# Ingress и Gateway API: Nginx Ingress, Traefik, Envoy Gateway, cert-manager

## 1. Введение (зачем это нужно в реальной работе)

Тикет в вашей очереди:

> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01
> **Что написал:** «Нам прислали ссылку на внутреннюю страницу `https://vector.corp.vektor.lab` — браузер пишет "Ваше подключение не является защищённым". И почему-то у коллег из HR та же ссылка открывается по IP, а по имени нет. Разберитесь, пожалуйста».
> **Срочность:** средняя

Этот тикет — ровно про тему главы. Пользователь видит только «браузер ругается», а внутри — цепочка: имя резолвится не туда, трафик приходит на контроллер Ingress, тот по хосту выбирает правило, правило указывает на Service, Service — на Pod'ы, а сертификат для HTTPS кто-то (или что-то) должен был выпустить и продлить. В DevOps-реальности через Ingress проходит вообще весь внешний трафик к сервисам, поэтому «не открывается сайт» почти всегда означает работу на этом слое.

После главы вы должны уметь: поднять контроллер Ingress в кластере без облачного балансировщика (MetalLB на своём стенде), описать правила маршрутизации по хостам и путям, расшифровать, откуда взялись 404/503/502 от Ingress, автоматически выпустить и продлить TLS-сертификат через cert-manager, и переписать то же самое декларативно на Gateway API (GatewayClass + Gateway + HTTPRoute). И всё это — в Git, воспроизводимо, а не «руками в консоли».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Проблема, которую решает Ingress.** Service типа LoadBalancer в голом дата-центре (ваш стенд в VMware) никуда не «балансирует» — нет облачного провайдера, который выдаст IP. Даже если бы был, каждый сервис получал бы свой публичный адрес. А надо: один вход снаружи, разные сайты и пути внутри кластера. Это L7-маршрутизация — слой 7 модели OSI, в отличие от Service, который работает на L4 (TCP/UDP по IP:порт).

**Ingress — это только декларация.** Объект `Ingress` — набор правил «хост + путь → Service:порт». Он ничего не исполняет. Исполняет **Ingress-контроллер** — обычное приложение в кластере (Deployment или DaemonSet), которое через API-сервер смотрит за объектами `Ingress`, `Service`, `EndpointSlice`, `Secret`, генерирует из них свой конфиг (у nginx — `nginx.conf`), делает reload и проксирует трафик. Контроллер сам публикуется наружу через Service типа `LoadBalancer` или `NodePort`.

```text
                              слой L7 (хосты, пути, TLS)
  Браузер  ──►  VIP 10.10.10.200:443
                    │
                    ▼
        ┌───────────────────────────────┐
        │  ingress-nginx-controller Pod │  ◄── watch API: Ingress/Service/Endpoints/Secret
        │  (nginx + Go-контроллер)      │      генерирует nginx.conf, reload
        └───────────────┬───────────────┘
                        │  proxy_pass на ClusterIP (слой L4)
                        ▼
           Service web-vektor (ClusterIP 10.96.x.x:80)
                        │  kube-proxy / iptables / IPVS
                        ▼
           Pod nginx-1  /  Pod nginx-2   (10.244.x.x)
```

**Ключевые понятия Ingress:**

| Объект | Что делает |
|---|---|
| `IngressClass` | «Какой контроллер обслуживает этот Ingress». Несколько контроллеров в кластере — норм, выбираются по `spec.ingressClassName` |
| `pathType` | `Prefix` (по сегментам пути), `Exact`, `ImplementationSpecific` |
| аннотации | Всё, чего нет в спецификации: редиректы, таймауты, размер тела запроса. Vendor lock-in |
| TLS | `spec.tls[].secretName` — стандартный `kubernetes.io/tls` Secret с `tls.crt`/`tls.key`. Сам по себе не появляется |

**Gateway API** — следующий стандарт (GA с версии v1.0, CRD ставятся отдельно). Идея — разделить ответственность по ролям, а не запихивать всё в один объект с аннотациями:

```text
  Инфра-команда (cluster-scope)          Команда приложения (namespace)
  ┌──────────────────────────┐          ┌─────────────────────────────┐
  │ GatewayClass  "eg"       │◄─────────│                             │
  │  controllerName: ...     │          │                             │
  ├──────────────────────────┤          │  HTTPRoute  web-vektor      │
  │ Gateway  vektor-gw       │◄─────────│   parentRefs: [vektor-gw]   │
  │  listeners: http:80      │  bind    │   hostnames: [...]          │
  │  allowedRoutes: Same     │          │   rules: matches + filters  │
  └──────────────────────────┘          │          + backendRefs      │
                                        └─────────────────────────────┘
```

`GatewayClass` — «какой контроллер реализует этот класс» (аналог IngressClass). `Gateway` — сам вход: адрес, порты, TLS, из каких namespace разрешено подключаться. `HTTPRoute` — правила приложения: хосты, пути, заголовки, методы, веса бэкендов (канареечные релизы «из коробки»), редиректы, ретраи. Расширения — через `Policy Attachment` (например, `BackendTLSPolicy`), а не через строковые аннотации.

**Контроллеры, которые надо знать:**

- **ingress-nginx** (kubernetes/ingress-nginx) — nginx + Go-контроллер, де-факто стандарт. Важно: проект официально сворачивается, SIG Network объявил о прекращении поддержки в 2026 году `[ПРОВЕРИТЬ точные даты]` — на собеседовании это хороший маркер, что вы читаете новости. Есть отдельный продукт F5 «NGINX Ingress Controller» (nginxinc/kubernetes-ingress) — это другое.
- **Traefik** (v3) — Go, лёгкий, умеет ACME сам, без cert-manager. Свои CRD: `IngressRoute`, `Middleware`, `TLSStore`. Дефолт в k3s.
- **Envoy Gateway** — реализация Gateway API на Envoy от проекта Envoy Proxy. Нативно Gateway API, фильтры (ext_authz, rate limit, JWT), CRD `EnvoyProxy` для тонкой настройки data plane.

**cert-manager** — оператор (про CRD и операторы будет урок 44), который превращает «хочу сертификат» в объект Kubernetes. Его CRD: `Issuer` (namespace-scoped) и `ClusterIssuer` (на весь кластер) — «кто и как подписывает»; `Certificate` — «какой сертификат нужен и в какой Secret положить»; служебные `CertificateRequest`, `Order`, `Challenge` — внутренняя механика. Для Let's Encrypt (протокол ACME) есть три типа проверки владения доменом: **HTTP-01** (файл-токен по `http://домен/.well-known/acme-challenge/...`, нужен внешний 80 порт, wildcard не поддерживает), **DNS-01** (TXT-запись `_acme-challenge.домен`, работает для wildcard и в закрытых контурах, но нужен API-доступ к DNS-провайдеру через webhook), **TLS-ALPN-01** (редко). Порт наружу — 80/443, внутри — как решит контроллер.

## 3. Аналогия из жизни

Представьте бизнес-центр «Вектор». Снаружи один адрес и один вход с турникетами. На входе сидит **вахтёр** — это Ingress-контроллер: он физически стоит на проходной и провожает людей. У вахтёра есть **журнал** — это объект `Ingress`: «`vector.corp.vektor.lab` — в бухгалтерию, на третий этаж»; «`/api` — в серверную». Журнал сам по себе никого не пускает: если вахтёра нет, журнал можно сколько угодно печатать — на проходной никого.

Когда арендаторов стало двадцать, выяснилось, что журнал один, а редактируют его все, кто хочет, и половина записей — приписки на полях (аннотации). Тогда управляющая компания ввела **Gateway API**: она отвечает за здание и турникеты (`GatewayClass` + `Gateway` — «этот вход обслуживает такая-то охрана, вот эти порты открыты, вот этим фирмам вход разрешён»), а каждая фирма сама пишет свою страницу журнала (`HTTPRoute` — «кто именно и по какому пути идёт к нам в офис»). Никто не лезет в чужие записи, и всё типизировано: не «приписка на полях», а отдельное поле.

**cert-manager** — это паспортный стол: он сам следит за сроками, заранее заказывает продление пропусков и приносит готовый пропуск на вахту. И, как паспортный стол, он бесполезен, если вы забыли сказать ему, кого именно обслуживать.

Аналогия перестаёт работать в одном месте: вахтёр читает журнал один раз утром, а контроллер перечитывает свои объекты постоянно и перестраивает конфиг на ходу — без перезагрузки здания.

## 4. Практика и команды

**1. Есть ли вообще контроллер и какой класс используется**

```bash
kubectl get ingressclass
kubectl get pods -A | grep -Ei 'ingress|traefik|envoy'
```
```text
NAME    CONTROLLER             PARAMETERS   AGE
nginx   k8s.io/ingress-nginx   <none>       14m
eg      gateway.envoyproxy.io/gatewayclass-controller   <none>   6m
```
Смотрим: имя класса (`nginx`, `eg`) и `CONTROLLER`. Именно это имя пишется в `spec.ingressClassName` / `spec.gatewayClassName`. Пустой список = Ingress-объекты будут создаваться, но никто их не obслужит.

**2. Публикация контроллера наружу**

```bash
kubectl get svc -n ingress-nginx ingress-nginx-controller -o wide
```
```text
NAME                       TYPE           CLUSTER-IP    EXTERNAL-IP    PORT(S)          AGE
ingress-nginx-controller   LoadBalancer   10.96.14.7    10.10.10.200   80:31234/TCP,443:32567/TCP   14m
```
`EXTERNAL-IP: <pending>` навсегда — классика: Service типа LoadBalancer без «облака». Лечится MetalLB (шаг 1 лабораторной) либо переходом на NodePort и обращением по `<IP ноды>:<nodePort>`.

**3. Список правил и адрес, который реально анонсируется**

```bash
kubectl get ingress -n demo -o wide
kubectl describe ingress web-vektor -n demo
```
```text
NAME         CLASS   HOSTS                     ADDRESS        PORTS     AGE
web-vektor   nginx   vector.corp.vektor.lab    10.10.10.200   80, 443   6m
```
В `describe` важны две вещи: блок `Rules` (совпадают ли host/path/порт бэкенда с ожиданием) и `Events` (там бывают `Sync`/ошибки admission-валидации). `ADDRESS` заполняет контроллер — если пусто, контроллер объект не увидел.

**4. Проверка «с нужным SNI и Host», не трогая DNS**

```bash
curl -k -v --resolve vector.corp.vektor.lab:443:10.10.10.200 https://vector.corp.vektor.lab/
```
`--resolve host:port:ip` подставляет адрес только для этого запроса — удобно, когда DNS ещё не настроен, а `-k` игнорирует самоподписанный сертификат. В выводе ищите строку `* Server certificate:` (какой CN выдали) и потом `< HTTP/1.1 200`.

**5. Почему именно 503: смотрим Endpoints, а не Pod'ы**

```bash
kubectl get endpointslices -n demo -l kubernetes.io/service-name=web-vektor
kubectl get pods -n demo -o wide --show-labels
```
```text
NAME            ADDRESSTYPE   PORTS   ENDPOINTS                     AGE
web-vektor-abc  IPv4          80      10.244.1.15,10.244.2.9        6m
```
Пустой `ENDPOINTS` = Service не находит готовых Pod'ов (не совпал селектор или не проходит readinessProbe). nginx в этом случае отдаёт ровно `503 Service Temporarily Unavailable`. Смотреть надо на `readyz` Pod'а и на `spec.selector` Service.

**6. Логи и сгенерированный конфиг контроллера**

```bash
kubectl logs -n ingress-nginx deploy/ingress-nginx-controller --tail=30
kubectl exec -n ingress-nginx deploy/ingress-nginx-controller -- cat /etc/nginx/nginx.conf | less
```
```text
10.10.10.30 - - [12.02.2026:10:14:22 +0000] "GET /api/ HTTP/1.1" 503 199 "-" "curl/8.5.0" 92 0.000 [demo-api-vektor-80] [] 10.244.1.15:80, 10.244.2.9:80 : 0, 0 : 0 0.000, 0.000 503
```
Формат: код ответа, апстрим (`[namespace-service-port]`), адреса бэкендов, время. Если в квадратных скобках пусто — бэкендов нет. `nginx.conf` показывает, во что реально превратились ваши `Ingress`-объекты (это лучший способ понять merge правил по одному хосту).

**7. Rewrite пути — самая частая «магия»**

```yaml
metadata:
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /$2
    nginx.ingress.kubernetes.io/use-regex: "true"
spec:
  rules:
  - host: vector.corp.vektor.lab
    http:
      paths:
      - path: /api(/|$)(.*)
        pathType: ImplementationSpecific
```
`/$2` — подстановка второй группы регулярки, `use-regex: "true"` разрешает регулярку в `path`. Без этого `/api/v1/items` уедет на бэкенд как есть, и приложение ответит 404, потому что ждёт `/v1/items`. Аннотации действуют на **весь** объект Ingress — поэтому правила с разными rewrite делают разными объектами.

**8. Состояние сертификатов**

```bash
kubectl get certificate,certificaterequest,order,challenge -A
kubectl describe certificate web-vektor-tls -n demo
```
```text
NAMESPACE   NAME            READY   SECRET          AGE
demo        web-vektor-tls  True    web-vektor-tls  3m
```
`READY: True` = Secret создан и действителен. `False` — читайте `Events`/`Status` в `describe`: там будет причина (`Order` в состоянии `pending`, `Challenge` не прошёл). `kubectl get challenge -A` — самый быстрый способ увидеть, что ACME-проверка не проходит.

**9. Живой сертификат: что реально отдаёт сервер**

```bash
openssl s_client -connect 10.10.10.200:443 -servername vector.corp.vektor.lab </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```
```text
subject=CN = vector.corp.vektor.lab
issuer=CN = vektor-lab-ca
notBefore=Feb 12 10:11:00 2026 GMT
notAfter=May 13 10:11:00 2026 GMT
X509v3 Subject Alternative Name: DNS:vector.corp.vektor.lab
```
Проверяем issuer (кто подписал — наш внутренний CA или Let's Encrypt), срок и SAN (браузеры в 2026 году смотрят именно на SAN, а не на CN).

**10. Gateway API: объекты и их статус**

```bash
kubectl get gatewayclass,gateway,httproute -A
kubectl describe httproute web-vektor -n demo
```
```text
NAME                                     CLASS   ADDRESS        PROGRAMMED   AGE
gateway.gateway.networking.k8s.io/vektor-gw  eg  10.10.10.201   True         4m
```
В `describe httproute` ищите `Status → Parents → Conditions`: `Accepted=True` (Gateway принял маршрут) и `ResolvedRefs=True` (все backendRefs существуют). `Accepted=False, Reason=NotAllowedByListeners` — маршрут в namespace, который Gateway не пускает; `ResolvedRefs=False` — опечатка в имени Service.

**11. Быстрый «где сломалось» по событиям**

```bash
kubectl get events -A --sort-by=.lastTimestamp | tail -20
kubectl get events -n demo --field-selector involvedObject.kind=Certificate
```
События — единственное место, где cert-manager и контроллеры жалуются внятным текстом («failed to verify certificate», «secret not found»).

**12. Сравнение с Traefik (если ставите его вместо nginx)**

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: web-vektor
  namespace: demo
spec:
  entryPoints: [websecure]
  routes:
  - match: Host(`vector.corp.vektor.lab`) && PathPrefix(`/`)
    kind: Rule
    services:
    - name: web-vektor
      port: 80
  tls:
    secretName: web-vektor-tls
```
`match` — синтаксис правил Traefik, `entryPoints` — точки входа (аналог listener'ов Gateway). Traefik умеет выпускать ACME-сертификаты сам, без cert-manager, но тогда логика TLS живёт в конфиге Traefik, а не в объектах кластера.

**13. Диагностический минимум на будущее**

```bash
kubectl -n ingress-nginx get pods -o wide          # живы ли контроллеры
kubectl get svc,endpoints -n demo                  # есть ли бэкенды
kubectl describe ingress -n demo                   # правила и события
kubectl logs -n demo deploy/web-vektor --tail=20   # что говорит само приложение
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34 поднят: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), `kubectl` с рабочего места настроен на его kubeconfig.
- Навыки уроков 35 и 36: умеете создать Deployment/Service и ConfigMap.
- Образы тянем через локальное зеркало или Harbor (`HARBOR01`, 10.10.10.55) из уроков 31–32. Если зеркало не настроено — возьмите тег `nginx:*`, который гарантированно скачивается в вашем контуре, и замените его во всех манифестах.
- `UBU01` (10.10.10.30) — с него будем проверять curl'ом; на хосте с VMware Workstation для удобства добавьте строки в `C:\Windows\System32\drivers\etc\hosts` (правка от имени администратора):
  ```text
  10.10.10.200  vector.corp.vektor.lab
  10.10.10.201  gw.corp.vektor.lab
  ```
- Если MetalLB уже поставлен в уроке 37 — шаг 1 пропускаем, только сверяем пул адресов.

### Снапшот

В VMware Workstation: `VM → Snapshot → Take Snapshot`, имя `before-devops-038`. Через CLI (VMware Workstation должен быть установлен; путь к `vmrun.exe` — `C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe`, пути к `.vmx` подставьте свои):

```bash
vmrun -T ws snapshot "C:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-038
vmrun -T ws snapshot "C:\VMs\k8s-w01\k8s-w01.vmx"   before-devops-038
vmrun -T ws snapshot "C:\VMs\k8s-w02\k8s-w02.vmx"   before-devops-038
```

### Шаги

1. **MetalLB: выдаём «внешние» IP внутри VMnet8.** На `k8s-cp01`:
   ```bash
   kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml
   ```
   `[ПРОВЕРИТЬ актуальную версию]`. Ждём `Running` в `metallb-system`, затем создаём пул (адреса .200–.210 свободны: DHCP на стенде раздаёт .100–.150):
   ```bash
   kubectl apply -f - <<'EOF'
   apiVersion: metallb.io/v1beta1
   kind: IPAddressPool
   metadata:
     name: vektor-pool
     namespace: metallb-system
   spec:
     addresses:
     - 10.10.10.200-10.10.10.210
   ---
   apiVersion: metallb.io/v1beta1
   kind: L2Advertisement
   metadata:
     name: vektor-l2
     namespace: metallb-system
   spec:
     ipAddressPools:
     - vektor-pool
   EOF
   ```
   *Ожидаемо:* `kubectl get ipaddresspool -n metallb-system` — пул создан. Режим L2: один узел анонсирует адрес по ARP, настоящего ECMP-балансирования нет — для лабы нормально, для продакшена обсуждаемо.

2. **Ставим ingress-nginx (провайдер-агностик манифест, он даёт Service типа LoadBalancer).**
   ```bash
   kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.0/deploy/static/provider/cloud/deploy.yaml
   ```
   `[ПРОВЕРИТЬ тег версии]`. *Ожидаемо:* `kubectl get svc -n ingress-nginx ingress-nginx-controller` показывает `EXTERNAL-IP 10.10.10.200`, оба пода контроллера `Running`, есть `IngressClass nginx`.

3. **Приложение-заглушка в namespace `demo`: два ConfigMap, два Deployment, два Service.**
   ```bash
   kubectl create namespace demo
   kubectl apply -f - <<'EOF'
   apiVersion: v1
   kind: ConfigMap
   metadata: {name: web-index, namespace: demo}
   data:
     index.html: "<h1>web-vektor: страница из кластера</h1>\n"
   ---
   apiVersion: v1
   kind: ConfigMap
   metadata: {name: api-index, namespace: demo}
   data:
     index.html: "{\"service\":\"api-vektor\",\"status\":\"ok\"}\n"
   ---
   apiVersion: apps/v1
   kind: Deployment
   metadata: {name: web-vektor, namespace: demo}
   spec:
     replicas: 2
     selector: {matchLabels: {app: web-vektor}}
     template:
       metadata: {labels: {app: web-vektor}}
       spec:
         containers:
         - name: nginx
           image: nginx:1.27-alpine
           ports: [{containerPort: 80}]
           readinessProbe:
             httpGet: {path: /, port: 80}
           volumeMounts: [{name: www, mountPath: /usr/share/nginx/html}]
         volumes:
         - name: www
           configMap: {name: web-index}
   ---
   apiVersion: apps/v1
   kind: Deployment
   metadata: {name: api-vektor, namespace: demo}
   spec:
     replicas: 1
     selector: {matchLabels: {app: api-vektor}}
     template:
       metadata: {labels: {app: api-vektor}}
       spec:
         containers:
         - name: nginx
           image: nginx:1.27-alpine
           ports: [{containerPort: 80}]
           volumeMounts: [{name: www, mountPath: /usr/share/nginx/html}]
         volumes:
         - name: www
           configMap: {name: api-index}
   ---
   apiVersion: v1
   kind: Service
   metadata: {name: web-vektor, namespace: demo}
   spec:
     selector: {app: web-vektor}
     ports: [{port: 80, targetPort: 80}]
   ---
   apiVersion: v1
   kind: Service
   metadata: {name: api-vektor, namespace: demo}
   spec:
     selector: {app: api-vektor}
     ports: [{port: 80, targetPort: 80}]
   EOF
   ```
   *Ожидаемо:* `kubectl get pods -n demo` — три пода `Running`; `kubectl get endpointslices -n demo` — по одному адресу у `web-vektor` больше.

4. **Правило Ingress для сайта (без TLS пока).**
   ```bash
   kubectl apply -f - <<'EOF'
   apiVersion: networking.k8s.io/v1
   kind: Ingress
   metadata:
     name: web-vektor
     namespace: demo
   spec:
     ingressClassName: nginx
     rules:
     - host: vector.corp.vektor.lab
       http:
         paths:
         - path: /
           pathType: Prefix
           backend:
             service: {name: web-vektor, port: {number: 80}}
   EOF
   ```
   *Ожидаемо:* `kubectl get ingress -n demo` — `ADDRESS 10.10.10.200`, `PORTS 80`.

5. **Второй Ingress для `/api` с переписыванием пути.** Отдельный объект именно потому, что аннотации действуют на весь Ingress:
   ```bash
   kubectl apply -f - <<'EOF'
   apiVersion: networking.k8s.io/v1
   kind: Ingress
   metadata:
     name: api-vektor
     namespace: demo
     annotations:
       nginx.ingress.kubernetes.io/use-regex: "true"
       nginx.ingress.kubernetes.io/rewrite-target: /$2
   spec:
     ingressClassName: nginx
     rules:
     - host: vector.corp.vektor.lab
       http:
         paths:
         - path: /api(/|$)(.*)
           pathType: ImplementationSpecific
           backend:
             service: {name: api-vektor, port: {number: 80}}
   EOF
   ```

6. **Проверяем HTTP с UBU01.**
   ```bash
   curl -s --resolve vector.corp.vektor.lab:80:10.10.10.200 http://vector.corp.vektor.lab/
   curl -s --resolve vector.corp.vektor.lab:80:10.10.10.200 http://vector.corp.vektor.lab/api/
   ```
   *Ожидаемо:* первый запрос — `<h1>web-vektor...`, второй — JSON `{"service":"api-vektor"...}`. Если второй даёт 404 — вернитесь к шагу 5 и проверьте регулярку.

7. **Ставим cert-manager и ждём, пока поднимется webhook.**
   ```bash
   kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.17.0/cert-manager.yaml
   kubectl -n cert-manager wait --for=condition=Available deployment --all --timeout=180s
   ```
   `[ПРОВЕРИТЬ версию]`. *Ожидаемо:* три пода в `cert-manager` — `Running`, Deployment `Available`. Если `kubectl get certificate` ругается на отсутствующий API — webhook ещё не поднялся, подождите минуту.

8. **Цепочка доверия: self-signed → внутренний CA → ClusterIssuer.**
   ```bash
   kubectl apply -f - <<'EOF'
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata: {name: vektor-selfsigned}
   spec: {selfSigned: {}}
   ---
   apiVersion: cert-manager.io/v1
   kind: Certificate
   metadata: {name: vektor-ca, namespace: cert-manager}
   spec:
     isCA: true
     commonName: vektor-lab-ca
     secretName: vektor-ca-key-pair
     privateKey: {algorithm: ECDSA, size: 256}
     issuerRef: {name: vektor-selfsigned, kind: ClusterIssuer, group: cert-manager.io}
   ---
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata: {name: vektor-ca}
   spec:
     ca: {secretName: vektor-ca-key-pair}
   EOF
   ```
   *Ожидаемо:* `kubectl get certificate -n cert-manager` — `vektor-ca` со `READY True`.

9. **Включаем TLS на Ingress — cert-manager создаёт Certificate по аннотации.**
   ```bash
   kubectl annotate ingress web-vektor -n demo cert-manager.io/cluster-issuer=vektor-ca
   kubectl patch ingress web-vektor -n demo --type=merge -p '{"spec":{"tls":[{"hosts":["vector.corp.vektor.lab"],"secretName":"web-vektor-tls"}]}}'
   ```
   *Ожидаемо:* через 10–40 секунд `kubectl get certificate -n demo` → `web-vektor-tls READY True`, `kubectl get secret web-vektor-tls -n demo` — тип `kubernetes.io/tls`. Проверяем: `curl -k --resolve vector.corp.vektor.lab:443:10.10.10.200 https://vector.corp.vektor.lab/`.

10. **Проверяем сертификат глазами (как в разделе 4, команда 9)** — issuer должен быть `CN = vektor-lab-ca`, SAN — `vector.corp.vektor.lab`. `curl` без `-k` будет ругаться на неизвестный CA — это нормально для внутреннего CA; в рабочей среде корневой сертификат распространяют в доверенные хранилища через GPO (в вашем стенде это `DC01`).

11. **То же самое, но на Gateway API через Envoy Gateway.**
    ```bash
    kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
    kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.2.4/install.yaml
    ```
    `[ПРОВЕРИТЬ версии]`, `--server-side` нужен из-за размера CRD. Дальше:
    ```bash
    kubectl apply -f - <<'EOF'
    apiVersion: gateway.networking.k8s.io/v1
    kind: Gateway
    metadata: {name: vektor-gw, namespace: demo}
    spec:
      gatewayClassName: eg
      listeners:
      - name: http
        protocol: HTTP
        port: 80
        allowedRoutes: {namespaces: {from: Same}}
    ---
    apiVersion: gateway.networking.k8s.io/v1
    kind: HTTPRoute
    metadata: {name: web-vektor, namespace: demo}
    spec:
      parentRefs: [{name: vektor-gw}]
      hostnames: ["gw.corp.vektor.lab"]
      rules:
      - matches: [{path: {type: PathPrefix, value: /}}]
        backendRefs: [{name: web-vektor, port: 80}]
    EOF
    ```
    *Ожидаемо:* `kubectl get gateway -n demo` → `PROGRAMMED True`, `ADDRESS 10.10.10.201`; `curl -s --resolve gw.corp.vektor.lab:80:10.10.10.201 http://gw.corp.vektor.lab/` отдаёт нашу страницу. Обратите внимание: `GatewayClass eg` уже создан установщиком Envoy Gateway — руками его писать не нужно.

12. **Записываем в `stand-log.md`** что появилось: MetalLB с пулом, ingress-nginx на .200, Envoy Gateway на .201, namespace `demo`, два Ingress, ClusterIssuer `vektor-ca`, два hosts-имени.

### Проверка

- [ ] `kubectl get svc -n ingress-nginx ingress-nginx-controller` — `EXTERNAL-IP 10.10.10.200`, не `<pending>`
- [ ] `kubectl get ingressclass` содержит `nginx` и `eg`
- [ ] `http://vector.corp.vektor.lab/` с UBU01 отдаёт HTML из ConfigMap
- [ ] `http://vector.corp.vektor.lab/api/` отдаёт JSON, а не 404 (rewrite работает)
- [ ] `https://vector.corp.vektor.lab/` отвечает, `openssl s_client` показывает issuer `vektor-lab-ca`
- [ ] `kubectl get certificate -A` — все `READY True`
- [ ] `kubectl get httproute -n demo` — `Accepted=True`, `ResolvedRefs=True`
- [ ] `curl` на `gw.corp.vektor.lab` через 10.10.10.201 отдаёт ту же страницу
- [ ] В `stand-log.md` появилась запись про урок 38

### Сломай и почини

> **Пользователь:** ivanov / **ВМ:** WIN11-CL01
> **Что написал:** «Открываю `https://vector.corp.vektor.lab` — главная работает, а раздел `/api/` отдаёт "503 Service Temporarily Unavailable". Вчера всё было нормально. У нас отчёт, очень нужно».
> **Срочность:** высокая

**Как воспроизвести поломку** (имитируем «кто-то поправил порт в Service, а селектор не проверил»):

```bash
kubectl -n demo patch svc api-vektor --type=merge -p '{"spec":{"ports":[{"port":80,"targetPort":8080}]}}'
```

**Диагностика по шагам — от внешнего к внутреннему:**

1. `curl -k --resolve vector.corp.vektor.lab:443:10.10.10.200 https://vector.corp.vektor.lab/api/` — убеждаемся, что `503`, а не 404. **503 = маршрут найден, бэкенда нет. 404 = маршрута нет.** Это сразу отсекает половину гипотез.
2. `kubectl logs -n ingress-nginx deploy/ingress-nginx-controller --tail=20` — ищем строку с `[demo-api-vektor-80]`; если адресов апстрима нет — проблема на уровне Service/Endpoints.
3. `kubectl get endpointslices -n demo -l kubernetes.io/service-name=api-vektor` — видим `ENDPOINTS: <unset>` (порт сервиса не совпадает с портом контейнера, EndpointSlice пустой).
4. `kubectl describe svc api-vektor -n demo` — `TargetPort: 8080`, `Endpoints: <none>`.
5. `kubectl get pods -n demo -l app=api-vektor -o wide` — под `Running` и `Ready`, значит приложение живо, дело в порте.
6. `kubectl exec -n demo deploy/api-vektor -- wget -qO- http://127.0.0.1:80/` — nginx внутри слушает 80. Подтверждаем: 8080 никто не слушает.

**Решение:**

```bash
kubectl -n demo patch svc api-vektor --type=merge -p '{"spec":{"ports":[{"port":80,"targetPort":80}]}}'
kubectl get endpointslices -n demo -l kubernetes.io/service-name=api-vektor   # появились адреса
curl -k -s --resolve vector.corp.vektor.lab:443:10.10.10.200 https://vector.corp.vektor.lab/api/
```

**Ответ пользователю:** «Раздел `/api/` недоступен был из-за ошибки в конфигурации внутреннего балансировщика — трафик направлялся на неверный порт сервиса. Исправил, проверил: страница и `/api/` открываются. Пожалуйста, обновите страницу с Ctrl+F5; если браузер всё ещё показывает ошибку — пришлите скриншот».

**Закрытие заявки:** категория «веб-сервис / конфигурация», в описании — что именно менялось (порт в Service `api-vektor` в namespace `demo`) и как проверялось. Отдельным пунктом — предложение завести алерт на «Endpoints < 1 у продакшн-сервиса», иначе следующий такой случай поймает пользователь, а не мониторинг.

### Что изменилось на стенде

- **k8s-cp01 / k8s-w01 / k8s-w02:** добавлен MetalLB (`metallb-system`), пул `10.10.10.200–10.10.10.210` (L2).
- **k8s-cp01:** установлен ingress-nginx, Service `ingress-nginx-controller` получил `EXTERNAL-IP 10.10.10.200`.
- **k8s-cp01:** установлен cert-manager (`cert-manager` namespace), созданы `ClusterIssuer vektor-selfsigned`, `ClusterIssuer vektor-ca`, CA-сертификат `vektor-ca` (Secret `vektor-ca-key-pair`).
- **k8s-cp01:** установлены CRD Gateway API и Envoy Gateway (`envoy-gateway-system`), `GatewayClass eg`, `Gateway vektor-gw` в namespace `demo` с адресом `10.10.10.201`.
- **Кластер:** namespace `demo` — Deployment `web-vektor` (2 реплики), `api-vektor` (1), Service'ы с теми же именами, Ingress `web-vektor` и `api-vektor`, `HTTPRoute web-vektor`, Secret `web-vektor-tls`.
- **Хост читателя:** записи в `hosts` — `vector.corp.vektor.lab → 10.10.10.200`, `gw.corp.vektor.lab → 10.10.10.201`.
- **Внешние адреса:** `.200` — nginx ingress, `.201` — Envoy Gateway. Заняты, не использовать в других лабораторных.

### Откат

1. Если кластер «поехал»: в VMware Workstation выбрать ВМ → `VM → Snapshot → Snapshot Manager` → `before-devops-038` → `Go To`. Через CLI:
   ```bash
   vmrun -T ws revertToSnapshot "C:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-038
   vmrun -T ws revertToSnapshot "C:\VMs\k8s-w01\k8s-w01.vmx"   before-devops-038
   vmrun -T ws revertToSnapshot "C:\VMs\k8s-w02\k8s-w02.vmx"   before-devops-038
   ```
2. Мягкий откат без снапшота (если надо сохранить остальное) — удаляем в обратном порядке:
   ```bash
   kubectl delete namespace demo
   kubectl delete -f https://github.com/envoyproxy/gateway/releases/download/v1.2.4/install.yaml
   kubectl delete -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.0/deploy/static/provider/cloud/deploy.yaml
   kubectl delete -f https://github.com/cert-manager/cert-manager/releases/download/v1.17.0/cert-manager.yaml
   ```
3. И не забудьте убрать строки из `hosts` и записать откат в `stand-log.md`.

## 6. Ошибки новичков и безопасность

- **«Создал Ingress — а сайт не открывается» → контроллера нет.** Объект создаётся молча, `ADDRESS` пустой, ошибок в событиях нет. → Всегда начинайте с `kubectl get ingressclass` и `kubectl get pods -A | grep ingress`.
- **Ждать `EXTERNAL-IP` в лаборатории без MetalLB.** На голом kubeadm Service типа LoadBalancer навсегда останется `<pending>`. → MetalLB, либо NodePort, либо `kubectl port-forward` для теста.
- **Селектор Service не совпадает с labels Pod'а.** Ingress отдаёт 503, а `kubectl get pods` показывает красивые `Running`. → Смотреть `endpointslices`, а не только поды.
- **Один Ingress на все правила с разными `rewrite-target`.** Аннотация применяется ко всему объекту, пути начинают уезжать не туда. → Разные rewrite = разные Ingress-объекты (или Gateway API, где фильтры задаются на конкретное правило).
- **`pathType: ImplementationSpecific` по привычке.** Поведение зависит от контроллера, при миграции между nginx/Traefik правила ломаются. → Явно `Prefix` или `Exact`, кроме случаев регулярки.
- **Путать `Issuer` и `ClusterIssuer`.** `Issuer` живёт только в своём namespace, `Certificate` из другого namespace его не увидит (`ResolvedRefs=False`). → Для общего CA — `ClusterIssuer`.
- **Гонять ACME по production-эндпоинту Let's Encrypt.** Упираетесь в rate limit и получаете блокировку на сутки. → Сначала staging-эндпоинт, потом переключение.
- **Считать, что TLS «как-то сам».** Secret с сертификатом не создаётся магически; без `spec.tls` и без cert-manager nginx отдаст свой дефолтный fake-сертификат «Kubernetes Ingress Controller Fake Certificate» — пользователь увидит именно его.

### Безопасность

- **Не храните приватные ключи TLS в Git.** Только Secret в кластере, созданный cert-manager; в Git — манифест `Certificate`/`Issuer`, а не содержимое ключа.
- **Не включайте `nginx.ingress.kubernetes.io/configuration-snippet` и `server-snippet` «по-быстрому».** Это вставка произвольного текста в `nginx.conf` — фактически RCE-подобная возможность для того, у кого есть право писать Ingress; в новых версиях ingress-nginx сниппеты по умолчанию ограничены `[ПРОВЕРИТЬ актуальные флаги]`. Тот же класс уязвимостей — нашумевший CVE-2025-1974 (IngressNightmare) в admission-webhook ingress-nginx `[ПРОВЕРИТЬ]`; вывод — держите контроллер в актуальной версии и не выставляйте наружу его метрики/вебхук.
- **Не отключайте `ssl-redirect` и не ставьте самоподписанные сертификаты в продакшене.** На стенде self-signed CA — учебная необходимость; в бою это либо публичный CA, либо корпоративный, чей корень распространён через GPO/ MDM.
- **Ограничивайте размер тела запроса** (`proxy-body-size`) и включайте rate limit: дефолт «без лимита» + публичный Ingress = удобная цель для исчерпания ресурсов.
- **Ingress — это точка входа, а не файрвол.** Один публичный вход на весь кластер означает, что забытый внутренний сервис станет публичным, как только кто-то допишет правило. NetworkPolicy (урок 37) и ревью Ingress-манифестов остаются обязательными.

## 7. Вопросы на собеседовании

**1. Чем Ingress отличается от Ingress-контроллера? Что произойдёт, если создать Ingress в кластере без контроллера?**

Объект `Ingress` — декларация правил L7-маршрутизации, он ничего не исполняет. Исполняет контроллер: под/деплой, который watch'ит `Ingress`, `Service`, `EndpointSlice`, `Secret`, генерирует конфиг (для nginx — `nginx.conf`) и проксирует трафик. Без контроллера объект создастся успешно, `kubectl get ingress` покажет пустой `ADDRESS`, ошибок в событиях не будет — просто никто не обслуживает правила. `ingressClassName` указывает, какой именно контроллер должен взять объект, если их несколько.

*Что хочет услышать:* чёткое разделение «API-объект / data plane», понимание роли `IngressClass` и того, что молчание кластера — не признак успеха.
*Красный флаг:* «Ingress — это встроенный балансировщик Kubernetes» или «трафик обрабатывает kube-proxy по правилам Ingress».

**2. Зачем нужен Gateway API, если есть Ingress?**

Gateway API решает три боли Ingress. Роли: инфраструктурная команда владеет `GatewayClass` и `Gateway` (адреса, порты, TLS, какие namespace разрешены), команда приложения — только своим `HTTPRoute`. Переносимость: расширения задаются типизированными полями (`filters`, `backendRefs` с весами, матчинг по заголовкам и методам), а не vendor-специфичными аннотациями. Расширяемость: `Policy Attachment` вместо строк. Плюс мультипротокольность — `GRPCRoute`, `TLSRoute`, `TCPRoute` — и нативное деление по namespace через `allowedRoutes`.

*Что хочет услышать:* что это не «Ingress 2.0 с другим именем», а другая модель ответственности; что нужны отдельно установленные CRD и контроллер, поддерживающий Gateway API.
*Красный флаг:* «Ingress устарел, мы всё мигрировали за вечер» — без понимания, что ingress-nginx в 2026 всё ещё стоит у большинства.

**3. Как cert-manager получает сертификат Let's Encrypt? Чем HTTP-01 отличается от DNS-01 и что выбрать для внутреннего контура?**

cert-manager работает по протоколу ACME: создаёт `Certificate` → `CertificateRequest` → `Order` → `Challenge`, проходит проверку владения доменом, получает сертификат и кладёт его в указанный Secret, потом автоматически продлевает (обычно за 30 дней до истечения). HTTP-01: CA запрашивает `http://домен/.well-known/acme-challenge/<token>`, нужен доступ из интернета на 80 порт, wildcard-сертификаты не поддерживаются. DNS-01: создаётся TXT-запись `_acme-challenge.<домен>`, работает для wildcard и для случаев, когда 80 порт закрыт, но требует API-доступа к DNS-провайдеру через webhook. Для внутреннего контура, где внешний ACME в принципе недоступен, используют внутренний CA: self-signed → `Certificate` с `isCA: true` → `ClusterIssuer` типа `ca`, и уже им подписывают сервисные сертификаты — ровно то, что мы делали в лабораторной.

*Что хочет услышать:* знание цепочки объектов, разницы типов challenge и того, что продление автоматическое, а не «по календарю в голове».
*Красный флаг:* «ставлю certbot на ноду и по cron продлеваю».

**4. Пользователь получает 503 от Ingress. Ваши действия?**

503 от nginx ingress означает: маршрут найден, но бэкенда нет или он не Ready. Порядок: `curl` с `--resolve` (подтвердить код), логи контроллера (видно апстрим-группу), `kubectl get endpointslices` по нужному Service (пусто → проблема в Service), `kubectl describe svc` (селектор и targetPort), `kubectl get pods -o wide --show-labels` (Ready? метки совпадают?), `kubectl describe pod` (readinessProbe падает?). Различаю коды: 404 — нет правила (host/path), 502 — бэкенд ответил не по HTTP, 503 — нет эндпоинтов, 504 — таймаут до бэкенда.

*Что хочет услышать:* движение по цепочке «Ingress → Service → Endpoints → Pod» и интерпретацию кодов, а не «перезапущу контроллер».
*Красный флаг:* «удалю Ingress и создам заново» как первый шаг.

**5. Как выбрать между ingress-nginx, Traefik и Envoy Gateway?**

Критерии, а не «популярность». ingress-nginx — огромная база знаний, привычный nginx, но проект сворачивается `[ПРОВЕРИТЬ сроки]`, расширения через аннотации, и надо смотреть на альтернативы/поддержку. Traefik — быстрый старт, встроенный ACME, CRD `IngressRoute`/`Middleware`, дефолт в k3s, хорошо для небольших и средних кластеров. Envoy Gateway — нативный Gateway API на Envoy, фильтры (ext_authz, rate limit, JWT), перспективен как платформенный слой, но требует понимания Gateway API и Envoy. Дальше — требования: нужен ли Gateway API, mTLS/WAF, какая производительность, кто владелец (платформа или приложение), есть ли вендорская поддержка и доступность обновлений в РФ-контуре.

*Что хочет услышать:* аргументацию от требований и эксплуатации (кто будет обновлять, кто дежурит), упоминание заката ingress-nginx как фактор риска.
*Красный флаг:* «берём тот, у кого больше звёзд на GitHub».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Concepts → Services, Load Balancing, and Networking*: статьи «Ingress», «Ingress Controllers», «Gateway API». Поиск: `kubernetes ingress concept`, `kubernetes gateway api`.
- **gateway-api.sigs.k8s.io** — официальная документация Gateway API: *Introduction*, *API concepts*, *Guides* (миграция с Ingress). Поиск: `gateway api concepts roles`.
- **kubernetes.github.io/ingress-nginx** — документация ingress-nginx: *User guide* (аннотации, rewrite, TLS), *Deployment* (манифесты baremetal/cloud). Поиск: `ingress-nginx rewrite target annotation`.
- **doc.traefik.io** — документация Traefik v3: *Routing configuration* (`IngressRoute`, `Middleware`), *HTTPS / ACME*. Поиск: `traefik kubernetes ingressroute crd`.
- **gateway.envoyproxy.io** — документация Envoy Gateway: *Installation*, *Quickstart*, *Tasks* (filters, `EnvoyProxy`). Поиск: `envoy gateway install quickstart`.
- **cert-manager.io** — документация cert-manager: *Issuers* (SelfSigned, CA, ACME), *Usage* (Ingress annotations), *Troubleshooting*. Поиск: `cert-manager selfsigned ca clusterissuer`, `cert-manager http01 vs dns01`.
- **metallb.universe.tf** — документация MetalLB: *Installation*, *Configuration* (`IPAddressPool`, `L2Advertisement`). Поиск: `metallb layer2 configuration`.
- **kubernetes.io/blog** — блог проекта Kubernetes, официальные анонсы (в т.ч. по судьбе ingress-nginx и развитию Gateway API). Поиск: `kubernetes blog ingress-nginx retirement` `[ПРОВЕРИТЬ формулировку и даты]`.
