# VMware ESXi и vCenter: установка во вложенной виртуализации, datastore, сети, vSwitch

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Нам до конца недели нужно проверить обновление 1С на тестовой копии базы. Нужен отдельный сервер, но чтобы он не видел `\\FS01\Buh` — там реальные данные. И чтобы его можно было включать/выключать, не трогая продуктив.»
> **Срочность:** средняя

Продуктив «Вектора» — это FS01, DC01 и будущий SRV1C01. Ставить тестовую копию рядом с ними нельзя, значит нужен отдельный гипервизор и **изолированная сеть** для тестовой ВМ. Именно это и делает связка «ESXi + vSwitch + port group + datastore».

ESXi встречается в вакансиях L2 постоянно: даже если компания уходит на zVirt или Proxmox, старые хосты VMware живут годами, и «посмотреть, почему хост Not Responding» — типовая задача второй линии. После этой главы вы должны уметь: поднять учебный ESXi внутри VMware Workstation, назначить ему управляющий адрес, создать датастор из второго диска руками через CLI, собрать vSwitch с port group и VLAN, добавить VMkernel для vMotion и объяснить, зачем между ESXi и vCenter открывают порты 443 и 902.

Важно про рынок: Broadcom свернул бесплатную редакцию vSphere Hypervisor (free ESXi) и продажи в РФ [ПРОВЕРИТЬ], поэтому учимся на 60-дневном evaluation, а в работе чаще увидите zVirt, ПК СВ «Горизонт-ВС», Proxmox (уроки 45–46). Навык переносимый: vSwitch ≈ Linux-бридж, datastore ≈ LVM-хранилище.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**ESXi — гипервизор 1 типа.** Это не Linux с надстройкой: у него собственное ядро **VMkernel**, которое само владеет железом (CPU, RAM, HBA, NIC) и отдаёт ресурсы ВМ. Поверх ядра работают служебные процессы:

- `hostd` — локальный агент управления, обслуживает Host Client (HTTPS 443) и API;
- `vpxa` — агент, через который хост общается с vCenter (его нет, пока хост не добавлен в vCenter);
- **DCUI** — консоль на «физической» консоли (в лабе это окно ВМ в Workstation): сеть, пароль root, логи, перезагрузка. Аналог iLO/IDRAC в реальной жизни;
- `vmx` — процесс на каждую ВМ (по сути «менеджер» конкретной виртуалки).

**Вложенная виртуализация.** Workstation — гипервизор 2 типа, ESXi внутри него — гипервизор 1 типа. Чтобы ESXi смог сам запускать ВМ, внешний гипервизор должен пробросить в него аппаратную виртуализацию (Intel VT-x/EPT или AMD-V/RVI). Формально VMware поддерживает ESXi-в-ESXi, но с ограничениями (нет прямого доступа к железу, производительность ниже, часть фич недоступна) [ПРОВЕРИТЬ актуальный KB].

**Datastore** — место, где лежат файлы ВМ (`*.vmx`, `*.vmdk`, `*.vswp`, ISO). Основной формат — **VMFS6** (кластерная ФС на блочном устройстве). Локальный диск = датастор `datastore1`; второй диск можно отформатировать в отдельный датастор. Альтернатива — NFS-датастор (файловый, по сети, урок 48). Толщина дисков: **thin** (растёт по мере записи), **thick lazy zeroed**, **thick eager zeroed** (сразу забивает нулями, быстрее для кластеров).

**Сети.** В ESXi нет привычного `eth0`. Есть три сущности:

- **vSwitch** (стандартный) — виртуальный коммутатор уровня L2 внутри хоста;
- **uplink (vmnic)** — физический адаптер, подключённый к vSwitch («наружу»);
- **port group** — именованная группа портов с политиками (VLAN, security, teaming). ВМ и VMkernel подключаются именно к port group.

Плюс **VMkernel-адаптеры (vmk)**: это IP-стек самого гипервизора. Каждый тип трафика — отдельный vmk: management (vmk0), vMotion, iSCSI, NFS, FT. Отдельный vmk = отдельные правила, отдельная пропускная способность, отдельная безопасность.

VLAN бывает трёх видов: **EST** (теги ставит внешний коммутатор), **VST** (тег ставит ESXi по VLAN ID на port group — самый частый), **VGT** (тег ставит сама ВМ, trunk в гостя).

**vCenter (VCSA)** — отдельная виртуалка-апплаенс, управляющая многими хостами: единый инвентарь, шаблоны, кластеры, HA/DRS, права. Требует FQDN, прямую и обратную запись DNS и синхронное время — иначе развёртывание падает на stage 1/2.

