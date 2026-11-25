# VLAN, транки и STP: проектирование и настройка сегментации

## 1. Введение (зачем это нужно в реальной работе)

Тикет №4127. **Пользователь:** Петрова, ВМ WIN10-CL02 (HR). **Что написала:** «Здравствуйте! После обеда не открывается `\\FS01\HR` — пишет "Не удалось получить доступ к сетевому ресурсу". У Иванова из бухгалтерии всё работает. И ещё у нас в кабинете интернет стал сильно тормозить. Срочность высокая, у меня отчёт». 

Классика. Два симптома в одном тикете — «у одного работает, у другого нет» и «тормозит сеть» — почти всегда означают, что проблема не в сервере и не в учётке, а на втором уровне: порт попал не в тот VLAN, на коммутаторе закольцевалась линия, STP не отработал. Сервер FS01 при этом абсолютно здоров.

Почему это встречается постоянно: любая компания больше 20 человек так или иначе сегментирует сеть. Бухгалтерия с 1С не должна видеться с гостями и складскими терминалами, камеры не должны забиватьbroadcast'ом офис, телефония живёт в отдельном VLAN, а порт в переговорке вообще нужно отключить, чтобы туда не воткнули свой ноутбук. VLAN — это базовый инструмент, которым это делается, и на нём же построена гостевая сеть, DMZ и Wi-Fi с 802.1X (урок 37).

После главы ты должен уметь: спроектировать план VLAN для компании на 50 человек, настроить access- и trunk-порты, объяснить, что такое native VLAN и почему её меняют, поднять STP так, чтобы корнем был нужный коммутатор, и диагностировать три типовые аварии — «порт не в том VLAN», «петля», «транк обрезал VLAN».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**VLAN (Virtual LAN)** — это логический сегмент второго уровня. Физически коммутатор один, но внутри него как будто несколько независимых коммутаторов: кадры из VLAN 10 никогда не выйдут в VLAN 20. Работает это на уровне L2 модели OSI, потому что в кадр Ethernet вставляется тег стандарта **IEEE 802.1Q**.

Тег — 4 байта, вставляются между полем Source MAC и полем EtherType:

```text
Кадр Ethernet II с тегом 802.1Q (4 байта) — всего +4 байта к длине

+-----------+-----------+-----------+-----+-----+--------------+----------+-------+
| Dst MAC   | Src MAC   | TPID      | PCP | DEI |  VID (12 б)  | EtherType| Data  |
| 6 байт    | 6 байт    | 0x8100    | 3 б | 1 б |  0..4095     | 2 байта  | ...   |
+-----------+-----------+-----------+-----+-----+--------------+----------+-------+
                                        │           │
                                        │           └─ номер VLAN: рабочие 1..4094
                                        └─ приоритет 0..7 (CoS для QoS, телефония)
```

Что важно знать про поля: TPID `0x8100` — маркер «дальше идёт тег»; VID — 12 бит, реально используются 1–4094 (0 = только приоритет, 4095 зарезервирован). Отсюда классическое «максимум 4094 VLAN на домен». Кадр вырастает с 1518 до 1522 байт — обычно коммутатор это переваривает, но на промежуточном оборудовании, которое режет MTU, это причина «не проходят большие пакеты» (связка с уроком 29).

**Типы портов:**

| Тип порта | Что делает | Где применяется |
|---|---|---|
| **Access** | принадлежит одному VLAN, снимает тег при отправке хосту и вешает при приёме | порты для ПК, принтеров, серверов |
| **Trunk** | несёт много VLAN одновременно, каждый кадр тегирован | линки между коммутаторами, к роутеру, к гипервизору |
| **Hybrid** | смесь: какие-то VLAN тегированы, какие-то нет | встречается у MikroTik, Huawei, Eltex |

**Native VLAN (PVID для нетегированного трафика)** — тот VLAN, кадры которого на транке идут **без тега**. По умолчанию это VLAN 1. Именно здесь самая частая авария: на одном конце транка native VLAN 1, на другом 999 — трафик нетегированных VLAN «переезжает» в чужой сегмент, а в логах тишина. Правило: native VLAN на транке должна совпадать с обеих сторон и лучше вообще не нести пользовательский трафик.

