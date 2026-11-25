# Сети в облаке: VPC, подсети, NAT, VPN, peering, Load Balancer, WAF, DDoS-защита

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №4812.** Пользователь: petrova (Мария Петрова, HR). ВМ: WIN10-CL02. Текст: «HR-портал опять отдаёт 502. Вчера работал. Коллеги говорят, что „из офиса не открывается, а с телефона по LTE открывается“». Срочность: высокая, до конца дня отчёт.

Пока вы разбираетесь, выясняется, что портал живёт в облаке: приложение — в приватной подсети без публичного IP, наружу смотрит балансировщик, доступ в интернет у приложения только через NAT-шлюз, а из офиса к нему ходят через VPN. И «с телефона работает, из офиса нет» — это классика: сломалась маршрутизация или пир-линк, а не само приложение.

Облачная сеть — это то, чем DevOps занимается каждый день: развернуть VPC, нарезать подсети, закрыть security groups, поднять NAT, соединить площадки VPN-ом, опубликовать сервис через балансировщик, накрыть его WAF и не упасть от DDoS. Всё это делается кодом (Terraform/Pulumi) и живёт в Git. После этой главы вы должны: понимать разницу между security group и NACL, уметь объяснить путь пакета от пользователя до пода в приватной подсети, знать, когда peering, а когда VPN/transit, чем L4-балансировщик отличается от L7, и за что отвечает WAF, а за что — анти-DDoS.

## 2. Теория «под капотом»

**VPC (Virtual Private Cloud)** — логически изолированная сеть в облаке, которую вы нарезаете сами. Внутри неё вы задаёте CIDR (например, `10.100.0.0/16`), создаёте подсети, таблицы маршрутизации, шлюзы и списки доступа. Всё, что вне VPC, по умолчанию недостижимо.

**Подсети (subnets)** — диапазоны внутри VPC, привязанные к зоне доступности (в Yandex Cloud — `ru-central1-a/b/d`, в AWS — `eu-central-1a`). Делят на *публичные* (есть маршрут в интернет-шлюз) и *приватные* (маршрут только через NAT). Правило проектирования: база данных и бэкенд — в приватных, балансировщик и bastion — в публичных. Подсети в одной VPC маршрутизируются между собой автоматически (в AWS — да, в GCP — да, в Yandex Cloud — да, в пределах сети).

**Маршрутизация** — таблицы маршрутов. В AWS route table привязана к подсети, в Yandex Cloud — к подсети тоже, в Azure — к подсети, в GCP — глобальные маршруты на всю VPC. Отсюда главный источник инцидентов: маршрут есть, а трафик не идёт, потому что его режет security group.

**Два уровня фильтрации:**
- **Security group / firewall rule** — *stateful*, привязан к сетевому интерфейсу (или к группе ресурсов). Разрешили входящий 443 — ответный трафик пойдёт сам.
- **NACL (network ACL)** — *stateless*, привязан к подсети, нужны явные правила в обе стороны. В российских облаках аналог NACL встречается реже; в Yandex Cloud его роль частично играет security group на подсети/группе [ПРОВЕРИТЬ].

**NAT (Network Address Translation).** Приватной ВМ нужен исходящий доступ (обновления, вызовы API), но не нужен входящий. Варианты:
- **NAT-шлюз провайдера (managed)** — отдельный сервис, платите за час + за гигабайт. Yandex Cloud: NAT-шлюз (`egress NAT`) + запись в таблице маршрутов. AWS: NAT Gateway. Azure: NAT Gateway. GCP: Cloud NAT.
- **NAT-инстанс** — своя ВМ с `iptables/nftables masquerade`. Дешевле для мелочи, дороже по надёжности: это ваш单 point of failure, который надо патчить и мониторить.

**VPN.** Site-to-site (офис ↔ облако) обычно на IPsec (strongSwan, libreswan) или WireGuard; client-to-site — для людей. Ключевые боли: перекрывающиеся CIDR (офис `10.10.10.0/24` и VPC `10.10.10.0/24` — туннель не поднимется нормально), MTU (инкапсуляция съедает ~60–80 байт → нужен MSS clamp), и keepalive, без которого NAT «забывает» сессию.

**Peering** — прямая связь двух VPC без выхода в интернет. Не транзитивен: A↔B и B↔C не дают A↔C. Для «звезды» нужен transit gateway / hub-and-spoke с VPC-роутером. Ограничение: CIDR не должны пересекаться.

**Load Balancer.** L4 (TCP/UDP, network LB) — быстро, не смотрит внутрь, сохраняет IP клиента через Proxy Protocol. L7 (HTTP/HTTPS, application LB) — терминирует TLS, умеет host/path-роутинг, health checks, sticky sessions, заголовки `X-Forwarded-For`, `X-Forwarded-Proto`. Health check — обязателен; балансировщик без проверки здоровья льёт трафик в мёртвый бэкенд.

