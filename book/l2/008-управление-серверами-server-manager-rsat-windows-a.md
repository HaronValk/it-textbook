# Управление серверами: Server Manager, RSAT, Windows Admin Center, PowerShell Remoting

## 1. Введение (зачем это нужно в реальной работе)

Тикет в конце месяца:

```text
Пользователь: ivanov (Иван Иванов, бухгалтерия)
ВМ: WIN11-CL01
Что написал: «Не печатает принтер на FS01, задания висят в очереди.
И \\FS01\Buh открывается через раз. У нас закрытие месяца, срочно!»
Срочность: высокая
```

Ты сидишь за WIN11-CL01. Бежать в серверную (в нашем случае — переключаться в консоль VMware на FS01) каждый раз, когда надо посмотреть службу, — это путь L1. L2 управляет серверами удалённо и делает это сразу несколькими инструментами, потому что у каждого своя зона: Server Manager даёт обзор парка серверов, RSAT — привычные оснастки (AD, DNS, GPO) с рабочего места, Windows Admin Center — веб-панель с производительностью и файлами, а PowerShell Remoting — массовые операции одной командой на десять серверов сразу.

После этой главы ты должен: понимать, какой протокол и порт стоит за каждым из четырёх инструментов; уметь поднять и проверить канал управления; уметь выполнить одну команду на нескольких серверах и забрать результат; понимать, почему один инструмент «не видит» сервер, а другой видит; и знать, что делать, когда канал управления умер совсем (спойлер: идти в консоль гипервизора — и это нормальный инженерный приём, а не позор).

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Все четыре инструмента — это разные обёртки над двумя каналами до сервера:

**1. WinRM (Windows Remote Management)** — реализация протокола WS-Management от Microsoft. Служба `WinRM` слушает TCP **5985** (HTTP) и **5986** (HTTPS). Аутентификация — Kerberos (в домене), NTLM или Basic (Basic — только по HTTPS, иначе пароль уходит почти открытым текстом). Через WinRM работают: PowerShell Remoting, Windows Admin Center, `Get-CimInstance -ComputerName`, `Get-WindowsFeature -ComputerName`, сам Server Manager (частично).

**2. RPC/DCOM (TCP 135 + динамические порты)** — старый добрый канал Windows: оснастки MMC (`services.msc`, `compmgmt.msc`, `eventvwr.msc` с подключением к другому ПК), `sc.exe \\SERVER`, `Get-Service -ComputerName`. Важно: `Get-Service -ComputerName FS01` **не использует WinRM** — это частая путаница на собеседовании и на разборе инцидентов.

**3. LDAP (389/636)** — по нему RSAT-оснастки (ADUC, GPMC, DNS) читают и меняют каталог.

Начиная с Windows Server 2012 WinRM на серверах **включён по умолчанию**; на клиентских Windows — нет.

```text
                     WIN11-CL01 — рабочее место админа
   ┌──────────────────┬────────────────────┬──────────────────────┐
   │ dsa.msc, gpmc.msc│ Server Manager     │ Браузер → WAC :443   │
   │ (RSAT, MMC)      │ (пул серверов)     │ https://win11-cl01   │
   └────────┬─────────┴──────────┬─────────┴───────────┬──────────┘
            │ LDAP 389/636       │ WinRM 5985         │ WinRM 5985 (к цели)
            │ RPC 135+dynamic    │ RPC 135 (частично) │ SMB 445 (файлы)
            ▼                    ▼                    ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ DC01 10.10.10.10   DC02 10.10.10.11   FS01 10.10.10.20         │
   │ SRV1C01 10.10.10.25                                            │
   └────────────────────────────────────────────────────────────────┘
        Invoke-Command -ComputerName DC01,DC02,FS01 — одна команда,
        три сервера, три ответа в одной таблице
```

