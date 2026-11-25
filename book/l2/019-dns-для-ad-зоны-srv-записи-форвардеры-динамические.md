# DNS для AD: зоны, SRV-записи, форвардеры, динамические обновления, диагностика

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 9:20. Тикет от ivanov: «Пользователь / WIN11-CL01 / Добрый день! С утра не открывается `\\FS01\Buh` — пишет "Не найден сетевой путь". Если вбить `\\10.10.10.20\Buh` — открывается. И вход в систему теперь идёт минуты две, хотя вчера было нормально. Срочность: высокая, закрываю период».

Классика. Половина «непонятных» проблем с AD, входом в систему, доступом к шарам, принтерам и 1С — это DNS. Active Directory не умеет искать свои сервисы «по IP»: клиент спрашивает у DNS, где живут LDAP и Kerberos, и если ответ неверный, всё остальное разваливается каскадом. После этой главы ты должен уметь: читать зоны и понимать, где они физически хранятся и как реплицируются; находить и объяснять SRV-записи; настраивать форвардеры и условные форвардеры; включать защищённые динамические обновления и скавенджинг без риска снести живые записи; диагностировать DNS штатными средствами Windows (`dcdiag /test:dns`, `Resolve-DnsName`, `nslookup`, `dnscmd`) и с Linux (`dig`), и — главное — отличать «сломался DNS» от «сломалось что-то другое».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Зачем AD нужен DNS.** В AD нет центрального «справочника серверов». Клиент при входе выполняет **DC Location (DC Locator)**: сначала определяет свой сайт (по подсетям из урока 18), затем отправляет DNS-запрос на поиск SRV-записей служб. Получив список контроллеров домена, он выбирает ближайший и идёт к нему по LDAP/Kerberos. Нет правильного SRV — клиент вообще не найдёт домен.

**Уровень модели OSI:** DNS — прикладной уровень (L7), транспорт — UDP/53 для обычных запросов (до 512 байт, дальше TCP или EDNS0) и TCP/53 для зонных трансферов и больших ответов. Рядом живут: Kerberos 88 (TCP/UDP), LDAP 389, LDAPS 636, Global Catalog 3268/3269, SMB 445, RPC 135, W32Time 123/UDP.

**Зоны.** Зона — это часть пространства имён, за которую сервер отвечает. В AD-домене `corp.vektor.lab` при установке DC создаются минимум две:
- `corp.vektor.lab` — основная зона домена;
- `_msdcs.corp.vektor.lab` — служебная зона с SRV-записями. При AD-интеграции это **отдельная зона**, которая реплицируется на все DNS-серверы леса (раздел `ForestDnsZones`), а не только домена. Это сделано, чтобы контроллеры из других доменов леса тоже находили записи.

Типы зон: **primary** (главная, источник истины), **secondary** (копия только для чтения, получает данные зонным трансфером), **stub** (копия только NS/SOA/A-записей серверов зоны — чтобы знать, куда пересылать запросы). Плюс **AD-integrated** — это не отдельный тип, а *способ хранения*: данные зоны лежат не в файле `.dns`, а в каталоге AD, и реплицируются по обычной репликации AD.

Разделы приложений (application partitions), где живут AD-зоны:
| Раздел | Что там | Область репликации |
|---|---|---|
| `DomainDnsZones.corp.vektor.lab` | зона `corp.vektor.lab` (по умолчанию) | все DNS-серверы домена |
| `ForestDnsZones.corp.vektor.lab` | зона `_msdcs.corp.vektor.lab` | все DNS-серверы леса |

**SRV-запись.** Формат: `_service._proto.name TTL class SRV priority weight port target`. Приоритет — меньше значит лучше; вес — «размазывает» клиентов между равными по приоритету серверами. Что должно быть в домене:

```text
_ldap._tcp.corp.vektor.lab                       -> dc01/dc02 :389   (обычный LDAP)
_ldap._tcp.dc._msdcs.corp.vektor.lab             -> dc01/dc02 :389   (вход в домен)
_ldap._tcp.<site>._sites.dc._msdcs.corp.vektor.lab -> DC этого сайта (site-aware)
_kerberos._tcp.corp.vektor.lab                   -> dc01/dc02 :88
_kerberos._tcp.dc._msdcs.corp.vektor.lab         -> dc01/dc02 :88
_kpasswd._tcp.corp.vektor.lab                    -> dc01/dc02 :464  (смена пароля)
_gc._tcp.corp.vektor.lab                         -> dc01/dc02 :3268 (глобальный каталог)
_ldap._tcp.pdc._msdcs.corp.vektor.lab            -> DC с ролью PDC Emulator
```

