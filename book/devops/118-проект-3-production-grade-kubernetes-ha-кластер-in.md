# Проект №3: Production-grade Kubernetes — HA-кластер, Ingress, cert-manager, мониторинг, логи, бэкапы, Network Policies

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №1.** Пользователь: `petrova` (Мария Петрова, HR) / ВМ: WIN10-CL02 / Что написала: «Портал заявок на отпуск открывается через раз, а сегодня утром браузер писал "Ваше подключение не защищено". И когда он лежит — вы час молчите, мы не понимаем, работает он или нет» / Срочность: высокая, до конца дня.

**Заявка №2, в тот же день.** Подрядчик по ИБ: «В вашем кластере любой под может ходить к любому другому. Это не production. До устранения портал на согласование не принимаем».

Знакомо? Первая заявка — это про доступность, TLS и наблюдаемость. Вторая — про сетевую изоляцию. Всё вместе — это и есть разница между «я поднял kubectl-кластер на выходных» и «я сделал production-grade». В работе DevOps-инженера такие заявки приходят постоянно, потому что кластер без Ingress, сертификатов, мониторинга, логов, бэкапов и сетевых политик — это демо-стенд, а не платформа.

После этой главы ты будешь уметь: понимать, из чего состоит HA control plane и почему etcd требует нечётного числа узлов; выставлять приложение наружу через Ingress с автоматическим TLS от cert-manager; ставить метрики, логи и алерты; делать и, главное, **проверять** бэкапы кластера; закрывать namespace через NetworkPolicy и доказывать, что изоляция работает. Мониторинг, логи и бэкапы здесь — базовый уровень: глубокая наблюдаемость будет в уроке 119, DevSecOps и Vault — в 120, полное восстановление — в 122.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**HA control plane.** Control plane — это kube-apiserver, kube-scheduler, kube-controller-manager и etcd. etcd — распределённое key-value хранилище, которое является источником правды о состоянии кластера. Он использует алгоритм Raft: чтобы запись подтвердилась, нужно согласие большинства (кворум). Для 3 узлов кворум = 2, для 2 узлов кворум = 2 (то есть отказ одного = остановка записи). Поэтому etcd ставят **нечётным** числом: 3 или 5. Отсюда: production-HA control plane — минимум 3 узла со `stacked etcd` (etcd на тех же машинах) или внешний кластер etcd.

Чтобы клиенты не переключались руками, apiserver публикуется через один стабильный адрес — VIP (kube-vip, keepalived) или внешний балансировщик. В `kubeadm` это `--control-plane-endpoint`; остальные control plane узлы присоединяются через `kubeadm join --control-plane`.

На нашем стенде control plane один — `k8s-cp01`. Полное HA из трёх CP ты попробуешь отдельно (см. лабу, шаг 1 — там команды `kubeadm`, чтобы понимать механику), а на стенде мы делаем отказоустойчивость **уровня приложений**: несколько реплик, `topologySpreadConstraints`, `PodDisruptionBudget`, пробы.

**Порты, которые надо помнить:** kube-apiserver — 6443; etcd — 2379 (клиентский) и 2380 (peer); kubelet — 10250; kube-scheduler — 10259; kube-controller-manager — 10257; HTTP/HTTPS — 80/443; диапазон NodePort — 30000–32767.

**Ingress.** Ingress-объект — это декларация маршрутизации («host `app.corp.vektor.lab`, path `/` → Service `web:80`»). Сам он ничего не делает: трафик обслуживает **Ingress controller** — отдельный под/деплоймент, который читает Ingress-объекты через API и настраивает свой nginx. Ingress controller публикуется через Service (NodePort на bare-metal) или через LoadBalancer, если под ним есть балансировщик.

