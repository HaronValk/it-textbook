# DNS и DHCP как сервис: BIND, Unbound, PowerDNS, Kea, DNSSEC, split-horizon

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»:

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «После обеда не открывается `\\FS01\HR` — пишет "Не удаётся найти сетевой путь". У Иванова та же папка открывается. Интернет работает, сайты грузятся.»
> **Срочность:** высокая (до отчёта в налоговую час)

Дальше выясняется классика: утром вы переносили DHCP с DC01 на Kea, и в новом конфиге забыли опцию 6 (адреса DNS-серверов). Клиент при продлении аренды получил DNS, которого не ждали, — и разомкнулось всё, что зависит от имён: DFS-пути, вход в домен, принтеры, 1С.

DNS и DHCP — это два сервиса, без которых в корпоративной сети не работает **ничего**. При этом в L1 их обычно воспринимают как «галочки в оснастке Windows». На уровне сисаддина это уже полноценные сервисы с архитектурой: отдельный резолвер и отдельный авторитативный слой, отказоустойчивость (второй сервер, скрытый primary), DNSSEC, split-horizon (разные ответы для внутренних и внешних клиентов), API и автоматизация, миграция DHCP без простоя и регламент изменений.

После этой главы ты должен уметь: спроектировать разделение «рекурсивный резолвер / авторитативный сервер», поднять BIND, Unbound, PowerDNS и Kea с нуля на Linux, подписать зону DNSSEC и проверить валидацию, сделать split-horizon через views, перенести DHCP-скоуп с Windows на Kea и диагностировать «не резолвится имя» по шагам, а не по интуиции.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Две роли DNS-сервера.** Авторитативный (authoritative) знает содержимое своей зоны и отвечает фактами. Рекурсивный (resolver, кэш) бегает по иерархии от корня до нужного сервера, кэширует ответ по TTL и обслуживает клиентов. Смешивать их на одном сервере можно, но в корпоративной сети это разные функции и часто разные машины.

**Иерархия и делегирование.** Корень → TLD → зона компании → подзоны. Делегирование — это NS-записи в родительской зоне, указывающие, кто отвечает за дочернюю. В нашем стенде: зона `corp.vektor.lab` живёт на DC01 (AD-интегрированная, с динамическими обновлениями), а подзону `dmz.corp.vektor.lab` мы делегируем на Linux-серверы.

**Записи.** A/AAAA (адрес), CNAME (алиас), PTR (обратная зона), MX, TXT (SPF/DKIM/verification), SRV (AD: `_ldap._tcp.dc._msdcs...`), SOA (параметры зоны: serial, refresh, retry, expire, **negative TTL**), NS, и DNSSEC-записи: DNSKEY, RRSIG (подпись набора записей), DS (отпечаток ключа дочерней зоны в родителе), NSEC/NSEC3 (доказательство несуществования).

**Порты и транспорты.** DNS — 53/udp (обычные запросы), 53/tcp (большие ответы, AXFR/IXFR), DoT — 853/tcp, DoH — 443/tcp. DHCPv4 — 67/udp (сервер) и 68/udp (клиент), DHCPv6 — 547/udp. Уровень — прикладной (L7), но всё это висит на UDP и потому уязвимо к подделке источника: отсюда DHCP snooping, `allow-recursion`, rate limiting.

**DHCP: DORA.** Discover → Offer → Request → Ack. Сервер выдаёт адрес из пула, маску, шлюз, DNS (опция 6), домен (15), NTP, WPAD. Relay-агент (`option 82`) позволяет обслуживать чужие подсети: клиент в DMZ не слышит DHCP-сервер в офисе — запрос пересылает маршрутизатор.

**DNSSEC.** Не шифрует, а подтверждает подлинность: каждый набор записей подписан RRSIG, ключ зоны лежит в DNSKEY, а хэш ключа (DS) — в родительской зоне. Цепочка доверия идёт от корневого KSK. Валидирующий резолвер, получив ответ, проверяет подписи и ставит флаг `ad` (authenticated data). Невалидный ответ → SERVFAIL.

**Split-horizon.** Один и тот же зонный срез отдаёт разным клиентам разное (или вообще скрывает имена). В BIND это `view` с `match-clients`, в PowerDNS — отдельные зоны/инстансы либо views в свежих версиях `[ПРОВЕРИТЬ по документации PowerDNS: раздел Views]`.