Кто их создаёт? Служба **Netlogon** на каждом DC при старте (и раз в час) регистрирует свой набор записей в зоне. Эталонный список она пишет в файл `C:\Windows\System32\config\netlogon.dns` — по нему удобно сверять, чего не хватает.

**Динамические обновления.** Записи A/PTR клиентов и SRV контроллеров регистрируются автоматически (RFC 2136). Режимы зоны: `None`, `NonsecureAndSecure`, `Secure`. В `Secure` клиент должен сначала доказать, что он — это он (Kerberos/GSS-TSIG), и становится **владельцем** записи (ACL на объект в AD). Это защищает от подмены чужого имени. DHCP-сервер, регистрирующий записи за клиентов, создаёт их «от себя» — поэтому его учётку добавляют в группу **DnsUpdateProxy**, иначе владельцем будет DHCP, а скавенджинг или смена адреса создадут путаницу.

**Скавенджинг (очистка).** Запись с включённой отметкой времени (timestamp) проходит: no-refresh interval (по умолчанию 7 дней, обновления не продлевают жизнь) → refresh interval (7 дней, обновление продлевает) → удаление при скавенджинге. Итого «мёртвая» запись живёт около 14 дней. Записи, созданные вручную, по умолчанию имеют timestamp = 0 и не удаляются.

**Форвардеры.** DC — рекурсивный резолвер, но по умолчанию он ходит в интернет по root hints (корневым серверам), что медленно и часто блокируется провайдером. **Forwarder** — «спроси вот этот сервер» для всего, что не в моих зонах. **Условный форвардер (conditional forwarder)** — то же, но для конкретного домена (например, домена партнёра или DMZ). Внутренняя зона всегда имеет приоритет над форвардером.

```text
Клиент WIN11-CL01
      |  DNS-запрос _ldap._tcp.dc._msdcs.corp.vektor.lab (UDP/53)
      v
DC01 10.10.10.10  --- AD-репликация ---  DC02 10.10.10.11
   зона corp.vektor.lab  (DomainDnsZones)
   зона _msdcs.corp.vektor.lab (ForestDnsZones)
      |
      |  нет в моих зонах? -> forwarder (UDP/53) -> внешний резолвер
      v
   интернет / другой DNS
```

## 3. Аналогия из жизни

Представь городскую справочную. Есть картотека улиц: «улица Ленина, дом 20 — это FS01» — это A-записи. Есть отдельная картотека «где сидит касса, где сидит паспортный стол, где главный по городу» — это SRV-записи: ты не знаешь адрес кассы, ты знаешь, что справочная скажет, в каком здании она сегодня принимает. Менеджер справочной, который сам не знает ответа, звонит в соседнюю справочную (форвардер), а для одного конкретного района у него есть прямой телефон (условный форвардер). Записи в картотеку подают сами жители (динамические обновления), но с 2026 года — только с паспортом (защищённые обновления), а раз в две недели картотекарша выкидывает карточки, по которым давно никто не звонил (скавенджинг). Аналогия перестаёт работать в одном месте: справочная не умеет подтверждать подлинность карточки криптографически — а DNS с DNSSEC именно это и делает.

## 4. Практика и команды

**1. Инвентаризация зон и их области репликации**

```powershell
Get-DnsServerZone | Format-Table ZoneName, ZoneType, IsDsIntegrated, ReplicationScope, DynamicUpdate -AutoSize
Get-DnsServerDirectoryPartition | Format-Table DirectoryPartitionName, State -AutoSize
```

`IsDsIntegrated = True` — зона в AD. `ReplicationScope` покажет `Domain`, `Forest` или `Legacy`. Смотреть: есть ли `_msdcs.corp.vektor.lab` и стоит ли у неё `Forest`.

```text
ZoneName             ZoneType IsDsIntegrated ReplicationScope DynamicUpdate
--------             -------- -------------- ---------------- -------------
_msdcs.corp.vektor.lab Primary  True          Forest           Secure
corp.vektor.lab      Primary  True          Domain           Secure
```