**cert-manager.** Контроллер, который читает объекты `Certificate` и выпускает TLS-сертификаты, кладя их в Secret (классически `kubernetes.io/tls`). Работает через `Issuer`/`ClusterIssuer` (кластерный — на весь кластер). Типы: `SelfSigned`, `CA` (подписывает твоим внутренним CA), `ACME` (Let's Encrypt; HTTP-01 требует публичный домен, DNS-01 — для wildcard и закрытых зон). Для внутреннего домена `corp.vektor.lab` Let's Encrypt не подойдёт в принципе — валидатор не достучится. Поэтому в корпоративной среде делают цепочку: `SelfSigned` → корневой CA-сертификат → `ClusterIssuer` типа `CA` → сертификаты приложений. Автопродление делает сам cert-manager по `renewBefore`; **ключ CA при этом хранится в Secret и его потеря = перевыпуск всего парка**.

**Наблюдаемость и логи.** Метрики: `node-exporter` (метрики ОС), `kube-state-metrics` (состояние объектов Kubernetes), `metrics-server` (для `kubectl top`), Prometheus + Grafana. В Kubernetes Prometheus находит цели через `ServiceMonitor`/`PodMonitor` — CRD, которые описывают, какие Service скрейпить. Логи: агент (Promtail или Grafana Alloy) в DaemonSet собирает stdout контейнеров и шлёт в Loki, запросы — на LogQL.

**Бэкапы.** Два слоя: (1) etcd snapshot — состояние всего кластера, но **не** содержимое PV; (2) Velero — бэкап Kubernetes-объектов и (через Kopia/restic) данных томов в объектное хранилище. Метрики: RPO (сколько данных допустимо потерять) и RTO (за сколько обязаны подняться).

**NetworkPolicy.** Это объект Kubernetes, который описывает разрешённые потоки между подами. Важно: **политика не работает, если CNI её не реализует**. Calico и Cilium — работают, классический Flannel — нет (политики создадутся, но трафик не ограничат — молчаливый fail-open). Базовая схема — «default deny all» + точечные allow.

```text
                      офис 10.10.10.0/24
                              |
                    DNS: app.corp.vektor.lab -> 10.10.10.61
                              |
                  +-----------+-----------+
                  |  ingress-nginx (ns ingress-nginx)
                  |  :NodePort -> internal :80/:443
                  |  TLS-сертификат <-- cert-manager (ClusterIssuer vektor-ca)
                  +-----------+-----------+
                              |
        +---------------------+---------------------+
        |                                           |
   ns prod                                   ns monitoring
   web (3 реплики, PDB)                      Prometheus + Grafana
        |  NetworkPolicy: default-deny              ^
        v                                           | scrape /metrics
   api (2 реплики) --> внешняя БД        node-exporter, kube-state-metrics

   control plane: k8s-cp01 (apiserver :6443, stacked etcd :2379/:2380)
   workers: k8s-w01, k8s-w02 (kubelet :10250)
   бэкапы: etcd snapshot -> /var/backups/etcd; Velero -> MinIO в ns velero
```

## 3. Аналогия из жизни

Production-grade кластер — это аэропорт. Control plane — диспетчерская вышка: если она одна и упала, самолёты не садятся, поэтому в больших аэропортах вышек несколько и они голосуют (это кворум etcd). Ingress-контроллер — стойка регистрации на входе: пассажир приходит по одному адресу, а дальше его направляют в нужный терминал. cert-manager — служба пропусков: выдаёт бейджи с ограниченным сроком и сама напоминает о продлении. Мониторинг — камеры и датчики в зале, логи — журнал регистрации («кто в 14:03 прошёл через третьи двери»). Бэкапы — копия расписания рейсов в сейфе: если сгорел диспетчерский зал, аэропорт восстановит работу по копии. NetworkPolicy — зоны контроля доступа: стерильная зона, транзитная зона, зона выдачи багажа, и просто так между ними не пройти.

Аналогия ломается в одном: в аэропорту охранник физически стоит на проходе и не пропустит нарушителя, а NetworkPolicy — это правило, которое исполняет сетевой плагин, и если плагин его не умеет, дверь просто останется открытой, при этом в `kubectl get networkpolicy` всё будет выглядеть красиво.

## 4. Практика и команды

**1. Базовая проверка кластера.**
```bash
kubectl get nodes -o wide
kubectl -n kube-system get pods
```
`-o wide` добавляет IP узла, версию kubelet и container runtime — сразу видно, что версии совпадают и адреса из 10.10.10.0/24.
```text
NAME       STATUS   ROLES           AGE   VERSION   INTERNAL-IP
k8s-cp01   Ready    control-plane   12d   v1.3x.x   10.10.10.61
k8s-w01    Ready    <none>          12d   v1.3x.x   10.10.10.62
k8s-w02    Ready    <none>          12d   v1.3x.x   10.10.10.63
```
Смотрим: `Ready` у всех, `ROLES` — control-plane только у cp01, нет `NotReady` и `SchedulingDisabled`.

**2. Проверить, что CNI умеет NetworkPolicy.**
```bash
kubectl -n kube-system get daemonsets
kubectl get crd | grep -i -E 'calico|cilium'
kubectl -n kube-system logs ds/calico-node --tail=20
```
Если видишь `calico-node`/`cilium` — политики будут работать. Если `kube-flannel` и CRD Calico/Cilium нет — меняй CNI, иначе весь шаг с изоляцией бессмысленен.

**3. Установка Ingress-контроллера через Helm.**
```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -n ingress-nginx --create-namespace \
  --set controller.service.type=NodePort
kubectl -n ingress-nginx get svc,pods
kubectl get ingressclass
```
`--set controller.service.type=NodePort` — потому что на bare-metal нет облачного балансировщика и Service типа LoadBalancer навсегда останется в `<pending>`. Смотрим фактические порты в `kubectl get svc ingress-nginx-controller` (колонка PORT(S), вида `80:30xxx/TCP`).

**4. Деплой приложения с ресурсами, пробами и PDB.**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: prod}
spec:
  replicas: 3
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
        - name: web
          image: harbor.corp.vektor.lab/vektor/web:1.0.0
          ports: [{containerPort: 8080}]
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits:   {cpu: 500m, memory: 512Mi}
          readinessProbe: {httpGet: {path: /healthz, port: 8080}, initialDelaySeconds: 5}
          livenessProbe:  {httpGet: {path: /livez,  port: 8080}, periodSeconds: 10}
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: kubernetes.io/hostname
          whenUnsatisfiable: ScheduleAnyway
          labelSelector: {matchLabels: {app: web}}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: web, namespace: prod}
