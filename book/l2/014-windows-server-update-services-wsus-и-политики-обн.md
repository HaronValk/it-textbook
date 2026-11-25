# Windows Server Update Services (WSUS) и политики обновлений: развёртывание, группы, одобрение, отчёты

## 1. Введение (зачем это нужно в реальной работе)

**Заявка № 2141.** Пользователь: Мария Петрова. ВМ: WIN10-CL02 (HR). Что написала: «Вчера компьютер перезагрузился прямо во время работы, документ не сохранился. А в центре обновления написано, что последняя проверка была 4 месяца назад. Можно как-то сделать, чтобы он обновлялся, когда я не работаю?» Срочность: средняя. Параллельно безопасник просит сводку: «Сколько машин не получило критичные патчи за последние 30 дней?»

Дальше вы будете жить с этой темой постоянно. Обновления — единственный способ закрыть уязвимость на 50 машинах за одну ночь, не обходя каждую руками. При этом у обновлений есть неприятное свойство: они ломают работающее ПО (клиент 1С, КриптоПро, кассовые драйверы) и перезагружают компьютеры в самый неподходящий момент. Поэтому в реальной работе задача звучит не «поставить обновления», а «поставить нужные обновления нужным машинам в нужное время и доказать это отчётом». WSUS (Windows Server Update Services) — бесплатная роль Windows Server, которая даёт ровно это: один сервер-раздатчик внутри сети, группы компьютеров, ручное и автоматическое одобрение, дедлайны и отчёты. После главы вы сможете развернуть WSUS с нуля, разложить клиентов по группам через GPO, провести пилотное обновление, настроить дедлайн и объяснить руководителю цифрами, что происходит с парком.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

WSUS — это прикладной сервис (L7 модели OSI) поверх HTTP/HTTPS. Никакой магии: IIS-сайт «WSUS Administration» на портах **8530 (HTTP)** и **8531 (HTTPS)** — не 80/443, чтобы не конфликтовать с другими сайтами. Плюс база данных (по умолчанию WID — Windows Internal Database, бесплатный встроенный SQL-сервер; альтернатива — полноценный MS SQL) и папка с самими файлами обновлений (контент).

Как это работает по шагам:

```text
        Microsoft Update (upstream, HTTPS 443)
                 │  1) синхронизация: метаданные каталога + файлы обновлений
                 ▼
 ┌────────────────────────── FS01 (роль WSUS) ──────────────────────────┐
 │  IIS: сайт WSUS Administration   :8530 / :8531                        │
 │  БД: WID (или SQL)               Контент: папка WSUSContent           │
 │  Одобрения: кто, что, куда, с каким дедлайном                        │
 │  Группы компьютеров: Pilot / Workstations / Servers                  │
 └──────────────────────────────────────────────────────────────────────┘
        ▲  2) GPO сообщает клиенту адрес http://FS01:8530
        │  3) клиент: проверка (detect) → отчёт о состоянии → скачивание
        │  4) установка по расписанию или по дедлайну
   DC01, WIN11-CL01, WIN10-CL02  (Windows Update Agent — служба wuauserv)
```

Ключевые сущности, которые надо держать в голове:

- **Продукты и классификации.** WSUS не скачивает «всё подряд»: вы выбираете продукты (Windows 11, Windows 10, Windows Server 2022) и классификации (Critical Updates, Security Updates, Definition Updates, Update Rollups, Drivers, Upgrades). Метаданные приходят всегда — файлы только по выбранному.
- **Одобрение (approval).** Обновление, скачанное на сервер, никому не ставится, пока вы не одобрите его для группы: *Install* (установить), *Not Approved* (по умолчанию), *Decline* (отклонить, больше не предлагать).
- **Дедлайн (deadline).** Дата-время, после которого клиент обязан поставить обновление и перезагрузиться, даже если расписание GPO говорит «в 3:00 в воскресенье». Дедлайн задаётся только в консоли при одобрении, в правилах автоодобрения его нет `[ПРОВЕРИТЬ]`.
- **Группы компьютеров.** Два способа: серверные группы (перетаскиваешь мышкой в консоли) и client-side targeting (GPO говорит клиенту «ты из группы Workstations»). Второй — единственный рабочий для 50+ машин.
- **Supersedence (замещение).** Новый патч заменяет старый; старый помечается superseded. Чистка отклонённых/замещённых — часть гигиены.
- **Dual scan.** Windows 10/11 умеют параллельно ходить в интернет на Microsoft Update, если включены политики Windows Update for Business или не выставлен запрет. Тогда WSUS можно настроить идеально, а машина всё равно возьмёт патч из интернета.

