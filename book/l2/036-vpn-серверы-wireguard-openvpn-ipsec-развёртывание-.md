# VPN-серверы: WireGuard, OpenVPN, IPsec — развёртывание и диагностика в лаборатории

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №417.** Иван Иванов, бухгалтерия (WIN11-CL01): «Здравствуйте! Я на удалёнке второй день. VPN подключился (значок зелёный), а папка `\\FS01\Buh` не открывается — «Не удалось получить доступ». И 1С не грузится. Без VPN дома всё работало. Очень срочно, закрываю квартал».

Ситуация «туннель есть, а ресурсов нет» — это, наверное, самый частый тип VPN-заявки в поддержке L1/L2. И самая частая причина — не «VPN сломался», а маршрутизация, NAT или MTU на стороне шлюза. Поэтому VPN — это не «поставил пакет и забыл», а связка: ключи/сертификаты → туннель → маршруты → NAT → firewall → MTU → DNS.

В России 2026 года к этому добавляется регуляторика: для КИИ и ГИС нужны сертифицированные СКЗИ (КриптоПро, ViPNet, «Континент АПК»), а WireGuard/OpenVPN в чистом виде там не годятся. Но в 90% обычных контор VPN строится именно на них — потому что бесплатно, быстро и понятно.

После главы ты уметь: поднимать WireGuard, OpenVPN и IKEv2 на одном шлюзе, читать их статусы, находить причину «нет доступа через туннель» за 10 минут и не оставлять дыру в периметре.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Все три решения решают одну задачу — инкапсулировать твой IP-пакет в другой IP-пакет и погнать его через недоверенную сеть. Разница — в том, как договариваются о шифровании.

| | WireGuard | OpenVPN | IPsec (IKEv2) |
|---|---|---|---|
| Модель | Noise_IK, статические ключи | TLS поверх UDP/TCP | IKE + ESP |
| Порт/протокол | UDP 51820 | UDP 1194 (или TCP 443) | IKE UDP 500, NAT-T UDP 4500, ESP — IP-протокол 50 |
| Аутентификация | только ключи (Curve25519) | PKI: CA, сертификаты; или PSK | сертификаты, PSK, EAP |
| Где живёт | модуль ядра / userspace | userspace-процесс | ядро (XFRM) + демон IKE |
| Уровень | L3 | L3 (tun) или L2 (tap) | L3 (tunnel mode) / L3-payload (transport mode) |

```text
                 VMnet8 10.10.10.0/24  («интернет» в лабе)
   WIN11-CL01            |                    GW01 (Debian 12)
   10.10.10.1xx ---------+------------------- ens33 10.10.10.3
   (Иванов, «удалёнка»)                        ens37 10.10.20.1 --- DMZ --- WEB01 10.10.20.10
                                                    |
                              wg0  10.10.30.1/24  (WireGuard, UDP 51820)
                              tun0 10.10.40.1/24  (OpenVPN,  UDP 1194)
                              IKEv2 pool 10.10.40.200/28 (UDP 500/4500, ESP)
                                                    |
                       офис 10.10.10.0/24: DC01 10.10.10.10, FS01 10.10.10.20
```

**WireGuard.** Никакого рукопожатия «по сертификатам»: у каждого узла есть пара ключей, а `AllowedIPs` — это одновременно и таблица маршрутов, и ACL (что от пира принимать и куда ему можно ходить). Криптография: Curve25519, ChaCha20-Poly1305, BLAKE2s. Молчит в ответ на мусор — сканеры портов не видят открытый сервис. Минус: нет динамической выдачи адресов, нет встроенного DNS-push (в клиенте настраивается руками), нет CRL — отзыв устройства = удалить пир.

**OpenVPN.** Тяжёлый, но гибкий: TLS-рукопожатие, PKI с центром сертификации (CA), отзыв через CRL, `tun` (L3) и `tap` (L2), режимы «через UDP» и «через TCP 443», где его сложнее заблокировать. Медленнее WireGuard в разы, зато умеет всё, включая push маршрутов и DNS.

