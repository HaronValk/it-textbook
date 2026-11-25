# Сети в Kubernetes: CNI (Calico, Cilium, Flannel), NetworkPolicy, Service Mesh (обзор)

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку: **ivanov / WIN11-CL01 / «Портал согласования счетов в кластере открывается, но при сохранении выдаёт ошибку. Вчера всё работало, ничего не трогали» / средняя.**

Открываем кластер: поды `Running`, Service на месте, ошибок в событиях нет. Идём в под приложения — а он не резолвит имя базы. Или резолвит, но не может достучаться. Или может — как и любой другой под в кластере, включая чужой тестовый, который вы вчера запустили «на минуточку».

Сети в Kubernetes — это та часть, которая ломается тихо и не там, где ищешь. Pod получил IP от CNI, трафик между нодами ушёл в оверлей, Service раскидал его через iptables, а NetworkPolicy молча отбросила — без единой строчки в логах приложения, потому что пакет умер ещё до него.

После этой главы вы должны: понимать, что такое CNI и как kubelet через CRI-рантайм дёргает плагин; отличать Flannel от Calico и Cilium и осознанно выбирать; писать NetworkPolicy от «запретить всё» до точечного allow с DNS; диагностировать «не ходит трафик» по слоям; и внятно объяснить на собеседовании, зачем нужен service mesh и почему вы его пока не поставили.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Модель сети Kubernetes** (её называют flat network). Три правила, которые нужно знать наизусть:
1. Каждый под получает **свой уникальный IP** в кластере.
2. Поды общаются друг с другом **напрямую, без NAT**.
3. То, что под видит как свой IP, — это тот же IP, который видят остальные.

Без этого не работают ни Service, ни NetworkPolicy, ни mTLS в mesh.

**Кто за что отвечает.** CNI (Container Network Interface) — это *спецификация* и набор плагинов, которые выдают поду IP и настраивают маршрут до него. kubelet сам плагин не вызывает: он говорит CRI-рантайму (containerd) «создай под с сетью», а уже containerd запускает бинарник из `/opt/cni/bin` с конфигом из `/etc/cni/net.d/*.conflist`. Плагину по stdin передаётся JSON, а через переменные окружения — команда (`CNI_COMMAND=ADD|DEL|CHECK`), ID контейнера, путь к сетевому namespace, имя интерфейса. Плагин делает `veth`-пару, один конец в под, второй на ноду, назначает IP (IPAM) и прописывает маршруты.

Дальше по слоям:

```text
┌──────────────────── k8s-w01 (10.10.10.62) ─────────────────────┐
│  Pod api 10.244.1.5 ──veth── cali1a2b ─┐                        │
│  Pod db  10.244.1.9 ──veth── cali3c4d ─┴── маршруты ноды        │
│                                   │  overlay: vxlan / ipip / bgp │
└───────────────────────────────────┼─────────────────────────────┘
                                    │  VMnet8 10.10.10.0/24 (underlay)
┌───────────────────────────────────┴─────────────────────────────┐
│  k8s-cp01 (10.10.10.61):  Pod 10.244.0.7                        │
└─────────────────────────────────────────────────────────────────┘

Слои ответственности:
  CNI          → IP пода + связность под↔под (L3)
  kube-proxy   → DNAT Service ClusterIP → IP пода (iptables/IPVS/nftables)
  CoreDNS      → имя svc.ns.svc.cluster.local → ClusterIP
  NetworkPolicy→ разрешить/запретить L3-L4 между подами (реализует CNI!)
  Service Mesh → mTLS, ретраи, L7-маршрутизация (отдельный слой прокси)
```

**Три реализации CNI, которые спрашивают на собеседовании.**

| | Flannel | Calico | Cilium |
|---|---|---|---|
| Транспорт | VXLAN (UDP 8472) или `host-gw` | IPIP (proto 4), VXLAN (UDP 4789), BGP (TCP 179), без инкапсуляции | VXLAN (8472), Geneve (6081), native routing, eBPF |
| NetworkPolicy | **нет** | да (+ свои CRD, GlobalNetworkPolicy) | да (+ L7 через CiliumNetworkPolicy) |
| Dataplane | ядро Linux | iptables/nftables или eBPF | eBPF |
| Плюсы | ставится за 2 минуты, прост | зрелый, BGP-пиринг с коммутатором, гибкая политика | быстрый, Hubble для наблюдаемости, замена kube-proxy |
| Минусы | нет политик → только для тестов | больше сущностей (Felix, BIRD, Typha) | требует свежее ядро, свой CLI |

