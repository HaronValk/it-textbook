# Хранилище: PV/PVC, StorageClass, CSI, Longhorn, Ceph/Rook, NFS

## 1. Введение (зачем это нужно в реальной работе)

**Тикет в поддержку «Вектора»:**

> **Пользователь:** ivanov (бухгалтерия, ВМ WIN11-CL01)
> **Что написал:** «Открываю сервис отчётов — вчерашних отчётов нет, список пустой. Вчера загрузил три файла, видел их в списке. Сегодня их нет. Сервис при этом работает.»
> **Срочность:** высокая

Дальше вы идёте на `k8s-cp01`, смотрите `kubectl get pods` и видите, что под сервиса отчётов перезапускался ночью — например, после обновления Deployment или переезда на другую ноду. Файлы писались в файловую систему контейнера, а она живёт ровно столько же, сколько контейнер. Данные исчезли не «из-за сбоя» — они исчезли по проекту.

Это самая частая причина, по которой приложение, идеально работавшее на одной ВМ, в Kubernetes начинает «терять данные». В контейнерах нет ничего постоянного по умолчанию: ни диска, ни папки, ни гарантии, что под завтра будет на той же машине. Хранилище приходится проектировать отдельно — и это ровно та задача, которую в вакансиях DevOps формулируют как «работа с persistent storage: PV/PVC, StorageClass, CSI, Ceph/Longhorn/NFS».

После этой главы вы должны уметь: объяснить разницу между PV, PVC и StorageClass; руками сделать статический том и динамический через provisioner; понять, из чего состоит CSI-драйвер; поставить NFS-провижионер и StatefulSet с `volumeClaimTemplates`; осознанно выбрать между NFS, Longhorn и Ceph/Rook; и не потерять данные при удалении PVC.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Начнём снизу, от самого простого к самому «кластерному».

**Уровни хранения в Kubernetes:**

| Тип | Живёт, пока живёт | Годится для |
|-----|-------------------|-------------|
| Слой контейнера (writable layer) | контейнер | ничего важного, tmp-файлы |
| `emptyDir` | Pod | кэш, обмен между контейнерами пода |
| `hostPath` | конкретная нода | системные демоны (агенты, CNI), не для данных |
| PV/PVC | кластер (вне пода) | всё, что должно пережить пересоздание пода |
| «Внешний» бэкенд (NFS, Ceph, Longhorn, облако) | сам бэкенд | собственно данные |

**PersistentVolume (PV)** — объект уровня кластера (как Node), описывающий *конкретный кусок хранилища*: 1 GiB на NFS-сервере по такому-то пути, или RBD-образ в Ceph, или диск в облаке. У PV есть `capacity`, `accessModes`, `persistentVolumeReclaimPolicy` и `storageClassName`.

**PersistentVolumeClaim (PVC)** — объект в неймспейсе, то есть *заявка приложения*: «дайте 1 GiB, режим RWO, класс nfs-client». Под монтирует не PV, а PVC — это принципиально: разработчик не должен знать, где физически лежит том. Связывание (binding) PV↔PVC всегда один-к-одному.

**Access modes** (режимы доступа): `ReadWriteOnce` (RWO — том монтируется на запись на одну ноду), `ReadOnlyMany` (ROX), `ReadWriteMany` (RWX — много нод одновременно, нужен NFS/CephFS/Longhorn-share-manager), `ReadWriteOncePod` (RWOP — том только для одного пода; стабилен в свежих версиях, `[ПРОВЕРИТЬ]` текущий статус).

**StorageClass (SC)** — «прайс-лист типов хранения»: имя + провижионер (кто создаёт тома) + параметры + `reclaimPolicy` + `volumeBindingMode`. Именно SC включает **динамическое провижионирование**: PVC без готового PV заставляет провижионер создать PV на лету.

**Reclaim policy** — что делать с томом, когда PVC удалили: `Delete` (удалить данные) или `Retain` (оставить PV в статусе Released, разбираться руками). Третий исторический вариант `Recycle` устарел и в CSI не поддерживается.

