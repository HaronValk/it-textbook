# PowerShell для администратора: функции, модули, работа с AD, WMI/CIM, удалённое выполнение

## 1. Введение (зачем это нужно в реальной работе)

Заявка #4127, автор — `ivanov`, рабочая станция WIN11-CL01, срочность «средняя»:
> «Каждую пятницу руками обхожу FS01, DC01 и свой ноут, смотрю свободное место и переписываю в Excel. Полчаса точно. А в понедельник FS01 вообще встал — на C: не осталось места, бухгалтерия не могла сохранить документы, и узнали мы об этом от них, а не от вас».

L1-инженер закрывает такую заявку руками: зашёл, посмотрел, записал. L2 обязан превратить это в команду, которую можно запустить одной строкой и повесить в планировщик. Именно об этом глава: как писать функции, упаковывать их в модули, тянуть данные из AD и WMI/CIM и выполнять всё это сразу на нескольких машинах по сети, не вставая с кресла.

После этой главы вы сможете: написать функцию с параметрами и валидацией, собрать из неё модуль, импортировать его на любой машине домена, вытащить данные из `Win32_LogicalDisk` и `Win32_Service` локально и удалённо, прогнать одну и ту же команду на DC01 и FS01 одной строкой и понять, почему у неё в ответе какие-то «Deserialized» объекты. Плюс разберётесь, почему у пользователя «вчера скрипт работал, а сегодня нет» — это самый частый L2-тикет по PowerShell.

Массовое создание учёток, отчёты по неактивным пользователям, bash-аналоги, Ansible и CI — в уроках 82–87, здесь только фундамент.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

PowerShell существует в двух редакциях, и путать их — источник половины проблем в поддержке:

- **Windows PowerShell 5.1** — встроен в Windows, работает на .NET Framework, исполняемый файл `powershell.exe`. Именно он стоит на DC01, FS01 и рабочих станциях стенда «из коробки».
- **PowerShell 7.x** — отдельный продукт на .NET, `pwsh.exe`, ставится на Windows и Linux. Старые модули в него переносятся не всегда (например, `Get-WmiObject` там нет вообще).

**Cmdlet** — это скомпилированная команда вида «Глагол-Существительное» (`Get-ADUser`, `Stop-Service`). Набор разрешённых глаголов фиксирован — `Get-Verb`. Идея конвейера: каждая команда что-то получает, делает **одну** вещь и передаёт дальше. Передаёт она **объекты .NET**, а не текст. Это ключевое отличие от bash, и на этом построено всё дальше.

**Провайдеры** делают вид, что данные — это диск: `C:`, `HKLM:`, `AD:`, `Cert:`, `WSMan:`. `Get-ChildItem AD:` покажет содержимое каталога, если загружен модуль ActiveDirectory.

**Модуль** — единица упаковки кода: скриптовый `.psm1`, манифест `.psd1` (версия, автор, что экспортировать), бинарный `.dll`. PowerShell ищет модули по путям из `$env:PSModulePath`; модуль подхватывается автоматически, если имя папки совпадает с именем модуля.

**Откуда берутся данные:**

- **CIM/WMI.** WMI (Windows Management Instrumentation) — реализация стандарта CIM в Windows. Классы `Win32_*` описывают почти всё: диски, службы, процессы, ОС. Исторически WMI работал по DCOM (RPC, порт 135 + динамические порты) — это до сих пор головная боль с файрволами. Современные командлеты `Get-CimInstance` по умолчанию идут по **WSMan** (WinRM), а DCOM можно включить опционально через `New-CimSessionOption -Protocol Dcom`.
- **ActiveDirectory.** Модуль RSAT общается не по LDAP, а с **ADWS** — службой Active Directory Web Services на контроллере домена, **TCP 9389**. Это важно знать: если LDAP у вас открыт, а ADWS нет, модуль не заработает. Кстати, по-настоящему быстрый фильтр — только `-Filter`; он уходит на сервер, а `Where-Object` после `-Filter *` тащит в сеть весь домен.
- **PowerShell Remoting.** Два канала: **WinRM** (SOAP/WS-Management, TCP **5985** — HTTP, **5986** — HTTPS) для Windows и **SSH** (TCP 22) в PowerShell 7 для Windows и Linux. Аутентификация в домене — Kerberos.

Схема:

