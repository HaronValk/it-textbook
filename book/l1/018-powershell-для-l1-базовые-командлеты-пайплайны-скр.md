# PowerShell для L1: базовые командлеты, пайплайны и скрипты автоматизации рутины

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 9:12. В очереди заявок тикет:

> **Тема:** Не печатает и не открывается папка
> **От:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Текст:** «Добрый день! Третий день не печатается отчёт из 1С — отправляю, а в очереди висит. Сегодня ещё и папка \\FS01\HR не открывается, пишет "нет доступа". Отчёт нужен до обеда, помогите пожалуйста».
> **Срочность:** средняя

Классический L1-тикет, где надо проверить сразу три вещи: служба печати, доступ к сетевой папке, место на диске (частая причина «очередь висит»). Через графический интерфейс это десять минут кликов. Через PowerShell — три команды и сорок секунд.

PowerShell встречается в работе L1 постоянно: посмотреть, какие службы упали на машине пользователя, найти старые файлы, которые забили диск, выгрузить список учётных записей, проверить, что скрипт входа не сломался, автоматизировать обход десяти рабочих мест. Плюс на собеседовании L1 почти всегда спрашивают «чем `Get-Process` отличается от `tasklist`» и «что такое пайплайн».

После этой главы ты должен уметь: читать справку по любому командлету, строить конвейер из 3–4 команд, отличать объект от текста, писать простой скрипт `.ps1` с параметрами, безопасно запускать чужие скрипты и понимать, что делает `ExecutionPolicy`.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Что такое PowerShell.** Это одновременно три вещи: интерактивная оболочка (как CMD), язык сценариев (как bat, только нормальный) и набор модулей-библиотек. Работает поверх .NET. Есть две ветки:

- **Windows PowerShell 5.1** — встроена в Windows 10/11 и Server 2022, запускается как `powershell.exe`. Именно с ней ты будешь работать на стенде, она стоит по умолчанию.
- **PowerShell 7.x** — отдельный продукт, `pwsh.exe`, ставится дополнительно, кроссплатформенный (есть под Linux, в том числе под UBU01). На стенде L1 не обязателен, знать о нём надо.

**Командлет (cmdlet).** Название всегда по схеме `Глагол-Существительное`: `Get-Service`, `Stop-Process`, `Test-Path`. Глаголы берутся из одобренного списка (Get, Set, New, Remove, Start, Stop, Restart, Test, Enable, Disable, Import, Export, Invoke, Out), поэтому догадаться о названии реально. `Get-Command` показывает, что вообще доступно.

**Главное отличие от CMD — объекты.** CMD передаёт между командами текст: `ipconfig | find "IPv4"` — это поиск подстроки в строке. PowerShell передаёт объекты .NET — у каждого есть свойства (`Name`, `Status`, `StartType`) и методы. Ты фильтруешь не строку, а свойство. Поэтому `Where-Object Status -eq 'Stopped'` работает, а поиск по тексту дал бы мусор.

**Алиасы и внешние программы.** `gci` = `Get-ChildItem`, `?` = `Where-Object`, `sv` = `Set-Variable`. Это удобно руками, но в скриптах так писать нельзя (почему — в разделе 6). Отдельно есть внешние `.exe`: `ping.exe`, `ipconfig.exe`, `sfc.exe` — PowerShell их просто запускает, и на выходе получается текст, а не объект.

**Пайплайн (конвейер)** — символ `|`. Он передаёт объект из левой команды в правую:

```text
Get-Service | Where-Object Status -eq 'Stopped' | Select-Object Name,DisplayName | Sort-Object Name
     │                │                                    │                        │
 получаем          фильтруем: оставляем              оставляем только          сортируем
 все службы        только остановленные              два свойства              по имени
 (объекты)         (объекты)                         (объекты)                 (объекты)
```

Ключевой момент: до самого конца по конвейеру идут **объекты**. Форматтеры (`Format-Table`, `Format-List`) превращают объект в текст для показа — и после них пайплайн ломается.

**Провайдеры (PSDrive).** PowerShell умеет ходить не только по диску. `Get-PSDrive` покажет `C:`, `HKLM:` (реестр), `Env:` (переменные среды), `Cert:` (сертификаты). Поэтому `Get-ChildItem HKLM:\SOFTWARE` работает так же, как `Get-ChildItem C:\Windows`.

**Откуда берутся данные.** Под капотом PowerShell дергает разные подсистемы Windows:

