# Работа с оборудованием: Cisco, MikroTik, Huawei, Eltex и российские вендоры — базовая настройка

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку, 9:12 утра:

> **Пользователь:** Сидоров (склад) / **ВМ:** — / **Что написал:** «Нам на склад поставили новый коммутатор, коробку распаковали, включили — не работает ничего. Сканеры штрихкодов в сеть не видят, отгрузка встала. Срочность: высокая».

Дальше вы приезжаете (или идёте в серверную), находите коробку с устройством Eltex, на котором из коробки нет ни адреса, ни VLAN, ни пароля на SSH. И это не экзотика — так выглядит примерно каждый второй проект: пришло оборудование, его надо ввести в эксплуатацию, задокументировать и дальше поддерживать.

Почему тема встречается постоянно: в любой компании больше пятидесяти человек рано или поздно появляются управляемые коммутаторы, роутер на входе, точки доступа с контроллером. Сисадмин — это не «человек, который только Windows ставит», а первый, к кому бегут, когда «пропала сеть». Даже если у вас есть отдельный сетевой инженер, вам нужно уметь подключиться по консоли, прочитать конфиг, понять, что порт в `shutdown`, и не сломать при этом прод.

После этой главы вы будете: подключаться к устройству любым из четырёх способов (консоль, SSH, веб, API), ориентироваться в CLI четырёх вендорских семейств, делать базовую настройку по единому чек-листу, снимать и версионировать конфиги, обновлять прошивку без риска и объяснять заказчику, почему «просто нажми кнопку» не работает.

## 2. Теория «под капотом»

Сетевое устройство — это компьютер. У него есть процессор, память, флеш, загрузчик и операционная система (IOS, VRP, RouterOS, Eltex CLI поверх Linux). Отличие от сервера одно: большая часть трафика проходит мимо процессора, через коммутационный чип (ASIC).

Внутри устройства принято выделять три плоскости:

- **Data plane (плоскость данных)** — то, что реально двигает пакеты. Живёт в ASIC, работает на скорости портов.
- **Control plane (плоскость контроля)** — то, что строит таблицы: STP, LLDP, OSPF/BGP, ARP, MAC-learning. Работает на CPU.
- **Management plane (плоскость управления)** — то, через что входите вы: консоль, SSH, веб, SNMP. Именно её мы настраиваем и именно её защищаем.

```text
┌─────────────────── УСТРОЙСТВО (коммутатор/роутер) ───────────────────┐
│ MANAGEMENT PLANE  — сюда входим мы                                   │
│   Console (RS-232/USB) │ SSH 22 │ Telnet 23 │ HTTPS 443              │
│   SNMP 161/162 │ NETCONF 830 │ RESTCONF 443 │ API 8728/8729 │ 8291   │
├──────────────────────────────────────────────────────────────────────┤
│ CONTROL PLANE — CPU: STP/RSTP, LLDP/CDP/MNDP, OSPF/BGP, ARP, DHCP    │
├──────────────────────────────────────────────────────────────────────┤
│ DATA PLANE — ASIC: MAC-таблица, VLAN, ACL в кремнии, forwarding      │
├──────────────────────────────────────────────────────────────────────┤
│ ЖЕЛЕЗО: порты RJ-45/SFP, БП, вентиляторы, POST → bootloader → ОС     │
└──────────────────────────────────────────────────────────────────────┘
```

**Как устройство загружается.** Питание → POST (самопроверка) → загрузчик (Cisco: ROMMON; MikroTik: RouterBOOT; Huawei: BootROM; Eltex: U-Boot) → загрузка образа ОС с флеша → применение **стартового конфига** → устройство в работе.

**Ключевое понятие: running-config vs startup-config.** У Cisco и большинства вендоров конфигурация существует в двух копиях: рабочая (в оперативной памяти, `running-config`) и сохранённая (во флеше/NVRAM, `startup-config`). Изменения применяются сразу, но **не сохраняются** до явной команды. Ребут без сохранения = потеря всей работы. У MikroTik RouterOS подход другой: изменения сразу пишутся в конфигурационную базу, отдельного «save» нет, зато есть `/export` (текстовый дамп) и `/system backup` (бинарный бэкап).

**Три способа попасть внутрь и когда какой.** Консоль (out-of-band, OOB) — работает всегда, даже когда сеть мертва; нужен физический доступ и кабель. SSH — основной рабочий путь. Веб-интерфейс — удобен для быстрого старта у MikroTik (Winbox/WebFig) и Eltex, но плохо автоматизируется. Плюс API/NETCONF — это уже автоматизация, к ней мы вернёмся в треке DevOps.

