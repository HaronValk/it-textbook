# Архитектура Windows Server 2019/2022: роли, компоненты, Server Core и GUI

## 1. Введение (зачем это нужно в реальной работе)

**Заявка #L2-014**

- **Пользователь:** petrova (Мария Петрова, HR), ВМ WIN10-CL02
- **Что написала:** «Здравствуйте! Отправила приказ на печать на принтер в HR, а задание висит в очереди и не уходит. Второй день уже. Можно починить?»
- **Срочность:** средняя

Вы приходите на FS01, а там вопрос из вопроса: печать — это отдельная программа, служба, роль или «так, железка»? Где вообще смотреть, что на сервере установлено, что из этого запущено и кто отвечает за очередь печати?

В L1 сервер воспринимался как «компьютер с Windows, только большой». На L2 нужно видеть внутреннее устройство: сервер — это набор **ролей** (функций, ради которых машину поставили), каждая роль состоит из **компонентов** и опирается на **службы**; часть серверов в компаниях вообще ставится без графического интерфейса (Server Core), и управлять ими приходится удалённо. В Москве это встречается постоянно: банки, логистика, 1С-инфраструктура — везде серверы с разными наборами ролей, и половина из них без GUI.

После этой главы вы сможете: перечислить установленные роли и компоненты на любом сервере стенда одной командой; объяснить разницу между ролью, компонентом и службой; сказать, зачем нужен Server Core и как им пользоваться; поставить и снять компонент без мыши; понимать, где заканчивается «нажать кнопку» и начинается инженерное решение.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

### Слои: от железа до роли

Ядро Windows Server одинаково для всех редакций и для Core, и для GUI. Разница — в том, какие пользовательские компоненты установлены поверх.

```text
┌───────────────────────────────────────────────────────────────┐
│ ПРИЛОЖЕНИЯ И РОЛИ    AD DS │ DNS │ DHCP │ Файлы │ Печать │ 1С  │
│                      (роль = набор компонентов + служб)        │
├───────────────────────────────────────────────────────────────┤
│ СЛУЖБЫ (services.exe → svchost.exe / отдельные exe)            │
│ DHCPServer, DNS, Spooler, LanmanServer ...                     │
├───────────────────────────────────────────────────────────────┤
│ ПОЛЬЗОВАТЕЛЬСКИЙ РЕЖИМ: explorer.exe, mmc.exe, PowerShell,     │
│ ServerManager.exe, sconfig  ← здесь и живёт разница Core/GUI   │
├───────────────────────────────────────────────────────────────┤
│ ЯДРО: ntoskrnl.exe, HAL, драйверы (.sys), Win32 API, .NET      │
├───────────────────────────────────────────────────────────────┤
│ ЖЕЛЕЗО / ВМ VMware: CPU, RAM, диск, сетевой адаптер            │
└───────────────────────────────────────────────────────────────┘
```

Службы работают в **сессии 0** (session 0), изолированно от интерактивных входов (RDP-сессии — это сессия 1, 2 и далее). Поэтому «я залогинен, а служба не видит мой диск» — не баг, а архитектура.

### Роль, служба роли, компонент, служба

- **Роль (role)** — основная функция сервера: AD DS, DNS, DHCP, File and Storage Services, Print and Document Services, Web Server (IIS), Hyper-V, WSUS, AD CS, RDS.
- **Служба роли (role service)** — уточнение внутри роли. Например, внутри File and Storage Services: File Server, DFS Namespaces, DFS Replication, File Server Resource Manager, iSCSI Target Server, Server for NFS.
- **Компонент / функция (feature)** — вспомогательная возможность, не являющаяся функцией сервера: .NET Framework 3.5/4.8, BitLocker, Failover Clustering, Windows Server Backup, GPMC, SNMP, Telnet-клиент, `FS-SMB1` (поддержка древнего SMB1).
- **Служба (service)** — процесс, который реально работает: `DHCPServer`, `DNS`, `Spooler`, `LanmanServer` (файловый сервер SMB).

