# Высокая доступность: Pacemaker/Corosync, keepalived, HAProxy, балансировка и отказоустойчивость

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди: **Петрова / WIN10-CL02 / «Открываю внутренний портал — то грузится, то "сайт недоступен". У Иванова то же самое. Началось после обеда, работу останавливает» / Срочность: высокая.**

Ты приходишь на рабочее место, открываешь портал — и правда: пять запросов проходят, два висят до таймаута, один отдаёт 502. Сервер один, он живой, `systemctl status nginx` — `active (running)`. И вот тут начинается тема, которой посвящён этот урок: одна живая машина ещё не значит «сервис работает», и одна живая машина — это всегда единая точка отказа.

Высокая доступность (High Availability, HA) — это про то, чтобы сервис продолжал работать при падении отдельного сервера, сети, диска или процесса. В работе это встречается постоянно: внутренние порталы, 1С-публикации, прокси, входные точки для платёжных сервисов, публикация в интернет. После этой главы ты должен уметь: собрать двухузловой кластер Pacemaker/Corosync с плавающим IP; поднять пару балансировщиков HAProxy с VIP через keepalived; объяснить, чем VRRP отличается от кластера ресурсов; провести управляемый отказ и доказать, что сервис пережил его; и, главное, объяснить, почему «просто два сервера с одним IP» — это не HA, а генератор аварий.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Разделим тему на три независимых слоя — их постоянно путают, и на собеседовании это ловится первым делом.

**Слой 1. Плавающий адрес (VIP) и VRRP.** VIP (virtual IP) — адрес, который принадлежит не машине, а сервису. Он «переезжает» между узлами. Механизм — протокол **VRRP** (Virtual Router Redundancy Protocol, RFC 5798): узлы шлют друг другу анонсы (advertisement) раз в секунду; у кого приоритет выше — тот MASTER и держит IP, остальные BACKUP и слушают. Пропали анонсы от мастера на 3 интервала — BACKUP становится MASTER и делает gratuitous ARP, чтобы коммутатор и клиенты перестроили ARP-таблицу на новый MAC. VRRP работает поверх IP, номер протокола **112**, multicast-группа **224.0.0.18**. В Linux это реализует `keepalived` (плюс умеет проверять здоровье сервиса скриптами).

**Слой 2. Кластер ресурсов: Pacemaker + Corosync.** Это когда нужно перевозить не только IP, но и сервис, монтирование, том, БД. **Corosync** — транспорт: членство в кластере, кворум, обмен сообщениями (в современных версиях транспорт knet, UDP-порт **5405**). **Pacemaker** — CRM (Cluster Resource Manager): решает, где запускать ресурсы, следит за ними, перезапускает, мигрирует. Ресурсы описываются **resource agent**'ами (OCF-скрипты): `IPaddr2`, `systemd:nginx`, `Filesystem`, `pgsql`. Управление — утилитой `pcs` (демон `pcsd`, TCP **2224**). Есть понятие **кворума**: пока в кластере большинство голосов, он имеет право работать; потеряв кворум, корректный кластер останавливает ресурсы, а не делит их. И есть **STONITH/fencing** — «застрелить» отказавший узел (через IPMI, SBD+watchdog, гипервизор), чтобы он гарантированно не писал на общий диск.

**Слой 3. Балансировка: HAProxy.** L4-режим (`mode tcp`) — проксирует TCP-поток, ничего не понимая в содержимом. L7-режим (`mode http`) — разбирает HTTP, умеет маршрутизировать по Host/URL/header, отдавать статистику, делать health checks до конкретного URL. Алгоритмы: `roundrobin`, `leastconn`, `source` (по IP клиента — грубая привязка сессии). HAProxy в схеме с двумя экземплярами сам становится точкой отказа — поэтому их пара, а VIP перед ними раздают через keepalived.

Схема стенда (что соберём в лабе):

```text
        клиенты 10.10.10.0/24 (WIN11-CL01, WIN10-CL02)
                          |
                 VIP 10.10.10.210  (VRRP, proto 112)
                 /                      \
        LB01 10.10.10.33          LB02 10.10.10.34      (HAProxy, mode http)
        10.10.20.31               10.10.20.32           (второй адаптер, DMZ)
                 \                      /
             balance roundrobin + httpchk
                 /                      \
        WEB01 10.10.20.10         WEB02 10.10.20.11     (nginx)
                 
     --- отдельный кластер Pacemaker/Corosync ---
     UBU01 10.10.10.30   <--- corosync UDP 5405 --->   UBU02 10.10.10.32
     ресурсы: p_vip (10.10.10.200) + p_web (systemd:nginx)
```