Важная реалия 2026 года: **WSUS объявлен deprecated (сентябрь 2024)** — новые функции не разрабатываются, поддерживается то, что есть `[ПРОВЕРИТЬ актуальный статус]`. При этом в российских компаниях он живёт массово, потому что бесплатный, локальный и не требует облака. Знайте оба факта: «работаем на WSUS» и «стратегически смотрим в сторону Intune/Autopatch/Azure Update Manager, а в РФ — в сторону локальных систем патч-менеджмента».

## 3. Аналогия из жизни

Представьте склад автозапчастей при большом автопарке. Машины (компьютеры) не ездят за деталями в город (Microsoft Update) — это долго и создаёт пробки. Детали привозят на склад (WSUS-сервер), где кладовщик (администратор) решает: эту деталь в цех №1 (группа Workstations) — можно, в цех №2 (Servers) — пока нельзя, потому что там стоит важный станок (1С, контроллер домена). Есть журнал (отчёты), где видно, какая машина какую деталь уже получила. Есть наряд «заменить до пятницы» (дедлайн), который главнее обычного графика. Аналогия перестаёт работать в одном месте: деталь можно поставить на машину и «открутить назад» — но не всегда безболезненно, а на сервере с ролью откат патча иногда означает часы простоя.

## 4. Практика и команды

**1. Установка роли (на FS01, PowerShell от администратора).**

```powershell
Install-WindowsFeature -Name UpdateServices -IncludeManagementTools
```

`UpdateServices` тянет IIS, .NET, WID. `-IncludeManagementTools` добавляет консоль и модуль PowerShell `UpdateServices`.

**2. Postinstall — создание БД и папки контента.** При установке из GUI мастер делает это сам; при установке через PowerShell — обязательно вручную, иначе консоль не откроется.

```powershell
& "C:\Program Files\Update Services\Tools\WsusUtil.exe" postinstall CONTENT_DIR=C:\WSUS
```

`CONTENT_DIR` — куда складывать файлы. На стенде C:, в бою — отдельный том (легко получить 100–500 ГБ).

**3. Проверка сервера и подписки.**

```powershell
Get-WsusServer | Select-Object Name, PortNumber, Version
(Get-WsusServer).GetSubscription() | Select-Object SynchronizeAutomatically, LastSynchronizationTime
```

Порт должен быть 8530 (или 8531 при SSL). `LastSynchronizationTime` — когда последний раз забирали каталог с Microsoft.

**4. Продукты и классификации.**

```powershell
Get-WsusProduct | Where-Object Title -match 'Windows 11|Windows 10|Server 2022' |
    ForEach-Object { Set-WsusProduct -Product $_ }
Set-WsusClassification -Classification (Get-WsusClassification | Where-Object Title -match 'Security|Critical')
```

Смотреть: в списке продуктов должны быть именно те ОС, что в парке. Лишние продукты = лишние гигабайты метаданных и путаница в отчётах.

**5. Ручная синхронизация.**

```powershell
(Get-WsusServer).GetSubscription().StartSynchronization()
```

Синхронизация с Microsoft идёт минуты-часы; первая — долгая. Клиенты в это время не страдают, они работают с тем, что уже есть.

**6. Поиск нужных обновлений.**

```powershell
$wsus = Get-WsusServer
$wsus | Get-WsusUpdate -Classification Security -Approval Unapproved -Status Needed |
    Select-Object -First 20 Title, ClassificationTitle, CreationDate
```

