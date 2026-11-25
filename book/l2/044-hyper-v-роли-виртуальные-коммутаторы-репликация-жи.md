# Hyper-V: роли, виртуальные коммутаторы, репликация, живая миграция

## 1. Введение (зачем это нужно в реальной работе)

> **Заявка #4412.** Петрова (HR), ВМ WIN10-CL02: «Здравствуйте! С утра не открывается папка `\\FS01\HR` — пишет "Не удаётся получить доступ". Вчера всё работало. У Иванова в бухгалтерии `\\FS01\Buh` при этом открывается. Срочность: высокая, готовим приказы на увольнение».

Ты идёшь на FS01 — сервер пингуется, служба жива, но папка HR отвалилась. А FS01 — это виртуальная машина. И настоящая причина оказывается не в «сетевых настройках Windows», а в том, что ночью на хосте Hyper-V пересоздавали виртуальный коммутатор, и один из vNIC уехал в другой VLAN. Классика: L2-инженер, который не понимает, как устроена виртуализация, полдня проверяет DNS и кабели там, где проблема в виртуальном свиче.

В работе L2 виртуалка — это не «иконка в консоли». Ты будешь вручную ставить роль Hyper-V на серверы, объяснять коллегам, почему после включения Hyper-V «пропал интернет у хоста», настраивать репликацию, чтобы у бизнеса был горячий резерв, и выполнять живую миграцию в окно обслуживания без простоя. После этой главы ты умеешь: ставить роль Hyper-V и читать состояние хоста, различать три типа виртуальных коммутаторов и знать, когда какой нужен, понимать, что происходит на хосте при создании внешнего свича, настраивать Hyper-V Replica с тестовым и плановым переключением, и выполнять/диагностировать живую миграцию между двумя хостами.

Лицензии: всё делаем на **Windows Server 2022 Evaluation** (180 дней, роль Hyper-V входит бесплатно). В боевой среде лицензируется сам Windows Server по ядрам — Evaluation для продакшена не годится.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Hyper-V — гипервизор **1-го типа** (помнишь урок 41): он лежит прямо на железе (точнее, под Windows-хостом), а Windows-хост сам становится **родительским разделом** (root partition) — обычной ВМ с особыми правами.

```text
┌──────────────────────────────────────────────────────────────────┐
│                        ФИЗИЧЕСКИЙ СЕРВЕР                         │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  ГИПЕРВИЗОР (hvix64.exe / hvax64.exe)                      │  │
│  │  планировщик vCPU, управление памятью, VMBus (виртуальная   │  │
│  │  шина), адресация SLAT (EPT/NPT)                            │  │
│  └────────────────────────────────────────────────────────────┘  │
│      │ root partition              │ VM1            │ VM2         │
│      ▼                            ▼                ▼             │
│  ┌──────────────────┐    ┌────────────────┐ ┌───────────────┐    │
│  │ Windows Server   │    │ Windows /      │ │ Ubuntu (LIS)  │    │
│  │ (роль Hyper-V):  │    │ Linux-гость    │ │               │    │
│  │ - vmms (VMMS)    │    │ vmwp.exe       │ │ vmwp.exe      │    │
│  │ - WMI root\virt..│    │ VMBus → VSP/VSC│ │               │    │
│  │ - vmwp.exe       │    │ vNIC, vHDD     │ │               │    │
│  └──────────────────┘    └────────────────┘ └───────────────┘    │
│           │                     │                │               │
│      ┌────┴─────────────────────┴────────────────┴────┐          │
│      │  ВИРТУАЛЬНЫЙ КОММУТАТОР (vSwitch, внутри vmms) │          │
│      │  External / Internal / Private                 │          │
│      └────────────────────┬───────────────────────────┘          │
└───────────────────────────┼──────────────────────────────────────┘
                            ▼
                    ФИЗИЧЕСКИЙ NIC → кабель → коммутатор
```

**Роли и службы на хосте.** Служба **VMMS** (`vmms`) — «менеджер» Hyper-V, хранит конфигурацию ВМ, общается с гипервизором через **WMI-провайдер** `root\virtualization\v2` (PowerShell-модуль `HyperV` — это обёртка над ним). Для каждой запущенной ВМ есть процесс **`vmwp.exe`** (VM Worker Process) — он и есть «эмуляция» её железа. Гости не лезут в железо напрямую, а ходят по **VMBus** к родительскому разделу: гость использует драйвер **VSC** (Virtualization Service Client), родитель — **VSP** (Service Provider). Интеграционные службы (Integration Services) — это и есть набор этих драйверов плюс «сервисы» (Heartbeat, Time Sync, VSS-снапшоты, KVP-обмен данными).

**Коммутаторы.** Три типа, разница только в том, кого видит трафик:
- **External** — привязан к физическому NIC. ВМ выходят в реальную сеть, хост получает в свой стек виртуальный адаптер **`vEthernet (<имя свича>)`**, а IP-конфиг «переезжает» с физического NIC на него.
- **Internal** — ВМ + сам хост (нужен, например, для NAT и лабораторных сетей).
- **Private** — только ВМ между собой, хост не участвует.