Про MTU: инкапсуляция съедает байты — VXLAN 50 (MTU 1450 при underlay 1500), IPIP 20 (1480), WireGuard ~60. Отсюда классический баг «ping ходит, HTTPS висит».

**NetworkPolicy** — объект `networking.k8s.io/v1`, который применяется к подам, выбранным `podSelector`. Ключевое: это **только allow-правила**. Политики аддитивны: если под выбран хотя бы одной политикой, разрешено то, что разрешено любой из них; всё прочее — дроп. Нет политики вообще — разрешено всё. `podSelector: {}` = все поды неймспейса, и это же основа default-deny.

Тонкость, на которой валятся все: в одном элементе `from` несколько селекторов объединяются через **И**, а разные элементы списка — через **ИЛИ**. И чтобы трафик прошёл, нужен allow и на стороне отправителя (egress), и на стороне получателя (ingress). Политика не покрывает поды с `hostNetwork` и, в большинстве реализаций, трафик с самой ноды.

**Service Mesh** — прокси рядом с каждым подом (sidecar, обычно Envoy) или per-node (Istio ambient, Cilium). Даёт mTLS между подами, ретраи/таймауты/circuit breaking, канареечные релизы по заголовкам, единые метрики и трейсы. Цена: лишний хоп, память, своя кривая обучения. NetworkPolicy этого не умеет — она про «пустить/не пустить», а не «перепиши запрос и повтори дважды».

## 3. Аналогия из жизни

Бизнес-центр «Вектор», три корпуса. Каждому сотруднику выдают личный внутренний номер — это CNI. Между корпусами прорыты подземные туннели, чтобы курьер не бегал по улице в обход, — это оверлей (VXLAN/IPIP). Flannel — курьер, который просто носит посылки и ничего не проверяет. Calico — тот же курьер плюс охрана на входе в каждый кабинет со списком, кому к кому можно. Cilium — курьер со сканером и камерой: он ещё и логирует каждый визит. Единый номер 8-800 «позвоните в отдел» — это Service: kube-proxy переводит звонок на свободного сотрудника, причём звонящий не знает, на кого именно попал. NetworkPolicy — список на КПП: «курьеру из `demo` можно к `db`, и только с папками».

Service Mesh — это когда у каждого сотрудника появляется личный секретарь: он проверяет паспорт у входящего (mTLS), сам переспрашивает, если собеседник не ответил с первого раза (retry), и ведёт журнал всех разговоров. Удобно, но теперь в офисе вдвое больше людей, и если секретарь заболел — приём остановился.

Где аналогия ломается: правила в сети оперируют пакетами и IP, а не людьми и намерениями, и «секретарь» здесь — это ещё один контейнер, который ест CPU и имеет свои CVE.

## 4. Практика и команды

Всё выполняется с `k8s-cp01` (там `kubectl`) и на узлах по SSH.

**1. Что за CNI стоит в кластере**
```bash
kubectl get pods -n kube-system -o wide
kubectl get ds -A
```
Смотрим на имена: `calico-node`/`calico-kube-controllers`, `kube-flannel-ds`, `cilium`+`cilium-operator`. Видим `kube-flannel-ds` на всех трёх нодах в `Running` — значит, оверлей есть, а NetworkPolicy работать не будет.

**2. Конфиг CNI на ноде**
```bash
ls -l /etc/cni/net.d/
cat /etc/cni/net.d/10-flannel.conflist
ls /opt/cni/bin
```
В `.conflist` видно `"type": "flannel"`, `"delegate"` и подсеть. `bridge`, `host-local`, `loopback`, `portmap`, `tuning` в `/opt/cni/bin` — базовые плагины, они есть всегда; `calico`, `cilium-cni` появляются со своим CNI.