```text
                        интернет
                           |
                    [ корневые NS ]
                           |
                   [ NS зоны "lab" ]  (условно)
                           |
        +--------- corp.vektor.lab ---------+
        |                                   |
   DC01 10.10.10.10                   (AD-интегрированная зона,
   AD DS + DNS + DHCP-скоуп            динамические обновления)
        |  делегирование dmz
        v
   DNS01 10.10.10.12 (BIND, views)  <-- AXFR --  PDNS01 10.10.10.14
   internal: отдаёт зону                         (hidden primary, SQLite,
   external: REFUSED                             API, DNSSEC-подпись)
        ^
        |  forward corp.vektor.lab -> 10.10.10.10
   MON01 10.10.10.40 (Unbound, валидирующий резолвер, DNSSEC + кэш)
        ^
   WIN11-CL01 / WIN10-CL02 (DHCP от KEA01 10.10.10.15, DNS = 10.10.10.10)
```

## 3. Аналогия из жизни

Представь большую библиотеку. **Авторитативный DNS** — это картотека конкретного зала: там точно записано, что «Толстой, том 3» стоит на полке 12-Б. **Рекурсивный резолвер** — это библиотекарь на входе: сам он книг не хранит, но знает, в какой зал идти, запоминает вчерашние запросы (кэш) и не бегает второй раз за тем же. **Делегирование** — это табличка на двери: «зал периодики переехал в корпус Б, спросите там». **DNSSEC** — печать и подпись на карточке: карточка не запечатана в конверт (прочитать можно), но если подпись не сходится с известным оттиском из головного офиса, библиотекарь книгу не выдаст. **Split-horizon** — два каталога: для сотрудников с пропуском и для посетителей; посетитель по внутреннему каталогу получает «такого зала нет». **DHCP** — администратор на входе, который выдаёт посетителю номерок на свободный шкафчик на сутки; если он выдаст двум людям один шкафчик, начнётся драка.

Аналогия ломается на кэше: библиотекарь помнит книгу «навсегда», а DNS-ответ живёт ровно столько, сколько указано в TTL, и до истечения этого срока изменения на authoritative-сервере никто не увидит.

## 4. Практика и команды

**1. Проверка синтаксиса и зон в BIND.** Правка конфига без проверки — самая частая причина «DNS лёг».

```bash
sudo named-checkconf -z /etc/bind/named.conf
sudo named-checkzone dmz.corp.vektor.lab /etc/bind/db.dmz.internal
```
`-z` дополнительно грузит зоны и печатает их состояние. Смотреть: отсутствие `zone ... loaded serial N` означает, что зона не поднялась.

**2. Управление BIND через rndc.**

```bash
sudo rndc status
sudo rndc reload
sudo rndc zonestatus dmz.corp.vektor.lab
```
`reload` перечитывает конфиг и изменённые файлы зон без рестарта демона. В выводе `zonestatus` важны `type: slave`, `serial`, `next refresh`.

**3. Запросы через dig — с явным сервером и без рекурсии.**

```bash
dig +norecurse @10.10.10.12 web01.dmz.corp.vektor.lab A
dig @10.10.10.10 web01.dmz.corp.vektor.lab +short
dig AXFR dmz.corp.vektor.lab @10.10.10.12
```
`+norecurse` показывает, что сервер отвечает сам, а не бегает дальше; `AXFR` — проверка, что трансфер разрешён только своим. В ответе смотри `status:` (`NOERROR`, `NXDOMAIN`, `REFUSED`, `SERVFAIL`), `flags:` (`aa` — авторитативный ответ, `ad` — прошёл валидацию), `ANSWER SECTION`.

**4. Windows-сторона: делегирование и проверка.**

```powershell
Add-DnsServerZoneDelegation -Name "dmz" -ZoneName "corp.vektor.lab" `
  -NameServer "ns1.dmz.corp.vektor.lab." -IPAddress 10.10.10.12
Resolve-DnsName web01.dmz.corp.vektor.lab -Server 10.10.10.10 -Type A
Get-DnsServerZone -Name "dmz.corp.vektor.lab"
```
Первая команда создаёт в родительской зоне NS + glue-запись. `Resolve-DnsName -Server` проверяет именно путь «клиент → DC01 → делегирование».

**5. PowerDNS: зона как данные, а не файл.**

```bash
sudo pdnsutil create-zone dmz.corp.vektor.lab ns1.dmz.corp.vektor.lab
sudo pdnsutil add-record dmz.corp.vektor.lab web01 A 10.10.20.10
sudo pdnsutil increase-serial dmz.corp.vektor.lab
sudo pdnsutil show-zone dmz.corp.vektor.lab
```
Записи лежат в SQLite, поэтому `increase-serial` нужно вызывать руками (или через API). `show-zone` печатает метаданные: есть ли DNSSEC, какие ключи.

**6. PowerDNS API (DNS как код).**

```bash
curl -s -H "X-API-Key: $PDNS_API_KEY" \
  http://10.10.10.14:8081/api/v1/servers/localhost/zones/dmz.corp.vektor.lab. | jq '.rrsets[] | {name,type,records}'