**Windows Admin Center (WAC)** — это веб-приложение (шлюз), которое ставится на Windows Server или Windows 10/11 и слушает **443** с сертификатом. Ты открываешь браузер, а шлюз уже сам ходит к целевым серверам по WinRM (и по SMB **445**, когда показывает файлы). Отсюда правило: если WAC не подключается к серверу — проблема почти всегда в WinRM/кредах, а не в самом WAC.

**RSAT** (Remote Server Administration Tools) в Windows 10/11 — это не отдельный установщик, а набор «возможностей по требованию» (Features on Demand), которые ставятся через параметры системы. Ставить их на домашнюю редакцию Windows официально не поддерживается — нужна Pro/Enterprise [ПРОВЕРИТЬ].

Ещё одно понятие, без которого ты застрянешь: **double-hop**. Ты зашёл по `Enter-PSSession` на FS01 и оттуда пытаешься обратиться к DC01 — и получаешь отказ, потому что твой Kerberos-билет не передаётся на второй хоп (защита от кражи билетов). Решается делегированием Kerberos, JEA или выполнением операции с исходной машины, а не через CredSSP «по-быстрому».

## 3. Аналогия из жизни

Представь, что ты диспетчер, а серверы — это цеха на другом конце города. Server Manager — сводное табло на стене диспетчерской: лампочки по всем цехам, но показывает только то, что цех сам решил транслировать. RSAT — ящик с прямыми телефонами: отдельный аппарат в бухгалтерию (AD), отдельный в DNS, отдельный в политики; звонишь и говоришь с конкретной службой напрямую. Windows Admin Center — видеозвонок с планшетом: видишь картинку, можешь ткнуть пальцем и перезапустить станок, но для звонка нужна исправная линия и пропуск. PowerShell Remoting — рация на общей волне: одну фразу слышат все цеха сразу и отвечают хором, но если ты не представился (не прошёл аутентификацию) — тебя просто не слушают.

Аналогия ломается там, где «связь» в Windows не про расстояние, а про конкретную комбинацию службы, порта, правила фаервола и типа аутентификации — и ломается каждая из этих частей независимо.

## 4. Практика и команды

**1. Поднять канал управления на цели (FS01, DC02 — на их консоли или в сессии).**

```powershell
Enable-PSRemoting -Force
winrm enumerate winrm/config/listener
```

`-Force` — без интерактивных подтверждений (в лабе; в продакшене понимай, что именно разрешаешь). `winrm enumerate` показывает, есть ли слушатель:

```text
Listener
    Address = *
    Transport = HTTP
    Port = 5985
    Enabled = true
```

Если слушателя нет — WinRM не настроен, и никакие удалённые инструменты не заработают.

**2. Проверить доступность канала с рабочего места.**

```powershell
Test-NetConnection FS01 -Port 5985 -InformationLevel Quiet
Test-WSMan -ComputerName FS01
```

Первая — сетевой уровень (TCP). Вторая — уровень WS-Management: если `Test-NetConnection` даёт `True`, а `Test-WSMan` падает с ошибкой доступа — сеть в порядке, проблема в правах/аутентификации. `Test-WSMan` возвращает `ProductVersion`, `StackVersion` — по ним видно, что отвечает именно WinRM.

**3. Интерактивная сессия 1:1.**

```powershell
Enter-PSSession -ComputerName FS01
# приглашение сменилось: [FS01]: PS C:\Users\adm.student\Documents>
hostname
Get-Service Spooler
Exit-PSSession
```

Все команды выполняются **на FS01**, а не у тебя. Признак: приглашение с именем сервера. Забыл про `Exit-PSSession` — висишь в чужой сессии и правишь не ту машину.

**4. Одна команда на несколько серверов (главный приём L2).**

```powershell
Invoke-Command -ComputerName DC01,DC02,FS01 -ScriptBlock {
    [pscustomobject]@{
        Host    = $env:COMPUTERNAME
        Uptime  = (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
        PSver   = $PSVersionTable.PSVersion.ToString()
        FreeGB  = [math]::Round((Get-PSDrive C).Free/1GB,1)
    }
} | Format-Table -AutoSize
```