**Где это в OSI.** Порты, кабели, SFP — L1. MAC-таблицы, VLAN, STP — L2. IP-адресация, маршруты, ACL — L3. Базовая настройка почти всегда крутится вокруг L2–L3 плюс management plane.

**Производители и реалии РФ.** Cisco официально прекратил поставки и поддержку в РФ с 2022 года: оборудование работает, но обновлений безопасности и гарантии нет, а «серый» импорт — это риск неустранимых CVE. Huawei продаётся, но тоже под ограничениями. Практичная замена в корпоративной сети: **Eltex** (Новосибирск, входит в реестр российской радиоэлектронной продукции [ПРОВЕРИТЬ]), **Qtech**, **Zelax**, **NAG**, **TFortis** (индустриальные). MikroTik (Латвия) массово используется в SMB — дешёвый, но требует аккуратности с безопасностью и доступностью поставок [ПРОВЕРИТЬ]. Для госзакупок наличие в реестре Минпромторга (ПП №878 [ПРОВЕРИТЬ номер и год]) часто важнее характеристик.

## 3. Аналогия из жизни

Настройка коммутатора — это как заселение в новую квартиру в свежем доме. Сначала вам нужен ключ от подъезда (**консольный кабель**) — без него вы стоите на улице, даже если квартира ваша. Внутри вы ставите табличку с номером (**hostname**), прописываете адрес в паспортном столе (**IP-адрес на management-интерфейсе**), решаете, какие комнаты кому сдавать (**VLAN**), и вешаете замок с нормальным ключом вместо заводского «admin/admin» (**SSH-ключи, AAA**). Потом обязательно делаете вторую копию ключей и опись имущества (**бэкап конфига и инвентаризация**) — иначе после смены замка вы окажетесь на улице.

Аналогия перестаёт работать в одном месте: у квартиры один хозяин и одна дверь, а у сетевого устройства одновременно живут три плоскости (данные, контроль, управление), и проблема в плоскости данных может выглядеть как проблема консоли.

## 4. Практика и команды

Ниже — набор, который закрывает 90% первых задач на любом вендоре. Синтаксис Eltex ESR/MES в этой главе местами помечен `[ПРОВЕРИТЬ]` — сверяйтесь с руководством на конкретную модель, линейки различаются.

### 4.1. Подключение по консоли

```bash
# Linux: сначала находим адаптер USB-UART
sudo dmesg | grep -i ttyUSB          # появится /dev/ttyUSB0
sudo picocom -b 9600 -d 8 -p n -s 1 /dev/ttyUSB0
# альтернатива
sudo screen /dev/ttyUSB0 9600
```

Разбор ключей `picocom`: `-b` скорость (бод), `-d` биты данных, `-p n` чётность (none), `-s 1` стоп-бит. Выход: `Ctrl+A`, затем `Ctrl+X` (в `screen` — `Ctrl+A`, затем `K`). В Windows: PuTTY → Connection type **Serial** → Speed 9600 → COM-порт из Диспетчера устройств.

Скорости по умолчанию: Cisco и Huawei — **9600 8N1**, MikroTik RouterBOARD — **115200 8N1**, Eltex — **115200** [ПРОВЕРИТЬ по модели]. Ожидаемый результат: приглашение вида `Router>`, `<Huawei>`, `[admin@MikroTik] >`. Если видите «мусор» — почти наверняка не та скорость; если пусто — не тот кабель (нужен нуль-модемный/rollover, не прямой patch).

### 4.2. Идентификация устройства

```text
Cisco#   show version
Cisco#   show inventory
Huawei>  display version
[admin@MikroTik] > /system resource print
```

Смотрим: модель, серийный номер, версия ОС, uptime, объём flash, лицензия. Это первое, что вы записываете в инвентаризацию.

### 4.3. Кто рядом в сети (обнаружение соседей)

```text
Cisco#   show cdp neighbors detail
Cisco#   show lldp neighbors
Huawei>  display lldp neighbor brief
[admin@MikroTik] > /ip neighbor print
```

`CDP` — проприетарный протокол Cisco, `LLDP` — открытый стандарт, `MNDP` — MikroTik Discovery Protocol. Вывод показывает, к какому порту подключено какое устройство. Это самый быстрый способ понять топологию, не бегая с фонариком по серверной.

### 4.4. Базовая настройка Cisco IOS (коммутатор Catalyst)