```text
┌──────────────── PowerShell: оболочка + язык + модули ────────────────┐
│ Get-Service   Get-Process   Get-CimInstance   Get-ChildItem   Get-WinEvent │
└──────┬─────────────┬──────────────┬───────────────┬──────────────┬───┘
       │             │              │               │              │
   SCM (службы)  ядро/процессы   CIM/WMI         NTFS           журналы
       │             │              │               │           (EVTX)
       └──────────── Windows API / .NET Runtime ────┴──────────────┘
                              │
                      ядро Windows (ntoskrnl)
```

**Сетевые порты, которые всплывут в работе L1:**

| Что делаем | Порт / протокол |
|---|---|
| Открыть `\\FS01\Obshchee` | TCP 445 (SMB) |
| `Get-CimInstance -ComputerName WIN10-CL02` | TCP 135 (RPC) + динамические порты |
| `Invoke-Command -ComputerName ...` (Remoting) | TCP 5985 (HTTP) / 5986 (HTTPS), WinRM |
| Разрешение имён `FS01` → IP | UDP/TCP 53 (DNS) |
| Модуль `ActiveDirectory` | TCP 389 (LDAP), 636 (LDAPS) |

**ExecutionPolicy** — политика выполнения скриптов. `Restricted` (по умолчанию на клиентских ОС) запрещает `.ps1` вообще. `RemoteSigned` разрешает локальные скрипты, а скачанные из интернета — только с подписью. Политика — это защита от случайного запуска, **не** антивирус и не средство безопасности: обойти её тривиально.

## 3. Аналогия из жизни

Представь сортировочный центр «Вектор». По ленте едут коробки. У каждой есть накладная со свойствами: номер, вес, город, статус. Сотрудники не переписывают коробки в блокнот и не передают друг другу записки — они передают **сами коробки** дальше по ленте и на каждой станции смотрят нужное свойство. Один контролёр отбирает только коробки со статусом «остановлена» (`Where-Object`), второй оставляет на коробке только две наклейки — номер и город (`Select-Object`), третий ставит их по порядку (`Sort-Object`), четвёртый пересчитывает (`Measure-Object`). В конце упаковщик наклеивает итоговую этикетку для отчёта (`Format-Table`), и вот тут коробка превращается в плоскую бумажку — дальше по ленте её уже не перебрать по весу. Где аналогия перестаёт работать: настоящая коробка одна, а объект PowerShell можно «открыть» и у него бывают вложенные свойства и методы — можно, например, вызвать `Stop()` прямо у объекта службы.

## 4. Практика и команды

Все примеры — с WIN11-CL01, Windows 11. Запускать PowerShell: правый клик по «Пуск» → **Терминал** (в Windows 11) или **Windows PowerShell**. Для действий, меняющих систему, — «Терминал (администратор)».

**1. Что за версия и где я**

```powershell
$PSVersionTable
Get-Location
```

`$PSVersionTable` — хеш-таблица с версией. Смотри `PSVersion` (5.1 или 7.x) и `PSEdition` (Desktop/Core). `Get-Location` — текущий каталог (`pwd` — алиас). Зачем: скрипт, написанный под 7.x, может не работать в 5.1.

**2. Как узнать команду и её параметры**

```powershell
Get-Command -Noun Service
Get-Help Get-Service -Examples
Get-Help Get-Service -Parameter Name
Get-Member -InputObject (Get-Service -Name Spooler)
```

`Get-Command -Noun Service` — все командлеты, работающие со службами. `Get-Help -Examples` — только примеры, самый быстрый способ. `Get-Member` (`gm`) — какие свойства и методы есть у объекта; это половина всей диагностики. Сокращённый вывод `Get-Member`:

```text
   TypeName: System.ServiceProcess.ServiceController

Name                MemberType    Definition
----                ----------    ----------
Name                Property      string Name {get;}
Status              Property      System.ServiceProcess.ServiceControllerStatus Status {get;}
Stop                Method        void Stop()
```

Что смотреть: `Status` — свойство (значение), `Stop()` — метод (действие). Если `Update-Help` не сработал (нет интернета) — справка будет неполной, это нормально, `Get-Help -Examples` обычно есть из коробки.

**3. Службы — самая частая проверка L1**

```powershell
Get-Service -Name Spooler
Get-Service | Where-Object Status -eq 'Stopped' | Select-Object Name, DisplayName
Get-Service -Name Spooler -DependentServices
```

