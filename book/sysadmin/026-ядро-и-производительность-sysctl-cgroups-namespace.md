# Ядро и производительность: sysctl, cgroups, namespaces, профилирование, perf, strace

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4712.** Пользователь: Сидоров Алексей (склад). ВМ: UBU01. Текст: «Захожу по SSH на ubu01, запускаю скрипт выгрузки накладных `/opt/vektor/bin/sklad-export.sh` — висит минут двадцать, раньше укладывался в две. Zabbix с утра завалил алертами про load average. Смена закрывается в 18:00, помогите». Срочность: высокая.

Дальше начинается то, чем системный администратор отличается от «человека с sudo». Нужно за десять минут ответить на три вопроса: сервер реально загружен работой или ждёт диск; какой процесс виноват и в каком именно системном вызове он торчит; и что надо поменять — параметр ядра, лимит в cgroup или сам код скрипта. `top` на все три вопроса не отвечает.

После этой главы вы будете: понимать, где кончается user space и начинается ядро; менять параметры ядра через sysctl так, чтобы они выжили перезагрузку; читать cgroups v2 руками и через systemd, ограничивая юнит по памяти, CPU и I/O; понимать, из чего собран контейнер (namespaces + cgroups + rootfs) и уметь зайти внутрь чужого namespace; снимать профиль через `perf` и трассировку системных вызовов через `strace`, не убивая при этом прод. Это ровно тот набор, который на собеседовании отделяет «админа L2» от «инженера, которому можно отдать прод».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Всё, что вы видите в `top`, `free` и `df`, — это не «железо», а цифры, которые ядро посчитало и положило в `/proc` и `/sys`. Отсюда главная мысль: производительность Linux — это работа с виртуальной файловой системой ядра и с механизмами, которые этой ФС управляют.

```text
┌─────────────────────────── USER SPACE ───────────────────────────┐
│  bash   nginx   sklad-export.sh   sshd   systemd (PID 1)         │
│                    │  glibc / libc                                 │
└────────────────────┼────────── syscall (openat, read, mmap...) ───┘
                     ▼
┌─────────────────────────── KERNEL ──────────────────────────────┐
│  VFS │ планировщик CPU │ MM (page cache, swap) │ net │ block    │
│  cgroups v2 (учёт и лимиты)   namespaces (изоляция)             │
│  /proc/sys ← sysctl          /sys/fs/cgroup ← cgroup            │
└──────────────────────────────────────────────────────────────────┘
                     ▼
              диски / сеть / CPU (VT-x в VMware)
```

**sysctl** — это ручки ядра. Значение параметра живёт в `/proc/sys/...`, читается и пишется через `sysctl`, а постоянно сохраняется файлами в `/etc/sysctl.d/*.conf`, которые применяет `systemd-sysctl.service` при загрузке. Часть параметров глобальные, часть — «на пространство имён» (например, `net.*` свои в каждом net namespace, поэтому контейнер видит свои `somaxconn`).

**cgroups (control groups) v2** — механизм учёта и ограничения ресурсов. В cgroup v2 одна иерархия (в v1 их было по одной на контроллер), и всё лежит в `/sys/fs/cgroup`. systemd — единственный, кто эту иерархию создаёт: каждый юнит (`ssh.service`, `user.slice`, `docker-...`) — это каталог с файлами. Ключевые контроллеры: `cpu` (`cpu.max` — квота в формате «сколько микросекунд на период», `cpu.weight` — приоритет), `memory` (`memory.max` — жёсткий лимит, `memory.high` — мягкий, заставляющий ядро притормаживать процесс, `memory.current`, `memory.events` со счётчиком `oom_kill`), `io` (`io.max` — лимит по IOPS/байтам на устройство), `pids` (`pids.max` — защита от fork-бомбы).

**namespaces** — изоляция видимости. Восемь типов в современном ядре: `mnt` (точки монтирования), `pid` (свой набор PID, первый процесс становится PID 1), `net` (интерфейсы, маршруты, iptables/nftables), `uts` (hostname), `ipc` (очереди SysV), `user` (свой маппинг UID, позволяет rootless-контейнеры), `cgroup` (своя view иерархии), `time` (смещение часов загрузки, 5.6+). Контейнер = namespaces + cgroups + rootfs + capabilities/seccomp. Именно поэтому `ps` внутри контейнера показывает один процесс: это не магия, а pid namespace.

**Профилирование.** Два принципиально разных подхода. Сэмплирование (`perf`) — периодически снимаем стек и смотрим, где чаще оказывались; накладные расходы единицы процентов, годится для прода. Трассировка (`strace`) — перехват *каждого* системного вызова через ptrace; накладные расходы десятки-сотни процентов, годится для отладки одного процесса. Плюс метрики: `vmstat`, `pidstat`, `iostat` (пакет sysstat) и PSI — Pressure Stall Information, `/proc/pressure/{cpu,memory,io}`, насколько задачи *стояли в очереди* за ресурсом. PSI — самый честный ответ на «сервер тормозит?».

