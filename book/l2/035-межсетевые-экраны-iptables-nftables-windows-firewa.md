# Межсетевые экраны: iptables/nftables, Windows Firewall, правила, логирование

## 1. Введение (зачем это нужно в реальной работе)

Заявка в поддержку «Вектора»:

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Открываю внутренний портал http://10.10.20.10 — "сайт недоступен". Утром работало. Мне до 15:00 разослать приказ, портал нужен. Интернет при этом работает».
> **Срочность:** Высокая

Портал живёт в DMZ, за маршрутизатором GW01. Сайт «не открывается» — а nginx работает, сеть работает, DNS работает. Значит, пакет где-то умирает, и чаще всего — на межсетевом экране (firewall). Умение за 10 минут ответить на вопрос «кто именно съел пакет и записал ли он это в лог» — это тот навык, который отличает L2 от «перезагрузите компьютер».

После этой главы ты будешь уметь: читать и писать правила nftables и iptables, понимать, чем они отличаются и почему на Debian 12 важно, каким бэкендом ты пользуешься; настраивать Windows Firewall через PowerShell и `wf.msc`; включать логирование отброшенных пакетов с обеих сторон; видеть счётчики правил и таблицу соединений conntrack; и главное — диагностировать «порт не открывается» по шагам, а не перебором.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Netfilter** — это фреймворк в ядре Linux: набор «крючков» (hooks), через которые проходит каждый пакет. Правила в эти крючки пишутся утилитами: исторически `iptables`, современно — `nftables` (`nft`).

Пять хуков, каждый со своим приоритетом:

```text
      пакет пришёл
           │
     [prerouting]        ← DNAT, ранняя фильтрация
           │
    ┌──────┴───────┐  решение маршрутизации (куда пакет: мне или дальше?)
    │              │
[input]         [forward]        ← «мне» / «транзит»
    │              │
 локальный      [postrouting]     ← SNAT/masquerade
 процесс           │
    │          пакет ушёл
[output]
```

Важные следствия:
- Трафик **через** GW01 в DMZ обрабатывает цепочка `forward`, а не `input`. Классическая ошибка новичка — открыть правило в `input` и удивляться, что транзит не работает.
- `drop` — молча выбросить (клиент видит таймаут), `reject` — ответить ICMP/TCP-RST (клиент видит «connection refused»). Для диагностики это разные симптомы.

**Модель nftables:** таблица (`table`) → цепочка (`chain`, привязана к хуку и имеет policy) → правила (`rule`). Таблица имеет семейство: `inet` (IPv4+IPv6 сразу), `ip`, `ip6`, `arp`, `bridge`, `netdev`. Правила проверяются сверху вниз, первое совпавшее с терминальным действием (`accept`/`drop`/`reject`) решает судьбу пакета.

**iptables vs nftables:**
- iptables — четыре таблицы (`filter`, `nat`, `mangle`, `raw`) с фиксированными цепочками `INPUT/FORWARD/OUTPUT`. Каждое изменение = перезапись всего набора правил в ядро (не атомарно).
- nftables — одна универсальная структура: свои таблицы и цепочки с любыми именами, атомарная загрузка файла целиком (`nft -f`), сеты (`set`) и словари (`map`) для сотен адресов, счётчики (`counter`) и логирование прямо в правиле.
- На Debian 12 команда `iptables` — это обёртка `iptables-nft`, которая транслирует старый синтаксис в nftables. Проверяется через `iptables -V` — увидишь `(nf_tables)` или `(legacy)`. **Ключевой момент:** правила, созданные через `nft` в таблице `inet filter`, в выводе `iptables -L` не появятся — это разные таблицы. Не пугайся «пустого» iptables.

**conntrack** (connection tracking) — подсистема ядра, которая помнит состояние соединений: `NEW`, `ESTABLISHED`, `RELATED`, `INVALID`. Благодаря ей ты пишешь «пропустить established/related» одной строкой, а не открываешь обратный трафик для каждого сервиса. У неё есть лимит (`nf_conntrack_max`) и своя диагностика.