Где это в OSI: VRRP — L3 (протокол поверх IP), балансировка L4 — транспортный уровень, L7 — прикладной. Порты, которые надо помнить наизусть: VRRP = IP proto 112; Corosync/knet = UDP 5405; pcsd = TCP 2224; pacemaker-remoted = TCP 3121 `[ПРОВЕРИТЬ]`.

## 3. Аналогия из жизни

Представь вахту на проходной бизнес-центра. Стоит один охранник с бейджем «Вахта» (VIP). Рядом второй, без бейджа, но каждую секунду смотрит на первого: если тот жив и машет рукой (анонс VRRP) — второй просто наблюдает. Первый ушёл в туалет и не машет 3 секунды — второй надевает бейдж и начинает пускать людей (gratuitous ARP: «теперь вход через меня»). Если оба надели бейдж одновременно — посетители идут к разным охранникам с одинаковыми правами, а журнал посещений превращается в кашу: это split-brain. Поэтому в серьёзных зданиях есть ещё и инструкция «если напарник не отвечает — сначала обесточь его пост» (fencing), чтобы он точно ничего не подписывал.

Аналогия ломается на балансировщике: HAProxy — это не второй охранник, а диспетчер, который распределяет посетителей по трём разным стойкам регистрации, и умеет перестать отправлять людей к стойке, где сломался принтер.

## 4. Практика и команды

**1. Установка и первичная настройка узлов Pacemaker (Ubuntu).**

```bash
sudo apt update
sudo apt install -y pacemaker corosync pcs resource-agents fence-agents curl tcpdump
sudo systemctl enable --now pcsd
sudo passwd hacluster          # пароль кластерной учётки; на стенде Lab#2026-vektor
```
`pcs` — CLI управления, `pcsd` — его демон на каждом узле (TCP 2224), учётка `hacluster` — общая для всех узлов кластера. Набор пакетов может отличаться по версии дистрибутива — `[ПРОВЕРИТЬ]` через `apt-cache search pacemaker`.

**2. Создание кластера.**

```bash
# на обоих узлах: /etc/hosts → 10.10.10.30 ubu01, 10.10.10.32 ubu02
# hostnamectl set-hostname ubu01 | ubu02  (имя должно совпадать с /etc/hosts!)
sudo pcs host auth ubu01 ubu02 -u hacluster -p 'Lab#2026-vektor'
sudo pcs cluster setup ha-vektor ubu01 ubu02 --force
sudo pcs cluster start --all
sudo pcs cluster enable --all
```
`host auth` проверяет доступность pcsd и пароль; `cluster setup` генерирует `/etc/corosync/corosync.conf`; `start/enable --all` — запуск и автозапуск на всех узлах. Типичный вывод `pcs status`: список узлов `Online: [ ubu01 ubu02 ]`, `Full list of resources:` и `Daemon Status`.

**3. Свойства кластера и кворум.**

```bash
sudo pcs property set stonith-enabled=false
sudo pcs property set no-quorum-policy=ignore
sudo pcs quorum status
sudo corosync-quorumtool -s
```
`stonith-enabled=false` — **только для лаборатории** (см. раздел 6). `no-quorum-policy=ignore` разрешает работать в одиночку при потере кворума — в проде так делать нельзя. `corosync-quorumtool` покажет `Quorum: N`, `Expected votes`, `Total votes` — смотреть, что votes = 2 и Quorum = 1 при двух узлах (кворум по большинству).

**4. Ресурсы и группа.**

```bash
sudo pcs resource create p_vip ocf:heartbeat:IPaddr2 ip=10.10.10.200 cidr_netmask=24 op monitor interval=10s
sudo pcs resource create p_web systemd:nginx op monitor interval=15s --group g_web
sudo pcs resource create p_vip2 ...   # нет: VIP создаётся один раз
sudo pcs status --full
```
`IPaddr2` — ресурс плавающего адреса; `systemd:nginx` — управление юнитом через systemd; `--group g_web` — порядок и совместное размещение (сначала VIP, потом сервис). `op monitor interval=10s` — как часто проверять живость ресурса.

