# Скриптинг на PowerShell: автоматизация массовых операций с пользователями AD

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 9:40. В Service Desk падает заявка от Петровой (HR, WIN10-CL02):

> «Привет! В понедельник выходят 12 новых грузчиков на склад, нужны учётки и доступ в GG-Sklad. Ещё у трёх бухгалтеров сменился кабинет — поправьте, пожалуйста, в AD. И пришлите мне список всех, у кого есть доступ к \\FS01\Buh, директор просил проверить.»

Если делать это руками в «Active Directory Users and Computers», вы потратите час на клики, ошибётесь минимум дважды и всё равно забудете кого-нибудь. Если уметь PowerShell — это 15 минут: один CSV, один цикл, один отчёт.

Массовые операции с учётками — самая частая «скучная» работа системного администратора и одновременно самое частое место, где ломают прод: добавили весь отдел в группу с зарплатной папкой, сбросили пароли не тем людям, отключили не того Сидорова. Поэтому L1-специалисту нужно не «знать PowerShell вообще», а уметь три вещи: читать данные из AD пачками, безопасно менять их пачками (с `-WhatIf` и логом) и оформлять результат в отчёт, который можно отдать человеку.

После этой главы ты будешь уметь: ставить модуль RSAT, выгружать список пользователей из домена `corp.vektor.lab` в CSV, массово править атрибуты из файла, управлять членством в группах, сбрасывать пароль и отключать учётку одной командой, вести журнал выполненного скрипта и — главное — откатывать свои же изменения.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

PowerShell — это не «командная строка с другим синтаксисом», а среда, где всё работает с **объектами**. `Get-ADUser` возвращает не строку текста, а объект с десятками свойств (`SamAccountName`, `Enabled`, `DistinguishedName`, `LastLogonDate`…). Дальше эти объекты можно фильтровать, сортировать, передавать дальше по конвейеру и превращать в таблицу или CSV.

Модуль `ActiveDirectory` — это набор командлетов (`Get-ADUser`, `Set-ADUser`, `New-ADUser`, `Add-ADGroupMember`…). Он не «лезет в базу напрямую». На клиентской машине (WIN11-CL01) командлеты общаются со службой **AD Web Services** на контроллере домена по TCP-порту **9389**. На самом DC01 тот же модуль работает локально.

```text
   WIN11-CL01 (адрес по DHCP от DC01)
   ┌──────────────────────────────────────────┐
   │ PowerShell 5.1                           │
   │   └─ модуль ActiveDirectory (RSAT)       │
   │        └─ клиент ADWS                    │
   └──────────────┬───────────────────────────┘
                  │ TCP 9389 (AD Web Services)
                  ▼
   DC01 (10.10.10.10)
   ┌──────────────────────────────────────────┐
   │ AD Web Services                          │
   │   └─ AD DS ── база NTDS.dit              │
   │        ▲                                 │
   │        │ LDAP 389 / LDAPS 636,           │
   │        │ Kerberos 88, DNS 53             │
   └──────────────────────────────────────────┘
```

Порты, которые полезно помнить на L1: **389/636** — LDAP/LDAPS (сам протокол каталога), **88** — Kerberos (аутентификация), **53** — DNS (без него домен вообще не находится), **9389** — ADWS (по нему работает PowerShell-модуль), **5985/5986** — WinRM (удалённый PowerShell), **445** — SMB (это уже про доступ к `\\FS01\Buh`, не про AD). По модели OSI всё это прикладной уровень, L7; ниже — TCP/UDP, IP, Ethernet, и в VMware Workstation — виртуальный коммутатор VMnet8.

Как выглядит конвейер:

```text
Get-ADUser  ──►  [obj] [obj] [obj] [obj]
                    │
              Where-Object          фильтрация НА КЛИЕНТЕ (медленно, если объектов много)
                    │
             ForEach-Object { Set-ADUser ... }   один вызов на каждый объект
                    │
              Export-Csv            отчёт для человека
```

Ключевая мысль для собеседования и для жизни: **фильтровать надо на сервере** (`-Filter`, `-LDAPFilter`, `-SearchBase`), а не выкачивать весь домен и фильтровать на клиенте через `Where-Object`. В домене на 50 человек разница незаметна, в домене на 50 000 — это разница между «секунда» и «положил DC».