`-Approval` (Approved/Unapproved/Declined/All) и `-Status` (Needed, FailedOrNeeded, Any) — главные фильтры. Точные имена свойств объекта смотрите через `| Get-Member`: в разных версиях модуля они отличаются `[ПРОВЕРИТЬ]`.

**7. Одобрение на группу.**

```powershell
$u = $wsus | Get-WsusUpdate -Classification Security -Approval Unapproved -Status Needed |
     Where-Object Title -match 'Cumulative Update'
$u | Approve-WsusUpdate -Action Install -TargetGroupName 'Workstations' -Verbose
```

`-Action Install` (или `NotApproved`, `Decline`), `-TargetGroupName` — имя группы ровно как в консоли. Дедлайн через PowerShell не ставится — только в GUI (правый клик по одобрению → Deadline).

**8. Кто вообще пришёл на сервер и когда отчитывался.**

```powershell
Get-WsusComputer | Select-Object Name, IPAddress, LastReportedStatusTime, LastSyncTime |
    Sort-Object LastReportedStatusTime
```

Самая полезная команда поиска «потеряшек»: если `LastReportedStatusTime` двухнедельной давности — машина не обновляется, разбираемся.

**9. Обслуживание базы (обязательная рутина раз в месяц).**

```powershell
Invoke-WsusServerCleanup -UpdateServer $wsus -CleanupObsoleteUpdates `
    -CleanupUnneededContentFiles -CompressUpdates -DeclineExpiredUpdates -DeclineSupersededUpdates
```

Без этого БД и папка контента пухнут, а консоль начинает думать по 30 секунд на каждый клик.

**10. Диагностика самого сервера.**

```cmd
"C:\Program Files\Update Services\Tools\WsusUtil.exe" checkhealth
```

Проверяет БД, каталог контента и ссылки на файлы; проблемы пишет в журнал Application. Ещё два инструмента: `wsusutil reset` (перекачать отсутствующие файлы контента) и `wsusutil movecontent D:\WSUS -logfile C:\move.log` (перенести контент на другой диск).

**11. Клиентская часть: убедиться, что GPO доехала.**

```powershell
gpupdate /force
gpresult /r /scope computer
reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v WUServer
```

В `gpresult` ищем GPO-WSUS в списке применённых; в реестре — адрес `http://FS01:8530`. Если ключа нет — GPO не применилась (не та OU, фильтр безопасности, WMI-фильтр).

**12. Запустить проверку и прочитать лог клиента.**

```powershell
UsoClient StartScan
Get-WinEvent -LogName 'Microsoft-Windows-WindowsUpdateClient/Operational' -MaxEvents 30 |
    Where-Object Id -in 19,20,21,26 | Format-Table TimeCreated, Id, Message -Wrap
Get-WindowsUpdateLog -LogPath C:\Temp\WU.log
```

`UsoClient` — недокументированная утилита (старый `wuauclt /detectnow` устарел). В журнале: 19 — установка успешна, 20 — ошибка установки, 21 — требуется перезагрузка, 26 — обнаружение прошло успешно `[ПРОВЕРИТЬ ID 26]`. `Get-WindowsUpdateLog` склеивает ETL-трейсы в читаемый текст.

**13. Что реально стоит на машине.**

```powershell
Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 10
```

Быстрая сверка «поставилось ли» без похода в консоль.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Готовый стенд L2: DC01 (AD DS, DNS, DHCP, 10.10.10.10), FS01 (10.10.10.20, файловый сервер), WIN11-CL01 и WIN10-CL02 в домене. Все машины в `corp.vektor.lab`, учётка `VEKTOR\adm.student`, пароль стенда.

WSUS ставим **на FS01**. Отдельной ВМ под WSUS в таблице стенда нет, а на DC01 ставить IIS + WID — плохая практика (контроллер домена не должен тащить лишние роли). В реальной работе под WSUS выделяют отдельный сервер и отдельный том под контент — на стенде мы это сознательно упрощаем и записываем в `stand-log.md` как долг.

