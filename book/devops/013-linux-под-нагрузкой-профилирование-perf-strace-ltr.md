# Linux под нагрузкой: профилирование (perf, strace, ltrace), анализ памяти, диска, сети, OOM-killer

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди:

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Внутренний портал http://web01.corp.vektor.lab с утра открывается по 20 секунд. Остальные сайты летают. Уже и браузер перезапускал!»
> **Срочность:** средняя

Портал — это `WEB01` в DMZ. Пользователь видит «медленно», а ты должен за пять минут превратить «медленно» в конкретную цифру: 100% CPU, или своп, или диск в `%util 100`, или сеть с ретрансмитами. Дальше — в конкретный PID и конкретный системный вызов.

Это одна из самых частых работ дежурного DevOps. Приложения «тормозят» не потому, что «сервер плохой», а потому что кто-то забыл лимит памяти, кто-то написал `while true` в кроне, а кто-то выставил `worker_processes 64` на двух ядрах. Наниматель платит DevOps именно за это: не «перезагрузить и стало легче», а «вот причина, вот доказательство, вот фикс».

После главы ты будешь уметь:

- читать load average и не путать её с загрузкой CPU;
- снимать профиль CPU через `perf` и понимать, где горячо;
- ловить виновника по системным вызовам (`strace`) и библиотечным вызовам (`ltrace`);
- отличать утечку памяти от нормального page cache и находить OOM-kill в логах ядра;
- задавать лимиты памяти через systemd/cgroup v2 и делать так, чтобы OOM убивал нагрузочный тест, а не nginx;
- собирать вывод в отчёт в Git, чтобы через месяц было чем доказать свои слова.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ресурсы Linux, которые нас интересуют, — четыре: **CPU**, **память**, **диск**, **сеть**. Всё остальное — способы посмотреть на них.

Ключевой рубеж — системный вызов (syscall): граница между user space и ядром. Приложение не работает с диском напрямую, оно просит `read()`/`write()`/`openat()`, а ядро уже решает, попадёт ли запрос в page cache или уйдёт на устройство.

```text
        user space (nginx, java, stress-ng)
                      │  syscall: read() openat() mmap() sendto()
                      ▼
┌─────────────────────── ядро Linux ───────────────────────┐
│  VFS ── page cache ── I/O scheduler ── драйвер ── диск   │  ← iostat, iotop, /proc/pressure/io
│  планировщик CFS ── CPU ── PMU (счётчики)                │  ← perf, vmstat, pidstat
│  mm (страницы, RSS, swap) ── memory cgroup               │  ← free, smem, /proc/meminfo, OOM
│  net stack: socket → TCP → NIC                          │  ← ss, nstat, sar -n DEV
└──────────────────────────────────────────────────────────┘
        /proc /sys /sys/fs/cgroup  ← откуда всё это читается
                      ▲
             ptrace (strace/ltrace)  ← может остановить процесс
             eBPF (bpftrace, bcc)    ← живёт в ядре, почти без тормозов
```

Классы инструментов:

| Класс | Примеры | Цена | Что даёт |
|---|---|---|---|
| Счётчики | `vmstat`, `free`, `iostat`, `ss` | почти ноль | агрегаты из `/proc`, `/sys` |
| Сэмплеры | `perf top/record` | низкая | «где CPU проводит время», выборка стека |
| Трассировщики | `strace`, `ltrace` | **высокая** | каждый syscall / вызов библиотеки по отдельности |

**Load average** (`/proc/loadavg`) — это не «загрузка CPU в процентах». Это среднее число процессов, которые готовы к работе (R) **или ждут I/O в непрерываемом сне (D)** за 1/5/15 минут. Поэтому load 40 при 4 ядрах и `%id` 80 может означать не «CPU захлёбывается», а «двадцать процессов висят в D-state на медленном NFS или на сетевом хранилище». В VM на VMware Workstation добавь колонку `st` в `vmstat` — stolen time, время, которое гипервизор забрал у тебя.