**2. Все SRV-записи зоны с расшифровкой**

```powershell
Get-DnsServerResourceRecord -ZoneName "_msdcs.corp.vektor.lab" -RRType Srv |
  Select-Object HostName,
    @{n='Target';e={$_.RecordData.DomainName}},
    @{n='Port';e={$_.RecordData.Port}},
    @{n='Prio';e={$_.RecordData.Priority}},
    @{n='Weight';e={$_.RecordData.Weight}}
```

Ищем наличие `_ldap._tcp.dc`, `_kerberos._tcp.dc`, `_ldap._tcp.pdc`, site-записей. Если запись есть только на DC01, а DC02 нет — репликация DNS или Netlogon барахлит.

**3. Проверка глазами клиента: `Resolve-DnsName`** (запускать на WIN11-CL01)

```powershell
Resolve-DnsName -Name _ldap._tcp.dc._msdcs.corp.vektor.lab -Type SRV -Server 10.10.10.10
Resolve-DnsName -Name fs01.corp.vektor.lab -Server 10.10.10.10
```

`-Server` — спросить конкретный DNS, минуя клиентский кэш. Типичный ответ: `NameTarget : dc01.corp.vektor.lab`, `Port : 389`, `Priority : 0`, `Weight : 100`.

**4. `nslookup` — старый, но живёт в каждом собеседовании**

```cmd
nslookup -type=SRV _kerberos._tcp.corp.vektor.lab
nslookup fs01.corp.vektor.lab 10.10.10.10
nslookup -type=SOA corp.vektor.lab
```

В интерактивном режиме `nslookup` → `server 10.10.10.10` → `set type=SRV` → `_gc._tcp.corp.vektor.lab`. Смотреть: ответил ли нужный сервер (`Server:`) и что в `Non-authoritative answer` (значит, отдал кэш, а не зона).

**5. `dcdiag` — главный инструмент диагностики DNS в AD**

```cmd
dcdiag /test:dns /v /e > C:\Temp\dcdiag-dns.txt
```

`/test:dns` — набор DNS-тестов, `/v` — подробно, `/e` — по всем DC леса. Внутри подтестов: `DNS: Connectivity`, `DNS: Forwarders`, `DNS: Delegation`, `DNS: Dynamic update`, `DNS: Record registration`, `DNS: Resolve external name`. Ищем строки `... passed` и `... failed`. Отдельный подтест: `dcdiag /test:dns /DnsRecordRegistration`.

**6. Принудительная перерегистрация записей**

```cmd
ipconfig /registerdns
nltest /dsregdns
type C:\Windows\System32\config\netlogon.dns
```

`ipconfig /registerdns` — клиент перерегистрирует свой A/PTR. `nltest /dsregdns` — DC перерегистрирует SRV через Netlogon (не забыть перезапустить `Netlogon` после правок, если записи не появились: `Restart-Service Netlogon`).

**7. Кэш клиента**

```powershell
Get-DnsClientCache | Where-Object Entry -like "fs01*"
Clear-DnsClientCache
ipconfig /flushdns
```

`Get-DnsClientCache` показывает TTL и источник. `Clear-DnsClientCache` = `ipconfig /flushdns`. Частая история: админ поменял A-запись, а клиент 20 минут держит старую из кэша.

**8. Форвардеры**

```powershell
Get-DnsServerForwarder
Add-DnsServerForwarder -IPAddress 1.1.1.1
Set-DnsServerForwarder -IPAddress 1.1.1.1 -UseRootHint $false
```

`Get-DnsServerForwarder` вернёт `IPAddress` и `UseRootHint`. `-UseRootHint $false` заставляет сервер при недоступности форвардеров **не** идти по корневым серверам (в закрытом контуре это правильно). Условный форвардер (пример синтаксиса, в лабе не выполняем):

```powershell
Add-DnsServerConditionalForwarderZone -Name "dmz.vektor.lab" -MasterServers 10.10.20.10
```

GUI: `dnsmgmt.msc` → свойства сервера → **Forwarders / Условные пересылатели**; свойства зоны → **Zone Transfers / Dynamic Updates**.

**9. Режим динамических обновлений и скавенджинг**