**CSI (Container Storage Interface)** — стандарт gRPC-интерфейса между Kubernetes и драйвером хранилища. До него каждый тип тома был вкомпилирован в сам Kubernetes (in-tree), и новый массив приходилось ждать год. Сейчас драйвер — отдельный под, а Kubernetes общается с ним по стандарту. У драйвера три gRPC-сервиса: **Identity** (версия, возможности), **Controller** (CreateVolume/DeleteVolume/ControllerPublish), **Node** (NodeStageVolume/NodePublishVolume — монтирование на ноде).

```text
CONTROL PLANE                                     РАБОЧАЯ НОДА
┌──────────────────────────────────────┐         ┌───────────────────────────────┐
│ kube-controller-manager              │         │ kubelet                       │
│   └─ external-provisioner  ─┐        │         │   └─ node-driver-registrar    │
│   └─ external-attacher     ─┼─gRPC──► │         │          │ регистрирует сокет │
│   └─ external-snapshotter  ─┤ CSI     │         │          ▼                    │
│   └─ external-resizer      ─┘ controller│        │   CSI node-сервис (gRPC)      │
└───────────────┬──────────────────────┘         └───────────┬───────────────────┘
                │ CreateVolume / DeleteVolume                 │ NodeStage / NodePublish
                ▼                                            ▼
        бэкенд: Ceph RBD, NFS, Longhorn,            /var/lib/kubelet/pods/<uid>/
        Yandex/VK облачный диск, СХД                /volumes/kubernetes.io~csi/...
```

В API есть ещё три служебных объекта: `CSIDriver` (возможности драйвера), `CSINode` (какие драйверы есть на ноде) и `VolumeAttachment` (что к какой ноде прицеплено).

**Последовательность динамического провижионирования:** создали PVC → контроллер увидел SC → вызвал `CreateVolume` у драйвера → появился PV → `VolumeAttachment` → kubelet на ноде делает `NodeStage`/`NodePublish` → том смонтирован в под по пути `/var/lib/kubelet/pods/...`.

**Бэкенды, которые надо знать в лицо:**

| Решение | Что даёт | Цена |
|---|---|---|
| NFS (`nfs-subdir-external-provisioner`) | просто, RWX, любой NAS | нет снапшотов, нет блочного доступа, нет квот, единая точка отказа |
| Longhorn | блочные тома по iSCSI, реплики 2–3, снапшоты, бэкап в S3, RWX через share-manager | требует `open-iscsi` на нодах, ест RAM/CPU, сам является нагрузкой кластера |
| Rook/Ceph | RBD (блок), CephFS (файлы), RGW (S3), самовосстановление | минимум 3 ноды, «сырые» диски, высокая сложность |
| Облачный CSI (Yandex/VK/Cloud.ru) | managed, снапшоты, «просто работает» | деньги, привязка к провайдеру |

Порты, которые пригодятся при диагностике: NFS — TCP/UDP 2049 (`rpcbind` 111 нужен для NFSv3), iSCSI для Longhorn — TCP 3260, Ceph mon — 3300 и 6789, OSD — 6800–7300 `[ПРОВЕРИТЬ диапазон]`.

## 3. Аналогия из жизни

Склад компании «Вектор». Приложению-кладовщику нужно место под коробки. **PV** — это конкретная ячейка на стеллаже с инвентарным номером: «ячейка B-17, 2 кубометра, доступ на запись». **PVC** — заявка, которую пишет кладовщик: «нужна ячейка на 1 кубометр, доступ на запись, класс „обычный“». Kubernetes сам находит подходящую ячейку и сцепляет заявку с ней один-к-одному. **StorageClass** — это прайс-лист склада: «обычный стеллаж», «сейф», «холодильник», и к каждому классу приписан подрядчик, который эти ячейки нарезает. **CSI** — единый бланк заявки для любого подрядчика: неважно, кто арендодатель, форма одна и та же, поэтому Kubernetes не переписывают под каждый новый склад. **Longhorn** — вы кладёте коробку сразу в три ячейки в разных концах склада и не замечаете, когда одну из них залило. **Ceph** — целый распределённый склад, где бригада сама переставляет стеллажи. **NFS** — общий стеллаж в коридоре, к которому все ходят по одному проходу: удобно, дёшево, но если проход закрыли — встало всё.

