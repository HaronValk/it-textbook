# Хранилища: диски, тома, Storage Spaces, iSCSI, MPIO, расширение разделов

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди L2: «Пользователь / WIN11-CL01 (ivanov) / Не могу сохранить акт в `\\FS01\Buh`, Windows пишет "Недостаточно места на диске". Работа стоит, закрываю месяц / Высокая».

Дальше выясняется классика: на FS01 том с бухгалтерией заполнен на 97%, а на самом сервере есть три неразмеченных виртуальных диска, которые просто никто не подключил. Плюс на подходе стенд 1С, которому нужен отдельный быстрый диск, а свободных слотов в сервере нет.

Хранилища — это та тема, где админ L2 отличается от L1 быстрее всего. L1 открывает «Мой компьютер» и говорит «место кончилось». L2 смотрит: какой физический диск, какой пул, какой том, какой файл съел место, можно ли расширить онлайн, не рухнет ли это ночью. После этой главы ты будешь уметь: читать картину дисков и томов в PowerShell, безопасно расширять разделы без перезагрузки, собирать программный RAID из Storage Spaces, выдавать блочный диск по iSCSI на другой сервер и понимать, зачем нужен MPIO. И главное — диагностировать «диск пропал» за пять минут, а не за час.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Уровни хранения в Windows.** Снизу вверх: физический диск (`Get-Disk`) → разделы (`Get-Partition`) → том с файловой системой (`Get-Volume`) → буква диска или точка монтирования. Файловая система для серверов — NTFS (по умолчанию, умеет квоты, EFS, сжатие) или ReFS (устойчивее к повреждениям, поддерживает целостность потоков, но не умеет загрузку и не годится для некоторых ролей вроде размещения баз 1С — там NTFS). Обе поддерживают расширение тома **на лету**.

**Разметка.** MBR — старый, максимум 2 ТБ и 4 первичных раздела. GPT — современный, до 9,4 ЗБ, 128 разделов по умолчанию, обязателен для UEFI-загрузки. На серверах в 2026 году MBR встречается только на древних наследствах.

**Базовый против динамического.** Динамические диски (Dynamic Disks) — устаревшая технология: программные RAID-0/1/5 и тома, растянутые на несколько дисков. Их не рекомендуют: они плохо переносятся, ломаются при сбое и не поддерживают загрузку в некоторых сценариях. Их место заняли Storage Spaces.

**Storage Spaces** — программный RAID уровня ОС. Схема: несколько физических дисков объединяются в **пул (Storage Pool)**; внутри пула создаётся **виртуальный диск (Virtual Disk)** с нужной отказоустойчивостью; внутри виртуального диска — обычный том NTFS. Режимы устойчивости: `Simple` (без резервирования, аналог RAID-0), `Mirror` (копия, RAID-1, минимум 2 диска), `Parity` (чётность, RAID-5-подобный, минимум 3 диска). Ключевая особенность — **thin provisioning**: виртуальный диск можно объявить больше, чем реально занято, и «толстеть» по мере записи. Это удобно и опасно одновременно (см. раздел 6). Есть ещё Storage Spaces Direct (S2D) — кластерный вариант, но он не для одной ВМ.

