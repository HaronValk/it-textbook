# Хранилища в Linux: LVM, mdadm, ZFS, Btrfs, снапшоты, шифрование LUKS

## 1. Введение (зачем это нужно в реальной работе)

```text
Заявка #1142
От:      petrova (HR, WIN10-CL02)
Тема:    Не грузятся архивы на UBU01
Текст:   «Добрый день! Скрипт выгрузки архивов сотрудников на UBU01 падает
         с ошибкой "No space left on device". И ещё вопрос: на прошлой неделе
         я случайно перезаписала файл выгрузки не тем содержимым — можно
         вернуть вчерашнюю версию? Отчёт нужен к пятнице.»
Срочность: средняя
```

Такие заявки — ежедневность системного администратора. «Кончилось место», «диск умер», «верните файл», «а можно расширить без переустановки», «а если винчестер украдут из серверной, данные прочитают?» — всё это одна тема: управление хранилищем.

После этой главы ты должен уметь: собрать программный RAID из нескольких дисков и пережить смерть одного из них; нарезать логические тома LVM и расширять их на ходу без остановки сервиса; снять снапшот перед рискованным обновлением и откатиться на него; зашифровать раздел через LUKS так, чтобы в случае чего не потерять данные навсегда; понимать, когда брать ext4, а когда Btrfs или ZFS, и почему «RAID — это не бэкап». Всё это — база, на которой потом строятся HA-кластеры (урок 29) и отказоустойчивые сервисы.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

В Linux всё, с чего можно читать блоками, — это блочное устройство. Ядро выстраивает их в стек: снизу драйвер (SCSI/NVMe/virtio), выше — слой `device-mapper` и md, ещё выше — файловая система. Каждый уровень стека может быть «прослойкой»: RAID, шифрование, LVM, снапшоты.

```text
      ┌───────────────────────────────────────────────────────┐
      │ Приложение: nginx-логи, БД, /srv/hr-archive           │
      ├───────────────────────────────────────────────────────┤
      │ ФС: ext4 │ XFS │ Btrfs │ ZFS                          │
      ├───────────────────────────────────────────────────────┤
      │ LVM:  LV ──► VG ──► PV        (dm-linear, PE по 4 МиБ)│
      │ LUKS: dm-crypt (AES-XTS)      (шифрование секторов)   │
      │ RAID: md0 (md/raid1) │ ZFS RAIDZ │ Btrfs raid1        │
      ├───────────────────────────────────────────────────────┤
      │ Разделы: GPT (sda1…), таблица в начале диска          │
      ├───────────────────────────────────────────────────────┤
      │ /dev/sdb /dev/sdc /dev/sdd /dev/sde  ← диски VMware   │
      └───────────────────────────────────────────────────────┘
```

**mdadm (программный RAID).** Ядро собирает несколько дисков в массив `/dev/mdN`. Уровни: RAID0 — полоса без защиты (быстро, но смерть одного диска = смерть массива); RAID1 — зеркало; RAID5/6 — чётность, выдерживает 1/2 отказа; RAID10 — зеркало+полоса, лучшее по производительности записи. Метаданные версии 1.2 лежат в 4 КиБ от начала устройства. Массив хранит «грязный» флаг и bitmap (если включён) — ускоряет ресинк. Состояние видно в `/proc/mdstat`.

**LVM.** Три уровня: физический том PV (диск, раздел, md-массив, LUKS-контейнер), группа томов VG (общий пул места), логический том LV (то, что форматируется в ФС). Внутри VG место нарезано на экстенты (по умолчанию 4 МиБ). Метаданные LVM — текстовый файл, копии лежат в `/etc/lvm/backup` и `/etc/lvm/archive`. Практическая ценность: LV можно расширить, «выдернуть» кусок из VG, объединить диски разного размера и снять снапшот.

**Снапшоты и CoW (copy-on-write).** Два принципиально разных механизма:
- LVM-снапшот — отдеческий LV фиксированного размера. При изменении блока в оригинале старое содержимое копируется в снапшот. Кончилось место в снапшоте — снапшот инвалидируется. У thin-pool снапшоты «виртуальные», место не резервируется.
- Снапшоты Btrfs/ZFS — вообще не копии. Это зафиксированная ссылка на неизменяемые блоки (CoW). Занимают почти ноль, пока данные в оригинале не поменяются.

