# Балансировщики, прокси и CDN: HAProxy, Nginx, Traefik, SSL/TLS-терминация

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (ВМ WIN11-CL01)
> **Что написал:** «Открываю `https://web.vektor.lab` — сначала отдаёт страницу A, потом B, а иногда 503 Service Unavailable. И браузер ругается на сертификат. Мне для отчёта нужен один стабильный адрес и нормальный замок в адресной строке. **Срочность:** средняя».

За этой заявкой — три темы сразу: балансировка (почему A и B),健康-проверки (почему иногда 503) и TLS-терминация (почему «замок» не зелёный). В реальной работе это встречается постоянно: почти любой корпоративный сервис сегодня стоит либо за reverse proxy, либо за балансировщиком, либо за CDN — а чаще за всеми тремя. 1С, GitLab, Zabbix, Kubernetes Ingress, портал самообслуживания — всё это публикуется через что-то, что умеет держать 443-й порт, сертификат и правило «куда переслать».

После этой главы ты должен уметь: объяснить разницу между L4- и L7-балансировкой и выбрать режим; поднять HAProxy с TLS-терминацией и health-проверками; сделать то же самое на nginx как reverse proxy; понимать модель конфигурации Traefik (entrypoints → routers → services → middlewares); выпустить и положить сертификат правильно, а не «отключить проверку в браузере»; диагностировать 502/503/504 и понимать, где в цепочке они рождаются; знать, что такое CDN и какие тут ограничения в РФ.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Прокси** — посредник. **Forward proxy** стоит перед клиентами и ходит в интернет *за них* (типичный корпоративный интернет-шлюз). **Reverse proxy** стоит перед серверами и принимает запросы *от имени* сервиса: клиент думает, что говорит с сайтом, а на самом деле — с прокси. Балансировщик — это reverse proxy, у которого за спиной несколько одинаковых бэкендов.

**Уровни балансировки.** L4 (TCP/UDP) работает по IP:порт, не разбирая содержимое: быстро, дёшево, годится для любых протоколов (SMTP, базы, RDP), но не умеет маршрутизировать по URL и не понимает HTTP-ошибки. L7 (HTTP/HTTPS) разбирает запрос: заголовки, Host, path, cookie — умеет умную маршрутизацию, sticky-сессии, rewrite, health-check по эндпоинту. Цена — CPU и память.

**Модель HAProxy**: `frontend` (точка входа: bind, ACL, правила) → `backend` (пул серверов + алгоритм + проверки). `listen` — то и другое в одном блоке. Алгоритмы: `roundrobin`, `leastconn` (лучший для долгих соединений и БД), `source` (хеш по IP — грубая липкость), `uri`. Health-check: активный (`option httpchk`) и пассивный (наблюдение за реальными ответами, `observe`).

**Nginx** строится иначе: `http` → `server` (виртуальный хост) → `location` (путь) → `upstream` (пул бэкендов). Он же обычно отдаёт статику, поэтому на одном nginx часто висят и балансировка, и кэш, и раздача файлов.

**Traefik** — «облачный» прокси с динамической конфигурацией: провайдеры (Docker, file, Kubernetes CRD) сами сообщают ему, какие маршруты существуют, перезапуск не нужен. Абстракции: `entryPoint` (порт) → `router` (правило) → `middleware` (auth, redirect, headers) → `service` (пул).

**TLS-терминация** — расшифровка TLS на балансировщике: дальше до бэкенда трафик идёт открытым HTTP. Варианты: *passthrough* (балансировщик не расшифровывает, маршрутизация по SNI — только L4), *termination* (расшифровка на входе), *re-encryption* (расшифровал → проверил → снова зашифровал до бэкенда, нужен для compliance и Zero Trust). При терминации балансировщик обязан подставлять `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Real-IP`, иначе бэкенд видит только его IP, а редиректы уезжают на `http://`.

**Порты и протоколы:** 80/tcp HTTP, 443/tcp HTTPS (TLS 1.2/1.3), 8404 — статистика HAProxy (учебно), `PROXY protocol` — способ передать реальный IP клиента на L4. **SNI** (Server Name Indication) — имя хоста в открытом виде внутри ClientHello: по нему выбирается сертификат, и именно поэтому в дампе видно, куда шёл пользователь, даже без расшифровки.