**IPsec.** Работает не портом, а целым IP-протоколом: ESP (50) шифрует, AH (51) только подписывает. Сначала IKE ставит SA (Security Association) — первый этап (IKE SA), потом второй (IPsec SA, ESP). За NAT добавляется NAT-T на UDP 4500, иначе ESP сломается. В Windows IPsec есть из коробки — и для VPN (Always On VPN/IKEv2), и для «доменной изоляции» в transport mode.

**MTU — главный источник тихих поломок.** Внешние заголовки съедают место: WireGuard = 20 (IP) + 8 (UDP) + 32 (WG) = 60 байт, поэтому `wg-quick` ставит MTU 1420. OpenVPN с `mssfix`/`tun-mtu` — история та же. Симптом: ping проходит, а RDP/шарa/1С «висят».

## 3. Аналогия из жизни

Представь, что ты пересылаешь секретную папку документов через чужую почту. **WireGuard** — это фотографии страниц: ты заранее договорился с директором, у каждого свой личный штемпель, и любая страница, несущая штемпель, сразу считается легальной; курьер не знает адресов, он просто передаёт файл. **OpenVPN** — это официальный нотариальный пакет: сначала ты встречаешься с нотариусом (TLS-рукопожатие), проверяешь удостоверения друг друга, и только потом курьер везёт штемпелеванные страницы; расходов больше, но есть журнал нотариальных подписей. **IPsec** — это бронированный контейнер, который ставят не вокруг твоего сотрудника, а вокруг каждого ящика с документами: его пломбирует отдельная служба (IKE), и по дороге никто не видит ничего.

*Где аналогия перестаёт работать: в реальности курьерская доставка тебя не роняет; VPN же ломается ровно потому, что отправитель думает, что работает, а получатель не знает, куда ответить.*

## 4. Практика и команды

**1) Генерация ключей WireGuard**
```bash
umask 077
wg genkey | tee /etc/wireguard/gw01.key | wg pubkey > /etc/wireguard/gw01.pub
```
`genkey` — 32 случайных байта в base64, `pubkey` считает публичный из приватного. Вывод — две строки по 44 символа. Смотреть: `gw01.key` не должен быть доступен никому, кроме root; в чат и репозиторий его не вставляем.

**2) Управление туннелем и статус**
```bash
wg-quick up wg0
wg show
```
```text
interface: wg0
  public key: 3lRk...=
  private key: (hidden)
  listening port: 51820
peer: 8Xkb...=
  endpoint: 10.10.10.135:51422
  allowed ips: 10.10.30.12/32
  latest handshake: 12 seconds ago
  transfer: 4.21 KiB received, 6.02 KiB sent
  persistent keepalive: every 25 seconds
```
Смотрим: `latest handshake` обновляется (значит, крипто с пиром сходится), `transfer` растёт, `allowed ips` — не шире, чем нужно.

**3) Статус для скрипта/мониторинга**
```bash
wg show wg0 latest-handshakes
wg show wg0 dump
```
`latest-handshakes` — пары «pubkey unixtime». `0` = соединения не было никогда. Удобно вешать в Zabbix на порог «не было рукопожатия 5 минут».

**4) Кто слушает**
```bash
ss -lunp | grep -E '51820|1194'
```
У WireGuard сокет держит ядро, поэтому в колонке Process будет пусто — это нормально [ПРОВЕРИТЬ на вашей версии ядра]. У OpenVPN увидите процесс `openvpn`.

**5) Вижу ли я пакеты**
```bash
tcpdump -ni any udp port 51820 -c 20
tcpdump -ni wg0 icmp
```
Первое — есть ли трафик вообще; второе — доходит ли расшифрованный пакет до внутреннего интерфейса.

**6) Форвардинг и NAT**
```bash
sysctl net.ipv4.ip_forward
nft list chain ip nat postrouting
```
`ip_forward = 1` обязателен. Пустой `postrouting` при работающем туннеле — гарантированное «нет ответа от FS01».