Связка для L2: роль установлена → но служба остановлена → пользователь ничего не видит. 80% «сервер сломался» — это второй случай. Установленная роль и работающая служба — разные вещи, проверять надо и то, и то.

### Варианты установки: Desktop Experience, Core, MinShell

При установке Windows Server выбирается вариант: **Server with Desktop Experience** (полный GUI, explorer.exe, Панель управления) или **Server Core** (без оболочки). Это не разные ОС — один и тот же продукт, разный набор компонентов.

| | Desktop Experience | Server Core | MinShell |
|---|---|---|---|
| explorer.exe / рабочий стол | да | нет | нет |
| Server Manager, MMC-оснастки | да | нет (только с FOD) | да |
| PowerShell, CMD, `sconfig` | да | да | да |
| WinRM / RDP | да | да | да |
| Место на диске и объём обновлений | больше | меньше | меньше |
| Поверхность атаки | шире | уже | уже |

**MinShell (Minimal Server Interface)** — промежуточный вариант: есть Server Manager и MMC, но нет оболочки рабочего стола. Включается компонентами `Server-Gui-Mgmt-Infra` и `Server-Gui-Shell`.

**Server Core App Compatibility FOD** — «возможность по запросу» (Feature on Demand, FOD), которая добавляет на Core часть GUI: `mmc.exe`, `servermanager.exe`, некоторые элементы проводника. Ставится отдельно, требует источника (ISO с установочным образом) или WSUS [ПРОВЕРИТЬ точное имя пакета].

Полезно знать: **Nano Server** как вариант установки для обычных серверов больше не предлагается — он остался только как образ контейнера. **Полугодовой канал (SAC)** для Windows Server прекращён; 2019 и 2022 — это LTSC-релизы [ПРОВЕРИТЬ статус]. Редакции: Standard и Datacenter (у Datacenter больше прав на виртуализацию и Storage Spaces Direct), плюс Evaluation-версии на 180 дней — как раз то, что у нас на стенде.

### Как этим управляют: три пути к одному результату

1. **GUI**: Server Manager → «Управление» → «Добавить роли и компоненты».
2. **PowerShell**: модуль `ServerManager` — `Get-WindowsFeature`, `Install-WindowsFeature`, `Uninstall-WindowsFeature`. Важно: это модуль **Windows PowerShell 5.1**; в PowerShell 7 он не работает напрямую без `Import-Module ServerManager -UseWindowsPowerShell` [ПРОВЕРИТЬ].
3. **DISM** (`dism.exe /online /enable-feature ...`) — тот же механизм «под капотом», работает и там, где PowerShell-модуля нет.

Все три меняют одно и то же хранилище компонентов (`WinSxS`) и реестровые записи. Порты удалённого управления: WinRM — 5985 (HTTP) и 5986 (HTTPS), RDP — 3389, SMB — 445. По модели OSI это всё прикладной уровень (L7), но для диагностики важно: если 5985 закрыт файрволом, «PowerShell не подключается» — сетевая проблема, а не серверная.

## 3. Аналогия из жизни

Представьте производственный цех. Фундамент, электрика, вода — это **ядро**: оно одинаково, что бы вы ни производили. Станки — **службы**: именно они шумят и делают работу. Производственные линии, собранные из станков, — **роли**: линия «Печать», линия «Файлы», линия «Домен». Компоненты — это тележки, лампы и огнетушители: сами ничего не производят, но без них линия не запустится.

**Desktop Experience** — это цех с офисом менеджеров, коридорами, табличками и лифтом. Удобно, но отапливать и охранять надо больше, и в офис может зайти кто угодно. **Server Core** — тот же цех без офиса: управление идёт по рации (PowerShell, WinRM) и по пульту у входа (`sconfig`). Работает всё то же самое, но войти «просто погулять» некуда.

Аналогия ломается в одном: офис в цеху можно достроить и снести по желанию, а переключение Core ↔ Desktop Experience хотя и поддерживается официально (`Install-WindowsFeature Server-Gui-Mgmt-Infra,Server-Gui-Shell` и обратно) [ПРОВЕРИТЬ актуальность для 2022], на практике чаще переставляют сервер — чище и предсказуемее.

