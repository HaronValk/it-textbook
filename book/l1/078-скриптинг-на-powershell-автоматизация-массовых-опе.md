# Скриптинг на PowerShell: автоматизация массовых операций с пользователями AD

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 9:40. В `C:\Support\Tickets` падает заявка от Марии Петровой (HR, WIN10-CL02): «Иван Иванов переходит в отдел логистики с 1-го числа. Нужно: сменить ему отдел в учётке, добавить в группу GG-Sklad, убрать из GG-Buh, и ещё у нас 12 новых стажёров — список во вложении, заведи им учётки до обеда. Срочно, у нас онбординг». Срочность: высокая.

Если делать руками в оснастке «Active Directory — пользователи и компьютеры», на 12 стажёров уйдёт час с половиной, и на третьем обязательно опечатаешься в фамилии или забудешь галочку «Требовать смену пароля при следующем входе». А если завтра HR пришлёт ещё 30? А если через месяц — 200 при переводе целого отдела? Именно поэтому в реальной работе L1 и сисадмины автоматизируют массовые операции с AD скриптами. PowerShell — родной инструмент для этого в Windows-инфраструктуре: он уже стоит на DC01, умеет работать с AD через модуль `ActiveDirectory` и одинаково хорошо справляется и с одной учёткой, и с тысячей.

После этой главы ты будешь уметь: читать CSV со списком сотрудников, создавать учётки пачкой, менять атрибуты (отдел, должность, телефон), управлять членством в группах, безопасно сбрасывать пароли и — главное — делать всё это с `-WhatIf` и логом, чтобы не снести пол-домена одной опечаткой. Это ровно тот навык, который на собеседовании L1 отделяет «умею тыкать в ADUC» от «умею автоматизировать рутину».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Active Directory — это база данных (файл `NTDS.dit` на контроллере домена) плюс набор служб, которые к ней обращаются по сети. Когда ты создаёшь пользователя в оснастке ADUC, клиент на самом деле отправляет LDAP-запрос на DC01 по порту 389 (или 636 для LDAPS). PowerShell с модулем `ActiveDirectory` делает ровно то же самое — просто из командной строки.

Схема того, что происходит при запуске скрипта:

```text
[Твой скрипт .ps1 на DC01]
        |
        |  Import-Module ActiveDirectory
        |  New-ADUser / Set-ADUser / Add-ADGroupMember
        v
[Модуль ActiveDirectory (RSAT-AD-PowerShell)]
        |
        |  LDAP-запрос (порт 389) + Kerberos-аутентификация (порт 88)
        v
[DC01: служба NTDS (AD DS)]
        |
        v
[База NTDS.dit: объекты OU=Company,CN=Users,CN=Groups...]
```

Ключевые понятия, без которых скрипт не напишешь:

- **Объект AD** — учётка, группа, OU, компьютер. У каждого есть атрибуты: `SamAccountName` (логин, до 20 символов), `UserPrincipalName` (`ivanov@corp.vektor.lab`), `GivenName`, `Surname`, `Department`, `Title`, `DistinguishedName` (полный путь вида `CN=Ivan Ivanov,OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab`).
- **Модуль `ActiveDirectory`** — набор командлетов (`New-ADUser`, `Get-ADUser`, `Set-ADUser`, `Add-ADGroupMember`, `Remove-ADGroupMember`, `Get-ADGroupMember`, `Unlock-ADAccount`, `Enable-ADAccount`). Ставится как компонент RSAT-AD-PowerShell. На DC01 он уже есть (роль AD DS установлена в L1 №42).
- **CSV** — простейший формат таблицы, где первая строка — заголовки, дальше строки через запятую. `Import-Csv` превращает его в массив объектов PowerShell, у которых свойства называются как заголовки. Это стандартный способ «скормить» скрипту список сотрудников от HR.
- **`-WhatIf`** — «показать, что сделал бы, но не делать». Есть у всех командлетов, которые меняют данные. Твой главный предохранитель.
- **`-Confirm:$false`** — отключить интерактивное подтверждение. В скрипте для пачки пользователей без этого он будет спрашивать «Вы уверены?» 200 раз.
- **`try/catch`** — обработка ошибок. Если на 5-й строке CSV битые данные, скрипт не должен упасть целиком, а должен записать ошибку в лог и продолжить.

