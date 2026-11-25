# Wi-Fi на уровне проекта: радиообследование, контроллеры, 802.1X, гостевой портал

## 1. Введение (зачем это нужно в реальной работе)

```
Заявка #4127
Пользователь: petrova (Мария Петрова, HR), ВМ WIN10-CL02
Что написал: «В переговорной на втором этаже ноутбук постоянно теряет
корпоративную сеть, приходится переподключаться по пять минут.
Гости спрашивают пароль от Wi-Fi, мы даём один общий — он уже есть
у половины города. И ещё вопрос: гость из переговорной случайно
открыл \\FS01\Buh (папка ему не нужна, он её просто увидел).
Срочность: средняя, в четверг совещание с клиентом.»
```

Wi-Fi — это единственная часть сети, которая живёт в физическом мире: радиоволны не подчиняются конфигу и умеют мешать друг другу. Поэтому в работе сисадмина заявки «плохо ловит» идут пачками, а стоимость ошибки — не перезагрузка сервиса, а закупка на 300 тысяч точек доступа, которые поставили не там. На этом уровне от тебя ждут умения спланировать покрытие по цифрам, а не «на глаз», объяснить, зачем нужен контроллер и когда без него жить можно, перевести корпоративную сеть с одного Wi-Fi-пароля на 802.1X с проверкой в AD, и сделать гостевой доступ, который не пускает гостя во внутреннюю сеть. После главы ты должен уметь прочитать радиообследование, собрать WPA2-Enterprise с RADIUS, поднять гостевой портал и объяснить заказчику, почему «просто добавим ещё одну точку» — обычно не решение.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Радио — это общий полудуплексный провод.** Точка доступа (AP, access point) и клиенты делят один канал по CSMA/CA: прежде чем передавать, устройство слушает эфир и ждёт паузу. Значит, ёмкость канала делится на всех — и один клиент с плохим сигналом, который переспрашивает кадры по десять раз, съедает эфирное время у всех остальных.

Ключевые цифры, на которых строится проект:

| Параметр | Значение для проектирования |
|---|---|
| Уровень сигнала на границе соты | −67 dBm (не «две палки» на ноутбуке, а RSSI в dBm) |
| Отношение сигнал/шум (SNR) | ≥ 25 dB |
| Ширина канала 2.4 ГГц | только 20 MHz, непересекающиеся каналы 1/6/11 |
| Ширина канала 5 ГГц | 20/40/80 MHz, но 80 MHz — только в редких сценариях |
| Занятость эфира (channel utilization) | выше 50–60 % — пора добавлять точку или уводить клиентов на 5 ГГц |

Диапазоны: 2.4 ГГц (13 каналов в РФ, из них реально непересекающихся три), 5 ГГц (много каналов, но часть под DFS — точка обязана уступить канал при обнаружении радара), 6 ГГц (Wi-Fi 6E/7) — в России для массового использования пока не открыт `[ПРОВЕРИТЬ]`, поэтому в проектах 2026 года на него не рассчитываем.

**Архитектуры.** Автономная точка (каждая настраивается отдельно) годится для дома и маленького офиса. Дальше — контроллер:

```text
   ВАРИАНТ A: локальный контроллер (WLC)          ВАРИАНТ B: без контроллера
   ┌──────┐   CAPWAP/UDP 5246(control)  ┌────────┐
   │ AP1  │◄───────────────────────────►│        │      ┌──────┐
   ├──────┤   5247(data, если tunnel)   │  WLC   │      │ AP1  │ конфиг локально
   │ AP2  │◄───────────────────────────►│        │      ├──────┤ + VLAN + RADIUS
   └──────┘                             └───┬────┘      │ AP2  │
                                            │            └──────┘
                                       RADIUS 1812/1813
                                            │
                                       ┌────▼────┐
                                       │ DC01/NPS│──► AD: kerberos/ntlm
                                       └─────────┘
```

Локальный контроллер (Cisco Catalyst 9800, Eltex WLC, Huawei AC, MikroTik CAPsMAN, UniFi Controller) даёт единый SSID-профиль, централизованный роуминг, динамическое назначение VLAN и один радиус-клиент вместо сотни. Облачный контроллер удобен, но в РФ это ещё и вопрос доступности сервиса и требований к персональным данным — часто проще локальный. Важно: контроллер не обязан гнать трафик через себя (local switching), иначе он становится узким местом.

**Радиообследование** бывает трёх видов: предсказательное (по плану здания и модели стен, до закупки), натурное (ходим с ноутбуком/телефоном и меряем — то, что делаем мы), и валидационное (после монтажа: тест роуминга, throughput через iperf3, проверка, что клиент не «залипает» на дальней точке).