```text
                  WIN11-CL01 (ваша консоль)
   ┌──────────────────────────────────────────────────────┐
   │  powershell.exe / pwsh.exe                           │
   │    cmdlet ──► объект ──► следующий cmdlet            │
   │    функция ──► модуль .psm1 / .psd1 (PSModulePath)   │
   └───────┬───────────────┬──────────────────┬───────────┘
           │               │                  │
       локально        AD (RSAT)         Remoting
           │               │                  │
   CIM ──► WMI ──► ядро ОС  ADWS TCP 9389    WinRM 5985/5986 (Kerberos)
       (DCOM 135/  dyn)        │                 │ SSH 22 (PS 7)
                               ▼                 ▼
                             DC01             FS01 / DC01 / UBU01
```

Что прилетает обратно по сети: объект сериализуется в XML (CLIXML), на вашей машине восстанавливается как `Deserialized.*`. Свойства читаются, а **методы недоступны** — коробка превратилась в фотографию коробки.

## 3. Аналогия из жизни

Компания «Вектор» — логистическая, и склад подходит идеально. Представьте конвейер: по ленте едут **коробки** (объекты), а не рассыпанный песок (текст). У ленты стоят грузчики — cmdlets; каждый берёт коробку, делает ровно одну операцию («взвесить», «наклеить этикетку») и кладёт обратно. Функция — это бригада с инструкцией: у неё есть входные параметры («сколько коробок, какой вес считать тревожным»), и она может вернуть новую коробку с готовым отчётом. Модуль — это контейнер с инструментами и описью, который вы можете выдать любому складу. `Import-Module` — распаковать контейнер на месте.

Удалённое выполнение — рация на другой склад: вы просите FS01 «пересчитай коробки и продиктуй опись». Вам диктуют данные, но **сами коробки не привозят** — вы получаете описание, по которому можно принять решение по телефону, но физически коробку не откроете. Это и есть десериализация.

Аналогия ломается на том, что в реальном складе коробки физически передаются из рук в руки, а в PowerShell объект внутри одной сессии — это **ссылка** на живой объект .NET, и её свойства могут измениться под вами, пока вы её передаёте по конвейеру.

## 4. Практика и команды

**1. Осмотреться: версия, команды, справка, свойства**

```powershell
$PSVersionTable
Get-Command -Module Microsoft.PowerShell.Management | Select-Object -First 5
Get-Help Get-ADUser -Examples
Get-Verb | Where-Object Verb -eq 'Get' | Measure-Object
Get-Service | Get-Member -MemberType Property
```

`$PSVersionTable` — первый вопрос при любом странном поведении: 5.1 или 7.x. `Get-Command` ищет, есть ли вообще такая команда и из какого она модуля. `Get-Help -Examples` — быстрее, чем гуглить. `Get-Member` показывает, с каким типом объекта вы работаете и какие у него свойства и методы.

**2. Конвейер объектов (никогда не `Format-Table` в середине)**

```powershell
Get-ChildItem C:\Windows\Logs -Filter *.log |
    Where-Object Length -gt 1MB |
    Sort-Object Length -Descending |
    Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}} |
    Export-Csv C:\Temp\logs.csv -NoTypeInformation -Encoding UTF8
```

`Where-Object Length -gt 1MB` — сокращённый синтаксис вместо `{ $_.Length -gt 1MB }`. `@{n=...;e=...}` — вычисляемое свойство (name/expression). `Format-Table` превращает объекты в текст: после него `Export-Csv` и `Sort-Object` уже не работают. Правило: **форматирование — последняя команда**.

**3. Продвинутая функция**

```powershell
function Get-VektorDiskReport {
    [CmdletBinding()]
    param(
        [Parameter(ValueFromPipeline = $true)]
        [string[]]$ComputerName = 'localhost',

        [ValidateRange(1,100)]
        [int]$WarningPercent = 85
    )
    process {
        foreach ($computer in $ComputerName) {
            try {
                $disks = Get-CimInstance -ClassName Win32_LogicalDisk `
                         -Filter 'DriveType=3' -ComputerName $computer -ErrorAction Stop
            } catch {
                Write-Warning "$computer : $($_.Exception.Message)"
                continue
            }
            foreach ($d in $disks) {
                $used  = [math]::Round((1 - ($d.FreeSpace / $d.Size)) * 100, 1)
                $state = if ($used -ge $WarningPercent) { 'WARN' } else { 'OK' }
                [pscustomobject]@{
                    Computer = $computer; Drive = $d.DeviceID
                    FreeGB   = [math]::Round($d.FreeSpace / 1GB, 1)
                    UsedPct  = $used; State = $state
                }
            }
        }
    }
}
```

`[CmdletBinding()]` — превращает функцию в «продвинутую»: появляются общие параметры (`-Verbose`, `-ErrorAction`, `-WhatIf`). `ValueFromPipeline` — можно подавать имена машин по конвейеру. `process {}` выполняется для каждого пришедшего объекта. `ValidateRange` не пустит `-WarningPercent 150`. В `catch` — `continue`, чтобы одна недоступная машина не рвала весь отчёт.

**4. Модуль: файл, манифест, путь**

```powershell
$dir = "$HOME\Documents\WindowsPowerShell\Modules\VektorTools"
New-Item -ItemType Directory -Path $dir -Force | Out-Null
notepad "$dir\VektorTools.psm1"     # вставляем функцию из п.3 + Export-ModuleMember -Function *
New-ModuleManifest -Path "$dir\VektorTools.psd1" -RootModule 'VektorTools.psm1' `
    -ModuleVersion '1.0.0' -Author 'adm.student' -Description 'Утилиты поддержки Вектор' `
    -FunctionsToExport @('Get-VektorDiskReport')
