# Управление парком ПК: SCCM/MECM, WDS/MDT, Autopilot-аналоги, образы Windows, развёртывание по сети

## 1. Введение (зачем это нужно в реальной работе)

**Заявка (тикет №4127).** Пользователь: ivanov. ВМ: WIN11-CL01. Текст: «Алексей, у меня система после обновления грузится синим экраном, данные на рабочем столе нужны к завтрашнему отчёту. Можно переустановить, но чтобы 1С, принтер на FS01 и папка `\\FS01\Buh` были на месте сразу?» Срочность: высокая, срок — завтра 9:00.

Такие заявки — рутина любого сисадмина. Один-два ПК в неделю: у кого-то слетела Windows, кому-то привезли новый ноутбук, а в понедельник надо ввести в домен пять машин для нового отдела склада. Ставить Windows вручную, потом руками вводить в домен, ставить 1С, КриптоПро, драйверы принтера и настраивать папки — это 3–4 часа на машину и гарантированные расхождения между ПК. Профессиональный подход другой: один раз собирается эталонный образ, один раз описывается сценарий развёртывания (task sequence), и дальше машина поднимается по сети за 25–40 минут без участия человека, сразу в домене, сразу с нужным OU, сразу с набором ПО.

После этой главы ты должен уметь: собирать и обслуживать WIM-образ (DISM), готовить эталон к тиражированию (sysprep и его границы), поднимать сервер развёртывания на MDT + WDS, отдавать его в PXE по сети, понимать архитектуру MECM (ConfigMgr) и место Autopilot/Intune в этой картине, а также знать, чем это заменяют в РФ и на чём горят новички.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Управление парком раскладывается на четыре независимых слоя. Понимание слоёв — половина ответа на собеседовании.

| Слой | Что делает | Инструменты |
|---|---|---|
| Образ (образец) | эталон ОС + настройки | WIM/ESD, DISM, sysprep, unattend.xml |
| Транспорт доставки | как образ попал на пустой диск | PXE (WDS), USB, ISO, облако |
| Оркестрация | что и в каком порядке делать | MDT task sequence, MECM OSD |
| Пост-конфигурация | домен, GPO, приложения, политики | AD GPO (урок 19), MECM, Intune |

**WIM** — файловый образ (Windows Imaging Format), по сути «архив с деревом файлов и метаданными», внутри может лежать несколько редакций (`install.wim` из ISO: Index 1 = Home, 2 = Pro и т.д.). WIM умеет *single-instance* хранение и монтируется как папка — это ключевое отличие от ISO/GHO.

**sysprep** — утилита, которая переводит установленную Windows в состояние «как будто её только что поставили»: сбрасывает SID компьютера, чистит драйвер-кэш (с `/generalize`), удаляет следы машины, готовит OOBE. Ответы на вопросы OOBE (язык, имя ПК, часовой пояс, домен) даёт файл `unattend.xml` — он применяется в несколько проходов: `windowsPE` → `offlineServicing` → `specialize` → `oobeSystem`.

**Схема потока PXE-развёртывания (наш стенд):**

```text
                    ┌──────────── DC01 10.10.10.10 (DHCP) ────────────┐
  PXE-клиент ──────▶│ option 60 = PXEClient                          │
  (пустой диск)     │ option 66 = 10.10.10.35 (адрес сервера WDS)     │
                    │ option 67 = boot\x64\wdsnbp.com (файл загрузки)  │
                    └──────────────────────────────────────────────────┘
        │ 1. DHCP DISCOVER/OFFER/REQUEST/ACK  — UDP 67 (сервер) / 68 (клиент)
        ▼  клиент получил IP + 66 + 67
        │ 2. TFTP — UDP 69: скачивает wdsnbp.com → bootmgr.exe → BCD → boot.sdi → boot.wim
        ▼
  WinPE (LiteTouchPE_x64.wim) — мини-ОС в RAM, консоль развёртывания
        │ 3. SMB — TCP 445: \\DEP01\DeploymentShare$  (CustomSettings.ini, task sequence)
        │ 4. применяет install.wim на диск (DISM /Apply-Image), драйверы по Make/Model,
        │    вводит в домен, ставит приложения
        ▼
  Первая загрузка → specialize/oobeSystem → GPO из AD (урок 19) → рабочий ПК
```

Порты, которые надо помнить: **UDP 67/68** (DHCP), **UDP 69** (TFTP), **TCP 445** (SMB до Deployment Share), **TCP 80/443** (точка распространения MECM, Intune), **TCP 8530/8531** (WSUS-роль внутри MECM), административный протокол WDS — RPC на динамических портах.