```text
enable
configure terminal
hostname SW-ACC-01
ip domain-name corp.vektor.lab
username adm.student privilege 15 secret <СЛОЖНЫЙ_ПАРОЛЬ>
crypto key generate rsa modulus 2048
ip ssh version 2
!
interface GigabitEthernet0/1
 description UPLINK
 switchport mode access
 switchport access vlan 10
 no shutdown
!
interface Vlan10
 ip address 192.168.42.10 255.255.255.0
 no shutdown
ip default-gateway 192.168.42.1
ntp server 10.10.10.10
logging host 10.10.10.40
!
line vty 0 4
 transport input ssh
 login local
end
copy running-config startup-config
```

Ключевое: `crypto key generate rsa` создаёт ключ для SSH (без него SSH не поднимется), `transport input ssh` выключает Telnet на виртуальных терминалах, `no shutdown` поднимает интерфейс (по умолчанию порты в `shutdown`). `copy running-config startup-config` — то самое сохранение, которое забывают.

### 4.5. Базовая настройка Huawei VRP

```text
<Huawei> system-view
[Huawei] sysname SW-ACC-02
[SW-ACC-02] interface GigabitEthernet0/0/1
[SW-ACC-02-GigabitEthernet0/0/1] port link-type access
[SW-ACC-02-GigabitEthernet0/0/1] port default vlan 10
[SW-ACC-02-GigabitEthernet0/0/1] undo shutdown
[SW-ACC-02-GigabitEthernet0/0/1] quit
[SW-ACC-02] interface Vlanif 10
[SW-ACC-02-Vlanif10] ip address 192.168.42.11 24
[SW-ACC-02-Vlanif10] quit
[SW-ACC-02] ip route-static 0.0.0.0 0 192.168.42.1
[SW-ACC-02] aaa
[SW-ACC-02-aaa] local-user adm.student password irreversible-cipher <СЛОЖНЫЙ_ПАРОЛЬ>
[SW-ACC-02-aaa] local-user adm.student privilege level 15
[SW-ACC-02-aaa] local-user adm.student service-type ssh
[SW-ACC-02-aaa] quit
[SW-ACC-02] stelnet server enable
[SW-ACC-02] ssh user adm.student authentication-type password
[SW-ACC-02] user-interface vty 0 4
[SW-ACC-02-ui-vty0-4] authentication-mode aaa
[SW-ACC-02-ui-vty0-4] protocol inbound ssh
[SW-ACC-02-ui-vty0-4] quit
[SW-ACC-02] save
```

Обратите внимание: `undo shutdown` вместо `no shutdown`, `save` с подтверждением (`y`), а сохранённый конфиг лежит файлом `vrpcfg.zip` на flash.

### 4.6. Базовая настройка MikroTik RouterOS

```text
/system identity set name=CHR01
/ip address add address=192.168.42.2/24 interface=ether1
/ip route add dst-address=0.0.0.0/0 gateway=192.168.42.1
/ip service disable telnet,ftp,www,api
/user add name=adm.student group=full password=<СЛОЖНЫЙ_ПАРОЛЬ>
/user disable admin
/system ntp client set enabled=yes servers=10.10.10.10
/system logging action set 1 target=remote remote=10.10.10.40
/system logging add topics=info action=1
/export file=chr01-config
/system backup save name=chr01-backup
```

Особенность: `/export` даёт читаемый текстовый конфиг (его удобно хранить в git), `/system backup` — бинарник, восстанавливается только на ту же модель/версию. Перед рискованными изменениями включайте **safe mode**: `Ctrl+X` — если сессия оборвётся, RouterOS откатит изменения автоматически. Это спасло больше устройств, чем любой бэкап.

### 4.7. Eltex ESR/MES (Cisco-подобный CLI)

```text
configure terminal
hostname ESR01
interface gigabitethernet 1/0/1
 ip address 192.168.42.12 255.255.255.0
 no shutdown
exit
ip route 0.0.0.0 0.0.0.0 192.168.42.1
username adm.student password <СЛОЖНЫЙ_ПАРОЛЬ> privilege 15
copy running-config startup-config
```
Синтаксис и точные имена интерфейсов `[ПРОВЕРИТЬ]` в руководстве на конкретную модель ESR/MES — у разных линеек Eltex отличается.

### 4.8. Таблица-переводчик между вендорами