## 4. Практика и команды

Все команды — из PowerShell на DC01, если не указано иное.

**1. Полный список ролей и компонентов**

```powershell
Get-WindowsFeature
```

Вывод — три колонки: `Display Name`, `Name` (техническое имя, его и подставляют в команды), `Install State` (`Installed` / `Removed`). Роли вложенные: `[X]` у родителя и `[ ]` у дочернего — нормально. Смотреть: какие роли стоят, а какие «Removed».

```text
Display Name                              Name                    Install State
------------                              ----                    -------------
[X] Active Directory Domain Services      AD-Domain-Services          Installed
[X] DNS Server                            DNS                         Installed
[X] DHCP Server                           DHCP                        Installed
[X] File and Storage Services             FileAndStorage-Services     Installed
    [X] File Server                       FS-FileServer               Installed
    [ ] SMB 1.0/CIFS File Sharing Support FS-SMB1                     Removed
```

**2. Разбор конкретной роли и её служб роли**

```powershell
Get-WindowsFeature -Name AD-Domain-Services, DNS, DHCP, Print-Services |
    Format-Table Name, DisplayName, InstallState -AutoSize
(Get-WindowsFeature -Name Print-Services).SubFeatures | Format-Table Name, InstallState
```

`-Name` принимает массив, `SubFeatures` показывает вложенные службы роли (у печати это, например, `Print-Server`) [ПРОВЕРИТЬ точные имена].

**3. Инвентаризация «что стоит» в коротком виде**

```powershell
Get-WindowsFeature | Where-Object InstallState -eq 'Installed' |
    Sort-Object Name | Format-Table Name, DisplayName -AutoSize
```

Этой строкой удобно снимать «фотографию» сервера перед изменениями и вклеивать её в `stand-log.md`.

**4. Установка компонента**

```powershell
Install-WindowsFeature -Name Windows-Server-Backup
```

```text
Success Restart Needed Exit Code      Feature Result
------- -------------- ---------      --------------
True    No             Success        {Windows Server Backup}
```

Ключи: `-Name` — техническое имя; `-IncludeAllSubFeature` — все подкомпоненты; `-IncludeManagementTools` — консоль управления (для IIS — IIS Manager); `-Restart` — перезагрузить автоматически, если нужно; `-Source` — путь к `\sources\sxs` на ISO, если компонента нет в локальном хранилище. Смотреть на `Success` и `Restart Needed`: если `True` — без перезагрузки роль может не заработать.

**5. Удаление компонента**

```powershell
Uninstall-WindowsFeature -Name Web-Server -Restart
# на стенде НЕ выполняем — пример синтаксиса
```

`-Remove` дополнительно вычищает файлы из `WinSxS`: экономит место, но повторная установка потребует источника. На рабочем сервере лучше без `-Remove`, если не уверены.

**6. Управление другими серверами (у `Get-WindowsFeature` нет параметра `-ComputerName`)**

```powershell
Invoke-Command -ComputerName DC02, FS01 -ScriptBlock {
    Get-WindowsFeature | Where-Object InstallState -eq 'Installed' |
        Select-Object @{n='Server';e={$env:COMPUTERNAME}}, Name
} | Select-Object Server, Name | Sort-Object Server, Name
```

Требует работающего WinRM. Это же и есть «жизнь без GUI»: команда выполняется на удалённой машине, результат приходит к вам.

**7. То же через DISM**

```cmd
dism /online /get-features /format:table /english | findstr /i "backup print web-server"
dism /online /enable-feature /featurename:Windows-Server-Backup /all /norestart
```

`/online` — текущая ОС, `/get-features` — список, `/format:table` — таблица, `/enable-feature` + `/all` — с подкомпонентами, `/norestart` — не перезагружать. `/english` полезен, чтобы `findstr` не зависел от локализации [ПРОВЕРИТЬ наличие ключа в вашей сборке].

**8. Определить вариант установки**

