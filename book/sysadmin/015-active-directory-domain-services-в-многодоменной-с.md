# Active Directory Domain Services в многодоменной среде: трасты, миграция, консолидация доменов

## 1. Введение (зачем это нужно в реальной работе)

Тикет #4417. **Пользователь:** kuznetsov (пришёл в «Вектор» после покупки компании-партнёра). **ВМ:** WIN11-CL01. **Что написал:** «Не открывается \\FS01\Obshchee — пишет «Отказано в доступе». У половины моих коллег та же история, а у двоих сетевые диски отвалились после того, как вы что-то сделали с доменами. Месяц закрываем, работа стоит. **Срочность: высокая.**»

Такие заявки — не редкость, а норма для сисадмина, который работает дольше двух лет в одной компании. Причины всегда одни и те же: компанию купили (или она купила кого-то), есть филиал со своим доменом, остался «наследный» домен от прежнего подрядчика, или ИТ-директор решил консолидировать три домена в один, чтобы не держать три команды и три лицензионных счёта.

Многодоменная среда — это всегда про **границы доверия**: где заканчивается один периметр безопасности и начинается другой, как объяснить контроллеру домена, что «этот чужой пользователь — свой», и как перенести людей и данные, не сломав им доступ к файлам и почте.

После этой главы вы должны уметь: объяснить разницу между лесом и доменом и почему это важно; создавать и проверять трасты (trusts) разных типов; читать `nltest`, `netdom` и `Get-ADTrust` и понимать, что они говорят; диагностировать «траст есть, а доступа нет»; планировать миграцию пользователей и консолидацию домена с откатом.

## 2. Теория «под капотом»

**Домен** — граница политики безопасности и граница репликации. Внутри домена все контроллеры домена (DC) равноправны и реплицируют одну базу `NTDS.dit`. **Лес** — граница безопасности целиком: общая схема (Schema), общая конфигурация (Configuration NC), группы `Enterprise Admins` и `Schema Admins`, глобальный каталог. Домены внутри леса связаны **автоматическими двусторонними транзитивными трастами** (parent-child), их никто не создаёт руками — они возникают при `dcpromo` дочернего домена.

**Траст (trust)** — это объект в AD (`trustedDomain`) плюс общий секрет (trust password, по умолчанию меняется автоматически раз в 30 дней `[ПРОВЕРИТЬ]`), на котором строится межлесовой Kerberos: клиент получает TGT в своём домене, затем **referral ticket** (межреалмовый TGT, подписанный ключом траста) и предъявляет его в целевом домене, а тот выдаёт service ticket на нужный сервер.

Ключевые свойства траста:
- **Направление.** `Outgoing` = «мои пользователи могут ходить туда». `Incoming` = «их пользователи могут ходить ко мне». Двусторонний = оба.
- **Транзитивность.** Трасты внутри леса транзитивны. **External trust** (домен-в-домен, между лесами) — по умолчанию **не** транзитивен. **Forest trust** — транзитивен для всех доменов леса.
- **SID filtering (quarantine).** Механизм, который не даёт «чужим» SID из другого леса попасть в токен доступа. Для external-трастов включён всегда, для forest-трастов работает в режиме forest-aware. Именно он ломает `sIDHistory` при миграции — и его приходится осознанно отключать.
- **Аутентификация.** Forest-wide (по умолчанию: любой пользователь леса-партнёра может аутентифицироваться) или **selective authentication** (доступ только тем, кому явно дали право «Allowed to authenticate» на объекте).
- **Name suffix routing** — в forest-трасте настройка, какие UPN-суффиксы партнёрского леса маршрутизируются через этот траст.

**Что нужно сети.** Помимо 53/udp+tcp (DNS), 88 (Kerberos), 135 (RPC Endpoint Mapper), 389/636 (LDAP/LDAPS), 445 (SMB), 3268/3269 (Global Catalog), нужен динамический диапазон RPC 49152–65535 (в Server 2008+; для старых — 1025–5000). И обязательно — **взаимное разрешение имён**: без DNS траста «как бы есть», но аутентификация не работает.

