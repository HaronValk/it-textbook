# Основы eBPF: Cilium, Pixie, bpftrace — продвинутая диагностика и наблюдаемость

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4417**

> **Пользователь:** Петрова (HR), ВМ WIN10-CL02
> **Что написала:** «Открываю портал кадровых заявок `hr.vektor.lab` — страница грузится секунд пять, а иногда просто 502. Разработчики говорят: „у нас на ноутбуке всё работает“. Примите меры, у меня отчёт!»
> **Срочность:** средняя, до конца дня

Классика. Приложение живёт в Kubernetes, логи приложения чистые, метрики pod'а в норме, а пользователь страдает. Проблема в промежутке: сеть, DNS, политики, ретрансмиты, таймауты — то есть там, где обычные логи приложения ничего не видят.

Именно для этого слоя и придумали eBPF. За последние годы он превратился из «игрушки для трассировки пакетов» в фундамент: на eBPF работают сетевой слой Kubernetes (Cilium), наблюдаемость без правок кода (Pixie), runtime-безопасность (Tetragon), диагностика ядра (bpftrace). Это одна из немногих технологий, которая одновременно снижает оверхед и повышает видимость.

После этой главы ты будешь уметь: проверить, поддерживает ли ядро eBPF; снимать живые события с ядра одной строкой на bpftrace; видеть L3/L4/L7-потоки в кластере через Hubble; понимать, чем Cilium отличается от kube-proxy и iptables; знать, зачем нужен Pixie и когда он не нужен. И главное — ты будешь отвечать на вопрос «почему у пользователя 502, если в логах всё ок» не гаданием, а данными.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**eBPF (extended Berkeley Packet Filter)** — виртуальная машина внутри ядра Linux. Ты пишешь небольшую программу (на C, Rust или на DSL вроде bpftrace), компилируешь её в байткод eBPF и загружаешь в ядро системным вызовом `bpf(2)`. Дальше ядро само вызывает твою программу в нужной точке.

Что делает ядро при загрузке:

1. **Верификатор (verifier).** Статически проверяет программу: нет обращений по «мусорным» указателям, нет бесконечных циклов, стек не больше 512 байт, все ветки завершаются. Не прошла проверку — не загрузится вообще. Это ключевое отличие от модуля ядра: ошибка в eBPF-программе не уронит систему (в теории), а вот ошибка в модуле — легко.
2. **JIT-компиляция** в машинный код процессора. Поэтому оверхед — единицы процентов, а не «как у strace на 10 000 процессов».
3. **Maps** — разделяемая память между программой и user space (hash, array, ringbuf, perf buffer). Туда складывают счётчики, гистограммы, события.
4. **Helpers** — функции ядра, которые программа может вызывать (`bpf_get_current_comm()`, `bpf_probe_read_kernel()` и т.д.).

Точки подключения (attach points) — их надо знать, потому что на собеседовании спрашивают именно это:

| Точка | Что это | Когда использовать |
|---|---|---|
| `kprobe` / `kretprobe` | вход/выход почти любой функции ядра | быстрая диагностика, но нестабильный интерфейс (имена функций меняются между ядрами) |
| `tracepoint` | стабильные, документированные точки в ядре | предпочтительно: стабильный ABI |
| `uprobe` / `uretprobe` | функции в user space бинарника или библиотеки | профилирование приложений без пересборки |
| `fentry` / `fexit` | привязка к началу/концу функции через BTF | современная замена kprobe, быстрее |
| XDP | самый ранний приём пакета в драйвере | DDoS-фильтрация, балансировка |
| tc (traffic control) ingress/egress | после сетевого стека, до/после маршрутизации | здесь работает Cilium |
| cgroup / socket hooks | политики на уровне cgroup и сокетов | сетевые политики Kubernetes |
| LSM hooks | проверки безопасности | Tetragon, аудит доступа |

Отдельно два слова о **BTF (BPF Type Format)** и **CO-RE (Compile Once — Run Everywhere)**: BTF — это описание типов ядра, лежащее в `/sys/kernel/btf/vmlinux`. Благодаря нему одна и та же eBPF-программа работает на разных ядрах, подстраивая смещения полей структур на лету. Нет BTF — нет современных инструментов.

Схема — где eBPF врезается в путь запроса:

```text
клиент (WIN10-CL02, petrova)
   │  HTTP GET hr.vektor.lab
   ▼
[ k8s-w01 ] сетевой адаптер
   │
   ├── XDP                ← eBPF: фильтрация до выделения skb
   ├── tc ingress         ← eBPF Cilium: policy, LB, NAT
   │
   ├──── ядро ────────────────────────────────────────
   │      TCP/IP стек      ← tracepoint:tcp:* (ретраи, дропы)
   │      conntrack, Netfilter (или его отсутствие при kube-proxy replacement)
   │      VFS / файлы      ← kprobe:vfs_read
   │      планировщик      ← tracepoint:sched:*
   │      ───── граница user/kernel ─────
   │      приложение nginx ← uprobe в libssl/libc
   └──────────────────────────────────────────────────
   │
   ▼ ответ клиенту
Hubble / Pixie / bpftrace собирают всё это и отдают в виде потоков, метрик и стеков
```