Что ещё есть на порту коммутатора: пул MAC-адресов Hyper-V, **DHCP guard** (не дать ВМ поднять свой DHCP), **Router guard** (не дать объявить себя маршрутизатором), **port ACL** (разрешить/запретить MAC/IP), ограничение полосы, VLAN ID. Плюс аппаратные ускорения: VMQ, SR-IOV, RDMA — на L2 достаточно знать, что они есть и что при странной производительности их иногда отключают.

**Живая миграция.** ВМ переезжает с хоста A на хост B, пока работает: сначала копируется память (в несколько проходов, «грязные» страницы добираются на финальном проходе), в самом конце на долю секунды ВМ замирает и управление передаётся на B. В домене аутентификация — **Kerberos**, плюс нужна **constrained delegation** на объекте компьютера в AD. Если общего хранилища нет, работает **shared-nothing migration**: диск ВМ тоже переносится по сети (`-IncludeStorage`). Транспорт миграции — **TCP 6600**, есть сжатие и SMB-вариант.

**Репликация (Hyper-V Replica)** — асинхронная отправка изменений диска ВМ на второй хост. Реплика не «живая»: у неё своя копия, обновляется каждые N секунд (минимум 30). Работает по **HTTP (TCP 80, Kerberos)** или **HTTPS (TCP 443, сертификат)**.

**Порты, которые надо помнить:**

```text
TCP 6600        живая миграция
TCP 80 / 443    Hyper-V Replica (Kerberos / сертификат)
TCP 5985/5986   WinRM (PowerShell Remoting, Hyper-V Manager удалённо)
TCP 135 + динам. RPC, TCP 445  управление, VHDX по SMB
```

```powershell
# где смотреть всё это вживую на хосте
Get-VMHost | Format-List Name, LogicalProcessorCount, MemoryCapacity, `
    VirtualMachineMigrationEnabled, VirtualMachineMigrationAuthenticationType
```

## 3. Аналогия из жизни

Hyper-V — это **новый многоквартирный дом**. Гипервизор — фундамент и несущие конструкции: он один на всё здание и распределяет воду, свет и тепло. Родительский раздел (твой Windows Server) — управляющая компания: у неё есть ключи от всех квартир, но живёт она в одной из них. ВМ — квартиры: у каждой свои счётчики (vCPU, RAM), своя входная дверь (vNIC) и свой план помещений (VHDX). VMBus — стояки и общие шахты: по ним квартира получает услуги, не пробивая стены. Виртуальный коммутатор — распределительный щит в подвале: внешний свич — щит, подключённый к городской линии (у управляющей компании тогда «пропадает» прямой кабель и появляется розетка в подвале), внутренний — щит на дом и УК, приватный — только между квартирами. Checkpoint — фотография квартиры: мебель запомнили, но соседи продолжают жить, и откат вернёт всё к моменту фото. Живая миграция — переезд квартиры целиком в соседний дом без выключения света: вещи перевозят, пока хозяева смотрят телевизор, и только в момент подключения к новой подстанции свет мигнёт на секунду. Репликация — второй такой же дом в другом городе, куда каждые пять минут отправляют фотографии изменений: если первый дом сгорит, во втором можно жить, но последние 5 минут ремонта потеряны.

Где аналогия ломается: в доме нельзя выдать квартире «половину несущей стены», а vCPU и память Hyper-V делят динамически, и переезд (миграция) юридически возможен только между домами одной серии — то есть с совместимыми процессорами.

## 4. Практика и команды

**1. Установка роли и проверка.**
```powershell
Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart
Get-WindowsFeature Hyper-V
Get-VMHost | Select-Object Name, LogicalProcessorCount, MemoryCapacity
```
`-IncludeManagementTools` тянет Hyper-V Manager и модуль PowerShell. `-Restart` перезагрузит сервер. Вывод `Get-VMHost`: имя хоста, число логических ядер, объём RAM. GUI: **Server Manager → Manage → Add Roles and Features → Hyper-V**.

**2. Список ВМ и их состояние.**
```powershell
Get-VM | Select-Object Name, State, CPUUsage, MemoryAssigned, Uptime, Path
```
Смотрим: `State` (Running/Off/Saved), `MemoryAssigned` (сколько реально дали), `Uptime` (после миграции сбрасывается — удобный маркер), `Path` (где лежит конфиг, а рядом — VHDX).

**3. Создание внешнего коммутатора.**
```powershell
Get-NetAdapter | Format-Table Name, InterfaceDescription, Status
New-VMSwitch -Name "vSwitch-Office" -NetAdapterName "Ethernet0" -AllowManagementOS $true
Get-VMSwitch | Format-Table Name, SwitchType, NetAdapterInterfaceDescription
```
`-AllowManagementOS $true` оставляет хосту связь (создаётся `vEthernet (vSwitch-Office)`). Без этого параметра хост потеряет сеть — типичная авария «пропал RDP после создания свича».

**4. Внутренний и приватный коммутаторы.**
```powershell
New-VMSwitch -Name "vSwitch-Private" -SwitchType Private
New-VMSwitch -Name "vSwitch-Internal" -SwitchType Internal
```
Для Private/Internal **нельзя** указывать `-NetAdapterName`. У Internal хост получит `vEthernet` без IP — назначать вручную.

**5. Подключение ВМ к свичу и параметры порта.**
```powershell
Connect-VMNetworkAdapter -VMName HV01-VM01 -SwitchName "vSwitch-Office"
Get-VMNetworkAdapter -VMName HV01-VM01 | Format-List Name, SwitchName, MacAddress, VlanId, MacAddressSpoofing, DhcpGuard
Set-VMNetworkAdapter -VMName HV01-VM01 -VlanId 10
Set-VMNetworkAdapter -VMName HV01-VM01 -VlanId 0
```
`VlanId` ставится на vNIC гостя; работать будет только если физический порт коммутатора в транке. `MacAddressSpoofing` — разрешить ВМ отправлять кадры с «чужих» MAC (обязательно для вложенной виртуализации, в бою — дыра).

**6. Создание ВМ и настройка vCPU.**
```powershell
New-VM -Name HV01-VM01 -MemoryStartupBytes 2GB `
  -NewVHDPath D:\VMs\HV01-VM01\disk.vhdx -NewVHDSizeBytes 40GB `
  -SwitchName "vSwitch-Private" -Generation 2
