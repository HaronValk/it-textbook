# Контейнеры под капотом: namespaces, cgroups, overlayfs, runc, containerd, OCI

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»: **Пользователь:** ivanov (бухгалтерия). **Что написал:** «Утром не открывается тестовая страница бухгалтерского сервиса на UBU01 — вчера работала. И ещё вчера вечером сервер ругался на нехватку памяти, но `free -m` показывал, что память есть». **Срочность:** средняя.

Дальше вы заходите на UBU01 и видите: контейнер с приложением в состоянии `Exited (137)`, а на хосте 12 ГБ свободной памяти. Классика. Пока вы не понимаете, что такое namespaces, cgroups, overlayfs, runc и containerd, вы будете лечить такие заявки наугад: перезапускать контейнер, «добавлять памяти», перезагружать сервер. После этой главы вы будете точно знать, где смотреть: в какой cgroup упал процесс, что ему ограничили, какой слой overlayfs заполнил диск, как руками зайти в namespace работающего контейнера и как выглядит OCI-спек, по которой он вообще собрался.

Контейнеры — это 80% современного продакшена: Kubernetes, CI-раннеры, GitLab, Harbor, мониторинг, почти любое приложение в 2026 году едет в образе. Когда контейнер падает, интервьюер и тимлид ждут от вас не «перезапустил — заработало», а «посмотрел `memory.events`, увидел `oom_kill 1`, поднял лимит». Плюс это прямая дорога к Kubernetes: под — это те же namespaces и cgroups, только с оркестратором сверху.

После главы вы умеете: создавать namespaces руками через `unshare`, читать cgroups v2, объяснять overlayfs с copy-up и whiteout, запускать программу через `runc` вручную по OCI-спеке, работать с `ctr` и отличать `containerd`-namespaces от Linux-namespaces.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Контейнер — это не виртуалка.** Нет гипервизора, нет второй ОС, нет эмуляции железа. Контейнер — это обычный процесс (или дерево процессов) на вашем хосте, которому ядро Linux подсунуло три вещи:

1. **namespaces** — иллюзию отдельного мира (свой PID 1, свои сетевые интерфейсы, своя файловая система, своё hostname);
2. **cgroups** — учёт и лимиты ресурсов (память, CPU, диск, число процессов);
3. **rootfs** — свою корневую файловую систему, чаще всего собранную из слоёв через overlayfs.

Ядро у всех контейнеров **общее с хостом**. Отсюда и скорость (старт за миллисекунды), и главный риск: дырка в ядре = дырка во всех контейнерах сразу.

```text
 kubectl / docker / nerdctl / ctr        <- CLI, которым вы работаете
             |
             v
 containerd (демон, /run/containerd/containerd.sock, gRPC поверх unix-сокета)
   |-- content store   блобы слоёв образов, адресуются по sha256
   |-- image service   манифесты и индексы OCI
   |-- snapshotter     распакованные слои на диске (overlayfs)
   `-- tasks ---------> containerd-shim-runc-v2 (вахтёр, живёт всегда)
                              |
                              v
                            runc   <- создал namespaces/cgroups, настроил rootfs и УШЁЛ
                              |
                              v
                    [ namespaces | cgroups | rootfs ]
                        ваш процесс, внутри он PID 1
