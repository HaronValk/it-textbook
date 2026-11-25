# Системы хранения: SAN/NAS, RAID-уровни, ZFS, Ceph, NetApp/Huawei/российские СХД, репликация

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в HelpDesk:**

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Здравствуйте! Не открывается `\\FS01\HR` — пишет «Сетевое имя больше не доступно». Мне до конца дня нужно разослать приказы, а папки нет. У коллег на складе `\\FS01\Obshchee` открывается нормально».
> **Срочность:** высокая

Пока вы разбираете эту заявку, в голове должна выстроиться цепочка: WIN10-CL02 → сеть → FS01 → том D: → iSCSI-сессия → LUN на UBU01 → zvol → ZFS-пул → физические диски. Ошибка может сидеть на любом уровне, и правильный порядок диагностики — «снизу вверх, от хранилища к пользователю» — экономит часы.

Хранилище — это то, что в ИТ ломается тише всего и больнее всего. Диски умирают постоянно, RAID рассыпается, LUN отваливается после ребута, пул ZFS уходит в деградацию, а «бэкап на том же массиве» оказывается не бэкапом. После этой главы вы должны: понимать разницу между SAN, NAS и объектным хранилищем; считать полезную ёмкость RAID и осознанно выбирать уровень; поднимать mdadm-массив и ZFS-пул, читать их состояние; отдавать блочное устройство по iSCSI и файловый доступ по NFS; настраивать репликацию через снапшоты; знать, за что отвечают NetApp, Huawei и российские СХД; и объяснять на собеседовании, почему «RAID — это не бэкап».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Три способа отдать данные.** Хранилище различается по тому, что получает потребитель.

```text
Файловый доступ (NAS): клиент видит ПАПКУ
   приложение → SMB 445 (Windows) / NFS 2049 (Linux) → NAS-сервер (своя ФС: NTFS/ext4/ZFS) → диски

Блочный доступ (SAN): клиент видит ДИСК
   приложение/гипервизор → iSCSI 3260 / FC / NVMe-oF → массив → LUN
   на клиенте появляется /dev/sdX или Disk 1, файловую систему создаёт ОН САМ

Объектный доступ: клиент видит БАКЕТ и ключ
   приложение → S3 API (443) → шлюз (Ceph RGW, MinIO) → пулы
```

Ключевое следствие: NAS — это «сервер с шарами», файловая система одна и живёт на NAS. SAN — это «кусок диска по сети», и каждый клиент форматирует его под себя. Отсюда правило: один LUN нельзя одновременно подключать двумя серверами с правом записи (кроме кластерных ФС и специальных сценариев вроде HyperMetro/active-active) — получите разрушение ФС.

**Протоколы и порты.** iSCSI — TCP/3260, «SCSI по IP». Fibre Channel — отдельная сеть на оптике или витой паре (FCoE), на IP-порты не завязана. NFSv4 — TCP/2049 (плюс rpcbind/111 для вспомогательных служб NFSv3). SMB — TCP/445. NVMe-oF/TCP — 4420. S3 — 443/80. Ceph: MON 3300/6789, OSD 6800–7300.

**RAID.** RAID — избыточность за счёт дисков, не бэкап.

| Уровень | Мин. дисков | Полезная ёмкость | Отказов | Зачем |
|---|---|---|---|---|
| RAID 0 | 2 | 100% | 0 — потеря всего | кэш, временные данные |
| RAID 1 | 2 | 50% | 1 | ОС, журналы, малые массивы |
| RAID 5 | 3 | (n−1)/n | 1 | архивы на HDD до ~2 ТБ |
| RAID 6 | 4 | (n−2)/n | 2 | большие HDD, «холодный» архив |
| RAID 10 | 4 | 50% | до 1 в каждом зеркале | БД, диски ВМ |
| RAIDZ1/2/3 | 3/4/5 | примерно как 5/6 | 1/2/3 | ZFS вместо аппаратного RAID |

Две мины: **rebuild** на большом HDD идёт часами и сутками (в это время массив уязвим), и **write hole** у RAID5/6 — при пропадании питания часть страйпа может остаться несогласованной. Плюс «тихие» ошибки чтения: диск отдаёт неверные данные, ни RAID5, ни RAID6 этого не замечают — нужны контрольные суммы (ZFS, ReFS, Ceph).