Ещё одна деталь: атрибуты AD делятся на «базовые» (их `Get-ADUser` отдаёт сразу) и остальные — их надо явно запросить через `-Properties`. Забыл `-Properties Office` — получил в отчёте пустую колонку и полчаса недоумения.

## 3. Аналогия из жизни

Представь склад компании «Вектор». Кладовщик (PowerShell) не носит коробки в руках по одной и не переписывает их содержимое в блокнот. Он работает с **накладными**: каждая коробка — это объект, у которого есть артикул, вес, полка, дата поступления. Конвейер (`|`) — это транспортёр: контролёр (`Where-Object`) снимает с ленты коробки, которые нам не нужны; упаковщик (`ForEach-Object`) открывает каждую оставшуюся и кладёт внутрь наклейку; а `Export-Csv` — это опись, которую отдают директору.

Аналогия перестаёт работать в одном месте: коробку на конвейере нельзя «разобрать на детали» — она едет целиком, со всеми своими свойствами и типом, и именно поэтому после `Get-ADUser` ты можешь обратиться к `$_.LastLogonDate`, даже если в таблице на экране этой колонки не было.

## 4. Практика и команды

Все команды — на WIN11-CL01 (или прямо на DC01, если RSAT ставить не хочется), под учёткой `adm.student`, домен `corp.vektor.lab`.

**1. Проверить наличие модуля и (если нужно) установить RSAT**

```powershell
Get-Module -ListAvailable ActiveDirectory
Get-WindowsCapability -Online -Name Rsat*
```

`Get-Module -ListAvailable` ищет модули на диске, не загружая. Если пусто — ставим RSAT (Remote Server Administration Tools). GUI-путь: **Параметры → Система → Дополнительные компоненты → Добавить компонент → «Средства служб домена Active Directory и облегчённых служб каталогов (LDS)»**. Или командой:

```powershell
Add-WindowsCapability -Online -Name "Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0"
```

Точное имя пакета лучше взять из вывода `Get-WindowsCapability -Online -Name Rsat*` [ПРОВЕРИТЬ имя для твоей сборки]. В реальной компании RSAT часто уже стоит в образе, а права на создание учёток у L1, как правило, **делегированы** только на конкретное OU — не требуй Domain Admin.

**2. Подключить модуль и понять, с каким DC работаем**

```powershell
Import-Module ActiveDirectory
Get-ADDomainController -Discover | Select-Object Name, IPv4Address, Site
```

Ожидаемый вывод: `DC01`, `10.10.10.10`. Смысл: убедиться, что клиент видит DC и DNS настроен (иначе получишь ошибку «Unable to contact the server»).

**3. Получить список пользователей OU Company**

```powershell
Get-ADUser -Filter * -SearchBase "OU=Company,DC=corp,DC=vektor,DC=lab" `
  -Properties Office, LastLogonDate |
  Select-Object SamAccountName, Name, Enabled, Office, LastLogonDate |
  Format-Table -AutoSize
```

Ключи: `-Filter *` — серверный фильтр «все»; `-SearchBase` — ограничить поиск веткой дерева; `-Properties` — дотянуть необязательные атрибуты; `Select-Object` — оставить нужные колонки; `Format-Table` — только для глаз, в файл это писать нельзя.

```text
SamAccountName Name           Enabled Office            LastLogonDate
-------------- ----           ------- ------            -------------
ivanov         Иван Иванов      True  Москва, каб. 214  12.02.2026 9:14:03
petrova        Мария Петрова    True  Москва, каб. 118  12.02.2026 9:02:41
sidorov        Алексей Сидоров  True  Москва, склад     11.02.2026 18:20:10
```

**4. Посмотреть всё про одного пользователя**

```powershell
Get-ADUser -Identity ivanov -Properties * |
  Format-List Name, SamAccountName, DistinguishedName, Enabled,
              Description, Office, PasswordLastSet, LastLogonDate