**WAF (Web Application Firewall)** — reverse-proxy перед приложением, который матчит HTTP-запросы по сигнатурам (OWASP CRS) и блокирует SQLi, XSS, path traversal, LFI, ботов. Режимы: detection (только логи) и blocking. WAF работает на L7 и **не защищает от объёмных атак** на L3/L4.

**DDoS-защита** — это про провайдера: anycast, scrubbing-центры, фильтрация SYN-flood, amplification. На L7 — rate limiting на балансировщике/WAF/CDN и автобалансировка.

```text
                 Internet
                    │
        ┌───────────▼────────────┐
        │  Anti-DDoS (L3/L4)     │  ← провайдер, always-on + advanced
        └───────────┬────────────┘
                    │  public IP
        ┌───────────▼────────────┐
        │  WAF (L7) → LB (L7)    │  ← TLS termination, health checks
        └───────────┬────────────┘
                    │ 10.100.1.0/24  публичная подсеть
        ┌───────────▼────────────┐        ┌───────────────────┐
        │  app-1   app-2         │        │  NAT-шлюз         │
        │ 10.100.2.0/24 приватная│───────▶│  0.0.0.0/0        │
        └───────────┬────────────┘        └─────────┬─────────┘
                    │                                │
        ┌───────────▼────────────┐                  ▼
        │  DB 10.100.3.0/24      │              Internet (egress)
        └────────────────────────┘
                    ▲
                    │ VPN (WireGuard/IPsec) 10.200.0.0/30
        ┌───────────┴────────────┐
        │  офис 10.10.10.0/24    │  GW01
        └────────────────────────┘
```

## 3. Аналогия из жизни

VPC — это огороженный бизнес-центр с собственным адресом. Подсети — этажи: на первом («публичном») сидит ресепшн и охрана, на закрытых этажах — сотрудники. Security group — охранник на конкретной двери, который помнит, кого впустил, и пропускает ответ без повторного вопроса; NACL — турникет на входе в этаж, который ничего не помнит и проверяет каждого в обе стороны. NAT — секретарь: сотрудник отдаёт ему заявку, секретарь отправляет её от своего имени, ответ возвращается сотруднику; снаружи номера внутренних телефонов не знают. VPN — бронированный коридор между двумя бизнес-центрами. Peering — прямой переход между двумя соседними зданиями, но только между ними двумя: из третьего здания через него не пройти. Балансировщик — администратор на ресепшн, который распределяет посетителей по свободным сотрудникам и перестаёт отправлять людей к тому, кто не отвечает на внутренний звонок. WAF — сканер багажа, который вытаскивает подозрительные предметы. DDoS — толпа, забившая вход.

Аналогия ломается на stateful-природе: охранник-человек всё-таки иногда переспрашивает, а security group помнит состояние соединения строго по таблице и молча выбросит пакет, который вы «забыли» разрешить в обратную сторону.

## 4. Практика и команды

**1. Создание сети и подсетей (Yandex Cloud, `yc` CLI).**
```bash
yc vpc network create --name vpc-prod
yc vpc subnet create --name subnet-pub-a \
  --network-name vpc-prod --zone ru-central1-a --range 10.100.1.0/24
yc vpc subnet create --name subnet-app-a \
  --network-name vpc-prod --zone ru-central1-a --range 10.100.2.0/24
```
`--range` — CIDR, должен лежать внутри сети. Вывод — блок с `id`, `network_id`, `v4_cidr_blocks`. Смотрим, что `network_id` одинаковый, а диапазоны не пересекаются.

**2. Security group с явными правилами.**
```bash
yc vpc security-group create --name sg-web --network-name vpc-prod \
  --rule direction=ingress,port=443,protocol=tcp,v4-cidrs=[0.0.0.0/0] \
  --rule direction=ingress,port=22,protocol=tcp,v4-cidrs=[10.10.10.0/24] \
  --rule direction=egress,port=0,protocol=any,v4-cidrs=[0.0.0.0/0]
```
[ПРОВЕРИТЬ точный синтаксис `--rule` в вашей версии CLI.] Логика: 443 — всем, 22 — только из офисной сети, egress — любой. В Yandex Cloud по умолчанию создаётся `default-sg`; проверьте его правила глазами, прежде чем запускать прод [ПРОВЕРИТЬ политику default-sg].

**3. NAT-шлюз и таблица маршрутов (egress NAT).**
```bash
yc vpc gateway create --name nat-gw
yc vpc route-table create --name rt-private --network-name vpc-prod \
  --route destination=0.0.0.0/0,next-hop-gateway-id=<NAT_GW_ID>
yc vpc subnet update subnet-app-a --route-table-id <RT_ID>
```
[ПРОВЕРИТЬ формат `--route`.] Смысл: у приватной подсети появляется маршрут по умолчанию в NAT-шлюз, при этом публичного IP у ВМ нет.

