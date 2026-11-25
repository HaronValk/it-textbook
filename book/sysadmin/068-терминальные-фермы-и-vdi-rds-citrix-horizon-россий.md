# Терминальные фермы и VDI: RDS, Citrix, Horizon, российские решения, профили, FSLogix

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди IT:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Поставил дома на ноутбук 1С, но она тормозит и база не открывается. И ещё с домашнего компьютера нельзя — а мне надо сдать отчётность из дома. Можно как-то работать с рабочего стола удалённо, но чтобы там был мой рабочий стол, мои ярлыки и мои настройки 1С?»
> **Срочность:** средняя, но дедлайн отчётности — пятница

За этой заявкой стоит целый пласт работы системного администратора: терминальные фермы и VDI. Как только в компании появляется удалёнка, тонкие клиенты, бухгалтерия с 1С, подрядчики «на три месяца» или требование «данные не должны покидать контур» — появляется задача дать людям рабочий стол, который живёт не на их железе, а в датацентре.

Тема встречается постоянно, потому что: VDI закрывает и безопасность (данные не уходят на ноутбук), и лицензирование (одна лицензия на 1С — один сервер), и управляемость (переустановил шаблон — обновил сто человек). После этой главы ты должен уметь: различать session-based RDS и VDI и осознанно выбирать, разворачивать RDS-ферму через PowerShell, настраивать профили через FSLogix, диагностировать классические поломки «профиль не приехал», понимать ландшафт решений (Citrix, Horizon, Termidesk и К°) и говорить об этом на собеседовании на уровне middle.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Две принципиально разные модели.**

*Session-based (RDS Session Host).* Одна ОС Windows Server, внутри много одновременных пользовательских сессий. Пользователь получает не свою ОС, а «стол в коворкинге». Плотность огромная: 2 vCPU / 8 ГБ RAM держат 15–25 офисных пользователей. Дешево, управляемо, но все делят один реестр и одну файловую систему — «шумный сосед» роняет всех.

*VDI (Virtual Desktop Infrastructure).* Каждому пользователю — отдельная ВМ. Дороже в 3–5 раз по ресурсам, но полная изоляция, свои драйверы, свой софт. Бывает **personal** (постоянная ВМ, живёт с пользователем) и **pooled** (пул одноразовых ВМ: вошёл → получил чистую, вышел → ВМ уничтожилась, состояние подхватилось из профиля).

**Роли RDS.** Ферма — это не один сервер, а набор ролей:

```text
                 ИНТЕРНЕТ / ОФИС
                        |
                 [443]  v
            +---------------------+
            |  RD Gateway (443)   |   публикует RDP наружу без VPN
            +----------+----------+
                       |
            +----------v----------+        +---------------------+
            | RD Web Access (443)|------->| RD Connection Broker|
            | (веб-портал, .rdp) |        | (балансировка, база)|
            +---------------------+        +----+-----------+----+
                                                |           |
                             +------------------v+   +------v----------------+
                             | RD Session Host 1  |   | RD Session Host 2     |
                             | (сессии, прилож.)  |   | (сессии, прилож.)     |
                             +---------+----------+   +----------+------------+
                                       |                          |
                                       +----------> [445] <-------+
                                                       |
                                          +------------v-------------+
                                          | FS01: \\FS01\Profiles$   |
                                          | FSLogix VHDX-контейнеры  |
                                          +--------------------------+

            RD Licensing — отдельный сервер/роль, выдаёт RDS CAL
            RD Virtualization Host — только для VDI (роль Hyper-V)
```

**Протоколы и порты.**
- RDP — TCP 3389 (+ UDP 3389 для улучшенной доставки видео/графики). Шифруется TLS, аутентификация — NLA/CredSSP (сетевая аутентификация до создания сессии).
- RD Gateway / RD Web Access — TCP 443. Так пускают снаружи, не открывая 3389 в интернет.
- SMB 445 — до файлового сервера с профилями.
- Citrix: ICA/HDX — TCP 1494, Session Reliability — TCP 2598, доступ через Citrix Gateway — 443.
- VMware Horizon (сейчас бренд Omnissa): Blast Extreme — 8443 TCP/UDP, PCoIP — 4172 TCP/UDP, Connection Server — 443 [ПРОВЕРИТЬ актуальные порты в документации вендора].