**Cilium** — CNI-плагин для Kubernetes, который заменяет iptables-правила и kube-proxy eBPF-программами. Практический выигрыш: при 5000 сервисов iptables деградирует линейно, eBPF — нет (lookup по hash-таблице). Плюс L7-политики (по HTTP-методам и gRPC-методам), прозрачное шифрование WireGuard/IPsec и **Hubble** — потоковый наблюдатель, который читает события из eBPF и показывает: кто с кем говорил, на какой порт, с каким вердиктом (FORWARDED / DROPPED), а на L7 — с каким HTTP-кодом и латентностью.

**Pixie** — проект CNCF (изначально от New Relic). Автоматически перехватывает L7-трафик (HTTP, gRPC, DNS, Postgres, Redis, MySQL) через eBPF-uprobes в библиотеках TLS (OpenSSL/BoringSSL) и в сетевом стеке. Не нужны ни sidecar, ни правки кода, ни пересборка. Данные хранятся **внутри кластера** (компонент Vizier), наружу уходит только агрегат. Управление — через PxL-скрипты. Важно: Pixie видит трафик только если он не зашифрован на уровне, который eBPF не вскрывает (mTLS с ключами вне библиотеки — уже нет).

**bpftrace** — DSL и рантайм для трассировки. Синтаксис в духе awk: `пробa /фильтр/ { действие }`. Одна строка заменяет полдня написания C-программы. Инструменты из комплекта (`execsnoop.bt`, `biolatency.bt`, `opensnoop.bt`) — это те же скрипты, которые ты можешь прочитать и поправить под себя.

Порты, которые пригодятся: Hubble Relay — **4245**, Hubble Peer (agent) — **4244**, Hubble UI — **12000** [ПРОВЕРИТЬ по версии вашего Cilium]. Порты и endpoint'ы Cilium-агента смотри в `cilium status`.

Требования к ядру: Cilium работает на ядрах от 4.19, но полный набор функций (kube-proxy replacement, DSR, L7) требует 5.4+ и выше; Pixie — 4.14+ [ПРОВЕРИТЬ по матрице вашей версии]. На Ubuntu 24.04 (ядро 6.8) всё актуальное доступно из коробки.

## 3. Аналогия из жизни

Представь многоквартирный дом, который уже построен и заселён. Тебе нужно понять, почему в квартире 42 холодные батареи. Вариант «сносить стены и строить заново» — это переписать приложение с логированием на каждом шаге: дорого, долго, жильцы против.

eBPF — это датчики и камеры, которые ставят в уже готовом доме: на стояке в подвале, на вводе воды, на каждом этаже. Стены не трогаем, жильцов не переселяем, а видим всё: где давление падает, где вентиль прикрыт, где труба заросла.

Верификатор — это БТИ и пожарный инспектор одновременно: они смотрят проект датчика до установки и не дают просверлить несущую стену. Плохо спроектированный датчик просто не согласуют — дом не пострадает, но и данных ты не получишь.

Maps — это журнал показаний в подвале: датчики пишут туда, а ты приходишь и читаешь. JIT — переводчик, который переводит показания датчика на язык дома, чтобы читалось быстро.

Cilium — это автоматизированная система доступа в подъезд: вместо бумажного списка жильцов на двери (iptables) — электронная проходная, которая ещё и логирует каждый проход (Hubble). Pixie — видеонаблюдение с распознаванием лиц на входе: видит не только «кто-то зашёл», но и «зашёл Иван, с пакетом, в 14:32». bpftrace — твой личный портативный тепловизор, который ты приносишь точечно: посмотреть конкретный стояк прямо сейчас.

Где аналогия ломается: датчик физически невозможно поставить «внутрь» уже работающего процесса без его согласия, а eBPF влезает в любую функцию ядра и любого процесса — и именно поэтому это ещё и мощный инструмент атакующего, а не только админа.

## 4. Практика и команды

Все команды — на стенде «Вектор». Узлы кластера: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63). Работаем под пользователем с sudo.

### 4.1. Проверить, умеет ли ядро eBPF

```bash
uname -r
test -f /sys/kernel/btf/vmlinux && echo "BTF: ok" || echo "BTF: НЕТ"
cat /proc/sys/kernel/unprivileged_bpf_disabled
```

- `uname -r` — версия ядра. Для Cilium нужен минимум 4.19, комфортно — 5.10+.
- `/sys/kernel/btf/vmlinux` — наличие BTF. Нет файла → CO-RE и современные инструменты работать не будут (нужно ядро с `CONFIG_DEBUG_INFO_BTF=y`).
- `unprivileged_bpf_disabled` — `0` разрешает eBPF непривилегированным пользователям, `1`/`2` — запрещает. На Ubuntu по умолчанию обычно `2` [ПРОВЕРИТЬ на вашей сборке]. Для безопасности правильное значение — `2`.

Типичный вывод:

```text
6.8.0-45-generic
BTF: ok
2
```

