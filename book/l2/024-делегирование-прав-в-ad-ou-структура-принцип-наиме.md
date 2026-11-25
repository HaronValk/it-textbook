# Делегирование прав в AD: OU-структура, принцип наименьших привилегий

## 1. Введение (зачем это нужно в реальной работе)

Тикет в вашей учебной компании «Вектор»:

> **Пользователь:** Петрова Мария (`petrova`, HR, WIN10-CL02)
> **Что написала:** «Пришёл новый сотрудник на склад, надо завести ему учётку, и у Сидорова поменялся телефон — поправьте в карточке. Я каждый раз пишу вам и жду полдня. Может, дадите мне права? Мне бы только по HR и складу, пароли я трогать не буду».
> **Срочность:** средняя.

Дальше два пути. Первый — отдать Марии пароль `adm.student` (и через месяц получить «я случайно удалила бухгалтерию»). Второй — выдать ей ровно те права, которые нужны для работы, и ни граммом больше. Второй путь и называется **делегированием прав**: вы передаёте часть административных полномочий другому человеку или группе, ограничив их конкретной веткой каталога и конкретными операциями.

Это одна из самых частых задач L2-инженера. Тысячи компаний живут по схеме «хелпдеск сбрасывает пароли, HR заводит людей, склад правит членство в своих группах», и всё это держится на правах в AD. Одновременно это и самая частая причина инцидентов безопасности: «Full Control на весь домен, потому что так быстрее».

После этой главы ты должен уметь: читать существующие права на объекты AD; выдавать права через мастер делегирования и вручную через ACL; проверять, что права применились и именно там; аудировать их использование; и объяснить заказчику, почему «дайте мне все права, я осторожно» — не вариант.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

У каждого объекта AD (домен, OU, пользователь, группа, компьютер, GPO) есть атрибут `nTSecurityDescriptor` — **дескриптор безопасности**. Внутри четыре части:

- **Owner** — владелец объекта (может всегда переписать права);
- **Group** — исторический атрибут, в проверке доступа почти не участвует;
- **DACL** (Discretionary ACL) — список разрешений: кто что может делать. Именно его мы и правим;
- **SACL** (System ACL) — список того, что надо логировать (аудит). Читается только системой.

DACL состоит из **ACE** (Access Control Entry) — записей вида «кому / разрешить или запретить / что именно / к каким дочерним объектам применить / наследуется ли». ACE бывают простые (общие права: чтение, запись, Full Control) и **объектные** (object-specific) — те, что указывают на конкретное свойство или конкретное действие. Объектных два вида:

- **property rights** — права на конкретный атрибут: «читать телефон», «писать `telephoneNumber`»;
- **extended rights** (control access rights) — права на операцию: «сбросить пароль», «сменить пароль», «переместить объект», «реплицировать изменения каталога».

Проверка доступа происходит на контроллере домена в процессе `lsass.exe` (внутри него живёт NTDS): DC берёт SID-ы из билета Kerberos пользователя (PAC), суммирует все ACE «Allow», вычитает «Deny» — и решает. Deny всегда побеждает. Права выданы, но у человека старый билет — он их пока не увидит: нужно снятие нового TGT (`klist purge` или перелогин) для групп, а вот изменения ACL действуют сразу, потому что ACE ссылается на SID группы, который в токене уже есть.

Ключевая для нашей темы вещь — **наследование**. ACE, поставленный на OU с флагом «наследовать дочерним объектам», автоматически появляется в DACL всех объектов внутри OU. Правишь один раз наверху — работает внизу.

```text
DC=corp,DC=vektor,DC=lab
│  DACL домена: Domain Admins / Enterprise Admins / SYSTEM = Full Control
│  ← сюда НИКОГДА не делегируем рабочие задачи
└── OU=Company
    ├── OU=Бухгалтерия    ← ACE: VEKTOR\GG-IT  ExtendedRight Reset Password
    │   │                    + Write pwdLastSet, наслед. "Descendant User objects"
    │   └── ivanov         ← ACE унаследован, в его DACL стоит IsInherited=True
    ├── OU=HR             ← ACE: VEKTOR\GG-HR  Create/Delete user + Read/Write all props
    │   └── petrova
    ├── OU=Склад           ← ACE нет: sidorov недоступен для хелпдеска
    └── OU=IT
OU=Компьютеры   OU=Серверы   (отдельные ветки, свои ACL)
```

