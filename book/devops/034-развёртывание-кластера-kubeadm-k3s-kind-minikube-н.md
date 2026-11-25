# Развёртывание кластера: kubeadm, k3s, kind, minikube — на VMware/Proxmox в лаборатории

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в поддержку:**

> **Пользователь:** ivanov (Иван Иванов, тестирует новый платёжный сервис)
> **ВМ:** GIT01
> **Что написал:** «Нам под тестовый контур нужен свой Kubernetes. Разработчики хотят делать `kubectl apply`, а не объяснять мне, почему у них опять `docker run` не взлетел. Подними кластер до конца недели, желательно не один — нам нужен стенд, где можно проверить деплой и откат.»
> **Срочность:** средняя

Дальше выясняется классическая история: «нам нужен кластер» — это четыре разных задачи. Разработчику на ноутбуке нужен `kind`, чтобы прогонять манифесты за 40 секунд. Для быстрого прототипа на одном сервере — `k3s`. Для настоящего учебного кластера с тремя узлами, etcd, сертификатами и join-токенами — `kubeadm`. А `minikube` — это тренажёр, чтобы посмотреть на addon'ы, Ingress и StorageClass, не строя инфраструктуру.

После этой главы ты будешь уметь: подготовить Ubuntu-узлы (swap, модули ядра, containerd), поднять кластер на `kubeadm` из трёх ВМ на VMware, поставить CNI и довести узлы до `Ready`, проверить k3s как «быстрый» вариант, поднять `kind` и `minikube` на рабочей Linux-машине и держать несколько кластеров одновременно через `kubectl` contexts, не путая их между собой. Плюс поймёшь, чем отличается вложенная виртуализация в Proxmox от обычной ВМ в VMware Workstation и почему это важно до, а не после установки.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Архитектуру control plane и узлов (apiserver, etcd, kubelet, kube-proxy, CRI/CNI/CSI) мы разобрали в уроке 33. Здесь смотрим на другое: **как из голой ОС получается кластер** и что именно делает каждый инструмент.

Главная мысль: Kubernetes — это не программа, а **набор процессов + сертификаты + конфиги**. Кластер «разворачивается» не установкой пакета, а последовательностью: подготовка ОС → container runtime → kubelet → bootstrap control plane → CNI → join узлов.

```text
kubeadm init на k8s-cp01 (10.10.10.61)
 │
 ├─ 1. preflight: ОС, swap, cgroup v2, модули br_netfilter/overlay,
 │               порты 6443/2379/10250, время, hostname
 ├─ 2. PKI: CA, apiserver, etcd, front-proxy, sa.key  → /etc/kubernetes/pki
 ├─ 3. kubeconfig'и: admin.conf, kubelet.conf,
 │                   controller-manager.conf, scheduler.conf
 ├─ 4. static pods из /etc/kubernetes/manifests:
 │       etcd, kube-apiserver, kube-controller-manager, kube-scheduler
 │       (их запускает kubelet, а не systemd!)
 ├─ 5. kubelet → systemd-юнит, конфиг /var/lib/kubelet/config.yaml
 ├─ 6. bootstrap token + ConfigMap cluster-info в kube-system
 │       → это то, чем «пускают» новых узлов (kubeadm join)
 └─ 7. addons: kube-proxy (DaemonSet), CoreDNS (Deployment)

 ⚠ CNI kubeadm НЕ ставит. Пока не поставишь Flannel/Calico —
   узлы будут NotReady, CoreDNS в Pending.
```

Что делает каждый инструмент:

| Инструмент | Что разворачивает | Где живёт | Когда брать |
|---|---|---|---|
| **kubeadm** | «взрослый» кластер: PKI, static pods, токены, upgrade-путь | ВМ/серверы, полноценный control plane | учебный кластер «как в проде», подготовка к CKA |
| **k3s** | один бинарь + SQLite (или embedded etcd) вместо тяжёлого etcd, всё в одном процессе | systemd-сервис на Linux | быстрый стенд, edge, IoT, «за 2 минуты» |
| **kind** | узлы как Docker-контейнеры (`kindest/node`) | Docker/Podman на рабочей машине | тесты манифестов, CI, эмуляция multi-node |
| **minikube** | одну ВМ/контейнер с полноценным single-node кластером + addon'ы | драйвер docker/kvm/none | обучение, addon'ы, dashboard, ingress |
| **managed** (Яндекс, VK Cloud) | control plane у провайдера, тебе — только workers | облако | прод в РФ, урок 47 |

Порты, которые надо помнить (и открыть, если есть файрвол):