```

**Linux namespaces** (файлы-ссылки в `/proc/<pid>/ns/`):

| Namespace | Что изолирует | Флаг `unshare` |
|---|---|---|
| `pid` | нумерацию процессов (внутри свой PID 1) | `--pid --fork` |
| `net` | интерфейсы, маршруты, порты, iptables | `--net` |
| `mnt` | точки монтирования | `--mount` |
| `uts` | hostname и домен | `--uts` |
| `ipc` | очереди сообщений, разделяемая память | `--ipc` |
| `user` | маппинг UID/GID (root в контейнере ≠ root на хосте) | `--user` |
| `cgroup` | видимость иерархии cgroups | `--cgroup` |
| `time` | смещение системных часов (Linux 5.6+) | `--time` |

**cgroups v2** — единая иерархия в `/sys/fs/cgroup`, каждый контроллер — набор файлов в каталоге группы: `memory.max`, `memory.current`, `memory.events`, `cpu.max`, `cpu.weight`, `pids.max`, `io.max`. Включаются контроллеры вниз по дереву через `cgroup.subtree_control`. Ubuntu 24.04 по умолчанию использует именно v2 (`stat -fc %T /sys/fs/cgroup` → `cgroup2fs`). Важная деталь: **cgroup v1 и v2 несовместимы по интерфейсу**, половина старых статей в интернете про `memory.limit_in_bytes` не применима.

**overlayfs** — файловая система «поверх»: `lowerdir` (слои образа, только чтение, разделяются между контейнерами), `upperdir` (всё, что контейнер пишет), `workdir` (служебный, обязан быть на одной ФС с upper и пустым), `merged` (то, что видит контейнер). При первой записи в файл из нижнего слоя ядро делает **copy-up** — копирует файл в upper, и правки идут уже туда. Удаление файла из lower не удаляет его, а создаёт в upper **whiteout** (символьное устройство 0/0) — так файл «прячется».

**OCI** (Open Container Initiative) — три спецификации, которые сделали контейнеры переносимыми: **image-spec** (как устроен образ: слои + манифест), **runtime-spec** (как описать запуск: файл `config.json`), **distribution-spec** (как образы отдавать по HTTP из registry).

**runc** — эталонная реализация runtime-spec. Это маленький бинарник, который делает грязную работу: создаёт namespaces, пишет лимиты в cgroups, монтирует rootfs, делает `pivot_root` и `execve` вашего процесса. Дальше runc **не нужен** — он завершается. **containerd-shim** остаётся родителем процесса, поэтому перезапуск демона containerd не убивает контейнеры. Сам containerd — высокоуровневый менеджер: тянет образы, распаковывает слои, следит за тасками. Именно его использует Docker внутри себя и Kubernetes через CRI-плагин.

Локальная тонкость: у containerd есть **свои** namespaces (`default`, `moby`, `k8s.io`) — это логические группы объектов внутри демона, они никак не связаны с Linux namespaces. Контейнеры Docker видны как `ctr -n moby containers ls`, и это первая причина, почему `ctr containers ls` «ничего не показывает».

Сетевых портов у containerd нет: он слушает unix-сокет `/run/containerd/containerd.sock`. Сеть контейнера — это отдельный net namespace плюс пара veth — bridge на хосте (в Kubernetes это делает CNI). Запуск через `ctr` **сеть не настраивает** — внутри будет только `lo`.

## 3. Аналогия из жизни

Представьте офис в бизнес-центре. **namespaces** — это внутренняя планировка: своя нумерация кабинетов (у арендатора свой «кабинет №1», хотя в здании он 417-й), своя табличка на входе (hostname), свой внутренний телефонный коммутатор (net). Сотрудник свято уверен, что живёт в отдельном здании.

**cgroups** — счётчики и автоматы в щитке: электричество на офис ограничено 5 кВт, больше — вырубает именно этот офис, остальных не трогает; и по счётчику видно, кто сколько сжёг.

**overlayfs** — прозрачная калька поверх типового проекта этажа: планировка общая для всех (lower), но каждый арендатор поверх неё рисует свои пометки (upper), а если что-то зачёркивает — ставит поверх белый прямоугольник (whiteout), сам чертёж при этом не портится.

**OCI** — ГОСТ на планировку и на акт приёмки помещения; **runc** — прораб, который один раз собрал перегородки по этому ГОСТу и ушёл; **containerd** — управляющая компания бизнес-центра, которая сдаёт помещения, ведёт склад и держит вахтёра (**shim**), чтобы за дверью присматривали круглосуточно, даже когда сама УК уходит на обед.

Аналогия ломается в одном месте: у арендаторов на самом деле **общий фундамент и общая несущая стена** — это одно ядро Linux. Треснул фундамент — посыпались все офисы сразу, чего в реальном здании не бывает.

## 4. Практика и команды

Всё делаем на **UBU01** (10.10.10.30), под пользователем с `sudo`.

**1. Создать namespaces руками — `unshare`**

```bash
sudo unshare --pid --net --uts --ipc --mount --fork --mount-proc /bin/bash
```
`--fork` обязателен для `--pid` (иначе ваш bash станет PID 1 и не сможет нормально форкать), `--mount-proc` перемонтирует `/proc` уже в новом PID-namespace. Внутри:
```text
root@UBU01:/# hostname lab26 && hostname
lab26
root@UBU01:/# ps -ef
UID  PID  PPID  CMD
0      1     0  /bin/bash
```
Смотрим: `ps` показывает один процесс, `hostname` поменялся. `exit` — и на хосте ничего не изменилось. Это контейнер без контейнера.

**2. Список namespaces — `lsns` и `/proc/<pid>/ns`**

```bash
sudo lsns -t net
sudo ls -l /proc/1/ns/
```
```text
NS        TYPE NPROCS   PID USER COMMAND
4026531840 net       1  1234 root /usr/bin/containerd
...
lrwxrwxrwx 1 root root 0 ... net -> 'net:[4026531840]'
```
Номер в квадратных скобках — inode namespace. Два процесса с одинаковым inode видят одну сеть.

**3. Зайти внутрь работающего контейнера через `nsenter`**

```bash
sudo nsenter -t <PID> -n ip addr
sudo nsenter -t <PID> -m -p ps -ef
```
`-t` — целевой процесс, `-n` — зайти в net, `-m` — в mount, `-p` — в pid. `-a` = все сразу. Это ваш «аварийный вход», когда в образе нет ни ssh, ни bash.

**4. cgroups v2: где лимиты**

```bash
stat -fc %T /sys/fs/cgroup
cat /sys/fs/cgroup/system.slice/containerd.service/memory.max
cat /sys/fs/cgroup/system.slice/containerd.service/memory.events
```
```text
cgroup2fs
max
low 0
high 0
max 12
oom 0
oom_kill 0
```
`max` = лимита нет. `oom_kill 1` = кого-то уже убили по памяти. Это первое, что читают при разборе «контейнер упал».

**5. Кто сколько ест — `systemd-cgtop` / `systemd-cgls`**

```bash
systemd-cgtop -m
sudo systemd-cgls /system.slice/containerd.service
```
Показывает дерево групп и потребление по памяти/CPU. Быстрее, чем `docker stats`, и работает без Docker.

**6. overlayfs вручную — увидеть copy-up своими глазами**

```bash
sudo mkdir -p /opt/overlay-lab/{lower,upper,work,merged}
echo "версия 1" | sudo tee /opt/overlay-lab/lower/app.conf
sudo mount -t overlay overlay \
  -o lowerdir=/opt/overlay-lab/lower,upperdir=/opt/overlay-lab/upper,workdir=/opt/overlay-lab/work \
  /opt/overlay-lab/merged
