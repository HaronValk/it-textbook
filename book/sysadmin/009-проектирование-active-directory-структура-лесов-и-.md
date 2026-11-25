# Проектирование Active Directory: структура лесов и доменов, OU, сайты, политики, делегирование

## 1. Введение (зачем это нужно в реальной работе)

Тикет в пятницу вечером. **Пользователь:** Петрова (HR, WIN10-CL02). **Что написала:** «Алексей, добрый вечер. Завтра выходит новая сотрудница, нужен доступ к `\\FS01\HR`, и, пожалуйста, сделайте так, чтобы она не видела бухгалтерские папки — она у нас на полставки в двух отделах. И ещё: у неё вход в систему шёл четыре минуты, у меня столько же. Это нормально?» **Срочность:** выход на работу завтра в 9:00.

В этом тикете сразу три вещи из области проектирования AD: права на папки должны прийти из групп, а не «руками на папке»; долгий вход — почти наверняка следствие того, что клиент ищет контроллер домена не там, где надо (сайты и подсети); а чтобы завтра всё не переделывать вручную, нужна делегированная учётка для L1, которая сможет сбросить пароль, но не сможет сломать домен.

Active Directory — это не «служба, где заводят пользователей». Это база данных (каталог), которая одновременно является: системой аутентификации (Kerberos), службой каталогов (LDAP), фундаментом для групповых политик, точкой привязки прав доступа к файлам и точкой входа для всех остальных сервисов — 1С, почты, антивирусного сервера, систем резервного копирования. Ошибка в структуре OU на 50 сотрудников стоит двух дней аккуратной миграции; на 5000 — проекта на полгода. После этой главы ты будешь уметь: осознанно выбирать структуру леса и домена, проектировать OU под делегирование и GPO (а не «по отделам, как в 1С»), настраивать сайты и site links, настраивать делегирование по модели уровней администрирования, проверять репликацию и объяснять всё это на собеседовании.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Лес (forest) — граница безопасности.** Всё внутри леса доверяет друг другу: любой Domain Admin одного домена леса может (штатно или нештатно) получить права в другом домене. Поэтому «лес = одна компания/одна зона доверия». Домен — граница репликации, парольной политики (в базовом варианте) и административных полномочий по умолчанию, но **не** граница безопасности с 2008 functional level.

**Дерево и домены.** Дерево — домены с общим пространством имён (`corp.vektor.lab` → `msk.corp.vektor.lab`). Отдельные деревья в лесу — разные DNS-пространства (например, после покупки компании). Для «Вектора» (50 человек) правильный ответ: **один лес, один домен**. Второй домен оправдан, когда нужна отдельная парольная политика «по-старому», отдельная административная граница (аутсорс, подрядчик, филиал в другой юрисдикции) или когда репликация одного домена физически невозможна. В 2026 году парольные политики разной строгости делаются через PSO (Fine-Grained Password Policies) — отдельный домен ради этого больше не нужен.

**Сайты (sites) ≠ подсети для красоты.** Сайт — это объект, описывающий «зону с хорошей связностью»: внутри сайта репликация идёт по уведомлению (изменение пошло — партнёр получил через ~15 секунд), без сжатия, по RPC. Между сайтами репликация идёт по расписанию (минимум 15 минут, по умолчанию 180 минут), со сжатием, через bridgehead-серверы; топологию строит KCC (Knowledge Consistency Checker), а в каждом сайте один из DC назначается ISTG (Inter-Site Topology Generator). Сайт также влияет на **DC Locator**: клиент по своему IP находит подсеть (subnet object) → сайт → ищет DC сначала в своём сайте. Если в сайте DC нет, клиент идёт в «ближайший» сайт по стоимости site link. Отсюда и «вход четыре минуты»: клиент без объекта подсети может опрашивать DC в другом сайте через медленный канал.

```text
ЛЕС corp.vektor.lab
├── Домен corp.vektor.lab (NetBIOS VEKTOR, единственный)
│   ├── OU Company
│   │   ├── OU Бухгалтерия  (ivanov, GG-Buh)
│   │   ├── OU HR           (petrova, GG-HR)
│   │   ├── OU Склад        (sidorov, GG-Sklad)
│   │   └── OU IT           (GG-IT)
│   ├── OU Компьютеры       (WIN11-CL01, WIN10-CL02)
│   └── OU Серверы          (DC01, DC02, FS01, UBU01, ASTRA01)
└── CN=Sites,CN=Configuration
    ├── Site Moskva  ── subnet 10.10.10.0/24 ── DC01, DC02
    ├── Site DMZ     ── subnet 10.10.20.0/24 ── DC нет (fallback в Moskva)
    └── SiteLink DEFAULTIPSITELINK: cost 100, interval 180 min
```