**PSI (Pressure Stall Information)** — самый недооценённый инструмент. `/proc/pressure/cpu`, `memory`, `io` показывают, **сколько времени задачи реально стояли из-за нехватки ресурса**. Формат: `some avg10=... avg60=... avg300=... total=...` (микросекунды). `some` — часть задач стоит, `full` (для memory/io) — стоят все. Ноль везде — ресурс не при делах, даже если цифра выглядит страшной.

**Память.** `free` показывает `buff/cache` — это не «занято», это кеш, который отдадут при первой нужде. Смотреть надо на `available`. Плохие признаки: `available` близко к нулю, растёт `si/so` в `vmstat` (swap in/out), растёт `memory.pressure`. Утечка отличается от кеша тем, что RSS конкретного процесса монотонно растёт и не падает после сброса кеша.

**OOM-killer** — механизм ядра, который убивает процесс, когда памяти не хватает и освободить её другими способами нельзя. Ядро не выбирает случайно: у каждого процесса есть `oom_score` (растёт с потреблением памяти) и `oom_score_adj` от −1000 до +1000 (ручная поправка). Кто толще и без «пропуска» — того и убивают. В cgroup v2 (Ubuntu 24.04 по умолчанию) есть **memcg OOM**: при превышении `memory.max` ядро убивает процесс **внутри cgroup**, не трогая остальной хост. Именно так работают лимиты в Kubernetes — не зря `requests/limits` в манифесте это те же ручки cgroup.

**Диск.** `%util` и `await` в `iostat` — твои друзья. `await` — среднее время отдачи запроса (мс), `aqu-sz` — очередь. Растёт `await` при низком `%util` — классика виртуальной машины с тонким диском. Отдельная ловушка: файл удалён, а место не освободилось, потому что процесс держит дескриптор — ловится через `lsof +L1`.

**Сеть** разобрана в уроке 9. Здесь — только то, что важно под нагрузкой: счётчики ошибок и ретрансмит (не путать с «пропускной способностью»).

## 3. Аналогия из жизни

Ты — дежурный врач в больнице «Сервер». Приходит пациент: «Мне плохо, я торможу». Сначала медсестра снимает базовые показатели — давление, пульс, температуру: это `vmstat`, `free`, `df`, `ss`. Дёшево, быстро, без проникновения. Если картина непонятна — отправляешь на МРТ: аппарат делает снимки всего тела, не режет и не останавливает пациента, только смотрит его сотни раз в секунду. Это `perf`.

Если и это не объясняет — делаешь эндоскопию: трубку внутрь, и врач видит буквально каждый глоток. Это `strace`: он реально останавливает процесс на каждый системный вызов, поэтому процедура неприятная и замедляет пациента в 10–100 раз. `ltrace` — микроскоп на отдельные клетки: показывает не «сколько раз процесс сходил в ядро», а «сколько раз он дёрнул библиотечную функцию».

А `OOM-killer` — это санитар, который приходит в переполненную палату и выносит на улицу самого тяжёлого пациента. Он не советуется с врачом.

Аналогия перестаёт работать в одном месте: санитар в больнице смотрит на вес пациента, а ядро — ещё и на твой `oom_score_adj`, то есть на бумажку «не трогать, он тут самый важный», которую ты забыл приклеить к сервису.

## 4. Практика и команды

### 4.1. `uptime` и `vmstat` — базовые показатели

```bash
uptime
# 10:12:03 up 3 days,  2:14,  1 user,  load average: 2.41, 1.90, 0.88

vmstat 1 5
# procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
#  r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
#  3  0      0 412304  28116 902144    0    0     4    38  512 1020 48  4 47  1  0
```

Что смотреть: `r` — сколько задач ждут CPU (сравнивай с числом ядер); `b` — заблокированные (D-state); `si/so` — своп; `wa` — ожидание I/O; `st` — stolen в виртуалке.

### 4.2. `free` и `/proc/meminfo`

```bash
free -m
#                total        used        free      shared  buff/cache   available
# Mem:            1987         612         198           4         1176         1150
# Swap:           2047          12        2035
```
Ключевая колонка — `available`. `free` = 198 МБ при `available` = 1150 МБ — это нормально и хорошо.

### 4.3. PSI — «стояли или не стояли»