cat /opt/overlay-lab/merged/app.conf      # версия 1
echo "версия 2" | sudo tee /opt/overlay-lab/merged/app.conf
cat /opt/overlay-lab/lower/app.conf       # версия 1 — нижний слой цел
cat /opt/overlay-lab/upper/app.conf       # версия 2 — copy-up сработал
sudo rm /opt/overlay-lab/merged/app.conf
sudo ls -la /opt/overlay-lab/upper         # c--------- 0, 0 app.conf  <- whiteout
```
Типичные ошибки mount: `workdir and upperdir must reside under the same mount` (разные ФС) и непустой `workdir`.

**7. Как overlayfs выглядит у реального контейнера**

```bash
mount | grep overlay
findmnt -t overlay -o TARGET,SOURCE,OPTIONS
```
```text
overlay on /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/25/fs type overlay
  (rw,relatime,lowerdir=/var/lib/containerd/.../snapshots/1/fs,upperdir=/var/lib/containerd/.../snapshots/25/fs,...)
```
Смотрим цепочку `lowerdir` — это и есть слои образа, и `upperdir` — слой, который «растёт».

**8. containerd: версия и образы**

```bash
sudo ctr version
sudo ctr images pull docker.io/library/busybox:latest
sudo ctr images ls
```
```text
Client:  Version: v1.7.x ...
Server:  Version: v1.7.x ...
```
Если версии клиента и сервера расходятся, `ctr` печатает предупреждение — не игнорируйте, команды могут вести себя по-разному.

**9. Запустить контейнер с лимитом памяти**

```bash
sudo ctr run -t --memory-limit 33554432 docker.io/library/busybox:latest demo1 sh
```
`-t` — псевдотерминал, `--memory-limit` — байты (32 МиБ). Внутри:
```text
/ # cat /sys/fs/cgroup/memory.max
33554432
/ # ps -ef
PID   USER     TIME  COMMAND
    1 root      0:00 sh          <- внутри он PID 1