Где аналогия ломается: на реальном складе ячейку можно занять руками и договориться с кладовщиком, а в Kubernetes binding — чисто формальная процедура по capacity, accessModes, storageClassName и селекторам, и «договориться» нельзя.

## 4. Практика и команды

### 4.1 Посмотреть, что уже есть в кластере

```bash
kubectl get pv
kubectl get pvc -A
kubectl get storageclass
```

Пример (сокращённо):

```text
NAME              CAPACITY  ACCESS MODES  RECLAIM POLICY  STATUS  CLAIM                    STORAGECLASS  AGE
pv-static-report  1Gi       RWO           Retain          Bound   app/pvc-static-report    manual        5m

NAME         PROVISIONER                                    RECLAIMPOLICY  VOLUMEBINDINGMODE  ALLOWVOLUMEEXPANSION
nfs-client   k8s-sigs.io/nfs-subdir-external-provisioner    Delete         Immediate          false
local-path   rancher.io/local-path                          Delete         WaitForFirstConsumer  false
```

Смотрим: `STATUS` (Available / Bound / Released / Pending), `CLAIM` (кто занял PV) и `RECLAIM POLICY`. Если у SC стоит `(default)` в имени — она подхватывает PVC без явного `storageClassName`.

### 4.2 CSI-объекты

```bash
kubectl get csidrivers
kubectl get csinodes
kubectl get volumeattachments
```

В учебном кластере список может быть пустым — это нормально: in-tree NFS и `local-path` работают вообще без CSI. На облачном кластере вы увидите `ebs.csi.aws.com`, `disk-csi-driver.mks.ycloud.io` и т.п.

### 4.3 Статический PV на NFS

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-static-report
spec:
  capacity:
    storage: 1Gi
  accessModes: ["ReadWriteOnce"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: manual
  nfs:
    server: 10.10.10.30
    path: /srv/k8s-volumes/static-report
```

`storageClassName: manual` — это просто метка-строкой, провижионера за ней нет. `Retain` — сознательный выбор: каталог на NFS не будет удалён вместе с PVC.

### 4.4 PVC под него

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-static-report
  namespace: app
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: manual
  resources:
    requests:
      storage: 500Mi
```

PVC на 500Mi привяжется к PV на 1Gi: главное, чтобы запрос был не больше. Обратной «сдачи» остатка нет — весь GiB закрепляется за этим PVC.

### 4.5 Почему PVC висит Pending

```bash
kubectl describe pvc pvc-static-report -n app
```

В `Events` внизу видно ответ: `no persistent volumes available for this claim and no storage class is set`, или `waiting for first consumer to be created before binding`. В 80% случаев причина именно здесь, а не «в хранилище».

### 4.6 NFS-сервер: экспорт

```bash
sudo apt install -y nfs-kernel-server
sudo mkdir -p /srv/k8s-volumes/static-report
sudo chown -R nobody:nogroup /srv/k8s-volumes
```

```text
# /etc/exports
/srv/k8s-volumes  10.10.10.0/24(rw,sync,no_subtree_check,no_root_squash)
```

```bash
sudo exportfs -rav
sudo systemctl enable --now nfs-server
showmount -e 10.10.10.30
```

`showmount -e` должен вернуть `Export list for 10.10.10.30: /srv/k8s-volumes 10.10.10.0/24`. Флаг `-a` — все экспорты, `-r` — перечитать `/etc/exports`, `-v` — показать, что сделано. `no_root_squash` оставлен только ради лабы: в бою root внутри пода получил бы root на шаре.

### 4.7 Сделать StorageClass дефолтной

```bash
kubectl patch storageclass nfs-client -p \
  '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

Дефолтная SC может быть только одна; если их две, Kubernetes ругается в событиях, а PVC без `storageClassName` может уйти не туда.

### 4.8 Проверить том изнутри пода

```bash
kubectl exec -it web-cache-0 -- df -h /data
kubectl exec -it web-cache-0 -- sh -c 'dd if=/dev/zero of=/data/test.bin bs=1M count=100'
```

`df -h /data` покажет источник монтирования (`10.10.10.30:/srv/k8s-volumes/...`) — быстрый способ убедиться, что под реально видит сетевой том, а не локальную папку. `dd` заодно проверяет, что квоты и права не мешают записи.

### 4.9 StatefulSet: том на каждую реплику

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: web-cache
  namespace: app
spec:
  serviceName: web-cache
  replicas: 2
  selector:
    matchLabels: {app: web-cache}
  template:
    metadata:
      labels: {app: web-cache}
    spec:
      containers:
        - name: app
          image: busybox:1.36
          command: ["sh", "-c", "while true; do date >> /data/heartbeat.log; sleep 30; done"]
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: nfs-client
        resources:
          requests:
            storage: 1Gi
```

`volumeClaimTemplates` создаёт отдельный PVC на каждую реплику (`data-web-cache-0`, `data-web-cache-1`), и он переезжает за подом при пересоздании. Именно поэтому StatefulSet — единственный правильный вариант для «каждая реплика со своими данными».

### 4.10 Удаление PVC и судьба данных

```bash
kubectl delete pvc pvc-dynamic -n app          # policy Delete → каталог на NFS удалён
kubectl get pv pv-static-report                # policy Retain → статус Released, данные на месте

# вернуть Released-PV в работу под новый PVC
kubectl patch pv pv-static-report --type=json \
  -p='[{"op":"remove","path":"/spec/claimRef"}]'
```

После снятия `claimRef` PV снова `Available`, и новый PVC с `volumeName: pv-static-report` привяжется к нему.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), все `Ready`, `kubectl` работает с `k8s-cp01`.
- `UBU01` (10.10.10.30) — живая Ubuntu Server 24.04 с SSH, доступна из 10.10.10.0/24. Она и будет NFS-сервером (в проде так делать не стоит: файловый сервер и прод-кластер обычно разводят).
- `helm` на `k8s-cp01`. Полноценно Helm будет в уроке 42, здесь используем только `helm repo add` и `helm install` — по сути это «скачать чарт и применить манифесты».
- Ноды кластера видят `UBU01` по сети: `ping 10.10.10.30` с `k8s-w01`.