### Снапшот

На **FS01, DC01, WIN11-CL01, WIN10-CL02** (машины выключены или службы в покое) — снапшот с именем `before-l2-014`.

### Шаги

1. **FS01.** Входим как `VEKTOR\adm.student`, `Install-WindowsFeature -Name UpdateServices -IncludeManagementTools`. Ожидаемо: Success, требуется перезагрузка — перезагружаем.
2. **FS01.** Запускаем `wsusutil.exe postinstall CONTENT_DIR=C:\WSUS`. Ожидаемо: «Post install has successfully completed» `[ПРОВЕРИТЬ формулировку]`, появились БД и папка `C:\WSUS\WsusContent`.
3. **FS01.** Открываем консоль: Server Manager → Tools → Windows Server Update Services. Мастер первичной настройки: источник — *Synchronize from Microsoft Update*, прокси нет, язык — English + Russian, продукты — Windows 11, Windows 10, Windows Server 2022, классификации — Critical, Security, Definition, Update Rollups. Ожидаемо: консоль открылась, в узле Updates пусто (синхронизации ещё не было).
4. **FS01.** Options → Update Files and Languages → ставим **«Download update files to this server only when updates are approved»** (иначе сервер закачает всё подряд), «Download express installation files» — **выключено**. Затем ручная синхронизация: `(Get-WsusServer).GetSubscription().StartSynchronization()`. Ожидаемо: через 10–40 минут в узле Updates появились сотни записей, статус Synchronization — Succeeded.
5. **FS01.** В консоли Computers → All Computers создаём группы `Pilot`, `Workstations`, `Servers`. Ожидаемо: три группы, клиентов пока нет.
6. **DC01.** Создаём две GPO через PowerShell (или GPMC — Computer Configuration → Policies → Administrative Templates → Windows Components → Windows Update):

```powershell
Import-Module GroupPolicy
$wu = 'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
$au = "$wu\AU"
New-GPO -Name 'GPO-WSUS-Workstations' | Out-Null
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $wu -ValueName WUServer -Type String -Value 'http://FS01:8530'
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $wu -ValueName WUStatusServer -Type String -Value 'http://FS01:8530'
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $wu -ValueName DoNotConnectToWindowsUpdateInternetLocations -Type DWord -Value 1
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $au -ValueName UseWUServer -Type DWord -Value 1
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $au -ValueName AUOptions -Type DWord -Value 4
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $au -ValueName ScheduledInstallDay -Type DWord -Value 0
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $au -ValueName ScheduledInstallTime -Type DWord -Value 3
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $au -ValueName TargetGroupEnabled -Type DWord -Value 1
Set-GPRegistryValue 'GPO-WSUS-Workstations' -Key $au -ValueName TargetGroup -Type String -Value 'Workstations'
```

`ScheduledInstallDay 0` = каждый день, `ScheduledInstallTime 3` = 03:00. Проверьте DN OU: `Get-ADOrganizationalUnit -Filter * | Select DistinguishedName`, затем `New-GPLink -Name 'GPO-WSUS-Workstations' -Target 'OU=Компьютеры,DC=corp,DC=vektor,DC=lab'`. Для серверов делаем копию GPO с `AUOptions 3` (скачать и уведомить) и `TargetGroup 'Servers'`, линкуем на OU серверов, FS01 из фильтрации не исключаем — он будет своим же клиентом.

7. **WIN11-CL01.** `gpupdate /force`, затем `reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" /v WUServer` → `http://FS01:8530`. `UsoClient StartScan`. Ожидаемо: через 1–5 минут в консоли WSUS в группе Pilot/Workstations появляется WIN11-CL01 с временем отчёта «сейчас».
8. **WIN10-CL02.** Перетаскиваем WIN10-CL02 в группу `Pilot` (или меняем TargetGroup), на FS01 одобряем одно накопительное обновление **только для Pilot** и ставим дедлайн на ближайший час. Ожидаемо: машина ставит обновление и перезагружается без участия пользователя.
9. **FS01.** Смотрим результат: Reports → Computer Status / Update Status Summary, плюс `Get-WsusUpdate -Classification Security -Status FailedOrNeeded`. Ожидаемо: видно, кто Needed, кто Installed, кто Failed.
10. **FS01.** Только после успешного пилота одобряем то же обновление для `Workstations`. Ночью по расписанию GPO машины ставят патч сами.
11. **FS01.** `Invoke-WsusServerCleanup ...` (команда из раздела 4) и `wsusutil checkhealth`. Ожидаемо: без ошибок, журнал Application чистый.

