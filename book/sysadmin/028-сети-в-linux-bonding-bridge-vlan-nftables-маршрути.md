# Сети в Linux: bonding, bridge, VLAN, nftables, маршрутизация, policy routing

## 1. Введение (зачем это нужно в реальной работе)

Тикет в воркере техподдержки «Вектора»:

> **Пользователь:** petrova (Мария Петрова, HR) / **ВМ:** WIN10-CL02
> **Что написала:** «Открываю портал `http://10.10.20.10` — висит и через минуту таймаут. Папка `\\FS01\HR` при этом открывается нормально. У Иванова из бухгалтерии то же самое, я спросила. Срочность: средняя, но перед отчётом надо».

Заявка — типичный вход в тему «сети на уровне Linux-администратора». Портал живёт в DMZ, до него надо дойти через машину-маршрутизатор GW01, а на ней всё держится на четырёх вещах сразу: маршрутизация между сетями, файрвол nftables, maybe policy routing для источника из DMZ, и физика/виртуализация внизу — bridge, bond, VLAN. Ни один Windows-инструмент вам это не покажет и не починит.

В работе Linux-админ встречает эти темы постоянно: любой сервер, у которого два сетевых адаптера (файловый, резервного копирования, шлюз в интернет, ВМ-хост), любая виртуализация (Proxmox или KVM — а там мост `vmbr0` под каждой ВМ), любая сегментация отделов через VLAN, любая замена устаревшего iptables на nftables. Плюс РФ-специфика: шлюзы на Debian/Astra/РЕД ОС, аппаратные межсетевые экраны, и запрос «покажи, что реально доходит до порта» закрывается тем же `tcpdump`.

После этой главы вы умеете: собрать агрегированный канал из двух NIC, объяснить, зачем он нужен; поднять программный мост; прогнать трафик через 802.1Q-тег; написать атомарный набор правил nftables с состоянием соединений; включить маршрутизацию и настроить таблицу маршрутов, отличную от `main`.

## 2. Теория «под капотом»

Разложим по уровням. Здесь нет TCP-портов, кроме одного: 22 для управления (SSH), потому что всё остальное — L2/L3.

**Уровень 2 — «как кадр доедет до соседа».** Bonding (агрегация каналов) — это подвид драйвера, который подставляет под один логический интерфейс несколько физических. Режимы, которые надо знать: `active-backup` (один активный, второй спит; работает без настройки коммутатора), `802.3ad` = LACP (нужна настройка на коммутаторе, зато полоса складывается), `balance-xor`, `balance-tlb/alb`, `broadcast`, `round-robin`. Проверка живучести канала — `miimon` (период в мс опроса линка через MII), счётчик отказоустойчивости — в `/proc/net/bonding/bond0`.

Bridge — программный коммутатор внутри ядра. Порты моста — это интерфейсы (физические, bond'ы, vlan'ы, veth от виртуалок). Мост учит MAC-адреса, пересылает кадры между портами, при `vlan_filtering=0` ничего не фильтрует по тегам. Именно поэтому в Proxmox всё «висит» на `vmbr0`.

VLAN (802.1Q) — 4-байтовая вставка в кадр с 12-битным VID (1–4094) и полем приоритета. Порт коммутатора может быть access (untagged, один VLAN) или trunk (несколько VLAN, тегированные). В Linux VLAN — это отдельный интерфейс-наследник: `bond0.100`.

**Уровень 3 — «куда отправить пакет».** Ядро держит таблицы маршрутов. Основная — `main`. Решение принимается по самому длинному совпадению префикса. Поверх этого есть RPDB — rule-based database маршрутизации: перед таблицами ядро просматривает список правил `ip rule` по возрастанию приоритета, и первое совпавшее правило указывает, в какую таблицу смотреть. Так делается policy routing: «трафик из DMZ ищем в таблице 100, у которой другой default».

**Уровень 3/4 — фильтрация.** nftables пришёл на смену iptables. Идея: одна утилита `nft`, дерево `table → chain → rule`. Таблица имеет family (`inet` = IPv4+IPv6 в одной, `ip`, `ip6`, `bridge`, `arp`, `netdev`). Цепочка имеет тип (`filter`, `nat`, `route`), хук (`prerouting`, `input`, `forward`, `output`, `postrouting`) и приоритет. Правила в цепочке проверяются сверху вниз, вердикты: `accept`, `drop`, `reject`, `jump`, `return`. Главное отличие от iptables — атомарная загрузка всего набора из файла и наличие множеств (`{ 80, 443 }`) и словарей.