spec:
  minAvailable: 2
  selector: {matchLabels: {app: web}}
```
```bash
kubectl apply -f deployment.yaml
kubectl -n prod get pods -o wide
kubectl -n prod get pdb
```
Смотрим: поды распределены по `k8s-w01`/`k8s-w02`, `READY 1/1`, а в PDB `ALLOWED DISRUPTIONS` = 1 (пока 3 пода живы). `readinessProbe` убирает под из балансировки, `livenessProbe` перезапускает зависший.

**5. Ingress и TLS-секрет.**
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  namespace: prod
  annotations:
    cert-manager.io/cluster-issuer: vektor-ca
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts: [app.corp.vektor.lab]
      secretName: web-tls
  rules:
    - host: app.corp.vektor.lab
      http:
        paths:
          - path: /
            pathType: Prefix
            backend: {service: {name: web, port: {number: 80}}}
```
```bash
kubectl apply -f ingress.yaml
kubectl -n prod describe ingress web
```
В `describe` смотри блок `Events` и `TLS` — должен быть указан `web-tls`. Если `ADDRESS` пустой — контроллер не подхватил Ingress (проверь `ingressClassName`).

**6. cert-manager: установка и выпуск сертификата.**
```bash
helm repo add jetstack https://charts.jetstack.io
helm upgrade --install cert-manager jetstack/cert-manager \
  -n cert-manager --create-namespace --set crds.enabled=true
```
```yaml
# 1) самоподписанный корень
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
  commonName: vektor-internal-ca
  secretName: vektor-ca-key-pair
  duration: 8760h
  renewBefore: 720h
  privateKey: {algorithm: ECDSA, size: 256}
  issuerRef: {name: vektor-selfsigned, kind: ClusterIssuer}
---
# 2) CA-issuer на основе этого корня
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: {name: vektor-ca}
spec:
  ca:
    secretName: vektor-ca-key-pair
```
```bash
kubectl get clusterissuer
kubectl -n prod get certificate,certificaterequest,order,challenge
kubectl -n prod describe certificate web-tls
```
Смотрим `Ready: True` и `Message: Certificate is up to date and has not expired`. Секрет `vektor-ca-key-pair` для `ClusterIssuer` типа `CA` обязан лежать в namespace `cert-manager` (cluster resource namespace по умолчанию) — очень частая ошибка.

**7. Проверка сертификата снаружи, глазами клиента.**
```bash
echo | openssl s_client -connect app.corp.vektor.lab:30xxx -servername app.corp.vektor.lab 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```
Смотрим `issuer=CN = vektor-internal-ca` и `notAfter`. Если issuer `CN=ingress-nginx` — значит отдаётся дефолтный fake-сертификат контроллера, то есть твой Secret не подхватился.

**8. Метрики: `kubectl top` и скрейпинг.**
```bash
kubectl top nodes
kubectl top pods -n prod
kubectl get servicemonitors -A
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```
`top` работает только при живом metrics-server. `ServiceMonitor` — CRD, по которому Prometheus понимает, куда ходить за `/metrics`; если после установки чарта список пустой — у Prometheus нет нужного селектора лейблов.

**9. Логи через LogQL.**
```bash
kubectl -n prod logs -l app=web --tail=50
kubectl -n prod logs -l app=web --since=15m | grep -i error
```
В Grafana → Explore → datasource Loki:
```text
{namespace="prod", app="web"} |= "error" | json | line_format "{{.msg}}"
```
`|=` — фильтр подстроки, `| json` — парсинг JSON-логов, `line_format` — вывод только нужного поля. Смотри, что метки (label) — низкой кардинальности: `namespace`, `app`, `pod`; `request_id` в метку не кладут.

**10. Снапшот etcd.**
```bash
kubectl -n kube-system exec etcd-k8s-cp01 -- sh -c \
 'ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /var/lib/etcd/snapshot-$(date +%F).db'

kubectl cp kube-system/etcd-k8s-cp01:/var/lib/etcd/snapshot-$(date +%F).db ./etcd-snapshot.db
etcdctl snapshot status ./etcd-snapshot.db -w table
```
`--endpoints` — локальный etcd, сертификаты — из `/etc/kubernetes/pki/etcd/`. Вывод `snapshot status` показывает размер, число ключей и хеш. Снапшот **не содержит** данные PV — только состояние API.