**Профили — главная боль.** Пользователь ждёт «свой» рабочий стол: обои, «Мои документы», настройки Outlook, кэш 1С. Варианты:
1. **Локальный профиль** — привязан к конкретному хосту. Пользователь попал на другой сервер — пусто.
2. **Roaming profile** — папка профиля копируется по SMB при входе/выходе. Медленно, ломается при обрыве связи, конфликты при одновременном входе.
3. **Folder Redirection** — переносим только папки (Документы, Рабочий стол), настройки приложения остаются локальными.
4. **UE-V** — шаблоны настроек приложений, ограниченный набор.
5. **FSLogix Profile Container** — современный стандарт. Профиль целиком лежит в файле **VHDX** на файловой шаре, при входе этот диск **монтируется прямо в сессию**. Приложение видит обычный локальный профиль `C:\Users\ivanov`, но физически это сетевой виртуальный диск. Скорость — как у локального диска, а не копирование тысяч мелких файлов.
6. **FSLogix ODFC Container** — отдельный контейнер только под кэш Office/Teams/OneDrive (`.ost`, `.pst`), чтобы основной профиль не разрастался.

Плюс есть **Cloud Cache** — контейнер реплицируется в несколько мест (локальный диск + шара), сессия продолжает работать при падении шары.

## 3. Аналогия из жизни

Представь бизнес-центр. **Session Host** — это коворкинг: один большой зал, много столов, все дышат одним кондиционером и ходят в один туалет. Дешево, но если сосед принёс селёдку — пахнет у всех. **VDI** — отдельный кабинет на каждого: дорого, зато своя дверь и свой кондиционер.

**Профиль** — это личный сейф с твоими вещами. При локальном профиле сейф стоит в конкретном кабинете: переехал в другой — вещей нет. Roaming profile — это когда охранник при переезде на руках переносит сейф по этажам: долго, и если споткнулся — половина вещей рассыпалась. **FSLogix** — сейф на колёсах с рельсами: подъезжает к твоему столу целиком и подсоединяется за секунды.

Где аналогия ломается: сейф FSLogix одновременно может быть только у одного человека — второй вход того же пользователя на другой хост получит либо отказ, либо (в режиме Cloud Cache) другую копию, и синхронизация начнёт склеивать вещи из двух сейфов.

## 4. Практика и команды

**1. Проверить и установить роли RDS (RDS01, PowerShell от админа).**

```powershell
Get-WindowsFeature -Name RDS-*, RSAT-RDS*
Install-WindowsFeature -Name RDS-RD-Server, RDS-Connection-Broker, RDS-Web-Access, RDS-Licensing -IncludeManagementTools
```

`RDS-RD-Server` — сам Session Host (здесь живут сессии), `RDS-Connection-Broker` — «диспетчер», который решает, на какой хост посадить пользователя, `RDS-Web-Access` — веб-портал, `RDS-Licensing` — сервер лицензий. Вывод: `Success: True`, `Restart Needed: Yes`. В продакшене роли раскидывают по разным ВМ, на лабе всё на одной.

**2. Создать развёртывание и коллекцию.**

```powershell
New-RDSessionDeployment -ConnectionBroker "RDS01.corp.vektor.lab" `
  -WebAccessServer "RDS01.corp.vektor.lab" -SessionHost "RDS01.corp.vektor.lab"

New-RDSessionCollection -CollectionName "Vektor-Office" -SessionHost "RDS01.corp.vektor.lab" `
  -ConnectionBroker "RDS01.corp.vektor.lab"
```

Коллекция — логическая группа хостов и опубликованных приложений. Проверка:

```powershell
Get-RDSessionCollection -ConnectionBroker "RDS01.corp.vektor.lab" | Format-List
```

Смотрим `CollectionName`, `ResourceType`, `Size` (число хостов в коллекции).

**3. Опубликовать приложение (RemoteApp).**

```powershell
New-RDRemoteApp -CollectionName "Vektor-Office" -DisplayName "Блокнот" `
  -FilePath "C:\Windows\System32\notepad.exe" -ShowInWebAccess 1 `
  -ConnectionBroker "RDS01.corp.vektor.lab"
```