**MECM (Microsoft Endpoint Configuration Manager, он же ConfigMgr, бывший SCCM)** — это уже не «сервер развёртывания», а целая платформа: Site Server + база SQL + точки ролей (Distribution Point, Management Point, Software Update Point, PXE-enabled DP). Ключевые понятия: **collection** (динамическая группа устройств по запросу), **boundary / boundary group** (какие ПК обслуживает какая точка распространения), **task sequence** (тот же сценарий, но с ветвлениями, условиями и откатом), **OSD** (Operating System Deployment). MECM умеет не только ставить ОС, но и обновлять её, раздавать ПО и закрывать уязвимости.

**Autopilot** — противоположная философия: образа нет вообще. На заводе железо регистрируется в облаке по **hardware hash** (отпечаток оборудования), пользователь включает ноутбук, вводит корпоративную учётку — устройство само регистрируется в Intune, получает профили, приложения и политики. Никакого PXE, никаких серверов в офисе, но нужны Entra ID + Intune + лицензия и стабильный доступ к облаку Microsoft — для РФ 2026 это отдельный разговор (урок 23). Аналог в мире Linux/on-prem — MAAS, Foreman, cloud-init + Ansible.

**Статус инструментов (важно и часто спрашивают):** WDS объявлен Microsoft устаревшим (deprecated) в Windows Server 2022+ `[ПРОВЕРИТЬ в разделе «Features removed or no longer developed in Windows Server»]`; MDT (последняя ветка 8456) не развивается и официально не заявляет поддержку Windows 11 `[ПРОВЕРИТЬ]`. То есть WDS+MDT — это «работает, но не будущее», а MECM и Intune — поддерживаемые пути. В РФ к этому добавляются ALD Pro (Astra Linux) и РЕД АДМ (РЕД ОС) — оба заявляют централизованное развёртывание и управление парком `[ПРОВЕРИТЬ актуальные возможности на сайтах вендоров]`.

## 3. Аналогия из жизни

Развёртывание парка — это строительство типовых домов в коттеджном посёлке. **Образ** — это проект дома и склад стройматериалов: один раз согласовали, дальше только копируем. **WinPE** — бригада с инструментами, которая приезжает на пустой участок и умеет строить с нуля. **Task sequence** — прораб с техкартой: «сначала фундамент (разметка диска), потом коробка (apply image), потом коммуникации (драйверы), потом подключение к посёлку (домен)». **PXE/DHCP** — вахтёр на въезде, который по пропуску говорит бригаде, на какой участок ехать. **sysprep** — процедура «обезличивания» дома перед сдачей: убираем табличку с фамилией прежнего жильца и следы его мебели, чтобы новый жилец начал с чистого листа.

Аналогия ломается в одном месте: дом после сдачи нельзя «откатить к чертежу за 30 минут», а виртуалку — можно, если перед экспериментами сделан снапшот.

## 4. Практика и команды

**1. Посмотреть, что внутри WIM (индексы и редакции).**

```powershell
Get-WindowsImage -ImagePath D:\ISO\sources\install.wim | Format-Table ImageIndex, ImageName, Version
dism /Get-WimInfo /WimFile:D:\ISO\sources\install.wim
```

Типичный вывод `dism`:

```text
Index : 1
Name : Windows 10 Enterprise LTSC
Description : Windows 10 Enterprise LTSC
Size : 15 231 MB
```

Смотрим: какой индекс нам нужен. В MDT/MECM указывается именно **индекс**, а не имя.

**2. Смонтировать образ и доработать его офлайн (offline servicing).**

```powershell
$m = "C:\mnt"
Mount-WindowsImage -ImagePath D:\Images\base.wim -Index 3 -Path $m
Get-WindowsCapability -Path $m -Name "Language*"     # что стоит
Get-AppxProvisionedPackage -Path $m | Select DisplayName
Remove-AppxProvisionedPackage -Path $m -PackageName "Microsoft.BingWeather" -ErrorAction SilentlyContinue
Dismount-WindowsImage -Path $m -Save
```

Ключи: `-Path` — точка монтирования (пустая папка), `-Save` — сохранить изменения, `-Discard` — отменить. Так вычищают встроенные приложения и включают компоненты, не устанавливая Windows заново.

**3. Добавить драйверы прямо в образ.**