**STP (Spanning Tree Protocol)** — протокол, который гасит петли на втором уровне. Петля в Ethernet смертельна: у кадра нет поля TTL, поэтому широковещательный кадр (ARP, DHCP Discover, любой broadcast) начнёт бесконечно размножаться, и сеть умрёт за секунды. STP строит дерево: выбирает **корневой мост (root bridge)** и блокирует избыточные порты, оставляя один активный путь.

```text
Топология стенда после этой главы (лабораторная внутри GW01)

        VLAN 10 «Офис» 10.10.10.0/24        VLAN 20 «DMZ» 10.10.20.0/24
        ns-buh 10.10.10.201                ns-dmz 10.10.20.201
             │ access pvid 10                    │ access pvid 20
             │                                   │
        ┌────┴───────────────────────────────────┴────┐
        │            br0 — «коммутатор»               │
        │  порты access (pvid 10/20), trunk self      │
        └───────────────────┬─────────────────────────┘
                            │ tagged 10, 20
                ┌───────────┴────────────┐
                │  GW01 router-on-a-stick│
                │  br0.10 = 10.10.10.203 │
                │  br0.20 = 10.10.20.253 │
                └────────────────────────┘

        Параллельно: brs1 <═══ 2 линка ═══> brs2  → STP блокирует один
```

Корневой мост выбирается по **Bridge ID** = приоритет (по умолчанию 32768, кратен 4096) + MAC-адрес. Меньше — главнее. Дальше каждый коммутатор выбирает один **корневой порт** (лучший путь к корню), на каждом сегменте — **назначенный порт**. Остальные — заблокированы.

Версии STP: **802.1D** (классический, сходится 30–50 секунд), **PVST+** (Cisco, своё дерево на каждый VLAN), **802.1w RSTP** (быстрая сходимость, 1–3 секунды), **802.1s MSTP** (несколько VLAN мапятся на один экземпляр дерева — то, что используют в проде). Служебные кадры — **BPDU**, отправляются multicast-ом на `01:80:C2:00:00:00` каждые 2 секунды.

**Где это в общей картине:** VLAN — это L2-сегментация; чтобы разные VLAN общались, нужен L3: либо роутер с саб-интерфейсами («router-on-a-stick», один физический линк с тегами), либо L3-коммутатор с SVI-интерфейсами на каждом VLAN. Именно здесь VLAN стыкуется с уроком 30 про маршрутизацию. VLAN нужен почти везде: гипервизоры (в ESXi VLAN задаётся на port group, в Hyper-V — VLAN ID у виртуального адаптера), Wi-Fi (SSID → VLAN), IP-телефония (голос в отдельном VLAN с приоритетом PCP).

## 3. Аналогия из жизни

Представь офисное здание с одним длинным коридором и дверями без номеров. Все сотрудники — в одном коридоре, любой может зайти в любую дверь, а если кто-то крикнет (broadcast) — услышат все сразу, и начнётся хаос. Это сеть без VLAN.

VLAN — это когда коридор разгородили стенами и повесили на двери номера: 10 — бухгалтерия, 20 — склад, 99 — охрана. Внутри своей зоны люди ходят свободно, а между зонами — только через вахту (маршрутизатор), где есть журнал и можно не пустить. **Транк** — это служебный лифт, который возит тележки из всех зон сразу; на каждой тележке ярлык с номером зоны (тег 802.1Q), и лифт смотрит на ярлык. **Native VLAN** — это тележки без ярлыка: лифт считает, что они из зоны «по умолчанию», и если на разных этажах эта зона понимается по-разному, тележка уедет не туда. **STP** — это пожарный, который обходит здание и закрывает лишние двери, чтобы сквозняк не гонял бумаги по кругу.

Аналогия ломается на скорости: в реальности при петле без STP сеть умирает за секунды, и «пожарный» со своими 30 секундами сходимости сам становится частью проблемы.

## 4. Практика и команды

Стенд виртуальный, поэтому ниже — Linux (это то, что реально выполняется в лабе), плюс боевой синтаксис Cisco и MikroTik. Напоминание: `ip`/`bridge` — из пакета `iproute2`, команды под root.

**1. Посмотреть, что за мост и включены ли фильтрация VLAN и STP.**

```bash
ip -d link show type bridge
```
Сокращённый вывод:
```text
3: br0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...
    bridge forward_delay 1500 hello_time 200 max_age 2000
    vlan_filtering 1 vlan_protocol 802.1Q stp_state 1 ...
```
Смотрим `vlan_filtering 1` (без неё теги не разбираются вообще) и `stp_state`.