`-ComputerName` — список целей (параллельно, по умолчанию до 32 одновременно, `-ThrottleLimit` меняет); `-ScriptBlock` — что выполнить; `-AsJob` + `Receive-Job` — если серверов десятки и ждать вживую не хочется. В выводе появляется колонка `PSComputerName` — по ней видно, чей ответ. Смотри на строки с ошибками: молча пропущенный сервер — это тоже результат.

**5. Постоянная сессия: переиспользовать и не плодить подключения.**

```powershell
$s = New-PSSession -ComputerName FS01 -Name fs01-mgmt
Get-PSSession
Invoke-Command -Session $s -ScriptBlock { Get-Service Spooler }
Remove-PSSession $s
```

`-Name` даёт читаемое имя. Ещё есть `Disconnect-PSSession` / `Connect-PSSession` — сессия живёт на сервере, ты можешь отключиться и вернуться (удобно на долгих задачах). Не забывай `Remove-PSSession`: висящие сессии держат память на сервере.

**6. Копирование файлов через сам канал управления (без SMB).**

```powershell
Copy-Item -Path .\audit.txt -Destination C:\Temp\ -ToSession $s
Copy-Item -Path C:\Temp\audit.txt -FromSession $s -Destination .\from-fs01.txt
```

`-ToSession` / `-FromSession` работают поверх WinRM. Это спасение, когда SMB (445) закрыт фаерволом, а управлять надо.

**7. Инвентаризация без PowerShell-сессии — через CIM.**

```powershell
Get-CimInstance -ClassName Win32_LogicalDisk -ComputerName FS01 -Filter "DeviceID='C:'" |
    Select-Object PSComputerName, DeviceID, @{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}}
```

`-ClassName` — класс WMI/CIM; в PowerShell 5.1 `Get-CimInstance -ComputerName` по умолчанию идёт через WinRM (WSMan). То же через `-CimSession` для批量. Используй для опроса без запуска «полноценного» ремоутинга.

**8. Поставить RSAT на WIN11-CL01 и открыть оснастки.**

```powershell
Get-WindowsCapability -Online -Name "Rsat*" | Where-Object State -ne Installed |
    Select-Object Name
Add-WindowsCapability -Online -Name "Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0"
Add-WindowsCapability -Online -Name "Rsat.GroupPolicy.Management.Tools~~~~0.0.1.0"
Add-WindowsCapability -Online -Name "Rsat.Dns.Tools~~~~0.0.1.0"   # [ПРОВЕРИТЬ точное имя]
Get-Command -Module ActiveDirectory | Measure-Object
```

GUI-путь: **Параметры → Система → Дополнительные компоненты → Просмотреть компоненты → ввести «RSAT»**. После установки доступны `dsa.msc` (ADUC), `dnsmgmt.msc`, `gpmc.msc`, `dhcpmgmt.msc`. `Get-Command -Module ActiveDirectory` подтверждает, что модуль PowerShell для AD тоже приехал.

**9. Server Manager: собрать пул серверов и группу.**

```text
ServerManager.exe → Manage (Управление) → Add Servers → вкладка DNS
→ поиск corp.vektor.lab → выбрать DC01, DC02, FS01 → Add
→ Manage → Create Server Group → имя "Vektor-Servers"
```

После этого на одном экране видны роли, события и состояние служб управления (WinRM) по всем трём серверам. Красный «Manageability: Online — data retrieval failed» — это и есть индикатор проблем с каналом.

**10. Узнать роли на сервере, не заходя на него.**

```powershell
Get-WindowsFeature -ComputerName FS01 | Where-Object Installed |
    Select-Object Name, DisplayName
```

Работает только через WinRM (параметр `-ComputerName`). Если падает — смотри пункты 1 и 2. `Install-WindowsFeature -ComputerName FS01 -Name ... -Restart` умеет и ставить роли удалённо.