**3. Pod CIDR по нодам**
```bash
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'
```
Типично `10.244.0.0/24`, `10.244.1.0/24`, `10.244.2.0/24` — каждому узлу свой /24. Если в Calico IPPool другой пул (по умолчанию в манифесте `192.168.0.0/16`), поды будут получать адреса из пула, не совпадающего с `--pod-network-cidr` kubeadm.

**4. IP подов и их распределение по нодам**
```bash
kubectl get pods -A -o wide | head -20
```
Смотрим колонки IP и NODE. Два пода на разных нодах с адресами из разных /24 — норма.

**5. Туннели и MTU на ноде**
```bash
ip -d link show type vxlan
ip route
```
Интерфейсы `flannel.1`, `vxlan.calico`, `cilium_vxlan` — это и есть оверлей; в `ip -d` видно `vxlan id 1 ... dstport 8472` (Calico — `dstport 4789`). В `ip route` маршруты `10.244.1.0/24 via 10.10.10.62 dev flannel.1` — так нода знает, куда слать чужую подсеть.

**6. Отладочный под в сети**
```bash
kubectl run netshoot --image=nicolaka/netshoot:v0.13 --restart=Never -- sleep 3600
kubectl exec -it netshoot -- bash
# внутри:
ip a; ip route; cat /etc/resolv.conf
nslookup kubernetes.default.svc.cluster.local
curl -sv http://db.demo.svc.cluster.local
```
`netshoot` — образ с полным набором сетевых утилит. В РФ тяните его из HARBOR01 или через зеркало (урок 32), иначе `ImagePullBackOff`.

**7. Проверка MTU «как в бою»**
```bash
kubectl exec -it netshoot -- ping -M do -s 1400 <IP-пода-на-другой-ноде>
kubectl exec -it netshoot -- ping -M do -s 1450 <IP-пода-на-другой-ноде>
```
`-M do` запрещает фрагментацию. До 1400 проходит, на 1450 «message too long» — типичный признак, что оверлей не учёл overhead.

**8. Service и его эндпоинты**
```bash
kubectl get svc -n demo
kubectl get endpointslices -n demo
```
`kubectl get svc` показывает ClusterIP и порты, `endpointslices` — реальные IP подов, на которые DNAT'ит kube-proxy. Пустой список эндпоинтов = селектор Service не совпал с метками подов.

**9. Политики**
```bash
kubectl get networkpolicy -A
kubectl describe networkpolicy db-allow-api -n demo
```
`describe` печатает селекторы человеческим языком — самый быстрый способ понять, почему трафик дропается.

**10. Применить/удалить политику**
```bash
kubectl apply -f netpol-default-deny.yaml
kubectl delete networkpolicy default-deny-all -n demo
```

**11. Calico-специфика**
```bash
kubectl get ippools.crd.projectcalico.org -o wide
kubectl get pods -n kube-system -l k8s-app=calico-node
kubectl logs -n kube-system -l k8s-app=calico-node --tail=50
```

**12. Cilium-специфика** (если выбрали его)
```bash
cilium status
hubble observe --namespace demo --last 20
```
Hubble показывает поток «pod A → pod B, verdict FORWARDED/DROPPED» — то, чего у Calico из коробки нет.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Кластер из урока 34: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), все в `Ready`, pod CIDR `10.244.0.0/16`. `kubectl` настроен на `k8s-cp01`. Поднят HARBOR01 (10.10.10.55) или настроено зеркало Docker Hub (урок 32) — иначе образы не скачаются. GitLab на GIT01 (10.10.10.50) доступен для коммита манифестов. Если кластер не поднят — вернитесь к уроку 34, эта лаба без него не имеет смысла.

### Снапшот

В VMware Workstation на **всех трёх узлах** (`k8s-cp01`, `k8s-w01`, `k8s-w02`) — снапшот с именем `before-devops-037`. Удобно: выделить ВМ в библиотеке → правый клик → Snapshot → Take Snapshot. Или из CLI хоста: `vmrun snapshot "C:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-037`.

### Шаги