```bash
cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
# some avg10=12.40 avg60=3.10 avg300=0.80 total=18432015
```
Если `io some avg10` растёт — узкое место диск, а не CPU. Золото для дежурного.

### 4.4. `pidstat` — кто именно

```bash
pidstat -u -r -d 1 3
# UID  PID   %usr %system  %CPU   RSS  Command
# 0    1204  98.0    2.0   100.0  1840  stress-ng-cpu
# 0    1205   0.0   12.0    12.0 524288  stress-ng-vm
# PID  kB_rd/s kB_wr/s Command
# 1206   0.0   10240.0  stress-ng-hdd
```
Три режима сразу: `-u` CPU, `-r` RSS, `-d` дисковый I/O на процесс. Ставится пакетом `sysstat`.

### 4.5. `iostat` и `iotop` — диск

```bash
iostat -xz 1 3
# Device  r/s   w/s  rkB/s  wkB/s  aqu-sz  await  %util
# sda    0.40  42.00   6.00  840.00   2.10  48.5  99.8
```
`%util` ~100 + `await` в десятках мс = диск захлёбывается. `iotop -oPa` покажет конкретный процесс (`-o` только активные, `-P` процессы, `-a` накопленные байты).

### 4.6. `df -i` и `lsof +L1` — неочевидное место на диске

```bash
df -i /
lsof +L1 | head
# nginx   1204 root  5w REG  8,1 4821315 0t0 /var/log/nginx/access.log (deleted)
```
Inodes кончились или файл удалён, но держится дескриптором — `df` покажет 100%, а `du` — совсем другие цифры.

### 4.7. `perf` — профиль CPU

```bash
perf --version
sudo perf stat -p 1204 -- sleep 5
sudo perf top -p 1204          # интерактивно, q — выход
sudo perf record -F 99 -g -p 1204 -- sleep 10
sudo perf report --stdio | head -40
```
`-F 99` — 99 сэмплов в секунду (удобно: 1% ≈ один сэмпл), `-g` — собирать стеки вызовов (без него не будет флейм-графа для `flamegraph.pl`). Если в виртуалке `perf stat` пишет «not supported» — аппаратные счётчики (PMU) не проброшены; используй software-события: `perf stat -e task-clock,context-switches,page-faults,cycles -p PID`. Права проверяются `sysctl kernel.perf_event_paranoid` — на стенде можно понизить до 2, в бою это ослабление защиты ядра `[ПРОВЕРИТЬ значение по умолчанию в вашем дистрибутиве]`.

### 4.8. `strace` — системные вызовы

```bash
strace -c ls -l /etc
# % time     seconds  usecs/call     calls    errors syscall
#  31.20    0.001790          44        40           openat
#  22.10    0.001266           3       402           read
#  0.00     0.000000           0        12         1 statx

sudo strace -f -tt -T -e trace=network -p 1204
# 10:14:02.113455 sendto(3, "...", 64, 0, ...) = 64 <0.000021>
```
`-c` — сводка (смотри `errors` и `% time`), `-f` — следить за дочерними, `-tt` — время, `-T` — длительность вызова, `-e trace=` — фильтр. Замедление на боевом сервисе — реальное, не миф.

### 4.9. `ltrace` — библиотечные вызовы

```bash
cat >/tmp/demo.c <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void){ for(int i=0;i<5;i++){ char*p=malloc(128); strcpy(p,"vektor"); free(p);} printf("ok\n"); return 0;}
EOF
gcc -O0 -g -o /tmp/demo /tmp/demo.c
ltrace -c /tmp/demo
# % time     seconds  usecs/call     calls      function
#  ...                    1         strlen
#  ...                    5         malloc
#  ...                    5         free
```
Работает только для динамически слинкованных C/C++ бинарников через PLT. Go и Rust-бинарники (статически слинкованные) так не посмотришь — там уже `perf` и eBPF.

### 4.10. OOM-killer: где искать и как управлять