**Windows Firewall** устроен иначе: это надстройка над **WFP** (Windows Filtering Platform) — драйвером фильтрации в ядре, работающим на уровнях ALE (Application Layer Enforcement), transport и network. Служба `MpsSvc` (Windows Defender Firewall) управляет политиками, `BFE` (Base Filtering Engine) применяет их. Правила хранятся в реестре, проверяются тоже «первое совпадение», но **явный block-фильтр приоритетнее allow**.

Ключевая особенность Windows: **три профиля — Domain, Private, Public**. Профиль выбирается автоматически: Domain применяется, только когда машина видит свой контроллер домена. Уехал ноутбук из офиса — профиль стал Public — правила, созданные для Domain, перестали работать. Это причина №1 «а вчера работало».

Ещё отличие: в Windows по умолчанию **входящие блокируются, исходящие разрешены**. В Linux типичная жёсткая схема — default deny на input и forward.

## 3. Аналогия из жизни

Межсетевой экран — это КПП на проходной завода. У вахтёра два списка: кого пускать (allow) и кого не пускать (block), причём второй список главнее. Есть отдельный журнал (`лог`), куда он записывает тех, кого развернул. Есть журнал счётчиков: «за смену развернул 47 человек» — это `counter` в правиле. Умный вахтёр не проверяет заново того, кто уже прошёл по пропуску и возвращается с обеда — он помнит, что этот человек сегодня уже проверен: это `ct state established`. И есть второй КПП между цехами (транзит, `forward`), который проверяет не тех, кто идёт «в заводоуправление», а тех, кто идёт «из цеха А в цех Б».

Аналогия ломается вот на чём: вахтёр видит человека целиком, а firewall смотрит каждый пакет отдельно и восстанавливает «человека» только по таблице conntrack — и именно поэтому сброс таблицы соединений рвёт уже работающие сессии.

## 4. Практика и команды

### 4.1. Посмотреть, что уже есть (nft)

```bash
nft list ruleset            # весь набор правил
nft list tables             # какие таблицы вообще существуют
nft list ruleset -a         # с handles — понадобится для удаления конкретного правила
```

`-a` (`--handle`) показывает `handle 12` у каждого правила: без него правило не удалить точечно.

### 4.2. Применить и проверить конфиг атомарно

```bash
nft -c -f /etc/nftables.conf     # -c = check: проверить синтаксис, ничего не применяя
nft -f /etc/nftables.conf        # применить файл целиком одной транзакцией
systemctl enable --now nftables  # грузить /etc/nftables.conf при старте
systemctl status nftables
```

`-c` — твоя страховка от «применил и потерял SSH». Файл с `flush ruleset` в начале применяется целиком: либо всё, либо ничего.

### 4.3. Добавить правило с счётчиком и логом

```bash
nft insert rule inet filter forward tcp dport 3389 counter log prefix "GW01-FWD-DROP: " level info drop
nft list ruleset | grep -A2 "GW01-FWD-DROP"
```

`insert` ставит в начало цепочки, `add` — в конец. В выводе появится:

```text
counter packets 14 bytes 840 log prefix "GW01-FWD-DROP: " drop
```

`packets` растёт → правило работает и ловит именно этот трафик.

### 4.4. Сбросить счётчики перед тестом

```bash
nft reset counters        # обнулить все счётчики
nft reset counters table inet filter
```

Позволяет отделить «старый мусор» от того, что прилетело во время твоего теста.

### 4.5. Удалить конкретное правило

```bash
nft -a list chain inet filter forward
nft delete rule inet filter forward handle 17
```

### 4.6. Логи nftables: смотреть в journal/dmesg

```bash
journalctl -k -f | grep -i "GW01-FWD-DROP"
dmesg -w | grep GW01
```

nftables пишет в лог ядра (ring buffer + journald). Если на машине стоит rsyslog — дублируется в `/var/log/kern.log`. В реальной среде логи уходят на централизованный коллектор, иначе они теряются при перезагрузке.

### 4.7. Классический iptables (ты его точно встретишь на старых серверах)

```bash
iptables -L -n -v --line-numbers        # -n: без резолва, -v: счётчики, --line-numbers: номера
iptables -I INPUT 1 -p tcp --dport 22 -s 10.10.10.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 445 -j LOG --log-prefix "IPT-445: " --log-level 4
iptables -A INPUT -p tcp --dport 445 -j DROP
iptables-save > /root/ipt-$(date +%F).rules
iptables -Z                             # обнулить счётчики
```