```text
ПК-хост (Windows) + VMware Workstation (гипервизор 2 типа)
└─ VM ESX01 (8–24 ГБ RAM)  ← гипервизор 1 типа, VT-x/EPT проброшен
   ├─ VMkernel
   │   ├─ vmk0 10.10.10.80  → port group "Management Network"
   │   └─ vmk1 10.10.10.82  → port group "PG-vMotion" (тег VMotion)
   ├─ vSwitch0 ── uplink vmnic0 ─► VMnet8 ─► DC01 / FS01 / весь офис
   │     ├─ "Management Network"
   │     ├─ "PG-vMotion"
   │     └─ "PG-VMs"  (сюда подключаем ВМ, которым нужна офисная сеть)
   ├─ vSwitch1  (без uplink — полностью изолированный)
   │     └─ "PG-Test-1C"  → тестовая 1С, наружу не ходит
   └─ Datastore: datastore1 (VMFS6, локальный) + DS02 (VMFS6, 2-й диск)
```

Порты между ESXi и vCenter: **443** (API/UI), **902** (доступ к консоли ВМ, MKS), **5480** (интерфейс управления апплаенсом VCSA), **22** (SSH, только для администрирования). Полная матрица — в официальном «vSphere Port and Firewall» [ПРОВЕРИТЬ].

## 3. Аналогия из жизни

ESXi — это жилой дом. **VMkernel** — служба эксплуатации, которая владеет зданием и решает, кому сколько места. **vSwitch** — этажный распределительный щит: к нему сходятся квартиры, а снаружи входит один толстый кабель (**uplink/vmnic**). **Port group** — это подъезд с номером и своим набором правил: «в этом подъезде живут только тестовые 1С, и наружу им выходить запрещено» (изолированный vSwitch1 без uplink). **VMkernel-адаптер** — служебный кабинет самого дома: управляющий (management), грузовой лифт для переездов (vMotion), склад для коробок (iSCSI), и у каждого свой отдельный вход. **Datastore** — подвал, где стоят коробки жильцов; **vCenter** — управляющая компания, у которой есть список всех домов и право переселять жильцов между домами.

Аналогия ломается на снапшотах и vMotion: в реальном доме нельзя «переселить» жильца в соседний дом, не остановив его жизнь, — а vMotion ровно это и делает (урок 43).

## 4. Практика и команды

Все команды — на ESX01 по SSH (`ssh root@10.10.10.80`). Включается SSH в Host Client: **Host → Manage → Services → TSM-SSH → Start**.

**1. Проверить себя: версия, железо, память**
```bash
esxcli system version get
esxcli hardware memory get
esxcli hardware cpu global get
```
Вывод (сокращённо):
```text
   Version: 8.0.3
   Build: ...            Releasebuild: ...
   Physical Memory: 8388608 KB        <- 8 ГБ
   CPU Cores: 2   Hyperthreading: false
```
Смотрим: сколько реально RAM дали ВМ, видит ли ESXi 2 ядра (если `CPU Cores: 1` — плохо пробросили vCPU).

**2. Имя хоста, DNS, маршрут**
```bash
esxcli system hostname set --fqdn=esx01.corp.vektor.lab
esxcli network ip dns server add --server=10.10.10.10
esxcli network ip dns search add --domain=corp.vektor.lab
esxcli network ip route ipv4 list
```
```text
Network  Gateway     Flags  Iface
default  10.10.10.2  UG     vmk0
```
Нет строки `default` — хост не выйдет за свою подсеть. Классическая причина «хост виден только по ping из своей сети».

**3. Физические адаптеры (uplink-кандидаты)**
```bash
esxcli network nic list
```
```text
Name    PCI Device  Driver  Link  Speed  Duplex  MAC Address
vmnic0  0000:03:00  vmxnet3 Up    10000  Full   00:50:56:aa:11:01
```
`Link Up` и скорость — первое, что проверяют при «пропала сеть».

**4. Список vSwitch и port group**
```bash
esxcli network vswitch standard list
esxcli network vswitch standard portgroup list
```
```text
vSwitch0
   Name: vSwitch0   Ports: 128   MTU: 1500   Uplinks: vmnic0
   Portgroup: Management Network  VLAN ID: 0  Active: vmnic0
```
Если `Uplinks:` пусто — коммутатор «внутренний», ВМ наружу не выйдут (иногда это баг, иногда фича).