```powershell
(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').InstallationType
(Get-ComputerInfo).WindowsInstallationType
(Get-ComputerInfo).WindowsProductName
```

Вернёт `Server` (Desktop Experience) или `Server Core`. `Get-ComputerInfo` задумывается на пару секунд — это нормально.

**9. Роль → служба: кто реально работает**

```powershell
Get-CimInstance Win32_Service -Filter "Name='DHCPServer' OR Name='DNS' OR Name='Spooler'" |
    Select-Object Name, State, StartMode, StartName, PathName
```

```text
Name       State   StartMode  StartName        PathName
----       -----   ---------  ---------        --------
DHCPServer Running Auto       NT AUTHORITY\... C:\Windows\system32\svchost.exe -k ...
DNS        Running Auto       NT AUTHORITY\... C:\Windows\system32\dns.exe
Spooler    Running Auto       LocalSystem      C:\Windows\System32\spoolsv.exe
```

Смотреть: `State`, `StartMode` (`Auto` / `Manual` / `Disabled`) и под какой учёткой служба ходит.

**10. Кто в каком процессе живёт**

```cmd
tasklist /svc /fi "imagename eq svchost.exe"
```

Показывает, какие службы «упакованы» в общий `svchost.exe`. На современных сборках при объёме RAM больше ~3,5 ГБ каждая служба часто получает свой `svchost` — это сделано для устойчивости.

**11. Возможности по запросу (FOD)**

```powershell
Get-WindowsCapability -Online -Name "*AppCompat*"
# установка на Core (нужен источник):
# Add-WindowsCapability -Online -Name Server-AppCompat-FoD~~~~0.0.1.0
```

Имя возможности помечено как `[ПРОВЕРИТЬ]` — сверяйте с выводом `Get-WindowsCapability -Online` на своей сборке.

**12. `sconfig` — пульт управления Core**