```bash
sudo dmesg -T | grep -i -E 'out of memory|killed process'
# [Wed Jan 14 10:20:41 2026] Out of memory: Killed process 1205 (stress-ng-vm) total-vm:1052672kB, anon-rss:524288kB

cat /proc/1205/oom_score /proc/1205/oom_score_adj
sudo echo -500 | sudo tee /proc/$(pgrep -f nginx | head -1)/oom_score_adj
```
Логи «кто кого убил» — только ядро, поэтому `dmesg` / `journalctl -k`. В journald то же самое доступно через `journalctl -k --grep='Killed process'`.

### 4.11. Лимит памяти через systemd/cgroup v2

```bash
sudo systemd-run --unit=memtest -p MemoryMax=256M -p MemorySwapMax=0 \
  /usr/bin/stress-ng --vm 1 --vm-bytes 512M --timeout 60s
systemctl show memtest -p Result        # Result=oom-kill  [ПРОВЕРИТЬ в вашей версии systemd]
cat /sys/fs/cgroup/system.slice/memtest.service/memory.events
# low 0
# high 0
# max 12
# oom 1
```
`memory.events` в cgroup v2 — прямой счётчик: `oom 1` означает ровно то, что означает. Это же числом покажет, что лимит в Kubernetes-подe сработал.

### 4.12. Сеть под нагрузкой

```bash
ss -s
ss -tanp state established | head
nstat -az | grep -iE 'TcpRetransSegs|TcpExtTCPLostRetransmit|ListenOverflows'
sar -n DEV 1 3
```
Под нагрузкой важны не только байты, но и `ListenOverflows` (очередь accept переполнена) и ретрансмиты (твой TCP теряет пакеты).

### 4.13. Если рядом окажется Windows (для полноты картины)

```powershell
Get-Counter '\Memory\Available MBytes','\Processor(_Total)\% Processor Time' -SampleInterval 2 -MaxSamples 5
```
Аналог `perf`/`vmstat` в Windows — Performance Monitor (`perfmon`) и Sysinternals Process Explorer; полноценного аналога `strace` нет, ближайшее — Process Monitor. Для DevOps-трека это «знать, что существует», а не рабочий инструмент.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- `UBU01` (10.10.10.30, Ubuntu Server 24.04) — включена, SSH работает (урок 12), у твоего локального пользователя есть `sudo`.
- `WEB01` (10.10.20.10, nginx) — включена. Доступ с `UBU01` идёт через `GW01` (10.10.20.1), поэтому понадобится `ssh -J`. Если маршрутизации до DMZ нет — сначала проверь маршрут и nftables на `GW01` (урок 9) либо выполни шаги 6–7 локально на `UBU01`, разницы для профилирования нет.
- `MON01` (10.10.10.40, Zabbix) — опционально: после лабы посмотри исторические графики по `UBU01`, если агент уже стоит.
- Windows-часть стенда может отсутствовать — эта лаба её не требует.

### Снапшот

В VMware Workstation на `UBU01` и `WEB01`: правый клик по ВМ → **Snapshot → Take Snapshot…** → имя `before-devops-013`. Для чистоты `UBU01` лучше выключить (Power Off) перед снапшотом; `WEB01` можно снимать на ходу. Оба снапшота — с одним и тем же именем.

### Шаги

1. **Заходим на `UBU01` и снимаем точку отсчёта.**
   ```bash
   ssh <user>@10.10.10.30
   uptime; free -m; df -h /; ss -s; cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
   ```
   *Ожидаемо:* load average близка к нулю, `available` — сотни мегабайт свободных, PSI почти нулевой. Запиши эти цифры: это baseline, с ним будешь сравнивать.

2. **Ставим инструменты.**
   ```bash
   sudo apt update
   sudo apt install -y linux-tools-common linux-tools-generic stress-ng sysstat \
     strace ltrace htop iotop smem
   perf --version; strace -V; iostat -V
   ```
   *Ожидаемо:* три строки с версиями. Если `perf` ругается «perf not found for kernel ...» — поставь `linux-tools-$(uname -r)` или запусти бинарь из `/usr/lib/linux-tools-*/perf` `[ПРОВЕРИТЬ путь в вашей версии]`.

