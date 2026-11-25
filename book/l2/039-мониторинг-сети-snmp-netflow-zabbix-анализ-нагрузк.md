# Мониторинг сети: SNMP, NetFlow, Zabbix, анализ нагрузки каналов

## 1. Введение (зачем это нужно в реальной работе)

```
Заявка #4712
Пользователь: ivanov (Иван Иванов, бухгалтерия), ВМ WIN11-CL01
Что написал: «С 14:00 всё тормозит. Папка \\FS01\Buh открывается по 30 секунд,
отчёт копируется 25 минут вместо двух. Мария Петрова говорит — у неё то же самое.
Страница на 10.10.20.10 вообще не грузится. У нас закрытие месяца!»
Срочность: Высокая
```

L1 на такой заявке обычно разводит руками и говорит «перезагрузите компьютер». L2 обязан ответить на другие вопросы: канал действительно загружен или проблема в чём-то ещё? Когда началось? Кем создан трафик? Это нормальный бэкап или кто-то забыл запущенный нагрузочный тест?

Без мониторинга вы разговариваете с пользователем на уровне ощущений. С мониторингом — на уровне цифр: «в 14:07 загрузка интерфейса GW01 выросла с 3% до 92%, в потоках 87% трафика — это 10.10.10.3 → 10.10.10.30 порт 5201, на GW01 висит процесс iperf3». После этой главы вы должны уметь: поднять Zabbix и поставить на мониторинг узлы стенда по SNMP и по агенту, читать счётчики интерфейсов и считать загрузку канала в процентах, экспортировать и разбирать NetFlow-потоки, находить топ-«разговаривающих» и отличать загрузку канала от потерь и ошибок.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Мониторинг сети — это три разных источника данных, которые дополняют друг друга.

**1. Метрики по SNMP** (Simple Network Management Protocol). Схема «менеджер → агент»: Zabbix раз в минуту сам приходит на UDP/161 к агенту на устройстве и спрашивает значение конкретного счётчика. Агент отвечает. Обратное направление — **trap** (UDP/162): агент сам присылает сообщение при событии (интерфейс упал, температура выросла). Trap не заменяет опрос — он может потеряться, поэтому события всё равно подтверждают опросом.

Каждое значение в SNMP имеет адрес — **OID** (Object Identifier), строку вида `1.3.6.1.2.1.2.2.1.8.2` (последняя цифра — номер интерфейса). Дерево OID описано в **MIB** (Management Information Base) — это текстовые файлы-«схемы», дающие именам человеческий вид: `IF-MIB::ifOperStatus.2`. Версии: v1 — древняя, v2c — то же плюс bulk-запросы, авторизация одним «коммьюнити» в открытом виде (фактически пароль в plaintext), v3 — с аутентификацией и шифрованием (`authPriv`). В 2026 году в боевой среде — только v3 (или v2c с ACL по IP, если устройство v3 не умеет).

**2. Потоки (flows).** SNMP говорит «сколько всего прошло через порт». Потоки говорят «кто с кем и сколько». Устройство-экспортёр держит в памяти **flow cache**: как только поток (обычно 5-tuple: src IP, dst IP, src port, dst port, protocol) завершился по таймауту или по TCP FIN/RST, оно отправляет UDP-датаграмму коллектору. Варианты: **NetFlow v5** (фиксированный формат, IPv4-only), **NetFlow v9** (шаблоны, по сути предок IPFIX), **IPFIX** (RFC 7011, стандартизированный NetFlow v9), **sFlow** (не считает каждый поток — берёт статистическую выборку пакетов, 1 из N). Разница принципиальна: NetFlow видит всё, но дороже по CPU; sFlow дешевле и «приблизителен». Порты по умолчанию: NetFlow часто 2055/udp, IPFIX 4739/udp, sFlow 6343/udp — но это именно договорённость, коллектор слушает тот порт, который вы указали экспортёру.

**3. Агентский/синтетический мониторинг.** Zabbix agent на хосте отдаёт то, чего нет в SNMP: список процессов, размер очереди, свободное место, логи. Плюс проверки «извне»: ping, доступность TCP-порта, HTTP-код.

**Как это собирается в Zabbix.** Архитектура: `zabbix_server` (движок: pollers, trappers, history syncers) → СУБД (MySQL/MariaDB или PostgreSQL) для конфигурации и истории → `zabbix_frontend` (PHP+Apache, веб-интерфейс). Опрос инициирует **сервер**, а не агент — значит, агенту нужно разрешить входящие от сервера (10050/tcp), а серверу — исходящие. Сервер сам слушает 10051/tcp (приём данных от агентов в активном режиме и от прокси).