**ZFS.** Не файловая система в обычном смысле, а менеджер томов + ФС + RAID + контрольные суммы в одном. Пул (`zpool`) состоит из vdev'ов (диск, зеркало, RAIDZ1/2/3). Каждый блок имеет контрольную сумму — «тихое» повреждение данных детектируется и (в зеркалах/RAIDZ) чинится. Кэш ARC в RAM, SLOG/ZIL — отдельный лог для синхронной записи, L2ARC — кэш на SSD. Лицензия CDDL, поэтому в mainline-ядро ZFS не входит; в Ubuntu 24.04 ставится пакетом `zfsutils-linux` `[ПРОВЕРИТЬ: точная версия и способ сборки модуля для 24.04]`.

**Btrfs.** CoW-ФС с субтомами (subvolume — монтируемая часть ФС, аналог «папки с правами тома»), снапшотами, встроенным RAID (raid0/1/10 стабильны, raid5/6 до сих пор не рекомендуется для продакшена), сжатием (zstd) и скрабом. XFS — зрелая ФС для больших файлов и БД, но **уменьшать её нельзя**. ext4 — универсальная и самая предсказуемая, умеет и расти, и сжиматься.

**LUKS2 и dm-crypt.** LUKS — формат заголовка, dm-crypt — модуль ядра, который шифрует. Заголовок LUKS2 (16 МиБ) хранит соль и зашифрованный мастер-ключ в нескольких keyslot'ах; каждый слот защищён паролем или ключевым файлом через PBKDF (по умолчанию argon2id). Меняя пароль, ты расшифровываешь мастер-ключ и заново его шифруешь — сами данные не переписываются. AES-XTS — типовой шифр для дисков.

**Сетевые хранилища** (на будущее): NFS — 2049/tcp+udp, SMB — 445/tcp, iSCSI — 3260/tcp. Пока они не нужны, но стек «файл → LVM → RAID → сеть» строится одинаково.

## 3. Аналогия из жизни

Представь склад. Стеллажи — это физические диски (PV). Ты ломаешь перегородки между стеллажами и получаешь одно большое помещение переменной вместимости — это VG. Дальше ты ставишь внутри перегородки и выделяешь конкретному отделу секцию нужного метража — это LV. Понадобилось больше места бухгалтерии — передвигаешь перегородку (lvextend), товар при этом не выгружаешь.

mdadm — это когда рядом стоят два одинаковых стеллажа с идентичным товаром: один сгорел, второй работает. Снапшот — фотография склада, сделанная перед генеральной уборкой: по ней можно расставить всё как было. LUKS — опечатанная дверь с кодовым замком: даже если вор проник в помещение, ящики не открыть.

Аналогия перестаёт работать на CoW-снапшотах: фотография склада — это не копия товара, а «замороженная» ссылка на те же самые полки, и пока товар не переставили, снапшот не занимает ни сантиметра.

## 4. Практика и команды

**1. Что вообще есть на машине**

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,UUID
blkid
```

`-o` задаёт набор столбцов: имя, размер, тип (`disk`/`part`/`lvm`/`raid1`/`crypt`), ФС, точка монтирования, UUID. В `blkid` берём UUID — только по нему и монтируем в `/etc/fstab`.

```text
NAME          SIZE TYPE  FSTYPE      MOUNTPOINTS   UUID
sda            40G disk
├─sda1          1M part
├─sda2          2G part  ext4        /boot         a1b2...
└─sda3         38G part  LVM2_member               c3d4...
  └─ubuntu--vg-ubuntu--lv 20G lvm  ext4  /         e5f6...