**iSCSI** — передача блочных команд SCSI по обычной сети TCP. Порты: **3260/TCP**. Роли: **target** (сервер, отдающий диски, у Windows — роль «iSCSI Target Server», отдаёт VHDX-файлы как LUN'ы) и **initiator** (клиент, подключающий LUN, служба `MSiSCSI`). Узлы идентифицируются по **IQN** — строке вида `iqn.1991-05.com.microsoft:fs01-target`, а initiator — по своему IQN (для Windows-клиента он генерируется при первом запуске службы). Портал (portal) — это IP:порт таргета. Поверх iSCSI можно включить CHAP-аутентификацию и IPsec-шифрование, по умолчанию трафик идёт открытым текстом.

**MPIO (Multipath I/O)** — несколько сетевых путей между инициатором и таргетом. Если один коммутатор/кабель/порт лёг, диск продолжает работать через второй путь; заодно можно балансировать нагрузку. Работает через **DSM** (Device Specific Module) — драйвер-модуль, который знает, как опрашивать конкретное устройство. Политики: `FOO` (только failover), `RR` (round robin), `LQD` (least queue depth), `LB` (least blocks), `SO` (subset). Важно: MPIO — это не teaming сетевых карт, они решают разные задачи.

Схема стенда после этой лабы:

```text
FS01 (Windows Server 2022)                      VMware: 4 виртуальных диска
┌──────────────────────────────────────────────────────────────┐
│ Disk1(10G)  Disk2(10G)  Disk3(10G)  Disk4(10G, RDM не нужен) │
└──────┬─────────┬──────────┬───────────┬──────────────────────┘
       └─────────┴──────────┴───────────┘
                     ▼
        Storage Pool  SP-Data01  (≈40 ГБ)
                     ▼
        Virtual Disk  VD-Data01  (Mirror, thin, 12 ГБ)
                     ▼
        Том E: NTFS "Data01"   ← здесь VHDX для iSCSI
                     │
                     │ VHDX E:\iSCSI\srv1c01-data.vhdx (5 ГБ)
                     ▼
SRV1C01 (initiator) ◄──── TCP 3260 (iSCSI) ──── FS01 (target)
        Диск D: NTFS "Data01-1C"
```

## 3. Аналогия из жизни

Представь склад. Физический диск — это само здание. Раздел — капитальные стены: раз поставил, переносить долго и дорого. Том — арендованная площадь с табличкой «Бухгалтерия» на двери: её можно расширить, если рядом освободился зал, но нельзя расширить в сторону через чужую комнату. Storage Spaces — это модульный стеллажный комплекс: ты покупаешь секции (пул), а «виртуальный стеллаж» нужного размера собираешь уже из них, и он может быть зеркальным (каждая коробка лежит в двух местах). iSCSI — очень длинный кабель-удлинитель для диска: сервер физически стоит в другом здании, а выглядит как локальный винчестер. MPIO — второй коридор к тому же складу: если один перекрыли, груз всё равно доедет.

Аналогия ломается на одном: стены на складе нельзя перестраивать, пока внутри работают люди, а том NTFS в Windows расширяется онлайн, без остановки пользователей.

## 4. Практика и команды

**1. `Get-Disk` — общая картина дисков.**

```powershell
Get-Disk | Format-Table Number, FriendlyName, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}, PartitionStyle, OperationalStatus, IsOffline
```

```text
Number FriendlyName          SizeGB PartitionStyle OperationalStatus IsOffline
------ ------------          ------ -------------- ----------------- ---------
0      VMware Virtual disk   60     GPT            Online            False
1      VMware Virtual disk   10     RAW            Offline           True
```

Смотрим: `PartitionStyle = RAW` — диск новый, не размечен. `IsOffline = True` — типичная SAN-политика Windows Server, новый диск приходит выключенным. Номер диска — то, чем ты будешь оперировать дальше, поэтому проверяй `FriendlyName` и размер, а не «на глаз».

**2. Перевод диска в онлайн и инициализация.**

```powershell
Set-Disk -Number 1 -IsOffline $false -IsReadOnly $false
Initialize-Disk -Number 1 -PartitionStyle GPT
```

`-IsReadOnly $false` снимает бит «только чтение» — он тоже ставится политикой для внешних LUN'ов. `Initialize-Disk` пишет служебную разметку; все данные на диске при этом уничтожаются.

**3. Создание раздела и форматирование одной строкой.**

```powershell
New-Partition -DiskNumber 1 -UseMaximumSize -DriveLetter E |
  Format-Volume -FileSystem NTFS -NewFileSystemLabel "Data01" -AllocationUnitSize 4096 -Confirm:$false
```

`-UseMaximumSize` берёт всё свободное место. `AllocationUnitSize` (кластер) 4 КБ — стандарт; для файлов >64 КБ и СУБД иногда ставят 64 КБ, но менять размер кластера после форматирования нельзя.

**4. `diskpart` — старый добрый инструмент, когда PowerShell неудобен.**

```cmd
diskpart
list disk
select disk 1
detail disk
clean
convert gpt
create partition primary
format fs=ntfs quick label="Data01"
assign letter=E
```

`detail disk` показывает серийный номер и путь к устройству — отличный способ убедиться, что ты не чистишь не тот диск. `clean` уничтожает разметку полностью.

**5. `Get-Partition` и `Get-Volume` — где лежит том.**

```powershell
Get-Partition -DriveLetter E | Format-List *
Get-Volume -DriveLetter E | Format-Table DriveLetter, FileSystemLabel, FileSystem, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}, HealthStatus
```

В `Get-Partition` смотрим `Offset` и `Size`: если после конца раздела есть неразмеченное пространство — том можно расширить.

**6. Расширение тома онлайн.**

```powershell
Get-PartitionSupportedSize -DriveLetter E -ErrorAction SilentlyContinue
# или после Resize-VirtualDisk:
Resize-Partition -DriveLetter E -Size 12GB
```

`Get-PartitionSupportedSize` (работает не на всех типах устройств — отсюда `-ErrorAction SilentlyContinue`) показывает минимальный и максимальный размер, до которого том может вырасти. `Resize-Partition` делает всё без перезагрузки, если свободное место **сразу за** разделом. Для системного тома C: расширение онлайн тоже возможно, но иногда Windows просит перезагрузку для применения — планируй окно.

**7. Storage Spaces: пул.**

```powershell
Get-PhysicalDisk | Format-Table FriendlyName, MediaType, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}, CanPool, HealthStatus
New-StoragePool -FriendlyName "SP-Data01" -StorageSubSystemFriendlyName "Windows Storage*" -PhysicalDisks (Get-PhysicalDisk -CanPool $true)
```

`-CanPool $true` фильтрует только свободные диски — защита от того, чтобы случайно затянуть в пул системный диск. `-StorageSubSystemFriendlyName "Windows Storage*"` — обязательный параметр, имя подсистемы почти всегда `Windows Storage Spaces`.

**8. Storage Spaces: виртуальный диск и том.**

```powershell
New-VirtualDisk -StoragePoolFriendlyName "SP-Data01" -FriendlyName "VD-Data01" `
  -ResiliencySettingName Mirror -NumberOfDataCopies 2 -Size 8GB -ProvisioningType Thin
Get-VirtualDisk | Get-Disk | Set-Disk -IsOffline $false
Resize-VirtualDisk -FriendlyName "VD-Data01" -Size 12GB
```

`Mirror` + копии 2 — нужен минимум два диска в пуле. `-ProvisioningType Thin` — место выделяется по факту записи. `Resize-VirtualDisk` увеличивает виртуальный диск, после чего том расширяется командой из пункта 6. Обратный путь — `Resize-VirtualDisk -Size` в меньшую сторону, но только для thin и только если данных реально меньше.

**9. iSCSI-target на FS01.**

```powershell
Install-WindowsFeature FS-iSCSITarget-Server -IncludeManagementTools
New-IscsiVirtualDisk -Path "E:\iSCSI\srv1c01-data.vhdx" -Size 5GB
New-IscsiServerTarget -TargetName "SRV1C01-Data" -InitiatorIds "IQN:*"
Add-IscsiVirtualDiskTargetMapping -TargetName "SRV1C01-Data" -DevicePath "E:\iSCSI\srv1c01-data.vhdx"
Get-IscsiServerTarget | Format-Table TargetName, Status, Enable
```

`-InitiatorIds "IQN:*"` разрешает подключиться **любому** инициатору — в лаборатории удобно, в бою недопустимо (см. раздел 6). GUI-путь: Server Manager → File and Storage Services → iSCSI → New iSCSI Virtual Disk (мастер).

**10. iSCSI-initiator на SRV1C01.**

```powershell
Set-Service MSiSCSI -StartupType Automatic
Start-Service MSiSCSI
New-IscsiTargetPortal -TargetPortalAddress 10.10.10.20
Get-IscsiTarget | Format-Table NodeAddress, IsConnected
Get-IscsiTarget | Connect-IscsiTarget -IsPersistent $true
Update-HostStorageCache
Get-IscsiSession | Format-Table SessionIdentifier, TargetNodeAddress, IsPersistent
```

`-IsPersistent $true` добавляет таргет в «Избранное» — сессия переподключится после перезагрузки. Без этого диск после ребута исчезнет (это и будет наша поломка). `Update-HostStorageCache` заставляет Windows перечитать список устройств без перезагрузки.

**11. MPIO.**

```powershell
Install-WindowsFeature Multipath-IO -IncludeManagementTools
Enable-MSDSMAutomaticClaim -BusType iSCSI
Get-MPIOAvailableHW
mpclaim -s -d
Set-MSDSMGlobalDefaultLoadBalancePolicy -Policy RR
```

`mpclaim -s -d` показывает диски и пути: сколько активных, сколько в ожидании. `Enable-MSDSMAutomaticClaim` включает автоматический захват новых iSCSI-устройств MPIO. Политику `RR` (round robin) ставят, когда путей реально два и они равнозначны.

**12. Проверка файловой системы и TRIM.**

```powershell
Repair-Volume -DriveLetter E -Scan
Optimize-Volume -DriveLetter E -ReTrim -Verbose
```

`Repair-Volume -Scan` — онлайн-проверка NTFS без отключения тома (аналог `chkdsk /scan`). `-ReTrim` отправляет TRIM-команды, полезно для SSD и thin-хранилищ, чтобы освободившиеся блоки реально освобождались.

**Linux-сторона (пригодится для DevOps):**

```bash
lsblk -f                      # дерево дисков, ФС, точки монтирования
df -h                         # свободное место на томах
sudo parted -l                # разметка и тип таблицы разделов
sudo growpart /dev/sda 3      # расширить раздел (пакет cloud-guest-utils [ПРОВЕРИТЬ])
sudo resize2fs /dev/sda3      # для ext4 — онлайн
sudo xfs_growfs /             # для XFS — только онлайн, уменьшать нельзя
sudo iscsiadm -m discovery -t sendtargets -p 10.10.10.20
sudo iscsiadm -m node -T <iqn> -l    # логин в таргет
```

С UBU01 можно подключить тот же iSCSI-таргет через `open-iscsi` и убедиться, что FS01 отдаёт блочное устройство любой ОС.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L1 полностью готов: DC01 (10.10.10.10), FS01 (10.10.10.20), домен `corp.vektor.lab`, учётка `adm.student`, пароль стенда `Lab#2026-vektor`.
- **SRV1C01** (10.10.10.25, Windows Server 2022 Evaluation, шлюз 10.10.10.2, DNS 10.10.10.10, в домен вводить не нужно). Если ты её ещё не поднимал — создай сейчас: это машина уровня L2 из таблицы стенда, для темы iSCSI она нужна как инициатор.
- FS01 логинится под `VEKTOR\adm.student` (член группы «Администраторы»).

### Снапшот

На FS01, SRV1C01 и DC01: VM → Snapshot → Take Snapshot, имя `before-l2-012`. В PowerShell на FS01 зафиксируй исходное состояние:

```powershell
Get-Disk | Out-File C:\stand-before-l2-012.txt
Get-Volume | Out-File C:\stand-before-l2-012.txt -Append
```

### Шаги

1. **FS01, VMware: добавь три диска.** VM → Settings → Add → Hard Disk → SCSI → Create a new virtual disk → 10 GB, одинарный файл. Повтори трижды. *Ожидаемо:* в настройках ВМ появились три жёстких диска по 10 ГБ.
2. **FS01: посмотри новые диски.** `Get-Disk`. *Ожидаемо:* диски с номерами 1–3, `PartitionStyle = RAW`, `IsOffline = True`.
3. **FS01: включи и инициализируй.**

```powershell
1..3 | ForEach-Object {
  Set-Disk -Number $_ -IsOffline $false -IsReadOnly $false
  Initialize-Disk -Number $_ -PartitionStyle GPT
}
```

*Ожидаемо:* у всех трёх дисков `PartitionStyle = GPT`, `OperationalStatus = Online`.

4. **FS01: создай пул.**

```powershell
New-StoragePool -FriendlyName "SP-Data01" -StorageSubSystemFriendlyName "Windows Storage*" `
  -PhysicalDisks (Get-PhysicalDisk -CanPool $true)
Get-StoragePool -FriendlyName "SP-Data01" | Format-Table FriendlyName, HealthStatus, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}
```

*Ожидаемо:* пул `SP-Data01`, `Healthy`, около 30 ГБ.

5. **FS01: создай зеркальный виртуальный диск и том.**

```powershell
New-VirtualDisk -StoragePoolFriendlyName "SP-Data01" -FriendlyName "VD-Data01" `
  -ResiliencySettingName Mirror -NumberOfDataCopies 2 -Size 8GB -ProvisioningType Thin
Get-VirtualDisk | Get-Disk | Set-Disk -IsOffline $false
Update-HostStorageCache
Get-Disk
```

