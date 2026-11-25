# Файловый сервер: SMB 3, DFS Namespaces/Replication, FSRM, квоты, теневые копии, права доступа

## 1. Введение (зачем это нужно в реальной работе)

**Заявка #4471.** Пользователь: Иван Иванов (`ivanov`, бухгалтерия). ВМ: WIN11-CL01. Что написал: «Доброе утро! Не могу сохранить накладную в `\\FS01\Buh`: сначала выскочило "Отказано в доступе", потом "Файл занят пользователем petrova". И ещё: вчерашняя версия документа, которую я правил вечером, куда-то пропала — можно восстановить?» Срочность: высокая (закрытие месяца).

Вот это — типичный день L2 в поддержке файлового сервера. Здесь сразу три разные технологии: права доступа (share + NTFS), открытые файлы (SMB-сессии) и теневые копии (VSS). Добавьте к этому DFS, когда папок и серверов становится больше одного, FSRM, когда надо ограничить «складирование» личных фото на сервере, и квоты, когда один отдел выжирает весь том.

Файловый сервер — самая «живая» роль в инфраструктуре: через него люди работают каждый день, и почти любая проблема воспринимается как «сервер сломался», даже если это один пользователь в одной папке. После этой главы вы должны уметь: считать итоговые права (share ∩ NTFS), находить и снимать блокировки файлов, настраивать FSRM-квоты и экраны файлов, включать теневые копии и восстанавливать из них данные, собрать DFS Namespace с несколькими целями и настроить репликацию DFSR, а также быстро отвечать на вопрос «почему у неё не открывается, а у меня открывается».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**SMB** (Server Message Block) — прикладной протокол доступа к файлам и принтерам. Работает поверх TCP **445** (старый NetBIOS-транспорт 139/TCP, 137–138/UDP — это уже легенда, но в диагностике ещё всплывает). Клиент аутентифицируется по Kerberos (в домене) или NTLM (по IP-адресу, в рабочей группе), после чего сервер проверяет права на каждом запросе.

Диалекты: SMB 1.0 (CIFS) — выключен и не нужен; SMB 2.0/2.1 (Vista/7, 2008) — без шифрования; **SMB 3.0** (2012/8) — шифрование, Multichannel (несколько сетевых адаптеров в одну сессию), SMB Direct (RDMA), прозрачный failover; **3.0.2** (2012 R2) — возможность отключать шифрование на отдельных шарах; **3.1.1** (2016/10/Server 2022) — шифрование AES-128-GCM, целостность предварительной аутентификации (pre-auth integrity), directory leasing. SMB over QUIC (UDP 443) есть в отдельных редакциях, на стенде не нужен `[ПРОВЕРИТЬ по редакции]`.

**Два слоя прав.** Разрешение на общий ресурс (share permission: Read / Change / Full) и разрешение NTFS (ACL на папке/файле). Итоговое право = **самое строгое из двух**, при этом внутри каждого слоя действует правило «накопление разрешений, Deny побеждает всё». Поэтому «я дал Everyone Full на шару, а он всё равно не может» — это норма: NTFS не пускает.

**DFS Namespaces (DFS-N)** — это «ярлык поверх серверов»: пользователь открывает `\\corp.vektor.lab\Shares\Buh`, а служба DFS на контроллере домена выдаёт referral на реальный `\\FS01\Buh`. Плюс: можно переехать на другой сервер, не переучивая 50 человек, и объединить папки с разных серверов в одно дерево. **DFS Replication (DFSR)** — отдельная служба, которая синхронизирует содержимое папок между серверами (мультимастер, с RDC — сжатием изменённых блоков). DFS-N — только навигация, DFSR — только копирование данных; друг без друга они работают.

**FSRM** (File Server Resource Manager) — надстройка над файловой системой: квоты (мягкие/жёсткие, на папку, с шаблонами и письмами), экраны файлов (file screening — блокировка mp3/avi/exe), отчёты по хранилищу, классификация файлов. Отличается от «квот NTFS» (`fsutil quota`), которые считаются на том и на пользователя, а не на папку.

**Теневые копии (VSS)** — служба Volume Shadow Copy делает copy-on-write снимок тома; клиент видит его как вкладку «Предыдущие версии». Снимки лежат **на том же томе** в области shadow storage (по умолчанию ~10% тома).

```text
Клиент WIN11-CL01
   │  \\corp.vektor.lab\Shares\Buh   (DFS-N: referral)
   ▼
DC01 (DFS Namespace)  ──referral──►  FS01 (10.10.10.20)
                                       ├─ SMB 3.1.1, TCP 445
                                       ├─ Share ACL  (Read/Change/Full)
                                       ├─ NTFS ACL   (OI)(CI)M для GG-Buh
                                       ├─ FSRM: квота 5 ГБ на D:\Shares\HR
                                       └─ VSS-снимки тома D: (10%)
DC02 (10.10.10.11)  ◄── DFSR ──────►  FS01 (реплика тестовой папки)
```

