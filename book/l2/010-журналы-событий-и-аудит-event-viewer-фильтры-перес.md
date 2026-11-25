# Журналы событий и аудит: Event Viewer, фильтры, пересылка событий, типовые Event ID

## 1. Введение (зачем это нужно в реальной работе)

Заявка в поддержку «Вектора»:

> **Пользователь:** ivanov / WIN11-CL01
> **Что написал:** «Вчера вечером компьютер вырубился сам, я потерял несохранённый отчёт. Сегодня в 9:15 опять всё зависло примерно на минуту. Сделайте что-нибудь, а то работать невозможно.»
> **Срочность:** средняя (работает, но данные теряются)

Ты подходишь к машине, а там всё в порядке: загрузилась, работает, ничего не «падает». Воспроизвести нечего, пользователь не помнит, что нажимал, время утекло. И единственное, что у тебя осталось, — журналы. Это самая частая ситуация в L2: **проблема уже прошла, а тебе нужно доказать, что она была, когда и почему**.

Журналы событий — это чёрный ящик Windows. 80% заявок «само перезагрузилось», «само отвалилось», «не пускает в папку», «принтер отваливается» закрываются чтением двух-трёх журналов. Аудит (audit policy) — это то, что вообще включает запись событий безопасности: без него в Security log пусто и разбирать инцидент нечем. Пересылка событий (WEF) — то, чем в реальной компании собирают логи с 50 серверов в одно место, потому что бегать по каждой машине с `eventvwr.msc` — это L1-навык, а не L2.

После этой главы ты должен уметь: читать и фильтровать журналы через GUI, PowerShell и `wevtutil`, разбирать событие по полям XML, а не по красивому тексту, включать нужные подкатегории аудита, настраивать сбор событий на отдельный сервер и знать наизусть полтора десятка Event ID, по которым строится 90% диагностики.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Кто пишет.** Приложения, драйверы, ядро и сама ОС генерируют события через механизм ETW (Event Tracing for Windows — трассировка событий Windows). Провайдер (provider) — это компонент, который «объявляет» свои события: `Microsoft-Windows-Kernel-Power`, `Service Control Manager`, `Microsoft-Windows-Security-Auditing`. Событие попадает в **канал** (channel) — именованный поток, и уже канал сохраняется службой **Windows Event Log** (служба `eventlog`, отображаемое имя «Журнал событий Windows») в файл формата `.evtx`.

**Где лежит.** Все журналы — в `C:\Windows\System32\winevt\Logs\` (файлы `System.evtx`, `Security.evtx`, `Application.evtx` и сотни других). Формат `.evtx` — бинарный, с 2008 года; старые `.evt` (2003) современными средствами читаются только через конвертацию.

**Какие бывают каналы.**
- Классические «Windows Logs»: **System** (ОС, драйверы, службы), **Application** (программы), **Security** (аудит, только он и только при включённой политике), **Setup** (установка компонентов и обновлений), **ForwardedEvents** (сюда падают события, присланные с других машин).
- **Applications and Services Logs** — иерархия на сотни каналов: `Microsoft-Windows-GroupPolicy/Operational`, `Microsoft-Windows-TerminalServices-LocalSessionManager/Operational`, `Microsoft-Windows-TaskScheduler/Operational` и т.д. Именно там живёт конкретика, которой нет в System.
- Типы каналов: Admin (включён всегда), Operational (обычно включён), Analytic и Debug (по умолчанию **выключены**, включаются вручную и пишут очень много).

**Из чего состоит событие.** Внутри — XML с двумя частями: `System` (метаданные: провайдер, EventID, версия, Level, Task, Keywords, TimeCreated, EventRecordID, Channel, Computer, Security/@UserID) и `EventData`/`UserData` (полезная нагрузка — те самые поля, по которым ты будешь искать). Пример мысленно: у 4625 в EventData лежат `TargetUserName`, `LogonType`, `IpAddress`, `Status`, `SubStatus`. **Красивый текст в колонке Message — это уже рендеринг**, он зависит от языка ОС и от наличия DLL провайдера; фильтровать надо по XML, а не по тексту.

**Уровни (Level):** 1 Critical, 2 Error, 3 Warning, 4 Information, 5 Verbose, 0 LogAlways.

**Размер и ротация.** У каждого канала есть максимальный размер и режим: перезаписывать по кругу (по умолчанию), архивировать при заполнении, не перезаписывать никогда. По умолчанию размеры скромные (порядка 20 МБ, точное значение смотри командой из раздела 4), и на шумном сервере Security-лог «прокручивается» за часы — вчерашние события физически исчезают. Это классическая причина «а журнал пустой».

**Аудит.** Запись в Security включается политикой: либо локально (`auditpol`), либо (правильно) через GPO — Advanced Audit Policy Configuration. Категорий девять (Account Logon, Account Management, Logon/Logoff, Object Access, Policy Change, Privilege Use, System, Detailed Tracking, DS Access), внутри — подкатегории. Пока не включён «Audit Logon» — событий 4624/4625 не будет вообще, и ты будешь искать проблему входа в пустом журнале.

**Пересылка (WEF, Windows Event Forwarding).** Источник (source) отправляет события сборщику (collector) по протоколу **WS-Management поверх WinRM**: TCP **5985** (HTTP) или **5986** (HTTPS). На сборщике работает служба **Windows Event Collector** (`Wecsvc`), она принимает поток по подписке (subscription) и складывает в канал **ForwardedEvents**. Подписка бывает **collector-initiated** (сборщик сам опрашивает список машин; учётка компьютера сборщика должна быть в группе Event Log Readers на источниках) и **source-initiated** (машины сами шлют на сборщик; список сборщиков раздаётся GPO, а учётки компьютеров-источников должны быть в Event Log Readers на сборщике). Второй вариант — промышленный стандарт для 50+ машин. В нормальной компании дальше стоит SIEM, и WEF — только транспорт до него.

```text
ИСТОЧНИКИ: DC01, FS01, WIN11-CL01
┌──────────────────────────────────────────────────────┐
│ приложение / драйвер / ядро / аудит                  │
│         ↓ (ETW-провайдер)                            │
│ служба «Журнал событий Windows» (eventlog)           │
│         ↓                                            │
│ каналы: System | Application | Security | ...        │
│         ↓                                            │
│ C:\Windows\System32\winevt\Logs\*.evtx               │
└───────────────────────┬──────────────────────────────┘
                        │ WinRM / WS-Management
                        │ TCP 5985 (HTTP) / 5986 (HTTPS)
                        ↓