**7) MTU**
```bash
ping -M do -s 1400 -c 2 10.10.10.20
ip link show wg0
```
`-M do` — запрет фрагментации. 1400 данных + 28 заголовков = 1428 байт, в туннель с MTU 1420 не влезет: увидишь «Message too long». В Windows: `ping -f -l 1372 10.10.10.20`.

**8) OpenVPN: сервер и логи**
```bash
systemctl status openvpn@server
journalctl -u openvpn@server -n 50 --no-pager
openvpn --config /etc/openvpn/server.conf --verb 4
```
В логе ищем `Initialization Sequence Completed` (поднялся) и `TLS Error`/`AUTH_FAILED` (проблемы аутентификации).

**9) PKI для OpenVPN (easy-rsa)**
```bash
make-cadir /etc/openvpn/easy-rsa && cd /etc/openvpn/easy-rsa
./easyrsa init-pki
./easyrsa --batch build-ca nopass
./easyrsa gen-req server nopass
./easyrsa --batch sign-req server server
./easyrsa --batch build-client-full ivanov-laptop nopass
./easyrsa revoke ivanov-laptop && ./easyrsa gen-crl
```
`build-client-full` = ключ + запрос + подпись одной командой. `revoke` + `gen-crl` — отзыв украденного ноутбука.

**10) Клиент OpenVPN с отладкой**
```bash
openvpn --config ivanov-laptop.ovpn --verb 4
```
`--verb 4` — умеренная детализация: видно `PUSH_REPLY` (сервер прислал маршруты и DNS) и полученный адрес.

**11) IPsec в Linux: SAs и ядро**
```bash
ipsec statusall
swanctl --list-sas          # альтернативный интерфейс в strongSwan 5.9 [ПРОВЕРИТЬ]
ip xfrm state
ip xfrm policy
```
`ESTABLISHED` — SA стоит. `ip xfrm` показывает, что реально лежит в ядре: SPI, алгоритмы, счётчики байт. Разошлись `statusall` и `xfrm` — проблема между демоном и ядром.

**12) Windows-сторона**
```powershell
Get-VpnConnection
Get-VpnConnection -Name "VPN GW01" | Select-Object ConnectionStatus,ServerAddress,TunnelType
rasdial "VPN GW01"
Get-NetIPsecMainModeSA
Get-NetIPsecQuickModeSA
Test-NetConnection 10.10.10.20 -Port 445
```
`rasdial` поднимает подключение из cmd (удобно для скриптов). `Get-NetIPsec*SA` — таблицы ассоциаций IPsec; если правило есть, а SA нет — не сошлась аутентификация.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- Стенд L2 готов: DC01 (10.10.10.10), FS01 (10.10.10.20), WIN11-CL01 (ivanov, DHCP), UBU01 (10.10.10.30), домен `corp.vektor.lab`, группы `GG-Buh`.
- Должны быть пройдены уроки 29 (MTU), 30 (маршруты), 35 (nftables) — здесь мы это используем.
- **GW01** должен существовать как маршрутизатор Debian 12 с двумя адаптерами (10.10.10.3 / 10.10.20.1) — если ты его ещё не поднимал в уроке 35, сделай это сейчас: минимально нужны два интерфейса, `ip_forward=1` и nftables.
- Имена интерфейсов на GW01 смотри `ip -br a`; в примерах — `ens33` (офис) и `ens37` (DMZ). У тебя могут быть другие.
- **Адресный план туннелей** — это единственное расширение стенда, которое вводится здесь и которого нет в описании: `10.10.30.0/24` (WireGuard), `10.10.40.0/24` (OpenVPN и пул IKEv2). Запиши это в `stand-log.md` первой строкой лабы.

### Снапшот
В VMware Workstation: выделить ВМ → **VM → Snapshot → Take Snapshot**, имя `before-l2-036`. Сделать на **GW01**, **WIN11-CL01**, **UBU01**. Если меняешь что-то ещё — и на них.

### Шаги