**Нагрузка канала.** Считать надо по 64-битным счётчикам `ifHCInOctets`/`ifHCOutOctets` (IF-MIB): 32-битные `ifInOctets` переполняются за 343 секунды на 100 Мбит/с, и вы получите «отрицательную» дельту. Формула: `Загрузка % = (Δбайт × 8 × 100) / (интервал_сек × скорость_бит/с)`. Плюс отдельно смотрят ошибки (`ifInErrors`, `ifOutDiscards`) — канал может быть свободен, а приложения тормозить из-за потерь и ретрансмиссий.

```text
                     ┌──────────── MON01 10.10.10.40 ─────────────┐
                     │ zabbix_server ── опрос ──┐                  │
                     │ zabbix_agent2 (10050/tcp)│                  │
                     │ nfcapd :2055/udp ←────────┼── потоки        │
                     │ MariaDB + Apache/PHP (/zabbix)              │
                     └───────────┬──────────────┼──────────────────┘
                        161/udp  │              │ 2055/udp
              ┌──────────────────┴───┐   ┌──────┴───────┐
              │ DC01, FS01 (SNMP)    │   │  GW01        │
              │ UBU01, GW01 (snmpd)  │   │  softflowd   │
              └──────────────────────┘   └──────┬───────┘
                                                │
              10.10.10.0/24 (офис) ──── GW01 ────┼──── 10.10.20.0/24 (DMZ)
              10.10.10.3                 10.10.20.1     WEB01 10.10.20.10
```

## 3. Аналогия из жизни

Представьте диспетчерскую в большом доме. **SNMP** — это обходчик с манометрами: раз в минуту подходит к стояку и записывает давление. Он видит только то, для чего есть датчик, и только в момент обхода. **Trap** — датчик протечки, который сам звонит в диспетчерскую, когда прорвало. **NetFlow** — журнал вахтёра: «в 14:05 из квартиры 12 вышел человек, в 14:22 вернулся, принёс два чемодана» — известно кто, куда и сколько, но не известно, что в чемоданах. **Zabbix** — это сама диспетчерская: экран со всеми показаниями, журнал за месяц и сирена, которая включается, когда давление выходит за порог. Анализ нагрузки канала — это чтение счётчика: вычли предыдущее показание из текущего, поделили на время — получили расход.

Аналогия ломается в одном: у воды расход непрерывный и мгновенно измеримый, а трафик дискретен, и «мгновенную скорость» вы получаете только как разницу двух замеров — поэтому при коротких всплесках SNMP с опросом раз в минуту врёт заниженно.

## 4. Практика и команды

**1. Базовый опрос SNMP — проверяем, что агент жив**

```bash
snmpwalk -v2c -c Vekt0rRO 10.10.10.30 1.3.6.1.2.1.1
```
`-v2c` — версия (v1|2c|3), `-c` — коммьюнити, дальше хост и OID. Вывод (сокращённо):
```text
SNMPv2-MIB::sysDescr.0 = STRING: Linux UBU01 6.8.0-... x86_64
SNMPv2-MIB::sysUpTime.0 = Timeticks: (1834200) 5:05:42.00
SNMPv2-MIB::sysName.0 = STRING: UBU01
```
Смотреть: `sysUpTime` (давно ли перезагружался), `sysName` (не перепутали ли хост). Если ответа нет — сначала проверяем, слушает ли агент `ss -lunp | grep 161` и не режет ли UFW.

**2. Точечное чтение и расшифровка OID**

```bash
snmpget -v2c -c Vekt0rRO -Oqv 10.10.10.30 1.3.6.1.2.1.1.3.0
snmptranslate -On IF-MIB::ifHCInOctets
```
`-Oqv` — только значение, без имени; `-On` — показать числовой OID. Вторая команда работает, если установлены MIB-файлы (`snmp-mibs-downloader` есть не во всех репозиториях [ПРОВЕРИТЬ], числовые OID работают всегда).

**3. Карта интерфейсов — с чего начинать любую диагностику**

```bash
snmpwalk -v2c -c Vekt0rRO 10.10.10.3 1.3.6.1.2.1.2.2.1.2 | head
snmpwalk -v2c -c Vekt0rRO 10.10.10.3 1.3.6.1.2.1.2.2.1.8
snmpwalk -v2c -c Vekt0rRO 10.10.10.3 1.3.6.1.2.1.31.1.1.1.18
```
`ifDescr` (…2.2.1.2) — тип/имя, `ifOperStatus` (…2.2.1.8) — `1=up, 2=down`, `ifAlias` (…31.1.1.1.18) — описание, которое вы сами задали на устройстве. Всегда возьмите за правило: без `ifAlias` мониторинг превращается в «интерфейс номер 7 опять грузит».

**4. 64-битные счётчики и расчёт загрузки**