**5. Управляемый отказ (failover) и возврат.**

```bash
sudo pcs resource move g_web ubu02     # принудительная миграция
sudo pcs status
sudo pcs resource clear g_web          # снять наложенное ограничение
sudo pcs node standby ubu01            # «вывести узел на обслуживание»
sudo pcs node unstandby ubu01
```
`move` создаёт временное location-constraint; без `clear` ресурс навсегда останется на ubu02. `standby` — штатный способ обслуживать узел: ресурсы уезжают, узел остаётся в кластере.

**6. Диагностика Corosync.**

```bash
sudo corosync-cfgtool -s
sudo corosync-cfgtool -n
sudo journalctl -u corosync -u pacemaker -n 50 --no-pager
```
`-s` показывает линки и статус соединения между узлами (в knet — `Link status`), `-n` — какие адреса реально используются. Если узлы «не видят» друг друга — здесь видно первым делом: чаще всего это firewall или разные сети на интерфейсах.

**7. keepalived: конфиг и проверка.**

```bash
sudo keepalived -t -f /etc/keepalived/keepalived.conf   # config test
sudo systemctl enable --now keepalived
sudo systemctl status keepalived --no-pager
sudo journalctl -u keepalived -f
```
`-t` проверяет синтаксис конфига без запуска `[ПРОВЕРИТЬ флаг в вашей версии keepalived]`. `journalctl -f` — обязательный инструмент: переходы состояний (`Entering MASTER STATE`, `Entering BACKUP STATE`) видны только там.

**8. Проверка VIP и ARP.**

```bash
ip -br a
ip -br a | grep 10.10.10.210
ip neigh show | grep 10.10.10.210
arping -I ens33 10.10.10.210 -c 3
```
`ip -br a` — компактный список адресов: VIP на мастере будет в строке интерфейса. `arping` с клиента/соседнего узла подтверждает, что VIP отвечает на ARP.

**9. Смотрим VRRP в эфире.**

```bash
sudo tcpdump -ni ens33 proto 112 -c 5
```
Типичный вывод: `IP 10.10.10.33 > 224.0.0.18: VRRPv2, Advertisement, vrid 51, prio 150, authtype none, intvl 1s`. Смотреть: анонсы идут **с одного** узла, prio соответствует конфигу, `vrid` совпадает на обеих машинах. Если видно анонсы с двух узлов с одним vrid — уже авария.

**10. HAProxy: проверка конфига и горячая перезагрузка.**

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo systemctl reload haproxy      # не restart: без обрыва соединений
sudo ss -lntp | grep -E ':(80|8404)'
```
`-c` — только валидация конфига, `reload` — плавное перечитывание (старые воркеры дорабатывают соединения). `ss -lntp` покажет, что HAProxy слушает ожидаемые адреса/порты.

**11. Статистика HAProxy через сокет.**

```bash
sudo mkdir -p /var/run/haproxy      # каталог должен существовать, иначе сокет не создастся
echo "show stat" | sudo socat stdio /var/run/haproxy/admin.sock | cut -d, -f1,2,18,5 | head
echo "show servers state" | sudo socat stdio /var/run/haproxy/admin.sock
```
Колонки CSV: `pxname,svname,status,...`; `status` = `UP`/`DOWN` у бэкендов — это то, что нужно смотреть при жалобе «502». Интерактивно: `sudo socat stdio /var/run/haproxy/admin.sock`, затем `help`, `show stat`, `quit`.

**12. Проверка сервиса с клиента.**

```bash
for i in $(seq 1 6); do curl -s http://10.10.10.210/ ; echo; done
curl -sI http://10.10.10.210/ | head -1
```
При `balance roundrobin` и двух живых бэкендах ответы чередуются (`web01`, `web02`). Если в браузере на WIN11-CL01 — просто открыть `http://10.10.10.210/`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 развёрнут: DC01 (10.10.10.10), FS01, WIN11-CL01, WIN10-CL02, UBU01 (10.10.10.30), ASTRA01, GW01 (10.10.10.3 / 10.10.20.1), WEB01 (10.10.20.10, nginx), MON01. Тема требует **четырёх новых ВМ** — добавляем их явно (это расширение таблицы стенда для трека «Системный админ»):

