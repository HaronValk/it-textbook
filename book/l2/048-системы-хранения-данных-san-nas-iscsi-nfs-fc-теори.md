# Системы хранения данных: SAN/NAS, iSCSI, NFS, FC (теория), RAID и отказоустойчивость

## 1. Введение (зачем это нужно в реальной работе)

09:12, понедельник. В очереди заявок тикет от ivanov (WIN11-CL01, бухгалтерия): «Не открывается папка со сканами на сетевом диске Z:. В проводнике диск есть, но при открытии пишет "Недоступен". У остальных девочек в отделе всё работает. Срочно, сдаём отчётность». Через двадцать минут — второй тикет, от petrova (WIN10-CL02): «Очень медленно сохраняются файлы в `\\FS01\HR`, один файл на 40 МБ пишется минуту».

Обе заявки — про хранение данных. Первая — про то, что диск в системе есть, а данных нет (типовая история с iSCSI: сессия потеряна, том помечен offline). Вторая — про деградацию: либо перегружен LUN, либо массив пересобирается после замены диска, либо сеть.

В работе L2 запросы «медленно работает диск», «пропала сетевая папка», «место закончилось», «сервер не видит диск после перезагрузки» — это, наверное, треть всего потока после «не печатает» и «не пускает в почту». Чтобы их разбирать, надо понимать три вещи: как данные физически лежат на дисках (RAID), как они отдаются клиенту (NAS/SAN, файловый против блочного доступа) и что именно ломается в этом пути.

После этой главы ты будешь: объяснять разницу между SAN и NAS за 30 секунд, поднимать iSCSI-таргет на Windows Server и подключать его с клиента, экспортировать NFS с Ubuntu, собирать программный RAID и переживать потерю диска без потери данных, измерять IOPS и latency и отличать «диск медленный» от «сеть медленная». FC (Fibre Channel) — только теория: физически оптоволокно на домашнем стенде не поднять, но на собеседованиях про него спрашивают, потому что в enterprise и в госсекторе его всё ещё много.

## 2. Теория «под капотом»

### DAS, NAS, SAN — три способа отдать диск

**DAS** (Direct Attached Storage) — диск внутри сервера или в полке, подключённой к его HBA/SAS-контроллеру. Быстро, дешево, но данные привязаны к одной машине.

**NAS** (Network Attached Storage) — сервер, который отдаёт **файлы** по сети. Клиент говорит: «дай мне файл `\\FS01\Buh\отчёт.xlsx`». Протоколы: SMB (TCP 445) в Windows-мире, NFS (TCP 2049) в Linux-мире. NAS сам решает, как разложить файл по дискам — клиент видит только папки.

**SAN** (Storage Area Network) — сеть, которая отдаёт **блоки**. Клиент получает «сырой» диск (LUN) и сам делает на нём файловую систему: форматирует в NTFS, ext4, кладёт кластерный том. Протоколы: iSCSI (по обычному Ethernet, TCP 3260) и FC (Fibre Channel — отдельная сеть на оптоволокне или твинаксиле, свои коммутаторы, свои HBA).

Ключевое отличие в одной фразе: **NAS — «дай файл», SAN — «дай диск»**. Из этого следует всё остальное: NAS умеет права NTFS на уровне файла и шарит том между сотней клиентов; SAN-том нельзя просто так отдать двоим серверам одновременно (получишь разрушение ФС), потому что клиент считает себя единственным владельцем блока.

```text
                 ┌──────────────────────────────────────────────┐
                 │            КЛИЕНТЫ (10.10.10.0/24)           │
                 │  WIN11-CL01     WIN10-CL02     UBU01/ASTRA01 │
                 └───────┬─────────────┬───────────────┬────────┘
                         │ SMB 445     │ SMB 445       │ NFS 2049
                         │             │               │
   ┌─────────────────────▼─────────────▼───┐      ┌────▼─────────────┐
   │  FS01 10.10.10.20 — NAS (файлы)       │      │ UBU01 10.10.10.30│
   │  \\FS01\Obshchee | \Buh | \HR          │      │ /srv/nfs/vektor  │
   │  + iSCSI Target Server (блоки)        │      └──────────────────┘
   └─────────────────────┬─────────────────┘
                         │ iSCSI TCP 3260 (блочный доступ)
                         ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  ФИЗИЧЕСКИЙ СЛОЙ: виртуальные диски VMware (.vmdk) на хосте   │
   │  RAID-массив (mdadm / Storage Spaces) → том → LUN → клиент    │
   └──────────────────────────────────────────────────────────────┘

   Отдельный мир (теория, на стенде не поднимаем):
   сервер ── HBA ── оптика ── FC-коммутатор ── СХД
                            (WWPN, zoning, LUN masking, 8/16/32GFC)
```

### iSCSI — SAN для бедных (и не только)

iSCSI упаковывает SCSI-команды в TCP/IP. Сервер-инициатор (клиент) и таргет (СХД или Windows-сервер с ролью iSCSI Target) обмениваются PDU через TCP **3260**.