*Ожидаемо:* появился диск 8 ГБ. Определи его номер (`Get-Disk | Where-Object FriendlyName -like "*Virtual Disk*"`).

6. **FS01: раздел и формат.**

```powershell
New-Partition -DiskNumber <номер> -UseMaximumSize -DriveLetter E |
  Format-Volume -FileSystem NTFS -NewFileSystemLabel "Data01" -Confirm:$false
Get-Volume -DriveLetter E
```

*Ожидаемо:* том E:, NTFS, метка `Data01`, ~8 ГБ, `Healthy`.

7. **FS01: подними iSCSI-target и создай LUN.**

```powershell
Install-WindowsFeature FS-iSCSITarget-Server -IncludeManagementTools
New-Item -Path "E:\iSCSI" -ItemType Directory
New-IscsiVirtualDisk -Path "E:\iSCSI\srv1c01-data.vhdx" -Size 5GB
New-IscsiServerTarget -TargetName "SRV1C01-Data" -InitiatorIds "IQN:*"
Add-IscsiVirtualDiskTargetMapping -TargetName "SRV1C01-Data" -DevicePath "E:\iSCSI\srv1c01-data.vhdx"
Get-IscsiServerTarget | Format-Table TargetName, Status, Enable
```

*Ожидаемо:* таргет в статусе `Enabled`, файл `srv1c01-data.vhdx` на E:.