Отдельно запомните разницу: **load average** в Linux = задачи в runnable-состоянии (`R`) + в непрерываемом сне (`D`, обычно ждёт диск). Поэтому LA=40 на 8 ядрах может означать не «CPU вскипел», а «диск не отдаёт данные». Портов и сетевых протоколов в этой теме нет — это слой ОС, а не сети; сеть разбираем в уроке 28.

## 3. Аналогия из жизни

Представьте не завод, а офисный центр. **Ядро** — это комендант здания и его служба эксплуатации: он выдаёт ключи, свет, воду, лифты. Жильцы (**user space**) не могут сами включить воду — только подать заявку (**системный вызов**), и вот `strace` — это журнал всех заявок с таймстампами и временем исполнения: видно, что кондиционер выключился, пока сантехник полчаса шёл.

**sysctl** — это щиток с ручками в подвале: давление воды, температура бойлера. Крутить можно, но если перекрутить — потоп, и соседние арендаторы это почувствуют.

**cgroups** — счётчики и автоматические предохранители по каждому арендатору: отделу склада выделено 256 МБ и полставки уборщицы, больше он не отожмёт, даже если очень захочет. **namespaces** — это то, что у каждого отдела свой номерной фонд и своя табличка с названием: склад сидит в кабинете 101 и искренне уверен, что 101 — единственный кабинет в мире; из коридора же видно, что «101 у отдела склада» — не то же самое, что «101 у бухгалтерии».

Аналогия ломается в одном месте: комендант-человек может проигнорировать заявку или срезать угол, а ядро ведёт себя строго по коду — поэтому «непонятное торможение» всегда имеет объяснение в цифрах, надо только знать, где смотреть.

## 4. Практика и команды

### 4.1. sysctl: чтение, запись, постоянство

```bash
sysctl vm.swappiness net.core.somaxconn fs.inotify.max_user_watches
# vm.swappiness = 60
# net.core.somaxconn = 4096
# fs.inotify.max_user_watches = 65536

sudo sysctl -w vm.swappiness=10            # только до перезагрузки
echo 'vm.swappiness = 10' | sudo tee /etc/sysctl.d/99-vektor-perf.conf
sudo sysctl --system                        # применить все файлы
sudo sysctl -p /etc/sysctl.d/99-vektor-perf.conf   # только один файл
```

`-a` — вывести всё (сотни строк), `-w` — записать, `-p` — прочитать файл, `--system` — пройти по `/etc/sysctl.d`, `/run/sysctl.d`, `/usr/lib/sysctl.d`, `/etc/sysctl.conf` в этом порядке (побеждает прочитанное позже). Смотрите на: `vm.swappiness` (агрессивность свопа), `net.core.somaxconn` (длина очереди accept — при 4096 трогать обычно нечего), `fs.inotify.max_user_watches` (растёт, когда IDE/бэкап «перестал видеть» файлы), `net.ipv4.ip_local_port_range` (кончились порты у прокси), `kernel.pid_max`.

### 4.2. Проверить, какая версия cgroups

```bash
stat -fc %T /sys/fs/cgroup      # cgroup2fs  → унифицированная v2 (Ubuntu 24.04)
cat /sys/fs/cgroup/cgroup.controllers
# cpuset cpu io memory hugetlb pids rdma misc
```

Если вернулось `tmpfs` — у вас гибрид, часть контроллеров в v1, файлы лежат иначе.

### 4.3. vmstat: первая картинка за 10 секунд

```bash
vmstat 1 5
# procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
#  r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
# 14  0      0 812344  21044 912336    0    0     2    18  980 4210 61 12 27  0  0
```

`r` — очередь к CPU (сравнивайте с `nproc`), `b` — заблокированные (ждут диск), `si/so` — своп реально шевелится (плохо), `cs` — переключения контекста, `wa` — ожидание I/O, `st` — steal (ваш гипервизор отнял CPU). Если `r` >> `nproc` — CPU; если `b` > 0 и `wa` растёт — диск.

### 4.4. sysstat: кто именно ест ресурс

```bash
sudo apt install -y sysstat
pidstat 1 3                       # по процессам: %usr %sys %iowait
mpstat -P ALL 1 3                 # по ядрам: одно ядро в 100% — признак однопоточного кода
iostat -xz 1 3                    # -x расширенно, -z без пустых устройств
```

В `iostat` смотрите `r_await/w_await` (мс на операцию; десятки — диск в полке), `aqu-sz` (очередь), `%util` (для SSD почти бесполезен — врут из-за параллелизма).

### 4.5. PSI: насколько все *ждали*

```bash
cat /proc/pressure/cpu /proc/pressure/io /proc/pressure/memory
# some avg10=42.11 avg60=18.03 avg300=6.71 total=918234512
# full avg10=3.02  avg60=1.11  avg300=0.40 total=61234512
```

`some` — хотя бы одна задача стояла, `full` — вся очередь стояла (только для memory/io). Значение > 20 по `some` стабильно — пора разбираться.

