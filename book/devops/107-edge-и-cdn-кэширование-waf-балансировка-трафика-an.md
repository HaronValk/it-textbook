# Edge и CDN: кэширование, WAF, балансировка трафика, Anycast, DDoS-защита

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №4471**
- **Пользователь:** ivanov (бухгалтерия)
- **ВМ:** WIN11-CL01
- **Что написал:** «Открываю shop.vektor.lab — цены вчерашние, у Петровой сегодняшние. Страница грузится секунды четыре. И вчера вечером минут двадцать сайт вообще не открывался, а в браузере висело "слишком много перенаправлений"».
- **Срочность:** средняя

Дальше выясняется классический набор: часть контента залипла в кэше на «границе» сети, часть пользователей ходит на другой узел, а ночью кто-то заливал форму поиска тысячами запросов.

Edge (граница сети) — это то, что стоит между пользователем и вашим приложением: кэш, TLS-терминация, WAF, лимиты, балансировка. CDN — это чужая (или своя) сеть таких edge-узлов, разбросанная по городам и странам. В работе DevOps это встречается постоянно: почти любой публичный сервис сегодня отдаётся через CDN или через собственный edge-слой (nginx, Envoy, Cloudflare, Yandex Cloud CDN). После главы вы будете уметь: настроить кэширующий edge на nginx, управлять временем жизни кэша и его инвалидацией, поставить простейший WAF-фильтр и rate limit, объяснить, чем L4-балансировщик отличается от L7, и внятно рассказать, как Anycast помогает переживать DDoS — и где он не помогает.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Путь запроса в реальном сервисе выглядит так:

```text
 Браузер
   │  HTTPS 443 (HTTP/2, HTTP/3 = QUIC/UDP 443)
   │  DNS: shop.vektor.lab → ближайший PoP (Anycast / GeoDNS)
   ▼
 [ EDGE / PoP CDN ]  ← TLS-терминация, кэш, WAF, rate-limit, bot-фильтр
   │  HTTP 80/8080, X-Forwarded-For, keepalive
   ▼
 [ SHIELD / mid-tier ] (опционально: второй слой кэша, чтобы не ронять origin)
   ▼
 [ ORIGIN: ваше приложение за балансировщиком ]
```

**Кэширование.** Кэш бывает в браузере, на edge и на промежуточном слое. Управляют им HTTP-заголовки: `Cache-Control: public, max-age=600, s-maxage=3600, stale-while-revalidate=60` (`max-age` — для браузера, `s-maxage` — для shared-кэшей вроде CDN), `ETag`/`If-None-Match` и `Last-Modified` для валидации (ответ 304 без тела), `Age` — сколько секунд объект уже лежит в кэше, `Vary` — по каким заголовкам различать варианты. Ключ кэша — то, чем «именуется» объект: обычно `схема + метод + Host + URI (+ Vary-заголовки)`. Главная метрика — **hit ratio** (доля попаданий); 85–95 % для статики — норма, для API всё индивидуально. Инвалидация — самое больное место: либо короткий TTL, либо версионирование URL (`app.v42.js`), либо явный purge по ключу/тегу.

**WAF** (Web Application Firewall) — фильтр HTTP-запросов. Два подхода: негативная модель (сигнатуры известных атак, OWASP CRS) и позитивная (allowlist разрешённых параметров). Плюс поведенческие правила: rate-based rules, «запрос без браузерных заголовков», JS-challenge, CAPTCHA. Правило простое: edge — самое дешёвое место, где можно отбросить мусор, он не доходит до вашего приложения.

**Балансировка.** L4 (TCP/UDP) смотрит только на IP/порт: быстро, не понимает HTTP, умеет DSR. L7 (HTTP) видит пути, заголовки, куки: умеет health-check по `/healthz`, retry, sticky-сессии, маршрутизацию по Host. Алгоритмы: round-robin, least_conn, `ip_hash`, `hash $request_uri` (даёт локальность кэша), EWMA. **Пассивные** проверки (`max_fails`/`fail_timeout`) и **активные** (периодический запрос `/healthz`) — не одно и то же.