```text
TCP 6443        kube-apiserver (kubeadm и k3s) — точка входа для kubectl и kubelet
TCP 2379-2380   etcd (client / peer)
TCP 10250       kubelet API (logs, exec, metrics)
TCP 10257/10259 kube-controller-manager / kube-scheduler (healthz)
TCP 30000-32767 NodePort range
UDP 8472        VXLAN Flannel
UDP 4789 / TCP 179  Calico VXLAN / BGP (если Calico)
```

Логика bootstrap-токена: `kubeadm init` создаёт токен и ConfigMap `cluster-info` в `kube-system`. Новый узел на `kubeadm join` приходит на 6443 **без сертификата**, предъявляет токен, получает CA-сертификат кластера (проверяя его по хешу) и уже с ним запрашивает клиентский сертификат через CSR API. Токен живёт 24 часа по умолчанию — это самая частая причина «вчера join работал, сегодня нет».

Вложенная виртуализация: чтобы внутри PVE01 (Proxmox на 10.10.10.70) поднять ВМ, сам гипервизор должен видеть VT-x/AMD-V. В VMware Workstation это галочка **VM Settings → Processors → Virtualize Intel VT-x/EPT or AMD-V/RVI**, на хосте — `kvm-ok` / `grep -c -E 'vmx|svm' /proc/cpuinfo`, в Proxmox — тип CPU `host` у ВМ.

## 3. Аналогия из жизни

Ты открываешь ресторан.

**kubeadm** — капитальный ремонт помещения своими руками по СНиП: проводка, вентиляция, пожарная сигнализация, отдельный щиток на каждую линию. Долго, больно, но получится настоящий ресторан, который пройдёт проверку и который можно расширять: пристроить зал, поменять плиту, поставить второй вход. Именно поэтому в проде и на экзаменах — kubeadm.

**k3s** — кухня «под ключ» из готовых модулей: привезли, подключили к воде и электричеству, через час готовите. Всё нужное внутри, ничего лишнего снаружи (etcd заменён на лёгкое хранилище, часть компонентов упакована в один бинарь). Для фуд-корта, для выездной точки, для «надо было вчера» — идеально.

**kind** — картонный макет ресторана в масштабе на столе. Расставить мебель, отрепетировать подачу, проверить, что официанты не сталкиваются в проходе. Быстро, дёшево, можно сломать и сделать заново. Но гостей туда не посадишь.