```

**2. Смотрим LVM**

```bash
pvs; vgs; lvs -o lv_name,lv_size,lv_attr,snap_percent
vgs -o vg_name,vg_size,vg_free
```

`vg_free` — сколько ещё можно нарезать. `lv_attr`: первая буква `-` (обычный) / `s` (снапшот) / `t` (thin), третья `w` (writable), `o` (open — смонтирован). `snap_percent` — заполнение снапшота, приближается к 100 → пора расширять.

**3. Создаём PV, VG, LV**

```bash
pvcreate /dev/md0
vgcreate vg_data /dev/md0
lvcreate -L 1G -n lv_hr vg_data
mkfs.ext4 -L hr-archive /dev/vg_data/lv_hr
```

`-L` — размер (можно `-l 50%FREE` — процент свободного места). Имя LV сразу становится `/dev/vg_data/lv_hr` и `/dev/mapper/vg_data-lv_hr`.

**4. Расширяем на ходу**

```bash
lvextend -L +500M -r /dev/vg_data/lv_hr
```

`-r` (`--resizefs`) сам вызывает `resize2fs` для ext4 или `xfs_growfs` для XFS. Без `-r` ФС останется старого размера, а `df` не изменится — классическая ошибка. Расширение ext4/XFS на смонтированной ФС безопасно.

```text
Size of logical volume vg_data/lv_hr changed from 1.00 GiB to 1.50 GiB.
Logical volume vg_data/lv_hr successfully resized.
```

**5. Снапшот LVM и откат**

```bash
lvcreate -L 300M -s -n snap_hr_before_upd /dev/vg_data/lv_hr
# ... что-то сломали ...
umount /srv/hr-archive
lvconvert --merge vg_data/snap_hr_before_upd
mount -a
```

`-s` — снапшот, `-L 300M` — сколько изменений он выдержит. `--merge` «отматывает» оригинал на состояние снапшота и удаляет снапшот. Если оригинал смонтирован, слияние откладывается до размонтирования — надёжнее размонтировать руками.

**6. Собираем RAID1**

```bash
mdadm --create /dev/md0 --level=1 --raid-devices=2 /dev/sdb /dev/sdc
cat /proc/mdstat
mdadm --detail /dev/md0 | head -20
```

```text
md0 : active raid1 sdc[1] sdb[0]
      2094080 blocks super 1.2 [2/2] [UU]
      [====>....]  resync = 45.2%
