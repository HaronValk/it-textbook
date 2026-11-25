# Основы Kubernetes: k3s/kubeadm, ingress, storage, Helm — первое знакомство

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди:

> **Пользователь:** petrova (HR) / ВМ WIN10-CL02
> **Что написал:** «Портал заявок на отпуск открывается, но при отправке пишет 502 Bad Gateway. У Иванова то же самое. Вчера работало.»
> **Срочность:** средняя

Три года назад такой тикет означал бы «пойди на сервер и проверь IIS». Сегодня всё чаще сервис живёт в Kubernetes: контейнеры, реплики, ingress, Helm-релиз. И админ, который умеет только `systemctl restart`, на такой заявке просто останавливается, потому что «сервера» нет — есть кластер, поды и контроллеры.

Kubernetes — это оркестратор контейнеров: он решает, *где* запустить контейнер, *сколько* копий держать, *как* к ним направить трафик и *что делать*, если один из узлов умер. Для системного админа это ровно та же работа, что и с серверами: ёмкость, сеть, диски, обновления, бэкапы, доступы, разбор инцидентов — только объекты другие.

После этой главы вы должны: понимать архитектуру кластера (control plane / worker, что делает kubelet и etcd), уметь поднять учебный кластер на k3s и знать, чем это отличается от kubeadm, ориентироваться в объектах (Pod, Deployment, Service, Ingress, PVC), разбирать базовый инцидент по цепочке «DNS → ingress → service → endpoints → pod → logs» и ставить приложения через Helm с возможностью отката. Это «первое знакомство»: глубину по сети и CNI даёт отдельная практика, а CI/CD и GitOps — урок 80.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Кластер = control plane + worker-узлы.** Control plane принимает решения, worker-узлы запускают контейнеры.

- **kube-apiserver** — единственная точка входа. Все (kubectl, контроллеры, kubelet) общаются только с ним, по HTTPS, порт **6443**. Всё остальное — детали реализации.
- **etcd** — распределённое key-value хранилище, «журнал состояния». В k3s по умолчанию вместо etcd — **SQLite** (через Kine); для HA-кластера k3s умеет embedded etcd.
- **kube-scheduler** — выбирает узел для нового пода (по ресурсам, taint/toleration, affinity).
- **kube-controller-manager** — набор циклов «привести факт к желаемому»: Deployment-контроллер держит число реплик, Node-контроллер реагирует на упавшие узлы.
- **kubelet** (на каждом узле, порт **10250**) — агент, который реально запускает контейнеры через **containerd**.
- **kube-proxy** — программирует iptables/nftables/IPVS, чтобы трафик на Service доходил до подов.
- **CNI** (в k3s по умолчанию **flannel**, инкапсуляция VXLAN, UDP **8472**) — сеть подов, обычно `10.42.0.0/16`.

**Объекты, которые надо знать наизусть:** Pod (минимальная единица, 1+ контейнер), ReplicaSet (держит число копий), Deployment (версионируемый ReplicaSet — обновления и откаты), Service (стабильный ClusterIP и балансировка), Ingress (HTTP-маршрутизация по хосту/пути), ConfigMap/Secret, PV/PVC/StorageClass (диски), Namespace (изоляция).

```text
                  ┌──────────── k8s-cp01 (10.10.10.61) ─────────────┐
  kubectl / helm  │ kube-apiserver :6443  ←→  etcd (или SQLite)     │
  с UBU01         │ scheduler   controller-manager   CoreDNS        │
                  │ Traefik (ingress) :80/:443 через svclb          │
                  └───────────────┬─────────────────────────────────┘
                     kubelet :10250 (mTLS), flannel :8472/UDP
        ┌─────────────────────────┴──────────────────────────┐
   k8s-w01 (10.10.10.62)                              k8s-w02 (10.10.10.63)
   kubelet + containerd + flannel                      kubelet + containerd + flannel
   поды 10.42.x.x                                      поды 10.42.x.x

  Путь запроса пользователя:
  WIN11-CL01 → DNS (DC01) → 10.10.10.61:80 → Traefik → Ingress (Host: portal...)
             → Service ClusterIP:80 → kube-proxy → Pod:80
```