1. **k8s-cp01.** `kubectl get pods -n kube-system -o wide` и `kubectl get ds -A`. Ожидаемо: видно, какой CNI стоит. Запишите в блокнот — от этого зависит шаг 6.
2. **k8s-w01.** `ls -l /etc/cni/net.d/ && cat /etc/cni/net.d/*.conflist`. Ожидаемо: один `.conflist` с типом `flannel`, `calico` или `cilium`, внутри — подсеть и `"mtu"`.
3. **k8s-cp01.** Проверьте pod CIDR по нодам (команда №3 из раздела 4). Ожидаемо: три разных /24 из `10.244.0.0/16`.
4. **k8s-cp01.** Запустите два отладочных пода на разных нодах:
```bash
kubectl run net-w01 --image=nicolaka/netshoot:v0.13 --overrides='{"spec":{"nodeName":"k8s-w01"}}' -- sleep 3600
kubectl run net-w02 --image=nicolaka/netshoot:v0.13 --overrides='{"spec":{"nodeName":"k8s-w02"}}' -- sleep 3600
kubectl get pods -o wide
```
Ожидаемо: оба `Running`, у каждого свой IP. Запомните IP пода на `k8s-w02`.
5. **k8s-cp01.** `kubectl exec -it net-w01 -- ping -c3 <IP-пода-net-w02>`. Ожидаемо: 0% потерь, время ~0.3–1 мс. Это базовая связность до всякой политики.
6. **k8s-cp01 (только если на шаге 1 был Flannel).** Меняем CNI на Calico — иначе NetworkPolicy не заработает:
```bash
kubectl delete -f <манифест flannel, которым ставили>   # обычно kube-flannel.yml
# на КАЖДОЙ ноде:
sudo rm -f /etc/cni/net.d/10-flannel.conflist
sudo systemctl restart containerd kubelet
```
Затем скачайте актуальный `calico.yaml` с docs.tigera.io (Self-managed → Kubernetes API datastore) и **до apply** поправьте `CALICO_IPV4POOL_CIDR` на `10.244.0.0/16`, чтобы совпадало с kubeadm:
```bash
kubectl apply -f calico.yaml
kubectl get pods -n kube-system -l k8s-app=calico-node -w
```
Ожидаемо: `calico-node` на трёх нодах в `Running 1/1`. Поды, созданные до смены CNI, пересоздайте. Версия манифеста и точные имена переменных — `[ПРОВЕРИТЬ]` на docs.tigera.io на момент прохождения.
7. **k8s-cp01.** Разверните демо-приложение:
```bash
kubectl create namespace demo
kubectl -n demo create deployment db --image=nginx:1.27
kubectl -n demo expose deployment db --port=80
kubectl -n demo run api --image=nicolaka/netshoot:v0.13 --labels app=api -- sleep 3600
kubectl -n demo run client --image=nicolaka/netshoot:v0.13 --labels app=client -- sleep 3600
kubectl -n demo get pods,svc -o wide
```
Ожидаемо: три пода `Running`, Service `db` с ClusterIP, эндпоинт — под `db`.
8. **k8s-cp01.** До политик:
```bash
kubectl -n demo exec api -- curl -s -o /dev/null -w '%{http_code}\n' http://db
kubectl -n demo exec client -- curl -s -o /dev/null -w '%{http_code}\n' http://db
```
Ожидаемо: `200` и `200`. Любой под ходит куда хочет — это дыра, которую сейчас закроем.
9. **k8s-cp01.** Примените default-deny в `demo`:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: demo
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
```
`kubectl apply -f default-deny.yaml`, затем повторите шаг 8. Ожидаемо: оба запроса висят и падают по таймауту; `nslookup` внутри пода тоже перестаёт работать — вы только что заблокировали DNS.
10. **k8s-cp01.** Разрешите DNS и точечный доступ:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: allow-dns-egress, namespace: demo}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: kube-system}
      ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: db-allow-api, namespace: demo}
spec:
  podSelector: {matchLabels: {app: db}}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {matchLabels: {app: api}}
      ports:
        - {protocol: TCP, port: 80}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: api-egress-db, namespace: demo}
spec:
  podSelector: {matchLabels: {app: api}}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector: {matchLabels: {app: db}}
      ports:
        - {protocol: TCP, port: 80}
```
11. **k8s-cp01.** Проверка: `kubectl -n demo exec api -- curl -s -o /dev/null -w '%{http_code}\n' http://db` → `200`; то же из `client` → таймаут. `kubectl get networkpolicy -n demo` → три политики.
12. **k8s-cp01 → GIT01.** Положите оба YAML в репозиторий (например, `k8s/netpol/`), закоммитьте. Допишите изменения в `stand-log.md`.