3. **Грузим CPU и смотрим агрегаты.**
   ```bash
   stress-ng --cpu 2 --timeout 180s &
   sleep 3; vmstat 1 5; uptime
   ```
   *Ожидаемо:* `r` ≈ 2–3, `us` близко к 100 суммарно, `wa` низкий, load average растёт к 2.

4. **Находим виновника по PID.**
   ```bash
   pidstat -u -r -d 1 3
   ps -eo pid,rss,comm --sort=-rss | head
   ```
   *Ожидаемо:* строки `stress-ng-cpu` с `%CPU` ~100 и умеренным RSS.

5. **Снимаем профиль `perf`.**
   ```bash
   PID=$(pgrep -n stress-ng-cpu)
   sudo perf stat -p $PID -- sleep 5
   sudo perf record -F 99 -g -p $PID -- sleep 10
   sudo perf report --stdio | head -40
   ```
   *Ожидаемо:* `perf stat` выдаёт цифры (или честно сообщает, что hardware-события недоступны в виртуалке — тогда повтори с `-e task-clock,context-switches,page-faults`). В отчёте — горячая функция вроде `sqrt`/`cos` из стрессора.

6. **Смотрим системные вызовы.**
   ```bash
   strace -c ls -l /etc
   sudo strace -c -p $PID -- sleep 5
   sudo strace -f -tt -T -e trace=openat,read,write -p $PID -- sleep 3 2>&1 | head -20
   ```
   *Ожидаемо:* таблица `calls/errors/% time`. У `ls` будут `openat` и `newfstatat`; у стрессора — минимум вызовов (он весь в user space), и это само по себе диагностический вывод.

7. **Пробуем `ltrace`.**
   ```bash
   ltrace -c /tmp/demo      # собери /tmp/demo из раздела 4.9
   ```
   *Ожидаемо:* список `malloc/free/strcpy/printf` с количеством вызовов. На `nginx`/Go-бинарниках этот номер не пройдёт — запомни почему.

8. **Диск под нагрузкой.**
   ```bash
   stress-ng --hdd 1 --hdd-bytes 512M --timeout 120s &
   iostat -xz 1 3
   sudo iotop -oPa -n 2
   cat /proc/pressure/io
   pidstat -d 1 2
   ```
   *Ожидаемо:* `%util` растёт к 60–100, `await` в десятках мс, `io some avg10` уходит с нуля, `iotop` показывает `stress-ng-hdd`.

9. **Провоцируем OOM внутри cgroup.**
   ```bash
   sudo systemd-run --unit=memtest -p MemoryMax=256M -p MemorySwapMax=0 \
     /usr/bin/stress-ng --vm 1 --vm-bytes 512M --timeout 60s
   systemctl status memtest --no-pager
   sudo dmesg -T | tail -20 | grep -i -E 'oom|killed process'
   cat /sys/fs/cgroup/system.slice/memtest.service/memory.events
   ```
   *Ожидаемо:* юнит завершается, в `dmesg` — `Memory cgroup out of memory: Killed process ...`, в `memory.events` — `oom 1`. Хост при этом жив, потому что убийство произошло внутри cgroup.

10. **Смотрим на память глазами ядра и `smem`.**
    ```bash
    grep -E 'MemAvailable|Cached|AnonPages|SwapTotal' /proc/meminfo
    sudo smem -r -t | head
    ```
    *Ожидаемо:* большая часть «занятой» памяти — кеш, а `PSS` в `smem` честнее показывает распределение по процессам `[ПРОВЕРИТЬ ключи -r/-t в вашей версии smem]`.

11. **Оформляем результат в Git.**
    ```bash
    cd ~/vektor-devops        # твой репозиторий из урока 5
    mkdir -p docs
    $EDITOR docs/13-linux-under-load.md
    git add docs/13-linux-under-load.md && git commit -m "lab 13: linux under load profilers cheat sheet" && git push
    ```
    *Ожидаемо:* в файле таблица «симптом → инструмент → что смотреть» + твои цифры до/после нагрузки.

### Проверка