/ # ip addr
1: lo: <LOOPBACK,UP,LOWER_UP> ...   <- сети нет, ctr её не настраивает
```
Хотите сеть хоста — `--net-host` (и это уже небезопасно, см. раздел 6).

**10. Список контейнеров и тасков**

```bash
sudo ctr containers ls
sudo ctr tasks ls
sudo ctr -n moby containers ls      # контейнеры Docker, если он установлен
```
`containers` — описание (образ, спек), `tasks` — запущенные процессы. Контейнер без таска = «создан, но не работает».

**11. Запустить программу через runc напрямую, без containerd**

```bash
sudo apt install -y busybox-static
sudo mkdir -p /opt/runc-lab/rootfs/{bin,proc,sys,dev,tmp,etc}
sudo cp /bin/busybox /opt/runc-lab/rootfs/bin/
sudo ln -sf busybox /opt/runc-lab/rootfs/bin/sh
cd /opt/runc-lab && sudo runc spec          # генерирует config.json
sudo runc run demo-runc
```
В другом терминале: `sudo runc list`, `sudo runc state demo-runc`. Внутри — `hostname` (по спеке `runc`), `ps`. `config.json` — это и есть OCI runtime-spec: `process.args`, `root.path`, `linux.namespaces`, `linux.resources`. Если runc ругается на отсутствующие `maskedPaths`/`readonlyPaths` — уберите эти блоки из `config.json` `[ПРОВЕРИТЬ]` под свою версию.

**12. Посмотреть OCI-спеку, которую сгенерировал containerd**

```bash
sudo find /run/containerd -name config.json 2>/dev/null
sudo cat /run/containerd/io.containerd.runtime.v2.task/default/demo1/config.json | jq '.process.args, .linux.namespaces'
```
Один и тот же формат — что у `runc spec`, что у containerd. В этом и смысл OCI.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS), доступ по SSH, пользователь с `sudo`, есть выход в интернет или локальное зеркало пакетов.
- Docker **не нужен** и даже мешает: мы работаем с containerd напрямую. Docker поставим в уроке 27.
- Остальные ВМ (WEB01, MON01, GIT01) не затрагиваются. HARBOR01 и k8s-узлы ещё не существуют — они появятся в уроках 31+.
- В `stand-log.md` есть записи по UBU01 с прошлых уроков.
- Если Docker Hub из РФ недоступен — образ `busybox` можно взять из локального прокси/зеркала (тема урока 32) или из вашего же реестра позже. Это ограничение доступа, а не ваша ошибка.

### Снапшот
В VMware Workstation для UBU01: **VM → Snapshot → Take Snapshot**, имя `before-devops-026`. Или из командной строки Windows:
```powershell
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-026
```
Снапшот делаем только на UBU01 — другие ВМ эта лаба не трогает.

### Шаги

1. **UBU01.** Проверяем, что cgroups v2: `stat -fc %T /sys/fs/cgroup`. Ожидаем `cgroup2fs`. Если `tmpfs` — стенд старый, дальше цифры лимитов будут по v1 `[ПРОВЕРИТЬ]`.
2. **UBU01.** Тренируем namespaces: `sudo unshare --pid --net --uts --ipc --mount --fork --mount-proc /bin/bash`, внутри `hostname lab26`, `ps -ef`. Ожидаем один процесс и новое имя. Выходим `exit`.
3. **UBU01.** Ставим containerd:
   ```bash
   sudo apt update && sudo apt install -y containerd
   sudo systemctl enable --now containerd
   sudo systemctl status containerd --no-pager
   sudo ctr version
   ```
   Ожидаем `active (running)` и совпадающие Client/Server версии.
4. **UBU01.** Тянем образ: `sudo ctr images pull docker.io/library/busybox:latest`, затем `sudo ctr images ls`. Ожидаем строку с `application/vnd.oci.image.index.v1+json` и размером ~2 МБ.
5. **UBU01, сессия SSH №1.** Запускаем контейнер: `sudo ctr run -t --memory-limit 33554432 docker.io/library/busybox:latest demo1 sh`. Оставляем висеть.
6. **UBU01, сессия SSH №2.** Смотрим с хоста: `sudo ctr tasks ls`, `ps aux | grep " sh$"`, находим PID процесса `sh`. Затем `sudo ls -l /proc/<PID>/ns/` — набор ссылок `pid`, `net`, `mnt`, `uts`, `ipc`.
7. **UBU01, сессия №2.** Заходим внутрь: `sudo nsenter -t <PID> -n ip addr` (только `lo`), `sudo nsenter -t <PID> -m ls /` (корень контейнера, а не хоста), `sudo nsenter -t <PID> -p ps -ef`.
8. **UBU01, сессия №1 (внутри контейнера).** `cat /sys/fs/cgroup/memory.max` → `33554432`; `cat /proc/1/cgroup`; `mount | head`. Ожидаем увидеть свой лимит и overlay-корень.
9. **UBU01, сессия №2.** `mount | grep overlay` — находим `lowerdir`/`upperdir` нашего контейнера. Затем `sudo ls /var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/` и `sudo du -sh` по паре каталогов. Ожидаем: слои образа лежат отдельно, верхний — маленький.
10. **UBU01, сессия №1.** Выходим из контейнера (`exit`). В сессии №2: `sudo ctr task rm demo1` (в некоторых версиях `delete`), `sudo ctr container rm demo1`.
11. **UBU01.** Повторяем опыт с overlayfs из раздела 4 (пункт 6) в `/opt/overlay-lab`, наблюдаем copy-up и whiteout. Затем `sudo umount /opt/overlay-lab/merged`.
12. **UBU01.** Запускаем программу через runc по шагам из раздела 4, пункт 11. Внутри нового контейнера `hostname` → `runc`. Выходим, `sudo runc list` — пусто, `sudo runc delete demo-runc` при необходимости.
13. **UBU01.** Копируем артефакты в Git (GIT01, 10.10.10.50): `config.json` из `/opt/runc-lab`, заметки и `stand-log.md`. Ожидаем коммит вида `lab 26: runc + cgroups`.
14. **UBU01 → stand-log.md.** Записываем изменения (см. следующий подраздел).

### Проверка
- [ ] `stat -fc %T /sys/fs/cgroup` выдаёт `cgroup2fs`.
- [ ] В `unshare`-шелле `ps -ef` показывает один процесс, а на хосте hostname не изменился.
- [ ] `systemctl is-active containerd` → `active`, `ctr version` показывает Client и Server без warning.
- [ ] `ctr images ls` содержит `docker.io/library/busybox:latest`.
- [ ] Внутри контейнера `cat /sys/fs/cgroup/memory.max` = `33554432`, `ps -ef` показывает PID 1.
- [ ] `nsenter -t <PID> -n ip addr` показывает только `lo`.
- [ ] `mount | grep overlay` содержит пути `snapshots/.../fs`.
- [ ] В `/opt/overlay-lab/upper` после правки появился файл, после удаления — whiteout `c--------- 0, 0`.
- [ ] `runc run demo-runc` запускается без containerd и Docker.
- [ ] Запись в `stand-log.md` сделана, артефакты в Git на GIT01.

### Сломай и почини

**Пользователь:** ivanov · **ВМ:** UBU01 · **Что написал:** «Бух-сервис опять недоступен, третий раз за день. В логах пусто, контейнер просто исчез» · **Срочность:** высокая.

**Как воспроизвести поломку намеренно.** На UBU01 запускаем контейнер с жёстким лимитом памяти и заставляем его съесть больше:
```bash
sudo ctr run -t --memory-limit 16777216 docker.io/library/busybox:latest oom-demo sh
# внутри:
dd if=/dev/zero of=/dev/shm/hog bs=1M count=64
```
`/dev/shm` — tmpfs, а память tmpfs учитывается в memory-cgroup, поэтому контейнер получит OOM kill.

**Диагностика по порядку:**
1. `sudo ctr tasks ls` — процесса нет, контейнер в состоянии `STOPPED`.
2. Код возврата: `echo $?` или `sudo ctr task ls`/логи. `137` = 128 + 9, то есть `SIGKILL`.
3. `dmesg -T | grep -i -E "oom|killed process"` — ищем строку вида `Memory cgroup out of memory: Killed process ... (dd)`.
4. `cat /sys/fs/cgroup/system.slice/containerd.service/<id>/memory.events` — видим `oom_kill 1`. Это приговор: убило ядро по лимиту, а не приложение.
5. `cat .../memory.max` и `memory.peak` (`memory.peak` есть в свежих ядрах `[ПРОВЕРИТЬ]`) — сверяем лимит с фактом.
6. Только теперь смотрим логи приложения — убеждаемся, что оно не писало «не хватает памяти» само.

**Решение.** Лимит 16 МиБ был выставлен «на глаз». Поднимаем до реального потребления с запасом (например, `--memory-limit 134217728`), а правильнее — зафиксировать лимит в манифесте (Docker Compose / Kubernetes) и в Git, чтобы он не жил в истории bash. Плюс временно убираем запись в `/dev/shm` или переносим её на диск.

**Ответ пользователю:** «Разобрался: контейнер сервиса убивало ядро — ему не хватало выделенной памяти при обработке выгрузки. Ограничение подняли с 16 до 128 МБ, сервис снова работает, за прошлые падения ошибок в приложении нет. Если повторится — увижу сразу, настроил порог по памяти». 

**Закрытие заявки:** категория «Инфраструктура / контейнеры», резолюция «OOM kill по memory-cgroup, лимит скорректирован», ссылка на запись в `stand-log.md`. Отдельным тикетом — задача «алертить на `oom_kill` в `memory.events`».

### Что изменилось на стенде

- **UBU01:** установлен и включён в автозапуск `containerd` (пакет из репозитория Ubuntu, версия — записать в журнал), сокет `/run/containerd/containerd.sock`.
- **UBU01:** в кэш containerd загружен образ `docker.io/library/busybox:latest` (тег и digest записать).
- **UBU01:** установлен пакет `busybox-static`.
- **UBU01:** создан каталог `/opt/overlay-lab` (lower/upper/work/merged), после лабы — размонтирован.
- **UBU01:** создан OCI-бандл `/opt/runc-lab` с `config.json` и `rootfs`.
- **GIT01 (10.10.10.50):** в репозитории стенда появился каталог `lab26/` с `config.json` и заметками.
- **stand-log.md:** запись «урок 26: containerd + runc + overlay + cgroups v2 на UBU01».
- Никаких новых ВМ, адресов, пользователей и общих папок не появилось.

### Откат

Если что-то пошло не так и хочется чистого состояния: UBU01 → **VM → Snapshot → Snapshot Manager → before-devops-026 → Revert**, либо
```powershell
& "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-026
```
Помните: откат уберёт и `containerd`, и артефакты в `/opt`. Поэтому перед откатом убедитесь, что `config.json` и `stand-log.md` закоммичены на GIT01 — иначе наработки пропадут.

## 6. Ошибки новичков и безопасность

1. **«Контейнер — это лёгкая виртуалка».** → Неверные ожидания по изоляции и производительности, попытки «поставить в контейнер своё ядро». → Контейнер = процесс + namespaces + cgroups на общем ядре.
2. **`ctr containers ls` «пусто», хотя Docker что-то запустил.** → containerd-неймспейсы разные. → `sudo ctr -n moby containers ls` (Docker) или `-n k8s.io` (Kubernetes).
3. **Читать в интернете `memory.limit_in_bytes` и не находить файл.** → Это cgroup v1, у вас v2. → `memory.max`, `memory.current`, `memory.events`.
4. **Монтировать overlay с `workdir` на другой ФС или непустым.** → `mount` падает с невнятной ошибкой. → `upperdir` и `workdir` на одном разделе, `workdir` пустой.
5. **Править файлы в `/var/lib/containerd/.../snapshots/`.** → Ломается слой, контейнеры перестают стартовать. → Нижние слои read-only by design, менять только образ.
6. **Путать exit code 137 с ошибкой приложения.** → Лечат приложение вместо лимитов. → 137 = SIGKILL: проверяем `dmesg` и `memory.events`.
7. **Запускать `runc` руками в бою «чтобы быстрее».** → Контейнер без менеджера: никто не перезапустит, не соберёт логи, не удалит мусор. → runc — только для отладки и понимания.
8. **Игнорировать `--net-host` и `--privileged` «на минутку».→ Контейнер видит сеть и устройства хоста. → В лаборатории можно, в проде — запрещено политикой (см. урок 30).

### Безопасность

- **root в контейнере ≈ root на хосте.** Без user namespace и с дефолтными capabilities процесс внутри имеет много прав. Смотрите `grep Cap /proc/<PID>/status` и `capsh --decode=<hex>`; в проде — rootless-режим и урезанный набор capabilities (уроки 29–30).
- **`ctr` работает от root и без аутентификации** — любой с доступом к сокету `containerd.sock` управляет контейнерами от имени root. Сокет — только для админов, права `660`, никогда не пробрасывать внутрь контейнера.
- **`--privileged`, `--net-host`, монтирование `/` или `/var/run/docker.sock`** внутри контейнера — фактическая сдача хоста. В учебной лабе это допустимо, только если вы прямо себе это разрешили и записали в журнал; в боевой среде такие запросы согласуются и чаще всего отклоняются.
- **Образы без проверки.** `:latest` из публичного реестра — это чужой код на вашем хосте. В бою: фиксация по digest, сканирование (Trivy/Grype), подпись (Cosign) — уроки 30–31.
- Пароль стенда `Lab#2026-vektor` и `sudo` без пароля — **только для этого стенда**. В реальной работе так нельзя.