**2. Создать «коммутатор» с фильтрацией VLAN и STP.**

```bash
ip link add br0 type bridge vlan_filtering 1 stp_state 1
ip link set br0 up
```
`type bridge` — виртуальный свитч, `vlan_filtering 1` — включить разбор 802.1Q, `stp_state 1` — включить STP (в Linux это 802.1D).

**3. Посмотреть таблицу VLAN на всех портах моста — главная команда диагностики.**

```bash
bridge vlan show
```
```text
port    vlan-id
br0     1 PVID Egress Untagged
veth-buh    10 PVID Egress Untagged
veth-dmz    20 PVID Egress Untagged
```
Читается так: `veth-buh` — access-порт VLAN 10 (PVID 10, без тега на выходе). Если у порта нет нужного `vlan-id` — трафик молча упадёт в `/dev/null`. Это команда №1 при «не туда воткнули порт».

**4. Назначить порт в VLAN и убрать дефолтный VLAN 1.**

```bash
bridge vlan del dev veth-buh vid 1
bridge vlan add dev veth-buh vid 10 pvid untagged
```
`pvid` — какой VLAN вешать на нетегированный кадр, `untagged` — не тегировать на выходе (то есть access). Для транка: `bridge vlan add dev veth-buh vid 10,20` (без `untagged` — значит тегировать).

**5. Поднять тегированный саб-интерфейс (router-on-a-stick).**

```bash
bridge vlan add dev br0 vid 10 self
ip link add link br0 name br0.10 type vlan id 10
ip addr add 10.10.10.203/24 dev br0.10
ip link set br0.10 up
```
`self` — добавить VLAN на сам мост (SVI). Без `self` саб-интерфейс не получит ни одного кадра, хотя `ip a` будет показывать адрес. Проверка: `ip -d link show br0.10` → `vlan id 10 protocol 802.1Q`.

**6. Посмотреть состояния портов STP.**

```bash
bridge link show
```
```text
4: veth-buh  master br0 state forwarding priority 32 cost 2
5: veth-l1   master br0 state blocking  priority 32 cost 2
```
`state blocking` на одном из двух параллельных линков — это STP работает, а не поломка.

**7. Таблица MAC-адресов (FDB): где коммутатор видел каждый MAC.**

```bash
bridge fdb show br br0
```
```text
00:50:56:aa:bb:01 dev veth-buh master br0
33:33:00:00:00:01 dev br0 self permanent
```
Один MAC, «прыгающий» между двумя портами, — верный признак петли.

**8. Увидеть теги своими глазами.**

```bash
tcpdump -i br0 -e -n vlan
```
`-e` печатает заголовки L2, `vlan` — фильтр по тегированным кадрам. В выводе будет `vlan 10, p 0` — номер VLAN и приоритет. Незаменимо, когда «вроде настроено, а не работает».

**9. Cisco: посмотреть VLAN и порты.**

```text
switch# show vlan brief
VLAN Name        Status    Ports
1    default     active    Gi0/3, Gi0/4
10   OFFICE      active    Gi0/1
20   DMZ         active    Gi0/2
```
Если порт не в списке нужного VLAN — он в VLAN 1, и это ответ на 80 % вопросов «почему не пингуется».

**10. Cisco: настройка access и trunk.**

```text
switch(config)# vlan 10
switch(config-vlan)# name OFFICE
switch(config)# interface GigabitEthernet0/1
switch(config-if)# switchport mode access
switch(config-if)# switchport access vlan 10
switch(config-if)# spanning-tree portfast
switch(config-if)# spanning-tree bpduguard enable

switch(config)# interface GigabitEthernet0/24
switch(config-if)# switchport trunk encapsulation dot1q
switch(config-if)# switchport mode trunk
switch(config-if)# switchport trunk allowed vlan 10,20
switch(config-if)# switchport trunk native vlan 999
switch(config-if)# switchport nonegotiate
```
`encapsulation dot1q` есть только на моделях, поддерживающих ISL; на новых — не требуется. `nonegotiate` выключает DTP — про безопасность ниже.

**11. Cisco: проверка транка и STP.**