### 4.6. systemd-cgtop и systemd-cgls

```bash
systemd-cgtop -m -b --iterations=1     # -m память, -c CPU, -i I/O, -b неинтерактивно
systemd-cgls /system.slice              # дерево юнитов в slice
```

### 4.7. Лимит на существующий юнит

```bash
sudo systemctl set-property ssh.service MemoryMax=200M CPUQuota=50%
systemctl show ssh.service -p MemoryMax -p CPUQuotaPerSecUSec
# MemoryMax=209715200
# CPUQuotaPerSecUSec=500ms
sudo systemctl set-property --runtime ssh.service MemoryMax=infinity   # снять на время
```

Без `--runtime` правка «залипает» в `/etc/systemd/system.control/` и переживает перезагрузку. Откат — `systemctl revert ssh.service`.

### 4.8. Разовый запуск в своей cgroup

```bash
sudo systemd-run --unit=vektor-test --slice=vektor.slice \
  -p MemoryMax=256M -p CPUQuota=50% \
  /usr/bin/stress-ng --vm 1 --vm-bytes 512M --timeout 60s
journalctl -u vektor-test -n 20
```

### 4.9. Читаем cgroup руками

```bash
C=/sys/fs/cgroup/vektor.slice/vektor-test.service
cat $C/memory.current $C/memory.max $C/memory.peak
cat $C/memory.events          # low high max oom oom_kill — счётчики
cat $C/cpu.stat                # usage_usec, nr_throttled, throttled_usec
cat $C/cpu.max $C/pids.current
```

Если `nr_throttled` растёт — процесс не «медленный», его душит квота.

### 4.10. Кто в каких namespace

```bash
lsns                            # таблица: NS TYPE NPROCS PID USER COMMAND
sudo ls -l /proc/$$/ns          # mnt pid net uts ipc user cgroup time
sudo lsns -t net -o NS,PID,COMMAND
```

### 4.11. unshare и nsenter

```bash
sudo unshare --pid --fork --mount-proc --uts bash
hostname lab-ns && ps -ef       # внутри: вы PID 1, видите только себя
exit
sudo nsenter -t <PID> -m -u -i -n -p   # зайти в namespace чужого процесса
```

### 4.12. Сетевой namespace руками

```bash
sudo ip netns add lab-ns
sudo ip -n lab-ns link set lo up
sudo ip netns exec lab-ns ip addr      # только lo, ни одного адреса
sudo ip netns pids lab-ns
sudo ip netns del lab-ns
```

### 4.13. strace: что процесс делает на самом деле

```bash
sudo strace -c -f -o /tmp/st.txt /opt/vektor/bin/sklad-export.sh
# % time     seconds  usecs/call     calls    errors syscall
#  98.20   41.234567     4123456        10           connect
#   1.10    0.460000        4600       100           openat
sudo strace -f -tt -T -y -e trace=openat,connect,read,write -p <PID>
```

`-c` — сводка по времени в вызовах (лучший первый шаг), `-f` — с дочерними, `-tt` — таймстампы, `-T` — сколько длился вызов, `-y` — печатать путь к файлу вместо fd, `-e trace=` — фильтр, `-o` — в файл. Если `strace` внезапно отдаёт `Operation not permitted`, проверьте `sysctl kernel.yama.ptrace_scope` (по умолчанию 1 — можно трассировать только своих потомков), а на Ubuntu 24.04 добавьте политику AppArmor для unprivileged ptrace `[ПРОВЕРИТЬ]`; запуск через `sudo` снимает большинство случаев.

### 4.14. perf: сэмплирование вместо трассировки

```bash
sudo apt install -y linux-tools-common linux-tools-generic
perf --version
sudo perf stat -p <PID> -- sleep 5           # IPC, cycles, context-switches
sudo perf top -g                             # живой профиль по символам
sudo perf record -F 99 -g -p <PID> -- sleep 10
sudo perf report --stdio --sort comm,dso,symbol | head -40
sudo perf trace -p <PID>                     # как strace, но дешевле
```

`-F 99` — 99 выборок в секунду, `-g` — собирать стеки (нужно для flame graph). Смотрите на `kernel.perf_event_paranoid` (2 в большинстве сборок, бывает выше) и `kernel.kptr_restrict`. В виртуалке на VMware аппаратные счётчики PMU часто недоступны — тогда `perf` работает на software-событиях; включение счётчиков в свойствах ВМ `[ПРОВЕРИТЬ]`.

### 4.15. Лимиты: prlimit и limits.conf

```bash
prlimit --pid <PID>                                  # текущие лимиты процесса
sudo prlimit --pid <PID> --nofile=65536:65536        # на живом процессе
cat /etc/security/limits.d/90-vektor.conf
# *  soft nofile 65536
# *  hard nofile 65536
```