| Имя | ОС | Адреса | Роль |
|-----|----|--------|------|
| UBU02 | Ubuntu Server 24.04 LTS | 10.10.10.32 | второй узел кластера Pacemaker/Corosync |
| LB01 | Ubuntu Server 24.04 LTS | 10.10.10.33 (VMnet8) + 10.10.20.31 (VMnet10) | HAProxy + keepalived, узел 1 |
| LB02 | Ubuntu Server 24.04 LTS | 10.10.10.34 (VMnet8) + 10.10.20.32 (VMnet10) | HAProxy + keepalived, узел 2 |
| WEB02 | Ubuntu Server 24.04 LTS | 10.10.20.11 | второй бэкенд nginx в DMZ |

Новые VIP: **10.10.10.200** (кластер Pacemaker) и **10.10.10.210** (keepalived/HAProxy). Оба вне DHCP-пула DC01 (10.10.10.100–150). На WEB02 — шлюз 10.10.20.1, DNS 10.10.10.10. Если у тебя ещё не установлен nginx на WEB01 — вернись к уроку про веб-сервер в DMZ или поставь `sudo apt install -y nginx` и положи простую страницу.

### Снапшот

В VMware Workstation на **UBU01, UBU02, LB01, LB02, WEB01, WEB02** (и DC01 на всякий случай): правый клик по ВМ → Snapshot → имя `before-sysadmin-029`. Для уже существующих ВМ снапшот делается при выключенной или работающей ВМ — VMware снимет состояние памяти, главное дождаться завершения. Пароль стенда везде `Lab#2026-vektor`.

### Шаги

1. **Создать ВМ** UBU02, LB01, LB02, WEB02 (Ubuntu Server 24.04 minimal, пользователь `student`, тот же пароль). LB01 и LB02 — с **двумя** сетевыми адаптерами: первый VMnet8 (custom), второй VMnet10 (host-only).
2. **Сеть на UBU02.** На ubu02 — статический адрес:
   ```yaml
   # /etc/netplan/50-cloud-init.yaml (имя файла и интерфейсов проверь: ip -br link)
   network:
     version: 2
     ethernets:
       ens33:
         addresses: [10.10.10.32/24]
         routes:
           - to: default
             via: 10.10.10.2
         nameservers:
           addresses: [10.10.10.10]
   ```
   Применить `sudo netplan apply`, проверить `ping 10.10.10.10`. **Ожидаемый результат:** адрес держится, домен резолвится.
3. **Сеть на LB01/LB02.** То же самое, но два интерфейса: LB01 — `10.10.10.33/24` (default via 10.10.10.2) и `10.10.20.31/24` без шлюза; LB02 — `10.10.10.34/24` и `10.10.20.32/24`. На WEB02 — `10.10.20.11/24`, шлюз `10.10.20.1`.
4. **Имена и hosts.** На UBU01, UBU02, LB01, LB02 добавить в `/etc/hosts` строки `10.10.10.30 ubu01`, `10.10.10.32 ubu02`, `10.10.10.33 lb01`, `10.10.10.34 lb02`. Проверить: `hostnamectl` показывает совпадающее имя. **Результат:** `ping ubu02` работает по имени.
5. **Пакеты.** На UBU01/UBU02: `pacemaker corosync pcs resource-agents nginx`. На LB01/LB02: `haproxy keepalived socat curl tcpdump nftables`. На WEB02: `nginx curl`. Везде проверить `timedatectl` — расхождение времени ломает TLS и логи.
6. **Контент бэкендов.** На WEB01 и WEB02: `echo "<h1>$(hostname)</h1>" | sudo tee /var/www/html/index.html`. На UBU01/UBU02 то же самое, затем `sudo systemctl disable --now nginx` (сервисом будет управлять Pacemaker). **Результат:** `curl http://10.10.20.11/` отдаёт `web02`; на ubu01 nginx остановлен.
7. **Кластер Pacemaker.** Выполнить команды из п.4.2–4.4 практики на UBU01 (пароль `hacluster` = `Lab#2026-vektor` на обоих узлах). **Результат:** `pcs status` — оба узла `Online`, группа `g_web` с `p_vip` и `p_web` запущена на одном узле; `curl http://10.10.10.200/` отдаёт имя этого узла.
8. **HAProxy на LB01/LB02.** Создать `/etc/haproxy/haproxy.cfg`:
   ```text
   global
       log /dev/log local0
       maxconn 2000
       stats socket /var/run/haproxy/admin.sock mode 660 level admin
       stats timeout 30s

   defaults
       mode http
       log global
       option httplog
       option dontlognull
       timeout connect 5s
       timeout client  30s
       timeout server  30s
       retries 2

   frontend fe_vektor_web
       bind 10.10.10.210:80
       default_backend be_vektor_web

   backend be_vektor_web
       balance roundrobin
       option httpchk GET /
       http-check expect status 200
       server web01 10.10.20.10:80 check
       server web02 10.10.20.11:80 check

   listen stats
       bind 10.10.10.210:8404
       stats enable
       stats uri /stats
   ```
   Разрешить bind на нелокальный адрес: `echo 'net.ipv4.ip_nonlocal_bind = 1' | sudo tee /etc/sysctl.d/90-nonlocal-bind.conf && sudo sysctl --system`. Затем `sudo mkdir -p /var/run/haproxy`, `sudo haproxy -c -f /etc/haproxy/haproxy.cfg`, `sudo systemctl enable --now haproxy`. **Результат:** `active (running)` на обоих.
