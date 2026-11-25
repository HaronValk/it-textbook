# Отказоустойчивая кластеризация (Failover Cluster): Quorum, CSV, роли, сценарии отказа

## 1. Введение (зачем это нужно в реальной работе)

**Тикет в helpdesk «Вектора»:** пользователь `ivanov` (бухгалтерия), ВМ `WIN11-CL01`: «Открываю `\\FS-CL01\ClusterTest` — „Сетевой путь не найден“. Утром работало, сейчас не могу закрыть месяц». Срочность — высокая.

Дальше происходит то, что отличает middle-админа от новичка. Новичок идёт на сервер и перезагружает его. Инженер смотрит на кластер: жив ли кворум, в каком состоянии группа ресурсов, на каком узле она должна работать, что случилось с общим диском и не отвалился ли iSCSI. Отказоустойчивая кластеризация (Failover Cluster, она же WSFC — Windows Server Failover Clustering) — это механизм, который превращает «сервер упал, лежим до утра» в «роль переехала на второй узел за 30 секунд». В «Векторе» так живут файловый сервис, 1С и будущие Hyper-V-хосты.

После этой главы ты должен уметь: объяснить, как считается кворум и почему он решает судьбу всего кластера; выбрать модель кворума под 2 узла, 3 узла и две площадки; осознанно применять Cluster Shared Volumes (CSV) и понимать, когда они не нужны; добавлять роли, настраивать владение и выполнять управляемый failover; диагностировать отказ по цепочке «пользователь → роль → ресурс → диск → iSCSI → служба».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Кластер — это группа серверов (**узлов**, nodes), которые вместе выглядят для клиента как один ресурс с одним именем и одним IP. Компоненты:

- **Cluster Service (ClusSvc)** — служба на каждом узле. Держит базу кластера (реплицируется через RPC), проводит голосование за кворум, мониторит здоровье узлов.
- **RHS (Resource Hosting Subsystem)** — «исполнитель»: отдельный процесс, который запускает и останавливает ресурсы (службу файлового сервера, виртуальную машину Hyper-V, экземпляр SQL). Если ресурс падает, падает RHS, а не весь кластер.
- **Ресурс (resource)** — минимальная единица управления: IP-адрес, сетевое имя, диск, служба. У ресурсов есть **зависимости** (сетевое имя зависит от IP, шара зависит от диска и сетевого имени).
- **Группа ресурсов / роль (role)** — набор ресурсов, которые переезжают вместе. В PowerShell слово `Group` и `Role` — почти синонимы.
- **Свидетел**ь (witness) — отдельный голос, который не хранит данные, а только разрешает спор.
- **CNO и VCO** — Cluster Name Object (`CL01$`) и Virtual Computer Object роли (`FS-CL01$`). Это обычные объекты компьютеров в AD, их создаёт кластер. Отсюда мостик к урокам 9 и 15: права на OU и DNS напрямую влияют на работоспособность кластера.

### Кворум

Кворум — правило «сколько голосов нужно, чтобы кластер имел право работать». Формула простая: побеждает сторона, у которой **строго больше половины** голосов.

| Конфигурация | Голоса | Нужно для кворума | Переживает |
|---|---|---|---|
| 2 узла, без свидетeля (NodeMajority) | 2 | 2 | ничего (без dynamic quorum) |
| 2 узла + disk witness | 3 | 2 | отказ 1 узла **или** свидетeля |
| 2 узла + file share witness | 3 | 2 | то же |
| 4 узла + witness | 5 | 3 | отказ 2 голосов |

Типы кворума: `NodeMajority`, `NodeAndDiskMajority` (диск-свидетель), `NodeAndFileShareMajority` (SMB-шара), `NoMajority` (легаси DiskOnly — в продакшене не использовать). Cloud Witness в Azure Storage в РФ официально недоступен, поэтому в нашей реальности выбор — диск или файловая шара `[ПРОВЕРИТЬ актуальность Azure в РФ на 2026]`.