```powershell
Set-DnsServerPrimaryZone -Name "corp.vektor.lab" -DynamicUpdate Secure
Get-DnsServerZoneAging -Name "corp.vektor.lab"
Set-DnsServerZoneAging -Name "corp.vektor.lab" -Aging $true -NoRefreshInterval 7.00:00:00 -RefreshInterval 7.00:00:00
Set-DnsServerScavenging -ScavengingState $true -ScavengingInterval 1.00:00:00
Start-DnsServerScavenging
```

`-DynamicUpdate` принимает `None | NonsecureAndSecure | Secure`. `NoRefreshInterval`/`RefreshInterval` — формат `дни.чч:мм:сс`. `Start-DnsServerScavenging` запускает очистку немедленно (полезно, чтобы не ждать сутки).

**10. `dnscmd` — то же самое, но из CMD (часто единственное, что есть на чужом сервере)**

```cmd
dnscmd /enumzones
dnscmd /zoneinfo corp.vektor.lab
dnscmd /clearcache
dnscmd /zonerefresh corp.vektor.lab
```

`/zoneinfo` покажет тип зоны, режим обновлений, allow-update, aging. `/clearcache` чистит кэш сервера (осторожно в бою — кратковременный рост нагрузки).

**11. Проверка с Linux (UBU01)**

```bash
sudo apt install -y dnsutils
dig @10.10.10.10 _ldap._tcp.corp.vektor.lab SRV +short
dig @10.10.10.10 fs01.corp.vektor.lab A
resolvectl status | head -20
```

`+short` — только ответ. Смотреть: отвечает ли DC01, отдаёт ли он SRV с `0 100 389 dc01.corp.vektor.lab`. Если `dig` без `@` возвращает `SERVFAIL` — проблема в `/etc/resolv.conf` клиента, а не в DNS.

**12. `Test-DnsServer` — быстрый health-check**

```powershell
Test-DnsServer -IPAddress 10.10.10.10 -ZoneName corp.vektor.lab
```

Прогоняет базовый набор проверок сервера и зоны, возвращает объект с результатами по каждому тесту. Удобно для «а сервер вообще живой?».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны: **DC01** (10.10.10.10, AD DS + DNS), **DC02** (10.10.10.11, второй DC из урока 17, DNS-сервер), **WIN11-CL01** (DHCP, клиент `ivanov`), **UBU01** (10.10.10.30, SSH). Домен `corp.vektor.lab` работает, сайты и подсети из урока 18 созданы. Имя сайта, которое ты задавал в уроке 18, подставляй вместо `<site>` в примерах ниже.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя:

```text
before-l2-019
```

Для DC01, DC02, WIN11-CL01, UBU01. Не пропускай DC02 — правки DNS уедут в репликацию.

### Шаги

1. **DC01.** Открой PowerShell от админа домена (`adm.student`). Выполни `Get-DnsServerZone | ft ZoneName,IsDsIntegrated,ReplicationScope,DynamicUpdate -AutoSize`. Ожидаемо: `corp.vektor.lab` (Scope=Domain) и `_msdcs.corp.vektor.lab` (Scope=Forest). Если `_msdcs` нет — зона не AD-интегрирована, это надо чинить до всего остального.

2. **DC01.** `Get-DnsServerDirectoryPartition` — увидишь `DomainDnsZones.corp.vektor.lab` и `ForestDnsZones.corp.vektor.lab` в состоянии `DNS`/`Enlisted`. Ожидаемо: оба раздела есть.

3. **DC01.** Выгрузи все SRV-записи (команда из п.4.2) в файл: `... | Out-File C:\Temp\srv-before.txt`. Ожидаемо: `_ldap._tcp.dc`, `_kerberos._tcp.dc`, `_kpasswd._tcp`, `_gc._tcp`, `_ldap._tcp.pdc`, site-записи `_ldap._tcp.<site>._sites.dc`.

4. **DC01 → DC02.** Повтори п.1 и п.3 на DC02. Ожидаемо: тот же набор записей (разница только в целевом имени DC в `Target`). Если на DC02 пусто — смотри `repadmin /showrepl` по разделам `DomainDnsZones`/`ForestDnsZones`.