```text
     corp.vektor.lab  (лес VEKTOR, DC01+DC02)         vektor-partner.lab (лес VPARTNER, DC03)
   ┌────────────────────────────────────────┐      ┌────────────────────────────────────────┐
   │ DC01 10.10.10.10  AD DS, DNS, DHCP     │      │ DC03 10.10.10.12  AD DS, DNS           │
   │ DC02 10.10.10.11  доп. DC (L2)         │◄────►│ OU=Partner, kuznetsov, GG-Partner      │
   │ FS01 10.10.10.20  \\FS01\Obshchee      │TRUST │                                        │
   │ WIN11-CL01 (DHCP)  клиент ivanov       │Forest│                                        │
   └────────────────────────────────────────┘Two-way└─────────────────────────────────────┘
        DNS: CF  vektor-partner.lab -> 10.10.10.12      DNS: CF corp.vektor.lab -> 10.10.10.10
        Kerberos: 88 | LDAP: 389/636 | GC: 3268/3269 | RPC: 135 + 49152-65535 | SMB: 445
```

**Консолидация доменов** — это проект, а не команда. Четыре реальных сценария:
1. **Миграция объектов между лесами (inter-forest)** — ADMT/скрипты, перенос пользователей, групп, компьютеров, `sIDHistory`, профилей. Траст нужен как «мост».
2. **Миграция внутри леса (intra-forest)** — `Move-ADObject` между доменами одного леса, пароль и SID сохраняются, `sIDHistory` не нужен.
3. **Переименование домена** — `rendom` (только внутри леса, сложно, требует простоя и полного бэкапа) `[ПРОВЕРИТЬ детали шагов]`.
4. **Greenfield / re-join** — новый домен с нуля, компьютеры перезаводятся, профили копируются `robocopy`, доступ переводится на **группы**, а не на SID. Дольше, но чище.

Инженерная истина: **правильная миграция переводит ACL на группы целевого домена, а `sIDHistory` — лишь страховка на переходный период**, а не постоянная конструкция.

## 3. Аналогия из жизни

Домен — это страна с собственным паспортом и своей полицией. Лес — союз стран с единым гражданством: переехал в соседнюю область — паспорт тот же, проверять нечего (транзитивный parent-child). Траст — межгосударственное соглашение о признании виз: пограничник (контроллер домена) смотрит на визу и пропускает.

SID filtering — пограничный контроль, который не пускает человека с чужим паспортом, даже если он размахивает справкой «я тут раньше жил». `sIDHistory` — как раз такая справка: старый штамп о прежнем гражданстве, чтобы старые договоры аренды (ACL на файлах) продолжали действовать после переезда. Selective authentication — виза, действующая только в конкретный офис, а не по всей стране.

Где аналогия ломается: в жизни гражданство одно и старый паспорт аннулируется. В AD `sIDHistory` живёт одновременно с новым SID и продолжает работать как ключ к ресурсам — поэтому это ещё и постоянный риск, а не просто архивная справка.

## 4. Практика и команды

**1. Посмотреть все трасты леса (PowerShell).**
```powershell
Get-ADTrust -Filter * | Format-Table Name,Direction,ForestTransitive,IntraForest,SIDFilteringQuarantined,SelectiveAuthentication -Auto
Get-ADTrust -Identity vektor-partner.lab | Format-List *
```
`-Filter *` — все объекты трастов; `-Identity` — один траст. В выводе смотрим `Direction` (BiDirectional), `ForestTransitive` (True = forest trust), `SIDFilteringQuarantined`, `SelectiveAuthentication`.

**2. Полная картина трастов со стороны DC.**
```cmd
nltest /domain_trusts /all_trusts /v
```
`/domain_trusts` — список трастов, `/all_trusts` — включая все домены леса, `/v` — подробно. Типичный вывод: `vektor-partner.lab (VPARTNER:VEKTOR-PARTNER.LAB) (Forest: 0x1F) (Direct Outbound) (Attr: 0x1F8)`. Смотрим флаги Forest/Inbound/Outbound и Attr.

**3. Найти контроллер домена в партнёрском домене.**
```cmd
nltest /dsgetdc:vektor-partner.lab /force
```
Проверяет, что Locator-запросы (DNS SRV) уходят и возвращаются. Если «не удалось найти DC» — проблема в DNS, а не в трасте.