### Снапшот

Снапшоты в VMware Workstation — на **всех четырёх** ВМ: `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01`. Через GUI: VM → Snapshot → Take Snapshot. Или из CMD на хосте:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-039
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\k8s-w01\k8s-w01.vmx" before-devops-039
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\k8s-w02\k8s-w02.vmx" before-devops-039
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-039
```

### Шаги

1. **k8s-cp01.** `kubectl get nodes -o wide` → три ноды `Ready`. Если нет — сначала урок 34, дальше идти нет смысла.
2. **UBU01.** Ставим NFS-сервер и готовим каталог (команды из 4.6), создаём отдельно `/srv/k8s-volumes/static-report` и `/srv/k8s-volumes/dynamic`. Проверяем `showmount -e 10.10.10.30` → в списке `/srv/k8s-volumes`.
3. **k8s-cp01.** `kubectl create namespace app`, применяем `pv-static-report.yaml` (4.3). `kubectl get pv` → `STATUS: Available`. Пока никакой PVC его не занял.
4. **k8s-cp01.** Применяем `pvc-static-report.yaml` (4.4). `kubectl get pvc -n app` → `Bound`, в `kubectl get pv` у PV появляется `CLAIM: app/pvc-static-report`.
5. **k8s-cp01.** Запускаем под `report-writer` (busybox, `command: ["sh","-c","echo hello > /data/index.html; sleep 3600"]`) с `persistentVolumeClaim.claimName: pvc-static-report`. `kubectl get pod` → `Running`.
6. **UBU01.** `ls -l /srv/k8s-volumes/static-report` → там реально лежит `index.html`. Вот он, момент «данные вне пода».
7. **k8s-cp01.** `kubectl delete pod report-writer`, затем создаём под заново (тот же манифест, имя `report-writer`), `kubectl exec report-writer -- cat /data/index.html` → `hello`. Данные пережили смерть пода. Записываем это в `stand-log.md`.
8. **k8s-cp01.** Ставим провижионер:
   ```bash
   helm repo add nfs-subdir https://kubernetes-sigs.github.io/nfs-subdir-external-provisioner/
   helm repo update
   helm install nfs-provisioner nfs-subdir/nfs-subdir-external-provisioner \
     -n nfs-provisioner --create-namespace \
     --set nfs.server=10.10.10.30 \
     --set nfs.path=/srv/k8s-volumes/dynamic \
     --set storageClass.name=nfs-client \
     --set storageClass.reclaimPolicy=Delete \
     --set storageClass.archiveOnDelete=false
   kubectl -n nfs-provisioner get pods   # 1/1 Running
   kubectl get storageclass nfs-client
   ```
   `archiveOnDelete=false` ставим явно, чтобы при удалении PVC каталог именно удалялся, а не переименовывался в `archived-...`.
9. **k8s-cp01.** Делаем SC дефолтной (команда 4.7). Если кластер на k3s — снимаем дефолт с `local-path`:
   ```bash
   kubectl patch storageclass local-path -p \
     '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
   ```
   Ожидаемо: `kubectl get storageclass` → `nfs-client (default)`.
10. **k8s-cp01.** Применяем PVC `pvc-dynamic` **без** `storageClassName`, 1Gi. `kubectl describe pvc pvc-dynamic -n app` → в событиях `Provisioning` → `ProvisioningSucceeded`. В `kubectl get pv` появился PV с именем вида `pvc-<uid>`, на UBU01 — каталог `dynamic/app-pvc-dynamic-pvc-<uid>/`.
11. **k8s-cp01.** Применяем StatefulSet `web-cache` (4.9) с `replicas: 2`. Ожидаемо: поды `web-cache-0`, `web-cache-1` в `Running`, PVC `data-web-cache-0` и `data-web-cache-1` в `Bound`, у каждого свой каталог на NFS. В логах пода — пусто, а в файле `/data/heartbeat.log` копятся строки.
12. **k8s-cp01.** `kubectl delete pod web-cache-0` → под пересоздался с тем же именем, `kubectl exec web-cache-0 -- cat /data/heartbeat.log` → история на месте и продолжает расти. Это и есть контракт StatefulSet + PVC.
13. **k8s-cp01, разбор полётов.** Удаляем `pvc-dynamic` → PV исчез, каталог на NFS тоже (проверить на UBU01). Удаляем `pvc-static-report` → PV `pv-static-report` жив, статус `Released`, каталог на месте. Возвращаем его в строй патчем `claimRef` (4.10).
14. **Опционально, Longhorn (тяжело для ноутбука).** На всех трёх нодах: `sudo apt install -y open-iscsi nfs-common && sudo systemctl enable --now iscsid`, затем на `k8s-cp01`:
    ```bash
    helm repo add longhorn https://charts.longhorn.io && helm repo update
    helm install longhorn longhorn/longhorn -n longhorn-system --create-namespace
    kubectl -n longhorn-system get pods
    kubectl get storageclass longhorn
    ```
    Longhorn хочет минимум ~4 GiB RAM и 2 vCPU на ноду и отдельный диск под реплики `[ПРОВЕРИТЬ актуальные требования]`. Если ноды на 2 GiB — шаг пропускаем, читаем вывод `kubectl -n longhorn-system get pods` и не удивляемся `Pending`.

### Проверка

- [ ] `kubectl get pv` показывает `pv-static-report` со статусом `Bound`, reclaim policy `Retain`.
- [ ] `kubectl get pvc -n app` — все PVC в `Bound`, ни одного `Pending`.
- [ ] `showmount -e 10.10.10.30` отдаёт `/srv/k8s-volumes`.
- [ ] На UBU01 в `/srv/k8s-volumes/dynamic` лежат каталоги под каждый динамический PVC.
- [ ] `kubectl exec web-cache-1 -- df -h /data` показывает NFS-источник.
- [ ] После `kubectl delete pod web-cache-0` данные в `/data/heartbeat.log` не обнулились.
- [ ] `kubectl get storageclass` — ровно одна SC с `(default)`.
- [ ] В `stand-log.md` записаны: NFS-экспорт, имена SC, PV, PVC, StatefulSet.

### Сломай и почини

> **Пользователь:** ivanov / WIN11-CL01
> **Что написал:** «Сервис отчётов не открывается вообще, страница висит. Вчера работало.»
> **Срочность:** высокая

Смотрим `kubectl -n app get pods`: под сервиса отчётов в `ContainerCreating`. Реальная причина — сломан NFS.

**Воспроизводим поломку:** на `UBU01` — `sudo systemctl stop nfs-server`, затем на `k8s-cp01` — `kubectl -n app delete pod <под сервиса отчётов>`, чтобы он попытался пересоздаться и примонтировать том.

**Диагностика по порядку:**

1. `kubectl -n app describe pod <pod>` → в `Events`: `MountVolume.SetUp failed for volume "pvc-..." : mount failed: exit status 32`. Вывод: проблема не в приложении, а в монтировании.
2. `kubectl -n app get pvc` → PVC всё ещё `Bound`. Значит, метаданные целы, сломан именно бэкенд.
3. С рабочей ноды: `showmount -e 10.10.10.30` → `clnt_create: RPC: Program not registered` или таймаут. Хост не отдаёт экспорты.
4. На `UBU01`: `systemctl status nfs-server` → `inactive (dead)`.
5. Проверяем, что дело не в сети: `ss -lntp | sudo grep 2049` → пусто; `sudo exportfs -v` → экспорт есть, но сервис не слушает.

**Решение:** `sudo systemctl start nfs-server && sudo systemctl enable nfs-server`. Под с `ContainerCreating` сам «дозреет» в течение пары минут (kubelet повторяет монтирование с backoff до ~2 мин) — принудительно удалять его не нужно.

**Ответ пользователю:** «Сервис отчётов не открывался из-за недоступности сетевого хранилища на файловом сервере. Хранилище восстановлено, сервис работает, данные за прошлые дни на месте — они физически не были потеряны. Проверьте, пожалуйста, список отчётов».

**Закрытие заявки:** причина — остановленный `nfs-server` на `UBU01`; данных не потеряли, потому что PVC был `Bound`, а том — `Retain`. Отдельным тикетом заводим мониторинг `nfs-server` и алерт на `kubelet_volume_stats_available_bytes`.

### Что изменилось на стенде

- **UBU01:** установлен `nfs-kernel-server`, экспортируется `/srv/k8s-volumes` в 10.10.10.0/24; каталоги `static-report`, `dynamic`.
- **Кластер k8s:** неймспейс `app`; PV `pv-static-report` (Retain, SC `manual`); PVC/под `report-writer`; StatefulSet `web-cache` + PVC `data-web-cache-0/1`.
- **Кластер k8s:** неймспейс `nfs-provisioner`, Helm-релиз `nfs-provisioner`, StorageClass `nfs-client` — **дефолтная**; у `local-path` снят флаг default (если k3s).
- **Опционально:** неймспейс `longhorn-system`, StorageClass `longhorn`, пакет `open-iscsi` на нодах.
- **Снапшот:** `before-devops-039` на четырёх ВМ.

### Откат

GUI: VM → Snapshot Manager → выбрать `before-devops-039` → Go To. Или из CMD:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-039
```