**k3s vs kubeadm.** kubeadm — «официальный установщик» upstream-кластера: вы сами ставите containerd, kubelet, kubeadm, kubectl, сами выбираете CNI, etcd, ingress. Гибко, но много шагов. k3s — один бинарник ~70 МБ, внутри уже containerd, flannel, Traefik, local-path-provisioner, metrics-server и serviceLB; ставится одной командой. Для филиала, тестового стенда и «мне нужен кластер на три ВМ» k3s — разумный выбор; для продакшена с требованиями к сертифицированной сборке — обычно managed Kubernetes или kubeadm/RKE2.

**Ingress** — это L7-правило (host/path → Service), а не сам прокси. Работает он только если в кластере есть **ingress controller** (в k3s — Traefik). Порт наружу контроллер получает через Service типа LoadBalancer, который в k3s реализует `svclb` (klipper-lb) — поднимает hostPort 80/443 на узлах.

**Storage.** Pod эфемерен: удалили — данные внутри контейнера пропали. Чтобы данные жили отдельно, нужны PV (реальный кусок диска), PVC (заявка на него) и StorageClass (как создавать PV автоматически). В k3s по умолчанию StorageClass `local-path` — каталог на конкретном узле. Это просто, но **не даёт отказоустойчивости**: под, уехавший на другой узел, этот PVC не смонтирует.

**Helm** — пакетный менеджер: chart = шаблоны манифестов + values.yaml. Релиз версионируется, поэтому `helm rollback` — штатная операция, а не «руками откатываем yaml из git».

## 3. Аналогия из жизни

Крупный порт. **Control plane** — диспетчерская башня: там знают, сколько судов должно стоять у причалов и какие причалы свободны; **etcd** — сейф с журналом, где записано, что *должно* быть. **Kubelet** — бригадир на конкретном причале: получил наряд — разгрузил контейнер, сломавшийся убрал, доложил. **Pod** — контейнер на площадке (и да, контейнеры в Kubernetes названы в честь морских). **Service** — единый внутренний номер телефона порта: неважно, на каком причале сейчас стоит груз, звонящий набирает один номер. **Ingress** — стойка на проходной: смотрит на табличку «HR» и направляет гостя к нужному причалу. **PV** — арендованный склад за забором, который живёт дольше, чем конкретный контейнер. **Helm** — типовой проект причала с опросным листом: меняешь в листе «2 крана вместо 1» — и получаешь готовый причал, а не рисуешь чертёж заново.

Аналогия ломается на главном: Kubernetes не «чинит» и не «переставляет» контейнер — он **уничтожает** нездоровый под и создаёт новый, с другим IP и другим именем. Внутреннее состояние контейнера при этом теряется, и именно поэтому в порту есть склады, а в кластере — PVC.

## 4. Практика и команды

Все команды — с UBU01 (10.10.10.30), где лежит kubeconfig, либо на узлах по SSH. Конфиг k3s: `/etc/rancher/k3s/k3s.yaml`.

**1) Установка сервера k3s (k8s-cp01).** Сначала файл конфигурации, потом установщик — так флаги не теряются при обновлении:

```bash
# /etc/rancher/k3s/config.yaml на k8s-cp01
node-ip: 10.10.10.61
write-kubeconfig-mode: "0640"
tls-san:
  - k8s-cp01
  - 10.10.10.61
```

```bash
curl -sfL https://get.k3s.io | sudo sh -
sudo systemctl status k3s --no-pager
sudo cat /var/lib/rancher/k3s/server/node-token   # токен для агентов
```

`write-kubeconfig-mode "0640"` — чтобы kubeconfig не был читаем всем миром. Типичный вывод: `Active: active (running)`. Если падает — `sudo journalctl -u k3s -e --no-pager`.

**2) Присоединение агентов (k8s-w01, k8s-w02).**

```bash
curl -sfL https://get.k3s.io | K3S_URL=https://10.10.10.61:6443 K3S_TOKEN=<токен> sudo -E sh -
```

`K3S_URL` — адрес API, `K3S_TOKEN` — общий секрет кластера. Ожидаемо: сервис `k3s-agent` в состоянии running.

**3) kubeconfig на рабочей машине (UBU01).**

```bash
mkdir -p ~/.kube
ssh -t adm.student@10.10.10.61 "sudo cat /etc/rancher/k3s/k3s.yaml" > ~/.kube/config
sed -i 's/127.0.0.1/10.10.10.61/' ~/.kube/config
chmod 600 ~/.kube/config
kubectl get nodes -o wide
```

Смотрим колонки `STATUS` (должно быть `Ready`), `ROLES` (`control-plane` / `<none>`), `INTERNAL-IP`, `VERSION`.