**802.1X** — это проверка пропуска на входе, а не один пароль на подъезд. Клиент ассоциируется с SSID, дальше идёт обмен EAPOL-кадрами (EtherType 0x888E) внутри 802.11: точка запрашивает identity, клиент отвечает, AP перекладывает это в RADIUS `Access-Request` (UDP 1812, accounting 1813) и получает `Access-Accept`. Только после этого выполняется 4-way handshake и выдаются ключи шифрования. Популярные методы: PEAP-MSCHAPv2 (логин-пароль из AD, самый частый в офисах) и EAP-TLS (клиентский сертификат, строже и без паролей). RADIUS умеет вернуть атрибуты `Tunnel-Type=VLAN`, `Tunnel-Medium-Type=IEEE-802`, `Tunnel-Private-Group-Id` — точка посадит пользователя в нужный VLAN. WPA3-Enterprise (192-bit) добавляет PMF (802.11w) и защиту управляющих кадров от деаутентификационных атак.

**Гостевой портал** — отдельный SSID в отдельном VLAN, где до авторизации разрешён только «walled garden»: DNS и адреса портала. Телефоны и Windows сами проверяют наличие интернета запросами на свои адреса ( captive.apple.com, connectivitycheck.gstatic.com, msftconnecttest.com ) и при подмене ответа показывают окно входа. После авторизации контроллер снимает ограничение (обычно через RADIUS CoA — Change of Authorization) и ставит лимит по времени/скорости.

## 3. Аналогия из жизни

Представь конференц-зал, где несколько ораторов говорят одновременно в одном помещении. Точка доступа — это громкоговоритель: если поставить его на максимальную мощность, его слышно даже в соседнем зале, и люди оттуда приходят к нему, хотя рядом стоит свой громкоговоритель — получается «липкий клиент» на дальней точке с ужасным качеством. Два оратора на одной частоте (co-channel interference) перекрикивают друг друга, и зал не понимает ни одного. Контроллер — это распорядитель, который заранее развёл ораторов по разным углам и следит, чтобы никто не переходил из зала в зал с пустым микрофоном. 802.1X — вахтёр на входе: проверяет пропуск у каждого лично, а не верит, что «раз ты знаешь кодовое слово, значит свой». Гостевой портал — стойка регистрации, где гостю выдают бейдж «только в холл и буфет», а не ключ от сейфовой комнаты. Аналогия ломается в одном: в зале оратор один говорит — остальные слушают, а в Wi-Fi клиент сам решает, к какому громкоговорителю идти, и точку доступа никто не спрашивает.

## 4. Практика и команды

**1. Текущее подключение (Windows).** Что за SSID, какой BSSID, шифр, канал.

```cmd
netsh wlan show interfaces
```

```text
Name                   : Wi-Fi
State                  : connected
SSID                   : VEKTOR-CORP
BSSID                  : 00:11:22:33:44:55
Authentication         : WPA2-Enterprise
Cipher                 : CCMP
Signal                 : 78%
Radio type             : 802.11ax
Channel                : 36
```

Смотрим `Authentication` (должно быть WPA2/WPA3-Enterprise, а не WPA2-Personal), `Signal` в процентах (грубо: 78 % ≈ −62 dBm) и `Channel` (36 = 5 ГГц, хорошо).

**2. Скан эфира с деталями по каждой точке (Windows).**

```cmd
netsh wlan show networks mode=bssid
```

Вывод содержит по каждому BSSID: SSID, тип аутентификации, шифр, `Signal`, `Channel`. Что искать: три-четыре SSID на одном канале в 2.4 ГГц, открытые сети, «левые» SSID с похожими именами (потенциальный rogue AP).

**3. Полный отчёт о беспроводной истории (Windows).** Отдаётся в тикет и в проектный отчёт.

```cmd
netsh wlan show wlanreport
```

Генерирует HTML в `C:\ProgramData\Microsoft\Windows\WlanReport\wlan-report-latest.html`: там причины обрывов, время ассоциаций, ошибки авторизации.

**4. Почему один общий пароль — плохо (Windows).**

```cmd
netsh wlan show profile name="VEKTOR-CORP" key=clear
```

В поле `Key Content` любой локальный админ видит пароль в открытом виде. Это ровно то, что сотрудники разнесут по чатам за неделю.

**5. Список интерфейсов и их режим (Linux).**

```bash
iw dev
```

```text
phy#0
	Interface wlan0
		ifindex 3
		type managed
```

`type managed` — клиент, `type AP` — точка, `type monitor` — захват кадров.

**6. Радиообследование по каналам (Linux) — главный инструмент натурного сюрвея.**

```bash
sudo iw dev wlan0 survey dump
```

```text
Survey data from wlan0
	frequency:			2437 MHz [in use]
	noise:				-95 dBm
	channel active time:		1820 ms
	channel busy time:		240 ms
	channel receive time:		180 ms
	channel transmit time:		60 ms
```

