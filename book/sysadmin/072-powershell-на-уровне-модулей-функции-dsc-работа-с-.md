# PowerShell на уровне модулей: функции, DSC, AD, Hyper-V, отчёты и REST

## 1. Введение (зачем это нужно в реальной работе)

Тикет в пятницу, 17:40: **ivanov (бухгалтерия) / WIN11-CL01 / «Печатаем закрытие периода, половина документов не уходит на печать. И ещё: в `\\FS01\Obshchee` нет утреннего файла `ad-report.html`, я по нему сверяю отпуска. Завтра сдача.» / Срочность: высокая.**

Раньше ты чинил такие вещи руками: зашёл на FS01, дёрнул службу, зашёл на DC01, руками собрал список учёток в Excel. На этом уровне так работать нельзя — 50 сотрудников это ещё терпимо, а 500 уже нет. Дальше в работе постоянно встречается одно и то же: одна и та же последовательность команд повторяется каждый день, и её надо превратить в **функцию**, функцию — в **модуль**, состояние сервера — в **декларативную конфигурацию** (DSC), а отчёты и интеграции — в скрипты, которые ходят в AD по ADWS, в Hyper-V по CIM и в чужие системы по REST.

После главы ты умеешь: собрать собственный модуль с манифестом и версией, подписать его, опросить AD без «убийства» контроллера домена, описать состояние сервера в DSC и проверить дрейф, снять параметры Hyper-V, собрать HTML-отчёт по расписанию и дёрнуть REST API из PowerShell. Exchange мы разберём теоретически — на стенде «Вектор» его нет и не будет (см. шаг про Exchange в разделе 4).

## 2. Теория «под капотом»

**Модуль** — это папка (или один `.psm1`), внутри которой лежат функции/командлеты и, желательно, **манифест** `.psd1`: файл-паспорт с версией, автором, списком экспортируемых функций и зависимостями. Манифест — это контракт: если функции нет в `FunctionsToExport`, снаружи её не видно, даже если она есть в файле.

PowerShell ищет модули по путям из `$env:PSModulePath` (для 5.1 — `C:\Program Files\WindowsPowerShell\Modules` и `C:\Users\<user>\Documents\WindowsPowerShell\Modules`). Имя папки = имя модуля, внутри — подпапка версии или файлы напрямую.

**Как модуль дотягивается до инфраструктуры:**

- **AD** — модуль `ActiveDirectory` (RSAT) говорит не по LDAP напрямую, а через службу **ADWS** (Active Directory Web Services), TCP **9389**. Отсюда классическая ошибка «Unable to find a default server» — это не LDAP упал, это ADWS. Резервные порты: Kerberos 88, LDAP 389, LDAPS 636, global catalog 3268, RPC 135 + динамика.
- **Удалённое выполнение** — **WinRM**: TCP **5985** (HTTP) / **5986** (HTTPS). `Invoke-Command`, `New-PSSession`, `Enter-PSSession` работают через него. SMB 445 нужен для `\\FS01\...` и старых сценариев.
- **Hyper-V** — управление через **CIM/WMI** (namespace `root\virtualization\v2`), транспорт — DCOM (135 + динамика) или WinRM. Поэтому `Get-VM` можно вызывать с `-ComputerName`.
- **Exchange** — отдельный мир: `New-PSSession -ConfigurationName Microsoft.Exchange` к `/PowerShell/` (on-prem) или `Connect-ExchangeOnline` (Exchange Online, современная аутентификация).
- **REST** — обычный HTTPS/HTTP + JSON, `Invoke-RestMethod`.

**DSC** (Desired State Configuration) — декларативный слой: ты пишешь «файл должен быть, служба должна работать», PowerShell компилирует это в MOF-файл и применяет через **LCM** (Local Configuration Manager). Есть push (`Start-DscConfiguration`) и pull (сервер раздаёт конфигурации). Ключевое: DSC умеет не только применять, но и **сообщать о дрейфе** — `Test-DscConfiguration`.