СБОРЩИК: FS01, служба Windows Event Collector (Wecsvc)
подписка Vektor-Security  →  канал ForwardedEvents
                        ↓
Get-WinEvent -LogName ForwardedEvents
                        ↓
       (в проде — SIEM / алерт в Zabbix / отчёт)
```

По модели OSI сама запись события — это прикладной уровень (L7), но событие может описывать что угодно ниже: от сбоя линка до ошибки Kerberos.

**Шпаргалка по типовым Event ID** (тексты зависят от версии и языка — всегда читай само событие):

| ID | Лог / источник | О чём | Что смотреть |
|----|----------------|-------|--------------|
| 41 | System / Kernel-Power | Система перезагрузилась без корректного завершения | Время, совпадение с 6008 |
| 6008 | System / EventLog | «Предыдущее завершение работы было неожиданным» | Время — момент «смерти» |
| 1074 | System / User32 | Кто инициировал перезагрузку/выключение | Имя процесса и пользователя |
| 6005 / 6006 | System / EventLog | Служба журнала запущена / остановлена | Границы сессии работы |
| 6013 | System / EventLog | Время работы системы в днях | Аптайм |
| 1001 | System / BugCheck, WER | Синий экран (код BugCheck), отчёт об ошибке | Код 0x…, путь к дампу |
| 1000 / 1002 | Application / Application Error, Application Hang | Падение / зависание программы | Faulting module — виновник |
| 1026 | Application / .NET Runtime | Необработанное исключение .NET | Трассировка стека |
| 7000 / 7009 / 7011 | System / Service Control Manager | Служба не стартовала / таймаут запуска / таймаут транзакции | Имя службы, код ошибки |
| 7031 / 7034 | System / SCM | Служба аварийно завершилась | Имя службы, счётчик падений |
| 7036 | System / SCM | Служба перешла в состояние «работает»/«остановлена» | Кто и когда дёргал службу |
| 7045 | System / SCM | Установлена новая служба | Часто — следствие установки ПО или вредоноса |
| 12 / 13 | System / Kernel-General | Старт / остановка ОС | Точные метки времени |
| 51 / 153 | System / Disk | Ошибка ввода-вывода, операция повторена | Проблемы диска/хранилища |
| 55 / 140 | System / Ntfs | Повреждение структуры ФС / не удалось сбросить данные в лог | Срочно к состоянию тома |
| 5719 | System / NETLOGON | Нет защищённой сессии с контроллером домена | Сеть, DNS, время |
| 1129 | System / GroupPolicy | Политики не применились — нет связи с DC | Адреса DC, DNS |
| 4624 | Security | Успешный вход | **Logon Type** (2 консоль, 3 сеть, 10 RDP, 5 служба), IP |
| 4625 | Security | Неудачный вход | SubStatus (0xC000006A — пароль), Logon Type, IP |
| 4634 / 4647 | Security | Выход из системы | Парность с 4624 |
| 4648 | Security | Вход с явно указанными учётными данными | `runas`, скрипты с паролями |
| 4672 | Security | Назначены привилегии администратора | Кто «под админом» |
| 4688 | Security | Запуск процесса (нужна политика) | Имя процесса, командная строка |
| 4720 / 4726 / 4738 | Security | Учётка создана / удалена / изменена | Кто изменил |
| 4732 | Security | Добавление в группу безопасности | Эскалация прав |
| 4740 / 4767 | Security | Блокировка / разблокировка учётки | Кто и откуда блокировал |
| 4771 / 4776 | Security | Провал предаутентификации Kerberos / проверки NTLM | Код ошибки, источник |
| 1102 / 4719 | Security | Журнал очищен / изменена политика аудита | Инцидент ИБ |
| 5140 / 5145 | Security | Доступ к сетевой папке (нужен аудит Object Access) | Кто, к какому ресурсу |

## 3. Аналогия из жизни

Представь бизнес-центр. На входе сидит вахтёр и ведёт три разных журнала. В **инженерном** (System) он пишет про сам дом: «лифт встал», «свет пропал в 14:03», «сантехник Петрович запустил насос». В **журнале арендаторов** (Application) пишут сами компании: «у нас упал принтер», «бухгалтерская программа выдала ошибку». А в **журнале охраны** (Security) фиксируется только то, что мы заранее попросили фиксировать: кто вошёл, кто не смог пройти по пропуску, кто получил ключ от серверной. Если мы не дали охране распоряжение «записывай попытки входа» — журнал охраны будет пустым, сколько ни смотри. Фильтр в Event Viewer — это просьба «покажи мне только записи за вчера с 18:00 и только про лифт». WEF — это центральный пульт, куда все три журнала со всех этажей стекаются в одну комнату.

Где аналогия ломается: бумажный журнал вахтёра не стирается сам. У нас — стирается: журнал заданного размера заворачивается по кругу, и старые записи бесследно исчезают именно тогда, когда они нужнее всего.

## 4. Практика и команды

**1. GUI: карта Event Viewer.**
```text
Win+R → eventvwr.msc
```
Слева дерево: `Windows Logs` (Application, Security, System, Setup, ForwardedEvents), `Applications and Services Logs`, `Subscriptions`. В центре — список событий (колонки Level, Date and Time, Source, Event ID, Task Category). Справа панель Actions: `Filter Current Log…`, `Find…`, `Save All Events As…`, `Attach Task To This Event…`, `Clear Log…`. Правая кнопка на канале → `Properties` — там размер, ротация и `Enable logging`.

**2. Инвентаризация: где журналы переполнены.**
```powershell
Get-WinEvent -ListLog * -ErrorAction SilentlyContinue |
  Where-Object RecordCount -gt 0 |
  Sort-Object RecordCount -Descending |
  Select-Object -First 15 LogName, RecordCount, FileSize, MaximumSizeInBytes, LogMode |
  Format-Table -AutoSize