```text
                 UBU01 (10.10.10.30, Ubuntu 24.04)
   ┌─────────────────────────────────────────────────────────┐
   │ ens33 ── 10.10.10.30/24  (основной, из L1-L2)           │
   │ ens37 ─┐                                                │
   │        ├── bond0 (active-backup) ─ bond0.100 ─ br0 (L2) │
   │ ens38 ─┘        драйвер bonding      802.1Q     мост     │
   └─────────────────────────────────────────────────────────┘

   LAN 10.10.10.0/24                                   DMZ 10.10.20.0/24
   DC01 .10  FS01 .20  UBU01 .30  PVE01 .70    WEB01 .10
        │                                                    │
        └──────[ens33] GW01 (Debian 12) [ens34]──────────────┘
              .3                        .1
                   │
                   ├── nftables: table inet filter
                   │      chains: input / forward / output  (policy drop)
                   ├── sysctl net.ipv4.ip_forward = 1
                   └── ip rule: from 10.10.20.0/24 → table 100
                                 table 100: default via 10.10.10.2
```

Важная деталь: `ip rule` и правила nftables **не сохраняются сами**. Маршруты и правила живут в ядре до перезагрузки; персистентность — это netplan (`routing-policy`), `post-up` в `/etc/network/interfaces` или systemd-юнит, а для nftables — `systemctl enable nftables` и `/etc/nftables.conf`.

## 3. Аналогия из жизни

Представьте логистический терминал «Вектора».

**Bonding** — две параллельные полосы на трассе между складом и терминалом. Пока обе свободны, машины едут по обеим (LACP); если одну перекрыли на ремонт, весь поток уходит на вторую (active-backup) — медленнее, но груз не встал.

**Bridge** — распределительный зал терминала: рамки на входе, дальше по номеру машины (MAC) кладовщик говорит, в какой док ехать. Первый раз он смотрит в накладную, дальше запоминает.

**VLAN** — цветные контейнеры. Все едут по одной дороге, но синие (бухгалтерия) разгружаются только в синий док, красные (HR) — только в красный. Никто не перепутает, хотя асфальт общий.

**Маршрутизация** — сортировочная таблица: «на Москву — через северные ворота, на Тверь — через южные». Побеждает самый конкретный адрес: написано «Тверь, ул. Советская, 5» — едем по этой записи, а не по общему «Тверь».

**Policy routing** — второй диспетчер, который смотрит не только на адрес, но и на пропуск водителя: «у кого пропуск DMZ — отправляй через северные ворота, остальных — через южные, адрес один и тот же».

**nftables** — охрана на въезде с журналом: пропускает только по списку, помнит, что фура уже заехала (conntrack), остальное разворачивает и записывает в журнал.

Где аналогия ломается: охрана не помнит состояний — а nftables с `ct state established` именно помнит, что ответ на исходящее соединение надо впустить, и без этого правила вы бы отрезали себе DNS и SSH.

## 4. Практика и команды

**1. Инвентаризация интерфейсов.**
```bash
ip -br link
ip -br addr
```
`-br` — brief (одна строка на интерфейс), `-br addr` добавляет адреса. Вывод:
```text
lo               UNKNOWN        00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>
ens33            UP             00:0c:29:aa:bb:01 <BROADCAST,MULTICAST,UP,LOWER_UP>
ens37            DOWN           00:0c:29:aa:bb:02 <BROADCAST,MULTICAST>
```
Смотрим: `UP`/`DOWN`/`LOWER_UP` (LOWER_UP = линк реально есть), MAC-адреса, наличие адреса.

**2. Детали устройства.**
```bash
ip -d link show bond0
ip -d link show bond0.100
```
`-d` — details, показывает параметры драйвера. Ищем `bond mode ACTIVE_BACKUP`, `vlan protocol 802.1Q id 100`.

**3. Создать bond и включить в него порты.**
```bash
sudo ip link add bond0 type bond mode active-backup miimon 100
sudo ip link set ens37 master bond0
sudo ip link set ens38 master bond0
sudo ip link set bond0 up
cat /proc/net/bonding/bond0
```
`mode` — режим, `miimon 100` — опрос линка каждые 100 мс. В `/proc/net/bonding/bond0` смотрим `Bonding Mode`, `Currently Active Slave`, `MII Status`, `Slave Interface`.