```powershell
Add-WindowsDriver -Path C:\mnt -Driver D:\Drivers\HP\Network -Recurse -ForceUnsigned
# альтернатива на живой системе:
pnputil /add-driver D:\Drivers\*.inf /subdirs /install
```

`-Recurse` обходит подпапки, `-ForceUnsigned` нужен для неподписанных драйверов (на стенде допустимо, в бою — только подписанные).

**4. Подготовить эталон к тиражированию.**

```cmd
C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /mode:vm /unattend:C:\unattend.xml
```

`/generalize` — сброс уникальных параметров, `/oobe` — при следующей загрузке запустить мастер первого запуска, `/shutdown` — выключить после подготовки, `/mode:vm` — поправка под виртуальную машину (быстрее проходит), `/unattend` — ответы. Ограничение: sysprep `/generalize` можно прогнать ограниченное число раз (порядка трёх) `[ПРОВЕРИТЬ точное значение для вашей сборки]` — поэтому эталон всегда держим на снапшоте/копии.

**5. Снять образ и сжать его.**

```cmd
dism /Capture-Image /ImageFile:D:\Images\WIN11-CL01.wim /CaptureDir:C:\ /Name:"Win11-CL01-base" /Compress:max /CheckIntegrity
dism /Export-Image /SourceImageFile:D:\Images\WIN11-CL01.wim /SourceIndex:1 /DestinationImageFile:D:\Images\WIN11-CL01-slim.wim /Compress:recovery
```

`/Compress:max` — максимальное сжатие (медленнее), `recovery` — сильнее и медленнее, зато для ESD. `/CheckIntegrity` проверяет целостность после записи — на 20-гигабайтном образе не пренебрегай.

**6. Разметить диск под UEFI в WinPE (скрипт diskpart).**

```text
diskpart
select disk 0
clean
convert gpt
create partition efi size=100
format quick fs=fat32 label="System"
create partition msr size=16
create partition primary
format quick fs=ntfs label="Windows"
assign letter="W"
```

100 МБ EFI и 16 МБ MSR — стандарт для UEFI/GPT. После применения образа нужен загрузчик:

```cmd
bcdboot W:\Windows /s S: /f UEFI
```

**7. Применить образ (то, что делает MDT внутри).**

```cmd
dism /Apply-Image /ImageFile:\\DEP01\DeploymentShare$\Operating Systems\WIN10\install.wim /Index:3 /ApplyDir:W:\
```

Проверка: `dir W:\Windows\System32\ntoskrnl.exe` — файл на месте.

**8. Управление WDS (`wdsutil`).**

```cmd
wdsutil /initialize-server /reminst:D:\RemoteInstall
wdsutil /add-image /ImageFile:"D:\DeploymentShare\Boot\LiteTouchPE_x64.wim" /ImageType:Boot /Name:"LiteTouch x64"
wdsutil /set-server /AnswerClients:Known
wdsutil /get-server /show:config
```

`/initialize-server` — первичная настройка хранилища (папка RemoteInstall), `/add-image` с `/ImageType:Boot` — добавить загрузочный WinPE, `/AnswerClients:Known` — отвечать только на заранее одобренные машины (в лабе поставим `All`, в бою — только `Known`).

**9. Настроить DHCP-опции для PXE (на DC01).**

```powershell
Set-DhcpServerv4OptionValue -ScopeId 10.10.10.0 -OptionId 66 -Value "10.10.10.35"
Set-DhcpServerv4OptionValue -ScopeId 10.10.10.0 -OptionId 67 -Value "boot\x64\wdsnbp.com"
Set-DhcpServerv4OptionValue -ScopeId 10.10.10.0 -OptionId 60 -Value "PXEClient"
Get-DhcpServerv4OptionValue -ScopeId 10.10.10.0
```

Опция 66 — адрес сервера загрузки, 67 — путь к загрузчику, 60 — класс «я PXE-клиент». **Важно:** если WDS и DHCP стоят на одном сервере, опции 66/67 задавать нельзя — PXE сломается `[ПРОВЕРИТЬ в документации Microsoft по WDS]`. У нас они на разных машинах (DC01 и DEP01), поэтому нужны.

**10. MDT из PowerShell (провайдер MDTProvider).**

```powershell
Add-PSSnapin Microsoft.BDD.PSSnapIn
New-PSDrive -Name DS001 -PSProvider MDTProvider -Root "D:\DeploymentShare"
Import-MDTOperatingSystem -Path "DS001:\Operating Systems" -SourcePath "D:\ISO\sources\install.wim" -DestinationFolder "WIN10-LTSC" -Verbose
Get-ChildItem "DS001:\Operating Systems"
```