Тонкость: `-j LOG` — **не** терминальное действие, пакет продолжится по цепочке. Поэтому после LOG нужен отдельный DROP. `-s 10.10.10.0/24` до `-j ACCEPT` — правило вида «только из офисной сети».

### 4.8. Какой бэкенд iptables используется

```bash
iptables -V
# iptables v1.8.9 (nf_tables)
update-alternatives --config iptables
```

Если на машине смешаны `legacy` и `nf_tables`, правила «не работают» — их пишут в разные места.

### 4.9. Таблица соединений

```bash
apt install -y conntrack
conntrack -L                                   # активные записи
conntrack -S                                   # статистика: invalid, insert_failed, drop
sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
```

`insert_failed`/`drop` в `conntrack -S` растут → таблица переполнена, пакеты теряются молча.

### 4.10. Проверить, живой ли порт (с клиента)

```bash
nc -zv 10.10.20.10 80          # -z: не передавать данные, -v: подробно
curl -I http://10.10.20.10     # проверить именно приложение
ss -tulpn | grep -E ':80|:22'  # на самом сервере: кто слушает
```

```powershell
Test-NetConnection -ComputerName 10.10.20.10 -Port 80
```

```text
TcpTestSucceeded : True
```

`False` + таймаут = пакет дропнули; `False` + «connection refused» = дошли, но никто не слушает.

### 4.11. Windows Firewall: профили и состояние

```powershell
Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,LogAllowed,LogBlocked,LogFileName
netsh advfirewall show allprofiles
# GUI: Win+R → wf.msc → Monitoring → Logging Settings
```

### 4.12. Включить логирование блокировок в Windows

```powershell
Set-NetFirewallProfile -Name Domain,Private,Public `
  -LogBlocked True -LogAllowed False `
  -LogFileName "$env:windir\System32\LogFiles\Firewall\pfirewall.log" `
  -LogMaxSizeKilobytes 16384
Get-Content "$env:windir\System32\LogFiles\Firewall\pfirewall.log" -Tail 10
```

Формат строки: `дата время действие протокол src-ip dst-ip src-port dst-port размер флаги ...`. Полей больше десятка, читать удобнее через `Select-String "DROP"`.

### 4.13. Правила Windows через PowerShell

```powershell
New-NetFirewallRule -DisplayName "VEKTOR-ICMP-Echo-In" -Direction Inbound `
  -Protocol ICMPv4 -IcmpType 8 -Action Allow -Profile Domain
Get-NetFirewallRule -DisplayName "VEKTOR-*" | Get-NetFirewallPortFilter
Remove-NetFirewallRule -DisplayName "VEKTOR-ICMP-Echo-In"
```

То же самое через `netsh` (часто встречается в старых скриптах):

```cmd
netsh advfirewall firewall add rule name="VEKTOR-ICMP-Echo-In" dir=in action=allow protocol=icmpv4:8,any
```

GUI-путь: `wf.msc` → Inbound Rules → New Rule → Port / Predefined / Custom.

### 4.14. Если нужны события в журнале Windows, а не только файл

```cmd
auditpol /set /subcategory:"Filtering Platform Packet Drop" /failure:enable
```

После этого блокировки попадают в Security-журнал (события 5152/5157 WFP). Включай осторожно: на нагруженном сервере это тысячи событий в минуту.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GW01** (Debian 12, 10.10.10.3 / 10.10.20.1, два адаптера), **WEB01** (Ubuntu, nginx на 10.10.20.10) — появились на треке L2. Если WEB01 ещё нет, подними минимально: Ubuntu Server, `apt install -y nginx`, статический адрес 10.10.20.10/24, шлюз 10.10.20.1.
- На GW01 установлен nftables: `apt install -y nftables conntrack`.
- **WIN10-CL02** (petrova) и **UBU01** — с маршрутом в DMZ через GW01 (делали в уроке 30): на Windows `New-NetRoute -DestinationPrefix 10.10.20.0/24 -NextHop 10.10.10.3 -InterfaceIndex <N>`, на Linux `ip route add 10.10.20.0/24 via 10.10.10.3`.
- **FS01** в домене, профиль Domain, гость `adm.student` с правами администратора на FS01.
- Если маршрута в DMZ нет — сначала пройди урок 30, без него транзит не проверить.