**11. Разница каналов RPC vs WinRM — на одном примере.**

```powershell
Get-Service -ComputerName FS01 -Name Spooler    # RPC 135 + динамика
Invoke-Command -ComputerName FS01 -ScriptBlock { Get-Service Spooler }  # WinRM 5985
```

Если первая работает, а вторая нет — сервер жив, сеть жива, сломан именно WinRM. Это половина диагностики.

**12. Заглянуть в Linux-часть стенда (на будущее, для DevOps-трека).**

```powershell
# PowerShell 7+, на UBU01 должен быть включён SSH
Enter-PSSession -HostName adm.student@10.10.10.30 -SSHTransport
```

`-HostName` + `-SSHTransport` — ремоутинг поверх SSH (22), а не WinRM. Полезно помнить: «PowerShell Remoting» ≠ «только Windows».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- DC01 (10.10.10.10), DC02 (10.10.10.11), FS01 (10.10.10.20) — в домене `corp.vektor.lab`, включены, WinRM по умолчанию активен.
- WIN11-CL01 — включена, вход под `VEKTOR\adm.student` (в лабе работаем доменным админом; в реальной работе для повседневных задач так не делают — см. раздел 6).
- DNS на WIN11-CL01 указывает на 10.10.10.10 (иначе имена `FS01`/`DC02` не разрешатся).
- Всё, что нужно по предыдущим урокам (домен, второй DC, файловый сервер), уже сделано.

### Снапшот

В VMware Workstation: выделить вкладку каждой ВМ → **VM → Snapshot → Take Snapshot**, имя `before-l2-008`. Сделать на **DC01, DC02, FS01, WIN11-CL01**. Причина: ставим RSAT и WAC на клиентскую ВМ и меняем состояние WinRM на серверах.

### Шаги

1. **WIN11-CL01.** `Test-NetConnection FS01 -Port 5985` → ожидаем `TcpTestSucceeded : True`.
2. **WIN11-CL01.** `Test-WSMan -ComputerName FS01` → в выводе `ProductVersion` и `StackVersion`, ошибок нет.
3. **WIN11-CL01.** `Enter-PSSession -ComputerName FS01`, внутри выполнить `hostname` и `Get-Service Spooler`, выйти `Exit-PSSession`. Ожидаемо: приглашение `[FS01]:`, `hostname` = `FS01`, служба `Running`.
4. **WIN11-CL01.** `Invoke-Command` из пункта 4 раздела 4 по `DC01,DC02,FS01`. Ожидаемо: три строки таблицы с разными `Host` и `Uptime`.
5. **WIN11-CL01.** Создать `"audit $(Get-Date)" | Out-File .\audit.txt`; `$s = New-PSSession -ComputerName FS01`; `Copy-Item .\audit.txt -Destination C:\Temp\ -ToSession $s`. Ожидаемо: файл появился на FS01 (проверить `Invoke-Command -Session $s { Get-ChildItem C:\Temp }`). `Remove-PSSession $s`.
6. **WIN11-CL01.** Установить RSAT (пункт 8 раздела 4): AD DS + Group Policy + DNS. Ожидаемо: `Get-Command -Module ActiveDirectory` отдаёт десятки команд.
7. **WIN11-CL01.** Открыть `dsa.msc` — видим OU `Company`, `Компьютеры`, `Серверы`, пользователей `ivanov`, `petrova`, `sidorov`. Открыть `dnsmgmt.msc` → правый клик на «DNS» → **Connect to DNS Server** → `DC01` → видим зону `corp.vektor.lab` с записями FS01, DC02.
8. **WIN11-CL01.** Server Manager → Add Servers → добавить DC01, DC02, FS01 → создать группу `Vektor-Servers`. Ожидаемо: у всех трёх в колонке Manageability — `Online`.
9. **WIN11-CL01.** `Get-WindowsFeature -ComputerName FS01 | Where-Object Installed` → видим `File-Services` / `Print-Server`.
10. **WIN11-CL01.** Скачать Windows Admin Center с официального сайта Microsoft (aka.ms/WACDownload), установить тихо:
    ```powershell
    msiexec /i .\WindowsAdminCenter.msi /qn /L*v wac-install.log SME_PORT=443 SSL_CERTIFICATE_OPTION=generate
    ```
    Открыть в браузере `https://win11-cl01` (сертификат самоподписанный — браузер ругнётся, это ожидаемо в лабе). Добавить подключение к `FS01`: **Add → Servers → FS01**. Ожидаемо: панель Overview с CPU/RAM, вкладки Services, Files, PowerShell.