`New-PSDrive` подключает папку Deployment Share как диск `DS001:` — дальше по ней ходишь как по файловой системе. `Import-MDTOperatingSystem` распаковывает WIM в дерево файлов и регистрирует его в MDT `[ПРОВЕРИТЬ актуальные имена командлетов для MDT 8456]`.

**11. Снять hardware hash для Autopilot (если он вообще доступен).**

```powershell
Install-Script -Name Get-WindowsAutoPilotInfo -Force
Get-WindowsAutoPilotInfo.ps1 -OutputFile C:\temp\WIN11-CL01-hash.csv -ComputerName WIN11-CL01
```

CSV содержит серийник, производителя, модель и хэш. Именно этот файл загружается в Intune → Devices → Enroll devices → Windows Autopilot devices. Без него Autopilot не узнает железо.

**12. Альтернатива на Linux: FOG Project (бесплатно, on-prem).**

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/FOGProject/fogproject.git
cd fogproject/bin && sudo ./installfog.sh
```

FOG умеет PXE, захват и раскатку образов, инвентаризацию, SNMP-пробуждение. Для многоадресной раскатки в класс — Clonezilla SE (DRBL), там multicast, то есть 20 машин получают образ одновременно с одной передачей.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд уровня sysadmin: DC01 (AD DS, DNS, DHCP, пул 10.10.10.100–150), FS01, WIN10-CL02 (рабочее место petrova, DHCP-адрес), WIN11-CL01, UBU01, ASTRA01, PVE01. Учётка `adm.student` с правами администратора домена. **Добавляем одну машину сверх таблицы** — сервер развёртывания:

| Имя | ОС | Адрес | Роль |
|---|---|---|---|
| DEP01 | Windows Server 2022 (Evaluation) | 10.10.10.35 | WDS + MDT, сервер развёртывания, OU `Серверы` |

DEP01: 2 vCPU, 4 ГБ RAM, диск C: 40 ГБ + второй диск D: 60 ГБ, сеть VMnet8, шлюз 10.10.10.2, DNS 10.10.10.10. MECM в лабе не поднимаем: ему нужен SQL Server и минимум 8–12 ГБ RAM, для домашнего стенда это нерационально — архитектуру MECM разбираем по документации, а руками делаем MDT (тот же слой оркестрации).

Понадобится установщик **Windows ADK** (Assessment and Deployment Kit) + **WinPE add-on** и **MDT** — оба бесплатны с сайта Microsoft. Если скачивание из РФ затруднено, легальная альтернатива — собрать WinPE через `copype` из ADK, либо сразу перейти к FOG/Clonezilla (пункт 12 раздела 4) `[ПРОВЕРИТЬ доступность загрузок ADK из вашего региона]`.

Данные petrova на WIN10-CL02 сохранить на `\\FS01\HR` — машину мы переустановим.

### Снапшот

Снапшоты `before-sysadmin-022` на всех затрагиваемых ВМ: **DC01, FS01, WIN10-CL02, DEP01** (DEP01 — сразу после установки ОС и сети, до установки ролей). В VMware Workstation: VM → Snapshot → Take Snapshot, имя ровно `before-sysadmin-022`.

### Шаги

1. **DEP01.** Переименовать в DEP01, задать статический IP 10.10.10.35/24, GW 10.10.10.2, DNS 10.10.10.10 (`sconfig` или Settings → Network). Проверка: `ping corp.vektor.lab` отвечает.
2. **DEP01.** Ввести в домен: `Add-Computer -DomainName corp.vektor.lab -Credential (Get-Credential VEKTOR\adm.student) -Restart`. Ожидаемо: машина в домене, компьютерный объект переносим в OU `Серверы`.
3. **DEP01.** Разметить D: и создать папку развёртывания: `New-Item -ItemType Directory D:\DeploymentShare`. Расшарить: `New-SmbShare -Name "DeploymentShare$" -Path D:\DeploymentShare -FullAccess "VEKTOR\Domain Admins"` — знак `$` скрывает шару из обзора.
4. **DEP01.** Установить Windows ADK + WinPE add-on, затем MDT (мастер установки, все компоненты по умолчанию). Ожидаемо: в меню «Пуск» появилась папка Microsoft Deployment Toolkit, в `C:\Program Files\Microsoft Deployment Toolkit` лежит Deployment Workbench.
5. **DEP01.** В Deployment Workbench: Deployment Shares → New Deployment Share → путь `D:\DeploymentShare` → имя `MDT Deployment Share` → Next до конца. Ожидаемо: создано дерево папок (Operating Systems, Task Sequences, Applications, Boot) и шара пересоздана MDT.
6. **DEP01.** Operating Systems → Import Operating System → «Full set of source files» → указать смонтированный ISO Windows 10 → папка `WIN10-LTSC`. Ожидаемо: в папке появились `install.wim` и индекс LTSC.
7. **DEP01.** Task Sequences → New Task Sequence → ID `WIN10-BASE`, Template «Standard Client Task Sequence», ОС — импортированная, имя компьютера **не** задавать фиксировано (используем `%OSDComputerName%`). На шаге «Domain/Workgroup» выбрать Domain, `corp.vektor.lab`, учётка `VEKTOR\adm.student`, `MachineObjectOU = OU=Компьютеры,OU=Company,DC=corp,DC=vektor,DC=lab`. Ожидаемо: появилась задача с шагами Install Operating System → Apply Windows PE → …
8. **DEP01.** VS Code или Notepad: правка `D:\DeploymentShare\Control\CustomSettings.ini` — добавить `OSDComputerName=WIN10-CL02`, `SkipComputerName=YES`, `SkipDomainMembership=YES`, `TimeZoneName=Russian Standard Time`. Сохранить.
9. **DEP01.** Правый клик по Deployment Share → **Update Deployment Share** → «Completely regenerate» → подождать. Ожидаемо: в `D:\DeploymentShare\Boot` появился `LiteTouchPE_x64.wim` (~300–500 МБ).
10. **DEP01.** Server Manager → Add Roles → **Windows Deployment Services** → Transport Server + Deployment Server → настроить (`WDS` → правый клик на сервере → Configure Server → «Standalone», папка `D:\RemoteInstall`, «Do not respond to any client»). Затем Boot Images → Add Boot Image → `LiteTouchPE_x64.wim`. Затем свойства сервера → PXE Response → **Respond to all client computers**. В лабе это допустимо; в бою — только «Known clients», иначе любой желающий загрузит ваш сценарий.
11. **DC01.** Задать опции 66/67/60 (команды из раздела 4, пункт 9). Проверить: `Get-DhcpServerv4OptionValue -ScopeId 10.10.10.0 | Where-Object OptionId -in 60,66,67`.
12. **WIN10-CL02.** Сначала сохранить данные: `robocopy C:\Users\petrova \\FS01\HR\petrova-backup /E`. Затем выключить, в VMware Settings → Options → Advanced → Boot Order поставить **Network** первым. Включить. Ожидаемо: `Start PXE over IPv4...`, затем экран Microsoft Deployment Toolkit с запросом пароля (учётка `VEKTOR\adm.student`).
13. **WIN10-CL02.** В мастере LiteTouch: Task Sequence `WIN10-BASE` → Computer name подставится `WIN10-CL02` → Domain join автоматически → Start. Ждём 20–40 минут, ВМ перезагрузится 2–3 раза. Ожидаемо: загрузка в Windows 10 LTSC, машина в домене, объект в OU `Компьютеры`.
14. **WIN10-CL02.** Вернуть пользователя и результаты: `Get-ComputerInfo | Select CsName, CsDomain`, `gpupdate /force`, вход под `VEKTOR\petrova`, затем вернуть ей папку из бэкапа.

### Проверка

- [ ] `ping 10.10.10.35` с WIN11-CL01 проходит, `nslookup dep01.corp.vektor.lab` резолвится.
- [ ] На DC01: `Get-DhcpServerv4OptionValue -ScopeId 10.10.10.0` показывает 60/66/67.
- [ ] `Get-Service WDSServer` на DEP01 — Running.
- [ ] На WIN10-CL02: `Get-ComputerInfo | Select CsName, CsDomain` → `WIN10-CL02`, `corp.vektor.lab`.
- [ ] В Active Directory Users and Computers компьютер есть в `Company\Компьютеры`.
- [ ] `Get-EventLog -LogName "Microsoft-Windows-Deployment-Services-Diagnostics" -Newest 20` на DEP01 показывает успешную выдачу загрузочного образа `[ПРОВЕРИТЬ точное имя журнала]`.
- [ ] petrova входит под своей учёткой, видит `\\FS01\HR`.
- [ ] В `stand-log.md` дописаны DEP01, роль WDS, папка развёртывания, DHCP-опции.

### Сломай и почини

**Пользователь:** ivanov
**ВМ:** WIN11-CL01
**Что написал:** «Алексей, попробовали переустановить мой ПК по сети — вылезает ">>Start PXE over IPv4" и потом "No boot filename received", и дальше грузится старая Windows. Завтра отчёт по складу, срочно!»
**Срочность:** высокая, срок — до 18:00.

**Как воспроизвести поломку.** На DC01 удалить опцию 67:

```powershell
Remove-DhcpServerv4OptionValue -ScopeId 10.10.10.0 -OptionId 67 -Force
```

**Диагностика по порядку (снизу вверх по стеку).**

1. Клиент получает IP вообще? На WIN11-CL01 загрузиться в WinPE нельзя, поэтому смотрим в `ipconfig /all` после загрузки старой Windows или в логе DHCP на DC01. Если аренды нет — проблема в сети/scope DHCP, PXE тут ни при чём.
2. Сообщение «No boot filename received» прямо говорит: DHCP-ответ пришёл, а опции 67 (или 66) в нём нет. Проверяем: `Get-DhcpServerv4OptionValue -ScopeId 10.10.10.0 | Where-Object OptionId -in 66,67`.
3. Служба WDS жива? `Get-Service WDSServer` на DEP01. Если Stopped — `Start-Service WDSServer`.
4. Загрузочный образ реально есть? `wdsutil /get-image /ImageType:Boot` (или оснастка WDS → Boot Images). Пустой список = PXE пройдёт, но загрузчик не отдастся.
5. Сетевой адаптер клиента: в VMware Boot Order действительно Network первым? Если нет — «No boot filename» сменится на обычную загрузку с диска.
6. Файрвол на DEP01: для диагностики `Get-NetFirewallProfile | Select Name, Enabled`. Правило WDS (`Windows Deployment Services`) должно быть включено. Отключать профиль целиком не надо — в бою это дыра.

**Решение.** Вернуть опцию:

```powershell
Set-DhcpServerv4OptionValue -ScopeId 10.10.10.0 -OptionId 67 -Value "boot\x64\wdsnbp.com"
```

Проверка: клиент снова получает `wdsnbp.com` и стартует LiteTouch.

**Ответ пользователю:** «Иван, причина найдена: в настройках DHCP пропала строка с путём к загрузчику — поэтому ваш ПК не мог получить адрес образа. Вернул, сейчас машина ставится по сети, все ваши файлы с рабочего стола перенесены в `\\FS01\Buh\ivanov-backup`. По плану — сегодня до 18:00, пришлю подтверждение.»

**Закрытие заявки.** Категория «Развёртывание ПО / PXE», в решении указать: снята DHCP option 67, восстановлена, проверено на WIN11-CL01, добавлено в чек-лист приёмки изменений DHCP. В журнал изменений — кто, когда, зачем снял опцию (наверняка она пропала при правке scope другим администратором).

### Что изменилось на стенде

- Новая ВМ **DEP01**, Windows Server 2022 Evaluation, 10.10.10.35, в домене `corp.vektor.lab`, OU `Серверы`. Диски C: 40 ГБ, D: 60 ГБ.
- Роли DEP01: Windows Deployment Services (Transport + Deployment), standalone-режим, папка `D:\RemoteInstall`.
- MDT: Deployment Share `D:\DeploymentShare`, шара `\\DEP01\DeploymentShare$`, импортированная ОС `WIN10-LTSC`, task sequence `WIN10-BASE`, загрузочный образ `LiteTouchPE_x64.wim`.
- DC01: в DHCP-scope 10.10.10.0 добавлены опции 60/66/67.
- WIN10-CL02 переустановлен по сети, снова в домене, в OU `Компьютеры`; бэкап пользователя лежит в `\\FS01\HR\petrova-backup`.
- Учётные данные `adm.student` прописаны в `CustomSettings.ini` и в свойствах task sequence — это учебная конфигурация, см. раздел 6.

### Откат

Полный откат: на каждой ВМ (DC01, DEP01, WIN10-CL02) — VM → Snapshot Manager → `before-sysadmin-022` → Revert. После отката DEP01 в сети не будет, DHCP-опции исчезнут, WIN10-CL02 вернётся к состоянию до лабы. В `stand-log.md` добавь строку «откат к before-sysadmin-022», чтобы не путаться, какое состояние сейчас актуально. Частичный откат — только DHCP-опции (`Remove-DhcpServerv4OptionValue`) или снятие роли WDS; удалять DEP01 «насовсем» не нужно — она пригодится в уроках про автоматизацию.

## 6. Ошибки новичков и безопасность

1. **Готовить эталон, не сделав sysprep.** → Клоны приходят с одинаковым именем и хвостами конфигурации, ломается домен и WSUS-идентификаторы. → Всегда `/generalize /oobe /shutdown`, ответы — в `unattend.xml`.
2. **Делать sysprep на машине, введённой в домен.** → Ошибки подготовки, «осиротевший» компьютерный объект. → Эталон держим в рабочей группе, ввод в домен — шагом task sequence.
3. **Один «толстый» образ на все модели.** → 30 ГБ образ, в котором 90% драйверов не нужны, и обновлять его больно. → Тонкий образ (Windows + минимум) + драйверы по Make/Model через WMI в MDT + приложения отдельными шагами.
4. **Не тестировать сценарий перед раскаткой.** → Двадцать одинаково сломанных ПК. → Пилот на 1–2 машинах, отдельная task sequence/collection для пилота, только потом весь парк.
5. **Оставлять PXE «Respond to all clients».** → Любая машина в сети может загрузить ваш сценарий и получить доменную учётку. → `Known clients`/предварительное одобрение, отдельный VLAN развёртывания.
6. **Хранить пароль доменногоJoin-аккаунта в `bootstrap.ini`/`CustomSettings.ini` в открытом виде.** → Пароль виден любому, кто прочитал шару. → Отдельная учётка с минимальными правами (только «Join computers to domain» на нужном OU), ротация пароля, аудит.
7. **Раскатывать без бэкапа данных пользователя.** → Потеря рабочих файлов при «переустановке». → Сначала `robocopy` на FS01, потом развёртывание; зафиксировать в тикете.
8. **Забыть про BitLocker и лицензии.** → После переустановки не восстанавливаются ключи, а OEM-лицензия не даёт прав на корпоративный образ. → Ключи в AD (урок 16), права на reimaging — только через Volume Licensing (урок 23).

**Безопасность (что нельзя в боевой среде).** Держать Deployment Share с правами `Everyone: Full` — нельзя, это точка доставки любого ПО в домен; отключать файрвол на сервере развёртывания ради «чтобы PXE прошёл» — нельзя, открывай только UDP 67/69 и TCP 445 из VLAN развёртывания; хранить пароли развёртывания в открытом виде — нельзя; переустанавливать рабочий ПК без согласования окна и без бэкапа — нельзя; использовать для проверки образы «с торрентов» — нельзя (и незаконно).

## 7. Вопросы на собеседовании

**1. Чем отличаются WDS, MDT и MECM и что вы выберете для 150 машин?**
Ответ: WDS — только транспорт (PXE + отдача boot.wim и install.wim), никакой оркестрации, объявлен deprecated. MDT — бесплатная надстройка над WDS: task sequence, база драйверов, приложения, правила `CustomSettings.ini`, захват образа. MECM — полноценная платформа: SQL, точки ролей, collections, boundary groups, обновления, инвентаризация, развёртывание ПО, отчётность. Для 150 машин с бюджетом — MECM; без бюджета — MDT+WDS (но помнить про их статус) или связка WDS + GPO + WSUS; в облаке — Intune/Autopilot.
*Что хочет услышать интервьюер*: что вы понимаете разделение «транспорт / оркестрация / платформа», а не называете всё «SCCM».
*Красный флаг*: «MDT — это то же самое, что SCCM» или «WDS ставит приложения».

**2. Как вы построите образ: толстый, тонкий или гибридный и почему?**
Ответ: тонкий (thin) — чистая Windows с обновлениями и минимумом компонентов, всё остальное (1С, КриптоПро, драйверы, принтеры) навешивается в task sequence. Причины: образ меньше, обновляется без пересборки, приложения версионируются отдельно, драйверы подбираются по модели. Гибрид допустим, если софт тяжёлый и одинаковый для всех (например, пакет офисных приложений).
*Интервьюер*: инженерное мышление про жизненный цикл и стоимость поддержки, а не «я привык делать толстый».
*Красный флаг*: «сделаю один образ на все ПК и никогда его не буду обновлять».

**3. Что такое sysprep и что произойдёт, если его не выполнить?**
Ответ: sysprep переводит установленную ОС в состояние первой загрузки: `/generalize` убирает уникальные данные (SID компьютера, кэш драйверов, события), `/oobe` запускает мастер, `unattend.xml` отвечает на его вопросы. Без sysprep клоны будут с одинаковым именем и параметрами, что даёт конфликты в домене, проблемы с WSUS ID и активацией. Отдельно упомяну, что «дубликат SID» — не главная проблема, как часто думают; главное — сброс состояния и OOBE.
*Интервьюер*: практическое понимание процедуры и того, что делать с доменом и данными.
*Красный флаг*: «sysprep нужен, чтобы сменить пароль администратора».

**4. Как клиент находит сервер развёртывания по сети и что сломается в другом VLAN?**
Ответ: DHCP-клиент получает адрес, вместе с ним опции 66 (сервер) и 67 (загрузчик) или `PXEClient` (60); далее TFTP (UDP 69) тянет `wdsnbp.com`, `bootmgr`, `BCD`, `boot.sdi`, `boot.wim`. Если DHCP и WDS на одном сервере — опции 66/67 не задают. В другом VLAN без helper-адреса или без маршрутизации broadcast PXE не работает — нужны IP helpers на коммутаторе или DHCP-proxy; TFTP должен быть доступен маршрутизируемо, иначе клиент получит адрес, но не скачает загрузчик.
*Интервьюер*: понимание реальной сетевой части, а не только «нажал Update Deployment Share».
*Красный флаг*: «PXE работает по HTTP» или «достаточно прописать DNS».

**5. Autopilot vs классическое развёртывание: в чём разница и когда Autopilot неприменим?**
Ответ: Autopilot не разворачивает образ — он «привязывает» конкретное железо (по hardware hash) к Intune и при первом включении скачивает нужный профиль/редакцию из облака, применяет профили и приложения. Нет PXE, нет серверов, идеален для удалённых сотрудников. Ограничения: нужны Entra ID + Intune + лицензия, стабильный интернет и доступ к облаку Microsoft, TPM 2.0 и совместимое железо; нет поддержки сценариев вроде «накатить образ с 1С с диска»; в РФ 2026 доступность облачных сервисов Microsoft — предмет отдельного анализа (урок 23). Альтернативы on-prem: MECM OSD, ALD Pro / РЕД АДМ для отечественных ОС.
*Интервьюер*: что вы не путаете «установку ОС» и «регистрацию устройства» и понимаете ограничения облака.
*Красный флаг*: «Autopilot — это образ в облаке».

## 8. Источники (официальная документация)

- **learn.microsoft.com**, раздел «Microsoft Configuration Manager» → «Operating system deployment» — архитектура ролей, task sequences, PXE-enabled DP. Поиск: `Configuration Manager operating system deployment overview`.
- **learn.microsoft.com**, раздел «Microsoft Deployment Toolkit» — Deployment Workbench, CustomSettings.ini, driver database. Поиск: `Microsoft Deployment Toolkit documentation`.
- **learn.microsoft.com**, «Windows Assessment and Deployment Kit (ADK)» + «WinPE» — создание загрузочных образов, `copype`. Поиск: `Windows ADK WinPE documentation`.
- **learn.microsoft.com**, «Deployment Image Servicing and Management (DISM)» и «Windows Imaging and Configuration Designer» — работа с WIM, offline servicing. Поиск: `DISM image management command-line options`.
- **learn.microsoft.com**, «Sysprep» — параметры `/generalize`, `/oobe`, `unattend.xml` и проходы настройки. Поиск: `Sysprep command-line options unattend`.
- **learn.microsoft.com**, «Windows Deployment Services» — установка роли, `wdsutil`, взаимодействие с DHCP. Поиск: `Windows Deployment Services getting started`.
- **learn.microsoft.com**, «Windows Autopilot» и «Intune» — hardware hash, deployment profiles, enrollment. Поиск: `Windows Autopilot requirements hardware hash`.
- **learn.microsoft.com**, «Features removed or no longer developed in Windows Server» — актуальный статус WDS `[ПРОВЕРИТЬ]`. Поиск: `Windows Server deprecated features WDS`.
- **fogproject.org**, раздел документации — установка FOG, PXE, захват образов. Поиск: `FOG Project installation guide`.
- **clonezilla.org**, раздел «Clonezilla Server Edition / DRBL» — multicast-раскатка. Поиск: `Clonezilla SE DRBL server setup`.
- **ubuntu.com**, раздел «MAAS» — развёртывание Linux-парка. Поиск: `MAAS Ubuntu bare metal provisioning`.
- **astralinux.ru** и **redos.red-soft.ru** — официальные ресурсы ALD Pro и РЕД АДМ; конкретные возможности развёртывания и управления парком `[ПРОВЕРИТЬ]` на актуальных версиях.