### Проверка

- [ ] На FS01 установлена роль UpdateServices, консоль открывается без ошибок
- [ ] Синхронизация с Microsoft Update прошла успешно, продукты и классификации выбраны
- [ ] В WSUS есть группы Pilot, Workstations, Servers
- [ ] `reg query` на WIN11-CL01 и WIN10-CL02 показывает WUServer = `http://FS01:8530`
- [ ] Обе клиентские машины видны в консоли в нужной группе, Last Reported Status — свежий
- [ ] Обновление установлено на пилотной машине и подтверждается `Get-HotFix`
- [ ] Отчёт по статусу обновлений строится и содержит данные
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

**Заявка № 2141 (продолжение).** Пользователь: Мария Петрова. ВМ: WIN10-CL02. Что написала: «Сегодня ночью обновление опять не поставилось. В настройках написано “Не удалось проверить обновления, код 0x8024401C”.» Срочность: высокая.

**Как воспроизвести поломку.** На DC01 в GPO-WSUS-Workstations меняем `WUServer` на `http://FS01:8531` (порт, на котором у нас нет HTTPS). На WIN10-CL02: `gpupdate /force`, затем `UsoClient StartScan`.

**Диагностика по шагам (не перескакивать):**
1. `gpresult /r /scope computer` — применилась ли GPO, нет ли конфликта с другой политикой.
2. `reg query ...\WindowsUpdate /v WUServer` — какой адрес реально в реестре. Видим 8531 → уже подозрительно.
3. `Test-NetConnection FS01 -Port 8531` → `TcpTestSucceeded: False`, а `-Port 8530` → True. Причина найдена на 80%.
4. `Invoke-WebRequest http://FS01:8530/ClientWebService/client.asmx -UseBasicParsing` → 200 OK (сервис жив, дело не в IIS).
5. На FS01: `Get-Item IIS:\AppPools\WsusPool | Select-Object state` → Started; `wsusutil checkhealth` → без ошибок. Сервер здоров.
6. `Get-WinEvent -LogName 'Microsoft-Windows-WindowsUpdateClient/Operational' -MaxEvents 20` — событие 20 с кодом 0x8024401C (таймаут HTTP-запроса к серверу обновлений) `[ПРОВЕРИТЬ расшифровку кода]`.

**Решение.** На DC01 правим `WUServer`/`WUStatusServer` обратно на `http://FS01:8530`. На WIN10-CL02: `gpupdate /force`, `UsoClient StartScan`, ждём событие 26 и проверяем `Get-HotFix`. Машина отчитывается в консоли.

**Ответ пользователю:** «Мария, добрый день. Причина — ошибка в политике: компьютер обращался к серверу обновлений по закрытому порту. Исправил настройки, обновления снова проверяются. Перезагрузки остались в 03:00, во время работы компьютер перезагружаться не будет. Если увидите значок “требуется перезагрузка” днём — можно спокойно отложить до вечера.» **Закрытие заявки:** категория «Windows Update / конфигурация», в решении — ссылка на GPO и скриншот отчёта WSUS с зелёным статусом.

### Что изменилось на стенде

- FS01: установлена роль WSUS (IIS-сайт WSUS Administration, WID, контент в `C:\WSUS`), порты 8530/8531.
- WSUS-группы компьютеров: `Pilot`, `Workstations`, `Servers`.
- DC01: GPO `GPO-WSUS-Workstations` (линк на OU компьютеров) и `GPO-WSUS-Servers` (линк на OU серверов), client-side targeting.
- WIN11-CL01, WIN10-CL02, DC01, FS01 — теперь клиенты WSUS `http://FS01:8530`, расписание установки 03:00.
- Одобрены security-обновления для Windows 10/11; пилотная машина обновлена.
- Долг стенда: WSUS на файловом сервере, контент на системном диске.

