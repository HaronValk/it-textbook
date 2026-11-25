# Аварийное восстановление AD: бэкап System State, авторитативное и неавторитативное восстановление, корзина AD

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4471.** Пользователь: Петрова (WIN10-CL02). Что написала: «Здравствуйте! Не открывается `\\FS01\HR` — пишет "Отказано в доступе". Вчера весь день работало, я ничего не меняла. И ещё пропала папка HR в адресной книге Outlook». Срочность: средняя.

Ты открываешь AD и выясняешь: группы `GG-HR` больше нет. Или хуже — нет всей OU `HR` с тремя учётками. Кто-то из «помощников» в пятницу вечером «навёл порядок». Знакомая ситуация: в AD одно неверное движение мышью в оснастке стоит дороже, чем упавший сервер, потому что удаление тут же реплицируется на все контроллеры домена и обратной кнопки «Отменить» в интерфейсе нет.

Уметь восстанавливать AD — это водораздел между «эникейщиком, который переустанавливает» и администратором. После этой главы ты будешь: делать бэкап System State и понимать, что в него входит; различать неавторитативное и авторитативное восстановление и знать, когда какое применять; работать в DSRM; включать и использовать корзину AD; понимать, при чём тут tombstone lifetime и почему бэкап месячной давности может «сломать» домен ещё сильнее.

## 2. Теория «под капотом»

**Что такое System State.** Это не «все файлы сервера», а набор критичных данных ОС и ролей. Для контроллера домена в него входят: база `NTDS.dit` (все объекты и хэши паролей), файлы журналов `edb*.log`, папка `SYSVOL` (скрипты и GPO), реестр, COM+ class registration database, загрузочные и системные файлы, база служб сертификатов (если стоит CA), данные кластера. Пользовательские данные (шары, профили) в System State **не входят** — это отдельная история про файловые бэкапы.

Бэкап делается через VSS (Volume Shadow Copy Service): том замораживается, и база копируется консистентной. Поэтому «просто скопировать `C:\Windows\NTDS\NTDS.dit`» нельзя — файл занят службой NTDS.

**Два режима восстановления.**
- **Неавторитативное (non-authoritative)** — восстановили базу из бэкапа и дали контроллеру домена догнать остальных партнёров по репликации. Всё, что изменилось после бэкапа, придёт от других DC. Данные, которых нет нигде, кроме бэкапа, — потеряются.
- **Авторитативное (authoritative)** — мы говорим: «эта версия объекта — главная, реплицируй её всем». Механизм: `ntdsutil` при авторитативном восстановлении поднимает номер версии атрибутов (`versionNumber`) у восстановленных объектов на очень большое значение, поэтому при репликации они побеждают. Авторитативное восстановление работает **только из DSRM**.

**DSRM** (Directory Services Restore Mode) — специальный режим загрузки DC: служба NTDS не стартует, база не смонтирована, домен не обслуживается, поэтому файлы можно безопасно перезаписать. Вход — локальная учётка `.\Administrator` с DSRM-паролем, который задавался при повышении сервера до DC. Пароль этот регулярно теряют — его можно сбросить через `ntdsutil`.

**Tombstone lifetime** — срок жизни «надгробия» удалённого объекта (по умолчанию 180 дней в современных версиях). После этого объект физически удаляется из базы и восстановить его можно только из бэкапа. Отсюда главное правило: **бэкап старше tombstone lifetime запрещён к восстановлению** — контроллер начнёт реплицировать «живые» объекты, которые другие DC уже похоронили, и в домене появятся lingering objects (объекты-призраки, которые есть на одном DC и отсутствуют на другом).

**Корзина AD (AD Recycle Bin)** — функция леса, появилась в функциональном уровне Windows Server 2008 R2. Когда она включена, удалённый объект сохраняет **все свои атрибуты** и восстанавливается одной командой в исходное расположение. Без неё объект превращается в «надгробие» с обрубленными атрибутами, и восстановить его можно только через авторитативное восстановление из бэкапа или редактором вроде ADRestore. Включение корзины **необратимо**.