- [ ] `perf --version`, `strace -V`, `iostat -V` отвечают без ошибок
- [ ] под `stress-ng --cpu 2` в `pidstat` видно процессы с `%CPU ≈ 100`
- [ ] `perf report --stdio` содержит хотя бы одну осмысленную функцию (не только `[unknown]`)
- [ ] `strace -c` даёт таблицу с колонкой `errors`
- [ ] `iostat -xz` при `--hdd` показывает `%util > 60`
- [ ] `/proc/pressure/io` был нулевым до и стал ненулевым во время диска
- [ ] `dmesg` содержит строку про OOM, `memory.events` — `oom 1`
- [ ] `docs/13-linux-under-load.md` закоммичен, `stand-log.md` обновлён

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Портал http://web01.corp.vektor.lab открывается 20 секунд, вчера было 2. Остальные сайты нормально.»
> **Срочность:** средняя

**Как воспроизвести поломку.** На `WEB01` (через `ssh -J <user>@10.10.10.3 <user>@10.10.20.10`) запусти «утечку» без лимита:

```bash
sudo systemd-run --unit=hog /usr/bin/stress-ng --vm 1 --vm-bytes 900M --vm-keep --timeout 900s
```

**Диагностика по порядку:**

1. С `UBU01` проверяем тайминги: `curl -o /dev/null -s -w 'connect=%{time_connect} ttfb=%{time_starttransfer} total=%{time_total}\n' http://10.10.20.10/`. Быстрый `connect` + огромный `ttfb` → проблема не в сети, а на сервере.
2. На `WEB01`: `uptime` и `vmstat 1 5` — высокий `r`/`b`, `si/so` не в нуле.
3. `cat /proc/pressure/memory` — `full avg10` растёт, значит ровно память.
4. `ps -eo pid,rss,comm --sort=-rss | head` — сверху `stress-ng-vm` с подозрительным RSS.
5. `sudo dmesg -T | grep -i 'killed process'` — видим, что OOM уже убил воркер nginx (или сам nginx).
6. `systemctl status nginx --no-pager` — `failed` / в цикле рестартов.

**Решение:**

```bash
sudo systemctl stop hog
sudo systemctl reset-failed nginx
sudo systemctl start nginx
curl -o /dev/null -s -w 'ttfb=%{time_starttransfer} total=%{time_total}\n' http://10.10.20.10/
```

**Профилактика** (в бою, а не на стенде — обязательно): добавить в `/etc/systemd/system/nginx.service.d/limits.conf`

```ini
[Service]
MemoryHigh=256M
MemoryMax=384M
OOMScoreAdjust=-500
```

`systemctl daemon-reload && systemctl restart nginx`. `OOMScoreAdjust=-500` делает nginx менее привлекательной жертвой, а лимит переносит расплату на cgroup, а не на весь хост. И алерт в `MON01` на `memory.pressure` + `swap used`.

**Ответ пользователю:** «Портал снова работает, причина — фоновый процесс на сервере портала, который съел всю память. Перезапустили сервис и ограничили потребление памяти, чтобы не повторилось. Если снова заметите задержки — пишите сразу, приложите время.»

**Закрытие заявки:** категория «Linux / производительность», в решении — ссылка на `docs/13-linux-under-load.md`, время реакции/решения, пометка «профилактика запланирована».

### Что изменилось на стенде

- `UBU01`: установлены `sysstat`, `stress-ng`, `strace`, `ltrace`, `htop`, `iotop`, `smem`, `linux-tools-*`; появлялся временный юнит `memtest` (сам удалился после завершения).
- `WEB01`: временный юнит `hog` — остановлен; `nginx` перезапущен; (опционально) добавлен drop-in с лимитами памяти.
- В Git-репозитории появился файл `docs/13-linux-under-load.md`.
- `stand-log.md`: строка с датой, перечнем пакетов и выводом OOM-эксперимента.

### Откат

VMware Workstation → правый клик по `UBU01`/`WEB01` → **Snapshots → Snapshot Manager** → выбрать `before-devops-013` → **Revert**. Важно: если репозиторий с отчётом лежит **внутри** ВМ, сначала сделай `git push`, иначе коммит исчезнет вместе со снапшотом. Точечный откат без снапшота: `sudo systemctl stop hog memtest; sudo systemctl reset-failed hog memtest; sudo systemctl restart nginx`.