**4. Проверить живость траста.**
```cmd
netdom trust corp.vektor.lab /domain:vektor-partner.lab /verify
```
`/verify` проверяет secure channel траста в обе стороны `[ПРОВЕРИТЬ необходимость ключей /userO /passwordO в вашей версии]`. Ожидаемо: «The command completed successfully». Ошибка «The trust relationship between the primary domain and the trusted domain failed» — траст сломан (пароль, DNS или отключённый DC).

**5. Взаимное разрешение имён и conditional forwarder.**
```powershell
Resolve-DnsName -Name corp.vektor.lab -Type SOA -Server 10.10.10.12
Add-DnsServerConditionalForwarderZone -Name "vektor-partner.lab" -MasterServers 10.10.10.12 -ReplicationScope Forest
Get-DnsServerZone | Where-Object {$_.ZoneType -eq "Forwarder"}
```
`-MasterServers` — IP DC партнёрского домена; `-ReplicationScope Forest` — зона уедет на все DC леса `[ПРОВЕРИТЬ имя параметра для conditional forwarder в вашей сборке]`. На DC03 симметрично: CF на `corp.vektor.lab` → 10.10.10.10. **Не делайте взаимные forwarders (всё-в-одно)** — получите петлю.

**6. Проверить порты до партнёрского DC.**
```powershell
53,88,135,389,445,3268 | ForEach-Object {
  $r = Test-NetConnection -ComputerName 10.10.10.12 -Port $_ -WarningAction SilentlyContinue
  "{0,-6} {1}" -f $_, $r.TcpTestSucceeded
}
```
Смотрим, что все True. 88 закрыт — Kerberos не работает, траст «мёртв» при живом пинге.

**7. Список DC партнёрского леса и их роли.**
```powershell
Get-ADDomainController -Filter * -Server vektor-partner.lab | Format-Table Name,Site,IsGlobalCatalog,OperatingSystem
```
`-Server` принимает имя домена/DC и уводит запрос в чужой лес — базовый приём во всех командлетах AD.

**8. Проверить ACL на файловом ресурсе глазами инженера.**
```powershell
Get-Acl "\\FS01\Obshchee" | Select-Object -ExpandProperty Access |
  Format-Table IdentityReference,FileSystemRights,AccessControlType -Auto
icacls "\\FS01\Obshchee"
```
Смотрим, что в ACL есть группы целевого домена (`VEKTOR\GG-Sklad`), а не только «красивые» имена пользователей. После миграции ACL должен быть переписан на группы.

**9. Найти объекты с SIDHistory (то есть «следы» миграции).**
```powershell
Get-ADObject -LDAPFilter "(sIDHistory=*)" -Properties sIDHistory,objectClass -Server DC01 |
  Select-Object Name,objectClass,sIDHistory
```
Пусто — миграции не было или её чистили. Есть записи — помните: `sIDHistory` блокируется SID filtering и не работает через недоверенный траст.

**10. Включение SIDHistory на трасте (опасно, только на время миграции).**
```cmd
netdom trust corp.vektor.lab /domain:vektor-partner.lab /enablesidhistory:yes /usero:adm.student /passwordo:*
netdom trust corp.vektor.lab /domain:vektor-partner.lab /verify
```
`/enablesidhistory` требует, чтобы траст был в направлении «принимающий доверяет источнику» `[ПРОВЕРИТЬ точный синтаксис и требования к привилегии «Migrate SID history»]`. В боевой среде включать только на окно миграции: это расширяет доверие до «SID из чужого леса проходит как свой».

**11. Проверить, что компьютер/сервер жив в домене (частая боль после миграций).**
```powershell
Test-ComputerSecureChannel -Verbose
Test-ComputerSecureChannel -Repair -Credential (Get-Credential)
```
`-Repair` пересоздаёт пароль machine account. Альтернатива в cmd: `nltest /sc_verify:corp.vektor.lab`.