**5. Создать vSwitch, port group, VLAN, прикрутить uplink**
```bash
esxcli network vswitch standard add -v vSwitch1
esxcli network vswitch standard portgroup add -v vSwitch1 -p PG-Test-1C
esxcli network vswitch standard portgroup set -p PG-Test-1C --vlan-id 0
esxcli network vswitch standard uplink add -v vSwitch1 -u vmnic1
```
`-v` — имя коммутатора, `-p` — имя port group, `--vlan-id 0` — без тега. Для VST-схемы ставите, например, `--vlan-id 42`, и коммутатор должен отдавать этот VLAN транком.

**6. Политики безопасности на port group**
```bash
esxcli network vswitch standard policy security set -v vSwitch1 \
  --allow-promiscuous false --allow-mac-change false --allow-forged-transmits false
```
По умолчанию в ESXi эти три опции **разрешены** (историческая совместимость) — в бою их принято выключать. Promiscuous нужен только для IDS/мониторинга внутри ВМ.

**7. VMkernel-адаптер для vMotion и тег**
```bash
esxcli network ip interface add -i vmk1 -p PG-vMotion
esxcli network ip interface ipv4 set -i vmk1 -t static -I 10.10.10.82 -N 255.255.255.0
esxcli network ip interface tag add -i vmk1 -t VMotion
esxcli network ip interface list
```
`-t VMotion` — тег, по которому хост понимает, что через vmk1 можно живой миграцией гонять память ВМ.

**8. Проверка связности с конкретного vmk**
```bash
vmkping -I vmk1 10.10.10.10
```
```text
PING 10.10.10.10: 56 data bytes
64 bytes from 10.10.10.10: icmp_seq=0 ttl=128 time=0.8 ms
```
`vmkping` идёт из нужного vmk, а не «вообще из хоста». Именно им проверяют, что vMotion-сеть реально ходит.

**9. Диски и датасторы**
```bash
esxcli storage core device list
esxcli storage filesystem list
esxcli storage vmfs extent list
```
```text
naa.6000c293...   Display Name: Local VMware Disk (naa.6000c293...)   Size: 40960
Volume Name: datastore1   VMFS-6   40 GB   Mounted: true
```
Второй диск обычно виден как отдельное `naa.*` устройство без датастора — с ним и работаем дальше.

**10. Создать датастор из CLI (VMFS6)**
```bash
# 1) узнать точное имя устройства (НЕ перепутать!)
esxcli storage core device list | grep -i naa
partedUtil getptbl /vmfs/devices/disks/naa.6000c293aabbccdd1122334455667788
# 2) разметить GPT и один раздел типа VMFS
partedUtil setptbl /vmfs/devices/disks/naa.6000c293... \
  gpt "1 2048 41940991 AA31E02A400F11DB9590000C2911D1B8 0"
# 3) отформатировать в VMFS6 с именем DS02
vmkfstools -C vmfs6 -S DS02 /vmfs/devices/disks/naa.6000c293...:1
```
`AA31E02A...` — фиксированный GPT-тип раздела VMFS. Второе число — последний сектор; для диска 20 ГБ это `размер_в_секторах − 2048`. Формулу проще не считать, а взять готовые цифры из KB VMware «Creating a VMFS datastore from the command line». GUI-путь: **Host Client → Storage → Datastores → New Datastore**.

**11. ВМ и их состояние**
```bash
vim-cmd vmsvc/getallvms
vim-cmd vmsvc/power.getstate 1
esxcli network vm list
```
Последняя показывает, какие ВМ к каким port group подключены — быстрый способ найти «а куда воткнули тестовый сервер».

**12. Логи и сервисы (то, чем реально диагностируют)**
```bash
tail -50 /var/log/hostd.log
tail -50 /var/log/vmkernel.log
tail -50 /var/log/vpxa.log
/etc/init.d/hostd restart
esxcli network firewall ruleset list
esxcli network ip connection list | head
```
`vpxa.log` — связь с vCenter; `vmkernel.log` — сеть, хранилища, падения; `hostd.log` — UI/API и операции с ВМ.

GUI, к которому всё это относится: Host Client `https://10.10.10.80/ui` → **Networking → Virtual switches**, **Storage → Datastores**, **Host → Manage → Services**.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 собран: DC01 (10.10.10.10, AD/DNS/DHCP), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01.