```

`[UU]` — оба диска на месте; `[U_]` — один выпал. `--detail` показывает `State: clean, degraded`, `Active Devices`, `Failed Devices`.

**7. Имитируем отказ и меняем диск**

```bash
mdadm --manage /dev/md0 --fail /dev/sdb --remove /dev/sdb
# физически заменили диск
mdadm --manage /dev/md0 --add /dev/sdb
```

После `--add` пойдёт rebuild. Для больших массивов добавляй `--bitmap=internal` при создании — после сбоя питания пересобирается только изменённое.

**8. Шифруем раздел**

```bash
cryptsetup luksFormat --type luks2 /dev/sdd
cryptsetup luksOpen /dev/sdd crypt_sdd
cryptsetup status crypt_sdd
mkfs.ext4 /dev/mapper/crypt_sdd
```

`status` показывает `type: LUKS2`, `cipher: aes-xts-plain64`, `keysize: 512 bits`, размер сектора. `luksOpen` создаёт `/dev/mapper/crypt_sdd`; для скриптов современный синоним — `cryptsetup open`.

**9. Бэкап заголовка и автоподключение**

```bash
cryptsetup luksHeaderBackup /dev/sdd --header-backup-file /root/luks-sdd-header.img
chmod 400 /root/luks-sdd-header.img
dd if=/dev/urandom of=/root/crypt_sdd.key bs=512 count=8
chmod 400 /root/crypt_sdd.key
cryptsetup luksAddKey /dev/sdd /root/crypt_sdd.key
```

`/etc/crypttab`:

```text
crypt_sdd  UUID=<UUID_LUKS>  /root/crypt_sdd.key  luks,discard,nofail
```

Первое поле — имя в `/dev/mapper`, второе — устройство, третье — ключ (`none` = спрашивать пароль при загрузке), четвёртое — опции. `nofail` обязателен на стенде: без него ВМ зависнет на загрузке в ожидании пароля.

**10. Btrfs: субтомы и снапшоты**

```bash
mkfs.btrfs -L btrfs_lab /dev/sde
mount /dev/sde /mnt/btrfs
btrfs subvolume create /mnt/btrfs/@hr /mnt/btrfs/snapshots
btrfs subvolume snapshot -r /mnt/btrfs/@hr /mnt/btrfs/snapshots/@hr-snap1
btrfs filesystem usage /mnt/btrfs
```

Снапшот делается **только от субтома**, не от корня ФС. `-r` — read-only. `filesystem usage` показывает выделено/использовано; заполнение выше ~85% у CoW-ФС приводит к резкому падению скорости.

**11. ZFS: пул, датасет, снапшот**

```bash
apt install -y zfsutils-linux
zpool create -f tank /var/lib/zfs-lab/disk1.img   # ТОЛЬКО ДЛЯ ЛАБЫ
zfs create tank/hr
zfs snapshot tank/hr@before-update
zfs list -t snapshot
zpool status
zfs rollback -r tank/hr@before-update
```

`zpool status` показывает состояние vdev, ошибки чтения/записи и результат `scrub`. Файл-в-качестве-vdev не даёт отказоустойчивости и в продакшене не применяется никогда — только чтобы потрогать ZFS на стенде.

**12. Диагностика «место кончилось»**

```bash
df -h /srv/hr-archive
df -i /srv/hr-archive
du -xhd1 /srv/hr-archive | sort -h
lsof -nP +L1 | grep hr-archive
```

`df -i` — не кончились ли inode. `du` с `-x` не уходит на другие ФС. `lsof +L1` показывает файлы, удалённые, но всё ещё открытые процессом — место не освобождается, пока процесс держит дескриптор.

**13. Проверяем fstab перед перезагрузкой**

```bash
findmnt --verify
mount -a
systemctl daemon-reload
```

`findmnt --verify` ловит опечатки в `/etc/fstab` до того, как машина не загрузится.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (Ubuntu Server 24.04 LTS, 10.10.10.30): установлен, SSH по ключу, работают `sudo` и пользователь из урока установки ОС. На корневой ФС должно быть ≥ 2 ГБ свободно (под файл-образ для ZFS).
- Ты в нём уже поставил `mdadm`, `lvm2`, `cryptsetup`, `btrfs-progs` (шаг 3).
- **DC01/FS01/WIN11-CL01/WIN10-CL02/ASTRA01** — в работе, но в этой лабораторной не участвуют. PVE01 не нужен.
- Дополнительно (явно, сверх таблицы стенда): к UBU01 добавляются **4 виртуальных диска по 2 ГБ** — `/dev/sdb`, `/dev/sdc`, `/dev/sdd`, `/dev/sde`. Системный `/dev/sda` не трогаем.

### Снапшот

1. Выключи UBU01: `sudo poweroff`.
2. В VMware Workstation для UBU01: **VM → Snapshot → Take Snapshot**, имя `before-sysadmin-027`.
3. Убедись, что снимок сделан **до** добавления дисков: при откате лишние диски исчезнут вместе с изменениями.

### Шаги

1. **UBU01, VMware, при выключенной ВМ.** VM → Settings → Add → Hard Disk → SCSI → Create a new virtual disk → 2 GB, «Store virtual disk as a single file» → Finish. Повторить 4 раза. Включить ВМ, зайти по SSH. Ожидаемо: четыре новых диска в конфигурации.
2. **Проверить, что ядро их увидело.**
   ```bash
   lsblk -o NAME,SIZE,TYPE
   ```
   Ожидаемо: `sdb`, `sdc`, `sdd`, `sde` по 2G, без разделов и ФС.
3. **Поставить пакеты.**
   ```bash
   sudo apt update && sudo apt install -y mdadm lvm2 cryptsetup-bin btrfs-progs zfsutils-linux
   ```
   Ожидаемо: без ошибок; `zfs` может поставить модуль ядра — это нормально.
4. **Собрать RAID1 из sdb и sdc.**
   ```bash
   sudo mdadm --create /dev/md0 --level=1 --raid-devices=2 --bitmap=internal /dev/sdb /dev/sdc
   cat /proc/mdstat
   ```
   Ожидаемо: `[2/2] [UU]`, ресинк от 0 до 100% (на 2 ГБ — секунды).
5. **Закрепить массив в конфиге.**
   ```bash
   sudo mdadm --detail --scan | sudo tee -a /etc/mdadm/mdadm.conf
   sudo update-initramfs -u -k all
   ```
   Ожидаемо: строка `ARRAY /dev/md0 metadata=1.2 ... UUID=...` в файле.
6. **LVM поверх RAID.**
   ```bash
   sudo pvcreate /dev/md0
   sudo vgcreate vg_data /dev/md0
   sudo lvcreate -L 1G -n lv_hr vg_data
   sudo mkfs.ext4 -L hr-archive /dev/vg_data/lv_hr
   sudo mkdir -p /srv/hr-archive
   ```
   Ожидаемо: `pvs`/`vgs`/`lvs` показывают новый PV, VG на ~2 ГБ и LV на 1 ГБ.
7. **Смонтировать и прописать в fstab по UUID.**
   ```bash
   sudo blkid /dev/vg_data/lv_hr      # взять UUID
   echo 'UUID=<вставить>  /srv/hr-archive  ext4  defaults,noatime  0 2' | sudo tee -a /etc/fstab
   sudo mount -a && findmnt --verify
   ```
   Ожидаемо: `/srv/hr-archive` смонтирован, проверка fstab без предупреждений.
8. **Расширить том на ходу.**
   ```bash
   sudo lvextend -L +500M -r /dev/vg_data/lv_hr
   df -h /srv/hr-archive
   ```
   Ожидаемо: размер 1.5G, без размонтирования.
9. **Снапшот LVM и откат.**
   ```bash
   echo 'version=1' | sudo tee /srv/hr-archive/report.txt
   sudo lvcreate -L 300M -s -n snap_hr_before_upd /dev/vg_data/lv_hr
   echo 'СЛОМАНО' | sudo tee /srv/hr-archive/report.txt
   sudo umount /srv/hr-archive
   sudo lvconvert --merge vg_data/snap_hr_before_upd
   sudo mount -a && cat /srv/hr-archive/report.txt
   ```
   Ожидаемо: в файле снова `version=1`.
10. **LUKS на sdd.**
    ```bash
    sudo cryptsetup luksFormat --type luks2 /dev/sdd
    sudo cryptsetup luksOpen /dev/sdd crypt_sdd
    sudo mkfs.ext4 /dev/mapper/crypt_sdd
    sudo mkdir -p /mnt/secure && sudo mount /dev/mapper/crypt_sdd /mnt/secure
    echo 'secret' | sudo tee /mnt/secure/test.txt
    sudo umount /mnt/secure && sudo cryptsetup close crypt_sdd
    sudo cryptsetup luksOpen /dev/sdd crypt_sdd && sudo mount /dev/mapper/crypt_sdd /mnt/secure && cat /mnt/secure/test.txt
    ```
    Ожидаемо: после закрытия и повторного открытия файл на месте.
11. **Бэкап заголовка и ключ-файл.**
    ```bash
    sudo cryptsetup luksHeaderBackup /dev/sdd --header-backup-file /root/luks-sdd-header.img
    sudo dd if=/dev/urandom of=/root/crypt_sdd.key bs=512 count=8
    sudo chmod 400 /root/crypt_sdd.key /root/luks-sdd-header.img
    sudo cryptsetup luksAddKey /dev/sdd /root/crypt_sdd.key
    sudo cryptsetup luksDump /dev/sdd | head -15
    ```
    Ожидаемо: в `luksDump` два keyslot'а (0 и 1), `PBKDF: argon2id`.
12. **Btrfs на sde.**
    ```bash
    sudo mkfs.btrfs -L btrfs_lab /dev/sde
    sudo mount /dev/sde /mnt/btrfs
    sudo btrfs subvolume create /mnt/btrfs/@hr /mnt/btrfs/snapshots
    echo 'data' | sudo tee /mnt/btrfs/@hr/file.txt
    sudo btrfs subvolume snapshot -r /mnt/btrfs/@hr /mnt/btrfs/snapshots/@hr-snap1
    echo 'broken' | sudo tee /mnt/btrfs/@hr/file.txt
    sudo mv /mnt/btrfs/@hr /mnt/btrfs/@hr-broken
    sudo btrfs subvolume snapshot /mnt/btrfs/snapshots/@hr-snap1 /mnt/btrfs/@hr
    cat /mnt/btrfs/@hr/file.txt
    ```
    Ожидаемо: в восстановленном субтоме снова `data`.
13. **ZFS на файловом vdev (учебно).**
    ```bash
    sudo mkdir -p /var/lib/zfs-lab
    sudo truncate -s 1G /var/lib/zfs-lab/disk1.img
    sudo zpool create -f tank /var/lib/zfs-lab/disk1.img
    sudo zfs create tank/hr
    sudo zfs snapshot tank/hr@before-update
    sudo zfs list -t snapshot
    sudo zpool status
    ```
    Ожидаемо: пул `tank` в состоянии `ONLINE`, один снапшот.
14. **Учения «диск умер».**
    ```bash
    sudo mdadm --manage /dev/md0 --fail /dev/sdb --remove /dev/sdb
    cat /proc/mdstat
    sudo mdadm --manage /dev/md0 --add /dev/sdb
    watch cat /proc/mdstat
    ```
    Ожидаемо: сначала `[2/1] [U_]`, затем ресинк и снова `[2/2] [UU]`. Данные в `/srv/hr-archive` доступны всё время.
15. **Записать изменения в `stand-log.md`.**

### Проверка

- [ ] `lsblk` показывает `/dev/md0` типа `raid1`, `/dev/mapper/crypt_sdd` типа `crypt`, ФС на `sde`.
- [ ] `cat /proc/mdstat` — `[2/2] [UU]`, без `degraded`.
- [ ] `vgs` — `vg_data`, `vg_free` около 500 МиБ.
- [ ] `df -h /srv/hr-archive` — 1.5G, заполнено мало.
- [ ] `cat /srv/hr-archive/report.txt` — `version=1` (откат LVM удался).
- [ ] `cryptsetup status crypt_sdd` — `type: LUKS2`, активен.
- [ ] `ls /root/luks-sdd-header.img` — файл существует, права 400.
- [ ] `cat /mnt/btrfs/@hr/file.txt` — `data`.
- [ ] `zpool status` — пул `tank` ONLINE, ошибок 0.
- [ ] `findmnt --verify` — без ошибок.
- [ ] `stand-log.md` обновлён.

### Сломай и почини

```text
Пользователь: petrova (HR, WIN10-CL02)
ВМ:           UBU01 (10.10.10.30), каталог /srv/hr-archive
Что написала: «Скрипт выгрузки опять упал: No space left on device.
              Я удалила старый архив, но место не освободилось. Помогите,
              выгрузка за квартал не уходит.»