`-FilePath` — путь к .exe на session host. UWP-приложения (новый «Калькулятор», «Ножницы») так не публикуются — нужен обходной путь [ПРОВЕРИТЬ под свою версию ОС].

**4. Настройки сессии через RDP-свойства коллекции.**

```powershell
Set-RDSessionCollectionConfiguration -CollectionName "Vektor-Office" `
  -CustomRdpProperty "redirectclipboard:i:1;drivestoredirect:s:*;usbdevicestoredirect:s:*" `
  -ConnectionBroker "RDS01.corp.vektor.lab"
```

`redirectclipboard` — общий буфер обмена, `drivestoredirect` — проброс локальных дисков клиента, `usbdevicestoredirect` — проброс USB-токенов (нужно для КриптоПро и ЭЦП). Осторожно: проброс дисков = канал утечки данных, в режиме «безопасный контур» его выключают.

**5. Кто сидит на ферме прямо сейчас.**

```powershell
Get-RDUserSession -CollectionName "Vektor-Office" -ConnectionBroker "RDS01.corp.vektor.lab" |
  Select-Object UserName, HostServer, UnifiedSessionId, SessionState, CreateTime
```

Альтернатива без PowerShell, работает и на самом хосте:

```cmd
quser /server:RDS01
```

```
 USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
 ivanov                rdp-tcp#3           3  Active          .  24.03.2026 09:12
 petrova               rdp-tcp#5           5  Disc           14  24.03.2026 09:40
```

`Disc` — отключённая сессия, держит ресурсы. Смотрим IDLE TIME и решаем, добивать ли.

**6. Принудительно завершить сессию.**

```powershell
Invoke-RDUserLogoff -HostServer "RDS01.corp.vektor.lab" -UnifiedSessionID 5 -Force
```

```cmd
logoff 5 /server:RDS01
```

**7. Теневое подключение к сессии пользователя (shadow).**

```cmd
mstsc /shadow:3 /v:RDS01.corp.vektor.lab /control
```

Админ видит экран ivanov и может управлять мышью — «подойди и покажи». Требует включённой политики *Set rules for remote control of Remote Desktop Services user sessions* (`Computer Configuration → Administrative Templates → Windows Components → Remote Desktop Services → Remote Desktop Session Host → Connections`). По умолчанию — запрещено, и это правильно: это фактически слежка.

**8. Установить агент FSLogix на session host.**

```cmd
msiexec /i FSLogixAppsSetup.msi /qn /norestart
```

Проверка, что службы и драйверы поднялись:

```powershell
Get-Service -Name 'frx*' | Select-Object Name, Status
```

Ожидаем две службы в состоянии `Running` [ПРОВЕРИТЬ точные имена служб в своей версии агента].

**9. Включить контейнер профилей через реестр.**

```powershell
$p = 'HKLM:\SOFTWARE\FSLogix\Profiles'
New-Item -Path $p -Force | Out-Null
New-ItemProperty -Path $p -Name Enabled      -PropertyType DWord      -Value 1 -Force
New-ItemProperty -Path $p -Name VHDLocations -PropertyType MultiString -Value '\\FS01\Profiles$' -Force
New-ItemProperty -Path $p -Name SizeInMBs    -PropertyType DWord      -Value 30720 -Force
Restart-Computer
```

`VHDLocations` — где искать контейнеры пользователей, `SizeInMBs` — максимальный размер динамического VHDX (30 ГБ для офисного профиля обычно с запасом; зависит от кэша Outlook и 1С).

**10. Создать шару под профили на FS01 и выдать права.**

```powershell
New-Item -Path 'D:\Profiles' -ItemType Directory -Force
New-SmbShare -Name 'Profiles$' -Path 'D:\Profiles' -FullAccess 'VEKTOR\Domain Admins'
Grant-SmbShareAccess -Name 'Profiles$' -AccountName 'VEKTOR\Domain Users'     -AccessRight Change -Force
Grant-SmbShareAccess -Name 'Profiles$' -AccountName 'VEKTOR\Domain Computers' -AccessRight Full   -Force

icacls D:\Profiles /grant "VEKTOR\Domain Computers:(OI)(CI)(F)"
icacls D:\Profiles /grant "VEKTOR\Domain Users:(OI)(CI)(M)"
icacls D:\Profiles /grant "CREATOR OWNER:(OI)(CI)(IO)(F)"
```

