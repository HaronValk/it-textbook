# Архитектура Kubernetes: control plane, etcd, kubelet, kube-proxy, CRI/CNI/CSI

## 1. Введение (зачем это нужно в реальной работе)

Тикет в трекере «Вектора»:

> **Пользователь:** ivanov
> **ВМ:** k8s-cp01
> **Что написал:** «Поднял тестовый кластер по твоей инструкции. `kubectl get nodes` показывает `NotReady`, поды CoreDNS висят в `Pending`. Почему не работает, если я всё сделал по шагам?»
> **Срочность:** средняя

Такие тикеты — хлеб DevOps-инженера. Дальше будет ещё интереснее: «после перезагрузки ноды `kubectl` не отвечает», «поды не стартуют, в событиях `FailedMount`», «сервис не пингуется из другого пода». И в 90% случаев быстрый ответ даёт не чтение документации, а понимание, **кто в кластере за что отвечает и по какому проводу с кем говорит**.

Kubernetes — это не «программа», а набор демонов, которые видят друг друга только через API и через файлы сокетов. Если ты знаешь, что `kubelet` не создаёт поды, а только исполняет приказы; что `etcd` — единственный источник правды; что без CNI нода не станет `Ready` физически, потому что подам некуда выдать IP, — ты диагностируешь за минуты, а не за часы.

После этой главы ты должен уметь: назвать все компоненты control plane и ноды, объяснить путь запроса `kubectl apply` до запущенного контейнера, показать порты, на которых всё слушает, найти статические поды control plane на диске, заглянуть в etcd через `etcdctl`, объяснить разницу между CRI, CNI и CSI и почему это три разных интерфейса, а не один.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Kubernetes построен на **декларативной модели и control loop**. Ты пишешь в API «хочу 3 реплики», система хранит это желаемое состояние и бесконечно сравнивает с фактическим, дотягивая одно до другого. Всё общение — по HTTPS/gRPC, почти всё — через `kube-apiserver`.

**Control plane (управляющий слой):**