5. **WIN11-CL01.** `Resolve-DnsName _ldap._tcp.dc._msdcs.corp.vektor.lab -Type SRV -Server 10.10.10.10` и то же с `-Server 10.10.10.11`. Ожидаемо: оба сервера отдают оба DC с `Port 389`, `Priority 0`.

6. **WIN11-CL01.** Проверь site-awareness: `Resolve-DnsName "_ldap._tcp.<site>._sites.dc._msdcs.corp.vektor.lab" -Type SRV`. Ожидаемо: в ответе DC твоего сайта. Затем `nltest /dsgetdc:corp.vektor.lab` — в выводе `DC: \\DC01...` и `Our Site Name: <site>`.

7. **DC01.** Проверь режим динамических обновлений и включи защищённый, если стоит иначе: `Set-DnsServerPrimaryZone -Name corp.vektor.lab -DynamicUpdate Secure`. Затем **WIN11-CL01**: `ipconfig /registerdns`, через 10–15 секунд — `Resolve-DnsName win11-cl01.corp.vektor.lab -Server 10.10.10.10`. Ожидаемо: запись с актуальным DHCP-адресом и ненулевым timestamp.

8. **UBU01.** Попробуй «в лоб» зарегистрировать запись без аутентификации:
```bash
sudo apt install -y dnsutils
nsupdate -v <<'EOF'
server 10.10.10.10
zone corp.vektor.lab
update add hacker.corp.vektor.lab 300 A 10.10.10.99
send
EOF
```
Ожидаемо: `update failed: REFUSED` — зона принимает только защищённые обновления. Это ровно то, что мы хотим увидеть.

9. **DC01.** Включи скавенджинг: `Set-DnsServerZoneAging -Name corp.vektor.lab -Aging $true -NoRefreshInterval 7.00:00:00 -RefreshInterval 7.00:00:00`, `Set-DnsServerScavenging -ScavengingState $true -ScavengingInterval 1.00:00:00`. Проверь `Get-DnsServerZoneAging -Name corp.vektor.lab`.

10. **DC01.** Настрой форвардер (если в VMnet8 есть интернет через NAT): `Add-DnsServerForwarder -IPAddress 1.1.1.1` и `Set-DnsServerForwarder -UseRootHint $false`. Нет интернета — просто зафиксируй вывод `Get-DnsServerForwarder` и шаг 11 пропусти. То же на DC02 (не DC01 в качестве форвардера — DC не должен форвардить на партнёра, это создаёт петли).

11. **WIN11-CL01.** `Resolve-DnsName learn.microsoft.com -Server 10.10.10.10` — внешнее имя резолвится через форвардер; `Resolve-DnsName fs01.corp.vektor.lab -Server 10.10.10.10` — внутреннее имя берётся из своей зоны (в ответе `NameHost`/A-запись, а не «пересланный» ответ). Ожидаемо: и то, и другое работает.

12. **DC01.** `dcdiag /test:dns /v /e > C:\Temp\dcdiag-dns-after.txt`, открой файл и найди блок по каждому DC. Ожидаемо: все подтесты `passed`. Сохрани файл — это baseline.

13. **Запись в журнал.** Допиши в `stand-log.md` всё, что изменил (см. ниже). Урок 19 — точка, на которую потом ссылаются уроки 20 (Kerberos/SPN) и 28 (диагностика AD).

### Проверка

- [ ] `Get-DnsServerZone` на DC01 и DC02 показывает обе зоны, `IsDsIntegrated = True`
- [ ] `ReplicationScope` у `_msdcs.corp.vektor.lab` = `Forest`, у `corp.vektor.lab` = `Domain`
- [ ] В зоне `_msdcs` есть `_ldap._tcp.dc`, `_kerberos._tcp.dc`, `_kpasswd._tcp`, `_gc._tcp`, `_ldap._tcp.pdc`
- [ ] Site-записи `_ldap._tcp.<site>._sites.*` присутствуют
- [ ] `Resolve-DnsName ... -Type SRV` возвращает оба DC и с 10.10.10.10, и с 10.10.10.11
- [ ] `DynamicUpdate = Secure` у обеих зон
- [ ] `nsupdate` с UBU01 получает `REFUSED`
- [ ] `Get-DnsServerZoneAging` показывает `Aging = True`, интервалы 7/7
- [ ] `dcdiag /test:dns /v /e` — без `failed`