Только на Server Core: `sconfig` → меню 1 (домен/workgroup), 2 (имя), 4 (удалённое управление), 7 (RDP), 13 (перезагрузка). В GUI-версии аналог — «Диспетчер сервера» → «Локальный сервер» и «Управление» → «Добавить роли и компоненты».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 в состоянии после урока 4: **DC01** (AD DS, DNS, DHCP, домен `corp.vektor.lab`), **DC02** (второй контроллер домена, 10.10.10.11, из урока 3), **FS01** (файлы, печать, 10.10.10.20). Учётки: `VEKTOR\adm.student`, пароль `Lab#2026-vektor`. Все действия — с консоли DC01, WinRM между серверами уже работает (настраивали в уроке 3–4). RSAT и Windows Admin Center не нужны — они в уроке 8.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-l2-006`. Машины: **DC01**, **DC02**, **FS01**.

### Шаги

1. **DC01.** Снимок текущего состояния:
   ```powershell
   Get-WindowsFeature | Where-Object InstallState -eq 'Installed' |
       Sort-Object Name | Format-Table Name, DisplayName -AutoSize
   ```
   Ожидаемо: `AD-Domain-Services`, `DNS`, `DHCP`, `FileAndStorage-Services`/`FS-FileServer`, `GPMC`. Скопируйте вывод в `stand-log.md`.

2. **DC01.** Определите вариант установки (команды из п. 8 практики). Ожидаемо: `Server` — то есть Desktop Experience.

3. **DC01.** Разберите роль на службы роли:
   ```powershell
   Get-WindowsFeature -Name FileAndStorage-Services |
       Select-Object Name, InstallState, SubFeatures
   ```
   Ожидаемо: список вложенных компонентов (`FS-FileServer`, `FS-Resource-Manager`, `FS-DFS-Namespace` и т.д.) — часть `Installed`, часть `Removed`.

4. **DC01.** Удалённая инвентаризация двух серверов одной командой:
   ```powershell
   Invoke-Command -ComputerName DC02, FS01 -ScriptBlock {
       [pscustomobject]@{
           Server  = $env:COMPUTERNAME
           Install = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').InstallationType
           Count   = (Get-WindowsFeature | Where-Object InstallState -eq 'Installed').Count
       }
   } | Format-Table -AutoSize
   ```
   Ожидаемо: две строки, `Install = Server`, у DC02 установленных компонентов меньше, чем у FS01.

5. **DC01 → FS01.** Поставьте компонент удалённо:
   ```powershell
   Invoke-Command -ComputerName FS01 -ScriptBlock {
       Install-WindowsFeature -Name Windows-Server-Backup
   }
   ```
   Ожидаемо: `Success True`, `Restart Needed No`, `Feature Result {Windows Server Backup}`.

6. **FS01.** Проверьте установку двумя способами — «родным» и через DISM:
   ```powershell
   Invoke-Command -ComputerName FS01 -ScriptBlock {
       Get-WindowsFeature -Name Windows-Server-Backup | Select-Object Name, InstallState
       dism /online /get-features /format:table /english | Select-String -Pattern 'Backup'
       wbadmin get versions
   }
   ```
   Ожидаемо: `Installed`, строка `Enabled` в выводе DISM, `wbadmin` отвечает, что версий для восстановления нет.

7. **FS01 (локально, через консоль VMware).** Откройте `servermanager.exe` → «Управление» → «Добавить роли и компоненты» → «Далее» до списка компонентов, найдите «Windows Server Backup». Ожидаемо: галочка стоит. Закройте мастер кнопкой «Отмена» — ничего не меняем. Это и есть проверка «GUI и PowerShell показывают одно и то же».

8. **DC01.** Посмотрите связку «роль → служба»:
   ```powershell
   Get-CimInstance Win32_Service -Filter "Name='DHCPServer' OR Name='DNS' OR Name='Spooler'" |
       Select-Object Name, State, StartMode, ProcessId, PathName
   ```
   Ожидаемо: три службы в состоянии `Running`, `StartMode Auto`. Запомните: `Spooler` — служба печати на FS01, живёт в `spoolsv.exe`.

9. **DC01.** Работа «как с Core» — всё через удалённую сессию:
   ```powershell
   Enter-PSSession -ComputerName DC02
   Get-WindowsFeature | Where-Object InstallState -eq 'Installed' | Measure-Object
   Get-Service DNS | Select-Object Name, Status
   Exit-PSSession
   ```
   Ожидаемо: промпт меняется на `[DC02]: PS>`, обе команды отрабатывают. Ни одного клика мышью на DC02 не понадобилось.

10. **DC01.** Посмотрите на процессы служб:
    ```cmd
    tasklist /svc /fi "imagename eq svchost.exe" | findstr /i "dns dhcp"
    ```
    Ожидаемо: строки `svchost.exe` с перечнем служб в последней колонке (DNS на самом деле `dns.exe`, не `svchost` — это тоже правильный ответ).

11. **DC01.** Проверьте, видны ли возможности App Compatibility:
    ```powershell
    Get-WindowsCapability -Online -Name "*AppCompat*"
    ```
    Результат запишите как есть — на сервере с GUI она может отсутствовать, и это нормально.

12. **DC01.** Допишите в `stand-log.md`: дату, что установлено на FS01, какие роли найдены на DC01/DC02/FS01, имя снапшота.

### Проверка

- [ ] На DC01 вывод `Get-WindowsFeature` сохранён в журнал стенда
- [ ] `InstallationType` на всех трёх серверах = `Server`
- [ ] `Invoke-Command` до DC02 и FS01 проходит без ошибок
- [ ] На FS01 `Windows-Server-Backup` в состоянии `Installed`, DISM показывает `Enabled`
- [ ] `wbadmin get versions` на FS01 отвечает без ошибки «команда не найдена»
- [ ] В Server Manager на FS01 компонент отмечен галочкой
- [ ] `Enter-PSSession` на DC02 работает, `Exit-PSSession` возвращает промпт DC01
- [ ] `stand-log.md` дополнен, снапшот `before-l2-006` существует на DC01, DC02, FS01

### Сломай и почини

**Заявка #L2-015**

- **Пользователь:** petrova (Мария Петрова, HR), ВМ WIN10-CL02
- **Что написала:** «Печать совсем встала: задания висят в очереди и не уходят, у коллег тоже. Принтер в HR не отвечает. Очень срочно, у нас приказы!»
- **Срочность:** высокая

**Как воспроизвести поломку.** На FS01 (консоль VMware, PowerShell от `adm.student`):

```powershell
Stop-Service -Name Spooler -Force
Set-Service  -Name Spooler -StartupType Disabled
Get-Service  -Name Spooler | Select-Object Name, Status, StartType
```

Ожидаемо: `Stopped`, `Disabled`. Пользователи при этом видят принтер, но задания не печатаются — классика.

**Диагностика по шагам.**

1. Границы проблемы: один пользователь или все? Печатает ли локальный принтер на WIN10-CL02? Если локально печатает, а через `\\FS01` нет — идём на сервер.
2. `Get-Service Spooler` на FS01 → `Stopped` / `Disabled`. Всё, виновник найден, но причину ещё не знаем.
3. Роль на месте? `Get-WindowsFeature Print-Services` → `Installed`. Значит, роль не удаляли — сломана служба, а не компонент.
4. Почему остановилась? Смотрим журнал:
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager';
       StartTime=(Get-Date).AddHours(-4)} |
     Where-Object Message -match 'Spooler' | Select-Object TimeCreated, Id, Message
   ```
   Ожидаемо: события 7040 (изменён тип запуска) и 7036 (изменено состояние) — они прямо говорят «кто-то переключил в Disabled», а не «служба упала».