```bash
snmpget -v2c -c Vekt0rRO -Oqv 10.10.10.3 1.3.6.1.2.1.31.1.1.1.6.2   # ifHCInOctets
snmpget -v2c -c Vekt0rRO -Oqv 10.10.10.3 1.3.6.1.2.1.31.1.1.1.15.2  # ifHighSpeed, Мбит/с
```
Первый замер, пауза 300 с, второй замер. Если дельта 750 000 000 байт, то `750e6 × 8 / 300 = 20 000 000 бит/с = 20 Мбит/с`; при скорости 1000 Мбит/с это 2%. Для скорости > 100 Мбит/с всегда берите `ifHC*`.

**5. Trap: проверяем, что сервер их принимает**

```bash
# на MON01 — слушаем в foreground
snmptrapd -f -Lo -c /etc/snmp/snmptrapd.conf
# в /etc/snmp/snmptrapd.conf (учебный стенд, потом так не делаем):
#   disableAuthorization yes
# на UBU01 — отправляем тестовый trap
snmptrap -v2c -c Vekt0rRO 10.10.10.40 '' 1.3.6.1.6.3.1.1.5.1 1.3.6.1.2.1.1.1.0 s "test from UBU01"
```
`-Lo` — логировать в stdout. `disableAuthorization yes` отключает проверку коммьюнити — в реальности так нельзя, там `authCommunity log,execute,net Vekt0rRO`.

**6. Windows: счётчики интерфейса без SNMP-агента**

```powershell
Get-NetAdapter | Format-Table Name,LinkSpeed,Status
Get-NetAdapterStatistics -Name 'Ethernet0'
Get-Counter '\Network Interface(*)\Bytes Total/sec' -SampleInterval 1 -MaxSamples 5
```
`Get-NetAdapterStatistics` отдаёт `ReceivedBytes`/`SentBytes` — делим дельту на время, получаем байты/с. `Get-Counter` берёт системный счётчик `Bytes Total/sec` каждую секунду, 5 проб. В GUI то же самое: **Performance Monitor** → `Performance Monitor` → `+` → `Network Interface`.

**7. История нагрузки на Linux — sysstat**

```bash
sudo apt install -y sysstat
sar -n DEV 1 5
sar -n DEV -f /var/log/sysstat/sa$(date +%d)
```
`-n DEV` — по сетевым устройствам, `1 5` — 5 замеров по секунде, `-f` — читать историю за конкретный день. Ключевая колонка — `%ifutil` (загрузка интерфейса в процентах). История включается через `ENABLED="true"` в `/etc/default/sysstat`.

**8. Кто грузит канал прямо сейчас**

```bash
sudo apt install -y iftop
sudo iftop -i ens33 -n -P -B
```
`-i` интерфейс, `-n` не резолвить DNS, `-P` показывать порты, `-B` в байтах (без `-B` — биты). На экране топ пар хостов и три шкалы (2s/10s/40s). Выход — `q`.

**9. Быстрый взгляд на счётчики и сокеты**

```bash
ip -s link show ens33
ss -s
```
`ip -s link` — `RX: bytes/packets/errors/dropped`; `errors` > 0 на виртуальной машине — почти всегда признак разъехавшегося дуплекса или проблем хоста. `ss -s` — сводка по сокетам (`TCP: ... estab ...`); лавина ESTAB — повод искать приложение-«флудилку».

**10. Экспорт потоков: softflowd на GW01**

```bash
sudo apt install -y softflowd
ip -br a                                    # уточнить имя: ens33 / ens160
sudo softflowd -i ens33 -n 10.10.10.40:2055 -v 9 -t maxlife=60s
```
`-i` интерфейс (можно несколько раз), `-n` адрес коллектора и порт, `-v 9` версия (1|5|9|10, где 10 — IPFIX), `-t` таймауты жизни/активности потока (синтаксис `maxlife=` уточнить через `man softflowd` [ПРОВЕРИТЬ]).

**11. Коллектор и топ-разговоры: nfcapd + nfdump**

```bash
sudo apt install -y nfdump
sudo mkdir -p /var/flows
sudo nfcapd -p 2055 -w /var/flows -S 1 -D -t 300
nfdump -r /var/flows/2026/02/16/nfcapd.202602161400 -s srcip/bytes -n 10
nfdump -r /var/flows/2026/02/16/nfcapd.202602161400 -s ip/bytes -A srcip,dstip,dstport -n 10
nfdump -r /var/flows/2026/02/16/nfcapd.202602161400 'src ip 10.10.10.3 and dst port 5201' -c 20
```
`nfcapd`: `-p` порт, `-w` каталог, `-S 1` иерархия подкаталогов (год/месяц/день), `-t` интервал ротации файла в секундах (по умолчанию 300), `-D` демон. `nfdump`: `-r` читать файл, `-s srcip/bytes` агрегировать и отсортировать топ по байтам, `-n` сколько строк, `-A` состав агрегации, фильтр — выражение в кавычках (`src ip`, `dst port`, `proto tcp`), `-c` ограничить число записей.

**12. Проверка агента Zabbix вручную**