8. **SRV1C01: подключи LUN.**

```powershell
Set-Service MSiSCSI -StartupType Automatic; Start-Service MSiSCSI
New-IscsiTargetPortal -TargetPortalAddress 10.10.10.20
Get-IscsiTarget
Get-IscsiTarget | Connect-IscsiTarget -IsPersistent $true
Update-HostStorageCache
Get-Disk
```

*Ожидаемо:* в `Get-Disk` появился диск 5 ГБ с `IsOffline = True` (SAN-политика). Включи:

```powershell
Set-Disk -Number <номер> -IsOffline $false -IsReadOnly $false
Initialize-Disk -Number <номер> -PartitionStyle GPT
New-Partition -DiskNumber <номер> -UseMaximumSize -DriveLetter D |
  Format-Volume -FileSystem NTFS -NewFileSystemLabel "Data01-1C" -Confirm:$false
"test" | Out-File D:\1c-test.txt
```

*Ожидаемо:* диск D: 5 ГБ, файл `1c-test.txt` создан.

9. **FS01: расширь виртуальный диск и том онлайн.**

```powershell
Resize-VirtualDisk -FriendlyName "VD-Data01" -Size 12GB
Update-HostStorageCache
Get-Partition -DriveLetter E
Resize-Partition -DriveLetter E -Size 12GB
Get-Volume -DriveLetter E
```