- **kube-apiserver** — единственная точка входа в кластер. REST API + watch. Делает аутентификацию (кто ты), авторизацию (RBAC, можно ли тебе), admission-плагины (валидация/мутация объектов), и только потом пишет в etcd. Никто, кроме apiserver, в etcd не пишет. Порт **6443/TCP**.
- **etcd** — распределённое key-value хранилище, источник правды. Все объекты Kubernetes лежат там под ключами `/registry/...`. Использует алгоритм консенсуса **Raft**: нужен кворум `(n/2)+1`, поэтому etcd-узлов делают 1, 3 или 5, но не 2 и не 4. Порты **2379/TCP** (клиентский) и **2380/TCP** (между членами кластера).
- **kube-scheduler** — решает, на какую ноду поедет под. Два этапа: фильтрация (что вообще подходит по ресурсам, taint'ам, affinity) и scoring (что лучше). Порт **10259/TCP**.
- **kube-controller-manager** — набор контроллеров в одном процессе: node, deployment, replicaset, endpoints, serviceaccount, job и другие. Каждый крутит свой цикл «сравнил — поправил». Порт **10257/TCP**.
- **cloud-controller-manager** — опционально, если кластер в облаке (балансировщики, маршруты).

**Слой ноды (data plane):**

- **kubelet** — агент на каждой ноде. Не создаёт поды сам: он watch'ит apiserver, видит поды, назначенные *его* ноде, и приводит контейнеры в соответствие. Отвечает за пробы (liveness/readiness), монтирование томов, отчёт о состоянии ноды. API на **10250/TCP**. Отдельно умеет читать **статические поды** из `/etc/kubernetes/manifests/` — так kubeadm запускает сам control plane (курица и яйцо: apiserver ещё не работает, а запустить его надо).
- **container runtime** — `containerd` (или CRI-O). Реально дёргает namespaces/cgroups, качает образы, запускает контейнеры.
- **kube-proxy** — реализует объекты `Service`: пишет правила iptables/IPVS/nftables, чтобы трафик на виртуальный IP сервиса попал в один из подов. Метрики/healthz — порты **10249/10256/TCP** `[ПРОВЕРИТЬ актуальные значения]`.
- **CNI-плагин** (Calico, Cilium, Flannel) — демон на ноде, который выдаёт подам IP и настраивает маршрутизацию.

**Три интерфейса, которые надо различать:**

| Интерфейс | Что стандартизирует | Кто вызывает | Где конфиг |
|---|---|---|---|
| **CRI** (Container Runtime Interface) | запуск контейнеров: gRPC между kubelet и runtime | kubelet | `/etc/containerd/config.toml`, сокет `/run/containerd/containerd.sock` |
| **CNI** (Container Network Interface) | выдать поду IP и сеть | container runtime по просьбе kubelet | `/etc/cni/net.d/*.conf`, бинарники в `/opt/cni/bin` |
| **CSI** (Container Storage Interface) | подключить/смонтировать том | kubelet (Node-сервис) и контроллеры в кластере (Controller-сервис) | объекты `CSIDriver`, `StorageClass`, external-provisioner/attacher |

Смысл один: вынести «вендорскую» часть за стабильный gRPC-контракт. До CRI kubelet знал про Docker напрямую, и каждая смена рантайма ломала код.

```text
                    ┌──────────────────── CONTROL PLANE ────────────────────┐
 kubectl ──HTTPS──► │ kube-apiserver :6443                                   │
 (6443)             │   authn → authz(RBAC) → admission → запись             │
                    │        │                            ▲                  │
                    │        ▼                            │ watch            │
                    │   etcd :2379/:2380  ◄── только apiserver пишет        │
                    │        ▲                                               │
                    │        │ читают                                       │
                    │  kube-scheduler :10259   kube-controller-manager :10257│
                    └────────────────────────┬───────────────────────────────┘
                                             │ HTTPS/watch (kubelet тянет сам)
                    ┌────────────────────────▼───── НОДА ────────────────────┐
                    │ kubelet :10250                                         │
                    │   ├─ CRI ──► containerd ──► runc ──► контейнеры        │
                    │   ├─ CNI ──► /opt/cni/bin + /etc/cni/net.d (IP подам)  │
                    │   ├─ CSI ──► монтирование томов                        │
                    │   └─ статические поды: /etc/kubernetes/manifests       │
                    │ kube-proxy ──► iptables/IPVS ──► Service VIP → Pod     │
                    └────────────────────────────────────────────────────────┘
```

Важная деталь: kubelet **сам** открывает соединение к apiserver и держит watch. Не apiserver приходит на ноду — это снимает кучу проблем с NAT и файрволами.

## 3. Аналогия из жизни

Представь ресторан.

**Стойка администратора** — это `kube-apiserver`. Единственный вход для гостей; мимо неё ни заказ не принять, ни узнать статус. **Книга заказов** за стойкой — `etcd`: если запись туда не попала, заказа не существует, сколько бы повар ни старался. **Метрдотель** — `kube-scheduler`: смотрит, какие столики (ноды) свободны, и рассаживает гостей. **Менеджер зала** — `kube-controller-manager`: ходит и сверяет, что каждый заказ из книги реально выполняется, а если нет — поднимает шум.

На кухне — **повар**, `kubelet`. Он не придумывает блюда: он берёт из книги то, что метрдотель назначил его кухне, и готовит. **Плита с универсальным разъёмом** — это CRI: повару всё равно, газовая она или индукционная, лишь бы разъём подходил. **Водопровод и разводка по залу** — CNI: без него блюдо некуда нести, гость не найдёт свой столик. **Склад с холодильниками** — CSI: интерфейс, по которому привозят и выдают продукты. **Швейцар у входа** — `kube-proxy`: гость назвал номер столика (VIP сервиса), а швейцар знает, в какую дверь его проводить.

Аналогия ломается здесь: повар в ресторане сам решает, что и как готовить, а `kubelet` не решает ничего — он исполняет ровно то, что уже записано в etcd и назначено его ноде.

## 4. Практика и команды

**1. Кто вообще есть в control plane**

```bash
kubectl get pods -n kube-system -o wide
```
```
NAME                              READY   STATUS    NODE
etcd-k8s-cp01                     1/1     Running   k8s-cp01
kube-apiserver-k8s-cp01           1/1     Running   k8s-cp01
kube-controller-manager-k8s-cp01  1/1     Running   k8s-cp01
kube-scheduler-k8s-cp01           1/1     Running   k8s-cp01
kube-proxy-xxxxx                  1/1     Running   k8s-cp01
coredns-yyyyy                     0/1     Pending   <none>
```
Суффикс `-k8s-cp01` в имени — признак **статического пода** (mirror pod). `Pending` у CoreDNS без ноды — почти всегда отсутствие CNI.

**2. Проверить здоровье API**

```bash
kubectl get --raw='/readyz?verbose'
kubectl cluster-info
```
`readyz` отвечает `ok` по каждому блоку (`etcd`, `poststarthook/...`). Если etcd недоступен — увидишь `[-]etcd failed`. Это первое, что смотрят при «кластер лежит».

`kubectl get componentstatuses` (сокращённо `cs`) — **устаревшая** команда, в новых версиях может вообще не отдавать данные. Не используй как основной инструмент.

**3. Порты, которые реально слушаются**

```bash
sudo ss -tlnp | grep -E '6443|2379|2380|10250|10257|10259'
```
```
LISTEN 0 4096 *:6443  *:*  users:(("kube-apiserver",pid=...))
LISTEN 0 4096 *:2379  *:*  users:(("etcd",pid=...))
LISTEN 0 4096 *:2380  *:*  users:(("etcd",pid=...))
LISTEN 0 4096 *:10250 *:*  users:(("kubelet",pid=...))
```
Смотреть: 6443 наружу — да; 2379/2380 наружу — **нет, только на localhost или между etcd-узлами**. Это базовая гигиена.

**4. Что видит kubelet через CRI**

```bash
sudo crictl info
sudo crictl ps -a
sudo crictl pods
sudo crictl logs <container-id> --tail=30
```
`crictl info` показывает `config.sandboxImage`, `runtimeName`, `cgroupDriver`. Классическая ошибка видна сразу: `cgroupDriver: cgroupfs` при systemd-хосте.

**5. Что видит containerd целиком (не только CRI)**

```bash
sudo ctr -n k8s.io containers ls
sudo ctr -n k8s.io images ls | head
```
`ctr` показывает всё содержимое namespace `k8s.io`. `crictl` — только то, что создано через CRI. Разница важна при поиске «контейнера-призрака», который жрёт диск.

**6. Логи kubelet и его конфиг**

```bash
sudo systemctl status kubelet
sudo journalctl -u kubelet -n 80 --no-pager
sudo cat /var/lib/kubelet/config.yaml
sudo systemctl cat kubelet   # видно drop-in 10-kubeadm.conf
```
В `config.yaml` важны `cgroupDriver: systemd`, `clusterDNS`, `staticPodPath: /etc/kubernetes/manifests`, `authentication.x509.clientCAFile`.

**7. Статические поды — на диске**

```bash
ls -l /etc/kubernetes/manifests/
sudo grep -E 'image:|command:' /etc/kubernetes/manifests/etcd.yaml
```
Четыре yaml-файла: `etcd`, `kube-apiserver`, `kube-controller-manager`, `kube-scheduler`. kubelet следит за каталогом: файл появился — под создан, файл убрали — под удалён.

**8. etcd: состав кластера и его состояние**

```bash
sudo kubectl -n kube-system exec -it etcd-k8s-cp01 -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  endpoint status --write-out=table
```
```
+---------------------------+------------------+---------+---------+-----------+...
|         ENDPOINT          |        ID        | VERSION | DB SIZE | IS LEADER |
| https://127.0.0.1:2379    | 8e9e05c52164694d | 3.5.x   |   25 MB |      true |
```
Смотреть: `IS LEADER`, `DB SIZE` (растёт — значит нет компакции, урок 46), `RAFT INDEX`.

**9. Где Kubernetes хранит объекты**

```bash
sudo kubectl -n kube-system exec -it etcd-k8s-cp01 -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/ --prefix --keys-only | head -n 10
```
```
/registry/services/endpoints/kube-system/kube-dns
/registry/services/specs/default/kubernetes
/registry/pods/kube-system/etcd-k8s-cp01
```
Вот и «книга заказов». Руками здесь ничего не править — только читать.

**10. kube-proxy: что он сделал с iptables**

```bash
kubectl -n kube-system get ds kube-proxy -o wide
kubectl -n kube-system logs ds/kube-proxy --tail=20
kubectl -n kube-system get cm kube-proxy -o yaml | grep -A3 -i mode
sudo iptables-save | grep -c KUBE
```
Если `grep -c KUBE` = 0 при работающих сервисах — kube-proxy не в iptables-режиме (например, IPVS или nftables). Проверяй режим, прежде чем искать правила.

**11. CNI на ноде**

```bash
ls -l /etc/cni/net.d/ /opt/cni/bin/ | head -n 20
kubectl describe node k8s-cp01 | grep -A6 Conditions
```
Пустой `/etc/cni/net.d` → в Conditions будет `Ready False`, `KubeletNotReady` с текстом `Network plugin returns error: cni plugin not initialized`.

**12. CSI: драйверы и классы хранения**

```bash
kubectl get csidrivers
kubectl get storageclass
kubectl get volumeattachments
```
В пустом кластере — пусто. Так и должно быть: CSI-драйвер ставится отдельно (урок 39).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **k8s-cp01** — Ubuntu Server 24.04, `10.10.10.61`, SSH, sudo, 2 vCPU / 4 ГБ RAM / 40 ГБ диск (минимум для control plane).
- **k8s-w01** (`10.10.10.62`) и **k8s-w02** (`10.10.10.63`) — существуют и доступны по SSH, но в этой лабе не меняются. Их подключим в уроке 34.
- **HARBOR01** (`10.10.10.55`) из урока 31 — желателен как pull-through cache образов `registry.k8s.io`. Если его нет или доступ к `registry.k8s.io` из твоей сети закрыт — используй зеркало своего провайдера `[ПРОВЕРИТЬ доступность]`.
- Кластера ещё нет: это первая лаба модуля. Полноценный кластер с рабочими нодами и CNI соберём в уроке 34; сейчас поднимаем **только control plane**, чтобы посмотреть на компоненты живьём.

### Снапшот

На **k8s-cp01** (k8s-w01/w02 не трогаем):

VMware Workstation → вкладка нужной ВМ → **VM → Snapshot → Take Snapshot…** → имя `before-devops-033` → Take Snapshot.

Если ВМ запущена — VMware предупредит, что снапшот включает состояние памяти; для экономии места можно снять снапшот при выключенной ВМ.

### Шаги

1. **k8s-cp01.** Проверяем себя и соседей:
   ```bash
   hostnamectl; ip -br a; ping -c2 10.10.10.62; ping -c2 10.10.10.63
   ```
   *Ожидаемо:* hostname `k8s-cp01`, один адаптер с `10.10.10.61/24`, пинги идут.

2. **k8s-cp01.** Добавляем имена в `/etc/hosts` (пригодится для `--control-plane-endpoint`):
   ```bash
   echo "10.10.10.61 k8s-cp01.corp.vektor.lab k8s-cp01" | sudo tee -a /etc/hosts
   echo "10.10.10.62 k8s-w01.corp.vektor.lab k8s-w01"  | sudo tee -a /etc/hosts
   echo "10.10.10.63 k8s-w02.corp.vektor.lab k8s-w02"  | sudo tee -a /etc/hosts
   getent hosts k8s-cp01.corp.vektor.lab
   ```
   *Ожидаемо:* команда `getent` вернула строку с `10.10.10.61`.

3. **k8s-cp01.** Отключаем swap, включаем модули ядра и sysctl:
   ```bash
   sudo swapoff -a
   sudo sed -i '/ swap / s/^/#/' /etc/fstab
   printf 'overlay\nbr_netfilter\n' | sudo tee /etc/modules-load.d/k8s.conf
   sudo modprobe overlay && sudo modprobe br_netfilter
   printf 'net.bridge.bridge-nf-call-iptables=1\nnet.bridge.bridge-nf-call-ip6tables=1\nnet.ipv4.ip_forward=1\n' | sudo tee /etc/sysctl.d/99-kubernetes.conf
   sudo sysctl --system | grep -E 'bridge-nf-call-iptables|ip_forward'
   free -h
   ```
   *Ожидаемо:* `free` показывает `Swap: 0`, sysctl вывел три значения = 1.

4. **k8s-cp01.** Ставим containerd и правим конфиг:
   ```bash
   sudo apt-get update && sudo apt-get install -y containerd
   sudo mkdir -p /etc/containerd
   containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
   sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
   grep -n 'SystemdCgroup\|sandbox_image' /etc/containerd/config.toml
   sudo systemctl enable --now containerd && systemctl is-active containerd
   ```
   *Ожидаемо:* `SystemdCgroup = true`, `sandbox_image` указывает на pause-образ. Запиши его имя — оно должно совпадать с тем, что покажет `kubeadm config images list` на шаге 7.

5. **k8s-cp01.** Ставим `crictl` и проверяем CRI:
   ```bash
   CRICTL_VERSION=v1.31.1   # [ПРОВЕРИТЬ] актуальную версию на github.com/kubernetes-sigs/cri-tools/releases
   wget -q https://github.com/kubernetes-sigs/cri-tools/releases/download/${CRICTL_VERSION}/crictl-${CRICTL_VERSION}-linux-amd64.tar.gz
   sudo tar zxf crictl-${CRICTL_VERSION}-linux-amd64.tar.gz -C /usr/local/bin
   printf 'runtime-endpoint: unix:///run/containerd/containerd.sock\nimage-endpoint: unix:///run/containerd/containerd.sock\ntimeout: 10\ndebug: false\n' | sudo tee /etc/crictl.yaml
   sudo crictl info | head -n 20
   ```
   *Ожидаемо:* JSON с `"runtimeName": "containerd"` и `config.cgroupDriver: "systemd"`. Если тут `cgroupfs` — вернись к шагу 4.

6. **k8s-cp01.** Подключаем репозиторий Kubernetes и ставим пакеты:
   ```bash
   sudo apt-get install -y apt-transport-https ca-certificates curl gpg
   curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.31/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
   echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.31/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list
   sudo apt-get update
   sudo apt-get install -y kubelet kubeadm kubectl
   sudo apt-mark hold kubelet kubeadm kubectl
   kubeadm version -o short
   ```
   *Ожидаемо:* версия вида `v1.31.x` `[ПРОВЕРИТЬ актуальную стабильную ветку]`. `apt-mark hold` обязателен — иначе `apt upgrade` однажды рассинхронит версии.

7. **k8s-cp01.** Смотрим, какие образы нужны, и качаем их:
   ```bash
   sudo kubeadm config images list
   sudo kubeadm config images pull
   sudo crictl images | wc -l
   ```
   *Ожидаемо:* список из 7–8 образов (`kube-apiserver`, `kube-controller-manager`, `kube-scheduler`, `kube-proxy`, `etcd`, `pause`, `coredns`), затем столько же строк в `crictl images`. Если `registry.k8s.io` недоступен — добавь `--image-repository=10.10.10.55/<проект-в-harbor>` `[ПРОВЕРИТЬ имя проекта в Harbor01]`.

8. **k8s-cp01.** Инициализируем control plane:
   ```bash
   sudo kubeadm init \
     --apiserver-advertise-address=10.10.10.61 \
     --control-plane-endpoint=k8s-cp01.corp.vektor.lab \
     --pod-network-cidr=10.244.0.0/16 \
     --cri-socket=unix:///run/containerd/containerd.sock
   ```
   *Ожидаемо:* в конце `Your Kubernetes control-plane has initialized successfully!` и блок команд для kubeconfig. Пул `10.244.0.0/16` не пересекается с `10.10.10.0/24` и `10.10.20.0/24` — в уроке 37 настроим CNI именно под него.

9. **k8s-cp01.** Настраиваем `kubectl` для своего пользователя:
   ```bash
   mkdir -p $HOME/.kube
   sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
   sudo chown $(id -u):$(id -g) $HOME/.kube/config
   kubectl get --raw='/readyz?verbose'
   ```
   *Ожидаемо:* `readyz` возвращает `ok` по каждому блоку. Файл `/etc/kubernetes/admin.conf` — это **кластерный админ**, обращайся с ним как с root-паролём.

10. **k8s-cp01.** Смотрим на архитектуру живьём:
    ```bash
    kubectl get pods -n kube-system -o wide
    ls -l /etc/kubernetes/manifests/
    sudo crictl pods
    sudo ss -tlnp | grep -E '6443|2379|2380|10250|10257|10259'
    ```
    *Ожидаемо:* 4 статических пода `Running`, `kube-proxy-*` `Running`; 4 yaml в manifests; в `ss` — все шесть портов.

11. **k8s-cp01.** Заглядываем в etcd (команда из раздела 4, пункт 8) и убеждаемся, что объекты лежат под `/registry/`.
    *Ожидаемо:* `member list` — один член, `IS LEADER true`; `get /registry/ --prefix --keys-only` отдаёт ключи.

12. **k8s-cp01.** Убеждаемся, что нода `NotReady` — и понимаем почему:
    ```bash
    kubectl get nodes
    kubectl describe node k8s-cp01 | grep -A8 Conditions
    ls -l /etc/cni/net.d/
    kubectl get pods -n kube-system | grep coredns
    ```
    *Ожидаемо:* `NotReady`, причина `KubeletNotReady ... cni plugin not initialized`, каталог `/etc/cni/net.d` пуст, CoreDNS в `Pending`. **Это нормально для данного урока** — CNI ставится в уроке 37.

13. **k8s-cp01.** Записываем в `stand-log.md` (на своей рабочей машине или в Git-репозитории стенда):
    ```markdown
    ## Урок devops-033 (архитектура Kubernetes)
    - k8s-cp01 (10.10.10.61): swap off, модули overlay/br_netfilter, sysctl 99-kubernetes.conf
    - containerd + SystemdCgroup=true, /etc/crictl.yaml
    - kubelet/kubeadm/kubectl v1.31.x (apt-mark hold)
    - kubeadm init: control-plane-endpoint k8s-cp01.corp.vektor.lab, pod-network-cidr 10.244.0.0/16
    - kubeconfig: /home/<user>/.kube/config (из admin.conf)
    - Нода NotReady, CNI отсутствует — ожидаемо, ставим в уроке 037
    ```

### Проверка

- [ ] `systemctl is-active containerd` → `active`, `crictl info` показывает `cgroupDriver: systemd`
- [ ] `kubeadm version -o short` и `kubectl version --client` — одна ветка, пакеты на `hold`
- [ ] `kubectl get --raw='/readyz?verbose'` — все блоки `ok`
- [ ] `kubectl get pods -n kube-system` — 4 статических пода и kube-proxy в `Running`
- [ ] `ls /etc/kubernetes/manifests/` — ровно 4 файла
- [ ] `ss -tlnp` показывает 6443, 2379, 2380, 10250 (и 10257/10259 на localhost)
- [ ] `etcdctl member list` отдаёт одного члена, `IS LEADER true`
- [ ] `etcdctl get /registry/ --prefix --keys-only` возвращает ключи
- [ ] `kubectl get nodes` — `NotReady` с причиной «cni plugin not initialized», и ты можешь это объяснить
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** k8s-cp01
> **Что написал:** «После перезагрузки сервера `kubectl get nodes` выдаёт `The connection to the server k8s-cp01.corp.vektor.lab:6443 was refused - did you specify the right host or port?`. Кластер умер, помоги»
> **Срочность:** высокая

**Воспроизводим поломку** (на снапшоте `before-devops-033` уже есть рабочая лаба, так что сначала сделай новый снапшот вручную, например `before-devops-033-break`):

```bash
sudo systemctl stop containerd
sudo systemctl status containerd --no-pager
```

**Диагностика по порядку:**

1. `sudo systemctl status kubelet --no-pager` → kubelet `active (running)`, но в логах мусор. Значит проблема не в самом kubelet.
2. `sudo journalctl -u kubelet -n 100 --no-pager | grep -i -E 'runtime|containerd|refused'` → `failed to get container runtime status: ... connect: connection refused`.
3. `sudo crictl ps` → `connection refused` на `/run/containerd/containerd.sock`. CRI мёртв.
4. `sudo systemctl status containerd --no-pager` → `inactive (dead)`.
5. `sudo journalctl -u containerd -n 50 --no-pager` → либо чисто (просто остановили), либо ошибка парсинга `config.toml` — тогда смотри, что правил последним.
6. Вывод, который надо проговорить: **control plane сам живёт в контейнерах**. Упал runtime — упали apiserver, etcd, scheduler, controller-manager. Именно поэтому `kubectl` не может подключиться.

**Решение:**

```bash
sudo systemctl start containerd
sleep 20
sudo crictl ps
kubectl get pods -n kube-system
kubectl get --raw='/readyz?verbose'
```
kubelet перезапускать обычно не нужно — он сам переподключится к сокету и пересоздаст статические поды. Если через минуту не поднялось: `sudo systemctl restart kubelet`.

**Второй вариант поломки** (чтобы увидеть механизм статических подов): `sudo mv /etc/kubernetes/manifests/etcd.yaml /root/` — etcd-под исчезнет, apiserver начнёт отдавать ошибки про storage, `readyz` покажет `[-]etcd failed`. Лечится возвратом файла на место.

**Ответ пользователю:**
> Иван, привет. Причина — остановился `containerd` на k8s-cp01. Компоненты control plane (apiserver, etcd, scheduler) в Kubernetes сами работают как контейнеры, поэтому без runtime кластер недоступен целиком, а не частично. Сейчас всё поднято, `readyz` зелёный, нода в прежнем состоянии (`NotReady` — это ожидаемо, CNI поставим в уроке 37). Если повторится — сначала `systemctl status containerd` и `journalctl -u containerd`.

**Закрытие заявки:** в комментарии указать причину, действие и что проверил после восстановления (список из чек-листа).

### Что изменилось на стенде

- **k8s-cp01** (`10.10.10.61`): swap отключён и закомментирован в `/etc/fstab`; модули `overlay`, `br_netfilter` в `/etc/modules-load.d/k8s.conf`; `/etc/sysctl.d/99-kubernetes.conf`; установлен и включён `containerd` с `SystemdCgroup = true`; `/etc/crictl.yaml`; установлены `kubelet`, `kubeadm`, `kubectl` (на `apt-mark hold`); в `/etc/hosts` добавлены `k8s-cp01`, `k8s-w01`, `k8s-w02` с FQDN в `corp.vektor.lab`.
- Поднят **одиночный control plane**: `/etc/kubernetes/` (манифесты, PKI, kubeconfig'и), `/var/lib/etcd/`, `/var/lib/kubelet/`, CNI ещё нет.
- Пул подов: `10.244.0.0/16` (зафиксировать — от него зависит настройка CNI в уроке 37).
- **k8s-w01**, **k8s-w02** — без изменений, ждут урока 34.
- Нода `k8s-cp01` в статусе `NotReady` по причине отсутствия CNI — это осознанное состояние стенда.

### Откат

1. **Через VMware (предпочтительно):** выключить `k8s-cp01` → **VM → Snapshot → Snapshot Manager** → выбрать `before-devops-033` → **Go to**. ВМ вернётся ровно в состояние до лабы.
2. **Через kubeadm, если снапшота нет:**
   ```bash
   sudo kubeadm reset -f --cri-socket unix:///run/containerd/containerd.sock
   rm -rf $HOME/.kube
   sudo rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet /etc/cni/net.d
   sudo systemctl restart containerd kubelet
   ```
   После этого стенд снова «чистая Ubuntu с containerd», и урок 34 можно начинать с нуля.

## 6. Ошибки новичков и безопасность

1. **«Кластер сломался» из-за `NotReady`.** → На самом деле не установлен CNI, и это штатное состояние. → Проверяй `kubectl describe node` и `ls /etc/cni/net.d` прежде чем переустанавливать кластер.
2. **Правка etcd руками (`etcdctl put`).** → Ломается инвариант: apiserver не узнает об изменении, объект рассинхронизируется, а восстановление — из бэкапа. → etcd только читаем; изменения — только через API.
3. **Чётное число etcd-узлов или etcd на NFS/медленном диске.** → Кворум не собирается или Raft уходит в бесконечные выборы, кластер «замирает». → 1/3/5 узлов, локальный SSD, отдельный диск, мониторинг латентности.
4. **`apt upgrade` без `hold` на kubelet/kubeadm/kubectl.** → Версии расходятся, kubelet может оказаться новее apiserver — это запрещено (kubelet не должен быть новее control plane, допустимое отставание — несколько минорных версий `[ПРОВЕРИТЬ актуальную skew policy]`). → Пиннинг версий и обновление по плану (урок 46).
5. **Ручные правки `config.toml` и `/var/lib/kubelet/config.yaml` без версионирования.** → Через месяц никто не помнит, почему на одной ноде другой `cgroupDriver`, и нода ведёт себя иначе. → Всё в Git (Ansible/cloud-init), дрейф исключён.
6. **`--image-repository` и образы «с интернета» без проверки.** → Молча прилетает чужой образ. → Свой Harbor с pull-through cache и подписью Cosign (уроки 31–32).
7. **Отключение проверок «чтобы заработало».** → `--anonymous-auth=true` на kubelet или открытый 2379 = полный доступ к кластеру у любого в сети. → Не трогать дефолты безопасности, а если трогаешь — фиксировать в `stand-log.md` и возвращать обратно.
8. **`kubectl` под `root` с `admin.conf`.** → Любая опечатка в `delete` бьёт по всему кластеру без ограничений. → Отдельные kubeconfig'и и RBAC-роли (урок 41); `admin.conf` — только для аварийных операций.

**Безопасность (что нельзя в боевой среде):**

- etcd-клиентский сертификат = доступ ко **всем** секретам кластера в открытом виде (по умолчанию Secret в etcd не шифруется, только base64). В продакшене включают encryption at rest и ограничивают доступ к `/etc/kubernetes/pki/etcd/`.
- Порты 2379/2380, 10250, 6443 не должны смотреть в интернет. В нашей лабе `10.10.10.0/24` — закрытая сеть; в реальной — только через bastion/VPN.
- На стенде мы используем учебные пароли и `sudo` без пароля `[если так настроено]` — в бою это недопустимо.

## 7. Вопросы на собеседовании

**Вопрос 1. Что происходит по шагам, когда я делаю `kubectl apply -f pod.yaml`?**

Ответ: `kubectl` читает kubeconfig, сериализует объект и отправляет HTTPS-запрос на `kube-apiserver:6443`. Apiserver аутентифицирует клиента (сертификат/токен), авторизует через RBAC, прогоняет через admission-плагины (мутация + валидация), затем валидирует схему и **записывает объект в etcd**. Scheduler через watch видит под без `nodeName`, выбирает ноду (фильтрация + scoring) и делает binding — проставляет `nodeName`. Kubelet этой ноды видит через watch «мне назначили под», вызывает CRI (`RunPodSandbox`) — containerd создаёт sandbox, CNI выдаёт IP, затем создаются контейнеры, kubelet монтирует тома через CSI и запускает пробы. Статус пода kubelet пишет обратно в apiserver → etcd.

*Что хочет услышать интервьюер:* что apiserver — единственный, кто пишет в etcd; что kubelet **не создаёт** поды, а исполняет; что связь односторонняя (kubelet тянет).

*Красный флаг:* «kubectl отправляет команду на kubelet напрямую» или «scheduler запускает контейнер».

**Вопрос 2. Зачем etcd нечётное число узлов и что будет при потере кворума?**

Ответ: etcd использует Raft, для записи нужно большинство — кворум `(n/2)+1`. При 3 узлах кворум 2, при 5 — 3. Чётное число не даёт выигрыша в отказоустойчивости: 4 узла терпят потерю 1, как и 3, но требуют больше ресурсов. При потере кворума кластер etcd становится **read-only и недееспособным**: apiserver не может писать, новые поды не создаются, изменения не применяются; при этом уже запущенные контейнеры продолжают работать (kubelet автономен). Лечится восстановлением узлов или восстановлением из снапшота etcd (урок 46).

*Что хочет услышать интервьюер:* формулу кворума, понимание, что существующие поды продолжат жить, и что есть процедура бэкапа/восстановления.

*Красный флаг:* «при потере кворума всё сразу останавливается» или «etcd можно поставить на 2 узла для надёжности».

**Вопрос 3. Чем kubelet отличается от kube-proxy?**

Ответ: kubelet — агент ноды, работает с **подами**: получает назначенные поды от apiserver, запускает/останавливает контейнеры через CRI, монтирует тома, выполняет пробы, отчитывается о состоянии ноды и подов. kube-proxy — сетевой компонент, работает с **сервисами**: трансформирует объекты `Service`/`Endpoints` в правила iptables/IPVS/nftables, чтобы трафик на ClusterIP/NodePort попадал на живой под. Они не пересекаются: kubelet не знает про сервисы, kube-proxy не знает про контейнеры.

*Что хочет услышать интервьюер:* чёткое разделение «поды vs сервисы», упоминание CRI и того, что kube-proxy сейчас опционален (замена — Cilium в kube-proxy-less режиме).

*Красный флаг:* «kube-proxy балансирует нагрузку между подами приложения» (нет — он лишь DNAT на уровне ядра, без health-aware балансировки).

**Вопрос 4. Что такое CRI, CNI и CSI и почему это три разных интерфейса? Что будет, если не установить CNI?**

Ответ: CRI — gRPC-контракт между kubelet и container runtime (containerd, CRI-O), стандартизирует запуск sandbox/контейнеров и работу с образами. CNI — спецификация плагина сети: поду выдаётся IP и настраивается связность; вызывается runtime'ом, конфиги в `/etc/cni/net.d`, бинарники в `/opt/cni/bin`. CSI — gRPC-контракт для хранилища с тремя сервисами (Identity/Controller/Node) и sidecar-контейнерами. Разные они потому, что решают независимые задачи разных вендоров и меняются с разной скоростью. Без CNI нода останется `NotReady` с причиной `cni plugin not initialized`, поды (включая CoreDNS) будут в `Pending`, потому что им нечего выдать IP; поды с `hostNetwork: true` при этом запустятся.

*Что хочет услышать интервьюер:* что все три — про слабую связанность и сменяемость вендора, и конкретное поведение кластера без CNI.

*Красный флаг:* «CNI — это то же самое, что Service» или «без CNI ничего не запустится вообще».

**Вопрос 5. Упал kube-apiserver. Что произойдёт с уже запущенными подами и как диагностировать?**

Ответ: запущенные контейнеры продолжат работать — kubelet держит локальное состояние и не останавливает поды без указаний. Но управление ляжет: `kubectl` не работает, новые поды не создаются, сервис-эндпоинты не обновляются, провалы нод не обнаруживаются. Диагностика: `systemctl status kubelet` → `journalctl -u kubelet` → `sudo crictl ps` (виден ли контейнер apiserver) → `ls /etc/kubernetes/manifests/` (не пропал ли `kube-apiserver.yaml`) → `crictl logs <id>` → проверить etcd (`etcdctl endpoint health`) → `ss -tlnp` на 6443. Частая причина на лабораторном стенде — kubelet не смог перезапустить статический под из-за ошибки в манифесте или недоступного образа.

*Что хочет услышать интервьюер:* понимание, что control plane — тоже контейнеры под управлением kubelet, и что деградация управления ≠ мгновенная остановка приложения.

*Красный флаг:* «все поды сразу упадут» или «надо переустанавливать кластер».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Concepts → Overview → Kubernetes Components*; статьи *Cluster Architecture*, *Nodes*, *Control Plane-Node Communication*, *Kubelet*, *kube-proxy*, *Container Runtimes*, *Network Plugins*, *Storage Classes / CSI*. Поиск: `kubernetes components site:kubernetes.io`, `kubernetes CRI CNI CSI difference`.
- **kubernetes.io** — *Reference → Command line tool (kubeadm) → kubeadm init* и *kubeadm config images*. Поиск: `kubeadm init reference kubernetes.io`.
- **kubernetes.io** — *Tasks → Administer a Cluster → Creating a cluster with kubeadm* (свежая пошаговая инструкция под текущую версию — версии пакетов и репозиториев меняются, всегда сверяйся с ней). Поиск: `creating a cluster with kubeadm kubernetes.io`.
- **kubernetes.io** — *Concepts → Cluster Administration → Node Autoscaling* `[ПРОВЕРИТЬ, не перенесли ли раздел]` и *Version Skew Policy* (правила совместимости версий kubelet/apiserver). Поиск: `kubernetes version skew policy`.
- **etcd.io** — *Docs → Operations guide* (member list, endpoint status, disaster recovery, defragmentation). Поиск: `etcd operations guide endpoint status`.
- **containerd.io** — *Docs → Hosts and containers → Configuring containerd* (`config.toml`, `SystemdCgroup`, registry mirrors, sandbox image). Поиск: `containerd config version 2 SystemdCgroup CRI`.
- **github.com/kubernetes-sigs/cri-tools** — README и `docs/crictl.md` (валидация CRI, `crictl info/ps/logs`). Поиск: `crictl documentation cri-tools`.
- **github.com/containernetworking/cni** — спецификация CNI и список плагинов (`bridge`, `host-local`, `loopback`, `portmap`). Поиск: `CNI specification containernetworking`.
- **github.com/container-storage-interface/spec** —спецификация CSI, описание трёх сервисов и sidecar'ов. Поиск: `container storage interface spec CSI`.
- **learn.microsoft.com** `[ПРОВЕРИТЬ актуальность раздела]` — материалы по AKS как источнику архитектурных схем; полезно для сравнения managed-кластера с self-hosted.

Все конкретные номера версий (`v1.31.x`, `crictl v1.31.1`, версия pause-образа, значения портов kube-proxy) помечены в тексте как `[ПРОВЕРИТЬ]` — сверяй их с официальным *Version Skew Policy* и release notes на момент прохождения лабы.