```text
┌──────────── твой скрипт / Runbook ─────────────┐
│ Get-VektorInactiveUser -Days 90 | Export-Csv   │
└───────────────────┬────────────────────────────┘
                    │ вызов функции
┌───────────────────▼────────────────────────────┐
│ Модуль VektorTools (.psm1 + .psd1, версия)     │
└───────────────────┬────────────────────────────┘
                    │ внутри — командлеты
┌───────────────────▼────────────────────────────────────────┐
│ Движок PowerShell 5.1 / 7.x + провайдеры + CIM + HTTP      │
└──┬─────────────┬──────────────┬────────────┬───────────────┘
   │ ADWS 9389   │ WinRM 5985/6 │ CIM/DCOM   │ HTTPS 443/80
┌──▼───────┐ ┌───▼───────┐ ┌────▼──────┐ ┌───▼────────────┐
│ DC01     │ │ FS01      │ │ HV01      │ │ MON01 / PVE01  │
│ AD, DNS  │ │ DSC push  │ │ Hyper-V   │ │ REST (JSON)    │
└──────────┘ └───────────┘ └───────────┘ └────────────────┘
```

## 3. Аналогия из жизни

Мастерская. Модуль — это ящик инструментов с этикеткой: на этикетке (манифест) написано, что внутри и какой это версии, чтобы ты не схватил старый ящик, где ключ на 13 сломан. Функция — самодельная насадка, собранная из стандартных бит: снаружи одна рукоятка, внутри пять движений. DSC — это не «сделай», а «должно быть всегда»: как термостат, который не «включает котёл по команде», а держит температуру и сам сообщает, что она уехала. REST — это звонок в соседнюю мастерскую по телефону: ты не идёшь туда, ты диктуешь запрос и получаешь ответ по строгой форме. DOM — телефоны, факсы и прочие Email на месте.

Где аналогия ломается: термостат не спорит с тобой, а DSC-конфигурация может «воевать» с ручными правками — кто последний применился, тот и прав, и об этом легко забыть.

## 4. Практика и команды

**1. Осмотр модулей и путей**
```powershell
$env:PSModulePath -split ';'
Get-Module -ListAvailable ActiveDirectory, PSDesiredStateConfiguration |
    Select-Object Name, Version, Path
Import-Module ActiveDirectory -Force
Get-Command -Module ActiveDirectory | Measure-Object
```
`-ListAvailable` читает диск, а не сессию. Смотри, что модуль один и тот же не лежит в двух версиях — потом полдня ищешь, почему «не та» функция.

**2. Создание модуля и манифеста**
```powershell
$root = "$env:ProgramFiles\WindowsPowerShell\Modules\VektorTools"
New-Item -ItemType Directory -Path $root -Force
New-ModuleManifest -Path "$root\VektorTools.psd1" `
    -RootModule 'VektorTools.psm1' -ModuleVersion '1.0.0' `
    -Author 'adm.student' -Description 'Стенд Вектор: AD-отчёты' `
    -RequiredModules @('ActiveDirectory') `
    -FunctionsToExport @('Get-VektorInactiveUser','New-VektorOnboardingUser')
Test-ModuleManifest "$root\VektorTools.psd1" | Select-Object Name, Version
```
`-RequiredModules` — зависимости (при импорте подтянутся сразу). `Test-ModuleManifest` ловит опечатки в экспорте до того, как модуль попадёт к людям.

**3. Удалённая работа (WinRM)**
```powershell
Enable-PSRemoting -SkipNetworkProfileCheck -Force   # на DC01/FS01, один раз
$s = New-PSSession -ComputerName FS01 -Credential (Get-Credential VEKTOR\adm.student)
Invoke-Command -Session $s -ScriptBlock { Get-Service Spooler | Select-Object Status,StartType }
Remove-PSSession $s
```
`-SkipNetworkProfileCheck` нужен, потому что стендовая сеть часто «Public». В бою профиль сети должен быть DomainPrivate, а WinRM — только по 5986 с сертификатом.

**4. AD: неактивные учётки**
```powershell
Search-ADAccount -AccountInactive -TimeSpan '90.00:00:00' -UsersOnly -Server DC01 |
    Select-Object Name, SamAccountName, LastLogonDate
```
`-Server DC01` фиксирует контроллер — важно, когда их несколько: иначе разные DC дадут разные цифры (репликация). `TimeSpan` в формате «дни.часы:минуты:секунды».

**5. AD: группы и состав**
```powershell
Get-ADGroupMember -Identity 'GG-Buh' -Recursive |
    Select-Object Name, objectClass, SamAccountName
Get-ADGroup -Filter 'Name -like "GG-*"' -Properties Members |
    Select-Object Name, @{n='Count';e={$_.Members.Count}}
```
`-Recursive` разворачивает вложенные группы. `@{n=...;e=...}` — вычисляемое свойство, базовая техника для отчётов.