```text
                 офис 10.10.10.0/24            DMZ 10.10.20.0/24
                        |                              |
                        | 443/tcp TLS                  |
                        v                              |
        +----------------------------------------+     |
        | GW01 (Debian 12)                       |     |
        | 10.10.10.3 / 10.10.20.1                |     |
        | nftables + HAProxy                     |     |
        |  fe_https :443  TLS-терминация         |     |
        |  be_web roundrobin, httpchk /healthz   |     |
        +------------------+---------------------+     |
                           | 80/tcp, без TLS           |
              +------------+------------+               |
              v                         v               v
    WEB01:8080 (backend A)     WEB01:8081 (backend B)
        10.10.20.10                 10.10.20.10
```

Важное упрощение стенда: оба бэкенда — это один WEB01 на двух портах. В жизни так делать нельзя (одна машина = одна точка отказа), зато в лабе видно и алгоритм, и health-check, и потерю одного «сервера».

**CDN** (Content Delivery Network) — сеть точек присутствия (PoP), которая кэширует статику ближе к пользователю и отдаёт её оттуда. Работает на DNS/anycast: пользователь попадает в ближайший PoP, PoP ищет в кэше по ключу (URL + Vary-заголовки), при промахе идёт на origin, при попадании отдаёт `X-Cache: HIT` и `Age`. Ключевые понятия: TTL, purge (сброс кэша), origin shield, подписанные ссылки для приватного контента, динамическое ускорение (маршрутизация до origin по оптимизированному пути). В РФ практически значимы Yandex Cloud CDN, Selectel, VK Cloud, DDoS-Guard; доступность Cloudflare из РФ нестабильна `[ПРОВЕРИТЬ]`. И помни про 152-ФЗ: если через CDN-провайдера с зарубежными PoP проходят персональные данные — это разговор с юристами, а не с сетевиком.

## 3. Аналогия из жизни

Балансировщик — это администратор на ресепшене бизнес-центра. Посетитель приходит на один адрес (домен) и один вход (443-й порт), а администратор решает, в какой офис его отправить: по очереди (roundrobin), к тому, кто меньше занят (leastconn), или всегда к «своему» менеджеру, если посетитель уже вёл дело (sticky-сессия по cookie). Если в офисе B потух свет, администратор перестаёт туда отправлять людей — это health-check; а посетитель получает вежливое «сейчас никто не принимает» — это 503.

TLS-терминация — это вахтёр, который вскрывает конверт с сургучной печатью на входе, читает адрес и передаёт записку внутри здания уже без печати. Удобно: внутри можно быстро разносить. Риск: всё, что внутри здания, больше не защищено печатью — поэтому в нормальной среде внутренние сегменты либо доверенные, либо записку запечатывают снова (re-encryption).

CDN в этой метафоре — сеть пунктов выдачи заказов: курьер не едет через полгорода, если коробка уже лежит в соседнем ПВЗ. Аналогия перестаёт работать в одном месте: вахтёр не проверяет криптографическую подпись отправителя, а TLS — это именно про доверие к серверу, а не про пропуск в здание.

## 4. Практика и команды