5. Очередь: `Get-Printer | Select-Object Name, PortName, PrinterStatus`, затем `Get-PrintJob -PrinterName '<имя>'`. Зависшие задания надо снять, иначе после запуска службы они «выстрелят» пачкой.
6. Зависимости: `Get-Service Spooler | Select-Object -ExpandProperty DependentServices` и `ServicesDependedOn` — убеждаемся, что RPC на месте и запущен.

**Решение.**

```powershell
Set-Service   -Name Spooler -StartupType Automatic
Start-Service -Name Spooler
Get-PrintJob -PrinterName '<имя>' | Remove-PrintJob
Restart-Service -Name Spooler
Get-Service Spooler | Select-Object Name, Status, StartType
```

**Ответ пользователю:** «Мария, добрый день. Причина найдена: на файловом сервере была остановлена служба печати (диспетчер печати), принтер и задания были в порядке. Службу запустили, тип запуска вернули в автоматический, зависшие задания сняли. Проверьте, пожалуйста, печать — и напишите, если что-то ещё висит в очереди».

**Закрытие заявки.** Категория «Печать», приоритет средний, корневая причина: «служба Spooler переведена в Disabled (изменение конфигурации), задания не обрабатывались». В базу знаний — заметка «Печать не идёт: проверь Spooler на FS01, затем очередь», в список проверок для будущего MON01 — мониторинг службы `Spooler` на FS01.

> В боевой среде причину «кто отключил службу» надо докапывать до конца: ищите события аудита входа, историю изменений в системе тикетов, активность подрядчиков. Служба не отключается сама.

### Что изменилось на стенде

- **FS01:** установлен компонент `Windows-Server-Backup` (роль не добавлялась).
- **FS01:** служба `Spooler` возвращена в `Automatic` и запущена (после учебной поломки).
- **DC01, DC02:** изменения только читающие, конфигурация не менялась.
- **Учётные записи, адреса, папки:** без изменений.
- **stand-log.md:** запись о дате, снапшоте `before-l2-006`, наборе ролей на DC01/DC02/FS01 и установленном компоненте.

### Откат

VMware Workstation → выберите ВМ (DC01, DC02, FS01) → **VM → Snapshot → Snapshot Manager** → выберите `before-l2-006` → **Go To**. Откатывайте все три машины, даже если меняли одну: так состояние стенда останется согласованным. После отката проверьте, что DC01 отвечает по DNS (`nslookup corp.vektor.lab`) и что FS01 видит домен. Если снапшот не нужен — удалите его только после того, как убедитесь, что стенд рабочий.