11. **WIN11-CL01.** В WAC на FS01 открыть вкладку PowerShell и выполнить `Get-Service Spooler`. Ожидаемо: тот же результат, что через `Enter-PSSession`, — потому что канал один.
12. Записать в `stand-log.md`: что установлено, какие серверы добавлены, какие сессии закрыты.

### Проверка

- [ ] `Test-WSMan FS01` и `Test-WSMan DC02` завершаются без ошибок.
- [ ] `Invoke-Command` по трём серверам вернул три строки, без красных ошибок.
- [ ] Файл `audit.txt` реально лежит в `C:\Temp` на FS01.
- [ ] `dsa.msc` показывает структуру OU домена `corp.vektor.lab`.
- [ ] В Server Manager все три сервера в статусе `Online`.
- [ ] WAC открывается по `https://win11-cl01`, FS01 добавлен и отдаёт данные.
- [ ] `Get-PSSession` на WIN11-CL01 пуст (сессии закрыты).
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

```text
Пользователь: ivanov (Иван Иванов, бухгалтерия)
ВМ: WIN11-CL01
Что написал: «Принтер на FS01 не печатает, задания висят в очереди.
\\FS01\Buh вроде открывается. У нас закрытие месяца!»
Срочность: высокая
```

**Как воспроизвести поломку.** На FS01 (в консоли VMware) выполнить от админа:

```powershell
Stop-Service Spooler -Force
Set-Service Spooler -StartupType Disabled
Stop-Service WinRM -Force
Set-Service WinRM -StartupType Disabled
Disable-NetFirewallRule -DisplayGroup "Windows Remote Management"
```

Смысл: у пользователя отваливается печать (Spooler), а у тебя одновременно — канал управления (WinRM). Так бывает после «оптимизации» сервера коллегой или кривого обновления.

**Диагностика по шагам (порядок важен).**

1. Сеть жива? С WIN11-CL01: `Test-NetConnection FS01 -Port 445` → `True`. Сервер доступен, дело не в сети.
2. Канал управления: `Test-NetConnection FS01 -Port 5985` → `False`. `Test-WSMan FS01` → ошибка. WinRM лежит — значит `Invoke-Command` и WAC работать не будут.
3. А RPC? `Get-Service -ComputerName FS01 -Name Spooler` → приходит ответ: `Status: Stopped`, `StartType: Disabled`. **Вывод: сервер жив, сломаны две вещи — Spooler и WinRM.** Это и есть разделение каналов из раздела 2.

   Если RPC-запрос не проходит — проверить правила группы «Remote Service Management» в фаерволе FS01 [ПРОВЕРИТЬ набор правил по умолчанию для вашего профиля].
4. Поднять Spooler удалённо по RPC:

   ```cmd
   sc.exe \\FS01 config Spooler start= auto
   sc.exe \\FS01 start Spooler
   ```

   GUI-альтернатива: `services.msc` → **Action → Connect to another computer → FS01** → Spooler → Start.
5. WinRM по RPC не поднять надёжно (службу запустить можно, но слушатель и правило фаервола настраиваются локально). Идём **в консоль VMware на FS01** — это и есть out-of-band доступ; в реальной работе его роль играют iLO/iDRAC/IPMI или KVM-over-IP:

   ```powershell
   Set-Service WinRM -StartupType Automatic
   Start-Service WinRM
   Enable-PSRemoting -Force
   Get-Service Spooler, WinRM | Select-Object Name, Status, StartType
   ```
