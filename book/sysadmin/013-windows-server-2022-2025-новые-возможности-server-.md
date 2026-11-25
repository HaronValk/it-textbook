# Windows Server 2022/2025: новые возможности, Server Core, Hotpatching, Secured-core

## 1. Введение (зачем это нужно в реальной работе)

Тикет из очереди:

> **#4211 — Петрова М., ВМ WIN10-CL02 (HR)**
> «Доброе утро! Не открывается `\\FS01\HR` — пишет, что нет доступа к сетевому ресурсу. Вчера всё работало. Коллеги говорят, что сервер ночью перезагружался. У нас сегодня приказы, очень срочно.»
> **Срочность: высокая.**

Одновременно в почте висит письмо от директора по безопасности: «Подтвердите, что серверы соответствуют требованиям защищённой конфигурации, и что перезагрузки для обновлений можно сократить — простой файлового сервера unacceptable».

Оба запроса — про одно и то же: **как устроена и как обслуживается современная Windows Server**. Выбор варианта установки (Core или с графикой) определяет поверхность атаки и объём патчей. Механизм обновлений определяет окна простоя и ночные перезагрузки. Secured-core определяет, можно ли вообще говорить «сервер защищён на уровне железа». После этой главы ты будешь уметь: осознанно выбирать редакцию и вариант установки, управлять Server Core без графики, читать журнал и объяснять причину ночной перезагрузки, проверять фактическое состояние VBS/HVCI и аргументированно говорить про hotpatching — включая то, что он не работает «просто так».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Линейка и сроки жизни.** Windows Server с 2022 года существует только в LTSC-ветке (Long-Term Servicing Channel — долгосрочный канал, обновления раз в 2–3 года, поддержка 10 лет). Промежуточный SAC-канал (полугодовые сборки) закрыт. Ориентиры: Server 2016 — расширенная поддержка до января 2027, Server 2019 — до января 2029, Server 2022 — до октября 2031, Server 2025 — GA в ноябре 2024, поддержка до 2034 [ПРОВЕРИТЬ точные даты в lifecycle-таблице Microsoft]. Для сисадмина это значит: планирование миграции — это не «когда удобно», а «до даты EOL минус полгода на тесты».

**Windows Server 2025 — что реально изменилось** (для практики, не для маркетинга):
- ядро от Windows 11 24H2, сборка 26100 [ПРОВЕРИТЬ];
- **hotpatching** стал доступен не только в Azure Edition, но в Standard/Datacenter — через Azure Arc;
- усиление SMB: блокировка NTLM для SMB-клиента, подписание SMB по умолчанию `[ПРОВЕРИТЬ — настройка менялась в предрелизных сборках]`;
- делегированные управляемые сервисные учётные записи (dMSA) — замена gMSA;
- LSA Protection (RunAsPPL) включена по умолчанию на чистых установках `[ПРОВЕРИТЬ]`;
- уровень функционала домена/леса Windows Server 2025 (пригодится в уроке 15);
- поддержка постквантовой криптографии (ML-DSA/ML-KEM) в криптопровайдерах `[ПРОВЕРИТЬ объём поддержки]`.

**Server Core vs Desktop Experience.** Вариант установки — это не «галочка», это два разных набора бинарников. Core не содержит explorer.exe, MMC-оснасток, браузера, панели управления. Остаются: ядро, драйверы, службы, PowerShell, cmd, `sconfig`, средства удалённого управления (WinRM/RDP). Следствия: меньше диска, меньше патчей, меньше перезагрузок, меньше поверхности атаки. Обратная сторона: локально ничего не «посмотреть глазами» — только консоль гипервизора и удалённое управление. **Начиная с Windows Server 2016 конвертировать Core↔Desktop без переустановки нельзя** — это ключевой факт, который любят спрашивать.

**Secured-core server** — это не «настройка», а набор требований к железу + конфигурация ОС из четырёх слоёв:

```text
                ┌─────────────── ПЛАТФОРМА ────────────────┐
                │ UEFI + Secure Boot │ TPM 2.0 │ Boot Guard │  ← слой 1–2: корень доверия,
                │ (измеренная загрузка)          (AMD HVB) │     защита прошивки
                └───────────────────────┬──────────────────┘
                                        │
                ┌───────────────────────▼──────────────────┐
                │ Hyper-V гипервизор = VBS (база)          │
                │  ├─ HVCI: целостность кода ядра          │  ← слой 3: виртуализационная
                │  ├─ Credential Guard: изоляция LSA       │     защита
                │  └─ System Guard Secure Launch (DRTM)    │
                └───────────────────────┬──────────────────┘
                                        │
   ┌────────────────────────────────────▼────────────────────────────────┐
   │ ВАРИАНТ УСТАНОВКИ                                                   │
   │  Server Core: ядро + драйверы + PowerShell + sconfig, без оболочки  │
   │  Desktop Experience: + explorer, MMC, браузер, GUI                  │
   └────────────────────────────────────┬────────────────────────────────┘
                                        │
   ОБСЛУЖИВАНИЕ:  Windows Update/WSUS → ежемесячный патч + перезагрузка
                  Azure Arc + Hotpatch → квартальный baseline + hotpatch
```

**Hotpatching** работает так: раз в квартал (январь, апрель, июль, октябрь) ставится **baseline** — обычное кумулятивное обновление с перезагрузкой. В остальные месяцы часть обновлений безопасности приходит как hotpatch: код правится **в памяти работающего процесса**, двоичные файлы на диске не подменяются до следующего baseline, перезагрузка не требуется. Ограничения: не любые обновления hotpatch-совместимы (обновления .NET, драйверов, компонентов, требующих замены файлов, — нет), нужен агент Azure Arc и подключение к Azure, требования к виртуальным машинам и подписке менялись — сверяй по Learn `[ПРОВЕРИТЬ актуальные prerequisites]`. В РФ-реалиях 2026 это чаще «посмотреть и объяснить», чем «внедрить»: продажи новых лицензий Microsoft в РФ ограничены, а Azure Arc требует зарубежной подписки. Легальная альтернатива — окна обслуживания через WSUS и грамотное планирование (или Linux-инфраструктура).

**Управляющий трафик, порты:** WinRM 5985/TCP (HTTP), 5986/TCP (HTTPS) — основной канал PowerShell Remoting; RDP 3389/TCP; SMB 445/TCP; Windows Admin Center — 443/TCP на шлюзе. Всё это прикладной уровень (L7 модели OSI) — то есть для работы ему нужны живая сеть, DNS и корректное имя.

**Важно про стенд:** VMware Workstation — вложенная виртуализация. VBS/HVCI внутри гостя запустится, только если в настройках ВМ включено «Virtualize Intel VT-x/EPT or AMD-V/RVI» и версия оборудования достаточно новая `[ПРОВЕРИТЬ для своей версии Workstation]`. Если не запускается — это ограничение стенда, а не ошибка настройки; результат всё равно фиксируем.

## 3. Аналогия из жизни

Представь бронированный инкассаторский фургон. **Secured-core** — это сам фургон: броня, замок с датчиком вскрытия, TPM как «пломба на двигателе», Secure Boot как проверка, что в двигатель не поставили чужую деталь до выезда. **Server Core** — минимальная комплектация салона: только сиденье водителя, руль и приборная панель. Нет магнитолы, нет пассажирских кресел, нет бардачка, который можно взломать и что-то в нём забыть, — меньше деталей, которые ломаются и которые надо обслуживать. **Hotpatching** — замена тормозных колодок на ходу: иногда возможно, но не для любого узла и не отменяет планового техобслуживания в сервисе раз в квартал. Аналогия перестаёт работать в одном месте: фургон нельзя «переделать из пассажирского в грузовой» без замены кузова — как нельзя превратить Desktop Experience в Server Core без переустановки.

## 4. Практика и команды

**1. Меню Server Core — `sconfig`.** Единственный «GUI» на Core. Запускается из консоли (или через VMware-консоль ВМ).

```cmd
sconfig
```