**ZFS.** Это файловая система и менеджер томов в одном: пул (zpool) собирается из vdev'ов (зеркала, RAIDZ, одиночные диски), внутри — датасеты и zvol'ы. Фишки: copy-on-write, контрольные суммы во всех блоках, самовосстановление в зеркалах/RAIDZ, снапшоты почти бесплатно, сжатие (lz4/zstd), ARC в памяти, SLOG (быстрый лог для синхронной записи) и L2ARC (кэш на SSD). Расширять пул можно только добавлением новых vdev'ов — «долить один диск в RAIDZ» нельзя (в новых версиях появился `raidz expansion`, но это не серебряная пуля). Аппаратный RAID под ZFS не нужен и вреден: ZFS должен видеть диски сам.

**Ceph.** Распределённая система: OSD (диски), MON (кворум и карта), MDS (CephFS), RGW (S3). Данные режутся на PG и раскладываются алгоритмом CRUSH по OSD; отказоустойчивость — реплики (обычно 3) или erasure coding. Разворачивание кластера Ceph в Proxmox мы разбирали в уроке 35, здесь важно только место в картине: Ceph — это software-defined SAN/NAS/object «из коробок».

**Вендоры.** NetApp ONTAP: собственная ФС WAFL, агрегаты → тома → LUN, снапшоты, SnapMirror (репликация) и SnapVault; умеет всё — FC, iSCSI, NFS, SMB, S3. Huawei OceanStor Dorado (all-flash, active-active HyperMetro, репликация HyperReplication), OceanStor Pacific (scale-out). Российские: YADRO «Яхонт»/TATLIN, «Аэродиск», RAIDIX (программный RAID), СХД «Булат» — [ПРОВЕРИТЬ] актуальные модели и наличие в реестре Минпромторга перед закупкой; в 2026-м смотрите не только на «российскость», но и на сертификацию ФСТЭК, поддержку в РФ и совместимость с вашей платформой виртуализации.

**Репликация.** Синхронная: запись подтверждается после записи на обе площадки, RPO = 0, но растёт задержка — рабочие дистанции десятки километров. Асинхронная: RPO — минуты, задержка не растёт. Снапшот-репликация (ZFS `send`, SnapMirror) гоняет только изменения. Active-active (HyperMetro, vMSC) — обе площадки пишут одновременно. Отдельно: **снапшот — не бэкап**, он лежит на том же массиве и умрёт вместе с ним.

## 3. Аналогия из жизни

NAS — это **шкаф с документами и вахтёром**: вы говорите «дайте папку HR за март», вахтёр сам идёт и приносит. Вам не важно, на какой полке лежит лист. SAN — это **участок земли, который вам выделили**: вы получаете надел и строите на нём что хотите — дом, склад, бетонный забор. Землю дали, а что вы там построите и не зальёте ли фундаментом соседскую межу — ваша ответственность; поэтому два человека на одном участке с правом застройки быстро получают катастрофу. Репликация — это **второй офис с копией архива**: синхронная — курьер стоит и ждёт, пока копию поставят на полку (медленно, но копия есть всегда); асинхронная — документы уезжают раз в час (быстро, но час работы потеряется). Где аналогия ломается: у реального RAID деградация начинается не с «поломки полки», а с тихой ошибки чтения, которую никто не замечает, пока не запустишь scrub.

## 4. Практика и команды

**1. Что за диски у нас в системе (Linux).**
```bash
lsblk -o NAME,SIZE,TYPE,ROTA,MODEL,SERIAL,MOUNTPOINT
```
`ROTA=1` — HDD (вращающийся), `0` — SSD. `TYPE` покажет `disk/part/lvm/raid` — сразу видно, есть ли уже массив. Смотрим на `MODEL` и `SERIAL`: по серийнику меняют диск в серверной.

**2. Здоровье диска (SMART).**
```bash
sudo smartctl -a /dev/sdb | egrep 'SMART overall|Reallocated|Pending|Offline_Uncorrectable'
```
`-a` — вся информация. Ключевые атрибуты: `Reallocated_Sector_Ct`, `Current_Pending_Sector`, `Offline_Uncorrectable`. Рост pending-секторов — диск «посыпался», менять. На виртуальных дисках VMware SMART недоступен — увидите `SMART support is: Unavailable` [ПРОВЕРИТЬ на вашей версии]; это нормально, в лабе мы смотрим только на наличие устройства.