## 3. Аналогия из жизни

Файловый сервер — это офисное здание с охраной. **Share-разрешение** — пропуск на этаж: пустят или нет. **NTFS** — замки на конкретных кабинетах внутри этажа: даже с пропуском на этаж в бухгалтерию вы не войдёте, если ваш ключ не подходит. **Deny** — это не «нет ключа», а приказ охраны «этого человека не пускать никуда», он перебивает любой пропуск. **DFS Namespace** — стойка навигации в холле: вы идёте в «Бухгалтерию», а не в «корпус Б, 3 этаж, комната 312», и если бухгалтерию переехали в другой корпус, вы об этом не узнаете. **DFSR** — курьер, который возит копии документов между двумя зданиями, причём возит только изменённые страницы, но не умеет ждать, пока вы допишете документ. **Теневые копии** — это фоторафии комнаты раз в день: если кто-то разбил вазу, по фото видно, как было, но восстановить можно только то, что попало в кадр, и фотоальбом хранится в той же комнате. Где аналогия ломается: охрана в здании одна, а SMB-сессий,share-прав и наследования ACL — три независимых механизма, и они конфликтуют между собой гораздо изощрённее.

## 4. Практика и команды

Все команды — PowerShell на FS01 от `VEKTOR\adm.student` (сессия с повышенными правами). Клиентские — на WIN11-CL01.

**1. Состояние SMB-сервера и диалекты**
```powershell
Get-SmbServerConfiguration | Select EnableSMB1Protocol,EnableSMB2Protocol,RequireSecuritySignature,EnableSecuritySignature,EncryptData
Set-SmbServerConfiguration -EnableSMB1Protocol $false -Confirm:$false
Remove-WindowsFeature FS-SMB1     # если компонент SMB1 вообще установлен
```
`EnableSMB1Protocol: False` — хорошо. `RequireSecuritySignature` на файловом сервере в 2022 по умолчанию `False` (на контроллерах домена — обязательна); в новых версиях политика меняется `[ПРОВЕРИТЬ]`. `EncryptData: False` — норма, шифрование включают точечно на чувствительных шарах.

**2. Список шар и создание с явными правами**
```powershell
Get-SmbShare | Format-Table Name,Path,Description
New-SmbShare -Name "Buh" -Path "D:\Shares\Buh" -FullAccess "VEKTOR\adm.student" -ChangeAccess "VEKTOR\GG-Buh"
Get-SmbShareAccess -Name "Buh"
```
В выводе `Get-SmbShareAccess` смотрим `AccountName` и `AccessControlType`. Штатная модель: на шаре — `Change` для рабочей группы и `Full` для админов, всё остальное режет NTFS.

**3. Тонкая настройка шары: ABE (скрытие чужих папок) и шифрование**
```powershell
Set-SmbShare -Name "HR" -FolderEnumerationMode AccessBased -EncryptData $true -Force
```
`AccessBased` — пользователь видит только те папки, на которые у него есть права (Access-Based Enumeration). `EncryptData $true` — трафик шифруется, клиент обязан быть SMB 3.0+; на старых NAS это ломает доступ.

**4. Кто держит файл открытым (главный инструмент по «файл занят»)**
```powershell
Get-SmbOpenFile | Where-Object Path -like "*Nakladnaya*" | Format-Table ClientUserName,ClientComputerName,Path,FileId
Get-SmbSession -ClientComputerName 10.10.10.101
Close-SmbOpenFile -FileId 2147483714 -Force
```
`FileId` — обязательный ключ для закрытия. `-Force` закрывает даже при активном чтении: пользователь потеряет несохранённые данные, поэтому сначала звонок, потом закрытие. `Get-SmbSession` показывает все сессии с IP — удобно, когда «сервер тормозит» из-за сотни подключений с одной машины.

**5. Диагностика со стороны клиента**
```powershell
Get-SmbConnection | Format-Table ServerName,ShareName,Dialect,Encrypted,Signed
Get-SmbClientConfiguration | Select RequireSecuritySignature,EnableSecuritySignature,EnableSMB1Protocol
```
`Dialect: 3.1.1` — хорошо; `2.1` — клиент или сервер старый, шифрование недоступно; `1.0` — авария, ищите древний NAS или MFP-сканер.