Import-Module VektorTools -Force
Get-Command -Module VektorTools
```

Имя папки **должно** совпадать с именем модуля, иначе автозагрузка не сработает. `$env:PSModulePath` — список путей поиска, можно дописать сетевую шару: `$env:PSModulePath += ';\\FS01\Obshchee\PS'`.

**5. Установка модулей из галереи**

```powershell
Find-Module -Name PSWindowsUpdate | Select-Object Name, Version
Install-Module -Name PSWindowsUpdate -Scope CurrentUser -Force -AllowClobber
```

`-Scope CurrentUser` не требует прав администратора. `-AllowClobber` разрешает перекрыть одноимённую команду. В PowerShell 7.4+ есть более новый `Install-PSResource`. Из РФ `powershellgallery.com` может быть недоступен — тогда легальный путь: внутренний репозиторий (файловая шара с `.nupkg` + `Register-PSRepository`) или вендорный пакет от поставщика. `[ПРОВЕРИТЬ]` актуальную доступность галереи.

**6. Active Directory через RSAT**

```powershell
Get-WindowsCapability -Name 'RSAT*' -Online | Where-Object State -eq 'NotPresent' | Select-Object Name
Add-WindowsCapability -Online -Name 'Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0'
Import-Module ActiveDirectory
Get-ADUser -Filter * -Properties Department | Select-Object -First 3 SamAccountName, Name, Enabled, Department
Get-ADGroupMember -Identity 'GG-Buh' -Recursive | Select-Object Name, objectClass
Get-ADOrganizationalUnit -Filter * | Select-Object Name, DistinguishedName
```

`-Filter *` — обязательный параметр (серверный фильтр), `-Properties` дотягивает то, чего нет в «лёгком» наборе по умолчанию. `-Recursive` разворачивает вложенные группы. Список OU полезен, чтобы не угадывать DN: для `Buh` это `OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab`; кириллицу в скриптах надёжнее брать из `DistinguishedName`, а не набирать руками.

**7. WMI/CIM: чтение и вызов методов**

```powershell
Get-CimInstance -ClassName Win32_OperatingSystem | Select-Object PSComputerName, LastBootUpTime, Version
Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID, FreeSpace, Size
$cs  = New-CimSession -ComputerName FS01
$svc = Get-CimInstance -CimSession $cs -ClassName Win32_Service -Filter "Name='Spooler'"
$svc | Invoke-CimMethod -MethodName StopService
$svc | Invoke-CimMethod -MethodName StartService
Remove-CimSession $cs
```

`Get-WmiObject` — устаревшая команда (в PowerShell 7 её нет). Методы вызываются через `Invoke-CimMethod`, причём **на объекте** (`-InputObject`), а не по имени класса — так безопаснее. В PowerShell 7 поведение параметра `-ComputerName` у CIM-команд отличается от 5.1 (DCOM не поддерживается), поэтому явная `New-CimSession` — универсальный вариант. `[ПРОВЕРИТЬ]` на вашей версии.

**8. Проверить и включить Remoting**

```powershell
Test-WSMan -ComputerName FS01
Enable-PSRemoting -Force            # на клиентской ОС; на Server WinRM обычно уже настроен
```

`Test-WSMan` показывает версию протокола и стек. `Enable-PSRemoting -Force` поднимает службу WinRM, создаёт слушатель 5985 и правила файрвола. На Windows Server WinRM включён по умолчанию, на Windows 10/11 — нет.

**9. Выполнить на нескольких машинах сразу**

```powershell
Invoke-Command -ComputerName DC01, FS01 -ScriptBlock {
    Get-CimInstance Win32_OperatingSystem | Select-Object PSComputerName, LastBootUpTime
} -ThrottleLimit 10 -AsJob
```

`-ThrottleLimit` по умолчанию 32 — столько сессий одновременно. `-AsJob` уводит работу в фон, результат забирается `Receive-Job`. Свойство `PSComputerName` добавляется автоматически: сразу видно, откуда ответ.

**10. Постоянная сессия и копирование файлов**

```powershell
$s = New-PSSession -ComputerName FS01
Invoke-Command -Session $s -ScriptBlock { Get-PSDrive C }
Copy-Item -Path .\report.ps1 -Destination C:\Temp\ -ToSession $s
Enter-PSSession -Session $s      # интерактивно, выход — exit
Remove-PSSession $s
```

`New-PSSession` экономит время при серии команд: аутентификация один раз. `Copy-Item -ToSession` / `-FromSession` — штатный способ гонять файлы, не поднимая SMB-шару. Не забывайте `Remove-PSSession`, иначе сессии копятся.

**11. Ошибки: ловить, а не игнорировать**

```powershell
$ErrorActionPreference = 'Stop'
try {
    Get-ADUser -Identity 'sidoroff' -ErrorAction Stop
} catch [Microsoft.ActiveDirectory.Management.ADIdentityNotFoundException] {
    Write-Warning "Учётка не найдена: $($_.Exception.Message)"
} catch {
    Write-Error "Другая ошибка: $($_.Exception.GetType().FullName)"
}
$LASTEXITCODE   # код возврата внешней .exe (ping, robocopy)
```

По умолчанию большинство ошибок **не** останавливают скрипт — он идёт дальше и делает вид, что всё хорошо. `-ErrorAction Stop` или `$ErrorActionPreference='Stop'` включают нормальную обработку. `$Error[0]` — последняя ошибка, `$LASTEXITCODE` — код возврата для внешних программ.

**12. Политика выполнения, подпись, журнал**

```powershell
Get-ExecutionPolicy -List
Get-AuthenticodeSignature .\report.ps1 | Select-Object Status, SignerCertificate
Get-Item .\report.ps1 -Stream * ;  Unblock-File .\report.ps1
Start-Transcript -Path C:\Temp\vektor-$(Get-Date -Format yyyyMMdd-HHmm).txt
```

`Get-ExecutionPolicy -List` показывает политику по областям: MachinePolicy (из GPO) → UserPolicy → Process → CurrentUser → LocalMachine, действует самая приоритетная. Значения: `Restricted`, `AllSigned`, `RemoteSigned`, `Unrestricted`, `Bypass`, `Undefined`. `Unblock-File` снимает метку «скачано из интернета» (поток `Zone.Identifier`), из-за которой `RemoteSigned` блокирует скрипт.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Готовый стенд L1: **DC01** (10.10.10.10, AD DS + DNS + DHCP, домен `corp.vektor.lab`), **FS01** (10.10.10.20, шара `\\FS01\Obshchee`), **WIN11-CL01** (рабочее место `ivanov`, в домене), **UBU01** (10.10.10.30, SSH).
- Учётка `adm.student` входит в `Domain Admins` (по итогам L1) — на ней работаем.
- Пароль стенда: `Lab#2026-vektor`.
- Если RSAT и доменные команды не настроены ни на одной машине — ставьте RSAT на WIN11-CL01 по шагу 2, этого достаточно.
- Проверка перед стартом: `nltest /dsgetdc:corp.vektor.lab` на WIN11-CL01 должен вернуть `\\DC01`.