**4) Обзор кластера и системных подов.**

```bash
kubectl cluster-info
kubectl get pods -A -o wide
kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.capacity.cpu,MEM:.status.capacity.memory
```

В `kube-system` должны быть `coredns`, `traefik`, `local-path-provisioner`, `metrics-server`, `svclb-traefik-*`.

**5) Быстрый тест «жив ли кластер».**

```bash
kubectl create deployment smoke --image=nginx:stable-alpine --replicas=2
kubectl get deploy,rs,pods -l app=smoke
kubectl delete deployment smoke
```

Цепочка `deploy → rs → 2 pods` — это и есть работа контроллеров вживую.

**6) Разбор пода: describe и logs.**

```bash
kubectl describe pod -n portal -l app=portal | tail -30
kubectl logs -n portal deploy/portal --tail=50
kubectl logs -n portal <pod> --previous      # логи упавшего контейнера
```

В `describe` внизу — `Events:`; там `FailedScheduling`, `ImagePullBackOff`, `OOMKilled` и причина. Это первое место, куда смотрят.

**7) События по всему кластеру.**

```bash
kubectl get events -A --sort-by=.metadata.creationTimestamp | tail -20
```

Дешёвый способ увидеть, что происходило в кластере последние минуты, без чтения логов каждого компонента.

**8) Внутрь контейнера и порт-форвард.**

```bash
kubectl exec -it -n portal deploy/portal -- sh
kubectl port-forward -n portal svc/portal 8080:80
curl -I http://127.0.0.1:8080
```

Порт-форвард идёт через API-сервер и работает даже без ingress — полезно, когда непонятно, «приложение или маршрутизация».

**9) Сервисы, endpoints и ingress.**

```bash
kubectl get svc,ingress -n portal
kubectl get endpointslices -n portal
kubectl describe svc portal -n portal | sed -n '/Port/,/Endpoints/p'
```

Если `Endpoints: <none>` — селектор сервиса не совпадает с метками подов либо поды не Ready. Это причина 503 примерно в половине случаев.

**10) Storage.**

```bash
kubectl get storageclass
kubectl get pvc -n portal
kubectl describe pvc portal-data -n portal | tail -15
```

`local-path` помечен `(default)`. PVC в состоянии `Pending` с событием `waiting for first consumer` — это норма для local-path: PV создастся при появлении пода.

**11) Обслуживание узла.**

```bash
kubectl drain k8s-w01 --ignore-daemonsets --delete-emptydir-data
kubectl uncordon k8s-w01
```

`drain` аккуратно выселяет поды перед обслуживанием. Без `--ignore-daemonsets` команда упрётся в DaemonSet и не пойдёт.

**12) Helm: установка, история, откат.**

```bash
helm repo add traefik https://traefik.github.io/charts   # [ПРОВЕРИТЬ] актуальность URL
helm repo update
helm create portal
helm lint ./portal
helm upgrade --install portal ./portal -n portal --create-namespace
helm list -A
helm history portal -n portal
helm rollback portal 1 -n portal
```

`upgrade --install` идемпотентен: первый запуск ставит, последующие обновляют. `history` показывает ревизии — по ним и откатываемся.

**13) Уровень узла глазами админа.**

```bash
sudo systemctl status k3s --no-pager
sudo /usr/local/bin/k3s crictl ps | head
sudo du -sh /var/lib/rancher/k3s/server/db    # размер SQLite-базы
```

`k3s crictl` — обёртка над crictl, показывает реальные контейнеры containerd, а не абстракции Kubernetes.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L2 развёрнут: DC01 (10.10.10.10, DNS/DHCP), FS01, WIN11-CL01, UBU01 (10.10.10.30).
- **Новые ВМ для этого урока** (в таблице стенда они помечены как DevOps-трек, но на треке «Системный админ» вводятся здесь — фиксируем это в `stand-log.md`):
  - `k8s-cp01` — Ubuntu Server 24.04 LTS, 10.10.10.61, 2 vCPU / 4 ГБ / 30 ГБ;
  - `k8s-w01` — Ubuntu Server 24.04 LTS, 10.10.10.62, 2 vCPU / 4 ГБ / 25 ГБ;
  - `k8s-w02` — Ubuntu Server 24.04 LTS, 10.10.10.63, 2 vCPU / 4 ГБ / 25 ГБ.
  
  Адреса статические, вне DHCP-пула 10.10.10.100–150. Если на k8s-узлах остался Docker из урока 78 — снесите его (`sudo apt remove docker-ce docker-ce-cli containerd.io`): k3s несёт собственный containerd, два рантайма на одном узле конфликтуют правилами iptables.