Ключевой момент: **компьютерный аккаунт session host** (`RDS01$`, он входит в `Domain Computers`) должен иметь Full Control на корне шары — именно он создаёт папку и VHDX при первом входе. Пользователю — Modify. Это типовая схема, сверься с актуальной документацией Microsoft [ПРОВЕРИТЬ]. Дать `Domain Computers` полный доступ — шире, чем нужно в бою; в продакшене заводят отдельную группу `GG-FSLogix-Hosts` и кладут туда только хосты фермы.

**11. Проверить, что контейнеры реально создаются.**

```powershell
Get-ChildItem '\\FS01\Profiles$\' -Recurse -Filter *.vhdx |
  Select-Object FullName, Length, LastWriteTime
```

Ожидаем `\\FS01\Profiles$\ivanov\Profile-ivanov.vhdx` (имя зависит от настроек `FlipFlopProfileDirectoryName`).

**12. Диагностика FSLogix — журнал событий.**

```powershell
Get-WinEvent -LogName 'Microsoft-Windows-FSLogix-Apps/Operational' -MaxEvents 30 |
  Where-Object LevelDisplayName -in 'Error','Warning' |
  Select-Object TimeCreated, Id, Message
```

Здесь видно «не удалосьattachить контейнер», «доступ запрещён», «таймаут». Первое место, куда смотрят при жалобе «профиль пустой».

**13. Подключение с клиента и с Linux.**

```cmd
mstsc /v:RDS01.corp.vektor.lab /f
```

```bash
sudo apt install freerdp3-x11   # или freerdp2-x11 в старых репозиториях [ПРОВЕРИТЬ имя пакета]
xfreerdp /v:RDS01.corp.vektor.lab /u:ivanov /d:VEKTOR /f /dynamic-resolution
```

Из Linux удобно проверять, что ферма отдаёт RDP и что NLA работает, не поднимая Windows-клиент. На UBU01/ASTRA01 это законный способ для дежурной смены.

**14. Лицензирование RDS.**

```powershell
Set-RDLicenseConfiguration -LicenseServer "RDS01.corp.vektor.lab" -Mode PerUser `
  -ConnectionBroker "RDS01.corp.vektor.lab"