Set-VMProcessor -VMName HV01-VM01 -Count 2
Set-VMProcessor -VMName HV01-VM01 -CompatibilityForMigrationEnabled $true
```
`-Generation 2` — UEFI, без эмуляции legacy BIOS. `CompatibilityForMigrationEnabled` урезает набор CPU-фич, чтобы ВМ переезжала между разными поколениями процессоров (в бою — спасение при миграции на новый сервер).

**7. Checkpoint (снапшот) и интеграционные службы.**
```powershell
Checkpoint-VM -VMName HV01-VM01 -SnapshotName "before-migration"
Get-VMSnapshot -VMName HV01-VM01 | Format-Table Name, SnapshotType, CreationTime
Get-VMIntegrationService -VMName HV01-VM01 | Format-Table Name, Enabled, PrimaryStatusDescription
Restore-VMSnapshot -VMName HV01-VM01 -Name "before-migration" -Confirm:$false
```
`SnapshotType` = Standard или Production. Production использует VSS внутри гостя (консистентный бэкап), Standard — «фотография диска на ходу». `PrimaryStatusDescription` у Integration Services — `OK` или `No contact` (нет драйверов/служба не отвечает).

**8. Включение и настройка живой миграции.**
```powershell
Enable-VMMigration
Set-VMHost -VirtualMachineMigrationAuthenticationType Kerberos `
            -VirtualMachineMigrationPerformanceOption Compression
Get-VMHost | Select-Object VirtualMachineMigrationEnabled, VirtualMachineMigrationAuthenticationType
```
Точный набор значений `-VirtualMachineMigrationPerformanceOption` в вашей сборке сверяйте через `Get-Help Set-VMHost -Parameter VirtualMachineMigrationPerformanceOption` `[ПРОВЕРИТЬ]`. GUI: **Hyper-V Manager → Hyper-V Settings → Live Migrations**.

**9. Сама миграция и её диагностика.**
```powershell
Compare-VM -Name HV01-VM01 -DestinationHost HV02.corp.vektor.lab | `
    Select-Object -ExpandProperty Incompatibilities
Move-VM -Name HV01-VM01 -DestinationHost HV02.corp.vektor.lab `
        -IncludeStorage -DestinationStoragePath D:\VMs\HV01-VM01
```
`Compare-VM` — прогон «примерки» без переноса: покажет несовместимости (нет пути, нет прав, другое поколение CPU). `-IncludeStorage` — shared-nothing: VHDX копируется по сети. Отсутствие `-IncludeStorage` означает, что диск должен быть доступен обоим хостам (SMB/CSV).

**10. Репликация: настройка и наблюдение.**
```powershell
# на HV02 (реплика)
Set-VMReplicationServer -ReplicationEnabled $true -AllowedAuthenticationType Kerberos `
    -ReplicationKerberosAuthenticationPort 80 -DefaultStoragePath D:\Replica

# на HV01 (источник)
Enable-VMReplication -VMName HV01-VM01 -ReplicaServerName HV02.corp.vektor.lab `
    -ReplicaServerPort 80 -AuthenticationType Kerberos `
    -CompressionEnabled $true -ReplicationFrequencySec 300