Срочность:    высокая, до конца дня
```

**Воспроизведение (UBU01, одна SSH-сессия):**

```bash
dd if=/dev/zero of=/srv/hr-archive/held.bin bs=1M count=600 status=none
exec 3<>/srv/hr-archive/held.bin      # держим файл открытым, как это делает процесс
rm /srv/hr-archive/held.bin           # «удалили» — а место не освободилось
dd if=/dev/zero of=/srv/hr-archive/filler.bin bs=1M count=2000 || true
```

**Диагностика по порядку:**

1. `df -h /srv/hr-archive` → 100%. Значит, дело в месте, а не в правах.
2. `df -i /srv/hr-archive` → inode в норме. Отсекаем вторую классическую причину.
3. `du -xhd1 /srv/hr-archive | sort -h` → суммарно ~200 МиБ, а `df` говорит «занято 1.5G». Расхождение — сигнал.
4. `sudo lsof -nP +L1 | grep hr-archive` → строка с `held.bin (deleted)` и размером 600 МиБ. Вот виновник: файл удалён, но дескриптор открыт.
5. `sudo vgs vg_data` → `VFree` ~500 МиБ. Проверяем, есть ли вообще куда расти.

**Решение:** закрыть дескриптор (в реальной жизни — перезапустить процесс/сервис, который держит файл; `kill -HUP` ротирует лог):

```bash
exec 3>&-
sudo rm /srv/hr-archive/filler.bin
df -h /srv/hr-archive
```

Если бы после освобождения места всё равно не хватало — расширяем том: `sudo lvextend -L +400M -r /dev/vg_data/lv_hr`.

**Ответ пользователю:**

> Мария, добрый день. Место освободили, причина — файл, удалённый из каталога, но всё ещё удерживаемый процессом. Выгрузку можно повторять. Чтобы такого не повторялось, мы настроим пороги оповещения по заполнению `/srv/hr-archive` на 80%. Старые архивы просьба не удалять самостоятельно — есть регламент ротации.

**Закрытие заявки:** категория «Дисковое пространство», приоритет → «Средний», в решении — причина, действие, профилактика (мониторинг).

### Что изменилось на стенде

- UBU01: добавлены 4 виртуальных диска по 2 ГБ — `/dev/sdb`, `/dev/sdc`, `/dev/sdd`, `/dev/sde`.
- UBU01: RAID1 `/dev/md0` (sdb+sdc) с внутренним bitmap, прописан в `/etc/mdadm/mdadm.conf`, initramfs обновлён.
- UBU01: VG `vg_data` на `/dev/md0`, LV `lv_hr` (1.5 ГБ после расширения), ФС ext4 с меткой `hr-archive`, смонтирован в `/srv/hr-archive`, запись в `/etc/fstab` по UUID.
- UBU01: `/dev/sdd` — LUKS2-контейнер, открывается как `/dev/mapper/crypt_sdd`, ФС ext4 в `/mnt/secure`. Бэкап заголовка `/root/luks-sdd-header.img`, ключ-файл `/root/crypt_sdd.key`, второй keyslot добавлен.
- UBU01: `/dev/sde` — Btrfs с субтомами `@hr`, `@hr-broken`, `snapshots/@hr-snap1`, смонтирован в `/mnt/btrfs`.
- UBU01: ZFS-пул `tank` на файловом vdev `/var/lib/zfs-lab/disk1.img`, датасет `tank/hr`, снапшот `@before-update`.
- UBU01: установлены `mdadm`, `lvm2`, `cryptsetup-bin`, `btrfs-progs`, `zfsutils-linux`.
- Пароль стенда `Lab#2026-vektor` использован как парольная фраза LUKS (только для стенда).