**minikube** — симулятор управления рестораном на планшете: настоящие процессы, но одна кухня и один зал, зато с кнопками «включить доставку», «включить бронирование» (addon'ы).

Аналогия перестаёт работать на производительности: макет из картона не покажет, что твой `requests`/`limits` не сходится с реальной памятью узла, а k3s в одном процессе не расскажет, как ведёт себя etcd при потере кворума.

## 4. Практика и команды

Всё делаем на Ubuntu Server 24.04 (узлы `k8s-cp01`/`k8s-w01`/`k8s-w02` и рабочая `UBU01`). Версию Kubernetes подставляй актуальную стабильную — `[ПРОВЕРИТЬ]` на kubernetes.io/releases на момент чтения; в примерах ниже условно `v1.32`.

**1. Подготовка ОС (на всех будущих узлах).** Swap обязан быть выключен — kubelet отказывается стартовать с включённым swap.

```bash
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab      # чтобы swap не вернулся после ребута
free -h                                        # в строке Swap должно быть 0B

cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
sudo modprobe overlay && sudo modprobe br_netfilter

cat <<EOF | sudo tee /etc/sysctl.d/99-kubernetes.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system
```

`br_netfilter` + `bridge-nf-call-iptables=1` нужны, чтобы iptables видел трафик, идущий через linux-bridge (это основа Service и Pod-сети). `ip_forward=1` — маршрутизация между подами на разных узлах.

**2. Container runtime — containerd.**

```bash
sudo apt update && sudo apt install -y containerd
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml > /dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd && sudo systemctl enable containerd
containerd config dump | grep -i systemdcgroup   # ждём "SystemdCgroup = true"
```

`SystemdCgroup = true` — containerd управляет cgroups через systemd. Если оставить `false`, а система на cgroup v2 (Ubuntu 24.04 — именно так), kubelet будет ругаться на несовпадение cgroup-драйверов. В containerd 2.x путь в конфиге другой (`io.containerd.cri.v1.runtime`) `[ПРОВЕРИТЬ]` — поэтому проверяй именно через `containerd config dump`, а не глазами по файлу.

**3. Пакеты Kubernetes (репозиторий pkgs.k8s.io, старый apt.kubernetes.io мёртв).**

```bash
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt update
sudo apt install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

`apt-mark hold` фиксирует версии: если завтра apt обновит kubelet до следующего минора, а kubeadm останется старым — кластер встанет. Смотри `kubeadm version`, `kubelet --version`, `kubectl version --client` — миноры должны совпадать.

**4. Инициализация control plane.**

```bash
sudo kubeadm init \
  --apiserver-advertise-address=10.10.10.61 \
  --control-plane-endpoint=k8s-cp01.corp.vektor.lab \
  --pod-network-cidr=10.244.0.0/16 \
  --cri-socket=unix:///run/containerd/containerd.sock
```

`--apiserver-advertise-address` — адрес, по которому apiserver виден узлам (не перепутай с NAT-адресом). `--control-plane-endpoint` — имя, которое попадёт во все kubeconfig'и; для HA за ним стоит VIP/балансировщик, и тогда кластер переживёт падение одного CP. `--pod-network-cidr` должен совпадать с сетью CNI, иначе поды не получат маршрут. В конце вывода — блок `kubeadm join ...` с токеном и `--discovery-token-ca-cert-hash`. Сохрани его.

Типичный (сокращённый) вывод:

```text
[init] Using Kubernetes version: v1.32.x
[preflight] Running pre-flight checks
[control-plane] Writing kube-apiserver manifest
[etcd] Creating static Pod manifest for local etcd
[addons] Applied essential addon: CoreDNS
[addons] Applied essential addon: kube-proxy
Your Kubernetes control-plane has initialized successfully!
kubeadm join k8s-cp01.corp.vektor.lab:6443 --token abcdef.0123456789abcdef \
  --discovery-token-ca-cert-hash sha256:...
```

Если в этом логе есть `[ERROR` — дальше не идём, читаем `journalctl -u kubelet`.

**5. kubeconfig для обычного пользователя (не root).**

```bash
mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
kubectl get nodes -o wide
```

`admin.conf` — это фактически root-доступ к кластеру. Права `600`, никогда в Git (RBAC и ServiceAccount — урок 41).

**6. CNI — ставим Flannel (для Calico pod CIDR по умолчанию другой).**

```bash
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
kubectl -n kube-system get pods -l app=flannel -w
```

Ждём `Running` на каждом узле, затем `kubectl get nodes` → `Ready`. Если поставил Calico — `--pod-network-cidr` должен совпадать с `calicoNetwork.ipPools[].cidr` в Installation CR `[ПРОВЕРИТЬ]`. Подробно CNI — урок 37.

**7. Присоединение рабочих узлов.**

```bash
# если токен протух (TTL 24ч) — на cp01:
sudo kubeadm token create --print-join-command

# на k8s-w01 и k8s-w02:
sudo kubeadm join k8s-cp01.corp.vektor.lab:6443 --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash> \
  --cri-socket=unix:///run/containerd/containerd.sock
```

`--print-join-command` печатает готовую строку с новым токеном — самый быстрый способ починить «token expired». `--cri-socket` нужен, если на узле больше одного runtime (например, остался Docker).

**8. k3s — быстрый вариант (на `k8s-cp01` и `k8s-w01`, до kubeadm).**

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --write-kubeconfig-mode 644 \
  --disable traefik --tls-san 10.10.10.61" sh -s -
sudo cat /var/lib/rancher/k3s/server/node-token

# на k8s-w01:
curl -sfL https://get.k3s.io | K3S_URL=https://10.10.10.61:6443 \
  K3S_TOKEN=<node-token> sh -s -
k3s kubectl get nodes
```

k3s ставит всё одним бинарём, кладёт kubeconfig в `/etc/rancher/k3s/k3s.yaml`, сервис — `systemctl status k3s`. `--disable traefik` убирает встроенный Ingress (нам он тут не нужен). Удаление: `/usr/local/bin/k3s-uninstall.sh` на сервере, `/usr/local/bin/k3s-agent-uninstall.sh` на агенте. Swap тоже должен быть выключен.

**9. kind — кластер в Docker-контейнерах.**

```yaml
# kind-vektor.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 80
        hostPort: 8080
        protocol: TCP
  - role: worker
```

```bash
kind create cluster --name vektor-lab --config kind-vektor.yaml
kubectl config get-contexts
kind get clusters
kind delete cluster --name vektor-lab
```

Контекст называется `kind-vektor-lab`. Узлы — контейнеры, поэтому «перезагрузить узел» нельзя, зато `docker exec -it vektor-lab-worker bash` даёт shell внутри. Идеально для CI: поднять, прогнать `kubectl apply -f`, удалить.

**10. minikube — тренажёр с addon'ами.**

```bash
minikube start --driver=docker --cpus=2 --memory=4096 --profile vektor-mini
minikube status -p vektor-mini
minikube addons list
minikube addons enable ingress -p vektor-mini
minikube stop -p vektor-mini
```

`--profile` создаёт отдельный кластер с отдельным kubeconfig-контекстом (имя контекста = имя профиля). `--kubernetes-version=stable` — если нужна конкретная стабильная версия `[ПРОВЕРИТЬ синтаксис для твоей версии minikube]`.

**11. Диагностика на старте (это половина работы).**

```bash
kubectl cluster-info
kubectl get --raw='/readyz?verbose'        # детальный healthz
sudo kubeadm certs check-expiration        # когда истекают сертификаты
sudo journalctl -u kubelet -n 50 --no-pager
sudo crictl ps -a                          # что вообще видит runtime
sudo crictl logs <container-id>            # логи static pod'а в обход kubectl
```

`crictl` — критично важный инструмент: когда apiserver не поднялся, `kubectl` бесполезен, а `crictl ps -a` показывает, что etcd-контейнер падает с конкретной ошибкой.

**12. Proxmox/VMware — проверка вложенной виртуализации.**

```bash
grep -c -E 'vmx|svm' /proc/cpuinfo         # >0 — виртуализация видна
# на PVE:
cat /sys/module/kvm_intel/parameters/nested  # ожидаем Y
qm set 101 --cpu host                        # тип CPU host для вложенных ВМ
```

В VMware Workstation галочка **VM Settings → Processors → Virtualize Intel VT-x/EPT** обязательна для PVE01.

## 5. Лабораторная работа

### Предусловия

- `UBU01` (10.10.10.30) — Ubuntu Server 24.04, Docker установлен (уроки 26–28), SSH по ключу настроен.
- Три новые ВМ Ubuntu Server 24.04 для этого трека, статические адреса из таблицы стенда: `k8s-cp01` = 10.10.10.61, `k8s-w01` = 10.10.10.62, `k8s-w02` = 10.10.10.63. Минимум 2 vCPU / 4 ГБ RAM / 40 ГБ на cp01, 2 vCPU / 2 ГБ / 30 ГБ на воркерах. ОС ставится тем же способом, что и UBU01 в модуле L1.
- На всех трёх: уникальный `hostname`, запись в `/etc/hosts` на каждом узле:
  ```text
  10.10.10.61  k8s-cp01.corp.vektor.lab k8s-cp01
  10.10.10.62  k8s-w01.corp.vektor.lab  k8s-w01
  10.10.10.63  k8s-w02.corp.vektor.lab  k8s-w02
  ```
  Без этого `--control-plane-endpoint` не разрешится и join упадёт.
- Синхронизация времени: `timedatectl` → `System clock synchronized: yes`. Разъехавшиеся часы дают ошибки TLS «certificate is not yet valid».
- Образ для тестового pod'а — из своего `HARBOR01` (10.10.10.55), потому что Docker Hub из РФ может быть недоступен (уроки 31–32) `[ПРОВЕРИТЬ имя проекта в своём Harbor]`.
- Windows-часть стенда для этой лабы не нужна.

### Снапшот

На каждой затрагиваемой ВМ — `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01`.

GUI VMware Workstation: выделить ВМ → **VM → Snapshot → Take Snapshot** → имя `before-devops-034`.

Или из командной строки хоста:

```powershell
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot `
  "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-034
```

`[ПРОВЕРИТЬ путь к vmrun.exe и к .vmx на своём хосте]`. Имя снапшота должно быть ровно `before-devops-034` — на него ссылается раздел «Откат».

### Шаги

1. **Создать и настроить три ВМ** — на хосте, в VMware Workstation. Ожидаемо: три Ubuntu 24.04 с адресами .61/.62/.63, пингуются с `UBU01` (`ping -c2 10.10.10.61`), SSH работает с ключом.
2. **На всех трёх — базовая подготовка ОС** (команды из п.1 раздела 4: `swapoff`, `fstab`, модули, sysctl). Ожидаемо: `free -h` показывает `Swap: 0B`, `lsmod | grep br_netfilter` — модуль загружен.
3. **Поставить и настроить containerd** на всех трёх (п.2). Ожидаемо: `systemctl is-active containerd` → `active`, `containerd config dump | grep -i systemdcgroup` → `true`.
4. **Поставить kubelet/kubeadm/kubectl и зафиксировать версии** (п.3). Ожидаемо: `kubeadm version` и `kubelet --version` — одинаковый минор.
5. **Быстрый прогон k3s** на `k8s-cp01` (server) и `k8s-w01` (agent), п.8. Ожидаемо: `k3s kubectl get nodes` показывает две строки `Ready` примерно через минуту. Записать в `stand-log.md` вывод `kubectl get nodes -o wide` — это пригодится для сравнения.
6. **Снести k3s, чтобы не конфликтовал за 6443:** на `k8s-w01` — `sudo /usr/local/bin/k3s-agent-uninstall.sh`, на `k8s-cp01` — `sudo /usr/local/bin/k3s-uninstall.sh`. Ожидаемо: `ss -lntp | grep 6443` пусто.
7. **`kubeadm init` на `k8s-cp01`** (п.4). Ожидаемо: `Your Kubernetes control-plane has initialized successfully!` и строка `kubeadm join ...` в конце. Скопировать её в `stand-log.md`.
8. **Настроить kubeconfig пользователя** (п.5). Ожидаемо: `kubectl get nodes` показывает `k8s-cp01` в статусе `NotReady` — это нормально, CNI ещё нет.
9. **Поставить Flannel** (п.6). Ожидаемо: в течение 1–2 минут `kubectl get nodes` → `k8s-cp01 Ready`, `kubectl -n kube-system get pods` — все `Running`, включая `coredns-*`.
10. **Присоединить `k8s-w01` и `k8s-w02`** (п.7). Ожидаемо: `kubectl get nodes -o wide` — три узла `Ready`, у каждого свой `INTERNAL-IP` и `CONTAINER-RUNTIME containerd://...`.
11. **Проверить, что поды реально планируются на разных узлах:** `kubectl create deployment web --image=<образ из HARBOR01> --replicas=3`, затем `kubectl get pods -o wide`. Ожидаемо: три pod'а `Running`, распределены по `k8s-w01`/`k8s-w02`/`k8s-cp01` (на cp01 поды пойдут, только если снят taint — по умолчанию control plane изолирован, это нормально).
12. **Поднять kind на `UBU01`** (п.9) и **minikube** (п.10). Ожидаемо: `kubectl config get-contexts` показывает три контекста — `kubernetes-admin@kubernetes` (kubeadm), `kind-vektor-lab`, `vektor-mini`. Переключение — `kubectl config use-context kind-vektor-lab`, проверка — `kubectl get nodes`.
13. **Записать результат в `stand-log.md`**: адреса, версии, имена контекстов, где какой кластер.

### Проверка

- [ ] `kubectl get nodes -o wide` на `k8s-cp01` — ровно три узла, все `Ready`, версия kubelet одна и та же.
- [ ] `kubectl -n kube-system get pods` — `coredns`, `kube-proxy`, `kube-apiserver`, `etcd`, `kube-scheduler`, `kube-controller-manager`, `kube-flannel-*` — все `Running`/`Completed`, без `CrashLoopBackOff`.
- [ ] `kubectl get --raw='/readyz?verbose'` — все проверки `ok`.
- [ ] `sudo kubeadm certs check-expiration` — сертификаты валидны, ближайшее истечение — CA через годы.
- [ ] `kubectl get pods -o wide` для тестового деплоя — 3 pod'а `Running`, распределены по узлам.
- [ ] `kubectl config get-contexts` — три контекста, переключение между ними работает без переустановки kubeconfig'ов.
- [ ] После `sudo reboot` на `k8s-w02` (по одному!) узел возвращается в `Ready` за 1–2 минуты — kubelet включён в автозапуск.
- [ ] `stand-log.md` дополнен.

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, тестирует платёжный сервис)
> **ВМ:** k8s-w02
> **Что написал:** «После перезагрузки воркера новые поды висят в Pending, старые не переезжают. Деплой не раскатывается, у меня демо завтра.»
> **Срочность:** высокая

**Воспроизводим поломку** (на `k8s-w02`, от root):

```bash
sudo sed -i '/ swap / s/^#//' /etc/fstab    # раскомментировали swap обратно
sudo swapon -a
sudo reboot
```

По сути имитируем «коллега восстановил swap для 1С, потому что „так доктор прописал“» — очень жизненная история.

**Диагностика, по порядку:**

1. `kubectl get nodes -o wide` с `k8s-cp01` → `k8s-w02` в `NotReady`. Значит, проблема на узле, а не в деплое.
2. `kubectl describe node k8s-w02 | tail -30` → в Conditions `Ready False`, в Events `NodeNotReady`, часто `kubelet stopped posting node status`. Запоминаем время последнего отчёта.
3. `ssh k8s-w02`, затем `systemctl status kubelet` → `failed` или `activating (auto-restart)`.
4. `sudo journalctl -u kubelet -n 50 --no-pager` → ключевая строка: `failed to run Kubelet: running with swap on is not supported, please disable swap`.
5. Подтверждаем гипотезу: `free -h` показывает ненулевой Swap, `grep swap /etc/fstab` — строка без `#`.

**Решение:**

```bash
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab
sudo systemctl restart kubelet
systemctl is-active kubelet
# на cp01:
kubectl get nodes -w     # ждём k8s-w02 Ready
```

Если pod'ы после этого остались в `Pending` — они поднимутся сами за 30–60 секунд (kubelet заново регистрируется), принудительно ничего удалять не надо.

**Ответ пользователю:** «Причина — на узле k8s-w02 после перезагрузки включился swap: kubelet с ним не запускается, поэтому узел выпал из кластера и планировщик не мог туда ставить поды. Swap отключил, вернул запрет в `/etc/fstab`, kubelet перезапущен, узел в статусе Ready. Деплой раскатывается штатно, проверь свой namespace — если поды остались в Pending, напиши, посмотрю `kubectl describe pod`.»

**Закрытие заявки:** категория «Kubernetes / узел NotReady», причина «swap on», решение «swapoff + fstab + restart kubelet», в `stand-log.md` — строка «k8s-w02: swap снова выключен, инцидент воспроизведён в лабе 034».

### Что изменилось на стенде

- Появились ВМ `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), Ubuntu Server 24.04, 2 vCPU каждая.
- На всех трёх: swap отключён и закомментирован в `/etc/fstab`, загружены `overlay` и `br_netfilter`, применены sysctl из `/etc/sysctl.d/99-kubernetes.conf`, установлен containerd с `SystemdCgroup = true`, установлены `kubelet`/`kubeadm`/`kubectl` и зафиксированы через `apt-mark hold`.
- Кластер kubeadm: control plane на `k8s-cp01`, endpoint `k8s-cp01.corp.vektor.lab:6443`, pod CIDR `10.244.0.0/16`, CNI Flannel, CoreDNS и kube-proxy — штатные.
- kubeconfig пользователя лежит на `k8s-cp01` в `~/.kube/config` (владелец — учебная учётка администратора стенда).
- На `UBU01`: кластер `kind` с именем `vektor-lab` (контекст `kind-vektor-lab`) и профиль minikube `vektor-mini`, установлен `kind` и `minikube`.
- k3s на `k8s-cp01`/`k8s-w01` устанавливался и был удалён — состояние стенда k3s не содержит.
- Добавлены записи в `/etc/hosts` всех трёх узлов и в `stand-log.md`.

### Откат

- Остановить и удалить учебные кластеры, чтобы не занимать порты и память:
  ```bash
  kind delete cluster --name vektor-lab
  minikube delete -p vektor-mini
  sudo kubeadm reset -f && sudo rm -rf /etc/cni/net.d $HOME/.kube
  ```
  На воркерах — `sudo kubeadm reset -f`, затем `sudo systemctl restart containerd`.
- Полный откат до состояния до лабы: в VMware Workstation выбрать ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-devops-034` → **Go to**. Повторить для `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01`.
- Если ВМ создавались впервые в этой лабе — можно просто удалить их из инвентаря (файлы ВМ оставить), но тогда следующие главы модуля 5 не на чем будет делать: кластер нужен постоянно. Минимум — оставь `k8s-cp01` + один воркер.
- Не забыть откатить `stand-log.md` не надо: записи об инциденте — это история, её не стираем.

## 6. Ошибки новичков и безопасность

1. **Забыл swap / fstab вернул его после ребута** → kubelet не стартует, узел `NotReady` (ровно наш кейс из лабы) → всегда `swapoff -a` **и** правка `/etc/fstab`, проверка `free -h` после перезагрузки.
2. **`--pod-network-cidr` не совпадает с сетью CNI** → pod'ы получают IP, но не маршрутизируются между узлами; CoreDNS в `Pending`/`CrashLoopBackOff`, `kubectl exec` в под не работает → сначала выбрать CNI, потом `kubeadm init`.
3. **Разные минорные версии kubeadm и kubelet** → `kubeadm join` отказывается, upgrade ломается → `apt-mark hold`, проверка `kubeadm version` / `kubelet --version` перед каждым шагом.
4. **`--apiserver-advertise-address` смотрит «не туда»** (указали адрес NAT/DHCP или 127.0.0.1) → apiserver поднимается, но узлы до него не доходят, `join` висит → адрес должен быть статическим адресом узла в сети 10.10.10.0/24.
5. **Время не синхронизировано** → `x509: certificate has expired or is not yet valid`, необъяснимые отказы TLS на ровном месте → `timedatectl` до установки.
6. **`kubeadm init` без CNI и паника «ничего не работает»** → узлы `NotReady` — это **ожидаемое** состояние, а не поломка; сначала CNI, потом выводы.
7. **Один control plane и нет бэкапа etcd** → потеря ВМ = потеря кластера целиком (бэкап etcd — урок 46) → минимум `etcdctl snapshot save` по расписанию и снапшот ВМ.
8. **Путаница контекстов: `kubectl delete` улетел не в тот кластер** → удалённые ресурсы в kind или, хуже, в «почти прод» → всегда `kubectl config current-context` перед деструктивной командой; разные кластеры — разные контексты с говорящими именами.

### Безопасность

- **`/etc/kubernetes/admin.conf` — это полный доступ к кластеру**, без RBAC-ограничений. Хранить только с правами `600`, не копировать в Git, не пересылать в чат. В нормальной среде админ ходит через отдельного пользователя с ролью, а не через `admin.conf`.
- **Join-токен = приглашение в кластер.** Кто получил токен и хеш CA в течение 24 часов — тот может стать узлом (а узел видит секреты своего узла). Поэтому `--print-join-command` не публикуют в общем чате, а токен отзывают: `kubeadm token delete <token>`.
- **API-сервер 6443 наружу — это подарок.** В лабе он доступен всей сети 10.10.10.0/24, и это нормально для стенда; в бою — закрытый порт, доступ через bastion/VPN, `--anonymous-auth=false` (по умолчанию так и есть в kubeadm) и аудит-логи.
- **`kubectl` без RBAC и с кластерролью `cluster-admin` для всех** — типичная лаба-упрощёнка. В рабочей среде — namespace-scoped роли, ServiceAccount'ы, NetworkPolicy; всё это в уроке 41.
- **Docker-группа на UBU01 для kind/minikube фактически равна root** на этой машине. Для лабораторного ПК приемлемо, для сервера — rootless Podman или выделенная ВМ.

## 7. Вопросы на собеседовании

**1. Чем kubeadm отличается от k3s и когда ты выберешь что?**

**Ответ:** kubeadm — «сборочный инструмент»: он не запускает кластер сам, а готовит PKI, kubeconfig'и, static pods control plane и токены, оставляя тебе стандартный upstream-Kubernetes с отдельным etcd, полным набором компонентов и штатным путём обновления (`kubeadm upgrade`). k3s — дистрибутив: один бинарь, всё в одном процессе, вместо etcd по умолчанию SQLite (или embedded etcd), в комплекте Traefik, ServiceLB, local-path-provisioner. Выбор: kubeadm — когда нужен «как в проде», совместимость с upstream-документацией, экзамен CKA, HA-кластер с контролем над etcd; k3s — быстрый стенд, edge/IoT, ограниченные ресурсы, «кластер за 10 минут». Ограничение k3s: часть привычных вещей отличается (пути, отсутствующие по умолчанию addon'ы), и переносить оттуда знания в прод-настройку надо с поправкой.