```
Так зоны правит CI, а не человек в консоли: изменения ревьюятся в Git (см. урок про GIT01 в DevOps-треке).

**7. Подпись зоны и проверка DNSSEC.**

```bash
sudo pdnsutil secure-zone dmz.corp.vektor.lab
dig +dnssec @10.10.10.14 web01.dmz.corp.vektor.lab A | grep -E "RRSIG|flags"
dig DNSKEY dmz.corp.vektor.lab @10.10.10.14 +short
```
`secure-zone` генерирует KSK/ZSK и включает подпись. В `flags` появляется `ad` только у валидирующего резолвера, у авторитативного будет `aa`.

**8. Unbound: проверка конфига и управление.**

```bash
sudo unbound-checkconf
sudo unbound-control status
sudo unbound-control stats_noreset | grep -E "num.query|num.answer_secure"
```
`num.answer_secure` — сколько ответов прошло DNSSEC-валидацию. Если он нулевой, а трафик есть — цепочка доверия не работает.

**9. Проверка валидации у резолвера.**

```bash
dig @10.10.10.40 cloudflare.com +dnssec | grep flags
unbound-host -D -v ietf.org
```
Ищем `flags: qr rd ra ad`. Если вместо ответа `SERVFAIL` — подпись не сошлась, и это правильное поведение.

**10. Kea: проверка и запуск.**

```bash
sudo kea-dhcp4 -t /etc/kea/kea-dhcp4.conf
sudo systemctl restart kea-dhcp4-server
journalctl -u kea-dhcp4-server -n 30 --no-pager
```
`-t` — только проверка конфига, ничего не запускает. В логах ищи `DHCP4_STARTED` и пары `DHCP4_LEASE_ALLOC` (кому, какой адрес).

**11. Просмотр аренд в Kea через control-socket.**

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"command":"lease4-get-all","service":["dhcp4"]}' http://127.0.0.1:8000/ | jq
```
Нужен запущенный `kea-ctrl-agent`. Альтернатива — файл `/var/lib/kea/kea-leases4.csv`, но читать его на живой системе не аккуратно.

**12. Windows DHCP до миграции: инвентаризация и бэкап.**

```powershell
Get-DhcpServerv4Scope | Format-Table ScopeId,Name,StartRange,EndRange,State
Get-DhcpServerv4OptionValue -ScopeId 10.10.10.0
Get-DhcpServerv4Lease -ScopeId 10.10.10.0 | Measure-Object
Backup-DhcpServer -ComputerName DC01 -Path C:\Backup\dhcp
```
`Backup-DhcpServer` даёт откат по конфигурации, если миграция провалится. `State: Active` → перед включением Kea перевести в `InActive`.

**13. Клиент: что реально пришло по DHCP.**

```cmd
ipconfig /all
ipconfig /release
ipconfig /renew
ipconfig /displaydns
```
В `/all` смотри «DHCP-сервер» (кто выдал) и «DNS-серверы» (опция 6). Это первое, что проверяют по любой заявке «не открывается по имени».

GUI-пути: Windows DNS — `dnsmgmt.msc`; Windows DHCP — `dhcpmgmt.msc`; в BIND всё в `/etc/bind/named.conf*`; PowerDNS — `/etc/powerdns/pdns.conf`; Kea — `/etc/kea/kea-dhcp4.conf`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 готов: DC01 (AD DS + DNS + DHCP, пул 10.10.10.100–150), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01, GW01, WEB01 (10.10.20.10), MON01 (10.10.10.40), DC02. Также есть PVE01.

Добавляем на этом уроке три новые ВМ (Ubuntu Server 24.04 LTS, минимальная установка, статический IP, тот же учебный пароль `Lab#2026-vektor`):

| Имя | Адрес | Роль |
|-----|-------|------|
| DNS01 | 10.10.10.12 | BIND 9: видимый авторитативный слой, split-horizon через views, secondary для `dmz.corp.vektor.lab` |
| PDNS01 | 10.10.10.14 | PowerDNS Authoritative + SQLite: hidden primary, API, DNSSEC-подпись |
| KEA01 | 10.10.10.15 | Kea DHCPv4: новый сервер DHCP для офисной сети |