`channel busy time / active time` = занятость эфира. Больше ~50 % — канал перегружен, пора переезжать или добавлять точку. `noise` у hwsim и реальных карт показывает шумовую полку.

**7. Скан соседних сетей (Linux).**

```bash
sudo iw dev wlan0 scan | grep -E "SSID|signal|DS Parameter set|RSN|WPA"
```

`signal` в dBm, `DS Parameter set: channel 6` — канал. Быстрый способ построить карту занятости каналов без коммерческого софта.

**8. Что умеет адаптер и какой регуляторный домен включён.**

```bash
iw phy phy0 info | head -40
iw reg get
```

`iw reg get` покажет `country RU` и разрешённые диапазоны. Если стоит `country 00`, карта может не давать часть каналов 5 ГГц.

**9. Подключение из консоли с отладкой EAP (Linux).** Бывает нужен, когда Windows молча пишет «не удалось подключиться».

```bash
sudo wpa_supplicant -i wlan1 -c /etc/wpa_supplicant/corp.conf -Dnl80211 -d
```

Ключи: `-i` интерфейс, `-c` конфиг, `-Dnl80211` драйвер, `-d` debug (ещё есть `-dd` — совсем подробно). В логе видно `EAP: EAP-Request/Identity`, `EAP-PEAP: Tunneled EAP-Success` или причину отказа.

**10. Проверка RADIUS без точки доступа.**

```bash
radtest ivanov 'Lab#2026-vektor' 127.0.0.1 0 lab-radius-secret
```

```text
Received Access-Accept Id 14 from 127.0.0.1:1812 to 127.0.0.1:41234 length 46
	Tunnel-Type:0 = VLAN
	Tunnel-Private-Group-Id:0 = "30"
```

Синтаксис: пользователь, пароль, сервер, NAS-port, shared secret. Видим `Access-Accept` и атрибуты динамического VLAN.

**11. Отладка самого RADIUS.**

```bash
sudo freeradius -X
```

`-X` — foreground + максимальный debug. В консоли видно цепочку `(0) Received Access-Request`, `(0) eap: ...`, `(0) Sent Access-Accept`. Это первый инструмент, когда 802.1X «не пускает».

**12. Захват рукопожатия.** `hwsim0` — monitor-интерфейс виртуального радио.

```bash
sudo tcpdump -i hwsim0 -w /tmp/eapol.pcap
sudo tcpdump -i lo -nn udp port 1812 -w /tmp/radius.pcap
```

В Wireshark фильтр `eapol` покажет 4-way handshake, `eap` — обмен методами, `radius` — запросы к серверу.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 собран: DC01 (10.10.10.10, AD DS/DNS/DHCP), FS01 (10.10.10.20), UBU01 (10.10.10.30, Ubuntu Server 24.04), WEB01 (10.10.20.10, nginx в DMZ), GW01, MON01 (10.10.10.40). Работаем на UBU01.

**Важно:** VMware Workstation не эмулирует Wi-Fi-радио. Поэтому вместо USB-свистка используем штатный модуль ядра Linux `mac80211_hwsim` — он создаёт виртуальные радиоинтерфейсы, между которыми реально ходят 802.11-кадры (включая EAPOL и 4-way handshake). Всё остальное — настоящий hostapd, настоящий FreeRADIUS, настоящий nginx.

Адреса `10.10.10.250` и `10.10.10.251` — временные, только внутри виртуального радиолинка, ни за одной машиной стенда они не закреплены и вне лабы не используются.

### Снапшот

На UBU01 (и на DC01, если будешь делать опциональный шаг 12):
VMware Workstation → правый клик по ВМ → Snapshot → Take Snapshot → имя `before-sysadmin-049`.
Или из консоли хоста ВМ: `vmrun snapshot "<путь к .vmx>" before-sysadmin-049`.

### Шаги

1. **UBU01. Ставим пакеты.**
   ```bash
   sudo apt update
   sudo apt install -y hostapd wpasupplicant freeradius freeradius-utils iw tcpdump nginx-light curl
   ```
   Ожидаемо: `hostapd` и `freeradius` установлены. Сервисы `hostapd` и `freeradius` после установки могут сразу полезть в автозапуск — гасим: `sudo systemctl disable --now hostapd freeradius`.

2. **UBU01. Поднимаем виртуальное радио.**
   ```bash
   sudo modprobe mac80211_hwsim radios=2
   iw dev
   ```
   Ожидаемо: `wlan0`, `wlan1` и monitor-интерфейс `hwsim0`. Если модуля нет — поставь `linux-modules-extra-$(uname -r)` `[ПРОВЕРИТЬ для твоего ядра]`.