Get-VMReplication -VMName HV01-VM01
Measure-VMReplication -VMName HV01-VM01
```
`Measure-VMReplication` — главный прибор: `Health` (Normal/Warning/Critical), `LastReplicationTime`, `ReplicationLatency`, размер очереди. `ReplicationFrequencySec` минимум 30.

**11. Переключение на реплику.**
```powershell
Start-VMFailover -VMName HV01-VM01 -AsTest          # тестовое, создаёт "HV01-VM01 - Test"
Stop-VMFailover  -VMName HV01-VM01 -TurnOff         # убрать тестовую копию [ПРОВЕРИТЬ имя параметра в вашей сборке]

Start-VMFailover -Prepare -VMName HV01-VM01         # плановое: дослать хвост, выключить источник
Start-VMFailover -VMName HV01-VM01                  # на HV02
Complete-VMFailover -VMName HV01-VM01               # зафиксировать
Set-VMReplication -VMName HV01-VM01 -Reverse        # теперь HV02 — источник
```
Тестовое переключение **обязательно** перед боевым: оно поднимает копию в изолированной сети и не рвёт репликацию.

**12. Со стороны гостя Linux** (если ВМ — Ubuntu/Astra):
```bash
lsmod | grep -E 'hv_vmbus|hv_netvsc|hv_storvsc|hv_utils'
ip -br link                     # hv_netvsc = синтетический адаптер Hyper-V
systemd-analyze | head -3        # hv_utils даёт точную синхронизацию времени
```
Нет `hv_netvsc` — гость увидит только legacy-адаптер или вообще ничего: проверяй ядро и версию LIS-модулей.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- Стенд L2 в полном составе: DC01 (AD DS, DNS, DHCP 10.10.10.100–150), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01. Учётки `adm.student`, `ivanov`, `petrova` работают.
- **Важно про стенд.** Постоянного Hyper-V-хоста в таблице машин нет, и это осознанно: Hyper-V мы проходим на двух **временных** ВМ, которые после главы удаляются. Создаём в VMware Workstation:
  - **HV01** — Windows Server 2022 Evaluation, 4 vCPU, 8 ГБ RAM, 100 ГБ, сеть VMnet8, адрес **10.10.10.12**;
  - **HV02** — то же самое, адрес **10.10.10.13**.
  В `stand-log.md` сразу пометь их как временные лабораторные.
- На физическом ПК: ≥16 ГБ RAM (лучше 24–32), ~200 ГБ свободного места, в BIOS включена виртуализация (Intel VT-x + EPT или AMD-V + RVI).
- В настройках каждой ВМ в VMware: **VM → Settings → Processors → галочка "Virtualize Intel VT-x/EPT or AMD-V/RVI"** (и "Virtualize IOMMU", если есть). Без неё Hyper-V внутри гостя не заработает.
- Если на самом хосте включён Hyper-V/WSL2/Device Guard, VMware Workstation работает через Windows Hypervisor Platform и вложенная виртуализация может быть недоступна `[ПРОВЕРИТЬ на своём железе]`. Тогда временно: `bcdedit /set hypervisorlaunchtype off` + перезагрузка.

### Снапшот
В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-l2-044`. Сделать на **DC01** (появятся объекты компьютеров и настройки делегирования), а также на **HV01** и **HV02** сразу после установки ОС и ввода в домен, до установки роли Hyper-V.

### Шаги

1. **HV01/HV02, VMware Workstation.** Создай две ВМ по параметрам выше, установи Windows Server 2022 Evaluation (Desktop Experience). *Ожидаемо:* обе загрузились, видят сеть, получают адрес по DHCP от DC01.
2. **HV01/HV02, внутри гостя.** Задай статику: `10.10.10.12`/`10.10.10.13`, маска `255.255.255.0`, шлюз `10.10.10.2`, DNS `10.10.10.10`. Переименуй в `HV01`/`HV02`, перезагрузи, введи в домен `corp.vektor.lab` учёткой `VEKTOR\adm.student`.
   ```powershell
   nltest /dsgetdc:corp.vektor.lab
   ```
   *Ожидаемо:* «команда выполнена успешно», DC01 найден. На DC01 перенеси оба объекта компьютеров в OU `Серверы` (`Get-ADComputer HV01 | Move-ADObject -TargetPath "OU=Серверы,OU=Компьютеры,DC=corp,DC=vektor,DC=lab"` `[ПРОВЕРИТЬ DN в вашей структуре]`).
3. **HV01 и HV02.** Установи роль: `Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart`. После перезагрузки: `Get-VMHost | Select Name, LogicalProcessorCount, MemoryCapacity, VirtualMachineMigrationEnabled`.
   *Ожидаемо:* роль `Installed`, `VirtualMachineMigrationEnabled : False`, ядра и RAM видны.
4. **HV01.** Создай внешний коммутатор:
   ```powershell
   Get-NetAdapter | Format-Table Name, InterfaceDescription
   New-VMSwitch -Name "vSwitch-Office" -NetAdapterName "Ethernet0" -AllowManagementOS $true
   Get-VMSwitch | Format-Table Name, SwitchType
   ipconfig
   ```
   *Ожидаемо:* связь на секунды прервётся, появится `vEthernet (vSwitch-Office)`, IP `10.10.10.12` сохранится (если уехал в APIPA — назначь заново). Это ровно то, что происходит в бою: делай такое только в окно обслуживания.