### Снапшот

В VMware Workstation по каждой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-l2-081` — на **DC01, FS01, WIN11-CL01, UBU01**. Все четыре, потому что дальше мы будем выполнять код на каждой.

### Шаги

1. **WIN11-CL01.** Войти под `ivanov`, открыть Терминал с повышением (Win+X → «Терминал (администратор)»), в UAC ввести `VEKTOR\adm.student`. Проверить: `$PSVersionTable` → должно быть `5.1.xxxxx`; `Get-ExecutionPolicy -List` → `LocalMachine` = `RemoteSigned` (по умолчанию на клиенте).
2. **WIN11-CL01.** `Get-WindowsCapability -Name 'RSAT*' -Online | Where-Object State -eq 'NotPresent' | Select-Object Name` — увидите список непринятых компонентов. Затем `Add-WindowsCapability -Online -Name 'Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0'`. Ожидаемо: `Online: True`, `RestartNeeded: False`. (Нужен доступ к Windows Update или к FOD-источнику через `-Source`.)
3. **WIN11-CL01.** `Import-Module ActiveDirectory`; `(Get-Command -Module ActiveDirectory).Count` → несколько десятков команд. `Get-ADUser -Filter * -Properties Department | Select-Object -First 5 SamAccountName, Name, Enabled, Department` → в списке `adm.student`, `ivanov`, `petrova`, `sidorov`.
4. **WIN11-CL01.** `Get-ADOrganizationalUnit -Filter * | Select-Object Name, DistinguishedName` → выписать DN для `Бухгалтерия` и `Company`. Пригодится в уроке 82.
5. **WIN11-CL01.** Создать модуль: команды из п.4 раздела 4 (`New-Item`, `notepad` с функцией `Get-VektorDiskReport`, `New-ModuleManifest`). Проверить: `Import-Module VektorTools -Force; Get-Command -Module VektorTools` → одна ваша функция.
6. **WIN11-CL01.** Запустить: `Get-VektorDiskReport -ComputerName FS01, DC01 -WarningPercent 80 | Format-Table -AutoSize` → таблица с колонками `Computer, Drive, FreeGB, UsedPct, State`. Ожидаемо: по каждой машине строка `C:` со `State = OK`.
7. **WIN11-CL01.** CIM-сессия и вызов метода на FS01: `$cs = New-CimSession -ComputerName FS01`; `$svc = Get-CimInstance -CimSession $cs -ClassName Win32_Service -Filter "Name='Spooler'"`; `$svc.State` → `Running`; `$svc | Invoke-CimMethod -MethodName StopService`; снова `$svc.State` — объект нужно перезапросить, чтобы увидеть `Stopped`; `StartService`. Затем `Remove-CimSession $cs`.
8. **WIN11-CL01.** `Test-WSMan -ComputerName DC01` → `ProductVersion` и стек WSMan. Затем `Invoke-Command -ComputerName DC01, FS01 -ScriptBlock { Get-CimInstance Win32_OperatingSystem | Select-Object PSComputerName, LastBootUpTime }` → по строке на каждую машину с `PSComputerName`.
9. **WIN11-CL01.** Убедиться в десериализации: `Invoke-Command -ComputerName FS01 -ScriptBlock { Get-Service Spooler } | Get-Member` → тип `Deserialized.System.ServiceProcess.ServiceController`. Теперь `(Invoke-Command -ComputerName FS01 -ScriptBlock { Get-Service Spooler }).Stop()` → ошибка «Method invocation failed» — метод недоступен.
10. **WIN11-CL01.** `$s = New-PSSession -ComputerName FS01`; `Invoke-Command -Session $s -ScriptBlock { Get-PSDrive C | Select-Object Used, Free }`; `Copy-Item "$HOME\Documents\WindowsPowerShell\Modules\VektorTools\VektorTools.psm1" -Destination C:\Temp\ -ToSession $s` (папку `C:\Temp` создать заранее: `Invoke-Command -Session $s -ScriptBlock { New-Item -ItemType Directory C:\Temp -Force }`). `Remove-PSSession $s`.
11. **WIN11-CL01.** Обработка ошибок: `try { Get-ADUser -Identity 'sidoroff' -ErrorAction Stop } catch { $_.Exception.GetType().FullName }` → тип исключения `ADIdentityNotFoundException`. Убедиться, что скрипт не упал, а ветка `catch` отработала.
12. **WIN11-CL01.** Скопировать модуль на общую папку: создать `\\FS01\Obshchee\PS\VektorTools\` и положить туда `.psm1` и `.psd1`. Затем `$env:PSModulePath += ';\\FS01\Obshchee\PS'`, `Import-Module VektorTools -Force` — модуль грузится по сети. Записать изменения в `stand-log.md` и снять транскрипт: `Stop-Transcript`.

Опционально: **UBU01**, если на нём настроен SSH и стоит PowerShell 7 на WIN11-CL01 — `Enter-PSSession -HostName 10.10.10.30 -UserName <ваш локальный пользователь UBU01>`. Учётную запись Linux уточните по главам L1, я её здесь не выдумываю.

### Проверка

- [ ] `$PSVersionTable` на WIN11-CL01 показывает 5.1, `psversion` не пустой.
- [ ] RSAT установлен, `Get-Command -Module ActiveDirectory` возвращает команды, `Get-ADUser` видит `ivanov` и `petrova`.
- [ ] Модуль `VektorTools` импортируется и по локальному пути, и с `\\FS01\Obshchee\PS`.
- [ ] `Get-VektorDiskReport -ComputerName FS01, DC01` выдаёт по строке на диск C: обеих машин.
- [ ] `Invoke-CimMethod -MethodName StopService` реально останавливает спулер, `StartService` возвращает его в `Running`.
- [ ] `Invoke-Command -ComputerName DC01, FS01` возвращает два объекта с разными `PSComputerName`.
- [ ] `Get-Member` для результата `Invoke-Command` показывает префикс `Deserialized.`, а вызов метода на таком объекте падает.
- [ ] `Get-ExecutionPolicy -List` показывает, какая область задаёт политику, и вы понимаете приоритет.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Пользователь:** `ivanov` **/ ВМ:** WIN11-CL01 **/ Что написал:** «Запускаю наш скрипт отчёта по дискам — который вы мне вчера дали — и получаю: `не удается загрузить файл C:\Scripts\disk-report.ps1, так как выполнение сценариев отключено в этой системе`. Вчера работало! Мне до пятницы отчёт нужен.» **/ Срочность:** средняя.

**Как воспроизвести (намеренная поломка):** на WIN11-CL01 под администратором выполнить `Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy Restricted -Force`. Запустить `powershell -File C:\Scripts\disk-report.ps1` — получите ту же ошибку. (В реальной компании такую политику обычно прилетает GPO — тогда чинить нужно не на машине.)

**Диагностика по порядку:**

1. `Get-ExecutionPolicy -List` — смотрим все области. Видим `LocalMachine = Restricted` при `CurrentUser = RemoteSigned`. Действует более приоритетная (LocalMachine) — вот причина. Если сверху стоит `MachinePolicy = Restricted`, это GPO, и `Set-ExecutionPolicy` на клиенте не поможет.
2. Проверить, откуда скрипт: локальный диск или шара. `Test-Path C:\Scripts\disk-report.ps1`. Если скрипт лежит на `\\FS01\Obshchee`, `RemoteSigned` его заблокирует без подписи — это другая ветка.
3. `Get-AuthenticodeSignature C:\Scripts\disk-report.ps1` → `Status: NotSigned`. Значит, вариант «просто поставить AllSigned» не подходит.
4. `Get-Item C:\Scripts\disk-report.ps1 -Stream *` → если есть поток `Zone:Identifier`, файл скачан из интернета. Лечится `Unblock-File`.
5. `Get-Content C:\Scripts\disk-report.ps1 -TotalCount 5` — заодно убедиться, что содержимое не подменили (в реальной работе — сверять хеш с эталоном).

**Решение:** для рабочей станции поднять `Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy RemoteSigned -Force` (это дефолт Windows-клиента) и проверить запуск. Если политика приходит из GPO — не трогать клиент, а идти к тому, кто владеет GPO: Computer Configuration → Administrative Templates → Windows Components → Windows PowerShell → Turn on Script Execution. Если скрипт берётся с шары — либо `Unblock-File`, либо подписать его (корпоративный сертификат + `Set-AuthenticodeSignature`) и оставить `AllSigned`.

**Ответ пользователю:** «Иван, причину нашёл: на машине была жёсткая политика выполнения скриптов (`Restricted`), поэтому ваш файл не запускался — сам скрипт цел. Вернул стандартную политику `RemoteSigned`, отчёт запускается, проверил у вас на WIN11-CL01. Если снова появится — напишите, разберёмся, откуда прилетает настройка.»

**Закрытие заявки:** категория «ПО / политики безопасности», решение «ExecutionPolicy LocalMachine Restricted → RemoteSigned», приложить вывод `Get-ExecutionPolicy -List` до и после, отметить в базе знаний типовой сценарий.

### Что изменилось на стенде

- **WIN11-CL01:** установлен компонент RSAT `Rsat.ActiveDirectory.DS-LDS.Tools`, появился модуль `ActiveDirectory`.
- **WIN11-CL01:** создан модуль `VektorTools` (`C:\Users\<профиль>\Documents\WindowsPowerShell\Modules\VektorTools\VektorTools.psm1` + `.psd1`), функция `Get-VektorDiskReport` v1.0.0.
- **FS01:** созданы папки `C:\Temp` и `\\FS01\Obshchee\PS\VektorTools\` (центральная копия модуля).
- **WIN11-CL01:** в текущей сессии расширен `$env:PSModulePath` путём `\\FS01\Obshchee\PS`.
- **FS01:** служба `Spooler` перезапущена (остановлена и запущена через CIM).
- **WIN11-CL01:** политика выполнения скриптов `LocalMachine` изменена на `RemoteSigned` (в рамках «Сломай и почини»).
- Создан файл транскрипта `C:\Temp\vektor-<дата>.txt`.

### Откат

Если что-то пошло не так: в VMware Workstation выбрать ВМ → **VM → Snapshot → Snapshot Manager** → выделить `before-l2-081` → **Go To**. Сделать это на всех четырёх ВМ (DC01, FS01, WIN11-CL01, UBU01). Откат вернёт и RSAT, и модуль, и политику — стенд будет ровно в состоянии до урока. Не забудьте после отката дописать в `stand-log.md` строку «откат к before-l2-081».

## 6. Ошибки новичков и безопасность

1. **`Format-Table` в середине конвейера** → объект превращается в текст, `Sort-Object`, `Export-Csv`, `Where-Object` после него работают неправильно или падают. → Форматирование всегда последним; для промежуточных шагов — `Select-Object`.
2. **`Get-ADUser -Filter * | Where-Object {...}`** → в память и по сети тянется весь домен, на 50 сотрудниках незаметно, на 5000 — положит скрипт и нагрузит контроллер. → Фильтровать серверно через `-Filter`, `Where-Object` только для добивки.
3. **`Get-WmiObject`** → устаревшая команда, в PowerShell 7 отсутствует, DCOM-порты часто закрыты. → `Get-CimInstance` + `New-CimSession`.
4. **Скрипт «молча врёт»** → при ошибке `Remove-Item` или `Get-CimInstance` скрипт идёт дальше и пишет «успех». → `-ErrorAction Stop` внутри `try/catch`, проверка `$?` и `$LASTEXITCODE`.
5. **`Write-Host` внутри функции** → вывод не попадает в конвейер, его нельзя ни перехватить, ни сохранить. → Возвращать `[pscustomobject]`, а сообщения — через `Write-Verbose`/`Write-Warning`.
6. **Пароли и токены прямо в `.ps1`** → скрипт уезжает в шару, пароль уезжает вместе с ним. → Секреты через `Get-Credential`, Windows Credential Manager, хранилище из урока 76; в идеале — gMSA для сервисных задач.
7. **`-Force` и `Install-Module` из галереи без разбора** → в ваш сеанс `adm.student` заезжает чужой код с полными правами. → Ставить только из внутреннего репозитория, проверять издателя и подпись (`Get-AuthenticodeSignature`), тестировать на тестовом хосте.
8. **Remoting по IP и с `-Credential` в скрипте** → Kerberos не работает, падает на NTLM/TrustedHosts, а пароль в открытом виде. → Обращаться по DNS-имени (`FS01`), использовать доменную аутентификацию.

**Безопасность.** `ExecutionPolicy` — **не** механизм безопасности: это защита от случайного запуска, и любой может выполнить `-ExecutionPolicy Bypass` в своей сессии. В боевой среде политикой не ограничиваются: включают **Script Block Logging** (событие 4104 в журнале `Microsoft-Windows-PowerShell/Operational`) и **Module Logging** через GPO, чтобы видеть, что именно исполнялось, даже если скрипт подписан. Модули и скрипты подписывают, применяют `AllSigned` там, где это оправдано, а для делегированных задач настраивают **JEA** (`New-PSSessionConfigurationFile` + `Register-PSSessionConfiguration`), чтобы оператор первой линии мог перезапустить спулер, но не мог прочитать хеши паролей домена. Отдельно: `Enable-PSRemoting` открывает WinRM на всю сеть — в продакшене это делают через GPO и ограничивают правило файрвола подсетью администрирования, а не `Any`. `-Credential` в открытом виде в скрипте — увольнительная статья, а не «удобно для теста».

## 7. Вопросы на собеседовании

**1. Чем `Get-WmiObject` отличается от `Get-CimInstance`?**

`Get-WmiObject` — старый командлет Windows PowerShell, работающий через DCOM (RPC 135 и динамические порты), не поддерживает CIM-сессии и отсутствует в PowerShell 7. `Get-CimInstance` — современный, по умолчанию использует WSMan/WinRM (один порт, проще с файрволами), поддерживает `New-CimSession`, выбор протокола (`New-CimSessionOption -Protocol Dcom`) и вызов методов через `Invoke-CimMethod`. Классы те же — `Win32_LogicalDisk`, `Win32_Service`.
*Что хочет услышать:* понимание, что CIM — стандарт, WMI — реализация, и знание про DCOM/WSMan.
*Красный флаг:* «это одно и то же, просто синонимы».

**2. Как работает PowerShell Remoting, какие порты нужны и что такое «второй прыжок»?**

Remoting идёт через WinRM: TCP 5985 (HTTP) или 5986 (HTTPS), в домене аутентификация Kerberos, объекты сериализуются в CLIXML. На клиенте включается `Enable-PSRemoting`, проверяется `Test-WSMan`. В PowerShell 7 есть транспорт SSH (22). Второй прыжок (double hop): с WIN11-CL01 я захожу `Invoke-Command` на FS01, а оттуда пытаюсь обратиться к `\\FS01\Obshchee` на третьей машине — Kerberos не передаёт мои учётные данные дальше, получаю 401. Решается делегированием (constrained delegation), `CredSSP` (небезопасно, только для тестов) или `-Authentication Kerberos` с правильно настроенным SPN.
*Что хочет услышать:* порты, Kerberos, понимание ограничения и хотя бы один легальный способ обхода.
*Красный флаг:* «включу CredSSP везде, так проще».

**3. Чем функция отличается от скрипта и от модуля? Как сделать функцию с поддержкой конвейера?**

Функция живёт в текущей сессии, скрипт — файл `.ps1`, который запускается в своей области видимости и умирает после выполнения, модуль — упакованный набор функций (`.psm1` + манифест `.psd1`), который импортируется и переиспользуется, у него есть версия и явный список экспорта. Для конвейера: `[CmdletBinding()]`, параметр с `[Parameter(ValueFromPipeline=$true)]`, блок `process {}` — тогда функция вызывается для каждого объекта по одному, а не получает весь массив.
*Что хочет услышать:* `begin/process/end`, `CmdletBinding`, `Export-ModuleMember`.
*Красный флаг:* «модуль — это просто папка со скриптами, разницы нет».

**4. ExecutionPolicy — это про безопасность? Какие значения, как легально обойти?**

Нет, это защита от случайного запуска, а не граница безопасности: пользователь с правами может поставить `Bypass` в своей сессии. Значения: `Restricted`, `AllSigned`, `RemoteSigned`, `Unrestricted`, `Bypass`, `Undefined`. Приоритет областей: MachinePolicy → UserPolicy → Process → CurrentUser → LocalMachine. Легально: `RemoteSigned` + `Unblock-File` для локально скачанных файлов, подпись корпоративным сертификатом и `AllSigned` в чувствительных сегментах, политика через GPO. Реальная безопасность — Script Block Logging (4104), Constrained Language Mode, JEA, AppLocker/WDAC.
*Что хочет услышать:* «не безопасность» и знание приоритета областей.
*Красный флаг:* «поставлю Bypass на весь домен, чтобы не мешало».

**5. Почему объект, вернувшийся из `Invoke-Command`, ведёт себя странно — нет методов?**

Он десериализован: по сети ушёл XML (CLIXML), обратно пришёл `Deserialized.System.…` — свойства читаются, методы нет. Решения: выполнить метод **на удалённой стороне** внутри `-ScriptBlock` (`{ (Get-Service Spooler).Stop() }`), либо использовать `Invoke-CimMethod` на CIM-сессии, либо `Enter-PSSession` и работать интерактивно. Иногда помогает повторный каст, но только если тип есть на локальной машине.
*Что хочет услышать:* понимание сериализации и того, что логику нужно переносить на удалённую сторону.
*Красный флаг:* «надо перезагрузить машину, чтобы методы появились».

## 8. Источники (официальная документация)

- **learn.microsoft.com**, раздел «PowerShell» → «PowerShell 7.4 documentation» и «Windows PowerShell 5.1». Поиск: `PowerShell about_Functions_Advanced`, `about_Functions_CmdletBindingAttribute`.
- **learn.microsoft.com**, «PowerShell Remoting» — статьи `about_Remote`, `about_Remote_Troubleshooting`, `about_PSRemoting`. Поиск: `PowerShell remoting WinRM 5985 kerberos about_Remote`.
- **learn.microsoft.com**, справочник `CimCmdlets` — `Get-CimInstance`, `New-CimSession`, `Invoke-CimMethod`. Поиск: `Get-CimInstance documentation New-CimSession`.
- **learn.microsoft.com**, «Active Directory module for Windows PowerShell» и «Remote Server Administration Tools». Поиск: `Active Directory module RSAT Add-WindowsCapability Rsat.ActiveDirectory`.
- **learn.microsoft.com**, «about_Execution_Policies» и «about_Logging_Windows» (Script Block Logging, Event ID 4104). Поиск: `about_Execution_Policies PowerShell script block logging 4104`.
- **learn.microsoft.com**, «Just Enough Administration (JEA)». Поиск: `JEA New-PSSessionConfigurationFile Register-PSSessionConfiguration`.
- **powershellgallery.com** — официальная галерея модулей PowerShell; из РФ доступность может быть ограничена `[ПРОВЕРИТЬ]`, легальная альтернатива — внутренний репозиторий (`Register-PSRepository`) или вендорный пакет.
- **github.com/PowerShell** — исходный код и релизы PowerShell 7; `[ПРОВЕРИТЬ]` актуальную версию и порядок установки в вашей среде.