| Задача | Cisco IOS | Huawei VRP | MikroTik | Eltex |
|---|---|---|---|---|
| Войти в конфиг | `configure terminal` | `system-view` | сразу | `configure terminal` |
| Имя | `hostname` | `sysname` | `/system identity set name=` | `hostname` |
| Порт | `Gi0/1` | `GE0/0/1` | `ether1` | `gi1/0/1` [ПРОВЕРИТЬ] |
| Включить порт | `no shutdown` | `undo shutdown` | включён по умолчанию | `no shutdown` |
| Показать конфиг | `show running-config` | `display current-configuration` | `/export` | `show running-config` |
| Сохранить | `copy run start` | `save` | автосохранение | `copy run start` [ПРОВЕРИТЬ] |
| Маршрут | `ip route 0.0.0.0 0.0.0.0 GW` | `ip route-static 0.0.0.0 0 GW` | `/ip route add gateway=GW` | `ip route ...` |

Держите эту таблицу под рукой: 80% работы — это перевести знакомую операцию на язык очередного вендора.

### 4.9. Массовый бэкап конфигов с UBU01

```bash
#!/usr/bin/env bash
# /home/adm/backup-netcfg.sh
DATE=$(date +%F); DEST=/srv/netconfig/$DATE; mkdir -p "$DEST"
for dev in 192.168.42.2 192.168.42.10 192.168.42.11; do
  ssh -o BatchMode=yes -o ConnectTimeout=5 adm.student@"$dev" \
      'show running-config' > "$DEST/$dev.cfg" 2>&1 \
      || echo "FAIL $dev" >> "$DEST/errors.log"
done
```
Для MikroTik команда в аргументе SSH — `/export` `[ПРОВЕРИТЬ]` в вашей версии RouterOS. Дальше `diff` между папками покажет, кто и что менял.

### 4.10. Единое время и логи

```text
Cisco#  show ntp status
Cisco#  show clock detail
Huawei> display ntp-service status
[admin@MikroTik] > /system ntp client print
```
Без точного времени логи бесполезны, а SSH-ключи и сертификаты начинают «не работать» на ровном месте. Источник времени в стенде — DC01 (10.10.10.10).

### 4.11. SNMPv3 вместо «public»

```bash
# проверка с UBU01 (пакет snmp)
snmpwalk -v3 -l authPriv -u monitor -a SHA -A 'AuthPass#1' -x AES -X 'PrivPass#1' 192.168.42.2
```
SNMPv1/v2c с community `public` — классическая дыра: по нему читается вся инвентаризация, а иногда и пишется конфиг. Только v3 с auth+priv.

### 4.12. Обновление прошивки

```text
[admin@MikroTik] > /system package update check-for-updates
[admin@MikroTik] > /system package update download
[admin@MikroTik] > /system reboot
```
Cisco: `copy tftp://10.10.10.30/image.bin flash:` → `boot system flash:image.bin` → `reload`. Huawei: загрузка через TFTP и `startup system-software`. Правило: сначала бэкап конфига, потом чтение release notes, потом окно обслуживания, потом проверка, потом план отката (в flash держим две версии).

### 4.13. Диагностика портов и MAC

```text
Cisco#  show interfaces status
Cisco#  show interfaces Gi0/1
Cisco#  show mac address-table | include Gi0/1
Huawei> display interface brief
Huawei> display mac-address
[admin@MikroTik] > /interface print stats
[admin@MikroTik] > /interface ethernet monitor ether1 once
```
Смотрим: `up/up` или `down`, ошибки (`CRC`, `runts` — обычно кабель), скорость/дуплекс, на каком порту виден MAC искомого устройства.

### 4.14. Восстановление доступа (только при физическом доступе)

Cisco: перезагрузка → `Ctrl+C` в течение первых секунд → ROMMON → `confreg 0x2142` (игнорировать стартовый конфиг) → загрузка → `copy start run` → смена пароля → `config-register 0x2102` → save. Huawei: `Ctrl+B` в BootROM. MikroTik: кнопка **reset** при включении (сброс конфига) либо Netinstall. **Это лабораторные процедуры**: на боевом устройстве они требуют согласованного окна и Physical Security.

## 5. Лабораторная работа

Тема требует вендорского оборудования, которого в стенде физически нет. Поэтому лабораторная строится в два слоя: **эталон** — реальный маршрутизатор стенда GW01 (Debian 12, 10.10.10.3 / 10.10.20.1), **эмуляция вендорских CLI** — ВМ на PVE01 в изолированном сегменте `192.168.42.0/24` (это новый лабораторный сегмент, он не пересекается с VMnet8/VMnet10 и существует только внутри PVE01). Так вы получаете настоящие RouterOS/VRP/IOS-подобные команды без покупки железа и без риска уронить стенд.

### Предусловия