**4. Peering двух VPC.**
```bash
yc vpc peering connection create --name peer-prod-shared \
  --source-id <VPC_A_ID> --destination-id <VPC_B_ID> [ПРОВЕРИТЬ]
```
После создания нужно **с обеих сторон** добавить маршруты на CIDR соседа в route table. Забыть про обратный маршрут — самая частая ошибка.

**5. AWS: VPC + подсеть + интернет-шлюз.**
```bash
aws ec2 create-vpc --cidr-block 10.100.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=vpc-prod}]'
aws ec2 create-subnet --vpc-id vpc-0abc --cidr-block 10.100.1.0/24 \
  --availability-zone eu-central-1a
aws ec2 create-internet-gateway
aws ec2 attach-internet-gateway --vpc-id vpc-0abc --internet-gateway-id igw-0abc
```
Вывод `create-vpc` содержит `"State": "pending"` — дождитесь `available`. IGW нужен только публичным подсетям.

**6. AWS: NAT Gateway и маршрут для приватной подсети.**
```bash
aws ec2 allocate-address --domain vpc          # → AllocationId
aws ec2 create-nat-gateway --subnet-id subnet-pub --allocation-id eipalloc-0abc
aws ec2 create-route --route-table-id rtb-priv \
  --destination-cidr-block 0.0.0.0/0 --nat-gateway-id nat-0abc
```
NAT Gateway живёт в **публичной** подсети, а используется приватной. Он платный: за час и за обработанный гигабайт.

**7. AWS: peering, в том числе межрегиональный и transit.**
```bash
aws ec2 create-vpc-peering-connection --vpc-id vpc-a --peer-vpc-id vpc-b \
  --peer-region eu-west-1
aws ec2 accept-vpc-peering-connection --vpc-peering-connection-id pcx-0abc
```
Для топологии «хаб» вместо паутины пиров — Transit Gateway (`aws ec2 create-transit-gateway`) в AWS и VPC-роутер/аналог в российских облаках [ПРОВЕРИТЬ наличие managed-аналога в вашем провайдере].

**8. IaC-версия сети (Terraform, Yandex Cloud).**
```hcl
resource "yandex_vpc_network" "prod" { name = "vpc-prod" }

resource "yandex_vpc_gateway" "nat" {
  name = "nat-gw"
  shared_egress_gateway {}
}

resource "yandex_vpc_route_table" "private" {
  name       = "rt-private"
  network_id = yandex_vpc_network.prod.id
  static_route {
    destination_prefix = "0.0.0.0/0"
    gateway_id         = yandex_vpc_gateway.nat.id
  }
}

resource "yandex_vpc_subnet" "app" {
  name           = "subnet-app-a"
  zone           = "ru-central1-a"
  network_id     = yandex_vpc_network.prod.id
  v4_cidr_blocks = ["10.100.2.0/24"]
  route_table_id = yandex_vpc_route_table.private.id
}
```
[ПРОВЕРИТЬ имена ресурсов `yandex_vpc_gateway`/`shared_egress_gateway` под вашу версию провайдера.] Сеть — в Git, изменения — через MR, `terraform plan` в CI.

**9. WireGuard: site-to-site туннель (облачная сторона).**
```bash
wg genkey | tee /etc/wireguard/priv.key | wg pubkey > /etc/wireguard/pub.key
chmod 600 /etc/wireguard/priv.key
```
```ini
# /etc/wireguard/wg0.conf на облачной стороне (UBU01)
[Interface]
Address    = 10.200.0.1/30
ListenPort = 51820
PrivateKey = <PRIV_UBU01>

[Peer]
PublicKey  = <PUB_GW01>
AllowedIPs = 10.10.10.0/24, 10.10.20.0/24
```
```bash
wg-quick up wg0
wg show                     # смотреть latest handshake и transfer
```
`AllowedIPs` — это и маршрут, и криптографический фильтр. `Latest handshake` старше ~3 минут при живом трафике = туннель не дышит.

**10. Load Balancer L7 (HAProxy) с health check.**
```haproxy
global
    log /dev/log local0
defaults
    mode http
    timeout connect 5s
    timeout client  30s
    timeout server  30s
frontend fe_http
    bind *:8080
    default_backend be_app
backend be_app
    balance roundrobin
    option httpchk GET /
    http-check expect status 200
    server app-a 10.100.1.10:8000 check
    server app-b 10.100.2.10:8000 check
```
`check` включает активные пробы; `http-check expect status 200` — что считать «живым». Смотреть `echo "show stat" | socat stdio /var/run/haproxy.sock` или `haproxy -c -f /etc/haproxy/haproxy.cfg`.