1. **GW01.** `ip -br a` → зафиксировать имена интерфейсов и адреса. Ожидаемо: `ens33 10.10.10.3/24`, `ens37 10.10.20.1/24`, шлюз по умолчанию 10.10.10.2 (NAT VMware).
2. **GW01.** `apt update && apt install -y wireguard-tools openvpn easy-rsa strongswan nftables` затем `wg --version`. Ожидаемо: пакеты встали, версия wireguard-tools вывелась. Если `wireguard-tools` недоступен — проверь `sources.list` [ПРОВЕРИТЬ под свою редакцию Debian].
3. **GW01.** Включить форвардинг навсегда:
```bash
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-vpn.conf
sysctl --system && sysctl net.ipv4.ip_forward
```
Ожидаемо: `net.ipv4.ip_forward = 1`.
4. **GW01.** Добавить в `/etc/nftables.conf` (в существующие таблицы, **не** через `flush ruleset` — иначе снесёшь свою работу из урока 35):
```bash
# в table inet filter, chain input
udp dport { 51820, 1194, 500, 4500 } accept
ip protocol esp accept

# в table ip nat, chain postrouting
ip saddr 10.10.30.0/24 oifname "ens33" masquerade
ip saddr 10.10.40.0/24 oifname "ens33" masquerade
```
Применить: `nft -f /etc/nftables.conf && systemctl enable --now nftables`. Ожидаемо: ошибок нет, `nft list chain ip nat postrouting` показывает два правила masquerade.
5. **GW01.** Сгенерировать ключи: свой (`gw01`), для UBU01 (`ubu01`) и для ноутбука Иванова (`win11`). Способ — как в п.4.1 практики. Ожидаемо: 6 файлов, приватные — с правами 600.
6. **GW01.** Создать `/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.10.30.1/24
ListenPort = 51820
PrivateKey = <приватный ключ GW01>

[Peer]
# UBU01
PublicKey = <публичный ключ UBU01>
AllowedIPs = 10.10.30.11/32

[Peer]
# WIN11-CL01, ivanov
PublicKey = <публичный ключ WIN11-CL01>
AllowedIPs = 10.10.30.12/32
```
`chmod 600 /etc/wireguard/wg0.conf`, затем `systemctl enable --now wg-quick@wg0 && wg show`. Ожидаемо: интерфейс `wg0` с портом 51820, два пира, handshake пока `0` — это нормально, клиентов ещё нет.
7. **UBU01.** `apt install -y wireguard-tools`, создать `/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.10.30.11/32
PrivateKey = <приватный ключ UBU01>

[Peer]
PublicKey = <публичный ключ GW01>
Endpoint = 10.10.10.3:51820
AllowedIPs = 10.10.10.0/24, 10.10.30.0/24
PersistentKeepalive = 25
```
`wg-quick up wg0`, затем `ping -c2 10.10.30.1` и `ping -c2 10.10.10.20`. Ожидаемо: оба ping проходят, на GW01 `wg show` показывает свежий handshake.
8. **WIN11-CL01 (ivanov).** Поставить официальный клиент WireGuard for Windows (бесплатный, с wireguard.com; вариант `winget install WireGuard.WireGuard` [ПРОВЕРИТЬ id пакета]). Создать туннель: Address `10.10.30.12/32`, Peer — публичный ключ GW01, Endpoint `10.10.10.3:51820`, AllowedIPs `10.10.10.0/24, 10.10.30.0/24`, keepalive 25. Подключить, затем `ping 10.10.10.20` и открыть `\\FS01\Buh`. Ожидаемо: папка открывается под правами ivanov (он в `GG-Buh`).
9. **GW01.** OpenVPN. PKI по п.4.9 (имя клиента `ivanov-laptop`), ключ `tls-crypt`:
```bash
openvpn --genkey secret /etc/openvpn/ta.key
```
Файл `/etc/openvpn/server.conf`:
```ini
port 1194
proto udp
dev tun
ca /etc/openvpn/easy-rsa/pki/ca.crt
cert /etc/openvpn/easy-rsa/pki/issued/server.crt
key /etc/openvpn/easy-rsa/pki/private/server.key
dh none
ecdh-curve prime256v1
tls-crypt /etc/openvpn/ta.key
server 10.10.40.0 255.255.255.0
push "route 10.10.10.0 255.255.255.0"
push "dhcp-option DNS 10.10.10.10"
keepalive 10 60
persist-key
persist-tun
user nobody
group nogroup
status /var/log/openvpn-status.log
verb 3
```
`systemctl enable --now openvpn@server`, затем `ss -lunp | grep 1194`. Ожидаемо: порт слушается, в `journalctl -u openvpn@server` нет ошибок.
10. **UBU01.** Собрать `ivanov-laptop.ovpn` (ca.crt, issued/ivanov-laptop.crt, private/ivanov-laptop.key, ta.key, `remote 10.10.10.3 1194`, `dev tun`, `verb 4`) и запустить `openvpn --config ivanov-laptop.ovpn`. Ожидаемо: `Initialization Sequence Completed`, в `ip -br a` появился `tun0` с адресом из 10.10.40.0/24, `ping 10.10.10.20` идёт.
11. **GW01.** strongSwan: `/etc/ipsec.conf` — `conn ikev2-rw` с `left=10.10.10.3`, `leftsubnet=10.10.10.0/24`, `leftauth=psk`, `right=%any`, `rightauth=psk`, `rightsourceip=10.10.40.200/28`, `rightdns=10.10.10.10`, `auto=add`, `keyexchange=ikev2`. В `/etc/ipsec.secrets` — `%any %any : PSK "VektorLab2026psk"` (пароль **без** `#` и кавычек внутри: `#` в этом файле может читаться как комментарий [ПРОВЕРИТЬ]). `ipsec restart && ipsec statusall`. Ожидаемо: `0 up, 0 connecting` — коннект ждёт клиента.
12. **WIN11-CL01.** Параметры → Сеть и Интернет → VPN → Добавить: имя `VPN GW01`, сервер `10.10.10.3`, тип IKEv2, вход по предварительному ключу `VektorLab2026psk`. Подключить, затем на GW01 `ipsec statusall` и `ip xfrm state`. Ожидаемо: `ESTABLISHED`, два SA (IKE + ESP), счётчики байт растут. С Windows — `Get-VpnConnection` показывает `Connected`.
13. Записать в `stand-log.md`: адресный план туннелей, ключи/сертификаты, открытые порты, адреса пиров.