```text
===============================================================================
 Server Configuration
 1) Domain/Workgroup:                    Domain: corp.vektor.lab
 2) Computer Name:                       CORE01
 3) Add Local Administrator
 4) Configure Remote Management          Enabled
 5) Windows Update Settings              Automatic
 6) Download and Install Updates
 7) Remote Desktop                        Disabled
 8) Network Settings
 9) Date and Time
...
```

Номера пунктов в разных версиях отличаются `[ПРОВЕРИТЬ]` — ориентируйся по подписям. Пункт 1 — ввод домена, 8 — IP/DNS, 4 — WinRM и RDP.

**2. Определить вариант установки и сборку.** Через реестр — самый надёжный способ.

```powershell
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' |
  Select-Object ProductName, InstallationType, DisplayVersion, CurrentBuild
```

```text
ProductName      : Windows Server 2022 Datacenter
InstallationType : Server Core
DisplayVersion   : 21H2
CurrentBuild     : 20348
```

`InstallationType` = `Server Core` или `Server` (это Desktop Experience). На 2025 ожидай `CurrentBuild 26100`.

**3. Список и установка ролей на Core.**

```powershell
Get-WindowsFeature | Where-Object Installed
Get-WindowsFeature -Name *DNS*, *File*
Install-WindowsFeature -Name Web-Server -IncludeManagementTools
```

`-IncludeManagementTools` тянет модули управления (на Core локальных MMC нет, но командлеты появляются). `Install-WindowsFeature` на Core работает так же, как на GUI-сервере; в GUI-пути это Server Manager → Manage → Add Roles and Features.

**4. Удалённое управление: WinRM + PowerShell Remoting.** На CORE01 (из консоли):

```powershell
Enable-PSRemoting -Force
Test-WSMan -ComputerName CORE01
```

С WIN11-CL01 (нужен RSAT):

```powershell
Enter-PSSession -ComputerName CORE01 -Credential VEKTOR\adm.student
Invoke-Command -ComputerName CORE01 -ScriptBlock { Get-ComputerInfo | Select-Object WindowsProductName, OsBuildNumber }
```

```text
WindowsProductName OsBuildNumber
------------------ --------------
Windows Server 2022 20348
```

В домене Kerberos работает «из коробки», TrustedHosts не нужен. Если `Enter-PSSession` падает с «WinRM cannot complete the operation» — сначала `Test-NetConnection CORE01 -Port 5985`.

**5. Проверка членства в домене без GUI.**

```cmd
nltest /dsgetdc:corp.vektor.lab
```

```text
           DC: \\DC01.corp.vektor.lab
      Address: \\10.10.10.10
     Dom Guid: ...
    Dom Name: corp.vektor.lab
  Forest Name: corp.vektor.lab
```

```powershell
Test-ComputerSecureChannel -Verbose    # True = канал с доменом жив, пароль машины в порядке
```

**6. Включить RDP на Core (осознанно, не «чтобы удобно»).**

```powershell
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name 'fDenyTSConnections' -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
```

В боевой среде RDP на сервер открывают только из сегмента администрирования (PAW/VLAN управления) и только с NLA — тема урока 18.

**7. Диагностика «сеть или служба» без графики.**

```powershell
Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction
Get-NetFirewallRule -Name 'IIS-WebServerRole-HTTP-In-TCP' | Select-Object DisplayName, Enabled, Direction, Action
Test-NetConnection 10.10.10.12 -Port 80
Test-NetConnection 10.10.10.12 -Port 5985
```

`TcpTestSucceeded : False` при живом пинге = блокирует файрвол или служба не слушает. Имя правила IIS в разных сборках может отличаться `[ПРОВЕРИТЬ у себя]`, тогда ищи через `Get-NetFirewallPortFilter`.

**8. Фактическое состояние VBS/HVCI (Secured-core, слой ОС).**

```powershell
Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard |
  Select-Object VirtualizationBasedSecurityStatus, SecurityServicesConfigured, SecurityServicesRunning, CodeIntegrityPolicyEnforcementStatus
```

```text
VirtualizationBasedSecurityStatus         : 2
SecurityServicesConfigured                : {2}
SecurityServicesRunning                   : {2}
CodeIntegrityPolicyEnforcementStatus      : 2
```