**6. RSAT на рабочей станции**
```powershell
Get-WindowsCapability -Online -Name 'Rsat*' | Where-Object Name -like '*ActiveDirectory*'
Add-WindowsCapability -Online -Name 'Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0'
Import-Module ActiveDirectory
Get-ADDomainController -Filter * | Select-Object Name, IPv4Address, IsGlobalCatalog
```
Точное имя пакета лучше брать из первой команды — `[ПРОВЕРИТЬ]` для конкретной сборки Win11.

**7. Hyper-V**
```powershell
Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart
Get-VMHost | Select-Object ComputerName, VirtualMachinePath
New-VMSwitch -Name 'Vektor-Internal' -SwitchType Internal
New-VM -Name 'LAB-VM01' -MemoryStartupBytes 1GB -NewVHDPath 'C:\VMs\LAB-VM01.vhdx' `
    -NewVHDSizeBytes 10GB -SwitchName 'Vektor-Internal'
Get-VM | Select-Object Name, State, Uptime
```
`-NewVHDPath` создаёт диск на лету. `Checkpoint-VM`/`Restore-VMSnapshot` — аналог снапшотов VMware внутри гостя.

**8. Exchange (на стенде недоступно — только шаблон для реальной среды)**
```powershell
# Exchange Online (тенант, современная аутентификация). В стенде «Вектор» Exchange НЕТ.
Connect-ExchangeOnline -UserPrincipalName adm.student@corp.vektor.lab
Get-EXOMailbox -ResultSize 100 | Select-Object DisplayName, PrimarySmtpAddress
Get-EXOMailboxStatistics -Identity ivanov | Select-Object TotalItemSize, ItemCount
Disconnect-ExchangeOnline -Confirm:$false

# on-prem Exchange (2016/2019): удалённый сеанс к /PowerShell/
$ex = New-PSSession -ConfigurationName Microsoft.Exchange `
      -ConnectionUri 'http://<exchange-fqdn>/PowerShell/' -Authentication Kerberos
Import-PSSession $ex -CommandName Get-Mailbox, Get-MailboxDatabase
```
`Get-EXO*` быстрее старых `Get-*` в EXO. Точные имена параметров в текущей версии — `[ПРОВЕРИТЬ]`.

**9. DSC: ресурсы и проверка состояния**
```powershell
Get-DscResource | Where-Object Module -eq 'PSDesiredStateConfiguration'
Get-DscLocalConfigurationManager | Select-Object ConfigurationMode, RefreshMode
Test-DscConfiguration -Detailed
```
В `-Detailed` смотри `InDesiredState` и `ResourcesNotInDesiredState`. Если пусто — дрейфа нет.

**10. Отчёт в CSV и HTML**
```powershell
$data = Search-ADAccount -AccountInactive -TimeSpan '60.00:00:00' -UsersOnly -Server DC01
$data | Select-Object Name, SamAccountName, LastLogonDate |
    Export-Csv 'C:\Reports\inactive.csv' -NoTypeInformation -Encoding UTF8
$html = $data | ConvertTo-Html -Title 'Неактивные учётки' `
    -PreContent "<h2>Стенд «Вектор», $(Get-Date -Format 'yyyy-MM-dd HH:mm')</h2>"
$html | Out-File 'C:\Reports\ad-report.html' -Encoding UTF8
```
`-NoTypeInformation` убирает мусорную первую строку в CSV. `-Encoding UTF8` обязателен, иначе Excel откроет кракозябры (в 5.1 это UTF-8 с BOM).

**11. REST: JSON-RPC к Zabbix на MON01**
```powershell
$body = @{ jsonrpc='2.0'; method='user.login'
           params=@{ user='Admin'; password='<пароль Zabbix>' }; id=1 } |
        ConvertTo-Json -Depth 4
$r = Invoke-RestMethod -Uri 'http://10.10.10.40/api_jsonrpc.php' -Method Post `
     -ContentType 'application/json' -Body $body
$token = $r.result
(Invoke-RestMethod -Uri 'http://10.10.10.40/api_jsonrpc.php' -Method Post `
    -ContentType 'application/json' -Body (@{ jsonrpc='2.0'; method='host.get'
    params=@{ output=@('hostid','host'); limit=3 }; auth=$token; id=2 } |
    ConvertTo-Json -Depth 4)).result
```
`ConvertTo-Json -Depth` — иначе вложенные хэши «схлопнутся» в строку. Версия Zabbix влияет на метод аутентификации — `[ПРОВЕРИТЬ]` под свою версию.

**12. Задача по расписанию**
```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' `
     -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Scripts\AdReport.ps1'