**12. Подготовка к выводу DC при консолидации.**
```cmd
netdom query fsmo
```
Смотрим, не живут ли на выводимом DC роли хозяев операций. Перенос — `Move-ADDirectoryServerOperationMasterRole -Identity DC02 -OperationMasterRole SchemaMaster,DomainNamingMaster,PDCEmulator,RIDMaster,InfrastructureMaster`, затем `ntdsutil` → «Metadata cleanup» для остатков.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- Готовый стенд L2: **DC01** (10.10.10.10, AD DS, DNS, DHCP, лес `corp.vektor.lab`), **DC02** (10.10.10.11, второй DC), **FS01** (10.10.10.20, папка `\\FS01\Obshchee` с ACL по группам `GG-*`), **WIN11-CL01** (DHCP-адрес).
- Учётка `adm.student` — администратор домена `VEKTOR`. Пароль стенда `Lab#2026-vektor`.
- **Новая ВМ DC03 создаётся в этой главе** (её нет в базовой таблице стенда — добавляем явно, для трека «Сисадмин»): Windows Server 2022 Evaluation, 2 vCPU, 4 ГБ RAM, 60 ГБ диск, сеть **VMnet8**, IP **10.10.10.12**, лес **`vektor-partner.lab`**, NetBIOS `VPARTNER`. Адрес вне DHCP-пула (100–150), с DC01/DC02/FS01 не конфликтует.

### Снапшот
В VMware Workstation на **DC01**, **DC02**, **FS01** и **WIN11-CL01**: `VM → Snapshot → Take Snapshot…`, имя `before-sysadmin-015`. DC03 пока не существует — по ней снапшот делаем сразу после установки ОС и настройки IP (перед промоушеном), с тем же именем.

### Шаги
1. **DC03.** Создать ВМ, установить Windows Server 2022 Evaluation. Задать имя `DC03`, статический IP `10.10.10.12/24`, шлюз `10.10.10.2`, DNS `127.0.0.1`. Проверка: `ipconfig /all` показывает нужные адреса. Ожидаемо: сеть есть, домена нет.
2. **DC03.** `Install-WindowsFeature AD-Domain-Services -IncludeManagementTools`, затем промоушен **нового леса**: `Install-ADDSForest -DomainName vektor-partner.lab -DomainNetbiosName VPARTNER -InstallDns` (DSRM-пароль `Lab#2026-vektor`). Перезагрузка. Ожидаемо: `Get-ADDomain` отдаёт `vektor-partner.lab`.
3. **DC03 → DC01 (DNS).** На DC03 forwarder на `10.10.10.10`; на DC01 создать conditional forwarder `vektor-partner.lab` → `10.10.10.12` со scope Forest (реплицируется на DC02). Ожидаемо: `Resolve-DnsName -Name corp.vektor.lab -Type SOA -Server 10.10.10.12` и обратная проверка с DC01/DC02 на `10.10.10.12` отвечают.
4. **DC03.** Создать OU `Partner`, пользователя `kuznetsov` (пароль `Lab#2026-vektor`, снять «Сменить пароль при первом входе»), группу `GG-Partner` (Global), добавить `kuznetsov` в неё. Ожидаемо: `Get-ADUser kuznetsov`, `Get-ADGroupMember GG-Partner -Server DC03`.
5. **DC01.** `dsa.msc` → `corp.vektor.lab` → Properties → вкладка **Trusts** → **New Trust** → `vektor-partner.lab` → **Forest trust** → **Two-way** → **Forest-wide authentication** → пароли `Lab#2026-vektor`. Ожидаемо: мастер завершён с сообщением об успехе; предупреждение о SID filtering — это норма.
6. **DC01.** Проверка траста: `Get-ADTrust -Identity vektor-partner.lab | fl` (Direction BiDirectional, ForestTransitive True) и `nltest /domain_trusts /all_trusts /v`. Ожидаемо: траст виден, `netdom trust corp.vektor.lab /domain:vektor-partner.lab /verify` → «successfully».
7. **DC01.** Проверка портов до `10.10.10.12` по списку 53/88/135/389/445/3268. Ожидаемо: все True. Если 88 закрыт — остановитесь и разбирайтесь с файрволом на DC03, дальше идти бессмысленно.
8. **FS01.** В свойствах `\\FS01\Obshchee` → Security → Edit → **Locations** → выбрать `vektor-partner.lab` → добавить `GG-Partner` с правом **Read**. Ожидаемо: в ACL появилась запись `vektor-partner\GG-Partner`.
9. **WIN11-CL01.** Проверка кросс-лесовой аутентификации: `net use \\FS01\Obshchee /user:vektor-partner\kuznetsov *` (ввести пароль стенда). Ожидаемо: «Команда выполнена успешно». Затем `net use \\FS01\Obshchee /delete`.
10. **DC01 (консолидация).** Перенести пользователя в свой домен: `New-ADUser -Name "Дмитрий Кузнецов" -SamAccountName kuznetsov -Path "OU=Склад,OU=Company,DC=corp,DC=vektor,DC=lab" -AccountPassword (Read-Host -AsSecureString) -Enabled $true -Server DC01`, добавить в `GG-Sklad`. Ожидаемо: `Get-ADUser kuznetsov -Server DC01` находит учётку.
11. **WIN11-CL01.** Проверить новый доступ: `net use \\FS01\Obshchee /user:VEKTOR\kuznetsov *` — успех; `net use \\FS01\Buh /user:VEKTOR\kuznetsov *` — **отказ** (он не в `GG-Buh`). Это правильный результат: принцип минимальных привилегий подтверждён.
12. **DC01.** Записать в `stand-log.md`: DC03 (IP, лес), траст, conditional forwarders, OU `Partner`, `kuznetsov` и `GG-Partner`, ACL на `\\FS01\Obshchee`, новая учётка `VEKTOR\kuznetsov`. Сохранить `Get-ADTrust -Filter *` в файл как артефакт.