*Что хочет услышать интервьюер:* что kubeadm — не «дистрибутив», а bootstrap-утилита; что CNI не входит; что понимаешь цену лёгкости k3s.
*Красный флаг:* «k3s — это не настоящий Kubernetes» или «kubeadm ставит кластер одной командой и всё».

**2. Что делает `kubeadm init` под капотом?**

**Ответ:** Preflight-проверки (ОС, swap, cgroup-драйвер, модули ядра, порты, версии), генерация PKI в `/etc/kubernetes/pki`, генерация kubeconfig'ов (`admin`, `kubelet`, `controller-manager`, `scheduler`), запись манифестов static pods в `/etc/kubernetes/manifests` — их запускает kubelet, а не systemd, поэтому etcd и apiserver «поднимаются сами»; настройка kubelet, создание bootstrap-токена и ConfigMap `cluster-info` для join, установка addon'ов kube-proxy и CoreDNS. **CNI не ставится** — это ответственность администратора. Отсюда типичное состояние после init: control plane `Running`, узел `NotReady`.

*Что хочет услышать интервьюер:* понимание, что control plane — это static pods, и что CNI отдельно.
*Красный флаг:* «kubeadm устанавливает Docker и Calico».

**3. Узел после `kubeadm join` в статусе NotReady. Твои действия?**