## 6. Ошибки новичков и безопасность

1. **Путать роль и службу.** → «Роль установлена, значит работает» → на деле служба `Stopped`. Правильно: всегда проверять и `InstallState`, и `State` службы.
2. **`Uninstall-WindowsFeature -Remove` «чтобы почистить».** → Компонент удалён вместе с файлами, повторная установка требует ISO, а иногда и перезагрузки. Правильно: без `-Remove`, если нет прямой необходимости.
3. **Ставить GUI «чтобы удобнее» на все серверы.** → Больше патчей, больше поверхности атаки, больше RAM. Правильно: GUI там, где он реально нужен (терминальные серверы, рабочие места админов), остальное — Core + удалённое управление.
4. **Пытаться `Get-WindowsFeature -ComputerName DC02`.** → Ошибка параметра: его нет. Правильно: `Invoke-Command` или `Enter-PSSession`.
5. **Ставить лишние роли на контроллер домена.** → DC — самый критичный узел в сети; IIS, RDS или 1С на нём превращают его в мишень и усложняют восстановление. Правильно: DC — только AD DS, DNS, при необходимости DHCP.
6. **Забыть про `Restart Needed: Yes`.** → Роль числится установленной, но не работает до перезагрузки. Правильно: читать вывод установки целиком.
7. **Настроить Core и не проверить WinRM.** → Сервер есть, доступа нет, едем в дата-центр с монитором. Правильно: сразу после установки Core — `Enable-PSRemoting`, проверка `Test-WSMan`, запись в документацию.
8. **Ставить компоненты «на всякий случай», без записи.** → Через полгода никто не знает, зачем на FS01 стоит `Telnet-Client`. Правильно: каждое изменение — в `stand-log.md` и в базу знаний.

### Безопасность

- Не отключайте файрвол и UAC «ради удалённого управления» — открывайте конкретные порты (5985/5986) и ограничивайте источники.
- Не включайте устаревшие компоненты (`FS-SMB1`, `Telnet-Client`, SNMP v1) без письменного обоснования: это классические векторы атак.
- Не давайте `adm.student`-подобные доменные права всем подряд: в норме для установки ролей хватает делегированных прав на OU серверов, а доступ к серверам выдаётся по группам.
- Не храните пароли в скриптах и `ps1`-файлах на общих папках — только защищённые хранилища и управляемые учётки.
- Не выходите с сервера в интернет «скачать компонент»: источник компонентов — WSUS или установочный ISO; сервер в DMZ тем более не должен иметь прямого выхода.

## 7. Вопросы на собеседовании

**1. Чем роль отличается от компонента и от службы?**

Ответ: роль — это основная функция сервера, ради которой его поставили (AD DS, DNS, DHCP, файловый сервер, IIS), она состоит из служб ролей. Компонент (feature) — вспомогательная возможность: .NET Framework, BitLocker, Windows Server Backup, Failover Clustering. Служба — процесс, который непосредственно работает (`DHCPServer`, `DNS`, `Spooler`). Роль может быть установлена, а её служба — остановлена, и тогда пользователь видит «сервер не работает».

*Что хочет услышать интервьюер:* что вы не путаете уровни и умеете проверять оба. *Красный флаг:* «ну, роль и компонент — это одно и то же, просто в разных меню».

**2. Server Core или Desktop Experience — что выберете для контроллера домена и почему?**

Ответ: для DC — Server Core. Меньше компонентов, меньше обновлений и перезагрузок, уже поверхность атаки, ниже потребление RAM и диска, а управление всё равно удалённое (RSAT, PowerShell, WAC). Компенсация отсутствия GUI: WinRM настроен сразу, `sconfig` для базовой настройки, при необходимости — App Compatibility FOD для MMC. Desktop Experience оставлю там, где он нужен по делу: терминальные серверы, серверы с 1С, где работают через GUI, машины администраторов.