### Снапшот

В VMware Workstation для каждой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-l2-035`.
Машины: GW01, WEB01, FS01, WIN10-CL02, UBU01. Имя снапшота одно и то же на всех — так проще откатывать.

### Шаги

1. **GW01.** Проверь исходное состояние.
   ```bash
   ip -br a
   nft --version
   nft list ruleset
   sysctl net.ipv4.ip_forward
   ```
   Ожидаемо: `ens33 10.10.10.3/24`, `ens34 10.10.20.1/24`; ruleset пустой (или только дефолтная пустая таблица); `ip_forward = 0`.

2. **GW01.** Включи маршрутизацию и сразу зафиксируй её в файле.
   ```bash
   echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-vektor-forward.conf
   sysctl --system
   ```
   Проверка: `sysctl net.ipv4.ip_forward` → `= 1`.

3. **WIN10-CL02 (petrova).** Baseline до правил.
   ```powershell
   Test-NetConnection 10.10.20.10 -Port 80
   Test-NetConnection 10.10.20.10 -Port 22
   ```
   Ожидаемо: **оба True** — firewall на GW01 ещё ничего не фильтрует. Запомни это состояние.

4. **GW01.** Создай `/etc/nftables.conf`:
   ```bash
   cat > /etc/nftables.conf <<'EOF'
   #!/usr/sbin/nft -f
   flush ruleset

   table inet filter {
       chain input {
           type filter hook input priority filter; policy drop;
           ct state established,related accept
           iif lo accept
           ip protocol icmp accept
           ip saddr 10.10.10.0/24 tcp dport 22 accept
           counter log prefix "GW01-IN-DROP: " level info drop
       }

       chain forward {
           type filter hook forward priority filter; policy drop;
           ct state established,related accept
           ip protocol icmp accept
           ip saddr 10.10.10.0/24 ip daddr 10.10.20.10 tcp dport { 80, 443 } accept
           counter log prefix "GW01-FWD-DROP: " level info drop
       }

       chain output {
           type filter hook output priority filter; policy accept;
       }
   }
   EOF
   ```
   Разбор: `ct state established,related accept` первым — иначе ответные пакеты умрут; `iif lo` — петлевой интерфейс; SSH разрешён **только** из офисной сети — не открывай 22 всему миру; в forward разрешён HTTP/HTTPS строго на WEB01; последнее правило — «всё остальное в лог и в drop».

5. **GW01.** Проверь и примени атомарно, включи автозапуск.
   ```bash
   nft -c -f /etc/nftables.conf
   nft -f /etc/nftables.conf
   systemctl enable --now nftables
   nft list ruleset
   systemctl is-enabled nftables
   ```
   Ожидаемо: `enabled`, в ruleset три цепочки, `policy drop` у input и forward.

6. **WIN10-CL02.** Проверь, что разрешено и что нет.
   ```powershell
   Test-NetConnection 10.10.20.10 -Port 80     # True
   Test-NetConnection 10.10.20.10 -Port 22     # False (таймаут)
   ping 10.10.20.10                            # работает (ICMP разрешён в forward)
   ```
   Разница «таймаут против refused» здесь принципиальна: таймаут = пакет дропнули на firewall.

7. **GW01.** Посмотри, как это выглядит в счётчиках и логах.
   ```bash
   nft -a list chain inet filter forward
   journalctl -k --since "-5 min" | grep GW01-FWD-DROP
   ```
   Ожидаемо: у последнего правила растут `packets`, в логе строки вида `GW01-FWD-DROP: IN=ens33 OUT=ens34 SRC=10.10.10.x DST=10.10.20.10 ... DPT=22`.

8. **FS01.** Включи логирование Windows Firewall и посмотри профиль.
   ```powershell
   Get-NetFirewallProfile | ft Name,Enabled,DefaultInboundAction,LogBlocked,LogFileName
   Set-NetFirewallProfile -Name Domain -LogBlocked True -LogMaxSizeKilobytes 16384
   ```
   Ожидаемо: `DefaultInboundAction = Block`, `LogBlocked` стало `True`.

9. **FS01.** Разреши ICMP echo из офисной сети (по умолчанию Windows его блокирует).
   ```powershell
   New-NetFirewallRule -DisplayName "VEKTOR-ICMP-Echo-In" -Direction Inbound `
     -Protocol ICMPv4 -IcmpType 8 -Action Allow -Profile Domain `
     -RemoteAddress 10.10.10.0/24
   ```
   Проверка с WIN10-CL02: `ping FS01` — пошло. До правила не шло.

10. **UBU01.** Сгенерируй блокировку и прочитай лог FS01.
    ```bash
    nc -zv 10.10.10.20 3389
    ```
    Ожидаемо: таймаут. Затем на FS01:
    ```powershell
    Get-Content "$env:windir\System32\LogFiles\Firewall\pfirewall.log" -Tail 5 |
      Select-String "DROP"
    ```
    Ожидаемо: строка с `DROP TCP 10.10.10.30 10.10.10.20 ... 3389 ... RECEIVE`.

11. **Запиши в `stand-log.md`**: на GW01 включён `ip_forward`, применён `/etc/nftables.conf`, сервис nftables в автозапуске, разрешены SSH из 10.10.10.0/24 и HTTP/HTTPS на 10.10.20.10, логирование в journald; на FS01 включён `LogBlocked`, создано правило `VEKTOR-ICMP-Echo-In`.

### Проверка

- [ ] `sysctl net.ipv4.ip_forward` на GW01 = 1 после перезагрузки (проверь `sysctl --system`, затем reboot — это лучший тест).
- [ ] `systemctl is-enabled nftables` → `enabled`.
- [ ] `nft list ruleset` на GW01 показывает три цепочки и `policy drop` у input/forward.
- [ ] С WIN10-CL02 `Test-NetConnection 10.10.20.10 -Port 80` → True, `-Port 22` → False.
- [ ] `journalctl -k | grep GW01-FWD-DROP` содержит записи с `DPT=22`.
- [ ] SSH на GW01 с WIN11-CL01 и UBU01 работает (правило input).
- [ ] На FS01 `Get-NetFirewallRule -DisplayName "VEKTOR-ICMP-Echo-In"` показывает `Enabled: True`, `Profile: Domain`.
- [ ] `pfirewall.log` содержит хотя бы одну строку `DROP`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Портал http://10.10.20.10 опять не открывается, "сайт недоступен". Вчера всё работало. Очень нужно к 15:00!»
> **Срочность:** Высокая

**Как воспроизвести поломку** (делаем намеренно, на GW01 от `root`):

```bash
systemctl disable nftables
rm -f /etc/sysctl.d/99-vektor-forward.conf
sysctl -w net.ipv4.ip_forward=0
reboot
```

Это учебная имитация реального сценария: коллега «настроил firewall», но не сохранил настройки, а ночью сервер обновил ядро и перезагрузился.

**Диагностика по шагам (именно в этом порядке):**

1. **С клиента (WIN10-CL02).** `Test-NetConnection 10.10.20.10 -Port 80` → `False`. `ping 10.10.20.1` (GW01) → отвечает. Значит сеть до шлюза жива, проблема дальше.
2. **С клиента.** `tracert 10.10.20.10` → первый хоп 10.10.10.3, дальше тишина. Маршрут на клиенте есть, пакет уходит в GW01 и не возвращается. Уже сузили до одного узла.
3. **На GW01.** `curl -I http://10.10.20.10` → `HTTP/1.1 200 OK`. То есть nginx жив и связность GW01↔WEB01 в порядке — проблема именно в транзите, не в веб-сервере.
4. **На GW01.** `sysctl net.ipv4.ip_forward` → `0`. **Вот она.** Ядро не пересылает пакеты между адаптерами.
5. **На GW01.** `nft list ruleset` → пусто. Подтверждение: правила не загрузились после старта.
6. **На GW01.** `systemctl is-enabled nftables` → `disabled`. Вторая причина. Обе надо устранить, иначе «починим» наполовину.
7. Отдельно убеждаемся, что дело не в клиенте: `Get-NetFirewallProfile` на WIN10-CL02 показывает `Enabled: True`, но исходящие разрешены, и `ping 10.10.20.1` уже доказал, что клиент не виноват.