**4. Проверить отказоустойчивость.**
```bash
sudo ip link set ens37 down
grep 'Currently Active Slave' /proc/net/bonding/bond0
sudo ip link set ens37 up
echo +ens38 | sudo tee /sys/class/net/bond0/bonding/active_slave   # ручное переключение [ПРОВЕРИТЬ]
```
После падения активного слейва роль переходит на второй. Обратно сама не всегда возвращается — для этого есть `primary`.

**5. Сохранить конфигурацию (Ubuntu 24.04, netplan).**
```yaml
# /etc/netplan/60-lab-bond.yaml
network:
  version: 2
  ethernets:
    ens37: {dhcp4: false}
    ens38: {dhcp4: false}
  bonds:
    bond0:
      interfaces: [ens37, ens38]
      parameters:
        mode: active-backup
        mii-monitor-interval: 100
  vlans:
    bond0.100:
      id: 100
      link: bond0
  bridges:
    br0:
      interfaces: [bond0.100]
      dhcp4: false
```
```bash
sudo netplan generate
sudo netplan apply
```
Файл с номером 60 применяется после основных — так мы не трогаем ens33 и не рвём SSH.

**6. Создать VLAN.**
```bash
sudo ip link add link bond0 name bond0.100 type vlan id 100
sudo ip link set bond0.100 up
ip -d link show bond0.100
```
`link bond0` — родитель, `id 100` — тег.

**7. Создать bridge и посмотреть порты.**
```bash
sudo ip link add name br0 type bridge
sudo ip link set bond0.100 master br0
sudo ip link set br0 up
bridge link show
bridge vlan show
```
`bridge link show` — порты и их состояние. `bridge vlan show` будет **пустым**, пока не включён `vlan_filtering` (по умолчанию 0): мост пересылает теги «как есть». Если нужен vlan-aware мост — `ip link set br0 type bridge vlan_filtering 1`.

**8. Маршруты: смотреть и добавлять.**
```bash
ip route
ip route get 10.10.20.10
sudo ip route add 10.10.20.0/24 via 10.10.10.3 dev ens33
```
`ip route get` — «каким маршрутом я реально пойду до адреса»; лучший инструмент диагностики, когда `ping` не идёт. Вывод: `10.10.20.10 via 10.10.10.3 dev ens33 src 10.10.10.30 uid 0`.

**9. Policy routing.**
```bash
ip rule show
sudo ip rule add from 10.10.20.0/24 lookup 100 priority 100
sudo ip route add default via 10.10.10.2 table 100
ip route show table 100
ip route get 8.8.8.8 from 10.10.20.10
```
`ip rule show` по умолчанию:
```text
0:      from all lookup local
32766:  from all lookup main
32767:  from all lookup default
```
Наше правило с приоритетом 100 встанет выше `main` — это и есть переключение таблицы по источнику. `from ... lookup 100` + `default via ... table 100` — классическая схема «DMZ ходит в интернет через свой канал».

**10. То же через метку (fwmark) — когда признак не адрес.**
```bash
sudo nft add table ip mangle
sudo nft add chain ip mangle prerouting '{ type filter hook prerouting priority mangle ; }'
sudo nft add rule ip mangle prerouting ip saddr 10.10.20.10 meta mark set 0x1
sudo ip rule add fwmark 0x1 lookup 100 priority 90
```
Приоритет 90 сработает раньше 100 — правило с меткой «перебивает» правило по источнику. Так делают, когда политику задаёт nftables (например, пометить не по IP, а по порту или по домену).

**11. nftables: проверить и загрузить набор.**
```bash
sudo nft -c -f /etc/nftables.conf     # check, ничего не применяет
sudo nft -f /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft list ruleset
```
Минимальный рабочий `/etc/nftables.conf` для шлюза:
```nft
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    iif "lo" accept
    ip protocol icmp accept
    ip saddr 10.10.10.0/24 tcp dport 22 accept
    counter log prefix "IN-DROP: " limit rate 5/minute
  }
  chain forward {
    type filter hook forward priority filter; policy drop;
    ct state established,related accept
    ip protocol icmp accept
    iifname "ens33" oifname "ens34" ip saddr 10.10.10.0/24 \
      ip daddr 10.10.20.10 tcp dport { 80, 443 } counter accept
    counter log prefix "FWD-DROP: " limit rate 5/minute
  }
  chain output {
    type filter hook output priority filter; policy accept;
  }
}
```
`ct state established,related accept` — пропуск ответов на уже разрешённые соединения (это и есть stateful-файрвол). `counter` — счётчик пакетов/байт по правилу, видно в `nft list ruleset`. Логи дропов уходят в ядровой журнал: `journalctl -k -f`.