9. **keepalived на LB01** (`/etc/keepalived/keepalived.conf`):
   ```text
   global_defs {
       router_id lb01
   }

   vrrp_script chk_haproxy {
       script "/usr/bin/systemctl is-active --quiet haproxy"
       interval 2
       timeout 2
       fall 3
       rise 2
       weight -30
   }

   vrrp_instance VI_VEKTOR_WEB {
       state MASTER
       interface ens33
       virtual_router_id 51
       priority 150
       advert_int 1
       unicast_src_ip 10.10.10.33
       unicast_peer {
           10.10.10.34
       }
       virtual_ipaddress {
           10.10.10.210/24 dev ens33
       }
       track_script {
           chk_haproxy
       }
   }
   ```
   На LB02 — тот же файл, но `router_id lb02`, `state BACKUP`, `priority 100`, `unicast_src_ip 10.10.10.34`, peer `10.10.10.33`. Запустить: `sudo systemctl enable --now keepalived`. Блок `authentication` из старых гайдов не добавляем — в keepalived 2.x аутентификация VRRP фактически выведена из употребления `[ПРОВЕРИТЬ в вашей версии]`, защищать надо сетью, а не паролем в анонсе.
10. **Проверка балансировки.** С UBU01: `for i in $(seq 1 6); do curl -s http://10.10.10.210/; echo; done`. **Результат:** чередование `web01` / `web02`; в браузере на WIN11-CL01 `http://10.10.10.210/stats` показывает два UP-бэкенда.
11. **Управляемые отказы.** (а) `sudo systemctl stop keepalived` на LB01 → VIP уехал на LB02, `curl` продолжает работать; вернуть `start`. (б) `sudo systemctl stop nginx` на WEB02 → через ~2 интервала проверки HAProxy помечает `web02` как `DOWN`, весь трафик идёт на web01; вернуть. (в) `sudo pcs node standby ubu01` → `p_vip` и `p_web` уезжают на ubu02, `curl http://10.10.10.200/` отдаёт `ubu02`; вернуть `unstandby`.
12. **Записать изменения** в `stand-log.md`: новые ВМ, адреса, VIP, имена ресурсов, пути конфигов.

### Проверка

- [ ] `pcs status` на UBU01: два узла `Online`, `g_web` — `Started` на одном узле, `p_vip` идёт первым.
- [ ] `ip -br a` на LB01/LB02: VIP `10.10.10.210` ровно на одном узле.
- [ ] `sudo tcpdump -ni ens33 proto 112 -c 3` — анонсы идут только с мастера.
- [ ] `curl -s http://10.10.10.210/` шесть раз подряд — оба имени бэкендов присутствуют.
- [ ] `echo "show stat" | sudo socat stdio /var/run/haproxy/admin.sock` — `web01` и `web02` в статусе `UP`.
- [ ] `curl -s http://10.10.10.200/` — имя узла-владельца ресурса, после `standby` меняется.
- [ ] `journalctl -u keepalived -n 20` — строки о переходе в MASTER/BACKUP без ошибок `VRRP_IF_...`.
- [ ] В `stand-log.md` записаны все новые адреса и VIP.