Отдельная ловушка — **adminSDHolder** и процесс **SDProp**. Для «защищённых групп» (Domain Admins, Enterprise Admins, Administrators, Account Operators, Backup Operators, Print Operators, Server Operators, Replicator, Schema Admins, Group Policy Creator Owners и др.) раз в 60 минут SDProp копирует эталонный DACL с контейнера `CN=AdminSDHolder,CN=System,DC=...` на сами объекты и **принудительно отключает наследование**. У таких учёток `adminCount=1`. Итог: делегировал хелпдеску сброс паролей на OU, а у «своего» пользователя, который когда-то побывал в Backup Operators, сброс не работает — ACE сняли. Это нормальное поведение, а не баг.

И чтобы сразу снять путаницу в терминах: **делегирование прав** (то, что мы делаем) и **Kerberos delegation** (unconstrained/constrained delegation — передача учётных данных службы службе) — разные вещи, хотя звучат одинаково. На собеседовании это любят.

Порты, если спросят про путь: LDAP 389/636 (TCP), Kerberos 88, SMB 445, RPC — а сама проверка прав происходит внутри DC, по сети ничего не летит.

## 3. Аналогия из жизни

Бизнес-центр с пропускной системой. Есть генеральный ключ директора — от всего здания, включая щитовую и серверную (Domain Admin). Есть ключ от этажа (делегирование на OU). Есть отдельный доступ к сейфу в кабинете (extended right «сбросить пароль»). Есть журнал на вахте, куда пишут, кто и когда заходил (SACL и события 4662/4724).

Уборщица получает ключ от своего этажа — и только от него: в бухгалтерию её ключ не подходит, хотя она честный сотрудник. Хорошая пропускная система никогда не выдаёт ключ «от всего здания, потому что человек свой» — она выдаёт ровно тот ключ, который нужен для работы, и забирает его в день увольнения.

Аналогия ломается в одном месте: в реальном мире ключ от кабинета не даёт права поменять замок. В AD право «полный доступ» (Full Control) включает WriteDACL и WriteOwner — то есть человек может переписать права под себя и выкинуть оттуда тебя.

## 4. Практика и команды

Все примеры — на стенде «Вектор». Работаем с DC01, в консоли PowerShell от `adm.student`. Модуль `ActiveDirectory` на DC01 уже есть.

**1. Инвентаризация OU — с чего всегда начинать.**

```powershell
Get-ADOrganizationalUnit -Filter * -SearchBase "DC=corp,DC=vektor,DC=lab" |
    Select-Object Name, DistinguishedName | Sort-Object DistinguishedName
```

`-SearchBase` ограничивает поиск ветвью. В выводе — имена OU и их полные DN; DN потом понадобится для всех остальных команд.

**2. Где реально лежит пользователь (частая причина «а права не работают»).**

```powershell
Get-ADUser ivanov, petrova, sidorov | Select-Object SamAccountName, DistinguishedName
```

Смотрим DN целиком: если `petrova` лежит в `OU=HR`, то никакое делегирование на `OU=Бухгалтерия` её не касается.

**3. Просмотр DACL: GUI.** `dsa.msc` → меню **Вид → Дополнительные компоненты (Advanced Features)** → правый клик по OU → **Свойства → вкладка Безопасность → Дополнительно**. Здесь видно: `Тип` (Allow/Deny), `Унаследовано от`, `Применяется к` (`Descendant User objects` и т.п.). Без «Дополнительных компонентов» вкладки «Безопасность» на OU не будет.

**4. Просмотр DACL: `dsacls` (родная утилита AD DS).**

```cmd
dsacls "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab"
```

Сокращённый вывод:

```text
Access list:
    Permissions for VEKTOR\Domain Admins
    Full Control
    Permissions for NT AUTHORITY\SYSTEM
    Full Control
    Permissions for VEKTOR\GG-IT
    Special access
        RESET PASSWORD
        WRITE PDL
The command completed successfully.
```

`dsacls` показывает и унаследованные, и явные ACE. Ключи, которые реально нужны: `/I:S` — применить к объекту и всему поддереву (наследование вниз) `[ПРОВЕРИТЬ различие флагов T/S/P в справке `dsacls /?`]`; `/G trustee:права` — выдать; `/R trustee` — снять все ACE указанного принципала; `/D` — запретить (используем почти никогда).

**5. Просмотр DACL: PowerShell, с типами прав.**

```powershell
(Get-Acl "AD:\OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab").Access |
    Format-List IdentityReference, ActiveDirectoryRights, AccessControlType,
                IsInherited, InheritanceType, ObjectType, InheritedObjectType
```