```text
switch# show interfaces trunk
Port     Mode  Encapsulation Status    Native vlan
Gi0/24   on    802.1q        trunking  999
Port     Vlans allowed on trunk
Gi0/24   10,20

switch# show spanning-tree vlan 10
VLAN0010
  Spanning tree enabled protocol rstp
  Root ID  Priority 24586
           Address  0011.2222.3333
           This bridge is the root
```
Приоритет 24586 = 24576 + 10 — значит, root задан вручную (`spanning-tree vlan 10 root primary`), это правильно.

**12. MikroTik (RouterOS 7): VLAN-фильтрация на бридже.**

```text
/interface bridge add name=br-lan vlan-filtering=yes protocol-mode=rstp
/interface bridge port add bridge=br-lan interface=ether1 pvid=10
/interface bridge port add bridge=br-lan interface=ether24 frame-types=admit-only-vlan-tagged
/interface bridge vlan add bridge=br-lan vlan-ids=10,20 tagged=br-lan,ether24 untagged=ether1
```
`frame-types=admit-only-vlan-tagged` делает порт чистым транком. Точные имена параметров в твоей версии RouterOS сверь в WinBox `[ПРОВЕРИТЬ]`.

В GUI: Hyper-V — «Диспетчер виртуальных коммутаторов» → VLAN ID; ESXi — порт-группа → VLAN ID; MikroTik WinBox — Bridge → VLAN.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужна **GW01** (Debian 12, два адаптера: 10.10.10.3 в VMnet8 и 10.10.20.1 в VMnet10) — она появилась в уроке 30. Если её нет, сначала пройди урок 30.

Важный честный момент: **VMware Workstation не умеет 802.1Q-тегирование на виртуальных сетях** (в отличие от ESXi, где VLAN ID задаётся на порт-группе) `[ПРОВЕРИТЬ под свою версию Workstation]`. Поэтому «коммутатор» мы соберём программно **внутри GW01**: мост Linux с VLAN-фильтрацией плюс network namespaces в роли хостов. Это настоящий 802.1Q, настоящий access/trunk, настоящий STP — просто внутри одной ВМ. Реальные Cisco-коммутаторы будут в уроке 32.

Доступ к GW01 по SSH или консоли VMware, пользователь с sudo.

### Снапшот

В VMware Workstation на ВМ **GW01**: правый клик по ВМ → Snapshot → Take Snapshot, имя `before-l2-031`. Остальные ВМ не трогаем, но по правилу стенда снимок делается на всех затрагиваемых — здесь затрагивается только GW01.

### Шаги

1. **GW01, консоль.** Проверь руки: `ip -br link show` и `ip -br addr show`. Ожидаемо: два физических интерфейса (имена вида `ens33`/`ens34` — у тебя могут отличаться) с адресами 10.10.10.3 и 10.10.20.1.
2. **GW01.** Поставь инструменты: `apt update && apt install -y tcpdump vlan bridge-utils` и загрузи модуль: `modprobe 8021q`. Ожидаемо: без ошибок, `lsmod | grep 8021q` показывает модуль.
3. **GW01.** Создай мост-«коммутатор»: `ip link add br0 type bridge vlan_filtering 1 stp_state 1 && ip link set br0 up`. Затем `bridge vlan show` — ожидаемо одна строка: `br0  1 PVID Egress Untagged`.
4. **GW01.** Создай два «хоста»-неймспейса и подключи их к мосту: `ip netns add ns-buh`, `ip netns add ns-dmz`, `ip link add veth-buh type veth peer name veth-buh-ns`, `ip link add veth-dmz type veth peer name veth-dmz-ns`, `ip link set veth-buh master br0`, `ip link set veth-dmz master br0`, `ip link set veth-buh up`, `ip link set veth-dmz up`. Ожидаемо: `bridge link show` показывает два порта в состоянии `forwarding`.
5. **GW01.** Сначала оба порта — в VLAN 10 (одна «комната»): `bridge vlan del dev veth-buh vid 1`, `bridge vlan del dev veth-dmz vid 1`, `bridge vlan add dev veth-buh vid 10 pvid untagged`, `bridge vlan add dev veth-dmz vid 10 pvid untagged`. Перенеси концы в неймспейсы и настрой адреса:
   ```bash
   ip link set veth-buh-ns netns ns-buh
   ip link set veth-dmz-ns netns ns-dmz
   ip netns exec ns-buh ip link set lo up
   ip netns exec ns-buh ip link set veth-buh-ns name eth0
   ip netns exec ns-buh ip addr add 10.10.10.201/24 dev eth0
   ip netns exec ns-buh ip link set eth0 up
   ip netns exec ns-dmz ip link set lo up
   ip netns exec ns-dmz ip link set veth-dmz-ns name eth0
   ip netns exec ns-dmz ip addr add 10.10.10.202/24 dev eth0
   ip netns exec ns-dmz ip link set eth0 up
   ```
   Адреса взяты из свободной части 10.10.10.0/24 — вне пула DHCP 100–150 и вне адресов ВМ стенда. Это временные тестовые интерфейсы внутри GW01, наружу они не видны.