```text
                 Удалили объект в AD
                          |
             +------------+-------------+
             |                          |
     Корзина AD включена        Корзина НЕ включена
             |                          |
   объект в CN=Deleted Objects   объект = tombstone
   с полными атрибутами          (обрубленные атрибуты)
             |                          |
   Restore-ADObject              1) ADRestore / ldp (если атрибутов хватает)
   (секунды, без ребута)         2) авторитативное восстановление из бэкапа
                                 3) если > tombstone lifetime — объекта нет

  Полная последовательность восстановления DC:
  DSRM -> wbadmin (неавторитативно) -> ребут (всё ещё DSRM)
       -> ntdsutil authoritative restore (если нужно) -> обычная загрузка
       -> repadmin /syncall, dcdiag
```

Сетевые порты, которые нас касаются: репликация и LDAP-запросы — 389/636 (LDAP), 3268/3269 (Global Catalog), 88 (Kerberos), 135 + динамические RPC (репликация, DFSR), 445 (SYSVOL по SMB). Сам `wbadmin` в сеть не ходит, если бэкап пишется на локальный диск.

## 3. Аналогия из жизни

Представь библиотеку с каталогом на карточках, и каталог этот ведут три библиотекаря одновременно, каждый в своей тетради, а потом сверяют записи. Кто-то вырвал из тетради страницу с десятью книгами. Неавторитативное восстановление — библиотекарь достаёт из шкафа старую копию страницы и говорит: «сверяемся с коллегами, что у них новее — то и впишем». Авторитативное — он берёт старую копию и объявляет: «эта страница главная, перепишите её к себе все, даже если вы уже вычеркнули эти книги». Корзина AD — это корзина для бумаг под столом, из которой страницу можно достать целиком, а не в виде обрывков.

Аналогия ломается в одном месте: у бумаги нет «номера версии», а в AD именно счётчик версий атрибута решает, чья запись победит, — поэтому авторитативное восстановление не «переписывает», а именно навязывает версию.

## 4. Практика и команды

**1. Установка Windows Server Backup (DC01, PowerShell от админа).**
```powershell
Install-WindowsFeature Windows-Server-Backup -IncludeManagementTools
```
Роль/компонент `Windows-Server-Backup` ставит оснастку `wbadmin` и MMC. Без неё `wbadmin` отвечает «не удалось найти». GUI: `Server Manager → Add Roles and Features → Features → Windows Server Backup`.

**2. Список существующих бэкапов.**
```powershell
wbadmin get versions
```
```
Backup time: 3/15/2026 10:04 PM
Backup location: Local hard disk E:
Version identifier: 03/15/2026-22:04
Can Recover: Volume, File and folders, Application, System state
```
Смотрим **Version identifier** — именно его подставляем в `-version:`. Если бэкап System State — в строке `Can Recover` будет `System state`.

**3. Создание бэкапа System State.**
```powershell
wbadmin start systemstatebackup -backuptarget:E: -quiet
```
`-backuptarget` — том, куда пишем (не может быть системным); `-quiet` — без интерактивных подтверждений, удобно для расписания. Занимает от единиц до десятков минут в зависимости от размера базы. Расписание — `wbadmin enable backup` или Планировщик задач.

**4. Что именно попало в бэкап.**
```powershell
wbadmin get items -version:03/15/2026-22:04
```
Показывает перечень компонентов внутри версии. Если в списке нет `System State` — значит, бэкапили тома, а не состояние системы, и для восстановления AD он бесполезен.

**5. Неавторитативное восстановление (в DSRM).**
```powershell
wbadmin start systemstaterecovery -version:03/15/2026-22:04 -quiet
```
Ключ `-authsysvol` дополнительно делает авторитативным восстановление SYSVOL (нужно, когда «уехали» GPO; применяется с осторожностью — перетирает SYSVOL на всех партнёрах). После завершения `wbadmin` попросит перезагрузку — и она должна пройти **снова в DSRM**.

**6. Управление DSRM-паролем.**
```powershell
ntdsutil "set dsrm password" "reset password on server null" "Lab#2026-vektor" q q
```
`null` = текущий сервер (локальная машина). Синтаксис одной строкой — чтобы работало из PowerShell; в интерактивном `ntdsutil` те же команды вводятся построчно. В боевой среде DSRM-пароль хранится в сейфе вместе с процедурой восстановления.