### Откат

Выключаем все четыре ВМ → VMware: Snapshot Manager → `before-l2-014` → Revert на каждой. Порядок: сначала FS01 и клиенты, DC01 последним. Важно: откат снапшота контроллера домена — это потенциальный USN rollback; в нашем стенде DC единственный и откатывается вместе со всеми клиентами, поэтому проблемы нет, но в боевой среде так не делают (там — authoritative restore или пересоздание DC).

## 6. Ошибки новичков и безопасность

1. **Включить «Download express installation files»** → контент раздувается в разы, диск забивается за месяц → держим выключенным, если нет причин.
2. **Ставить WSUS на контроллер домена или на системный диск** → IIS и БД на DC = лишняя поверхность атаки, а контент на C: рано или поздно даёт событие о недоступности каталога контента и остановку синхронизации → отдельный сервер и отдельный том.
3. **Одобрить всё и сразу на все группы** → утром 50 человек с перезагрузкой и потерянными документами, а на сервере с 1С — сломанный клиент → пилот → группа → дедлайн вне рабочих часов.
4. **Оставить «Download files only when approved» выключенным** → сервер качает обновления для продуктов, которых у вас нет.
5. **Путать серверные группы и client-side targeting** → все компьютеры в «Unassigned Computers», одобрения не работают → выбираем один способ (в нашем случае GPO) и не перетаскиваем машины руками.
6. **Оставить dual scan** → клиент ходит и в WSUS, и в интернет на Microsoft Update, обновления приходят в обход одобрений → политика запрета интернет-локаций + проверка через `gpresult`.
7. **Не делать cleanup** → база и контент растут, консоль тормозит, отчёты строятся минутами.
8. **Удалять файлы из `WsusContent` руками** → «битые» обновления и ошибки скачивания у клиентов → только `wsusutil reset`.
9. **Забыть про жизненный цикл ОС** → в 2026 году Windows 10 (не LTSC) вне поддержки с 14.10.2025; на стенде стоит LTSC-редакция, у неё свой срок `[ПРОВЕРИТЬ]` — на боевых машинах без ESU обновлений просто не будет, и WSUS тут не поможет.

**Безопасность.** WSUS по HTTP (8530) не шифрует трафик: сами файлы обновлений подписаны Microsoft, но метаданные и отчёты клиентов идут открытым текстом — в боевой среде включают HTTPS (`wsusutil configuressl <FQDN>`, порт 8531) или, как минимум, держат WSUS в отдельном сегменте. WSUS-администратор = человек, который может раскатать любое обновление на весь парк: не давайте эту роль всем подряд, не оставляйте консоль на сервере с общим локальным админом. Не отключайте файрвол «чтобы заработало» — открывайте только 8530/8531 и только с клиентских подсетей. И не храните снапшоты WSUS как способ «откатить патч» на боевом сервере.

## 7. Вопросы на собеседовании

**1. «Пользователь пишет, что обновления не приходят. Как найдёшь причину?»**
Ответ: идём по цепочке сверху вниз. GPO: `gpresult /r`, `reg query` — есть ли WUServer и правильный ли адрес. Сеть: `Test-NetConnection FS01 -Port 8530`. Сервер: консоль WSUS, статус синхронизации, `Get-Item IIS:\AppPools\WsusPool`, `wsusutil checkhealth`. Клиент: журнал `Microsoft-Windows-WindowsUpdateClient/Operational`, код ошибки, `Get-WindowsUpdateLog`. В 90% случаев это GPO не на той OU, неверный порт/адрес, «залипший» клиент (`net stop wuauserv`, сброс SoftwareDistribution) или дуал-скан.
*Что хочет услышать интервьюер:* методичную диагностику по слоям, а не «переустановлю Windows». *Красный флаг:* сразу «сброшу SoftwareDistribution» без единой проверки.