### 4.2. Посмотреть, что вообще поддерживается

```bash
sudo apt update && sudo apt install -y linux-tools-common linux-tools-$(uname -r)
sudo bpftool feature probe | head -30
```

`bpftool` — «швейцарский нож» для eBPF: список программ, карт, BTF, проверка возможностей ядра. `feature probe` печатает матрицу «поддерживается / нет» по helper'ам и типам программ; `head -30` — потому что вывод длинный. Смотреть на строки `eBPF support:` и `program_type ... : yes/no` [ПРОВЕРИТЬ точный формат в вашей версии].

### 4.3. Установить и проверить bpftrace

```bash
sudo apt install -y bpftrace bpfcc-tools
bpftrace --version
```

`bpfcc-tools` ставит готовые утилиты BCC (`execsnoop-bpfcc`, `biolatency-bpfcc` — на Ubuntu у них суффикс `-bpfcc`). Версия bpftrace выводится в виде `bpftrace v0.x.y`.

### 4.4. Что можно трассировать

```bash
sudo bpftrace -l | wc -l
sudo bpftrace -l 'tracepoint:tcp:*'
sudo bpftrace -l 'kprobe:vfs_*'
```

- `-l` — список доступных проб (не запускает трассировку).
- Шаблоны с `*` — фильтр по имени. Первая команда покажет общее число проб (на типичной Ubuntu — десятки тысяч).
- `tracepoint:tcp:*` — точки TCP-стека: `tcp_retransmit_skb`, `tcp_receive_reset`, `tcp_probe` и т.д.
- `kprobe:vfs_*` — файловые операции ядра.

Именно так ты узнаёшь **имена полей** конкретной пробы: `sudo bpftrace -lv 'tracepoint:tcp:tcp_retransmit_skb'` (ключ `-v` — verbose, печатает аргументы) [ПРОВЕРИТЬ синтаксис `-lv` в вашей версии].

### 4.5. Первая программа за 10 секунд

```bash
sudo bpftrace -e 'BEGIN { printf("eBPF жив, pid=%d\n", pid); }'
```

`-e` — программа строкой; `BEGIN` — блок, исполняемый один раз при старте; `pid` — встроенная переменная (PID процесса bpftrace).

### 4.6. Кто запускает процессы (execsnoop)

```bash
sudo bpftrace -e 'tracepoint:syscalls:sys_enter_execve {
    printf("%-7d %-16s %s\n", pid, comm, str(args->filename));
}'
```

- `tracepoint:syscalls:sys_enter_execve` — момент вызова `execve(2)`, то есть «процесс запускается».
- `pid` — PID, `comm` — имя процесса (обрезано до 16 символов), `str(args->filename)` — путь к бинарнику (строка из user space).
- `%-7d` — форматирование с выравниванием.

Вывод (сокращённо):

```text
9123    runc             /usr/bin/runc
9124    sh               /bin/sh
9130    nginx            /usr/sbin/nginx
```

В кластере это видно как `runc` → `sh` → полезная нагрузка: именно так ловят «а почему под что-то выполняет при старте».

### 4.7. Гистограмма размеров чтения

```bash
sudo bpftrace -e 'kretprobe:vfs_read /comm == "nginx"/ { @bytes = hist(retval); }'
```

- `kretprobe:vfs_read` — на **выходе** из `vfs_read`, значит доступен `retval`.
- `/comm == "nginx"/` — фильтр: только процесс nginx. Фильтр ставится косыми чертами.
- `hist(retval)` — построить лог2-гистограмму в карте `@bytes`.
- `@` — начало имени карты, `@bytes` — твоя карта.

Вывод (Ctrl-C):

```text
@bytes:
[1]                  0 |                                                    |
[2, 4)              14 |@                                                   |
[4, 8)             230 |@@@@@@@@@@@@@@@@                                    |
...
```

Смотреть: в какой корзине «горб». Если основная масса — 4–8 КБ, это нормальный страничный ввод-вывод; если много мелких — приложение читает по байту и страдает.

### 4.8. Сэмплирование стека CPU

```bash
sudo bpftrace -e 'profile:hz:99 { @[comm, kstack] = count(); }'
```

- `profile:hz:99` — псевдопроба: срабатывает 99 раз в секунду на каждом CPU. Фактически профилировщик без `perf`.
- `kstack` — стек вызовов ядра. Для user space — `ustack`.
- `@[comm, kstack] = count()` — считаем попадания по паре «процесс + стек».

Вывод длинный (по 10–20 строк на стек); смотреть на вершину списка — там, где `count` максимален: это и есть «горячая» функция. Классика: обнаружить, что время уходит не в приложение, а в `spin_lock` в conntrack.

### 4.9. Готовые скрипты bpftrace

```bash
sudo bpftrace /usr/share/bpftrace/tools/execsnoop.bt
sudo bpftrace /usr/share/bpftrace/tools/biolatency.bt
```