```
```text
LogName     RecordCount   FileSize MaximumSizeInBytes LogMode
-------     -----------   -------- ------------------ -------
Security         102311  204472320          209715200 Circular
System             4211   18104320           20971520 Circular
```
Смотрим: если `FileSize` уже равен `MaximumSizeInBytes` при `LogMode = Circular` — журнал прямо сейчас затирает сам себя.

**3. Чтение журнала.**
```powershell
Get-WinEvent -LogName System -MaxEvents 10 |
  Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message | Format-List
```
`-MaxEvents` ограничивает выборку (без него команда вытянет весь журнал и надолго задумается); `-Oldest` меняет порядок на «сначала старые». `Get-EventLog` — устаревший командлет, видит только классические журналы; в работе используем `Get-WinEvent`.

**4. Фильтр на стороне службы: `-FilterHashtable`.**
```powershell
$since = (Get-Date).AddDays(-2)
Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=$since} |
  Group-Object ProviderName, Id | Sort-Object Count -Descending |
  Select-Object -First 10 Count, Name
```
Ключи: `LogName` (или `Path` — для файла `.evtx`), `Level` (1–5), `ID` (число или массив), `ProviderName`, `StartTime`/`EndTime`, `Data` (поиск по значению поля). Главное: фильтрация выполняется службой журнала, а не в PowerShell — это в разы быстрее, чем `Where-Object` по всему массиву.

**5. XPath: точный фильтр по XML.**
```powershell
Get-WinEvent -LogName Security -FilterXPath "*[System[(EventID=4625)]]" -MaxEvents 5

Get-WinEvent -LogName System -FilterXPath `
  "*[System[Level=2 and TimeCreated[timediff(@SystemTime) <= 86400000]]]"
```
`86400000` — миллисекунды, то есть ровно сутки назад. XPath сложнее читается, но это единственный синтаксис, который понимают все инструменты (в том числе WEF-подписки).

**6. Разбор события по полям, а не по тексту.**
```powershell
$e = Get-WinEvent -LogName Security -FilterXPath "*[System[EventID=4625]]" -MaxEvents 1
$e.ToXml()
$e.Properties | ForEach-Object { $_.Value }
```
`ToXml()` даёт полный XML с именами полей; `Properties` — плоский список значений в порядке провайдера (индексы у разных событий разные, поэтому по именам надёжнее).

**7. `wevtutil` — то же самое из CMD (работает на Server Core).**
```cmd
wevtutil gl Security
wevtutil qe System /q:"*[System[(Level=1 or Level=2)]]" /c:5 /rd:true /f:text
wevtutil epl Security C:\Temp\Security_backup.evtx
```
`gl` = get-log (параметры канала), `qe` = query-events (`/q:` XPath, `/c:` сколько, `/rd:true` — от новых к старым, `/f:text|xml` — формат), `epl` = export-log (выгружает канал в файл, журнал при этом не чистится).