Что важно помнить про iSCSI в работе L2:
- Том iSCSI — это обычный базовый диск в `diskmgmt.msc`. Если сеть до таргета пропала, Windows видит **offline/недоступный диск**, а не «сетевую папку». Файловая система на нём при этом в опасности, если это кластерный том.
- Discovery бывает двух видов: SendTargets (клиент спрашивает «какие у тебя таргеты?») и iSNS (центральный каталог). В 90% случаев — SendTargets.
- Аутентификация — **CHAP** (односторонний или взаимный). На учебном стенде мы её включать не будем, в продакшене без неё нельзя.
- `Persistent` (постоянные сессии) — при перезагрузке клиента инициатор сам переподключается. Галочку «Add this connection to the list of Favorite Targets» нужно ставить всегда.

### NFS — файловый доступ в Linux-мире

NFSv4 работает только по **TCP 2049**. В NFSv3 помимо 2049 нужны rpcbind (TCP/UDP 111) и mountd с динамическим портом — из-за этого NFSv3 так неудобен в файрволах, и его до сих пор мучают в старых инфраструктурах. Экспорт описывается в `/etc/exports` строкой «путь клиент(опции)», применяется `exportfs -rav`. Аутентификация в NFSv4 — по UID/GID или Kerberos (sec=krb5), поэтому «в лоб» между Windows и Linux без маппинга пользователей NFS работает плохо.

### RAID: пережить смерть диска

RAID (Redundant Array of Independent Disks) — объединение дисков так, чтобы массив пережил отказ части из них.

| Уровень | Мин. дисков | Ёмкость | Переживает отказ | Чтение / запись | Где применяют |
|---|---|---|---|---|---|
| RAID 0 | 2 | 100% | 0 дисков | быстро / быстро | кэш, временные данные |
| RAID 1 | 2 | 50% | 1 диск | быстро / средне | ОС, маленькие БД |
| RAID 5 | 3 | (n−1)/n | 1 диск | быстро / медленно | файлопомойки, бэкапы |
| RAID 6 | 4 | (n−2)/n | 2 диска | быстро / очень медленно | большие массивы на SATA |
| RAID 10 | 4 | 50% | по 1 в зеркале | быстро / быстро | БД, 1С, гипервизоры |

Как RAID 5 считает: данные режутся на блоки и раскладываются по дискам, плюс на каждом «проходе» пишется **чётность** (XOR). Потеряли диск — потерянный блок восстанавливается из чётности остальных. Отсюда: при отказе RAID 5 работает **в разы медленнее** (каждое чтение требует вычисления), а **rebuild** большого массива может занять 12–48 часов. Всё это время второй отказ = потеря данных.

Поэтому в 2026-м большие массивы на дешёвых SATA собирают в RAID 6, а не RAID 5; RAID 10 — для того, что тормозить не имеет права.

Ещё три вещи, которые L2 обязан знать:
- **Hot spare** — диск в резерве, который автоматически встраивается на место отказавшего.
- **Write hole** — если питание пропало в момент записи, чётность может не сойтись с данными. Лечится **BBU/Cache Vault** на аппаратном контроллере (или ZFS/ReFS-подходом).
- **RAID не резервная копия.** Массив защищает от смерти диска, но не от `rm -rf`, шифровальщика и ошибки человека — удаление файла размножится по всем дискам мгновенно. Бэкапы — урок 49.

Аппаратный RAID делает контроллер (у него свой кэш, BBU и своя батарея проблем), программный — ОС: `mdadm` в Linux, **Storage Spaces** в Windows, ZFS/MD в NAS. На стенде мы будем делать программный: он честнее показывает, что происходит.

## 3. Аналогия из жизни

Представь склад компании «Вектор». **NAS** — это офисная кладовка с кладовщиком: ты говоришь «мне коробку с надписью "Отчёт за март"», он сам ищет её на стеллажах и приносит. Тебе не важно, на какой полке она лежала. **SAN** — это эстакада, по которой подают не коробки, а участок ленты-транспортёра: ты получаешь пустой отрезок и сам решаешь, что на него поставить, а кладовщика рядом нет. **RAID** — это когда каждую коробку кладут в двух местах или пишут на ящик контрольную сумму, чтобы при пропаже одной коробки её можно было восстановить. **Hot spare** — пустой стеллаж, который сразу занимают под восстановление. Аналогия перестаёт работать на rebuild: кладовщик восстановил бы коробку за секунды, а диск в реальности пересобирается часами и всё это время массив тормозит.

## 4. Практика и команды

**1. Инвентаризация дисков — с чего начинается любой разбор «диск пропал».**