Скрипты лежат в `/usr/share/bpftrace/tools/`, их можно и нужно читать — это учебник по eBPF. Запуск до Ctrl-C; параметры (например, длительность) описаны в `--help` конкретного скрипта [ПРОВЕРИТЬ для файла, который берёте].

### 4.10. Кто загрузил eBPF-программы в ядро

```bash
sudo bpftool prog list | head -40
sudo bpftool prog list | grep -ci cilium
```

Вывод (сокращённо):

```text
142: sched_cls  name cilium_from_container  tag 8f2a...  gpl
143: sched_cls  name cilium_to_container    tag 91bc...  gpl
```

`name` — имя программы, `gpl`/`proprietary` — лицензия. Если после установки Cilium `grep -c cilium` показывает 50+, значит eBPF-датапас реально работает.

### 4.11. Cilium и Hubble

```bash
cilium status --wait
cilium hubble enable --ui
cilium hubble port-forward &
hubble status
hubble observe --namespace hr-portal --last 20 -o compact
```

- `cilium status` — состояние агента, режим `KubeProxyReplacement`, статус Hubble.
- `cilium hubble enable --ui` — включить Hubble (relay + UI) через Helm-апдейт релиза Cilium.
- `cilium hubble port-forward` — пробросить порт 4245 на локальную машину, чтобы работал CLI `hubble`.
- `hubble status` — здоровье relay и ноды.
- `hubble observe` — потоки. Ключи: `--last N` (последние N), `-o compact` (компактный вид), `--verdict DROPPED`, `--type l7`, `--protocol http`, `--pod`, `--follow` (стрим, как `tail -f`).

Вывод (сокращённо):

```text
hr-portal/hhr-portal-6d9-abc:54321 -> hr-portal/hhr-portal-6d9-xyz:80 http-request FORWARDED (GET /requests)
hr-portal/hhr-portal-6d9-xyz:80  -> hr-portal/hhr-portal-6d9-abc:54321 http-response FORWARDED (GET /requests 200 4ms)
```

Смотреть: `FORWARDED` или `DROPPED`, HTTP-код, латентность (`4ms`), путь.

### 4.12. Дропы на узле

```bash
kubectl -n kube-system exec ds/cilium -- cilium monitor -t drop
```

- `cilium monitor` — «сырой» поток событий агента; `-t drop` — только дропы.
- Запускать через `kubectl exec` в DaemonSet проще, чем искать сокет на ноде.

### 4.13. Pixie (обзорно)

```bash
px deploy
kubectl get pods -n pl
```

- `px deploy` — разворачивает Vizier (агенты Pixie) в namespace `pl`.
- Проверка — обычным `kubectl get pods -n pl`: должны быть `vizier-*`, `kelvin`, `metadata` и др. в статусе Running.