**Dynamic quorum** (по умолчанию включён с 2012 R2): когда узел уходит, кластер временно понижает общий вес голосов, чтобы оставшиеся сохранили большинство. Именно поэтому 2-узловой кластер без свидетeля обычно выживает при отказе одного узла — но только до перезапуска, и это не повод так проектировать. `NodeWeight = 0` используется для узлов «голосующего свидетеля» на удалённой площадке, чтобы сайты не перетягивали кворум друг у друга.

**Force quorum** (`Start-ClusterNode -ForceQuorum`) — аварийный запуск кластера без большинства. Опасен: если второй узел на самом деле жив, оба узла начнут писать в один общий LUN — split brain и порча данных.

### Как происходит failover

1. Cluster Service теряет heartbeat узла-владельца (таймаут health check).
2. Группа помечается к переезду, выбирается возможный владелец (Possible owners → Preferred owners).
3. На новом узле RHS поднимает ресурсы **в порядке зависимостей**: диск (с SCSI-3 persistent reservations) → IP → сетевое имя → служба.
4. Клиент видит разрыв 10–60 секунд (зависит от роли), затем reconnect.

Сетевые порты: RPC TCP 135 + динамический диапазон (управление кластером, `Get-Cluster*`, WMI), SMB TCP 445 (доступ к файловой шарe-свидетелю и redirected I/O для CSV), iSCSI TCP 3260. Старый UDP 3343 в актуальных версиях не используется `[ПРОВЕРИТЬ]`.

### CSV — Cluster Shared Volume

```
   CLN01 (coordinator)                       CLN02
   ┌─────────────────┐                  ┌─────────────────┐
   │ NTFS-владелец   │◄── SMB (445) ────│ redirected I/O  │
   │ тома + lock     │                  │                 │
   └────────┬────────┘                  └────────┬────────┘
            │        direct I/O (block)          │
            └──────────►  LUN (iSCSI)  ◄─────────┘
                  C:\ClusterStorage\Volume1
```

CSV — это том, смонтированный на **всех** узлах одновременно по одному пути `C:\ClusterStorage\VolumeN`. Один узел назначен **координатором**: он владеет NTFS и блокировками. Остальные либо читают/пишут напрямую блоками, если файл «их», либо идут через **redirected I/O** по SMB к координатору. Смысл: нет буквы диска, привязанной к владельцу, и при переезде виртуальной машины Hyper-V не нужно размонтировать том. CSV включается вместе с ролью Hyper-V `[ПРОВЕРИТЬ: требуется ли роль Hyper-V для создания CSV в 2022/2025]` и нужен прежде всего для Hyper-V и Scale-Out File Server. Для классического файлового сервера (наш случай) CSV не нужен — нужен обычный кластерный диск.

## 3. Аналогия из жизни

Дежурная бригада хирургов. Два врача (узлы) сидят в одной операционной (общий диск). Пациенты — роли: каждый в свой момент оперируется одним врачом, остальные ждут. Журнал операций (cluster database) реплицируется между врачами. Кворум — это правило больницы: «операция считается законной, если её подтвердило большинство дежурных». Поэтому при двух врачах приглашают нотариуса (свидетеля): если один врач ушёл на вызов, второй плюс нотариус всё ещё большинство, и больница работает. Нотариус ничего не оперирует — он только подписывает. Force quorum — это когда оставшийся врач говорит: «нотариуса нет, но я сам себе подпишу», и продолжает резать; если второй врач в этот момент вернётся и тоже начнёт резать того же пациента — катастрофа. **Где аналогия ломается:** в кластере нет главного врача, который принимает решение за всех — консенсус распределённый, и «подпись» — это математика голосов, а не авторитет.

## 4. Практика и команды

Все команды — PowerShell с модуля **FailoverClusters** (на узле кластера, от администратора домена).

**1. Кто в кластере и жив ли он**
```powershell
Get-ClusterNode
```
```text
Name   State NodeWeight
----   ----- ----------
CLN01  Up             1
CLN02  Up             1
```
`State`: `Up` — работает, `Paused` — обслуживание (роли уехали), `Down` — узел потерян. `NodeWeight = 0` — узел не голосует.