- Стенд L2 развёрнут полностью: DC01 (10.10.10.10), GW01 (10.10.10.3 / 10.10.20.1), UBU01 (10.10.10.30), MON01 (10.10.10.40).
- **PVE01 (10.10.10.70)** развёрнут и доступен по SSH (глава 35). Нужно ~8 ГБ RAM и ~30 ГБ диска свободно.
- MON01 принимает syslog от устройств (глава 48 — если ещё не настроен, временно проверяйте логи локально).
- Учётка `adm.student`, пароль стенда `Lab#2026-vektor`.
- MikroTik CHR скачивается с официального сайта бесплатно; бесплатная лицензия CHR ограничивает скорость ~1 Мбит/с на интерфейс — для лабы достаточно `[ПРОВЕРИТЬ актуальные условия]`.
- Для второго вендора: либо реальный коммутатор Eltex/Huawei/Cisco под рукой, либо бесплатный **Cisco Packet Tracer** (легально, через Cisco Networking Academy / Skills for All `[ПРОВЕРИТЬ доступность из РФ]`), либо легальный образ вендора. Пиратские образы IOS/VRP не используем.

### Снапшот

В VMware Workstation на **PVE01, GW01, UBU01, MON01**: `VM → Snapshot → Take Snapshot`, имя `before-sysadmin-042`. Внутри PVE01 для будущей ВМ дополнительно: `qm snapshot 200 before-sysadmin-042` (после создания).

### Шаги

1. **PVE01.** Создать изолированный бридж:
   ```text
   # /etc/network/interfaces на PVE01
   auto vmbr42
   iface vmbr42 inet static
       address 192.168.42.1/24
       bridge-ports none
       bridge-stp off
       bridge-fd 0
   ```
   Применить: `ifreload -a` `[ПРОВЕРИТЬ — ifupdown2]`. Ожидаемо: `ip a show vmbr42` показывает 192.168.42.1/24, физические интерфейсы не затронуты.
2. **PVE01.** Скачать CHR (raw disk image) с mikrotik.com/download, распаковать. Создать и импортировать ВМ:
   ```bash
   qm create 200 --name CHR01 --memory 256 --cores 1 \
       --net0 virtio,bridge=vmbr42 --scsihw virtio-scsi-pci
   qm importdisk 200 chr-<версия>.img local-lvm
   qm set 200 --scsi0 local-lvm:vm-200-disk-0 --boot order=scsi0
   qm start 200
   ```
   Ожидаемо: ВМ запускается, `qm status 200` → `running`.
3. **PVE01.** Подключиться к консоли: `qm terminal 200`. Выйти — `Ctrl+O`. Ожидаемо: приглашение RouterOS и логин `admin` без пароля (сразу задать пароль!). Если консоль «немая» — проверьте, что смотрите именно serial-консоль ВМ, а не VNC.
4. **CHR01 (через консоль).** Сменить пароль администратора, создать свою учётку, задать имя и адрес:
   ```text
   /user set admin password=<СЛОЖНЫЙ_ПАРОЛЬ>
   /user add name=adm.student group=full password=Lab#2026-vektor
   /system identity set name=CHR01
   /ip address add address=192.168.42.2/24 interface=ether1
   /ip route add dst-address=0.0.0.0/0 gateway=192.168.42.1
   ```
   Ожидаемо: `/ip address print` — один адрес; `ping 192.168.42.1` отвечает.
5. **CHR01.** Закрыть небезопасные сервисы и включить логирование:
   ```text
   /ip service print
   /ip service disable telnet,ftp,www,api
   /system ntp client set enabled=yes servers=10.10.10.10
   /system logging action set 1 target=remote remote=10.10.10.40
   /system logging add topics=info action=1
   ```
   Ожидаемо: `/ip service print` показывает `ssh` разрешён, `telnet/www/api` — `no`.
6. **PVE01.** Проверить доступность по сети и отсутствие лишних портов:
   ```bash
   ping -c3 192.168.42.2
   ssh adm.student@192.168.42.2 '/system identity print'
   sudo apt install -y nmap && nmap -p22,23,80,443,8291 192.168.42.2
   ```
   Ожидаемо: открыт только 22.
7. **CHR01.** Снять конфиг и бэкап, забрать их на UBU01:
   ```text
   /export file=chr01-config
   /system backup save name=chr01-backup
   ```
   ```bash
   # на UBU01
   mkdir -p /srv/netconfig/lab042 && cd /srv/netconfig/lab042
   scp adm.student@192.168.42.2:/chr01-config.rsc .
   ```
   `[ПРОВЕРИТЬ]` работу SCP в вашей версии RouterOS; если не работает — `/tool fetch ... upload=yes` на TFTP/HTTP-сервер, поднятый на UBU01.