3. **UBU01. Базовое «радиообследование» до настройки.**
   ```bash
   sudo ip link set wlan0 up
   sudo iw dev wlan0 survey dump
   sudo iw phy phy0 info | grep -A3 "Band 1"
   ```
   Ожидаемо: пустой эфир, `channel busy time` близка к нулю, шумовая полка около −95 dBm. Зафиксируй эти числа — это твоя точка отсчёта.

4. **UBU01. Готовим два сегмента: корпоративный и гостевой.**
   ```bash
   sudo ip link add br-corp type bridge
   sudo ip link add br-guest type bridge
   sudo ip link set br-corp up
   sudo ip link set br-guest up
   sudo ip addr add 10.10.10.250/32 dev br-corp
   sudo ip addr add 10.10.10.251/32 dev br-guest
   sudo ip route add 10.10.10.201/32 dev br-corp
   sudo ip route add 10.10.10.202/32 dev br-guest
   ```
   Маски `/32` и host-маршруты — чтобы два бриджа в одной подсети не конфликтовали в таблице маршрутизации.

5. **UBU01. Конфиг hostapd с двумя SSID.**
   ```bash
   sudo tee /etc/hostapd/hostapd.conf >/dev/null <<'EOF'
   interface=wlan0
   driver=nl80211
   hw_mode=g
   channel=6
   country_code=RU
   ieee80211n=1
   ssid=VEKTOR-CORP
   bridge=br-corp
   auth_algs=1
   wpa=2
   wpa_key_mgmt=WPA-EAP
   rsn_pairwise=CCMP
   ieee80211w=1
   auth_server_addr=127.0.0.1
   auth_server_port=1812
   auth_server_shared_secret=lab-radius-secret
   acct_server_addr=127.0.0.1
   acct_server_port=1813
   acct_server_shared_secret=lab-radius-secret

   bss=wlan0_1
   ssid=VEKTOR-GUEST
   bridge=br-guest
   auth_algs=1
   wpa=0
   ap_isolate=1
   EOF
   sudo hostapd -dd /etc/hostapd/hostapd.conf
   ```
   Ожидаемо: `wlan0: interface state UNINITIALIZED->ENABLED`, `Setup of interface done`. Второй BSS (`wlan0_1`) — гостевая сеть без пароля, но с изоляцией клиентов.

6. **UBU01. Смотрим, что точка «видна» в эфире.**
   ```bash
   sudo ip netns add sta
   sudo ip link set wlan1 netns sta
   sudo ip netns exec sta ip link set wlan1 up
   sudo ip netns exec sta iw dev wlan1 scan | grep -E "SSID|RSN|signal"
   ```
   Ожидаемо: два SSID — `VEKTOR-CORP` (с RSN/WPA2) и `VEKTOR-GUEST` (без RSN). Это и есть натурное обследование в миниатюре.

7. **UBU01. FreeRADIUS: клиент и пользователь.**
   ```bash
   sudo tee -a /etc/freeradius/3.0/clients.conf >/dev/null <<'EOF'
   client ubu01 {
       ipaddr = 127.0.0.1
       secret = lab-radius-secret
       shortname = ubu01
   }
   EOF
   sudo tee -a /etc/freeradius/3.0/users >/dev/null <<'EOF'
   ivanov  Cleartext-Password := "Lab#2026-vektor"
       Tunnel-Type = VLAN,
       Tunnel-Medium-Type = IEEE-802,
       Tunnel-Private-Group-Id = "30"
   EOF
   sudo freeradius -X
   ```
   Путь `/etc/freeradius/3.0/` — для Debian/Ubuntu `[ПРОВЕРИТЬ в своей версии пакета]`. Ожидаемо: `Ready to process requests`.

8. **UBU01, второй терминал. Проверяем RADIUS напрямую.**
   ```bash
   radtest ivanov 'Lab#2026-vektor' 127.0.0.1 0 lab-radius-secret
   ```
   Ожидаемо: `Received Access-Accept` и три атрибута Tunnel-*. В логе `freeradius -X` — `(0) Sent Access-Accept`.

9. **UBU01. Настоящее подключение клиента по 802.1X.**
   ```bash
   sudo tee /etc/wpa_supplicant/corp.conf >/dev/null <<'EOF'
   network={
       ssid="VEKTOR-CORP"
       key_mgmt=WPA-EAP
       eap=PEAP
       identity="ivanov"
       password="Lab#2026-vektor"
       phase2="auth=MSCHAPV2"
   }
   EOF
   sudo ip netns exec sta wpa_supplicant -i wlan1 -c /etc/wpa_supplicant/corp.conf -B
   sudo ip netns exec sta wpa_cli -i wlan1 status
   ```
   Ожидаемо: `wpa_state=COMPLETED`, `key_mgmt=WPA2/IEEE 802.1X`, `pairwise_cipher=CCMP`. В логе FreeRADIUS — Access-Accept.