Get-RDLicenseConfiguration -ConnectionBroker "RDS01.corp.vektor.lab"
```

`PerUser` — по числу уникальных пользователей (удобно при сменных рабочих местах), `PerDevice` — по числу устройств. Без настроенного лицензирования ферма работает в льготном периоде ограниченное время (порядка 120 дней), потом отказывает в подключении [ПРОВЕРИТЬ точный срок в актуальной документации].

**15. GUI там, где без него неудобно.** `Server Manager → Remote Desktop Services → Collections → Vektor-Office → Tasks` — публикация RemoteApp, правка свойств сессии. `dsa.msc` — группы доступа. `fsmgmt.msc` — открытые файлы на шаре профилей (видно, кто «держит» VHDX).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Тема требует сервера, которого нет в таблице стенда L2, поэтому по правилам трека добавляем его явно: **RDS01** — Windows Server 2022 (Evaluation), 10.10.10.35, 2 vCPU / 4 ГБ RAM / 60 ГБ диск, член домена `corp.vektor.lab`.

### Предусловия

- Стенд L2 развёрнут: DC01 (AD DS, DNS, DHCP), FS01 (файловый сервер), WIN11-CL01, WIN10-CL02, UBU01.
- Учётки `adm.student` (админ домена), `ivanov`, `petrova`, пароль стенда `Lab#2026-vektor`.
- На DC01 создана OU `Company → IT`, есть группы `GG-IT`, `GG-Buh`.
- На FS01 есть свободный диск `D:` (или создай виртуальный диск в VMware, 40 ГБ).

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-sysadmin-068`. Затрагиваем: **DC01, FS01, RDS01, WIN11-CL01**.

### Шаги

1. **RDS01 (до ввода в домен).** Создай ВМ, поставь Windows Server 2022 Evaluation, задай имя `RDS01`, статический IP `10.10.10.35/24`, шлюз `10.10.10.2`, DNS `10.10.10.10`. *Ожидаемо:* `ipconfig /all` показывает нужные адреса, `ping dc01.corp.vektor.lab` отвечает.
2. **RDS01.** Введи в домен: `Add-Computer -DomainName corp.vektor.lab -Credential VEKTOR\adm.student -Restart`. *Ожидаемо:* после перезагрузки `whoami` → `vektor\adm.student`, компьютер в OU `Компьютеры`.
3. **DC01.** Создай группу доступа к ферме и положи туда пользователей:
   ```powershell
   New-ADGroup -Name "GG-RDS-Users" -GroupScope Global -Path "OU=IT,OU=Company,DC=corp,DC=vektor,DC=lab"
   Add-ADGroupMember -Identity "GG-RDS-Users" -Members ivanov, petrova
   ```
   *Ожидаемо:* `Get-ADGroupMember GG-RDS-Users` показывает двоих.
4. **RDS01.** Установи роли (команда №1 из раздела 4), перезагрузись. *Ожидаемо:* `Get-WindowsFeature RDS-RD-Server` → `Installed: True`.
5. **RDS01.** Создай развёртывание и коллекцию `Vektor-Office` (команда №2). *Ожидаемо:* `Get-RDSessionCollection` возвращает коллекцию с одним хостом.
6. **RDS01.** Опубликуй RemoteApp «Блокнот» (команда №3) и включи проброс буфера обмена (№4). *Ожидаемо:* в `Get-RDRemoteApp -CollectionName "Vektor-Office"` появилась запись с `DisplayName = Блокнот`.
7. **FS01.** Создай шару `\\FS01\Profiles$` и права (команда №10). *Ожидаемо:* `Get-SmbShareAccess -Name 'Profiles$'` показывает три записи ACE.
8. **RDS01.** Установи FSLogix (№8), настрой реестр (№9), перезагрузись. *Ожидаемо:* службы `frx*` в статусе `Running`.
9. **RDS01.** Настрой лицензирование в режиме `PerUser` (№14). *Ожидаемо:* `Get-RDLicenseConfiguration` показывает `Mode: PerUser`.
10. **WIN11-CL01.** Зайди как `VEKTOR\ivanov` через `mstsc /v:RDS01.corp.vektor.lab`. Создай на удалённом рабочем столе файл `тест-ivanov.txt`. Выйди (не «отключиться», а «выйти»). *Ожидаемо:* сессия завершается без ошибок профиля.
11. **FS01.** Проверь контейнер (команда №11). *Ожидаемо:* появился `Profile-ivanov.vhdx`.
12. **WIN11-CL01.** Зайди снова как `ivanov`. *Ожидаемо:* файл `тест-ivanov.txt` на месте — профиль приехал из контейнера.
13. **WIN10-CL02.** Зайди как `petrova` параллельно (вторая лицензия/сессия). *Ожидаемо:* у Петровой свой отдельный VHDX, файлы Иванова не видны.
14. **RDS01.** Пока `petrova` в сессии, со стороны админа открой теневое подключение (№7). *Ожидаемо:* видно её экран во втором окне mstsc.

### Проверка

- [ ] `Get-RDSessionCollection` на RDS01 показывает `Vektor-Office` с одним session host.
- [ ] `mstsc /v:RDS01.corp.vektor.lab` под `ivanov` пускает по домену, без локальных учёток RDS01.
- [ ] В `\\FS01\Profiles$\` есть по одному VHDX на Иванова и Петрову.
- [ ] Файл, созданный в первой сессии Иванова, виден во второй.
- [ ] `Get-RDUserSession` показывает обеих пользовательниц с разными `UnifiedSessionId`.
- [ ] `Get-WinEvent -LogName 'Microsoft-Windows-FSLogix-Apps/Operational'` не содержит ошибок уровня Error за последний час.
- [ ] В `mstsc /shadow` видно сессию Петровой.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Опять эта удалёнка! Захожу на рабочий стол, а он как новый: ни ярлыков, ни папки „Отчётность“, 1С спрашивает настройки заново. Вчера всё было. Сдавать отчёт в пятницу!»
> **Срочность:** высокая

**Как воспроизвести поломку намеренно.** На FS01 убери права у компьютерного аккаунта хоста:

```powershell
icacls D:\Profiles /remove:g "VEKTOR\RDS01$"
```

Затем на RDS01 заверши сессию Иванова (`Invoke-RDUserLogoff`) и попроси войти снова. Эффект тот же: FSLogix не может создать/смонтировать контейнер, Windows поднимает **временный профиль** (`C:\Users\TEMP.RDS01.001`) — рабочий стол пустой.

**Диагностика по шагам.**

1. `Get-RDUserSession -CollectionName "Vektor-Office" -ConnectionBroker "RDS01.corp.vektor.lab"` — убедиться, что сессия одна и висит на RDS01. Ожидаемо: `SessionState: Active`.
2. На RDS01: `Get-WinEvent -LogName 'Microsoft-Windows-FSLogix-Apps/Operational' -MaxEvents 50` — искать `Error` с текстом про access denied / failed to attach. Это подтверждает гипотезу за одну минуту.
3. Проверить, что шара жива: `Test-Path '\\FS01\Profiles$\'` с RDS01. Если `False` — проблема в сети/шаре, а не в правах.
4. Посмотреть права шары: `Get-SmbShareAccess -Name 'Profiles$' -CimSession FS01` и NTFS: `icacls \\FS01\Profiles$`.
5. Проверить, существует ли проблемный контейнер и кто владелец: `Get-ChildItem '\\FS01\Profiles$\ivanov' | Select-Object Name, Length`.
6. Проверить, не «держит» ли VHDX зависшая сессия: `openfiles /query /s FS01` или `fsmgmt.msc` на FS01.