```

`-Identity` понимает sAMAccountName, DN и SID. `Format-List` — когда колонок много. Смотри на `DistinguishedName` (где лежит учётка) и `PasswordLastSet`.

**5. Членство в группах**

```powershell
Get-ADPrincipalGroupMembership -Identity petrova | Select-Object Name, GroupScope
Get-ADGroupMember -Identity GG-Buh | Select-Object Name, SamAccountName
```

Первая команда отвечает «в каких группах состоит человек», вторая — «кто состоит в группе». На L1 это самая частая диагностика прав доступа.

**6. Информация по команде / поиск команды**

```powershell
Get-Help Set-ADUser -Examples
Get-Command *ADUser* -Module ActiveDirectory
```

`Get-Help -Examples` — быстрее, чем гуглить. `Get-Command` — когда забыл, как называется командлет.

**7. Массовое изменение атрибутов из CSV (сначала сухой прогон)**

Файл `C:\Lab\users.csv` (сохранить в UTF-8):

```csv
SamAccountName,Office,Description
ivanov,"Москва, каб. 214","Бухгалтерия, ВМ WIN11-CL01"
petrova,"Москва, каб. 118","HR, ВМ WIN10-CL02"
sidorov,"Москва, склад","Склад"
```

```powershell
$users = Import-Csv -Path C:\Lab\users.csv -Encoding UTF8
foreach ($u in $users) {
    Set-ADUser -Identity $u.SamAccountName -Office $u.Office -Description $u.Description -WhatIf
}
```

`-WhatIf` — «покажи, что сделал бы, но не делай». Все командлеты AD его поддерживают. Это твой ремень безопасности. Если Excel сохранил файл через `;`, добавь `-Delimiter ';'`.

**8. То же, но по-настоящему и с обработкой ошибок**

```powershell
$users = Import-Csv -Path C:\Lab\users.csv -Encoding UTF8
foreach ($u in $users) {
    try {
        Set-ADUser -Identity $u.SamAccountName -Office $u.Office -Description $u.Description -ErrorAction Stop
        Write-Host "OK   $($u.SamAccountName)"
    } catch {
        Write-Warning "FAIL $($u.SamAccountName): $($_.Exception.Message)"
    }
}
```

`try/catch` + `-ErrorAction Stop` нужен, чтобы одна кривая строка не убивала весь цикл: скрипт идёт дальше и в конце ты видишь список провалов.

**9. Сброс пароля**

```powershell
$pwd = ConvertTo-SecureString 'Lab#2026-vektor' -AsPlainText -Force
Set-ADAccountPassword -Identity sidorov -Reset -NewPassword $pwd
```

`ConvertTo-SecureString -AsPlainText -Force` превращает строку в защищённый объект. В бою пароль не пишут в скрипт — его вводят через `Read-Host -AsSecureString` или берут из хранилища секретов.

**10. Отключить / включить / разблокировать**

```powershell
Disable-ADAccount -Identity sidorov
Search-ADAccount -AccountDisabled -UsersOnly | Select-Object Name, SamAccountName
Enable-ADAccount -Identity sidorov
Unlock-ADAccount -Identity ivanov
```

`Search-ADAccount` умеет ещё `-AccountExpired`, `-PasswordNeverExpires`, `-AccountInactive`. `Unlock-ADAccount` на незаблокированной учётке вернёт ошибку — это нормально.

**11. Выгрузить отчёт в CSV**

```powershell
Get-ADUser -Filter * -SearchBase "OU=Company,DC=corp,DC=vektor,DC=lab" -Properties Office, LastLogonDate |
  Select-Object SamAccountName, Name, Enabled, Office, LastLogonDate |
  Export-Csv -Path C:\Lab\ad-report-78.csv -NoTypeInformation -Encoding UTF8
```

`-NoTypeInformation` убирает мусорную первую строку `#TYPE`, `-Encoding UTF8` спасает кириллицу. В файл — только `Export-Csv`, никогда `>` или `Out-File`.

**12. Linux-сторона (UBU01): тот же вопрос через LDAP**

```bash
ldapsearch -x -H ldap://10.10.10.10 -D "ivanov@corp.vektor.lab" -W \
  -b "DC=corp,DC=vektor,DC=lab" "(sAMAccountName=sidorov)" description office
```