**Про машины ESXi отдельно.** В таблице стенда ESXi-хостов нет — они появляются только в этом уроке как учебная надстройка над стендом (VMware-часть стенда, аналог PVE01 для Proxmox). Имена фиксируем раз и навсегда в `stand-log.md` и дальше используем только так: **ESX01 — 10.10.10.80**, **ESX02 — 10.10.10.81** (необязательно), **VC01 — 10.10.10.85** (vCenter, если хватает RAM). Адреса вне DHCP-пула DC01 (100–150) и не конфликтуют с таблицей стенда.

Требования к ПК-хосту: VT-x/EPT включён в BIOS, VMware Workstation 17. По памяти честно:

| RAM хоста | Что реально сделать |
|---|---|
| 16 ГБ | только ESX01 на 8 ГБ, vCenter не поднимаем (теория + разбор) |
| 32 ГБ | ESX01 на 20 ГБ + VC01 (tiny ~12 ГБ), остальной стенд выключен |
| 64 ГБ | ESX01 + ESX02 + VC01, можно потрогать кластер |

ISO ESXi 8 берём с официального сайта Broadcom/VMware (60-дневный evaluation). Точные цифры минимальных требований (RAM, размер загрузочного диска) сверяйте с актуальной документацией: ориентир — 8 ГБ RAM и 32 ГБ диска минимум, лучше 60–100 ГБ [ПРОВЕРИТЬ].

Если какую-то часть стенда вы ещё не проходили — например, не создана обратная зона DNS на DC01 — сделайте её сейчас, она нужна vCenter (урок 34).

### Снапшот

1. На DC01 (он работает): Workstation → **VM → Snapshot → Take Snapshot**, имя `before-l2-042`.
2. ESX01 создаём с нуля, поэтому снапшот снимаем сразу после установки ESXi и **до** настройки сети/датастора: `before-l2-042`.
3. Если поднимаете VC01 — после развёртывания и до добавления хостов: `before-l2-042`.

### Шаги

1. **Создать ВМ ESX01 в Workstation.** New Virtual Machine → Custom → «I will install the operating system later» → Guest OS: `Other / Other 64-bit` (если в вашей версии Workstation есть профиль VMware ESXi — берите его) → Firmware type: **UEFI**, Secure Boot выключить → 2 vCPU, **8 ГБ RAM** (или 20 ГБ при планах на vCenter) → диск **60 ГБ**, controller LSI Logic SAS → один сетевой адаптер, **Custom: VMnet8** → в **Processors** поставить галку **«Virtualize Intel VT-x/EPT or AMD-V/RVI»**.
   *Ожидаемо:* ВМ создана, в разделе Processors галка стоит. Без неё ESXi либо не поставится, либо упадёт в PSOD при первом включении ВМ.

2. **Установить ESXi.** Подключить ISO, включить ВМ, пройти установщик: принять EULA, выбрать диск, пароль root `Lab#2026-vektor`, подтвердить, дождаться перезагрузки.
   *Ожидаемо:* на экране жёлто-серый DCUI с адресом `http://...` и «Download tools».

3. **Настроить management-сеть через DCUI.** В окне ВМ: **F2** → логин `root` / `Lab#2026-vektor` → **Configure Management Network** → **IPv4 Configuration**: static, IP `10.10.10.80`, маска `255.255.255.0`, шлюз `10.10.10.2` → **DNS Configuration**: `10.10.10.10`, suffix `corp.vektor.lab` → **VLAN (optional)**: пусто (нет) → **IPv6**: отключить → **Esc** → **Y** → перезапуск management network (**F11**).
   *Ожидаемо:* в DCUI сверху адрес `https://10.10.10.80`.

4. **Проверить связность с ПК и добавить DNS-записи на DC01** (PowerShell от админа):
```powershell
Test-NetConnection 10.10.10.80 -Port 443
Add-DnsServerResourceRecordA -Name esx01 -IPv4Address 10.10.10.80 -ZoneName corp.vektor.lab
Add-DnsServerResourceRecordPtr -Name 80 -ZoneName 10.10.10.in-addr.arpa `
  -PtrDomainName esx01.corp.vektor.lab
Resolve-DnsName esx01.corp.vektor.lab
```
   *Ожидаемо:* `TcpTestSucceeded : True`, `Resolve-DnsName` отдаёт `10.10.10.80`.

5. **Войти в Host Client** `https://10.10.10.80/ui`, логин `root` / `Lab#2026-vektor`. Принять evaluation (60 дней) → **Host → Manage → Licensing**.
   *Ожидаемо:* в лицензии «vSphere 8 Evaluation, 60 days remaining». ESXi без лицензии — норма для лабы; бесплатную редакцию сейчас не скачать [ПРОВЕРИТЬ].