`AD:\` — диск провайдера ActiveDirectory для PowerShell; `Get-Acl` по нему читает дескриптор прямо из каталога. Сокращённый вывод выданного хелпдеску права:

```text
IdentityReference   : VEKTOR\GG-IT
ActiveDirectoryRights : ExtendedRight
AccessControlType   : Allow
IsInherited         : False
InheritanceType     : Descendents
ObjectType          : 00299570-246d-11d0-a768-00aa006e0529
InheritedObjectType : bf967aba-0de6-11d0-a285-00aa003049e2
```

`ObjectType` = GUID права «Reset Password» (`User-Force-Change-Password`), `InheritedObjectType` = класс «user» `[ПРОВЕРИТЬ GUID по документации перед использованием в скриптах]`. `IsInherited=False` — ACE поставлен здесь, а не приехал сверху.

**6. Только то, что выдано руками (для отчёта и разбора).**

```powershell
(Get-Acl "AD:\OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab").Access |
    Where-Object { -not $_.IsInherited } |
    Select-Object IdentityReference, ActiveDirectoryRights, InheritanceType, ObjectType
```

**7. Мастер делегирования (GUI, 90% рабочих задач).** `dsa.msc` → правый клик по OU → **Delegate Control…** → **Add** → `GG-IT` → **Next** → выбрать задачу. В списке есть готовые: `Create, delete, and manage user accounts`, `Reset user passwords and force password change at next logon`, `Read all user information`, `Modify the membership of a group`, `Join a computer to the domain`, `Manage Group Policy links`. Именно «Reset user passwords…» ставит extended right «Reset Password» + право записи `pwdLastSet` на дочерние user-объекты.

**8. Ручное делегирование конкретных атрибутов.** Свойства OU → **Безопасность → Дополнительно → Добавить** → выбрать принципала → **Тип: Разрешить** → **Применяется к: Потомки — объекты пользователей** → галочки `Read telephoneNumber`, `Write telephoneNumber`, `Read title`, `Write title`. Так HR правит карточки, но не может менять пароли и членство в группах.

**9. Проверка эффективных прав.** Безопасность → Дополнительно → вкладка **Действующий доступ (Effective Access)** → выбрать пользователя/группу → `Просмотр`. Показывает итог с учётом всех групп `[ПРОВЕРИТЬ наличие вкладки в вашей сборке — в свежих билдах Windows Server 2022 она есть]`.

**10. Выдача/снятие простого права через `dsacls`.**

```cmd
dsacls "OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab" /I:S /G "VEKTOR\GG-HR:GR"
dsacls "OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab" /R "VEKTOR\GG-LEGACY"
```

`GR` = Generic Read (чтение всех свойств), `/I:S` — на поддерево. Второй командой снимаются **все** ACE указанного принципала на объекте — удобно при уборке после увольнения, но сначала посмотри, что там было.

**11. Токен и группы: почему права «не видны».**

```powershell
Add-ADGroupMember -Identity GG-IT -Members ivanov
whoami /groups | Select-String "GG-IT"
klist purge
```

`whoami /groups` показывает SID-ы в текущем токене. Новая группа попадёт в токен только после нового входа/Kerberos-билета: `klist purge` чистит билеты, следующее обращение к сети берёт свежий TGT с обновлённым PAC. Ещё вариант — `gpupdate /force` и перелогин, но именно билет — корень проблемы.

**12. Найти «защищённые» учётки, на которых делегирование не работает.**

```powershell
Get-ADUser -Filter * -Properties adminCount |
    Where-Object adminCount -eq 1 | Select-Object SamAccountName, DistinguishedName
```

**13. Аудит: кто пользуется выданными правами.**

```powershell
auditpol /get /subcategory:"Directory Service Access"
auditpol /set /subcategory:"Directory Service Access" /success:enable /failure:enable
```

`auditpol /get` вернёт строку вида `Directory Service Access   No Auditing`. Включать правильно не через `auditpol`, а через GPO: `Default Domain Controllers Policy` → Computer Configuration → Policies → Windows Settings → Security Settings → Advanced Audit Policy Configuration → Audit Policies → **DS Access → Audit Directory Service Access** → Success and Failure, затем `gpupdate /force` на DC01. Дальше на OU добавляется **SACL** (Свойства → Безопасность → Дополнительно → Аудит) — и только после этого в Security-логе появятся события 4662 (операция над объектом каталога). События 4720/4724/4725/4726/4738 пишутся и без SACL — это аудит управления учётными записями.

**14. Чтение событий.**

```powershell
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4720,4724,4725,4726} -MaxEvents 20 |
    Select-Object TimeCreated, Id, @{n='Msg';e={($_.Message -split "`n")[0]}}