Важно: `limits.conf`/`limits.d` применяется через PAM при логине и **не действует** на systemd-юниты — там нужен `LimitNOFILE=` в unit-файле. Для nginx/1С это классическая причина «too many open files» после перезапуска.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны: **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS, доступ по SSH, локальная учётка с `sudo` из трека L1) и **MON01** (10.10.10.40, Zabbix) — если агент Zabbix на UBU01 уже стоит (ставили в треке L2), в конце проверим алерт; если агента нет, шаг 11 пропускаем. Windows-машины и DC01 не нужны. Стенд L2 должен быть в рабочем состоянии.

```bash
# на UBU01 проверить готовность
uname -r            # 6.8.0-xx-generic
nproc               # сколько ядер видит Ubuntu
free -h && df -h /
```

### Снапшот

На хосте VMware Workstation, при выключенной или включённой ВМ (снапшот можно делать на работающей): выделить **UBU01** → меню VM → Snapshot → Take Snapshot → имя `before-sysadmin-026`. Аналогично для **MON01**, если будете менять в нём что-то кроме чтения.

Из командной строки хоста (Windows, `vmrun.exe` из папки VMware Workstation):

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "C:\Users\<user>\Documents\Virtual Machines\UBU01\UBU01.vmx" before-sysadmin-026
```

### Шаги

1. **Записать состояние стенда.** На хосте создать/дополнить `stand-log.md` строкой: «дата, урок 26, снапшот before-sysadmin-026 на UBU01/MON01».
2. **Снять baseline на UBU01.**
   ```bash
   mkdir -p ~/lab26 && cd ~/lab26
   { uptime; nproc; vmstat 1 5; free -m; cat /proc/pressure/cpu; cat /proc/pressure/io; } | tee baseline.txt
   ```
   Ожидаемо: LA около 0, `r` = 0–1, PSI `avg10` близко к 0. Сохраните файл — потом сравните.
3. **Создать «виновника» — тот самый скрипт выгрузки.**
   ```bash
   sudo mkdir -p /opt/vektor/bin
   sudo tee /opt/vektor/bin/sklad-export.sh >/dev/null <<'EOF'
   #!/bin/bash
   # Учебный «скрипт выгрузки накладных»
   for i in $(seq 1 10); do
     curl -s --connect-timeout 60 http://10.10.20.99/api/export?id=$i >/dev/null
   done
   EOF
   sudo chmod +x /opt/vektor/bin/sklad-export.sh
   ```
   `10.10.20.99` — заведомо несуществующий адрес в DMZ-подсети, поэтому каждый `curl` будет висеть до таймаута. Это наша искусственная «поломка» из тикета.
4. **Воспроизвести жалобу.** В одном терминале:
   ```bash
   time /opt/vektor/bin/sklad-export.sh
   ```
   В другом, параллельно: `vmstat 1 5` и `ps -eo pid,stat,%cpu,cmd | grep curl`. Ожидаемо: скрипт висит, процесс в состоянии `S` (sleep), CPU почти не ест, LA почти не растёт. Вывод: **это не CPU**, и «load average 40» из тикета — отдельная история.
5. **Смоделировать вторую проблему — реальную нагрузку CPU.**
   ```bash
   sudo apt install -y stress-ng
   stress-ng --cpu $(nproc) --timeout 60s &
   vmstat 1 5; pidstat 1 3; mpstat -P ALL 1 3
   ```
   Ожидаемо: `r` ≈ `nproc`, `us` ~100%, LA быстро растёт. Сравните с baseline — теперь вы видите разницу между «ждёт I/O» и «считает».
6. **sysctl: посмотреть, изменить, закрепить.**
   ```bash
   sysctl vm.swappiness fs.inotify.max_user_watches net.core.somaxconn
   sudo tee /etc/sysctl.d/99-vektor-perf.conf >/dev/null <<'EOF'
   # Стенд «Вектор», урок 26. Учебные значения, не для прода.
   vm.swappiness = 10
   fs.inotify.max_user_watches = 524288
   EOF
   sudo sysctl --system
   sysctl vm.swappiness fs.inotify.max_user_watches
   ```
   Ожидаемо: оба значения применились. Перезагружать пока не нужно, но запомните: файл в `/etc/sysctl.d` — это то, что переживёт ребут, а `sysctl -w` — нет.
7. **cgroups: ограничить «бэкап» по памяти и CPU.**
   ```bash
   sudo systemd-run --unit=vektor-backup-test --slice=vektor.slice \
     -p MemoryMax=256M -p CPUQuota=50% \
     /usr/bin/stress-ng --vm 1 --vm-bytes 512M --timeout 60s
   systemd-cgtop -m -b --iterations=1
   C=/sys/fs/cgroup/vektor.slice/vektor-backup-test.service
   cat $C/memory.max $C/memory.current $C/memory.events $C/cpu.max
   journalctl -u vektor-backup-test -n 30
   ```
   Ожидаемо: `memory.events` покажет `oom_kill 1` — процесс, которому нужно 512 МБ, убит в пределах 256 МБ. Это и есть демонстрация лимита: ядро не «тормозит» его, а убивает.
8. **cgroups: мягкое ограничение CPU.**
   ```bash
   sudo systemd-run --unit=vektor-cpu-test --slice=vektor.slice \
     -p CPUQuota=25% bash -c 'while :; do :; done'
   C=/sys/fs/cgroup/vektor.slice/vektor-cpu-test.service
   sleep 5; cat $C/cpu.stat; cat $C/cpu.max
   sudo systemctl stop vektor-cpu-test
   ```
   Ожидаемо: `nr_throttled` и `throttled_usec` растут, `cpu.max` = `25000 100000`. Процесс «живой, но медленный» — типичная жалоба «после ваших лимитов всё еле шевелится».
9. **namespaces: убедиться, что изоляция — это про видимость.**
   ```bash
   sudo unshare --pid --fork --mount-proc --uts bash
   hostname lab-ns; ps -ef; echo $$; exit
   lsns -t pid -o NS,PID,COMMAND | head
   sudo ip netns add lab-ns
   sudo ip netns exec lab-ns ip addr
   sudo ip netns del lab-ns
   ```
   Ожидаемо: внутри pid-namespace `ps -ef` показывает один-два процесса, hostname изменился только внутри, а в net namespace есть только `lo`. Хост при этом не изменился.
10. **strace: найти, где именно висит скрипт.**
    ```bash
    sudo strace -c -f -o /tmp/st-sum.txt /opt/vektor/bin/sklad-export.sh
    cat /tmp/st-sum.txt
    sudo timeout 15 strace -f -tt -T -y -e trace=connect -o /tmp/st-connect.txt /opt/vektor/bin/sklad-export.sh
    head -20 /tmp/st-connect.txt
    ```
    Ожидаемо: в сводке `connect` съедает почти всё время, а в трассировке видно `connect(... sin_port=htons(80), sin_addr=inet_addr("10.10.20.99"))` с `<60.000123>` в конце строки. Диагноз готов: процесс ждёт TCP-соединения, а не считает.
11. **perf: посмотреть, куда уходит CPU у нормального процесса.**
    ```bash
    sudo apt install -y linux-tools-common linux-tools-generic
    sudo perf stat /usr/bin/stress-ng --cpu 2 --timeout 5s
    sudo perf top -g --delay 2     # посмотреть 10 секунд, выйти по q
    sudo perf record -F 99 -g -- sleep 5
    sudo perf report --stdio --sort comm,dso | head -30
    ```
    Ожидаемо: `perf stat` покажет `task-clock`, `context-switches`, `instructions per cycle` (IPC); в `perf report` вверху `stress-ng` и функции ядра. Сравните профиль с baseline-периодом — на пустой системе вверху будет `swapper`/idle.
12. **Починить скрипт и перемерить.**
    ```bash
    sudo sed -i 's/--connect-timeout 60/--connect-timeout 3/' /opt/vektor/bin/sklad-export.sh
    time /opt/vektor/bin/sklad-export.sh
    ```
    Ожидаемо: скрипт отрабатывает за ~30 секунд вместо 600 (10 попыток × 3 с), ошибаясь, но быстро. Теперь `strace -c` покажет уже не 98% в `connect`, а нормальное распределение.
13. **Проверить Zabbix (если агент есть).** На MON01 → Monitoring → Latest data / Problems по хосту UBU01: убедиться, что триггер по load average погас после остановки `stress-ng`, а триггер по памяти не сработал (лимит cgroup убивает процесс, но не «съедает» память хоста).
14. **Записать итог в `stand-log.md`**: создан `/opt/vektor/bin/sklad-export.sh`, файл `/etc/sysctl.d/99-vektor-perf.conf`, slice `vektor.slice` (создан systemd автоматически, живёт до перезагрузки или удаления юнитов), установлен `stress-ng`; временные юниты `vektor-backup-test`, `vektor-cpu-test` остановлены.

### Проверка

- [ ] `sysctl vm.swappiness` возвращает 10 и после `systemctl restart systemd-sysctl`.
- [ ] Файл `/etc/sysctl.d/99-vektor-perf.conf` существует и содержит оба параметра.
- [ ] `stat -fc %T /sys/fs/cgroup` возвращает `cgroup2fs`.
- [ ] В `/sys/fs/cgroup/vektor.slice/vektor-backup-test.service/memory.events` счётчик `oom_kill` больше нуля.
- [ ] В `cpu.stat` юнита `vektor-cpu-test` `nr_throttled` > 0.
- [ ] `unshare --pid` внутри показывает `ps -ef` из 1–2 строк, hostname `lab-ns`.
- [ ] `strace -c` показывает `connect` первым по времени до правки скрипта и не первым — после.
- [ ] `perf stat` отработал без ошибок доступа (или вы зафиксировали в логе, что PMU в ВМ недоступен).
- [ ] `systemctl list-units 'vektor*'` не содержит активных юнитов.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Заявка #4719.** Пользователь: Сидоров Алексей (склад). ВМ: UBU01. Текст: «После твоих настроек скрипт выгрузки вообще не работает: запускается и через пять секунд "Killed". Вчера хоть висел, а теперь падает. Мне отчёт к закрытию смены нужен». Срочность: высокая.

**Как воспроизвести.** Вы (якобы «оптимизируя») поставили юниту выгрузки слишком маленький лимит:

```bash
sudo systemd-run --unit=vektor-export --slice=vektor.slice \
  -p MemoryMax=64M -p CPUQuota=50% \
  /opt/vektor/bin/sklad-export.sh