6. **Включить SSH и собрать базовую информацию по CLI.** **Host → Manage → Services → TSM-SSH → Start**, затем `ssh root@10.10.10.80` и команды из раздела 4 (пункты 1–4). Зафиксируйте вывод `esxcli network nic list`: имя адаптера (`vmnic0`) и `Link: Up`.
   *Ожидаемо:* видите vSwitch0 с uplink vmnic0 и port group `Management Network`, VLAN 0.

7. **Добавить второй виртуальный диск под датастор.** Выключить ESX01 → Workstation **VM → Settings → Add → Hard Disk → 20 ГБ, thin** → включить ESX01 → в SSH:
```bash
esxcli storage core adapter rescan --all
esxcli storage core device list | grep naa
```
   *Ожидаемо:* в списке два `naa.*` — загрузочный и новый 20 ГБ.

8. **Разметить и отформатировать DS02** (пункт 10 раздела 4), затем проверить:
```bash
esxcli storage filesystem list
```
   *Ожидаемо:* две строки — `datastore1` (VMFS-6, ~60 ГБ) и `DS02` (VMFS-6, ~20 ГБ), обе `Mounted: true`, в Host Client → **Storage → Datastores** их тоже видно.

9. **Собрать изолированный vSwitch1 без uplink и port group PG-Test-1C.**
```bash
esxcli network vswitch standard add -v vSwitch1
esxcli network vswitch standard portgroup add -v vSwitch1 -p PG-Test-1C
esxcli network vswitch standard list
```
   *Ожидаемо:* у vSwitch1 в строке `Uplinks:` пусто. Это и есть сеть «наружу нельзя» из тикета ivanov.

10. **Сделать vmk1 для vMotion** (пункт 7 раздела 4), проверить `vmkping -I vmk1 10.10.10.10`.
   *Ожидаемо:* пинг с vmk1 проходит, `esxcli network ip interface list` показывает vmk1 с тегом `VMotion`.

11. **Проверить изоляцию на practice.** В Host Client создайте ВМ `test-1c` (1 vCPU, 512 МБ, без ОС) и подключите её к `PG-Test-1C`; затем:
```bash
esxcli network vm list
```
   *Ожидаемо:* `test-1c` в списке с port group `PG-Test-1C`. ВМ можно включить, ОС в неё не ставим — это уже трёхуровневая вложенность, RAM дорого.

12. **vCenter (только если ≥32 ГБ RAM).** Запустить установщик VCSA (installer на Windows) → **Deploy**: target = `10.10.10.80` (root), VM name `VC01`, размер **Tiny**, датастор `DS02`, сеть `PG-VMs`/`Management Network`, IP `10.10.10.85/24`, GW `10.10.10.2`, DNS `10.10.10.10`, FQDN `vc01.corp.vektor.lab`, NTP `10.10.10.10`, SSO-домен `vsphere.local`, пароль `Lab#2026-vektor`. Затем stage 2 (Setup). Не забудьте A/PTR-записи для `vc01` **до** деплоя.
   *Ожидаемо:* `https://vc01.corp.vektor.lab/ui` открывается, вход `administrator@vsphere.local`. Дальше: Datacenter `VEKTOR-DC` → Cluster → Add Host `10.10.10.80`.

13. **Записать всё в `stand-log.md`.**

### Проверка

- [ ] ESX01 стартует, в DCUI адрес `https://10.10.10.80`, вход root работает.
- [ ] `Test-NetConnection 10.10.10.80 -Port 443` = True; `Resolve-DnsName esx01.corp.vektor.lab` отдаёт `10.10.10.80`.
- [ ] `esxcli network nic list` — `vmnic0`, `Link Up`.
- [ ] `esxcli network vswitch standard list` — vSwitch0 (uplink vmnic0) и vSwitch1 (uplink пусто).
- [ ] `esxcli storage filesystem list` — `datastore1` и `DS02`, обе Mounted.
- [ ] `esxcli network ip interface list` — vmk0 и vmk1, у vmk1 тег VMotion.
- [ ] `vmkping -I vmk1 10.10.10.10` проходит.
- [ ] ВМ `test-1c` привязана к `PG-Test-1C`.
- [ ] (если делали) vCenter видит ESX01 в состоянии Connected.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Тестовый сервер, который вы делали, с обеда не пингуется, страница `https://10.10.10.80` не открывается. Мне до вечера отчёт сдавать.»
> **Срочность:** высокая