- На DC01 в зоне `corp.vektor.lab` будет добавлена A-запись `portal` → 10.10.10.61.
- `kubectl`/`helm` — на UBU01. Пользователь `adm.student`, пароль стенда `Lab#2026-vektor`.

### Снапшот

На хосте VMware Workstation, для каждой из ВМ `k8s-cp01`, `k8s-w01`, `k8s-w02` (ВМ выключены или включены — снапшот делаем на выключенных, так чище):

```text
VM → Snapshot → Take Snapshot… → имя: before-sysadmin-079
```

Или из командной строки Windows на хосте:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-sysadmin-079
```

Если узлы размещены на PVE01 — `qm snapshot <VMID> before-sysadmin-079`.

### Шаги

1. **На всех трёх ВМ — имя хоста и статический IP.** `sudo hostnamectl set-hostname k8s-cp01` (и аналогично), затем netplan (`ip -br a` покажет имя интерфейса):

   ```yaml
   # /etc/netplan/60-static.yaml, права 600
   network:
     version: 2
     ethernets:
       ens33:                       # [ПРОВЕРИТЬ] своё имя интерфейса
         dhcp4: false
         addresses: [10.10.10.61/24]
         routes:
           - to: default
             via: 10.10.10.2
         nameservers:
           addresses: [10.10.10.10]
   ```

   `sudo chmod 600 /etc/netplan/60-static.yaml && sudo netplan apply`. **Ожидаемо:** `ip -br a` показывает 10.10.10.61/24, `ping 10.10.10.10` идёт. Если в системе уже есть `/etc/netplan/50-cloud-init.yaml`, он может перетирать настройки — тогда правьте его или отключайте управление сетью cloud-init.

2. **Подготовка ядра и swap на всех трёх ВМ.**

   ```bash
   sudo tee /etc/modules-load.d/k8s.conf <<'EOF'
   overlay
   br_netfilter
   EOF
   sudo modprobe overlay && sudo modprobe br_netfilter
   sudo tee /etc/sysctl.d/99-k8s.conf <<'EOF'
   net.bridge.bridge-nf-call-iptables  = 1
   net.bridge.bridge-nf-call-ip6tables = 1
   net.ipv4.ip_forward                 = 1
   EOF
   sudo sysctl --system
   sudo swapoff -a
   sudo sed -i '/ swap / s/^/#/' /etc/fstab
   ```

   **Ожидаемо:** `free -h` показывает Swap: 0B. Kubelet не любит swap — либо выключаем, либо настраиваем отдельно (в этом уроке выключаем).

3. **Синхронизация времени и SSH-доступ.** `timedatectl` — должно быть `System clock synchronized: yes`. С UBU01: `ssh-copy-id adm.student@10.10.10.61` (и .62/.63). **Ожидаемо:** `ssh adm.student@10.10.10.62 hostname` отвечает `k8s-w01` без пароля.

4. **Установка k3s-сервера на k8s-cp01.** Создайте `/etc/rancher/k3s/config.yaml` из раздела 4, затем `curl -sfL https://get.k3s.io | sudo sh -`. **Ожидаемо:** `sudo systemctl is-active k3s` → `active`, `sudo kubectl get nodes` → один узел `Ready`. Если `get.k3s.io` недоступен из вашей сети — ставьте из air-gap: скачайте `k3s` бинарник и `k3s-airgap-images-amd64.tar.zst` с github.com/k3s-io/k3s/releases на хосте и перенесите на ВМ (легально, Apache-2.0).

5. **kubeconfig на UBU01** (команды из п.3 раздела 4). **Ожидаемо:** `kubectl get nodes` с UBU01 видит `k8s-cp01 Ready`.

6. **Присоединение k8s-w01 и k8s-w02.** Токен: `sudo cat /var/lib/rancher/k3s/server/node-token`. **Ожидаемо:** `kubectl get nodes` → три узла `Ready`, `INTERNAL-IP` = .61/.62/.63. Если агент не подключился — `sudo journalctl -u k3s-agent -e` на нём и проверка `curl -k https://10.10.10.61:6443/ping` (ожидается `pong`).