```bash
zabbix_get -s 10.10.10.30 -k agent.ping
zabbix_get -s 10.10.10.30 -k system.cpu.load[all,avg1]
zabbix_get -s 10.10.10.30 -k 'net.if.in[ens33]'
curl -s -X POST -H 'Content-Type: application/json-rpc' \
  -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}' \
  http://10.10.10.40/zabbix/api_jsonrpc.php
```
`agent.ping` должен вернуть `1`. Если таймаут — вопрос почти всегда в файрволе или в `Server=` в конфиге агента. Последняя команда — простейший вызов Zabbix API, возвращает версию сервера.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Готовый стенд L1: DC01 (10.10.10.10, AD/DNS/DHCP), FS01 (10.10.10.20), WIN11-CL01, WIN10-CL02, UBU01 (10.10.10.30), ASTRA01 (10.10.10.31). Домен `corp.vektor.lab`, учётка `adm.student`, пароль стенда `Lab#2026-vektor`.

Нужны две ВМ из таблицы L2:
- **GW01** — Debian 12, 10.10.10.3 (VMnet8) и 10.10.20.1 (VMnet10), маршрутизация включена. Если её нет — она делается в уроке 30 (маршрутизация) и 35 (файрволы); пройдите их раньше. Минимум на месте: Debian 12 minimal, два адаптера, `net.ipv4.ip_forward=1` в `/etc/sysctl.d/99-forward.conf`, `sysctl --system`.
- **MON01** — создаём сейчас: Ubuntu Server 24.04 LTS, 2 vCPU, 2 ГБ RAM, 20 ГБ диск, один адаптер в VMnet8, адрес 10.10.10.40/24, шлюз 10.10.10.2, DNS 10.10.10.10.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя строго `before-l2-039` — на MON01 (после установки ОС и настройки сети, но до Zabbix), GW01, DC01, FS01, UBU01.

### Шаги

1. **MON01: статический адрес.** Создать файл `/etc/netplan/50-static.yaml`:
   ```yaml
   network:
     version: 2
     ethernets:
       ens33:
         dhcp4: false
         addresses: [10.10.10.40/24]
         routes:
           - to: default
             via: 10.10.10.2
         nameservers:
           addresses: [10.10.10.10]
   ```
   `sudo chmod 600 /etc/netplan/50-static.yaml && sudo netplan apply`, затем `sudo hostnamectl set-hostname MON01`.
   *Ожидаемо:* `ping -c2 10.10.10.10` и `ping -c2 10.10.10.2` отвечают, `getent hosts corp.vektor.lab` что-то возвращает.

2. **MON01: базовые пакеты.**
   ```bash
   sudo apt update && sudo apt -y upgrade
   sudo apt -y install snmp nfdump sysstat iftop curl tcpdump mariadb-server
   sudo sed -i 's/^ENABLED=.*/ENABLED="true"/' /etc/default/sysstat
   sudo systemctl enable --now sysstat mariadb
   ```
   *Ожидаемо:* `systemctl is-active mariadb` → `active`.

3. **MON01: репозиторий и пакеты Zabbix 7.0 LTS** (актуальную LTS-ветку сверьте на zabbix.com [ПРОВЕРИТЬ]):
   ```bash
   wget https://repo.zabbix.com/zabbix/7.0/ubuntu/pool/main/z/zabbix-release/zabbix-release_latest_7.0+ubuntu24.04_all.deb
   sudo dpkg -i zabbix-release_latest_7.0+ubuntu24.04_all.deb
   sudo apt update
   sudo apt -y install zabbix-server-mysql zabbix-frontend-php zabbix-apache-conf \
                       zabbix-sql-scripts zabbix-agent2
   ```
   *Ожидаемо:* `apt policy zabbix-server-mysql` показывает установленную версию 7.0.x.

4. **MON01: база и схема.**
   ```bash
   sudo mysql
   ```
   ```sql
   create database zabbix character set utf8mb4 collate utf8mb4_bin;
   create user 'zabbix'@'localhost' identified by 'ZabbixDB#2026';
   grant all privileges on zabbix.* to 'zabbix'@'localhost';
   set global log_bin_trust_function_creators = 1;
   quit;
   ```
   ```bash
   zcat /usr/share/zabbix-sql-scripts/mysql/server.sql.gz | \
     mysql --default-character-set=utf8mb4 -uzabbix -p zabbix
   sudo mysql -e "set global log_bin_trust_function_creators = 0;"
   ```
   *Ожидаемо:* импорт идёт 1–3 минуты без ошибок, `sudo mysql -e "use zabbix; show tables;" | wc -l` — больше сотни.