### Проверка
- [ ] `wg show` на GW01: у обоих пиров есть свежий handshake, transfer в обе стороны не равен нулю.
- [ ] С UBU01 `ping 10.10.10.20` и `ping 10.10.10.10` проходят.
- [ ] На WIN11-CL01 под ivanov открывается `\\FS01\Buh`, а `\\FS01\HR` — нет (права группы работают как надо).
- [ ] С WIN11-CL01 при включённом VPN обычный интернет не отвалился (split tunnel: AllowedIPs только 10.10.10.0/24).
- [ ] `ss -lunp` на GW01: слушаются 51820 и 1194.
- [ ] `journalctl -u openvpn@server` содержит `Initialization Sequence Completed` для клиента.
- [ ] `ipsec statusall` — `ESTABLISHED`, и `ip xfrm state` показывает ESP с ненулевыми байтами.
- [ ] `nft list chain ip nat postrouting` — два правила masquerade.
- [ ] В `stand-log.md` появилась запись о новой адресации.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «VPN подключается, значок зелёный, пишет «туннель активен». Но `\\FS01\Buh` не открывается — «Не удалось получить доступ». И `ping 10.10.10.20` не идёт. Раньше из офиса всё открывалось. Срочность: средняя, до конца дня нужен отчёт».
> **Срочность:** средняя (блокирует работу, но не всю компанию).

**Как воспроизвести поломку.** На GW01 от имени root:
```bash
nft flush chain ip nat postrouting
```
Туннель остаётся полностью рабочим, но ответ из офисной сети теперь некуда вернуть.