Перед откатом ВМ должна быть выключена. Если откатывать не хочется, чистим аккуратно: `kubectl delete -n app sts/web-cache pvc --all pod/report-writer`, `helm uninstall nfs-provisioner -n nfs-provisioner`, `kubectl delete pv pv-static-report`, и только потом `sudo rm -rf` каталогов на UBU01.

## 6. Ошибки новичков и безопасность

1. **Писать данные в слой контейнера или `emptyDir`.** → Данные исчезают при любом пересоздании пода, а под пересоздаётся от обновления образа, переезда на другую ноду, OOM. → Всё, что переживает деплой, кладём в PVC.
2. **`hostPath` под бизнес-данные.** → Под переехал на другую ноду — данных нет; плюс `hostPath` ломает изоляцию, под получает доступ к хосту. → `hostPath` только для системных демонов (агенты, CNI), данные — PV.
3. **PVC прямо в Deployment при `replicas > 1`.** → Все реплики пишут в один и тот же том, файлы затирают друг друга, а при RWO часть подов вообще не стартует. → Для «свои данные у каждой реплики» — StatefulSet с `volumeClaimTemplates`.
4. **`reclaimPolicy: Delete` на проде без бэкапа.** → `kubectl delete pvc` стирает данные мгновенно и невозвратно. → Для БД и всего, что дорого, `Retain` + бэкап (Velero/снапшоты, тема урока 46).
5. **База данных на NFS.** → NFS не даёт нормальных гарантий `fsync` и блокировок; PostgreSQL/1С на NFS — классический способ получить битый файл данных. → БД — на блочном хранилище (Longhorn, Ceph RBD, локальный NVMe), NFS — для файлов, статики, RWX-обменников.
6. **Две дефолтные StorageClass.** → PVC без `storageClassName` уходит в случайную SC, часть томов оказывается на медленном бэкенде. → Одна дефолтная, остальные — явным именем.
7. **Longhorn/Ceph на одном диске и без `open-iscsi`.** → Реплики делят один физический диск (смысл репликации теряется), поды Longhorn висят в `CrashLoopBackOff`. → Отдельный диск на ноду, `open-iscsi` и `nfs-common` до установки.
8. **Не следить за заполнением томов.** → Том переполнился → под падает или файловая система уходит в read-only. → Алерты по `kubelet_volume_stats_available_bytes` и `kubelet_volume_stats_used_bytes`.