**6. NTFS-права: чтение и разбор**
```powershell
(Get-Acl "D:\Shares\Buh").Access |
  Format-Table IdentityReference,FileSystemRights,AccessControlType,IsInherited -AutoSize
```
Смотрим три вещи: `IsInherited` (унаследовано от родителя или задано вручную), `AccessControlType` (Allow/**Deny**) и `IdentityReference` (группа, а не фамилия!). Права на пользователях вместо групп — техдолг.

**7. NTFS-права: массовое назначение через icacls**
```cmd
icacls "D:\Shares\Buh" /inheritance:d
icacls "D:\Shares\Buh" /grant "VEKTOR\GG-Buh:(OI)(CI)M"
icacls "D:\Shares\Buh" /remove "BUILTIN\Users" /T /C
icacls "D:\Shares\Buh" /save D:\acl-backup\buh.txt /T
```
`/inheritance:d` — отключить наследование с копированием текущих ACE (не `/inheritance:r`, который их снесёт). `(OI)(CI)M` — объекты и контейнеры, Modify. `/T` — рекурсивно, `/C` — продолжать при ошибках. `/save` — снимок ACL перед массовыми правками, `/restore` вернёт обратно. Это ваш «снапшот прав».

**8. FSRM: установка, квота, экран файлов**
```powershell
Install-WindowsFeature FS-Resource-Manager -IncludeManagementTools
New-FsrmQuota -Path "D:\Shares\HR" -Size 5GB -SoftLimit -Threshold 85,95,100
New-FsrmFileGroup -Name "Медиа" -IncludePattern @("*.mp3","*.avi","*.mkv","*.iso")
New-FsrmFileScreen -Path "D:\Shares\Obshchee" -IncludeGroup "Медиа" -Active
Get-FsrmQuota | Format-Table Path,Size,SoftLimit,Usage
```
`-SoftLimit` — только предупреждает (жёсткая квота = запись блокируется). `-Threshold` — проценты, на которых FSRM шлёт события/письма. Имена встроенных шаблонов квот зависят от локали `[ПРОВЕРИТЬ]`, поэтому задавайте размер явно. На Server Core GUI у FSRM нет — только PowerShell и удалённая оснастка.

**9. Теневые копии: смотреть и расширять**
```powershell
vssadmin list shadowstorage /for=D:
vssadmin list shadows /for=D:
vssadmin resize shadowstorage /for=D: /on=D: /maxsize=15%
Get-CimInstance -ClassName Win32_ShadowCopy | Select InstallDate,VolumeName
```
Если `Maximum Shadow Copy Storage space: Unbounded` или занято 100% — старые снимки удаляются молча, и пользователь теряет «вчерашнюю версию». Создание снимка вручную — оснастка «Свойства системы → Защита системы → Создать» или планировщик с `vssadmin create shadow /for=D:` `[ПРОВЕРИТЬ в вашей сборке]`.

**10. DFS Namespace: корень и папки**
```powershell
Install-WindowsFeature FS-DFS-Namespace,FS-DFS-Replication -IncludeManagementTools
New-SmbShare -Name "DFSRoot" -Path "D:\DFSRoot" -FullAccess "VEKTOR\adm.student"
New-DfsnRoot -Path "\\corp.vektor.lab\Shares" -TargetPath "\\FS01\DFSRoot" -Type DomainV2
New-DfsnFolder -Path "\\corp.vektor.lab\Shares\Buh" -TargetPath "\\FS01\Buh"
Get-DfsnFolderTarget -Path "\\corp.vektor.lab\Shares\Buh"
```
`New-DfsnRoot` требует, чтобы целевая шара уже существовала — поэтому сначала `New-SmbShare`. `DomainV2` — доменный namespace, хранится в AD, доступен как `\\corp.vektor.lab\Shares`.

**11. DFS Replication: группа, участники, состояние**
```powershell
New-DfsReplicationGroup -GroupName "RG-Test" -Description "Учебная репликация"
Add-DfsrMember -GroupName "RG-Test" -ComputerName FS01,DC02
Add-DfsrConnection -GroupName "RG-Test" -SourceComputerName FS01 -DestinationComputerName DC02
New-DfsReplicatedFolder -GroupName "RG-Test" -FolderName "TestRG"
Set-DfsrMembership -GroupName "RG-Test" -FolderName "TestRG" -ComputerName FS01 -ContentPath "D:\DFSR\TestRG" -PrimaryMember $true
Set-DfsrMembership -GroupName "RG-Test" -FolderName "TestRG" -ComputerName DC02 -ContentPath "D:\DFSR\TestRG" -PrimaryMember $false
Get-DfsrState -ComputerName DC02 | Format-Table ReplicationGroupName,State
Get-DfsrBacklog -GroupName "RG-Test" -FolderName "TestRG" -SourceComputerName FS01 -DestinationComputerName DC02
```
`-PrimaryMember $true` — ровно у одного участника, до начала начальной репликации. `Get-DfsrState` со `State: Normal` — репликация здорова; `Get-DfsrBacklog` покажет количество файлов, ещё не доехавших. Порядок команд важен: `Add-DfsrConnection` до `New-DfsReplicatedFolder`.

**12. Аудит доступа к файлам**
```powershell
auditpol /get /subcategory:"File Share"
Get-WinEvent -FilterHashtable @{LogName='Security';Id=5145;StartTime=(Get-Date).AddHours(-2)} |
  Select-Object TimeCreated,@{n='User';e={$_.Properties[1].Value}},@{n='Path';e={$_.Properties[8].Value}} -First 20
```
5145 — детальный доступ к шаре (нужен включённый «Audit File Share» + SACL на папке), 5140 — подключение к шаре. Без SACL события не появятся: аудит включается в двух местах.

**13. Кто съел место**
```powershell
Get-ChildItem "D:\Shares" -Directory | ForEach-Object {
  $s = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
  [pscustomobject]@{Folder=$_.Name; GB=[math]::Round($s/1GB,2)}
} | Sort-Object GB -Descending
```
Дальше сужаем до конкретной папки. Быстрее — Treesize или отчёт FSRM по хранилищу.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **FS01** (10.10.10.20, Windows Server 2022) — член домена, есть том **D:** для данных (создан в уроке 12; если тома нет — добавьте виртуальный диск в VMware и создайте том). Учётка `VEKTOR\adm.student` с правами локального администратора.
- **DC01** (10.10.10.10) — AD DS, DNS, DHCP работают; группы `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT` существуют.
- **DC02** (10.10.10.11) — второй контроллер домена, введён в домен, репликация SYSVOL в порядке. Нужен для части DFSR.
- **WIN11-CL01** — рабочая станция `ivanov` (учётка `VEKTOR\ivanov`), получает адрес по DHCP. Для теста нужен ещё вход под `petrova` (можно на WIN10-CL02).
- Папки `\\FS01\Obshchee`, `\\FS01\Buh`, `\\FS01\HR` с L1 существуют. Если они лежат на `C:\Shares`, мы переносим их в `D:\Shares` и пересоздаём шары с теми же именами — все ссылки у пользователей останутся рабочими.

### Снапшот
В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot, имя **`before-l2-013`**. Сделать на **FS01**, **DC02** и **WIN11-CL01**.

### Шаги

1. **FS01.** Создаём структуру на томе данных.
```powershell
New-Item -ItemType Directory -Path D:\Shares\Obshchee,D:\Shares\Buh,D:\Shares\HR,D:\DFSRoot,D:\ACL-Backup -Force
```
Ожидаемо: папки созданы, `D:\Shares` — корень данных.

2. **FS01.** Сохраняем текущие ACL как страховку и отключаем наследование там, где будем задавать права.
```cmd
icacls "D:\Shares\Buh" /save D:\ACL-Backup\buh-before.txt /T
icacls "D:\Shares\HR" /inheritance:d
```
Ожидаемо: « processed file: D:\Shares\HR». Наследование отключено, текущие ACE скопированы.

3. **FS01.** Раскладываем NTFS-права по группам.
```cmd
icacls "D:\Shares\Buh" /inheritance:d
icacls "D:\Shares\Buh" /grant "VEKTOR\GG-Buh:(OI)(CI)M" /grant "VEKTOR\adm.student:(OI)(CI)F" /remove "BUILTIN\Users" /T
icacls "D:\Shares\HR" /grant "VEKTOR\GG-HR:(OI)(CI)M" /remove "BUILTIN\Users" /T
icacls "D:\Shares\Obshchee" /grant "VEKTOR\Domain Users:(OI)(CI)M"
```
Ожидаемо: в `Buh` и `HR` группа `Users` исчезла, появились соответствующие GG-группы с Modify.

4. **FS01.** Пересоздаём шары с явными правами и включаем ABE/шифрование на HR.
```powershell
New-SmbShare -Name "Buh" -Path "D:\Shares\Buh" -FullAccess "VEKTOR\adm.student" -ChangeAccess "VEKTOR\GG-Buh" -Force
New-SmbShare -Name "HR"  -Path "D:\Shares\HR"  -FullAccess "VEKTOR\adm.student" -ChangeAccess "VEKTOR\GG-HR"  -Force
New-SmbShare -Name "Obshchee" -Path "D:\Shares\Obshchee" -FullAccess "VEKTOR\adm.student" -ChangeAccess "VEKTOR\Domain Users" -Force
Set-SmbShare -Name "HR" -FolderEnumerationMode AccessBased -EncryptData $true -Force
Get-SmbShareAccess -Name "HR"
```
Ожидаемо: у `HR` две записи — `adm.student` Full, `GG-HR` Change; ABE включён.

5. **WIN11-CL01** (под `ivanov`). Проверяем доступ и видимость.
```powershell
net use \\FS01\Buh /user:VEKTOR\ivanov
explorer \\FS01\Buh
Get-SmbConnection | Format-Table ServerName,ShareName,Dialect,Encrypted
```
Ожидаемо: `Buh` открывается, `Dialect: 3.1.1`, `Encrypted: False` (для Buh шифрование не включали). В `\\FS01` под `ivanov` папки `HR` не видно (ABE + нет прав).

6. **WIN10-CL02** (под `petrova`). Проверяем, что HR виден и открывается, а Buh — нет.
Ожидаемо: `\\FS01\HR` открывается; `\\FS01\Buh` — «Отказано в доступе». Это и есть работа двух слоёв прав.

7. **FS01.** Настраиваем FSRM: жёсткая квота на папку пользователя и экран файлов.
```powershell
Install-WindowsFeature FS-Resource-Manager -IncludeManagementTools
New-FsrmQuota -Path "D:\Shares\HR" -Size 5GB -SoftLimit -Threshold 80,95
New-FsrmFileGroup -Name "Медиа" -IncludePattern @("*.mp3","*.avi","*.mkv","*.iso")
New-FsrmFileScreen -Path "D:\Shares\Obshchee" -IncludeGroup "Медиа" -Active
Get-FsrmQuota | Format-Table Path,Size,SoftLimit
```
Ожидаемо: квота 5 ГБ (мягкая), файл-скрин активен. **Проверка эффекта:** скопируйте на `\\FS01\Obshchee` файл `.mp3` — операция должна быть заблокирована с сообщением об экране файлов.

8. **FS01.** Включаем теневые копии тома D: и создаём снимок.
Графически: `Win+R` → `SystemPropertiesProtection` → выберите том D: → «Настроить» → «Включить защиту системы», «Максимальное использование» 10% → «Создать». Затем проверяем:
```powershell
vssadmin list shadowstorage /for=D:
vssadmin list shadows /for=D:
```
Ожидаемо: в списке снимков есть только что созданный, размер хранилища ограничен.

9. **WIN11-CL01** (под `ivanov`). Создаём файл в `\\FS01\Buh`, меняем его, затем восстанавливаем предыдущую версию.
Правый клик по файлу → «Свойства» → вкладка **«Предыдущие версии»** → выбрать снимок → «Восстановить». Ожидаемо: версия файла откатывается к состоянию на момент снимка. Если вкладки нет — проверьте, что открываете по UNC-пути `\\FS01\Buh`, а не через DFS `[ПРОВЕРИТЬ]`.

10. **FS01.** Собираем DFS Namespace.
```powershell
Install-WindowsFeature FS-DFS-Namespace,FS-DFS-Replication -IncludeManagementTools
New-DfsnRoot -Path "\\corp.vektor.lab\Shares" -TargetPath "\\FS01\DFSRoot" -Type DomainV2
New-DfsnFolder -Path "\\corp.vektor.lab\Shares\Buh" -TargetPath "\\FS01\Buh"
New-DfsnFolder -Path "\\corp.vektor.lab\Shares\HR"  -TargetPath "\\FS01\HR"
Get-DfsnFolder -Path "\\corp.vektor.lab\Shares\*"
```
Ожидаемо: под `\\corp.vektor.lab\Shares` видны папки `Buh` и `HR`, обе ведут на FS01. Проверьте с клиента: `explorer \\corp.vektor.lab\Shares\Buh` открывается под `ivanov`.

11. **FS01.** Настраиваем DFS-репликацию между FS01 и DC02 (учебная группа).
```powershell
New-DfsReplicationGroup -GroupName "RG-Test" -Description "Учебная репликация"
Add-DfsrMember -GroupName "RG-Test" -ComputerName FS01,DC02
Add-DfsrConnection -GroupName "RG-Test" -SourceComputerName FS01 -DestinationComputerName DC02
New-DfsReplicatedFolder -GroupName "RG-Test" -FolderName "TestRG"
Set-DfsrMembership -GroupName "RG-Test" -FolderName "TestRG" -ComputerName FS01 -ContentPath "D:\DFSR\TestRG" -PrimaryMember $true
Set-DfsrMembership -GroupName "RG-Test" -FolderName "TestRG" -ComputerName DC02 -ContentPath "D:\DFSR\TestRG" -PrimaryMember $false
```
Ожидаемо: команды проходят без ошибок. **Важно:** реплицировать пользовательские данные на контроллер домена — учебный костыль; в бою под это ставят отдельный файловый сервер, а на DC оставляют только SYSVOL.

12. **FS01.** Кладём файл в источник и проверяем, что он доехал до DC02.
```powershell
"test $(Get-Date)" | Out-File D:\DFSR\TestRG\probe.txt -Encoding utf8
Start-Sleep -Seconds 60
Get-DfsrState -ComputerName DC02 | Format-Table ReplicationGroupName,State
Get-DfsrBacklog -GroupName "RG-Test" -FolderName "TestRG" -SourceComputerName FS01 -DestinationComputerName DC02
Test-Path \\DC02\D$\DFSR\TestRG\probe.txt
```
Ожидаемо: `State: Normal`, backlog пуст (или уменьшается), `Test-Path` возвращает `True`. Если `False` — смотрите журнал `DFS Replication` в Event Viewer на обоих серверах.

13. **Запись в журнал.** Допишите в `stand-log.md` всё, что изменилось (см. следующий подраздел). **Проверьте, что шара `\\FS01\Obshchee` всё ещё доступна** — если старая лежала на C:, убедитесь, что не осталось двух шар с одним именем и старыми путями.

### Проверка
- [ ] `Get-SmbShare` на FS01 показывает `Obshchee`, `Buh`, `HR` с путями на `D:\Shares`.
- [ ] `Get-SmbShareAccess -Name HR` содержит только `adm.student` (Full) и `GG-HR` (Change).
- [ ] `Get-Acl D:\Shares\Buh` не содержит `BUILTIN\Users`, содержит `VEKTOR\GG-Buh` с Modify и `IsInherited: False`.
- [ ] Под `ivanov` папка `\\FS01\HR` не видна и не открывается; под `petrova` — открывается.
- [ ] `Get-SmbConnection` с клиента показывает `Dialect 3.1.1`.
- [ ] Квота FSRM на `D:\Shares\HR` существует; копирование `.mp3` в `Obshchee` блокируется.
- [ ] `vssadmin list shadows /for=D:` показывает минимум один снимок; вкладка «Предыдущие версии» на клиенте работает.
- [ ] `\\corp.vektor.lab\Shares\Buh` открывается и ведёт на `\\FS01\Buh`.
- [ ] `Get-DfsrState -ComputerName DC02` → `Normal`, `probe.txt` существует на DC02.
- [ ] В `stand-log.md` появилась запись про урок 13.

### Сломай и почини

**Пользователь:** Петрова Мария (`petrova`). **ВМ:** WIN10-CL02. **Что написала:** «Здравствуйте! С утра не могу открыть `\\FS01\HR` — "Отказано в доступе". Вчера вечером всё работало, я ничего не меняла. И ещё: пропала папка "Приказы", которая была внутри. Срочно, у меня отчёт!» **Срочность:** высокая.

**Как воспроизвести поломку (на FS01):**
```cmd
icacls "D:\Shares\HR" /deny "VEKTOR\GG-HR:(OI)(CI)(RX)"
```

**Диагностика по шагам:**
1. **Уточняем у пользователя:** какой путь, какая ошибка дословно, работает ли у коллеги из HR. Это сразу делит проблему на «одна машина» и «вся группа».
2. **Проверяем связь и разрешение имени** с клиента: `ping FS01`, `nslookup FS01`. Если не пингуется — проблема не в правах, идите в сеть/DNS.
3. **Смотрим сессию:** `Get-SmbConnection` на клиенте — есть ли подключение к `\\FS01\HR`, под какой учёткой (`net use`). Частая причина — старая сессия под другой учёткой.
4. **Проверяем группу:** `whoami /groups | findstr /i GG-HR` на клиенте. Если группы нет — пользователя убрали из группы, либо не обновился Kerberos-билет (лечится `klist purge` и повторным входом).
5. **Проверяем шару:** `Get-SmbShareAccess -Name "HR"` на FS01. `GG-HR: Allow` на месте — значит, дело в NTFS.
6. **Проверяем NTFS:** `(Get-Acl D:\Shares\HR).Access | ft IdentityReference,FileSystemRights,AccessControlType,IsInherited`. Видим `VEKTOR\GG-HR  ReadAndExecute  Deny`. **Deny побеждает любой Allow** — вот и корень.
7. **Подтверждаем на клиенте:** в свойствах папки → «Безопасность» → «Дополнительно» → вкладка «Действующий доступ» → выбрать `petrova`. Показывает «Нет доступа» с указанием, какой ACE его заблокировал.
8. **Почему пропала папка «Приказы»:** ABE. Пока пользователь не имеет прав, папка не отображается. Это симптом той же причины, а не отдельная поломка.

**Решение:**
```cmd
icacls "D:\Shares\HR" /remove:d "VEKTOR\GG-HR"
icacls "D:\Shares\HR"
```
После снятия Deny петрова переподключается (`net use \\FS01\HR /delete`, затем заново) — папка и «Приказы» на месте.

**Ответ пользователю:** «Мария, добрый день. Нашли причину: на папке HR ошибочно стоял запрет для группы HR — он перебивал разрешение. Запрет снят, доступ восстановлен. Пожалуйста, отключите сетевой диск и подключите заново (или перезайдите в систему). Папка "Приказы" не пропадала — она скрывалась, потому что прав не было. Если что-то ещё не открывается — напишите, посмотрим. Заявку закрываю, при повторении — сразу звоните.»

**Закрытие заявки:** категория «Файловый сервер → Права доступа», причина «Explicit Deny ACE на NTFS», решение записано в базу знаний. Плюс превентив: раз в месяц снимать дамп ACL через `icacls /save` и сравнивать.

### Что изменилось на стенде
- **FS01:** создан том данных `D:` со структурой `D:\Shares\{Obshchee,Buh,HR}`, `D:\DFSRoot`, `D:\ACL-Backup`, `D:\DFSR\TestRG`.
- **FS01:** шары `Obshchee`, `Buh`, `HR`, `DFSRoot` пересозданы с явными share-правами и путями на `D:`; на `HR` включены ABE и SMB-шифрование.
- **FS01:** NTFS-права переведены на группы `GG-Buh`, `GG-HR`, `Domain Users`; `BUILTIN\Users` удалён из `Buh` и `HR`.
- **FS01:** установлены роли `FS-Resource-Manager`, `FS-DFS-Namespace`, `FS-DFS-Replication`.
- **FS01:** FSRM: мягкая квота 5 ГБ на `D:\Shares\HR`, группа файлов «Медиа», активный файл-скрин на `Obshchee`.
- **FS01:** включена защита системы тома D:, лимит shadow storage 10%, создан первый снимок.
- **Домен:** namespace `\\corp.vektor.lab\Shares` (DomainV2) с папками `Buh`, `HR`; группа репликации `RG-Test` (FS01 ↔ DC02, папка `TestRG`).
- **DC02:** получена папка `D:\DFSR\TestRG` с реплицированным содержимым.

### Откат
VMware Workstation → правый клик по ВМ → Snapshot Manager → выбрать `before-l2-013` → **Revert To Snapshot**. Сделать на FS01, DC02 и WIN11-CL01 (на клиенте — если испортили настройки подключений). После откатa FS01 удалите запись про урок 13 из `stand-log.md` или пометьте её как «откатано». Если откатывать FS01, а DC02 оставить — удалите группу репликации на DC02 (`Remove-DfsReplicationGroup -GroupName "RG-Test" -Force`) и почистите `D:\DFSR`, иначе получите «висячий» член группы.

## 6. Ошибки новичков и безопасность

1. **«Дал Everyone Full Control — заработало».** → Любой сотрудник, включая подрядчика с гостевой учёткой, может удалить или зашифровать данные. → Разрешения только на группы AD, share = Change, тонкая настройка — через NTFS.
2. **Права напрямую на пользователей.** → Через полгода никто не знает, у кого что; при увольнении доступ остаётся. → Только группы, пользователи — члены групп.
3. **`/inheritance:r` вместо `/inheritance:d`.** → Снос всех унаследованных ACE, папка становится доступна только админам, отдел встал. → Всегда `/inheritance:d`, перед массовыми правками — `icacls /save`.
4. **Один Deny «чтобы запретить лишнее».** → Deny перебивает Allow и часто ломает доступ всей группе, а ABE ещё и «прячет» папки. → Проектируйте через отсутствие Allow, Deny — только осознанно.
5. **Жёсткая квота без порогов и уведомлений.** → Пользователь видит «Не удаётся сохранить» без причины, а вы узнаёте об этом из заявки. → Мягкая квота + пороги 80/95 + письмо ответственному.
6. **Теневые копии считают бэкапом.** → Снимки лежат на том же томе: умер диск, пришло шифровальщик-шифрование, кончилось место — снимков нет. → VSS — это «отмена последних действий», бэкап — отдельно, на другое хранилище.
7. **DFSR считают заменой HA или бэкапу.** → DFSR не блокирует файлы, не годится для баз 1С, PST и открытых по сети файлов; при конфликте версий файл уезжает в `ConflictAndDeleted`. → DFSR — для филиалов и статичного контента, базы — на SQL/1С-сервер.
8. **Шифрование SMB «на всякий случай» на всех шарах.** → Старые сканеры/МФУ и Windows 7 перестают работать, падает скорость на слабых CPU. → Включайте точечно на чувствительных данных, проверяя совместимость.

**Безопасность.** Не выдавайте `adm.student` пользователям «на пять минут» — это не откатывается и не аудируется глазами. Включайте аудит доступа (SACL + `Audit File Share`) на папках с персональными данными: без него вы не докажете, кто удалил файл. Не храните на файловом сервере ключи КриптоПро и контейнеры ЭП в общедоступных папках — только в защищённых каталогах с узким ACL и аудитом. Не оставляйте SMB1 ради старого сканера — изолируйте устройство в отдельный VLAN. И помните: `Get-SmbOpenFile` показывает, кто что открыл, — это персональные данные, доступ к ним только у ИТ.

## 7. Вопросы на собеседовании

**1. Чем отличаются разрешения на общий ресурс от NTFS и как считается итоговое право?**
Итоговое право — пересечение (самое строгое) из share- и NTFS-разрешений. Внутри каждого слоя Allow накапливается, явный Deny перебивает всё. Классическая схема: на шаре — Change для рабочей группы и Full для админов, вся логика — в NTFS. Если человек не проходит — сначала смотрим, есть ли у него доступ к шаре, потом NTFS, потом group membership и токен Kerberos.
*Что хочет услышать интервьюер:* понимание двух слоёв и правила «Deny побеждает». *Красный флаг:* «достаточно дать Full на шару».

**2. Что даёт SMB 3 по сравнению с SMB 2.1 и как это проверить?**
Шифрование (AES-128-CCM в 3.0, GCM в 3.1.1), SMB Multichannel — агрегирование нескольких сетевых интерфейсов, SMB Direct (RDMA) — обход стека TCP с минимальной задержкой, прозрачный failover на кластерах, VSS for SMB shares, улучшенные счётчики производительности, а в 3.1.1 — pre-auth integrity. Проверка: `Get-SmbConnection` на клиенте (Dialect), `Get-SmbServerConfiguration` на сервере, счётчики `SMB Client Shares`/`SMB Server Shares` в PerfMon.
*Что хочет услышать интервьюер:* что кандидат знает про шифрование и Multichannel, а не только «быстрее стало». *Красный флаг:* «SMB3 — это просто версия винды».

**3. Зачем нужны DFS Namespaces и DFS Replication, в чём разница?**
DFS-N — это слой навигации: абстрактное имя `\\corp.vektor.lab\Shares\Buh` с referral на реальный сервер. Позволяет переехать на другой сервер без переучивания людей, объединить папки с разных серверов, добавить вторую цель для отказоустойчивости. DFSR — это репликация содержимого между серверами: мультимастер, RDC (копируются только изменённые блоки), работает на своём расписании. DFS-N без DFSR — просто удобные ссылки; DFSR без DFS-N — копии данных на разных путях.
*Что хочет услышать интервьюер:* чёткое разделение «навигация» и «данные». *Красный флаг:* «DFS — это бэкап/кластер».

**4. Пользователь говорит «не открывается папка на файловом сервере». Ваши действия по шагам?**
Уточняю путь, текст ошибки, работает ли у коллег — это сразу сужает круг. Дальше: связь и DNS (`ping`, `nslookup`), сессия (`Get-SmbConnection`, `net use`), членство в группе (`whoami /groups`), share-права (`Get-SmbShareAccess`), NTFS ACL (`Get-Acl`, ищу Deny и разрыв наследования), «действующий доступ» в свойствах папки, при включённом аудите — события 5145/4663 в Security-логе. Отдельно проверяю, не блокирует ли антивирус/防火墙 и нет ли переполнения квоты на томе.
*Что хочет услышать интервьюер:* методичность и знание, где именно смотреть. *Красный флаг:* «перезагрузил бы сервер».

**5. Как работают теневые копии и можно ли считать их резервной копией?**
VSS делает copy-on-write снимок тома по расписанию, снимки хранятся в shadow storage на том же томе (по умолчанию ~10%). Пользователь видит их как «Предыдущие версии». Копией это не является: тот же диск, тот же массив, при переполнении хранилища старые снимки удаляются автоматически, а ransomware зашифрует и снимки, если у него есть права. Резервная копия — отдельное хранилище, с проверкой восстановления.
*Что хочет услышать интервьюер:* что кандидат не путает VSS и бэкап, знает про лимит shadow storage. *Красный флаг:* «теневые копии — это и есть бэкап».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Storage» → «File and Storage Services»: обзоры SMB, DFS Namespaces, DFS Replication, FSRM, Volume Shadow Copy Service. Поиск: `SMB security enhancements site:learn.microsoft.com`, `DFS Namespaces overview site:learn.microsoft.com`.
- **learn.microsoft.com** — справочник PowerShell: модули `SmbShare`, `Dfsn`, `Dfsr`, `Fsrm`, `Storage`. Поиск: `New-DfsnRoot site:learn.microsoft.com`, `New-FsrmQuota site:learn.microsoft.com`.
- **learn.microsoft.com** — «File Server Resource Manager overview» и «Quotas» / «File screening management». Поиск: `FSRM quotas overview site:learn.microsoft.com`.
- **learn.microsoft.com** — «SMB 3.0 protocol features» / «SMB 3.1.1 features». Поиск: `SMB 3.1.1 pre-authentication integrity site:learn.microsoft.com`.
- **learn.microsoft.com** — «Access-based enumeration» и «Advanced security settings / Effective access». Поиск: `access-based enumeration site:learn.microsoft.com`.
- **learn.microsoft.com** — журналы событий аудита: 5140, 5145, 4663. Поиск: `5145 event ID site:learn.microsoft.com`.
- **learn.microsoft.com** — «DFS Replication: What's new» и «DFS Replication troubleshooting». Поиск: `DFS Replication troubleshooting site:learn.microsoft.com`. `[ПРОВЕРИТЬ]` актуальность статей для Windows Server 2022 — часть материалов наследуется из 2012 R2 без изменений по существу.
- **Astra Linux / РЕД ОС** — на будущее (кросс-платформенный доступ к SMB-шарам через `smbclient`/`mount.cifs`): документация на `astra.ru` и `redos.red-soft.ru`, раздел про Samba/cifs-utils. Поиск: `cifs-utils mount Windows share Astra Linux`.