**OU — то, что реально проектируют руками.** Объект в AD лежит ровно в одном OU. OU влияет на: (1) какие GPO применяются, (2) кому делегированы права, (3) как объект видят админы. Отделы — плохой единственный критерий: бухгалтерия и HR различаются правами, но обе содержат и людей, и компьютеры с разной политикой. Рабочая схема: верхний уровень — по административному домену/подразделению, внутри — разделение «пользователи / компьютеры / группы», а политики навешиваются не на отдел, а на роль (`WKS-Baseline`, `SEC-Admins`).

**GPO и порядок применения (LSDOU).** Политика применяется в порядке: Local → Site → Domain → OU (вложенные OU — сверху вниз), последняя выигрывает. `Enforced` (ранее No Override) пробивает `Block Inheritance`. Фильтрация — security filtering по группам, WMI-фильтры (по ОС, по модели), loopback processing (для терминальных серверов и 1С-ферм). Шаблоны админ-политик хранятся в Central Store: `\\corp.vektor.lab\SYSVOL\corp.vektor.lab\Policies\PolicyDefinitions`.

**Делегирование и уровни администрирования.** Классическая модель Tier 0/1/2 (Microsoft Active Directory Tier Model): Tier 0 — DC, AD, PKI; Tier 1 — серверы; Tier 2 — рабочие станции и пользователи. Учётки Tier 0 не логинятся на клиентские ОС. Защита: группа `Protected Users`, флаг «Account is sensitive and cannot be delegated», группы админов в `AdminSDHolder` (ACL перезаписывается SDPROP раз в час — поэтому «дал права Domain Admin и убрал» не работает), Windows LAPS для локальных админов `[ПРОВЕРИТЬ: встроенный Windows LAPS в Win11/Server 2022]`.

**Порты, без которых AD «не работает»:** DNS 53 (TCP/UDP) — самое частое место поломки; Kerberos 88 (TCP/UDP); kpasswd 464; LDAP 389, LDAPS 636; Global Catalog 3268/3269; SMB 445 (SYSVOL/NETLOGON); RPC endpoint mapper 135 и динамический диапазон RPC (обычно 49152–65535, в продакшене его сужают); W32Time 123 UDP; AD Web Services 9389. Аутентификация на клиенте — Kerberos (тикет TGT от KDC), в Linux-мире (Astra01, UBU01) — Kerberos + SSSD/realmd через LDAP к DC.

## 3. Аналогия из жизни

Представь город. **Лес** — страна с едиными паспортами: паспорт, выданный в любом городе, действует везде, и полиция всех городов подчиняется одним законам. **Домен** — город с собственным ЗАГСом: записи о жителях хранятся здесь, и город сам решает, кому доверять. **OU** — районы и улицы внутри города: сами по себе они ничего не запрещают, но именно по ним раскладывают листовки (GPO) и назначают вахтёров с ключами (делегирование). **Сайт** — почтовое отделение: он не про «где человек прописан», а про то, как быстро ходят письма между районами; если в районе нет своего отделения, письма поедут в соседнее по объездной дороге. **Site link** — та самая объездная дорога с её стоимостью (пробками) и расписанием рейсов. **Функциональный уровень** — минимальная версия законов, которую город обязуется соблюдать: поднял — назад уже не откатишь. **Делегирование** — выдать вахтёру связку ключей от одного подъезда, а не от всего города.

Где аналогия ломается: в городе человек может физически жить в одном районе, а прописан в другом, и это нормально; в AD объект принадлежит ровно одному OU, а «физическое» расположение (сайт) определяется IP-адресом компьютера через subnet object, и эти две вещи друг с другом не связаны.

## 4. Практика и команды

Все команды — из PowerShell с модулем `ActiveDirectory` (на DC01 он есть; на WIN11-CL01 ставится через Settings → System → Optional features → RSAT: Active Directory Domain Services and Lightweight Directory Services Tools).

**1. Инвентаризация леса и домена — с чего начинается любой аудит.**
```powershell
Get-ADForest | Format-List Name,ForestMode,RootDomain,Sites,GlobalCatalogs,SchemaMaster,DomainNamingMaster
Get-ADDomain | Format-List Name,DNSRoot,NetBIOSName,DomainMode,PDCEmulator,RIDMaster,InfrastructureMaster
```
`ForestMode`/`DomainMode` — функциональные уровни (например `Windows2016Forest`). `GlobalCatalogs` — список GC. Смотрим: один ли лес, один ли домен, где живут FSMO-роли, нет ли домена «на память от прошлого админа».