**Как воспроизвести поломку.** На ESX01 по SSH: `esxcli network vswitch standard portgroup set -p "Management Network" --vlan-id 99`. Это имитация «коллега поправил VLAN на port group, чтобы добавить сеть, и ушёл». Хост мгновенно теряет связь с 10.10.10.0/24 — ровно как в реальной жизни, когда management-сеть случайно уезжает в тегированный VLAN, которого на аплинке нет.

**Диагностика по шагам (не перескакивать!):**

1. С WIN11-CL01: `ping 10.10.10.80` — тишина. Значит проблема не в браузере.
2. На DC01: `Resolve-DnsName esx01.corp.vektor.lab` — имя резолвится, `arp -a | findstr 10.10.10.80` — MAC нет. Вывод: узел недоступен на L2, DNS не при чём.
3. Проверить, что ВМ ESX01 вообще работает: окно ВМ в Workstation → DCUI на экране, значит гипервизор жив, проблема сетевая.
4. **Войти через консоль (это наш «iLO»)**: F2 → ввести пароль → **Configure Management Network → VLAN (optional)** — там пусто, но это лишь DCUI-настройка. Дальше **View Management Network** / **Test Management Network**: IP и шлюз заданы верно, пинг шлюза `10.10.10.2` не идёт.
5. **Поднять management обратно**: F2 → Configure Management Network → **VLAN (optional)** → ввести `0` (или оставить пустым) → Esc → **Y** → перезапуск management network.
   Если не помогло — войти локально не получится, тогда идём через консоль загрузки: перезагрузить (F12) и в загрузчике ESXi сбросить сетевые настройки. В реальной среде на этом шаге смотрят iLO/IDRAC.
6. Убедиться, что VLAN реально снят: `esxcli network vswitch standard portgroup list` → у `Management Network` `VLAN ID: 0`.
7. Проверить, что не осталось хвостов: `esxcli network ip interface ipv4 get` (vmk0 = 10.10.10.80/24), `vmkping -I vmk0 10.10.10.10`, `esxcli network firewall ruleset list | grep -i vpxa`.
8. Посмотреть логи, чтобы зафиксировать причину в тикете: `grep -i vlan /var/log/vmkernel.log | tail`.
9. Если хост был в vCenter — после восстановления он перейдёт в Connected за 1–3 минуты; если нет: `/etc/init.d/vpxa restart`, затем на VC01 — Reconnect.

**Ответ пользователю:** «Тестовый стенд восстановлен, `10.10.10.80` снова доступен, ваши файлы не затронуты. Причина — некорректное изменение VLAN на управляющей сети гипервизора; исправлено через консоль, на продуктивные сервисы не повлияло. Чтобы такого не повторялось, перед сетевыми правками снимаем снапшот и согласуем окно.»

**Закрытие заявки:** категория «Виртуализация → сеть гипервизора», решение — «некорректный VLAN ID на management port group, восстановлено через DCUI», привязка CMDB к ESX01, заметка «change request на сетевые настройки ESXi обязателен». Время: диагностика 10 мин, восстановление 3 мин.

### Что изменилось на стенде

- Добавлена ВМ **ESX01** — ESXi 8 Evaluation, `10.10.10.80`, FQDN `esx01.corp.vektor.lab`, root/`Lab#2026-vektor`.
- В Workstation у ESX01: 1 NIC на VMnet8, включён проброс VT-x/EPT, второй диск 20 ГБ.
- На ESX01: `datastore1` (VMFS6, локальный) и **DS02** (VMFS6, 20 ГБ); vSwitch0 (vmnic0, `Management Network`, `PG-vMotion`), **vSwitch1** без uplink (`PG-Test-1C`); vmk1 `10.10.10.82` с тегом VMotion; ВМ `test-1c` (без ОС).
- На DC01: A- и PTR-записи `esx01`; (если делали vCenter) `vc01` → `10.10.10.85`.
- (Опционально) **VC01** — VCSA Tiny, `10.10.10.85`, SSO `vsphere.local`, ESX01 добавлен в дата-центр `VEKTOR-DC`.
- SSH на ESX01 включён — оставлять включённым вне лабы не надо.

### Откат