**7. Загрузка в DSRM и обратно.**
```cmd
bcdedit /set safeboot dsrepair
shutdown /r /t 0
:: после работ:
bcdedit /deletevalue safeboot
shutdown /r /t 0
```
Альтернатива — F8 при загрузке и выбор режима восстановления служб каталогов. После входа в DSRM проверь, что NTDS реально не работает: оснастка ADUC не откроется, `Get-ADUser` вернёт ошибку — это нормально.

**8. Авторитативное восстановление объекта или поддерева (только в DSRM).**
```cmd
ntdsutil
activate instance ntds
authoritative restore
restore subtree "OU=Test-Restore,OU=Company,DC=corp,DC=vektor,DC=lab"
```
```
Opening DIT database... (это может занять несколько минут)
Authoritative Restore of subtree "OU=Test-Restore,..." completed successfully.
```
`restore subtree` — рекурсивно по OU; `restore database` — вся база целиком (в реальной жизни почти никогда не нужно и очень опасно: «воскресит» всё, что удаляли после бэкапа). Кавычки обязательны, если в DN есть пробелы/кириллица.

**9. Проверка репликации после восстановления.**
```powershell
repadmin /syncall DC01 /AdeP
repadmin /showobjmeta DC01 "CN=Test User,OU=Test-Restore,OU=Company,DC=corp,DC=vektor,DC=lab"
dcdiag /v /test:replications,sysvol,netlogons,advertising
```
`/syncall /AdeP`: A — все разделы, d — идентификация по DN, e — включая межсайтовые связи, P — push (толкать изменения партнёрам). В `showobjmeta` смотрим колонку версий атрибутов: после авторитативного восстановления она должна быть заметно выше прежней — это и есть «навязанная» версия.

**10. Статус корзины AD.**
```powershell
Get-ADOptionalFeature -Filter * | Select-Object Name,EnabledScopes
```
```
Name                              EnabledScopes
----                              -------------
Recycle Bin Feature               {CN=Partitions,CN=Configuration,DC=corp,DC=vektor,DC=lab}
```
Пустой `EnabledScopes` = корзина выключена. Включение (необратимо, требует Enterprise Admins, выполнять в корне леса):
```powershell
Enable-ADOptionalFeature -Identity 'Recycle Bin Feature' -Scope ForestOrConfigurationSet -Target corp.vektor.lab
```

**11. Поиск и восстановление удалённых объектов.**
```powershell
Get-ADObject -Filter 'Deleted -eq $true -and ObjectClass -eq "group"' -IncludeDeletedObjects `
    -Properties LastKnownParent,whenChanged |
  Select-Object Name,ObjectGUID,LastKnownParent,whenChanged

Restore-ADObject -Identity 'b7e2c1a4-1f3d-4a55-9c11-7d0b2a9e4c33'
```
`-IncludeDeletedObjects` — единственный способ увидеть контейнер `CN=Deleted Objects` через PowerShell. `LastKnownParent` показывает, откуда объект удалён (куда вернётся). Восстанавливать нужно **сверху вниз**: сначала OU, потом объекты внутри — командлет восстанавливает ровно один объект, не поддерево.

**12. Tombstone lifetime — проверка перед восстановлением.**
```powershell
$cfg = (Get-ADRootDSE).configurationNamingContext
Get-ADObject -Identity "CN=Directory Service,CN=Windows NT,CN=Services,$cfg" -Properties tombstoneLifetime |
  Select-Object tombstoneLifetime