**3. Сборка массива mdadm (RAID1).**
```bash
sudo apt install -y mdadm
sudo mdadm --create /dev/md0 --level=1 --raid-devices=2 /dev/sdb /dev/sdc
cat /proc/mdstat
```
`--level=1` — зеркало, `--raid-devices=2` — сколько устройств в массиве. В `/proc/mdstat` смотрим строку `[UU]`: две `U` — оба диска живы, `[U_]` — один выпал. Там же `resync = 12.3%` — идёт синхронизация.

**4. Детали массива и состояние.**
```bash
sudo mdadm --detail /dev/md0
```
`State : clean` — норма; `degraded` — нет одного диска; `recovering` — идёт rebuild. `Active Devices : 2`, `Failed Devices : 0`, `Resync Status` — то, что вы зачитываете в тикет.

**5. Имитация отказа и замена диска.**
```bash
sudo mdadm /dev/md0 --fail /dev/sdb --remove /dev/sdb
sudo mdadm --zero-superblock /dev/sdb
sudo mdadm /dev/md0 --add /dev/sdb
watch -n2 cat /proc/mdstat
```
`--fail` помечает диск сбойным, `--remove` выдёргивает из массива, `--zero-superblock` стирает метаданные (иначе `--add` откажется), `--add` возвращает и запускает rebuild.

**6. Создание ZFS-пула и датасета.**
```bash
sudo apt install -y zfsutils-linux
sudo zpool create -f -o ashift=12 tank mirror /dev/sdd /dev/sde
sudo zfs create tank/data
sudo zpool status -v
```
`ashift=12` — блок 4К, обязательный минимум для современных дисков (иначе убитая производительность). `mirror` — vdev из двух дисков. `zpool status -v` смотрим `state: ONLINE`, `errors: No known data errors`, и что `scan` показывал scrub.

**7. Сжатие и свойства датасета.**
```bash
sudo zfs set compression=lz4 tank/data
sudo zfs set atime=off tank
sudo zfs get compression,used,available tank/data
```
`lz4` — почти бесплатное сжатие, включает дедупликацию «по факту» для текста и логов. `atime=off` убирает лишние записи при чтении.

**8. Снапшоты и репликация (zfs send/receive).**
```bash
sudo zfs snapshot tank/data@snap1
sudo zfs send tank/data@snap1 | sudo zfs receive backup/data
echo "new" | sudo tee /tank/data/file2.txt
sudo zfs snapshot tank/data@snap2
sudo zfs send -i tank/data@snap1 tank/data@snap2 | sudo zfs receive backup/data
sudo zfs list -t snapshot -r backup
```
`-i` — инкремент от базы. Это и есть «репликация бедного человека»: гоняем только дельту. В продакшене оборачивается в скрипт + cron/systemd-timer + проверка кода возврата.

**9. Проверка целостности и что изменилось.**
```bash
sudo zpool scrub tank
sudo zpool status -v
sudo zfs diff tank/data@snap1 tank/data@snap2
```
`scrub` читает все блоки и сверяет контрольные суммы — единственный способ поймать «тихую» порчу. `zfs diff` покажет список изменённых/добавленных/удалённых файлов между снапшотами (удобно в разборе «кто удалил папку»).

**10. Файловый доступ по NFS (NAS).**
```bash
sudo apt install -y nfs-kernel-server
echo '/tank/nfs 10.10.10.0/24(rw,sync,no_subtree_check)' | sudo tee -a /etc/exports
sudo exportfs -ra && sudo systemctl enable --now nfs-server
showmount -e 10.10.10.30                 # на клиенте
sudo mount -t nfs 10.10.10.30:/tank/nfs /mnt/nfs-vektor
```
`exportfs -ra` перечитывает `/etc/exports`, `showmount -e` показывает, что реально отдаётся. `no_subtree_check` — стандарт для простых шар.

**11. Блочный доступ по iSCSI: цель (target) на UBU01.**
```bash
sudo apt install -y targetcli-fb
sudo zfs create -V 2G -o volblocksize=16k tank/lun0
sudo targetcli
/> backstores/block create name=lun0 dev=/dev/zvol/tank/lun0
/> iscsi/ create iqn.2026-01.lab.vektor:ubu01.san
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/portals/ delete 0.0.0.0 3260
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/portals/ create 10.10.10.30 3260
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/luns/ create /backstores/block/lun0
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/acls/ create <IQN инициатора FS01>
/> saveconfig
```
`backstore` — что отдаём (zvol как блочное устройство), `lun` — публикация, `acl` — кому именно (по IQN), `saveconfig` — чтобы конфиг выжил перезагрузку. Без ACL цель открыта всем — в лабе ок, в продакшене нет.