**12. Включить маршрутизацию.**
```bash
sudo sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-router.conf
sudo sysctl --system
```
Без этого ядро молча выбросит пакет, пришедший не на свой адрес, — и вы полчаса будете искать «а почему не идёт».

**13. Посмотреть теги в живом трафике.**
```bash
sudo tcpdump -ni ens37 -e -c 5 vlan
```
`-e` — печатать заголовки L2 (там видно `ethertype 802.1Q (0x8100), vlan 100`), `-n` — без DNS, `-c` — сколько пакетов взять. `-i bond0` тоже покажет тег `[ПРОВЕРИТЬ]` на вашей версии ядра.

**14. Таблица соединений.**
```bash
sudo apt install conntrack
sudo conntrack -L | grep 10.10.20
```
Показывает, что ядро реально видит как соединение. Незаменимо, когда правило написано, а трафик «не тот».

### GUI-эквиваленты
В Ubuntu Server GUI нет. В Astra Linux / РЕД ОС с рабочим столом сеть настраивается через «Настройки → Сеть» и NetworkManager (`nmcli`), но bonding/bridge/VLAN/policy routing там всё равно делаются руками или через `nmcli con add type bond ...` — на уровне администратора CLI первичен.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS) — доступ по SSH и консоль VMware, `sudo`.
- **GW01** (Debian 12, 10.10.10.3 / 10.10.20.1) — с L2, два адаптера. Проверьте: `ip -br addr` показывает оба адреса. Пакет `nftables` и `conntrack` могут требовать установки: `sudo apt install nftables conntrack` (нужен доступ к репозиториям через NAT-устройство 10.10.10.2).
- **WEB01** (10.10.20.10, nginx) — отвечает на `curl -I http://10.10.20.10` с самой себя.
- **WIN11-CL01** — рабочее место ivanov, адрес по DHCP, нужен для проверки клиентского доступа.
- **PVE01** (10.10.10.70) — не меняем, только посмотрим его мост для сравнения.

### Снапшот
В VMware Workstation на выключенных/работающих ВМ: **VM → Snapshot → Take Snapshot**, имя `before-sysadmin-028` для **UBU01, GW01, WEB01, WIN11-CL01**. Для UBU01 — выключите ВМ перед снапшотом, чтобы снапшот не тянул состояние адаптеров.

### Шаги

1. **UBU01, питание выключено.** VM → Settings → Add → Network Adapter → Custom: **VMnet8**, повторить ещё раз. Запишите себе: два новых адаптера в основной сети. Включите ВМ. Ожидаемо: `ip -br link` показывает плюс два интерфейса (`ens37`, `ens38` — имена могут быть другими, зафиксируйте фактические и дальше подставляйте свои).

2. **UBU01.** `sudo ip link add bond0 type bond mode active-backup miimon 100`, `sudo ip link set ens37 master bond0`, `sudo ip link set ens38 master bond0`, `sudo ip link set bond0 up`. Ожидаемо: `cat /proc/net/bonding/bond0` → `Bonding Mode: fault-tolerance (active-backup)`, `Currently Active Slave: ens37`, оба слейва `MII Status: up`.

3. **UBU01.** `sudo ip link set ens37 down`, затем `grep 'Currently Active Slave' /proc/net/bonding/bond0`; вернуть `sudo ip link set ens37 up`. Ожидаемо: активным стал `ens38`. Это и есть демонстрация отказоустойчивости.

4. **UBU01.** `sudo ip link add link bond0 name bond0.100 type vlan id 100`, `sudo ip link set bond0.100 up`, `ip -d link show bond0.100`. Ожидаемо: в выводе `vlan protocol 802.1Q id 100`.

5. **UBU01.** `sudo ip link add name br0 type bridge`, `sudo ip link set bond0.100 master br0`, `sudo ip link set br0 up`, `sudo ip addr add 10.10.10.35/32 dev br0`, `bridge link show`. Ожидаемо: `bond0.100` числится портом моста, `br0` — `state UP`. Адрес даём `/32` намеренно: так мы не создаём второй connected-маршрут в 10.10.10.0/24 и не рискуем потерять SSH на 10.10.10.30.