6. Проверка с WIN11-CL01: `Test-WSMan FS01` → ок; `Invoke-Command -ComputerName FS01 { Get-Service Spooler }` → `Running`.
7. Проверить очередь печати: в WAC на FS01 → **Services → Spooler**, либо `Get-Printer -ComputerName FS01`. Попросить ivanov отправить тестовую страницу.

**Ответ пользователю:**

```text
Иван, добрый день. Причина найдена: на файловом сервере остановилась служба
диспетчера печати (Spooler) — она отвечает за очередь печати. Службу запустил,
автозапуск вернул. Заодно починил канал удалённого управления сервером,
из-за которого диагностика заняла дольше.
Пожалуйста, отправьте документ на печать ещё раз и напишите, если задание
снова зависнет. Заявку закрываю, при повторе — сразу звоните.
```

**Закрытие заявки:** категория «Печать / служба Spooler», root cause — «служба Spooler остановлена и переведена в Disabled; дополнительно отключён WinRM», решение — «запуск служб, восстановление автозапуска, проверка очереди», профилактика — «мониторинг службы Spooler и WinRM на FS01 (пригодится в главе про Zabbix/MON01)». Если такое повторится — искать, кто и зачем меняет конфигурацию (журнал событий, глава 10).

### Что изменилось на стенде

- **WIN11-CL01:** установлены RSAT-компоненты (AD DS/LDS Tools, Group Policy Management, DNS Tools), установлен Windows Admin Center (шлюз на порту 443, самоподписанный сертификат). ВМ временно используется как рабочее место администратора — в `stand-log.md` отметить, что в реальной среде под это выделяют отдельный хост/ jump-сервер.
- **Server Manager на WIN11-CL01:** создана группа серверов `Vektor-Servers` (DC01, DC02, FS01) — это локальный профиль пользователя, на серверы не влияет.
- **FS01:** появился каталог `C:\Temp` с файлом `audit.txt`; состояние служб Spooler и WinRM — `Running`, автозапуск `Automatic`; правило фаервола «Windows Remote Management» включено.
- **DC01, DC02:** без изменений в ролях; WinRM активен (по умолчанию).
- **Учётные записи и адреса:** без изменений.

### Откат

Если что-то пошло не так (например, WAC не ставится или RSAT «сломал» клиентскую ВМ): выключить затронутые ВМ → **VM → Snapshot → Snapshot Manager** → выбрать `before-l2-008` → **Go to**. Откатывать на всех четырёх ВМ, иначе состояния разъедутся (на FS01 восстановятся Spooler/WinRM в исходном виде, на WIN11-CL01 исчезнут RSAT и WAC). После отката — запись в `stand-log.md` о том, что лабораторная откачена и почему.

## 6. Ошибки новичков и безопасность