Вводим один новый поддомен: **`dmz.corp.vektor.lab`** — делегируется с DC01 на DNS01. Больше новых имён и адресов не придумываем. Unbound ставим на существующий MON01 (10.10.10.40), чтобы не плодить ВМ.

Проверь, что в VMware NAT-сеть VMnet8 адреса не раздаёт (DHCP VMware выключен) — иначе получишь конфликт с Kea.

### Снапшот

На всех затрагиваемых ВМ — DC01, MON01, WIN10-CL02, WIN11-CL01, DNS01, PDNS01, KEA01:

VMware Workstation: `VM → Snapshot → Take Snapshot`, имя `before-sysadmin-046`.

Или из командной строки хоста (пути подставь свои):

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\DC01\DC01.vmx" before-sysadmin-046
```

### Шаги

1. **DNS01, PDNS01, KEA01: статическая сеть.** На каждой ВМ в `/etc/netplan/50-cloud-init.yaml` (или своём файле) прописать адрес, шлюз 10.10.10.2, DNS 10.10.10.10, применить `sudo netplan apply`, проверить `ip -br a` и `ping 10.10.10.10`. Ожидаемо: пинг до DC01 идёт, имя `dc01.corp.vektor.lab` резолвится.

2. **PDNS01: установка PowerDNS с SQLite.**
```bash
sudo apt update && sudo apt install -y pdns-server pdns-backend-sqlite3 sqlite3 jq
sudo sqlite3 /var/lib/powerdns/pdns.sqlite3 < /usr/share/pdns-backend-sqlite3/schema/schema.sqlite3.sql
```
Точный путь к `schema.sqlite3.sql` в твоей версии пакета проверь: `dpkg -L pdns-backend-sqlite3 | grep schema` `[ПРОВЕРИТЬ]`. В `/etc/powerdns/pdns.conf`: `launch=gsqlite3`, `gsqlite3-database=/var/lib/powerdns/pdns.sqlite3`, `allow-axfr-ips=10.10.10.12`, плюс блок API (`api=yes`, `api-key=...`, `webserver=yes`, `webserver-address=10.10.10.14`, `webserver-port=8081`, `webserver-allow-from=10.10.10.0/24`). `systemctl restart pdns` → `systemctl is-active pdns` = `active`.

3. **PDNS01: зона, записи, DNSSEC.**
```bash
sudo pdnsutil create-zone dmz.corp.vektor.lab ns1.dmz.corp.vektor.lab
sudo pdnsutil add-record dmz.corp.vektor.lab ns1 A 10.10.10.12
sudo pdnsutil add-record dmz.corp.vektor.lab web01 A 10.10.20.10
sudo pdnsutil add-record dmz.corp.vektor.lab gw01 A 10.10.20.1
sudo pdnsutil secure-zone dmz.corp.vektor.lab
dig @10.10.10.14 web01.dmz.corp.vektor.lab +short
```
Ожидаемо: `10.10.20.10`, а `dig +dnssec` показывает RRSIG.

4. **DNS01: BIND как secondary с split-horizon.** Ставим `bind9 bind9utils dnsutils`. В `/etc/bind/named.conf.local`:
```text
acl "office" { 10.10.10.0/24; };
acl "dmz"    { 10.10.20.0/24; };

view "internal" {
    match-clients { office; dmz; };
    recursion yes;
    allow-query { office; dmz; };
    zone "dmz.corp.vektor.lab" {
        type slave;
        masters { 10.10.10.14; };
        file "/var/cache/bind/dmz.corp.vektor.lab.db";
    };
};

view "external" {
    match-clients { any; };
    recursion no;
    allow-query { any; };
};
```
`named-checkconf -z`, `systemctl restart named`, `rndc zonestatus dmz.corp.vektor.lab` → `type: slave`, serial совпадает с PDNS01. Важно: при использовании views **все** зоны описываются внутри views.

5. **Проверка split-horizon.** С UBU01 (10.10.10.30, попадает в view `internal`): `dig @10.10.10.12 web01.dmz.corp.vektor.lab +short` → `10.10.20.10`. На самом DNS01: `dig @127.0.0.1 web01.dmz.corp.vektor.lab` → `status: REFUSED` (клиент не из разрешённых сетей; для него зоны не существует). Это и есть split-horizon в чистом виде.

6. **DC01: делегирование.** В PowerShell от `adm.student`:
```powershell
Add-DnsServerZoneDelegation -Name "dmz" -ZoneName "corp.vektor.lab" `
  -NameServer "ns1.dmz.corp.vektor.lab." -IPAddress 10.10.10.12
```
С WIN11-CL01: `Resolve-DnsName web01.dmz.corp.vektor.lab` → `10.10.20.10`. Теперь доменные клиенты видят DMZ-имена через AD DNS.