6. **GW01.** Проверь связность: `ip netns exec ns-buh ping -c2 10.10.10.202`. Ожидаемо: 2 пакета, 0 % потерь — оба «в одном VLAN».
7. **GW01.** Теперь переведи `ns-dmz` в VLAN 20 и в подсеть DMZ:
   ```bash
   bridge vlan del dev veth-dmz vid 10
   bridge vlan add dev veth-dmz vid 20 pvid untagged
   ip netns exec ns-dmz ip addr flush dev eth0
   ip netns exec ns-dmz ip addr add 10.10.20.201/24 dev eth0
   ```
   Проверь: `ip netns exec ns-buh ping -c2 10.10.20.201` — ожидаемо **не проходит** (нет маршрута / таймаут). Это не поломка, это VLAN: один кабель, но разные сегменты. Убедись: `bridge vlan show` показывает `veth-dmz 20 PVID Egress Untagged`, а `tcpdump -i br0 -e -n vlan` при пинге показывает кадры с `vlan 20` — тег ставится, но в VLAN 10 их никто не ждёт.
8. **GW01.** Включи маршрутизацию между VLAN (router-on-a-stick):
   ```bash
   sysctl -w net.ipv4.ip_forward=1
   bridge vlan add dev br0 vid 10 self
   bridge vlan add dev br0 vid 20 self
   ip link add link br0 name br0.10 type vlan id 10
   ip link add link br0 name br0.20 type vlan id 20
   ip addr add 10.10.10.203/24 dev br0.10
   ip addr add 10.10.20.253/24 dev br0.20
   ip link set br0.10 up && ip link set br0.20 up
   ip netns exec ns-buh ip route add default via 10.10.10.203
   ip netns exec ns-dmz ip route add default via 10.10.20.253
   ```
   Ожидаемо: `ip netns exec ns-buh ping -c2 10.10.20.201` теперь проходит. Смотрим `ip route get 10.10.20.201` — маршрут через `br0.20`.
9. **GW01.** Собери петлю для STP: два моста и два параллельных линка.
   ```bash
   ip link add brs1 type bridge stp_state 1 && ip link set brs1 up
   ip link add brs2 type bridge stp_state 1 && ip link set brs2 up
   ip link add vs1 type veth peer name vs1b
   ip link add vs2 type veth peer name vs2b
   ip link set vs1 master brs1 && ip link set vs1b master brs2
   ip link set vs2 master brs1 && ip link set vs2b master brs2
   ip link set vs1 up; ip link set vs1b up; ip link set vs2 up; ip link set vs2b up
   bridge link show
   ```
   Ожидаемо: три порта в `forwarding`, а один (`vs2` или `vs1b`) — в `blocking`. Это STP погасил петлю. Запиши, какой именно.
10. **GW01.** Убедись, что дерево устойчиво: `bridge fdb show br brs1` — один и тот же MAC не должен «прыгать» между портами. Затем верни `ns-buh` в рабочее состояние и проверь ещё раз `ping 10.10.20.201`.
11. **Хост + stand-log.md.** Внеси запись: «GW01 получила мост br0 (vlan_filtering, STP), неймспейсы ns-buh/ns-dmz, адреса .201/.202/.203 в 10.10.10.0/24 и .201/.253 в 10.10.20.0/24, включён ip_forward, мосты brs1/brs2 для демо STP».

### Проверка

- [ ] `ip -d link show br0` содержит `vlan_filtering 1` и `stp_state 1`.
- [ ] `bridge vlan show` показывает `veth-buh 10 PVID Egress Untagged` и `veth-dmz 20 PVID Egress Untagged`.
- [ ] `ping` из `ns-buh` в `10.10.20.201` до шага 8 не проходит, после шага 8 — проходит.
- [ ] `tcpdump -i br0 -e -n vlan` показывает тег `vlan 20` на кадрах из `ns-dmz`.
- [ ] `bridge link show` в конфигурации brs1/brs2 содержит ровно один порт в `blocking`.
- [ ] `sysctl net.ipv4.ip_forward` = 1.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Пользователь:** Иванов. **ВМ:** WIN11-CL01. **Что написал:** «Не открывается наш веб-сервер `http://10.10.20.10` — таймаут. До шлюза 10.10.10.3 пинг идёт, до DMZ — нет. У Петровой то же самое. Срочность: высокая». 