**8. Размер и ротация канала.**
```cmd
wevtutil sl Security /ms:268435456
wevtutil gl Security
```
`/ms` — максимальный размер в байтах (268435456 = 256 МБ); `/rt:true` — не перезаписывать события (тогда канал встанет на заполнении и потребует ручной очистки). То же в GUI: правой кнопкой по каналу → Properties → Maximum log size + «Overwrite events as needed / Archive the log when full / Do not overwrite events».

**9. Политика аудита: смотрим и включаем.**
```cmd
auditpol /get /category:*
auditpol /set /subcategory:"Logon" /success:enable /failure:enable
auditpol /set /subcategory:"User Account Management" /success:enable /failure:enable
auditpol /backup /file:C:\Temp\audit.csv
auditpol /restore /file:C:\Temp\audit.csv
```
`/get /category:*` — срез всех подкатегорий; `/set /subcategory:` — включение; `/backup` и `/restore` — сохранение и раскатка настроек. Важно: **на русской Windows названия подкатегорий локализованы**, и `"Logon"` может не подойти — сначала посмотри реальные имена в выводе `auditpol /get /category:*` (или используй GUID подкатегории из документации Microsoft). И помни: доменная GPO перебивает локальные настройки `auditpol`.

**10. Расширенная политика аудита через GPO (правильный путь).**
```text
Computer Configuration → Policies → Windows Settings → Security Settings →
  Advanced Audit Policy Configuration → Audit Policies → Logon/Logoff →
    "Audit Logon" = Success and Failure
```
Плюс в `Security Settings → Local Policies → Security Options` включить `Audit: Force audit policy subcategory settings (Windows Vista or later) to override audit policy category settings` — иначе старые «категории» могут перебить подкатегории.

**11. Сборщик: подготовка FS01.**
```powershell
wecutil qc /q:true
Get-Service Wecsvc
winrm enumerate winrm/config/listener
```
`wecutil qc` (quick-config) включает службу Windows Event Collector и настраивает её; `winrm enumerate` показывает, слушает ли сборщик 5985/5986.

**12. Источники: разрешаем WinRM и чтение журналов.**
```powershell
# на DC01 и WIN11-CL01
winrm quickconfig -q
Add-LocalGroupMember -Group 'Event Log Readers' -Member 'VEKTOR\FS01$'
```
`winrm quickconfig` поднимает службу WinRM, создаёт HTTP-слушатель на 5985 и открывает порт в файрволе. Учётка компьютера сборщика (`FS01$`) должна иметь право читать журналы — даём ровно группу `Event Log Readers`, а не администратора. На контроллере домена эта группа хранится в AD (контейнер Builtin), то есть членство подействует на всех DC.

**13. Подписка и чтение ForwardedEvents.**
```powershell
wecutil cs C:\WEF\vektor-security.xml
wecutil es
wecutil gs Vektor-Security /f:xml
wecutil gr Vektor-Security          # состояние выполнения подписки
Get-WinEvent -LogName ForwardedEvents -MaxEvents 20 |
  Select-Object TimeCreated, Id, ProviderName, MachineName
```
`cs` = create-subscription, `es` = enumerate-subscriptions, `gs` = get-subscription, `gr` = get-subscription-run-time-status (главный инструмент, когда «не приходит»).

**14. Автоматизация: задача по событию.**
```text
Event Viewer → выбрать событие → справа "Attach Task To This Event…"
```
Создаёт задание планировщика с триггером «при регистрации события». Штука мощная, но не вешай её на высокочастотные события (4624 пишется десятками в минуту) — получишь шторм задач. В нормальной эксплуатации реакцию на события делают через мониторинг, а не через задачи на каждом событии.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **DC01** (10.10.10.10) — Windows Server 2022, AD DS + DNS + DHCP, домен `corp.vektor.lab`, ты работаешь под `VEKTOR\adm.student`.
- **FS01** (10.10.10.20) — Windows Server 2022 в домене, роль файлового сервера уже есть (из L1). Роль сборщика журналов включим на месте.
- **WIN11-CL01** — Windows 11 в домене, рабочее место `ivanov`.
- Все три машины видят друг друга по имени, время синхронизировано (иначе Kerberos и корреляция событий поедут).
- Если FS01 ещё не поднят — вернись к главе L1 про файловый сервер; без него лабораторную по пересылке не сделать.

### Снапшот

```powershell
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" snapshot "D:\VMs\DC01\DC01.vmx" before-l2-010
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" snapshot "D:\VMs\FS01\FS01.vmx" before-l2-010
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" snapshot "D:\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l2-010
```
Путь к `vmrun.exe` и к `.vmx` у тебя может отличаться — проверь. То же самое мышкой: VM → Snapshot → Take Snapshot.

### Шаги

1. **DC01.** `Win+R` → `eventvwr.msc`. Открой `Windows Logs → System`. Найди события 6005/6006 (запуск и остановка службы журнала) и 12/13 — это границы последней сессии. **Ожидаемо:** видишь минимум одну пару «остановлен → запущен», время совпадает с последней загрузкой.
2. **DC01.** Оцени размеры ключевых журналов:
   ```powershell
   Get-WinEvent -ListLog System, Security, Application |
     Select-Object LogName, MaximumSizeInBytes, FileSize, RecordCount, LogMode | Format-Table -AutoSize
   ```
   **Ожидаемо:** размеры порядка 20 МБ, `LogMode = Circular`.