**Ответ:** Порядок такой: `kubectl get nodes -o wide` → `kubectl describe node <имя>` (Conditions и Events) → ssh на узел → `systemctl status kubelet` → `journalctl -u kubelet -n 100`. Типовые причины по порядку частоты: не установлен CNI (NotReady у **всех** узлов, CoreDNS в Pending), swap включён (в логе kubelet прямая ошибка про swap), рассинхрон cgroup-драйвера containerd и kubelet, несовпадение версий kubeadm/kubelet, не резолвится `--control-plane-endpoint`, закрыты порты 6443/10250, разъехалось время. Проверяю также `crictl ps -a` — видит ли runtime контейнеры вообще. Чинится либо установкой CNI, либо правкой конфигурации узла + `systemctl restart kubelet`.

*Что хочет услышать интервьюер:* дисциплинированный порядок диагностики «от кластера к узлу к процессу к логу», а не «переустановлю кластер».
*Красный флаг:* «сразу делаю `kubeadm reset` и заново» без диагностики.

**4. Как сделать control plane отказоустойчивым?**

**Ответ:** Нечётное число control plane узлов — 3 или 5, потому что etcd требует кворума `(n/2)+1`. Перед apiserver'ами ставится балансировщик (HAProxy/keepalived с VIP, kube-vip или L4-балансер облака), и все узлы инициализируются с одним `--control-plane-endpoint=<VIP>:6443`, чтобы kubeconfig'и не менялись при отказе. Дальше `kubeadm init --control-plane-endpoint ... --upload-certs`, и остальные CP присоединяются с `--control-plane --certificate-key`. etcd может быть stacked (на тех же узлах) или external (отдельный кластер etcd). Практические ограничения: 2 CP — хуже, чем 1 (кворума нет ни при какой потере), а при 3 CP теряется один — кластер работает, теряются два — etcd read-only и кластер неуправляем.