## 6. Ошибки новичков и безопасность

1. **`strace -f` на боевом нагруженном сервисе.** → Замедление в разы, таймауты у пользователей. → Сначала `perf`/`pidstat`, `strace` — точечно, короткими окнами (`-- sleep 3`), лучше на копии трафика.
2. **`free` показывает 200 МБ — «память кончилась!»** → паника, рестарт, потеря кеша. → Смотри `available`, PSI и `si/so`.
3. **Высокая load average = «CPU виноват».** → Оптимизируешь код, а там D-state на диске. → Всегда проверяй `b`/`wa`/`%iowait` и `/proc/pressure/io`.
4. **`perf` в виртуалке без проверки событий.** → «not supported», вывод пустой, делаешь ложный вывод «профилировать нельзя». → Проверь PMU, переключись на software-события.
5. **Убить «самый жирный» процесс после OOM, не читая `dmesg`.** → Убил невиновного, а утечка осталась. → Сначала `dmesg | grep -i 'killed process'`, потом решение.
6. **Поднять `MemoryMax` «чтобы не убивало».** → Проблема переезжает на уровень хоста, теперь падает всё. → Ищи причину роста RSS, лимит ставь по реальному потреблению + запас.
7. **Отключить swap «для производительности».** → OOM приходит раньше и жёстче. → Осознанный выбор: либо swap как амортизатор, либо `MemorySwapMax=0` **для конкретного сервиса**, а не глобально.
8. **Проверять на `du`, что место освободилось.** → Врёт при удалённых открытых файлах и при inode-исчерпании. → `lsof +L1` и `df -i`.

### Безопасность

- `kernel.perf_event_paranoid` и `ptrace_scope` — это **защитные механизмы**. Понижать их на боевой машине ради удобства нельзя: `ptrace` позволяет читать память процессов, включая секреты и ключи.
- `strace`/`ltrace` на сервере с секретами может вывести в лог токены и пароли из аргументов syscall. Логи с трассировкой — потенциальная утечка, хранить их как обычные логи нельзя.
- `dmesg` может содержать адреса памяти ядра — на боевых системах `kernel.dmesg_restrict=1` ставят не просто так.
- `stress-ng` на общей машине = DoS для коллег. На стенде — можно, в проде — только в изолированной среде и по согласованию.

## 7. Вопросы на собеседовании

**1. Load average 40, ядер 4, но `top` показывает 80% idle. Что происходит?**

Ответ: load average в Linux считает не только R (runnable), но и D (uninterruptible sleep) — процессы, застрявшие в ожидании I/O (диск, NFS, сетевые блочные устройства). Если CPU свободен, а load высокий — смотри `vmstat`: колонка `b`, `wa`, и `/proc/pressure/io`. Дальше `pidstat -d`, `iostat -xz`, `dmesg` на предмет «task blocked for more than 120 seconds». Часто виновник — сетевой диск или деградировавшее хранилище в виртуалке.

*Что хочет услышать интервьюер:* что ты знаешь про D-state и не путаешь load с утилизацией CPU.
*Красный флаг:* «надо добавить ядер» без единой проверки.

**2. Как найти процесс, который грузит диск, и как отличить «диск медленный» от «приложение глупое»?**

Ответ: `pidstat -d 1`, `iotop -oPa`, `/proc/<pid>/io`. Дальше `iostat -xz`: высокий `%util` + высокий `await` = железо/хранилище; низкий `%util` при высоком `await` — очередь на уровне гипервизора; высокий `w/s` при маленьком размере записи — приложение делает много мелких операций (лечится батчингом/буферизацией). Плюс `/proc/pressure/io`, чтобы понять, страдают ли другие задачи.

*Что хочет услышать:* методичную локализацию «приложение vs инфраструктура».
*Красный флаг:* «поставлю диск SSD и всё пройдёт».

**3. Как работает OOM-killer и как защитить критичный сервис?**