```powershell
Get-Disk | Format-Table Number,FriendlyName,OperationalStatus,HealthStatus,Size,PartitionStyle
Get-Volume | Format-Table DriveLetter,FileSystemLabel,FileSystem,SizeRemaining,Size
```

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT      # дерево: диск → раздел → ФС → куда смонтирован
blkid                                          # UUID и тип ФС по каждому разделу
df -hT                                         # что реально смонтировано и сколько занято
```

Ключевое в выводе Windows — `OperationalStatus` (`Online` / `Offline`), `HealthStatus` (`Healthy` / `Warning` / `Unhealthy`) и `PartitionStyle` (`RAW` = диск без разметки). В Linux — `TYPE` (`disk`/`part`/`raid5`) и пустой `MOUNTPOINT` у раздела, который должен быть смонтирован.

**2. Разметка и форматирование нового диска (Windows).**

```powershell
Initialize-Disk -Number 1 -PartitionStyle GPT
New-Partition -DiskNumber 1 -UseMaximumSize -DriveLetter Z
Format-Volume -DriveLetter Z -FileSystem NTFS -NewFileSystemLabel "Lab-Data" -Confirm:$false
```

`Initialize-Disk` создаёт таблицу разделов (GPT — современный стандарт, MBR — только для старых систем и дисков <2 ТБ). `New-Partition -UseMaximumSize` съедает всё место. `Format-Volume` создаёт ФС. **Осторожно:** `Initialize-Disk` на диске с данными (даже с `-Confirm:$false`) — это потеря данных, проверяй `Number` дважды.

**3. Программный RAID в Windows — Storage Spaces.**

```powershell
Get-PhysicalDisk | Format-Table FriendlyName,MediaType,Size,HealthStatus
New-StoragePool -FriendlyName "Pool-Lab" -StorageSubsystemFriendlyName "Windows Storage*" -PhysicalDisks (Get-PhysicalDisk -CanPool $true)
New-VirtualDisk -StoragePoolFriendlyName "Pool-Lab" -FriendlyName "VD-Parity" -ResiliencySettingName Parity -NumberOfColumns 3 -Size 20GB
Get-VirtualDisk | Format-Table FriendlyName,ResiliencySettingName,HealthStatus,OperationalStatus
```

`ResiliencySettingName` — аналог уровня RAID: `Simple` (RAID 0), `Mirror` (RAID 1/10), `Parity` (RAID 5). `HealthStatus` у виртуального диска — первое, что смотрят при деградации. Storage Spaces на стенде заводить не обязательно, если делаешь RAID в Linux — но знать про него надо, в продакшене Windows-файлопомойки часто живут именно так.

**4. Поднимаем iSCSI-таргет на FS01.**

```powershell
Install-WindowsFeature -Name FS-iSCSITarget-Server -IncludeManagementTools
New-IscsiVirtualDisk -Path "D:\iSCSI\lab-lun01.vhdx" -Size 5GB
New-IscsiServerTarget -TargetName "tgt-win11" -InitiatorIds "IPAddress:10.10.10.100"
Add-IscsiVirtualDiskTargetMapping -TargetName "tgt-win11" -Path "D:\iSCSI\lab-lun01.vhdx"
Get-IscsiServerTarget | Format-Table TargetName,Status
```

`-InitiatorIds` — кому разрешено подключиться. Можно ограничить по IP, по IQN или по DNS-имени. `New-IscsiVirtualDisk` создаёт **VHDX-файл** на существующем томе — то есть SAN у нас «программный», внутри NTFS. Проверить статус таргета: `Get-IscsiServerTarget`, оснастка «iSCSI Target» в «Средства» / «Server Manager → File and Storage Services».

**5. Проверка доступности порта iSCSI с клиента.**

```powershell
Test-NetConnection -ComputerName 10.10.10.20 -Port 3260
```

Смотрим `TcpTestSucceeded : True`. Если `False` — сначала сеть и файрвол (`Get-NetFirewallRule -DisplayName "*iSCSI*"`), и только потом всё остальное. Это самый частый корень «диск не подключился».

**6. Подключение к iSCSI-таргету с WIN11-CL01.**

```powershell
Set-Service -Name MSiSCSI -StartupType Automatic -Status Running
Get-IscsiTarget | Format-Table NodeAddress,IsConnected
$t = Get-IscsiTarget | Where-Object NodeAddress -like "*tgt-win11*"
Connect-IscsiTarget -NodeAddress $t.NodeAddress -IsPersistent $true
Get-IscsiSession | Format-Table TargetNodeAddress,IsPersistent,SessionIdentifier
Get-Disk | Where-Object BusType -eq 'iSCSI'
```

`-IsPersistent $true` — сессия восстановится после ребута. `BusType -eq 'iSCSI'` — фильтр, который сразу показывает «наш» диск. GUI-путь: `iscsicpl` → вкладка Targets → ввести IP → Connect.

**7. Программный RAID в Linux: сборка.**

```bash
sudo apt install -y mdadm
sudo mdadm --create /dev/md0 --level=5 --raid-devices=3 /dev/sdb /dev/sdc /dev/sdd
cat /proc/mdstat
sudo mdadm --detail /dev/md0
```

`--level=5` — уровень, `--raid-devices=3` — сколько дисков в массиве (должно совпадать с числом устройств). `/proc/mdstat` показывает `[UUU]` — состояние участников (`U` = up, `_` = отсутствует) и прогресс `resync`/`recovery` в процентах. `mdadm --detail` даёт `State : clean`, `Active Devices`, `Failed Devices`.

**8. Деградация и замена диска в RAID.**

```bash
sudo mdadm --fail /dev/md0 /dev/sdb        # имитируем отказ (или диск реально выпал)
cat /proc/mdstat                            # ожидаем [U_U] и State : clean, degraded
sudo mdadm --remove /dev/md0 /dev/sdb
sudo mdadm --add /dev/md0 /dev/sdb          # возвращаем «новый» диск
watch -n 5 cat /proc/mdstat                 # смотрим progress recovery
```

Рабочая последовательность при реальном отказе: `--fail` (если диск ещё виден системе) → `--remove` → замена железа → `--add`. Тестовая замена тем же устройством работает только потому, что мы «притворяемся» — массив пересоберёт его с нуля.

**9. Файловая система и монтирование по UUID.**

```bash
sudo mkfs.ext4 -L lab-raid /dev/md0
sudo mkdir -p /srv/lab-raid
sudo blkid /dev/md0                      # берём UUID=...
```

В `/etc/fstab` добавляем строку `UUID=<uuid> /srv/lab-raid ext4 defaults,nofail 0 2`. Монтировать по `/dev/md0` нельзя: имена дисков могут поменяться, UUID — нет. `nofail` не даст серверу уйти в аварийный режим при недоступном массиве (актуально для внешних SAN-томов).

**10. NFS-сервер на UBU01 и проверка экспорта.**

```bash
sudo apt install -y nfs-kernel-server
sudo mkdir -p /srv/nfs/vektor && sudo chown nobody:nogroup /srv/nfs/vektor
echo "/srv/nfs/vektor 10.10.10.0/24(rw,sync,no_subtree_check)" | sudo tee -a /etc/exports
sudo exportfs -rav
showmount -e localhost
```

`exportfs -rav` — `-r` перечитать `/etc/exports`, `-a` все, `-v` подробно. `showmount -e` — «что я вообще отдаю». С клиента (ASTRA01 или UBU01 через loopback): `sudo mount -t nfs 10.10.10.30:/srv/nfs/vektor /mnt && df -hT /mnt`. Опция `sync` безопаснее `async` (меньше риск потери данных при падении), `no_subtree_check` — стандартная рекомендация для простого экспорта каталога.

**11. Замер производительности диска — цифры вместо «тормозит».**

```powershell
Get-Counter '\PhysicalDisk(_Total)\Avg. Disk sec/Read','\PhysicalDisk(_Total)\Avg. Disk sec/Write' -SampleInterval 2 -MaxSamples 5
```

```bash
iostat -xz 1 5            # из пакета sysstat: r/s, w/s, await, %util по каждому диску
dd if=/dev/zero of=/srv/lab-raid/test.bin bs=1M count=512 oflag=direct   # тест записи
```

`Avg. Disk sec/Read` в секундах: <0,010 — отлично, 0,010–0,020 — норма, >0,025 — пользователи уже жалуются. В `iostat` смотрим `await` (мс на операцию) и `%util` (загрузка устройства, >90% = очередь). `oflag=direct` обходит кэш, иначе цифры будут красивые и бессмысленные.

**12. Здоровье дисков.**

```bash
sudo smartctl -a /dev/sdb | head -30     # пакет smartmontools
```

Смотрим `Reallocated_Sector_Ct`, `Pending_Sector_Ct`, `Media_Wearout` — рост этих счётчиков означает «диск скоро умрёт». На виртуальных дисках VMware SMART обычно не проксируется, вернётся `Permission denied` или пустота — это нормально для лабы, но на реальном железе команда обязательна.

## 5. Лабораторная работа

### Предусловия

Стенд L1 готов: DC01 (10.10.10.10, AD/DNS/DHCP), FS01 (10.10.10.20, файловый сервер), WIN11-CL01 (DHCP, учётка `ivanov`), UBU01 (10.10.10.30, SSH), ASTRA01 (10.10.10.31). Из машин трека L2 достаточно тех, что уже есть. Дополнительно на FS01 желательно свободное место на диске C: (не меньше 10 ГБ) под VHDX-файлы таргета — если мало, добавь FS01 ещё один виртуальный диск и используй его.

Нам понадобятся **дополнительные виртуальные диски**: три по 5 ГБ на UBU01 (для RAID 5). В VMware Workstation: выключить ВМ → `VM → Settings → Add → Hard Disk → SCSI → Create a new virtual disk`, размер 5 ГБ, «Allocate all disk space now» **не** ставить (тонкий диск). Повторить три раза. Диски будут видны как `/dev/sdb`, `/dev/sdc`, `/dev/sdd` — проверь через `lsblk`, имена могут отличаться, если на UBU01 уже есть другие диски.

### Снапшот

На хосте, при выключенных ВМ (или через `VM → Snapshot → Take Snapshot` на работающих):

```text
FS01        → before-l2-048
UBU01       → before-l2-048
WIN11-CL01  → before-l2-048
ASTRA01     → before-l2-048
```

Снапшот делай **до** добавления новых виртуальных дисков, чтобы откат гарантированно убрал и их. В `stand-log.md` записываем: «before-l2-048 снят, 4 ВМ».

### Шаги

1. **FS01, подготовка томов.** В `diskmgmt.msc` или через PowerShell создай на свободном месте папку `D:\iSCSI` (или используй существующий том). Проверь: `Get-Volume`. Ожидаемо: есть том с достаточным `SizeRemaining`.

2. **FS01, установка роли iSCSI Target.** `Install-WindowsFeature -Name FS-iSCSITarget-Server -IncludeManagementTools`. Ожидаемо: `Success : True`, `RestartNeeded : No`. В «Server Manager → File and Storage Services» появляется раздел iSCSI.

3. **FS01, создание LUN и таргета.**

```powershell
New-IscsiVirtualDisk -Path "D:\iSCSI\lab-lun01.vhdx" -Size 5GB
New-IscsiServerTarget -TargetName "tgt-win11" -InitiatorIds "IPAddress:10.10.10.100"
Add-IscsiVirtualDiskTargetMapping -TargetName "tgt-win11" -Path "D:\iSCSI\lab-lun01.vhdx"
Get-IscsiServerTarget | Format-Table TargetName,Status
```

Ожидаемо: `Status : Idle` (или `NotConnected` до первого подключения) и файл `lab-lun01.vhdx` в `D:\iSCSI`.

> IP `10.10.10.100` — это адрес WIN11-CL01 из DHCP-пула. Проверь его на клиенте (`ipconfig`) и подставь свой, если DHCP выдал другой.

4. **FS01, файрвол.** `Get-NetFirewallRule -DisplayName "*iSCSI*" | Format-Table DisplayName,Enabled,Direction`. Ожидаемо: правило для TCP 3260 включено (`Enabled : True`). Если нет — включи именно это правило, а не отключай профиль файрвола целиком.

5. **WIN11-CL01, проверка связности.** `Test-NetConnection -ComputerName 10.10.10.20 -Port 3260`. Ожидаемо: `TcpTestSucceeded : True`. Если `False` — стоп, дальше идти бессмысленно: смотри шаг 4 и `ping 10.10.10.20`.

6. **WIN11-CL01, подключение.** Запусти службу `MSiSCSI`, выполни `Get-IscsiTarget`, найди `tgt-win11`, подключись `Connect-IscsiTarget -IsPersistent $true`. Ожидаемо: `Get-IscsiSession` показывает сессию с `IsPersistent : True`, а `Get-Disk` — новый диск с `BusType : iSCSI`, `OperationalStatus : Offline`.

7. **WIN11-CL01, приведение диска в строй.**

```powershell
Get-Disk | Where-Object BusType -eq 'iSCSI' | Set-Disk -IsOffline $false
Get-Disk | Where-Object BusType -eq 'iSCSI' | Set-Disk -IsReadOnly $false
Initialize-Disk -Number <номер> -PartitionStyle GPT
New-Partition -DiskNumber <номер> -UseMaximumSize -DriveLetter Z
Format-Volume -DriveLetter Z -FileSystem NTFS -NewFileSystemLabel "Scan-Buh" -Confirm:$false
```

Ожидаемо: в проводнике появляется диск `Z: Scan-Buh`, доступный на запись.

8. **UBU01, RAID 5.** Проверь `lsblk`, что три новых диска пустые и не смонтированы. Собери массив:

```bash
sudo apt install -y mdadm
sudo mdadm --create /dev/md0 --level=5 --raid-devices=3 /dev/sdb /dev/sdc /dev/sdd
cat /proc/mdstat
```

Ожидаемо: `md0 : active raid5 sdd[2] sdc[1] sdb[0]`, `[UUU]`, `resync` может идти несколько минут — дождись `100%`.

9. **UBU01, ФС и монтирование.**

```bash
sudo mkfs.ext4 -L lab-raid /dev/md0
sudo mkdir -p /srv/lab-raid
sudo blkid /dev/md0
sudo mount UUID=<uuid> /srv/lab-raid     # или mount /dev/md0 /srv/lab-raid на один раз
df -hT /srv/lab-raid
```

Ожидаемо: смонтирован том ~10 ГБ (5+5+5 минус чётность). Запиши UUID в `/etc/fstab` со `nofail` и сделай `sudo mdadm --detail --scan | sudo tee -a /etc/mdadm/mdadm.conf && sudo update-initramfs -u`, чтобы массив собирался при загрузке.

10. **UBU01, NFS-экспорт.** Настрой `/etc/exports` как в разделе 4 (п.10), `exportfs -rav`, `showmount -e localhost`. Ожидаемо: строка `/srv/nfs/vektor 10.10.10.0/24`.

11. **ASTRA01, монтирование NFS.** `sudo apt install -y nfs-common` (если нет), затем `sudo mount -t nfs 10.10.10.30:/srv/nfs/vektor /mnt`. Если репозиторий недоступен — проверь экспорт с самого UBU01: `sudo mount -t nfs 127.0.0.1:/srv/nfs/vektor /mnt`. Ожидаемо: `df -hT /mnt` показывает `10.10.10.30:/srv/nfs/vektor`.

12. **Замеры и журнал.** Прогони `dd` на `/srv/lab-raid`, `iostat -xz 1 5` во время записи, на WIN11-CL01 — `Get-Counter` по `Avg. Disk sec/Write` для диска Z. Запиши цифры в `stand-log.md`: это твоя «базовая линия» для будущих глав (47 и 49).

### Проверка

- [ ] `Get-IscsiServerTarget` на FS01 показывает `tgt-win11` в статусе Idle/Connected, файл `lab-lun01.vhdx` существует.
- [ ] На WIN11-CL01 есть диск `Z:` с меткой `Scan-Buh`, файл на него копируется и переживает перезагрузку клиента.
- [ ] `Get-IscsiSession` на WIN11-CL01 после перезагрузки снова показывает сессию (`IsPersistent : True`).
- [ ] `cat /proc/mdstat` на UBU01: `[UUU]`, состояние `active`.
- [ ] `/srv/lab-raid` смонтирован, `df -hT` показывает ~10 ГБ.
- [ ] `showmount -e localhost` на UBU01 отдаёт `/srv/nfs/vektor`.
- [ ] ASTRA01 видит смонтированный NFS-каталог (или проверка через loopback выполнена).
- [ ] В `stand-log.md` записаны: новые виртуальные диски, роль на FS01, LUN, буква Z, массив md0, экспорт NFS.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Диск Z: (Scan-Buh) в проводнике есть, но при открытии "Недоступен", файлы не открываются. У соседки вроде всё нормально. Надо срочно, отправляем сканы в банк».
> **Срочность:** высокая

**Как воспроизвести поломку.** На FS01 останови службу таргета: `Stop-Service -Name WinTarget -Force` (имя службы iSCSI Target Server — `WinTarget`; проверь `Get-Service *target*`). Через 30–60 секунд на WIN11-CL01 проверь `Get-Disk` — iSCSI-диск уйдёт в `Offline` / `Unhealthy`, `Z:` перестанет открываться. Альтернативный вариант поломки — на FS01 убрать маппинг: `Remove-IscsiVirtualDiskTargetMapping -TargetName "tgt-win11" -Path "D:\iSCSI\lab-lun01.vhdx"`.

**Диагностика по шагам (сверху вниз, от клиента к серверу):**

1. На WIN11-CL01: `Get-Disk | Where-Object BusType -eq 'iSCSI'`. Если `OperationalStatus : Offline` — проблема не в правах NTFS и не в «диске», а в транспорте. Отсекаем половину гипотез сразу.
2. `Get-IscsiSession` — сессия есть или её нет? Если сессии нет, идём к сети.
3. `Test-NetConnection 10.10.10.20 -Port 3260` — если `False`, вопрос к FS01 (служба/файрвол) или к сети.
4. На FS01: `Get-Service -Name WinTarget` — `Stopped`? Это корень.
5. `Get-IscsiServerTarget` на FS01 — статус таргета; в «Просмотр событий → Application and Services Logs → Microsoft → Windows → iSCSI Target Server` смотрим ошибки.
6. Проверяем, что VHDX-файл на месте и на диске есть место: `Get-Item D:\iSCSI\lab-lun01.vhdx`, `Get-Volume`.
7. Только после этого смотрим `Get-WinEvent -LogName System -MaxEvents 50` на клиенте — ошибки `disk`/`iScsiPrt` (Event ID 153, 129 — «сброс устройства», признак проблем с таймаутами).