**11. WAF и rate limiting на nginx (упрощённая модель).**
```nginx
map $request_uri $bad_uri {
    default 0;
    "~*(union.*select|\.\./|<script)" 1;
}
limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;

server {
    listen 8081;
    if ($bad_uri) { return 403; }
    location / {
        limit_req zone=perip burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```
В проде вместо самодельного `map` — ModSecurity v3 + OWASP CRS или Coraza; в облаке — managed WAF (Yandex Cloud Smart Web Security, AWS WAF, Azure WAF, Cloud Armor) [ПРОВЕРИТЬ актуальное название WAF-сервиса у вашего провайдера].

**12. nftables: NAT и анти-flood на своей Linux-машине.**
```bash
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv4.tcp_syncookies=1
nft add table ip nat
nft add chain ip nat post { type nat hook postrouting priority 100 \; }
nft add rule ip nat post ip saddr 10.100.0.0/16 oifname "ens33" masquerade
nft add rule inet filter input tcp dport 8080 ct state new \
  limit rate over 20/second counter drop
```
`masquerade` — SNAT на исходящий интерфейс (имя взять из `ip -br a`). Второе правило — примитивный L4-лимитер: больше 20 новых соединений в секунду с одного… нет, тут без `per-ip` — это общий лимит; для per-IP нужен `meter`.

**13. Диагностика пути пакета.**
```bash
ip route get 10.10.20.10        # каким маршрутом пойдёт пакет и с каким src
wg show                         # состояние туннелей
mtr -n -c 20 10.10.20.10        # где теряются пакеты
curl -v -H "Host: hr.vektor.lab" http://10.10.10.30:8080/
dig +short hr.vektor.lab
```
`ip route get` — самая недооценённая команда: она сразу показывает, уйдёт ли пакет в туннель или в дефолт-шлюз.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Моделируем облачную сеть на стенде: VPC и подсети — через network namespaces на UBU01, NAT и анти-flood — nftables, VPN — WireGuard до GW01, балансировщик — HAProxy, WAF — nginx. Всё бесплатно и локально; опционально в конце — то же самое в Yandex Cloud на триальном гранте.

### Предусловия
- **UBU01** (10.10.10.30, Ubuntu 24.04): sudo, доступ в интернет для `apt`.
- **GW01** (10.10.10.3 / 10.10.20.1, Debian 12, nftables): играет роль офисного шлюза и второй стороны VPN.
- **WEB01** (10.10.20.10, nginx): целевой ресурс «в DMZ», до которого проверяем доступность через туннель.
- Windows-часть не нужна — DevOps-трек опирается только на Linux.
- Пакеты: `apt install -y nftables haproxy nginx wireguard-tools iproute2 curl mtr-tiny apache2-utils`.

### Снапшот
В VMware Workstation на UBU01, GW01 и WEB01: `VM → Snapshot → Take Snapshot`, имя **`before-devops-072`**. Если предпочитаете CLI: `vmrun snapshot "/path/UBU01.vmx" before-devops-072` [ПРОВЕРИТЬ путь и имя vmrun в вашей установке].

### Шаги

1. **UBU01.** Создаём две «VPC»-подсети как namespaces.
   ```bash
   sudo ip netns add vpc-a; sudo ip netns add vpc-b
   sudo ip link add veth-a type veth peer name veth-a-host
   sudo ip link set veth-a netns vpc-a
   sudo ip addr add 10.100.1.1/24 dev veth-a-host; sudo ip link set veth-a-host up
   sudo ip netns exec vpc-a ip addr add 10.100.1.10/24 dev veth-a
   sudo ip netns exec vpc-a ip link set veth-a up
   sudo ip netns exec vpc-a ip link set lo up
   sudo ip netns exec vpc-a ip route add default via 10.100.1.1
   ```
   Ожидаемо: `sudo ip netns exec vpc-a ping -c2 10.100.1.1` отвечает. Повторить для `vpc-b` с `10.100.2.0/24` / `10.100.2.10`.

2. **UBU01.** Проверяем изоляцию: `sudo ip netns exec vpc-a ping -c2 10.100.2.10` → **100% packet loss**. Это и есть «две VPC без пиринга».

3. **UBU01.** Включаем маршрутизацию и NAT (аналог NAT-шлюза провайдера).
   ```bash
   sudo sysctl -w net.ipv4.ip_forward=1
   sudo nft add table ip nat
   sudo nft add chain ip nat post '{ type nat hook postrouting priority 100 ; }'
   sudo nft add rule ip nat post ip saddr 10.100.0.0/16 oifname "<ВАШ_ИНТЕРФЕЙС>" masquerade
   sudo ip netns exec vpc-a ping -c2 10.10.10.10
   ```
   Ожидаемо: DC01 пингуется из namespace, хотя у `vpc-a` нет адреса из офисной сети. Проверка: `sudo ip netns exec vpc-a curl -s ifconfig.me` вернёт внешний IP стенда.