10. **UBU01. Даём клиенту адрес и проверяем доступ.**
    ```bash
    sudo ip netns exec sta ip addr add 10.10.10.201/24 dev wlan1
    sudo ip netns exec sta ping -c 3 10.10.10.250
    sudo ip netns exec sta iw dev wlan1 link
    ```
    Ожидаемо: пинг проходит, `iw link` показывает `signal: -30 dBm` и битрейт. В реальной сети адрес выдал бы DHCP, а VLAN — тот самый `Tunnel-Private-Group-Id = 30`.

11. **UBU01. Гостевой портал.**
    ```bash
    sudo mkdir -p /var/www/guest-portal
    sudo tee /var/www/guest-portal/index.html >/dev/null <<'EOF'
    <h1>VEKTOR-GUEST</h1><p>Введите номер из SMS от администратора.</p>
    EOF
    sudo tee /etc/nginx/sites-enabled/portal >/dev/null <<'EOF'
    server { listen 8080 default_server; root /var/www/guest-portal; index index.html; }
    EOF
    sudo nginx -t && sudo systemctl reload nginx
    sudo nft add table ip captive
    sudo nft 'add chain ip captive prerouting { type nat hook prerouting priority dstnat ; }'
    sudo nft add rule ip captive prerouting iifname "br-guest" tcp dport 80 redirect to :8080
    sudo nft add table ip guestfence
    sudo nft 'add chain ip guestfence forward { type filter hook forward priority filter ; }'
    sudo nft add rule ip guestfence forward iifname "br-guest" oifname "br-corp" drop
    ```
    Ожидаемо: `nginx: configuration file ... test is successful`.

12. **UBU01. Переключаемся на гостевую сеть и смотрим, что видит гость.**
    ```bash
    sudo ip netns exec sta wpa_cli -i wlan1 terminate
    sudo ip netns exec sta wpa_supplicant -i wlan1 -c <(printf 'network={\n ssid="VEKTOR-GUEST"\n key_mgmt=NONE\n}\n') -B
    sudo ip netns exec sta ip addr flush dev wlan1
    sudo ip netns exec sta ip addr add 10.10.10.202/24 dev wlan1
    sudo ip netns exec sta curl -v http://10.10.10.20/
    ```
    Ожидаемо: вместо FS01 приходит HTML портала (редирект nftables перехватил 80-й порт). Пинг до `10.10.10.250` (корпоративный сегмент) не проходит — это и есть изоляция.

13. **UBU01. Захват и разбор.**
    ```bash
    sudo timeout 30 tcpdump -i hwsim0 -w /tmp/eapol.pcap
    # в другом терминале повтори шаг 9
    ```
    Открой pcap в Wireshark: фильтр `eapol` → 4-way handshake, фильтр `eap` → EAP-Request/Identity и PEAP-туннель. Сделай скриншот в портфолио.

14. **Записать в `stand-log.md`.** См. раздел ниже.

### Проверка

- [ ] `iw dev` на UBU01 показывает `wlan0` (AP), `wlan1` (в netns), `hwsim0`.
- [ ] `iw dev wlan1 scan` из netns `sta` видит оба SSID.
- [ ] `radtest` возвращает `Access-Accept` с атрибутами `Tunnel-*`.
- [ ] `wpa_cli status` в корпоративной сети: `key_mgmt=WPA2/IEEE 802.1X`.
- [ ] `ping 10.10.10.250` из корпоративного подключения проходит.
- [ ] В гостевой сети `curl http://10.10.10.20/` отдаёт страницу портала, а не FS01.
- [ ] `ping 10.10.10.250` из гостевой сети не проходит.
- [ ] В `/tmp/eapol.pcap` в Wireshark видны кадры EAPOL.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

```
Пользователь: ivanov (Иван Иванов, бухгалтерия), ВМ WIN11-CL01
Что написал: «Ноутбук в переговорной не подключается к VEKTOR-CORP,
пишет "не удалось подключиться к этой сети". Дома всё работает,
телефон к той же сети цепляется. Совещание через 20 минут!»
Срочность: высокая
```

**Как воспроизвести поломку.** На UBU01 в конфиге FreeRADIUS `clients.conf` поменяй `secret` на другой, не меняя его в hostapd. Перезапусти `freeradius -X` и попробуй подключиться (шаг 9).

**Диагностика по порядку:**
1. Видит ли клиент SSID вообще: `iw dev wlan1 scan | grep VEKTOR`. Нет — проблема в радио/канале, не в авторизации.
2. Проходит ли ассоциация: в логе `hostapd -dd` ищем `AP-STA-CONNECTED`. Если ассоциация есть, а ключей нет — идём дальше.
3. `freeradius -X` — есть ли вообще `Received Access-Request`. Нет запроса → hostapd не смог достучаться до RADIUS (адрес, порт, secret).
4. Если есть `Access-Request`, но `Received Access-Reject` со `message: Invalid shared secret` — рассинхрон секретов. Это наш случай.
5. Если `Access-Reject` с `eap: Failed to authenticate` — проблема в учётке/пароле/методе (например, PEAP-MSCHAPv2 против пользователя с истёкшим паролем).