**Диагностика по шагам (не перескакивать!):**
1. WIN11-CL01: `wg show` — handshake свежий, transfer капает. Вывод: ключи, порт и endpoint в порядке, проблема не в туннеле.
2. WIN11-CL01: `ping 10.10.30.1` — отвечает. Туннель до шлюза жив.
3. WIN11-CL01: `ping 10.10.10.20` — тишина. Проблема на пути «шлюз → офис».
4. GW01: `sysctl net.ipv4.ip_forward` — `1`. Форвардинг не виноват.
5. GW01: `tcpdump -ni wg0 icmp` — видим echo request от 10.10.30.12. Пакет вошёл в расшифрованном виде.
6. GW01: `tcpdump -ni ens33 icmp` — видим тот же запрос, уходящий с **источником 10.10.30.12**. Вот и корень: NAT не выполняется, FS01 (шлюз 10.10.10.10) не знает, куда отвечать на 10.10.30.0/24.
7. GW01: `nft list chain ip nat postrouting` — пусто. Подтверждено.
8. Восстановить правила: `nft -f /etc/nftables.conf`, снова `nft list chain ip nat postrouting` (правила на месте), затем с клиента `ping 10.10.10.20` и открыть `\\FS01\Buh`.

**Ответ пользователю:** «Иван, добрый день. Туннель у вас был исправен, проблема была на нашем VPN-шлюзе: после правки firewall пропало правило трансляции адресов, поэтому ответы из офисной сети не возвращались к вам. Правило восстановил, проверил на своей стороне — `\\FS01\Buh` открывается. Переподключите VPN и попробуйте ещё раз, если что — сразу напишите».

**Закрытие заявки:** категория «Сеть / VPN», приоритет средний, корневая причина «отсутствовало правило masquerade в nat postrouting на GW01», действие «правило восстановлено, проверены все три VPN-сервиса», профилактика — «настройки nftables в git, после изменений обязательная проверка `nft list ruleset` и тест туннеля с клиента».

### Что изменилось на стенде
- GW01 стал VPN-шлюзом: пакеты `wireguard-tools`, `openvpn`, `easy-rsa`, `strongswan`; сервисы `wg-quick@wg0`, `openvpn@server`, `ipsec`.
- Новые адресные пространства (расширение стенда, зафиксировано в `stand-log.md`): `10.10.30.0/24` — WireGuard (GW01 = .1), `10.10.40.0/24` — OpenVPN (GW01 = .1) и пул IKEv2 `10.10.40.200/28`.
- Пиры WireGuard: UBU01 `10.10.30.11/32`, WIN11-CL01 `10.10.30.12/32`.
- Ключи в `/etc/wireguard/`, PKI в `/etc/openvpn/easy-rsa/pki/`, сертификат клиента `ivanov-laptop`.
- В nftables на GW01: разрешены UDP 51820, 1194, 500, 4500 и `ip protocol esp`; два правила masquerade в `ip nat postrouting`.
- На WIN11-CL01: клиент WireGuard + профиль `VPN GW01` (IKEv2, PSK).

### Откат
Мягкий: на GW01 `wg-quick down wg0`, `systemctl disable --now openvpn@server`, `ipsec stop`, убрать добавленные правила nftables и перезагрузить firewall; на клиентах отключить/удалить туннели. Жёсткий: **VM → Snapshot → Snapshot Manager** → выбрать `before-l2-036` → **Restore** на GW01, WIN11-CL01, UBU01.

## 6. Ошибки новичков и безопасность