**11. Velero: бэкап и восстановление.**
```bash
velero backup create prod-daily --include-namespaces prod,cert-manager
velero backup get
velero backup describe prod-daily --details
velero restore create --from-backup prod-daily --include-namespaces prod
velero restore get
```
`--include-namespaces` ограничивает объём; `--details` показывает список объектов. Restore в занятый namespace частично конфликтует — для проверки бэкапа восстанавливай в чистый namespace или после удаления ресурсов.

**12. NetworkPolicy: default deny и точечные разрешения.**
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny, namespace: prod}
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: web-allow, namespace: prod}
spec:
  podSelector: {matchLabels: {app: web}}
  policyTypes: [Ingress, Egress]
  ingress:
    - from:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}
      ports: [{protocol: TCP, port: 8080}]
  egress:
    - to:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: kube-system}}
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
```
```bash
kubectl apply -f netpol.yaml
kubectl run netshoot --rm -it --image=nicolaka/netshoot -n prod -- /bin/bash
# внутри: nc -zv -w 3 web 8080   и   nc -zv -w 3 <ip-другого-пода> 8080
```
Первый запрос проходит, второй висит и падает по таймауту. **Egress DNS разрешать обязательно** — иначе все запросы по именам умрут с `no such host`, и это выглядит как «сломался кластер».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63) — кластер kubeadm, собранный в предыдущих главах модуля, CNI **Calico** (обязательно, иначе NetworkPolicy не работают). `GIT01` (10.10.10.50) — для хранения манифестов, `HARBOR01` (10.10.10.55) — чтобы взять образ приложения из проекта №1. `DC01` (10.10.10.10) — DNS-зона `corp.vektor.lab`. Клиент — `WIN11-CL01` для проверки из браузера. Учётка `adm.student`, пароль стенда.

Если кластер ещё не собран — минимальная настройка: на всех трёх нодах containerd, `kubeadm`/`kubelet`/`kubectl`, `kubeadm init --pod-network-cidr=10.244.0.0/16 --control-plane-endpoint "<адрес apiserver>:6443"`, затем Calico. Для справки, как выглядит HA (на стенде не обязательно):
```bash
# на первом CP:  --control-plane-endpoint "<VIP или DNS балансировщика>:6443" --upload-certs
# на остальных:  kubeadm join <endpoint>:6443 --token ... --discovery-token-ca-cert-hash ... \
#                --control-plane --certificate-key ...
```
Ресурсы: cp01 — 4 vCPU / 8 ГБ, воркеры — 2–4 vCPU / 4–6 ГБ, иначе kube-prometheus-stack + Loki уронят ноды по памяти. Если ПК слабый — ставь наблюдаемость после того, как закроешь остальные шаги.

### Снапшот

В VMware Workstation на **всех** затрагиваемых ВМ (`k8s-cp01`, `k8s-w01`, `k8s-w02`, `DC01`): VM → Snapshot → Take Snapshot, имя `before-devops-118`. Или из CLI, если установлен `vmrun`:
```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" snapshot "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-118
```
[ПРОВЕРИТЬ путь к vmrun на твоей версии Workstation]

### Шаги

1. **`k8s-cp01`.** Проверь кластер: `kubectl get nodes -o wide`, `kubectl -n kube-system get pods | grep -E 'calico|kube-proxy|coredns'`. Ожидаемо: 3 узла `Ready`, Calico в `Running`, CoreDNS 2 реплики.
2. **`k8s-cp01`.** Создай окружение: `kubectl create namespace prod`, `kubectl label namespace prod pod-security.kubernetes.io/enforce=baseline`. Ожидаемо: namespace создан, лейбл виден в `kubectl get ns prod --show-labels`.
3. **`k8s-cp01`.** Примени `deployment.yaml` и `pdb` из раздела 4 (образ — из HARBOR01). Проверь `kubectl -n prod get pods -o wide`. Ожидаемо: 3 пода `Running 1/1`, распределены по `k8s-w01` и `k8s-w02`.
4. **`k8s-cp01`.** Установи ingress-nginx (команды из раздела 4, п.3). Проверь `kubectl get ingressclass` и `kubectl -n ingress-nginx get svc`. Ожидаемо: `ingressclass nginx` и NodePort вида `80:30xxx`.
5. **`DC01`.** В DNS-менеджере оснастки DNS добавь в зону `corp.vektor.lab` запись A: `app` → `10.10.10.61`, `grafana` → `10.10.10.61`. [ПРОВЕРИТЬ путь: DNS → Forward Lookup Zones → corp.vektor.lab → New Host (A or AAAA)]. На `WIN11-CL01`: `nslookup app.corp.vektor.lab` → ожидаемо `10.10.10.61`. Если DNS править не хочешь — временно добавь строку в `C:\Windows\System32\drivers\etc\hosts`, но помни, что это костыль.
6. **`k8s-cp01`.** Установи cert-manager, примени issuer-манифесты и `ingress.yaml`. Проверь `kubectl -n prod get certificate` и `kubectl -n prod get secret web-tls`. Ожидаемо: `web-tls` `READY True`, секрет существует, тип `kubernetes.io/tls`.
7. **`WIN11-CL01`.** Импортируй корневой CA в доверенные: достань сертификат `kubectl -n cert-manager get secret vektor-ca-key-pair -o jsonpath='{.data.tls\.crt}' | base64 -d > vektor-ca.crt`, перенеси на клиент и поставь через `certmgr.msc` → Доверенные корневые центры сертификации → Импорт [ПРОВЕРИТЬ пункт меню в твоей редакции]. Открой `https://app.corp.vektor.lab:30xxx`. Ожидаемо: страница открывается **без** предупреждения, в свойствах сертификата издатель `vektor-internal-ca`.
8. **`k8s-cp01`.** Установи kube-prometheus-stack:
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f monitoring-values.yaml
```
где `monitoring-values.yaml` содержит `grafana.ingress.enabled: true`, `grafana.ingress.hosts[0]: grafana.corp.vektor.lab`, `grafana.ingress.tls[0].secretName: grafana-tls`, `prometheus.prometheusSpec.retention: 6h`, разумные `resources.requests`. Ожидаемо: `kubectl -n monitoring get pods` — все `Running`, Grafana открывается по `https://grafana.corp.vektor.lab:30xxx`.
9. **`k8s-cp01`.** Поставь Loki и агент сбора логов (Promtail или Grafana Alloy — Promtail официально уходит в deprecation, уточни актуальный чарт [ПРОВЕРИТЬ]). Сгенерируй тестовый лог: `kubectl -n prod exec deploy/web -- curl -s localhost:8080/nonexistent`. В Grafana → Explore → Loki выполни `{namespace="prod"} |= "error"`. Ожидаемо: строки с ошибкой видны, метки `namespace`/`app`/`pod` присутствуют.
10. **`k8s-cp01`.** Сделай снапшот etcd (раздел 4, п.10), сохрани копию в `/var/backups/etcd/` и проверь `snapshot status`. Затем разверни MinIO в namespace `velero` и установи Velero с бэкендом MinIO, выполни `velero backup create prod-daily --include-namespaces prod,cert-manager`, дождись `STATUS Completed` в `velero backup get`. Ожидаемо: бэкап есть, `velero backup describe --details` показывает объекты.
11. **`k8s-cp01`.** Примени `default-deny` и `web-allow` из раздела 4. Проверь `kubectl -n prod get networkpolicy` и тест из `netshoot` (раздел 4, п.12). Ожидаемо: снаружи через Ingress приложение открывается, из чужого пода по прямому IP — таймаут.
12. **`k8s-cp01`.** Проверка отказоустойчивости: `kubectl drain k8s-w01 --ignore-daemonsets --delete-emptydir-data`, наблюдай `kubectl -n prod get pods -o wide -w`, затем `kubectl uncordon k8s-w01`. Ожидаемо: поды переезжают, `kubectl -n prod get pdb` не даёт уронить ниже `minAvailable: 2`, сервис остаётся доступен.