**2. Список контроллеров и их принадлежность сайтам.**
```powershell
Get-ADDomainController -Filter * |
  Format-Table Name, Site, IPv4Address, IsGlobalCatalog, OperatingSystem, IsReadOnly
```
Ключевое поле — `Site`. Если DC физически в Москве, а `Site` показывает `Default-First-Site-Name`, значит объект подсети не создан и DC Locator работает «как получится».

**3. FSMO-роли текстом и в GUI.**
```cmd
netdom query fsmo
```
Вывод: пять строк с именами DC. Смысл: Schema Master и Domain Naming Master — по одному на лес; PDC Emulator, RID Master, Infrastructure Master — по одному на домен. Перенос (только осознанно, в окно обслуживания):
```powershell
Move-ADDirectoryServerOperationMasterRole -Identity DC02 -OperationMasterRole PDCEmulator,RIDMaster,InfrastructureMaster -Confirm:$true
```
`-WhatIf` покажет план без переноса `[ПРОВЕРИТЬ: поддержка -WhatIf у конкретной версии модуля]`.

**4. Проверка, включена ли корзина AD (Recycle Bin) — обязательный пункт аудита.**
```powershell
Get-ADOptionalFeature -Filter * | Format-Table Name,EnabledScopes
Enable-ADOptionalFeature -Identity 'Recycle Bin Feature' -Scope ForestOrConfigurationSet -Target corp.vektor.lab
```
Операция **необратима**. После включения удалённый пользователь ещё 180 дней лежит в `CN=Deleted Objects` и восстанавливается через `Restore-ADObject`.

**5. Сайты и подсети.**
```powershell
Get-ADReplicationSite -Filter *
Get-ADReplicationSubnet -Filter * | Format-Table Name,Site
New-ADReplicationSite -Name "DMZ"
New-ADReplicationSubnet -Name "10.10.20.0/24" -Site "DMZ"
```
`New-ADReplicationSubnet` создаёт объект в `CN=Subnets`; формат — CIDR. Пока подсети нет, клиенты из этого диапазона «не принадлежат» ни одному сайту.

**6. Site links — стоимость и расписание.**
```powershell
New-ADReplicationSiteLink -Name "MOSKVA-DMZ" -SitesIncluded "Moskva","DMZ" `
  -Cost 100 -ReplicationFrequencyInMinutes 15 -InterSiteTransportProtocol IP
Set-ADReplicationSiteLink -Identity "MOSKVA-DMZ" -ReplicationFrequencyInMinutes 30
```
`-Cost` — «пробки»: меньшая стоимость = более предпочтительный маршрут. Минимальный интервал межсайтовой репликации — 15 минут (меньше нельзя, планировщик не даст).

**7. Проверка DC Locator глазами клиента.**
```cmd
nltest /dsgetsite
nltest /dsgetdc:corp.vektor.lab /force /gc
```
```powershell
Resolve-DnsName -Name "_ldap._tcp.dc._msdcs.corp.vektor.lab" -Type SRV
```
Первая команда вернёт имя сайта клиента (`Moskva`). Вторая — какой DC клиент выбрал и почему (флаги `GC`, `DS`). Третья — SRV-записи, по которым клиент вообще находит DC; если тут пусто, домен «пропал» независимо от состояния DC.

**8. Аудит структуры OU и защиты от удаления.**
```powershell
Get-ADOrganizationalUnit -Filter * -Properties ProtectedFromAccidentalDeletion |
  Select-Object Name, ProtectedFromAccidentalDeletion, CanonicalName