```
`180` = дней. Если дата бэкапа старше — восстанавливать нельзя. GUI-путь: `ADSI Edit → Configuration → Services → Windows NT → Directory Service → Properties → tombstoneLifetime`.

**13. GUI для тех же задач.** Корзина: `ADUC → View → Advanced Features → corp.vektor.lab → Deleted Objects` (объекты с иконкой «корзина», правый клик → Restore). Бэкап: `Server Manager → Tools → Windows Server Backup → Local Backup → Backup Once / Recover`.

## 5. Лабораторная работа

### Предусловия
- **DC01** (10.10.10.10): AD DS, DNS, DHCP, домен `corp.vektor.lab` — здоров.
- **DC02** (10.10.10.11): второй контроллер, репликация работает (урок 17). Если его нет — сначала урок 17, без второго DC авторитативное восстановление не на чем показать.
- **FS01** (10.10.10.20): шара `\\FS01\HR`, в ACL — `VEKTOR\GG-HR`.
- **WIN10-CL02**: рабочее место `petrova`, вход доменной учёткой.
- Учётки: `adm.student` (админ домена), `petrova`, группа `GG-HR`.
- На DC01 нет отдельного тома под бэкап — добавим (шаг 2).

### Снапшот
В VMware Workstation: правый клик по ВМ → `Snapshot → Take Snapshot`, имя **`before-l2-026`**. Сделать на **DC01, DC02, FS01, WIN10-CL02** (DC01 — обязательно, он основной). В `stand-log.md` записать: «before-l2-026 снят на DC01/DC02/FS01/WIN10-CL02 перед работами по восстановлению AD».

### Шаги

1. **DC01.** Добавить второй виртуальный диск: `VM → Settings → Add → Hard Disk → SCSI → 40 GB → Store as single file`. В `diskmgmt.msc`: инициализировать, создать том **E:**, файловая система NTFS.
   *Ожидаемо:* в «Моём компьютере» появился диск E: на 40 ГБ.

2. **DC01.** Установить компонент бэкапа: `Install-WindowsFeature Windows-Server-Backup -IncludeManagementTools`.
   *Ожидаемо:* `Success: True`, `Restart Needed: No`; `wbadmin /?` выдаёт справку.

3. **DC01.** Проверить корзину: `Get-ADOptionalFeature -Filter * | Select Name,EnabledScopes`.
   *Ожидаемо:* `EnabledScopes` пустой → корзина выключена. Записать это в журнал стенда.

4. **DC01.** Включить корзину: `Enable-ADOptionalFeature -Identity 'Recycle Bin Feature' -Scope ForestOrConfigurationSet -Target corp.vektor.lab`.
   *Ожидаемо:* предупреждение о необратимости и запрос подтверждения; после — `EnabledScopes` содержит `CN=Partitions,...`. Повторный запуск `Get-ADOptionalFeature` это подтверждает.

5. **DC01.** Создать учебные объекты:
   ```powershell
   New-ADOrganizationalUnit -Name "Test-Restore" -Path "OU=Company,DC=corp,DC=vektor,DC=lab"
   New-ADUser -Name "Test User" -SamAccountName "test.user" -Path "OU=Test-Restore,OU=Company,DC=corp,DC=vektor,DC=lab" -Enabled $true -AccountPassword (ConvertTo-SecureString 'Lab#2026-vektor' -AsPlainText -Force)
   Add-ADGroupMember -Identity GG-HR -Members test.user
   ```
   *Ожидаемо:* в ADUC видна OU `Test-Restore` с пользователем, `Get-ADPrincipalGroupMembership test.user` показывает `GG-HR`.

6. **DC01.** Снять бэкап System State: `wbadmin start systemstatebackup -backuptarget:E: -quiet`, затем `wbadmin get versions`.
   *Ожидаемо:* «Backup completed successfully», в списке версия с `Can Recover: System state`. **Записать version identifier в `stand-log.md`** — он понадобится в шаге 9.

7. **DC01.** Демо корзины: `Remove-ADUser -Identity test.user -Confirm:$false`, затем
   ```powershell
   Get-ADObject -Filter 'Deleted -eq $true' -IncludeDeletedObjects -Properties LastKnownParent | Select Name,ObjectGUID,LastKnownParent
   Restore-ADObject -Identity '<GUID из вывода>'
   ```
   *Ожидаемо:* пользователь вернулся в OU `Test-Restore`; `Get-ADPrincipalGroupMembership test.user` снова показывает `GG-HR`. В `ADUC → Advanced Features → Deleted Objects` до восстановления он был виден.

8. **DC01.** Имитация аварии: удалить OU целиком вместе с содержимым, дать реплицироваться, проверить на DC02.
   ```powershell
   Remove-ADOrganizationalUnit -Identity "OU=Test-Restore,OU=Company,DC=corp,DC=vektor,DC=lab" -Recursive -Confirm:$false
   repadmin /syncall DC02 /AdeP
   Invoke-Command -ComputerName DC02 -ScriptBlock { Get-ADOrganizationalUnit -Filter "Name -eq 'Test-Restore'" }
   ```
   *Ожидаемо:* пустой результат — «авария» разъехалась по домену. Вернуть через корзину можно, но мы специально идём длинным путём.

9. **DC01.** Задать DSRM-пароль и уйти в DSRM:
   ```powershell
   ntdsutil "set dsrm password" "reset password on server null" "Lab#2026-vektor" q q
   bcdedit /set safeboot dsrepair
   shutdown /r /t 0
   ```
   *Ожидаемо:* после ребута на экране входа — «This computer is running in Directory Services Restore Mode». Входим `.\Administrator` + `Lab#2026-vektor`. `Get-ADUser` не работает — это нормально.