`-Name` принимает маску (`Get-Service -Name 'win*'`). `-DependentServices` — какие службы зависят от этой (важно: останавливать Spooler при живой очереди — так себе идея). Вывод второй команды: список остановленных служб с человекочитаемыми именами. Смотреть: нет ли среди них `Print Spooler`, `Workstation`, `DNS Client`.

**4. Процессы: кто съел процессор**

```powershell
Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Name, Id, CPU, WS
```

`Sort-Object CPU -Descending` — по убыванию процессорного времени; `-First 5` — топ-5; `WS` — Working Set, объём памяти в байтах. Смотреть: не сидит ли в топе `SearchIndexer`, `MsMpEng` (антивирус), `OUTLOOK` на 3 ГБ.

**5. Диск: свободное место**

```powershell
Get-Volume | Where-Object DriveType -eq 'Fixed'
Get-CimInstance -Filter "DeviceID='C:'" -ClassName Win32_LogicalDisk |
    Select-Object DeviceID, @{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}}
```

`Get-Volume` — современный командлет (Windows 8/2012+), отдаёт `SizeRemaining`. `Get-CimInstance` — «старый» путь через WMI, работает и на удалённой машине через `-ComputerName`. Второй пример показывает вычисляемое свойство (`@{n=...;e={...}}`): переводим байты в гигабайты. Порог для тревоги на рабочей станции — меньше 10 ГБ свободно.

**6. Проверка доступности: путь и порт**

```powershell
Test-Path \\FS01\Obshchee
Test-Path \\FS01\HR
Test-NetConnection -ComputerName FS01 -Port 445
Test-Connection -ComputerName DC01 -Count 2
```

`Test-Path` возвращает `True`/`False` — есть ли путь (для UNC-пути проверяет и доступ). `Test-NetConnection` (`tnc`) — пинг + проверка TCP-порта, ключевое для диагностики «папка не открывается». Смотреть `TcpTestSucceeded: True`. `Test-Connection` — аналог `ping`, но с объектами.

**7. Файлы: поиск и подсчёт**

```powershell
Get-ChildItem -Path $env:TEMP -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object LastWriteTime -lt (Get-Date).AddDays(-30) |
    Measure-Object -Property Length -Sum
```

`-Recurse` — с подкаталогами, `-File` — только файлы, `-ErrorAction SilentlyContinue` — не сыпать красным на недоступные папки. `(Get-Date).AddDays(-30)` — дата 30 дней назад. `Measure-Object -Sum` даёт `Count` и `Sum` (в байтах). Смотреть: `Count` и `Sum/1GB`.

**8. Журналы событий**

```powershell
Get-WinEvent -LogName System -MaxEvents 50 |
    Where-Object LevelDisplayName -eq 'Error' |
    Select-Object TimeCreated, Id, ProviderName, Message
Get-WinEvent -FilterHashtable @{LogName='System'; Level=2; StartTime=(Get-Date).AddDays(-1)} -ErrorAction SilentlyContinue |
    Group-Object ProviderName | Sort-Object Count -Descending
```

`Get-WinEvent` — предпочтительный способ (в PowerShell 7 `Get-EventLog` вообще убрали). `-FilterHashtable` фильтрует на стороне системы, это в разы быстрее, чем `Where-Object` после. `Level=2` — ошибка, `3` — предупреждение. `Group-Object` показывает, какой источник сыпет ошибками чаще всех — сразу видно виновника.

**9. Принтеры и перезапуск службы**

```powershell
Get-Printer | Select-Object Name, PrinterStatus, JobCount
Get-PrintJob -PrinterName 'HP-LJ-M404'
Restart-Service -Name Spooler -Force
```

`Get-Printer` — модуль `PrintManagement`. `Get-PrintJob` — что висит в очереди. `Restart-Service` требует прав администратора; `-Force` останавливает зависимые службы. Через GUI: «Параметры → Bluetooth и устройства → Принтеры и сканеры», службы — `services.msc`.

**10. Операторы конвейера, которые нужны каждый день**

```powershell
Get-Service | Where-Object { $_.Status -eq 'Running' -and $_.StartType -eq 'Automatic' }
Get-Process | Select-Object -Property Name, Id, @{n='MemMB';e={[math]::Round($_.WS/1MB)}}
Get-Service | Group-Object Status
Get-ChildItem C:\Windows\Logs -Filter *.log | ForEach-Object { $_.Name }
```