Требования: 2+ vCPU и 4+ ГБ RAM свободно на кластер [ПРОВЕРИТЬ по документации вашей версии], ядро 4.14+, Linux. На трёх ВМ в VMware Workstation это ощутимо — включай только если готов выделить ресурсы. Точный синтаксис подкоманд `px` смотри в `px --help` [ПРОВЕРИТЬ наименования подкоманд в вашей версии].

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер: `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63) — готов, `kubectl` работает с `k8s-cp01`.
- CNI — Cilium (ставили в уроке 104). Проверка: `kubectl -n kube-system get ds | grep cilium`. Если CNI другой (Calico/Flannel) — шаги 7–10 пропусти, bpftrace-часть выполняй полностью, а Cilium поставь отдельно по главе 104.
- `MON01` (10.10.10.40) — для сравнения метрик, не обязательно.
- `HARBOR01` (10.10.10.55) — можно тянуть образы оттуда; в примерах ниже используется публичный `nginx` (если Docker Hub из вашей сети недоступен, подставь образ из HARBOR01 и укажи его в манифесте).
- SSH-доступ с `k8s-cp01` на `k8s-w01` под пользователем с sudo.

### Снапшот

В VMware Workstation — снапшот **всех трёх** ВМ кластера с именем `before-devops-109`:

- `k8s-cp01` → Snapshot → Take Snapshot → имя `before-devops-109`
- `k8s-w01` → `before-devops-109`
- `k8s-w02` → `before-devops-109`

Важно: в кластере откат одной ноды рассинхронизирует etcd и состояние Cilium. Откатывать — только все три одновременно.

### Шаги

1. **Проверка ядра и BTF** — на `k8s-w01` (SSH с `k8s-cp01`).
   `uname -r; test -f /sys/kernel/btf/vmlinux && echo BTF-ok`
   Ожидаемо: `6.8.0-...-generic` и `BTF-ok`. Если BTF нет — дальше пойдёт только часть инструментов, зафиксируй это в журнале.

2. **Установка инструментов** — на `k8s-w01`.
   `sudo apt update && sudo apt install -y bpftrace bpfcc-tools linux-tools-common linux-tools-$(uname -r)`
   Ожидаемо: `bpftrace --version` печатает версию, `sudo bpftool prog list` работает.

3. **Развернуть демо-приложение** — на `k8s-cp01`. Создай `hr-portal.yaml`:

   ```yaml
   apiVersion: v1
   kind: Namespace
   metadata:
     name: hr-portal
   ---
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: hr-portal
     namespace: hr-portal
   spec:
     replicas: 2
     selector:
       matchLabels: { app: hr-portal }
     template:
       metadata:
         labels: { app: hr-portal }
       spec:
         containers:
           - name: web
             image: nginx:stable
             ports:
               - containerPort: 80
   ---
   apiVersion: v1
   kind: Service
   metadata:
     name: hr-portal
     namespace: hr-portal
   spec:
     selector: { app: hr-portal }
     ports:
       - port: 80
         targetPort: 80
   ```

   `kubectl apply -f hr-portal.yaml`
   Ожидаемо: `namespace/hr-portal created`, `deployment.apps/hr-portal created`, `service/hr-portal created`; `kubectl -n hr-portal get pods` — два пода `Running`.

4. **Снять execsnoop на узле** — на `k8s-w01` запусти трассировку из п. 4.6. Не закрывая её, на `k8s-cp01` выполни:
   `kubectl -n hr-portal exec deploy/hr-portal -- sh -c 'echo hello > /tmp/x'`
   Ожидаемо: в выводе bpftrace появились строки с `runc`, `sh`, `echo`. Это твой первый «живой» перехват контейнерных процессов.

5. **Увидеть файловый ввод-вывод пода** — на `k8s-w01`:
   `sudo bpftrace -e 'kretprobe:vfs_read /comm == "nginx"/ { @bytes = hist(retval); }'`
   Затем с `k8s-cp01`: `kubectl -n hr-portal exec deploy/hr-portal -- sh -c 'head -c 200000 /etc/services > /dev/null'`
   Ожидаемо: гистограмма наполнилась, в старших корзинах (64K, 128K) появились значения — nginx читает крупными блоками.

6. **Профиль CPU по стекам** — на `k8s-w01`:
   `sudo bpftrace -e 'profile:hz:99 { @[comm, kstack] = count(); }'`
   Дай поработать 15–20 секунд, Ctrl-C. Ожидаемо: список стеков с counts; топ — `swapper`/`kubelet`/`containerd-shim` в простое. Запиши топ-3 в журнал.

7. **Проверить Cilium и включить Hubble** — на `k8s-cp01`:
   `cilium status --wait`
   Ожидаемо: `Cilium: Ok`, `KubeProxyReplacement: True`, `Hubble: Disabled/Ok`.
   Далее: `cilium hubble enable --ui && cilium status --wait`
   Ожидаемо: поды `hubble-relay` и `hubble-ui` в `kube-system` в статусе Running.

8. **Пробросить порт и посмотреть потоки** — на `k8s-cp01`:
   `cilium hubble port-forward &` затем `hubble status`
   Ожидаемо: `Healthcheck: Ok`, список нод.
   Сгенерируй трафик:
   `kubectl -n hr-portal run curl-test --image=curlimages/curl --rm -it --restart=Never -- curl -sS -m 5 http://hr-portal.hr-portal.svc.cluster.local`
   Затем: `hubble observe --namespace hr-portal --last 20 -o compact`
   Ожидаемо: строки `http-request FORWARDED (GET /)` и `http-response FORWARDED (GET / 200 ...ms)`.

9. **Проверить, что eBPF-программы Cilium реально в ядре** — на `k8s-w01`:
   `sudo bpftool prog list | grep -c cilium`
   Ожидаемо: число порядка десятков. Сравни с `k8s-w02` — должно быть сопоставимо.

10. **Посмотреть L7 в разрезе латентности** — на `k8s-cp01`:
    `hubble observe --namespace hr-portal --type l7 --last 50 -o compact`
    Ожидаемо: видно коды ответов и время. Это тот самый инструмент, который отвечает на «почему 5 секунд» — если в потоке `http-response ... 200 8ms`, а пользователь ждёт 5 секунд, значит проблема до кластера (DNS, сеть, браузер).

11. **Опционально: Pixie.** Если на кластере есть свободные 4 ГБ RAM:
    `px deploy` → `kubectl get pods -n pl`
    Ожидаемо: поды Pixie в Running. Если кластер «задыхается» — `px delete` и зафиксируй в журнале, что не хватило ресурсов (это тоже результат).

12. **Записать изменения** в `stand-log.md`: namespace `hr-portal`, включённый Hubble, установленные пакеты на `k8s-w01`.

### Проверка

- [ ] `uname -r` на `k8s-w01` и наличие `/sys/kernel/btf/vmlinux` подтверждены
- [ ] `bpftrace --version` и `bpftool prog list` работают
- [ ] `kubectl -n hr-portal get pods` — 2/2 Running
- [ ] execsnoop показал события от `kubectl exec`
- [ ] гистограмма `vfs_read` для nginx непустая
- [ ] `cilium status` — `Cilium: Ok`, `KubeProxyReplacement: True`
- [ ] `hubble status` — `Healthcheck: Ok`
- [ ] `hubble observe` показал `http-request FORWARDED` для тестового curl
- [ ] `bpftool prog list | grep -c cilium` > 0
- [ ] запись в `stand-log.md` сделана