**12. Инициатор iSCSI на Windows (FS01).**
```powershell
Get-InitiatorPort
Connect-IscsiTarget -NodeAddress "iqn.2026-01.lab.vektor:ubu01.san" `
  -TargetPortalAddress 10.10.10.30 -IsPersistent $true
Update-HostStorageCache
Get-Disk | Format-Table Number,FriendlyName,OperationalStatus,PartitionStyle
Get-IscsiSession | Format-Table TargetNodeAddress,IsConnected
```
`-IsPersistent $true` — переподключаться после ребута (без этого LUN не вернётся, и это классическая причина утренней аварии). `Update-HostStorageCache` заставляет Windows перечитать список дисков.

**13. Диски и пулы на Windows (Storage Spaces / физические диски).**
```powershell
Get-PhysicalDisk | Format-Table FriendlyName,MediaType,Size,HealthStatus
Get-StoragePool
Get-VirtualDisk | Format-Table FriendlyName,ResiliencySettingName,Size,HealthStatus
Get-Volume | Format-Table DriveLetter,FileSystemLabel,SizeRemaining,HealthStatus
Get-SmbShare | Format-Table Name,Path
```
`ResiliencySettingName` = `Mirror` / `Parity` — это, соответственно, аналог RAID1/RAID10 и RAID5 в Storage Spaces. `HealthStatus` должен быть `Healthy`; `Warning` — уже повод разбираться.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS) — живой, SSH под `adm.student`, интернет/локальный репозиторий для `apt`.
- **FS01** (10.10.10.20, Windows Server 2022) — роль файлового сервера настроена, шары `Obshchee`, `Buh`, `HR` существуют, группа `GG-HR` имеет доступ к `HR`.
- **PVE01** (10.10.10.70, Proxmox VE) — установлен на прошлых уроках, доступен по SSH.
- **WIN10-CL02** (WIN10-CL02, учётка `petrova`) — для проверки доступа к шаре.
- Пароль стенда: `Lab#2026-vektor`. Все имена машин, папок и групп — строго из описания стенда.

### Снапшот
В VMware Workstation: выделить ВМ → **VM → Snapshot → Take Snapshot** (или иконка камеры на панели), имя `before-sysadmin-037`. Сделать на **UBU01**, **FS01**, **PVE01**. У WIN10-CL02 снапшот не обязателен — она не меняется, но можно снять и её для единообразия.

### Шаги

1. **UBU01: добавить диски.** Выключить ВМ → **VM Settings → Add → Hard Disk → SCSI → Create a new virtual disk → 2 GB → Store virtual disk as a single file**. Добавить **пять** дисков. Включить ВМ.
   *Ожидаемо:* в системе появились `/dev/sdb`…`/dev/sdf`.

2. **UBU01: убедиться, что видно.**
```bash
lsblk -o NAME,SIZE,TYPE,ROTA,SERIAL
```
*Ожидаемо:* пять дисков по 2 ГБ без разделов и точек монтирования.

3. **UBU01: RAID1 через mdadm.**
```bash
sudo apt install -y mdadm
sudo mdadm --create /dev/md0 --level=1 --raid-devices=2 /dev/sdb /dev/sdc
sudo mkfs.ext4 -L mdraid /dev/md0
sudo mkdir -p /srv/mdraid && sudo mount /dev/md0 /srv/mdraid
echo "test-$(date)" | sudo tee /srv/mdraid/proof.txt
cat /proc/mdstat
```
*Ожидаемо:* строка `md0 : active raid1 sdc[1] sdb[0]` и `[UU]`, файл создан.

4. **UBU01: сломать и починить диск массива.**
```bash
sudo mdadm /dev/md0 --fail /dev/sdb --remove /dev/sdb
cat /proc/mdstat
sudo mdadm --zero-superblock /dev/sdb
sudo mdadm /dev/md0 --add /dev/sdb
watch -n2 cat /proc/mdstat
```
*Ожидаемо:* сначала `[U_]` и `degraded`, после `--add` — `recovery = …` и снова `[UU]`. Файл `proof.txt` читается всё время.

5. **UBU01: ZFS-пул из зеркала.**
```bash
sudo apt install -y zfsutils-linux
sudo zpool create -f -o ashift=12 tank mirror /dev/sdd /dev/sde
sudo zfs create tank/data
sudo zfs create tank/nfs
sudo zfs set compression=lz4 tank
sudo zfs set atime=off tank
sudo zpool status -v
```
*Ожидаемо:* `state: ONLINE`, `mirror-0` из двух устройств, `errors: No known data errors`. Датасеты смонтированы в `/tank/data` и `/tank/nfs`.