```
`CanonicalName` — путь в формате `corp.vektor.lab/Company/Склад`. Флаг должен быть `True` практически везде: случайно удалённый OU с 200 пользователями — это вечер с корзиной AD и без корзины — катастрофа.

**9. Делегирование: GUI и проверка ACL.**
GUI: `dsa.msc` → View → Advanced Features → правый клик по OU → **Delegate Control** → Add `GG-IT` → задача «Reset user passwords and force password change at next logon» → Finish.
```cmd
dsacls "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab"
```
`dsacls` без ключей — только чтение ACL, безопасно. Ищем строку с `GG-IT` и правом `Reset Password` (на русской ОС формулировка локализована `[ПРОВЕРИТЬ]`). Из PowerShell: `Get-Acl -Path "AD:\OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab"`. Выдавать права из CLI (`dsacls /G`) можно, но синтаксис прав легко переврать — на первых порах только мастер.

**10. GPO: список, отчёт, привязка.**
```powershell
Get-GPO -All | Sort-Object DisplayName | Format-Table DisplayName,GpoStatus,ModificationTime
Get-GPOReport -Name "VEKTOR-WKS-Baseline" -ReportType Html -Path C:\Temp\gpo.html
New-GPLink -Name "VEKTOR-WKS-Baseline" -Target "OU=Компьютеры,DC=corp,DC=vektor,DC=lab" -LinkEnabled Yes
Set-GPLink -Name "VEKTOR-WKS-Baseline" -Target "OU=Компьютеры,DC=corp,DC=vektor,DC=lab" -Enforced Yes -Order 1
```
`-Order 1` — политика применяется последней (выигрывает). `-Enforced Yes` — пробивает блокировку наследования: включай только когда реально нужно продавить настройку во все вложенные OU.

**11. Результат применения политик и состояние репликации.**
```cmd
gpresult /r
repadmin /replsummary
dcdiag /test:netlogon /test:dns /test:replications
```
`gpresult /r` покажет, какие GPO применились и какие отфильтрованы (Filtering: Not Applied). `repadmin /replsummary` — таблицу «кто с кем и насколько отстал»; ошибки должны быть 0. `dcdiag` с тремя тестами — быстрый health check DC.

**12. Linux-сторона: домен виден и с Ubuntu/Astra.**
```bash
realm discover corp.vektor.lab
kinit adm.student@CORP.VEKTOR.LAB
klist
ldapsearch -H ldap://dc01.corp.vektor.lab -Y GSSAPI \
  -b "dc=corp,dc=vektor,dc=lab" "(objectClass=organizationalUnit)" dn