### Сломай и почини

> **Пользователь:** petrova
> **ВМ:** WIN10-CL02
> **Что написал:** «Портал по адресу 10.10.10.210 то открывается, то нет: иногда висит, иногда "сайт недоступен", иногда 502. Иванов говорит то же. Началось после обеда, работа стоит.»
> **Срочность:** высокая

**Как воспроизвести.** На LB02 добавляем правило, режущее VRRP (имитация «вчера настроили nftables по уроку 28 и закрыли лишнее»):

```bash
sudo nft add table inet lab
sudo nft 'add chain inet lab input { type filter hook input priority 0; }'
sudo nft add rule inet lab input ip protocol vrrp drop
sudo systemctl restart keepalived
```
LB02 перестаёт слышать мастера, объявляет себя MASTER, и VIP оказывается на двух машинах одновременно — классический split-brain уровня L2.

**Диагностика по порядку.**
1. С клиента: `curl -v http://10.10.10.210/` в цикле — часть запросов проходит, часть рвётся по таймауту/сбросу. Значит, проблема не в nginx-бэкенде.
2. `ip -br a` на LB01 **и** LB02 — VIP присутствует на обоих. Это уже приговор.
3. `journalctl -u keepalived -n 50` на LB02 — свежая запись `Entering MASTER STATE`, хотя LB01 жив.
4. `sudo tcpdump -ni ens33 proto 112` на LB02 — видим только исходящие анонсы, входящих от 10.10.10.33 нет.
5. `sudo nft list ruleset` на LB02 — находим правило `ip protocol vrrp drop`.
6. `systemctl is-active haproxy` на обоих — оба живы (значит, `chk_haproxy` не при чём).

**Решение.**

```bash
sudo nft delete table inet lab          # убрать блокировку VRRP
sudo systemctl restart keepalived
ip -br a | grep 10.10.10.210           # VIP должен быть только на одном узле
sudo journalctl -u keepalived -n 20
```
После этого на LB02 — `Entering BACKUP STATE`, VIP остаётся на LB01, `curl` в цикле стабилен.

**Ответ пользователю:** «Портал не работал из-за конфликта двух балансировщиков: один из них из-за сетевого правила перестал видеть напарника и оба одновременно отвечали по одному адресу. Правило убрал, сейчас балансировщики снова работают в паре. Проверьте, пожалуйста, страницу — если что-то ещё осталось, пришлите скриншот.»

**Закрытие заявки:** тип «сбой конфигурации», корневая причина — правило nftables, блокирующее VRRP (IP proto 112); действие — правило удалено, конфликт устранён; профилактика — согласовывать правила firewall для HA-узлов и завести на MON01 триггер «VIP виден на двух узлах одновременно» / «нет MASTER».

### Что изменилось на стенде

- Добавлены ВМ: **UBU02** (10.10.10.32), **LB01** (10.10.10.33 + 10.10.20.31), **LB02** (10.10.10.34 + 10.10.20.32), **WEB02** (10.10.20.11, nginx).
- Новые VIP: **10.10.10.200** (Pacemaker), **10.10.10.210** (keepalived/HAProxy).
- Кластер Pacemaker: имя **ha-vektor**, узлы ubu01/ubu02, группа **g_web** (`p_vip`, `p_web`), `stonith-enabled=false`, `no-quorum-policy=ignore`.
- LB01/LB02: `haproxy` (конфиг `/etc/haproxy/haproxy.cfg`, stats `:8404/stats`), `keepalived` (`/etc/keepalived/keepalived.conf`, `vrid 51`), `net.ipv4.ip_nonlocal_bind=1`.
- На UBU01/UBU02 установлен nginx, юнит выключен (`disable`) и управляется Pacemaker; на WEB01/WEB02 заменена страница `index.html` с именем хоста.
- В `/etc/hosts` на Linux-узлах добавлены имена ubu01, ubu02, lb01, lb02.

### Откат