**Решение.** Привести `auth_server_shared_secret` в `hostapd.conf` и `secret` в `clients.conf` к одному значению, перезапустить оба сервиса, повторить подключение.

**Ответ пользователю:** «Проблема на стороне точки доступа — рассинхронизировался ключ между точкой и сервером авторизации, к вашей учётке это не относится. Починили, проверьте, пожалуйста, через 2 минуты; если снова не пустит — пришлите скриншот ошибки».

**Закрытие заявки:** категория «Wi-Fi / 802.1X», решение в двух строках, ссылка на запись в `stand-log.md`. Профилактика: включить в Zabbix на MON01 проверку `radtest` раз в 5 минут по тестовой учётке.

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлены `hostapd`, `wpasupplicant`, `freeradius`, `freeradius-utils`, `iw`, `tcpdump`, `nginx-light`, `curl`.
- **UBU01:** загружен модуль `mac80211_hwsim` (2 радио), созданы интерфейсы `wlan0`, `wlan1`, `hwsim0`.
- **UBU01:** созданы бриджи `br-corp` (10.10.10.250/32) и `br-guest` (10.10.10.251/32), host-маршруты на 10.10.10.201/202.
- **UBU01:** `/etc/hostapd/hostapd.conf` — два SSID: `VEKTOR-CORP` (WPA2-Enterprise, RADIUS 127.0.0.1:1812) и `VEKTOR-GUEST` (open, ap_isolate).
- **UBU01:** FreeRADIUS с клиентом `ubu01` (secret `lab-radius-secret`), пользователь `ivanov` → VLAN 30 через Tunnel-атрибуты.
- **UBU01:** netns `sta` с `wlan1`; nginx-портал на :8080; nftables-таблицы `captive` и `guestfence`.
- **Не менялось:** DC01, FS01, WEB01, GW01, MON01, WIN11-CL01, WIN10-CL02, ASTRA01.
- **Тема следующего шага (задел):** перенос портала и RADIUS-проверки в DMZ на WEB01, интеграция NPS на DC01 с AD-группами `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT`.

### Откат

1. Остановить сервисы: `sudo systemctl stop nginx hostapd freeradius`.
2. Убрать артефакты: `sudo ip netns del sta; sudo ip link del br-corp; sudo ip link del br-guest; sudo nft delete table ip captive; sudo nft delete table ip guestfence`.
3. Выгрузить модуль: `sudo modprobe -r mac80211_hwsim`.
4. Если что-то пошло не так на уровне ОС — VMware Workstation → Snapshot Manager → выбрать `before-sysadmin-049` → **Revert To**. Стенд вернётся к состоянию до лабы; конфиги `/etc/hostapd` и `/etc/freeradius` откатятся вместе с диском.

## 6. Ошибки новичков и безопасность

1. **Один общий пароль на весь офис.**
   → через месяц его знают все, включая уволенных; сменить нельзя без обхода всех устройств.
   → правильно: WPA2/WPA3-Enterprise с 802.1X и проверкой в AD, а для «умных» устройств без 802.1X — отдельный SSID с уникальным PSK (WPA3-SAE / DPSK).

2. **Выкрутить мощность точки на максимум «чтобы лучше ловило».**
   → клиенты с краёв цепляются к дальней точке, «липкий клиент», соты не перекрываются нормально, роуминг не срабатывает.
   → правильно: мощность подбирается по сюрвею под целевую границу соты −67 dBm, в 2.4 ГГц — ниже, чем в 5 ГГц; включить band steering и минимальный RSSI.

3. **40 МГц на 2.4 ГГц.**
   → в диапазоне 2.4 ГГц всего три непересекающихся канала, широкий канал перекрывает соседей и превращает эфир в кашу.
   → правильно: 2.4 ГГц всегда 20 MHz, широкие каналы — только 5 ГГц и только после сюрвея.

4. **Гостевая сеть в том же VLAN и с тем же DHCP-пулом, что и корпоративная.**
   → гость видит `\\FS01\Buh`… ровно то, что случилось в заявке #4127.
   → правильно: отдельный SSID → отдельный VLAN → отдельная подсеть → ACL до портала и интернета, client isolation (`ap_isolate=1`).

5. **Captive portal без понимания captive portal detection.**
   → телефоны не показывают окно входа, HTTPS-запросы рвутся «ошибкой сертификата», пользователи думают, что Wi-Fi не работает.
   → правильно: разрешить в walled garden адреса проверки (`captive.apple.com`, `connectivitycheck.gstatic.com`, `msftconnecttest.com`), перехватывать только HTTP/80 и DNS, 443 не подменять, ставить понятную страницу с таймаутом сессии.