*Что хочет услышать интервьюер:* кворум etcd, VIP/endpoint как единственная точка входа, `--certificate-key` для join CP.
*Красный флаг:* «подниму два control plane для надёжности».

**5. kind и minikube — где граница применимости? Можно ли на них проверять прод-манифесты?**

**Ответ:** Оба — про локальную разработку и CI, не про прод. kind гоняет узлы как Docker-контейнеры: быстро, воспроизводимо, легко в CI (`kind create cluster` + `kubectl apply` + `kind delete`), поддерживает multi-node и загрузку локальных образов (`kind load docker-image`). minikube — одна «машина» с полноценным single-node кластером и удобными addon'ами (ingress, dashboard, metrics-server), хорош для обучения. Границы: нет реальной сети между физическими узлами (не проверишь MTU, cross-node latency, NetworkPolicy на реальном трафике), нет реального CSI и дисков, нет нагрузки и ресурсов прод-узлов, нет HA control plane, поведение CNI в kind упрощено. Поэтому манифесты, Helm-чарты, RBAC и базовые сценарии деплоя — да, проверять можно и нужно; проблемы производительности, storage, сетевой политики и отказоустойчивости — только на настоящем кластере.

*Что хочет услышать интервьюер:* что ты используешь kind в CI, но не считаешь его заменой стейджингу.
*Красный флаг:* «у нас всё работает в minikube, значит и в проде заработает».