8. **Второй вендор (Packet Tracer / реальное устройство).** Выполнить «эквивалент»: hostname, management IP, только SSH, локальный пользователь с привилегией 15, NTP на 10.10.10.10, syslog на 10.10.10.40, сохранение конфига. Использовать команды из 4.4 (Cisco) или 4.5 (Huawei). Ожидаемо: устройство пингуется, SSH работает, Telnet закрыт.
9. **CHR01.** Обновление прошивки как процедура:
   ```text
   /system package update check-for-updates
   /system package update download
   /system reboot
   ```
   После ребута: `/system resource print` (новая версия), `/ip address print` (конфиг на месте), `ssh` работает. Если конфиг пропал — вы забыли, что RouterOS хранит его в базе; восстановление: `/system backup load name=chr01-backup`.
10. **UBU01 + stand-log.** Оформить инвентаризацию: файл `/srv/netconfig/network-inventory.md` (устройство, модель, версия ОС, mgmt-IP, серийник, порт подключения, кто отвечает, дата последнего бэкапа) и запись в `stand-log.md`.

### Проверка

- [ ] На PVE01 существует `vmbr42` с адресом 192.168.42.1/24, VMnet8/VMnet10 не изменились.
- [ ] CHR01 пингуется с PVE01 и доступен по SSH под `adm.student`.
- [ ] На CHR01 отключены telnet, ftp, www, api; `nmap` показывает только порт 22.
- [ ] Время на CHR01 совпадает с DC01 (± пара секунд).
- [ ] `/export` и `/system backup` созданы, копия лежит на UBU01 в `/srv/netconfig/lab042`.
- [ ] Второй вендор настроен по тому же чек-листу (имя, IP, SSH-only, NTP, syslog, save).
- [ ] `network-inventory.md` заполнен, запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** Сидоров (склад) / **ВМ:** — / **Что написал:** «После обеда перестал заходить на коммутатор по SSH — connection refused. Пингуется, а SSH нет. Нам туда лезть надо, у нас инвентаризация. Срочность: средняя».

**Как воспроизвести поломку (CHR01, через консоль `qm terminal 200`):**
```text
/ip firewall filter add chain=input protocol=tcp dst-port=22 action=drop place-before=0
/ip service disable ssh
```
(на учебном устройстве это безопасно: консоль остаётся).

**Диагностика по порядку:**
1. С PVE01: `ping -c3 192.168.42.2` — отвечает? Значит L3 жив, проблема в плоскости управления.
2. `nmap -p22 192.168.42.2` — порт закрыт/фильтруется. Фиксируем: «сеть есть, сервис нет».
3. Идём по консоли (`qm terminal 200`) — это OOB, работает всегда.
4. `/ip service print` — видим `ssh: no`. Уже причина №1.
5. `/ip firewall filter print` — видим правило `drop` для 22/tcp. Причина №2 (двойная поломка — как в жизни).
6. `/log print` — ищем записи о попытках входа и изменениях конфигурации.
7. Проверяем, что нас не заблокировал `address-list`/`/ip firewall address-list print` — на CHR01 пусто.

**Решение:**
```text
/ip firewall filter remove [find where dst-port=22 and action=drop]
/ip service enable ssh
/ip service print
```
Затем `ssh adm.student@192.168.42.2` с PVE01 — вход есть.

**Ответ пользователю:** «Доступ восстановлен, причина — ошибочное правило фильтра на самом коммутаторе, плюс был выключен SSH-сервис. Проверил: вход работает, конфигурация сохранена, на работу склада это не влияло (управление и трафик сканеров разведены). Если повторится — сразу пишите, у нас есть консольный доступ».

**Закрытие заявки:** категория «Сеть / оборудование», решение — правка firewall-правила и включение сервиса, корневая причина — изменение конфигурации без согласования, профилактика — бэкап конфигов по cron и запрет правок вне окна.

### Что изменилось на стенде