7. **Смоук-тест кластера** (п.5 раздела 4). **Ожидаемо:** два пода `smoke` в `Running`, оба можно удалить. Убеждаемся, что планировщик распределяет поды по разным узлам (`kubectl get pods -o wide`).

8. **Создание Helm-чарта портала.** `helm create portal`, затем правки `values.yaml`:

   ```yaml
   image:
     repository: nginx
     tag: "stable-alpine"        # фиксируйте версию, не latest
   service:
     type: ClusterIP
     port: 80
   ingress:
     enabled: true
     className: "traefik"        # [ПРОВЕРИТЬ] в вашей версии Helm шаблон ingress может быть на аннотации
     hosts:
       - host: portal.corp.vektor.lab
         paths:
           - path: /
             pathType: Prefix
   resources:
     requests: {cpu: 50m, memory: 64Mi}
     limits:   {cpu: 200m, memory: 128Mi}
   ```

   `helm lint ./portal`, `helm template portal ./portal | head -60` — глазами проверяем, что получится.

9. **Установка релиза.** `helm upgrade --install portal ./portal -n portal --create-namespace`. **Ожидаемо:** `helm list -A` показывает `portal` в статусе `deployed`, `kubectl get pods -n portal` — под `Running`.

10. **DNS-запись на DC01.** В PowerShell на DC01 (модуль DnsServer):

    ```powershell
    Add-DnsServerResourceRecordA -Name portal -ZoneName corp.vektor.lab -IPv4Address 10.10.10.61
    ```

    **Ожидаемо:** с WIN11-CL01 `nslookup portal.corp.vektor.lab` возвращает 10.10.10.61.

11. **Проверка через ingress с рабочей станции.** В браузере WIN11-CL01: `http://portal.corp.vektor.lab` → страница nginx. С UBU01: `curl -I http://portal.corp.vektor.lab`. **Ожидаемо:** `HTTP/1.1 200 OK`, заголовок `Server: nginx`. Если 404 — Traefik не нашёл правило (проверьте `kubectl get ingress -n portal`); если таймаут — проверьте, что на k8s-cp01 слушается 80 (`sudo ss -lntp | grep :80`).

12. **Storage: PVC и данные.** Создайте PVC `portal-data` (1Gi, `storageClassName: local-path`) и отдельный Deployment `portal-cache` (1 реплика) с монтированием на `/data`. Зайдите в под, создайте файл:

    ```bash
    kubectl exec -it -n portal deploy/portal-cache -- sh -c 'echo hello > /data/probe.txt'
    kubectl delete pod -n portal -l app=portal-cache
    kubectl exec -it -n portal deploy/portal-cache -- cat /data/probe.txt
    ```

    **Ожидаемо:** файл на месте после пересоздания пода. Дальше — эксперимент: `kubectl cordon k8s-w01`, удалите под и посмотрите, на какой узел он уедет. Если уедет на другой — PVC останется `Pending`. Это и есть ограничение local-path, зафиксируйте его в журнале.

13. **Запись в `stand-log.md`:** три новых узла с адресами, версия k3s, namespace `portal`, DNS-запись, StorageClass по умолчанию.

### Проверка

- [ ] `kubectl get nodes` — три узла `Ready`, адреса .61/.62/.63.
- [ ] `kubectl get pods -A` — все поды `Running`, `coredns` и `traefik` присутствуют.
- [ ] `kubectl get ingress -n portal` — хост `portal.corp.vektor.lab` в списке.
- [ ] `nslookup portal.corp.vektor.lab` с WIN11-CL01 — 10.10.10.61.
- [ ] `curl -I http://portal.corp.vektor.lab` — 200.
- [ ] `helm list -A` — релиз `portal`, ревизия 1, `deployed`.
- [ ] `kubectl get pvc -n portal` — `portal-data` в состоянии `Bound`.
- [ ] Данные в `/data` переживают удаление пода.
- [ ] `stand-log.md` обновлён.

### Сломай и почини

> **Пользователь:** ivanov / ВМ WIN11-CL01 / «Не открывается портал заявок: браузер пишет **502 Bad Gateway**. У Петровой тоже. Вчера всё работало.» / **Срочность:** высокая.

**Воспроизводим поломку** (на UBU01): мы «обновляли» релиз и случайно поменяли порт сервиса:

```bash
helm upgrade portal ./portal -n portal --set service.port=8080
```