### Проверка

- [ ] `kubectl get nodes` — три узла `Ready`, `kubectl get pods -n kube-system` — CNI-поды `Running`.
- [ ] Поды на разных нодах пингуют друг друга (шаг 5).
- [ ] `kubectl get ippools.crd.projectcalico.org -o wide` (для Calico) показывает пул `10.244.0.0/16`.
- [ ] До политик `api` и `client` получают `200` от `db`.
- [ ] После default-deny оба получают таймаут.
- [ ] После allow-политик `api` → `200`, `client` → таймаут.
- [ ] DNS внутри `api` работает: `nslookup db.demo.svc.cluster.local` возвращает ClusterIP.
- [ ] Манифесты закоммичены в Git, запись в `stand-log.md` сделана.

### Сломай и почини

**Пользователь:** petrova (HR)
**ВМ:** WIN10-CL02
**Что написал:** «Портал обучения на кластере открывается, а при входе — “не удалось загрузить данные”. Утром работало, после обеда перестало.»
**Срочность:** средняя, но к вечеру нужен отчёт.

**Как воспроизвести поломку (на `k8s-cp01`):** удалите политику DNS — `kubectl delete networkpolicy allow-dns-egress -n demo`. Сайт продолжит открываться (статика отдаётся), а любой запрос к API-поде, которой нужно резолвить `db`, упадёт.

**Диагностика по порядку:**
1. Что вообще происходит: `kubectl -n demo get pods` — `api` в `Running`, рестартов нет. Значит, дело не в приложении.
2. Логи: `kubectl -n demo logs deploy/api --tail=30`. Ищем `could not translate host name "db" ... Name or service not known` — это DNS, а не сеть до базы.
3. Изнутри пода руками: `kubectl -n demo exec api -- nslookup db` → таймаут; `kubectl -n demo exec api -- curl -s -m3 http://10.96.0.10:53` — тоже тишина.
4. Проверяем политики: `kubectl get networkpolicy -n demo` → видим `default-deny-all` и **отсутствие** `allow-dns-egress`. Причина найдена.
5. Убеждаемся, что CoreDNS жив: `kubectl -n kube-system get pods -l k8s-app=kube-dns` — `Running`. Значит, блокировка на стороне политики.

**Решение:** вернуть политику `allow-dns-egress` (`kubectl apply -f allow-dns-egress.yaml`), проверить `nslookup`, затем `curl` из `api`. Дополнительно — закрыть процесс: политики хранятся в Git, значит, добавить в CI проверку, что `allow-dns-egress` присутствует в каждом неймспейсе.

**Ответ пользователю:** «Проблема была на стороне кластера: при обновлении правил безопасности мы заблокировали сервису доступ к DNS. Правило вернули, вход работает. Причина устранена, чтобы не повторилось — правила теперь проходят проверку в пайплайне.»

**Закрытие заявки:** категория «конфигурация кластера», решение «добавлено сетевое правило allow-DNS в namespace demo», в комментарий — ссылка на коммит.

### Что изменилось на стенде

- CNI на `k8s-cp01`/`k8s-w01`/`k8s-w02` — **Calico** (если был Flannel), pod CIDR `10.244.0.0/16`, IPPool `default-ipv4-ippool`.
- Новый неймспейс `demo`.
- В нём: Deployment `db` (nginx) + Service `db` (ClusterIP, порт 80), поды `api` и `client` (netshoot), поды `net-w01`, `net-w02` — можно удалить в конце.
- NetworkPolicy в `demo`: `default-deny-all`, `allow-dns-egress`, `db-allow-api`, `api-egress-db`.
- В Git на GIT01 — каталог `k8s/netpol/` с манифестами.
- Запись в `stand-log.md` о смене CNI и новых объектах.

### Откат