3. **DC01.** Увеличь Security до 256 МБ и проверь:
   ```cmd
   wevtutil sl Security /ms:268435456
   wevtutil gl Security
   ```
   **Ожидаемо:** в блоке `logging:` поле `maxSize: 268435456`.
4. **DC01.** Посмотри текущую политику аудита и включи нужное:
   ```cmd
   auditpol /get /category:*
   auditpol /set /subcategory:"Logon" /success:enable /failure:enable
   auditpol /set /subcategory:"Account Lockout" /success:enable
   auditpol /set /subcategory:"User Account Management" /success:enable /failure:enable
   ```
   **Ожидаемо:** до выполнения Security-лог почти пуст по части входов; после — в выводе `/get` у соответствующих подкатегорий `Success and Failure`. Если имена подкатегорий не подошли (русская локаль) — возьми точные из вывода `/get /category:*`.
5. **WIN11-CL01.** Сгенерируй неудачный и удачный сетевой вход:
   ```cmd
   net use \\DC01.corp.vektor.lab\SYSVOL /user:VEKTOR\ivanov WrongPass123
   net use \\DC01.corp.vektor.lab\SYSVOL /user:VEKTOR\ivanov Lab#2026-vektor
   net use \\DC01.corp.vektor.lab\SYSVOL /delete
   ```
   **Ожидаемо:** первая команда — «Системная ошибка 1326», вторая — «Команда выполнена успешно». На DC01 при этом появляются 4625 и 4624 с типом входа 3, а также 4771 при провале Kerberos.
6. **DC01.** Найди и разбери неудачные входы по полям:
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625; StartTime=(Get-Date).AddHours(-2)} |
     ForEach-Object {
       $x = [xml]$_.ToXml()
       [pscustomobject]@{
         Time  = $_.TimeCreated
         User  = ($x.Event.EventData.Data | Where-Object Name -eq 'TargetUserName').'#text'
         Type  = ($x.Event.EventData.Data | Where-Object Name -eq 'LogonType').'#text'
         IP    = ($x.Event.EventData.Data | Where-Object Name -eq 'IpAddress').'#text'
         Status= ($x.Event.EventData.Data | Where-Object Name -eq 'Status').'#text'
       }
     } | Format-Table -AutoSize
   ```
   **Ожидаемо:** строка с `User = ivanov`, `Type = 3`, `Status = 0xC000006D` (или `SubStatus 0xC000006A`).
7. **FS01.** Включи сборщик:
   ```powershell
   wecutil qc /q:true
   Get-Service Wecsvc
   ```
   **Ожидаемо:** служба `Wecsvc` в состоянии Running, тип запуска Automatic (Delayed).
8. **DC01 и WIN11-CL01.** Разреши WinRM и доступ сборщику:
   ```powershell
   winrm quickconfig -q
   Add-LocalGroupMember -Group 'Event Log Readers' -Member 'VEKTOR\FS01$'
   Get-LocalGroupMember -Group 'Event Log Readers'
   ```
   **Ожидаемо:** `winrm quickconfig` сообщает, что служба запущена и слушатель создан; в группе появилась `VEKTOR\FS01$`.
9. **FS01.** Создай файл подписки `C:\WEF\vektor-security.xml`:
   ```xml
   <Subscription xmlns="http://schemas.microsoft.com/2006/03/windows/events/subscription">
     <SubscriptionId>Vektor-Security</SubscriptionId>
     <SubscriptionType>CollectorInitiated</SubscriptionType>
     <Description>Входы и ошибки System с серверов Вектор</Description>
     <Enabled>true</Enabled>
     <Delivery Mode="Push">
       <Batching><MaxItems>20</MaxItems><MaxLatencyTime>30000</MaxLatencyTime></Batching>
       <Heartbeat><Interval>900000</Interval></Heartbeat>
     </Delivery>
     <Query>
       <![CDATA[
       <QueryList>
         <Query Id="0" Path="Security">
           <Select Path="Security">*[System[(EventID=4624 or EventID=4625 or EventID=4740)]]</Select>
         </Query>
         <Query Id="1" Path="System">
           <Select Path="System">*[System[(Level=1 or Level=2)]]</Select>
         </Query>
       </QueryList>
       ]]>
     </Query>
     <ReadExistingEvents>true</ReadExistingEvents>
     <TransportName>HTTP</TransportName>
     <ContentFormat>RenderedText</ContentFormat>
     <Locale Language="ru-RU"/>
     <LogFile>ForwardedEvents</LogFile>
     <ComputerList>DC01.corp.vektor.lab</ComputerList>
   </Subscription>
   ```
   Затем:
   ```powershell
   wecutil cs C:\WEF\vektor-security.xml
   wecutil es
   wecutil gr Vektor-Security
   ```
   **Ожидаемо:** подписка создана, в `gr` статус активен, ошибок по DC01 нет. Если `wecutil` ругается на схему — создай подписку мышкой (`Event Viewer → Subscriptions → Create Subscription`) и выгрузи её как эталон: `wecutil gs Vektor-Security /f:xml` (этот XML всегда валиднее написанного руками).
10. **FS01.** Проверь, что события доехали:
    ```powershell
    Get-WinEvent -LogName ForwardedEvents -MaxEvents 20 |
      Select-Object TimeCreated, Id, ProviderName, MachineName | Format-Table -AutoSize
    ```
    **Ожидаемо:** строки с Id 4624/4625 и 7000-серией, в `MachineName` — исходная машина (если пусто, открой XML события: имя источника есть в поле `Computer`).
11. **FS01.** Проверь глазами через GUI: `eventvwr.msc → Windows Logs → ForwardedEvents`. **Ожидаемо:** те же события, в тексте сообщения указан компьютер-источник.
12. **DC01.** Дозадай порог срабатывания: найди в Security событие 4624 и посмотри поля `LogonType` и `IpAddress` через `ToXml()`. **Ожидаемо:** тип 3 и адрес WIN11-CL01 — ты научился отвечать на вопрос «откуда пришёл вход» без догадок.
13. **Хост.** Запиши в `stand-log.md`: что включено, что настроено, какие файлы созданы.

### Проверка

- [ ] На DC01 в Security появились события 4624 и 4625 после шага 5.
- [ ] Ты можешь показать, где в XML лежат `TargetUserName`, `LogonType`, `IpAddress`, `Status`.
- [ ] `wevtutil gl Security` показывает `maxSize: 268435456`.
- [ ] `auditpol /get /category:*` показывает включённый Logon (Success and Failure).
- [ ] Служба `Wecsvc` на FS01 работает.
- [ ] `wecutil es` показывает подписку `Vektor-Security`, `wecutil gr` — активна.
- [ ] В `ForwardedEvents` на FS01 есть события с DC01.
- [ ] В `stand-log.md` записаны все изменения.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Вчера вечером компьютер вырубился сам, сегодня утром опять всё зависло. Отчёт не сохранился. Сделайте, чтобы больше не повторялось.»
> **Срочность:** средняя

**Как воспроизвести поломку.** На хосте в VMware Workstation для WIN11-CL01 сделай **VM → Power → Power Off** (именно Power Off, а не Shut Down Guest и не Restart Guest). Это эмуляция «выдернули питание». Затем включи ВМ и дай ей загрузиться. В журналах появится ровно та картина, что видит инженер по такой заявке.

**Диагностика по шагам (именно в этом порядке):**

1. Собери таймлайн за трое суток одной командой:
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,1074,6008,6005,6006,6013,1001,12,13;
     StartTime=(Get-Date).AddDays(-3)} | Sort-Object TimeCreated |
     Select-Object TimeCreated, Id, ProviderName | Format-Table -AutoSize
   ```