7. **MON01: Unbound как валидирующий резолвер.** `sudo apt install -y unbound unbound-anchor`. В `/etc/unbound/unbound.conf.d/vektor.conf`:
```yaml
server:
    interface: 10.10.10.40
    access-control: 10.10.10.0/24 allow
    auto-trust-anchor-file: "/var/lib/unbound/root.key"
    domain-insecure: "corp.vektor.lab."
    verbosity: 1
forward-zone:
    name: "corp.vektor.lab."
    forward-addr: 10.10.10.10
```
`unbound-checkconf`, `systemctl restart unbound`, `systemctl enable unbound`. Если порт занят systemd-resolved, отключи stub-listener (`DNSStubListener=no` в `/etc/systemd/resolved.conf` и рестарт `systemd-resolved`).

8. **Проверка DNSSEC-валидации.** `dig @10.10.10.40 cloudflare.com +dnssec | grep flags` → есть `ad`. `dig @10.10.10.40 web01.dmz.corp.vektor.lab` → отвечает (зона помечена `domain-insecure`, потому что внутренняя зона не имеет цепочки доверия от корня — это нормальная практика, но ты должен уметь объяснить, почему так).

9. **KEA01: установка и конфиг.**
```bash
sudo apt install -y kea-dhcp4-server kea-ctrl-agent
sudo kea-dhcp4 -t /etc/kea/kea-dhcp4.conf
```
В `/etc/kea/kea-dhcp4.conf` — subnet `10.10.10.0/24`, пул `10.10.10.100 - 10.10.10.150` (тот же, что был на DC01), опции: `routers` = 10.10.10.2, `domain-name-servers` = 10.10.10.10, `domain-name` = corp.vektor.lab. `interfaces` — имя твоего адаптера из `ip -br a`. Плюс `lease-database` типа `memfile`.

10. **Переключение DHCP.** На DC01: `Set-DhcpServerv4Scope -ScopeId 10.10.10.0 -State InActive` (или стоп service `DHCPServer`). На KEA01: `systemctl restart kea-dhcp4-server && systemctl restart kea-ctrl-agent`. На WIN10-CL02: `ipconfig /release` → `ipconfig /renew` → `ipconfig /all`. Ожидаемо: «DHCP-сервер» = 10.10.10.15, адрес из 100–150, DNS = 10.10.10.10.

11. **Проверка аренды через API Kea.** `curl` из пункта 11 раздела 4 → в JSON есть `hw-address` клиента и `"result": 0`.

12. **Документирование.** В `stand-log.md`: новые ВМ, новая подзона, перенос DHCP. В регламент — «изменения в зонах только через `pdnsutil`/API с записью в журнал, серийник поднимаем всегда».

### Проверка

- [ ] `dig @10.10.10.14 web01.dmz.corp.vektor.lab +dnssec` отдаёт A + RRSIG
- [ ] `dig @10.10.10.12 web01.dmz.corp.vektor.lab` с UBU01 — отвечает, с localhost DNS01 — REFUSED
- [ ] На DC01 есть делегирование: `Get-DnsServerZone -Name "corp.vektor.lab" | ...` и `Resolve-DnsName web01.dmz.corp.vektor.lab` с WIN11-CL01 работает
- [ ] `dig @10.10.10.40 cloudflare.com +dnssec` содержит флаг `ad`
- [ ] `ipconfig /all` на WIN10-CL02: DHCP-сервер 10.10.10.15, DNS 10.10.10.10, домен corp.vektor.lab
- [ ] `\\FS01\HR` с WIN10-CL02 открывается
- [ ] `stand-log.md` обновлён

### Сломай и почини

**Пользователь:** petrova
**ВМ:** WIN10-CL02
**Что написала:** «Не открывается `\\FS01\HR` — "Не удаётся найти сетевой путь". Сайты открываются. У Иванова работает.»
**Срочность:** высокая

**Как воспроизвести.** На KEA01 в конфиге в опции `domain-name-servers` укажи `10.10.10.12` (DNS01) вместо `10.10.10.10`. Перезапусти Kea, на WIN10-CL02 сделай `ipconfig /release` + `/renew`.