6. **UBU01: снапшот и репликация на второй пул.**
```bash
sudo zpool create -f backup /dev/sdf
echo "v1" | sudo tee /tank/data/doc.txt
sudo zfs snapshot tank/data@snap1
sudo zfs send tank/data@snap1 | sudo zfs receive backup/data
echo "v2" | sudo tee /tank/data/doc.txt
sudo zfs snapshot tank/data@snap2
sudo zfs send -i tank/data@snap1 tank/data@snap2 | sudo zfs receive backup/data
sudo zfs list -t snapshot -r backup
sudo zfs diff tank/data@snap1 tank/data@snap2
```
*Ожидаемо:* на `backup` два снапшота, `doc.txt` содержит `v2`, `zfs diff` показывает `M /tank/data/doc.txt`. Одиночный диск `backup` — только для лабы: в реальности это было бы зеркало на другой площадке.

7. **UBU01 → PVE01: NAS по NFS.**
```bash
# на UBU01
sudo apt install -y nfs-kernel-server
echo '/tank/nfs 10.10.10.0/24(rw,sync,no_subtree_check)' | sudo tee -a /etc/exports
sudo exportfs -ra && sudo systemctl enable --now nfs-server
# на PVE01
sudo mkdir -p /mnt/nfs-vektor
sudo mount -t nfs 10.10.10.30:/tank/nfs /mnt/nfs-vektor
df -hT /mnt/nfs-vektor
```
*Ожидаемо:* на PVE01 `df -hT` показывает тип `nfs4` и объём пула.

8. **FS01: узнать свой IQN инициатора.**
```powershell
Get-InitiatorPort | Format-Table NodeAddress,ConnectionType
```
*Ожидаемо:* строка вида `iqn.1991-05.com.microsoft:fs01-...` — её нужно вписать в ACL на следующем шаге.

9. **UBU01: опубликовать LUN по iSCSI.**
```bash
sudo apt install -y targetcli-fb
sudo zfs create -V 2G -o volblocksize=16k tank/lun0
sudo targetcli
/> backstores/block create name=lun0 dev=/dev/zvol/tank/lun0
/> iscsi/ create iqn.2026-01.lab.vektor:ubu01.san
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/portals/ delete 0.0.0.0 3260
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/portals/ create 10.10.10.30 3260
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/luns/ create /backstores/block/lun0
/> iscsi/iqn.2026-01.lab.vektor:ubu01.san/tpg1/acls/ create <IQN FS01 из шага 8>
/> saveconfig
/> exit
```
Затем проверить, что порт слушается: `sudo ss -lntp | grep 3260`. Если включён `ufw` — `sudo ufw allow 3260/tcp`.
*Ожидаемо:* цель создана, LUN привязан, ACL содержит IQN FS01.

10. **FS01: подключить LUN, отформатировать, перевесить шару HR.**
```powershell
Connect-IscsiTarget -NodeAddress "iqn.2026-01.lab.vektor:ubu01.san" `
  -TargetPortalAddress 10.10.10.30 -IsPersistent $true