2. **Есть 1074?** Смотри поля: имя процесса и пользователь, от чьего имени инициировано завершение. Если 1074 есть — это плановое выключение (обновления, админ, скрипт), и дальше разбираешься с источником, а не с железом.
3. **1074 нет, а 6008 + 41 есть** — завершение было некорректным. Время 6008 — момент «смерти».
4. **Есть 1001 (BugCheck)?** Значит был синий экран: смотри код `0x…` и `C:\Windows\Minidump\*.dmp`; разбор дампа — тема урока 11.
5. **1001 нет** — идём в железо и гипервизор. В лабе: журнал задач VMware на хосте (ВМ была выключена снятием питания). В реальности: ИБП, журнал IPMI/iLO, температура, кабели питания.
6. **Проверь диск:** Disk 51/153, Ntfs 55/140 за тот же период. Зависания на вводе-выводе дают и «залипание на минуту», и отвал.
7. **Проверь, не «помог» ли кто-то извне:** Security 4647/4634, RDP-события 1149 в `TerminalServices-RemoteConnectionManager/Operational`, установка обновлений в `Setup` ночью.
8. **Проверь `System Properties → Advanced → Startup and Recovery → Automatically restart`.** Если галочка стоит, BSOD уходит в перезагрузку и пользователь видит не «синий экран», а «само перезагрузилось» — классика.

**Решение (в лабе):** фиксируем причину — некорректное отключение питания (в нашем случае это действие администратора в VMware). Дополнительно проверяем, что ВМ стартует штатно и 7000-серия не сыпется.

**Ответ пользователю:**
> Иван, посмотрел журналы. В 18:42 и в 9:12 система завершилась некорректно — события 41 и 6008, плановой перезагрузки (1074) не было. Синних экранов и ошибок диска тоже нет. Похоже на пропадание питания/аппаратное зависание — это не сбой Windows. Что сделать с твоей стороны: включить автосохранение в Excel/1С, не выключать ПК кнопкой и, если повторится, сразу записать точное время. Со своей стороны проверю питание и настройки электропитания.

**Закрытие заявки:** категория «Оборудование / стабильность», причина «некорректное завершение питания», действие «диагностика по журналам, рекомендации пользователю», профилактика «автосохранение + контроль ИБП», ссылка на KB-запись в базе знаний (глава 5 трека).

### Что изменилось на стенде