### Откат

1. На UBU01: `sudo umount /mnt/secure /mnt/btrfs /srv/hr-archive; sudo cryptsetup close crypt_sdd; sudo zpool export tank; sudo mdadm --stop /dev/md0`.
2. Выключить ВМ: `sudo poweroff`.
3. VMware Workstation → **VM → Snapshot → Snapshot Manager** → выбрать `before-sysadmin-027` → **Go To**. Подтвердить.
4. Включить ВМ и проверить `lsblk`: диски `sdb`–`sde` исчезли, система в состоянии до лабораторной. Если нужен стенд под следующий урок — добавь диски заново по шагу 1.
5. Записать откат в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **`lvextend` без `-r`.** → `df` не меняется, через день повторная заявка «место не появилось». → Всегда `lvextend -L +N -r` либо явный `resize2fs`/`xfs_growfs` и проверка `df`.
2. **`lvreduce` без предварительного сжатия ФС.** → потеря данных и «битая» ФС. → Сначала `umount`, `e2fsck -f`, `resize2fs` до нужного размера, потом `lvreduce`. XFS уменьшать нельзя вообще — только пересоздавать и переносить данные.
3. **Массив не прописан в `/etc/mdadm/mdadm.conf` и initramfs не обновлён.** → после перезагрузки массив собирается как `/dev/md127`, fstab по UUID не находит ФС, сервер падает в emergency mode. → `mdadm --detail --scan >> /etc/mdadm/mdadm.conf` + `update-initramfs -u -k all`.
4. **Монтирование по `/dev/sdX`.** → добавили диск, буквы сдвинулись, смонтировалось не то. → Только UUID (`blkid`), для LVM — `/dev/mapper/vg-lv`, для md — `/dev/md0`.
5. **Снапшот считают бэкапом.** → умер диск — умерли и оригинал, и снапшот на том же диске. → Снапшот — инструмент отката на минуты/часы; бэкап лежит на другом носителе и проверяется восстановлением.
6. **Не следят за заполнением LVM-снапшота.** → `snap_percent` 100% → снапшот инвалидируется, откат невозможен. → Мониторинг `lvs -o snap_percent`, размер снапшота ≥ ожидаемого объёма изменений.
7. **LUKS без бэкапа заголовка.** → повреждённый заголовок = потеря всех данных, мастер-ключ не восстановить. → `luksHeaderBackup` сразу после `luksFormat`, копия на отдельном носителе.
8. **RAID5 на дешёвых SATA-дисках без BBU и мониторинга.** → «write hole» и вторая смерть при ресинке. → RAID10 под БД, RAID6/RAIDZ2 под архивы, обязательный алерт на `degraded`.