### Сломай и почини

> **Пользователь:** Петрова (HR) / ВМ WIN10-CL02
> **Что написала:** «Портал кадровых заявок вообще перестал открываться — крутится и выдаёт 502. Пять минут назад работал!»
> **Срочность:** высокая

**Как воспроизвести поломку.** На `k8s-cp01` примени «забытую» политику:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: hr-portal-deny
  namespace: hr-portal
spec:
  podSelector:
    matchLabels: { app: hr-portal }
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: monitoring }
      ports:
        - protocol: TCP
          port: 80
```

`kubectl apply -f netpol-deny.yaml`. Теперь в поды пускает только namespace `monitoring`; ingress-контроллер и тестовый curl получают таймаут → пользователь видит 502.

**Диагностика по порядку:**

1. `kubectl -n hr-portal get pods` — поды Running? Да. Значит проблема не в приложении.
2. `kubectl -n hr-portal get endpoints hr-portal` — endpoint'ы есть (два IP)? Да. Значит Service находит поды.
3. Проверить с тестового пода: `kubectl -n hr-portal run curl-test --image=curlimages/curl --rm -it --restart=Never -- curl -sS -m 5 -v http://hr-portal.hr-portal.svc.cluster.local` → висит и падает по таймауту. Трафик не доходит.
4. **Hubble:** `hubble observe --namespace hr-portal --verdict DROPPED --last 30 -o compact`. Видим `DROPPED` с reason вроде `Policy denied` и направлением `-> hr-portal/...:80`. Гипотеза: сетевая политика.
5. Подтвердить: `kubectl -n hr-portal get networkpolicy` → есть `hr-portal-deny`, которого вчера не было. Плюс `kubectl -n hr-portal describe networkpolicy hr-portal-deny`.
6. Проверить на уровне ядра на `k8s-w01`: `kubectl -n kube-system exec ds/cilium -- cilium monitor -t drop` — видно дропы с указанием политики.
7. **Решение:** согласовать с владельцем приложения (в учебной среде — просто удалить) `kubectl -n hr-portal delete networkpolicy hr-portal-deny`, повторить curl — 200 OK, `hubble observe` показывает `FORWARDED`.

**Ответ пользователю:** «Портал снова доступен, проверьте, пожалуйста. Причина — в кластер применили правило сетевой изоляции, которое блокировало доступ к подам портала. Правило снято; чтобы не повторилось, добавим проверку политик в CI и алерт на рост дропов в мониторинге.»

**Закрытие заявки:** указать root cause (NetworkPolicy), время восстановления, действие по предотвращению (проверка netpol в пайплайне, алерт на `hubble_drop_total` [ПРОВЕРИТЬ имя метрики]), тег `network-policy`.

### Что изменилось на стенде

- `k8s-w01`: установлены `bpftrace`, `bpfcc-tools`, `linux-tools-*`.
- Кластер: включён Hubble (relay + UI) в `kube-system`.
- Новый namespace `hr-portal` с Deployment `hr-portal` (2 реплики, `nginx`), Service `hr-portal` и NetworkPolicy `hr-portal-deny` (удалена по итогам разбора — зафиксировать состояние).
- В `stand-log.md` добавлена запись: «Урок 109: eBPF. Hubble включён, порт 4245 проброшен локально, bpftrace на k8s-w01, namespace hr-portal создан».

### Откат

- Если сломался только `k8s-w01`: сначала попробуй `sudo apt remove --purge bpftrace bpfcc-tools`. Если сломан кластер — выключи **все три** ВМ и откати `before-devops-109` на каждой, затем включай в порядке `k8s-cp01` → `k8s-w01` → `k8s-w02`.
- Hubble отключается без снапшота: `cilium hubble disable` (или `helm upgrade` без `hubble.enabled`).
- Демо-стенд убирается так: `kubectl delete namespace hr-portal`.
- Pixie: `px delete` (или удалить namespace `pl`).
- Откат снапшота **одной** ноды кластера — плохая идея: etcd и Cilium разъедутся. Откатывай весь кластер.

## 6. Ошибки новичков и безопасность

1. **Запускать bpftrace в продакшене без ограничения по времени и фильтров.**
   → Кольцевые буферы и `printf` на каждом событии на нагруженном узле дают заметный оверхед и забивают вывод.
   → Всегда ограничивай: фильтр `/comm == "..." /`, `interval:s:10` для сброса, `timeout` в скрипте, запускай на одной ноде.

2. **Думать, что kprobe-диагностика переносима между ядрами.**
   → Имена функций ядра меняются между версиями, скрипт падает или, хуже, читает не то поле.
   → Предпочитай `tracepoint` (стабильный ABI), `fentry`/`fexit` через BTF и CO-RE. Скрипты с kprobe подписывай версией ядра.

3. **Путать «дроп в Hubble» с «дропом в приложении».**
   → Hubble видит только то, что прошло через eBPF Cilium; приложение может само отдавать 503.
   → Смотри и `verdict`, и `http-response ... 5xx`; при наличии Pixie — сравни с трейсами приложения.