```
`realm discover` (пакеты `realmd`, `sssd`, `krb5-user`, `adcli` — состав зависит от дистрибутива `[ПРОВЕРИТЬ для Astra Linux Common Edition]`) печатает найденный домен и поддерживаемые методы входа. `kinit` получает Kerberos-тикет — это и есть проверка, что домен «настоящий», а не только DNS-запись. `ldapsearch` с `-Y GSSAPI` читает каталог без пароля в командной строке.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- DC01 (10.10.10.10) — AD DS, DNS, DHCP, роли живые; DC02 (10.10.10.11) — второй DC, реплика работает.
- FS01 (10.10.10.20), WIN11-CL01, WIN10-CL02 — в домене. UBU01/ASTRA01 — по желанию для шага 12.
- Учётки: `adm.student` (админ домена), группы `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT`, OU `Company` → `Бухгалтерия/HR/Склад/IT`, `Компьютеры`, `Серверы`.
- На WIN11-CL01 установлены RSAT (AD DS и Group Policy Management), на DC01 — консоли `dssite.msc` (`AD Sites and Services`), `gpmc.msc`.

### Снапшот
В VMware Workstation: VM → Snapshot → Take Snapshot, имя **`before-sysadmin-009`** на **DC01, DC02, FS01 и WIN11-CL01**. Сделай снапшоты всех четырёх в один момент времени — DC01 и DC02 должны откатываться вместе (см. «Откат»).

### Шаги
1. **WIN11-CL01.** Установи RSAT, открой PowerShell от `adm.student`: `Import-Module ActiveDirectory; Get-ADForest | fl Name,ForestMode,Sites`. **Ожидаемо:** `ForestMode: Windows2016Forest [ПРОВЕРИТЬ на своём стенде]`, `Sites: {Default-First-Site-Name}`.
2. **DC01.** Зафиксируй текущее состояние в новый файл `C:\Adm\ad-design.md`: `Get-ADDomain | fl Name,DomainMode,PDCEmulator`, `Get-ADDomainController -Filter * | ft Name,Site,IPv4Address`, `netdom query fsmo`. **Ожидаемо:** оба DC в `Default-First-Site-Name`, все FSMO на DC01. Это твоя «точка ноль» — пригодится в портфолио.
3. **DC01.** Включи корзину AD: `Enable-ADOptionalFeature -Identity 'Recycle Bin Feature' -Scope ForestOrConfigurationSet -Target corp.vektor.lab`. **Ожидаемо:** предупреждение о необратимости и успешное выполнение; проверка — `Get-ADOptionalFeature -Filter * | ft Name,EnabledScopes`.
4. **DC01.** Переименуй сайт по умолчанию: 
```powershell
Rename-ADObject -Identity "CN=Default-First-Site-Name,CN=Sites,CN=Configuration,DC=corp,DC=vektor,DC=lab" -NewName "Moskva"
Get-ADReplicationSite -Filter *
```
**Ожидаемо:** в списке один сайт `Moskva` (в GUI `dssite.msc` — то же самое). Если PowerShell ругается — переименуй через `dssite.msc` (правый клик → Rename).
5. **DC01.** Создай объект подсети офиса: `New-ADReplicationSubnet -Name "10.10.10.0/24" -Site "Moskva"`. **Ожидаемо:** `Get-ADReplicationSubnet -Filter *` показывает одну подсеть с сайтом `Moskva`.
6. **DC01.** Создай сайт DMZ и его подсеть (задел под будущий RODC/сервер обновлений в DMZ): `New-ADReplicationSite -Name "DMZ"`, затем `New-ADReplicationSubnet -Name "10.10.20.0/24" -Site "DMZ"`. **Ожидаемо:** два сайта, две подсети.
7. **DC01.** Привяжи новый сайт к транспорту: `New-ADReplicationSiteLink -Name "MOSKVA-DMZ" -SitesIncluded "Moskva","DMZ" -Cost 100 -ReplicationFrequencyInMinutes 15 -InterSiteTransportProtocol IP`. **Ожидаемо:** сайт `DMZ` больше не «висит» без site link (иначе KCC не построит топологию). Запиши в `ad-design.md`: «в DMZ DC нет, клиенты через fallback ходят в Moskva; при появлении доменной машины в DMZ — RODC, не полноценный DC».
8. **WIN11-CL01.** Проверь DC Locator: `nltest /dsgetsite` → `Moskva`; `nltest /dsgetdc:corp.vektor.lab /force /gc` → выбран DC01 или DC02 с флагом `GC`; `Resolve-DnsName "_ldap._tcp.dc._msdcs.corp.vektor.lab" -Type SRV` → две SRV-записи (по одной на DC). **Ожидаемо:** сайт определяется, DC находится мгновенно.
9. **DC01.** Аудит OU: `Get-ADOrganizationalUnit -Filter * -Properties ProtectedFromAccidentalDeletion | ft Name,ProtectedFromAccidentalDeletion,CanonicalName`. Для всех OU, где флаг `False`, включи защиту: `Set-ADOrganizationalUnit -Identity "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab" -ProtectedFromAccidentalDeletion $true`. **Ожидаемо:** у всех OU `True`.
10. **DC01.** Делегируй `GG-IT` сброс паролей пользователей склада: `dsa.msc` → Advanced Features → правый клик по OU `Склад` → **Delegate Control** → Add `GG-IT` → «Reset user passwords and force password change at next logon». Проверь: `dsacls "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab"` содержит ACE для `VEKTOR\GG-IT`. **Ожидаемо:** ACE есть, других новых ACE нет.
11. **WIN11-CL01.** Заведи каркас политики: `New-GPO -Name "VEKTOR-WKS-Baseline" -Comment "Базовая политика рабочих станций. Владелец: adm.student"`, затем привяжи к `OU=Компьютеры` (`New-GPLink`), наполнение настройками — в отдельной главе. Проверь `Get-GPOReport`. **Ожидаемо:** GPO существует, привязана, отчёт открывается в браузере.
12. **DC01.** Финальная проверка здоровья: `repadmin /replsummary`, `dcdiag /test:netlogon /test:dns /test:replications`, `gpresult /r` на клиенте (политика `VEKTOR-WKS-Baseline` в списке Applied). Допиши `stand-log.md` и `ad-design.md`.

### Проверка
- [ ] `Get-ADReplicationSite -Filter *` возвращает `Moskva` и `DMZ`, сайта `Default-First-Site-Name` больше нет.
- [ ] `Get-ADReplicationSubnet -Filter *` содержит `10.10.10.0/24 → Moskva` и `10.10.20.0/24 → DMZ`.
- [ ] `nltest /dsgetsite` на WIN11-CL01 возвращает `Moskva`.
- [ ] `Get-ADOptionalFeature -Filter *` показывает `Recycle Bin Feature` с непустым `EnabledScopes`.
- [ ] У всех OU `ProtectedFromAccidentalDeletion = True`.
- [ ] `dsacls` на OU `Склад` содержит ACE для `GG-IT`; других лишних ACE нет.
- [ ] GPO `VEKTOR-WKS-Baseline` привязана к `OU=Компьютеры`, `gpresult /r` её видит.
- [ ] `repadmin /replsummary` — 0 ошибок; `dcdiag` без критичных FAIL.
- [ ] `ad-design.md` и `stand-log.md` обновлены.

### Сломай и почини
> **Пользователь:** Иванов (бухгалтерия, WIN11-CL01)
> **ВМ:** WIN11-CL01
> **Что написал:** «Не могу войти, пишет: "Не удалось установить доверительные отношения между этой рабочей станцией и основным доменом". Вчера всё работало, ничего не трогал. У меня закрытие месяца!»
> **Срочность:** высокая, блокирует работу.

**Как воспроизвести (на стенде, осознанно):** на DC01 сбрось пароль компьютерной учётки, не меняя его на клиенте:
```powershell
Get-ADComputer WIN11-CL01 | Set-ADAccountPassword -Reset -NewPassword (ConvertTo-SecureString "Lab#2026-vektor" -AsPlainText -Force)
```
Клиент и DC теперь считают разные пароли канала — secure channel рвётся.

**Диагностика по порядку:**
1. На WIN11-CL01 под локальным админом: `Test-ComputerSecureChannel -Verbose` → `False` (канал сломан).
2. `nltest /sc_verify:corp.vektor.lab` → ошибка 5 / «trust relationship failed».
3. `nltest /dsgetdc:corp.vektor.lab` и `nltest /dsgetsite` — убедись, что DC и сайт находятся. Если нет — проблема не в канале, а в DNS/сети, и чинить надо там.
4. `w32tm /query /status` — проверь расхождение времени: больше 5 минут ломает Kerberos и даёт похожие симптомы.
5. На DC01: `Get-ADComputer WIN11-CL01 | fl Name,Enabled,LastLogonDate` — учётка существует и включена (если её удалили — другой сценарий, нужно переприсоединение).

**Решение:**
```powershell
Test-ComputerSecureChannel -Repair -Credential (Get-Credential VEKTOR\adm.student)
klist purge
shutdown /r /t 0
```
После перезагрузки — вход доменной учёткой. Если `-Repair` не сработал (объект пересоздан, SID разошёлся) — вывести из домена и ввести заново: `Remove-Computer -UnjoinDomainCredential (Get-Credential VEKTOR\adm.student) -Restart`, затем `Add-Computer -DomainName corp.vektor.lab -OUPath "OU=Компьютеры,DC=corp,DC=vektor,DC=lab" -Restart`. Важно: `-OUPath` сразу кладёт компьютер в нужный OU, иначе он уедет в контейнер `CN=Computers`, на который GPO не привяжешь.

**Что ответить пользователю:** «Иван, проблема на стороне компьютера — сбился защищённый канал с контроллером домена (частая история после сброса учётной записи или восстановления из бэкапа). Данные и почта не пострадали. Перезапущу машину, вход должен занять минуту; если что-то пойдёт не так — переприсоединю к домену, это ещё 10 минут и тоже без потери файлов. Отпишусь, как закончу.»
**Закрытие заявки:** указать в решении «сброшен/пересоздан secure channel компьютера WIN11-CL01, выполнен `Test-ComputerSecureChannel -Repair`, перезагрузка, вход проверен», добавить prevention-заметку: компьютерные учётки не сбрасывать вручную без причины, для переустановок использовать переприсоединение.

### Что изменилось на стенде
- Сайт `Default-First-Site-Name` переименован в **Moskva**; создан сайт **DMZ**.
- Созданы объекты подсетей: `10.10.10.0/24 → Moskva`, `10.10.20.0/24 → DMZ`.
- Создан site link **MOSKVA-DMZ** (cost 100, interval 15 мин, транспорт IP).
- Включена **AD Recycle Bin** на лесу `corp.vektor.lab` (необратимо).
- Все OU получили `ProtectedFromAccidentalDeletion = True`.
- На OU `Склад` делегировано `GG-IT`: сброс паролей пользователей + требование смены при следующем входе.
- Создана GPO **VEKTOR-WKS-Baseline**, привязана к `OU=Компьютеры`.
- Появились файлы `C:\Adm\ad-design.md` (паспорт AD) и обновлён `stand-log.md`.

### Откат
VMware Workstation → VM → Snapshot Manager → выбери `before-sysadmin-009` → Revert. **Откатывай DC01 и DC02 одновременно к снапшотам одного поколения**, иначе получишь расхождение каталога и «залипшую» репликацию. После отката: на DC01 `w32tm /resync`, затем `repadmin /replsummary` и `dcdiag /test:replications` — убедись, что стенд здоров. Отдельно помни: откат снапшота DC в продакшене — классический способ получить USN rollback и сломанную репликацию; там защита — AD Recycle Bin + резервная копия System State (Windows Server Backup), а не снапшот гипервизора.

## 6. Ошибки новичков и безопасность

1. **«Сделаю по домену на каждый отдел»** → пять доменов, пять парольных политик, кросс-доменные группы и ад из доверительных отношений там, где хватало одного домена и OU. → Один лес/один домен по умолчанию; второй домен — только под административную или юридическую границу.
2. **OU «как в 1С», только по отделам** → в OU `Бухгалтерия` лежат и люди, и компьютеры, и группы; политики на компьютер и на пользователя конфликтуют, делегировать нечего. → Внутри OU — разделение по типам объектов, политики по ролям, а не по названию отдела.
3. **Нет объектов подсетей** → клиенты не знают свой сайт, DC Locator может выбрать контроллер через медленный канал; вход по 3–5 минут, `SYSVOL` тянется вечно. → Каждый IP-диапазон пользователей должен быть в `CN=Subnets` и привязан к сайту.
4. **Сайт создали, site link — нет** → новый сайт без транспорта: KCC не построит топологию, репликация «в никуда». → После `New-ADReplicationSite` сразу добавь сайт в site link и проверь `repadmin /bridgeheads`.
5. **GPO привязали к сайту или к корню домена** → политика применяется ко всем и ломает неожиданные машины, отладка невозможна. → Привязка к OU, точечно; `Enforced` — только для действительно обязательных настроек.
6. **Раздача Domain Admin «на время»** → членство в защищённых группах возвращается через SDPROP (AdminSDHolder), права «не убираются», и любая скомпрометированная учётка = весь лес. → Делегирование точечными ACE через Delegate Control + отдельные учётки Tier 0.
7. **Удалили OU «руками», защиты нет** → уехали все пользователи отдела и их права на папки. → `ProtectedFromAccidentalDeletion = True` на всех OU + включённая корзина AD.
8. **Сброс пароля компьютерной учётки «чтобы проверить»** → ровно та заявка из раздела 5, только в проде и в 9 утра. → Не трогать компьютерные учётки; для переустановки — переприсоединение с `-OUPath`.

**Безопасность (что нельзя в боевой среде):** держать `adm.student`-подобную учётку одновременно доменным админом и рабочей почтой на клиентской ОС; отключать `Protected Users` «потому что не логинится»; выдавать права на OU группе, в которую входят все (`Domain Users`); хранить пароли в GPO (local admin password через Preferences — плейнтекст в SYSVOL). В домене с требованиями по защите (приказ ФСТЭК №17, 152-ФЗ) дополнительно фиксируются: состав защищённых групп и их аудит, парольные политики (в т.ч. PSO для админов), журналирование входа на DC, интеграция Linux-хостов (Astra01) через SSSD с ограничением `sudo` по группам AD, а не «всем админам всё».

## 7. Вопросы на собеседовании

**1. «Зачем второй контроллер домена и как клиент выбирает, к какому идти?»**
Ответ: второй DC — отказоустойчивость аутентификации, DNS и SYSVOL, плюс распределение нагрузки; FSMO-роли при этом живут на одном из них. Выбор делает DC Locator: клиент по DNS находит SRV-записи `_ldap._tcp.dc._msdcs.<домен>`, определяет свой сайт по subnet object, ищет DC в своём сайте, при отсутствии — в ближайшем по стоимости site link, проверяет доступность LDAP/Kerberos и берёт первый ответивший; PDC Emulator используется при смене пароля и для совместимости. **Что хочет услышать:** понимание связки «DNS → сайт → DC», а не «просто два сервера».
**Красный флаг:** «второй DC нужен для бэкапа AD» (нет, это не бэкап) или «клиент идёт на первый по алфавиту».

**2. «Как вы проектируете структуру OU?»**
Ответ: драйверы — делегирование и GPO, а не оргструктура. Верхний уровень — административные границы/подразделения, внутри — разделение пользователей/компьютеров/групп/сервисных учёток; отдельные OU под админские учётки Tier 0 с урезанным ACL; политики привязываются к функциональным OU (`WKS`, `Servers`, `RDS`), а не к отделам; структура должна переживать смену оргструктуры. Плюс защита от удаления и понятный нейминг. **Что хочет услышать:** «OU проектируется под применение политик и делегирование».
**Красный флаг:** «делаем OU по отделам, как в штатном расписании» без обсуждения политик.

**3. «Чем сайт AD отличается от подсети и когда заводить новый сайт?»**
Ответ: подсеть (subnet object) — это отображение IP-диапазона на сайт; сайт — зона хорошей связности, влияющая на топологию репликации (внутри — уведомления ~15 с, между — расписание от 15 мин, сжатие, bridgehead) и на выбор DC клиентами. Новый сайт заводят, когда появляется удалённая площадка с медленным/дорогим каналом и (обычно) свой DC/RODC; сайт без DC допустим, но клиенты будут ходить в соседний по стоимости site link. **Что хочет услышать:** site = репликация + DC Locator, subnet = mapping.
**Красный флаг:** «сайт нужен для группировки компьютеров» или «сайты — это VLAN».

**4. «Как делегировать права в AD и почему не раздают Domain Admin?»**
Ответ: модель Tier 0/1/2, отдельные учётки по уровням, `Protected Users`, LAPS для локальных админов. Делегирование — через Delegate Control на конкретном OU (сброс паролей, создание объектов, управление членством в группах), с проверкой ACL (`dsacls`), тестированием на тестовой учётке и документированием. Учитывать AdminSDHolder/SDPROP для защищённых групп, регулярный аудит ACL (`Get-Acl` на OU) и отзыв прав при увольнении. **Что хочет услышать:** «наименьшие привилегии + проверяемость».
**Красный флаг:** «проще дать Domain Admin, потом уберём» — не уберётся автоматически.

**5. «Политика не применилась. Ваши действия?»**
Ответ: `gpresult /r` и `gpresult /h` на проблемной машине — смотреть, что Applied, что Filtering (Not Applied); проверить: привязка к нужному OU (и что объект реально в нём, а не в `CN=Computers`), security filtering, WMI-фильтр, `Enforced`/`Block Inheritance`, затем `gpupdate /force` и журналы `Microsoft-Windows-GroupPolicy` в Event Viewer; отдельно — доступность SYSVOL на DC (`\\corp.vektor.lab\SYSVOL`) и репликация (`repadmin`), потому что GPO живёт в SYSVOL и в каталоге. **Что хочет услышать:** системный порядок «область применения → фильтры → SYSVOL → логи».
**Красный флаг:** «перезагрузил — заработало» без понимания причины.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — разделы «Active Directory Domain Services» и «AD DS Design» (Active Directory Logical Structure: forests, domains, OUs), «Active Directory Sites and Services», «How DNS Support for Active Directory Works», «Group Policy overview», «Best Practices for Delegating Active Directory Administration» `[ПРОВЕРИТЬ название раздела]`. Поиск: `AD DS logical structure design site`, `AD DS site link replication interval`, `delegating AD administration best practices`.
- **learn.microsoft.com** — справка по командлетам модулей `ActiveDirectory`, `GroupPolicy`, `DnsClient`; утилиты `repadmin`, `dcdiag`, `nltest`, `dsacls`. Поиск: `repadmin replsummary`, `dcdiag test netlogon`, `Get-ADReplicationSiteLink`.
- **learn.microsoft.com** — «Active Directory Tier Model» / «Enterprise access model» (модель уровней администрирования), «Protected Users security group», «Windows LAPS overview» `[ПРОВЕРИТЬ актуальность названий]`. Поиск: `Active Directory tier model`, `Protected Users group`.
- **learn.microsoft.com** — «Fine-Grained Password Policies» (PSO). Поиск: `fine grained password policy PSO`.
- **ubuntu.com** — раздел Server docs, «Service - SSSD» / «Kerberos»: интеграция Linux в AD. Поиск: `ubuntu sssd active directory join realmd`.
- **docs.redhat.com** — «Integrating RHEL systems directly with Windows Active Directory» (SSSD, realm, adcli). Поиск: `rhel sssd ad integration`.
- **wiki.astralinux.ru** — документация Astra Linux: ввод в домен (ALD Pro, SSSD/Kerberos) `[ПРОВЕРИТЬ актуальные статьи]`. Поиск: `Astra Linux ввод в домен Active Directory`.
- **windowsserverdocs / activity** — учебные версии Windows Server 2022 Evaluation и Windows 11 Enterprise Evaluation доступны для скачивания с сайта Microsoft; учти, что в РФ загрузка с microsoft.com может требовать обходных легальных путей (VL-партнёр, реселлер) `[ПРОВЕРИТЬ доступность]`; альтернатива для отработки навыков — Samba AD DC на Linux, но это не AD «один в один».