10. **DC01 (в DSRM).** Восстановить состояние системы:
    ```powershell
    wbadmin start systemstaterecovery -version:<version identifier из шага 6> -quiet
    ```
    *Ожидаемо:* «The restore operation completed successfully», запрос на перезагрузку. Перезагружаемся — снова попадаем в DSRM (safeboot ещё установлен).

11. **DC01 (в DSRM).** Авторитативно вернуть OU:
    ```cmd
    ntdsutil
    activate instance ntds
    authoritative restore
    restore subtree "OU=Test-Restore,OU=Company,DC=corp,DC=vektor,DC=lab"
    ```
    На вопрос `Are you sure you want to perform this authoritative restore?` — `Yes`. Затем `quit`, `quit`.
    *Ожидаемо:* «Authoritative Restore completed successfully».

12. **DC01.** Выйти из DSRM: `bcdedit /deletevalue safeboot`, `shutdown /r /t 0`. После обычной загрузки — проверить службы: `Get-Service NTDS,DFSR,DNS` в состоянии Running; `dcdiag /v /test:replications,sysvol,netlogons` без ошибок; `repadmin /syncall DC01 /AdeP`; затем на DC02 — `Get-ADOrganizationalUnit -Filter "Name -eq 'Test-Restore'"` и `Get-ADUser test.user`.
    *Ожидаемо:* OU и пользователь есть на обоих DC; в `repadmin /showobjmeta` версии атрибутов OU выше, чем были до удаления. В `stand-log.md` — запись о полной процедуре и номере версии бэкапа.

### Проверка
- [ ] `Get-ADOptionalFeature` показывает включённый `Recycle Bin Feature`.
- [ ] В `wbadmin get versions` есть версия с `Can Recover: System state`.
- [ ] Пользователь `test.user` восстановлен из корзины без перезагрузки DC.
- [ ] После шага 8 OU отсутствовала на DC02.
- [ ] После шага 12 OU `Test-Restore` и пользователь `test.user` присутствуют **на обоих** контроллерах.
- [ ] `dcdiag /test:replications,sysvol,netlogons` — без ошибок.
- [ ] `Get-Service NTDS` = Running, вход в домен с WIN10-CL02 работает.
- [ ] В `stand-log.md` записаны: включение корзины, DSRM-пароль (учебный), номер версии бэкапа, факт авторитативного восстановления.

### Сломай и почини
**Пользователь:** Петрова (WIN10-CL02). **Что написала:** «Не открывается `\\FS01\HR`, пишет "Отказано в доступе". Вчера работало, я ничего не меняла. И в Outlook пропала рассылка HR». **Срочность:** средняя (блокирует работу с кадровыми документами).

**Воспроизведение поломки (DC01, от `adm.student`):**
```powershell
Remove-ADGroup -Identity GG-HR -Confirm:$false
```