`-H` — сервер, `-D` — кем аутентифицируемся, `-W` — спросить пароль, `-b` — база поиска, дальше LDAP-фильтр и список атрибутов. Учти: на Windows Server 2022 с усиленными настройками простой bind без TLS может быть отклонён — тогда только LDAPS (636) или работа с Windows [ПРОВЕРИТЬ в вашем домене]. Модуль `ActiveDirectory` под Linux/pwsh не работает.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **DC01** (10.10.10.10) — Windows Server 2022, поднят AD DS, домен `corp.vektor.lab`, есть OU `Company` с подразделениями `Бухгалтерия`, `HR`, `Склад`, `IT`, группы `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT`, пользователи `ivanov`, `petrova`, `sidorov`.
- **WIN11-CL01** — Windows 11, введён в домен, вход под `VEKTOR\adm.student` (администратор домена — только потому, что это учебный стенд).
- **FS01** (10.10.10.20) — файловый сервер, папки `\\FS01\Obshchee`, `\\FS01\Buh`, `\\FS01\HR` (нужен только на шаге «Сломай и почини»).
- Если AD ещё не развёрнут — сначала пройди модуль по Active Directory, без домена эта лабораторная невозможна.

### Снапшот

В VMware Workstation: выделить ВМ → **VM → Snapshot → Take Snapshot…** → имя `before-l1-078`. Сделать на **DC01**, **WIN11-CL01** и **FS01** (FS01 — потому что проверяем доступ к папке).

### Шаги

1. **WIN11-CL01.** Проверить модуль: `Get-Module -ListAvailable ActiveDirectory`. Ожидаемо: модуль есть (или ставим RSAT и перезапускаем PowerShell).
2. **WIN11-CL01.** Открыть PowerShell **от имени администратора**, выполнить `Import-Module ActiveDirectory` и `Get-ADDomainController -Discover`. Ожидаемо: `DC01`, `10.10.10.10`. Если ошибка — проверить DNS (`ipconfig /all`: DNS-сервер должен быть 10.10.10.10).
3. **WIN11-CL01.** Выполнить команду из п.4.3. Ожидаемо: таблица из трёх пользователей.
4. **WIN11-CL01.** Посмотреть детали: `Get-ADUser -Identity ivanov -Properties *`. Найти `DistinguishedName` — он должен указывать в `OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab`.
5. **WIN11-CL01.** Создать папку `C:\Lab` и файл `C:\Lab\users.csv` из п.4.7 (UTF-8). Проверить: `Import-Csv C:\Lab\users.csv | Format-Table`. Ожидаемо: три строки, русские буквы читаемы. Если видишь `Ð˜Ð²Ð°Ð½Ð¾Ð²` — проблема с кодировкой, пересохрани файл в UTF-8.
6. **WIN11-CL01.** Сухой прогон из п.4.7 с `-WhatIf`. Ожидаемо: три строки вида `What if: Performing the operation "Set" on target "CN=Иван Иванов,..."` и **ни одного реального изменения**.
7. **WIN11-CL01.** Запустить `Start-Transcript -Path C:\Lab\transcript-78.txt -Append`. Затем выполнить цикл из п.4.8 без `-WhatIf`. Ожидаемо: `OK ivanov`, `OK petrova`, `OK sidorov`.
8. **WIN11-CL01.** Проверить результат: `Get-ADUser -Filter * -SearchBase "OU=Company,DC=corp,DC=vektor,DC=lab" -Properties Office | Select Name, Office, Description | Format-Table`. Ожидаемо: у всех трёх заполнены Office и Description.
9. **WIN11-CL01.** Проверить членство: `Get-ADPrincipalGroupMembership -Identity petrova | Select Name`. Ожидаемо: среди групп есть `GG-HR`, `Domain Users`. Записать вывод.
10. **WIN11-CL01.** Сбросить пароль `sidorov` (п.4.9) — пароль тот же `Lab#2026-vektor`, поэтому вход не изменится. Затем `Disable-ADAccount -Identity sidorov` → `Search-ADAccount -AccountDisabled -UsersOnly` (sidorov в списке) → `Enable-ADAccount -Identity sidorov`. Ожидаемо: цикл отключения/включения отработал, учётка снова активна.
11. **WIN11-CL01.** Выгрузить отчёт `C:\Lab\ad-report-78.csv` (п.4.11), открыть в блокноте и в Excel. Ожидаемо: колонки через запятую, кириллица корректна.
12. **WIN11-CL01.** `Stop-Transcript`. Открыть `C:\Lab\transcript-78.txt`, убедиться, что все команды и их вывод записаны. Дописать изменения в `stand-log.md`.

### Проверка