- На **PVE01** создан бридж `vmbr42`, адрес 192.168.42.1/24, без физического порта (изолированный лабораторный сегмент).
- На **PVE01** создана ВМ `CHR01` (id 200, MikroTik CHR), 256 МБ RAM, интерфейс в `vmbr42`.
- **CHR01**: имя `CHR01`, адрес 192.168.42.2/24, шлюз 192.168.42.1, пользователь `adm.student` (full), отключены telnet/ftp/www/api, включён syslog на 10.10.10.40, NTP на 10.10.10.10.
- **UBU01**: каталог `/srv/netconfig/lab042` с `chr01-config.rsc`, `/srv/netconfig/network-inventory.md`.
- Второй вендор (эмулятор/реальное устройство): настроен по чек-листу, данные внесены в инвентаризацию.
- В `stand-log.md` добавлена запись о лабораторной 042.

### Откат

1. На PVE01: `qm stop 200 && qm destroy 200` (ВМ лабораторная, данных в ней нет).
2. Удалить блок `vmbr42` из `/etc/network/interfaces`, выполнить `ifreload -a`, проверить `ip a`.
3. Если сомневаетесь — откатить снапшоты `before-sysadmin-042` на PVE01, GW01, UBU01, MON01 (VM → Snapshot → Revert). Каталог `/srv/netconfig` на UBU01 исчезнет вместе со снапшотом.
4. Проверить, что VMnet8/VMnet10 и стенд L2 работают как раньше: `ping 10.10.10.10`, вход на DC01, доступ к `\\FS01\Obshchee`.

## 6. Ошибки новичков и безопасность

1. **Настройка без консольного доступа.** → Меняете IP или ACL, SSH отваливается, иdevice недоступен: едете в серверную. → Всегда сначала подключайте консоль (или проверяйте конфиг командой с таймаутом/`reload in 5` у Cisco, safe mode у MikroTik).
2. **Забыли сохранить конфиг.** → После ребута устройство вернулось к заводскому состоянию, обычно в самый неподходящий момент. → `copy run start` / `save` сразу после успешной проверки, и это отдельный пункт чек-листа.
3. **Порт в `shutdown`.** → «Кабель исправен, а не работает» — самая частая причина тикета «не работает розетка». → `show interfaces status`, `undo/no shutdown`.
4. **Заводские `admin/admin`, Telnet, HTTP, SNMP `public`.** → Устройство взламывают в первый месяц; сканеры Shodan находят такие за минуты. → SSH-only, локальная учётка со сложным паролем, SNMPv3, management-интерфейс в отдельном VLAN с ACL.
5. **Прошивка без бэкапа и окна.** → Устройство уходит в бутлуп, откатывать нечем. → Бэкап конфига + две версии образа во flash + согласованное окно + проверка после ребута.
6. **Копипаст конфига из интернета.** → Чужие VLAN, ACL, маршруты; иногда — закладки. → Копируйте только синтаксис, значения всегда свои; читайте `diff` перед применением.
7. **Нет инвентаризации.** → Через год никто не знает, какой порт куда идёт и кто отвечает за устройство. → `network-inventory.md` + подписи на патч-панели + бэкапы по расписанию.
8. **Управление из пользовательского VLAN.** → Любой сотрудник с ноутбуком может дотянуться до management-интерфейса. → Отдельный mgmt-VLAN, ACL на input, доступ только с jump-host.

**Безопасность.** В боевой среде нельзя: держать Telnet/HTTP открытыми «пока настраиваем», оставлять заводские пароли и общие учётки, хранить конфиги в открытом виде в общей папке `\\FS01\Obshchee` (там их прочитает любой), давать подрядчику полный доступ без отдельных учёток и журналирования, отключать AAA ради удобства. Всё, что мы делаем в лабе (пароль `Lab#2026-vektor`, отключённые сервисы «по-простому», единый пароль на все ВМ), — только для стенда; в реальности — персональные учётки, AAA через RADIUS/TACACS+, журнал всех изменений.

## 7. Вопросы на собеседовании

**1. Чем `running-config` отличается от `startup-config` и как это выглядит у разных вендоров?**
Рабочая конфигурация — в оперативной памяти, применяется сразу; стартовая — во флеше/NVRAM, читается при загрузке. Изменения не сохраняются автоматически. Cisco: `show running-config` / `show startup-config`, сохранить — `copy running-config startup-config`. Huawei: `display current-configuration` / `display saved-configuration`, `save` в `vrpcfg.zip`. MikroTik — исключение: конфиг хранится в базе, отдельного save нет, есть `/export` и `/system backup`.
*Что хочет услышать интервьюер:* понимание, что «настроил» и «сохранил» — разные вещи.
*Красный флаг:* «нужно всегда нажимать кнопку Save в веб-интерфейсе, там же всё сохраняется».