**Anycast.** Один и тот же IP-префикс (например, /24) анонсируется по BGP (TCP/179) из десятков точек мира. Маршрутизаторы интернета сами приводят клиента к ближайшей анонсирующей точке. Свойства: минимальная задержка, автоматический уход от сбоя (точка сняла анонс — трафик поехал в соседнюю), атака размазывается по всем PoP. Ограничения: TCP-сессия привязана к одной точке (посреди соединения «переехать» нельзя), обратный путь может идти асимметрично, состояние (кэш, сессии) надо синхронизировать между PoP. Anycast ≠ магия: если ваш origin открыт по IP, атакующий пойдёт мимо CDN.

**DDoS-классы.** Объёмные (L3/L4: UDP-флуд, amplification через DNS/NTP, SYN-флуд — миллионы пакетов в секунду), протокольные (исчерпание conntrack, медленные соединения — slowloris), прикладные (L7: HTTP-флуд «как настоящий браузер», перебор логинов). Защита многослойная: RTBH (Remotely Triggered Black Hole — «чёрная дыра» через BGP-community, когда лучше потерять сервис, чем канал), FlowSpec (RFC 5575 — передать фильтр провайдеру по BGP), scrubbing-центры, лимиты на edge, challenge, автоскейл, договор с провайдером.

## 3. Аналогия из жизни

Сеть пиццерий. **CDN-узел** — это мини-кухня в вашем районе: тесто и начинку (статика) привезли заранее, поэтому пицца едет 10 минут, а не 2 часа из головного цеха (origin). **TTL** — срок годности заготовки: истёк — пекарь идёт за свежей. **Инвалидация** — менеджер позвонил и сказал «рецепт сменился, старые заготовки в мусор». **WAF** — охранник на входе, который не пускает людей с плакатами «у меня 4000 заказов». **Балансировка** — диспетчер, который распределяет заказы между поварами и перестаёт звать того, кто уронил противень. **Anycast** — вы звоните по одному общему номеру, а телефонная станция соединяет вас с ближайшей пиццерией; **DDoS** — тысяча человек одновременно звонят и молчат в трубку, и спасает только то, что колл-центров много и у них есть фильтр ненужных звонков.

Где аналогия ломается: пиццу можно отдать клиенту из любой кухни, а TCP-соединение посреди разговора перевести на другую точку Anycast нельзя — состояние сессии останется на старой.

## 4. Практика и команды

**1. Проверить, что вообще умеет ваш nginx**

```bash
nginx -v
nginx -V 2>&1 | tr ' ' '\n' | grep -E 'http_v2|realip|stub_status|modules-path'
```

`-V` показывает флаги сборки. Смотрим наличие `--with-http_realip_module` (нужен, чтобы видеть реальный IP клиента за прокси) и `--with-http_v2_module`.

**2. Проверить и «распечатать» конфиг целиком**

```bash
sudo nginx -t          # синтаксис
sudo nginx -T | less   # весь эффективный конфиг со всеми include
```

`-t` — обязательный шаг перед каждым `reload`. `-T` спасает, когда непонятно, какой `conf.d/*.conf` победил.

**3. Включить кэширующий reverse-proxy**

```nginx
# /etc/nginx/conf.d/edge-shop.conf  (в http-контексте через include)
proxy_cache_path /var/cache/nginx/edge levels=1:2 keys_zone=edge_cache:20m
                 max_size=2g inactive=60m use_temp_path=off;

upstream shop_backend {
    server 10.10.20.10:8080 max_fails=3 fail_timeout=10s;
    keepalive 16;
}

server {
    listen 80;
    server_name shop.vektor.lab;

    location / {
        proxy_pass http://shop_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        proxy_cache edge_cache;
        proxy_cache_key "$scheme$request_method$host$request_uri";
        proxy_cache_valid 200 301 302 10m;
        proxy_cache_valid 404 1m;
        proxy_cache_lock on;
        proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;

        add_header X-Cache-Status $upstream_cache_status always;
        add_header X-Edge-Node edge1 always;
    }
}
```

Ключевое: `keys_zone` — общая память под метаданные, `inactive` — когда удалять невостребованное, `proxy_cache_lock` — не пускать 500 запросов одновременно за одним и тем же объектом (защита origin при «холодном» кэше).

**4. Увидеть попадание/промах**

```bash
curl -sI -H 'Host: shop.vektor.lab' http://10.10.10.30/
curl -sI -H 'Host: shop.vektor.lab' http://10.10.10.30/
```

```text
HTTP/1.1 200 OK
X-Cache-Status: MISS
Age: 0
---
HTTP/1.1 200 OK
X-Cache-Status: HIT
Age: 7
```