5. **MON01: конфиг сервера и запуск.** В `/etc/zabbix/zabbix_server.conf` раскомментировать и заполнить `DBPassword=ZabbixDB#2026`. Затем:
   ```bash
   sudo systemctl restart zabbix-server zabbix-agent2 apache2
   sudo systemctl enable zabbix-server zabbix-agent2 apache2
   ss -lntp | grep -E '10051|10050|:80'
   ```
   *Ожидаемо:* три процесса слушают порты, в `/var/log/zabbix/zabbix_server.log` строка `server #0 started`. Открываем `http://10.10.10.40/zabbix`, входим `Admin` / `zabbix`, **сразу меняем пароль**: Administration → Users → Admin → Change password.

6. **MON01: ставим сам себя на мониторинг по агенту.** В `/etc/zabbix/zabbix_agent2.conf` — `Server=10.10.10.40`, `ServerActive=10.10.10.40`, `Hostname=MON01`; `sudo systemctl restart zabbix-agent2`. В веб-интерфейсе: Data collection → Hosts → Create host, имя `MON01`, группа `Linux servers`, интерфейс Agent 10.10.10.40:10050, шаблон `Linux by Zabbix agent`.
   *Ожидаемо:* через 1–2 минуты в Latest data появляются `CPU utilization`, `Available memory`, `Interface ens33: Bits received`.

7. **UBU01 и GW01: поднимаем snmpd.** На обеих машинах:
   ```bash
   sudo apt -y install snmpd
   sudo tee -a /etc/snmp/snmpd.conf >/dev/null <<'EOF'
   agentAddress udp:161
   rocommunity Vekt0rRO 10.10.10.40
   sysLocation  "Moscow, office"
   sysContact  it@vektor.lab
   EOF
   sudo systemctl restart snmpd && sudo systemctl enable snmpd
   ```
   Проверка с MON01: `snmpwalk -v2c -c Vekt0rRO 10.10.10.30 1.3.6.1.2.1.1.1.0`. На UBU01 при необходимости открыть порт: `sudo ufw allow from 10.10.10.40 to any port 161 proto udp`.
   В Zabbix: Create host `UBU01` (группа `Linux servers`), интерфейс **SNMP** 10.10.10.30:161, макрос `{$SNMP_COMMUNITY}` = `Vekt0rRO`, шаблоны `Linux by SNMP` и `Network interfaces by SNMP`. То же для `GW01` (10.10.10.3).
   *Ожидаемо:* статус хостов SNMP — зелёный, в Latest data есть `Interface ens33: Bits received`.

8. **DC01 и FS01: включаем SNMP на Windows.** На DC01 в PowerShell от `adm.student`:
   ```powershell
   Install-WindowsFeature -Name SNMP-Service -IncludeManagementTools
   $c = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP\Parameters\ValidCommunities'
   New-ItemProperty -Path $c -Name 'Vekt0rRO' -Value 4 -PropertyType DWord -Force
   $m = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP\Parameters\PermittedManagers'
   New-ItemProperty -Path $m -Name '1' -Value '10.10.10.40' -PropertyType String -Force
   Restart-Service SNMP
   Get-Service SNMP
   ```
   `4` = read only, `8` = read write (не надо). То же на FS01. Проверка с MON01: `snmpwalk -v2c -c Vekt0rRO 10.10.10.20 1.3.6.1.2.1.1`.
   *Ожидаемо:* `Get-Service SNMP` → `Running`, с MON01 приходит `sysDescr: Hardware: Intel64 ... Software: Windows Version 10.0`. Добавьте оба хоста в Zabbix с шаблонами `Windows by SNMP` и `Network interfaces by SNMP`.
   *Важно:* служба SNMP в Windows официально считается устаревшей (deprecated) — в новых версиях её может не быть, тогда единственный путь — Zabbix agent.

9. **GW01 → MON01: потоки.** На MON01 запустить коллектор:
   ```bash
   sudo mkdir -p /var/flows
   sudo nfcapd -p 2055 -w /var/flows -S 1 -D -t 300
   sudo ss -lunp | grep 2055
   sudo ufw allow from 10.10.10.3 to any port 2055 proto udp
   ```
   На GW01:
   ```bash
   sudo apt -y install softflowd iperf3
   sudo softflowd -i ens33 -n 10.10.10.40:2055 -v 9
   sudo softflowd -i ens160 -n 10.10.10.40:2055 -v 9   # второй адаптер, если имя другое — проверьте ip -br a
   ```
   Генерируем трафик «в DMZ»: с WIN11-CL01 в браузере несколько раз открыть `http://10.10.20.10` и скачать файл; либо с MON01 `curl -o /dev/null http://10.10.20.10/`.
   *Ожидаемо:* через 5–6 минут в `/var/flows/YYYY/MM/DD/` появились файлы `nfcapd.*`, а `nfdump -r <файл> -c 10` показывает записи с `10.10.20.10`.