**Безопасность.** `no_root_squash` на NFS-экспорте означает: процесс с UID 0 внутри пода — это root на шаре, и он может писать в чужие каталоги. То же с каталогами в режиме 0777, которые создаёт провижионер по умолчанию. В боевой среде: экспорт только в подсеть кластера, `root_squash`, отдельный NFS-сервер вне прод-кластера, NetworkPolicy на поды, которые ходят на 2049, шифрование трафика (NFS over TLS/Kerberos или переход на CSI-драйвер с шифрованием), и обязательно `encryption at rest` на самом бэкенде — иначе ноутбук/диск с Ceph или Longhorn = все данные компании. В лабе мы этого не делаем сознательно и только на стенде.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Чем PV отличается от PVC и зачем это разделение?**

**Ответ:** PV — объект уровня кластера, описывающий конкретный ресурс хранения (ёмкость, режим доступа, политику возврата, путь/бэкенд). PVC — неймспейсный объект-заявка приложения на ресурс с определёнными требованиями. Разделение даёт две вещи: администратор описывает хранилище независимо от приложений, а разработчик описывает потребность, не зная, NFS там, Ceph или облачный диск. Связывание PV↔PVC — один-к-одному, по capacity ≥ запроса, accessModes, `storageClassName` и селекторам.
**Что хочет услышать интервьюер:** понимание, что PVC — это абстракция для разработчика и точка отвязки от конкретного вендора.
**Красный флаг:** «PV — это диск, PVC — это папка» или «это одно и то же, просто разные имена».