1. **Приватные ключи в общем чате/репозитории** → компрометация всего VPN. Правильно: ключ генерится на конечной машине, передаётся только публичный; приватные — 600 и вне git.
2. **`AllowedIPs = 0.0.0.0/0` «чтобы всё работало»** → весь трафик клиента уходит в туннель, при отсутствии NAT на шлюзе интернет отваливается, а DNS утекает. Правильно: split tunnel — только нужные подсети.
3. **Забыли `ip_forward` или masquerade** → классика «туннель есть, ресурсов нет». Правильно: чек-лист «форвардинг → NAT → firewall → DNS» и `tcpdump` на обоих интерфейсах.
4. **Пир с `AllowedIPs = 10.10.10.0/24`** → устройство получает доступ ко всей офисной сети одним ключом, без ACL. Правильно: `/32` на конкретный адрес клиента.
5. **Игнорирование MTU** → «ping идёт, RDP/1С/шара висят». Правильно: `ping -M do`, `mssfix`/`tun-mtu`, тест после развёртывания.
6. **OpenVPN без `tls-crypt`/`tls-auth`** → сервер отвечает на любые TLS-пробы, его легко найти и нагрузить. Правильно: всегда включать.
7. **Один PSK на всех для IKEv2** → ушёл сотрудник, меняем ключ у всей компании. Правильно: сертификаты + CRL, PSK — только на время теста.
8. **VPN «настроил и забыл»** → никто не замечает, что handshake пропал неделю назад. Правильно: мониторинг `wg show latest-handshakes` и логов OpenVPN.

**Безопасность.** В лабе пароль стенда `Lab#2026-vektor` и PSK `VektorLab2026psk` — учебные; в боевой среде так нельзя: разные секреты на каждый сервис, ротация, хранение в менеджере секретов. Не отключай firewall ради теста «навсегда» — только точечное правило и обратно. Не публикуй VPN-порт в интернет без нужды и без ограничения источника. И помни про регуляторику РФ: для КИИ/ГИС/персональных данных в госструктурах нужны сертифицированные СКЗИ (КриптоПро, ViPNet, «Континент АПК»), а WireGuard/OpenVPN — это про обычный бизнес и обучение. Дистрибутивы WireGuard и OpenVPN — свободные, но скачивание из РФ иногда требует зеркала; легальная альтернатива — пакеты из репозиториев Debian/Ubuntu.

## 7. Вопросы на собеседовании (4–5 вопросов с ответами)

**В1. Чем WireGuard отличается от OpenVPN и что вы выберете для 50 сотрудников?**
Ответ: WireGuard — статические ключи, Noise_IK, UDP 51820, код в ядре, очень быстрый, но нет PKI, CRL и автоматической выдачи адресов/DNS — управление ключами руками или скриптами. OpenVPN — TLS, PKI с CA, отзыв сертификатов, push маршрутов и DNS, TCP 443, работает везде, но медленнее и тяжелее. Для 50 человек я бы взял WireGuard с простым генератором конфигов и мониторингом handshake, а OpenVPN — если нужна интеграция с существующим CA/AD или обход ограничений по TCP 443.
*Что хочет услышать интервьюер:* понимание компромисса «простота и скорость vs управляемость и гибкость», а не «WireGuard лучше».
*Красный флаг:* «WireGuard безопаснее, потому что новый» — без конкретики про PKI, отзыв ключей и мониторинг.

**В2. Пользователь говорит: VPN подключился, но ресурсы недоступны. Ваши действия?**
Ответ: сначала отделяю «туннель» от «доступа». Смотрю на сервере статус (`wg show` / `openvpn-status.log` / `ipsec statusall`): есть ли handshake и трафик. Пингую адрес шлюза внутри туннеля — жив ли туннель. Потом пингую внутренний ресурс. Если туннель жив, а ресурс нет — проверяю `ip_forward`, NAT/masquerade, правила firewall, маршруты на внутренних хостах (знают ли они обратный путь до туннельной подсети), DNS и MTU. Смотрю `tcpdump` на туннельном и внешнем интерфейсе: с каким source уходит пакет и приходит ли ответ.
*Что хочет услышать интервьюер:* чёткий порядок «туннель → маршрут → NAT → firewall → DNS → MTU» и работа с tcpdump, а не «перезагрузил сервер — заработало».
*Красный флаг:* сразу менять конфиг без диагностики или винить провайдера/пользователя.