**Безопасность.** Шифрование диска — прямое требование при работе с персональными данными (152-ФЗ) и в защищённых контурах `[ПРОВЕРИТЬ: конкретные пункты приказов ФСТЭК для твоего случая]`. Ключевой файл на незашифрованном корне обесценивает LUKS: кто получил диск — получил и ключ. В бою ключ хранят на отдельном носителе, в TPM/Vault/HSM или вводят вручную при загрузке. Отдельно помни про **swap**: если он не зашифрован, туда могут утечь расшифрованные данные из памяти. Опция `discard` на LUKS ускоряет SSD, но выдаёт, какие блоки используются — на ноутбуках и в чувствительных системах её отключают и делают периодический `fstrim`. И на стенде пароль один на всё — в реальной работе так делать нельзя.

## 7. Вопросы на собеседовании

**1. Чем LVM-снапшот отличается от снапшота Btrfs/ZFS?**

LVM-снапшот — отдельный том фиксированного размера, куда по мере изменения оригинала копируются старые блоки (copy-on-write на уровне блоков). Он резервирует место заранее, переполнение инвалидирует снапшот, а изменения в снапшоте нужно потом «мержить» обратно (`lvconvert --merge`). В Btrfs/ZFS снапшот — это неизменяемая ссылка на уже существующие блоки: места почти не занимает, создаётся мгновенно, но и «места под изменения» не резервирует; удаление снапшота освобождает блоки, на которые больше никто не ссылается. Откат у них — это либо `zfs rollback`, либо переключение субтома/датасета, а не слияние.

*Что хочет услышать интервьюер:* понимание, что это разные механизмы с разной ценой и разными рисками. *Красный флаг:* «это одно и то же, просто команды разные».