6. **UBU01.** Проверяем, что кадры реально уходят с тегом:
```bash
sudo ip route add 10.10.10.10/32 dev br0 table 100
sudo ip rule add from 10.10.10.35 lookup 100 priority 100
sudo tcpdump -ni "$(grep -oP 'Currently Active Slave:\s+\K\S+' /proc/net/bonding/bond0)" -e -c 5 vlan &
sudo ping -c 2 -I 10.10.10.35 10.10.10.10
```
Ожидаемо: в дампе строки вида `ethertype 802.1Q (0x8100), length 102: vlan 100, p 0, ethertype IPv4`, ответа на ping нет. Это нормально и поучительно: виртуальный коммутатор VMware Workstation без настройки VLAN тегированные кадры между ВМ не пропускает `[ПРОВЕРИТЬ]`, в реальной сети на порту коммутатора был бы настроен trunk.

7. **UBU01.** Убираем тестовую ловушку: `sudo ip rule del from 10.10.10.35 lookup 100 priority 100`, `sudo ip route del 10.10.10.10/32 dev br0 table 100`. Устройства `bond0`, `bond0.100`, `br0` оставляем. Переносим конфигурацию в netplan (файл из раздела 4, п.5), **работая в консоли VMware, а не по SSH**: `sudo netplan generate && sudo netplan apply`. Ожидаемо: `ip -br link` — те же устройства, но теперь они переживут перезагрузку; SSH на 10.10.10.30 не отвалился.

8. **GW01.** Включаем маршрутизацию: `sudo sysctl -w net.ipv4.ip_forward=1`, создаём `/etc/sysctl.d/99-router.conf` со строкой `net.ipv4.ip_forward = 1`, `sudo sysctl --system`. Ожидаемо: `sysctl net.ipv4.ip_forward` → `= 1`.

9. **GW01.** Записываем `/etc/nftables.conf` (текст из раздела 4, п.11; проверьте, что имена `ens33`/`ens34` совпадают с вашими — `ip -br link`). Далее `sudo nft -c -f /etc/nftables.conf` → без вывода; `sudo nft -f /etc/nftables.conf`; `sudo systemctl enable --now nftables`; `sudo nft list ruleset`. Ожидаемо: цепочки с `policy drop`, счётчики `packets 0 bytes 0`.

10. **GW01.** Policy routing для DMZ: `sudo ip rule add from 10.10.20.0/24 lookup 100 priority 100`, `sudo ip route add default via 10.10.10.2 table 100`. Проверка: `ip route get 8.8.8.8 from 10.10.20.10` → `8.8.8.8 from 10.10.20.10 via 10.10.10.2 dev ens33 table 100`. Постоянство: добавьте обе команды в `post-up` своего интерфейса или в systemd-юнит (правила `ip rule` сами не сохраняются).

11. **WIN11-CL01.** В PowerShell от администратора: `route add 10.10.20.0 mask 255.255.255.0 10.10.10.3 -p`. Затем `curl.exe -I http://10.10.20.10` и `tracert 10.10.20.10`. Ожидаемо: `HTTP/1.1 200 OK` (или 301 от nginx), а трассировка идёт через `10.10.10.3`. Правильный промышленный способ раздать этот маршрут — DHCP-опция 121 на DC01; ручной `route add -p` оставляем как быстрый тест.

12. **WEB01.** `curl -m 5 -I http://10.10.10.20` (FS01) — ожидаемо таймаут: обратное направление DMZ→LAN мы закрыли политикой `forward`. На GW01 `nft list chain inet filter forward` — счётчик перед `log prefix "FWD-DROP"` вырос; `journalctl -k -n 5` показывает строку `FWD-DROP:`. В реальной эксплуатации именно так и должно быть: DMZ инициирует только к разрешённым сервисам.

### Проверка
- [ ] `ip -br link` на UBU01: `bond0` в состоянии `UP`, `bond0.100` и `br0` есть.
- [ ] `/proc/net/bonding/bond0`: режим `active-backup`, оба слейва `up`.
- [ ] Отключение активного слейва переключает `Currently Active Slave`.
- [ ] `ip -d link show bond0.100` содержит `id 100`.
- [ ] `bridge link show` показывает `bond0.100` как порт `br0`.
- [ ] В `tcpdump -e` виден `vlan 100`.
- [ ] После `netplan apply` конфигурация не потерялась (`netplan get` / повторный `ip -br link`).
- [ ] `sysctl net.ipv4.ip_forward` = 1 и значение есть в `/etc/sysctl.d/99-router.conf`.
- [ ] `systemctl is-enabled nftables` → `enabled`.
- [ ] `ip rule show` содержит строку с `from 10.10.20.0/24 lookup 100`.
- [ ] `ip route get 8.8.8.8 from 10.10.20.10` показывает `table 100`.
- [ ] С WIN11-CL01 портал `http://10.10.20.10` открывается, `tracert` идёт через GW01.
- [ ] С WEB01 в LAN — таймаут, в журнале GW01 есть `FWD-DROP`.