4. **UBU01.** Эмулируем **peering**: разрешаем маршрут между подсетями через хост.
   ```bash
   sudo ip netns exec vpc-b ip route add 10.100.1.0/24 via 10.100.2.1
   sudo ip netns exec vpc-a ip route add 10.100.2.0/24 via 10.100.1.1
   sudo ip netns exec vpc-a ping -c2 10.100.2.10
   ```
   Ожидаемо: пинг пошёл. Записываем в журнал: «peering = маршруты в обе стороны + отсутствие пересечения CIDR».

5. **GW01.** Поднимаем WireGuard (офисная сторона).
   ```bash
   sudo apt install -y wireguard-tools
   sudo -i; umask 077
   wg genkey > /etc/wireguard/priv.key; wg pubkey < /etc/wireguard/priv.key > /etc/wireguard/pub.key
   ```
   `/etc/wireguard/wg0.conf`:
   ```ini
   [Interface]
   Address = 10.200.0.2/30
   ListenPort = 51820
   PrivateKey = <PRIV_GW01>

   [Peer]
   PublicKey  = <PUB_UBU01>
   AllowedIPs = 10.100.0.0/16
   PersistentKeepalive = 25
   ```
   `sudo wg-quick up wg0`, затем `sudo nft list ruleset` — убедиться, что UDP/51820 не блокируется.

6. **UBU01.** Вторая сторона туннеля + маршрут в DMZ.
   ```bash
   sudo wg-quick up wg0            # конфиг из раздела 4, Address = 10.200.0.1/30
   sudo sysctl -w net.ipv4.ip_forward=1
   sudo ip route add 10.10.20.0/24 dev wg0
   sudo ip netns exec vpc-a ip route add 10.10.20.0/24 via 10.100.1.1
   sudo ip netns exec vpc-a curl -sI http://10.10.20.10 | head -1
   ```
   Ожидаемо: `HTTP/1.1 200 OK` — трафик из «облачной» подсети дошёл до DMZ через туннель. `sudo wg show` показывает свежий `latest handshake`.

7. **UBU01.** Поднимаем два «инстанса приложения» и балансировщик.
   ```bash
   sudo mkdir -p /srv/app-a /srv/app-b
   echo "app-a" | sudo tee /srv/app-a/index.html
   echo "app-b" | sudo tee /srv/app-b/index.html
   sudo ip netns exec vpc-a python3 -m http.server 8000 --directory /srv/app-a &
   sudo ip netns exec vpc-b python3 -m http.server 8000 --directory /srv/app-b &
   ```
   Конфиг HAProxy — из раздела 4, затем `sudo systemctl restart haproxy`.
   ```bash
   for i in 1 2 3 4; do curl -s http://10.10.10.30:8080/; done
   ```
   Ожидаемо: чередование `app-a` / `app-b`.

8. **UBU01.** Проверяем health check: гасим один бэкенд.
   ```bash
   sudo pkill -f "http.server 8000"      # убьёт оба, запустите заново только app-a
   sudo ip netns exec vpc-a python3 -m http.server 8000 --directory /srv/app-a &
   for i in 1 2 3; do curl -s http://10.10.10.30:8080/; done
   ```
   Ожидаемо: всегда `app-a`, 503 нет — HAProxy убрал мёртвый узел из ротации.

9. **UBU01.** Включаем WAF-прослойку (nginx на 8081) и проверяем блокировку.
   ```bash
   sudo tee /etc/nginx/sites-enabled/waf-demo.conf <<'EOF'
   map $request_uri $bad_uri {
       default 0;
       "~*(union.*select|\.\./|<script)" 1;
   }
   limit_req_zone $binary_remote_addr zone=perip:10m rate=10r/s;
   server {
       listen 8081;
       if ($bad_uri) { return 403; }
       location / {
           limit_req zone=perip burst=20 nodelay;
           proxy_pass http://10.10.10.30:8080;
           proxy_set_header X-Forwarded-For $remote_addr;
       }
   }
   EOF
   sudo nginx -t && sudo systemctl reload nginx
   curl -s -o /dev/null -w "%{http_code}\n" "http://10.10.10.30:8081/?id=1"
   curl -s -o /dev/null -w "%{http_code}\n" "http://10.10.10.30:8081/?id=1%20union%20select%201"
   ```
   Ожидаемо: `200`, затем `403`. Второй запрос — типичная SQLi-попытка.

10. **UBU01.** Имитируем L7-flood и смотрим, что видит защита.
    ```bash
    sudo nft add rule inet filter input tcp dport 8081 ct state new \
      limit rate over 20/second counter drop
    ab -n 3000 -c 200 http://10.10.10.30:8081/ || true
    sudo nft list ruleset | grep -A2 "dport 8081"
    grep "limiting requests" /var/log/nginx/error.log | tail -3
    ```
    Ожидаемо: часть запросов отваливается, счётчик `drop` растёт, в логе nginx — `limiting requests`. Это учебная модель; в облаке L3/L4-объём гасит провайдер, L7 — WAF/CDN.