- **DC01:** размер журнала Security увеличен до 256 МБ; включена расширенная политика аудита (Logon, Account Lockout, User Account Management); в группе `Event Log Readers` (Builtin в AD) появилась учётка компьютера `VEKTOR\FS01$`; WinRM включён, слушатель HTTP/5985.
- **WIN11-CL01:** WinRM включён; `VEKTOR\FS01$` добавлена в локальную группу `Event Log Readers`.
- **FS01:** включена служба Windows Event Collector (`Wecsvc`), создана подписка `Vektor-Security` (collector-initiated, источники — DC01), файл `C:\WEF\vektor-security.xml`, канал `ForwardedEvents` наполняется.
- **Домен:** следов в AD, кроме членства в Builtin/Event Log Readers, нет; учётные записи и группы не менялись.
- **Пароли/адреса/папки:** без изменений.

### Откат

```powershell
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" revertToSnapshot "D:\VMs\DC01\DC01.vmx" before-l2-010
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" revertToSnapshot "D:\VMs\FS01\FS01.vmx" before-l2-010
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" revertToSnapshot "D:\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l2-010
```
Или мышкой: VM → Snapshot → Snapshot Manager → выбрать `before-l2-010` → Go To. После отката допиши в `stand-log.md` строку «откатился к before-l2-010, изменения такие-то потеряны». Если хочешь сохранить настройки аудита для следующих глав — не откатывайся, оставь как новое состояние стенда.

## 6. Ошибки новичков и безопасность

1. **Читать только колонку Message.** → Не видишь `IpAddress`, `LogonType`, `Status`, теряешь половину улик. → Правильно: `ToXml()` и имена полей.
2. **Фильтровать в PowerShell через `Where-Object` по всему журналу.** → На больших логах команда висит минутами и ест RAM, на сервере с 100k событий — таймаут. → Правильно: `-FilterHashtable` или `-FilterXPath`, фильтрация на стороне службы.
3. **Чистить журнал «чтобы не шумел».** → Уничтожаешь доказательства, получаешь событие 1102 и в нормальной компании — разбор с ИБ. → Правильно: увеличить размер, настроить ротацию, выгрузить `wevtutil epl`, отключить лишние подкатегории аудита.
4. **Искать виноватого по одному 4625.** → Не учитываешь тип входа: Type 3 — это сеть/сервис, Type 2 — консоль, Type 10 — RDP; и не correlируешь с 4771/4740. → Правильно: смотреть связку 4625 + 4771 + 4740 и поле `IpAddress`.
5. **Включить аудит Object Access на файловом сервере «на всякий случай».** → Security-лог переполняется за часы, растёт нагрузка на диск, найти в нём что-то невозможно. → Правильно: включать точечно, на конкретную папку через SACL, с увеличенным логом.
6. **Думать, что журнал бесконечный.** → Событие «пропало» именно тогда, когда понадобилось. → Правильно: знать `MaximumSizeInBytes` и `LogMode` каждого канала, который тебе важен.
7. **Верить времени события без проверки синхронизации.** → События с DC и с клиента «не сходятся» на минуты, Kerberos вообще валится при разнице больше 5 минут. → Правильно: сверять `w32tm /query /status` и часовой пояс.
8. **Игнорировать Applications and Services Logs.** → Ищешь причину «отвалился RDP» в System, а она в `TerminalServices-*`, ищешь проблемы GPO — а они в `GroupPolicy/Operational`. → Правильно: сначала подумать, какой компонент виноват, потом открывать его канал.

**Безопасность.** Журналы — это персональные данные и доказательства. В 4624/4625 лежат имена учётных записей и IP-адреса сотрудников; при включённом аудите доступа к файлам — имена файлов и папок, то есть содержимое работы бухгалтерии и HR. Всё это нельзя бесконтрольно копировать на флешку, отправлять в личные облака и «на всякий случай» пересылать в чат — по 152-ФЗ это обработка ПДн, и в компании должны быть регламент и согласование. WEF по HTTP (5985) идёт **открытым текстом**: в учебном стенде это допустимо, в бою — HTTPS 5986 с сертификатом (пригодится глава 15 про PKI). Права на чтение Security-лога выдаются через `Event Log Readers`, а не через доменного администратора: администратор, который читает журналы аудита, — это тот, кто может замести следы. И последнее: очистка журнала и изменение политики аудита (1102, 4719) в нормальной среде — это инцидент ИБ с уведомлением, а не «техническая мелочь».

## 7. Вопросы на собеседовании

**1. «Пользователь говорит, что компьютер перезагрузился сам. С чего начнёшь?»**
Ответ: с таймлайна в System log за нужный период по ID 41, 6008, 1074, 1001, 6005/6006, 12/13. Если есть 1074 — выясняю, какой процесс и какой пользователь инициировал (планово). Если 1074 нет, а есть 6008+41 — некорректное завершение, смотрю время. Дальше: 1001 (был ли BugCheck, есть ли дамп в `C:\Windows\Minidump`), ошибки диска (Disk 51/153, Ntfs 55), недавние обновления в Setup, галочка Automatically restart. Параллельно — питание/ИБП и журнал гипервизора.
*Что хочет услышать интервьюер:* структурный порядок «плановое или нет → было ли BSOD → железо», а не «переустановлю Windows».
*Красный флаг:* «посмотрю, что пишет пользователь»; сразу переустановка/замена железа; не упомянуть 1074.