### Проверка
- [ ] DC03 отвечает по 10.10.10.12, `Get-ADDomain` показывает `vektor-partner.lab`.
- [ ] `Get-ADTrust -Identity vektor-partner.lab` — Direction BiDirectional, ForestTransitive True.
- [ ] `netdom trust … /verify` и `nltest /domain_trusts /all_trusts /v` без ошибок.
- [ ] `Resolve-DnsName` в обе стороны разрешает имена SOA.
- [ ] Все порты 53/88/135/389/445/3268 открыты.
- [ ] `net use \\FS01\Obshchee /user:vektor-partner\kuznetsov *` работает.
- [ ] `VEKTOR\kuznetsov` в `GG-Sklad`, `\\FS01\Buh` для него закрыт.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини
**Пользователь:** kuznetsov / ВМ: WIN11-CL01 / **Что написал:** «С утра не открывается \\FS01\Obshchee, пишет «Не удалось найти сетевой путь» и «Не найден входной билет Kerberos». Вчера всё работало» / **Срочность:** высокая.

**Воспроизводим поломку:** на DC01 открыть `dnsmgmt.msc` и в conditional forwarder `vektor-partner.lab` поменять `10.10.10.12` на `10.10.10.99`, затем `Clear-DnsServerCache -Force` на DC01 и DC02, на клиенте `ipconfig /flushdns`, `klist purge`.

**Диагностика по шагам:**
1. С клиента: `Resolve-DnsName vektor-partner.lab -Type SOA` → пусто/таймаут. Уже половина ответа.
2. С DC01: `Resolve-DnsName vektor-partner.lab -Type SOA -Server 10.10.10.12` → работает. Значит сеть и DC03 живы, проблема в пробросе.
3. `Get-DnsServerZone` / `dnsmgmt.msc` на DC01 и DC02 → видим неверный `10.10.10.99` в CF.
4. `nltest /dsgetdc:vektor-partner.lab` → ошибка Locator: подтверждает DNS, а не траст.
5. `Get-ADTrust -Identity vektor-partner.lab` и `netdom trust … /verify` → траст цел (важно: не начинать чинить траст!).
6. На DC03 журнал Security: 4771 (KDC pre-auth failed) отсутствует — до Kerberos дело не дошло.

**Решение:** вернуть `10.10.10.12` в CF, `Clear-DnsServerCache -Force` на DC01/DC02, на клиенте `ipconfig /flushdns; klist purge`, повторить `net use \\FS01\Obshchee /user:vektor-partner\kuznetsov *` — успех.

**Ответ пользователю:** «Проблема была в разрешении имён до домена партнёра на нашем DNS-сервере, траст не пострадал. Исправлено, доступ восстановлен, проверил под вашей учётной записью. Если повторится — сразу пишите, займёт минуту».

**Закрытие:** зафиксировать инцидент в `stand-log.md` строкой «инцидент: CF на DC01 указывал на несуществующий IP, RCA — ручная правка зоны без согласования», категория «изменение конфигурации DNS».