*Ожидаемо:* том E: вырос до 12 ГБ без перезагрузки и без отключения тома, файлы на месте.

10. **Проверь, что расширение не сломало iSCSI.** На SRV1C01: `Get-Volume -DriveLetter D`, открой `D:\1c-test.txt`, затем `Get-IscsiSession`. *Ожидаемо:* диск D: жив, сессия активна, `IsPersistent = True`.
11. **Опционально: MPIO.** Установи роль на SRV1C01 (`Install-WindowsFeature Multipath-IO -IncludeManagementTools`), выполни `Enable-MSDSMAutomaticClaim -BusType iSCSI`, перезагрузись, проверь `mpclaim -s -d`. Полноценные два пути требуют второго портала: добавь FS01 второй адаптер в VMnet8 с адресом `10.10.10.21` — **это расширение стенда, зафиксируй его в `stand-log.md`** (адрес свободен в IP-плане). Без второго портала MPIO покажет один путь — это нормально, механику ты увидишь.
12. **Запиши в `stand-log.md`:** новые диски FS01, пул, виртуальный диск, том E:, роль iSCSI Target, таргет, диск D: на SRV1C01.

### Проверка

- [ ] `Get-StoragePool SP-Data01` → `Healthy`, 3 физических диска.
- [ ] `Get-VirtualDisk VD-Data01` → `Mirror`, `Healthy`, 12 ГБ.
- [ ] `Get-Volume -DriveLetter E` → NTFS, `Data01`, 12 ГБ, `Healthy`.
- [ ] `Get-IscsiServerTarget` на FS01 → `Enabled`.
- [ ] `Get-IscsiSession` на SRV1C01 → одна активная сессия, `IsPersistent = True`.
- [ ] На SRV1C01 существует `D:\1c-test.txt`.
- [ ] В `stand-log.md` записаны все изменения.

### Сломай и почини

```text
Пользователь / ВМ: SRV1C01 (учётка adm.student)
Что написал: «Перезагрузил сервер 1С, а диска D: нет вообще. 1С пишет "база не найдена", ночное резервное копирование не пройдёт»
Срочность: Высокая
```

**Воспроизведение поломки** (на SRV1C01, от админа):