5. **HV01.** Приватный свич и вложенная ВМ:
   ```powershell
   New-VMSwitch -Name "vSwitch-Private" -SwitchType Private
   New-VM -Name HV01-VM01 -MemoryStartupBytes 2GB -Generation 2 `
     -NewVHDPath D:\VMs\HV01-VM01\disk.vhdx -NewVHDSizeBytes 40GB `
     -SwitchName "vSwitch-Private"
   Set-VMProcessor -VMName HV01-VM01 -Count 2
   Add-VMDvdDrive -VMName HV01-VM01 -Path D:\ISO\WS2022.iso
   Start-VM -Name HV01-VM01; vmconnect.exe localhost HV01-VM01
   ```
   Установи в вложенную ВМ Windows Server 2022 Evaluation (можно Server Core — быстрее). *Ожидаемо:* в `Get-VM` две строки, `HV01-VM01` в состоянии `Running`; внутри гостя сети нет (приватный свич).
6. **HV01.** Разреши «чужие» MAC и переведи гостя во внешнюю сеть:
   ```powershell
   Set-VMNetworkAdapter -VMName HV01-VM01 -MacAddressSpoofing On
   Connect-VMNetworkAdapter -VMName HV01-VM01 -SwitchName "vSwitch-Office"
   ```
   В консоли гостя: `ipconfig /renew`, затем `ping 10.10.10.10`.
   *Ожидаемо:* гость получает адрес из пула DHCP DC01 (10.10.10.100–150) и пингует DC01. **Без** `MacAddressSpoofing On` пакеты гостя будут дропаться — вложенные ВМ шлют кадры со своими MAC, а внешний свич их не знает.
7. **HV01, демонстрация VLAN (аккуратно).**
   ```powershell
   Set-VMNetworkAdapter -VMName HV01-VM01 -VlanId 10
   ```
   В гостя: `ping 10.10.10.10`. *Ожидаемо:* пинг **перестанет** идти — VMnet8/NAT не транкован, тегированный трафик просто некуда деть. Верни `-VlanId 0` и убедись, что пинг восстановился. Вывод: VLAN на vNIC работает только вместе с транком на физике.
8. **HV01.** Сделай чекпоинт и посмотри интеграционные службы:
   ```powershell
   Checkpoint-VM -VMName HV01-VM01 -SnapshotName "before-migration"
   Get-VMSnapshot -VMName HV01-VM01 | Format-Table Name, SnapshotType
   Get-VMIntegrationService -VMName HV01-VM01 | Format-Table Name, Enabled, PrimaryStatusDescription
   ```
   *Ожидаемо:* один чекпоинт; у `Heartbeat`, `Time Synchronization`, `KVP Exchange` статус `OK` (у `VSS` может быть `No contact` — нормально).
9. **HV01 + HV02, подготовка миграции.**
   ```powershell
   # на обоих хостах
   Enable-VMMigration
   Set-VMHost -VirtualMachineMigrationAuthenticationType Kerberos
   ```
   На DC01 в **ADUC** найди объект компьютера **HV02** → Свойства → **Делегирование** → «Доверять этому компьютеру делегирование только указанным службам» → «Только Kerberos» → Добавить → пользователи/компьютеры → **HV01** → службы **`cifs`** и **`Microsoft Virtual System Migration Service`** `[ПРОВЕРИТЬ формулировку служб в вашей локали]`.
   *Ожидаемо:* делегирование сохранено; `Get-VMHost` на обоих показывает `VirtualMachineMigrationEnabled : True`.
10. **WIN11-CL01 → HV01, проверка простоя.** Узнай IP вложенной ВМ (в её консоли `ipconfig`), затем на WIN11-CL01 запусти `ping -t <IP вложенной ВМ>`. На HV01:
    ```powershell
    Compare-VM -Name HV01-VM01 -DestinationHost HV02.corp.vektor.lab | Select -Expand Incompatibilities
    Move-VM -Name HV01-VM01 -DestinationHost HV02.corp.vektor.lab `
            -IncludeStorage -DestinationStoragePath D:\VMs\HV01-VM01
    ```
    *Ожидаемо:* `Incompatibilities` пуст, миграция занимает десятки секунд, пинг даёт максимум 1–2 потерянных пакета, `Get-VM` на HV02 показывает ВМ у себя, на HV01 её нет.
11. **HV02, репликация.**
    ```powershell
    Set-VMReplicationServer -ReplicationEnabled $true -AllowedAuthenticationType Kerberos `
        -ReplicationKerberosAuthenticationPort 80 -DefaultStoragePath D:\Replica
    ```
    В Hyper-V Manager на HV02: **Hyper-V Settings → Replication Configuration → Enable this computer as a Replica server**, авторизация «Allow replication from any authenticated server», хранилище `D:\Replica`. Затем **на HV01**:
    ```powershell
    Enable-VMReplication -VMName HV01-VM01 -ReplicaServerName HV02.corp.vektor.lab `
        -ReplicaServerPort 80 -AuthenticationType Kerberos `
        -CompressionEnabled $true -ReplicationFrequencySec 300
    Get-VMReplication -VMName HV01-VM01
    ```
    *Ожидаемо:* `State : Enabled`, через 5–10 минут `Measure-VMReplication` покажет `Health : Normal` и заполненные `LastReplicationTime`.