```

Скрипт (в учебной версии — `curl`, в реальной — парсер Excel/CSV) требует больше 64 МБ и получает OOM внутри cgroup.

**Диагностика по шагам.**

1. `systemctl status vektor-export` → `Active: failed (Result: signal)`, `status=9/KILL`. Уже половина ответа: не ошибка скрипта, а его убили.
2. `journalctl -u vektor-export -n 50 --no-pager` → строка `A process of this unit has been killed by the OOM killer.`
3. `sudo dmesg -T | grep -i -E 'oom|killed process' | tail` → `Memory cgroup out of memory: Killed process ... (curl|bash)`. Ключевое слово `Memory cgroup` — убил не хостовый OOM, а лимит группы.
4. `C=/sys/fs/cgroup/vektor.slice/vektor-export.service; cat $C/memory.events $C/memory.peak $C/memory.max` → `oom_kill 1`, `memory.peak` упёрся в `memory.max`. Причина подтверждена.
5. `systemd-cgtop -m -b --iterations=1` во время работы — видно, как `memory.current` подползает к потолку.
6. Проверить, не течёт ли процесс сам: запустить без лимита и смотреть `memory.current` каждые 5 секунд. Если растёт монотонно — проблема в коде (утечка), лимит был лишь детектором.

**Решение.** Поднять лимит до измеренного потребления с запасом ×1.5–2 (например, `MemoryMax=512M`), оставив `CPUQuota` как есть; убедиться, что `oom_kill` больше не растёт. Если выяснилось, что процесс течёт — завести отдельную задачу разработчику и оставить лимит как страховку от «съедания» всего сервера.

```bash
sudo systemctl set-property vektor-export.service MemoryMax=512M
systemctl show vektor-export.service -p MemoryMax
sudo systemctl restart vektor-export
```

**Ответ пользователю:** «Алексей, добрый день. Причина — ограничение памяти, которое я поставил на процесс выгрузки: ему нужно около 400 МБ, а лимит был 64 МБ, поэтому система его принудительно завершала. Лимит поднял до 512 МБ, скрипт отработал штатно за N минут. Если снова увидите "Killed" — напишите, посмотрим на утечку памяти в самом скрипте».

**Закрытие.** В тикете — причина, действие, проверка; в `stand-log.md` — новое значение `MemoryMax` у юнита; в заметках — что лимиты cgroup всегда выставляются по измеренному профилю, а не «на глаз».

### Что изменилось на стенде

- UBU01: создан каталог `/opt/vektor/bin` и скрипт `/opt/vektor/bin/sklad-export.sh` (учебный, с таймаутом 3 с).
- UBU01: создан `/etc/sysctl.d/99-vektor-perf.conf` (`vm.swappiness=10`, `fs.inotify.max_user_watches=524288`).
- UBU01: установлен пакет `stress-ng`; опционально `sysstat`, `linux-tools-common`, `linux-tools-generic`.
- UBU01: существует slice `vektor.slice` и (возможно) юнит `vektor-export.service` с `MemoryMax=512M` — дальнейшие главы могут на него ссылаться как на «сервис выгрузки склада».
- UBU01: в `stand-log.md` записан снапшот `before-sysadmin-026`.
- MON01: изменений нет (только чтение).

### Откат

Если что-то сломалось сильнее, чем хотелось (например, вы поменяли `vm.swappiness` на боевом узле с базой или уронили sshd лимитом): выключить ВМ → в VMware Workstation правой кнопкой по снапшоту `before-sysadmin-026` → Snapshot Manager → Revert. Из CLI:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "C:\Users\<user>\Documents\Virtual Machines\UBU01\UBU01.vmx" before-sysadmin-026
```