## 7. Вопросы на собеседовании

**1. Чем контейнер отличается от виртуальной машины? Что такое namespaces и cgroups?**
Ответ: ВМ запускает полноценную гостевую ОС под гипервизором, у неё своё ядро и эмуляция железа. Контейнер — это обычный процесс на хосте, изолированный Linux namespaces (pid, net, mnt, uts, ipc, user, cgroup, time) и ограниченный cgroups (v2: `memory.max`, `cpu.max`, `pids.max`, `io.max`). Ядро общее, поэтому старт быстрый и плотность выше, но изоляция слабее: уязвимость ядра затрагивает все контейнеры. Rootfs контейнера обычно собран из слоёв через overlayfs.
*Что хочет услышать интервьюер:* что вы понимаете «процесс на общем ядре», а не пересказываете маркетинг.
*Красный флаг:* «контейнер — это лёгкая ВМ» или «у контейнера своё ядро».

**2. Зачем нужен runc, если есть containerd? И что такое containerd-shim?**
Ответ: containerd — высокоуровневый демон: тянет образы, распаковывает слои, управляет жизненным циклом. Он не создаёт namespaces сам, а вызывает низкоуровневый OCI-runtime — runc. runc по `config.json` создаёт namespaces, пишет лимиты в cgroups, монтирует rootfs, делает `execve` процесса и завершается. Между containerd и процессом стоит `containerd-shim-runc-v2`: он остаётся родителем, собирает exit-код, держит stdio, поэтому перезапуск containerd не убивает контейнеры и они не становятся зомби.
*Что хочет услышать:* понимание цепочки CLI → containerd → shim → runc → процесс и того, кто живёт дольше всех.
*Красный флаг:* «runc — это демон» или «containerd запускает процесс напрямую».