**Решение.** На FS01: `Start-Service -Name WinTarget`, `Get-IscsiServerTarget` → убедиться, что `Status` не `Failed`. На WIN11-CL01: подождать 30 секунд (автовосстановление постоянной сессии), при необходимости `Disconnect-IscsiSession` → `Connect-IscsiTarget -IsPersistent $true`. Проверить `Set-Disk -IsOffline $false` и открыть `Z:`.

**Ответ пользователю:** «Иван Иванов, добрый день. Проблема на стороне файлового сервера — служба, которая отдаёт диск Z:, останавливалась. Сейчас она запущена, диск подключён заново, файлы доступны. Проверьте, пожалуйста: откройте Z: и один любой скан. Если снова пропадёт — напишите, разберём глубже, но данные не потеряны».

**Как закрыть заявку.** Категория «СХД / iSCSI», приоритет «Высокий», в решении: корневая причина — остановленная служба iSCSI Target на FS01; действие — запуск службы, проверка постоянных сессий; профилактика — проверить, не настроен ли автозапуск службы (`Set-Service -Name WinTarget -StartupType Automatic`), и вынести алерт на статус таргета (пригодится в главе про мониторинг).

### Что изменилось на стенде

- **FS01:** установлена роль iSCSI Target Server; создан VHDX `D:\iSCSI\lab-lun01.vhdx` (5 ГБ); создан таргет `tgt-win11` с маппингом и разрешением по IP `10.10.10.100`; служба `WinTarget` — в состоянии Running.
- **WIN11-CL01:** подключена постоянная iSCSI-сессия к `10.10.10.20`; диск `Z:` (NTFS, метка `Scan-Buh`) поверх LUN.
- **UBU01:** добавлены три виртуальных диска по 5 ГБ; собран `/dev/md0` (RAID 5, ext4, метка `lab-raid`), смонтирован в `/srv/lab-raid`; настроен NFS-экспорт `/srv/nfs/vektor` для `10.10.10.0/24`; запись в `/etc/fstab` и `/etc/mdadm/mdadm.conf`.
- **ASTRA01:** смонтирован NFS-каталог с UBU01 (временное монтирование, в fstab не прописан).
- **Домен и учётки не менялись.**