## 8. Источники (официальная документация)

- **kubernetes.io** — разделы «Installing kubeadm», «Creating a cluster with kubeadm», «Container Runtimes», «Kubernetes Components», «Certificates». Поиск: `kubeadm create cluster site:kubernetes.io`, `kubernetes container runtimes containerd systemd cgroup`.
- **kubernetes.io** — страница релизов и поддерживаемых версий (сверять актуальную версию перед установкой). Поиск: `kubernetes releases supported versions`.
- **k3s.io** — «Quick-Start Guide», «Configuration Options», «Resource Profiling». Поиск: `k3s quick start install server agent`.
- **kind.sigs.k8s.io** — «Quick Start», «Configuration» (multi-node, extraPortMappings, `kind load docker-image`). Поиск: `kind configuration multi-node cluster`.
- **minikube.sigs.k8s.io** — «Handbook», «Drivers», «Addons». Поиск: `minikube handbook drivers docker`.
- **github.com/flannel-io/flannel** — манифест `kube-flannel.yml` и раздел про `--pod-network-cidr`. Поиск: `flannel kubernetes kubeadm pod cidr`.
- **projectcalico.org** — «Install Calico» / «Calico the hard way» `[ПРОВЕРИТЬ актуальный путь раздела]`.
- **pve.proxmox.com** (Proxmox VE Administration Guide) — «Virtual Machines», «Nested virtualization», `qm` и cloud-init. Поиск: `proxmox nested virtualization kvm_intel nested`.
- **techdocs.broadcom.com** — документация VMware Workstation (снапшоты, `vmrun`, настройки процессора) `[ПРОВЕРИТЬ: документация VMware переехала на портал Broadcom, старые ссылки docs.vmware.com редиректят]`. Поиск: `vmware workstation vmrun snapshot command`.