**Как воспроизвести (GW01, root):** убери VLAN 20 с самого моста, как будто её забыли в allowed-списке транка:

```bash
bridge vlan del dev br0 vid 20 self
```

**Диагностика по порядку:**

1. С уровня «пользователя»: `ip netns exec ns-buh ping -c2 10.10.10.203` — проходит (шлюз/локальный VLAN живой). `ip netns exec ns-buh ping -c2 10.10.20.201` — таймаут. Симптом воспроизведён, проблема на стыке L2/L3.
2. Проверяем, что маршрутизация вообще есть: `sysctl net.ipv4.ip_forward` → 1, `ip route get 10.10.20.201` → маршрут через `br0.20`. Значит, L3 настроен.
3. Смотрим адреса: `ip -br addr show br0.20` → адрес 10.10.20.253 на месте. Значит, «интерфейс не поднят» отпадает.
4. Смотрим VLAN-таблицу моста: `bridge vlan show` → у `br0` в списке только `1` и `10`, **VLAN 20 отсутствует**. Вот она. Саб-интерфейс `br0.20` существует, но кадры этого VLAN до него не доходят — мост их не пропускает к собственному порту.
5. Подтверждаем трафиком: на `ns-dmz` запусти `ping -c100 10.10.20.253`, а на GW01 — `tcpdump -i br0 -e -n vlan`. Видим кадры `vlan 20` от `ns-dmz`, но ответа нет — тег входит в мост и умирает.
6. Фиксим: `bridge vlan add dev br0 vid 20 self`. Проверяем: `ip netns exec ns-buh ping -c2 10.10.20.201` → проходит.

**Ответ пользователю:** «Иван, добрый день. Проблема была на маршрутизаторе между офисной сетью и DMZ: VLAN веб-сегмента не был разрешён на транке, трафик до него не доходил. Исправил, веб-сервер открывается. Проверьте, пожалуйста, и напишите, если ещё что-то не работает».

**Закрытие заявки:** категория «Сеть / конфигурация L2», решение — «добавлен VLAN 20 в список разрешённых на транке br0». В постмортем записать: изменение конфигурации транка делать через ревью и с проверкой `bridge vlan show` до/после.

### Что изменилось на стенде

- **GW01:** добавлены мосты `br0` (vlan_filtering + STP), `brs1`, `brs2`; саб-интерфейсы `br0.10` (10.10.10.203/24) и `br0.20` (10.10.20.253/24); включён `net.ipv4.ip_forward=1` (для GW01 это штатно, она маршрутизатор); добавлены network namespaces `ns-buh` (10.10.10.201/24) и `ns-dmz` (10.10.20.201/24); установлены пакеты `tcpdump`, `vlan`, `bridge-utils`.
- **Остальные ВМ не тронуты.** VLAN-план компании зафиксирован решением: VLAN 10 — офис 10.10.10.0/24, VLAN 20 — DMZ 10.10.20.0/24, VLAN 999 — служебная как native/blackhole.
- Важно: неймспейсы и мосты **не сохраняются после перезагрузки GW01**. Для следующих глав сохрани команды в `/root/vlan-lab.sh` и запускай при необходимости — либо считай лабораторию одноразовой.

### Откат

VMware Workstation → правый клик по **GW01** → Snapshot Manager → выбери `before-l2-031` → **Revert**. Если хочешь откатить только часть, вручную:

```bash
ip netns del ns-buh; ip netns del ns-dmz
ip link del br0; ip link del brs1; ip link del brs2
sysctl -w net.ipv4.ip_forward=0
```

Затем удали соответствующую запись из `stand-log.md` и отметь, что состояние откатано.

## 6. Ошибки новичков и безопасность