- [ ] `Get-ADDomainController -Discover` возвращает `DC01` / `10.10.10.10`
- [ ] `Get-Module -ListAvailable ActiveDirectory` показывает модуль
- [ ] В `C:\Lab\ad-report-78.csv` три пользователя, кириллица не «кракозябрами»
- [ ] У `ivanov`, `petrova`, `sidorov` заполнены Office и Description
- [ ] `sidorov` активен (`Enabled = True`) после цикла disable/enable
- [ ] `petrova` по-прежнему только в `GG-HR` (не в `GG-Buh`!)
- [ ] `C:\Lab\transcript-78.txt` содержит все выполненные команды
- [ ] В `stand-log.md` появилась запись об уроке 78

### Сломай и почини

> **Пользователь:** Мария Петрова
> **ВМ:** WIN10-CL02
> **Что написала:** «Открываю `\\FS01\Buh` — вижу зарплатную ведомость за февраль, хотя я из HR и раньше папка не открывалась. Это нормально?»
> **Срочность:** средняя (доступ к чужим данным — уже инцидент, но не простой)

**Как воспроизвести.** В `C:\Lab\groups.csv` опечатка: в строке для `petrova` в колонке Group стоит `GG-Buh` вместо `GG-HR` (классическая ошибка copy-paste). Выполняем:

```powershell
Add-ADGroupMember -Identity GG-Buh -Members petrova
```

Теперь у Петровой есть доступ к `\\FS01\Buh` (папка выдана группе `GG-Buh`), и на WIN10-CL02 она действительно увидит содержимое.

**Диагностика по шагам.**

1. **DC01 / WIN11-CL01:** `Get-ADPrincipalGroupMembership -Identity petrova | Select Name` → видим `GG-Buh`. Гипотеза подтверждается.
2. Проверяем, когда и кем добавлено: `Get-ADGroupMember -Identity GG-Buh | Select Name, SamAccountName` и журнал безопасности DC01 (**Event Viewer → Windows Logs → Security**, событие 4728 «Участник добавлен в глобальную группу») [ПРОВЕРИТЬ номер события в вашей локали].
3. Смотрим свой транскрипт `C:\Lab\transcript-78.txt` — там будет строка с `Add-ADGroupMember … GG-Buh`. Нашли источник: наш скрипт + кривой CSV.
4. Проверяем, не затронуты ли ещё люди: `Get-ADGroupMember -Identity GG-Buh | Select Name` — в группе должны быть только бухгалтеры.
5. Убеждаемся, что дело именно в группе, а не в прямых правах NTFS на FS01: на FS01 в свойствах `Buh` → **Security** → в списке только `GG-Buh`, прямых записей для `petrova` нет.

**Решение:**

```powershell
Remove-ADGroupMember -Identity GG-Buh -Members petrova -Confirm:$false
Get-ADPrincipalGroupMembership -Identity petrova | Select Name   # проверка
```

Затем на WIN10-CL02: `klist purge` и **выход/вход** пользователя (или перезагрузка). Без этого старый Kerberos-билет с правами `GG-Buh` живёт до истечения, и папка может открываться ещё какое-то время.

**Ответ пользователю:** «Мария, спасибо, что заметили. Это была ошибка в массовом изменении прав с нашей стороны, доступ уже отключён. Пожалуйста, выйдите и войдите в систему заново (или перезагрузите компьютер), после этого `\\FS01\Buh` открываться не должна. Если откроется — напишите, разберёмся дальше. Отдельно сообщу руководителю ИБ.» Не сваливай вину на «глюк системы» — честно скажи, что это была ошибка администрирования.

**Закрытие заявки:** категория «Права доступа», приоритет средний, в решении — что сделано, что проверено, кто уведомлён. Отдельным тикетом — задача «исправить groups.csv и добавить проверку `-WhatIf` перед запуском». Инцидент с доступом к персональным данным (зарплаты) стоит зафиксировать в журнале инцидентов ИБ, даже если это учебный стенд.

### Что изменилось на стенде