$t = New-ScheduledTaskTrigger -Daily -At 07:00
Register-ScheduledTask -TaskName 'Vektor-AdReport' -Action $a -Trigger $t `
    -User 'VEKTOR\adm.student' -RunLevel Highest
Get-ScheduledTaskInfo -TaskName 'Vektor-AdReport' | Select-Object LastTaskResult, LastRunTime
```
`LastTaskResult = 0` — успех. Всё остальное расшифровывай через `Get-Help about_...`/документацию планировщика.

**13. Подпись модуля**
```powershell
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject 'CN=Vektor Lab Code Signing' -CertStoreLocation 'Cert:\CurrentUser\My'
Set-AuthenticodeSignature -FilePath "$env:ProgramFiles\WindowsPowerShell\Modules\VektorTools\VektorTools.psm1" `
    -Certificate $cert
Get-AuthenticodeSignature "$env:ProgramFiles\WindowsPowerShell\Modules\VektorTools\VektorTools.psm1" |
    Select-Object Status, SignerCertificate
```
`Status = Valid` и сертификат в `TrustedPublisher`. В бою подпись — от внутреннего УЦ (или КриптоПро-совместимого), самоподписанный годится только для лабы.

## 5. Лабораторная работа

### Предусловия
- **DC01** (10.10.10.10): AD DS, DNS, DHCP, домен `corp.vektor.lab`, OU `Company\{Бухгалтерия,HR,Склад,IT}`, группы `GG-*`, учётки `ivanov`, `petrova`, `sidorov`, `adm.student`. Служба **ADWS** запущена.
- **FS01** (10.10.10.20): роли файлового сервера и печати, папки `Obshchee`, `Buh`, `HR`.
- **WIN11-CL01**: в домене, пользователь `ivanov`, есть права локального администратора у `adm.student`.
- **MON01** (10.10.10.40): Zabbix из трека L2, доступ в веб-интерфейс.
- **HV01** — новой ВМ на стенде нет. Для шага 7 создаём: Windows Server 2022 Evaluation, 4 ГБ RAM, 2 vCPU, адрес **10.10.10.36**, в настройках ВМ VMware — галочка «Virtualize Intel VT-x/EPT» (вложенная виртуализация). Шаг помечен как опциональный, если на хосте мало RAM.
- DSC и `ActiveDirectory` удобнее гонять в **Windows PowerShell 5.1** на DC01 — в PowerShell 7 командлетов DSC «из коробки» нет `[ПРОВЕРИТЬ]`.

### Снапшот
На каждой затрагиваемой ВМ (DC01, FS01, WIN11-CL01, MON01, HV01) — снапшот `before-sysadmin-072`. Из VMware Workstation: VM → Snapshot → Take Snapshot. Или из CMD на хосте:
```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" snapshot "C:\VMs\DC01\DC01.vmx" before-sysadmin-072
```
Путь к `vmrun.exe` уточни у себя. Снапшот работающего DC — crash-consistent; в продакшене так делать нельзя без кворумного/прикладного бэкапа и без понимания USN rollback.

### Шаги

1. **DC01.** Проверить базу: `Get-Service ADWS`, `Get-Module -ListAvailable ActiveDirectory`, `$env:PSModulePath`. Ожидаемо: служба Running, модуль есть, путь `C:\Program Files\WindowsPowerShell\Modules` в списке.
2. **DC01.** Создать папку модуля и файл `VektorTools.psm1` с двумя функциями из раздела 4 (п.2–3). Сохранить **как UTF-8 с BOM** — иначе PowerShell 5.1 прочитает кириллицу в DN как мусор. Ожидаемо: файл на месте, `Get-Content` показывает читаемый текст.
3. **DC01.** Сгенерировать манифест (раздел 4, п.2), выполнить `Test-ModuleManifest`. Ожидаемо: `Version 1.0.0`, две функции в `ExportedFunctions`.
4. **DC01.** `Import-Module VektorTools -Force`, затем `Get-Command -Module VektorTools` и `Get-Help Get-VektorInactiveUser -Full`. Ожидаемо: видны ровно две функции, справка подтянулась из комментариев. Прогнать `Get-VektorInactiveUser -Days 90 -Verbose`. Ожидаемо: список учёток или пустой список (если все логинились недавно).
5. **DC01.** Проверить создание учётки в режиме «примерка»: `New-VektorOnboardingUser -SamAccountName test.ps72 -GivenName Иван -Surname Тестов -Department IT -WhatIf`. Ожидаемо: строка «What if: Performing the operation...». Реальную учётку не создаём — она сломает модель стенда; создашь, когда придёт настоящий новичок.
6. **WIN11-CL01.** Установить RSAT-AD-PowerShell (раздел 4, п.6), выполнить `Get-ADUser ivanov -Properties LastLogonDate, MemberOf -Server DC01`. Ожидаемо: `MemberOf` содержит `GG-Buh`. Это демонстрация: админ работает с DC01 удалённо, а не логинится на него.
7. **HV01 (опционально).** Установить роль Hyper-V с перезагрузкой, создать внутренний коммутатор `Vektor-Internal`, ВМ `LAB-VM01` c VHDX 10 ГБ (раздел 4, п.7), затем `Get-VM | Select Name,State`. Ожидаемо: `LAB-VM01`, `State = Off`.
8. **FS01.** Проверить DSC: `Get-DscResource | Measure-Object`, `Test-DscConfiguration -Detailed`. Ожидаемо: `InDesiredState = True` (пока ничего не описано). Создать `C:\DSC\VektorFileServer.ps1`:
```powershell
Configuration VektorFileServer {
    Import-DscResource -ModuleName PSDesiredStateConfiguration
    Node localhost {
        File ReportDir {
            Ensure          = 'Present'
            Type            = 'Directory'
            DestinationPath = 'C:\Reports'
        }
        Service Spooler {
            Name        = 'Spooler'
            State       = 'Running'
            StartupType = 'Automatic'
        }
    }
}
```
Затем `. C:\DSC\VektorFileServer.ps1; VektorFileServer -OutputPath C:\DSC\VektorFileServer` и `Start-DscConfiguration -Path C:\DSC\VektorFileServer -Wait -Verbose -Force`. Ожидаемо: два ресурса применены без ошибок, `C:\Reports` создан.
9. **FS01.** Проверить дрейф: `Test-DscConfiguration -Detailed`, `Get-DscConfigurationStatus | Select Status, ResourcesInDesiredState`. Ожидаемо: `InDesiredState = True`.
10. **MON01 (из DC01 или WIN11-CL01).** Выполнить REST-запрос из раздела 4, п.11. Ожидаемо: токен в `$token`, в ответе `host.get` — 1–3 хоста Zabbix.
11. **DC01.** Собрать HTML-отчёт `C:\Reports\ad-report.html` (раздел 4, п.10), зарегистрировать задачу `Vektor-AdReport` (п.12), запустить вручную `Start-ScheduledTask -TaskName 'Vektor-AdReport'` и проверить `Get-ScheduledTaskInfo`. Ожидаемо: `LastTaskResult = 0`, файл обновился по времени.
12. **DC01.** Подписать модуль (раздел 4, п.13), проверить `Get-AuthenticodeSignature`. Ожидаемо: `Status = Valid`.
13. **Всё.** Записать в `stand-log.md`: что создано, где лежит, какая версия модуля, какой сертификат.

### Проверка
- [ ] `Get-Command -Module VektorTools` возвращает ровно `Get-VektorInactiveUser` и `New-VektorOnboardingUser`.
- [ ] `Test-ModuleManifest` на DC01 без ошибок, версия `1.0.0`.
- [ ] На WIN11-CL01 `Get-ADUser ivanov -Server DC01` отрабатывает без запроса модулей.
- [ ] `Test-DscConfiguration -Detailed` на FS01: `InDesiredState = True`, в `ResourcesInDesiredState` два ресурса.
- [ ] `C:\Reports\ad-report.html` существует и открывается в браузере без кракозябр.
- [ ] `Get-ScheduledTaskInfo -TaskName 'Vektor-AdReport'` → `LastTaskResult = 0`.
- [ ] REST-запрос к MON01 вернул непустой `result`.
- [ ] `Get-AuthenticodeSignature` на `.psm1` → `Valid`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01 / **Что написал:** «Печать встала: задания висят в очереди, принтер на FS01 молчит. И в `\\FS01\Obshchee` нет утреннего `ad-report.html` — я по нему сверяю отпуска. Закрытие периода, завтра сдача.» / **Срочность:** высокая.

**Как воспроизвести поломку (на FS01 и DC01):**
```powershell
Stop-Service Spooler -Force                      # FS01: «кто-то отключил печать»
# DC01: ломаем экспорт в манифесте
(Get-Content "$env:ProgramFiles\WindowsPowerShell\Modules\VektorTools\VektorTools.psd1") `
    -replace "FunctionsToExport = @\('Get-VektorInactiveUser','New-VektorOnboardingUser'\)", `
             "FunctionsToExport = @()" | Set-Content "$env:ProgramFiles\WindowsPowerShell\Modules\VektorTools\VektorTools.psd1"