1. **Пользовательские порты оставлены в VLAN 1.** → Любой, кто воткнул кабель, попадает в «дефолтную» сеть, часто с управлением. → Все порты явно назначаются в рабочий VLAN, VLAN 1 не используется для доступа, неиспользуемые порты уводятся в blackhole-VLAN и гасятся `shutdown`.
2. **Trunk «на всякий случай» со всеми VLAN.** → Широковещательные штормы и утечка сегментов, лишняя нагрузка. → Всегда `switchport trunk allowed vlan ...` с точным списком и проверка `show interfaces trunk`.
3. **Native VLAN не совпадает на концах транка.** → Нетегированный трафик уезжает в чужой сегмент, CDP ругается, а пользователи видят «частичную связность». → Native VLAN одинаковая с двух сторон, отдельная служебная (999), не VLAN 1.
4. **STP не настроен на корень вручную.** → Корнем становится случайный коммутатор в дальней комнате, топология перестраивается непредсказуемо, часть трафика идёт через медленный линк. → Явный `spanning-tree vlan X root primary` на ядре и `root secondary` на резерве.
5. **PortFast включён на порту в сторону коммутатора.** → В сеть попадают чужие BPDU, дерево перестраивается, сети «моргают». → PortFast только на access-портах до конечных устройств, всегда вместе с `bpduguard`.
6. **Двойное тегирование и VLAN hopping.** → Атакующий с access-порта шлёт кадры с двумя тегами и выбирается в чужой VLAN. → Native VLAN не равна VLAN пользователей, DTP выключен (`switchport nonegotiate`), неиспользуемые порты погашены.
7. **Диагностика «на глаз» по лампочкам.** → Часами ищут проблему в сервере, когда дело в порту. → Всегда `bridge vlan show` / `show interfaces trunk` / `show vlan brief` в первую очередь, потом уже сервер.
8. **Настройка транка без окна и без бэкапа конфига.** → Одна ошибка в allowed-списке — и филиал без сети. → Конфиг сохранён (`copy run start`), есть rollback-план, изменения в окне.

**Безопасность.** В учебной лабе мы спокойно делаем `sysctl -w net.ipv4.ip_forward=1`, создаём VLAN-ы и не ограничиваем трафик между сегментами — на стенде это допустимо. В боевой среде так нельзя: сегментация нужна именно для **изоляции** (бухгалтерия с 1С и персональными данными не должна видеться со складскими терминалами и гостями), поэтому между VLAN обязан стоять межсетевой экран с правилами «по умолчанию запрещено», а не просто маршрутизация. Управляющие интерфейсы коммутаторов (SVI, SSH) выносятся в отдельный management VLAN и закрываются ACL — иначе компрометация одного рабочего места даёт доступ к управлению всей сетью. Никогда не оставляй `telnet`, `http` и дефолтные пароли на коммутаторе. И пароли на стенде у нас учебные общие — в реальной работе так делать нельзя.

## 7. Вопросы на собеседовании

**1. Чем access-порт отличается от trunk, и что такое native VLAN?**

Ответ: access-порт принадлежит одному VLAN, кадры хосту уходят без тега, входящие нетегированные получают PVID этого порта. Trunk несёт несколько VLAN, каждый кадр помечен тегом 802.1Q, кроме трафика native VLAN — он идёт нетегированным для совместимости с устройствами, не понимающими теги. Native VLAN по умолчанию VLAN 1; её меняют на служебную и следят, чтобы она совпадала на обоих концах транка. **Что хочет услышать интервьюер:** что ты понимаешь: тег ставится только на транке, а native — это дыра совместимости, а не «главный VLAN». **Красный флаг:** «native VLAN — это VLAN управления» или «на access-порту тоже есть тег».

**2. Что произойдёт, если на одном конце транка native VLAN 1, а на другом — 999?**

Ответ: нетегированные кадры с одной стороны попадут в VLAN 999, с другой — в VLAN 1. Трафик native VLAN просто не пройдёт между сегментами, а часть широковещательного трафика утечёт в чужой VLAN. Cisco при этом обычно выводит предупреждение о mismatch в CDP-логах, но не всегда. Диагностика: сравнить `show interfaces trunk` с обеих сторон, посмотреть native vlan в выводе. Лечение: привести native VLAN к одной и той же служебной с обеих сторон, а лучше вообще не гонять пользовательский трафик в native. **Что хочет услышать интервьюер:** понимание, что тег — единственный признак принадлежности, и что при его отсутствии стороны договариваются «по умолчанию». **Красный флаг:** «ничего не будет, коммутаторы сами договорятся».

**3. Как STP выбирает корневой мост и как гарантировать, что корнем будет ядро?**