**Решение.**

```powershell
icacls D:\Profiles /grant "VEKTOR\RDS01$:(OI)(CI)(F)"
```

На RDS01: `Invoke-RDUserLogoff -HostServer "RDS01.corp.vektor.lab" -UnifiedSessionID <id> -Force`, попросить войти заново. Профиль подхватится.

**Ответ пользователю:** «Иван, проблема была на стороне сервера профилей — доступ к вашему контейнеру настроек потерялся. Исправили, войдите заново: рабочий стол, ярлыки и настройки 1С вернутся. Если увидите пустой стол ещё раз — сразу напишите, посмотрим адресно. Отчётность не пострадала: файлы в „Документах“ на сетевом диске в порядке.»

**Закрытие заявки:** категория «VDI / профили», решение — «восстановлены NTFS-права компьютерного аккаунта session host на шару профилей», в постмортем добавить пункт «проверять права после изменений на FS01» и, в идеале, мониторинг на событие FSLogix Error.

### Что изменилось на стенде

- Новая ВМ **RDS01** — Windows Server 2022 Evaluation, `10.10.10.35`, в домене, роли RDS Session Host / Connection Broker / Web Access / Licensing.
- Коллекция **Vektor-Office** с опубликованным RemoteApp «Блокнот», включён проброс буфера и дисков.
- На DC01 появилась группа **GG-RDS-Users** (ivanov, petrova), лежит в `OU=IT,OU=Company`.
- На FS01 создана папка `D:\Profiles` и шара **`\\FS01\Profiles$`** с правами для `Domain Users` (Change) и `Domain Computers` (Full).
- На RDS01 установлен **FSLogix Apps**, включён контейнер профилей, `VHDLocations = \\FS01\Profiles$`, размер 30 ГБ.
- Созданы контейнеры `Profile-ivanov.vhdx`, `Profile-petrova.vhdx`.
- RD Licensing переведён в режим **PerUser**.

### Откат

VMware Workstation → выбери ВМ → **Snapshot Manager** → снапшот `before-sysadmin-068` → **Restore**. Откатывать нужно **все четыре** ВМ (DC01, FS01, RDS01, WIN11-CL01), иначе получишь рассинхрон: например, DC помнит группу `GG-RDS-Users`, а FS01 — нет, или наоборот. Если откатываешь только RDS01 — не забудь вручную убрать `\\FS01\Profiles$` и группу на DC01, иначе они останутся «сиротами» в журнале стенда.

## 6. Ошибки новичков и безопасность