### Откат

На хосте: выключить ВМ → `VM → Snapshot → Snapshot Manager` → выбрать `before-l2-048` → `Go to`. Для UBU01 откат вернёт и удалит добавленные виртуальные диски — предварительно **удали их из настроек ВМ после отката**, если снапшот их не подхватил (`VM → Settings → Hard Disk → Remove`, без удаления файлов с хоста, чтобы освободить место вручную). После отката перепроверь на FS01: `Get-IscsiServerTarget` (таргета быть не должно) и на клиенте `Get-IscsiSession` (сессий быть не должно). Если откатываешь только UBU01, не забудь, что экспорт NFS исчезнет и ASTRA01 потеряет точку монтирования — размонтируй её заранее.

## 6. Ошибки новичков и безопасность

1. **«Дам-ка я iSCSI-том двум серверам, так быстрее».** → Два инициатора пишут в один LUN без кластерной ФС (CSV, GFS2, OCFS2) — файловая система разрушается за минуты, восстановление только из бэкапа. → Один LUN — один владелец; для совместного доступа — SMB/NFS или кластерная ФС.
2. **`mdadm --create` на дисках, где что-то было.** → Массив собирается поверх старых суперблоков, данные теряются. → Перед сборкой `lsblk` + `wipefs -n /dev/sdX` (проверка без записи) и только потом решение.
3. **Монтирование по `/dev/sdX` в `/etc/fstab`.** → После подключения нового диска имена сдвигаются, сервер не грузится или монтирует не то. → Всегда UUID (`blkid`) плюс `nofail`.
4. **RAID 5 из больших SATA-дисков «на 20 ТБ, чтобы места побольше».** → Rebuild идёт сутки, URE (неисправимая ошибка чтения) на этом объёме почти гарантирован, второй диск выпадает — массив мёртв. → RAID 6 или RAID 10, плюс бэкап.
5. **Считать RAID резервной копией.** → Шифровальщик или `rm -rf` размножается на все диски массива мгновенно. → RAID — про доступность, бэкап — про восстановление (глава 49).
6. **`Initialize-Disk` не на том номере.** → Потеря данных на живом томе, потому что «диск 1» оказался рабочим. → Всегда `Get-Disk` перед операцией, в продакшене — двойное подтверждение и снапшот.
7. **Тестировать производительность через `dd` без `oflag=direct`.** → Кэш ОС/контроллера даёт гигабайты в секунду, в реальности диск еле дышит. → `oflag=direct`, `fio`, `iostat` во время нагрузки.
8. **Диагностировать «медленную папку» со стороны пользователя.** → Гадание вместо цифр. → Сначала `iostat`/`Get-Counter` на сервере и `Test-NetConnection` — определить, это диск, сеть или приложение.