1. **Ставить WAC на контроллер домена или на файловый сервер «чтобы под рукой было».** → Веб-шлюз с сертификатом на критичном сервере = лишняя поверхность атаки и лишняя перезагрузка при обновлении. → Правильно: отдельный management-хост (в нашем стенде допустимо на клиентской ВМ, но с пометкой в журнале).
2. **Путать каналы.** «`Get-Service -ComputerName` работает, значит WinRM жив» → неверно (это RPC), и на диагностике теряется полчаса. → Проверяй канал тем инструментом, который используешь.
3. **Работать везде доменным админом.** → Один фишинг/один `Invoke-Command` с чужой машины — и скомпрометирован весь домен. → Отдельная учётка администратора, повышение прав по необходимости, JEA для типовых операций.
4. **Решать double-hop через CredSSP «потому что быстро».** → CredSSP передаёт открытые креды на второй хоп; при компромате первой машины крадутся креды второй. → Kerberos constrained delegation / RBCD / JEA либо переписать задачу так, чтобы команда шла с исходной машины.
5. **`Set-Item WSMan:\localhost\Client\TrustedHosts -Value "*"`** в рабочей группе. → Аутентификация NTLM куда угодно, MITM подсовывает свой сервер. → Перечислять конкретные хосты, лучше — домен и Kerberos.
6. **Оставлять открытые `PSSession` / `Disconnect-PSSession` на неделю.** → Съеденная память сервера и висящие конфигурации оболочки. → `Get-PSSession | Remove-PSSession` после работы, `IdleTimeout` в конфиге.
7. **«Server Manager показывает Online — значит всё хорошо».** → Данные в Server Manager кэшируются и обновляются не мгновенно; зелёная лампочка не означает, что служба не упала минуту назад. → Проверять состояние конкретной службы/счётчика, а не только обзор.
8. **Копировать скрипты с паролем в открытом виде** (`ConvertTo-SecureString "Lab#2026-vektor" -AsPlainText -Force`). → Пароль в истории команд и в Git. → `Get-Credential`, Windows Credential Manager, gMSA/Managed Identity.

### Безопасность

- **WinRM по HTTP (5985) — только внутри доверенной сети.** С Kerberos/NTLM содержимое сообщений шифруется на уровне протокола, но метаданные и сам факт обмена видны. Через недоверенные сегменты — только 5986 с валидным сертификатом.
- **Basic-аутентификация в WinRM = почти открытый пароль** (base64). Включать только на изолированном стенде и никогда — на боевых серверах.
- **Нельзя выставлять 5985/5986 и 443 (WAC) в интернет.** Управляющий контур — во внутренней сети, доступ через VPN или jump-host с MFA.
- В лабе мы работаем доменным админом и держим простой пароль `Lab#2026-vektor` — это допустимо только на учебном стенде.
- `Enable-PSRemoting -Force` расширяет права удалённого выполнения. В продакшене перед этим настраивают группы доступа, JEA-конечные точки и журналирование (PowerShell Script Block Logging).

## 7. Вопросы на собеседовании

**Вопрос 1. Через какие порты и протоколы работает PowerShell Remoting? Что проверишь, если он «не подключается»?**

Ответ: WinRM/WS-Management, TCP 5985 (HTTP) и 5986 (HTTPS). Аутентификация — Kerberos в домене, NTLM, Basic (только HTTPS). Диагностика по порядку: `Test-NetConnection -Port 5985` (сеть/фаервол) → `Test-WSMan` (служба и WS-Man отвечает) → запущена ли служба `WinRM` и есть ли слушатель (`winrm enumerate winrm/config/listener`) → права учётки (должна быть в Administrators или Remote Management Users) → правило фаервола «Windows Remote Management» → журнал `Microsoft-Windows-WinRM/Operational`.
*Что хочет услышать интервьюер:* системный порядок проверки «сеть → служба → аутентификация → права», а не «перезагрузил сервер, заработало».
*Красный флаг:* «Открыл 5985 в интернет и включил Basic — заработало».

**Вопрос 2. Server Manager не показывает данные с сервера — «Manageability: Online, data retrieval failed». Твои действия?**

Ответ: Server Manager ходит к серверу по WinRM/CIM и RPC. Проверяю: сервер пингуется и 5985 открыт; `Test-WSMan`; служба WinRM и её слушатель; правило фаервола; время на сервере (рассинхрон > 5 минут ломает Kerberos — типичная причина «внезапно перестало»); членство в группе `WinRM Remote Management Users` / локальные админы. Отдельно проверяю через независимый канал (`Get-Service -ComputerName` по RPC) — если RPC работает, а WinRM нет, проблема локализована.
*Что хочет услышать интервьюер:* что ты разделяешь каналы и проверяешь Kerberos-время.
*Красный флаг:* «Удалил сервер из пула и добавил заново» без понимания причины.

**Вопрос 3. Что такое double-hop problem и как её решать правильно?**