Ответ: при нехватке памяти ядро выбирает жертву по `oom_score` (зависит от потребления) с поправкой `oom_score_adj` (−1000…+1000). В cgroup v2 при превышении `memory.max` срабатывает memcg OOM — убивает только внутри группы. Защита: `OOMScoreAdjust=-500` (или −1000 для «не убивать никогда»), лимиты `MemoryHigh`/`MemoryMax`, корректные `requests/limits` в Kubernetes, алерты на PSI и swap, и главное — устранение причины роста памяти.

*Что хочет услышать:* понимание разницы между глобальным OOM и cgroup OOM, знание `oom_score_adj`.
*Красный флаг:* «OOM убивает случайный процесс» или «отключу OOM через sysctl».

**4. Чем `perf`, `strace` и `ltrace` отличаются и когда что применять?**

Ответ: `perf` — сэмплирующий профилировщик по CPU-событиям (PMU, software events, tracepoints), накладные расходы низкие, даёт горячие функции и стеки → флейм-графы. `strace` — трассировка системных вызовов через `ptrace`, останавливает процесс на входе и выходе из syscall, замедление в разы. `ltrace` — трассировка библиотечных вызовов через PLT, только для динамически слинкованных C/C++. Порядок: сначала счётчики (`vmstat`, `pidstat`), потом `perf`, и только потом точечно `strace` — когда нужно понять, на каком именно вызове висит процесс.

*Что хочет услышать:* понимание цены инструмента и дисциплины «от дешёвого к дорогому».
*Красный флаг:* «всегда запускаю strace, он же всё показывает».

**5. `available` в `free` маленький, swap используется. Это уже авария?**

Ответ: не обязательно. Page cache вытесняется, анонимные страницы уходят в swap — это нормальная работа. Авария — когда растёт `memory.pressure` (`full avg10` устойчиво > 0), `si/so` не прекращаются, процессы в D-state, растут major page faults и появляются OOM-строки в `dmesg`. Тогда смотрим, кто именно растёт по RSS, и решаем: лимит, рестарт, утечка в коде.

*Что хочет услышать:* что ты различаешь «память закончилась» и «память работает как задумано».
*Красный флаг:* «swap это зло, надо `swapoff -a` на всех серверах».

## 8. Источники (официальная документация)

- **man-pages (`man7.org`)** — разделы: `man 1 perf`, `man 1 strace`, `man 1 iostat`, `man 5 proc`, `man 7 cgroups`, `man 5 systemd.resource-control`. Поисковая фраза: `man 7 cgroups v2 memory controller`.
- **docs.kernel.org** — раздел про управление памятью ядра и OOM: `Documentation/admin-guide/mm/` (в частности, «Out Of Memory Management»), `Documentation/accounting/psi.rst` (Pressure Stall Information). Поиск: `kernel psi pressure stall information documentation`.
- **kernel.org** — `Documentation/filesystems/proc.rst` (описание `/proc/meminfo`, `/proc/loadavg`, `/proc/pressure`). Поиск: `proc.txt meminfo fields explanation kernel`.
- **learn.microsoft.com** — раздел про Performance Monitor и счётчики Windows: «Performance Monitor», «Get-Counter». Поиск: `windows performance monitor counters memory available mbytes`.
- **brendangregg.com** — методичка «Linux Performance Analysis in 60,000 Milliseconds» и «Linux Performance Tools» (60-секундный чек-лист и карта инструментов). Поиск: `brendan gregg linux performance 60 seconds`.
- **ubuntu.com** (документация сервера) — раздел про systemd и управление ресурсами. Поиск: `ubuntu server documentation systemd resource control memory limit`.
- **man7 `perf_event_open(2)`** — если нужно понять, почему в виртуалке не работают аппаратные счётчики. Поиск: `perf_event_open virtualization hardware counters availability`.

Всё, что касается точных значений по умолчанию (`kernel.perf_event_paranoid`, `Result=oom-kill`, ключи `smem`, путь к бинарю `perf`) — помечено `[ПРОВЕРИТЬ]`, потому что зависит от версии дистрибутива и ядра. Проверяй на своём `UBU01` и записывай в `stand-log.md`.