### Проверка

- [ ] `kubectl get nodes` — 3 узла `Ready`, после `uncordon` нет `SchedulingDisabled`
- [ ] `kubectl -n prod get pods` — 3 реплики `Running`, распределены по двум воркерам
- [ ] `kubectl -n prod get pdb` — `ALLOWED DISRUPTIONS` не равно 0 при штатной работе
- [ ] `kubectl get ingressclass` показывает `nginx`, `kubectl -n prod get ingress` имеет `ADDRESS`
- [ ] `kubectl -n prod get certificate` — `READY True`
- [ ] `curl -I https://app.corp.vektor.lab:30xxx` с клиента — `200`, без ошибки сертификата
- [ ] Grafana открывается, в `Status → Targets` все цели `UP`
- [ ] В Loki находится запись по `{namespace="prod"}`
- [ ] `velero backup get` — свежий бэкап `Completed`; etcd-снапшот проверен `snapshot status`
- [ ] Из `netshoot` прямой доступ к чужому поду блокируется, доступ через Ingress работает

### Сломай и почини

> **Пользователь:** petrova / ВМ: WIN10-CL02
> **Что написала:** «Портал опять с замком! Пишет, что подключение не защищено. Вчера всё было нормально. У нас совещание через час»
> **Срочность:** высокая