11. **Журнал.** Дописать в `stand-log.md`: созданные namespaces, адреса `10.100.1.0/24` и `10.100.2.0/24`, туннель `10.200.0.0/30`, порт 8080 (LB), 8081 (WAF), правила nftables, маршрут `10.10.20.0/24 via wg0`.

12. **Опционально — реальное облако.** Если есть триальный грант Yandex Cloud, повторите шаги 1, 3, 7 командами из раздела 4 (`yc vpc network create` → подсети → SG → NAT-шлюз → ВМ с nginx → ALB). Не забудьте удалить ресурсы и проверить баланс: NAT-шлюз и ALB тарифицируются по часам.

### Проверка
- [ ] `ip netns list` показывает `vpc-a` и `vpc-b`, у каждого свой адрес.
- [ ] До шага 4 пинг между подсетями не проходил, после — проходит.
- [ ] `sudo ip netns exec vpc-a curl -s ifconfig.me` возвращает внешний IP (NAT работает).
- [ ] `sudo wg show` на UBU01 и GW01: `latest handshake` моложе 3 минут.
- [ ] `curl -sI http://10.10.20.10` из `vpc-a` даёт 200 — туннель до DMZ живой.
- [ ] `curl http://10.10.10.30:8080/` в цикле даёт оба бэкенда, после остановки одного — только живой.
- [ ] SQLi-запрос через 8081 возвращает 403, обычный — 200.
- [ ] Счётчик `drop` в nftables вырос после `ab`.
- [ ] `stand-log.md` обновлён.

### Сломай и почини

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02 / HR-портал
> **Что написал:** «Портал через балансировщик отдаёт 502, а вчера всё работало. И ещё: из офиса не открывается складская страница в DMZ, хотя с мобильного интернета открывается.»
> **Срочность:** высокая

**Как воспроизвести поломку (на UBU01, от sudo):**
```bash
# 1) «упал» один бэкенд, а health check настроен слишком мягко — убираем check у app-b
sudo sed -i 's/server app-b 10.100.2.10:8000 check/server app-b 10.100.2.10:8000/' /etc/haproxy/haproxy.cfg
sudo systemctl reload haproxy
sudo pkill -f "http.server 8000"        # оба бэкенда лежат
# 2) «сломался» VPN: дропаем UDP/51820 на GW01
sudo nft add rule inet filter input udp dport 51820 drop
```

**Диагностика по порядку:**
1. Смотрим на симптом снаружи: `curl -s -o /dev/null -w "%{http_code}\n" http://10.10.10.30:8080/` → 503. Значит, проблема за балансировщиком, а не на входе.
2. Проверяем бэкенды напрямую: `sudo ip netns exec vpc-a curl -sI http://10.100.1.10:8000` → connection refused. Приложение мертво.
3. Смотрим статистику LB: `echo "show stat" | sudo socat stdio /var/run/haproxy.sock | cut -d, -f1,2,18` → оба сервера `DOWN`. Если бы `check` стоял, HAProxy сам бы их исключил и отдал 503 осмысленно, а не «через боль».
4. Вторая жалоба — VPN: `sudo wg show` на UBU01 → `latest handshake` давно, `transfer` не растёт.
5. Проверяем связность до GW01: `ping -c3 10.10.10.3` — живой; значит, проблема в UDP.
6. `sudo nft list ruleset | grep -n 51820` на GW01 → видим правило `drop`. Причина найдена.
7. Поднимаем бэкенды: `sudo ip netns exec vpc-a python3 -m http.server 8000 --directory /srv/app-a &` (и app-b аналогично), возвращаем `check` в конфиг, `sudo systemctl reload haproxy`.
8. Удаляем правило: `sudo nft -a list ruleset | grep 51820` → взять handle, `sudo nft delete rule inet filter input handle <N>`.
9. Проверяем: `sudo wg show` — свежий handshake; `curl http://10.10.10.30:8080/` — 200; `sudo ip netns exec vpc-a curl -sI http://10.10.20.10` — 200.

**Ответ пользователю:** «Мария, добрый день. Причина 502 — упал процесс приложения на одном из бэкендов, плюс балансировщик не исключал его из ротации из-за неверной настройки health check. Подняли сервис, проверку здоровья исправили. Вторая проблема — блокирующее правило на VPN-шлюзе, из-за него офис не видел DMZ; правило убрали, туннель восстановлен. Сейчас портал и складская страница доступны, проверьте, пожалуйста. По итогам добавим алерт на падение бэкенда и на давность handshake.»

**Закрытие заявки:** категория «Сеть / VPN», решение — «восстановление сервиса + фикс конфигурации health check», в журнал — запись о причине и профилактике.