### Сломай и почини

> **Пользователь:** Петрова Мария (`petrova`)
> **ВМ:** WIN10-CL02
> **Что написала:** «Не могу зайти на `\\FS01\Buh` — "Не найден сетевой путь". По IP `\\10.10.10.20\Buh` открывается. И вход в систему стал долгим, минуты две. Срочность: средняя».
> **Срочность:** средняя

**Как воспроизвести поломку.** На DC01 в `dnsmgmt.msc` открой зону `corp.vektor.lab`, найди A-запись `fs01` и измени её с 10.10.10.20 на неверный адрес, например 10.10.10.220 (`Set-DnsServerResourceRecord` или через GUI: правый клик по записи → **Properties** → изменить IP → **OK**). Либо, что ближе к реальности, «удали лишнюю запись»: удали `fs01` целиком. Дождись репликации на DC02 (`repadmin /syncall /AdeP`) и на WIN10-CL02 выполни `ipconfig /flushdns`.

**Диагностика по шагам:**

1. На WIN10-CL02: `ipconfig /all` — какой DNS-сервер выдан DHCP? Должен быть 10.10.10.10 (и/или .11). Если 8.8.8.8 или 10.10.10.30 — проблема в DHCP/настройках клиента, а не в зоне.
2. `Resolve-DnsName fs01.corp.vektor.lab` — имя не резолвится или резолвится в неправильный IP. Значит, проблема на стороне DNS, а не SMB.
3. `Resolve-DnsName fs01.corp.vektor.lab -Server 10.10.10.10` и `-Server 10.10.10.11` — сравни. Если один отвечает, а второй нет — репликация зоны. Если оба «плохие» — запись битая в самой зоне.
4. На DC01: `Get-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name fs01` — есть ли запись и какой у неё IP.
5. На FS01: `ipconfig /all` — реальный адрес. Сравни с записью в DNS.
6. Причина найдена → на FS01 `ipconfig /registerdns`, `Restart-Service Netlogon` (для SRV), проверить запись снова.
7. Проверить `dcdiag /test:dns /DnsRecordRegistration`, чтобы убедиться, что автоперерегистрация вообще работает.

**Решение:** восстановить корректную A-запись `fs01 → 10.10.10.20` (через `ipconfig /registerdns` на FS01 или вручную), дать репликации разойтись (`repadmin /syncall /AdeP`), на клиенте `Clear-DnsClientCache`, проверить `Resolve-DnsName` и открыть шару.

**Ответ пользователю:** «Мария, добрый день! Причина была в DNS: имя сервера `FS01` временно указывало на неверный адрес, поэтому Windows не находила сервер по имени (а по IP всё работало). Запись исправлена, кэш на вашем компьютере сброшен. Проверьте, пожалуйста, `\\FS01\Buh` — должно открываться. Если снова повторится, сразу напишите — посмотрим журналы регистрации DNS. Заявку закрываю, при повторении открою заново».

**Закрытие заявки:** категория «Сеть / DNS», приоритет средний, время решения; в описании — корневая причина (некорректная A-запись), действие (перерегистрация через `ipconfig /registerdns`), профилактика (проверить, что FS01 не имеет второго NIC/неверной статики, включён скавенджинг с адекватными интервалами).

### Что изменилось на стенде

- DC01 и DC02: на зонах `corp.vektor.lab` и `_msdcs.corp.vektor.lab` подтверждён режим `DynamicUpdate = Secure`.
- DC01 и DC02: включён скавенджинг (`ScavengingState = True`, интервал 1 день) и aging зоны `corp.vektor.lab` (no-refresh 7 дней, refresh 7 дней).
- DC01/DC02: настроены (или проверены) форвардеры, `UseRootHint` — по факту выполнения шага 10.
- WIN11-CL01: зарегистрирована A-запись `win11-cl01.corp.vektor.lab` с timestamp; PTR — если обратная зона `10.10.10.in-addr.arpa` создана, иначе отметить в журнале как «PTR не настроен».
- UBU01: установлен пакет `dnsutils`.
- FS01: подтверждена A-запись `fs01 → 10.10.10.20` (после починки).
- Артефакты: `C:\Temp\srv-before.txt`, `C:\Temp\dcdiag-dns-after.txt` на DC01.
- Запись в `stand-log.md` с датой и версией снапшота `before-l2-019`.

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбери `before-l2-019` → **Go to**. Сделай это для DC01, DC02, WIN11-CL01, UBU01. Если менял DNS-записи только на DC01, а репликация уже разошлась — откатывай **оба** DC, иначе получишь расхождение зон и «обратно всплывшие» записи. После отката на клиентах: `Clear-DnsClientCache`. В `stand-log.md` добавь строку «откат к before-l2-019, причина: …».