**Диагностика по порядку.**
1. `ipconfig /all` на WIN10-CL02 → блок «DNS-серверы»: 10.10.10.12. Уже подозрительно.
2. `nslookup fs01.corp.vektor.lab 10.10.10.10` → отвечает; `nslookup fs01.corp.vektor.lab 10.10.10.12` → NXDOMAIN/REFUSED.
3. `Resolve-DnsName -Type SRV _ldap._tcp.dc._msdcs.corp.vektor.lab -Server 10.10.10.12` → пусто. Значит, доменные SRV-записи клиент не получит, и дальше посыпется всё.
4. `ping 10.10.10.20` по IP → работает. Значит, сеть в порядке, проблема только в резолвинге.
5. На KEA01: `curl ... lease4-get-all` → видно, что аренда выдана, и в ней опция 6 неверная. Сверяем с резервной копией конфига DC01 (`Get-DhcpServerv4OptionValue -OptionId 6`).

**Решение.** Исправить опцию 6 на `10.10.10.10`, добавить второй адрес резолвера `10.10.10.40` (MON01), `kea-dhcp4 -t`, перезапуск, `ipconfig /renew` на клиенте, `ipconfig /flushdns`.

**Ответ пользователю:** «Мария, проблема была в настройке DHCP после перевода сервиса на новый сервер: вашему компьютеру выдавался неверный адрес DNS. Исправил, обновите адрес командой `ipconfig /renew` (или перезагрузите ПК) — папка `\\FS01\HR` откроется. Если повторится — сразу пишите, я проверю в течение 5 минут.»

**Закрытие заявки.** Категория «Сеть / DNS», причина «ошибка конфигурации DHCP (опция 6) после миграции», действие «исправлен конфиг Kea, проверено на WIN10-CL02», профилактика — чек-лист миграции DHCP в регламенте и алерт на «клиент получил DNS не из белого списка».

### Что изменилось на стенде

- Появились ВМ: **DNS01** 10.10.10.12 (BIND 9, views), **PDNS01** 10.10.10.14 (PowerDNS + SQLite, API на 8081, DNSSEC), **KEA01** 10.10.10.15 (Kea DHCPv4, ctrl-agent на 8000).
- На DC01 создано делегирование подзоны **`dmz.corp.vektor.lab`** → `ns1.dmz.corp.vektor.lab` (10.10.10.12).
- На MON01 (10.10.10.40) работает **Unbound** как внутренний валидирующий резолвер.
- **DHCP офисной сети переведён на KEA01**, на DC01 скоуп `10.10.10.0` переведён в состояние `InActive`. Пул тот же: 10.10.10.100–150.
- Ветка `dmz.corp.vektor.lab` содержит: `ns1` → 10.10.10.12, `web01` → 10.10.20.10, `gw01` → 10.10.20.1.

### Откат

Для учебного отката: `VM → Snapshot → Snapshot Manager` → выбрать `before-sysadmin-046` → `Go To`. Или `vmrun -T ws revertToSnapshot "D:\VMs\DC01\DC01.vmx" before-sysadmin-046` (для каждой ВМ).

Оперативный откат без снапшота (он же план Б в боевой среде): `systemctl stop kea-dhcp4-server kea-ctrl-agent` на KEA01 → `Set-DhcpServerv4Scope -ScopeId 10.10.10.0 -State Active` на DC01 → клиенты продлевают аренду. DNS-изменения откатываются удалением делегирования (`Remove-DnsServerZoneDelegation`) и остановкой BIND/PowerDNS — DNS-кэш доживёт по TTL, поэтому TTL на время миграции снижают заранее (за сутки до работ), а не в момент.

## 6. Ошибки новичков и безопасность

1. **Два DHCP-сервера в одном L2-сегменте «просто для теста».** → Клиенты получают адреса то от одного, то от другого, появляются дубликаты и «плавающие» проблемы. → Правильно: на время теста деактивировать старый скоуп, либо split-scope 80/20, либо DHCP failover, а на коммутаторах — DHCP snooping с доверенным портом (см. уроки 42 и 47).

2. **Открытая рекурсия.** `allow-recursion { any; }` в BIND или `access-control: 0.0.0.0/0 allow` в Unbound. → Сервер становится усилителем DNS-амплификации и точкой слива внутренних имён. → Рекурсию разрешать только своим подсетям, авторитативные ответы — всем.

3. **AXFR «на всякий случай» всем.** → Любой желающий выгружает полную карту сети за одну команду. → `allow-transfer` только на адреса secondaries, желательно с TSIG-ключом; проверять `dig AXFR` с чужой машины — должно быть `Transfer failed`.