Import-Module VektorTools -Force
Get-Command -Module VektorTools                  # пусто → скрипт отчёта падает
```
Затем `Start-ScheduledTask -TaskName 'Vektor-AdReport'` — задача «успешно» отработает с ошибкой в скрипте, файл не обновится.

**Диагностика (строго по порядку):**
1. FS01: `Get-Service Spooler` → `Stopped`. Первый вопрос: **кто** его остановил. `Get-WinEvent -LogName System -MaxEvents 200 | Where-Object {$_.Id -in 7034,7036,7040}` — ищем событие остановки службы и время.
2. FS01: `Test-DscConfiguration -Detailed` → `InDesiredState = False`, в `ResourcesNotInDesiredState` — `Service Spooler`. Значит, состояние уехало от описанного.
3. FS01: `Get-DscConfigurationStatus` → видно, когда последний раз применялось и с каким результатом.
4. DC01: `Get-ScheduledTaskInfo -TaskName 'Vektor-AdReport'` → `LastTaskResult` не 0 (или 0, но файла нет).
5. DC01: запустить скрипт руками — `powershell.exe -NoProfile -File C:\Scripts\AdReport.ps1`. Видим `The term 'Get-VektorInactiveUser' is not recognized`.
6. DC01: `Get-Command -Module VektorTools` → пусто. `Get-Content ...VektorTools.psd1 | Select-String FunctionsToExport` → `@()`.

**Решение:**
- FS01: не просто `Start-Service Spooler`, а `Start-DscConfiguration -Path C:\DSC\VektorFileServer -Wait -Verbose` — тогда состояние вернёт DSC, а не ты руками; дрейф зафиксирован в логе.
- DC01: вернуть `FunctionsToExport` с двумя функциями, `Import-Module VektorTools -Force` (или перезапустить сессию планировщика), затем `Start-ScheduledTask`, проверить `LastTaskResult = 0` и время файла.
- Причина: ручное вмешательство + правка манифеста без `Test-ModuleManifest` и без ревью. Лечится процессом (Git-flow, урок 74) и проверкой в CI (урок 80).

**Ответ пользователю:** «Иван, печать восстановлена, очередь должна разойтись в течение пары минут — перезапусти проблемный документ. Отчёт `ad-report.html` пересобран, лежит на прежнем месте с сегодняшней датой. Причина: на файловом сервере служба печати была отключена вне регламента, конфигурация возвращена автоматически; скрипт отчёта падал из-за ошибки в модуле, модуль исправлен. Чтобы такого не повторялось, на печать и на отчёт поставим проверку в мониторинге.»

**Закрытие заявки:** категория «печать / файловый сервис», приоритет «средний» (блокер бизнес-процесса), решение описано в двух строках, корневая причина — «отклонение конфигурации от описанной в DSC + регрессия в модуле», профилактика — алерт в Zabbix на `Spooler` и на возраст файла `ad-report.html` (Zabbix-триггер на `vfs.file.time.change`).

### Что изменилось на стенде
- **DC01:** папка `C:\Program Files\WindowsPowerShell\Modules\VektorTools` (файлы `VektorTools.psm1`, `VektorTools.psd1`, версия 1.0.0), функции `Get-VektorInactiveUser` и `New-VektorOnboardingUser`, сертификат `CN=Vektor Lab Code Signing` в `Cert:\CurrentUser\My` и `TrustedPublisher`, папка `C:\Reports`, файл `C:\Reports\ad-report.html`, задача планировщика `Vektor-AdReport` (ежедневно 07:00, от `VEKTOR\adm.student`), скрипт `C:\Scripts\AdReport.ps1`.
- **FS01:** применена DSC-конфигурация `VektorFileServer` (MOF в `C:\DSC\VektorFileServer`), DSC управляет папкой `C:\Reports` и службой `Spooler`.
- **WIN11-CL01:** установлен пакет RSAT `Rsat.ActiveDirectory.DS-LDS.Tools`.
- **HV01 (если создан):** 10.10.10.36, роль Hyper-V, внутренний коммутатор `Vektor-Internal`, ВМ `LAB-VM01` с VHDX 10 ГБ в `C:\VMs`.
- **MON01:** без изменений, только чтение через API.
- Модуль распространяется **только** через копирование в `ProgramFiles`, шары для модулей не используются (осознанно).

### Откат
```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" revertToSnapshot "C:\VMs\FS01\FS01.vmx" before-sysadmin-072
```
Или в GUI: VM → Snapshot → Snapshot Manager → выбрать `before-sysadmin-072` → Go To. Порядок: сначала FS01, потом DC01 (если DC откатываешь на работающем домене — откатывай и клиентов, иначе рассинхрон паролей машинных учёток). Запись в `stand-log.md` об откате — обязательна.

## 6. Ошибки новичков и безопасность

1. **`FunctionsToExport = '*'` в манифесте** → в модуль «утекают» вспомогательные функции, потом кто-то на них завяжется, и переименование ломает чужие скрипты. → Перечисляй явно и проверяй `Test-ModuleManifest`.
2. **`Get-ADUser -Filter * -Properties *` на весь домен в 9:00** → тяжёлый LDAP-запрос, DC «приседает»,Properties *» тянет десятки атрибутов. → Узкий `-Filter` (например `Enabled -eq $true`), конкретные `-Properties`, `-ResultPageSize`, запуск вне пиков.
3. **Хардкод пароля в скрипте** → пароль в Git/на шаре, ротация невозможна. → `Get-Credential`, Windows Credential Manager, `SecretManagement`, gMSA для задач планировщика. В лабе пароль `Lab#2026-vektor` — только лаба.
4. **Кириллица без BOM в `.psm1`** → DN вида `OU=Бухгалтерия,...` превращается в мусор, `New-ADUser` падает с невнятной ошибкой. → UTF-8 with BOM для 5.1.
5. **`Start-DscConfiguration` без `-Wait`** и без последующего `Test-DscConfiguration` → «применилось» на самом деле нет. → Всегда `-Wait -Verbose`, затем `Test-DscConfiguration -Detailed`.
6. **Ручные правки того, что описано в DSC** → через сутки дрейф и «оно само сломалось». → Правка идёт в конфигурацию, а не в сервер.
7. **`ConvertTo-Json` без `-Depth`** → вложенные параметры API «схлопываются» в строку, сервер отвечает ошибкой парсинга. → `-Depth 4` и выше.
8. **`ExecutionPolicy Bypass` в задаче планировщика как норма** → обход политики на всём, что запускается. → Подпись скриптов + `RemoteSigned`, `Bypass` — только на стенде и с комментарием в коде.