**3. Как работает overlayfs и что происходит при записи в файл из образа?**
Ответ: overlayfs объединяет read-only `lowerdir` (слои образа, общие для всех контейнеров) и `upperdir` (персональный слой контейнера); `workdir` — служебный каталог на той же ФС, `merged` — то, что видит контейнер. При первой записи в файл из lower ядро делает copy-up: копирует файл в upper, правки идут туда, нижний слой остаётся целым. Удаление файла из lower создаёт whiteout (символьное устройство 0/0) в upper. Поэтому образ переиспользуется десятками контейнеров, а «весит» контейнер только своим upper.
*Что хочет услышать:* copy-up, whiteout, общие lower-слои, экономию места.
*Красный флаг:* «контейнер копирует весь образ к себе» (нет) или «overlayfs — это про сеть».

**4. Что входит в OCI и зачем эта стандартизация?**
Ответ: три спецификации: image-spec (слои + манифест + индекс), runtime-spec (файл `config.json`: args, env, namespaces, ресурсы, mounts), distribution-spec (HTTP API registry). Благодаря им образ, собранный Docker, запускается в Podman, containerd, Kubernetes, а рантайм можно менять (runc, crun, runsc/gVisor) без пересборки образов. Практический эффект: вендор-лок нет, инструменты взаимозаменяемы.
*Что хочет услышать:* что вы называете хотя бы image-spec и runtime-spec и понимаете выгоду переносимости.
*Красный флаг:* «OCI — это формат Docker-образа» без понимания, что спецификаций три и они про разное.