**2. Роли и их владельцы**
```powershell
Get-ClusterGroup | Format-Table Name, OwnerNode, State -AutoSize
```
```text
Name              OwnerNode State
----              --------- -----
Cluster Group     CLN01     Online
Available Storage CLN02     Online
FS-CL01           CLN01     Online
```
`Cluster Group` — имя кластера и его IP; `Available Storage` — свободные кластерные диски. Смотри, что `Failed` или `Offline`.

**3. Ресурсы внутри роли**
```powershell
Get-ClusterResource | Where-Object OwnerGroup -eq 'FS-CL01' |
    Format-Table Name, ResourceType, State -AutoSize
```
```text
Name                    ResourceType State
----                    ------------ -----
Cluster Disk 1          Physical Disk Online
IP Address 10.10.10.91  IP Address   Online
Network Name (FS-CL01)  Network Name Online
File Server (FS-CL01)   File Server  Online
```
Порядок строк — примерно порядок зависимостей. Диск `Failed` → выше по цепочке всё тоже ляжет.

**4. Кворум: посмотреть и изменить**
```powershell
Get-ClusterQuorum
Set-ClusterQuorum -NodeAndDiskMajority "Cluster Disk 2"     # диск-свидетель
Set-ClusterQuorum -NodeAndFileShareMajority "\\FS01\Witness" # шара-свидетель
Set-ClusterQuorum -NodeMajority                              # убрать свидетеля
```
```text
Cluster QuorumResource QuorumType
------- -------------- ----------
CL01    Witness Disk   NodeAndDiskMajority
```
После смены модели всегда проверяй `Get-ClusterQuorum` ещё раз — команда молча применяет не то, если имя диска указано неверно.