**2. Что такое StorageClass и как работает динамическое провижионирование? Чем `Immediate` отличается от `WaitForFirstConsumer`?**

**Ответ:** StorageClass описывает класс хранения: провижионер, параметры бэкенда, `reclaimPolicy`, `volumeBindingMode`, `allowVolumeExpansion`. Динамическое провижионирование: PVC без готового PV ссылается на SC, внешний провижионер по её параметрам создаёт PV, контроллер делает `VolumeAttachment`, kubelet монтирует том в под. `volumeBindingMode: Immediate` создаёт том сразу при появлении PVC; `WaitForFirstConsumer` — только когда появился под, которому том нужен, и сразу в зоне/на ноде этого пода. Второй режим обязателен для локальных и зональных дисков, иначе том создастся не в той зоне и под не запустится.
**Что хочет услышать интервьюер:** что вы понимаете проблему «том создан не там, где под».
**Красный флаг:** «WaitForFirstConsumer — это про кэш» или путаница с `podAffinity`.

**3. Что такое CSI и из чего состоит драйвер?**

**Ответ:** Container Storage Interface — стандарт gRPC-интерфейса между Kubernetes и драйвером хранилища, снявший необходимость встраивать каждый тип тома в код Kubernetes. Драйвер реализует три сервиса: Identity (информация о драйвере и возможностях), Controller (CreateVolume, DeleteVolume, ControllerPublish, снапшоты, resize) и Node (NodeStageVolume, NodePublishVolume, NodeGetCapabilities). На стороне control plane с драйвером работают sidecar-контейнеры: external-provisioner, external-attacher, external-snapshotter, external-resizer; на ноде — node-driver-registrar, который регистрирует unix-сокет драйвера в kubelet. Состояние видно в объектах `CSIDriver`, `CSINode`, `VolumeAttachment`.
**Что хочет услышать интервьюер:** понимание, что драйвер — это под, а не «магия внутри kubelet».
**Красный флаг:** «CSI — это про контейнеры» или незнание, что `VolumeAttachment` существует.