### Безопасность

- **CHAP на iSCSI.** На стенде мы разрешили подключение по IP и без аутентификации. В боевой среде iSCSI без CHAP (или IPsec) означает: любой, кто дотянулся до порта 3260, может подключиться к LUN и прочитать данные в обход NTFS-прав — права файловой системы не защищают блоки.
- **NFS-экспорт на всю подсеть.** `10.10.10.0/24(rw)` на стенде удобен, в продакшене — только конкретные хосты, `ro` где хватает, и Kerberos (`sec=krb5`) для чувствительных данных.
- **Не отключай файрвол «чтобы заработало».** Открывай конкретное правило (TCP 3260, TCP 2049), а не профиль целиком. На стенде это учебная сеть, в офисе — прямой путь к инциденту ИБ.
- **Пароли и ключи не в git-репозиториях стенда.** Даже учебный `Lab#2026-vektor` не должен попасть в файл, который ты потом показываешь на собеседовании как портфолио.
- **Права на VHDX.** Файл `lab-lun01.vhdx` — это твои данные. Доступ к папке `D:\iSCSI` должен быть только у администраторов, иначе LUN можно украсть копированием файла.

## 7. Вопросы на собеседовании

**1. Чем NAS отличается от SAN и что вы выберете для файлового сервера отдела на 50 человек?**