**5. Проверка перед созданием кластера**
```powershell
Test-Cluster -Node CLN01,CLN02 -Include "Storage","Network","System Configuration" -Force
```
Отчёт сохраняется в `C:\Windows\Cluster\Reports\` (mht) `[ПРОВЕРИТЬ путь]`. Красное — блокер (нет общего LUN, разные версии, одинаковые IP), жёлтое — предупреждение.

**6. Создание кластера**
```powershell
New-Cluster -Name CL01 -Node CLN01,CLN02 -StaticAddress 10.10.10.90 -NoStorage
```
`-Name` — имя кластера и CNO; `-StaticAddress` — IP кластера (нужен для IPv4-only стенда, иначе DHCP); `-NoStorage` — не забирать диски автоматически, добавим руками: `Add-ClusterDisk`. Кластер сразу создаёт объект `CL01$` в AD и A-запись в DNS.

**7. Роль файлового сервера**
```powershell
Add-ClusterFileServerRole -Storage "Cluster Disk 1" -Name FS-CL01 -StaticAddress 10.10.10.91
```
`-Storage` — кластерный диск, `-Name` — VCO (создастся объект `FS-CL01$`), `-StaticAddress` — «плавающий» IP роли.

**8. Управляемый переезд**
```powershell
Move-ClusterGroup -Name FS-CL01 -Node CLN02
Move-ClusterGroup -Name FS-CL01 -Wait 0   # асинхронно, просто инициировать
```
Так делают плановое обслуживание: сначала руками перевозят роли, потом гасят узел.

**9. Режим обслуживания узла**
```powershell
Suspend-ClusterNode -Name CLN01 -Drain
Resume-ClusterNode -Name CLN01
```
`-Drain` выселяет все роли с узла и ставит его в `Paused` — правильный способ патчить узел.

**10. Остановка/запуск узла и force quorum**
```powershell
Stop-ClusterNode -Name CLN01
Start-ClusterNode -Name CLN02 -ForceQuorum
```
`-ForceQuorum` — только когда второго узла точно нет (см. «Безопасность»).

**11. Лог кластера — главный инструмент диагностики**
```powershell
Get-ClusterLog -TimeSpan 15 -Destination C:\Temp -UseLocalTime
```
В `C:\Temp` появится `Cluster.log` со всех узлов. Ищи `ERR`, `WARN`, `RHS`, `Quorum`, `Resgroup`.

**12. CSV**
```powershell
Get-ClusterSharedVolume
Add-ClusterSharedVolume -Name "Cluster Disk 3"
Get-ClusterSharedVolume | Format-List Name, State, SharedVolumeInfo
```
`State: Online`; путь вида `C:\ClusterStorage\Volume1`. Команда `Get-ClusterSharedVolumeState` в части версий может отсутствовать `[ПРОВЕРИТЬ]`.

**13. Кластерное имя не резолвится/не пингуется**
```powershell
Update-ClusterNetworkNameResource -Name "Network Name (FS-CL01)"
```
Перерегистрирует DNS-запись VCO. Классика после смены IP или прав на DNS-зону.

**14. GUI (если проще глазами):** `failoverclusters.msc` → Roles / Nodes / Storage / Networks; кворум — правый клик по кластеру → **More Actions → Configure Cluster Quorum Settings**; шара в роли — правая панель выбранной роли → **Add a share** `[ПРОВЕРИТЬ название пункта]`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Для этой лабы стенд L2 расширяется тремя ВМ, которых раньше не было (описываю явно):

| Имя | ОС | Адрес | Роль |
|---|---|---|---|
| CLN01 | Windows Server 2022 Evaluation | 10.10.10.80 | узел кластера 1 |
| CLN02 | Windows Server 2022 Evaluation | 10.10.10.81 | узел кластера 2 |
| SAN01 | Windows Server 2022 Evaluation | 10.10.10.85 | iSCSI Target Server (общий LUN) |

Объекты кластера: кластер **CL01** — 10.10.10.90; роль **FS-CL01** — 10.10.10.91, шара `\\FS-CL01\ClusterTest`.

### Предусловия
- DC01 (AD DS, DNS, DHCP) работает, ты вошёл как `VEKTOR\adm.student`.
- Все три новые ВМ — в домене `corp.vektor.lab`, статические IP на VMnet8, шлюз 10.10.10.2, DNS 10.10.10.10.
- PVE01 по программе трека есть, но **эту лабу проще ставить прямо в VMware Workstation**: три Windows-ВМ внутри вложенного Proxmox на домашнем ПК съедят 8–10 ГБ RAM сверх уже работающих DC01/FS01. Если хочешь именно в PVE01 — заводи там ВМ с CPU type `host` и включённым вложенным виртуализированным VT-x в настройках самой PVE01. Логика лабы от этого не меняется.
- Свободно ~120 ГБ на хосте, на каждую ВМ — 2 vCPU, 4 ГБ RAM, диск 40 ГБ; на SAN01 добавь второй диск 20 ГБ под VHDX-файлы.

### Снапшот
На хосте: для DC01, FS01, WIN11-CL01, CLN01, CLN02, SAN01 — `VM → Snapshot → Take Snapshot`, имя **`before-sysadmin-014`**. Если ВМ живут в PVE01 — снапшот всей PVE01 через Web UI (`Datacenter → PVE01 → Snapshots → Take snapshot`) с тем же именем. Фиксируй снапшоты до установки ролей.

### Шаги

1. **CLN01/CLN02: подготовка.** Переименуй, задай статические IP (.80/.81), введи в домен, перезагрузись. Ожидаемо: `whoami` → `vektor\adm.student`, `nltest /dsgetdc:corp.vektor.lab` отвечает.
2. **CLN01/CLN02: компоненты.** `Install-WindowsFeature Failover-Clustering -IncludeManagementTools`, затем `Start-Service msiscsi; Set-Service msiscsi -StartupType Automatic`. Ожидаемо: `Success: True`, `Get-Service msiscsi` → `Running`.
3. **SAN01: iSCSI Target Server.** `Install-WindowsFeature FS-iSCSITarget-Server -IncludeManagementTools`. Затем через Server Manager → **File and Storage Services → iSCSI** создай два виртуальных диска: `cl01-data.vhdx` 5 ГБ и `cl01-witness.vhdx` 1 ГБ, и один target `cl01-target`, в который включи оба диска. Для лабы выбери «Allow any initiator» (в продакшене — только по IQN конкретных узлов, скажу ниже).
4. **CLN01: подключение LUN.** `Get-IscsiTarget | Connect-IscsiTarget -IsPersistent $true` (или `iscsicpl` → Discovery → Add 10.10.10.85). В `diskmgmt.msc` диск 5 ГБ → **Online → Initialize → New Simple Volume → NTFS, метка CLUSDATA**. Диск 1 ГБ **не трогай** — свидетель остаётся неразмеченным. Ожидаемо: `E:` с меткой CLUSDATA.
5. **CLN02: подключение без захвата.** Те же действия, но диск оставь **Offline** (правый клик → Offline). Ожидаемо: `Get-Disk` показывает оба диска, `OperationalStatus: Offline`. Правило: один LUN в один момент online только на одном узле.
6. **Валидация.** На CLN01: `Test-Cluster -Node CLN01,CLN02 -Include "Storage","Network" -Force`. Ожидаемо: отчёт открывается, критичных ошибок нет; предупреждения про общий диск — норма на этом этапе.
7. **Создание кластера.** `New-Cluster -Name CL01 -Node CLN01,CLN02 -StaticAddress 10.10.10.90`. Ожидаемо: `Get-ClusterNode` показывает два `Up`, `Resolve-DnsName CL01` → 10.10.10.90. Перемести объекты `CL01$` в OU `Компьютеры`.
8. **Кворум.** `Get-ClusterDisk` (диски должны быть `Cluster Disk 1` и `Cluster Disk 2`), затем `Set-ClusterQuorum -NodeAndDiskMajority "Cluster Disk 2"`. Ожидаемо: `Get-ClusterQuorum` → `NodeAndDiskMajority`, диск 2 исчез из `Available Storage`.
9. **Роль и шара.** `Add-ClusterFileServerRole -Storage "Cluster Disk 1" -Name FS-CL01 -StaticAddress 10.10.10.91`. На узле-владельце создай папку на диске (`E:\ClusterTest`) и шару: FCM → Roles → `FS-CL01` → **Add a share** (или `New-SmbShare -Name ClusterTest -Path E:\ClusterTest -FullAccess "VEKTOR\GG-IT","VEKTOR\adm.student"` на владельце `[ПРОВЕРИТЬ, что File Server-ресурс подхватывает шару автоматически]`). Ожидаемо: с `WIN11-CL01` под `VEKTOR\ivanov` открывается `\\FS-CL01\ClusterTest` (ivanov добавь в `GG-IT` на время лабы или дай права `GG-Buh`).
10. **Плановый failover.** Засеки время, выполни `Move-ClusterGroup -Name FS-CL01 -Node CLN02`, проверь `Get-ClusterGroup FS-CL01` → `OwnerNode = CLN02`, шара снова открывается. Ожидаемо: разрыв доступа 10–30 секунд.
11. **Обслуживание.** `Suspend-ClusterNode -Name CLN02 -Drain` → роли уезжают на CLN01, узел `Paused`. Затем `Resume-ClusterNode -Name CLN02`.
12. **Эксперимент с кворумом (учебный, ломает кластер намеренно).** `Set-ClusterQuorum -NodeMajority`, `(Get-Cluster).DynamicQuorum = 0`, затем `Stop-ClusterNode -Name CLN01`. Ожидаемо: CLN02 теряет кворум, `Get-ClusterGroup` на CLN02 отдаёт ошибку/`Cluster Group` offline. Восстановление: `Start-ClusterNode -Name CLN02 -ForceQuorum`, вернуть `DynamicQuorum = 1` и `Set-ClusterQuorum -NodeAndDiskMajority "Cluster Disk 2"`, поднять CLN01.
13. **Журнал.** Запиши в `stand-log.md`: новые ВМ, IP, имена кластера и роли, модель кворума.

### Проверка
- [ ] `Get-ClusterNode` → оба узла `Up`, `NodeWeight = 1`.
- [ ] `Get-ClusterQuorum` → `NodeAndDiskMajority`, ресурс `Witness Disk`.
- [ ] `Get-ClusterGroup` → `FS-CL01` в состоянии `Online`, владелец — ожидаемый узел.
- [ ] `\\FS-CL01\ClusterTest` открывается с `WIN11-CL01` под `ivanov`.
- [ ] `Move-ClusterGroup` перевозит роль, шара возвращается без перезагрузки клиента.
- [ ] `Suspend-ClusterNode -Drain` → узел `Paused`, роли на втором узле.
- [ ] `Get-ClusterLog -TimeSpan 10` создаёт файлы логов без ошибок чтения.
- [ ] В `stand-log.md` появилась запись про CL01/FS-CL01.

### Сломай и почини

> **Пользователь:** `ivanov` **ВМ:** `WIN11-CL01` **Что написал:** «`\\FS-CL01\ClusterTest` не открывается — „Сетевой путь не найден“. Утром работало, сейчас не могу закрыть месяц». **Срочность:** высокая.

**Как воспроизвести.** На SAN01: `Get-Service *iscsi*` (уточни имя службы), затем `Stop-Service WinTarget -Force` `[ПРОВЕРИТЬ имя службы]` — имитируем падение iSCSI-target. Оба LUN исчезают у обоих узлов: диск-свидетель и диск роли.

**Диагностика по порядку:**
1. С `WIN11-CL01`: `Test-NetConnection FS-CL01 -Port 445` — падает уже здесь, значит проблема не в правах, а в сервисе.
2. На CLN01: `Get-ClusterGroup` → `FS-CL01` в `Failed`. Кластерный сервис при этом жив (`Cluster Group` → `Online`) — то есть кворум есть.
3. `Get-ClusterResource | Where-Object State -ne 'Online'` → `Cluster Disk 1` в `Failed`.
4. `Get-ClusterLog -TimeSpan 15 -Destination C:\Temp` → ищи `Lost connection to disk`, `LUN reset`, `Resgroup`.
5. `Get-Disk` на обоих узлах → диска `CLUSDATA` нет. Значит уровень ниже кластера.
6. `Get-IscsiSession` на узлах → сессии в состоянии `Disconnected`/`Lost`.
7. С CLN01: `Test-NetConnection 10.10.10.85 -Port 3260` → порт закрыт.
8. На SAN01: `Get-Service WinTarget` → `Stopped`.

**Решение.** `Start-Service WinTarget` на SAN01 → диски возвращаются. Если роль не поднялась сама: `Start-ClusterResource -Name "Cluster Disk 1"`, затем `Move-ClusterGroup -Name FS-CL01 -Node CLN01` (или `Start-ClusterGroup`). Проверить: `Get-ClusterGroup` → `Online`, с клиента шара открывается.

**Ответ пользователю:** «Иван Петрович, отказ был на стороне дискового хранилища, кластер удержал остальные сервисы. Сейчас доступ восстановлен — закройте и откройте проводник, шара `ClusterTest` на месте. Если увидите ошибку повторно — сразу напишите, я буду на связи до конца дня».

**Закрытие заявки:** категория «инфраструктура / отказ хранилища», в решении — «перезапуск службы iSCSI Target на SAN01, восстановление ресурсов кластера», в рекомендациях — мониторинг службы (это задел под урок 21 и Zabbix с MON01).

### Что изменилось на стенде
- Добавлены ВМ: **CLN01** (10.10.10.80), **CLN02** (10.10.10.81), **SAN01** (10.10.10.85).
- Роли: на CLN01/CLN02 — Failover Clustering + iSCSI Initiator; на SAN01 — iSCSI Target Server с target `cl01-target` и двумя VHDX.
- Кластер **CL01** (10.10.10.90), кворум `NodeAndDiskMajority`, диск-свидетель `Cluster Disk 2`.
- Роль **FS-CL01** (10.10.10.91), шара `\\FS-CL01\ClusterTest` (права на `GG-IT` и/или `GG-Buh`).
- В AD: объекты `CL01$` и `FS-CL01$` перенесены в OU `Компьютеры`.
- Изменения в DC01: A-записи `CL01`, `FS-CL01` в зоне `corp.vektor.lab`.

### Откат
Если что-то пошло не так: VMware Workstation → `VM → Snapshot Manager` → выбрать `before-sysadmin-014` → **Revert** на CLN01, CLN02, SAN01 (и на DC01, если правила объекты в AD). В PVE01 — `Snapshots → Rollback`. Если решил оставить кластер, но не хочешь ломать домен: на каждом узле `Clear-ClusterNode` (после `Stop-ClusterNode`), затем вручную удалить в ADUC объекты `CL01$`, `FS-CL01$` и A-записи в DNS. Журнал `stand-log.md` дополни строкой об откате — это тоже часть истории стенда.

## 6. Ошибки новичков и безопасность

1. **«Кластер = бэкап».** Кластер защищает от отказа железа, но не от `rm`/`del`, шифровальщика или порчи данных. → Нужны отдельные резервные копии и тесты восстановления (урок 20).
2. **2 узла без свидетeля и без dynamic quorum.** → Любой отказ = потеря кворума, роль не поднимается вообще. → Всегда добавляй disk witness или file share witness.
3. **Force quorum «на всякий случай» при живом втором узле.** → Split brain, два узла пишут в один LUN, повреждение NTFS. → Сначала убедись, что второй узел физически выключен/изолирован.
4. **Один LUN, подключённый online на двух узлах вручную.** → Повреждение файловой системы. → Перед созданием кластера диск online только на одном узле; дальше дисками управляет только кластер.
5. **Ждут от CSV буквы диска.** → «Диск не виден» — на самом деле он в `C:\ClusterStorage\VolumeN`. → CSV — про Hyper-V и SOFS, для файловой роли используй кластерный диск.
6. **Кластер собрали и не проверили failover.** → При реальном отказе выясняется, что шара не поднимается из-за прав или зависимостей. → Плановый `Move-ClusterGroup` и `Suspend -Drain` — в регламент, раз в квартал.
7. **Сломали CNO/VCO в AD или DNS.** → Кластер не может обновить свою запись, имя роли не резолвится, клиенты «не видят сервер». → `Update-ClusterNetworkNameResource`, не удалять объекты руками без понимания.
8. **Ждут, что кластер «полечит» 1С или БД.** → Файловая шара с базой 1С ≠ кластеризация самой 1С; нужны средства уровня приложения. → Уточняй у вендора поддерживаемую схему HA.

### Безопасность
- iSCSI «Allow any initiator» без CHAP — это открытый LUN для любого в сети. В бою — только конкретные IQN + CHAP или отдельный VLAN хранения.
- Права на шару-свидетель (`\\FS01\Witness`) дают возможность влиять на кворум (DoS всего кластера). Выдавай только CNO (`CL01$`) и админам.
- Администратор узла кластера фактически управляет всеми ролями — это уровень Tier 0/1 (мостик к уроку 18).
- Учебный пароль `Lab#2026-vektor` и «Allow any initiator» — только стенд. В боевой среде так нельзя: пароли уникальные, доступ по принципу минимальных привилегий, все изменения через change management.