Workstation → выбрать ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-l2-042` → **Go to**. Откатывать ESX01 целиком: удалить ВМ и поставить заново (или вернуть снапшот, снятый сразу после установки) — датастор DS02 и все port group при откате исчезнут, это нормально. На DC01 откатите снапшот — записи DNS тоже откатятся. В vCenter: удалить ESX01 из инвентаря (Right click → Remove from inventory), затем удалить ВМ VC01 из Workstation. Не забудьте обновить `stand-log.md` после откатa, иначе следующая глава будет опираться на несуществующую инфраструктуру.

## 6. Ошибки новичков и безопасность

1. **Не включил «Virtualize Intel VT-x/EPT»** → ESXi падает в PSOD (фиолетовый экран) при первом включении ВМ или отказывается стартовать. Правильно: галка в настройках процессора ВМ до установки, VT-x в BIOS хоста.
2. **Два сетевых адаптера ВМ на один VMnet8** → широковещательные кадры приходят дважды, в лаборатории это выглядит как «то работает, то нет». Правильно: разные uplink — на разные сегменты; проверять `esxcli network nic list` и схему сети.
3. **Форматирование не того `naa.*`** → `vmkfstools -C` затирает датастор с ВМ. Правильно: сначала `partedUtil getptbl` на конкретное устройство и сверка размера, толькопотом форматирование; перед этим — снапшот.
4. **Изменил VLAN/сеть management без консольного доступа** → хост потерян (наша лаба). Правильно: снапшот, окно, доступ к DCUI/iLO, и только потом правки.
5. **Оставил promiscuous/MAC change/forged transmits разрешёнными** → ВМ может подменить MAC и устроить ARP-спуф в сети. Правильно: на port group с ВМ пользователей эти опции выключить.
6. **SSH и root-пароль из лабы в проде** → прямой доступ ко всему гипервизору. Правильно: SSH включать на время работ и выключать, root-пароль уникальный, админ-доступ через именованные учётки vCenter.
7. **Двойной overcommit во вложенной схеме** → RAM раздаётся и внешним, и внутренним гипервизором, ВМ внутри начинают свопиться в `*.vswp` и «тормозят без причины». Правильно: считать RAM по уровням, не выделять внутреннему ESXi больше, чем реально даёт хост.
8. **vCenter без PTR-записи и NTP** → деплой падает на stage 1/2 или апплаенс не поднимается. Правильно: A + PTR до запуска установщика, NTP на DC01, синхронное время.

**Безопасность.** В боевой среде нельзя: держать ESXi без лицензии/патчей, отключать его firewall целиком (`esxcli network firewall set --enabled false`) ради «чтобы работало», открывать 443/902 в интернет, ставить на management vmk адрес из DHCP, хранить ISO и снапшоты ВМ на том же датасторе годами, использовать один root-пароль на все хосты. Всё, что мы делаем в лабе «попроще» (evaluation, root/`Lab#2026-vektor`, SSH включён), в реальности закрывается лицензией, SSO-учётками, MFA на VPN и отдельной management-сетью без выхода в интернет.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем стандартный vSwitch отличается от distributed, и когда что используете?**

Ответ: стандартный vSwitch настраивается отдельно на каждом хосте, живёт внутри хоста и не требует vCenter — подходит для одиночного хоста и аварийного доступа. Distributed Switch (vDS) настраивается один раз в vCenter и применяется ко всем хостам кластера: единые port group, VLAN, политика teaming, миграция настроек вместе с ВМ при vMotion. Минус vDS: он часть vCenter — без vCenter менять конфигурацию нельзя (хотя трафик продолжает ходить). В маленьком офисе — standard, в кластере из 3+ хостов — vDS.
*Что хочет услышать интервьюер:* понимание, что vDS — про единообразие и масштаб, стандартный — про независимость от vCenter.
*Красный флаг:* «vDS быстрее, потому что распределённый» — это не про скорость.

**Вопрос 2. Зачем отдельные VMkernel-адаптеры для management, vMotion и iSCSI?**

Ответ: каждый vmk — отдельный IP-стек и отдельная точка применения политик. Management отделяют, чтобы миграции и бэкапы не «съедали» управление; vMotion — потому что для него нужен стабильный канал с низкой задержкой и часто jumbo frames (MTU 9000), а также отдельный тег VMotion; iSCSI — из-за MTU и требований к пропускной способности, плюс изоляция трафика хранилища от пользовательского. Плюс требования безопасности: management-сеть не должна быть доступна из пользовательских сегментов.
*Что хочет услышать:* «изоляция трафика + MTU + предсказуемость + безопасность», а не заученный список.
*Красный флаг:* «можно всё на vmk0, разницы нет».

**Вопрос 3. Что такое datastore, чем VMFS отличается от NFS-датастора и что такое thin/thick?**