**1. Проверка конфигурации HAProxy перед перезагрузкой**

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
# Configuration file is valid
sudo haproxy -vv | head -20   # версия, поддержка SSL, лимиты
```
`-c` — только проверка синтаксиса, `-f` — файл конфигурации, `-vv` — расширенная информация о сборке. Смотреть: `HAProxy version` (в Debian 12 — ветка 2.6 `[ПРОВЕРИТЬ]`), наличие `Built with OpenSSL` (без него `ssl` в bind не заработает).

**2. Мягкая перезагрузка без обрыва соединений**

```bash
sudo systemctl reload haproxy
ps -eo pid,ppid,cmd | grep haproxy   # кратковременно два процесса: старый доживает
```
`reload` = «запусти новый процесс и передай ему сокеты, старый доработает текущие соединения» (`-sf`). `restart` рвёт активные сессии — в продакшене это инцидент.

**3. Runtime API через admin-сокет**

```bash
echo "show stat" | sudo socat stdio /var/run/haproxy/admin.sock
echo "show servers state be_web" | sudo socat stdio /var/run/haproxy/admin.sock
echo "set server be_web/web-b state maint" | sudo socat stdio /var/run/haproxy/admin.sock
```
Вывод `show stat` — CSV, десятки колонок. Смотреть: `status` (`UP`/`DOWN`/`MAINT`), `check_status` (`L7OK`, `L7STS`, `L4CON`), `chkfail` (сколько проверок провалено), `qcur`/`scur` (очередь и текущие сессии). Команда `set server ... state maint` — способ вывести бэкенд на обслуживание без правки конфига.

**4. Синтаксис и reload у nginx**

```bash
sudo nginx -t && sudo nginx -s reload
```
`-t` — тест конфига и проверка доступности файлов сертификатов, `-s reload` — плавный перезапуск воркеров. Никогда не `systemctl restart nginx` на боевом без причины.

**5. Reverse proxy на nginx (когда HAProxy не нужен)**

```nginx
upstream ve_web {
    server 10.10.20.10:8080 max_fails=3 fail_timeout=10s;
    server 10.10.20.10:8081 max_fails=3 fail_timeout=10s;
    keepalive 16;
}
server {
    listen 443 ssl;
    server_name web.vektor.lab;
    ssl_certificate     /etc/nginx/certs/web.crt;
    ssl_certificate_key /etc/nginx/certs/web.key;
    location / {
        proxy_pass http://ve_web;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```
`keepalive` в upstream — переиспользование соединений до бэкенда (иначе на каждый запрос новый TCP). `max_fails`/`fail_timeout` — пассивная проверка. `proxy_http_version 1.1` обязателен для WebSocket.

**6. Проверка TLS «как браузер», но из терминала**

```bash
curl -vk --resolve web.vektor.lab:443:10.10.20.1 https://web.vektor.lab/
```
`--resolve` подменяет DNS только для этого запроса (не нужно править `/etc/hosts`), `-v` показывает handshake: версию TLS, ALPN (`h2`), цепочку сертификатов, `-k` — игнор доверия (только для лабы!). В выводе смотреть `SSL certificate verify ok` против `self-signed certificate`.

**7. Разбор сертификата без браузера**

```bash
openssl s_client -connect 10.10.20.1:443 -servername web.vektor.lab -showcerts </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```
`-servername` включает SNI (без него сервер может отдать дефолтный сертификат). Смотреть: `subjectAltName` — там должны быть и `DNS:web.vektor.lab`, и `IP:10.10.20.1`, если по IP тоже ходят; `notAfter` — дата.

**8. Кто слушает и куда реально уходят соединения**

```bash
sudo ss -tlnp 'sport = :443'
sudo ss -tnp state established '( dport = :8080 or dport = :8081 )'
```
Первая — что процесс реально слушает 443 (частая причина «конфиг верный, а порт занят»). Вторая — распределение установленных сессий по бэкендам: сразу видно перекос.

**9. Видно ли SNI в трафике (важно для безопасности)**

```bash
sudo tcpdump -ni any -A 'tcp port 443' -c 20 | grep -a -m1 -oE 'web\.vektor\.lab'
```
Да, видно: имя хоста летит открытым текстом в ClientHello. Вывод: SNI-фильтрация работает у провайдера, но и ты не можешь спрятать имена сервисов от того, кто смотрит трафик. С ECH (Encrypted Client Hello) это меняется, но поддержка неполная `[ПРОВЕРИТЬ]`.

**10. Проверка с рабочей станции Windows**

```powershell
curl.exe -vk https://web.vektor.lab/ --resolve web.vektor.lab:443:10.10.20.1
Invoke-WebRequest -Uri https://web.vektor.lab/ -SkipCertificateCheck   # PowerShell 7+
certutil -addstore -f Root .\web.crt                                  # доверяем учебному CA
Invoke-WebRequest -Uri https://web.vektor.lab/                        # теперь без исключений
```
В Windows PowerShell 5.1 параметра `-SkipCertificateCheck` нет — используй `curl.exe` или добавь сертификат в `Cert:\LocalMachine\Root` (`Import-Certificate -FilePath .\web.crt -CertStoreLocation Cert:\LocalMachine\Root`). `curl` в PowerShell — алиас на `Invoke-WebRequest`, поэтому пиши именно `curl.exe`.

**11. Traefik: динамическая конфигурация файлом**

```bash
sudo apt install -y docker.io   # на WEB01, Ubuntu 24.04
sudo mkdir -p /etc/traefik
```
```yaml
# /etc/traefik/traefik.yml — статическая часть
entryPoints:
  web:
    address: ":8090"
providers:
  file:
    filename: /etc/traefik/dynamic.yml
```
```yaml
# /etc/traefik/dynamic.yml — динамическая часть
http:
  routers:
    ve-web:
      rule: "Host(`web.vektor.lab`)"
      entryPoints: [web]
      service: ve-pool
  services:
    ve-pool:
      loadBalancer:
        servers:
          - url: "http://10.10.20.10:8080"
          - url: "http://10.10.20.10:8081"
        healthCheck:
          path: /healthz
          intervalSeconds: 5
```
```bash
docker run -d --name traefik --restart unless-stopped \
  -p 8090:8090 -v /etc/traefik:/etc/traefik:ro traefik:v3.1   # тег образа [ПРОВЕРИТЬ]
curl -s -H "Host: web.vektor.lab" http://10.10.20.10:8090/
```
Ключевое отличие: Traefik читает `dynamic.yml` сам, без reload — правила меняются «на лету». Точные имена полей healthCheck сверяй в документации `[ПРОВЕРИТЬ]`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны ВМ, созданные на треке L2:
- **GW01** (Debian 12), адреса `10.10.10.3` и `10.10.20.1`, настроен как маршрутизатор с nftables (урок 41–43), интернет наружу есть.
- **WEB01** (Ubuntu Server, nginx), `10.10.20.10`, доступен с GW01.
- **WIN11-CL01** для проверки из браузера.

Если WEB01 или GW01 нет — вернись к урокам L2 про DMZ и NGFW. Минимальная настройка на месте: на WEB01 `sudo apt install -y nginx`, на GW01 `sudo apt install -y haproxy socat`. Проверь имена интерфейсов на GW01: `ip -br a` (обычно `ens33`/`ens37` — `[ПРОВЕРИТЬ]`).

### Снапшот

В VMware Workstation: для **GW01** и **WEB01** — `VM → Snapshot → Take Snapshot`, имя `before-sysadmin-045`. Для WIN11-CL01 достаточно имени `before-sysadmin-045` тоже, если планируешь импорт сертификата в хранилище.

### Шаги

1. **WEB01.** Создать два «бэкенда» и health-эндпоинт:
   ```bash
   sudo tee /etc/nginx/sites-available/backends.conf >/dev/null <<'EOF'
   server {
       listen 8080;
       server_name web.vektor.lab;
       default_type text/plain;
       location = /healthz { return 200 "ok\n"; }
       location / {
           add_header X-Backend "A" always;
           return 200 "backend A, client=$remote_addr, xff=$http_x_forwarded_for\n";
       }
   }
   server {
       listen 8081;
       server_name web.vektor.lab;
       default_type text/plain;
       location = /healthz { return 200 "ok\n"; }
       location / {
           add_header X-Backend "B" always;
           return 200 "backend B, client=$remote_addr, xff=$http_x_forwarded_for\n";
       }
   }
   EOF
   sudo ln -sf /etc/nginx/sites-available/backends.conf /etc/nginx/sites-enabled/backends.conf
   sudo nginx -t && sudo nginx -s reload
   curl -s http://127.0.0.1:8080/ ; curl -s http://127.0.0.1:8081/
   ```
   *Ожидаемо:* `backend A...` и `backend B...`.

2. **GW01.** Установить и остановить HAProxy, чтобы подготовить конфиг:
   ```bash
   sudo apt update && sudo apt install -y haproxy socat
   sudo systemctl stop haproxy
   haproxy -v | head -2
   ```
   *Ожидаемо:* строка версии HAProxy 2.6.x `[ПРОВЕРИТЬ]`.

3. **GW01.** Сгенерировать учебный сертификат с SAN:
   ```bash
   sudo mkdir -p /etc/haproxy/certs && cd /etc/haproxy/certs
   sudo openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
     -keyout web.key -out web.crt \
     -subj "/C=RU/O=Vektor Lab/CN=web.vektor.lab" \
     -addext "subjectAltName=DNS:web.vektor.lab,IP:10.10.20.1"
   sudo cat web.crt web.key | sudo tee web.pem >/dev/null
   sudo chmod 600 web.pem web.key
   sudo openssl x509 -in web.crt -noout -ext subjectAltName
   ```
   *Ожидаемо:* в SAN видны `DNS:web.vektor.lab, IP:10.10.20.1`.

4. **GW01.** Написать конфиг HAProxy (положить в `/etc/haproxy/haproxy.cfg`, старый сохранить как `haproxy.cfg.dist`):
   ```haproxy
   global
       log /dev/log local0
       maxconn 2000
       stats socket /var/run/haproxy/admin.sock mode 660 level admin
       stats timeout 30s

   defaults
       log     global
       mode    http
       option  httplog
       option  forwardfor
       timeout connect 5s
       timeout client  50s
       timeout server  50s

   frontend fe_http
       bind :80
       http-request redirect scheme https code 301

   frontend fe_https
       bind :443 ssl crt /etc/haproxy/certs/web.pem alpn h2,http/1.1
       http-request set-header X-Forwarded-Proto https
       default_backend be_web

   backend be_web
       balance roundrobin
       option httpchk GET /healthz
       http-check expect status 200
       server web-a 10.10.20.10:8080 check
       server web-b 10.10.20.10:8081 check

   listen stats
       bind 10.10.10.3:8404
       stats enable
       stats uri /stats
       stats refresh 5s
       stats auth admin:Lab#2026-vektor
   ```
   *Ожидаемо:* файл на месте, `crt` — это cert+key в одном файле (HAProxy иначе не стартует).

5. **GW01.** Проверить и запустить:
   ```bash
   sudo haproxy -c -f /etc/haproxy/haproxy.cfg
   sudo systemctl enable --now haproxy
   sudo ss -tlnp 'sport = :443 or sport = :8404'
   sudo journalctl -u haproxy -n 20 --no-pager
   ```
   *Ожидаемо:* `Configuration file is valid`, в `ss` — слушающие сокеты, в логе нет `ALERT`.

6. **GW01.** Если nftables (урок 43) режет входящий трафик — добавить разрешение и сохранить правила:
   ```bash
   sudo nft list ruleset | head -40
   sudo nft insert rule inet filter input tcp dport { 80, 443, 8404 } accept   # [ПРОВЕРИТЬ имя таблицы/цепочки]
   ```
   *Ожидаемо:* правила добавлены, `curl` с WIN11-CL01 проходит. В боевой среде так «в лоб» не делают — правило пишется в конфиг и проходит ревью.

7. **WIN11-CL01.** Проверить балансировку и TLS:
   ```powershell
   curl.exe -k --resolve web.vektor.lab:443:10.10.10.3 https://web.vektor.lab/
   curl.exe -k --resolve web.vektor.lab:443:10.10.10.3 https://web.vektor.lab/
   curl.exe -k -I --resolve web.vektor.lab:443:10.10.10.3 https://web.vektor.lab/
   ```
   *Ожидаемо:* первый ответ `backend A`, второй `backend B` (roundrobin). Проверь `X-Forwarded-For` — там должен быть твой IP `10.10.10.x`.

8. **GW01.** Посмотреть статистику и состояние серверов:
   ```bash
   echo "show stat" | sudo socat stdio /var/run/haproxy/admin.sock | cut -d, -f1,2,18,19,33 | head -5
   ```
   *Ожидаемо:* для `be_web` оба сервера `UP`, `check_status` = `L7OK`. И в браузере на WIN11-CL01: `http://10.10.10.3:8404/stats` (логин `admin`).

9. **WEB01.** Сымитировать падение бэкенда B: временно убрать блок `listen 8081` (или закомментировать) и перезагрузить nginx. Через ~10–15 секунд снова дёрнуть `curl` с WIN11-CL01 пять раз.
   *Ожидаемо:* все ответы — `backend A`, в статистике `web-b` в состоянии `DOWN`, `chkfail` растёт. Пользователь не видит ошибок — это и есть смысл health-check.

10. **GW01.** Проверить редирект с HTTP на HTTPS:
    ```bash
    curl -sI http://10.10.20.1/ | head -3
    ```
    *Ожидаемо:* `HTTP/1.1 301` и `Location: https://10.10.20.1/`.

11. **WEB01.** Опционально — Traefik на порту 8090 по конфигам из раздела 4 (пункт 11). Проверить: `curl -s -H "Host: web.vektor.lab" http://10.10.20.10:8090/` должен дать `backend A`/`backend B`.

### Проверка

- [ ] `haproxy -c` говорит `valid`, `systemctl status haproxy` — `active (running)`.
- [ ] `ss -tlnp` на GW01 показывает 443 и 8404.
- [ ] Пять последовательных запросов дают чередование A/B (примерно 3/2).
- [ ] `curl -vk` показывает успешный TLS-handshake, ALPN `h2`, сертификат с нужным SAN.
- [ ] В ответе бэкенда виден реальный IP клиента из `X-Forwarded-For`, а не `10.10.20.1`.
- [ ] На `/stats` оба сервера `UP`, `check_status` = `L7OK`.
- [ ] После отключения 8081 бэкенд `web-b` уходит в `DOWN`, ответы продолжаются, ошибок для пользователя нет.
- [ ] HTTP на 80-м порту отдаёт 301 на HTTPS.
- [ ] В `stand-log.md` записаны изменения.

### Сломай и почини

> **Пользователь:** petrova (ВМ WIN10-CL02)
> **Что написал:** «Открываю `https://web.vektor.lab` — **503 Service Unavailable**, у всего отдела то же самое. Раньше работало. **Срочность:** высокая».

**Воспроизведение поломки.** На GW01 в бэкенде заменить путь проверки на несуществующий:

```bash
sudo sed -i 's#option httpchk GET /healthz#option httpchk GET /health#' /etc/haproxy/haproxy.cfg
sudo systemctl reload haproxy
curl -sk --resolve web.vektor.lab:443:10.10.20.1 https://web.vektor.lab/ -o /dev/null -w "%{http_code}\n"
# 503
```

**Диагностика по порядку:**

1. С фронта: `curl -sk -o /dev/null -w "%{http_code}\n" https://web.vektor.lab/` → 503. Значит, HAProxy жив, но бэкендов нет.
2. Смотрим статистику: `echo "show stat" | sudo socat stdio /var/run/haproxy/admin.sock | cut -d, -f1,2,18,19,33` → оба сервера `DOWN`, `check_status` = `L7STS`.
3. Проверяем бэкенд напрямую с GW01: `curl -sv http://10.10.20.10:8080/health` → nginx отвечает **404**. Вот и причина: проверка ходит не туда.
4. Подтверждаем на WEB01 в `access.log`: видим поток `GET /health` с кодом 404 раз в 2 секунды — это HAProxy.
5. Проверяем, что сам сервис жив: `curl -s http://10.10.20.10:8080/healthz` → `ok`, `curl -s http://10.10.20.10:8081/healthz` → `ok`.

**Решение.** Вернуть правильный путь проверки (или добавить `location = /health { return 200 "ok\n"; }` на бэкенде, если менять LB нельзя):

```bash
sudo sed -i 's#option httpchk GET /health#option httpchk GET /healthz#' /etc/haproxy/haproxy.cfg
sudo haproxy -c -f /etc/haproxy/haproxy.cfg && sudo systemctl reload haproxy
curl -sk --resolve web.vektor.lab:443:10.10.20.1 https://web.vektor.lab/
```

**Ответ пользователю:** «Проблема была на стороне балансировщика: проверка доступности сервиса ходила на неверный адрес, и он считал оба сервера нерабочими. Исправил, проверил с трёх рабочих мест — сайт открывается, ошибок нет. Если увидите 503 снова — сразу пишите, у нас есть страница статуса, посмотрю за минуту».

**Закрытие заявки:** категория «веб-сервис», решение «некорректный health-check path → исправлен конфиг HAProxy, reload без простоя», добавлен пункт в регламент: «после изменения конфигурации бэкенда согласовать путь health-эндпоинта с владельцем сервиса».

### Что изменилось на стенде

- **GW01 (`10.10.10.3` / `10.10.20.1`)**: установлен `haproxy` и `socat`; HAProxy слушает 80 (redirect), 443 (TLS-терминация) и 8404 (stats, basic-auth `admin`); admin-сокет `/var/run/haproxy/admin.sock`; сертификат `/etc/haproxy/certs/web.{crt,key,pem}` (CN `web.vektor.lab`, SAN `DNS:web.vektor.lab, IP:10.10.20.1`); возможно, добавлено правило nftables для 80/443/8404.
- **WEB01 (`10.10.20.10`)**: nginx дополнительно слушает 8080 и 8081 (`/etc/nginx/sites-available/backends.conf`), эндпоинт `/healthz` на обоих портах; опционально — Docker и контейнер Traefik на 8090 с конфигами `/etc/traefik/traefik.yml` и `dynamic.yml`.
- **WIN11-CL01**: (если делал) учебный сертификат `web.crt` импортирован в `LocalMachine\Root`.
- **Новый сервис стенда**: `https://web.vektor.lab` → GW01:443 → WEB01:8080/8081. На него можно ссылаться в следующих главах (мониторинг 48-го урока, сегментация 47-го).

### Откат

VMware Workstation → выбрать ВМ (GW01 и/или WEB01) → `VM → Snapshot → Snapshot Manager` → выделить `before-sysadmin-045` → **Restore**. Если нужно сохранить текущее состояние (например, Traefik уже настроен и хочется оставить), сделай сначала новый снапшот `after-sysadmin-045`, а откатывайся только частично. После откатов обнови `stand-log.md`: запиши, что именно откатили и почему.

## 6. Ошибки новичков и безопасность

1. **`systemctl restart` вместо `reload`.** → Обрыв всех активных сессий, всплеск ошибок у пользователей. → Проверяй конфиг `haproxy -c` / `nginx -t`, затем `reload`.
2. **Health-check на главную страницу.** → Главная тянет БД и внешние API: притормозила база — балансировщик выкидывает все бэкенды, получается каскадный отказ. → Отдельный лёгкий эндпоинт `/healthz` без зависимостей, с кодом 200 и коротким таймаутом.
3. **Доверие `X-Forwarded-For` от кого угодно.** → Пользователь подделывает IP в заголовке, аудит-логи врут, IP-based ACL обходятся. → Заголовок ставит только балансировщик; на бэкенде `set_real_ip_from <IP LB>` и `real_ip_header X-Forwarded-For` (nginx), остальные источники игнорировать.
4. **Нет таймаутов.** → «Зависший» бэкенд держит соединения и воркеры бесконечно; в логе HAProxy предупреждение о незаданных таймаутах `[ПРОВЕРИТЬ точные дефолты для версии]`. → Явно задавать `timeout connect/client/server` и понимать разницу между 502 (бэкенд ответил мусором/закрыл соединение), 503 (нет живых серверов) и 504 (не успел ответить).
5. **Sticky-сессии «на всякий случай».** → Неравномерная нагрузка, «толстый» пользователь кладёт свой бэкенд, при падении сервера он теряет корзину. → Липкость только если приложение реально хранит состояние в памяти; правильнее — общий session store (Redis/БД).
6. **Статистика LB открыта всем.** → `/stats` без пароля сливает топологию, имена серверов и нагрузку. → `stats auth`, bind на loopback/VPN, ACL по источнику; в боевой среде — вообще отдельный management-интерфейс.
7. **Самоподписанный сертификат в проде / «нажмите продолжить».** → Пользователи приучаются игнорировать ошибки TLS, это прямой путь к MITM. → Внутренний CA или ACME, срок и автопродление под мониторингом, HSTS после проверки.
8. **CDN кэширует всё подряд.** → В кэш попадают личные кабинеты и `Set-Cookie`, данные одного клиента видит другой. → `Cache-Control: private/no-store` на приватных ответах, аккуратный cache key, проверка `X-Cache`/`Age` после релиза.

### Безопасность

В боевой среде: только TLS 1.2+/1.3 (TLS 1.0/1.1 выключены), отключены слабые шифры и SSLv3, включён OCSP stapling, приватные ключи лежат с правами `600` и не в Git; ключ от публичного сервиса — на отдельном хосте, не на том же, где крутится всё остальное. Балансировщик — точка, через которую проходит весь трафик: на нём уместны rate limiting, базовые WAF-правила и логирование, но помни, что терминация TLS означает «здесь трафик расшифрован» — доступ к этой машине должен быть максимально узким. Никогда не отключай проверку сертификата «чтобы работало» и не храни учебные пароли вида `Lab#2026-vektor` в конфигах продакшена (здесь это допустимо только потому, что стенд изолирован).

## 7. Вопросы на собеседовании

**1. Чем L4-балансировка отличается от L7 и когда что выбирать?**

*Ответ:* L4 работает с TCP/UDP-потоком: решение принимается по IP и порту, содержимое не разбирается — минимальная задержка, любой протокол, но нет маршрутизации по URL/Host, нет умных health-check по HTTP и нет sticky по cookie. L7 разбирает HTTP: маршрутизация по Host/path/заголовкам, rewrite, проверки по эндпоинту, но выше CPU и требования к сертификатам. L4 — для баз данных, не-HTTP протоколов, экстремальной нагрузки; L7 — для веб-сервисов, микросервисов, Kubernetes Ingress.

*Что хочет услышать интервьюер:* что кандидат понимает цену L7 и не тащит его туда, где он не нужен, и помнит, что при L4 реальный IP клиента передаётся только через PROXY protocol.

*Красный флаг:* «L7 всегда лучше, потому что умнее».

**2. Терминация, passthrough и re-encryption — в чём разница и что выберешь?**

*Ответ:* **Терминация** — TLS заканчивается на балансировщике, до бэкенда идёт HTTP: удобно (один сертификат, кэш, WAF), но внутри сети трафик открыт. **Passthrough** — балансировщик не расшифровывает, маршрутизация по SNI, только L4: сертификат на бэкенде, нет L7-функций. **Re-encryption** — балансировщик расшифровал, применил политику и зашифровал заново до бэкенда: нужно там, где внутренняя сеть не считается доверенной (Zero Trust, требования аудита). Часто используют mTLS между LB и бэкендом.

*Что хочет услышать интервьюер:* понимание, что терминация — это компромисс между удобством и моделью угроз, а не «так проще».

*Красный флаг:* «Всегда терминация, внутри же всё равно своя сеть».

**3. Как балансировщик понимает, что бэкенд жив, и как не устроить каскадный отказ?**

*Ответ:* активные проверки (HAProxy `option httpchk` + `http-check expect status 200`, nginx `max_fails`/`fail_timeout`, Traefik healthCheck) и пассивные — по реальным ответам. Порог: `rise`/`fall` (сколько успехов подряд для UP и сколько провалов для DOWN), интервал, таймаут. Каскадный отказ получается, когда health-check зависит от общих ресурсов (БД, внешний API): падает общая зависимость — LB выкидывает все бэкенды разом. Поэтому проверка должна быть локальной и дешёвой, а `fall` — не с единицы, чтобы не дёргаться на единичной ошибке.

*Что хочет услышать интервьюер:* что кандидат видел, как «правильные» health-check валят весь кластер.

*Красный флаг:* «Проверяю главную страницу, она же показательнее».

**4. Пользователь жалуется на 502. Где искать?**

*Ответ:* 502 — балансировщик получил невалидный ответ от бэкенда или не смог установить соединение. Порядок: (1) логи LB (`option httplog`, `journalctl -u haproxy`, nginx `error.log` — там конкретная причина: connection refused, reset, invalid header); (2) прямой запрос к бэкенду с самого LB (`curl -v http://backend:port/`) — исключаем сеть и приложение; (3) если приложение живо, но отвечает медленно — это уже 504, смотрим таймауты и время ответа; (4) если бэкенд закрывает соединение — часто keepalive-несоответствие (`proxy_http_version 1.1` и `Connection` заголовки); (5) проверяем, не режет ли что-то размер заголовка или `Host`.

*Что хочет услышать интервьюер:* системный порядок «от LB вниз по цепочке», а не «перезапустим всё».

*Красный флаг:* «Перезапущу nginx, обычно помогает».

**5. Как сделать отказоустойчивым сам балансировщик?**

*Ответ:* один LB — единая точка отказа. Варианты: два узла с плавающим IP через VRRP/keepalived (актив-пассив, самый частый в корпоративной сети), DNS round-robin или GeoDNS (актив-актив, но зависит от TTL и кэшей клиентов), anycast на уровне провайдера, аппаратные LB с HA-парой. Плюс отдельный вопрос — синхронизация конфигурации и сертификатов между узлами (Ansible, общий репозиторий) и проверка, что при переключении сессии не рвутся или рвутся предсказуемо.

*Что хочет услышать интервьюер:* что кандидат думает про «кто балансирует балансировщик» и про TTL DNS как фактор времени переключения.

*Красный флаг:* «Поставлю два HAProxy за DNS, всё само разрулится» — без понимания TTL и здоровья.

## 8. Источники (официальная документация)

- **haproxy.org** — раздел *Configuration Manual* (frontend/backend, `option httpchk`, `ssl`, `stats socket`) и *Management Guide*. Поиск: `haproxy configuration manual httpchk`.
- **nginx.org** — раздел *Documentation → ngx_http_upstream_module* и *ngx_http_proxy_module*, а также статья *NGINX Reverse Proxy*. Поиск: `nginx reverse proxy guide proxy_set_header X-Forwarded-Proto`.
- **doc.traefik.io** — разделы *Routing & Load Balancing* (routers, services, middlewares) и *Providers → File*. Поиск: `traefik v3 file provider routers services healthcheck`.
- **openssl.org** — *Documents → man pages* для `openssl req`, `openssl s_client`, `openssl x509`. Поиск: `openssl req addext subjectAltName`.
- **learn.microsoft.com** — разделы про IIS Application Request Routing (ARR) и *Certificate Stores* в Windows. Поиск: `IIS ARR reverse proxy server farm`.
- **RFC 8446** (TLS 1.3) на **rfc-editor.org** — для понимания handshake и SNI/ALPN. Поиск: `rfc 8446 tls 1.3`.
- **MDN Web Docs (developer.mozilla.org)** — статьи *X-Forwarded-For*, *HTTP caching*, *Cache-Control*. Поиск: `mdn x-forwarded-for cache-control`.
- Провайдеры CDN в РФ для практики: **yandex.cloud** (Yandex Cloud CDN), **selectel.ru**, **vk.cloud**, **ddos-guard.net** — разделы «CDN / кэширование / purge». Точные названия тарифов и функций меняются — сверяй на сайте `[ПРОВЕРИТЬ]` перед проектированием.

Если какой-то пункт меню или параметр в интерфейсе провайдера/панели отличается от описанного — доверяй официальной документации на момент чтения, а расхождение фиксируй в `stand-log.md`.