6. **RADIUS на тестовом shared secret и с клиентом «любой IP».**
   → украденный секрет = возможность слать произвольные `Access-Accept`.
   → правильно: отдельный секрет на каждую точку/контроллер, привязка клиента по IP, ротация секретов, RADIUS в отдельном VLAN управления.

7. **Сертификаты на 10 лет и ручной импорт на 300 ноутбуков.**
   → при компрометации ключа перевыпуск превращается в проект на месяц.
   → правильно: внутренний CA (AD CS), шаблон на 1–2 года, раскатка корневого сертификата и профиля Wi-Fi через GPO, автопродление.

8. **Нет мониторинга Wi-Fi.**
   → узнаём о проблеме из жалоб, а не из графиков.
   → правильно: SNMP/API с контроллера в Zabbix на MON01 (число клиентов, занятость каналов, ошибки авторизации), плюс синтетическая проверка `radtest`.

**Безопасность.** В боевой среде нельзя: держать RADIUS в той же подсети, что и гостевой Wi-Fi; использовать WPA-TKIP и WEP; оставлять открытый SSID с доступом в внутреннюю сеть; хранить shared secret в конфиге точки в открытом виде без ограничения доступа к самой точке; отключать PMF на WPA3 ради «совместимости со старым сканером». Отдельно про стенд: пароль `Lab#2026-vektor` и секрет `lab-radius-secret` — учебные, в реальной работе так нельзя, там пароли только из хранилища секретов и с ротацией.

## 7. Вопросы на собеседовании

**Вопрос 1. Как ты проводишь радиообследование перед развёртыванием Wi-Fi и почему нельзя просто поставить точку в каждый кабинет?**

Ответ: сначала предсказательное — по планам этажей, материалу стен и модели точек считаю нужное число AP и канальный план. Потом натурное: хожу с ноутбуком, снимаю `iw dev survey dump` и сканы, измеряю RSSI и SNR в ключевых точках, замеряю занятость эфира. Целевые параметры: −67 dBm и SNR ≥ 25 dB на границе соты, перекрытие соседних сот 15–20 % для роуминга. После монтажа — валидация: iperf3 через точку, тест роуминга при перемещении, проверка отсутствия «липких» клиентов. Точка в каждом кабинете — это co-channel interference, лишние 30 % бюджета и худший результат, чем половина точек, поставленных по расчёту.

*Что хочет услышать интервьюер:* что ты работаешь с цифрами и знаешь три вида сюрвея, а не «погулял с телефоном».
*Красный флаг:* «ставлю точки, пока везде не будет 5 палок»; отсутствие любых числовых критериев.

**Вопрос 2. Чем автономная точка отличается от контроллерной архитектуры, и когда ты выберешь локальный контроллер, а когда облачный?**

Ответ: автономная AP хранит конфиг у себя — это 1–5 точек и отсутствие требований к роумингу. Дальше — контроллер: единый профиль SSID, централизованные VLAN и RADIUS, нормальный роуминг, статистика. Протокол обычно CAPWAP (UDP 5246 control, 5247 data), причём data может коммутироваться локально на точке, а не гнать всё через контроллер. Локальный контроллер выбираю, когда важна автономность от интернета, требования к персданным и импортозамещению (Eltex и т.п.), когда сеть в одном здании. Облачный — когда много площадок и мало ИТ-рук, но тогда закладываю резервный путь до интернета и понимаю, что будет при недоступности облака: точки должны уметь работать на последнем применённом конфиге.

*Что хочет услышать интервьюер:* понимание CAPWAP, local switching и отказоустойчивости контроллера (N+1).
*Красный флаг:* «контроллер всегда лучше» без разговора о единой точке отказа и зависимости от вендора.

**Вопрос 3. Что происходит от нажатия «подключиться» до того, как клиент получил IP, в сети с 802.1X?**

Ответ: клиент сканирует, ассоциируется с SSID, дальше идёт EAPOL-обмен (EtherType 0x888E): AP отправляет EAP-Request/Identity, клиент отвечает identity, для PEAP строится TLS-туннель, внутри него MSCHAPv2 с логином-паролем. AP перекладывает это в RADIUS `Access-Request` на UDP 1812, NPS/FreeRADIUS проверяет в AD и возвращает `Access-Accept` с ключами MS-MPPE и, если настроено, атрибутами `Tunnel-Private-Group-Id` для динамического VLAN. Только теперь выполняется 4-way handshake и появляются PTK/GTK, затем клиент получает IP по DHCP. Если нужен гостевой сценарий — после смены политики контроллер присылает RADIUS CoA и переводит сессию в другой VLAN без переподключения.