Ответ: при удалённом выполнении команда на первом сервере не может аутентифицироваться на втором, потому что Kerberos-билет пользователя не передаётся дальше первого хоста. Решения: выполнять обращение с исходной машины; Kerberos constrained delegation (с указанием конкретных сервисов) или resource-based constrained delegation; JEA; в крайнем случае CredSSP, но он передаёт креды в открытом виде и не рекомендуется.
*Что хочет услышать интервьюер:* «CredSSP — последний вариант, а не первый», и понимание делегирования.
*Красный флаг:* «Включил CredSSP на всех серверах, работает же».

**Вопрос 4. Чем RSAT отличается от Windows Admin Center и что ты выберешь для 40 серверов?**

Ответ: RSAT — набор классических MMC-оснасток и PowerShell-модулей, ставится на клиентскую Windows, работает точечно и «по-старому», без своего сервиса. WAC — веб-шлюз на 443, ставится на отдельный хост, даёт единый обзор парка: производительность, службы, файлы, обновления, PowerShell-панель; удобен для команды и удалённого доступа. Для 40 серверов — WAC как единая панель плюс PowerShell Remoting для массовых операций и RSAT для AD/DNS/GPO, где оснастки привычнее и точнее.
*Что хочет услышать интервьюер:* что WAC не отменяет PowerShell и RSAT, а дополняет; понимание, что WAC — это шлюз, который надо обслуживать и защищать.
*Красный флаг:* «WAC умеет всё, остальное не нужно».

**Вопрос 5. Как дать инженеру L1/L2 управлять серверами, не выдавая доменного администратора?**

Ответ: отдельные учётные записи администраторов, добавление в локальную группу `Administrators` только нужных серверов или в `Remote Management Users` для ограниченного набора команд; JEA с ролевыми возможностями (перезапуск конкретных служб, чтение журналов) и виртуальными учётками; делегирование в AD на уровне OU; LAPS для локальных админов; журналирование PowerShell (Script Block/Module Logging) и аудит в Event Viewer. WAC при этом можно ограничить по ролям подключений.
*Что хочет услышать интервьюер:* принцип наименьших привилегий и JEA как реальный инструмент, а не только «дать админа».
*Красный флаг:* «Проще дать доменного админа, он же свой».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «PowerShell Remoting»: статьи *PowerShell Remoting overview*, *about_Remote*, *about_Remote_Troubleshooting*, *about_Remote_Requirements*. Поиск: `PowerShell remoting overview site:learn.microsoft.com`.
- **learn.microsoft.com** — *Windows Admin Center overview* и *Install Windows Admin Center* (там же параметры `SME_PORT`, `SSL_CERTIFICATE_OPTION`). Поиск: `Windows Admin Center silent install msiexec`.
- **learn.microsoft.com** — *Remote Server Administration Tools* (RSAT) для Windows 10/11 и список имён возможностей. Поиск: `RSAT Windows 11 install PowerShell Add-WindowsCapability`.
- **learn.microsoft.com** — *Server Manager* и *Manage multiple, remote servers with Server Manager*. Поиск: `Server Manager add servers manage multiple remote`.
- **learn.microsoft.com** — *Windows Remote Management (WinRM)* и *WS-Management protocol*, справка по `winrm` и `WSMan:` PSDrive. Поиск: `WinRM ports 5985 5986 firewall site:learn.microsoft.com`.
- **learn.microsoft.com** — *Just Enough Administration (JEA)*. Поиск: `Just Enough Administration overview`.
- **learn.microsoft.com** — *Kerberos constrained delegation* / *Resource-based constrained delegation*. Поиск: `Kerberos constrained delegation resource-based overview`.
- Точные имена RSAT-компонентов (`Rsat.Dns.Tools~~~~0.0.1.0` и др.) и набор правил фаервола «Remote Service Management» по умолчанию — **сверять по актуальной документации на момент прохождения** `[ПРОВЕРИТЬ]`.