**Как воспроизвести поломку (на `k8s-cp01`):**
```bash
kubectl -n prod delete secret web-tls
kubectl -n cert-manager delete secret vektor-ca-key-pair
```
Первый Secret заставит cert-manager перевыпускать сертификат для Ingress, второй ломает CA-issuer — перевыпуск провалится, и ingress-nginx начнёт отдавать свой дефолтный self-signed сертификат.

**Диагностика по порядку:**
1. `kubectl -n prod get certificate` — видим `READY False`. Значит проблема не в браузере.
2. `kubectl -n prod describe certificate web-tls` — в `Events`/`Message` будет причина: `Failed to get CA secret` / `secret "vektor-ca-key-pair" not found`.
3. `kubectl get clusterissuer vektor-ca -o yaml` и `kubectl describe clusterissuer vektor-ca` — статус `False`, сообщение про отсутствующий секрет.
4. `kubectl -n prod get certificaterequest` — последний запрос в состоянии `Failed`; `kubectl -n prod describe certificaterequest <имя>` подтверждает.
5. `kubectl -n cert-manager logs deploy/cert-manager --tail=100` — ошибки `not found` по секрету. Логи — финальная точка, они подтверждают гипотезу, а не заменяют её.
6. `kubectl -n cert-manager get secret` — секрета `vektor-ca-key-pair` действительно нет.

**Решение:**
```bash
velero backup get
velero restore create --from-backup <последний бэкап> \
  --include-namespaces cert-manager --include-resources secrets
```
[ПРОВЕРИТЬ точный синтаксис `--include-resources` в твоей версии Velero]. После восстановления секрета:
```bash
kubectl -n prod delete certificaterequest --all
kubectl -n prod get certificate -w
```
cert-manager увидит отсутствующий Secret `web-tls` и перевыпустит сертификат тем же CA. Проверка с клиента: `curl -I https://app.corp.vektor.lab:30xxx` → `200`, в свойствах сертификата издатель `vektor-internal-ca`. Если бэкапа нет — придётся пересоздать корневой CA, а это значит перевыпустить **все** сертификаты стенда; именно поэтому ключ CA бэкапят отдельно.

**Ответ пользователю:** «Мария, добрый день. Проблема была на стороне сертификата: он перестал обновляться, браузер показывал предупреждение. Сейчас сертификат перевыпущен, портал открывается штатно, к совещанию всё работает. Чтобы такое не повторялось, мы вынесли ключ сертификата в резервную копию и настроили алерт на истечение срока — за 30 дней придёт уведомление. Спасибо, что написали, это была полезная заявка».

**Закрытие заявки:** категория «Доступность / TLS», решение — восстановление CA-секрета из Velero и перевыпуск сертификата, корневая причина — «секрет CA не входил в стандартный бэкап и не было алерта на `Certificate` в состоянии `Ready=False`». Заведи follow-up: алерт `certmanager_certificate_expiration_timestamp_seconds < 30 дней` и правило бэкапа namespace `cert-manager`.

### Что изменилось на стенде

- `k8s-cp01/w01/w02`: namespace `prod` (лейбл Pod Security `baseline`), `ingress-nginx`, `cert-manager`, `monitoring`, `logging`, `velero`
- Helm-релизы: `ingress-nginx`, `cert-manager`, `kube-prometheus-stack`, Loki + агент, Velero
- `prod`: Deployment `web` (3 реплики, пробы, requests/limits), Service `web`, Ingress `web`, PDB `web`, NetworkPolicy `default-deny` и `web-allow`
- cert-manager: `ClusterIssuer vektor-selfsigned`, Certificate `vektor-ca` + Secret `vektor-ca-key-pair` (ns `cert-manager`), `ClusterIssuer vektor-ca`, Secret `web-tls` (ns `prod`), Secret `grafana-tls` (ns `monitoring`)
- `DC01`: A-записи `app.corp.vektor.lab` и `grafana.corp.vektor.lab` → 10.10.10.61
- `WIN11-CL01`: корневой CA `vektor-internal-ca` добавлен в доверенные
- Бэкапы: `/var/backups/etcd/snapshot-<дата>.db` на `k8s-cp01`, MinIO в ns `velero`, бэкап `prod-daily`
- Все манифесты закоммичены в Git на `GIT01`, папка `k8s/prod`, папка `k8s/platform`

### Откат

VMware Workstation → на каждой ВМ (`k8s-cp01`, `k8s-w01`, `k8s-w02`, `DC01`) → Snapshot Manager → `before-devops-118` → Revert. Откатывать надо **все** узлы: состояние etcd и состояние ВМ должны совпадать, иначе кластер поднимется с «протухшими» сертификатами kubelet и узлы уйдут в `NotReady`. После отката проверь `kubectl get nodes` и убедись, что часы на нодах не разъехались (`timedatectl`), иначе TLS внутри кластера начнёт отваливаться.

## 6. Ошибки новичков и безопасность