Update-HostStorageCache
Get-Disk | Format-Table Number,FriendlyName,OperationalStatus,Size
Initialize-Disk -Number <N> -PartitionStyle GPT
New-Partition -DiskNumber <N> -UseMaximumSize -DriveLetter D
Format-Volume -DriveLetter D -FileSystem NTFS -NewFileSystemLabel HRDATA -Confirm:$false
```
Затем перенести содержимое HR на новый том, пересоздать шару и права:
```powershell
robocopy "<текущий путь HR из прошлых глав, например C:\Shares\HR>" D:\HR /MIR /COPYALL
Remove-SmbShare -Name HR -Force
New-SmbShare -Name HR -Path D:\HR -FullAccess "VEKTOR\GG-HR"
icacls D:\HR /grant "VEKTOR\GG-HR:(OI)(CI)M"
```
*Ожидаемо:* диск D: на LUN, шара `HR` указывает на `D:\HR`. Проверка: на WIN10-CL02 под `petrova` открыть `\\FS01\HR`, создать/прочитать файл. **Записать в `stand-log.md`, что HR теперь физически на LUN с UBU01.**

11. **Зафиксировать состояние.** Записать в `stand-log.md` всё из раздела «Что изменилось». Проверить стрелками в голове всю цепочку: LUN → iSCSI → NTFS → SMB → WIN10-CL02.

### Проверка
- [ ] `cat /proc/mdstat` на UBU01: `md0` в состоянии `[UU]`, без `recovery`.
- [ ] `sudo zpool status -v`: `tank` — `ONLINE`, `errors: No known data errors`; `backup` — `ONLINE`.
- [ ] `sudo zfs list -t snapshot -r backup` показывает `snap1` и `snap2`.
- [ ] `showmount -e 10.10.10.30` с PVE01 отдаёт `/tank/nfs 10.10.10.0/24`.
- [ ] `mount | grep nfs` на PVE01 показывает смонтированный NFS.
- [ ] `Get-IscsiSession` на FS01: `IsConnected = True`, `IsPersistent = True`.
- [ ] `Get-Volume` на FS01: диск `D:` с меткой `HRDATA`, `HealthStatus = Healthy`.
- [ ] `petrova` на WIN10-CL02 открывает и пишет в `\\FS01\HR`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «`\\FS01\HR` не открывается — «Сетевое имя больше не доступно». `\\FS01\Obshchee` при этом открывается. Срочно, у меня приказы на подпись».
> **Срочность:** высокая

**Как воспроизвести (на FS01, от админа):**
```powershell
Disconnect-IscsiTarget -NodeAddress "iqn.2026-01.lab.vektor:ubu01.san" -Confirm:$false
Get-Disk | Format-Table Number,OperationalStatus
```
Диск пропадает, том `D:` уходит, шара `HR` остаётся, но путь недействителен. `Obshchee` живёт на другом томе — поэтому работает. Именно эта деталь («одна шара работает, другая нет») — главная зацепка.

**Диагностика по порядку — снизу вверх:**
1. **Хранилище на UBU01:** `sudo zpool status -v` — пул `ONLINE`, ошибок нет? Значит проблема не в дисках.
2. **Цель iSCSI:** `sudo targetcli ls` — LUN и ACL на месте, `sudo ss -lntp | grep 3260` — порт слушается.
3. **Сеть:** с FS01 `Test-NetConnection 10.10.10.30 -Port 3260` → `TcpTestSucceeded: True`. Если False — firewall/маршрут.
4. **Инициатор на FS01:** `Get-IscsiSession` — сессии нет. Смотрим журнал: `Get-WinEvent -LogName System -MaxEvents 50 | Where-Object {$_.ProviderName -match 'iSCSI|msiscsi'}`.
5. **Вывод:** сессия была разорвана, `IsPersistent` в момент подключения либо не был выставлен, либо сессия снята вручную. Диски на LUN отваливаются, шара ломается.

**Решение:**
```powershell
Connect-IscsiTarget -NodeAddress "iqn.2026-01.lab.vektor:ubu01.san" `
  -TargetPortalAddress 10.10.10.30 -IsPersistent $true
Update-HostStorageCache
Get-Disk | Format-Table Number,OperationalStatus
Set-Disk -Number <N> -IsOffline $false
Set-Disk -Number <N> -IsReadOnly $false
Get-Volume | Format-Table DriveLetter,FileSystemLabel,HealthStatus
Get-SmbShare -Name HR
```
Проверить с WIN10-CL02 под `petrova`, что `\\FS01\HR` открывается и файл сохраняется.

**Ответ пользователю:** «Мария, добрый день. Папка HR была недоступна из-за потери связи файлового сервера с дисковым массивом. Связь восстановили, данные на месте, доступ проверен. Если увидите ошибку снова — сразу напишите, дежурный посмотрит сессию».

**Закрытие заявки:** категория «Инфраструктура / Хранилище», в решении указать: «разрыв iSCSI-сессии FS01↔UBU01, восстановлено переподключением с `IsPersistent`, LUN переведён в Online». Завести задачу: мониторинг сессий iSCSI и алерт на `zpool` в деградации, чтобы узнавать об этом раньше пользователя.