4. **Ставить Pixie «на всякий случай» на маленький кластер.**
   → Vizier съедает несколько гигабайт RAM и CPU на каждом узле, поды начинают эвиктиться, и вы получаете новую аварию вместо наблюдаемости.
   → Считай ресурсы заранее, ставь в отдельный пул нод, сравнивай с альтернативами (Hubble + OTel, Grafana Alloy).

5. **Верить в «eBPF безопасен, потому что есть верификатор».**
   → Верификатор гарантирует отсутствие падений ядра, а не отсутствие утечки данных или обхода политик; eBPF-программа с правами root читает память любого процесса.
   → Минимальные привилегии, `kernel.unprivileged_bpf_disabled=2`, аудит того, кто и что загружает (`bpftool prog list`), подпись/контроль DaemonSet'ов.

6. **Запускать bpftrace в контейнере без `privileged` и ждать результата.**
   → BPF требует `CAP_BPF`/`CAP_SYS_ADMIN`, доступа к `/sys/kernel/btf` и `tracefs`; в обычном контейнере получите `Operation not permitted`.
   → Для лабы — запускай на узле по SSH. Для кластера — `kubectl debug node/k8s-w01` с privileged-контейнером, понимая, что это дыра в изоляции.

7. **Игнорировать, что на L7 виден трафик в открытом виде.**
   → Pixie/Hubble с L7-видимостью по сути видят содержимое запросов (пути, коды, иногда тела); это персональные данные, если в них есть ПДн.
   → Ограничивать доступ к `hubble observe` и Pixie, ретеншен данных, а для систем с ПДн — согласовать с ИБ и по 152-ФЗ определить состав данных и сроки хранения.

8. **Оставлять `unprivileged_bpf_disabled=0` ради удобства.**
   → Любой локальный пользователь может загрузить eBPF-программу, включая руткит-подобную.
   → Значение `2`, установка через sysctl в конфиге загрузчика, проверка в аудите.

**Безопасность (что нельзя в боевой среде):** нельзя ставить Cilium в режим kube-proxy replacement «на живом кластере» без окна обслуживания и плана отката; нельзя давать разработчикам `cluster-admin` «чтобы посмотрели Hubble»; нельзя отправлять данные Pixie во внешний SaaS без оценки 152-ФЗ и согласования с ИБ. И помни про реалии РФ: Cilium, Pixie и bpftrace — это OSS из CNCF, в реестре отечественного ПО их нет. В ГИС и системах с требованиями ФСТЭК их применение нужно согласовывать, а часть задач (runtime-безопасность) решать сертифицированными средствами; коммерческую поддержку Cilium оказывает Isovalent (Cisco) [ПРОВЕРИТЬ актуальный статус], в РФ распространены дистрибутивы Kubernetes с Cilium «из коробки» (например, Deckhouse) [ПРОВЕРИТЬ в документации конкретной версии]. Импортозамещённые ОС (Astra Linux, РЕД ОС, ALT) содержат современные ядра с поддержкой eBPF, но набор доступных инструментов и версии нужно проверять на месте [ПРОВЕРИТЬ].

## 7. Вопросы на собеседовании

**1. Что такое eBPF и почему это не модуль ядра?**

Ответ: eBPF — виртуальная машина в ядре Linux. Программа на C/DSL компилируется в байткод, проверяется верификатором (типы указателей, границы, отсутствие бесконечных циклов, размер стека) и JIT-компилируется в машинный код, после чего прикрепляется к точке (kprobe, tracepoint, XDP, tc, cgroup, LSM). Общается с user space через maps. Отличие от модуля: модуль — произвольный код в кольце 0 с полным доступом, ошибка = kernel panic; eBPF-программа ограничена верификатором и набором helper'ов, поэтому безопаснее, но и возможностей меньше. Модуль надо собирать под конкретное ядро; eBPF с CO-RE переносим.

*Что хочет услышать интервьюер:* верификатор, JIT, maps, attach points, CO-RE, понимание разницы в рисках.
*Красный флаг:* «eBPF — это просто tcpdump», «eBPF заменяет модули полностью» или незнание слова «верификатор».

**2. kprobe, tracepoint, uprobe, fentry — в чём разница и что выбрать?**

Ответ: kprobe/kretprobe — динамическая привязка к любой функции ядра, максимально гибко, но интерфейс нестабилен (имена меняются, аргументы через регистры, нужны `bpf_probe_read_*`). tracepoint — статические, документированные точки ядра со стабильным форматом, предпочтительны для продакшена. uprobe/uretprobe — то же, но для user space бинарников и библиотек: профилирование приложений без пересборки. fentry/fexit — привязка к функции через BTF, самый быстрый и типобезопасный вариант для современных ядер, постепенно вытесняет kprobe. Правило: есть tracepoint или fentry — бери их; kprobe — когда другого выхода нет.

*Что хочет услышать интервьюер:* понимание стабильности ABI и цены «гибкости», практический выбор.
*Красный флаг:* «всегда kprobe, он же универсальный».