## 7. Вопросы на собеседовании

**1. Что такое кворум и какую модель выберешь для двух узлов? Для двух площадок?**
Ответ: кворум — механизм большинства голосов, определяющий, имеет ли кластер право работать. Для двух узлов — `NodeAndDiskMajority` (диск-свидетель на третьем, независимом хранилище) или file share witness; для двух площадок — `NodeAndFileShareMajority` со свидетелем на третьей площадке, плюс `NodeWeight = 0` для узлов резервного сайта и `LowerQuorumPriorityNodeID`, чтобы основной сайт не терял кворум.
*Интервьюер хочет услышать:* понимание математики голосов и того, что свидетель не должен жить на том же хранилище, что данные.
*Красный флаг:* «двум узлам кворум не нужен» или «поставлю свидетеля на одном из узлов».

**2. Чем CSV отличается от обычного кластерного диска?**
Ответ: кластерный диск в каждый момент смонтирован с буквой только на узле-владельце, при переезде ресурса происходит unmount/mount. CSV виден на всех узлах по единому пути `C:\ClusterStorage\VolumeN`, один узел — координатор, остальные работают через direct I/O блоками или redirected I/O по SMB. CSV убирает операцию размонтирования при миграции ВМ и позволяет нескольким узлам одновременно обращаться к тому.
*Интервьюер хочет услышать:* coordinator node, direct/redirected I/O, области применения (Hyper-V, SOFS).
*Красный флаг:* «CSV — это просто шара» или «CSV нужен любому кластеру».