**4. Какие бывают reclaim policy и что выбрать для базы данных?**

**Ответ:** `Delete` — при удалении PVC удаляется и том вместе с данными; `Retain` — PV остаётся в статусе `Released`, данные целы, новый PVC к нему не привяжется без ручного вмешательства (снять `claimRef`); `Recycle` устарел и в CSI не поддерживается. Для БД — `Retain` плюс регулярные бэкапы: `Retain` спасает от случайного `kubectl delete pvc`, но не спасает от порчи данных, потери ноды или ошибки приложения. Политика задаётся на StorageClass и при необходимости переопределяется на конкретном PV.
**Что хочет услышать интервьюер:** что бэкап обязателен и `Retain` — не замена бэкапу.
**Красный флаг:** «Retain — это и есть бэкап».

**5. Как выбрать хранилище: NFS, Longhorn или Ceph/Rook?**

**Ответ:** Смотрю на три вещи: какие режимы доступа нужны, есть ли требования к блочному доступу/снапшотам/производительности и сколько ресурсов можно потратить. NFS — самый дешёвый вариант, даёт RWX, легко подключается к любому NAS, но нет снапшотов, квот и блочной семантики; годится для статики, обменников, артефактов. Longhorn — блочные тома поверх iSCSI с репликацией, снапшотами и бэкапом в S3, ставится в кластер как обычное приложение; хороший выбор для лаборатории, edge и небольших кластеров, но требует `open-iscsi`, RAM/CPU и отдельных дисков. Ceph/Rook — полноценная распределённая СХД: RBD (блок), CephFS (файлы), RGW (S3), самовосстановление; оправдан при 3+ нодах, «сырых» дисках и реальных требованиях к отказоустойчивости, иначе это дорогая и хрупкая игрушка. В облаке — managed CSI-драйвер провайдера, если нет причин тащить своё.
**Что хочет услышать интервьюер:** что выбор идёт от требований, а не от модности инструмента.
**Красный флаг:** «Ceph всегда, он же самый надёжный» без разговора о ресурсах и эксплуатации.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Storage*: статьи «Persistent Volumes», «PersistentVolumeClaim», «Storage Classes», «Volumes», «CSI Volume Cloning», «Volume Snapshots». Поиск: `kubernetes persistent volumes docs`, `kubernetes storage classes volumeBindingMode`.
- **kubernetes.io** — *Concepts / Storage / Ephemeral Volumes*: «emptyDir», «hostPath». Поиск: `kubernetes emptydir hostpath docs`.
- **kubernetes-csi.github.io** — сайт проекта CSI: спецификация и «CSI Drivers». Поиск: `container storage interface spec kubernetes-csi`.
- **github.com/kubernetes-sigs/nfs-subdir-external-provisioner** — README и каталог `deploy/`: параметры чарта, `archiveOnDelete`, RBAC. Поиск: `nfs subdir external provisioner helm chart values`. Названия файлов в `deploy/` на момент выхода главы `[ПРОВЕРИТЬ]`.
- **longhorn.io** — *Longhorn Documentation*: «Installation», «Prerequisites», «StorageClass», «Snapshots and Backups». Поиск: `longhorn prerequisites open-iscsi install helm`.
- **rook.io** — *Rook Documentation*: «Ceph Cluster», «Ceph Block Storage (RBD)», «Ceph Filesystem», «Ceph Object Storage». Поиск: `rook ceph cluster prerequisites raw devices`.
- **helm.sh** — *Docs → Using Helm*: команды `helm repo add`, `helm install`, `helm uninstall`. Поиск: `helm install chart namespace values`.
- **docs.k8s.io / kubernetes.io** — *Storage Capacity*, «Volume Expansion». Поиск: `kubernetes allowVolumeExpansion resize pvc`.

Обязательно сверьте актуальные версии чартов и требования к ресурсам перед установкой Longhorn/Rook — они меняются от релиза к релизу `[ПРОВЕРИТЬ]`.