`$upstream_cache_status` даёт `MISS`, `HIT`, `EXPIRED`, `STALE`, `UPDATING`, `REVALIDATED`, `BYPASS`. Растущий `Age` — признак, что объект отдаётся из кэша.

**5. Обойти кэш для проверки origin**

```bash
curl -sI -H 'Host: shop.vektor.lab' -H 'Cache-Control: no-cache' http://10.10.10.30/
```

Работает, если в конфиге есть `proxy_cache_bypass $http_cache_control;`. Так вы отделяете «origin отдаёт старое» от «edge отдаёт старое».

**6. Сбросить кэш (инвалидация)**

```bash
sudo rm -rf /var/cache/nginx/edge/*
sudo nginx -s reload
```

В open-source nginx директивы `proxy_cache_purge` нет — она в коммерческих сборках и сторонних модулях `[ПРОВЕРИТЬ для вашей сборки]`. Поэтому в продакшене инвалидацию делают версионированием URL или коротким `s-maxage`, а не `rm -rf` на каждой ноде.

**7. Лимиты запросов и соединений**

```nginx
limit_req_zone  $binary_remote_addr zone=req_per_ip:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=conn_per_ip:10m;

server {
    limit_req  zone=req_per_ip burst=20 nodelay;
    limit_conn conn_per_ip 20;
    limit_req_status 429;
    limit_conn_status 429;
}
```

`rate=10r/s` — средняя скорость, `burst` — очередь всплеска, `nodelay` — не придерживать всплеск. В логах увидите `limiting requests, excess: 1.234 by zone "req_per_ip"`.

**8. Мини-WAF на `map` (без внешних модулей)**

```nginx
map $request_uri $waf_bad {
    default 0;
    ~*(union\s+select|<script|\.\./\.\./|etc/passwd) 1;
}

server {
    if ($waf_bad) { return 403; }
}
```

Это грубая сигнатура, а не WAF: она ловит явные попытки и показывает механику. Промышленный вариант — ModSecurity v3 / OWASP CRS, Coraza WAF или правила вашего CDN-провайдера. С nginx связка ModSecurity собирается отдельно и капризна `[ПРОВЕРИТЬ актуальное состояние сборки]`.

**9. Реальный IP клиента за прокси**

```nginx
set_real_ip_from 10.10.10.0/24;
real_ip_header X-Forwarded-For;
real_ip_recursive on;
```

Без этого все лимиты считаются по IP балансировщика — и вы заблокируете весь офис разом.

**10. Нагрузочный тест и статус**

```bash
ab -n 500 -c 50 -H 'Host: shop.vektor.lab' http://10.10.10.30/   # apache2-utils
curl -s http://127.0.0.1/nginx_status                            # если включён stub_status
ss -s                                                            # сводка по сокетам
```

```text
Complete requests: 500
Failed requests:  47      ← это наши 429 от limit_req
Non-2xx responses: 47
Requests per second: 155.66 [#/sec] (mean)
```

**11. Отсечка флуда на самом edge (nftables)**

```bash
sudo sysctl -w net.ipv4.tcp_syncookies=1
sudo nft add table inet edgeguard
sudo nft 'add chain inet edgeguard input { type filter hook input priority 0; policy accept; }'
sudo nft add rule inet edgeguard input tcp dport { 80, 443 } ct state new \
     limit rate over 200/second burst 400 packets counter drop
sudo nft list ruleset
```

`counter` нужен, чтобы видеть, сколько пакетов реально попало под правило. В бою такое ставят на edge-ноде и/или отдают провайдеру через FlowSpec.

**12. Проверить, на какую именно ноду вы попали**

```bash
dig +noall +answer shop.vektor.lab A          # несколько A-записей = простейшая балансировка
curl -sI --resolve shop.vektor.lab:80:10.10.10.62 http://shop.vektor.lab/
```

`--resolve` жёстко прибивает домен к конкретному IP — незаменимо при отладке нескольких edge-нод.

**13. Конфиг — в Git**

```bash
git add nginx/edge-shop.conf
git commit -m "edge: cache zone, rate limits, waf-lite"
git push
```