**Диагностика по порядку (не прыгать через шаги):**
1. На WIN10-CL02: `whoami /groups | findstr GG-HR` — группы нет в токене. Значит, проблема не в шаре.
2. На DC01: `Get-ADPrincipalGroupMembership petrova | Select Name` — `GG-HR` отсутствует.
3. `Get-ADGroup -Identity GG-HR` → «Cannot find an object with identity» — группы в AD нет.
4. Проверить, что дело не в FS01: `Get-Acl \\FS01\HR | Format-List` — в ACL есть запись `VEKTOR\GG-HR` (осиротевшая SID), значит права настраивали, группа была.
5. Искать в корзине:
   ```powershell
   Get-ADObject -Filter 'Deleted -eq $true -and Name -eq "GG-HR"' -IncludeDeletedObjects -Properties LastKnownParent,whenChanged
   ```
   *Видим:* `GG-HR`, `LastKnownParent: OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab`, удалена сегодня.
6. Восстановить: `Restore-ADObject -Identity '<ObjectGUID>'`.
7. Проверить членство: `Get-ADGroupMember GG-HR` — `petrova` на месте (прямые ссылки `member` хранятся на самой группе, поэтому вернулись вместе с ней; если бы刪али и пользователей — восстанавливать надо было сначала их).
8. На WIN10-CL02: `klist purge`, `gpupdate /force`, выйти/войти, открыть `\\FS01\HR`.

**Решение:** группа восстановлена из корзины AD, права на шаре подхватились автоматически — ACL ссылается на SID, а SID при восстановлении сохраняется.

**Ответ пользователю:** «Мария, добрый день! Причина найдена: группа доступа HR была ошибочно удалена в каталоге. Восстановил её, права на `\\FS01\HR` вернулись. Пожалуйста, перезайдите в систему (или выполните `gpupdate /force`) и проверьте доступ. Список рассылки в Outlook также восстановится после обновления адресной книги, обычно в течение часа — если нет, напишите, обновлю вручную».

**Закрытие заявки:** категория «AD / права доступа», приоритет средний, в решении указать: «deleted group GG-HR restored from AD Recycle Bin, ObjectGUID …, membership preserved, access verified with user». Отдельным тикетом — предложение включить аудит удаления объектов в AD (иначе кто именно удалил, мы не узнаем: без аудита событие не записано).

### Что изменилось на стенде
- **DC01:** добавлен виртуальный диск **E: 40 ГБ**; установлен компонент `Windows-Server-Backup`; создан бэкап System State на E: (версия — указать в журнале); DSRM-пароль установлен в `Lab#2026-vektor`.
- **Лес `corp.vektor.lab`:** **включена корзина AD** (необратимо, функциональный уровень ≥ 2008 R2).
- **AD:** OU `Test-Restore` в `OU=Company` с пользователем `test.user` (пароль стенда), членство в `GG-HR`.
- **DC01/DC02:** подтверждена рабочая репликация после восстановления; DC01 перезагружался дважды (DSRM → обычный режим).
- Дальнейшие главы могут опираться на: корзина AD включена; на DC01 есть том E: под бэкапы; известен DSRM-пароль.

### Откат
Если что-то пошло не так (не выходит из DSRM, NTDS не стартует, репликация «красная»): выключить ВМ → `VM → Snapshot → Snapshot Manager` → выбрать `before-l2-026` → `Go To`. Восстанавливать в порядке: DC02, затем DC01 (чтобы домен не остался без второго контроллера), затем FS01 и WIN10-CL02. После отката записать в `stand-log.md`: «работа откачена к before-l2-026, корзина AD выключена, диск E: отсутствует» — иначе следующая глава будет опираться на несуществующее состояние.

## 6. Ошибки новичков и безопасность