### Сломай и почини

> **Пользователь:** petrova / **ВМ:** WIN10-CL02
> **Что написала:** «Портал опять не открывается — `http://10.10.20.10`, страница грузится и отваливается по таймауту. Утром работало. Папка `\\FS01\HR` открывается. Срочность: высокая, отчёт через час».

**Как воспроизвести поломку (GW01):** `sudo sysctl -w net.ipv4.ip_forward=0`. Всё остальное — правила nftables, маршрут на клиенте, nginx на WEB01 — остаётся рабочим. Это самая коварная поломка: «ничего не меняли», а маршрутизация тихо выключилась (например, после ручной правки sysctl или запуска тестового стенда).

**Диагностика по порядку:**
1. С WIN10-CL02: `ping 10.10.10.3` — GW01 отвечает? Значит, L2 и адрес клиента в порядке.
2. С клиента: `ping 10.10.20.10` — таймаут. Проблема за шлюзом.
3. На GW01: `ip -br addr` — оба адреса (`10.10.10.3`, `10.10.20.1`) на месте, `LOWER_UP` есть.
4. На GW01: `sysctl net.ipv4.ip_forward` → **`= 0`. Нашли.**
5. Контрольно: `sudo nft list ruleset | head -40` — цепочки на месте, счётчики `forward` не растут (пакеты до форварда не доходят).
6. `ip route get 10.10.20.10` на GW01 — маршрут до DMZ корректен, значит дело не в маршрутах.

**Решение:** `sudo sysctl -w net.ipv4.ip_forward=1`, затем проверить `/etc/sysctl.d/99-router.conf` (если строки нет — добавить и `sudo sysctl --system`). Проверка с клиента: `curl.exe -I http://10.10.20.10` → 200.

**Ответ пользователю:** «Мария, добрый день. Проблема была на нашем шлюзе между офисной сетью и сегментом с порталом — слетела настройка пересылки трафика, файловый сервер работает в другом сегменте и его это не касалось. Сейчас пересылку восстановил и закрепил в конфигурации, чтобы не повторилось. Портал проверен — открывается. Если снова увидите таймаут, пришлите скриншот и время, посмотрим логи».

**Закрытие заявки:** категория «Сеть / маршрутизация», причина «отключён `net.ipv4.ip_forward` на GW01», решение «включено и закреплено в `/etc/sysctl.d/99-router.conf`», профилактика «добавить в мониторинг проверку forward-счётчиков и состояния sysctl» (про мониторинг — урок 29 и MON01).

### Что изменилось на стенде
Строки для `stand-log.md`:
- **UBU01**: добавлены два адаптера в VMnet8 (`ens37`, `ens38`); создан `bond0` (active-backup, miimon 100); создан `bond0.100` (VLAN 100); создан `br0` (порт `bond0.100`, адрес 10.10.10.35/32); конфигурация вынесена в `/etc/netplan/60-lab-bond.yaml`.
- **GW01**: включён `net.ipv4.ip_forward=1` (+ `/etc/sysctl.d/99-router.conf`); установлен и включён `nftables.service`, конфигурация `/etc/nftables.conf` (input drop, forward drop с разрешением LAN→WEB01:80/443); добавлено правило `ip rule from 10.10.20.0/24 lookup 100` и `default via 10.10.10.2 table 100`.
- **WIN11-CL01**: постоянный маршрут `10.10.20.0/24 → 10.10.10.3`.
- **WEB01**: без изменений (использовался как цель), логи дропов смотрятся на GW01.

### Откат
VM → Snapshot Manager на UBU01 / GW01 / WIN11-CL01 → выбрать `before-sysadmin-028` → **Revert**. Откат UBU01 вернёт и количество адаптеров, поэтому дополнительные NIC в настройках VM останутся «висеть» — при необходимости удалите их вручную в VM → Settings. Точечный откат без снапшота: `sudo ip rule del from 10.10.20.0/24 lookup 100`; `sudo ip route flush table 100`; `sudo nft flush ruleset` (осторожно — снимет всю фильтрацию); `sudo sysctl -w net.ipv4.ip_forward=0`; на WIN11-CL01 `route delete 10.10.20.0`.