Edge-конфиг — такой же код. Изменения только через ревью и пайплайн, никаких правок «по SSH в проде».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **WEB01** (10.10.20.10, DMZ) — nginx работает; порт 8080 свободен (новый vhost).
- **UBU01** (10.10.10.30) — станет **edge1**, есть sudo, доступ в интернет/репозитории.
- **k8s-w01** (10.10.10.62) — временно станет **edge2**. Это учебный компромисс: в нормальной среде edge-ноды — отдельные ВМ или управляемые PoP, а не worker-нода кластера.
- **GW01** (10.10.10.3 / 10.10.20.1) — маршрутизация между VMnet8 и VMnet10 работает. Если GW01 нет — сначала пройдите тему L2 про маршрутизатор, без него edge не достанет origin.
- **k8s-w02** (10.10.10.63) — генератор нагрузки.
- **GIT01** (10.10.10.50) — репозиторий для конфигов.
- Windows-часть стенда может отсутствовать — лабораторная полностью на Linux.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-107` для **UBU01, k8s-w01, WEB01, GW01**. Одной строкой в терминале (если установлен `vmrun`):

```bash
vmrun -T ws snapshot "C:/VMs/UBU01/UBU01.vmx" before-devops-107
```

### Шаги

1. **UBU01 → проверка связности с DMZ.**
   `ip route get 10.10.20.10` должен показать `via 10.10.10.3`, а `curl -sI http://10.10.20.10/` — `200 OK` от nginx WEB01. Если 502/таймаут — проблема в GW01 или nftables, а не в кэше.

2. **WEB01 → поднять origin-приложение.**

   ```bash
   sudo mkdir -p /var/www/shop/api
   echo '<h1>Vektor Shop</h1><p>static v1</p>' | sudo tee /var/www/shop/index.html
   sudo tee /etc/nginx/sites-available/shop.conf >/dev/null <<'EOF'
   server {
       listen 8080;
       root /var/www/shop;
       add_header Cache-Control "public, max-age=60, s-maxage=60" always;
       add_header X-Origin-Version "v1" always;
       location / { try_files $uri $uri/ =404; }
       location /api/price {
           default_type application/json;
           add_header Cache-Control "public, s-maxage=60" always;
           return 200 '{"sku":"A-1","price":1490,"ts":"$time_iso8601"}';
       }
   }
   EOF
   sudo ln -sf /etc/nginx/sites-available/shop.conf /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   curl -sI http://127.0.0.1:8080/
   ```

   Ожидаем `200` и заголовки `Cache-Control`/`X-Origin-Version`.

3. **UBU01 → поставить nginx и включить edge-конфиг** из раздела 4 (пункт 3) в `/etc/nginx/conf.d/edge-shop.conf`. `sudo nginx -t` → `syntax is ok`, затем `sudo systemctl reload nginx`.

4. **Первый промах и попадание.** Дважды `curl -sI -H 'Host: shop.vektor.lab' http://10.10.10.30/`. Ожидаем `X-Cache-Status: MISS`, затем `HIT` и `Age > 0`. Смотрим `sudo tail -f /var/log/nginx/access.log`.

5. **Меняем контент на origin.** На WEB01 поменяйте цену в `/etc/nginx/sites-available/shop.conf` (`price` → `1790`) и `reload`. Запрос через edge всё ещё отдаёт `1490` — это кэш, а не баг. Затем на UBU01: `sudo rm -rf /var/cache/nginx/edge/* && sudo nginx -s reload`, повторный запрос — `1790`, статус `MISS`.

6. **Вторая edge-нода.** Скопируйте конфиг: `scp /etc/nginx/conf.d/edge-shop.conf adm.student@10.10.10.62:/tmp/`. На k8s-w01: `sudo apt install -y nginx`, положите файл в `/etc/nginx/conf.d/`, замените `add_header X-Edge-Node edge1` на `edge2`, `nginx -t`, `reload`. Проверка: `curl -sI --resolve shop.vektor.lab:80:10.10.10.62 http://shop.vektor.lab/` → `X-Edge-Node: edge2`, контент тот же. Это и есть свойство Anycast: любой узел отдаёт то же самое.

7. **WAF-фильтр.** На edge1 добавьте `map` из пункта 8 раздела 4. Проверка:
   `curl -si 'http://10.10.10.30/?id=1%20union%20select%20password' -H 'Host: shop.vektor.lab' | head -1` → `403`. Обычный запрос → `200`.