- **«Сделаю бэкап, скопировав `NTDS.dit`»** → файл занят службой NTDS, копия битая или неконсистентная → при восстановлении база не монтируется. Правильно: только VSS-бэкап (`wbadmin`, Veeam Agent Free и аналоги) или снапшот тома.
- **Восстановление из бэкапа старше tombstone lifetime** → lingering objects, «объекты-призраки», ручная чистка домена сутками. Правильно: перед восстановлением проверить `tombstoneLifetime` и дату бэкапа.
- **Путать авторитативное и неавторитативное** → сделал `restore database` «на всякий случай» и воскресил полсотни объектов, которые осознанно удаляли месяц. Правильно: точечно `restore subtree` для конкретной OU, `restore database` — практически никогда.
- **Нет записи DSRM-пароля** → в момент аварии выясняется, что войти в DSRM нечем, а сброс требует работающего DC. Правильно: пароль в сейфе/менеджере секретов, процедура восстановления описана заранее и **проверена** учебным восстановлением раз в полгода.
- **Корзина AD не включена «потому что не было времени»** → восстановление каждого удалённого пользователя превращается в многочасовой квест с DSRM. Правильно: включить заранее, это одна команда и одно окно изменений.
- **Восстановление DC без проверки репликации** → домен «вроде работает», но часть объектов есть только на одном контроллере. Правильно: после восстановления всегда `repadmin /syncall`, `repadmin /showrepl`, `dcdiag /v`.
- **Считать снапшот VMware резервной копией DC** → откат снапшота DC в проде — это не бэкап и не план восстановления. Для DC на Windows Server 2012+ и новее (VM-GenerationID) откат снапшота не приводит к USN rollback, но данные, созданные после снапшота, теряются, а доверие к снапшоту как к «бэкапу» — самая дорогая иллюзия в этой теме. В лабе снапшоты — только инструмент отката учебных действий.

**Безопасность.** Бэкап System State содержит `NTDS.dit` — то есть **хэши паролей всех пользователей домена**. Носитель с таким бэкапом по ценности равен «ключу от компании»: его нельзя хранить на общедоступной шаре `\\FS01\Obshchee`, в личной папке или на флешке в столе. В боевой среде — отдельный защищённый том или лента в сейфе, шифрование (BitLocker), список лиц с доступом, ротация. DSRM-пароль даёт локальный вход на контроллер домена и теоретически позволяет поднять NTDS офлайн и вытащить базу — обращайтесь с ним как с привилегированной учёткой (см. урок 25 про тиринг). И не делайте «на время лабы» отключение файрвола на DC01: в жизни контроллер домена — последнее, где стоит трогать сетевые профили.

## 7. Вопросы на собеседовании

**1. Чем авторитативное восстановление отличается от неавторитативного и когда вы примените каждое?**
Неавторитативное: база DC восстанавливается из бэкапа, а затем контроллер синхронизируется с партнёрами, и более свежие данные приходят от них. Применяется при отказе DC, когда остальные контроллеры живы и содержат актуальные данные. Авторитативное: восстановленным объектам поднимается номер версии атрибутов, и они при репликации перезаписывают копии на всех DC. Применяется, когда надо вернуть объект, которого больше нигде нет — например, удалённую OU или группу, а корзина AD не включена или объект удалён давно. Порядок: восстановление System State в DSRM, перезагрузка (всё ещё DSRM), `ntdsutil authoritative restore`, обычная загрузка, `repadmin /syncall`.
*Что хочет услышать интервьюер:* понимание, что «авторитативно» = навязать версию, а не «просто восстановить», и что делается это только из DSRM. *Красный флаг:* «авторитативное — это когда восстанавливаю всё, неавторитативное — когда один объект» (перепутано местами) или «разницы почти нет».

**2. Пользователь удалён. Ваши действия?**
Сначала проверяю, включена ли корзина AD: `Get-ADOptionalFeature`. Если да — нахожу объект через `Get-ADObject -IncludeDeletedObjects`, проверяю `LastKnownParent` и восстанавливаю `Restore-ADObject` по GUID: объект вернётся с атрибутами и членством в группы. Если нет — объект уже tombstone с обрезанными атрибутами: пробую ADRestore/ldp, а если атрибутов не хватает — восстанавливаю из System State-бэкапа с авторитативным восстановлением нужной ветки. Если удаление старше tombstone lifetime и бэкапа нет — объект не восстановим, создаём заново.
*Что хочет услышать:* порядок «корзина → tombstone → бэкап» и мысль, что корзину надо включать заранее. *Красный флаг:* «сразу достану из бэкапа» без вопроса о корзине и о сроке удаления.