10. **Смотрим топ-разговоры и загрузку.**
    ```bash
    nfdump -r /var/flows/2026/02/16/nfcapd.202602161400 -s srcip/bytes -n 10
    nfdump -r /var/flows/2026/02/16/nfcapd.202602161400 -s ip/bytes -A srcip,dstip,dstport -n 10
    ```
    *Ожидаемо:* в топе — 10.10.10.40 (MON01, качал) и адрес WIN11-CL01, назначение — 10.10.20.10:80. Дальше в Zabbix открываем Latest data по GW01 и смотрим `Interface ens33: Bits received` — те же цифры в битах/с, что в потоках в байтах/с.

11. **Дашборд и триггер.** Monitoring → Dashboards → Create dashboard → виджет `Problems` (только группа `Linux servers`), виджет `Graph` для `GW01: Interface ens33: Bits received`. В шаблоне `Network interfaces by SNMP` включить прототип триггера «High bandwidth usage» (порог в процентах от скорости интерфейса) [ПРОВЕРИТЬ название].
    *Ожидаемо:* на дашборде два виджета, при прогоне трафика график визуально «подпрыгивает».

12. **Запись в журнал стенда.** Дописать в `stand-log.md` всё из раздела «Что изменилось на стенде».

### Проверка

- [ ] `snmpwalk -v2c -c Vekt0rRO 10.10.10.30 1.3.6.1.2.1.1.1.0` с MON01 отвечает `Linux UBU01 ...`
- [ ] `snmpwalk` к 10.10.10.20 и 10.10.10.10 отвечает `Windows Version 10.0 ...`
- [ ] В Zabbix 5 хостов (MON01, UBU01, GW01, DC01, FS01) в статусе Enabled, ошибок SNMP Unreachable нет
- [ ] `htpasswd`-пароль Admin в Zabbix изменён с дефолтного
- [ ] `ss -lunp | grep 2055` на MON01 показывает nfcapd
- [ ] `nfdump -r <последний файл> -s srcip/bytes -n 5` выдаёт непустой список с 10.10.20.10
- [ ] `sar -n DEV 1 3` на MON01 показывает колонку `%ifutil`
- [ ] На дашборде виджет Problems пуст


### Сломай и почини

```
Пользователь / ВМ / Что написал / Срочность
ivanov / WIN11-CL01 / «Всё встало с 14:00: папка \\FS01\Buh открывается
по 30 секунд, страница на 10.10.20.10 не грузится вообще. У Петровой так же.»
Срочность: Высокая
```

**Как воспроизвести поломку.** На GW01 поднять сервер нагрузки и запустить с него же «забытый» тест:
```bash
# GW01
sudo iperf3 -s -D
sudo iperf3 -c 10.10.10.30 -P 8 -t 900
```
Канал VMnet8 забивается, GW01 грузится по CPU — и маршрутизация в DMZ деградирует. Подождите 3–5 минут, чтобы Zabbix собрал данные.

**Диагностика по шагам.**
1. Zabbix → Dashboards → `Problems`: убедиться, что нет `Unreachable` и упавших сервисов. Значит, это не «сервер лёг», а производительность.
2. Zabbix → Latest data по GW01 и UBU01: графики `Bits received` выросли в десятки раз, а ошибок (`Errors`) нет — это чистая загрузка, а не потери.
3. На GW01: `sudo iftop -i ens33 -n -P` — видно пару 10.10.10.3 ↔ 10.10.10.30, порт 5201.
4. На MON01 через потоки: `nfdump -r <последний файл> -s ip/bytes -A srcip,dstip,dstport -n 10` — топ-1: `10.10.10.3 → 10.10.10.30:5201`.
5. На GW01 ищем источник: `sudo ss -tunp | grep 5201`, затем `ps aux | grep iperf3`.
6. Решение: `sudo pkill iperf3` (и `sudo systemctl disable --now` если повесили в автозапуск).
7. Контроль: через 5–10 минут график в Zabbix возвращается к 2–5%, `sudo curl -o /dev/null -w '%{time_total}\n' http://10.10.20.10/` даёт < 0,1 с.

**Ответ пользователю.**
> Иван, добрый день. Причину нашёл: с 14:00 на маршрутизаторе GW01 работал тестовый нагрузочный процесс, который занимал практически весь канал. Сейчас он остановлен, доступ к папке и к порталу восстановлен. Проверьте, пожалуйста, копирование отчёта — должно снова укладываться в 1–2 минуты. Если что-то ещё тормозит, напишите, посмотрю точечно.

**Закрытие заявки.** Категория «Сеть → производительность», решение: «нерегламентированный нагрузочный процесс на GW01», связь с инцидентом-первопричиной не нужна. Отдельным тикетом — задача «запретить запуск нагрузочных тестов на GW01 и поставить триггер на >80% загрузки интерфейса».

### Что изменилось на стенде