```powershell
$s = Get-IscsiSession
Unregister-IscsiSession -SessionIdentifier $s.SessionIdentifier
Disconnect-IscsiSession -SessionIdentifier $s.SessionIdentifier -Confirm:$false
Set-Service MSiSCSI -StartupType Manual
Restart-Computer
```

**Диагностика по порядку:**

1. На SRV1C01 `Get-Disk` — диска 5 ГБ нет. `Get-Volume` — D: отсутствует.
2. `Get-Service MSiSCSI` → `Stopped`, `StartType = Manual`. Уже зацепка: служба не стартует сама.
3. `Start-Service MSiSCSI; Get-IscsiSession` → сессий нет. `Get-IscsiTarget` → таргет виден, `IsConnected = False`. Значит, сеть и портал в порядке, проблема в постоянстве сессии.
4. Проверь портал: `Get-IscsiTargetPortal` → `10.10.10.20:3260` есть. Если нет — `New-IscsiTargetPortal`.
5. На FS01: `Get-IscsiServerTarget | Format-Table TargetName, Status, Enable` → `Enabled`; `Get-IscsiVirtualDisk` → VHDX на месте; `Get-Volume -DriveLetter E` → `Healthy`. То есть таргет здоров, виноват инициатор.
6. Проверь журнал System на SRV1C01 (Event Viewer → Windows Logs → System) с источником `msiscsi` и `Disk` — там будут события об остановке/неудачном подключении.
7. **Исправление:**

```powershell
Set-Service MSiSCSI -StartupType Automatic
Start-Service MSiSCSI
Get-IscsiTarget | Connect-IscsiTarget -IsPersistent $true
Update-HostStorageCache
Get-Disk | Format-Table Number, FriendlyName, OperationalStatus, IsOffline
```

Если диск появился, но `IsOffline = True` — это SAN-политика:

```cmd
diskpart
san
san policy=onlineall
exit
```

Затем `Set-Disk -Number <н> -IsOffline $false -IsReadOnly $false`, проверь букву D: (`Get-Partition -DiskNumber <н>`), при необходимости назначь: `Set-Partition -DiskNumber <н> -PartitionNumber 1 -NewDriveLetter D`.

8. Убедись, что `D:\1c-test.txt` на месте, и что после `Restart-Computer` диск возвращается сам.

**Ответ пользователю:** «Диск вернули, данные на месте. Причина: служба iSCSI-инициатора стояла в ручном запуске и сессия не была сохранена как постоянная, поэтому после перезагрузки том не подключился. Сейчас служба в автозапуске, сессия постоянная. Проверил: диск D: доступен, файл базы открывается, можете запускать регламентное копирование».

**Закрытие заявки:** категория «Серверы → Хранилище», решение — «включён автозапуск MSiSCSI, сессия iSCSI сделана постоянной», в примечании — ссылка на runbook «Проверка iSCSI-инициатора после перезагрузки». Добавь в мониторинг проверку `Get-IscsiSession` (в L2 достаточно ручной проверки, на треке сисадмина — Zabbix на MON01).

### Что изменилось на стенде

- **FS01:** добавлены 3 виртуальных диска по 10 ГБ (GPT, online).
- **FS01:** создан пул `SP-Data01`, виртуальный диск `VD-Data01` (Mirror, thin, 12 ГБ).
- **FS01:** новый том `E:` NTFS, метка `Data01`, 12 ГБ; папка `E:\iSCSI`.
- **FS01:** установлена роль iSCSI Target Server; VHDX `E:\iSCSI\srv1c01-data.vhdx` (5 ГБ); таргет `SRV1C01-Data`.
- **SRV1C01:** поднята ВМ (10.10.10.25), служба `MSiSCSI` в автозапуске, постоянная сессия с FS01, диск `D:` NTFS `Data01-1C` 5 ГБ.
- **Опционально:** второй адаптер FS01 в VMnet8 с адресом 10.10.10.21 — расширение стенда, зафиксировать.

### Откат

1. На SRV1C01 отключи сессию: `Get-IscsiSession | Disconnect-IscsiSession -Confirm:$false`.
2. На FS01, SRV1C01 и DC01: VM → Snapshot → Snapshot Manager → выбери `before-l2-012` → Revert.
3. После отката FS01 вернётся к состоянию без добавленных дисков, пула и роли — добавленные в шаге 1 диски исчезнут из ВМ (это нормально).
4. Если нужен частичный откат (оставить пул, убрать только iSCSI): `Remove-IscsiServerTarget`, `Remove-IscsiVirtualDisk`, `Uninstall-WindowsFeature FS-iSCSITarget-Server`.
5. Обнови `stand-log.md` записью «откат к before-l2-012, состояние такое-то».