**Безопасность.** В боевой среде нельзя: запускать неподписанные модули от имени сервисной учётки с широкими правами; хранить модули на общей папке `\\FS01\Obshchee` (любой, кто имеет доступ, подменит код — это готовый способ эскалации); выдавать `adm.student` право «Domain Admin» для отчётов — отчётам нужна учётка с правом **чтения** каталога; гонять REST-запросы по HTTP с паролем в теле (только HTTPS и токен/сертификат); отключать WinRM-шифрование «чтобы работало». Отдельно: сертификат подписи — от внутреннего УЦ, приватный ключ не экспортируется, срок и отзыв контролируются.

## 7. Вопросы на собеседовании

**1. «Чем модуль отличается от скрипта и зачем манифест?»**
Ответ: скрипт — это последовательность действий «здесь и сейчас», модуль — переиспользуемый набор команд с контрактом. Манифест даёт версию, автора, список экспортируемых команд, зависимости и совместимость с версией PowerShell; он позволяет ставить модуль через репозиторий, обновлять и откатывать по версиям. Плюс это точка контроля: если функции нет в экспорте — её нет для потребителя.
*Что хочет услышать интервьюер:* понимание версионирования и управляемости, а не только «так удобнее».
*Красный флаг:* «манифест нужен, чтобы PowerShell не ругался».