Полный: выключить UBU02, LB01, LB02, WEB02 и удалить их из инвентаря; на UBU01 и WEB01 вернуться к снапшоту `before-sysadmin-029` (VM → Snapshot Manager → Restore). Частичный: `sudo pcs cluster destroy --all` на UBU01 (снимает кластер, оставляет машины), `sudo systemctl disable --now keepalived haproxy` на LB01/LB02, `sudo nft delete table inet lab` если правило осталось. После любого отката — запись в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **«Отключу stonith на минуту и забуду»** → при потере связи узлы делят ресурсы, два экземпляра пишут в один том, данные бьются необратимо. → Fencing настраивается **до** включения ресурсов: SBD + watchdog в лабе, `fence_ipmilan`/`fence_pve` в проде.
2. **Два узла с одним VIP из-за multicast/unicast-проблемы** → плавающие обрывы соединений, «иногда работает». → Всегда явный `unicast_peer` + правило firewall, разрешающее proto 112 только между двумя адресами, плюс мониторинг «VIP только на одном узле».
3. **Балансировка stateful-приложения без привязки сессии** → пользователь логинится и тут же вылетает, корзина пустеет. → Сессии выносить в Redis/БД или использовать `balance source`/cookie-привязку осознанно, понимая минусы.
4. **TCP-проверка вместо прикладной** → HAProxy считает бэкенд живым, потому что порт 80 открыт, хотя nginx отдаёт 500. → `option httpchk GET /healthz` + `http-check expect status 200` с отдельным эндпоинтом.
5. **VIP есть, а сервис мёртв** → keepalived без `vrrp_script` держит адрес на узле с упавшим HAProxy. → `track_script` с `weight` и проверкой именно своего демона.
6. **`systemctl restart haproxy` в рабочее время** → рвутся все соединения. → `systemctl reload` после `haproxy -c`.
7. **Хранение состояния локально** (загруженные файлы, кэш) → после failover всё «пропало». → Общее хранилище/NFS/объектное хранилище, приложение без локального состояния.
8. **Расхождение времени между узлами** → TLS/аутентификация/логи разъезжаются, поиск причины аварии превращается в квест. → chrony/`systemd-timesyncd` на всех узлах, проверка `timedatectl`.

**Безопасность.** В боевой среде: статистика HAProxy (`/stats`) не должна быть доступна без аутентификации и из внешней сети; admin-сокет — `mode 660` и только для доверенных админов; учётка `hacluster` — не стендовый пароль, а сгенерированный; VRRP пускать только между узлами (nftables), иначе любой в сети сможет подделать анонс с приоритетом 255 и «увести» VIP; fence-устройства имеют право выключать питание — это отдельная привилегированная зона, доступ к ней строго ограничен. На стенде пароль `Lab#2026-vektor` и `stonith-enabled=false` — учебные послабления, в реальной работе так нельзя.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**Вопрос 1. Чем keepalived+VRRP отличается от Pacemaker/Corosync? Что выберете для балансировщика, а что для БД?**
Ответ: keepalived — узкоспециализированный демон вокруг VRRP: плавающий IP и примитивные проверки скриптами. Pacemaker/Corosync — полноценный кластерный менеджер: кворум, ресурсы (IP, сервисы, ФС, том), зависимости и ограничения, fencing. Для пары HAProxy/nginx обычно достаточно keepalived — проще и меньше движущихся частей. Для БД или сервиса с монтируемым томом — Pacemaker, потому что там критичны порядок запуска, размещение и гарантия, что отказавший узел не пишет на диск.
*Что хочет услышать интервьюер:* понимание границ применимости, а не «мы всегда ставим Pacemaker».
*Красный флаг:* «Pacemaker — это то же самое, только от Red Hat» или уверенность, что keepalived умеет управлять монтированием тома.

**Вопрос 2. Что такое split-brain и почему STONITH обязателен?**
Ответ: split-brain — ситуация, когда узлы потеряли связь друг с другом, но оба живы и оба считают себя вправе владеть ресурсом. Результат: два мастера, два VIP, две записи в один том. Fencing (STONITH — Shoot The Other Node In The Head) гарантирует, что отказавший узел физически остановлен до того, как второй заберёт ресурсы: IPMI/управляемое питание, SBD на общем диске + watchdog, API гипервизора. Кворум дополнительно не даёт меньшинству принимать решения.
*Что хочет услышать интервьюер:* «без fencing не бывает HA, бывает лотерея», знание конкретных механизмов.
*Красный флаг:* «у нас stonith отключён, и всё нормально работает» без оговорок.