## 6. Ошибки новичков и безопасность

- **Расширять том, не проверив, что свободное место сразу за ним.** → Свободное место есть, а `Resize-Partition` падает: между разделом и «хвостом» стоит recovery-раздел. → Смотри карту разделов (`Get-Partition -DiskNumber N`), планируй разметку заранее; в крайнем случае — GParted Live с предварительным бэкапом, а не «на живую» на боевом сервере.
- **Путать номер диска.** → `Initialize-Disk -Number 1` на не том сервере = потеря данных. → Всегда сверяй `SerialNumber` и размер, используй `detail disk` в diskpart перед `clean`.
- **Считать Storage Spaces «просто RAID-ом» и брать `Simple`.** → Один сбой диска = потеря всего пула, а не «немного данных». → Для данных — `Mirror` или `Parity`; `Simple` только для временных/восстановимых данных.
- **Забыть про overcommit в thin provisioning.** → Виртуальные диски суммарно объявлены на 100 ГБ в пуле на 60 ГБ; пул заполняется, и все тома разом уходят в ошибку/только чтение. → Держи 20–30% запаса пула и мониторь `Get-StoragePool | Get-StoragePool`-показатели `AllocatedSize` против `Size`.
- **Оставлять `-InitiatorIds "IQN:*"` в проде.** → Любой хост в сети может подключить LUN. → Указывай конкретный IQN инициатора и включай CHAP.
- **Забыть `-IsPersistent $true`.** → После ребута диск исчезает (ровно наша поломка). → Постоянная сессия + служба `MSiSCSI` в автозапуске, проверка в чек-листе приёмки сервера.
- **Хранить единственную копию на одном пуле/сервере.** → Пул, RAID и снапшот ВМ — не бэкап. → Бэкап на отдельный носитель, правило 3-2-1.
- **Менять размер кластера/файловую систему «на лету».** → `Format-Volume` уничтожает данные; ReFS нельзя сделать загрузочным, XFS нельзя уменьшить. → Решай до форматирования, проверяй совместимость ФС с ролью (1С — NTFS).

**Безопасность.** iSCSI по умолчанию не шифруется и не аутентифицирует клиента: любой, кто оказался в сети 10.10.10.0/24, при открытом `IQN:*` увидит и подключит LUN. В бою — отдельный VLAN/подсеть для SAN-трафика, ограничение по IQN, CHAP, при необходимости IPsec, и никакого SAN-трафика через офисный коммутатор. Также не выдавай пользователям права администратора ради «чтобы видели диск» — права на данные решаются NTFS-правами (следующая глава про файловый сервер), а не членством в локальных админах.

## 7. Вопросы на собеседовании

**1. «Как расширить том на Windows без простоя и что делать, если место не сразу за разделом?»**

Ответ: NTFS и ReFS расширяются онлайн через `Resize-Partition` или Disk Management, если сразу после раздела есть неразмеченное пространство; для системного тома иногда нужна перезагрузка. Если место отделено другим разделом (например, recovery), штатными средствами не расширить — либо пересоздавать разметку с переносом, либо сторонний инструмент вроде GParted с бэкапом. Для виртуальных дисков сначала расширяем сам диск (в VMware — увеличить размер VMDK, для Storage Spaces — `Resize-VirtualDisk`), потом `Update-HostStorageCache`, потом том.

*Что хочет услышать:* понимание «сначала железо/гипервизор, потом раздел, потом ФС» и что расширение — не бэкап.
*Красный флаг:* «сделаю через Acronis/GParted на боевом, ничего страшного».

**2. «Чем Storage Spaces отличается от аппаратного RAID и что такое thin provisioning?»**

Ответ: Storage Spaces — программный уровень в ОС, пул из физических дисков, виртуальные диски с устойчивостью Simple/Mirror/Parity, гибкое перераспределение, интеграция с кластером (S2D). Аппаратный RAID делает контроллер, ОС видит готовый LUN, не зависит от драйверов ОС, но и не даёт такой гибкости; замена диска — через контроллер. Thin provisioning — виртуальный диск объявляется больше физически выделенного, место расходуется по мере записи; риск — overcommit и заполнение пула, после чего тома могут стать недоступны для записи.