**5. Контейнер завершился с кодом 137. Ваши действия?**
Ответ: 137 = 128 + 9, процесс получил SIGKILL. Дальше: `ctr tasks ls`/`docker ps -a` — состояние и код; `dmesg -T | grep -i oom` — ищем `Memory cgroup out of memory`; `cat /sys/fs/cgroup/.../memory.events` — `oom_kill 1` подтверждает OOM; сверяем `memory.max` с реальным потреблением; проверяем `pids.max` (иногда это лимит на процессы) и лимиты вышестоящих cgroups. Если OOM не подтвердился — ищем внешний SIGKILL: деплой, `kill -9` руками, liveness-probe в Kubernetes, OOM-killer хоста, а не cgroup.
*Что хочет услышать:* системную диагностику по источникам (ядро, cgroup, оркестратор), а не «перезапущу и посмотрю».
*Красный флаг:* «137 — это ошибка приложения» или «надо просто дать больше CPU».

## 8. Источники (официальная документация)

- **containerd.io** — раздел *Docs → Getting started / ctr*, поиск: `containerd ctr command reference`, `containerd namespaces explained`.
- **opencontainers.org** — спецификации: *Runtime Specification*, *Image Specification*, *Distribution Specification*; поиск: `OCI runtime spec config.json`, `OCI image spec layers manifest`.
- **github.com/opencontainers/runtime-spec** — `config.md`, `runtime.md`; поиск: `runtime spec linux namespaces cgroups`.
- **docs.kernel.org** — разделы *Namespaces*, *Control Group v2*, *Overlay Filesystem*; поиск: `cgroup v2 memory.max documentation`, `overlayfs copy up whiteout`.
- **man7.org** — `man 7 namespaces`, `man 1 unshare`, `man 1 nsenter`, `man 1 lsns`, `man 8 runc` (если установлен) `[ПРОВЕРИТЬ]`; поиск: `man 7 user_namespaces`.
- **ubuntu.com** (documentation / server) — раздел про cgroups и containerd; поиск: `ubuntu 24.04 containerd cgroup v2 systemd`.
- **kubernetes.io** — раздел *Concept → Containers* (для связи с подами); поиск: `kubernetes container runtime CRI containerd`.
- **github.com/opencontainers/runc** — `README.md`, `docs/`; поиск: `runc spec config.json example`.
- Точные номера версий containerd в репозитории Ubuntu 24.04 и набор флагов `ctr run` в вашей версии — `[ПРОВЕРИТЬ]` через `ctr run --help` и `apt policy containerd`.