Если кластер после смены CNI не поднялся: на всех трёх ВМ выключить, в VMware Workstation выбрать снапшот `before-devops-037` → Restore. После старта проверить `kubectl get nodes` и `kubectl get pods -n kube-system`. Точечный откат без снапшота: `kubectl delete networkpolicy --all -n demo` и `kubectl delete ns demo` — вернёт сетевую часть в исходное, но CNI останется новым.

## 6. Ошибки новичков и безопасность

1. **Поставить Flannel и ждать NetworkPolicy.** → Политики применяются без ошибок, но ничего не фильтруют: вы уверены, что база закрыта, а она открыта всему кластеру. → Ставьте CNI с поддержкой политик (Calico/Cilium) или Canal (Flannel + Calico policy).
2. **Default-deny без allow для DNS.** → Всё «падает» без внятной ошибки, `nslookup` висит по 5 секунд. → Всегда парой: default-deny + allow egress к `kube-system:53` (UDP и TCP).
3. **Разрешить только ingress.** → Трафик всё равно не идёт: пакет умирает на egress отправителя. → Правило нужно с двух сторон.
4. **`podSelector: {}` по невнимательности.** → Политика накрывает весь неймспейс, включая kube-system-подобные сервисы, и вы отрезаете себе доступ. → Перед `apply` — `kubectl diff -f` и `--dry-run=server`.
5. **Игнорировать MTU.** → `ping` и мелкие запросы ходят, HTTPS и gRPC висят на handshake. → Проверяйте `ping -M do -s` и согласовывайте MTU оверлея с underlay.
6. **Ставить service mesh «на всякий случай».** → Двойной расход памяти, латентность, отладка через три прокси. → Сначала NetworkPolicy + Ingress + метрики; mesh — когда нужен именно mTLS и L7-трафик-менеджмент.
7. **Тянуть образы с Docker Hub без зеркала.** → `ImagePullBackOff` на всех нодах, а лаба «не работает». → HARBOR01 или локальный прокси-кэш (урок 32).

**Безопасность.** NetworkPolicy — это не «файрвол на всю сеть»: она не защищает от трафика с самой ноды, от `hostNetwork`-подов и не логирует дропы (кроме Cilium/Hubble и платных режимов Calico). В боевой среде нельзя: оставлять `default-deny` только на бумаге и не проверять его реальным трафиком; открывать базу через `NodePort` (30000–32767) «чтобы быстрее» — порт виден всей сети `10.10.10.0/24`, а с NAT-шлюза потенциально и дальше; выдавать разработчикам `cluster-admin` для отладки сети вместо `kubectl exec`/`kubectl debug` в свой под. И помните: в этой лабе пароли и `default-deny` — учебные, в проде каждое правило должно проходить ревью и храниться в Git.

## 7. Вопросы на собеседовании

**1. Что такое CNI и кто его вызывает?**
CNI — спецификация плагинов, которые настраивают сеть пода: выдают IP через IPAM, создают `veth`, прописывают маршруты. Вызывает его не kubelet напрямую, а CRI-рантайм (containerd/CRI-O) по запросу kubelet: бинарник из `/opt/cni/bin` с конфигом из `/etc/cni/net.d`, JSON на stdin, команды `ADD`/`DEL`/`CHECK`. Дальше CNI отвечает только за L3-связность под↔под; Service — это kube-proxy, DNS — CoreDNS.
*Что хочет услышать:* понимание границы ответственности — CNI ≠ Service ≠ DNS.
*Красный флаг:* «CNI — это плагин для Service» или «kubelet сам настраивает iptables для подов».

**2. Flannel, Calico или Cilium — что выберете и почему?**
Flannel — минимальный оверлей (VXLAN 8472), ставится за минуты, NetworkPolicy не умеет: только тест/демо. Calico — зрелый выбор для прода: политики, IPIP/VXLAN/BGP, при BGP можно вообще без инкапсуляции и с меньшей латентностью, хорошо стыкуется с L3-фабрикой ЦОД. Cilium — eBPF: заменяет kube-proxy, умеет L7-политики (HTTP/DNS), даёт Hubble для наблюдаемости, но требует свежего ядра и своей экспертизы. В небольшом кластере на VMware я бы взял Calico; Cilium — когда нужны L7-политики и глубокая видимость.
*Что хочет услышать:* выбор по требованиям, а не «самый модный».
*Красный флаг:* «Cilium всегда лучше, потому что eBPF».