`Where-Object` с `{ }` — полный синтаксис, `$_` = текущий объект; нужен, когда условий несколько. `Select-Object` с хеш-таблицей — переименование и вычисление. `Group-Object` — сводка. `ForEach-Object` — выполнить действие над каждым.

**11. Сохранить результат**

```powershell
Get-Service | Select-Object Name, Status, StartType |
    Export-Csv -Path "$env:USERPROFILE\Desktop\services.csv" -NoTypeInformation -Encoding UTF8
Start-Transcript -Path "$env:USERPROFILE\Desktop\session.txt" -Append
Stop-Transcript
```

`Export-Csv` — в файл, который откроется в Excel; `-NoTypeInformation` убирает служебную первую строку; `-Encoding UTF8` нужен для русских букв (в PowerShell 7 для Excel лучше `utf8BOM`). `Start-Transcript` пишет всё, что ты делал в консоли, — золотая привычка при разборе заявки: потом есть чем доказать, что именно проверял.

**12. Скрипт `.ps1` и политика выполнения**

```powershell
Get-ExecutionPolicy -List
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

`-List` показывает политики по областям: `MachinePolicy`, `UserPolicy` (задаются групповой политикой, их руками не перебить), `CurrentUser`, `LocalMachine`. Менять себе (`CurrentUser`) можно без прав администратора. `RemoteSigned` — рабочий компромисс: свои скрипты запускаются, скачанные — только подписанные.

**13. Учётные записи (связь с уроком 17)**

```powershell
Get-LocalUser | Select-Object Name, Enabled, LastLogon
Get-LocalGroupMember -Group 'Администраторы'
```

Модуль `Microsoft.PowerShell.LocalAccounts`. Смотреть: нет ли лишних включённых учёток и кто сидит в локальных администраторах. Доменные аналоги (`Get-ADUser`) — только с модулем RSAT на машине, где установлены средства администрирования AD; на стенде L1 это появится вместе с DC01 и ролью AD DS.

**Для сравнения в Linux (UBU01):** `systemctl status`, `df -h`, `ps aux --sort=-%cpu | head`, `journalctl -p err -b`. Синтаксис другой, идея та же: команда → фильтр → вывод. В PowerShell 7 (`pwsh`) на Ubuntu работают почти те же командлеты.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **WIN11-CL01** — включена, Windows 11, вход под `ivanov` (если домен ещё не развёрнут — под локальной учётной записью, тогда вместо `ivanov` везде подставляй свою).
- **DC01** — включён, отдаёт DNS/DHCP (если DHCP ещё не настроен — на WIN11-CL01 адрес выставлен вручную в подсети 10.10.10.0/24).
- **FS01** — нужен только для шагов с `\\FS01\...`. Если FS01 ещё не поднят (это тема 17 — общие папки), шаги 7 и 8 замени на проверку локальной папки `C:\Temp`, а доступность проверяй на `DC01`.
- Права: для `Restart-Service` понадобится запуск PowerShell от администратора — это отдельно оговорено в шаге 9.

### Снапшот

В VMware Workstation: выдели ВМ → меню **VM → Snapshot → Take Snapshot** (или правый клик по вкладке ВМ → **Snapshot → Take Snapshot**). Имя: `before-l1-018`, описание: «Перед уроком 18, PowerShell». Сделай на **WIN11-CL01** и, если будешь трогать, на **FS01**.

### Шаги

1. **WIN11-CL01.** Правый клик по «Пуск» → **Терминал**. Ожидаемо: окно с приглашением `PS C:\Users\ivanov>`.
2. **WIN11-CL01.** Выполни `$PSVersionTable` и `Get-ExecutionPolicy -List`. Ожидаемо: `PSVersion 5.1.x`, у `CurrentUser` и `LocalMachine` — `Restricted` или `Undefined`.
3. **WIN11-CL01.** `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`. Ожидаемо: вопрос-подтверждение `[Y] Yes`, после `Y` команда молча завершается. Проверь: `Get-ExecutionPolicy -List` → у `CurrentUser` теперь `RemoteSigned`.
4. **WIN11-CL01.** Включи журнал: `Start-Transcript -Path "$env:USERPROFILE\Desktop\l1-018-session.txt" -Append`. Ожидаемо: «Файл журнала запущен...».
5. **WIN11-CL01.** Разведка: `Get-Command -Noun Service`, затем `Get-Help Get-Service -Examples` и `Get-Member -InputObject (Get-Service -Name Spooler)`. Ожидаемо: список из ~10 командлетов, примеры, таблица свойств и методов службы.
6. **WIN11-CL01.** `Get-Service | Where-Object Status -eq 'Stopped' | Select-Object Name, DisplayName`. Ожидаемо: короткий список остановленных служб. Запиши в блокнот, какие там есть — это база для сравнения «как должно быть».
7. **WIN11-CL01.** `Test-Path \\FS01\Obshchee`, затем `Test-Path \\FS01\HR`. Ожидаемо: `True` и `False` — `ivanov` из бухгалтерии, в `GG-HR` он не состоит, и это правильно (проверка из урока 17 в действии).
8. **WIN11-CL01.** `Test-NetConnection -ComputerName FS01 -Port 445`. Ожидаемо: `ComputerName: FS01`, `TcpTestSucceeded : True`.
9. **WIN11-CL01, PowerShell от администратора.** `Restart-Service -Name Spooler -Force`, затем `Get-Service Spooler`. Ожидаемо: `Status: Running`. Без прав администратора будет `Access is denied` — это ожидаемое поведение, а не поломка.
10. **WIN11-CL01.** `Get-Volume | Where-Object DriveType -eq 'Fixed'` и `Get-WinEvent -FilterHashtable @{LogName='System'; Level=2; StartTime=(Get-Date).AddDays(-1)} -ErrorAction SilentlyContinue | Group-Object ProviderName | Sort-Object Count -Descending`. Ожидаемо: таблица томов и сводка «источник — сколько ошибок». Если ошибок нет, вывод пустой — тоже результат.
11. **WIN11-CL01.** Создай на рабочем столе файл `C:\Users\ivanov\Desktop\Check-WS.ps1` (в Блокноте, «Сохранить как», тип «Все файлы», кодировка UTF-8) с содержимым:

```powershell
<#
.SYNOPSIS
  Быстрая проверка рабочего места перед выдачей пользователю. Стенд «Вектор», L1.