12. **HV02, тестовое переключение и журнал.**
    ```powershell
    Start-VMFailover -VMName HV01-VM01 -AsTest
    Get-VM | Where-Object Name -like "*Test*" | Select Name, State
    Stop-VMFailover -VMName HV01-VM01 -TurnOff
    ```
    *Ожидаемо:* появилась ВМ `HV01-VM01 - Test`, репликация при этом не порвалась. Запиши всё в `stand-log.md`.

### Проверка
- [ ] `Get-VMHost` на HV01 и HV02 показывает роль Hyper-V и `VirtualMachineMigrationEnabled : True`.
- [ ] `Get-VMSwitch` на HV01: `vSwitch-Office` (External), `vSwitch-Private` (Private).
- [ ] Вложенная ВМ `HV01-VM01` получает адрес из DHCP-пула DC01 и пингует `10.10.10.10`.
- [ ] `-VlanId 10` ломает связь, `-VlanId 0` возвращает.
- [ ] `Checkpoint-VM` создал чекпоинт, `Get-VMSnapshot` его видит.
- [ ] `Move-VM` прошёл без ошибок, пинг во время миграции прервался не более чем на 1–2 пакета.
- [ ] `Get-VMReplication` = `Enabled`, `Measure-VMReplication` = `Normal`.
- [ ] Тестовый failover создал ВМ `… - Test` и был корректно убран.
- [ ] В `stand-log.md` появилась запись о главе 44.

### Сломай и почини
> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01 / **Что написал:** «Не открывается наш тестовый портал на 10.10.10.1xx (тот, что на HV01-VM01). Браузер пишет "Не удаётся получить доступ к сайту". Утром не работало, вчера всё было ок. У нас из-за этого отчёт не уходит». / **Срочность:** средняя (блокирует отчёт).

**Как воспроизвести поломку.** На хосте, где сейчас живёт ВМ (после шага 10 это HV02):
```powershell
Set-VMNetworkAdapter -VMName HV01-VM01 -MacAddressSpoofing Off
```
Гость остаётся `Running`, но его кадры с «чужими» MAC коммутатор молча дропает.

**Диагностика по шагам.**
1. `Get-VM -Name HV01-VM01` на HV02 — ВМ `Running`, значит не «упала».
2. `Test-NetConnection 10.10.10.1xx -Port 80` с WIN11-CL01 — таймаут, не «отказано» (значит, трафик не доходит, а не «сервис не слушает»).
3. `Resolve-DnsName`/имя — не при чём: обращаемся по IP. Сразу отсекаем DNS.
4. В консоли гостя (`vmconnect`) — `ipconfig`: адрес **169.254.x.x**. Вот и корень: DHCP-запросы не доходят.
5. `Get-VMNetworkAdapter -VMName HV01-VM01 | fl Name, SwitchName, MacAddress, MacAddressSpoofing, DhcpGuard` — видим `MacAddressSpoofing : Off`. Сверяемся со `stand-log.md`/снапшотом: до миграции было `On`.
6. `Get-VMSwitch -Name vSwitch-Office | fl *` и `Get-NetAdapter` на хосте — свич на месте, физика `Up`, значит дело не в свиче.
7. **Причина:** настройка порта потерялась/была изменена при работах; без MAC spoofing вложенные ВМ не могут слать трафик.

**Решение.**
```powershell
Set-VMNetworkAdapter -VMName HV01-VM01 -MacAddressSpoofing On
# в консоли гостя
ipconfig /renew
```
Проверка: `Test-NetConnection 10.10.10.1xx -Port 80` с WIN11-CL01 → `TcpTestSucceeded : True`, в браузере портал открывается.

**Ответ пользователю:** «Иван, добрый день. Причина найдена: у тестовой ВМ на гипервизоре сбилась настройка сетевого адаптера, из-за этого она не получала адрес по DHCP. Настройку вернул, портал открывается. Проверь, пожалуйста, у себя и напиши, если что-то ещё не так. Заявку закрываю, если всё ок».

**Закрытие заявки:** категория «Виртуализация → сеть ВМ», приоритет средний, время решения, корневая причина «изменена настройка MAC spoofing на vNIC после миграции, DHCP-запросы дропались», действие «вернул настройку, перевёл решение на постоянную основу» + запись в `stand-log.md`.