1. **«Сделаю RDS на контроллере домена».** → Пользовательские сессии, нагрузка от Office и случайные админские действия ломают DC; любая компрометация сессии = компрометация домена. → Session Host всегда отдельная ВМ (у нас RDS01), DC01 не трогаем.
2. **Ожидать, что roaming profile «просто заработает».** → Медленные входы, «временный профиль», конфликты при двух сессиях, потеря настроек. → FSLogix Profile Container + Folder Redirection для тяжёлых папок.
3. **Дать на шару профилей `Everyone: Full Control`.** → Любой пользователь может прочитать/подменить чужой VHDX, а внутри — токены, кэш 1С, пароли в браузере. → Группа хостов + пользователь Modify на своей папке, Creator Owner.
4. **Открыть 3389 в интернет «чтобы работало с домашнего компа».** → Brute-force и BlueKeep-подобные уязвимости, шифровальщики через RDP — самая частая точка входа. → RD Gateway на 443 + MFA, либо VPN.
5. **Ставить FSLogix «потом настрою размеры».** → VHDX разрастается до дефолтного лимита, шара забита, у всех «нет места». → `SizeInMBs`, мониторинг свободного места на FS01,定期 архивирование брошенных контейнеров.
6. **Не завершать отключённые сессии.** → RAM и лицензии съедены «мёртвыми» пользователями. → GPO на таймауты: `Set time limit for disconnected sessions`, `End session when time limits are reached`.
7. **Игнорировать антивирус на шаре профилей.** → Сканирование VHDX «на лету» даёт блокировки и порчу контейнеров; либо наоборот — вирус разносится по всем профилям. → Исключения для `*.vhdx` по документации вендора + сканирование в окно обслуживания.
8. **Публиковать RemoteApp «в мир» без сегментации.** → Ферма становится точкой входа во всю сеть. → DMZ-сегмент для Gateway, firewall между сегментами, отдельные учётки для внешнего доступа.

**Безопасность (что нельзя в боевой среде).** Не выдавать `Domain Computers` полный доступ на шару профилей — только выделенной группе хостов. Не использовать теневые подключения без регламента и уведомления сотрудников (в РФ это ещё и вопрос 152-ФЗ о персональных данных). Не хранить на одном хосте фермы и личные профили, и секреты домена. Не запускать сессии пользователей с локальным администратором — на session host это означает админа у всех соседей по серверу.

## 7. Вопросы на собеседовании

**1. Чем session-based RDS отличается от VDI и когда что выбирать?**
Ответ: в RDS одна ОС обслуживает много сессий — высокая плотность, дешевле, проще обновлять; минус — общий реестр и файловая система, конфликты версий софта, «шумный сосед». VDI — отдельная ВМ на пользователя: изоляция, свои драйверы и версии ПО, но в разы дороже по RAM/CPU/дискам и сложнее в обслуживании (шаблоны, пулы, компоуз). Выбор: офисные задачи (Office, 1С, браузер) — RDS; разработчики, CAD, специфичный софт, требования ИБ по изоляции — VDI.
*Что хочет услышать:* понимание экономики (плотность) и ограничений, а не только названия продуктов.
*Красный флаг:* «VDI всегда лучше, потому что безопаснее».

**2. Почему обычные roaming-профили плохо работают на ферме и как это решает FSLogix?**
Ответ: roaming копирует тысячи мелких файлов по SMB при входе и выходе — медленный вход, обрыв связи ломает профиль, одновременный вход даёт конфликты. FSLogix держит профиль в VHDX на шаре и **монтирует** его в сессию: приложение работает с локальным диском, копирования нет, вход — секунды. Плюс ODFC-контейнер отдельно под кэш Office и Cloud Cache для устойчивости к падению шары.
*Что хочет услышать:* слово «mount», а не «copy», и разницу между Profile и ODFC.
*Красный флаг:* «FSLogix — это просто папка на сетевом диске».