**3. Как работает NetworkPolicy и почему после default-deny всё сломалось?**
Это allow-список L3/L4, применяемый к подам по `podSelector`. Пока ни одна политика не выбрала под — разрешено всё; как только выбрала — разрешено только то, что перечислено, остальное дроп. Политики аддитивны. `podSelector: {}` + `policyTypes: [Ingress, Egress]` отрезает всё, включая DNS к CoreDNS, поэтому без отдельного allow на `kube-system:53` приложение не резолвит имена. Реализует политику CNI, а не API-сервер: без Calico/Cilium объект создастся, но эффекта не будет.
*Что хочет услышать:* «allow-only», «нужен allow на DNS», «нужен allow с обеих сторон».
*Красный флаг:* «NetworkPolicy блокирует по умолчанию» или «политики применяет kube-proxy».

**4. Поды на разных нодах не видят друг друга. Ваши действия?**
Сверху вниз: `kubectl get pods -n kube-system` — живы ли поды CNI; `kubectl get pods -o wide` — получили ли поды IP; с ноды `ip -d link show type vxlan` и `ip route` — есть ли туннель и маршрут в чужой /24; `ping` между подами; `ping -M do -s` — не MTU ли; логи CNI (`kubectl logs -n kube-system -l k8s-app=calico-node`); совпадает ли `CALICO_IPV4POOL_CIDR` с `--pod-network-cidr` kubeadm; не режет ли трафик firewalld/ufw на нодах.
*Что хочет услышать:* методичный разбор по слоям, а не «переустановлю CNI».
*Красный флаг:* «сразу пересоздам кластер».

**5. Зачем нужен service mesh, если есть NetworkPolicy и Ingress?**
NetworkPolicy — бинарное «можно/нельзя» на L3/L4. Mesh даёт то, чего в ней нет: взаимный TLS с автоматической ротацией сертификатов, ретраи/таймауты/circuit breaking, маршрутизацию по заголовкам и весам (канарейки), единые метрики и трейсы по каждому вызову. Ingress — только вход снаружи, mesh — трафик *между* сервисами. Ставить его стоит, когда сервисов десятки и нужны mTLS и управляемые релизы; для трёх подов это лишняя сложность — хватит NetworkPolicy и обычных метрик. Современная альтернатива sidecar'ам — per-node режимы (Istio ambient, Cilium mesh) `[ПРОВЕРИТЬ актуальный статус в документации]`.
*Что хочет услышать:* понимание цены и места mesh в архитектуре.
*Красный флаг:* «mesh обязателен для любого Kubernetes».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Cluster Networking»; статьи «Network Policies», «Service», «DNS for Services and Pods», «Debugging DNS Resolution». Поиск: `kubernetes network policies`, `kubernetes cluster networking`.
- **docs.tigera.io** (Calico) — «Calico networking fundamentals», «Get started with Calico on-premises», «Network policy», «Determining the MTU». Поиск: `calico network policy tutorial`, `calico mtu`.
- **docs.cilium.io** — «Concepts», «Network Policy», «Hubble», «kube-proxy replacement». Поиск: `cilium network policy getting started`.
- **github.com/flannel-io/flannel** — README и раздел «Backends» (VXLAN, host-gw). Поиск: `flannel vxlan backend port`.
- **istio.io** — «Traffic Management», «Security» (mTLS), «Ambient mode». Поиск: `istio ambient mode`.
- **linkerd.io** — «Architecture», «Automatic mTLS». Поиск: `linkerd architecture micro-proxy`.
- **github.com/nicolaka/netshoot** — список утилит в образе. Поиск: `netshoot image`.
- **cni.dev** — спецификация CNI (команды ADD/DEL/CHECK, переменные окружения). Поиск: `cni specification plugin`.
- Точные версии манифестов, номера портов по умолчанию у конкретных релизов и статус ambient-режима Istio — сверяйте на этих ресурсах, они меняются: `[ПРОВЕРИТЬ]`.