### Что изменилось на стенде
- **UBU01:** добавлено 5 виртуальных дисков по 2 ГБ (`/dev/sdb`…`/dev/sdf`); массив `/dev/md0` (RAID1, `/dev/sdb`+`/dev/sdc`), ext4, метка `mdraid`, смонтирован в `/srv/mdraid`; ZFS-пул `tank` (mirror `/dev/sdd`+`/dev/sde`, `ashift=12`, `compression=lz4`, `atime=off`), датасеты `tank/data`, `tank/nfs`, zvol `tank/lun0` (2 ГБ); ZFS-пул `backup` (`/dev/sdf`) с репликой `backup/data` и снапшотами `snap1`, `snap2`; NFS-экспорт `/tank/nfs` в `10.10.10.0/24`; iSCSI-цель `iqn.2026-01.lab.vektor:ubu01.san` на 10.10.10.30:3260 с ACL для FS01.
- **PVE01:** смонтирован NFS `/mnt/nfs-vektor` с UBU01.
- **FS01:** подключён iSCSI-инициатор к UBU01 (persistent), появился диск `Disk <N>` → том `D:` (NTFS, метка `HRDATA`); **шара `HR` физически перенесена на `D:\HR`, права `GG-HR` пересозданы**.

### Откат
Выключить затрагиваемые ВМ → **VM → Snapshot → Snapshot Manager** → выбрать `before-sysadmin-037` → **Go to** (или **Revert**). После отката FS01 вернётся к прежнему расположению шары `HR` (на исходном томе) — убедиться, что `petrova` открывает `\\FS01\HR`, и записать откат в `stand-log.md`. Если нужно сохранить наработки, а убрать только LUN: на FS01 `Disconnect-IscsiTarget`, вернуть шару `HR` на старый путь и пересоздать через `New-SmbShare`.

## 6. Ошибки новичков и безопасность

1. **«RAID — это бэкап»** → при пожаре/шифровальщике/ошибочном `rm -rf` массив умирает целиком → правильные ожидания: RAID = доступность, бэкап = отдельная копия по 3-2-1-1-0 (урок 38).
2. **RAID5 на дисках 8–16 ТБ** → rebuild сутками, второй диск не выдерживает, URE добивает массив → берите RAID6/RAID10/RAIDZ2 или зеркала.
3. **Аппаратный RAID с write-back и без батареи/суперкапа** → потеря данных при сбое питания → проверять наличие BBU/NVRAM и политику кэша.
4. **ZFS поверх аппаратного RAID** → ZFS не видит отдельные диски, самовосстановление и `scrub` бесполезны → отдавайте ZFS диски напрямую (HBA/JBOD).
5. **Один LUN — двум серверам «для скорости»** → разрушение файловой системы → один LUN = один писатель, либо кластерная ФС/active-active массив.
6. **iSCSI без `IsPersistent` и без ACL/CHAP** → после ребута LUN не вернулся, а цель открыта всем в сети → persistent-логин, ACL по IQN, CHAP или выделенный VLAN.
7. **Снапшоты вместо бэкапа и без ротации** → пул забит снапшотами, запись встала → автоснапшоты с retention-политикой и отдельная реплика.
8. **Не настроен `scrub`/SMART-мониторинг** → «тихая» порча данных замечается через год → ежемесячный scrub, SMART-алерты, scrub-отчёт в мониторинге.

**Безопасность.** В лабе мы сознательно делаем небезопасные вещи: iSCSI без CHAP, NFS с `no_root_squash`-подобной логикой доступа по подсети, права `GG-HR` через `FullAccess`. В боевой среде: iSCSI — только в отдельном VLAN с ACL и CHAP, NFS — с `root_squash`, `sec=krb5` через Kerberos (у нас есть FreeIPA/AD из урока 30), SMB — SMB3 с подписью и шифрованием, шары — через группы, а не «Все». Для данных с персональными данными — учёт 152-ФЗ: сегментация, журналирование доступа к шарам, шифрование на уровне СХД (SED-диски, шифрование массивом), опять же без «лишних» прав у админов.

## 7. Вопросы на собеседовании

**1. «Чем SAN отличается от NAS и что вы выберете для 200 виртуалок?»**
Ответ: NAS отдаёт файлы (SMB/NFS), файловой системой управляет само хранилище; SAN отдаёт блоки (iSCSI/FC/NVMe-oF), ФС создаёт клиент. Для 200 виртуалок — блочный доступ: iSCSI/FC LUN под VMFS/Proxmox LVM или, лучше, shared storage (Ceph/vSAN/массив с поддержкой кластера), чтобы работали HA и живая миграция. NAS годится для ISO, шаблонов и бэкапов.
*Что хочет услышать:* понимание «файл vs блок», привязку к HA/миграциям, а не заученное определение.
*Красный флаг:* «SAN — это про оптику, NAS — про Ethernet» (технически бывает и наоборот).