#>
[CmdletBinding()]
param(
    [string]$SharePath  = '\\FS01\Obshchee',
    [string]$ReportPath = "$env:USERPROFILE\Desktop\ws-check.csv"
)

$report = @()

foreach ($name in 'Spooler', 'Dnscache', 'LanmanWorkstation') {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($null -eq $svc) {
        $status = 'НЕ НАЙДЕНА'; $detail = '-'
    } else {
        $status = $svc.Status; $detail = $svc.StartType
    }
    $report += [pscustomobject]@{ Item = "Служба: $name"; Status = $status; Detail = $detail }
}

$sys    = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='C:'"
$freeGb = [math]::Round($sys.FreeSpace / 1GB, 1)
$report += [pscustomobject]@{ Item = 'Свободно на C:'; Status = "$freeGb ГБ"; Detail = 'порог 10 ГБ' }

$report += [pscustomobject]@{ Item = "Доступ к $SharePath"; Status = (Test-Path -Path $SharePath); Detail = 'True = открывается' }

$errs = Get-WinEvent -FilterHashtable @{LogName = 'System'; Level = 2; StartTime = (Get-Date).AddDays(-1) } -ErrorAction SilentlyContinue
$report += [pscustomobject]@{ Item = 'Ошибки в System за 24 ч'; Status = ($errs | Measure-Object).Count; Detail = 'см. журнал' }

$report | Format-Table -AutoSize
$report | Export-Csv -Path $ReportPath -NoTypeInformation -Encoding UTF8
Write-Host "Отчёт сохранён: $ReportPath"
```

12. **WIN11-CL01.** Запусти скрипт: `.\Check-WS.ps1`. Ожидаемо: на экране таблица из шести строк, на рабочем столе появился `ws-check.csv`, открывается в Excel без «кракозябр».
13. **WIN11-CL01.** Запусти ещё раз с параметром: `.\Check-WS.ps1 -SharePath '\\FS01\HR'`. Ожидаемо: строка «Доступ к \\FS01\HR» со `Status: False` — параметризация работает.
14. **WIN11-CL01.** `Stop-Transcript`, закрой консоль. Ожидаемо: файл `l1-018-session.txt` на рабочем столе.
15. **Стенд.** Открой `stand-log.md` и допиши изменения (список — в разделе «Что изменилось»).

### Проверка

- [ ] `$PSVersionTable` показывает 5.1 (или 7.x), `PSEdition Desktop` (или Core)
- [ ] `Get-ExecutionPolicy -List` → у `CurrentUser` стоит `RemoteSigned`
- [ ] `Get-Service | Where-Object Status -eq 'Stopped'` выдаёт список без ошибок и без `Spooler`
- [ ] `Test-Path \\FS01\HR` → `False`, `Test-Path \\FS01\Obshchee` → `True`
- [ ] `Test-NetConnection FS01 -Port 445` → `TcpTestSucceeded : True`
- [ ] `Get-Volume` показывает системный том и свободное место
- [ ] `Get-WinEvent -FilterHashtable ...` выдаёт события или пустой список, но не ошибку
- [ ] `.\Check-WS.ps1` отрабатывает и создаёт `ws-check.csv`
- [ ] `ws-check.csv` открывается в Excel, русский текст читается
- [ ] `stand-log.md` обновлён

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Запускаю твой Check-WS.ps1 — вылезает красным: `...cannot be loaded because running scripts is disabled on this system`. Мне до обеда надо диск проверить, сделай что-нибудь!»
> **Срочность:** средняя (до конца рабочего дня)

**Как воспроизвести поломку (на WIN11-CL01):**

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Restricted
.\Check-WS.ps1
```