*Что хочет услышать:* разницу уровней (ОС vs контроллер), практические риски thin.
*Красный флаг:* «Storage Spaces — это всегда RAID-5» или «thin provisioning экономит место, поэтому можно объявлять сколько угодно».

**3. «Как работает iSCSI: кто такие target и initiator, какой порт, как защитить?»**

Ответ: iSCSI инкапсулирует SCSI-команды в TCP, порт 3260. Target отдаёт LUN (в Windows — роль iSCSI Target Server, отдаёт VHDX), initiator подключает (служба MSiSCSI, портал IP:3260, идентификация по IQN). Защита: ограничение по IQN инициатора (не `IQN:*`), CHAP, IPsec, отдельный VLAN/подсеть, не выдавать LUN'ы в общую офисную сеть.

*Что хочет услышать:* порт, IQN, роль постоянной сессии, что трафик по умолчанию не шифруется.
*Красный флаг:* «iSCSI — это файловая шара» (это блочное устройство, в отличие от SMB).

**4. «Что такое MPIO и чем он отличается от teaming сетевых карт?»**

Ответ: MPIO даёт несколько путей между инициатором и конкретным LUN: при отказе кабеля/порта/коммутатора диск продолжает работать через другой путь, плюс балансировка. Работает через DSM, политики FOO/RR/LQD/LB/SO, в Windows — роль Multipath I/O и `Enable-MSDSMAutomaticClaim -BusType iSCSI`. NIC teaming объединяет физические адаптеры в один логический интерфейс на уровне L2/L3 и проблему «одна карта — один путь до массива» решает лишь частично; MPIO — уровень хранилища и конкретных LUN.

*Что хочет услышать:* понимание, что MPIO — про пути к устройству, teaming — про агрегацию каналов, и что это дополняющие технологии.
*Красный флаг:* «MPIO и teaming — одно и то же».

**5. «Сервер после перезагрузки не видит диск с iSCSI. Твои действия?»**

Ответ: проверить службу `MSiSCSI` (запущена, автозапуск), `Get-IscsiSession`/`Get-IscsiTarget` — есть ли сессия и постоянная ли она, затем портал, затем сеть (пинг, 3260), затем на таргете — статус таргета и VHDX. Если диск появился, но offline — SAN-политика (`diskpart` → `san` → `onlineall`) и `Set-Disk -IsOffline $false`. Проверить журнал System (источники `msiscsi`, `Disk`). После исправления — сделать сессию постоянной и проверить перезагрузкой.

*Что хочет услышать:* порядок диагностики от инициатора к таргету и обратно, знание SAN-политики и `-IsPersistent`.
*Красный флаг:* «сразу переустановлю роль / переформатирую диск».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Storage Spaces overview» (Storage Spaces, пулы, resiliency settings). Поиск: `Storage Spaces overview site:learn.microsoft.com`.
- **learn.microsoft.com** — «iSCSI Target Server overview» и «iSCSI Target Boot and Storage» (роль FS-iSCSITarget-Server, создание VHDX и таргета). Поиск: `iSCSI Target Server overview learn.microsoft.com`.
- **learn.microsoft.com** — справочник по командлетам хранилища: `Resize-Partition`, `Resize-VirtualDisk`, `New-VirtualDisk`, `Connect-IscsiTarget`. Поиск: `Resize-Partition Storage module learn.microsoft.com`.
- **learn.microsoft.com** — «Multipath I/O overview» и «MPIO cmdlets» (`Enable-MSDSMAutomaticClaim`, `Get-MPIOAvailableHW`). Поиск: `Multipath I/O overview learn.microsoft.com`.
- **learn.microsoft.com** — «Diskpart command-line options», раздел `san` и `detail disk`. Поиск: `diskpart san policy learn.microsoft.com`.
- **learn.microsoft.com** — «ReFS overview» (что поддерживает, а что нет, включая загрузку). Поиск: `ReFS overview learn.microsoft.com`.
- **ubuntu.com** — серверная документация по `open-iscsi` и утилитам `parted`/`lsblk`. Поиск: `Ubuntu server open-iscsi documentation`. Название пакета для `growpart` — `cloud-guest-utils` `[ПРОВЕРИТЬ для 24.04]`.
- **kernel.org / man7.org** — man-страницы `iscsiadm(8)`, `resize2fs(8)`, `xfs_growfs(8)`. Поиск: `man iscsiadm sendtargets`.
- **docs.vmware.com** — «Add a virtual hard disk to a virtual machine» (VMware Workstation). Поиск: `VMware Workstation add hard disk documentation`.