**2. Что произойдёт, если LVM-снапшот переполнится?**

Снапшот станет невалидным (`Invalid snapshot`), откатиться на него нельзя, а в некоторых сценариях это влияет и на работу оригинала — том помечается как использующий невалидный снапшот, дальнейшие снапшоты с него не сделать. Лечится удалением снапшота и повторным созданием с адекватным размером; для критичных сценариев используют thin-пулы, где место расходуется по факту.

*Что хочет услышать интервьюер:* что кандидат считает размер снапшота и мониторит `snap_percent`. *Красный флаг:* «ничего не будет».

**3. RAID5 или RAID10 под базу данных? Что такое write hole?**

Под БД — RAID10: запись без вычисления чётности, предсказуемая latency, быстрый rebuild, потеря двух дисков из разных зеркал не убивает массив. RAID5 даёт больше ёмкости, но каждая запись — read-modify-write с чётностью, что бьёт по IOPS. Write hole — ситуация, когда при сбое питания часть stripe обновилась, а чётность нет: после восстановления массив отдаёт несогласованные данные, и узнать об этом без контрольных сумм нельзя. Отсюда BBU/NVRAM-кэш, а в ZFS аналог решён через CoW и RAIDZ.

*Что хочет услышать интервьюер:* связь уровня RAID с профилем нагрузки и понимание, что RAID ≠ бэкап. *Красный флаг:* «RAID5 всегда, он же дешевле».

**4. Как зашифруете диск и что сделаете в первую очередь после создания LUKS-контейнера?**

`cryptsetup luksFormat --type luks2 /dev/sdX`, затем **сразу** `cryptsetup luksHeaderBackup` — без заголовка мастер-ключ не восстановить, и данные превращаются в мусор. Дальше: `luksOpen`, `mkfs`, запись в `/etc/crypttab` (ключ или `none`), запись ФС в `/etc/fstab` по UUID с `nofail`, добавление второго keyslot'а как резервного, шифрование swap. Проверка — перезагрузка стенда и `cryptsetup status`.

*Что хочет услышать интервьюер:* что человек не забудет про заголовок и про загрузку. *Красный флаг:* сразу `luksFormat`, дальше по памяти.

**5. `df` показывает 100%, а `du` — 40%. Твои действия?**

Расхождение означает, что место занимает что-то, чего `du` не видит. Порядок: `df -i` (inode), `du -xhd1` по каталогам, затем `lsof -nP +L1` — удалённые, но открытые файлы; проверить скрытые точки монтирования (`findmnt`), не поверх ли смонтирован другой раздел; в контейнерах — не перекрывает ли `/var/lib/docker` или overlay; проверить, не заняли ли место снапшоты LVM (`lvs -o snap_percent`) и не в другом ли пространстве имён процесс. Решение — перезапустить держателя файла либо расширить том, но сначала найти причину.

*Что хочет услышать интервьюер:* структурный подход и знание про open-but-deleted. *Красный флаг:* «сразу добавлю диск».

## 8. Источники (официальная документация)

- **kernel.org** — раздел `Documentation/admin-guide/` (md, device-mapper, dm-crypt). Поиск: `kernel.org md.rst raid admin guide`.
- **man7.org** — страницы `man mdadm`, `man lvm`, `man cryptsetup-luksformat`, `man btrfs-subvolume`. Поиск: `man7 cryptsetup luksFormat`.
- **ubuntu.com** — Server documentation, раздел про storage / LVM / encryption. Поиск: `ubuntu server docs lvm`.
- **openzfs.github.io/openzfs-docs** — Getting Started, `zpool` и `zfs` man pages, RAIDZ. Поиск: `openzfs docs zpoolconcepts`.
- **btrfs.readthedocs.io** — Sysadmin Guide, Subvolumes, Snapshotting. Поиск: `btrfs sysadmin guide subvolume snapshot`.
- **gitlab.com/cryptsetup/cryptsetup** — README и FAQ по LUKS2, keyslots, argon2id. Поиск: `cryptsetup LUKS2 on-disk format specification`.
- **docs.redhat.com** — RHEL «Managing storage devices» (LVM, stratis, LUKS). Поиск: `rhel managing storage devices lvm`.
- Точные версии пакетов в Ubuntu 24.04 (cryptsetup, OpenZFS, btrfs-progs) — `[ПРОВЕРИТЬ]` через `apt policy <пакет>` на самой UBU01.