Ожидаемо: та самая красная ошибка `File ...Check-WS.ps1 cannot be loaded because running scripts is disabled on this system`.

**Диагностика по шагам:**

1. Прочитать ошибку целиком, а не первую строку. Здесь причина названа прямо: `running scripts is disabled`. Это не «скрипт сломан».
2. `Get-ExecutionPolicy -List` — смотрим, какая область что говорит. Если `MachinePolicy` или `UserPolicy` = `Restricted`, руками ничего не сделать: политика пришла из групповой политики, правится на DC01 в `gpedit.msc`/GPO, а не на клиенте.
3. Если `CurrentUser` = `Restricted` — это локальная настройка, чинится на месте.
4. Проверить, не заблокирован ли сам файл как «скачанный из интернета»: `Get-Item .\Check-WS.ps1 -Stream Zone.Identifier -ErrorAction SilentlyContinue`. Если поток есть — файл помечен, и при политике `RemoteSigned` он не запустится даже после смены политики.
5. Убедиться, что проблема не в содержимом скрипта: `Get-Content .\Check-WS.ps1 -TotalCount 5` — файл читается, синтаксис не при чём.

**Решение (при локальной причине):**

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
Unblock-File -Path .\Check-WS.ps1     # только если на шаге 4 нашли Zone.Identifier
.\Check-WS.ps1
```

**Чего делать нельзя:** советовать пользователю `Set-ExecutionPolicy Bypass -Scope LocalMachine` или `powershell -ExecutionPolicy Bypass -File ...`. Это снимает защиту для всей машины и всех пользователей, и в боевой среде так не делают — политику задают централизованно через GPO.

**Ответ пользователю:**

> Иван, добрый день. Скрипт не запускался из-за политики выполнения скриптов на твоей машине (защита Windows), сам скрипт в порядке. Поправил политику только для твоей учётной записи — теперь скрипт работает. Проверил: диск C: в норме, службы печати и сети запущены, доступ к \\FS01\Obshchee есть. Отчёт лежит на рабочем столе: `ws-check.csv`. Если что-то ещё будет — пиши.

**Закрытие заявки:** категория «ОС / настройки PowerShell», решение «изменена ExecutionPolicy в области CurrentUser на RemoteSigned», в комментарии — вывод `Get-ExecutionPolicy -List` до и после. Заявку закрываем после подтверждения от пользователя.

### Что изменилось на стенде

- **WIN11-CL01:** для учётной записи `ivanov` изменена политика выполнения скриптов: `CurrentUser = RemoteSigned` (хранится в реестре в ветке пользователя, не в `LocalMachine`).
- **WIN11-CL01:** на рабочем столе создан скрипт `Check-WS.ps1` (учётка `ivanov`).
- **WIN11-CL01:** на рабочем столе созданы файлы `ws-check.csv` и `l1-018-session.txt` (журнал сессии PowerShell).
- **WIN11-CL01:** служба `Spooler` перезапущена (состояние не изменилось, работала и работает).
- **FS01, DC01:** не изменялись. Права и группы не трогались.

### Откат

Если что-то пошло не так (например, случайно поменял политику в `LocalMachine` или испортил скрипт): выключи WIN11-CL01 → в VMware Workstation **VM → Snapshot → Snapshot Manager** → выбери `before-l1-018` → **Go To**. Машина вернётся к состоянию до урока, включая политику выполнения и файлы на рабочем столе. Если откатываться не хочешь — сотри вручную `Check-WS.ps1`, `ws-check.csv`, `l1-018-session.txt` и верни `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Undefined`.

## 6. Ошибки новичков и безопасность

- **`Format-Table` в середине пайплайна** → дальше идут строки, а не объекты, `Where-Object` перестаёт фильтровать по свойствам, появляется мусор или пустота → форматирование ставь только в самом конце, а для фильтрации используй `Select-Object`.
- **Алиасы в скриптах (`gci`, `?`, `%`, `ls`)** → на другой машине или в другом окружении алиас может значить другое (`ls` в Linux — совсем другая команда), скрипт тихо сломается → в `.ps1` всегда полные имена командлетов и параметров.
- **`-Force` и `Remove-Item` без проверки** → удаление папки пользователя вместе с содержимым → сначала `-WhatIf` (показывает, что было бы удалено), потом без него; и всегда сначала `Get-ChildItem` — посмотреть глазами.
- **`ExecutionPolicy Bypass` «чтобы работало»** → политика снята для всей машины, любой скачанный скрипт запускается молча → менять только `CurrentUser`, только на `RemoteSigned`, а в домене — централизованно через GPO.
- **Запуск `.ps1` из интернета «как есть»** → скрипт с обфусцированным кодом, кража учётных данных, шифровальщик → открыть в блокноте и прочитать, искать `Invoke-WebRequest`, `DownloadString`, `-EncodedCommand`, `net user`, `Set-MpPreference`; сомневаешься — не запускай.
- **Пароль в скрипте открытым текстом** → утёк вместе с файлом на общую папку → `Get-Credential` (спросит интерактивно) или Windows Credential Manager / gMSA; в скрипте паролей быть не должно.
- **`Get-Service | Stop-Service` без `-Name`** → попытка остановить всё, что можно, включая сетевые службы → всегда указывай `-Name` явно и проверяй `-WhatIf`.
- **Работа без транскрипта** → через час не помнишь, что именно проверял, и не можешь доказать правомерность действий → `Start-Transcript` в начале разбора заявки.

### Безопасность

В боевой среде PowerShell — это полноценный инструмент администрирования с правами того, кто его запустил. Поэтому: не запускать консоль «на всякий случай» от имени доменного администратора (принцип наименьших привилегий); не отключать `ExecutionPolicy` и не расширять права ради удобства; в компании обычно включено **логирование PowerShell** (Script Block Logging, Transcription) на контроллере домена — все твои команды пишутся в журнал, и это нормально; подписывать рабочие скрипты сертификатом и хранить их в защищённом репозитории, а не на сетевой шаре `\\FS01\Obshchee`, куда пишут все. На стенде пароль `Lab#2026-vektor` учебный — в реальной работе один пароль на всех сотрудников недопустим.