**2. «Как вы выкатите модуль на 300 серверов и не положите прод?»**
Ответ: собираю `.nupkg`, публикую в приватный репозиторий (PowerShellGet/NuGet или внутренний GitLab-пакет-реестр), на серверы ставлю `Install-Module -Repository Internal -RequiredVersion X.Y.Z` под управлением Ansible/CI. Семантическое версионирование, изменения только мажорной версией ломают совместимость. Сначала тестовый сегмент, потом канареечная группа, потом все; у каждой версии — подпись и changelog; откат — установка предыдущей версии, а не «правка на месте».
*Интервьюер:* rollout, версионирование, откат.
*Красный флаг:* «скопирую по шаре через `Copy-Item` на все сразу».

**3. «DSC push и pull — в чём разница, и что делать с дрейфом?»**
Ответ: push — ты сам компилируешь MOF и применяешь `Start-DscConfiguration`; подходит для небольшого парка. Pull — сервер раздаёт конфигурации по расписанию, LCM сам периодически применяет и сообщает статус; масштабируется, но требует инфраструктуры (pull-сервер, сертификаты). Дрейф ловим `Test-DscConfiguration`, статус — `Get-DscConfigurationStatus`, событийный лог DSC. Важно: DSC управляет состоянием, но не оркестрацией порядка между серверами — для этого Ansible/Terraform.
*Интервьюер:* разница push/pull, LCM, границы применимости, что DSC сейчас активно замещается (Machine Configuration / DSC v3) — `[ПРОВЕРИТЬ]` под актуальный статус.
*Красный флаг:* «DSC — это скрипт, который ставит роли».