8. **Rate limit.** На k8s-w02: `ab -n 300 -c 30 -H 'Host: shop.vektor.lab' http://10.10.10.30/`. Ожидаем в ответе `ab` ненулевые `Non-2xx responses`, в `/var/log/nginx/error.log` на UBU01 — `limiting requests, excess: ... by zone "req_per_ip"`. Легитимный `curl` при этом проходит.

9. **«DDoS» на L4 и отсечка.** На UBU01 примените правила nftables из пункта 11. На k8s-w02 запустите повторный `ab -n 500 -c 50`. На UBU01: `sudo nft -a list ruleset` — счётчик `counter` растёт; `ss -s` показывает `syn-recv`/`timewait`. Убедитесь, что один обычный `curl` всё ещё получает `200`.

10. **Наблюдаемость.** Включите на edge `stub_status`; добавьте в `log_format` `$request_time $upstream_response_time $upstream_cache_status`; проверьте, что `curl -s http://127.0.0.1/nginx_status` отдаёт `Active connections / accepts / handled / requests`. Опционально — проверка `curl`-скриптом в Zabbix на MON01.

11. **Конфиг в Git.** На UBU01 создайте каталог `edge-nginx/`, скопируйте туда `edge-shop.conf`, `git add/commit/push` в свой репозиторий на GIT01.

12. **Журнал стенда.** Допишите изменения в `stand-log.md`.

### Проверка

- [ ] `curl -sI` дважды даёт `MISS` → `HIT`, `Age` растёт
- [ ] После смены цены на origin edge отдаёт старое значение, после очистки кэша — новое
- [ ] `--resolve` на 10.10.10.62 отдаёт `X-Edge-Node: edge2` и тот же контент
- [ ] Запрос с `union select` возвращает `403`, обычный — `200`
- [ ] `ab` даёт часть не-2xx, в `error.log` есть `limiting requests`
- [ ] `nft list ruleset` показывает растущий счётчик дропнутых пакетов
- [ ] `nginx -t` на обеих edge-нодах: `syntax is ok`
- [ ] Конфиг закоммичен в Git, `stand-log.md` обновлён

### Сломай и почини

**Пользователь:** ivanov
**ВМ:** WIN11-CL01
**Что написал:** «Цены на shop.vektor.lab не меняются с обеда — у Петровой новые, у меня старые. F5 не помогает.»
**Срочность:** средняя

**Как воспроизвести.** На WEB01 поднимите `s-maxage` до `86400` (сутки) в `shop.conf`, `reload`, откройте новый товар/цену, обновите её ещё раз. С edge1: `curl -s -H 'Host: shop.vektor.lab' http://10.10.10.30/api/price` — старое значение, `X-Cache-Status: HIT`, `Age` в тысячах.

**Диагностика по порядку.**
1. Кэш или origin? `curl -s -H 'Host: shop.vektor.lab' -H 'Cache-Control: no-cache' http://10.10.10.30/api/price` → если новое, проблема в кэше.
2. Напрямую в origin: `curl -s http://10.10.20.10:8080/api/price` → убедиться, что новая цена вообще доехала до WEB01 (частая история: деплой прошёл не на все ноды).
3. `curl -sI` и посмотреть `Age`, `Cache-Control`, `X-Cache-Status` — виноват TTL.
4. Проверить конфиг обеих нод: `nginx -T | grep -E 's-maxage|proxy_cache_valid'` — не забыта ли edge2.
5. Отдельно — второй уровень: если есть shield-кэш, он держит свой TTL.

**Решение.** Уменьшить `s-maxage` до разумных 60 секунд на origin, добавить `proxy_cache_bypass $http_cache_control` (уже есть), очистить кэш на **обеих** нодах, в дальнейшем — версионировать API (`/api/v1/price?rev=42`) и не ставить суточный TTL на изменяемые данные.

**Ответ пользователю:** «Спасибо, проблема на стороне кэша на нашем пограничном сервере — цены обновлялись, но отдавалась сохранённая копия. Сбросили кэш и уменьшили время хранения до минуты, проверьте, пожалуйста, сейчас цены актуальны. При необходимости жёсткое обновление — Ctrl+F5.»

**Закрытие:** категория «Производительность/кэш», в базу знаний — заметка «TTL на изменяемые данные ≤ 60 с, инвалидация на всех edge-нодах».

### Что изменилось на стенде