## 6. Ошибки новичков и безопасность

1. **`nft flush ruleset` на живой машине или загрузка набора с `policy drop` без правила для SSH** → мгновенный лок-аут, лечится только консолью гипервизора. Правильно: сначала `nft -c -f`, а набор держать в файле и применять атомарно, имея открытую консоль VMware.
2. **Смешивать iptables и nftables** → правила «не работают», потому что на современных дистрибутивах `iptables` — это обёртка над nft (`iptables-nft`), и `iptables -L` показывает не всё. Правильно: одна технология на хост, проверка `nft list ruleset`.
3. **Bond `802.3ad` без настройки LACP на коммутаторе** → канал не поднимается или работает только один линк. Правильно: `active-backup` как безопасный дефолт, LACP — только по согласованию с сетевиками.
4. **Bond без `miimon`** → отказ кабеля не детектится, трафик уходит в пустоту. Правильно: всегда `miimon 100` (или `arp_interval` для сложных схем).
5. **VLAN id не совпал с портом коммутатора / перепутан trunk и access** → «пинг не идёт, хотя линк UP». Правильно: фиксировать схему тегов в документации и сверять `tcpdump -e` с обеих сторон.
6. **Bridge без STP при петле** → широковещательный шторм, сеть ложится за секунды. Правильно: `stp_state 1` на мостах с потенциальной петлёй и проверка топологии.
7. **Два default-маршрута без metric** → «то работает, то нет», асимметричный трафик. Правильно: осознанный `metric` или policy routing с проверяемым `ip route get`.
8. **Забыть `ip_forward` и/или забыть его сохранить** → работает до перезагрузки. Правильно: `/etc/sysctl.d/` и проверка после ребута.

### Безопасность
- Открытый `forward` с `policy accept` «на время, чтобы проверить» — самая частая причина, по которой из DMZ видно всю внутреннюю сеть. В боевой среде forward-политика только `drop`, разрешения — по конкретным парам адрес/порт и с логированием.
- `masquerade` из DMZ в LAN скрывает источник в логах и ломает аудит — NAT применяют к выходу в интернет, а не между внутренними сегментами.
- Правило `ip saddr 10.10.10.0/24 tcp dport 22 accept` допустимо только для管理-сегмента; на боевом шлюзе SSH ограничивают отдельным management-VLAN и ключами, а не общим пулом офиса.
- Логирование дропов обязательно (`log prefix ... limit rate`), но без `limit` оно само станет DoS для журнала.
- nftables-конфиг — объект конфигурации: он должен лежать в git и проходить ревью, а не правиться «в бою» через `nft add rule`.

## 7. Вопросы на собеседовании

**1. Чем `802.3ad` отличается от `active-backup` и что нужно от коммутатора?**
Ответ: `active-backup` — один активный слейв, второй в горячем резерве, коммутатор ничего не настраивает, полоса не складывается, есть переключение на отказ (доли секунды). `802.3ad` (LACP) — агрегация в один логический канал, нужен LACP-порт-ченнел на коммутаторе, полоса складывается, балансировка по `xmit_hash_policy` (по умолчанию на уровне L2, для нормального распределения часто ставят `layer3+4`). Проверка — `/proc/net/bonding/bond0`, `LACP rate`, состояние партнёра.
*Что хочет услышать интервьюер:* понимание, что LACP требует двусторонней настройки и что режим выбирается по возможностям сети.
*Красный флаг:* «всегда ставлю 802.3ad, он же быстрее» без упоминания коммутатора.

**2. Что такое policy routing и как он реализован в Linux?**
Ответ: выбор таблицы маршрутизации не только по адресу назначения, но и по дополнительным признакам — источнику, метке (`fwmark`), ToS, входному интерфейсу. Реализация — RPDB: список `ip rule` с приоритетами, первое совпавшее правило задаёт таблицу; таблицы 1–252 произвольные, `main` — 254, `local` — 255. Пример: `ip rule add from 10.10.20.0/24 lookup 100 priority 100` + `ip route add default via 10.10.10.2 table 100`. Проверка — `ip route get <ip> from <src>`, `ip rule show`. Правила не персистентны, нужен netplan `routing-policy` или systemd-юнит.
*Что хочет услышать интервьюер:* порядок правил, номера таблиц, как проверить и как сохранить.
*Красный флаг:* путать таблицу с цепочкой nftables или утверждать, что `ip rule` сохраняется сам.