4. **Правка зоны без проверки и без серийника.** → `named-checkzone` не запускали, serial не подняли → secondary не обновился, кэши держат старое. → Всегда `named-checkzone` / `increase-serial` и явная проверка `dig @secondary`.

5. **Миграция DHCP без инвентаризации опций.** → Потерялись опции 6/15/42/119 или зарезервированные адреса принтеров. → `Get-DhcpServerv4OptionValue`, `Get-DhcpServerv4Reservation`, `Backup-DhcpServer` **до** переключения.

6. **Один резолвер на всю компанию.** → Падение MON01 останавливает работу всех. → Минимум два резолвера, разные адреса в опции 6 DHCP; отдельный контроль (урок 48).

7. **DNSSEC без плана смены ключей.** Подписали и забыли. → Через год истёк/потерян ключ — зона перестаёт валидироваться у всех. → `dnssec-policy` / метаданные PowerDNS, календарный регламент ротации, мониторинг сроков.

8. **Изменения «в консоли» без журнала.** → Через месяц никто не знает, откуда взялась запись. → Все правки через API/`pdnsutil` с записью в `stand-log.md`/регламент, желательно из Git.

**Безопасность.** DNSSEC подтверждает целостность, но **не шифрует**: кто видит трафик, тот видит имена. Для приватности — DoT/DoH до резолвера, но не «в интернет по умолчанию». Поддельный DHCP-сервер = перехват трафика (выдал свой шлюз/DNS — стал шлюзом), поэтому DHCP snooping и 802.1X в боевой среде обязательны. Split-horizon не должен быть единственной защитой внутренних имён: он снижает заметность, но не спасает от утечки через NSEC-walking в подписанной зоне — для скрываемых зон используют NSEC3 или «чёрную ложь» `[ПРОВЕРИТЬ по документации PowerDNS/BIND]`. Логи DNS — это персональные данные о поведении сотрудников: срок хранения ограничивать, доступ — по регламенту. И никогда не храни API-ключ PowerDNS и TSIG-ключи в открытом виде в общем репозитории.

## 7. Вопросы на собеседовании

**1. Чем авторитативный DNS отличается от рекурсивного резолвера и почему их разделяют?**
Ответ: авторитативный владеет зоной и отвечает фактами по ней; резолвер зоной не владеет, он итерирует от корня, кэширует по TTL и обслуживает клиентов. Разделяют ради безопасности (открытая рекурсия на публичном authoritative — это амплификация и утечка), производительности (кэш обслуживает тысячи клиентов, авторитативному это не нужно) и управляемости: DDoS на резолвер не роняет публикацию зоны. В типовой схеме: BIND/PowerDNS — авторитативные (часто hidden primary + видимые secondaries), Unbound/BIND resolver — внутри сети.
*Что хочет услышать:* понимание двух разных функций и того, где какая живёт.
*Красный флаг:* «это одно и то же, просто у одного кэш больше».

**2. Как сделать split-horizon и какие у него риски?**
Ответ: в BIND — `view` с `match-clients` по ACL, в каждой view свой набор зон и политик рекурсии; в PowerDNS — отдельные зоны/инстансы либо views в новых версиях. Внутренним клиентам отдаём полную зону, внешним — минимальный набор публичных имён или REFUSED. Риски: рассинхронизация данных между представлениями, утечка внутренних имён (через NSEC, ошибки ACL, «забытый» публичный резолвер), сложность отладки («у меня резолвится, у клиента нет»), рост TTL-кэша при переключениях. Обязательно: единый источник истины, тесты из каждой сети, документирование, что именно скрыто.
*Что хочет услышать:* ACL по подсетям, разные данные, риски утечки и практика проверки из разных сегментов.
*Красный флаг:* «просто две зоны с одинаковым именем на двух серверах, ничего страшного».

**3. Что даёт DNSSEC и как проходит валидация?**
Ответ: гарантирует целостность и подлинность ответа (защита от подмены и отравления кэша), не даёт конфиденциальности. Набор записей подписан RRSIG; DNSKEY зоны публикуется, его хэш (DS) лежит в родительской зоне; валидирующий резолвер строит цепочку от корневого trust anchor до зоны, проверяет подписи, ставит `ad`, при несоответствии отдаёт SERVFAIL. Несуществование доказывается NSEC/NSEC3. Практика: подпись на праймари, передача RRSIG вместе с зоной, мониторинг сроков ключей, аккуратно с внутренними зонами — им нужен локальный trust anchor или `domain-insecure`.
*Что хочет услышать:* DS/DNSKEY/RRSIG, цепочка доверия, `ad`/SERVFAIL, отсутствие шифрования.
*Красный флаг:* «DNSSEC шифрует DNS-трафик».