### Что изменилось на стенде
- UBU01: netns `vpc-a` (`10.100.1.0/24`), `vpc-b` (`10.100.2.0/24`), veth-пары, `ip_forward=1`, таблица `ip nat` с masquerade.
- UBU01: `wg0` `10.200.0.1/30`, маршрут `10.10.20.0/24 dev wg0`.
- GW01: `wg0` `10.200.0.2/30`, порт UDP/51820, пир на UBU01.
- UBU01: HAProxy на `:8080` с бэкендами `10.100.1.10:8000` и `10.100.2.10:8000`; nginx-WAF на `:8081`.
- UBU01: правила nftables — rate limit на 8081, счётчики drop.
- Каталоги `/srv/app-a`, `/srv/app-b` на UBU01.
- Запись в `stand-log.md` обо всём перечисленном.

### Откат
1. Снять туннели и namespaces:
   ```bash
   sudo wg-quick down wg0                       # на UBU01 и на GW01
   sudo pkill -f "http.server 8000"
   sudo ip netns del vpc-a; sudo ip netns del vpc-b
   sudo nft delete table ip nat                 # если таблица создавалась только для лабы
   sudo rm -f /etc/nginx/sites-enabled/waf-demo.conf && sudo systemctl reload nginx
   ```
2. Если состояние nftables/сети непонятно — вернуть снапшот: VMware Workstation → выбрать ВМ → `VM → Snapshot → Snapshot Manager` → `before-devops-072` → **Revert**. Повторить на UBU01, GW01, WEB01.
3. Отметить в `stand-log.md`, что изменения откачены.

## 6. Ошибки новичков и безопасность

1. **Открыть `0.0.0.0/0` на 22/3389 «чтобы зайти».** → Через сутки — брутфорс и попытка шифровальщика. → Только bastion/VPN, SSH по ключам, SG на конкретные CIDR.
2. **Забыть обратный маршрут при peering.** → Пакеты уходят и не возвращаются, «пинг не идёт, хотя всё разрешено». → Проверять route table с двух сторон и `ip route get`.
3. **Пересекающиеся CIDR (`10.10.10.0/24` в офисе и в VPC).** → Peering/VPN не работает или работает частично. → Планировать адресацию заранее, для офиса брать `10.10.10.0/24`, для облака — `10.100.0.0/16`.
4. **NAT-инстанс без мониторинга и бэкапа конфигурации.** → Тихо умирает — «весь приватный контур без интернета». → Managed NAT-шлюз либо инстанс в двух зонах + алерт.
5. **Балансировщик без health check или с проверкой `/` вместо `/health`.** → Трафик льётся в мёртвый бэкенд, пользователи видят 502/таймауты. → Явный liveness-эндпоинт, интервал и порог, алерт на «все бэкенды DOWN».
6. **WAF годами в режиме detection.** → Логи пухнут, атаки проходят. → Валидировать правила на staging, затем blocking + исключения, обновлять CRS.
7. **Считать WAF заменой анти-DDoS.** → Объёмная атака кладёт канал раньше, чем WAF что-то увидит. → L3/L4 — у провайдера, L7 — WAF + rate limit + autoscaling.
8. **MTU/MSS не настроены в VPN.** → Мелочь работает, `git clone` и HTTPS висят. → `TCPMSS --clamp-mss-to-pmtu` или `mtu 1420` на туннеле.

**Безопасность.** В боевой среде нельзя: оставлять SG `any-any` «на время», публиковать БД в публичную подсеть, хранить ключи WireGuard/IPsec и `yc`/`aws`-токены в Git (только Vault/SOPS/secret manager), отключать логирование балансировщика и WAF (без логов инцидент не разобрать), а также держать WAF bypass-путь по прямому IP бэкенда. Отдельно про **экономику атак**: DDoS на L7 с автомасштабированием может стоить денег — ставьте лимиты максимального числа инстансов и бюджеты-алерты (это тема урока 76).

## 7. Вопросы на собеседовании

**1. Чем security group отличается от NACL и что вы выберете по умолчанию?**
Security group — stateful, применяется к интерфейсу/группе ресурсов, ответный трафик разрешается автоматически; правила только allow. NACL — stateless, применяется к подсети, проверяет каждый пакет в обе стороны, поддерживает deny. По умолчанию — security group: она проще и не ломает обратный трафик; NACL — как дополнительный «грубый» барьер на подсети (например, deny на весь диапазон известных сканеров).
*Что хочет услышать:* stateful/stateless, уровень применения, allow vs deny, порядок обработки.
*Красный флаг:* «это одно и то же, только в разных облаках».