**4. «Как безопасно ходить в REST API из скрипта и где хранить токены?»**
Ответ: секреты — не в коде и не в Git: `SecretManagement` с хранилищем (или Windows Credential Manager / vault), для сервисных задач — gMSA или отдельная учётка с минимальными правами. Транспорт — только HTTPS с валидацией сертификата, на внутренние адреса — внутренний УЦ, а не «`-SkipCertificateCheck` везде». Токен живёт в переменной сессии, не пишется в лог; `Start-Transcript` для таких скриптов отключаем или чистим. Ротация и срок жизни токена — в регламенте.
*Интервьюер:* секреты, TLS, минимальные права.
*Красный флаг:* «положу пароль в скрипт и зашифрую `ConvertTo-SecureString -AsPlainText -Force`, это же шифрование».

**5. «Нужен отчёт по неактивным учёткам за 90 дней. Как сделаешь, чтобы не убить DC?»**
Ответ: `Search-ADAccount -AccountInactive -TimeSpan '90.00:00:00' -UsersOnly -Server DC01`, явно указываю контроллер домена, запускаю по расписанию ночью, результат в CSV/HTML на файловый сервер. Если контроллеров несколько — фиксирую один и помню про репликацию (`LastLogonDate` может отличаться). Кэширую результат, не гоняю `-Properties *`, ограничиваю набор атрибутов, при большом домене — `-ResultPageSize`. Отчёт складываю с датой, настраиваю алерт на «файл не обновился».
*Интервьюер:* бережное отношение к DC, воспроизводимость, мониторинг самого отчёта.
*Красный флаг:* «запущу `Get-ADUser -Filter *` в цикле по каждому пользователю».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «PowerShell»: статьи «about_Modules», «about_Functions_Advanced», «New-ModuleManifest», «about_Signing». Поиск: `PowerShell module manifest FunctionsToExport`.
- **learn.microsoft.com** — «PowerShell Desired State Configuration overview» и «Get started with Windows PowerShell Desired State Configuration». Поиск: `PowerShell DSC Get-DscConfiguration Test-DscConfiguration`. Актуальный статус DSC v3 / Machine Configuration — `[ПРОВЕРИТЬ]`.
- **learn.microsoft.com** — «Active Directory PowerShell» / `Search-ADAccount`, `Get-ADUser`. Поиск: `Active Directory module ADWS 9389 requirements`.
- **learn.microsoft.com** — «PowerShell remoting over WinRM», `about_Remote_Troubleshooting`, `Enable-PSRemoting`. Поиск: `about_Remote_Troubleshooting`.
- **learn.microsoft.com** — «Hyper-V PowerShell» / cmdlets `New-VM`, `New-VMSwitch`. Поиск: `Hyper-V PowerShell New-VM New-VHD`.
- **learn.microsoft.com** — «Exchange PowerShell» / «Connect to Exchange Online PowerShell», `Get-EXOMailbox`. Поиск: `Connect-ExchangeOnline EXO cmdlets modern authentication` — версии модуля и параметры `[ПРОВЕРИТЬ]`.
- **learn.microsoft.com** — «Invoke-RestMethod», «ConvertTo-Json». Поиск: `Invoke-RestMethod ConvertTo-Json depth`.
- **powershellgallery.com** — раздел про публикацию модулей (`Publish-Module`, приватный репозиторий). Поиск: `PowerShellGet Publish-Module internal repository`.
- **zabbix.com** (раздел Documentation → API) — JSON-RPC, `user.login`, `host.get`. Версия и метод аутентификации `[ПРОВЕРИТЬ]`: `Zabbix API user.login host.get`.
- **pve.proxmox.com** — «Proxmox VE API», аутентификация по тикету (`/api2/json/access/ticket`) для альтернативы Zabbix. Поиск: `Proxmox VE API access ticket`.