**Решение:**

```bash
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-vektor-forward.conf
sysctl --system
nft -c -f /etc/nftables.conf
nft -f /etc/nftables.conf
systemctl enable --now nftables
sysctl net.ipv4.ip_forward && nft list ruleset | head
```

Проверка с клиента: `Test-NetConnection 10.10.20.10 -Port 80` → `True`. И обязательно **перезагрузи GW01 ещё раз** и проверь снова — иначе ты не починил, а замазал.

**Ответ пользователю:** «Мария, добрый день. Портал снова доступен, проверьте, пожалуйста, http://10.10.20.10. Причина была на маршрутизаторе между офисом и DMZ: после перезагрузки не загрузились правила фильтрации и не включилась пересылка трафика. Настройки сохранили постоянно, при следующей перезагрузке повториться не должно. Если увидите проблему снова — напишите, посмотрю логи.»

**Закрытие заявки:** категория «Сеть / Firewall», в решении — «не персистентная конфигурация nftables + ip_forward, восстановлено, добавлен шаг проверки после reboot», приложить выдержку из `journalctl -k`.

### Что изменилось на стенде

- **GW01:** `net.ipv4.ip_forward=1` через `/etc/sysctl.d/99-vektor-forward.conf`; применён `/etc/nftables.conf` (таблица `inet filter`, цепочки input/forward/output); `nftables.service` в автозапуске; разрешены SSH из 10.10.10.0/24 и TCP 80/443 на 10.10.20.10; ICMP разрешён; всё остальное — `counter log ... drop`.
- **WEB01:** без изменений (nginx как был).
- **FS01:** включён `LogBlocked` для профиля Domain, `LogMaxSizeKilobytes = 16384`; создано правило `VEKTOR-ICMP-Echo-In` (ICMPv4 type 8, профиль Domain, remote 10.10.10.0/24).
- **Клиенты:** без изменений; маршрут 10.10.20.0/24 через 10.10.10.3 сохранён с урока 30.
- Логи firewall теперь пишутся: nftables — в journald ядра GW01, Windows — в `%windir%\System32\LogFiles\Firewall\pfirewall.log` на FS01.