NAS — файловый доступ (SMB/NFS), клиент работает с файлами и папками, права на уровне файла, один том можно отдать многим клиентам одновременно. SAN — блочный доступ (iSCSI/FC), клиент получает LUN и сам создаёт на нём ФС, том принадлежит одному хосту (кроме кластерных ФС). Для файлового сервера на 50 человек — NAS (SMB-шара), это дешевле, проще в правах и в бэкапе. SAN нужен, когда есть СУБД, гипервизоры, кластеры — там нужен именно блок.

*Что хочет услышать интервьюер:* чёткое «файл против блока», связь с правами доступа и с количеством одновременных владельцев тома.
*Красный флаг:* «SAN быстрее, поэтому везде SAN» без понимания разницы уровней доступа.

**2. RAID 5 или RAID 10 под базу 1С? Почему?**

RAID 10. 1С генерирует много мелких случайных записей; RAID 5 на каждой записи пересчитывает чётность (read-modify-write), что даёт кратный проигрыш по IOPS записи и высокую latency. RAID 10 даёт зеркалирование без вычислений, быстрый rebuild и предсказуемую latency, ценой 50% ёмкости. RAID 5 уместен там, где преобладает чтение и объём важнее скорости — файлы, бэкапы.

*Что хочет услышать интервьюер:* понимание «write penalty» у RAID 5 и связи уровня RAID с профилем нагрузки.
*Красный флаг:* «RAID 5 везде, он же эффективнее по месту».