Частичный откат без снапшота: `sudo rm /etc/sysctl.d/99-vektor-perf.conf && sudo sysctl --system` (вернутся значения по умолчанию после ребута), `sudo systemctl revert vektor-export.service`, `sudo systemctl stop 'vektor-*' && sudo systemctl reset-failed 'vektor-*'`. Не забудьте поправить `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **`sysctl -w` и надежда, что «сохранится».** → После перезагрузки всё откатывается, инцидент возвращается, а причина «непонятна». → Правильно: только файл в `/etc/sysctl.d/` + `sysctl --system` + проверка после `systemctl restart systemd-sysctl`.
2. **Копипаст «тюнинга» из интернета (`vm.overcommit_memory=2`, `net.ipv4.tcp_tw_reuse`, `kernel.pid_max=4194304`, отключение `swappiness=0`).** → На сервере с 1С или СУБД это даёт отказы в выделении памяти и «загадочные» падения под нагрузкой. → Правильно: менять по одному параметру, под записанную метрику, с планом отката.
3. **Лимиты cgroup «на глаз».** → `MemoryMax=100M` для процесса, которому нужно 400 МБ: тишина в логах, «Killed», паника у пользователя. → Правильно: сначала измерить `memory.peak` под реальной нагрузкой, потом ставить лимит с запасом, и смотреть `memory.events`.
4. **Путать `memory.max` и `memory.high`.** → Ждут «мягкого торможения», получают OOM-kill. → Правильно: `memory.high` — throttle, `memory.max` — жёсткая стена; для продавления пиков лучше `memory.high` + `memory.max` как аварийный предохранитель.
5. **`strace -f` на проде «на минуточку».** → Процесс замедляется в разы, таймауты, каскадный отказ, а на многопоточном приложении ещё и `-f` даёт гигабайты лога. → Правильно: `strace -c` на коротком интервале, `timeout 10 strace ...`, фильтр `-e trace=`, сначала `perf` (дешевле), и никогда — на сервисе, который уже деградирует.
6. **`perf record` без `-g` и без ограничения по времени.** → Профиль бесполезен (нет стеков) и файл `perf.data` растёт сотнями мегабайт. → Правильно: `-F 99 -g`, всегда с `-- sleep N` или по PID.
7. **`limits.conf` для демона.** → «Прописал nofile 65536, а nginx всё равно падает с too many open files». → Правильно: `LimitNOFILE=` в unit + `systemctl daemon-reload` + перезапуск; проверять через `prlimit --pid`.
8. **Бесконтрольный `nsenter` в чужой namespace.** → Зашёл в network namespace контейнера, поправил маршрут — и сломал сетевую связность сервиса, при этом изменения не переживут пересоздание контейнера. → Правильно: понимать, что namespace-правки эфемерны; менять конфигурацию на уровне оркестратора/юнита.

### Безопасность

В боевой среде `sysctl`-правки и лимиты — это изменение конфигурации, а не «разовая команда»: они проходят через change management, с записанным «до/после» и окном отката. `sysctl kernel.perf_event_paranoid=-1` и `kernel.yama.ptrace_scope=0` открывают любому локальному пользователю чтение памяти чужих процессов (пароли, токены, ключи) — на учебном стенде допустимо, в проде это инцидент ИБ. Давать `sudo strace`/`perf` на прод-сервере незнакомому подрядчику — фактически давать доступ ко всем секретам в памяти процессов. Отключение `kernel.kptr_restrict` ради красивых символов в `perf report` — тоже снижение защищённости (упрощает эксплуатацию ядерных уязвимостей). На стенде «Вектор» мы сознательно ставим слабые учебные пароли и работаем под `sudo` — в реальной компании для этого нужны именные учётки, MFA и журнал аудита.

## 7. Вопросы на собеседовании

**1. «Load average 40 на сервере с 8 ядрами. Что это значит и что вы сделаете?»**
Ответ: LA в Linux считает задачи в состоянии `R` (runnable) **и** в непрерываемом сне `D`, поэтому 40 — это либо жёсткий дефицит CPU, либо очередь к диску/сети. Действую так: `nproc` для контекста → `vmstat 1` (смотрю `r`, `b`, `wa`, `si/so`) → `pidstat 1` (кто именно) → `iostat -xz 1` (r_await, aqu-sz) → `/proc/pressure/*` (что реально pressure). Если `r` >> nproc и `us` ~100% — CPU; если `b` > 0 и высокий `wa`/`r_await` — I/O, дальше смотрю `iotop`/`perf` по стекам, вижу ли процессы в `D` на конкретной ФС. Отдельно проверяю `st` — если он большой, проблема не в моей ВМ, а в гипервизоре.
*Что хочет услышать интервьюер:* что вы знаете про `D`-состояние и не сводите LA к CPU.
*Красный флаг:* «Load average 40 — значит сервер загружен на 500%, перезагружаю».

**2. «Чем cgroup v2 отличается от v1 и как systemd с ним работает?»**
Ответ: v1 — много независимых иерархий, по одной на контроллер, процесс мог быть в разных местах разных деревьев, из-за чего учёт путался. v2 — единая иерархия в `/sys/fs/cgroup`, контроллеры распределяются через `cgroup.controllers`/`cgroup.subtree_control`, жёсткое правило вложенности. systemd — единственный владелец иерархии: каждый юнит и slice — каталог с файлами, `MemoryMax`/`CPUQuota`/`IOReadBandwidthMax` пишутся в `memory.max`/`cpu.max`/`io.max`. Полезно помнить про PSI (`memory.pressure` внутри cgroup) и `memory.events` с `oom_kill` — по ним видно, что лимит реально сработал.
*Что хочет услышать:* понимание, что cgroup v2 — одна иерархия и что управляет ею systemd, а не «руками в /sys».
*Красный флаг:* «v2 — это то же самое, только файлы по-другому называются».

**3. «Как узнать, что контейнер — это не виртуалка? Что внутри у него есть, а чего нет?»**
Ответ: контейнер — это изолированный процессами ядра: namespaces (mnt, pid, net, uts, ipc, user, cgroup, time) + cgroups для лимитов + rootfs (overlayfs и т.п.) + capabilities/seccomp/AppArmor. Ядро у контейнера — хостовое, своего ядра нет, `uname -r` покажет хостовое; нет своего загрузчика и нельзя перезагрузить «контейнерную ОС». Проверить можно так: `lsns -p <PID>`, `cat /proc/<PID>/cgroup`, `nsenter -t <PID> -n ip addr`. Практическое следствие: sysctl вида `vm.*` и `kernel.*` из контейнера менять нельзя (они общие), а `net.*` — можно, они per-netns.
*Что хочет услышать:* что вы не путаете изоляцию с виртуализацией и знаете границы настройки.
*Красный флаг:* «Контейнер — это лёгкая виртуалка со своим ядром».

**4. «Процесс жрёт 100% CPU. Как найдёте причину: strace или perf? Обоснуйте.»**
Ответ: сначала `perf`. Если процесс «считает», а не «ждёт», `strace -c` покажет почти нулевое время в системных вызовах и десятки тысяч вызовов — это тупиковый путь, а накладные расходы ptrace сделают только хуже. `perf top`/`perf record -F 99 -g` + `perf report` дадут профиль по символам и стекам за единицы процентов overhead, и я увижу функцию/библиотеку. Если по профилю видно, что время в ядре (`[kernel.kallsyms]`) и в конкретных syscall-обёртках — тогда уже точечно `strace -c -f -e trace=<подозреваемое>`. Для «висит и ничего не делает» — наоборот, `strace` первым: он покажет, в каком вызове блокировка.
*Что хочет услышать:* что вы понимаете разницу overhead и выбираете инструмент под симптом.
*Красный флаг:* «Всегда первым делом strace -f, он же всё показывает».

**5. «Вы поставили приложению MemoryMax=512M. Оно работает, но иногда получает SIGKILL. Ваши действия?»**
Ответ: подтвердить, что убивает именно cgroup, а не хостовый OOM: `dmesg -T | grep -i 'memory cgroup out of memory'`, `journalctl -u <unit>` с «killed by the OOM killer», `cat memory.events` (`oom_kill`), `memory.peak` против `memory.max`. Дальше: посмотреть профиль потребления во времени (`memory.current` каждые несколько секунд, `systemd-cgtop -m`) — если пик кратковременный, поднять `MemoryMax`, либо поставить `memory.high` ниже максимума, чтобы ядро не убивало, а притормаживало и отдавало page cache. Если потребление монотонно растёт — это утечка, лимит лишь вскрыл её; эскалировать разработке. И зафиксировать в конфиге юнита, а не «руками на живом».
*Что хочет услышать:* диагностика по cgroup-файлам + различение «пик» / «утечка» + правка через unit.
*Красный флаг:* «Поставлю MemoryMax=infinity, чтобы не падало» — без анализа причины.

## 8. Источники (официальная документация)

- **docs.kernel.org** — раздел «Control Group v2» (администрирование cgroups, файлы `memory.*`, `cpu.*`, `io.*`); раздел «Namespaces» в Documentation; поиск: `cgroup v2 documentation memory.max`, `linux namespaces documentation`.
- **kernel.org** — Documentation → admin-guide → `sysctl` (полный список параметров ядра с описанием); поиск: `kernel sysctl vm.swappiness documentation`.
- **man7.org** — man-страницы: `cgroups(7)`, `namespaces(7)`, `sysctl(8)`, `sysctl.d(5)`, `strace(1)`, `perf(1)`, `prlimit(1)`, `psi(2)`; поиск: `man 7 cgroups`, `man 1 strace`.
- **freedesktop.org / systemd.io** — man-страницы `systemd.resource-control(5)`, `systemd-run(1)`, `systemd-cgtop(1)`, `systemd.slice(5)`; поиск: `systemd.resource-control MemoryMax`.
- **perf.wiki.kernel.org** — официальный wiki по perf (tutorial, `perf report`, flame graphs); поиск: `perf tutorial kernel wiki`.
- **brendangregg.com** — метод USE (Utilization/Saturation/Errors) и раздел про Linux performance tools; поиск: `Brendan Gregg USE method Linux`.
- **ubuntu.com** — Server documentation, раздел про cgroup v2 и sysctl (особенности поведения в Ubuntu 24.04); поиск: `Ubuntu 24.04 cgroup v2 server documentation`. Отдельно: список изменений Ubuntu 24.04 по ограничениям unprivileged user namespaces и ptrace `[ПРОВЕРИТЬ]` — на момент написания политика AppArmor менялась, сверяйтесь с release notes.
- **Пакеты и доступность из РФ:** `linux-tools-*`, `sysstat`, `stress-ng` — свободные пакеты из репозиториев Ubuntu, проблем с получением нет; при закрытом зеркале используйте локальное зеркало (тема урока 32).