Важно понимать: PowerShell-скрипт — это не «магия», а просто последовательность тех же действий, что ты делаешь мышкой. Разница в том, что скрипт повторяем, документируем и не устаёт.

## 3. Аналогия из жизни

Представь, что ты работаешь в отделе кадров и тебе нужно заполнить 200 одинаковых анкет на новых сотрудников. Можно взять ручку и каждую заполнить вручную — медленно, с опечатками, и на 50-й ты уже путаешь, куда ставить галочку. А можно сделать шаблон в Word с полями и функцией слияния: ты готовишь одну таблицу Excel с данными (ФИО, отдел, должность), нажимаешь «Слияние» — и Word сам печатает 200 анкет, подставляя данные из таблицы. PowerShell-скрипт с CSV — это ровно та же «функция слияния», только для Active Directory: CSV — это твоя таблица Excel, `New-ADUser` в цикле — это «напечатать анкету для каждой строки». Аналогия ломается в одном: Word не удалит по ошибке существующего сотрудника, а `Remove-ADUser` — удалит, и восстанавливать будешь из корзины AD (если она включена) или из бэкапа.

## 4. Практика и команды

Все команды ниже выполняются на **DC01** в PowerShell, запущенном **от имени администратора** (учётка `adm.student` — она же доменный админ). Модуль `ActiveDirectory` там уже есть.

**1. Проверить, что модуль AD доступен и домен виден.**

```powershell
Get-Module -ListAvailable ActiveDirectory
Get-ADDomain | Select-Object Name, DNSRoot, DistinguishedName
```

`Get-Module -ListAvailable` показывает установленные модули. `Get-ADDomain` возвращает объект домена. Ожидаемо увидишь `Name: VEKTOR`, `DNSRoot: corp.vektor.lab`, `DistinguishedName: DC=corp,DC=vektor,DC=lab`. Если модуль не найден — на DC01 это значит, что роль AD DS не установлена (у тебя она есть с L1 №42).

**2. Найти пользователя по логину и посмотреть его атрибуты.**

```powershell
Get-ADUser -Identity ivanov -Properties Department, Title, MemberOf |
    Format-List Name, SamAccountName, Department, Title, MemberOf
```

`-Identity` принимает логин (`SamAccountName`), DN или SID. `-Properties` — запросить дополнительные атрибуты, которых нет в выводе по умолчанию (по умолчанию `Get-ADUser` отдаёт только базовый набор). `Format-List` — вывести в столбик, удобно читать. Смотри на `Department` (должен быть «Бухгалтерия») и `MemberOf` (там будет `CN=GG-Buh,...`).

**3. Найти всех пользователей в OU «Бухгалтерия».**

```powershell
Get-ADUser -Filter * -SearchBase "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab" |
    Select-Object Name, SamAccountName, Enabled
```

`-Filter *` — «все объекты». `-SearchBase` — ограничить поиск конкретным OU. `Select-Object` — оставить только нужные колонки. Ожидаемо: `ivanov` (и, возможно, другие, если ты их создавал в L1 №43). `Enabled: True`.

**4. Создать одного пользователя вручную (для проверки синтаксиса).**

```powershell
$password = ConvertTo-SecureString "Lab#2026-vektor" -AsPlainText -Force
New-ADUser -Name "Тестовый Пользователь" `
    -SamAccountName "test.user" `
    -UserPrincipalName "test.user@corp.vektor.lab" `
    -Path "OU=IT,OU=Company,DC=corp,DC=vektor,DC=lab" `
    -AccountPassword $password `
    -ChangePasswordAtLogon $true `
    -Enabled $true
```