## 6. Ошибки новичков и безопасность

1. **Один DC указывает DNS-сервером сам себя.** → При падении DC домен «умирает», потому что второй сервер никто не спрашивал. → Правильно: DC01 → primary 10.10.10.11, secondary 10.10.10.10; DC02 — наоборот. Loopback (127.0.0.1) допустим только как дополнительный адрес.
2. **Прописать клиенту публичный DNS (8.8.8.8) «чтобы интернет работал».** → Внешние имена резолвятся, внутренние — нет; вход в домен медленный или невозможен. → Клиенты и серверы домена должны смотреть только на внутренние DNS, а внешний мир отдавать форвардером.
3. **Удалить зону `_msdcs.corp.vektor.lab` или вручную почистить SRV «чтобы не мешали».** → Клиенты не находят DC, ломается вход, Kerberos, GPO, доверие. → SRV-записями управляет Netlogon; чинить через `nltest /dsregdns`, а не ластиком.
4. **Включить `NonsecureAndSecure` ради удобства.** → Любой в сети может зарегистрировать `dc01.corp.vektor.lab` на свой IP → перехват аутентификации. → Только `Secure`.
5. **Агрессивный скавенджинг (no-refresh 1 день).** → Живые записи клиентов, которые не в сети 2 дня, удаляются, и они «не находят» серверы. → 7/7 по умолчанию, включать только вместе с aging и после проверки baseline.
6. **Путать «изменил на DC02» с «изменилось везде».** → Админ правит запись на DC02, клиент спрашивает DC01, видит старое. → Всегда `repadmin /syncall /AdeP` + проверка на обоих.
7. **Мультихоумный DC регистрирует SRV на все интерфейсы сразу.** → Часть клиентов получает недоступный адрес, вход «через раз». → Настройте регистрацию только нужного интерфейса (в свойствах DNS/сетевого адаптера) или отключите лишний NIC.
8. **DHCP регистрирует записи, а его учётка не в `DnsUpdateProxy`.** → Записи «принадлежат» DHCP, скавенджинг и смена адреса дают дубли/осиротевшие записи. → Добавить учётку DHCP в группу `DnsUpdateProxy` (и понимать, что членство даёт право писать в DNS — это привилегированная группа).

### Безопасность

В боевой среде **нельзя**: открывать зону на трансфер «всем» (только конкретным серверам), ставить `NonsecureAndSecure`, выставлять DNS-сервер DC в интернет, разрешать условные форвардеры на недоверенные адреса, «для теста» давать права на зону широкой группе. Группа **DNSAdmins** и локальные админы на DC — это уровень Tier 0: у них есть техники выполнения кода в контексте DNS-службы, работающей как SYSTEM; членство туда даётся осознанно и логируется. Форвардеры за пределы своей сети отправляйте только по доверенному каналу (в идеале — DoH/DoT на внешнем резолвере), иначе провайдер/посредник видит все ваши запросы. И помни: у нас стенд с единым учебным паролем — в реальной жизни такой практики нет.

## 7. Вопросы на собеседовании

**1. «Как клиент находит контроллер домена?»**
Ответ: через DC Locator. Клиент определяет свой сайт по подсети (`Configuration\Sites`), затем запрашивает у DNS SRV-записи: `_ldap._tcp.dc._msdcs.<domain>` для входа, site-specific `_ldap._tcp.<site>._sites.dc._msdcs.<domain>` для выбора ближайшего DC, `_kerberos._tcp` для аутентификации, `_gc._tcp` для глобального каталога, `_ldap._tcp.pdc._msdcs` для PDC Emulator. Записи регистрирует Netlogon на каждом DC.
*Что хочет услышать:* понимание связки «сайты + SRV + Netlogon», а не просто «через DNS».
*Красный флаг:* «клиент обращается к первому DC из списка в AD» — не понимает роли DNS.