- Добавлена ВМ **MON01** (Ubuntu Server 24.04), 10.10.10.40, шлюз 10.10.10.2, DNS 10.10.10.10.
- На MON01: MariaDB, Zabbix Server 7.0.x + фронтенд `/zabbix`, zabbix-agent2, nfcapd (UDP/2055, каталог `/var/flows`), nfdump, sysstat, iftop.
- Учётка Zabbix `Admin` с новым паролем (записать в `stand-log.md`, не в открытый файл).
- На UBU01 и GW01 установлен `snmpd`, коммьюнити `Vekt0rRO`, доступ только с 10.10.10.40.
- На GW01 установлен `softflowd` (NetFlow v9 → 10.10.10.40:2055) и `iperf3`.
- На DC01 и FS01 включена роль/служба **SNMP-Service**, коммьюнити `Vekt0rRO` (read only), разрешён менеджер 10.10.10.40.
- В Zabbix добавлены хосты: MON01 (agent), UBU01, GW01 (SNMP), DC01, FS01 (SNMP).
- Создан дашборд `L2-Net` и включён триггер высокой загрузки интерфейса.

### Откат

VM → Snapshot Manager на MON01, GW01, DC01, FS01, UBU01 → выбрать `before-l2-039` → **Revert**. Порядок: сначала MON01 и GW01, потом Windows-машины. Помните: при откате пропадут все файлы потоков в `/var/flows` и история метрик в Zabbix — если они нужны для следующих глав, сделайте копию `/var/flows` на хост. После отката проверьте, что на DC01/FS01 SNMP-служба исчезла, а на GW01 нет процесса `softflowd` — иначе получите «висящего экспортёра» без коллектора.

## 6. Ошибки новичков и безопасность

1. **Коммьюнити `public` и `private` везде.** → SNMPv2c передаёт строку открытым текстом, любой в сети прочитает конфиг устройства. → Правильно: v3 `authPriv`, а если нельзя — уникальное длинное коммьюнити + ACL на IP коллектора.
2. **Считать загрузку по 32-битным `ifInOctets`.** → Счётчик переполняется, дельта становится отрицательной или «прыгает». → `ifHCInOctets`/`ifHCOutOctets` и контроль, что у устройства есть 64-битные счётчики.
3. **Оперировать «мгновенной» скоростью.** → При опросе раз в минуту короткий всплеск на 20 секунд усредняется до 30% и вы его не увидите. → Для точной картины: несколько источников (потоки), меньший интервал на критичных портах, p95, а не среднее.
4. **Путать «канал загружен» и «канал теряет».** → Смотрите только байты, а проблема в `errors`/`discards` или в ретрансмиссиях. → Всегда проверять ошибки, дуплекс, MTU.
5. **Ставить SNMP-мониторинг без `ifAlias`.** → Через месяц вы не помните, что «интерфейс 7» — это аплинк в DMZ. → Описывать интерфейсы на устройствах, а не в голове.
6. **Ждать от NetFlow содержимого.** → В потоке нет ни URL, ни имени файла, ни полезной нагрузки. → Нужен DPI/прокси-логи (урок 38), а не NetFlow.
7. **Сравнивать sFlow и NetFlow как одно и то же.** → sFlow — выборка, его цифры на 10–20% ниже реальных. → Сравнивать только однотипные источники, для биллинга/разбора — NetFlow/IPFIX.
8. **Мониторинг без синхронизации времени.** → Графики Zabbix, логи и потоки не совпадают по времени, корреляция невозможна. → NTP/chrony на всех узлах, включая коллектор и экспортёры.

**Безопасность.** SNMP read-write (`rwcommunity`, значение `8` в `ValidCommunities`) в учебной лабе кажется безобидным — в бою это удалённое управление конфигурацией устройства по протоколу без шифрования. Фронтенд Zabbix с дефолтным `Admin/zabbix`, выставленный в общую сеть, — это утечка всей карты сети и учётных данных. Порт 10051/tcp и 2055/udp должны быть доступны только с доверенных адресов, а не «со всей сети». Данные мониторинга — «кто с кем и когда общался» — сами по себе чувствительны: в РФ они могут содержать персональные данные (152-ФЗ), поэтому доступ к коллектору и к истории потоков даётся ограниченному кругу, а срок хранения ограничивается регламентом. На стенде `disableAuthorization yes` в snmptrapd допустим только потому, что это изолированная лаборатория.

## 7. Вопросы на собеседовании

**1. Чем опрос по SNMP отличается от trap и когда что использовать?**

Ответ: опрос инициирует менеджер раз в N секунд — это надёжно, даёт историю и временные ряды, но не мгновенную реакцию. Trap инициирует агент при событии — быстро, но UDP, без гарантии доставки, и он не даёт истории. На практике: метрики и состояние — опросом, аварийные события (link down, питание, температура) — trap, при этом событие всё равно подтверждают опросом.
*Что хочет услышать:* «trap — дополнение, а не замена опросу», понимание, что потерянный trap нельзя заметить.
*Красный флаг:* «trap надёжнее, потому что приходит сразу».

**2. Что такое OID и MIB, как вы найдёте нужный счётчик на незнакомом устройстве?**