### Что изменилось на стенде
- Появились **временные** ВМ **HV01** (10.10.10.12) и **HV02** (10.10.10.13), Windows Server 2022 Evaluation, введены в домен `corp.vektor.lab`, объекты компьютеров в OU `Серверы`. **После главы удаляются.**
- На обоих хостах установлена роль Hyper-V; на HV02 настроен сервер реплики (`D:\Replica`, Kerberos/HTTP, порт 80).
- На HV01: коммутаторы `vSwitch-Office` (External на `Ethernet0`) и `vSwitch-Private` (Private).
- Вложенная ВМ `HV01-VM01` (2 vCPU, 2 ГБ RAM, диск 40 ГБ), сейчас размещена на **HV02**, `MacAddressSpoofing On`, MAC из пула Hyper-V, IP по DHCP от DC01.
- В AD на объекте компьютера HV02 — constrained delegation на HV01 (cifs, Microsoft Virtual System Migration Service).
- На DC01: включённый `Enable-VMMigration` не требуется (он на хостах), но появились записи компьютеров HV01/HV02 — учитывать при бэкапе AD.

### Откат
Полный откат — снапшот `before-l2-044` в VMware Workstation (правый клик по ВМ → **Snapshot → Snapshot Manager → Restore**). Откатывай **DC01, HV01, HV02** вместе, иначе останутся «висячие» записи компьютеров в AD. Помни: откат ВМ в VMware возвращает её целиком, включая виртуальные диски вложенных гостей. Если откат не нужен — просто **удали** HV01 и HV02 из инвентаря VMware вместе с каталогами дисков и убери объекты компьютеров из AD (`Remove-ADComputer`), а также записи в DNS (`dnsmgmt.msc`).

## 6. Ошибки новичков и безопасность

1. **Создать внешний vSwitch без `-AllowManagementOS $true`** → хост теряет сеть, RDP/Hyper-V Manager отваливаются, чинить приходится через консоль VMware/ILO.
   *Правильно:* сначала мысленно ответить «нужен ли мне доступ к хосту», затем создавать свич; в бою — только в окно обслуживания и с консольным доступом (iDRAC/iLO/КVM).
2. **Ставить Hyper-V на контроллер домена или на файловый сервер «за компанию»** → после перезагрузки DC не поднимается вовремя, DFS/репликация ломаются; Hyper-V на DC официально не рекомендуется.
   *Правильно:* Hyper-V — отдельный хост (или кластер хостов), роли не смешивать.
3. **Считать чекпоинт резервной копией** → диски-дельты растут, производительность падает, а при потере VHDX-цепочки теряется вся ВМ.
   *Правильно:* чекпоинт — на 1–2 дня и только перед изменениями; бэкап — Veeam/Windows Server Backup (глава 49).
4. **Забыть про MAC spoofing во вложенной виртуализации** → гость «в сети», но не получает DHCP, пинги не идут.
   *Правильно:* понимать, что это лабораторный костыль; в бою вложенную виртуализацию применяют осознанно.
5. **Настраивать VLAN на vNIC без транка на физическом порту** → ВМ исчезает из сети, «виноват Hyper-V».
   *Правильно:* сначала согласовать VLAN с сетевиками и проверить trunk/native VLAN на порту.
6. **Включать живую миграцию и ждать, что «просто заработает»** → ошибка «No credentials available» из-за отсутствия constrained delegation.
   *Правильно:* сначала `Compare-VM`, потом делегирование, потом миграция; тестировать на небоевой ВМ.
7. **Считать реплику бэкапом и ни разу не переключаться на неё** → при аварии выясняется, что реплика не поднимается или в ней битые данные.
   *Правильно:* раз в квартал — тестовый failover (`-AsTest`), запись результата в документацию.
8. **Не проверять совместимость CPU перед миграцией** → ВМ падает в BSOD или не стартует на новом хосте.
   *Правильно:* `CompatibilityForMigrationEnabled` и `Compare-VM` перед работами.

**Безопасность.** `MacAddressSpoofing On` в бою даёт ВМ возможность подменить MAC и обойти Port ACL/DHCP guard — включать только там, где реально нужно (виртуализация внутри ВМ, балансировщики). `DhcpGuard` и `RouterGuard` на пользовательских портах — наоборот, обязательно: это защита от «своего DHCP» и подмены шлюза. Не отключай firewall на хосте ради удобства Hyper-V Manager и не давай `adm.student`-подобным учёткам Hyper-V Administrators «на всякий случай»: право мигрировать ВМ = право забрать её данные. Реплику по HTTP (Kerberos) допустимо держать только внутри доверенной сети; через недоверенные сегменты — HTTPS с сертификатом. Пароли стенда (`Lab#2026-vektor`) — только для стенда.

## 7. Вопросы на собеседовании