### Откат

VMware Workstation → выбери ВМ → **Snapshot Manager** → снапшот `before-l2-035` → **Revert**. Сделай это на GW01, WEB01, FS01, WIN10-CL02, UBU01. Если откатываешь только GW01, не забудь, что на FS01 останется правило `VEKTOR-ICMP-Echo-In` — либо откати и её, либо удали правилом `Remove-NetFirewallRule -DisplayName "VEKTOR-ICMP-Echo-In"`. После отката обнови `stand-log.md` строкой «откат к before-l2-035».

## 6. Ошибки новичков и безопасность

1. **Ставят `policy drop`, не прописав allow.** → теряют SSH к серверу, лечат через консоль VMware. → Правильно: сначала правила `established,related` + SSH, `nft -c -f`, потом применение; на боевом сервере — `at`/таймер с откатом через 10 минут.
2. **Правила в runtime, без сохранения.** → после перезагрузки firewall «пустой», как в лабораторной. → Правильно: всё в `/etc/nftables.conf` (или `iptables-save` + `netfilter-persistent`), сервис в автозапуске, проверка после reboot.
3. **Смешивают `iptables-legacy` и `nftables`.** → правила пишутся, но не применяются; `iptables -L` «пустой», хотя nft всё показывает. → Правильно: один инструмент на хост, проверка `iptables -V` и `update-alternatives`.
4. **Забывают, что транзит — это `forward`.** → открывают порт в `input` на роутере, а трафик в DMZ всё равно не идёт. → Правильно: смотреть, через какой хук идёт пакет.
5. **Windows: правило для профиля Domain, машина ушла в Public.** → «вчера работало, сегодня нет», особенно у ноутбуков и серверов, потерявших связь с DC01. → Правильно: проверять `Get-NetFirewallProfile` первым делом, дублировать критичные правила на несколько профилей.
6. **Логирование без ограничения.** → `log` на каждое правило при скане или брутфорсе забивает `kern.log`/journal и ускоряет износ диска; в Windows — гигантский `pfirewall.log`. → Правильно: `limit rate`, разумный `LogMaxSizeKilobytes`, централизованный сбор логов.
7. **«Отключу firewall, проверю» — и забывают включить.** → машина остаётся без фильтрации неделями. → Правильно: проверять правила и счётчики, а не отключать; если отключил — записать в тикет и вернуть немедленно.
8. **Путают «порт слушается» и «порт доступен».** → `ss` на сервере показывает `LISTEN`, а с клиента таймаут: виноват промежуточный FW (GW01). → Правильно: проверять всю цепочку — сервер, промежуточные узлы, клиент.