- **WEB01 (10.10.20.10):** новый vhost на порту 8080, каталог `/var/www/shop`, эндпоинты `/` и `/api/price`.
- **UBU01 (10.10.10.30):** роль **edge1**, кэш-зона `edge_cache` в `/var/cache/nginx/edge`, rate limits, WAF-map, `stub_status`, таблица nftables `edgeguard`.
- **k8s-w01 (10.10.10.62):** роль **edge2** (временная, учебная), тот же конфиг с `X-Edge-Node: edge2`.
- **GW01:** без изменений конфигурации (только проверка маршрутизации).
- **GIT01 (10.10.10.50):** репозиторий `edge-nginx` с конфигом edge.
- **DNS-имя** `shop.vektor.lab` используется через заголовок `Host`/`--resolve`; A-записи на DC01 не менялись.
- `stand-log.md` дополнен этим списком.

### Откат

В VMware Workstation: правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-107` → **Restore**. Восстанавливать нужно все четыре ВМ (UBU01, k8s-w01, WEB01, GW01). Если nginx на k8s-w01 мешает кластеру — просто откатите её снапшотом. Конфиг в Git остаётся как артефакт: на будущих главах edge можно поднять из репозитория заново.

## 6. Ошибки новичков и безопасность

1. **Кэшировать всё подряд, включая `/api/profile` и страницы за логином.** → Один пользователь получает данные другого. → Правильно: `proxy_no_cache $cookie_session;`, `Cache-Control: private` от origin, отдельные `location` без кэша.
2. **Слишком большой TTL без инвалидации.** → Устаревшие цены/каталог, «баг, которого нет». → `s-maxage` 30–60 с для динамики, версионирование статики, отлаженный purge.
3. **Ключ кэша не учитывает `Host`/язык/схему.** → Контент одного сайта отдаётся на другом. → `proxy_cache_key "$scheme$request_method$host$request_uri"` плюс `Vary`.
4. **WAF сразу в блокирующем режиме.** → Ложные срабатывания режут нормальных пользователей и API-клиентов. → Сначала режим логирования, неделя наблюдения, исключения, потом блокировка.
5. **Rate limit по IP без `real_ip`.** → Блокируется весь офис за одним NAT или сам балансировщик. → `set_real_ip_from`/`real_ip_header`, лимиты по токену/аккаунту, ответ `429` с `Retry-After`.
6. **Считать, что CDN сам защитит от DDoS.** → Origin доступен по IP, атака идёт напрямую. → Закрыть origin файрволом: только адреса CDN, плюс секретный заголовок или mTLS между edge и origin.
7. **Не проверять «холодный кэш» после релиза.** → Шторм MISS-запросов кладёт origin. → `proxy_cache_lock`, `stale-while-revalidate`, прогрев, shield-слой.
8. **Логировать на edge токены, куки и персональные данные.** → Утечка в логи CDN-провайдера. → Урезать `log_format`, маскировать `Authorization`.

**Безопасность.** Терминация TLS на edge означает, что провайдер видит трафик в открытом виде — для персональных данных (152-ФЗ) и платёжных сервисов это отдельный разговор с юристами и ИБ, а не «просто включили CDN»; для критичных потоков — сквозное шифрование до origin и mTLS между edge и origin (см. урок 100 про Zero Trust). Секретный заголовок — слабая замена mTLS: он утекает в логи и конфиги. Личные домены и TLS-сертификаты в РФ имеют свои требования (в т.ч. использование сертификатов от доверенных УЦ и российских решений для отдельных категорий ресурсов) `[ПРОВЕРИТЬ актуальные требования 2026 года]`. И на стенде, и в жизни: пароли только учебные, ключи — не в Git.

## 7. Вопросы на собеседовании

**1. «У нас есть CDN. Почему пользователи после релиза видят старую версию сайта?»**
Ответ: смотрим на путь объекта: `Cache-Control`/`s-maxage` от origin, установленный TTL, ключ кэша, наличие промежуточного слоя и браузерного кэша; проверяем `X-Cache-Status` и `Age`. Лечение — короткий TTL для HTML, хэши в именах статики (`app.8f3a.js`), явный purge на всех PoP, `stale-while-revalidate` чтобы не отдавать 5xx при обновлении. Плюс проверка, что деплой доехал до всех origin-нод.
*Интервьюер хочет услышать:* системную диагностику по слоям и стратегию инвалидации, а не «нажмём purge».
*Красный флаг:* «поставим `no-cache` на всё» — убивает смысл CDN.

**2. «Что такое Anycast и какие у него ограничения?»**
Ответ: один IP-префикс анонсируется по BGP из многих точек; маршрутизация приводит клиента к ближайшей. Даёт низкую задержку, поглощение DDoS и автоматический failover при снятии анонса. Ограничения: TCP-сессия привязана к PoP, состояние (кэш, сессии) нужно синхронизировать, возможна асимметрия путей, нужен контроль анонсов (риск hijack, RPKI).
*Что хочет услышать:* понимание, что Anycast — это про BGP и маршрутизацию, а не про балансировщик.
*Красный флаг:* «Anycast балансирует нагрузку между серверами» — нет, он выбирает точку по маршруту.

**3. «Чем L4-балансировщик отличается от L7 и что выберете?»**
Ответ: L4 работает с TCP/UDP-потоком: быстрее, меньше ресурсов, умеет DSR, но не понимает HTTP — health-check только по порту. L7 разбирает HTTP: маршрутизация по путям и Host, ретраи, sticky, активные проверки, метрики по кодам ответа. Для TLS-пасса и высоких pps — L4, для приложений и canary — L7; часто ставят L4 перед L7.
*Красный флаг:* «L7 всегда лучше» без обсуждения стоимости и потери client IP.

**4. «Клиент жалуется, что его ботнет-трафик… то есть трафик его конкурентов кладёт наш сервис на 20 минут. Что делаем?»**
Ответ: классифицируем атаку (объёмная L3/L4 или L7 HTTP-флуд), смотрим на графики pps/bps и RPS, включаем лимиты на edge (`limit_req`/`limit_conn`), challenge для подозрительных, автоскейл, при объёмной атаке — провайдер: RTBH или FlowSpec, scrubbing. Параллельно закрываем origin от прямого доступа.
*Красный флаг:* «заблокируем весь Китай/все VPN» — режет живых пользователей.

**5. «Как защитить сервис от L7-флуда, если коммерческого anti-DDoS нет?»**
Ответ: многослойно — CDN/edge перед origin, `limit_req` по IP и по API-ключу, `limit_conn`, отсечка по поведенческим признакам (нет `Accept-Language`, аномальный UA), очередь `burst` вместо мгновенного 503, кэш для анонимных запросов, `proxy_cache_lock` и `stale` как защита origin, алерты на RPS/429, план на ручное включение «злого» режима. Понимать, что от 100 Гбит/с это не спасёт — нужен провайдер.

## 8. Источники (официальная документация)

- **nginx.org** — раздел `Documentation` → модули `ngx_http_proxy_module` (директивы `proxy_cache*`), `ngx_http_limit_req_module`, `ngx_http_limit_conn_module`, `ngx_http_realip_module`, `ngx_http_upstream_module`. Поиск: `nginx proxy_cache_path directive`.
- **RFC Editor / IETF (rfc-editor.org)** — RFC 9111 «HTTP Caching», RFC 5575 «Dissemination of Flow Specification Rules». Поиск: `RFC 9111 HTTP caching s-maxage`.
- **MDN Web Docs (developer.mozilla.org)** — статьи `Cache-Control`, `ETag`, `Vary`, `Age`. Поиск: `mdn cache-control stale-while-revalidate`.
- **OWASP (owasp.org)** — OWASP ModSecurity Core Rule Set, OWASP Top 10 (A05/A06). Поиск: `OWASP CRS paranoia level`.
- **Cloudflare Learning Center (cloudflare.com/learning)** — «What is Anycast», «What is a CDN», «What is a WAF». Поиск: `cloudflare learning what is anycast`.
- **Yandex Cloud (yandex.cloud/ru/docs)** — документация сервиса CDN: кэширование, правила, защита. Поиск: `Yandex Cloud CDN кэширование правила`.
- **Kubernetes (kubernetes.io)** — `Ingress` и аннотации ingress-nginx (кэш, лимиты, `X-Forwarded-For`). Поиск: `kubernetes ingress-nginx annotations proxy-body-size`.
- **Документация провайдеров anti-DDoS в РФ** (Qrator, DDoS-Guard, Selectel, VK Cloud) — разделы про scrubbing и фильтрацию; конкретные названия тарифов и функций меняются, проверяйте на сайте `[ПРОВЕРИТЬ]`.