**Вопрос 3. L4 и L7 балансировка — в чём разница и когда что?**
Ответ: L4 (`mode tcp` в HAProxy) проксирует поток без разбора содержимого: быстро, подходит для любых протоколов, но не умеет маршрутизировать по URL и не даёт умных проверок. L7 (`mode http`) разбирает запросы: маршрутизация по Host/Path/header, sticky-сессии через cookie, прикладные health checks, статистика, завершение TLS. L7 нужен для веб-приложений и микросервисов, L4 — для БД, произвольных TCP, терминалов, максимальной производительности.
*Что хочет услышать интервьюер:* понимание, что «балансировщик» — это не одна технология, а выбор уровня и что L7 дороже по CPU.
*Красный флаг:* путаница L4/L7 с «внутренний/внешний» или «аппаратный/программный».

**Вопрос 4. Как доказать, что HA действительно работает, а не просто нарисована на схеме?**
Ответ: тестами отказов — по одному компоненту за раз: остановить keepalived на мастере (VIP должен уехать), остановить HAProxy на мастере (должен сработать `chk_haproxy`), погасить бэкенд (HAProxy должен снять его с `UP`), выключить целый узел кластера (`pcs node standby`, затем реальное выключение), проверить поведение при потере кворума и при возврате узла. Проверять с клиента, а не только `pcs status`. Плюс мониторинг: алерт на «VIP на двух узлах», на «нет MASTER», на «все бэкенды DOWN». И регламент учений — раз в квартал, с записью в журнал.
*Что хочет услышать интервьюер:* культуру проверки и мониторинга, понимание, что HA без учений — это вера.
*Красный флаг:* «мы всё проверили при настройке полгода назад».

**Вопрос 5. Почему HAProxy иногда не стартует на узле с VIP и как это лечится?**
Ответ: потому что он пытается забиндиться на адрес `10.10.10.210`, которого на этом узле в данный момент нет (VIP держит напарник), — bind падает, HAProxy выходит, `chk_haproxy` переводит keepalived в FAULT, и получается замкнутый круг. Решения: `net.ipv4.ip_nonlocal_bind = 1` (разрешает bind на нелокальный адрес) либо bind на `0.0.0.0` с фильтрацией на firewall. Первый вариант предпочтительнее.
*Что хочет услышать интервьюер:* что ты видел эту ошибку вживую, а не только в теории.
*Красный флаг:* «надо просто перезапустить keepalived» без объяснения причины.

## 8. Источники (официальная документация)

- **clusterlabs.org** — сайт проекта Pacemaker/Corosync. Разделы: «Pacemaker Documentation» (Clusters from Scratch, Pacemaker Administration), «pcs — Pacemaker/Corosync configuration system». Поиск: `pacemaker clusters from scratch`, `pcs command reference`.
- **clusterlabs.org** — «Resource Agents» (OCF-агенты: IPaddr2, systemd). Поиск: `ocf resource agents IPaddr2`.
- **haproxy.com** — «HAProxy Documentation», «Configuration Manual» (разделы `balance`, `option httpchk`, `stats socket`). Поиск: `haproxy configuration manual 2.8`.
- **keepalived.readthedocs.io** `[ПРОВЕРИТЬ актуальность домена]` — руководство keepalived, раздел «VRRP» и «Man page keepalived.conf». Поиск: `keepalived vrrp_instance unicast_peer`.
- **datatracker.ietf.org** — RFC 5798 «Virtual Router Redundancy Protocol (VRRP) Version 3 for IPv4 and IPv6». Поиск: `RFC 5798 VRRP`.
- **ubuntu.com** — «Ubuntu Server Guide», раздел про высокую доступность и пакет `pcs` `[ПРОВЕРИТЬ наличие раздела в текущей версии]`. Поиск: `ubuntu server guide high availability pacemaker`.
- **docs.redhat.com** — «Configuring and managing high availability clusters» (RHEL). Полезно как эталон терминологии fencing/SBD, даже если стенд на Ubuntu. Поиск: `red hat configuring high availability clusters sbd`.
- **docs.kernel.org** — описание `net.ipv4.ip_nonlocal_bind` в IP Sysctl. Поиск: `ip_nonlocal_bind kernel documentation`.