*Что хочет услышать интервьюер:* осознанный выбор плюс план, как вы будете управлять и как восстановитесь, если сеть отвалится. *Красный флаг:* «Core — это неудобно, всегда ставлю с графикой».

**3. Как узнать, какие роли стоят на 30 серверах, и поставить недостающую роль на 5 из них?**

Ответ: инвентаризация — `Invoke-Command` по списку серверов с `Get-WindowsFeature` (или `Get-CimInstance`/`dism /online /get-features`), результат в CSV и в отчёт. Установка — тем же `Invoke-Command` с `Install-WindowsFeature`, при большом парке — через GPO, Configuration Manager, Ansible или WAC. Обязательно: снапшот/бэкап, окно обслуживания, `-Restart` под контроль, проверка результата (`InstallState`, служба, событие в журнале).

*Что хочет услышать интервьюер:* системный подход и понимание рисков массовых изменений. *Красный флаг:* «пройдусь по каждому серверу руками через RDP».

**4. Что такое App Compatibility FOD и когда он нужен?**

Ответ: возможность по запросу для Server Core, добавляющая часть графических инструментов — `mmc.exe`, оснастки, `servermanager.exe` и некоторые элементы проводника. Ставится отдельно из установочного образа или через WSUS. Нужен, когда на Core требуется конкретная MMC-оснастка или установщик приложения без поддержки Core, но переводить сервер на полный GUI не хочется. Минус — FOD расширяет поверхность атаки и требует источника.

*Что хочет услышать интервьюер:* что вы знаете про вариант «серединка» и не путаете его с полным Desktop Experience. *Красный флаг:* «на Core вообще ничего graphical не бывает».

**5. Как определить, в каком варианте установлен сервер, и можно ли его переконвертировать?**

Ответ: `InstallationType` в ветке `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion` или `(Get-ComputerInfo).WindowsInstallationType` — вернёт `Server` или `Server Core`. Конвертация официально поддерживается установкой/удалением компонентов `Server-Gui-Mgmt-Infra` и `Server-Gui-Shell` с указанием `-Source` на `\sources\sxs` [ПРОВЕРИТЬ актуальность для 2022], требует перезагрузки. На практике для критичных серверов предпочтительнее переустановка: конвертация оставляет лишние файлы и плохо документируется. На рабочем сервере — только с бэкапом и в окно обслуживания.

*Что хочет услышать интервьюер:* знание способа проверки и осторожность в изменениях. *Красный флаг:* «сейчас переключу одной командой, ничего страшного».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Server → Install and upgrade». Статья: *Server Core vs. Desktop Experience* — поиск: `server core vs desktop experience windows server 2022`.
- **learn.microsoft.com** — *Install roles, role services, and features by using Windows PowerShell cmdlets* и справочник по `Get-WindowsFeature` / `Install-WindowsFeature` — поиск: `install windowsfeature powershell server manager module`.
- **learn.microsoft.com** — *Server Core App Compatibility Feature on Demand (FOD)* — поиск: `server core app compatibility fod`.
- **learn.microsoft.com** — *DISM PowerShell cmdlets and DISM command-line options* (`/Get-Features`, `/Enable-Feature`) — поиск: `dism enable-feature online featurename`.
- **learn.microsoft.com** — *SConfig.cmd (Server Core)* — поиск: `sconfig server core options`.
- **learn.microsoft.com** — *Windows Server 2022 editions and licensing*; *Windows Server release information* (LTSC/SAC) — поиск: `windows server 2022 editions comparison`.
- **docs.vmware.com** — *Workstation: Using Snaphots* (снапшоты и откат на стенде) — поиск: `vmware workstation take snapshot revert`.
- **learn.microsoft.com** — *What's new in Windows Server 2022* — поиск: `what's new windows server 2022`.

Помеченные `[ПРОВЕРИТЬ]` детали (точное имя пакета AppCompat FOD, актуальность конвертации Core ↔ GUI для 2022, ключ `/english` у DISM, перечень служб ролей печати) сверяйте по выводу команд на своём стенде перед тем, как закреплять их в базе знаний.