### Безопасность

В боевой среде нельзя: править firewall на удалённом сервере без плана отката и без альтернативного доступа (IPMI/консоль); разрешать SSH и RDP «из любой сети» — только из админского сегмента; держать SMB (445) открытым между сегментами сети — это классический вектор червячных атак; хранить пароли и ключи в комментариях к правилам. Логи блокировок — это ещё и персональные данные о поведении сотрудников: доступ к ним ограничивают, срок хранения оговаривают.

В лабораторной мы разрешили SSH всей подсети 10.10.10.0/24 и используем единый учебный пароль `Lab#2026-vektor` — это допустимо только на изолированном стенде. В реальной среде SSH — по ключам, по конкретным адресам, с ограничением попыток.

## 7. Вопросы на собеседовании

**1. Чем nftables отличается от iptables и что ты выберешь на новом сервере?**
Ответ: nftables — современная замена, часть ядра начиная с 3.13; один инструмент вместо `iptables/ip6tables/arptables/ebtables`; таблицы и цепочки с произвольными именами и единая для IPv4/IPv6 таблица `inet`; сеты и map вместо сотен однотипных правил; атомарное применение всего файла через `nft -f`; счётчики и логирование прямо в правиле; лучшая производительность на больших наборах за счёт интерпретации байт-кода вместо линейного перебора. `iptables` на современных дистрибутивах — обёртка `iptables-nft`, старый синтаксис транслируется в nftables. На новом сервере выберу nftables; iptables оставлю только для совместимости со старыми скриптами и legacy-систем.
*Что хочет услышать интервьюер:* что ты понимаешь разницу архитектур, а не только «новее = лучше», и знаешь про `iptables-nft`.
*Красный флаг:* «iptables устарел, им никто не пользуется» — на деле половина парка ещё на нём.

**2. Что такое conntrack и почему `ct state established,related accept` ставят первым правилом?**
Ответ: conntrack — подсистема ядра, которая отслеживает состояние соединений. Первый пакет помечается `NEW`, ответные — `ESTABLISHED`, связанные (например, FTP data, ICMP-ошибки) — `RELATED`, невалидные — `INVALID`. Правило accept для established/related пропускает обратный трафик, не открывая отдельные правила на каждый высокий порт. Если его не поставить, обратные пакеты упрутся в `policy drop` и соединение не установится, хотя входящее правило есть. У таблицы есть лимит `nf_conntrack_max`; при переполнении в логах `nf_conntrack: table full, dropping packet`, а в `conntrack -S` растут `insert_failed`. Лечится увеличением лимита, сокращением времени жизни (`nf_conntrack_tcp_timeout_established`) и разбором, кто плодит соединения.
*Что хочет услышать интервьюер:* понимание stateful-фильтрации и того, что firewall помнит сессии.
*Красный флаг:* «established — это когда соединение уже установлено» и всё; отсутствие упоминания лимита таблицы.

**3. Пользователь не открывает сайт: сервис слушает, ping до сервера идёт, а TCP-соединение не устанавливается. Как диагностируешь?**
Ответ: сначала уточню, что именно не работает и с какого места. На сервере — `ss -tulpn | grep :80`, убедиться, что слушает нужный адрес и порт, и посмотреть `curl -I localhost`. С клиента — `Test-NetConnection -Port 80` / `nc -zv`, различить таймаут и refused. Таймаут → идём по пути: `tracert`, на промежуточных узлах `nft list ruleset -a` со счётчиками, `nft reset counters` перед тестом, `journalctl -k | grep prefix`, при Windows-узлах — `pfirewall.log`. Refused → дошли до хоста, проблема в приложении или в привязке к `127.0.0.1`. Дальше `conntrack -L` на шлюзе, проверка маршрутов и NAT (`masquerade`) на GW01. Если есть сомнения по конкретному пакету — `tcpdump -ni any host 10.10.20.10 and port 80` на клиенте и на шлюзе, видно, где пакет исчезает.
*Что хочет услышать интервьюер:* системный порядок, работа со счётчиками и логами firewall, различие таймаута и refused.
*Красный флаг:* «перезагружу сервер» или «отключу firewall и посмотрю».