**3. В RAID-массиве на сервере выпал диск. Ваши действия по шагам?**

Не паниковать и не пересобирать «на горячую» вслепую. 1) Зафиксировать состояние: `cat /proc/mdstat` или `mdadm --detail`, на Windows — `Get-PhysicalDisk` / консоль контроллера, проверить, что массив degraded, а не failed. 2) Проверить, есть ли hot spare и начался ли автоматический rebuild. 3) Убедиться в наличии свежего бэкапа — до замены диска. 4) Заменить диск, добавить в массив, дождаться окончания recovery, контролируя прогресс. 5) Предупредить, что в период rebuild производительность упадёт. 6) После сборки — проверить `State : clean`, записать инцидент.

*Что хочет услышать интервьюер:* порядок «сначала оценить и забэкапить, потом менять», понимание деградации производительности и риска второго отказа.
*Красный флаг:* «сразу выдерну диск и вставлю новый» без диагностики и без мысли о бэкапе.

**4. Пользователь говорит: «пропал сетевой диск Z:». Как диагностировать iSCSI-том?**

Сверху вниз. На клиенте: `Get-Disk` (статус iSCSI-диска — Offline/No Media?), `Get-IscsiSession` (сессия жива?), `Test-NetConnection <таргет> -Port 3260` (сеть и порт). На сервере: служба iSCSI Target (`WinTarget`), `Get-IscsiServerTarget` (статус), наличие VHDX-файла и свободного места, журнал iSCSI Target Server. Если сессия есть, а диск Offline — `Set-Disk -IsOffline $false`. Если сессии нет — `Connect-IscsiTarget -IsPersistent $true`. Отдельно проверить событие 153/129 в System — это таймауты устройства, признак проблем сети или перегрузки СХД.

*Что хочет услышать интервьюер:* методичный разбор по слоям (клиент → транспорт → сервер) и знание, что iSCSI-диск ведёт себя как локальный, а не как шара.
*Красный флаг:* «переустановлю инициатор» или «перезагружу сервер» вместо диагностики.

**5. RAID защищает от потери данных? Обоснуйте.**

Нет, RAID защищает от **отказа диска** — то есть обеспечивает доступность. Он не защищает от логических ошибок: удаления файлов, шифровальщика, битого обновления, ошибочного `format`, повреждения ФС — все эти изменения мгновенно и синхронно применяются ко всем дискам массива. Плюс RAID 0 не защищает вообще ни от чего, а RAID 5 в период rebuild уязвим ко второму отказу. Поэтому нужны бэкапы по правилу 3-2-1 и регулярное **тестирование восстановления** — по этому пункту и проверяют, работают ли бэкапы на самом деле.

*Что хочет услышать интервьюер:* разделение «отказоустойчивость» и «резервное копирование» и упоминание проверки восстановления, а не только создания копии.
*Красный флаг:* «у нас RAID, бэкап не нужен».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — разделы «iSCSI Target Server overview» (роль FS-iSCSITarget-Server), «Storage Spaces overview», «iSCSI Initiator cmdlets» (модуль iSCSI), «Multipath I/O overview». Поиск: `iSCSI Target Server Windows Server overview`, `Storage Spaces resiliency settings`, `Connect-IscsiTarget IsPersistent`.
- **kernel.org / man7.org** — man-страницы `mdadm(8)`, `md(4)`, `nfs(5)`, `exports(5)`. Поиск: `man mdadm create raid5`, `man 5 exports`.
- **ubuntu.com** — раздел Server documentation: «NFS», «Software RAID», «Storage». Поиск: `ubuntu server guide nfs`, `ubuntu mdadm raid5`.
- **docs.redhat.com** — «Managing storage devices», «Configuring NFS», раздел про LIO/targetcli. Поиск: `red hat nfs server configuration`, `red hat raid levels`.
- **vmware.com** — «Virtual Disk types», «SCSI controller types in VMware Workstation» (для понимания, что .vmdk под капотом). Поиск: `vmware workstation virtual disk types thin thick`.
- **ietf.org** — RFC 7143 (iSCSI Protocol, консолидирует RFC 3720/5048), RFC 7530 (NFSv4). Поиск: `RFC 7143 iSCSI`, `RFC 7530 NFS version 4`.

Пометка `[ПРОВЕРИТЬ]` по ходу главы: имя службы iSCSI Target (`WinTarget`) проверяется через `Get-Service *target*` на конкретной сборке; наличие клиента NFS в Windows 11 Enterprise — через `Get-WindowsFeature *NFS*` в PowerShell (в редакции Pro/Home может отсутствовать); точное имя пакета `targetcli` в Ubuntu 24.04 (`targetcli-fb`) — через `apt search targetcli`.