**1. Чем External vSwitch отличается от Internal и Private, и что происходит с сетью хоста при создании внешнего?**
Ответ: External привязывается к физическому NIC, ВМ получают доступ в реальную сеть; хост при `AllowManagementOS $true` получает виртуальный адаптер `vEthernet`, на который переезжает IP, а физическая карта остаётся без протоколов. Internal — ВМ плюс сам хост (используется для NAT, хост-сервисов). Private — только ВМ между собой, хост не видит трафик. При создании внешнего свича сеть хоста кратковременно рвётся — делать в окно обслуживания и с консольным доступом.
*Что хочет услышать интервьюер:* понимание, что свич — часть гипервизора, а не «настройка Windows», и что создание внешнего свича — риск потери управления хостом.
*Красный флаг:* «внешний — это когда ВМ в интернете»; не знать про `AllowManagementOS` и обрыв сети.

**2. Как работает живая миграция Hyper-V и что нужно, чтобы она заработала?**
Ответ: память ВМ копируется на целевой хост в несколько проходов, «грязные» страницы досылаются, в конце короткая пауза и переключение; TCP 6600, опционально сжатие или SMB, для shared-nothing переносится и диск. Нужны: включённая миграция на обоих хостах, доверие (один домен) и constrained delegation в AD, совместимость CPU, доступ к хранилищу или `-IncludeStorage`, отсутствие pass-through устройств.
*Что хочет услышать интервьюер:* «миграция — это сеть + AD-делегирование + совместимость CPU», и что простой измеряется в потерянных пакетах/секундах.
*Красный флаг:* «миграция — это когда ВМ копируется и выключается»; не знать про делегирование и порт 6600.

**3. Чем реплика отличается от бэкапа и от чекпоинта?**
Ответ: реплика — асинхронная копия на другом хосте для быстрого восстановления сервиса (RPO = частота репликации, минимум 30 с), хранит только актуальное состояние; чекпоинт — точка отката на том же хранилище, зависит от родительского диска; бэкап — независимая копия с историей и возможностью восстановить состояние «неделю назад», обычно с дедупликацией и на отдельном носителе. Ни реплика, ни чекпоинт не защищают от шифровальщика или логической порчи данных так, как бэкап с версиями.
*Что хочет услышать интервьюер:* ясное понимание RPO/RTO и что реплику надо тестировать.
*Красный флаг:* «реплика — это и есть бэкап».

**4. Пользователь говорит, что ВМ не пингуется, но в консоли она запущена и внутри всё работает. С чего начнёте?**
Ответ: сначала разделю «ВМ жива» и «сеть ВМ». Проверю на хосте, на каком свиче vNIC и его состояние (`Get-VMNetworkAdapter`): SwitchName, MacAddress, VlanId, MacAddressSpoofing, DhcpGuard. Затем в консоли гостя — `ipconfig` (APIPA = нет DHCP), шлюз, firewall-профиль. Потом — снаружи: `Test-NetConnection` по IP и порту, ARP на хосте, при миграции — сравню настройки адаптера с эталоном. Только после этого иду в физическую сеть и VLAN.
*Что хочет услышать интервьюер:* порядок «снизу вверх» и что первым делом смотрят конфигурацию порта и IP гостя, а не «перезагрузите ВМ».
*Красный флаг:* «перезагружу хост» без диагностики; прыжок сразу в коммутаторы.

**5. Зачем нужны DHCP guard и Router guard в Hyper-V?**
Ответ: чтобы ВМ не могла отвечать на DHCP-запросы (поднимать свой DHCP и раздавать левые шлюзы) и не могла рассылать ICMP-редиректы/объявлять себя маршрутизатором. Включаются на порту vNIC. Это защита от «внутреннего» нарушения: пользователь поднял виртуалку с DHCP и положил сегмент.
*Что хочет услышать интервьюер:* понимание, что виртуальный коммутатор — точка безопасности, а не просто «проброс сети».
*Красный флаг:* «это не нужно, у нас же виртуалки только у админов».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Hyper-V documentation» (обзор Hyper-V, системные требования, архитектура). Поиск: `Hyper-V documentation site:learn.microsoft.com`.
- **learn.microsoft.com** — «Create a virtual switch for Hyper-V virtual machines»; поиск: `Hyper-V virtual switch external internal private`.
- **learn.microsoft.com** — «Set up hosts for live migration without shared storage» и «Configure constrained delegation»; поиск: `Hyper-V live migration without shared storage constrained delegation`.
- **learn.microsoft.com** — «Set up a Replica Server» / «Hyper-V Replica overview» / «Fail over a replica virtual machine»; поиск: `Hyper-V Replica failover test planned` `[ПРОВЕРИТЬ точное имя статьи]`.
- **learn.microsoft.com** — справочник по командлетам модуля Hyper-V: `Set-VMHost`, `Move-VM`, `Enable-VMReplication`, `Start-VMFailover`; поиск: `HyperV PowerShell cmdlet reference Set-VMHost`.
- **learn.microsoft.com** — «Service overview and network port requirements for Windows» (порты 6600, 80/443, 5985/5986); поиск: `Windows service port requirements Hyper-V`.
- **learn.microsoft.com** — «Nested virtualization on Hyper-V» и документация VMware по «Virtualize Intel VT-x/EPT»; поиск: `Hyper-V nested virtualization mac address spoofing` и `VMware Workstation expose VT-x EPT`.