**3. Что делает dynamic quorum и когда он не спасёт?**
Ответ: при потере узла кластер динамически уменьшает общий вес голосов, чтобы оставшиеся сохранили большинство; при возврате узла веса восстанавливаются. Не спасёт при потере диска-свидетеля в конфигурации, где без него нет большинства, при `NodeWeight = 0` у нужных узлов, при отключённом dynamic quorum и при разделении сети на равные части (split-brain).
*Интервьюер хочет услышать:* понимание, что dynamic quorum — компенсация, а не замена правильному дизайну кворума.
*Красный флаг:* «dynamic quorum решает все проблемы, свидетель не нужен».

**4. Роль не поднялась после отказа узла. Твой порядок действий?**
Ответ: смотреть `Get-ClusterGroup` и `Get-ClusterResource` — что именно `Failed`; проверить зависимости снизу вверх (диск → IP → имя → служба); `Get-ClusterLog` за период отказа; проверить доступность LUN (`Get-Disk`, `Get-IscsiSession`, порт 3260); проверить, что IP роли не конфликтует и имя резолвится (`Update-ClusterNetworkNameResource`); проверить Possible/Preferred owners и права VCO; при необходимости — ручной `Move-ClusterGroup` с `-Wait`.
*Интервьюер хочет услышать:* системную цепочку, а не «перезагрузил оба узла».
*Красный флаг:* «сразу сделал force quorum» или «перезагрузил всё».