Ответ: по наименьшему Bridge ID — приоритет (кратен 4096, по умолчанию 32768) плюс MAC-адрес. Побеждает меньший. Чтобы корнем было ядро, на нём задают `spanning-tree vlan 10 root primary` (приоритет 24576) и на резервном — `root secondary` (28672); либо руками выставляют приоритеты с шагом, оставляя место для будущих коммутаторов. Проверка — `show spanning-tree vlan 10`, поле Root ID и строка `This bridge is the root`. **Что хочет услышать интервьюер:** что приоритет настраивается явно, а не «пусть будет как получится», и что ты знаешь команду проверки. **Красный флаг:** «корень — это тот, кто первым включился» (это миф) или «STP выбирает по скорости линка».

**4. Пользователь переехал на другое рабочее место — нет доступа к сетевой папке на FS01. Твои действия?**

Ответ: сначала локализую — пинг до шлюза, до FS01 (`ping`, `tracert`), проверка IP/маски/шлюза через `ipconfig /all`, потом `nslookup FS01.corp.vektor.lab`. Если пинг до шлюза есть, а до FS01 нет — иду на коммутатор: `show vlan brief` (порт в нужном VLAN?), `show interfaces status` (не err-disabled ли порт, не up/down ли), `show interfaces trunk` (VLAN разрешён на транке до FS01?), `show mac address-table | include <MAC>`. Если пинг идёт, а папка не открывается — это уже права, группы AD и шары, а не сеть. **Что хочет услышать интервьюер:** чёткий порядок «от клиента к сети, от L2 к L3 к приложению», а не «сразу полез на сервер». **Красный флаг:** «перезагрузил бы коммутатор» или «сразу перевыдал бы права».

**5. Зачем нужны PortFast и BPDU Guard и что будет при ошибке?**

Ответ: PortFast переводит access-порт сразу в forwarding, минуя 30 секунд прослушивания — иначе компьютер после включения полминуты не получает DHCP. BPDU Guard гасит порт (`err-disabled`), если на нём появились BPDU, — это защита от того, что сотрудник воткнёт в розетку домашний коммутатор и устроит петлю. Если PortFast включить на порту в сторону другого коммутатора — тот начнёт получать BPDU от «слишком быстрого» порта, топология будет постоянно перестраиваться, и сеть начнёт «моргать». Снимается `shutdown`/`no shutdown`, но лучше сразу включить BPDU Guard, чтобы порт сам гас. **Что хочет услышать интервьюер:** что PortFast не «ускоряет STP вообще», а применяется строго на границе сети. **Красный флаг:** «включил PortFast везде, так быстрее».

## 8. Источники (официальная документация)

- **cisco.com**, Cisco Catalyst Configuration Guide — разделы *Configuring VLANs*, *Configuring Trunking*, *Configuring Spanning Tree Protocol*. Поиск: `cisco catalyst configure vlan trunk configuration guide`.
- **cisco.com**, *Spanning Tree Protocol (STP) overview / Understanding PVST+ and RSTP*. Поиск: `cisco spanning tree protocol explained rstp pvst`.
- **docs.kernel.org**, Networking — *Linux Bridge*, *bridge command reference* (`bridge vlan`, `bridge link`, `bridge fdb`). Поиск: `linux bridge vlan filtering kernel documentation`.
- **wiki.linuxfoundation.org / netdev**, материалы по `iproute2` и VLAN-интерфейсам (`ip link add type vlan`). Поиск: `iproute2 vlan interface documentation`.
- **help.mikrotik.com**, *Bridge VLAN Filtering* в RouterOS. Поиск: `mikrotik bridge vlan filtering example`.
- **ieee802.org**, стандарт IEEE 802.1Q (тегирование VLAN) и 802.1D/802.1w (STP/RSTP). Поиск: `ieee 802.1q standard vlan tagging`.
- **learn.microsoft.com**, *Hyper-V Virtual Switch* — раздел про VLAN ID на виртуальных адаптерах. Поиск: `hyper-v virtual switch vlan id`. `[ПРОВЕРИТЬ название конкретной статьи]`
- Отдельно про VMware: официальная документация VMware Workstation по виртуальным сетям — вопрос поддержки 802.1Q в Workstation проверь в актуальной версии. Поиск: `vmware workstation virtual network vlan tagging support`. `[ПРОВЕРИТЬ]`