Ответ: OID — числовой адрес объекта в дереве, MIB — описание дерева, превращающее цифры в имена. Ищу так: `snmpwalk` по `.1.3.6.1.2.1.1` (кто это), затем по `IF-MIB` (`.1.3.6.1.2.1.2` и `.1.3.6.1.2.1.31`) для интерфейсов; если нужен вендорский показатель — MIB-файл от производителя плюс `snmptranslate`, либо MIB-браузер.
*Что хочет услышать:* умение идти по стандартным MIB, а не «погуглю готовую команду».
*Красный флаг:* «MIB — это база данных SNMP на устройстве» (это описание, а не БД значений).

**3. NetFlow, IPFIX, sFlow — в чём разница и что выберете?**

Ответ: NetFlow (v5/v9) — от Cisco, кэш потоков, экспорт по завершении/таймауту, полный учёт. IPFIX — стандартизированный потомок v9 с шаблонами (RFC 7011), вендор-нейтрален. sFlow — не потоки, а статистическая выборка пакетов с интерфейса, дешевле по CPU, но приблизителен. Выбор: полный учёт и разбор инцидентов — IPFIX/NetFlow; высокая нагрузка на коммутаторе и нужна только общая картина — sFlow.
*Что хочет услышать:* «sFlow — это семплирование, его цифры не абсолютны».
*Красный флаг:* «Это одно и то же, просто разные названия».

**4. Как посчитать загрузку канала в процентах и почему важны 64-битные счётчики?**

Ответ: два замера `ifHCInOctets` с интервалом T, дельта × 8 / T = бит/с, делим на скорость из `ifSpeed`/`ifHighSpeed` и умножаем на 100. 32-битный счётчик обнуляется на 4 294 967 296 байтах — на 100 Мбит/с это примерно раз в 5–6 минут, на 1 Гбит/с ещё чаще, поэтому дельта ломается. Плюс отдельно смотрю ошибки и дискарды.
*Что хочет услышать:* формулу, понимание переполнения и того, что высокий % загрузки не всегда равен «плохо» — важен p95 и характер трафика.
*Красный флаг:* «Беру `ifInOctets`, там же то же самое».

**5. Как Zabbix понимает, что хост недоступен, и почему бывают ложные срабатывания?**

Ответ: есть штатный триггер шаблона по `agent.ping`/`icmpping` (`nodata` за период), плюс внутренние метрики — «SNMP agent is unreachable», «Zabbix agent is not available». Ложные срабатывания обычно из-за: обрывов сети на короткое время, слишком агрессивных таймаутов, перегрузки poller'ов (мало процессов на большое число элементов данных), блокировки ICMP файрволом на хосте, смены IP/DNS.
*Что хочет услышать:* умение отличить «упал хост» от «сломался мониторинг» и проверить второе вручную (`zabbix_get`, `snmpwalk` с сервера).
*Красный флаг:* «Zabbix всегда прав, значит сервер упал» без проверки.

## 8. Источники (официальная документация)

- **zabbix.com** → Documentation → разделы «Installation from packages» (Ubuntu/Debian), «SNMP monitoring», «Configuring SNMP», «Items», «Triggers». Поиск: `zabbix 7.0 ubuntu installation from packages`, `zabbix snmp template network interfaces`.
- **net-snmp.org** → Documentation и FAQ: `snmpd.conf`, `snmptrapd.conf`, `snmpwalk`/`snmpget`. Поиск: `net-snmp snmpd.conf rocommunity agentaddress`.
- **learn.microsoft.com** → «Install and configure SNMP service» (раздел про Windows Server, помечен как deprecated), «Get-NetAdapterStatistics», «Get-Counter». Поиск: `windows server snmp service install powershell validcommunities`.
- **nfdump / nfcapd** → репозиторий проекта (github.com/phaag/nfdump) и man-страницы `nfcapd(1)`, `nfdump(1)`, `nf dumps` filter syntax. Поиск: `nfdump filter syntax src ip dst port`, `nfcapd subdir format -S`.
- **softflowd** → man `softflowd(8)` и проект на GitHub. Поиск: `softflowd man page export netflow v9 -n collector`.
- **IETF (ietf.org, rfc-editor.org)** → RFC 7011 (IPFIX), RFC 2863 (IF-MIB, ifHCInOctets), RFC 3411–3418 (SNMPv3). Поиск: `rfc 7011 ipfix architecture`, `rfc 2863 ifmib ifHCInOctets`.
- **sflow.org** → «sFlow specification» и «sFlow vs NetFlow». Поиск: `sflow sampling explained`.
- **ubuntu.com/server/docs** → раздел про сеть/netplan. Поиск: `ubuntu 24.04 netplan static ip configuration`.
- **Astra Linux** — документация производителя по установке пакетов `snmpd` и настройке сети [ПРОВЕРИТЬ актуальный раздел на сайте производителя].