**2. «Чем AD-integrated зона отличается от обычной primary?»**
Ответ: хранится в каталоге AD в разделе приложений (`DomainDnsZones`/`ForestDnsZones`), реплицируется по мультимастерной модели вместе с AD, а не зонным трансфером; можно задать область репликации (домен/лес/конкретные серверы); есть ACL на каждой записи — отсюда защищённые динамические обновления; бэкапится вместе с System State.
*Что хочет услышать:* мультимастер, разделы приложений, ACL на записи.
*Красный флаг:* «AD-зона не реплицируется» или путаница с secondary.

**3. «Зачем нужны форвардеры и чем отличаются от корневых подсказок и условных форвардеров?»**
Ответ: форвардер — сервер, которому DC отдаёт всё, что не входит в его зоны; быстрее и управляемее, чем обход root hints, и позволяет контролировать трафик. Root hints — встроенный список корневых серверов, работает без настройки, но часто медленный/блокируемый. Условный форвардер — пересылка только для конкретного домена (партнёр, DMZ, другой лес).
*Что хочет услышать:* что внутренние зоны всегда приоритетнее, и что нельзя форвардить DC на самого себя/партнёра по кругу.
*Красный флаг:* «форвардер и conditional forwarder — одно и то же».

**4. «Что такое защищённые динамические обновления и что будет, если включить незащищённые?»**
Ответ: `Secure` требует аутентификации клиента (Kerberos/GSS-TSIG), клиент становится владельцем записи через ACL. `NonsecureAndSecure` позволяет любому в сети зарегистрировать произвольное имя — классический вектор подмены (rogue DC, перехват аутентификации). Для DHCP-регистрации нужна группа `DnsUpdateProxy`.
*Что хочет услышать:* связку «ACL на записи + Kerberos» и знание про `DnsUpdateProxy`.
*Красный флаг:* «secure — это про шифрование DNS-трафика».

**5. «Пользователь жалуется, что не может войти в домен. Как проверить, что дело в DNS?»**
Ответ: с клиента `ipconfig /all` (правильный ли DNS), `Resolve-DnsName _ldap._tcp.dc._msdcs.<domain> -Type SRV`, `nltest /dsgetdc:<domain>`; с DC — `dcdiag /test:dns /v /e`, `Get-DnsServerResourceRecord`, проверка `netlogon.dns`, `ipconfig /registerdns` и `nltest /dsregdns`. Смотрим также время (Kerberos ±5 мин) и кэш (`Clear-DnsClientCache`).
*Что хочет услышать:* пошаговый порядок «клиент → DNS-сервер → зона → перерегистрация», а не «перезагрузите».
*Красный флаг:* сразу лезть в AD и пересоздавать учётку компьютера.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «DNS and Active Directory» / «Active Directory-Integrated DNS Zones»; поиск: `active directory integrated dns zones site:learn.microsoft.com`
- **learn.microsoft.com** — «Service locator resource records» / «DNS Records that are required for AD»; поиск: `service locator SRV records Active Directory site:learn.microsoft.com`
- **learn.microsoft.com** — «Dynamic update» и «Secure dynamic updates», «Aging and scavenging»; поиск: `dns aging scavenging no-refresh interval site:learn.microsoft.com`
- **learn.microsoft.com** — командлеты `DnsServer`; поиск: `Get-DnsServerResourceRecord DnsServer module site:learn.microsoft.com`
- **learn.microsoft.com** — `dcdiag` и тест DNS; поиск: `dcdiag /test:dns DnsBasic DnsRecordRegistration site:learn.microsoft.com`
- **learn.microsoft.com** — «Forwarders and conditional forwarders»; поиск: `DNS forwarders conditional forwarders root hints site:learn.microsoft.com`
- **ubuntu.com / bind9.readthedocs.io** — утилиты `dig`, `nsupdate`; поиск: `bind9 dig nsupdate documentation`
- Общие справочники по RFC 2782 (SRV), RFC 2136 (dynamic updates), RFC 4034 (DNSSEC) — **ietf.org**; поиск: `RFC 2782 SRV record ietf` `[ПРОВЕРИТЬ номер RFC для DNSSEC-записи перед публикацией]`