- DC01: у `ivanov`, `petrova`, `sidorov` заполнены атрибуты `Office` и `Description`.
- DC01: у `sidorov` выполнен сброс пароля (пароль остался `Lab#2026-vektor`), проведён цикл disable/enable — учётка активна.
- DC01: `petrova` временно добавлялась в `GG-Buh` и была удалена; итоговое членство — `GG-HR`.
- WIN11-CL01: установлен модуль RSAT ActiveDirectory (если не был установлен ранее).
- WIN11-CL01: созданы `C:\Lab\users.csv`, `C:\Lab\groups.csv`, `C:\Lab\ad-report-78.csv`, `C:\Lab\transcript-78.txt`.
- Новых учётных записей, групп и OU не создавалось.

### Откат

- Изменения в AD живут в базе `NTDS.dit` на **DC01**. Чтобы откатить всё сразу: выключить DC01 → **VM → Snapshot → Snapshot Manager** → выбрать `before-l1-078` → **Go To**.
- Важно: откат DC01 откатывает **весь** каталог. Если ты после снапшота успел ввести клиентскую ВМ в домен, доверие к компьютеру может сломаться — лечится `Test-ComputerSecureChannel -Repair -Credential (Get-Credential)` на клиенте [ПРОВЕРИТЬ синтаксис для твоей версии].
- Файлы `C:\Lab\*` на WIN11-CL01 откатом DC01 не удалятся — удали вручную или откати и клиентскую ВМ.
- Точечный откат без снапшота: `Remove-ADGroupMember`, `Set-ADUser -Clear Description,Office`, `Enable-ADAccount`.

## 6. Ошибки новичков и безопасность

1. **`Get-ADUser -Filter * | Where-Object {…}` на весь домен** → выкачивание всех объектов и нагрузка на DC и сеть → фильтруй на сервере: `-Filter`, `-LDAPFilter`, `-SearchBase`.
2. **Забыл `-Properties`** → в отчёте пустые колонки, «данных нет» → сначала `Get-ADUser -Identity X -Properties *`, посмотри, как атрибут называется, потом запрашивай его явно.
3. **Вывод в файл через `>` или `Out-File`** → получаешь текст с обрезанными колонками, который невозможно нормально открыть в Excel → только `Export-Csv -NoTypeInformation -Encoding UTF8`.
4. **Запуск массового изменения сразу, без `-WhatIf` и без бэкапа** → «а почему у Петровой доступ к зарплатам?» → всегда: экспорт текущего состояния в CSV → `-WhatIf` → прогон на одной учётке → полный запуск с транскриптом.
5. **`Set-ExecutionPolicy Unrestricted` на всю машину** → снимаешь защиту от чужих скриптов → правильнее `RemoteSigned` для сессии (`-Scope Process`) или `Unblock-File` для конкретного файла.
6. **Пароль в открытом виде в скрипте или в CSV на `\\FS01\Obshchee`** → утечка учётных данных → `Read-Host -AsSecureString`, хранилище секретов, а файлы со списками ФИО — это персональные данные, им не место в общей папке.
7. **Удаление учётки вместо отключения** → теряются права, почта, история, аудит; восстановление из бэкапа AD — боль → сначала `Disable-ADAccount`, потом регламент и срок хранения.
8. **«Я добавил в группу, а доступа нет — AD сломан»** → не обновился Kerberos-билет → `klist purge` + повторный вход, `gpresult /r`, и только потом копать NTFS-права.

### Безопасность

- На стенде `adm.student` — администратор домена, иначе лабораторную не сделать. В реальной компании L1 **не должен** иметь Domain Admin: права выдаются делегированием на конкретное OU.
- Никогда не отключай файрвол и не расширяй права «чтобы быстрее заработало» на боевой сети. На стенде тоже делай это осознанно и возвращай назад.
- Транскрипт и CSV могут содержать ФИО, логины, кабинеты — это персональные данные. Храни в личной папке, не в `\\FS01\Obshchee`, и не прикладывай к тикету без необходимости.
- Перед любым `Remove-*` проверь, что переменная не пустая: `if (-not $u.SamAccountName) { continue }`. Пустая переменная в `-Members $null` может дать неожиданный результат.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Чем `-Filter` отличается от `-LDAPFilter` и почему `Where-Object` после `Get-ADUser` — плохая идея?**