*Что хочет услышать интервьюер:* что ты знаешь порядок и умеешь отличить «не ассоциируется» от «не авторизуется» от «не получает IP».
*Красный флаг:* путаница между 802.11-ассоциацией и 802.1X-авторизацией; «RADIUS выдаёт IP».

**Вопрос 4. Как правильно сделать гостевой Wi-Fi, чтобы он был удобным и не открывал внутреннюю сеть?**

Ответ: отдельный SSID, отдельный VLAN, отдельная подсеть, DHCP из этой подсети, полный запрет маршрутов в корпоративные VLAN, client isolation. До авторизации — только walled garden: DNS и адреса портала плюс адреса captive portal detection, чтобы телефон сам показал окно. Портал — с таймаутом сессии, лимитом скорости и журналом, кто когда подключался (это ещё и требование по 152-ФЗ о хранении). После авторизации — RADIUS CoA или локальное решение контроллера. HTTPS не перехватываем, иначе получим ошибки сертификатов и справедливые жалобы гостей.

*Что хочет услышать интервьюер:* сегментация + понимание, как работает определение портала на устройствах.
*Красный флаг:* «гостям даём пароль от основной сети»; перехват 443 через самоподписанный сертификат без понимания последствий.

**Вопрос 5. Чем опасен общий PSK в офисе и что ты сделаешь с rogue AP?**

Ответ: PSK знают все, уволенный сотрудник сохраняет доступ, ключ лежит в открытом виде на каждой машине (`netsh wlan show profile key=clear`), а смена ключа — это обход всех устройств. Плюс PMKID-атаки и офлайн-подбор на слабом пароле. Решение — 802.1X с индивидуальными учётками и блокировкой в AD, EAP-TLS для критичных сегментов, WPA3-Enterprise 192-bit с PMF для новых устройств. Rogue AP ищем средствами WIDS/WIPS на контроллере: он видит BSSID, которых нет в плане, и умеет их локализовывать по уровню сигнала. Реакция: не «глушить» вслепую (в РФ это регуляторно спорно), а найти порт на коммутаторе, отключить его и разобраться с владельцем устройства.

*Что хочет услышать интервьюер:* связка «PSK → нет неотрекаемости → нет блокировки уволенного» и практический план реакции.
*Красный флаг:* «поставлю пароль посложнее» как единственная мера; предложение глушить чужие точки.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — разделы «Network Policy Server (NPS)», «Deploy 802.1X wireless access», «Wireless Network (IEEE 802.11) Policies» (GPO). Поиск: `NPS 802.1X wireless deployment` `[ПРОВЕРИТЬ актуальные названия разделов]`.
- **w1.fi / hostapd (документация в архиве wpa_supplicant, файл `hostapd.conf`)** — полное описание всех директив: `auth_server_addr`, `ieee80211w`, `bss=`, `bridge=`. Поиск: `hostapd.conf example WPA-EAP`.
- **freeradius.org** — документация FreeRADIUS, разделы «EAP module», «clients.conf», «users file», «TLS certificates». Поиск: `FreeRADIUS PEAP MSCHAPv2 howto`.
- **datatracker.ietf.org** — RFC 3748 (EAP), RFC 2865 (RADIUS), RFC 3580 (RADIUS для 802.1X), RFC 5216 (EAP-TLS). Поиск: `RFC 3580 IEEE 802.1X RADIUS guidelines`.
- **kernel.org** — документация ядра по `mac80211` и `mac80211_hwsim` (раздел wireless drivers). Поиск: `mac80211_hwsim testmode documentation`.
- **wi-fi.org** — Wi-Fi Alliance, разделы про WPA3, Wi-Fi 6/6E/7, спецификации. Поиск: `WPA3 Specification Wi-Fi Alliance`.
- **cisco.com** — руководства по Catalyst 9800 и беспроводным контроллерам, разделы «Radio Resource Management», «Client Roaming», «CAPWAP». Поиск: `Cisco 9800 wireless design guide CAPWAP`.
- **eltex-co.ru** — документация на контроллеры и точки доступа Eltex (российский вендор, реестр отечественного ПО). Поиск: `Eltex WLC руководство администратора`.
- **nginx.org** — раздел «Beginner's Guide», настройка `server {}`, `listen`, `root`. Поиск: `nginx beginner guide server block`.
- **packetfence.org** — открытый captive portal / NAC для гостевого доступа (как пример промышленного решения вместо самодельного). Поиск: `PacketFence captive portal documentation`.
- **nftables.org / wiki.nftables.org** — раздел про NAT и `redirect`, hook `prerouting`, `priority dstnat`. Поиск: `nftables redirect port example`.

Всё, что касается точных путей конфигов FreeRADIUS в конкретной версии Ubuntu и перечня каналов 5/6 ГГц в РФ по решениям ГКРЧ, сверяй с документацией на дату работы — помечено `[ПРОВЕРИТЬ]` в тексте.