Шаблон `helm create` подставляет `service.port` и в `containerPort`, и в `targetPort` сервиса. nginx при этом слушает 80 → соединение до пода отбивается.

**Диагностика по порядку (сверху вниз, не прыгая):**

1. `nslookup portal.corp.vektor.lab` с WIN11-CL01 → 10.10.10.61. DNS в порядке, идём дальше.
2. `curl -I http://portal.corp.vektor.lab` с UBU01 → `502`. Значит Traefik жив, а upstream не отвечает. (Если было бы `503` — почти наверняка пустые endpoints.)
3. `kubectl get ingress -n portal` → правило на месте.
4. `kubectl get pods -n portal` → поды `Running`, но `RESTARTS` не растёт. Приложение не падает — проблема в связке.
5. `kubectl get endpointslices -n portal` → endpoint есть, `10.42.x.x:8080`. Вот и зацепка: порт 8080.
6. `kubectl describe svc portal -n portal` → `Port: 80/TCP`, `TargetPort: 8080`.
7. `kubectl exec -it -n portal deploy/portal -- sh -c 'ss -lnt'` → nginx слушает **:80**, восьмитысячника нет. Диагноз подтверждён.
8. `kubectl logs -n kube-system -l app.kubernetes.io/name=traefik --tail=20` → `dial tcp 10.42.x.x:8080: connect: connection refused`.
9. `helm history portal -n portal` → ревизия 2 (`superseded`), ревизия 1 (`deployed`) — рабочая.

**Решение:** `helm rollback portal 1 -n portal`, затем `kubectl rollout status deploy/portal -n portal` и `curl -I` → 200.

**Ответ пользователю:** «Портал снова работает, проверил: страница открывается. Причина — ошибка в конфигурации при обновлении сервиса, данные не пострадали. Если увидите 502 снова — пришлите скриншот, но это уже устранено.»

**Закрытие заявки:** в комментарии — «причина: неверный targetPort после helm upgrade, откат на ревизию 1; профилактика: проверка `helm template`/`--dry-run` перед upgrade и smoke-тест после релиза». Заведите задачу «добавить в чек-лист релиза проверку endpoints».

### Что изменилось на стенде

- Появились ВМ: `k8s-cp01` (10.10.10.61, control plane k3s), `k8s-w01` (10.10.10.62, агент), `k8s-w02` (10.10.10.63, агент). Ubuntu Server 24.04 LTS, статические адреса, swap off, модули overlay/br_netfilter.
- На DC01: A-запись `portal.corp.vektor.lab` → 10.10.10.61.
- В кластере: namespace `portal`, Helm-релиз `portal` (chart `./portal`, nginx), Service `portal` (ClusterIP:80), Ingress `portal` (host `portal.corp.vektor.lab`, ingressClassName `traefik`), PVC `portal-data` (local-path, 1Gi), Deployment `portal-cache`.
- StorageClass по умолчанию — `local-path`. Ingress controller — Traefik из состава k3s.
- На UBU01: `~/.kube/config` (права 600), установлен `helm`.

### Откат