**4. Как организовать логирование блокировок в Linux и Windows и что делать с объёмом логов?**
Ответ: Linux — `counter log prefix "GW01-FWD-DROP: " level info drop` в правиле nftables; пишется в лог ядра, читается через `journalctl -k`/`dmesg`; iptables — `-j LOG --log-prefix`. Обязательно ограничение частоты (`limit rate`) и осмысленный префикс, чтобы грепать одно правило. Windows — `Set-NetFirewallProfile -LogBlocked True -LogFileName ... -LogMaxSizeKilobytes ...`, файл `pfirewall.log`, поля с датой, src/dst, портами и действием; для событий в Security-журнале включается аудит `Filtering Platform Packet Drop`. Объём: включать логи только на проблемных правилах, ограничивать rate и размер файла, ротацию, отправку на централизованный коллектор (rsyslog/syslog-ng/WEF) и срок хранения.
*Что хочет услышать интервьюер:* что ты понимаешь цену логирования и умеешь читать собственные логи.
*Красный флаг:* «включу лог на все правила, потом разберусь».

**5. Как сделать правила firewall переживающими перезагрузку и как безопасно вносить изменения на удалённом сервере?**
Ответ: Linux — конфиг в `/etc/nftables.conf`, `systemctl enable nftables`, проверка `nft -c -f` до применения, `iptables` — `iptables-save` + `netfilter-persistent`. Windows — правила хранятся в реестре и применяются службой BFE/MpsSvc автоматически, отдельного «сохранения» нет, но нужно следить за профилями. Безопасные изменения на удалённом хосте: сначала `nft -c -f`; применять вместе с таймером отката (`at` или `systemd-run --on-active=600 ...`), иметь консольный доступ (VMware console, IPMI, KVM), снапшот ВМ перед изменениями, окно работ и предупреждение пользователям.
*Что хочет услышать интервьюер:* зрелость и страх перед lockout — это плюс, а не минус.
*Красный флаг:* «сохраню, потом проверю» — типичный способ остаться без сервера.

## 8. Источники (официальная документация)

- **netfilter.org** — официальный сайт проекта netfilter; раздел nftables / «nftables wiki», подстраницы Quick reference, Simple rule management, Configuring chains. Поиск: `nftables wiki quick reference`, `nftables flowtable` [ПРОВЕРИТЬ актуальность структуры вики].
- **man7.org** — man-страницы `nft(8)`, `iptables(8)`, `iptables-extensions(8)` (там описаны цели `LOG`, `REJECT` и модуль `conntrack`). Поиск: `man nft 8`, `iptables-extensions LOG target`.
- **learn.microsoft.com** — раздел «Windows Defender Firewall with Advanced Security»; статьи по модулю PowerShell `NetSecurity` (`New-NetFirewallRule`, `Set-NetFirewallProfile`, `Get-NetFirewallRule`) и справочник `netsh advfirewall`. Поиск: `New-NetFirewallRule PowerShell reference`, `netsh advfirewall commands windows server`.
- **learn.microsoft.com** — «Audit Filtering Platform Packet Drop» / события WFP 5152 и 5157. Поиск: `audit filtering platform packet drop event 5152`.
- **kernel.org** — документация ядра по netfilter sysctl: `nf_conntrack_max`, таймауты. Поиск: `kernel documentation nf_conntrack-sysctl`.
- **debian.org** (Debian Wiki) — страница по nftables и управлению firewall на Debian 12. Поиск: `debian wiki nftables`.
- **ubuntu.com** — документация по `ufw` (удобная обёртка над netfilter; пригодится, когда правила простые). Поиск: `ubuntu ufw documentation`.
- **docs.docker.com / kubernetes.io** — если контейнеры на хосте, помни: Docker и k8s сами активно пишут правила в iptables/nftables, ручной `flush ruleset` ломает сеть контейнеров. Поиск: `docker iptables reference`, `k8s kube-proxy iptables mode` [ПРОВЕРИТЬ поведение nftables-бэкенда kube-proxy в актуальной версии].