1. **NetworkPolicy на Flannel.** → Политики создаются, но ничего не блокируют; ложное чувство защищённости. → Сначала проверь CNI (Calico/Cilium), только потом обещай ИБ изоляцию.
2. **`default-deny` без разрешения DNS.** → Все сервисы падают с `no such host`, диагностика уводит в сторону «сломался CoreDNS». → Всегда добавляй egress на 53 UDP/TCP в `kube-system`.
3. **Нет `requests`/`limits`.** → Один под съедает память ноды, kubelet начинает эвиктить соседей. → Задавай requests всегда, limits — осознанно, с замером.
4. **Одна реплика и нет PDB.** → `kubectl drain` или обновление ноды = простой. → 3 реплики, `topologySpreadConstraints`, `minAvailable`.
5. **CA-ключ cert-manager живёт только в кластере.** → Потеря = перевыпуск всех сертификатов. → Отдельный бэкап Secret `vektor-ca-key-pair`, вне общего цикла, и алерт на `Ready=False`.
6. **Бэкап etcd без проверки восстановления.** → «Бэкап есть» превращается в «бэкапа нет» ровно в момент аварии. → Раз в квартал разворачивай снапшот на тестовом кластере.
7. **Velero без бэкапа данных томов.** → Kubernetes-объекты восстановились, а содержимое PV — нет. → Включай file-system backup (Kopia/restic) или снапшоты томов, проверяй restore.
8. **Секреты в values-файлах в Git.** → Утечка паролей Grafana, MinIO, Harbor. → SOPS/Sealed Secrets/Vault, значения — только в защищённом хранилище.

**Безопасность.** На стенде API-сервер доступен из всей подсети 10.10.10.0/24 — это лабораторный компромисс: в бою apiserver закрывают security group/nftables, доступ — через bastion и VPN, а kubeconfig с `cluster-admin` не раздают разработчикам (им — только namespace-scoped Role). NodePort с TLS-редиректом — учебная схема; в бою ставят внешний LB с WAF и rate limiting. Self-signed CA удобен в лаборатории, но требует ручной раздачи корня на все машины; в компании это делает GPO на домене `corp.vektor.lab`, а приватный ключ CA лежит не в git, а в Vault. И помни правило стенда: всё, что «упростили ради лабы», должно быть записано в `stand-log.md` как известное упрощение.

## 7. Вопросы на собеседовании

**1. Как устроен HA control plane в кластере kubeadm и почему узлов три, а не два?**

Ответ: control plane — это несколько kube-apiserver за общим стабильным адресом (VIP через kube-vip/keepalived или внешний LB) плюс etcd, который использует Raft и требует кворума для записи. Для 3 узлов кворум = 2, отказ одного переживаем. Для 2 узлов кворум = 2, отказ любого останавливает запись — то есть HA нет. Пятёрка даёт отказ двух, но растёт задержка репликации. В kubeadm это делается `kubeadm init --control-plane-endpoint ... --upload-certs` и `kubeadm join --control-plane`, etcd либо stacked (на тех же нодах), либо external (отдельный кластер). Scheduler и controller-manager работают в режиме leader election — активен один, остальные ждут.

*Что хочет услышать интервьюер:* что ты понимаешь кворум и почему чётное число — зло, и знаешь, что HA control plane без стабильного endpoint бессмыслен.

*Красный флаг:* «поднимем два мастера, если один упадёт — второй продолжит» — это ровно тот ответ, который показывает отсутствие понимания Raft.

**2. Как cert-manager продлевает сертификаты и что делать, если CA потерян?**

Ответ: `Certificate` описывает желаемое состояние (`dnsNames`, `duration`, `renewBefore`), cert-manager создаёт `CertificateRequest`, Issuer его подписывает и кладёт результат в Secret типа `kubernetes.io/tls`. Продление запускается автоматически за `renewBefore` до истечения (по умолчанию — треть срока), плюс перевыпуск при изменении Secret или самого `Certificate`. Если CA-ключ утерян или удалён, все `Certificate`, ссылающиеся на этот `ClusterIssuer`, становятся `Ready=False`, а кластер продолжает отдавать старый сертификат до истечения срока — то есть авария отложенная и потому коварная. Лечится восстановлением Secret CA из бэкапа (Velero) и перевыпуском; если бэкапа нет — создаётся новый CA и перевыпускается всё, с повторной раздачей корня клиентам.

*Что хочет услышать интервьюер:* понимание цепочки объектов и того, что PKI — это тоже данные, которые надо бэкапить.

*Красный флаг:* «cert-manager сам всё делает, там нечего бэкапить».

**3. Как работает NetworkPolicy и почему иногда «не работает»?**