**4. Как перенести DHCP с Windows на Kea без простоя?**
Ответ: инвентаризация (`Get-DhcpServerv4Scope/OptionValue/Reservation`, `Backup-DhcpServer`), проверка, что Kea отдаёт те же опции и пул; подготовка конфига и проверка `kea-dhcp4 -t`; понижение lease-time до короткого значения за сутки-двое; переключение в окно работ: деактивация старого скоупа → запуск Kea → проверка `lease4-get-all` и `ipconfig /renew` на тестовых клиентах (Windows 10/11 и Linux); план откатa (стоп Kea, активация старого скоупа); мониторинг отказов DHCP и алерт. Отдельно — DDNS: у Windows динамические обновления идут по GSS-TSIG, Kea умеет обычный TSIG, поэтому «просто включить» не получится — либо отдельная зона с разрешёнными небезопасными обновлениями, либо отказ от DDNS для части подсетей.
*Что хочет услышать:* инвентаризация, тест, короткий lease, обратимость, отдельно про DDNS/GSS-TSIG.
*Красный флаг:* «поднял Kea рядом, работает же» — без выключения старого сервера.

**5. Пользователь пишет «не открывается сетевая папка по имени, по IP работает». Твои шаги?**
Ответ: 1) `ipconfig /all` — какие DNS выданы, от какого DHCP; 2) `nslookup` имени на выданном DNS и на эталонном (10.10.10.10) — сузить до клиента/резолвера/зоны; 3) `Resolve-DnsName -Type SRV _ldap._tcp.dc._msdcs...` — жив ли доменный резолвинг; 4) `ipconfig /flushdns`, проверка `displaydns` на закэшированный NXDOMAIN; 5) на сервере — `dig @authoritative`, `rndc zonestatus`, серийник, `journalctl`/логи BIND; 6) проверка split-horizon/ACL, если клиент из другой подсети; 7) после исправления — проверка на втором клиенте, чтобы не «починить одному». Всегда отвечать пользователю с ETA и делать запись в тикете.
*Что хочет услышать:* порядок «клиент → резолвер → авторитативный → зона», а не хаотичные пинги.
*Красный флаг:* «перезагрузите компьютер, потом посмотрим».

## 8. Источники (официальная документация)

- **ISC BIND 9** — `isc.org`: «BIND 9 Administrator Reference Manual», разделы «Views», «Zone Transfers», «DNSSEC», «Response Policy Zones». Поиск: `BIND 9 ARM views match-clients`, `BIND 9 ARM dnssec-policy inline-signing`.
- **Unbound** — `nlnetlabs.nl`: «Unbound documentation», разделы «unbound.conf», «DNSSEC validation», «Access control». Поиск: `unbound.conf auto-trust-anchor-file domain-insecure`.
- **PowerDNS Authoritative Server** — `doc.powerdns.com`: «Authoritative Server → Backends (Generic SQLite3)», «DNSSEC», «HTTP API», «pdnsutil». Поиск: `PowerDNS authoritative sqlite3 backend pdnsutil secure-zone`, `PowerDNS Authoritative HTTP API zones`.
- **ISC Kea DHCP** — `kea.readthedocs.io`: «Kea DHCPv4 Server», «Lease Database Backends», «Management API / Control Agent», «DHCP-DDNS». Поиск: `Kea DHCPv4 configuration subnet4 option-data domain-name-servers`, `Kea control agent lease4-get-all`.
- **Microsoft Learn** — `learn.microsoft.com`: разделы «DNS Server», «DHCP Server» (cmdlets `Add-DnsServerZoneDelegation`, `Set-DhcpServerv4Scope`, `Backup-DhcpServer`). Поиск: `Add-DnsServerZoneDelegation PowerShell DNS Server`, `Set-DhcpServerv4Scope State InActive`.
- **RFC** — `datatracker.ietf.org`: RFC 1034/1035 (DNS), RFC 4033–4035 (DNSSEC), RFC 2131 (DHCP), RFC 3046 (option 82), RFC 5737 (документационные диапазоны адресов).
- **Astra Linux / РЕД ОС** — `astralinux.ru`, `redos.red-soft.ru`: руководства администратора, разделы про BIND/DNS и сетевые службы (наличие конкретных пакетов Kea/PowerDNS в репозиториях проверяй по документации своей версии, `[ПРОВЕРИТЬ]`).