### Что изменилось на стенде
- **DC03** — новая ВМ: Windows Server 2022 Evaluation, 10.10.10.12, лес `vektor-partner.lab`, NetBIOS `VPARTNER`, роль AD DS + DNS.
- **Forest trust** `corp.vektor.lab` ↔ `vektor-partner.lab`, two-way, forest-wide.
- **Conditional forwarders:** DC01/DC02 → `vektor-partner.lab` = 10.10.10.12; DC03 → `corp.vektor.lab` = 10.10.10.10.
- **DC03:** OU `Partner`, пользователь `kuznetsov`, группа `GG-Partner`.
- **FS01:** в ACL `\\FS01\Obshchee` добавлена `vektor-partner\GG-Partner` (Read).
- **DC01/VEKTOR:** пользователь `VEKTOR\kuznetsov` в OU `Company\Склад`, член `GG-Sklad`.

### Откат
`VM → Snapshot → Snapshot Manager` → выбрать `before-sysadmin-015` → **Revert** на DC01, DC02, FS01, WIN11-CL01. Для DC03: либо revert её снапшота (перед промоушеном), либо, если снапшота нет, удалить ВМ целиком и удалить остатки траста/CF на DC01 (`netdom trust corp.vektor.lab /domain:vektor-partner.lab /remove`) и CF-зону. После отката — обязательно почистить `stand-log.md`, чтобы журнал соответствовал реальному состоянию.

## 6. Ошибки новичков и безопасность

1. **«Сделаю двусторонний forest trust на всякий случай»** → любой пользователь партнёрского леса аутентифицируется во всём вашем лесу, поверхность атаки растёт мгновенно. → Делать траст минимально необходимым по направлению, аутентификацию — selective, для конкретных групп.
2. **Начинать миграцию без инвентаризации** (кто, какие группы, какие ACL, какие сервисные учётки) → половина доступов «отвалилась», а вы не знаете, что чинить. → Таблица «источник → цель → владелец → проверка» до первой команды.
3. **Править DNS «по памяти» без проверки с обеих сторон** → траст жив, а аутентификация нет. → Стандарт: `Resolve-DnsName` с клиента, с DC-источника, с DC-цели.
4. **Оставить `sIDHistory` и отключённый SID filtering навсегда** → чужой лес может подсунуть SID привилегированной группы (Domain Admins) и получить права у вас. → SIDHistory — только на окно миграции, затем переписать ACL на группы и очистить `sIDHistory`.
5. **Переносить пароли «руками» и объявлять всем новый пароль в чате** → утечка и потеря доверия. → PES/ADMT либо принудительная смена пароля при первом входе после миграции.
6. **Выводить DC из домена без переноса FSMO и GC** → ошибки `netdom query fsmo`, недоступные PDC-операции, отказ смены паролей. → `netdom query fsmo` → `Move-ADDirectoryServerOperationMasterRole` → `ntdsutil` metadata cleanup → проверка репликации.
7. **Мигрировать без бэкапа System State и без снапшота** → нечего откатывать. → Снапшот + `wbadmin`/бэкап System State на DC до работ.
8. **Молча менять чужой домен** (правки в партнёрском лесу) → политический конфликт и неработающий траст. → Всё, что за границей вашего леса, — через согласование и с письменным окном работ.

**Безопасность.** В боевой среде нельзя: держать открытый forest-wide trust с внешней организацией без срока и владельца; включать `/enablesidhistory` без плана выключения; хранить пароли trust account в вики; давать партнёрам доступ к `\\FS01\Buh`/`\\FS01\HR` «пока тестируем». Всё, что вы делаете с трастами, должно быть в регламенте: кто владелец траста, зачем, до какой даты, кто проверяет раз в квартал.

## 7. Вопросы на собеседовании

**1. Чем лес отличается от домена и какие там границы безопасности?**
Ответ: домен — граница политики безопасности и репликации одной базы `NTDS.dit`; лес — граница безопасности целиком, включая схему, конфигурацию, `Enterprise Admins`/`Schema Admins` и SID filtering между лесами. Домены внутри леса доверяют друг другу автоматически и транзитивно; между лесами доверие только ручное, через траст.
*Что хочет услышать:* что кандидат не считает «домен = лес» и понимает, что `Enterprise Admins` — это Tier 0 на весь лес.
*Красный флаг:* «лес — это просто несколько доменов, разницы нет».