**2. Как восстановить доступ к устройству, если потерян пароль и сеть недоступна?**
Только через консоль и физический доступ: у Cisco — прерывание загрузки в ROMMON, `confreg 0x2142`, загрузка без стартового конфига, копирование `startup` в `running`, смена пароля, возврат `config-register 0x2102`, сохранение. У Huawei — `Ctrl+B` в BootROM. У MikroTik — кнопка reset при включении либо Netinstall. На боевом устройстве это согласованное окно, уведомление, присутствие второго инженера и проверка после.
*Что хочет услышать:* процедура и то, что это не «просто кнопка», а регламентированная операция.
*Красный флаг:* «сброшу на заводские настройки, потом настрою заново» — без оценки последствий.

**3. Как организовать управление сорока коммутаторами в одном офисе?**
Единый mgmt-VLAN, статические адреса или резервирование по DHCP (по MAC), доступ только по SSH с jump-host, централизованная AAA (RADIUS/TACACS+), единый NTP (иначе логи не сопоставить), syslog на коллектор (MON01), SNMPv3 для мониторинга, шаблоны конфигурации по ролям (access/aggregation), автоматический бэкап конфигов с хранением и `diff`, документированная инвентаризация, окна обслуживания и план откатов, единая версия прошивки по вендору/модели.
*Что хочет услышать:* системность — не «настрою каждый», а «управляю как парком».
*Красный флаг:* «зайду на каждый по очереди и поправлю руками».

**4. Cisco официально не поставляет оборудование в РФ. Что делать и какие риски?**
Работающий парк не выключается, но обновлений безопасности и гарантии нет — накапливаются неустранимые CVE. «Серый» импорт не даёт права на обновления и заканчивается отказом вендора в поддержке. Практично: новое — Eltex/Qtech/Huawei/Zelax с проверкой наличия в реестре российской продукции (для госзакупок), для существующего парка — компенсирующие меры (изоляция mgmt-сети, ACL, мониторинг, план миграции по критичности), пилотная замена на одной площадке.
*Что хочет услышать:* знание реалий и инженерный обходной план, а не политику.
*Красный флаг:* «поставим неофициальную прошивку/активатор».

**5. Как выкатываете изменение на боевой коммутатор?**
Бэкап конфига и прошивки → чтение документации на версию → согласование окна → проверка консольного/OOB-доступа → применение по частям с проверкой после каждого шага → сохранение конфига → проверка сервиса (ping, SSH, трафик пользователей) → запись в журнал изменений. Если меняю удалённо и есть риск потерять связь — `reload in 10` у Cisco как «мёртвая рука» или safe mode у MikroTik.
*Что хочет услышать:* регламент, откат и проверка — а не «ну я быстро».
*Красный флаг:* «правлю в рабочее время, там же ничего серьёзного».

## 8. Источники (официальная документация)

- **Cisco** — домен `cisco.com`. Раздел: Configuration Guides / Command Reference для вашей платформы (например, Catalyst 9200). Поиск: `cisco catalyst 9200 configuration guide pdf`, `cisco ios command reference show interfaces`. `[ПРОВЕРИТЬ актуальность доступа из РФ]`
- **MikroTik** — домен `help.mikrotik.com`. Раздел: RouterOS Documentation, «Cloud Hosted Router», «First Time Configuration», «Safe Mode». Поиск: `mikrotik routeros first time configuration`, `mikrotik chr download`.
- **Huawei** — домен `support.huawei.com` (`[ПРОВЕРИТЬ доступность]`). Раздел: Product Documentation → VRP Configuration Guide. Поиск: `huawei s5720 configuration guide stelnet`.
- **Eltex** — домен `eltex-co.ru`. Раздел: Документация на ESR/MES. Поиск: `eltex esr configuration guide`, `eltex mes руководство по эксплуатации`.
- **Qtech / Zelax / TFortis** — официальные сайты вендоров (`qtech.ru`, `zelax.ru`, `tfortis.ru`), раздел «Документация/Руководства». `[ПРОВЕРИТЬ актуальность]`
- **Реестр российской радиоэлектронной продукции** — `gisp.gov.ru` `[ПРОВЕРИТЬ точный раздел]`. Поиск: `единый реестр российской радиоэлектронной продукции проверить модель`.
- **Cisco Packet Tracer** — `skillsforall.com` (бесплатно, легально). Поиск: `cisco packet tracer download skills for all`. `[ПРОВЕРИТЬ доступность из РФ]`
- **Proxmox VE** — домен `pve.proxmox.com`. Раздел: Administration Guide, «Network Configuration», «qm command line». Поиск: `proxmox network configuration bridge`, `proxmox qm importdisk`.