Ответ: NetworkPolicy — декларативное описание разрешённых потоков на L3/L4 с точки зрения пода (podSelector). Аддитивная модель: если хоть одна политика выбирает под, он переходит в режим deny-by-default для указанных в `policyTypes` направлений, дальше разрешают только явные правила (OR по правилам, AND внутри одного правила по `from`+`ports`). Исполняет политику CNI-плагин, а не сам Kubernetes: Calico и Cilium умеют, классический Flannel — нет. Поэтому «создал политику и всё равно ходит» — это почти всегда про CNI. Второй частый случай — забыли egress на DNS. Третья тонкость — `namespaceSelector` по лейблу `kubernetes.io/metadata.name`, который Kubernetes ставит автоматически.

*Что хочет услышать интервьюер:* что ты знаешь про зависимость от CNI и про аддитивность, а не только «запретить всё».

*Красный флаг:* «NetworkPolicy работает из коробки в любом кластере» или «это как security group у облака».

**4. Как ты бэкапишь Kubernetes-кластер и что входит в бэкап?**

Ответ: три слоя. Первый — etcd snapshot: всё состояние API (Deployment, Service, Secret, ConfigMap, RBAC), восстанавливается быстро, но не содержит данных PV. Второй — Velero: бэкап объектов по namespace в объектное хранилище плюс, через file-system backup или снапшоты, содержимое томов. Третий — данные внешних систем (БД), они бэкапятся своими средствами. Дальше — расписание, retention, и главное: регулярная проверка restore в отдельном namespace или на отдельном кластере, с зафиксированным RPO/RTO. Ещё важно бэкапить то, что «не выглядит как приложение»: Secret’ы CA, kubeconfig, манифесты установки кластера.

*Что хочет услышать интервьюер:* различение состояния API и данных, и что бэкап без проверки восстановления не считается.

*Красный флаг:* «у нас etcd-снапшот раз в сутки, значит всё покрыто» — без темы PV и restore-теста.

**5. Ingress отдаёт 502 Bad Gateway, приложение в кластере Running. Как диагностировать?**

Ответ: идти по цепочке снизу вверх, а не гадать. Сначала `kubectl -n prod get endpointslices`/`get endpoints` — есть ли готовые адреса у Service (если пусто: не совпал селектор или не проходит `readinessProbe`). Потом `kubectl -n prod port-forward svc/web 8080:80` и `curl localhost:8080` — живо ли приложение в обход Ingress. Затем `kubectl -n ingress-nginx logs <pod>` — видно `upstream timed out` / `connect refused` (порт в Service указан неверно) / `no endpoints`. Дальше проверяю `containerPort` vs `targetPort` vs фактический порт приложения, потом NetworkPolicy — политика, запрещающая доступ из namespace `ingress-nginx`, даёт именно таймауты и 502. И только потом конфиг самого Ingress.

*Что хочет услышать интервьюер:* структурный порядок диагностики по слоям и знание, что 502 — это «контроллер не смог достучаться до upstream», а не «приложение упало».

*Красный флаг:* «перезапущу поды, обычно помогает» без единой проверки.

## 8. Источники (официальная документация)

- **kubernetes.io** — разделы «Creating a cluster with kubeadm», «High Availability Topology», «Ingress», «Network Policies», «Pod Disruption Budgets», «Configure Liveness, Readiness and Startup Probes». Поиск: `kubernetes kubeadm high availability topology`, `kubernetes network policy default deny`.
- **etcd.io** — раздел «Operations guide / Disaster recovery» (snapshot save/restore). Поиск: `etcd disaster recovery snapshot restore`.
- **cert-manager.io** — разделы «Concepts», «CA Issuer», «SelfSigned Issuer», «Troubleshooting certificates», «Backup and Restore». Поиск: `cert-manager CA issuer ClusterIssuer`, `cert-manager troubleshooting not ready`.
- **kubernetes.github.io/ingress-nginx** (документация ingress-nginx) — «Installation with Helm», «Bare-metal considerations», «Annotations». Поиск: `ingress-nginx bare metal NodePort`, `ingress-nginx force-ssl-redirect annotation`.
- **velero.io** — «Installation», «Backup reference», «Restore reference», «File system backup». Поиск: `velero backup restore file system backup kopia`.
- **docs.tigera.io** (Calico) — «Network policy» и «Calico the hard way / system requirements». Поиск: `calico network policy default deny`.
- **github.com/prometheus-community/helm-charts** — чарт `kube-prometheus-stack`, README и `values.yaml`. Поиск: `kube-prometheus-stack helm values grafana ingress`.
- **grafana.com/docs/loki** — «Get started», «LogQL», «Send logs» (Alloy/Promtail, статус поддержки Promtail уточни — он переводится в deprecated [ПРОВЕРИТЬ]). Поиск: `loki logql query examples`, `grafana alloy promtail replacement`.
- **helm.sh/docs** — «Using Helm», команды `upgrade --install`, `repo add`. Поиск: `helm upgrade install namespace create-namespace`.