**В3. Что такое split tunnel и full tunnel, какие риски у каждого?**
Ответ: full tunnel (`AllowedIPs 0.0.0.0/0`) — весь трафик клиента идёт через компанию: удобно для контроля и фильтрации, но нагружает канал и требует NAT/маршрутизации «в интернет» на шлюзе. Split tunnel — в туннель идут только корпоративные подсети: быстрее и дешевле, но клиент одновременно в двух сетях, значит возможны утечки DNS и обход корпоративных фильтров, а также атака с заражённого домашнего ПК напрямую в LAN.
*Что хочет услышать интервьюер:* понимание, что split tunnel — это не только про скорость, но и про безопасность и DNS.
*Красный флаг:* «split tunnel всегда лучше, он же быстрее».

**В4. Зачем в IPsec нужен NAT-T и порт 4500?**
Ответ: ESP — это IP-протокол 50, у него нет портов, поэтому NAT-устройство не может сопоставить ответ с соединением и ломает его. NAT-T инкапсулирует ESP в UDP 4500, добавляя 4-байтовый заголовок, и дополнительно обнаруживает NAT при рукопожатии, чтобы подставить корректные идентификаторы в IKE. IKE при этом идёт по UDP 500, а после обнаружения NAT переезжает на 4500.
*Что хочет услышать интервьюер:* что кандидат понимает разницу между «портом» и «IP-протоколом» и почему firewall-правило `udp 500` без 4500 и `esp` не работает.
*Красный флаг:* «NAT-T нужен для шифрования» или путаница между IKE и ESP.

**В5. Почему через VPN «плывёт» MTU и как это лечить?**
Ответ: инкапсуляция добавляет заголовки: WireGuard — около 60 байт (IP+UDP+WG), OpenVPN — свои служебные поля, поэтому полезная нагрузка должна уменьшиться, иначе крупные пакеты с DF-флагом дропаются, а мелкие (ping, DNS) проходят. Лечится уменьшением MTU на туннельном интерфейсе (WireGuard по умолчанию 1420) и/или MSS clamping в firewall (`tcp mss clamp`/`--clamp-mss-to-pmtu` в nftables/iptables), плюс диагностика `ping -M do -s` (Linux) и `ping -f -l` (Windows), в идеале — `tracepath`/`mtr` для поиска реального PMTU.
*Что хочет услышать интервьюер:* понимание, что это не «глюк VPN», а физика инкапсуляции, и знание конкретных инструментов.
*Красный флаг:* «поставил MTU 1200, и всё заработало» без объяснения, почему и какой ценой.

## 8. Источники (официальная документация)

- **wireguard.com** — раздел «Quick Start», а также man-страницы `wg(8)` и `wg-quick(8)`. Поиск: `wireguard quick start`, `wg-quick man page`.
- **openvpn.net** — «OpenVPN 2.x How-To» и «Reference Manual» (разделы про `tls-crypt`, `server`, `push`, `mssfix`). Поиск: `openvpn how-to tls-crypt`, `openvpn reference manual mssfix`.
- **docs.strongswan.org** — «Introduction» и «Configuration» (ipsec.conf, swanctl, PSK, right sourceip). Поиск: `strongswan ikev2 psk roadwarrior`.
- **learn.microsoft.com** — «Always On VPN», «VPN technical overview», «Troubleshooting Windows VPN», а также справочники по `Get-VpnConnection`, `Get-NetIPsecMainModeSA`. Поиск: `windows vpn ikev2 troubleshooting`.
- **wiki.debian.org** и **ubuntu.com/server/docs** — разделы про WireGuard, OpenVPN и nftables. Поиск: `debian wiki wireguard`, `ubuntu server docs openvpn`.
- **nftables.org** — «Quick reference» по masquerade и MSS clamping. Поиск: `nftables masquerade mss clamp`.

Всё, где в тексте стоит `[ПРОВЕРИТЬ]`, сверяй по этим ресурсам под свою версию ПО: номера версий и точные пути файлов меняются.