**2. «RAID5 против RAID10 — что возьмёте под базу 1С и почему?»**
Ответ: RAID10. Запись и rebuild быстрее, нет write hole, предсказуемая latency. RAID5 даст больше ёмкости, но на запись страдает (read-modify-write + чётность) и rebuild долгий — для СУБД риск. Если ёмкость критична — RAID6/RAIDZ2, но всё равно не под горячую базу.
*Что хочет услышать:* trade-off ёмкость/производительность/риск, а не «RAID5 экономит диск».
*Красный флаг:* «RAID5 всегда лучше, потому что дешевле».

**3. «Пул ZFS в состоянии DEGRADED, приложение работает. Ваши действия?»**
Ответ: `zpool status -v` — понять, какой vdev/диск выпал; проверить SMART; заменить диск и `zpool replace`; убедиться, что идёт resilver и `errors: No known data errors`; после — `zpool scrub`; проверить, что есть свежая реплика/бэкап. Не перезагружать «на всякий случай» и не запускать `zpool clear` без анализа.
*Что хочет услышать:* порядок «диагностика → замена → resilver → scrub → проверка бэкапа», без героизма.
*Красный флаг:* «сделаю `zpool clear` и само пройдёт».

**4. «Как организуете репликацию между площадками с RPO 15 минут?»**
Ответ: асинхронная репликация массива (SnapMirror/HyperReplication) или `zfs send -i` по расписанию; RPO определяется интервалом снапшотов; отдельно продумать RTO — как переключить приложение (DNS, IP, конфиги), и обязательно регламент регулярных тестов переключения (урок 39). Синхронную репликацию при 15 минутах RPO не тащим — она требует низкой задержки и дороже.
*Что хочет услышать:* разницу sync/async, связку RPO/RTO с расписанием, упоминание тестов.
*Красный флаг:* «включу синхронную, будет надёжнее» без оценки задержки и стоимости.

**5. «Почему российские заказчики смотрят на YADRO/Аэродиск, а не только на NetApp/Huawei?»**
Ответ: требования импортозамещения и реестров, сертификация ФСТЭК для ГИС/ИСПДн, гарантированная поддержка и ЗИП в РФ, риск санкционных ограничений на поставку и обновления. При выборе смотрим: поддержка нужных протоколов и платформы виртуализации, наличие в реестре, тесты производительности, поддержка репликации, стоимость владения.
*Что хочет услышать:* инженерный, а не политический ответ — совместимость, сертификация, поддержка, SLA.
*Красный флаг:* «импортозамещение — это только бумажки» или наоборот «всё импортное запрещено».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — разделы «Storage Spaces overview», «iSCSI Target Server overview», «Storage Spaces Direct», «Disk management»; поиск: `Storage Spaces resiliency settings site:learn.microsoft.com`.
- **openzfs.org** и **openzfs.github.io/openzfs-docs** — «ZFS Administrators Guide», главы про `zpool`, `zfs send/receive`, `scrub`; поиск: `OpenZFS send receive incremental documentation`.
- **kernel.org** — документация по `md`/mdadm в `Documentation/admin-guide/md.rst`; поиск: `mdadm raid documentation kernel.org`.
- **docs.ceph.com** — «Architecture», «Pools and PG», «CRUSH maps»; поиск: `Ceph architecture OSD MON PG documentation`.
- **docs.netapp.com** — «ONTAP documentation», «SnapMirror», «WAFL»; поиск: `NetApp ONTAP SnapMirror configuration guide`.
- **support.huawei.com** — «OceanStor Dorado», «HyperMetro», «HyperReplication»; поиск: `Huawei OceanStor HyperMetro best practices`.
- **ubuntu.com/server/docs** — «NFS», «iSCSI»; поиск: `Ubuntu Server NFS iSCSI documentation`.
- **docs.kernel.org / man7.org** — `targetcli`/LIO и `iscsiadm` man-страницы; поиск: `targetcli backstore LUN ACL cheat sheet` [ПРОВЕРИТЬ актуальность синтаксиса для targetcli-fb в Ubuntu 24.04].
- **Реестр Минпромторга / Единый реестр российской радиоэлектронной продукции** — проверка статуса СХД YADRO, «Аэродиск», RAIDIX перед закупкой; поиск: `реестр Минпромторга СХД российского производства` [ПРОВЕРИТЬ актуальные модели и статусы].