`-Filter` — серверная фильтрация в синтаксисе, похожем на PowerShell; `-LDAPFilter` — фильтрация на сервере «сырым» LDAP-выражением (мощнее для сложных условий, например `(&(objectClass=user)(!userAccountControl:1.2.840.113556.1.4.803:=2))`). `Where-Object` работает уже **на клиенте**: сначала выкачиваются все объекты, потом отбрасываются лишние — это медленно и грузит DC и сеть.
*Что хочет услышать интервьюер:* понимание «клиент vs сервер» и что ты не будешь `-Filter *` на 10 000 учёток.
*Красный флаг:* «разницы нет, главное чтобы работало».

**2. Как безопасно массово изменить 150 учётных записей?**

По шагам: получить список от заказчика и согласовать → выгрузить текущее состояние в CSV (это бэкап) → подготовить скрипт с `-WhatIf` и проверить, что он затронет ровно 150 нужных объектов → прогнать на 1–2 учётках → включить `Start-Transcript` и запустить в согласованное окно → сверить результат с исходным CSV → подготовить обратный скрипт.
*Что хочет услышать интервьюер:* дисциплина, а не героизм. `-WhatIf`, бэкап, лог, пилот, окно.
*Красный флаг:* «напишу цикл и запущу, потом посмотрим, что сломалось».

**3. Сотрудник уволен. Что делаешь с учётной записью?**

Не удаляю сразу. Отключаю (`Disable-ADAccount`), завершаю сеансы и чищу билеты, снимаю лишние группы, убираю из рассылок, перекладываю в отдельное OU для отключённых, настраиваю автоответ на почте, фиксирую в тикете. Удаление — только по регламенту и после срока хранения, чтобы сохранить аудит и не потерять историю доступов. Если уволенный — «злой», дополнительно проверяю, нет ли у него прав администратора или сохранённых сессий.
*Что хочет услышать интервьюер:* «сначала блокировка, не удаление» + понимание, что доступ — это не только учётка.
*Красный флаг:* «сразу удалю, чего тянуть».

**4. Добавил пользователя в группу, а доступ не появился. Почему?**

У пользователя уже есть Kerberos-билет, выданный при входе, с прежним списком групп. Нужен повторный вход (или `klist purge` + вход). Дальше проверяем: репликация, если DC несколько; `gpresult /r` — какие группы реально применились; права на шаре и NTFS-права на FS01; не мешает ли кэш SMB.
*Что хочет услышать интервьюер:* знание про токен и умение идти от «пользователь → группа → шара → NTFS».
*Красный флаг:* «права применяются мгновенно, проблема не у меня».

**5. Как сбросить пароль и как передать его пользователю?**

Сброс: `ConvertTo-SecureString` + `Set-ADAccountPassword -Reset` + `Set-ADUser -ChangePasswordAtLogon $true`, чтобы человек сразу сменил пароль на свой. Передача — по регламенту: лично или по защищённому каналу, никогда в общем чате и никогда в тексте тикета. Массовый сброс — только с согласованием и уведомлением пользователей заранее.
*Что хочет услышать интервьюер:* что ты не хранишь пароли в скрипте и не рассылаешь их в мессенджере.
*Красный флаг:* «пришлю пароль в чат, так быстрее».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Active Directory PowerShell»: справочник по командлетам `Get-ADUser`, `Set-ADUser`, `New-ADUser`, `Add-ADGroupMember`. Поиск: `Get-ADUser Set-ADUser cmdlet reference site:learn.microsoft.com`.
- **learn.microsoft.com** — статья «Active Directory Administration with Active Directory Web Services» (про ADWS и порт 9389). Поиск: `Active Directory Web Services overview port 9389`.
- **learn.microsoft.com** — «Remote Server Administration Tools (RSAT)» и раздел про `Get-WindowsCapability`. Поиск: `RSAT Windows 11 install WindowsCapability ActiveDirectory`.
- **learn.microsoft.com** — справка по языку: `about_Try_Catch_Finally`, `about_Execution_Policies`, `Export-Csv`, `Import-Csv`. Поиск: `about_Execution_Policies PowerShell`.
- **learn.microsoft.com** — «WhatIf and Confirm» (`about_CommonParameters`, `-WhatIf`). Поиск: `PowerShell WhatIf Confirm common parameters`.
- **Стенд:** `stand-log.md` и транскрипты в `C:\Lab\` — твоя собственная документация, которая на собеседовании будет ценнее любой ссылки [ПРОВЕРИТЬ актуальность версий модулей для вашей сборки Windows 11].