`ConvertTo-SecureString` превращает строку в защищённый объект (AD не принимает пароль открытым текстом). `-Path` — OU, куда кладём. `-ChangePasswordAtLogon $true` — заставит сменить пароль при первом входе (стандартная практика). `-Enabled $true` — учётка активна сразу. Обратные кавычки `` ` `` — перенос строки в PowerShell (не путать с `\` в bash).

**5. Создать пачку пользователей из CSV.**

Сначала CSV. Положи в `C:\Support\Tickets\new-users.csv` (на DC01 создай папку, если её нет):

```text
FirstName,LastName,Sam,Department,Title
Алексей,Сидоров,sidorov,Склад,Кладовщик
Пётр,Смирнов,smirnov,Склад,Кладовщик
Ольга,Кузнецова,kuznetsova,HR,Менеджер по персоналу
```

Скрипт:

```powershell
Import-Csv "C:\Support\Tickets\new-users.csv" | ForEach-Object {
    $sam = $_.Sam
    $upn = "$sam@corp.vektor.lab"
    $ou  = switch ($_.Department) {
        "Бухгалтерия" { "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab" }
        "HR"          { "OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab" }
        "Склад"       { "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab" }
        default       { "OU=IT,OU=Company,DC=corp,DC=vektor,DC=lab" }
    }
    $pwd = ConvertTo-SecureString "Lab#2026-vektor" -AsPlainText -Force
    New-ADUser -Name "$($_.FirstName) $($_.LastName)" `
        -GivenName $_.FirstName -Surname $_.LastName `
        -SamAccountName $sam -UserPrincipalName $upn `
        -Department $_.Department -Title $_.Title `
        -Path $ou -AccountPassword $pwd `
        -ChangePasswordAtLogon $true -Enabled $true
    Write-Host "Создан: $sam"
}
```

`Import-Csv` читает файл, `ForEach-Object` идёт по строкам, `$_` — текущая строка, `$_.Sam` — значение колонки `Sam`. `switch` выбирает OU по отделу. `$($_.FirstName)` — подстановка свойства в строку (нужна, потому что `$_` внутри строки не раскрывается сам). Ожидаемо: три строки «Создан: sidorov», «Создан: smirnov», «Создан: kuznetsova». Если запустить повторно — упадёт с ошибкой «объект уже существует» на первой же строке, потому что `New-ADUser` не идемпотентен.

**6. Массово поменять атрибуты (перевод Иванова в склад).**

```powershell
Set-ADUser -Identity ivanov -Department "Склад" -Title "Логист" -WhatIf
Set-ADUser -Identity ivanov -Department "Склад" -Title "Логист"
```

`Set-ADUser` меняет атрибуты существующей учётки. Первая строка с `-WhatIf` покажет «What if: Performing the operation "Set" on target "CN=Иван Иванов,..."» и ничего не сделает — это твоя проверка. Вторая строка реально меняет. Всегда сначала `-WhatIf`, потом без него.

**7. Массово добавить/убрать из групп.**

```powershell
Add-ADGroupMember -Identity "GG-Sklad" -Members ivanov
Remove-ADGroupMember -Identity "GG-Buh" -Members ivanov -Confirm:$false
Get-ADGroupMember -Identity "GG-Sklad" | Select-Object Name, SamAccountName
```

`Add-ADGroupMember` принимает логины, DN или массив. `-Confirm:$false` убирает запрос «Вы уверены?» (у `Remove-ADGroupMember` он есть по умолчанию). `Get-ADGroupMember` — проверить состав. Ожидаемо: в `GG-Sklad` появится `ivanov`, в `GG-Buh` его не будет.

**8. Массовый сброс пароля с разблокировкой.**

```powershell
$users = Get-Content "C:\Support\Tickets\reset-list.txt"
$pwd = ConvertTo-SecureString "Lab#2026-vektor" -AsPlainText -Force
foreach ($u in $users) {
    try {
        Set-ADAccountPassword -Identity $u -NewPassword $pwd -Reset
        Set-ADUser -Identity $u -ChangePasswordAtLogon $true
        Unlock-ADAccount -Identity $u
        Write-Host "OK: $u"
    } catch {
        Write-Warning "Ошибка для $u : $($_.Exception.Message)"
    }
}
```

`Get-Content` читает текстовый файл построчно (один логин на строку). `Set-ADAccountPassword -Reset` — сброс без указания старого пароля (нужно админское право). `Unlock-ADAccount` — снять блокировку после неудачных попыток входа. `try/catch` — если логина нет, скрипт не упадёт, а напишет предупреждение и пойдёт дальше. Это критично для пачек: одна опечатка в списке не должна ронять весь процесс.

**9. Проверить, кто заблокирован и у кого истёк пароль.**

```powershell
Search-ADAccount -LockedOut | Select-Object Name, SamAccountName
Search-ADAccount -PasswordExpired | Select-Object Name, SamAccountName
Search-ADAccount -AccountDisabled | Select-Object Name, SamAccountName
```

`Search-ADAccount` — готовые фильтры для типовых задач L1. Быстрее, чем писать `Get-ADUser -Filter {LockedOut -eq $true}`.

**10. Логировать всё в файл (обязательная практика).**

```powershell
Start-Transcript -Path "C:\Support\Tickets\ad-script-$(Get-Date -Format yyyyMMdd-HHmm).log"
# ... твои команды ...
Stop-Transcript
```

`Start-Transcript` записывает всё, что выводится в консоль, в файл. `Get-Date -Format` подставляет дату в имя. В рабочей среде это требование аудита: если скрипт что-то сломал, ты должен показать, что именно он делал.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- DC01 (10.10.10.10) — Windows Server 2022, роль AD DS установлена, домен `corp.vektor.lab` работает (L1 №42), OU `Company` с подпапками `Бухгалтерия`, `HR`, `Склад`, `IT` и группы `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT` созданы (L1 №43).
- Учётки `ivanov`, `petrova`, `sidorov` существуют в домене (L1 №43). `ivanov` состоит в `GG-Buh`.
- WIN11-CL01 и WIN10-CL02 введены в домен (L1 №44) — для проверки, что изменения видны с клиента.
- На DC01 есть модуль `ActiveDirectory` (идёт с ролью AD DS).
- Папка `C:\Support\Tickets` на DC01 создана (если нет — создай).

### Снапшот

На DC01 (и на WIN11-CL01, если хочешь откатить и клиентскую часть) сделай снапшот с именем `before-l1-078`. Через GUI VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot. Через `vmrun` с хоста:

```powershell
vmrun -T ws snapshot "C:\Lab\vektor\VMs\DC01\DC01.vmx" before-l1-078
```

### Шаги

1. **На DC01** открой PowerShell от имени администратора (`adm.student`). Проверь модуль и домен:

    ```powershell
    Get-Module -ListAvailable ActiveDirectory
    Get-ADDomain | Select-Object Name, DNSRoot
    ```

    Ожидаемо: модуль найден, `Name: VEKTOR`, `DNSRoot: corp.vektor.lab`.

2. **На DC01** создай папку и CSV со списком новых сотрудников:

    ```powershell
    New-Item -ItemType Directory -Path "C:\Support\Tickets" -Force | Out-Null
    @"
    FirstName,LastName,Sam,Department,Title
    Пётр,Смирнов,smirnov,Склад,Кладовщик
    Ольга,Кузнецова,kuznetsova,HR,Менеджер по персоналу
    "@ | Set-Content -Path "C:\Support\Tickets\new-users.csv" -Encoding UTF8
    ```

    Ожидаемо: файл создан, `Get-Content` показывает три строки (заголовок + две записи). `-Encoding UTF8` важен: без него кириллица в CSV превратится в кракозябры.

3. **На DC01** запусти скрипт создания из раздела 4 (пункт 5), но сначала с `-WhatIf`:

    ```powershell
    Import-Csv "C:\Support\Tickets\new-users.csv" | ForEach-Object {
        New-ADUser -Name "$($_.FirstName) $($_.LastName)" `
            -SamAccountName $_.Sam -Path "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab" `
            -WhatIf
    }
    ```

    Ожидаемо: две строки «What if: Performing the operation "New" on target "CN=Пётр Смирнов,..."». Ничего не создано. Проверь: `Get-ADUser smirnov` → ошибка «не найден».

4. **На DC01** убери `-WhatIf` и запусти реально (полный скрипт из раздела 4, пункт 5). Ожидаемо: «Создан: smirnov», «Создан: kuznetsova». Проверь:

    ```powershell
    Get-ADUser smirnov, kuznetsova -Properties Department, Title |
        Select-Object Name, SamAccountName, Department, Title, Enabled
    ```

    Ожидаемо: обе учётки `Enabled: True`, отделы «Склад» и «HR», должности заполнены.

5. **На DC01** переведи Иванова в склад (сценарий из заявки):

    ```powershell
    Set-ADUser -Identity ivanov -Department "Склад" -Title "Логист" -WhatIf
    Set-ADUser -Identity ivanov -Department "Склад" -Title "Логист"
    Add-ADGroupMember -Identity "GG-Sklad" -Members ivanov
    Remove-ADGroupMember -Identity "GG-Buh" -Members ivanov -Confirm:$false
    ```

    Ожидаемо: `-WhatIf` показал план, потом реально поменялось. Проверь:

    ```powershell
    Get-ADUser ivanov -Properties Department, Title, MemberOf |
        Format-List Department, Title, MemberOf
    Get-ADGroupMember GG-Sklad | Select-Object Name
    Get-ADGroupMember GG-Buh | Select-Object Name
    ```

    Ожидаемо: `Department: Склад`, `Title: Логист`, в `MemberOf` есть `GG-Sklad` и нет `GG-Buh`. В `GG-Sklad` теперь `ivanov` и `smirnov`.

6. **На DC01** сделай массовый сброс пароля для списка. Создай файл:

    ```powershell
    @"
    smirnov
    kuznetsova
    "@ | Set-Content "C:\Support\Tickets\reset-list.txt" -Encoding UTF8
    ```

    Запусти скрипт из раздела 4, пункт 8. Ожидаемо: «OK: smirnov», «OK: kuznetsova». Проверь, что пароль требует смены:

    ```powershell
    Get-ADUser smirnov -Properties PasswordLastSet, pwdLastSet |
        Select-Object Name, PasswordLastSet
    ```

    `PasswordLastSet` обновится на текущее время.

7. **На DC01** проверь блокировки и истёкшие пароли:

    ```powershell
    Search-ADAccount -LockedOut | Select-Object Name, SamAccountName
    Search-ADAccount -PasswordExpired | Select-Object Name, SamAccountName
    ```

    Ожидаемо: пусто (никто не заблокирован, пароли свежие). Если бы кто-то был — увидел бы список.

8. **На WIN11-CL01** войди доменной учёткой `VEKTOR\smirnov` (пароль `Lab#2026-vektor`, при первом входе попросит сменить). Ожидаемо: вход проходит, Windows требует смены пароля — значит, скрипт создал учётку корректно и `ChangePasswordAtLogon` сработал.

9. **На DC01** включи транскрипт и повтори любую операцию (например, `Get-ADUser ivanov`), затем останови:

    ```powershell
    Start-Transcript -Path "C:\Support\Tickets\ad-script-test.log"
    Get-ADUser ivanov -Properties Department
    Stop-Transcript
    Get-Content "C:\Support\Tickets\ad-script-test.log" | Select-Object -First 20
    ```

    Ожидаемо: в логе видны команды и их вывод. Это твой аудит.

10. **На хосте** допиши в `C:\Lab\vektor\stand-log.md` запись: «L1 №78: на DC01 созданы учётки smirnov, kuznetsova; ivanov переведён в Склад (Department, Title, GG-Sklad/GG-Buh); отработан массовый сброс пароля; скрипты в C:\Support\Tickets».

### Проверка

- [ ] `Get-ADUser smirnov, kuznetsova` возвращает обе учётки, `Enabled: True`.
- [ ] `Get-ADUser ivanov -Properties Department, Title` показывает `Склад` / `Логист`.
- [ ] `Get-ADGroupMember GG-Sklad` содержит `ivanov` и `smirnov`.
- [ ] `Get-ADGroupMember GG-Buh` не содержит `ivanov`.
- [ ] Вход `VEKTOR\smirnov` на WIN11-CL01 проходит и требует смены пароля.
- [ ] Файл `C:\Support\Tickets\ad-script-test.log` существует и содержит вывод команд.
- [ ] В `C:\Lab\vektor\stand-log.md` есть запись об уроке.

### Сломай и почини

**Заявка.** Пользователь: Мария Петрова (HR, WIN10-CL02). Что написала: «Я запустила ваш скрипт для новых стажёров, он отработал, но половина учёток не создалась, а вторая половина — без отдела. И теперь при попытке завести нового сотрудника вручную ADUC ругается "не удаётся создать объект, так как он уже существует". Помогите, у нас онбординг через час». Срочность: критическая.

**Как воспроизвести поломку.** На DC01 в CSV `new-users.csv` поменяй заголовок `Department` на `Departament` (опечатка) и добавь строку с уже существующим `Sam` (например, `ivanov`). Запусти скрипт из пункта 5 без `try/catch`. Скрипт упадёт на строке с `ivanov` с ошибкой «объект уже существует» и остановится — остальные строки не обработаются. А у тех, что успели создаться до падения, `-Department $_.Department` подставит `$null`, потому что колонки `Department` в CSV нет (есть `Departament`) — отдел будет пустой.

**Диагностика по шагам.**

1. Спроси у Марии, какой именно скрипт она запускала и где лежит CSV. Попроси файл.
2. На DC01 открой CSV: `Get-Content "C:\Support\Tickets\new-users.csv" -TotalCount 1`. Смотри заголовки — видишь `Departament` вместо `Department`. Это причина пустых отделов.
3. Проверь, какие учётки реально создались: `Get-ADUser -Filter * -SearchBase "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab" | Select-Object Name, SamAccountName, Department`. Видишь часть с пустым `Department`.
4. Проверь, есть ли `ivanov` в CSV: `Import-Csv ... | Where-Object Sam -eq "ivanov"`. Есть — вот причина падения.
5. Проверь, не осталось ли «полу-созданных» учёток: `Get-ADUser -Filter {Department -notlike "*"} -SearchBase "OU=Company,DC=corp,DC=vektor,DC=lab"`.

**Решение.**

1. Исправь CSV: заголовок `Departament` → `Department`, убери строку с `ivanov` (он уже есть).
2. Для уже созданных с пустым отделом — обнови атрибуты:

    ```powershell
    Import-Csv "C:\Support\Tickets\new-users.csv" | ForEach-Object {
        try {
            Set-ADUser -Identity $_.Sam -Department $_.Department -Title $_.Title -ErrorAction Stop
            Write-Host "Обновлён: $($_.Sam)"
        } catch {
            Write-Warning "Пропущен $($_.Sam): $($_.Exception.Message)"
        }
    }
    ```

3. Для тех, кто не создался из-за падения — запусти создание заново, но с `try/catch` и проверкой на существование:

    ```powershell
    Import-Csv "C:\Support\Tickets\new-users.csv" | ForEach-Object {
        if (-not (Get-ADUser -Filter "SamAccountName -eq '$($_.Sam)'" -ErrorAction SilentlyContinue)) {
            # ... New-ADUser ...
        } else {
            Write-Host "Уже есть: $($_.Sam)"
        }
    }
    ```

4. В следующий раз — всегда `-WhatIf` перед запуском и `try/catch` внутри цикла.

**Ответ пользователю.** «Мария, разобрались. Причина — опечатка в заголовке CSV (`Departament` вместо `Department`) и строка с уже существующим сотрудником: скрипт падал на ней и не доходил до остальных. Исправили CSV, досоздали недостающие учётки, у существующих заполнили отдел и должность. Проверьте, пожалуйста, список — все ли стажёры на месте. На будущее: перед запуском скрипта присылайте CSV на проверку, мы прогоняем его с `-WhatIf`».

**Закрытие заявки.** Категория: «AD / Учётные записи / Массовое создание». Решение: исправление CSV, досоздание учёток, обновление атрибутов. Время: ~40 минут. Профилактика: в базе знаний — памятка «Формат CSV для массового создания учёток» с обязательным `-WhatIf`.

### Что изменилось на стенде

- На DC01 созданы доменные учётки `smirnov` (Склад, Кладовщик) и `kuznetsova` (HR, Менеджер по персоналу), обе с требованием смены пароля.
- Учётка `ivanov`: `Department` = «Склад», `Title` = «Логист», добавлена в `GG-Sklad`, удалена из `GG-Buh`.
- На DC01 в `C:\Support\Tickets` появились файлы `new-users.csv`, `reset-list.txt`, `ad-script-test.log`.
- Пароли `smirnov` и `kuznetsova` сброшены на `Lab#2026-vektor` с требованием смены.
- В `C:\Lab\vektor\stand-log.md` добавлена запись об уроке.
- Новых ВМ и ролей нет.

### Откат

Через GUI VMware Workstation: правый клик по DC01 → Snapshot Manager → выбери `before-l1-078` → Revert. Через `vmrun` с хоста:

```powershell
vmrun -T ws revertToSnapshot "C:\Lab\vektor\VMs\DC01\DC01.vmx" before-l1-078
```

Если хочешь сохранить результат лабораторной, но откатить только «сломанное» — не откатывай снапшот, а вручную удали лишние учётки: `Remove-ADUser -Identity smirnov -Confirm:$false` (и так же `kuznetsova`), верни `ivanov` в `GG-Buh` и убери из `GG-Sklad`.

## 6. Ошибки новичков и безопасность

1. **Запуск без `-WhatIf`.** Ошибка: сразу `New-ADUser`/`Set-ADUser` на пачке. К чему приводит: одна опечатка в CSV — и 50 учёток созданы не в том OU или с пустым отделом. Правильно: всегда сначала `-WhatIf`, смотри план, потом запускай.
2. **Нет `try/catch` в цикле.** Ошибка: скрипт падает на первой же проблемной строке. К чему приводит: половина обработана, половина нет, состояние непонятное. Правильно: `try/catch` внутри `ForEach-Object`, ошибки — в лог, цикл продолжается.
3. **Кириллица в CSV без `-Encoding UTF8`.** Ошибка: сохранил CSV в ANSI. К чему приводит: «Иванов» превращается в «Ð˜Ð²Ð°Ð½Ð¾Ð²», учётки с кракозябрами. Правильно: `Set-Content -Encoding UTF8` и проверка `Get-Content` перед запуском.
4. **`Remove-ADUser` без `-WhatIf` и без бэкапа.** Ошибка: «удалю-ка тестового». К чему приводит: учётка удалена, корзина AD может быть не включена, восстановление — из бэкапа DC01 (L1 №48). Правильно: сначала `Disable-ADAccount`, потом через неделю — удаление; и всегда `-WhatIf`.
5. **Пароль в скрипте открытым текстом.** Ошибка: `$pwd = "Lab#2026-vektor"`. К чему приводит: пароль в истории PowerShell, в логах, в Git. Правильно: `Read-Host -AsSecureString` или `ConvertTo-SecureString` из защищённого файла; в рабочей среде — вообще не хранить пароли в скриптах.
6. **Запуск от доменного админа «на всякий случай».** Ошибка: скрипт для смены телефона запускается под `adm.student`. К чему приводит: если в скрипте ошибка — она снесёт что угодно. Правильно: минимально необходимые права, для большинства операций с пользователями хватает делегирования на OU.
7. **Нет лога.** Ошибка: запустил, что-то произошло, а что — неизвестно. К чему приводит: при разборе инцидента нечего показать. Правильно: `Start-Transcript` + `Write-Host`/`Write-Warning` с логином в каждой строке.

**Безопасность.** В рабочей среде скрипты, меняющие AD, — это изменение учётных записей сотрудников, то есть обработка персональных данных (152-ФЗ, L1 №67). Нельзя: хранить пароли в скриптах и в Git, запускать массовые операции без согласования с владельцем процесса (HR/ИБ), логировать пароли в транскрипт (PowerShell маскирует `SecureString`, но не строки). На стенде пароль `Lab#2026-vektor` — учебный; в реальной работе такой пароль на всех учётках был бы грубейшим нарушением.

## 7. Вопросы на собеседовании

**Вопрос 1.** «Как бы вы массово создали 50 учёток в AD?»
**Ответ.** Через CSV и PowerShell: готовлю таблицу с колонками `FirstName, LastName, Sam, Department, Title`, кладу в шару, на DC01 запускаю скрипт с `Import-Csv` и `ForEach-Object`, внутри — `New-ADUser` с `-Path` по OU, `-AccountPassword` через `ConvertTo-SecureString`, `-ChangePasswordAtLogon $true`. Сначала прогоняю с `-WhatIf`, проверяю план, потом реально. В цикле — `try/catch`, чтобы одна плохая строка не роняла всё. Логирую через `Start-Transcript`.
**Что хочет услышать интервьюер.** Что ты знаешь `Import-Csv`, `New-ADUser`, `-WhatIf`, `try/catch` и понимаешь риск массовой операции.
**Красный флаг.** «Напишу цикл и запущу сразу, потом посмотрю» — без `-WhatIf` и без лога.

**Вопрос 2.** «Чем `Set-ADUser` отличается от `New-ADUser` и когда что использовать?»
**Ответ.** `New-ADUser` создаёт новый объект, `Set-ADUser` меняет атрибуты существующего. Если запустить `New-ADUser` для существующего `SamAccountName` — ошибка «объект уже существует». Поэтому в скриптах для повторного запуска сначала проверяю `Get-ADUser -Filter "SamAccountName -eq '...'"` и только если пусто — создаю, иначе — `Set-ADUser`.
**Что хочет услышать интервьюер.** Понимание идемпотентности и типовой ошибки «уже существует».
**Красный флаг.** «Не знаю, они одинаковые».

**Вопрос 3.** «Пользователь заблокирован, пароль истёк. Как разблокировать и сбросить пароль скриптом?»
**Ответ.** `Unlock-ADAccount -Identity <логин>` снимает блокировку, `Set-ADAccountPassword -Identity <логин> -NewPassword $pwd -Reset` сбрасывает пароль без старого, `Set-ADUser -Identity <логин> -ChangePasswordAtLogon $true` заставляет сменить при входе. Для поиска всех заблокированных — `Search-ADAccount -LockedOut`. Всё это можно завернуть в цикл по списку из файла с `try/catch`.
**Что хочет услышать интервьюер.** Знание трёх командлетов и порядка действий.
**Красный флаг.** «Сброшу через ADUC мышкой» — не отвечает на вопрос про скрипт.

**Вопрос 4.** «Как вы защитите скрипт от ошибки, которая снесёт не то?»
**Ответ.** Три уровня: `-WhatIf` перед запуском (показывает план), `try/catch` в цикле (одна ошибка не роняет всё), `Start-Transcript` (лог всех действий). Плюс — запуск под учёткой с минимальными правами (делегирование на OU, а не доменный админ). Плюс — снапшот/бэкап DC01 перед массовой операцией.
**Что хочет услышать интервьюер.** Что ты думаешь о последствиях, а не только о «чтобы работало».
**Красный флаг.** «Запущу от админа, ничего не случится».

**Вопрос 5.** «Где взять модуль `ActiveDirectory`, если его нет?»
**Ответ.** На Windows Server — установить роль AD DS (модуль идёт с ней) или компонент RSAT-AD-PowerShell. На Windows 10/11 — установить RSAT: `Add-WindowsCapability -Online -Name Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0` [ПРОВЕРИТЬ имя компонента в твоей версии Windows]. После установки — `Import-Module ActiveDirectory`.
**Что хочет услышать интервьюер.** Что ты знаешь про RSAT и не путаешь клиент и сервер.
**Красный флаг.** «Скачаю с интернета» — в рабочей среде это недопустимо.

## 8. Источники (официальная документация)

- Microsoft Learn (learn.microsoft.com), раздел «Active Directory Domain Services» → «Active Directory PowerShell»: поисковая фраза `Active Directory PowerShell module cmdlets`.
- Microsoft Learn, статья «New-ADUser»: поисковая фраза `New-ADUser PowerShell`.
- Microsoft Learn, статья «Set-ADUser»: поисковая фраза `Set-ADUser PowerShell`.
- Microsoft Learn, статья «Import-Csv»: поисковая фраза `Import-Csv PowerShell`.
- Microsoft Learn, статья «about_CommonParameters» (про `-WhatIf`, `-Confirm`): поисковая фраза `about_CommonParameters WhatIf Confirm`.
- Microsoft Learn, статья «Start-Transcript»: поисковая фраза `Start-Transcript PowerShell`.
- Microsoft Learn, статья «Search-ADAccount»: поисковая фраза `Search-ADAccount LockedOut PasswordExpired`.
- Документация VMware Workstation (docs.vmware.com), раздел про снапшоты и `vmrun`: поисковая фраза `vmrun snapshot revertToSnapshot Workstation` [ПРОВЕРИТЬ синтаксис в твоей версии].