**3. Как Cilium использует eBPF и чем это лучше kube-proxy с iptables?**

Ответ: Cilium заменяет kube-proxy eBPF-программами в точке tc (ingress/egress) и на сокетах: балансировка, DNAT, политики L3/L4, а с L7-парсером — политики по HTTP/gRPC. Сервисный lookup идёт по hash-таблицам, а не по цепочкам iptables, поэтому время обработки не растёт линейно с числом сервисов; нет «перестройки всех правил» при каждом изменении. Плюс identity-based политики (по меткам pod'ов, а не по IP), встроенный Hubble для наблюдаемости, опционально WireGuard-шифрование и Tetragon для runtime-безопасности. Минусы: требуется современное ядро, отладка сложнее (не посмотришь `iptables -L`), больше требований к экспертизе.

*Что хочет услышать интервьюер:* понимание проблемы масштабирования iptables, место eBPF в датапасе, готовность назвать минусы.
*Красный флаг:* «Cilium просто быстрее» без объяснения почему; незнание, что это CNI.

**4. Под не отвечает, в логах приложения чисто. Как найдёшь причину с помощью eBPF-инструментов?**

Ответ: по слоям, сверху вниз. Сначала `kubectl get pods/endpoints` — жив ли под и есть ли endpoint'ы. Дальше Hubble: `hubble observe --namespace X --verdict DROPPED` — есть ли дропы политикой; `hubble observe --type l7` — доходят ли HTTP-запросы и с какими кодами/латентностью. Если дропов нет и запрос не доходит — `cilium monitor -t drop` на ноде. Если доходит до ядра, но не до приложения — bpftrace на узле: `tracepoint:tcp:tcp_retransmit_skb` (ретраи), `kprobe`-гистограмма по `vfs_read` (диск), `profile:hz:99` по `kstack` (где CPU). Если нужен L7 без политик — Pixie. Параллельно проверяю DNS (`hubble observe --protocol dns` или `dig` из пода) и латентность на ingress.

*Что хочет услышать интервьюер:* системный порядок «от L3 к L7 и от кластера к ядру», а не «сразу запущу tcpdump».
*Красный флаг:* «перезапущу под, вдруг поможет» или отсутствие плана.

**5. Какие риски безопасности несёт eBPF?**

Ответ: три группы. Первая — сама возможность загрузить код в ядро: при `unprivileged_bpf_disabled=0` локальный пользователь может загрузить программу, включая вредоносную; поэтому значение должно быть `2`, а доступ к `bpf(2)` — только у привилегированных. Вторая — уязвимости в верификаторе и JIT (за историю было несколько CVE, приводящих к повышению привилегий), значит нужен регулярный патчинг ядра. Третья — эксплуатационная: privileged DaemonSet с bpftrace/Pixie в кластере равен root на всех нодах, а L7-видимость означает доступ к содержимому запросов (потенциально ПДн). Меры: минимальные привилегии, аудит загруженных программ (`bpftool prog list`), ограничение доступа к Hubble/Pixie, ретеншен и маскирование данных, соответствие 152-ФЗ.

*Что хочет услышать интервьюер:* что кандидат не считает eBPF «безопасным по определению» и знает про привилегии и CVE.
*Красный флаг:* «верификатор всё проверяет, рисков нет».

## 8. Источники (официальная документация)

- **ebpf.io** — раздел «What is eBPF?» и карта экосистемы. Поиск: `ebpf.io what is ebpf`.
- **docs.ebpf.io** — официальная документация по ядру и eBPF: verifier, program types, maps, CO-RE. Поиск: `docs.ebpf.io verifier`, `docs.ebpf.io program types`.
- **cilium.io** (docs.cilium.io) — разделы «Cilium Introduction», «Kubernetes Without kube-proxy», «Network Policy», «Hubble»: `hubble observe`, `cilium status`, `cilium monitor`. Поиск: `cilium docs hubble observe`, `cilium kube-proxy replacement`.
- **github.com/bpftrace/bpftrace** — man page, «Reference Guide», «One-Liner Tutorial», каталог `tools/`. Поиск: `bpftrace reference guide`, `bpftrace one-liner tutorial`.
- **px.dev** (docs) — документация Pixie: установка (`px deploy`), PxL-скрипты, требования к ресурсам. Поиск: `pixie docs px deploy`, `pixie pxl scripts`.
- **kubernetes.io** — «Network Policies» (для лабораторной с блокировкой). Поиск: `kubernetes network policies`.
- **kernel.org** — Documentation/bpf, включая BTF и CO-RE. Поиск: `kernel.org bpf documentation core`.
- **man7.org** — man-страницы `bpftrace(8)`, `bpf(2)`, `bpf-helpers(7)`. Поиск: `man7 bpf-helpers`.

Всё, что касается точных версий (минимальные ядра для Cilium/Pixie, номера портов Hubble UI, имена подкоманд `px`, названия метрик дропов), сверяй с документацией под свою версию — в тексте такие места помечены `[ПРОВЕРИТЬ]`.