`VirtualizationBasedSecurityStatus`: 0 — выключено, 1 — включено, но не запущено, 2 — работает. `SecurityServicesRunning`: 1 — Credential Guard, 2 — HVCI (Memory Integrity), 3 — System Guard Secure Launch, 4 — SMM Firmware Measurement. Пусто = VBS не работает.

**9. Корень доверия: TPM и Secure Boot.**

```powershell
Get-Tpm | Select-Object TpmPresent, TpmReady, TpmEnabled, ManufacturerIdTxt
Confirm-SecureBootUEFI
```

```text
TpmPresent : True
TpmReady   : True
```

`Confirm-SecureBootUEFI` вернёт `True` только на UEFI+Secure Boot; на BIOS-прошивке команда выдаст ошибку «Cmdlet not supported». GUI-путь на клиенте: `msinfo32` → System Summary → строки «Secure Boot State», «Virtualization-based security Services Running», «Device Guard».

**10. Почему сервер перезагрузился ночью.**

```powershell
Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074,6006,6008} -MaxEvents 20 |
  Select-Object TimeCreated, Id, @{n='Msg';e={$_.Message -replace "`r`n",' '}} | Format-Table -Wrap
Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 5
```

1074 — кто инициировал перезагрузку (в сообщении будет `Operating System: Service pack (Planned)` и имя процесса, часто `TiWorker.exe`); 6006 — «журнал остановлен корректно»; 6008 — некорректное завершение. Это и есть доказательная база для ответа пользователю.

**11. Редакция, вариант и канал лицензии.**

```powershell
Get-WindowsEdition -Online
```

```text
Edition      : ServerStandard
Server Core  : True
```

```cmd
slmgr.vbs /dlv
```

Смотри `ProductName` и `License Status` — Evaluation живёт 180 дней `[ПРОВЕРИТЬ для 2025]`, после чего сервер начинает отключаться. Для стенда это нормально, для продакшена — нет.

**12. App Compatibility FOD — «необязательные» GUI-компоненты для Core.**

```cmd
DISM /Online /Get-Capabilities | findstr /i AppCompatibility
DISM /Online /Add-Capability /CapabilityName:ServerCore.AppCompatibility~~~~0.0.1.0
```

FOD (Feature on Demand — компонент по запросу) добавляет часть библиотек и ограниченный набор средств, чтобы на Core запускались некоторые GUI-утилиты и MMC-оснастки. Точное имя capability в разных версиях отличается — проверяй выводом `Get-Capabilities` `[ПРОВЕРИТЬ]`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 собран: DC01 (AD DS, DNS, DHCP, 10.10.10.10), FS01 (10.10.10.20), WIN11-CL01 (домен, `ivanov`), WIN10-CL02 (`petrova`), UBU01, ASTRA01, PVE01. Домен `corp.vektor.lab`, учётка `VEKTOR\adm.student`.

Для этой темы мы **явно добавляем на стенд одну новую ВМ**: `CORE01` — Windows Server 2022/2025 Evaluation, вариант **Server Core**, адрес `10.10.10.12` (вне DHCP-пула 100–150), роль: учебный сервер для практики headless-управления; в уроке 21 к нему подключимся через Windows Admin Center. Дистрибутив — Evaluation ISO с Microsoft Evaluation Center (180 дней); скачивание из РФ может быть затруднено, легальная альтернатива — учебные материалы вендора или Linux-стенд.

### Снапшот

В VMware Workstation на **DC01, FS01, WIN11-CL01** (их мы затрагиваем) — снапшот `before-sysadmin-013`. На CORE01 снапшот снять до установки нельзя, поэтому: сначала ставим ОС начисто, **сразу после установки и до ввода в домен** делаем снапшот `before-sysadmin-013` на CORE01 — это и будет точка откатa «чистая ОС». Фиксируем в `stand-log.md`: «CORE01 — базовая установка Server Core, снапшот before-sysadmin-013».

### Шаги