**2. «Как организуешь обновления для 300 рабочих мест и 50 серверов?»**
Ответ: один WSUS-сервер (или иерархия: нижестоящий + вышестоящий) с отдельным томом под контент. Группы: Pilot (5–10 машин), Workstations, Servers по подгруппам (DC, 1С, файловые). GPO с client-side targeting, установка ночью, дедлайн на security-обновления, пилот минимум 3–7 дней, автоодобрение только для Critical/Security рабочих станций, серверы — всегда вручную. Отчёт раз в месяц: сколько машин не отчиталось, сколько с ошибками.
*Интервьюер:* понимание, что обновление — это управляемый процесс с пилотом и окнами. *Красный флаг:* «включу автообновление у всех».

**3. «Чем дедлайн отличается от расписания в GPO и что такое supersedence?»**
Ответ: расписание GPO — когда клиент ставит по умолчанию; дедлайн, заданный при одобрении, главнее и заставляет установить/перезагрузиться к конкретному времени. Supersedence — новый патч замещает старый; замещённый помечается superseded и отклоняется при cleanup, иначе путаница в отчётах и лишние установки.
*Интервьюер:* практическое владение консолью. *Красный флаг:* «это одно и то же».

**4. «Как докажешь, что конкретный сервер обновлён, и как откатишь патч?»**
Ответ: `Get-HotFix`, событие 19 в журнале WindowsUpdateClient, отчёт Computer Status в WSUS. Откат — через `wusa /uninstall /kb:XXXXXXX` или Параметры → Центр обновления → История обновлений → Удалить; для серверов это простой, поэтому сначала оценка риска и снапшот/бэкап. 
*Интервьюер:* «умеет не только ставить, но и отвечать за результат». *Красный флаг:* «откатить нельзя, только переустановить».

**5. «WSUS объявлен устаревшим. Что дальше и что делать нам?»**
Ответ: Microsoft перестала развивать WSUS, ставка на облачные сервисы управления обновлениями (Intune/Autopatch, Azure Update Manager). Но WSUS ещё поддерживается, и в локальной инфраструктуре (тем более в РФ, где облачные сервисы Microsoft недоступны) он остаётся рабочим вариантом; параллельно смотрят в сторону локальных систем patch-менеджмента (например, модули управления уязвимостями в Kaspersky Security Center `[ПРОВЕРИТЬ набор функций]`), а для Astra Linux/РЕД ОС — обновления через собственные репозитории и локальные зеркала, не через WSUS.
*Интервьюер:* информированность о трендах + прагматизм. *Красный флаг:* «WSUS мёртв, надо всё срочно переделать» без понимания, чем именно заменять в конкретной среде.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Server Update Services (WSUS)»: Deploy WSUS, Approve updates, WSUS computer groups, WSUS reports. Поиск: `WSUS deployment guide site:learn.microsoft.com`.
- **learn.microsoft.com** — «Windows Update client policies» / «Configure Automatic Updates» (описание AUOptions 2–7 и реестровых ключей WindowsUpdate\AU). Поиск: `Configure Automatic Updates policy registry AUOptions`.
- **learn.microsoft.com** — справка по модулю PowerShell `UpdateServices` (Get-WsusServer, Approve-WsusUpdate, Invoke-WsusServerCleanup). Поиск: `Get-WsusServer UpdateServices module`.
- **learn.microsoft.com** — заметка о статусе WSUS (deprecation, сентябрь 2024). Поиск: `WSUS deprecated feature Windows Server`.
- **learn.microsoft.com** — Windows 10 / Windows 11 lifecycle (даты окончания поддержки, LTSC-редакции, ESU). Поиск: `Windows 10 Enterprise LTSC 2021 end of servicing`.
- **ubuntu.com / astralinux.ru** — обновление Linux-хостов через собственные репозитории и локальные зеркала (для контраста с WSUS). Поиск: `Ubuntu unattended-upgrades documentation`.