**3. Как FSLogix устроен внутри и какие права нужны на шару профилей?**
Ответ: агент FSLogix ставится на хост, драйвер перехватывает доступ к `C:\Users\<user>` и подсовывает смонтированный VHDX (`Profile-<user>.vhdx`, динамический, с лимитом `SizeInMBs`). Права: share — пользователи Change, хосты Full; NTFS — компьютерные аккаунты session host Full Control на корне (создают папки и VHDX), пользователь Modify на своей папке, Creator Owner. Файл не должен одновременно монтироваться в две сессии.
*Что хочет услышать:* что именно **компьютерный аккаунт** хоста создаёт контейнер — на этом ломается 80% внедрений.
*Красный флаг:* «Дам Everyone Full Control, чтобы точно заработало».

**4. Пользователь жалуется, что на терминале пустой рабочий стол. Ваши действия?**
Ответ: сначала отличить временный профиль от проблем с ярлыками. Проверяю `Get-RDUserSession` (одна ли я сессия, какой хост), затем журнал `Microsoft-Windows-FSLogix-Apps/Operational` на Error — там будет причина (нет доступа, нет места, контейнер заблокирован). Дальше `Test-Path` до шары, `Get-SmbShareAccess`, NTFS через `icacls`, проверка свободного места на FS01 и `fsmgmt.msc` на предмет «кто держит VHDX». Лечение: права/место/завершение зависшей сессии, затем повторный вход. Параллельно — проверяю, у одного пользователя проблема или у всех (одна сессия = проблема пользователя, все = шара или хост).
*Что хочет услышать:* порядок «сессия → лог → доступ → место», а не «перезагружу сервер».
*Красный флаг:* «Переустановлю FSLogix» / «перезагружу session host днём».

**5. Спроектируйте терминальный доступ для 100 пользователей с требованиями импортозамещения.**
Ответ: считаю нагрузку (для офиса ~10–15 сессий на 4 vCPU / 16 ГБ, дальше — по факту тестов), беру минимум два session host за Connection Broker для HA, отдельный файловый сервер под FSLogix-профили с бэкапом, RD Gateway в DMZ для внешнего доступа. ОС и платформа — из реестра российского ПО (Astra Linux, РЕД ОС) с VDI-решением вроде Termidesk [ПРОВЕРИТЬ актуальный список вендоров и совместимость на 2026 год], гипервизор — отечественный или Proxmox VE как учебный/пилотный вариант. Печати и сканеры проверяю заранее — они главный источник боли в терминале. Лицензии: RDS CAL или VDA-подписка, для отечественного стека — лицензии вендора. Документирую: схему, регламент входа, инструкцию пользователю, план восстановления.
*Что хочет услышать:* HA, бэкап профилей, отдельный Gateway, проверка совместимости периферии и лицензий, а не только «поставлю Citrix».
*Красный флаг:* «Возьмём VMware Horizon» без учёта доступности лицензий в РФ и без альтернатив.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Remote Desktop Services documentation» (архитектура ролей, RD Gateway, лицензирование). Поиск: `RDS session host deployment PowerShell`.
- **learn.microsoft.com** — раздел «FSLogix documentation» (Profile Container, ODFC, prerequisites, permissions, Cloud Cache). Поиск: `FSLogix profile container prerequisites permissions`.
- **learn.microsoft.com** — «Remote Desktop Services cmdlets» (справка по `New-RDSessionDeployment`, `New-RDRemoteApp`, `Set-RDSessionCollectionConfiguration`). Поиск: `RDS PowerShell cmdlets reference`.
- **citrix.com** — «Citrix Virtual Apps and Desktops 7 documentation» (Delivery Controller, VDA, StoreFront, Profile Management). Поиск: `Citrix Virtual Apps and Desktops architecture overview`.
- **omnissa.com** — документация Horizon (бывшая линейка VMware EUC; бренд и доступность в РФ уточнять) [ПРОВЕРИТЬ]. Поиск: `Omnissa Horizon architecture Blast Extreme ports`.
- **reestr.digital.gov.ru** — реестр российского ПО Минцифры, класс «средства виртуализации» и «VDI» (там актуальный на 2026 год список отечественных решений) [ПРОВЕРИТЬ домен реестра].
- **termidesk.ru** — сайт и документация Termidesk (российская VDI-платформа) [ПРОВЕРИТЬ точный домен и наличие публичной документации].
- **ubuntu.com / freerdp.com** — установка и параметры `xfreerdp` для Linux-клиента. Поиск: `FreeRDP command line options`.