## 7. Вопросы на собеседовании

**1. Чем командлет PowerShell отличается от алиаса и от обычной программы вроде `ipconfig`?**

Командлет — это скомпилированная команда .NET с именем `Глагол-Существительное`, которая принимает и возвращает объекты. Алиас — просто короткое имя-ссылка на командлет (`gci` → `Get-ChildItem`), удобное в консоли, но ненадёжное в скриптах. Внешняя программа (`ipconfig.exe`, `ping.exe`) — отдельный процесс, который возвращает текст, а не объекты, поэтому её вывод приходится разбирать `Select-String`, а не фильтровать по свойствам.
*Что хочет услышать интервьюер:* понимание «объекты против текста» и того, что алиасы не для скриптов.
*Красный флаг:* «это одно и то же, просто разные названия».

**2. Что такое пайплайн и почему `Get-Process | Format-Table | Where-Object CPU -gt 100` не сработает?**

Пайплайн (`|`) передаёт результат одной команды на вход следующей. Внутри пайплайна передаются объекты .NET, у которых есть свойства и методы. `Format-Table` — это форматтер, он превращает объекты в текстовые строки для показа на экране, и после него `Where-Object` уже не может обратиться к свойству `CPU`. Правильно: `Get-Process | Where-Object CPU -gt 100 | Format-Table`.
*Что хочет услышать интервьюер:* что форматирование — это финальный этап, и что кандидат реально строил конвейеры.
*Красный флаг:* «не знаю, должно работать» или предложение убрать `Where-Object`.