**2. «Как найти все неудачные входы за сутки по конкретному пользователю?»**
Ответ: включённый аудит Logon (иначе событий нет), затем `Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4625; StartTime=(Get-Date).AddHours(-24)}` и разбор XML-полей `TargetUserName`, `LogonType`, `IpAddress`, `Status/SubStatus`. Дополняю 4771 (Kerberos pre-auth failed, код 0x18 — неверный пароль) и 4740 (блокировка). Смотрю тип входа: 2 — консоль, 3 — сеть/сервис, 10 — RDP. Для читаемости фильтрую по пользователю уже после выборки по ID и времени.
*Что хочет услышать интервьюер:* знание, что без включённой политики аудита ничего нет; понимание Logon Type и SubStatus; фильтрация на стороне службы.
*Красный флаг:* путает 4624 и 4625; ищет по тексту сообщения; не знает про типы входа.

**3. «Как собрать журналы с 50 серверов в одно место?»**
Ответ: Windows Event Forwarding — выделенный сборщик (Windows Server) со службой Windows Event Collector, подписки, транспорт WinRM (5985/5986), канал ForwardedEvents. Для 50+ машин — **source-initiated** подписка: список сборщиков раздаётся GPO (`Configure target Subscription Manager`), учётки компьютеров-источников добавляются в `Event Log Readers` на сборщике. В подписке — узкий набор ID и уровней (Critical/Error + 4624/4625/4740), иначе объём убьёт и сеть, и диск. Дальше — SIEM/мониторинг и ретеншн. Обязательно: контроль времени (NTP) и HTTPS с сертификатом.
*Что хочет услышать интервьюер:* WEF + WEC, разница collector/source-initiated, порты, ограничение объёма фильтром.
*Красный флаг:* «просто зайду на каждый сервер»; «поставлю агент на 50 машин и буду опрашивать PowerShell'ом по кругу»; не думать про объём.

**4. «Security-журнал перезаписывается, старые события теряются. Что делать?»**
Ответ: увеличить `MaximumSizeInBytes` (через `wevtutil sl` или GPO), выбрать режим ротации (для аудита — «Archive when full» или «Do not overwrite» с автoархивацией), проверить, что именно аудитится: убрать лишние подкатегории (типичные пожиратели — Object Access, Process Creation, Detailed Tracking), добавить фильтры и вынести нужные события сборщику. Очистку «руками» не использовать: это уничтожение доказательств, событие 1102.
*Что хочет услышать интервьюер:* размер+ротация+сокращение объёма+централизованный сбор; понимание, что чистка лога — не решение.
*Красный флаг:* «сейчас очищу»; «отключу аудит, чтобы не мешал».

**5. «Что такое Event ID 4624 с Logon Type 3? А с Type 10?»**
Ответ: 4624 — успешный вход. Type 3 — сетевой вход (SMB к шаре, обращение к службе, проверка доступности), крайне частый и обычно «шумовой»: под ним видно, кто ходит по сети под сервисными учётками. Type 10 — вход через RDP (RemoteInteractive). Type 2 — консольный, Type 5 — вход службы. Понимание типа критично: «пользователь не входил» может быть правдой для консоли и ложью для сети.
*Что хочет услышать интервьюер:* различает типы входа и умеет отделять нормальный сетевой шум от аномалии.
*Красный флаг:* «не знаю, какая разница»; считает любой 4624 «взломом».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Event Log» (обзор архитектуры, каналы, формат `.evtx`). Поиск: `Windows Event Log reference site:learn.microsoft.com`.
- **learn.microsoft.com** — справочник по командлетам `Get-WinEvent` и `Get-EventLog` (ключи, `-FilterHashtable`, `-FilterXPath`). Поиск: `Get-WinEvent FilterHashtable site:learn.microsoft.com`.
- **learn.microsoft.com** — статья «wevtutil» (все подкоманды и параметры). Поиск: `wevtutil commands site:learn.microsoft.com`.
- **learn.microsoft.com** — «Advanced security audit policy settings» (список подкатегорий, GUID'ов и рекомендаций). Поиск: `advanced security audit policy settings site:learn.microsoft.com`.
- **learn.microsoft.com** — «Monitoring Active Directory for Signs of Compromise» и «Events to Monitor» (эталонный список Event ID для ИБ-мониторинга). Поиск: `events to monitor Windows Server site:learn.microsoft.com`.
- **learn.microsoft.com** — «Windows Event Forwarding» / «Use Windows Event Forwarding to help with intrusion detection» (WEC, подписки, source-initiated). Поиск: `Windows Event Forwarding subscription manager site:learn.microsoft.com`.
- **learn.microsoft.com/sysinternals** — утилиты для безопасного воспроизведения сбоев (например, NotMyFault) в тестовой среде. Поиск: `Sysinternals NotMyFault`.
- Точные значения размеров журналов по умолчанию и локализованные имена подкатегорий аудита зависят от версии и языка — проверяй на стенде командами `wevtutil gl <log>` и `auditpol /get /category:*` `[ПРОВЕРИТЬ]`; то же касается актуальных номеров событий, добавляемых новыми сборками Windows.