С UBU01 (если кластер в нерабочем состоянии, сначала снимаем ВМ): выключите `k8s-cp01`, `k8s-w01`, `k8s-w02`.

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-sysadmin-079
```

Повторите для `k8s-w01` и `k8s-w02` (в GUI: VM → Snapshot → Snapshot Manager → выбрать `before-sysadmin-079` → Go To). Если снапшот недоступен, а нужно «с нуля» — на сервере `sudo /usr/local/bin/k3s-uninstall.sh`, на агентах `sudo /usr/local/bin/k3s-agent-uninstall.sh`. DNS-запись на DC01 удаляется через `Remove-DnsServerResourceRecord -Name portal -ZoneName corp.vektor.lab -RRType A`.

## 6. Ошибки новичков и безопасность

1. **Ставить k3s с `write-kubeconfig-mode 0644` и раздавать kubeconfig «чтобы коллеги посмотрели».** → kubeconfig = полный доступ к кластеру. → правильно: `0640`, отдельные ServiceAccount и RBAC-роли на namespace для каждого приложения.
2. **Работать только под `cluster-admin`.** → одна опечатка в `kubectl delete -A` роняет всё. → выдавать админам отдельные учётки, а для повседневных задач — роль на namespace.
3. **Использовать тег `latest`.** → `ImagePullPolicy: Always` + перезапуск пода = внезапно другая версия приложения. → фиксировать конкретный тег, а лучше digest.
4. **Считать local-path надёжным хранилищем.** → узел умер — данные недоступны, под на другом узле не смонтирует PVC. → для продакшена CSI с репликацией (Longhorn, Ceph RBD, NFS) [ПРОВЕРИТЬ совместимость с вашей версией], а local-path — только под кэш и тесты.
5. **Удалять namespace «на всякий случай».** → вместе с ним уходят PVC и, для local-path, каталоги с данными. → сначала `kubectl get all,pvc -n <ns>`, бэкап, потом удаление.
6. **Не задавать requests/limits.** → один под съедает RAM узла, kubelet начинает убивать соседей (OOMKilled). → запросы и лимиты на каждый контейнер с первого дня.
7. **Копировать Secret в git в открытом виде.** → утечка паролей. → Sealed Secrets/SOPS или внешний Vault (см. урок 75); в кластере — шифрование etcd at rest (`--secrets-encryption` в k3s) [ПРОВЕРИТЬ флаг для вашей версии].
8. **Выставлять NodePort/ingress в интернет без TLS и аутентификации.** → сервис доступен всему миру. → TLS-терминация на ingress, аутентификация на уровне приложения или forward-auth, ограничение по IP на уровне GW01/nftables.

**Безопасность отдельно.** На стенде мы разрешаем HTTP без TLS, отключаем swap, ставим кластер на учебные пароли — в боевой среде так нельзя: ingress только по HTTPS с валидным (в РФ — часто отечественным) сертификатом, API-сервер за VPN/бастионом, аудит-логи включены, образы из доверенного реестра (Harbor) с подписью, регулярные обновления узлов. И помните: в Kubernetes **нет** привычной границы «сервер» — под из одного namespace может достучаться до другого, если не настроены NetworkPolicy. По умолчанию сеть плоская и открытая.

## 7. Вопросы на собеседовании

**1. Чем k3s отличается от kubeadm и что вы выберете для филиала на 3 серверах?**

Ответ: kubeadm — установщик «чистого» upstream Kubernetes: вы отдельно ставите containerd, kubelet, kubeadm, выбираете CNI, etcd, ingress. k3s — один бинарник с уже встроенными containerd, flannel, Traefik, local-path, metrics-server и SQLite вместо etcd (для HA — embedded etcd), плюс serviceLB. Для филиала/тестового контура k3s быстрее и проще в обслуживании. Для продакшена с требованиями к сертифицированной сборке и внешней поддержке — managed Kubernetes или kubeadm/RKE2. Ключевое: k3s — это тот же API Kubernetes, просто другой набор компонентов по умолчанию.

*Что хочет услышать:* понимание, что API одинаковый, разница в батарейках и в эксплуатационной цене, а не «k3s — это игрушка».

*Красный флаг:* «k3s — обрезанный Kubernetes, не для продакшена» без аргументов, или путаница «k3s — это дистрибутив Docker».

**2. Что происходит по шагам, когда вы делаете `kubectl apply -f deployment.yaml`?**

Ответ: kubectl валидирует YAML и отправляет его в kube-apiserver (6443) с аутентификацией по сертификату; apiserver проверяет RBAC и admission-контроллеры, сохраняет объект в etcd. Deployment-контроллер видит новый объект и создаёт ReplicaSet, тот — Pod'ы (объекты в статусе Pending, без узла). Scheduler выбирает узлы по ресурсам и ограничениям и проставляет `nodeName`. Kubelet на узле видит назначенный под, через containerd тянет образ и запускает контейнеры, затем репортит статус обратно в apiserver. kube-proxy обновляет правила, чтобы Service указывал на новый под.

*Что хочет услышать:* порядок «apiserver → etcd → контроллер → scheduler → kubelet → containerd» и понимание, что все решения принимает apiserver.

*Красный флаг:* «kubectl сам запускает контейнер на сервере», «scheduler запускает поды».

**3. Как трафик от пользователя доходит до пода? Чем Service отличается от Ingress?**

Ответ: Service — это L4-абстракция: стабильный ClusterIP и набор endpoints, трафик распределяет kube-proxy по правилам iptables/IPVS. Типы: ClusterIP (внутри кластера), NodePort (порт на каждом узле, 30000–32767), LoadBalancer (внешний балансировщик, в k3s — svclb/klipper-lb). Ingress — L7-правило маршрутизации по хосту и пути, работает только при наличии ingress controller (Traefik/nginx-ingress), который сам опубликован через Service. Путь: DNS → IP узла:80 → Traefik → подбор правила по Host → Service → ClusterIP → под.

*Что хочет услышать:* разделение L4/L7, роль controller и понимание, что Ingress без controller — просто YAML.

*Красный флаг:* «Ingress балансирует нагрузку по подам» (балансирует Service), «NodePort нужен для внутреннего трафика».

**4. Под в статусе `Pending` / `CrashLoopBackOff`. Ваши действия?**

Ответ: Pending — планировщик не смог разместить под: нет свободных ресурсов (requests больше свободного), taint на узлах, несовпадение nodeSelector/affinity, PVC не привязан. Смотрим `kubectl describe pod` и блок Events (`FailedScheduling` с причиной), `kubectl describe node | grep -A5 Allocated`, `kubectl get pvc`. CrashLoopBackOff — контейнер запускается и падает: `kubectl logs <pod>` и `--previous`, `describe` на предмет `OOMKilled`/`Liveness probe failed`, проверка конфигурации и переменных окружения. Отдельно — `ImagePullBackOff` (нет доступа к реестру, опечатка в имени образа, истёкший pull secret).

*Что хочет услышать:* методичный разбор по Events и логам, различение «не смогли запустить» и «запустили, но падает».

*Красный флаг:* «перезапущу Deployment, вдруг поможет», без чтения describe и логов.

**5. Чем PV отличается от PVC, зачем StorageClass и почему local-path не даёт отказоустойчивости?**

Ответ: PV — реальный ресурс хранения (диск, NFS-экспорт, том CSI). PVC — заявка от приложения на объём и режим доступа; Kubernetes связывает заявку с подходящим PV. StorageClass описывает, *как* создавать PV динамически (какой provisioner, какие параметры). В k3s по умолчанию `local-path`: он создаёт каталог на том узле, куда сел под. Данные привязаны к узлу, репликации нет, `ReadWriteOnce` — при переезде пода на другой узел PVC не смонтируется. Для отказоустойчивости нужны CSI-драйверы с репликацией или сетевое хранилище, и тогда один и тот же том доступен пересозданному поду на другом узле.

*Что хочет услышать:* понимание динамического провижининга и явное признание ограничений локального тома.

*Красный флаг:* «PVC хранит данные», «local-path подходит для базы 1С в продакшене».

## 8. Источники (официальная документация)

- **kubernetes.io** — разделы *Concepts* («Cluster Architecture», «Pods», «Deployments», «Services, Load Balancing, and Networking», «Ingress», «Storage»), *Tasks* («Configure a Pod to Use a PersistentVolume for Storage», «Troubleshooting Applications»), *Reference* («kubectl»). Поисковые фразы: `kubernetes cluster architecture`, `kubernetes ingress concepts`, `kubernetes persistent volumes task`, `kubectl cheat sheet`.
- **k3s.io** — *Documentation*: «Quick-Start», «Installation Requirements», «Configuration Options», «Air-Gap Install», «Managing Packaged Components». Поисковые фразы: `k3s quick start`, `k3s configuration options config.yaml`, `k3s air-gap install`.
- **helm.sh** — *Docs*: «Using Helm», «Charts», «Helm Commands: upgrade, rollback, history». Поисковые фразы: `helm create chart`, `helm rollback release`, `helm upgrade install`.
- **traefik.io** — *Docs*: Kubernetes Ingress provider, Traefik IngressRoute. Поиск: `traefik kubernetes ingress provider`. `[ПРОВЕРИТЬ]` актуальный URL Helm-репозитория Traefik — сверяйтесь на официальном сайте перед `helm repo add`.
- **github.com/rancher/local-path-provisioner** — README: как работает и какие ограничения у local-path. Поиск: `local path provisioner limitations`.
- **containerd.io** и **github.com/kubernetes-sigs/cri-tools** — `crictl` команды для диагностики контейнеров на узле. Поиск: `crictl usage kubernetes`.
- **learn.microsoft.com** — обзор Kubernetes для администраторов Windows-инфраструктуры (полезно для понимания managed-вариантов): раздел *Azure Kubernetes Service — Concepts*. Поиск: `AKS core concepts`. `[ПРОВЕРИТЬ]` названия разделов — интерфейс документации периодически меняется.