Ответ: datastore — хранилище файлов ВМ. VMFS6 — кластерная блочная ФС на SAN/DAS, с блокировками, позволяет нескольким хостам одновременно работать с одним хранилищем (нужно для vMotion/HA). NFS-датастор — файловый шаринг с NAS, проще и часто дешевле, но зависит от сети и настроек NFS-сервера; для ESXi это обычный NFS v3/v4.1 [ПРОВЕРИТЬ набор версий для вашей версии]. Диски: thin растёт по мере записи, thick lazy zeroed выделяет место сразу без обнуления, thick eager zeroed сразу обнуляет — быстрее для кластеров и FT. По умолчанию thin экономит место, но легко «упирается» в датастор.
*Что хочет услышать:* связь выбором хранилища и фичами (vMotion, HA), а не только «место для файлов».
*Красный флаг:* «datastore — это папка».

**Вопрос 4. Какие порты нужны между ESXi и vCenter и что произойдёт при потере связи?**

Ответ: 443 (API/UI, управление), 902 (доступ к консоли ВМ, MKS), при необходимости 5480 (интерфейс апплаенса), 22 (SSH, администрирование). При потере связи хост в vCenter становится `Not Responding`/`Disconnected`, но **ВМ продолжают работать** — vCenter не в пути данных. Новые задачи (клонирование, vMotion, HA-рестарт) не выполняются, DRS не балансирует; после восстановления связи хост переподключается автоматически или вручную.
*Что хочет услышать:* «vCenter — это управление, а не трафик ВМ; ВМ не падают».
*Красный флаг:* «если упал vCenter, все ВМ остановятся».

**Вопрос 5. Хост в vCenter показывается как Not Responding. Ваши действия?**

Ответ: по порядку — ping/порт 443 до хоста; если недоступен, идти через консоль/iLO (DCUI) и смотреть IP, маску, шлюз, VLAN, линк на `vmnic`; проверить `esxcli network nic list`, `esxcli network vswitch standard portgroup list` (не уехал ли VLAN), `esxcli network ip interface ipv4 get`. Если хост пингуется, а vCenter нет — смотреть `vpxa`: `/etc/init.d/vpxa restart`, `tail /var/log/vpxa.log`, проверить firewall rulesets, время на хосте и в vCenter (рассинхрон ломает аутентификацию). Дальше — `hostd` (`/etc/init.d/hostd restart`), при зависании — maintenance mode и перезагрузка хоста только в окно. Обязательно проверить, не отвалился ли storage (all paths down) — тогда хост может быть «жив», но неработоспособен.
*Что хочет услышать:* структурированный алгоритм «сеть → сервисы → время → storage → логи», а не «перезагрузить хост».
*Красный флаг:* «сразу ребутну хост» — без проверки, что ВМ на нём не пострадают.

## 8. Источники (официальная документация)

- **techdocs.broadcom.com** (сюда переехала документация VMware после приобретения Broadcom [ПРОВЕРИТЬ актуальный адрес раздела]) — разделы «vSphere Installation and Setup», «vSphere Networking», «vSphere Storage», «vCenter Server Installation and Setup». Поиск: `vsphere 8 networking concepts standard switch port group`.
- **knowledge.broadcom.com** (бывшая база знаний VMware) — статьи «Creating a VMFS datastore from the command line», «Nested virtualization and ESXi», «Troubleshooting a host in Not Responding state», «Required ports for vCenter Server and ESXi». Поиск: `create VMFS datastore partedUtil vmkfstools kb`.
- **knowledge.broadcom.com** — «virtualizing ESXi in VMware Workstation» / «nested ESXi support». Поиск: `nested ESXi in Workstation VT-x EPT requirements`.
- **docs.vmware.com** — документация VMware Workstation Pro (настройки процессора, UEFI, виртуальные сети VMnet). Поиск: `vmware workstation pro documentation virtualize intel vt-x ept`.
- **learn.microsoft.com** — справочник по `Add-DnsServerResourceRecordA` / `Add-DnsServerResourceRecordPtr` для шага с DNS на DC01. Поиск: `Add-DnsServerResourceRecordPtr powershell`.
- **ubuntu.com** — не требуется в этой главе, но пригодится в уроке 45 (KVM/Proxmox) для сравнения.
- Точные минимальные требования ESXi 8 (RAM, размер загрузочного диска, поддержка legacy BIOS/UEFI) и сроки evaluation — сверять на странице «vSphere 8 Installation and Setup → System Requirements» [ПРОВЕРИТЬ].