```

4720 — создан пользователь, 4724 — попытка сброса пароля, 4725/4726 — отключён/удалён, 4728/4732 — добавление в группу (global/local).

**15. Отчёт по всему делегированию — раз в квартал.**

```powershell
Get-ADOrganizationalUnit -Filter * -SearchBase "DC=corp,DC=vektor,DC=lab" | ForEach-Object {
    $dn = $_.DistinguishedName
    (Get-Acl "AD:\$dn").Access | Where-Object { -not $_.IsInherited } | ForEach-Object {
        [pscustomobject]@{
            OU        = $dn
            Trustee   = $_.IdentityReference
            Rights    = $_.ActiveDirectoryRights
            Type      = $_.AccessControlType
            AppliesTo = $_.InheritanceType
            ObjectType = $_.ObjectType
        }
    }
} | Export-Csv C:\Temp\delegation-report.csv -NoTypeInformation -Encoding UTF8
```

**16. Удаление OU с делегированием часто падает не из-за прав, а из-за защиты от случайного удаления:**

```powershell
Set-ADOrganizationalUnit -Identity "OU=Temp,OU=Company,DC=corp,DC=vektor,DC=lab" -ProtectedFromAccidentalDeletion $false
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны: **DC01** (AD DS, DNS, DHCP, домен `corp.vektor.lab`), **WIN11-CL01** (`ivanov`, Бухгалтерия), **WIN10-CL02** (`petrova`, HR), при наличии — **DC02** из урока 17 для проверки репликации ACL. OU-структура `Company → Бухгалтерия/HR/Склад/IT`, группы `GG-IT`, `GG-HR`, пользователи размещены по своим OU. Если структуры нет — сначала пройди модуль по Active Directory.