**3. Что входит в бэкап System State на контроллере домена и можно ли обойтись копированием NTDS.dit?**
Входят `NTDS.dit`, журналы `edb*.log`, SYSVOL, реестр, COM+ база, системные и загрузочные файлы, при наличии — база служб сертификатов и данные кластера. Копирование `NTDS.dit` вручную некорректно: файл удерживается службой NTDS, копия неконсистентна; нужен VSS-бэкап. Плюс для реального восстановления важна не только база, но и SYSVOL с GPO.
*Что хочет услышать:* знание состава и того, что бэкап делается механизмом теневых копий. *Красный флаг:* «просто копирую папку NTDS на флешку».

**4. Опишите пошагово восстановление контроллера домена из System State.**
Проверяю дату бэкапа относительно tombstone lifetime → загружаюсь в DSRM (`bcdedit /set safeboot dsrepair`) → вход `.\Administrator` с DSRM-паролем → `wbadmin get versions` → `wbadmin start systemstaterecovery -version:...` → перезагрузка (снова DSRM) → при необходимости `ntdsutil authoritative restore` → `bcdedit /deletevalue safeboot` → обычная загрузка → `dcdiag`, `repadmin /syncall`, проверка служб NTDS/DFSR/DNS/SYSVOL. Пока DC в DSRM, он не обслуживает пользователей — это надо учитывать в плане и предупредить.
*Что хочет услышать:* порядок и понимание, что между восстановлением и авторитативным шагом нужна перезагрузка, оставаясь в DSRM. *Красный флаг:* «восстановлю и сразу сделаю авторитативное в обычном режиме».

**5. Что такое tombstone lifetime и чем грозит его нарушение?**
Срок, в течение которого удалённый объект хранится в виде «надгробия» (по умолчанию 180 дней). После этого он удаляется окончательно. Если восстановить DC из бэкапа старше этого срока, контроллер начнёт реплицировать объекты, которых на партнёрах уже нет, — появятся lingering objects; также возможны расхождения и ошибки репликации. Поэтому бэкапы AD старше tombstone lifetime не используют, а сам срок проверяют атрибутом `tombstoneLifetime` в `CN=Directory Service,CN=Windows NT,CN=Services,<Configuration NC>`.
*Что хочет услышать:* связь «срок бэкапа ↔ lingering objects», а не просто «объект исчезнет через 180 дней». *Красный флаг:* «tombstone — это журнал удалений, его можно чистить вручную».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Active Directory Domain Services → Operations → Backup and Recovery»; статьи «AD DS: Authoritative Restore», «AD DS: Non-authoritative Restore», «Restore a domain controller from a system state backup», «Enable or disable the AD Recycle Bin» (поиск: `active directory authoritative restore system state`, `ad recycle bin enable`).
- **learn.microsoft.com** — «Windows Server Backup» и справка по `wbadmin`: «Windows Server Backup command reference», `wbadmin start systemstatebackup` (поиск: `wbadmin systemstatebackup command reference`).
- **learn.microsoft.com** — «Tombstone lifetime», «Lingering objects», «How the Active Directory Replication Model Works» (поиск: `ad tombstone lifetime lingering objects`).
- **learn.microsoft.com** — справка PowerShell: `Restore-ADObject`, `Get-ADObject`, `Enable-ADOptionalFeature`, `Get-ADOptionalFeature` в модуле ActiveDirectory (поиск: `Restore-ADObject -IncludeDeletedObjects`).
- **learn.microsoft.com** — «Ntdsutil: authoritative restore» и «set dsrm password» в разделе команд `ntdsutil` (поиск: `ntdsutil authoritative restore restore subtree`).
- **learn.microsoft.com** — «Restore SYSVOL with DFS Replication / authoritative and non-authoritative SYSVOL restore» (поиск: `dfsr authoritative sysvol restore msDFSR-Enabled`) — атрибуты процедуры уточнить: `[ПРОВЕРИТЬ]`.
- **learn.microsoft.com** — «USN rollback» и «VM-GenerationID» (поиск: `usn rollback virtualized domain controller vm-generationid`).