1. **Создать ВМ CORE01** в VMware Workstation: 2 vCPU, 4 ГБ RAM, 60 ГБ диск, сеть VMnet8. В настройках процессора включить «Virtualize Intel VT-x/EPT or AMD-V/RVI» (нужно для VBS). Смонтировать Evaluation ISO.
2. **Установка**: при выборе редакции взять Standard/Datacenter Evaluation, на экране выбора варианта — **«Server Core»** (строка без «Desktop Experience»). Пароль локального администратора: `Lab#2026-vektor`. **Ожидаемо:** после установки вместо рабочего стола — чёрное окно cmd, приветствие с `sconfig`.
3. **Первичная настройка:** запустить `sconfig` → п. 2 задать имя `CORE01` → перезагрузка; п. 8 задать статический IP `10.10.10.12/24`, шлюз `10.10.10.2`, DNS `10.10.10.10`. **Ожидаемо:** `ipconfig /all` показывает адрес и DNS-сервер.
4. **Ввод в домен:** `sconfig` → п. 1 → Domain → `corp.vektor.lab`, пользователь `VEKTOR\adm.student`. **Ожидаемо:** «Welcome to the corp.vektor.lab domain», требуется перезагрузка.
5. **Проверка домена и варианта установки** (консоль CORE01): `nltest /dsgetdc:corp.vektor.lab`, `Test-ComputerSecureChannel`, п.4 из раздела 4. **Ожидаемо:** DC01 в ответе, `True`, `InstallationType : Server Core`.
6. **Проверка из AD:** на DC01 `Get-ADComputer CORE01 | Select Name, DistinguishedName, OperatingSystem`; на WIN11-CL01 `Resolve-DnsName CORE01`. **Ожидаемо:** компьютер в контейнере `CN=Computers` (позже в уроке 19 перенесём в OU `Компьютеры`), DNS-запись `10.10.10.12`.
7. **Включить удалённое управление:** на CORE01 `Enable-PSRemoting -Force`, `Test-WSMan -ComputerName CORE01`. С WIN11-CL01 — `Enter-PSSession -ComputerName CORE01 -Credential VEKTOR\adm.student`. **Ожидаемо:** приглашение `[CORE01]: PS C:\Users\adm.student\Documents>`.
8. **Установить роль удалённо** (из сессии на CORE01): `Install-WindowsFeature -Name Web-Server -IncludeManagementTools`. **Ожидаемо:** `Success Restart Needed Exit Code Feature Result` → `No`. Затем с WIN11-CL01: `Test-NetConnection 10.10.10.12 -Port 80` → `TcpTestSucceeded : True`.
9. **Проверить состояние защиты:** на CORE01 `Get-Tpm`, `Confirm-SecureBootUEFI`, `Get-CimInstance Win32_DeviceGuard`. **Ожидаемо:** TPM Present=True (виртуальный TPM VMware `[ПРОВЕРИТЬ наличие в твоей версии]`), Secure Boot True/ошибка на BIOS, VBS — 0 или 2 в зависимости от вложенной виртуализации. Записать фактические значения в `stand-log.md`.
10. **Разобрать ночную перезагрузку FS01** (это и есть тикет #4211): на FS01 `Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074,6006}` и `Get-HotFix | Sort InstalledOn -Descending | Select -First 3`. **Ожидаемо:** событие 1074 с причиной `Operating System: Service pack (Planned)` в 03:00–04:00 и свежий KB.
11. **Зафиксировать вывод по hotpatching:** прочитать на learn.microsoft.com раздел про hotpatch для Windows Server 2025, записать в `stand-log.md` три пункта: (а) почему на нашем стенде hotpatch недоступен (нет Azure Arc/подписки), (б) что baseline всё равно даёт 4 перезагрузки в год, (в) чем заменить — WSUS + окно обслуживания.
12. **Обновить `stand-log.md`** по разделу «Что изменилось на стенде» ниже.

### Проверка

- [ ] CORE01 пингуется и отвечает по 5985/TCP с WIN11-CL01.
- [ ] `Get-ItemProperty ... InstallationType` = `Server Core`.
- [ ] `Test-ComputerSecureChannel` возвращает `True`.
- [ ] `Enter-PSSession` с WIN11-CL01 открывает сессию без TrustedHosts.
- [ ] `http://10.10.10.12` открывается с WIN11-CL01 (страница IIS).
- [ ] `Get-ADComputer CORE01` на DC01 показывает объект с Windows Server.
- [ ] В `stand-log.md` записаны значения DeviceGuard и причина перезагрузки FS01.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Не открывается `http://10.10.10.12` — просто крутится и таймаут. Вчера ИТ просили проверить, что новый сервер отвечает. И ещё у меня с утра RDP до него не идёт, но это, наверное, неважно.»
> **Срочность:** средняя.

**Воспроизводим поломку** (на CORE01, из консоли VMware — иначе потеряем доступ):

```powershell
Disable-NetFirewallRule -Name 'IIS-WebServerRole-HTTP-In-TCP'
Get-NetFirewallRule -DisplayGroup 'Windows Remote Management' | Disable-NetFirewallRule
```

**Диагностика по порядку** (не прыгать через шаги):
1. Жива ли ВМ вообще: в VMware Workstation смотрим, что CORE01 запущена, в консоли логин работает.
2. С WIN11-CL01: `Resolve-DnsName CORE01` — правильный ли IP; `Test-NetConnection 10.10.10.12 -Port 445` — SMB отвечает? Если да — сеть и стек TCP живы, проблема выше.
3. `Test-NetConnection 10.10.10.12 -Port 80` и `-Port 5985` — оба `False`. Значит, либо службы не слушают, либо блокирует файрвол.
4. В консоли CORE01: `Get-Service W3SVC | Select Status` (IIS работает), `Get-NetTCPConnection -LocalPort 80 -State Listen` (порт слушается).
5. `Get-NetFirewallProfile | Select Name, Enabled, DefaultInboundAction` и `Get-NetFirewallRule -Name 'IIS-WebServerRole-HTTP-In-TCP' | Select Enabled` → `Enabled: False`. Причина найдена: правила отключены.

**Решение:**

```powershell
Enable-NetFirewallRule -Name 'IIS-WebServerRole-HTTP-In-TCP'
Enable-NetFirewallRule -DisplayGroup 'Windows Remote Management'
Test-NetConnection 10.10.10.12 -Port 80   # с WIN11-CL01 после исправления
```

**Ответ пользователю:** «Иван, спасибо за сигнал. Проблема была на стороне сервера: после настройки firewall оказались отключены разрешающие правила для веб-службы и удалённого управления. Правила включены, доступ восстановлен, проверь, пожалуйста, страницу `http://10.10.10.12`. RDP на этот сервер специально закрыт политикой — администрирование идёт через PowerShell/Windows Admin Center. Заявку закрываю, если что — пиши.»
**Закрытие:** в тикете указать root cause (disabled firewall rules), действие (enabled), время; в `stand-log.md` — строку «CORE01: firewall rules IIS/WinRM были отключены в рамках учебной поломки, восстановлены».

### Что изменилось на стенде

- Новая ВМ **CORE01**: Windows Server 2022/2025 Evaluation, вариант **Server Core**, `10.10.10.12`, шлюз `10.10.10.2`, DNS `10.10.10.10`, член домена `corp.vektor.lab`.
- На CORE01 включены: WinRM/PowerShell Remoting, роль **Web Server (IIS)** (учебная, для проверки управляемости).
- В AD появился объект компьютера `CORE01` (в `CN=Computers`).
- На FS01 в журнале зафиксирована ночная плановая перезагрузка после установки обновлений (дата/время — в журнал стенда).
- Снапшоты `before-sysadmin-013` на DC01, FS01, WIN11-CL01, CORE01.

### Откат

VMware Workstation → выключить ВМ → Snapshot Manager → выбрать `before-sysadmin-013` → **Revert**. Для CORE01 откат возвращает чистую Server Core без домена: после откатa заново шаги 3–8. Если ВМ не запускается после экспериментов с VBS — снять галочку «Virtualize Intel VT-x/EPT», запуститься, проверить журнал.

## 6. Ошибки новичков и безопасность

1. **Ставят Desktop Experience «по привычке»** → больше бинарников, больше ежемесячных патчей, больше перезагрузок и больше точек входа для атакующего. → Для ролей уровня файлового сервера, DC, гипервизора — Core по умолчанию; GUI оставить там, где реально нужен (терминальные, некоторые 1С-сценарии).
2. **Думают, что Core можно «доставить GUI» или наоборот** → на 2016+ конвертации нет, только переустановка; попытка ставить `Server-Gui-Shell` ни к чему не приведёт. → Вариант установки выбирается один раз и осознанно, до продакшена.
3. **Ставят Core и не настраивают удалённое управление** → сервер есть, а доступа нет; каждая мелочь — через консоль гипервизора. → Сразу после установки: `Enable-PSRemoting`, проверка WinRM с рабочей станции, документирование.
4. **Открывают RDP «чтобы было удобно», часто в интернет** → брутфорс, RDP-атаки, компрометация сервера. → Доступ только из сегмента администрирования, NLA включён, учётки `adm.*` не используются для интерактивного входа в боевой среде (урок 18, Tiering/PAW).
5. **Включают HVCI, не проверив драйверы** → несовместимый драйвер не загрузится, устройство отвалится или система не стартует. → Сначала тест на стенде, проверка совместимости драйверов/ПО (антивирус, криптопровайдеры КриптоПро `[ПРОВЕРИТЬ совместимость версий]`), потом продакшен.
6. **Путают «включил VBS» и «Secured-core server»** → Secured-core требует сертифицированного железа: TPM 2.0, UEFI Secure Boot, DRTM, защита прошивки, DMA-защита. → На обычном ПК честно говорить «VBS/HVCI включены, Secured-core нет».
7. **Планируют hotpatching как «отмену перезагрузок»** → baseline раз в квартал всё равно перезагружает; часть обновлений не hotpatch-совместима. → Считать окна обслуживания по-прежнему, hotpatch — как уменьшение их числа, а не отказ.
8. **Ставят Evaluation и забывают про срок** → через 180 дней сервер начинает отключаться в самый неподходящий момент. → Календарная заметка + план лицензирования (урок 23).

**Безопасность (что нельзя в бою):** отключать файрвол и UAC «для теста» насовсем; включать SMB1 и анонимный доступ к шарам; хранить пароли в скриптах и в `stand-log`; выкладывать Core-сервер в интернет без обновлений и без сегментации; использовать пиратские сборки и «активаторы» вместо Evaluation/лицензии. На стенде широкие права и один пароль на всё допустимы только потому, что это изолированная лаборатория.

## 7. Вопросы на собеседовании

**1. Чем Server Core отличается от Desktop Experience и почему Microsoft рекомендует Core?**
Ответ: Core — это установленный вариант без оболочки: нет explorer.exe, MMC, браузера, панели управления; остаются ядро, драйверы, службы, PowerShell, cmd, sconfig, WinRM/RDP. Плюсы: меньше диска и памяти, меньше бинарников — меньше уязвимостей и патчей, реже перезагрузки, проще соответствие требованиям. Минусы: управление только удалённое (RSAT, PowerShell, WAC) или через консоль гипервизора. Управление — `Enter-PSSession`, `Install-WindowsFeature`, `sconfig`.
*Что хочет услышать:* что ты выбирал вариант установки осознанно, знаешь про отсутствие GUI и про инструменты управления без графики.
*Красный флаг:* «Core — это урезанная Windows, там ничего не работает» или «поставлю GUI, потом конвертирую».

**2. Можно ли преобразовать существующую установку Desktop Experience в Server Core?**
Ответ: на Windows Server 2012/2012 R2 это было возможно, начиная с 2016 — нет: набор компонентов фиксируется при установке, `Server-Gui-Shell`/`Server-Gui-Mgmt` недоступны. Практика: пилотная ВМ, выбор варианта, при ошибке — переустановка/пересоздание из шаблона, поэтому решение принимается до продакшена.
*Что хочет услышать:* конкретику про версии и понимание цены ошибки.
*Красный флаг:* «да, через DISM добавлю `Server-Gui-Shell`» — этого компонента на современных версиях нет.

**3. Что такое Secured-core server, из чего состоит и как проверить?**
Ответ: это не одна настройка, а комбинация сертифицированного железа и конфигурации ОС по четырём направлениям: (1) аппаратный корень доверия — TPM 2.0, UEFI Secure Boot, измеренная загрузка; (2) защита прошивки — Intel Boot Guard / AMD Hardware Verified Boot; (3) виртуализационная защита — VBS: HVCI (целостность кода ядра) и Credential Guard; (4) защита ОС — System Guard Secure Launch (DRTM) и защита от DMA-атак. Проверка: `Get-CimInstance Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard` (VirtualizationBasedSecurityStatus, SecurityServicesRunning), `Get-Tpm`, `Confirm-SecureBootUEFI`, GUI `msinfo32`.
*Что хочет услышать:* понимание, что Secured-core — свойство платформы, а не галочка, и умение подтвердить состояние командами.
*Красный флаг:* «включил HVCI — значит сервер Secured-core» или путаница VBS и Secure Boot.

**4. Что такое hotpatching, какие у него ограничения и требования?**
Ответ: механизм установки обновлений безопасности без перезагрузки — код правится в памяти работающих процессов. Раз в квартал ставится baseline (январь, апрель, июль, октябрь) с перезагрузкой; в остальные месяцы обновления приходят как hotpatch, если они hotpatch-совместимы (обновления .NET, драйверов и части компонентов — нет). Требуется Windows Server 2022 Azure Edition или 2025 Standard/Datacenter с подключением через Azure Arc; конкретные требования к ВМ/подписке надо сверять по Learn `[ПРОВЕРИТЬ]`. В среде без Azure — WSUS и планирование окон обслуживания.
*Что хочет услышать:* что ты не продаёшь hotpatch как «вечный аптайм», знаешь про baseline и про требования инфраструктуры.
*Красный флаг:* «hotpatch ставит все обновления без перезагрузки» или «включается групповой политикой».

**5. Сервер перезагрузился ночью, пользователи жалуются. Как выяснить причину и что отвечать?**
Ответ: сначала факты, потом действия. `Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074,6006,6008}`, `Get-HotFix | Sort InstalledOn -Descending` — событие 1074 покажет, какой процесс инициировал перезагрузку (обычно `TiWorker.exe`, причина `Service pack (Planned)`), 6006/6008 покажут корректность завершения. Дальше — план: окно обслуживания вне рабочих часов, WSUS с утверждёнными группами (пилот/прод), отказоустойчивость (второй файловый сервер/DFS — урок 20), уведомление пользователей заранее. Пользователю — честно: «плановое обновление безопасности, окно 03:00–03:20, выполнено успешно, данные не затронуты».
*Что хочет услышать:* дисциплинированную диагностику по журналам и системное мышление (WSUS, окна, резервирование), а не «перезагрузите компьютер».
*Красный флаг:* отключить обновления навсегда или «поставить обновления вручную когда-нибудь».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Server»: статьи «Windows Server 2025 overview», «Windows Server release information», «Windows Server lifecycle». Поиск: `windows server 2025 what's new`.
- **learn.microsoft.com** — «Server Core installation» / «Install Server Core»: варианты установки, роли и компоненты, недоступные на Core. Поиск: `server core installation roles not included`.
- **learn.microsoft.com** — «Hotpatch updates for Windows Server» (requirements, baseline schedule, supported updates). Поиск: `hotpatch windows server 2025 requirements`. Часть деталей `[ПРОВЕРИТЬ]`.
- **learn.microsoft.com** — «Secured-core server» / «Device Guard and Credential Guard» / «Virtualization-based Security». Поиск: `secured-core server requirements`, `Win32_DeviceGuard class`.
- **learn.microsoft.com** — «PowerShell Remoting over WinRM», «Enable-PSRemoting», «sconfig». Поиск: `about remote troubleshooting powershell remoting`.
- **microsoft.com** (Evaluation Center) — образы Windows Server Evaluation. Поиск: `windows server 2022 evaluation iso`. Доступность скачивания из РФ `[ПРОВЕРИТЬ]`.
- **catalog.update.microsoft.com** — каталог обновлений для проверки KB и дат выпуска.