**2. Какие бывают трасты и чем forest trust отличается от external?**
Ответ: parent-child и tree-root (автоматические, транзитивные), shortcut (оптимизация пути внутри леса), forest (транзитивный для всех доменов леса, поддерживает name suffix routing и forest-aware SID filtering), external (домен-в-домен, не транзитивный, SID filtering включён), realm (с Kerberos-реалмом, например MIT/FreeIPA). Направление — incoming/outgoing; аутентификация — forest-wide или selective.
*Что хочет услышать:* связку «тип траста → транзитивность → поведение SID filtering».
*Красный флаг:* «все трасты одинаковые, кроме названия».

**3. Что такое SID filtering и что такое sIDHistory?**
Ответ: SID filtering (quarantine) не пропускает в токен доступа SID, выданные за пределами доверенного леса — защита от инъекции привилегированных SID. `sIDHistory` — атрибут, хранящий старый SID пользователя после миграции, чтобы старые ACL на файловых ресурсах продолжали работать. Для миграции SID filtering ослабляют (и включают приём SIDHistory) на время, потом ACL переводят на группы и историю чистят.
*Что хочет услышать:* понимание, что SIDHistory — временный костыль с риском.
*Красный флаг:* «включим SIDHistory и оставим, так удобнее».

**4. Как бы вы мигрировали 40 пользователей из леса партнёра в наш лес?**
Ответ: инвентаризация (учётки, группы, ACL, сервисные учётки, профили), снапшоты и бэкап System State, траст «принимающий доверяет источнику», ADMT (пользователи/группы/компьютеры, PES для паролей, `sIDHistory` на переходный период) либо greenfield: новые учётки + переприсоединение ПК + `robocopy` профилей + перевод ACL на группы. План проверки по каждому пользователю, окно работ, план отката, выключение `sIDHistory` и удаление траста по завершении.
*Что хочет услышать:* проект с этапами, а не «нажму migrate в ADMT».
*Красный флаг:* «ADMT всё сделает сам, план не нужен».

**5. Траст есть, пользователь не может открыть шару. Ваши действия?**
Ответ: с клиента — `klist`, `Resolve-DnsName` партнёрского домена, `nltest /dsgetdc:`; на DC — `Get-ADTrust`, `netdom trust /verify`, порты 88/389/445/3268, журнал Security (4768/4769/4771), `%windir%\debug\netlogon.log`; проверить ACL (есть ли группа партнёрского домена) и права logon. Порядок: имя → порт → траст → авторизация (ACL). Чаще всего виноват DNS.
*Что хочет услышать:* системный порядок диагностики, от дешёвого к дорогому.
*Красный флаг:* «сразу пересоздам траст».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Active Directory Domain Services»: статьи «How Domain and Forest Trusts Work», «Domain and Forest Trusts Technical Reference», «Netdom trust», «Active Directory Migration Tool (ADMT)». Поиск: `site:learn.microsoft.com domain and forest trusts how it works`, `admt guide inter-forest migration`.
- **learn.microsoft.com** — «Active Directory Domain Services command-line tools» и справка по `nltest`, `ntdsutil`, `rendom`, `Move-ADDirectoryServerOperationMasterRole`. Поиск: `nltest domain_trusts reference`, `ntdsutil metadata cleanup`.
- **learn.microsoft.com** — «Active Directory Domains and Trusts» (MMC-оснастка) и статью про SID filtering / «SID Filtering and Quarantine» `[ПРОВЕРИТЬ точное название статьи]`. Поиск: `sid filtering quarantine forest trust`.
- **learn.microsoft.com** — справка по DNS Server PowerShell (`Add-DnsServerConditionalForwarderZone`, `Add-DnsServerForwarder`). Поиск: `dnsserver conditional forwarder powershell`.
- **Доступность ADMT:** утилита распространялась через Центр загрузок Microsoft; в 2026 году скачивание из РФ может быть затруднено `[ПРОВЕРИТЬ актуальную страницу и поддержку Windows Server 2022]`. Легальные альтернативы: миграция скриптами PowerShell + переприсоединение ПК, либо коммерческие инструменты (Quest/Dell Migration Manager for AD) — только с лицензией.
- **Windows Server 2022 Evaluation** — страница оценки на **microsoft.com** (180 дней, продление переустановкой `[ПРОВЕРИТЬ условия]`). Поиск: `Windows Server 2022 evaluation download`.