**3. Чем nftables отличается от iptables по архитектуре?**
Ответ: iptables — отдельные утилиты под каждое семейство (`iptables`, `ip6tables`, `arptables`, `ebtables`) и линейные списки правил, каждое изменение — перезапись всего набора в ядро, не атомарно. nftables — единый `nft`, дерево `table (family) → chain (type/hook/priority) → rule`, `inet`-family покрывает IPv4 и IPv6 сразу, есть множества и словари (интервалы, IP-списки — O(1) вместо линейного перебора), атомарная загрузка набора из файла (`nft -f`), счётчики и `log` как выражения внутри правила. Ядро всё равно использует netfilter-хуки, поэтому конвертация «iptables → nft» возможна (`iptables-nft`).
*Что хочет услышать интервьюер:* атомарность, единая утилита, семейство `inet`, множества.
*Красный флаг:* «nftables — это отдельный файрвол в обход netfilter».

**4. Зачем гипервизору мост и что будет, если ошибиться с VLAN на порту?**
Ответ: мост (`vmbr0` в Proxmox, `br0` в KVM) — L2-коммутатор внутри хоста, объединяющий физический NIC и виртуальные интерфейсы ВМ в один broadcast-домен; ВМ получает адрес в той же сети, что и хост. `vlan_filtering` позволяет сделать мост VLAN-aware: порт ВМ делают access для её VLAN, аплинк — trunk. Ошибка в теге/PVID → ВМ грузится, линк UP, но нет DHCP и пинга: типичная ситуация «хост видит, гость нет». Диагностика — `bridge link show`, `bridge vlan show`, `tcpdump -e` на аплинке.
*Что хочет услышать интервьюер:* понимание, что bridge — это L2, и умение отличить проблему тегов от проблемы маршрутизации.
*Красный флаг:* «мост нужен, чтобы ВМ имела доступ в интернет» (это NAT/маршрутизация, не мост).

**5. Как вы построите политику фильтрации на шлюзе между офисом и DMZ?**
Ответ: базовая политика `drop` на `input` и `forward`, `accept` только для `ct state established,related` (stateful), `lo`, ICMP — осознанно (по типам), управление — SSH только из management-подсети; forward — по матрице «источник → назначение → порт», по одной строке на разрешённую пару, каждая с `counter`; в конце — `counter log prefix ... limit rate` для видимости дропов; обратное направление DMZ→LAN не открывается вообще, при необходимости — через прокси/reverse-proxy, а не через форвард. Всё в файле `/etc/nftables.conf`, версионируется, применяется через `nft -f` после `nft -c`.
*Что хочет услышать интервьюер:* default-deny, состояние соединений, минимум разрешений, логирование, конфигурация как код.
*Красный флаг:* «открываю `forward accept`, а потом закрываю лишнее» или отсутствие `established`.

## 8. Источники

- **kernel.org** — раздел Documentation → networking: статьи `bonding` (режимы, miimon, xmit_hash_policy), `bridge` (vlan_filtering), `vlan`. Поиск: `linux kernel documentation bonding rst`.
- **nftables.org / wiki.nftables.org** — официальный вики: разделы «Quick reference», «Configuring NAT», «Sets», «Logging traffic». Поиск: `nftables wiki quick reference`.
- **man7.org** — man-страницы `ip-rule(8)`, `ip-route(8)`, `ip-link(8)`, `nft(8)`, `bridge(8)`. Поиск: `man7 ip-rule`.
- **ubuntu.com** — документация Netplan: раздел про bonds, bridges, VLANs и `routing-policy`. Поиск: `netplan documentation bonds vlans routing-policy`.
- **debian.org** — Debian Wiki, статьи `nftables` и `BridgeNetworkConnections` (схемы с `/etc/network/interfaces`). Поиск: `debian wiki nftables`.
- **NetworkManager** — документация по `nmcli` для bond/bridge/vlan `[ПРОВЕРИТЬ актуальный адрес раздела]`. Поиск: `nmcli bond bridge vlan example`.

Глубокие ссылки намеренно не даю: структура сайтов меняется, а поисковая фраза доведёт до актуальной страницы быстрее.