**3. Что такое ExecutionPolicy и защищает ли она от вредоносных скриптов?**

Это политика выполнения скриптов: `Restricted` (по умолчанию на клиентских Windows, `.ps1` не запускаются), `RemoteSigned` (локальные — можно, скачанные — только с подписью), `AllSigned`, `Unrestricted`, `Bypass`. Задаётся отдельно для областей `MachinePolicy`, `UserPolicy` (из GPO), `CurrentUser`, `LocalMachine`; приоритет — сверху вниз. От вирусов она **не защищает**: любую команду можно ввести в консоль вручную, а политику легко обойти флагами. Это защита от случайного запуска, а не средство безопасности — реальная защита это антивирус, AppLocker/WDAC, подпись кода и права пользователя.
*Что хочет услышать интервьюер:* что кандидат не считает `ExecutionPolicy` системой безопасности и знает про приоритет областей.
*Красный флаг:* «это антивирус» или «её нужно всегда отключать, она мешает».

**4. Пользователь пишет: «не открывается сетевая папка». Какими командами PowerShell ты это проверишь?**

Сначала `Test-Path \\FS01\HR` — есть ли доступ под текущей учёткой. Потом `Test-NetConnection -ComputerName FS01 -Port 445` — доходит ли трафик до SMB. Если порт открыт и `Test-Path` = `False` — это права: смотрю группы пользователя (`whoami /groups` или `Get-LocalGroupMember`), сверяю с NTFS-разрешениями и правами на шару (`Get-SmbShareAccess -Name HR` на FS01). Дополнительно `Get-Service LanmanWorkstation` и `Resolve-DnsName FS01` — не отвалился ли клиент SMB и правильно ли резолвится имя.
*Что хочет услышать интервьюер:* порядок диагностики «сеть → порт → права» и знание, что PowerShell умеет проверять каждое звено.
*Красный флаг:* «перезагружу компьютер пользователя» без единой проверки.

**5. Как бы ты автоматизировал ежедневную проверку десяти рабочих мест?**

Скрипт `.ps1` с блоком `param()`, чтобы путь отчёта и список машин были параметрами, а не хардкодом. Внутри — цикл `foreach` по именам машин, проверки (`Get-CimInstance -ComputerName` для диска, `Get-Service -ComputerName` для служб, `Test-NetConnection` для портов), сбор результатов в массив объектов `[pscustomobject]`, затем `Export-Csv` в файл на шаре и `Format-Table` на экран. Запуск — по расписанию через Планировщик заданий (`Register-ScheduledTask`) под отдельной сервисной учёткой с минимальными правами, пароль — не в скрипте. И самое главное: перед первым запуском протестировать на одной машине и добавить `-ErrorAction SilentlyContinue` / `try/catch`, чтобы одна недоступная машина не валила весь отчёт.
*Что хочет услышать интервьюер:* параметры вместо хардкода, обработка ошибок, тест на одной машине, отсутствие паролей в коде.
*Красный флаг:* «напишу десять команд подряд и запущу руками» или скрипт с паролем администратора внутри.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «PowerShell» → «PowerShell 5.1» и «PowerShell 7.x»: обзор, установка, «PowerShell 101». Поиск: `PowerShell documentation site:learn.microsoft.com`.
- **learn.microsoft.com** — справочник по командлетам: статьи `Get-Service`, `Where-Object`, `Select-Object`, `Get-WinEvent`, `Test-NetConnection`, `Export-Csv`. Поиск: `Get-WinEvent FilterHashtable site:learn.microsoft.com`.
- **learn.microsoft.com** — концептуальные статьи `about_Pipelines`, `about_Execution_Policies`, `about_Objects`, `about_Operators`. Поиск: `about_Execution_Policies site:learn.microsoft.com`.
- **learn.microsoft.com** — «PowerShell security best practices» / «about_Signing» / «about_Logging_Windows». Поиск: `about_Logging_Windows site:learn.microsoft.com`.
- **learn.microsoft.com** — классы WMI/CIM: `Win32_LogicalDisk`, `Win32_OperatingSystem`. Поиск: `Win32_LogicalDisk class site:learn.microsoft.com`.
- **github.com/PowerShell/PowerShell** — репозиторий PowerShell 7 (релизы, документация). Поиск: `PowerShell releases GitHub`.
- Статья «PowerShell Scripting» в каталоге Microsoft Learn [ПРОВЕРИТЬ точное название раздела на 2026 год — структура каталога меняется].