**2. Как дать ВМ в приватной подсети исходящий интернет, не давая публичный IP?**
Создать NAT-шлюз (managed) в публичной подсети, добавить в route table приватной подсети маршрут `0.0.0.0/0 → NAT`, обеспечить SG egress. Альтернатива — NAT-инстанс с masquerade, но это SPOF, который надо патчить и мониторить. Обратный входящий трафик через NAT не приходит — для этого нужен балансировщик или bastion.
*Что хочет услышать:* NAT + route table + отсутствие входящего доступа, понимание цены и отличий от публичного IP.
*Красный флаг:* «просто выдать публичный IP и закрыть файрволом».

**3. Peering, transit gateway или VPN — когда что?**
Peering — две VPC, не пересекающиеся CIDR, нужен прямой быстрый трафик без транзита; не транзитивен и плохо масштабируется при 10+ сетях. Transit gateway / VPC-роутер — когда сетей много и нужна звезда с централизованной маршрутизацией и инспекцией. VPN — когда соединяем облако с площадкой вне облака (офис, другой провайдер) или нужен шифрованный канал через интернет; IPsec — совместимость с оборудованием, WireGuard — простота и скорость.
*Что хочет услышать:* критерии выбора и ограничение «peering не транзитивен».
*Красный флаг:* «всегда peering, он же бесплатный».

**4. Чем L4-балансировщик отличается от L7? Где терминируется TLS?**
L4 балансирует TCP/UDP-потоки: быстро, мало функций, IP клиента передаётся через Proxy Protocol. L7 разбирает HTTP: host/path-роутинг, health check по URL, cookies, заголовки, обычно терминирует TLS (сертификат на LB, дальше — HTTP или re-encrypt). При терминации на LB бэкенд видит IP балансировщика — клиентский передаётся в `X-Forwarded-For`, и приложение должно ему доверять только от известного прокси.
*Что хочет услышать:* уровни, TLS-терминация, health check, X-Forwarded-For, производительность.
*Красный флаг:* «L7 всегда лучше, берём его везде» без понимания цены и потери клиентского IP.

**5. Как защищать сервис от DDoS и что делает WAF?**
Слои: L3/L4 (SYN-flood, amplification) — провайдерская always-on защита + scrubbing, свои nftables-лимиты как дополнение; L7 (flood запросами, боты) — WAF с CRS, rate limiting, капча, CDN, автoscaling с потолком. WAF инспектирует содержимое HTTP и блокирует эксплойты, но не гасит объём; анти-DDoS не отличает легитимный POST от вредоносного. Нужны оба + алерты и бюджеты.
*Что хочет услышать:* разделение L3/L4 и L7, роль провайдера, что WAF ≠ анти-DDoS.
*Красный флаг:* «поставим WAF, и DDoS не страшен».

## 8. Источники (официальная документация)

- **Yandex Cloud** — `yandex.cloud`, разделы документации: «Virtual Private Cloud» (сети, подсети, security groups, NAT-шлюз, маршрутизация, пиринг), «Application Load Balancer», «Network Load Balancer», «Smart Web Security / DDoS-защита» [ПРОВЕРИТЬ актуальные названия сервисов WAF и защиты от DDoS]. Поиск: `yandex cloud vpc security group terraform`, `yandex cloud egress nat gateway`.
- **AWS** — `docs.aws.amazon.com`, разделы «Amazon VPC User Guide» (subnets, route tables, NAT gateway, VPC peering, Transit Gateway), «Elastic Load Balancing User Guide», «AWS WAF Developer Guide», «AWS Shield». Поиск: `aws vpc nat gateway route table`, `aws waf web acl alb`.
- **Microsoft Azure** — `learn.microsoft.com`, разделы «Virtual Network», «Network security groups», «Azure Load Balancer / Application Gateway», «Azure WAF», «Azure DDoS Protection». Поиск: `azure vnet peering nsg overview`.
- **Google Cloud** — `cloud.google.com`, разделы «VPC overview», «Cloud NAT», «Cloud VPN / HA VPN», «Cloud Load Balancing», «Cloud Armor». Поиск: `gcp vpc firewall rules cloud nat`.
- **WireGuard** — `wireguard.com`, раздел «Quick Start» и man-страницы `wg(8)`, `wg-quick(8)`. Поиск: `wireguard site to site allowedips`.
- **nginx** — `nginx.org`, разделы «ngx_http_limit_req_module», «ngx_http_proxy_module», «TCP/UDP load balancing». Поиск: `nginx limit_req zone burst`.
- **HAProxy** — `docs.haproxy.org`, разделы «configuration manual», «health checks». Поиск: `haproxy http-check expect status`.
- **OWASP** — `owasp.org`, проект «ModSecurity Core Rule Set (CRS)», «OWASP Top 10». Поиск: `owasp crs paranoia level nginx modsecurity`.
- **Terraform-провайдеры** — `registry.terraform.io`, страницы провайдеров `yandex-cloud` и `hashicorp/aws`, ресурсы VPC/сетей. Поиск: `terraform registry yandex vpc route table`.