**5. Кластер — это решение HA для 1С и SQL?**
Ответ: кластер даёт отказоустойчивость на уровне ресурса (диск, IP, служба) — он поднимет службу на другом узле. Но корректность данных и сессий обеспечивает приложение: для SQL это Always On/FCI с поддержкой со стороны вендора, для 1С — кластер серверов 1С, а не Windows-кластер поверх файловой шары. Windows-кластер — фундамент, но не замена HA уровня приложения; плюс он не защищает от логических ошибок и не даёт балансировки нагрузки.
*Интервьюер хочет услышать:* различие failover и HA приложения, знание, что «кластеризовать шару с базой» — плохая идея.
*Красный флаг:* «поставлю 1С на кластерный диск — и всё будет отказоустойчиво».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Failover Clustering» (Windows Server): обзор WSFC, требования, поддерживаемые роли. Поиск: `Failover Clustering overview Windows Server site:learn.microsoft.com`.
- **learn.microsoft.com** — «Cluster quorum» / «Configure cluster quorum» / «Dynamic quorum». Поиск: `cluster quorum witness dynamic quorum site:learn.microsoft.com`.
- **learn.microsoft.com** — «Cluster Shared Volumes overview», «Using Cluster Shared Volumes in a Failover Cluster». Поиск: `Cluster Shared Volumes overview site:learn.microsoft.com`.
- **learn.microsoft.com** — справочник PowerShell-модуля FailoverClusters (`New-Cluster`, `Set-ClusterQuorum`, `Add-ClusterFileServerRole`, `Get-ClusterLog`). Поиск: `FailoverClusters PowerShell module New-Cluster site:learn.microsoft.com`. Локально: `Get-Help New-Cluster -Full`.
- **learn.microsoft.com** — «iSCSI Target Server overview» (роль `FS-iSCSITarget-Server`). Поиск: `iSCSI Target Server overview site:learn.microsoft.com`.
- **learn.microsoft.com** — «Failover Clustering technical reference» / порты и сетевые требования кластера. Поиск: `failover cluster network ports requirements site:learn.microsoft.com`. `[ПРОВЕРИТЬ актуальный список портов для 2022/2025]`