ВНИМАНИЕ, учебный костыль: чтобы проверить делегирование, нам нужен непривилегированный «хелпдеск». На стенде нет отдельного IT-сотрудника, поэтому мы **временно** добавим `ivanov` в `GG-IT`. В боевой среде так делать нельзя: под одной учёткой не смешивают рабочую роль бухгалтера и админские полномочия (подробно — урок 25 про тиринг).

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-l2-024`. Машины: **DC01**, **DC02** (если есть), **WIN11-CL01**, **WIN10-CL02**. Снапшот делать при выключенной ВМ или в состоянии паузы — так меньше шансов получить «побитый» диск.

### Шаги

1. **DC01.** Инвентаризация: `Get-ADOrganizationalUnit -Filter * -SearchBase "DC=corp,DC=vektor,DC=lab" | Select Name, DistinguishedName` и `Get-ADUser ivanov, petrova, sidorov | Select SamAccountName, DistinguishedName`.
   *Ожидаемо:* видишь все четыре OU и точные DN пользователей. Запиши DN в блокнот.

2. **DC01.** Снять baseline ACL, чтобы потом было с чем сравнивать:
   ```cmd
   mkdir C:\Temp
   dsacls "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab" > C:\Temp\acl-buh-before.txt
   dsacls "OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab" > C:\Temp\acl-hr-before.txt
   ```
   *Ожидаемо:* два текстовых файла с текущим списком доступа.

3. **DC01.** `dsa.msc` → **Вид → Дополнительные компоненты** → свойства `OU=Бухгалтерия` → **Безопасность → Дополнительно**. Разберись, какие ACE на объекте (`Унаследовано от` пусто) и какие приехали сверху.
   *Ожидаемо:* явные ACE от Domain Admins/Enterprise Admins/SYSTEM, остальные — унаследованные от домена и `Company`.

4. **DC01.** Делегирование для хелпдеска: правый клик по `OU=Бухгалтерия` → **Delegate Control…** → **Add** → `VEKTOR\GG-IT` → отметь **две** задачи: `Reset user passwords and force password change at next logon` и `Read all user information` → Finish.
   *Ожидаемо:* мастер завершился без ошибок.

5. **DC01.** Проверка: повтори `dsacls` и
   ```powershell
   (Get-Acl "AD:\OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,lab".Replace(",lab"»,",lab»"))  # см. шаг 5a
   ```
   Проще так:
   ```powershell
   (Get-Acl "AD:\OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab").Access |
       Where-Object { $_.IdentityReference -like "*GG-IT*" } |
       Format-Table ActiveDirectoryRights, IsInherited, InheritanceType, ObjectType
   ```
   *Ожидаемо:* две-три записи для `VEKTOR\GG-IT` с `IsInherited=False`, `InheritanceType=Descendents`, в `ObjectType` — GUID права сброса пароля.

6. **DC01.** Делегирование для HR: свойства `OU=HR` → Безопасность → Дополнительно → Добавить → принципал `VEKTOR\GG-HR` → Тип `Разрешить` → **Применяется к: Потомки — объекты пользователей** → отметь `Create user objects`, `Delete user objects`, `Read all properties`, `Write all properties` → ОК.
   *Ожидаемо:* ACE появились, наследование вниз включено.

7. **DC01.** `Add-ADGroupMember -Identity GG-IT -Members ivanov` и проверка `Get-ADGroupMember GG-IT | Select Name`.
   *Ожидаемо:* `ivanov` в составе `GG-IT`.

8. **WIN11-CL01.** Войди как `VEKTOR\ivanov`. Установи RSAT только с AD-модулем:
   ```powershell
   Add-WindowsCapability -Online -Name "Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0"
   klist purge
   Import-Module ActiveDirectory
   whoami /groups | Select-String "GG-IT"
   ```
   *Ожидаемо:* capability `Installed`, в выводе `whoami` появилась строка с `VEKTOR\GG-IT`. Если строки нет — перелогинься (новый вход заново собирает токен).

9. **WIN11-CL01, позитивный тест.** Сбрасываем пароль пользователю Бухгалтерии (на стенде это сам `ivanov`, ставим тот же учебный пароль):
   ```powershell
   $p = ConvertTo-SecureString 'Lab#2026-vektor' -AsPlainText -Force
   Set-ADAccountPassword -Identity ivanov -Reset -NewPassword $p
   ```
   *Ожидаемо:* команда выполнилась молча, без ошибки. Право сработало.

10. **WIN11-CL01, негативный тест (проверка границ).**
    ```powershell
    Set-ADAccountPassword -Identity petrova -Reset -NewPassword $p
    ```
    *Ожидаемо:* `Set-ADAccountPassword : Access is denied` — `petrova` в OU=HR, туда делегирование не распространяется. Так и должно быть.

11. **WIN10-CL02.** Войди как `VEKTOR\petrova`, поставь RSAT тем же `Add-WindowsCapability`, затем:
    ```powershell
    New-ADUser -Name "tmp-deleg-01" -SamAccountName "tmp-deleg-01" -Path "OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab"
    Set-ADUser -Identity tmp-deleg-01 -Title "Кладовщик"
    New-ADUser -Name "tmp-deleg-02" -Path "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab"
    ```
    *Ожидаемо:* первая команда — успех (HR может создавать в своей OU), вторая — успех (может править атрибуты), третья — `Access is denied` (Бухгалтерия не её территория). Дополнительно попробуй сбросить пароль `tmp-deleg-01`: скорее всего получишь отказ — право «Reset Password» отдельное и мы его HR не давали. Если вдруг сработает — запиши наблюдение и разберись по ACE, какие именно права дал пункт «Write all properties».

12. **DC01.** Включи аудит и проверь его. В `gpmc.msc` открой `Default Domain Controllers Policy` → Edit → Computer Configuration → Policies → Windows Settings → Security Settings → Advanced Audit Policy Configuration → Audit Policies → **DS Access → Audit Directory Service Access** = Success and Failure. На DC01: `gpupdate /force`. Затем на `OU=Бухгалтерия` → Безопасность → Дополнительно → **Аудит** → Добавить → `Everyone` → Success, Failure → Применяется к: Потомки — объекты пользователей → `Write all properties`, `Delete`.
    Повтори шаг 9 и посмотри:
    ```powershell
    Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4724} -MaxEvents 5 | Select TimeCreated, Id
    ```
    *Ожидаемо:* событие 4724 с субъектом `ivanov` и целью `ivanov`.

13. **DC02 (если развёрнут в уроке 17).** Проверь, что ACL доехал репликацией:
    ```powershell
    dsacls "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab" | Select-String "GG-IT"
    ```
    *Ожидаемо:* та же запись. Если нет — `repadmin /replicate DC02 DC01 "DC=corp,DC=vektor,DC=lab"` и посмотри `repadmin /showrepl`.

14. **DC01.** Собери отчёт (команда №15 из раздела 4) в `C:\Temp\delegation-report.csv`, удали временного пользователя `Remove-ADUser tmp-deleg-01 -Confirm:$false`, убери `ivanov` из `GG-IT`: `Remove-ADGroupMember -Identity GG-IT -Members ivanov`. Запиши всё в `stand-log.md`.

### Проверка

- [ ] На `OU=Бухгалтерия` есть не менее двух не унаследованных ACE для `VEKTOR\GG-IT` с `InheritanceType = Descendents`.
- [ ] `ivanov` может сбросить пароль пользователю из `OU=Бухгалтерия` и **не может** — из `OU=HR` и `OU=Склад`.
- [ ] `petrova` может создать пользователя в `OU=HR` и **не может** в `OU=Бухгалтерия`.
- [ ] В `C:\Temp\delegation-report.csv` есть строки по обеим OU, и `Domain Admins` там не «размножен» по всей ветке.
- [ ] В Security-логе есть 4724 и (при включённом SACL) 4662 по `OU=Бухгалтерия`.
- [ ] Временный `tmp-deleg-01` удалён, `ivanov` убран из `GG-IT`, в `stand-log.md` записаны изменения.

### Сломай и почини

> **Пользователь:** `ivanov` / WIN11-CL01
> **Что написал:** «Не могу сбросить пароль новому сотруднику `tmp-deleg-01` — «Доступ запрещён». У Петровой и у себя сбрасывается, а у него нет. Человек ждёт, завтра первый рабочий день».
> **Срочность:** высокая.

**Как воспроизвести поломку.** На шаге 11 `petrova` создала `tmp-deleg-01` в `OU=HR` (её зона). Хелпдеск из `GG-IT` имеет права только на `OU=Бухгалтерия`. Всё «сломано» тем, что объект лежит не в той OU.

**Диагностика по шагам.**
1. Уточняем у пользователя точное имя объекта: `tmp-deleg-01`.
2. Смотрим, что за объект и где он: `Get-ADUser tmp-deleg-01 | Select DistinguishedName` → `CN=tmp-deleg-01,OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab`. Вот и корень.
3. Проверяем, что права хелпдеска на месте и никуда не делись: `Get-Acl` по `OU=Бухгалтерия`, фильтр `GG-IT`.
4. Проверяем, что у `ivanov` в токене есть `GG-IT` (на CL01: `whoami /groups`). Если нет — не права виноваты, а старый билет, лечится `klist purge` + перелогин.
5. Проверяем `adminCount` у проблемного объекта: `Get-ADUser tmp-deleg-01 -Properties adminCount | Select adminCount`. Если `1` — мы в сценарии adminSDHolder, и никакое делегирование на OU не поможет.
6. Только после этого делаем вывод: объект вне зоны делегирования.

**Решение.** Переместить объект в правильную OU силами администратора: `Move-ADObject -Identity "CN=tmp-deleg-01,OU=HR,OU=Company,DC=corp,DC=vektor,DC=lab" -TargetPath "OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab"`. После перемещения ACE наследуется автоматически (наследование на новом родителе включено), и сброс пароля у `ivanov` проходит. Если перемещать нельзя — выдать точечное право на конкретный объект, но это плохая практика: точечные ACE забываются и не аудируются.

**Ответ пользователю:** «Проблема была не в ваших правах: учётка нового сотрудника оказалась создана в подразделении HR, а ваши полномочия действуют в подразделении Бухгалтерия. Я перенёс учётку в правильное подразделение, попробуйте сбросить пароль ещё раз — должно пройти. И на будущее: если новый человек не сбрасывается, скажите мне его логин, я проверю, где он лежит».

**Закрытие заявки.** Категория «AD → Учётные записи → Права доступа». В решении: причина (объект вне области делегирования), действие (Move-ADObject), профилактика (HR создаёт сотрудников строго в OU своего отдела; в шаблоне заявки на создание учётки появилось поле «подразделение»).

### Что изменилось на стенде

- `OU=Бухгалтерия,OU=Company,DC=corp,DC=vektor,DC=lab`: ACE для `VEKTOR\GG-IT` — extended right «Reset Password» + write `pwdLastSet` (наследуются на объекты-потомки класса user) и «Read all user information».
- `OU=HR,OU=Company,...`: ACE для `VEKTOR\GG-HR` — create/delete user objects, read/write all properties, наследуются вниз.
- `GG-IT`: `ivanov` временно добавлен и удалён (по факту — вернулись в исходное состояние).
- `tmp-deleg-01` создан в `OU=HR` и удалён; на время лабораторной побывал в `OU=Бухгалтерия`.
- `DC01`: в `Default Domain Controllers Policy` включён аудит DS Access (Success+Failure); на `OU=Бухгалтерия` добавлен SACL.
- `WIN11-CL01` и `WIN10-CL02`: установлен компонент RSAT `Rsat.ActiveDirectory.DS-LDS.Tools`.
- Файлы: `C:\Temp\acl-buh-before.txt`, `C:\Temp\acl-hr-before.txt`, `C:\Temp\delegation-report.csv`.

### Откат

Если что-то пошло не так: выключи ВМ → **VM → Snapshot → Snapshot Manager** → выбери `before-l2-024` → **Go To** (или **Revert**). Восстанавливай все машины, которые трогал (DC01, DC02, CL01, CL02), иначе получишь рассинхрон: на DC01 права есть, на CL01 — уже нет. После отката обязательно обнови запись в `stand-log.md`: что откатили и почему.

## 6. Ошибки новичков и безопасность

1. **Full Control на OU вместо конкретных задач.** → Хелпдеск получает `WriteDACL` и `WriteOwner`: может переписать права, выкинуть администратора, создать компьютер и зарегистрировать его в домене. → Выдавай только нужные пункты мастера делегирования; Full Control — только администраторам.
2. **Делегирование личному пользователю, а не группе.** → Человек уволился/перешёл в другой отдел — права остались, а вспомнить о них некому. → Всегда выдаём права группе-роли (`GG-IT`, `GG-HR`), людей двигаем в группах.
3. **Делегирование на корень домена или на `OU=Company`.** → Права неявно расползаются на все отделы, легко выйти за рамки. → Самая узкая OU, которая решает задачу.
4. **Deny для «ограничения».** → Deny приоритетнее любого Allow, его источник трудно найти, наследование утаскивает его вниз. Права «отобрали» и никто не понимает почему. → Не выдавай лишнего, вместо Deny — просто не выдавай Allow.
5. **ACE «Только этот объект» вместо «Потомки — объекты пользователей».** → Право есть на самой OU, но не на пользователях внутри: в ADUC на пользователе всё «серое», операция падает. → Всегда проверяй столбец «Применяется к» и `InheritanceType`.
6. **Отключённое наследование на дочерней OU.** → «Мы же дали права, почему не работает?» — потому что на OU ниже стоит галка «Заблокировать наследование». → Проверяй `IsInherited` на конкретном объекте, а не только на родителе.
7. **Ждать делегирования на защищённых учётках.** → `adminCount=1` (бывшие/действующие члены Domain Admins, Backup Operators и др.) — SDProp снимает наследование, права не действуют. → Проверяй `adminCount`, для таких учёток — только админский путь.
8. **Нет аудита.** → После инцидента нельзя доказать, кто создал учётку и кто сбросил пароль. → Включай DS Access auditing и SACL на чувствительных OU, но не на всём домене сразу — лог распухнет.

### Безопасность

- Право `Reset Password` фактически равно «войти под пользователем»: сброс + вход = доступ к его данным. Выдавай его минимальному кругу и только на OU, где реально работает хелпдеск.
- Никогда не делегируй `Replicating Directory Changes` и `Replicating Directory Changes All` — это путь к DCSync и краже хешей всего домена (подробно — урок 25).
- В боевой среде под админскими полномочиями не сидят с рабочей учётки. Отдельный аккаунт `adm.student` для админских задач и обычный — для почты и браузера; на стенде мы сознательно смешали это в шаге 7 и пометили как учебный костыль.
- Каждое расширение прав фиксируй в заявке: кто попросил, зачем, на какую OU, до какой даты. Делегирование без срока и владельца через год превращается в «а почему у склада права на Бухгалтерию?».

## 7. Вопросы на собеседовании

**1. Как выдать сотруднику поддержки право сбрасывать пароли только для бухгалтерии и не дать ничего лишнего?**

Ответ: через мастер делегирования в `dsa.msc` на OU `Бухгалтерия` — задачу «Reset user passwords and force password change at next logon» для группы хелпдеска. Мастер ставит два ACE: extended right `User-Force-Change-Password` и запись `pwdLastSet`, оба наследуются на объекты-потомки класса user. Никакого Full Control, никаких прав на OU=HR и на корень домена. Группа, а не человек; проверка — `Get-Acl`/`dsacls` и практический тест под непривилегированной учёткой.

*Что хочет услышать интервьюер:* понимание, что «сброс пароля» — это отдельное extended right, а не «изменение свойств», и что область действия задаётся OU.
*Красный флаг:* «дам Full Control на OU, там разберутся» или «дам права на весь домен, но попрошу не баловаться».

**2. Чем property rights отличаются от extended rights? Приведи примеры.**

Ответ: property rights — права на конкретный атрибут объекта (`telephoneNumber`, `title`, `manager`), extended rights (control access rights) — права на операцию, которая не сводится к записи одного атрибута: `Reset Password`, `Change Password`, `Move Tree`, `Replicating Directory Changes`, `Send As`. В ACL и то и другое выглядит как ACE с `ObjectType = <GUID>`, но для property rights GUID — это `schemaIDGUID` атрибута, для extended rights — `rightsGuid` из `CN=Extended-Rights,CN=Configuration`. Разница важна на практике: «Write all properties» не даёт сбросить пароль, а право «Reset Password» даёт, хотя пароль — тоже атрибут.

*Что хочет услышать интервьюер:* что человек реально читал ACL и знает про объектные ACE, а не только про галки в мастере.
*Красный флаг:* «это одно и то же, просто разные названия».

**3. Пользователь в нужной OU, права на OU выданы, но операция всё равно «Access denied». Где искать?**

Ответ: пять типовых причин. (1) Объект фактически лежит в другой OU — проверяем `DistinguishedName`. (2) На дочерней OU заблокировано наследование, ACE не доехал — смотрим `IsInherited` на самом объекте. (3) Учётка защищена: `adminCount=1`, adminSDHolder/SDProp снял наследование. (4) У пользователя старый Kerberos-билет без нужной группы — `whoami /groups` и `klist purge` с перелогином. (5) Стоит Deny-ACE или права выданы другой группе, а человек в неё не входит. Порядок: объект → его DACL → токен пользователя → тест.

*Что хочет услышать интервьюер:* системный порядок диагностики от объекта к токену, знание adminSDHolder.
*Красный флаг:* «перезагружу контроллер домена» или «сброшу права и выдам заново» без диагностики.

**4. Почему Full Control на OU — плохая идея, даже если человек «свой»?**

Ответ: Full Control включает `WriteDACL` и `WriteOwner` — обладатель может переписать ACL под себя, выдать права кому угодно и забрать права у администратора; плюс он может создавать объекты любых классов в этой ветке (включая компьютеры — а через них и Kerberos-атаки) и удалять их. Это нарушает принцип наименьших привилегий и делает аудит бессмысленным: невозможно доказать, кто что изменил. Правильно — точечные задачи мастера делегирования, группы-роли, регулярный отчёт по выданным ACE.

*Что хочет услышать интервьюер:* понимание, что ACL — тоже объект атаки, и что делегирование нужно сопровождать (отзыв прав при увольнении, отчёты).
*Красный флаг:* «Full Control удобнее, а риск небольшой».

**5. Чем делегирование прав в AD отличается от Kerberos delegation?**

Ответ: делегирование прав (AD permissions / delegation of control) — это записи в DACL объектов каталога: кто какие операции с объектами AD может делать. Kerberos delegation — свойство учётной записи службы (компьютера или сервисного аккаунта), позволяющее ей при обращении к следующему сервису предъявить билет от имени пользователя: unconstrained (без ограничений, опасно), constrained (список разрешённых сервисов, `msDS-AllowedToDelegateTo`), resource-based (настраивается на целевом ресурсе). Термины совпадают, механизмы完全不同 — первое про права в каталоге, второе про аутентификацию между службами.

*Что хочет услышать интервьюер:* что кандидат не путает два разных механизма и знает про опасность unconstrained delegation.
*Красный флаг:* «это одно и то же» / «Kerberos delegation — это когда админ даёт права пользователю».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Server → Active Directory Domain Services». Статьи: «Delegation of Control Wizard», «How Active Directory handles permissions / Access control», «Active Directory Security Descriptor», «AdminSDHolder and SDProp». Поиск: `Active Directory delegation of control best practices`, `adminSDHolder SDProp`.
- **learn.microsoft.com** — «dsacls» (справочник по синтаксису и флагам). Поиск: `dsacls syntax /I /G /R`.
- **learn.microsoft.com** — «Advanced Security Settings → Effective Access», «Advanced Audit Policy Configuration → DS Access». Поиск: `audit directory service access 4662 event`.
- **learn.microsoft.com** — «Audit account management events» (список событий 4720–4767). Поиск: `event 4724 password reset attempt`.
- **learn.microsoft.com** — «Kerberos Constrained Delegation Overview». Поиск: `kerberos constrained delegation msDS-AllowedToDelegateTo`.
- **docs.vmware.com / techdocs.broadcom.com** — справочник VMware Workstation по снапшотам `[ПРОВЕРИТЬ актуальный домен документации VMware после смены владельца продукта]`. Поиск: `VMware Workstation take snapshot revert snapshot manager`.

Всё, что помечено `[ПРОВЕРИТЬ]`, перед использованием в реальной работе сверь с официальной документацией и своей версией ОС.
