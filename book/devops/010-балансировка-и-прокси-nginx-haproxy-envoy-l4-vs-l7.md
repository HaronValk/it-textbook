# Балансировка и прокси: Nginx, HAProxy, Envoy, L4 vs L7, health checks, sticky sessions

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в техподдержку «Вектора»:**

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Внутренний кабинет `app.vektor.lab` открывается через раз: то страница, то "502 Bad Gateway". Началось после обеда, у Петровой то же самое. Работа стоит.»
> **Срочность:** средняя

Почти любой сервис в компании, который видят пользователи, стоит не напрямую: перед приложением всегда есть прокси или балансировщик. Он принимает соединение, решает, на какой бэкенд его отправить, проверяет, что бэкенд жив, терминирует TLS, пишет логи и метрики. Когда «работает через раз» — виноват чаще всего именно этот слой: один бэкенд умер, health check его не заметил, а балансировщик продолжает слать туда половину запросов.

После этой главы ты будешь уметь: поднять nginx как L7-балансировщик, HAProxy в режимах L4 и L7, прочитать конфиг Envoy, объяснить разницу между L4 и L7, настроить активные и пассивные health checks, сделать sticky sessions и понимать, почему они считаются костылём. Это базовый навык DevOps: тот же nginx/HAProxy стоит перед Kubernetes Ingress, тот же принцип health check используется в Service и Pod readiness.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Прокси** — посредник, который принимает соединение от клиента и сам открывает соединение к серверу. **Балансировщик** — прокси, который выбирает, к какому из нескольких серверов подключиться.

### L4 vs L7

```
Клиент
  │  TCP :443
  ▼
┌──────────────────────────────────────────┐
│ L4-балансировщик (HAProxy mode tcp,      │  видит: src/dst IP, порты, TCP-флаги
│ IPVS, LVS, AWS NLB)                      │  не видит: URL, Host, заголовки, cookie
└──────────────────────────────────────────┘
  │  TCP (часто с PROXY protocol)
  ▼
┌──────────────────────────────────────────┐
│ L7-прокси (nginx, HAProxy mode http,     │  видит: метод, путь, Host, cookie,
│ Envoy, Traefik, Ingress)                 │  заголовки, может терминировать TLS
└──────────────────────────────────────────┘
  │  HTTP (keepalive)
  ▼
Бэкенды: app-a / app-b
```

| | L4 | L7 |
|---|---|---|
| Уровень OSI | 4 (транспорт) | 7 (приложение) |
| Что видит | IP, порт, TCP-сессию | URL, Host, cookie, заголовки |
| Умеет | быстрая пересылка пакетов, TLS passthrough | маршрутизация по пути, sticky по cookie, ретраи, кэш, рерайт |
| Цена | очень быстро, мало CPU | больше CPU, больше возможностей |
| Отладка | tcpdump и всё | логи запросов, коды ответов |

Правило выбора: **L4 — когда нужна скорость и «всё равно, что внутри» (TLS passthrough, БД, произвольные протоколы). L7 — когда нужно маршрутизировать по содержимому HTTP и управлять сессиями.** В реальности их часто ставят каскадом: L4 снаружи (VIP, DDoS), L7 внутри (nginx/Envoy).

### Алгоритмы выбора бэкенда

- **round robin** — по кругу. Дефолт почти везде. Нормально, когда бэкенды одинаковые.
- **least connections / least requests** — на тот, где меньше активных соединений. Лучше при разной скорости ответа.
- **source/ip hash** — хеш от IP клиента: один клиент стабильно попадает на один бэкенд. Это и есть примитивная sticky session.
- **hash по cookie / ring hash** — то же, но по значению cookie или по хешу ключа (используется в Envoy и CDN).

### Health checks: пассивные и активные

- **Пассивный (passive)** — балансировщик наблюдает за реальными ответами. Nginx OSS умеет только это: `max_fails=3 fail_timeout=10s` — после 3 ошибок за 10 секунд сервер выводится из ротации на 10 секунд, потом пробуется снова. Активных проверок в nginx open source **нет**, они только в NGINX Plus (директива `health_check`) или в сторонних модулях.
- **Активный (active)** — отдельный пробник сам ходит на бэкенд: `option httpchk GET /healthz` в HAProxy, блок `health_checks` в Envoy. Интервал, пороги `fall`/`rise`, ожидаемый код ответа.
- **Outlier detection** — продвинутая пассивная проверка (Envoy): сервер временно исключается по статистике ошибок (5xx, таймауты) без явного «упал».

Ключевая мысль: **пассивный чек узнаёт о проблеме только через пользователя**, который получит ошибку. Активный выводит сервер заранее.

### Sticky sessions

Нужны, когда приложение хранит состояние в памяти процесса (сессия, корзина, загруженный файл), а не в Redis/БД.

- nginx OSS: `ip_hash` или `hash $cookie_session consistent`. Директивы `sticky` в nginx OSS **нет** — это NGINX Plus.
- HAProxy: `cookie SRV insert indirect nocache` + `server app-a ... cookie A` — HAProxy сам вставляет cookie и по нему прибивает клиента к серверу.
- Envoy: `hash_policy` по cookie в `route`.

Минусы одинаковы везде: неравномерная нагрузка, при потере бэкенда сессии «умирают», нельзя спокойно выкатывать новую версию. Правильное решение — вынести состояние из приложения.

### Где это в общей картине DevOps

Kubernetes Service — это L4-балансировка (kube-proxy/iptables/IPVS), Pod readiness probe — тот же health check. Ingress-контроллер (nginx-ingress, Envoy-based) — L7-прокси. Порт 9901 у Envoy — admin API: там метрики, конфиг, управление кластерами.

## 3. Аналогия из жизни

Представь стойку регистрации в аэропорту. **L4-балансировщик** — это охранник на входе в зал: он смотрит только на то, что человек вообще вошёл (IP и порт), и распихивает людей по стойкам наугад, по очереди. Ему всё равно, бизнес-класс ты или с просроченным паспортом — он не смотрит в документы. **L7-прокси** — это администратор в зале: он читает посадочный талон (URL, Host, cookie) и отправляет тебя на нужную стойку, а если стойка закрыта — на соседнюю.

**Health check** — это табличка «стойка открыта» на каждой стойке, которую администратор проверяет каждые две секунды. **Sticky session** — это когда тебя закрепили за конкретной сотрудницей, потому что твои документы лежат у неё в ящике; если сотрудница ушла на перерыв, документы искать заново.

Аналогия ломается на моменте, где балансировщик может перекинуть запрос на другой бэкенд **посреди уже начатого диалога** (keepalive-соединение), а человек так не может.

## 4. Практика и команды

Ниже — всё, что ты будешь делать на стенде. `UBU01` (10.10.10.30) — балансировщик, `MON01` (10.10.10.40) — два демо-бэкенда на портах 8081 и 8082 (в реальности это были бы отдельные хосты или поды; на учебном стенде экономим ВМ).

**1. Кто вообще слушает порты**

```bash
sudo ss -ltnp | grep -E ':(80|8080|8081|8082|8404|9000|9901)\b'
```

`-l` — только слушающие, `-t` — TCP, `-n` — без резолва имён, `-p` — процесс. Типичный вывод:

```text
LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=1421,fd=6))
LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:(("haproxy",pid=1550,fd=5))
```

Смотрим: занят ли порт, чей именно процесс, не висит ли чужой конфиг.

**2. Проверить конфиг nginx перед рестартом**

```bash
sudo nginx -t          # синтаксис
sudo nginx -T | less   # синтаксис + полный дамп собранного конфига
```

`-t` — test, `-T` — test + dump. Результат: `syntax is ok` / `test is successful`. Если ошибка — файл и строка указаны.

**3. Перезагрузить nginx без разрыва соединений**

```bash
sudo systemctl reload nginx      # graceful: старые воркеры дорабатывают запросы
sudo systemctl restart nginx     # жёстко: рвёт соединения
```

В работе почти всегда `reload`.

**4. Запрос с подменой Host и готовым DNS (без правки /etc/hosts)**

```bash
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' \
  --resolve app.vektor.lab:80:10.10.10.30 http://app.vektor.lab/
```

`--resolve имя:порт:IP` — curl сам подставит адрес, DNS не нужен. `-w` — шаблон вывода, `%{http_code}` — код ответа, `%{time_total}` — время.

**5. Проверить, как распределяются запросы (главный инструмент L1/L2-диагностики)**

```bash
for i in $(seq 20); do
  curl -s --resolve app.vektor.lab:80:10.10.10.30 http://app.vektor.lab/ | grep -o 'app-[ab]'
done | sort | uniq -c
```

Ожидаемо: `10 app-a` / `10 app-b`. Если `20 app-a` — либо второй бэкенд в дауне, либо включён `ip_hash`, либо не тот upstream.

**6. Sticky по cookie: клиентский «сосуд» для cookie**

```bash
curl -s -c /tmp/jar.txt -b /tmp/jar.txt -D- --resolve app.vektor.lab:8080:10.10.10.30 \
  http://app.vektor.lab:8080/ | grep -E 'HTTP/|Set-Cookie|app-'
```

`-c` — писать cookie в файл, `-b` — читать оттуда, `-D-` — вывести заголовки ответа. В ответе HAProxy добавит `Set-Cookie: SRV=A`. Повторные запросы с тем же `-b` идут на тот же сервер.

**7. Синтаксис конфига HAProxy**

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
```

`-c` — check, `-f` — файл конфигурации. Вывод: `Configuration file is valid`.

**8. Живая статистика HAProxy через сокет**

```bash
echo "show stat" | sudo socat stdio /run/haproxy/admin.sock | grep -E ',(app-a|app-b),'
```

`socat stdio <unix-socket>` — подключение к unix-сокету (если socat нет: `sudo apt install socat`, либо `nc -U /run/haproxy/admin.sock`). Первая строка CSV — заголовки; смотрим колонки `status` (`UP`/`DOWN`), `check_status`, `weight`.

**9. Envoy: посмотреть кластеры и здоровье эндпоинтов**

```bash
curl -s http://127.0.0.1:9901/clusters | grep -E 'app_cluster|health_flags'
curl -s 'http://127.0.0.1:9901/stats?filter=cluster.app_cluster' | head -20
```

Admin API Envoy на 9901: `/clusters`, `/stats`, `/config_dump`. Метрики вида `upstream_cx_active`, `upstream_rq_5xx`.

**10. Логи балансировщика**

```bash
sudo journalctl -u nginx -u haproxy -f
sudo tail -f /var/log/nginx/app-lb.error.log
```

`-u` — юнит systemd, `-f` — follow. В error-логе nginx при мёртвом бэкенде будет `connect() failed (111: Connection refused) while connecting to upstream`.

**11. Увидеть разницу L4 и L7 своими глазами**

```bash
sudo tcpdump -ni any -A 'tcp port 8080' | grep -E 'GET|Host:'
sudo tcpdump -ni any -c 5 'tcp port 9000'
```

Для HTTP-frontend (`:8080`) видно текст запроса — можно маршрутизировать по пути. Для TCP-frontend (`:9000`) трафик непрозрачен; если внутри TLS — только шифротекст.

**12. Проверить статус юнита бэкенда (пригодится в лабораторной)**

```bash
systemctl status app-a --no-pager
journalctl -u app-b -n 30 --no-pager
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

| ВМ | Адрес | Состояние |
|---|---|---|
| UBU01 | 10.10.10.30 | Ubuntu 24.04, SSH, `sudo`, интернет (NAT через VMnet8) |
| MON01 | 10.10.10.40 | Ubuntu 24.04, SSH, `sudo` |

Обе ВМ в одной сети 10.10.10.0/24, значит видят друг друга напрямую — GW01 и DMZ для этой лабы не нужны. Если MON01 ещё не развёрнут по плану стенда (урок 3), подними минимальную Ubuntu-ВМ: статический адрес 10.10.10.40, SSH, пользователь с `sudo` — этого достаточно. Windows-часть стенда не нужна.

### Снапшот

В VMware Workstation на **UBU01** и **MON01**:

```
VM → Snapshot → Take Snapshot… → имя: before-devops-010
```

Один и тот же префикс имени на всех затрагиваемых ВМ — так потом понятно, к какому уроку откатываться.

### Шаги

1. **UBU01: поставить балансировщики.**
   ```bash
   sudo apt update && sudo apt install -y nginx haproxy socat curl
   nginx -v; haproxy -v
   ```
   Ожидаемо: `nginx version: nginx/1.24...`, `HAProxy version 2.8...` (версии в Ubuntu 24.04, точные номера могут отличаться).

2. **MON01: подготовить два «приложения».**
   ```bash
   sudo mkdir -p /srv/app-a /srv/app-b
   echo "hello from app-a on $(hostname)" | sudo tee /srv/app-a/index.html
   echo "hello from app-b on $(hostname)" | sudo tee /srv/app-b/index.html
   sudo touch /srv/app-a/healthz /srv/app-b/healthz
   sudo chmod -R a+rX /srv/app-a /srv/app-b
   ```
   Ожидаемо: две папки с разным `index.html` и пустыми файлами `healthz` (по ним будут ходить health checks).

3. **MON01: оформить бэкенды как systemd-юниты** (как в уроке 8). `/etc/systemd/system/app-a.service`:
   ```ini
   [Unit]
   Description=Vektor demo app A
   After=network-online.target

   [Service]
   Type=simple
   WorkingDirectory=/srv/app-a
   ExecStart=/usr/bin/python3 -m http.server 8081 --bind 0.0.0.0
   Restart=on-failure
   NoNewPrivileges=yes
   ProtectSystem=strict
   ProtectHome=yes
   PrivateTmp=yes
   ReadOnlyPaths=/srv/app-a

   [Install]
   WantedBy=multi-user.target
   ```
   Для `app-b` — то же самое с `8082` и `/srv/app-b`. Затем:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now app-a app-b
   curl -s http://127.0.0.1:8081/ ; curl -s http://127.0.0.1:8082/
   ```

4. **UBU01: убедиться, что бэкенды видны по сети.**
   ```bash
   curl -s http://10.10.10.40:8081/ && curl -s http://10.10.10.40:8082/
   ```
   Ожидаемо: два разных текста `app-a` / `app-b`. Если «connection refused» — вернись к шагу 3, проверь `ss -ltnp` на MON01.

5. **UBU01: конфиг nginx как L7-балансировщика.** Файл `/etc/nginx/sites-available/app-lb.conf`:
   ```nginx
   upstream app_backend {
       least_conn;
       server 10.10.10.40:8081 max_fails=3 fail_timeout=10s;
       server 10.10.10.40:8082 max_fails=3 fail_timeout=10s;
       keepalive 16;
   }

   server {
       listen 80;
       server_name app.vektor.lab;

       access_log /var/log/nginx/app-lb.access.log;
       error_log  /var/log/nginx/app-lb.error.log warn;

       location / {
           proxy_pass http://app_backend;
           proxy_http_version 1.1;
           proxy_set_header Connection "";
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_connect_timeout 2s;
           proxy_next_upstream error timeout http_502;
       }
   }
   ```
   ```bash
   sudo ln -s /etc/nginx/sites-available/app-lb.conf /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```
   Что важно: `keepalive 16` работает только вместе с `proxy_http_version 1.1` и пустым `Connection` — иначе nginx открывает новое TCP-соединение на каждый запрос.

6. **UBU01: проверить распределение.**
   ```bash
   for i in $(seq 20); do
     curl -s --resolve app.vektor.lab:80:10.10.10.30 http://app.vektor.lab/ | grep -o 'app-[ab]'
   done | sort | uniq -c
   ```
   Ожидаемо: примерно поровну `app-a` и `app-b`.

7. **UBU01: включить sticky по IP и посмотреть, что изменилось.** В блоке `upstream` добавь `ip_hash;` (вместо `least_conn;`), выполни `sudo nginx -t && sudo systemctl reload nginx` и повтори цикл из шага 6. Ожидаемо: все 20 запросов — на один и тот же бэкенд. Затем верни `least_conn;`.

8. **UBU01: настроить HAProxy — сразу и L7 (`:8080`), и L4 (`:9000`), и статистику (`:8404`).** Положи целиком в `/etc/haproxy/haproxy.cfg`:
   ```haproxy
   global
       log /dev/log local0
       maxconn 2000
       stats socket /run/haproxy/admin.sock mode 660 level admin
       user haproxy
       group haproxy
       daemon

   defaults
       log     global
       mode    http
       option  httplog
       option  dontlognull
       timeout connect 2s
       timeout client  30s
       timeout server  30s
       retries 2

   frontend fe_http
       bind *:8080
       default_backend be_app_http

   backend be_app_http
       balance roundrobin
       option httpchk GET /healthz
       http-check expect status 200
       default-server check inter 2s fall 3 rise 2
       cookie SRV insert indirect nocache
       server app-a 10.10.10.40:8081 cookie A
       server app-b 10.10.10.40:8082 cookie B

   frontend fe_tcp
       bind *:9000
       mode tcp
       default_backend be_app_tcp

   backend be_app_tcp
       mode tcp
       balance leastconn
       server app-a 10.10.10.40:8081 check
       server app-b 10.10.10.40:8082 check

   listen stats
       bind *:8404
       mode http
       stats enable
       stats uri /stats
       stats refresh 5s
       stats admin if LOCALHOST
   ```
   ```bash
   sudo haproxy -c -f /etc/haproxy/haproxy.cfg
   sudo systemctl restart haproxy
   sudo systemctl status haproxy --no-pager
   ```
   Ожидаемо: конфиг валиден, сервис `active (running)`, заняты порты 8080, 8404, 9000. На Ubuntu логи HAProxy по умолчанию идут в journal (`journalctl -u haproxy`); отдельный `/var/log/haproxy.log` появится только после настройки rsyslog `[ПРОВЕРИТЬ под свою версию]`.

9. **UBU01: проверить sticky cookie.** Первый запрос с сохранением cookie, второй — с ним же:
   ```bash
   curl -s -c /tmp/jar.txt -D- --resolve app.vektor.lab:8080:10.10.10.30 \
     http://app.vektor.lab:8080/ | grep -Ei 'set-cookie|app-'
   curl -s -b /tmp/jar.txt --resolve app.vektor.lab:8080:10.10.10.30 \
     http://app.vektor.lab:8080/
   ```
   Ожидаемо: в первом ответе `set-cookie: SRV=A`, второй запрос идёт на тот же `app-a`, хотя round robin должен был отдать `app-b`.

10. **UBU01: посмотреть статусы в живую.**
    ```bash
    echo "show stat" | sudo socat stdio /run/haproxy/admin.sock | grep -E ',(app-a|app-b),'
    ```
    Плюс в браузере (или `curl`) `http://10.10.10.30:8404/stats` — таблица с колонками `Status`, `check`, `LastChk`.

11. **Проверить health check в деле.** На MON01: `sudo systemctl stop app-b`. Через ~6 секунд (`inter 2s` × `fall 3`) в статистике `app-b` станет `DOWN`, весь трафик через `:8080` пойдёт на `app-a` без единой ошибки у клиента. Через nginx (`:80`) картина другая: первые запросы на мёртвый бэкенд дадут 502, пока `max_fails=3` не выведет его из ротации на 10 секунд. Верни: `sudo systemctl start app-b` — статус станет `UP` после двух успешных проверок (`rise 2`).

12. **Опционально: Envoy.** Envoy нет в стандартных репозиториях Ubuntu `[ПРОВЕРИТЬ актуальный способ установки в официальной документации envoyproxy.io]` — ставь по официальной инструкции или через официальный образ. Конфиг `/etc/envoy/envoy.yaml`:
    ```yaml
    static_resources:
      listeners:
      - name: http_listener
        address:
          socket_address: { address: 0.0.0.0, port_value: 10000 }
        filter_chains:
        - filters:
          - name: envoy.filters.network.http_connection_manager
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
              stat_prefix: ingress_http
              route_config:
                name: local_route
                virtual_hosts:
                - name: app
                  domains: ["*"]
                  routes:
                  - match: { prefix: "/" }
                    route: { cluster: app_cluster }
              http_filters:
              - name: envoy.filters.http.router
                typed_config:
                  "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
      clusters:
      - name: app_cluster
        connect_timeout: 2s
        type: STRICT_DNS
        lb_policy: LEAST_REQUEST
        load_assignment:
          cluster_name: app_cluster
          endpoints:
          - lb_endpoints:
            - endpoint:
                address:
                  socket_address: { address: 10.10.10.40, port_value: 8081 }
            - endpoint:
                address:
                  socket_address: { address: 10.10.10.40, port_value: 8082 }
        health_checks:
          timeout: 1s
          interval: 3s
          unhealthy_threshold: 3
          healthy_threshold: 2
          http_health_check:
            path: /healthz
    admin:
      address:
        socket_address: { address: 127.0.0.1, port_value: 9901 }
    ```
    Запуск: `envoy -c /etc/envoy/envoy.yaml --log-level info`. Проверка: `curl -s localhost:9901/clusters | grep app_cluster`.

13. **Зафиксировать конфиги в Git** (пока локально на UBU01; на GIT01 уедет, когда он появится):
    ```bash
    sudo mkdir -p /opt/vektor-lb && sudo chown $USER /opt/vektor-lb && cd /opt/vektor-lb
    git init
    cp /etc/nginx/sites-available/app-lb.conf ./nginx-app-lb.conf
    cp /etc/haproxy/haproxy.cfg ./haproxy.cfg
    git add . && git commit -m "LB: nginx L7 + haproxy L4/L7, health checks, sticky"
    ```

### Проверка

- [ ] `sudo nginx -t` на UBU01 — `test is successful`.
- [ ] `sudo haproxy -c -f /etc/haproxy/haproxy.cfg` — `Configuration file is valid`.
- [ ] `ss -ltnp` на UBU01 показывает слушающие 80 (nginx), 8080/8404/9000 (haproxy).
- [ ] Цикл из 20 curl через `:80` даёт примерно 10/10 между `app-a` и `app-b`.
- [ ] С `ip_hash` все 20 запросов идут на один бэкенд.
- [ ] `curl -c/-b` через `:8080` сохраняет `SRV=A` и держит клиента на `app-a`.
- [ ] В `http://10.10.10.30:8404/stats` оба сервера `UP`, колонка `LastChk` обновляется.
- [ ] После `systemctl stop app-b` на MON01 статус в HAProxy становится `DOWN`, а ответы через `:8080` продолжают быть 200.
- [ ] `/opt/vektor-lb` — git-репозиторий с коммитом.

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия) / ВМ WIN11-CL01
> **Что написал:** «Кабинет `app.vektor.lab` то открывается, то "502 Bad Gateway". Через раз. У Петровой тоже.»
> **Срочность:** средняя

**Как воспроизвести поломку** (на MON01):

```bash
sudo systemctl stop app-b
sudo systemctl disable app-b     # чтобы после ребута «неожиданно» не починилось
```

**Диагностика по шагам (именно в этом порядке):**

1. **Повторить симптом своими руками** — сначала подтверди проблему, а не поверь на слово:
   ```bash
   for i in $(seq 10); do
     curl -s -o /dev/null -w '%{http_code} ' --resolve app.vektor.lab:80:10.10.10.30 http://app.vektor.lab/
   done; echo
   ```
   Видим смесь `200 200 502 200 200 502` → проблема на стороне бэкендов, а не у пользователя.

2. **Логи балансировщика** — куда именно не достучались:
   ```bash
   sudo tail -50 /var/log/nginx/app-lb.error.log
   ```
   Ключевая строка: `connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://10.10.10.40:8082/"`. Адрес назван → идём туда.

3. **Проверить бэкенд** (на MON01):
   ```bash
   ss -ltnp | grep -E ':(8081|8082)\b'
   systemctl status app-b --no-pager
   journalctl -u app-b -n 50 --no-pager
   ```
   Порт 8082 не слушается, юнит `inactive (dead)` — вот и причина.

4. **Понять, почему не спасли health checks.** Через nginx OSS активных проверок нет: `max_fails=3 fail_timeout=10s` — это пассивный чек, он выводит сервер из ротации **только после трёх ошибок за 10 секунд**, а первые запросы уже получили 502. Через HAProxy (`:8080`) та же авария прошла бы незаметно — там `option httpchk` проверяет `/healthz` каждые 2 секунды.

5. **Решение:**
   ```bash
   sudo systemctl enable --now app-b
   systemctl status app-b --no-pager
   curl -s http://127.0.0.1:8082/healthz -o /dev/null -w '%{http_code}\n'
   ```
   Затем на UBU01 дождаться `UP` в статистике HAProxy (до 2 секунд × `rise 2`) и повторить цикл из пункта 1 — все `200`.

6. **Ответ пользователю:** «Иван, добрый день. Причина — один из двух серверов приложения был остановлен, часть запросов уходила на него и получала ошибку. Сервер снова запущен и добавлен в автозагрузку, сейчас все запросы проходят штатно. Проверьте, пожалуйста, кабинет; если останется ошибка — пришлите скриншот и время, посмотрю логи точечно.»

7. **Закрыть заявку:** зафиксировать в тикете «root cause: сервис app-b на MON01 в состоянии stopped; действие: enable --now; профил. мера: перевести сервис на активные health checks (HAProxy/Envoy) вместо пассивных nginx». Обновить `stand-log.md`.

### Что изменилось на стенде

- **UBU01 (10.10.10.30)** — новая роль: edge/балансировщик. Пакеты `nginx`, `haproxy`, `socat`. Nginx на `:80`, сайт `app.vektor.lab` → upstream `app_backend` (10.10.10.40:8081, :8082), конфиг `/etc/nginx/sites-available/app-lb.conf`, дефолтный сайт отключён. HAProxy: `fe_http :8080` (round robin + sticky cookie `SRV` + httpchk `/healthz`), `fe_tcp :9000` (mode tcp, leastconn), статистика `:8404/stats`, admin-сокет `/run/haproxy/admin.sock`. Опционально Envoy `:10000`, admin `127.0.0.1:9901`. Локальный git-репозиторий `/opt/vektor-lb`.
- **MON01 (10.10.10.40)** — новая роль: демо-бэкенды. Каталоги `/srv/app-a`, `/srv/app-b`, файлы `index.html`, `healthz`. systemd-юниты `app-a.service` (:8081) и `app-b.service` (:8082).
- **Имя `app.vektor.lab`** — пока резолвится только через `curl --resolve`; записи в DNS нет.
- **Не менялось:** DC01, FS01, домен, пользователи, общие папки — Windows-часть для этой лабы не нужна.

### Откат

VMware Workstation на каждой из двух ВМ (UBU01, MON01): правый клик по ВМ → **Snapshot → Snapshot Manager** → выбрать `before-devops-010` → **Go To / Revert**. ВМ вернётся к состоянию до лабы: пакеты, конфиги, юниты и каталоги на MON01 исчезнут. Если нужно сохранить конфиги — сначала вытолкни `/opt/vektor-lb` в любой внешний Git или скопируй на хост. Git-репозиторий внутри ВМ откатится вместе с ней.

## 6. Ошибки новичков и безопасность

1. **Дефолтный round robin на неравных бэкендах.** → Медленный сервер копит соединения и становится ещё медленнее. → Смотри на p95 латентности и переключайся на `least_conn` / `LEAST_REQUEST`, а лучше — выравнивай бэкенды.
2. **Нет keepalive до бэкендов.** → nginx открывает новое TCP-соединение на каждый запрос, лишний handshake, тысячи TIME_WAIT. → `keepalive N` в upstream + `proxy_http_version 1.1` + `proxy_set_header Connection ""`.
3. **Health check «заходит на главную».** → Проверка дергает тяжёлый запрос к БД и сама создаёт нагрузку; или наоборот проверяет только TCP-порт, а приложение уже отдаёт 500. → Отдельный лёгкий `/healthz`, отдающий 200 без зависимостей; для «глубокой» проверки — отдельный `/readyz` с ограничением по частоте.
4. **Health check без `rise`/`fall`.** → Флаппинг: сервер то в ротации, то нет, каждый раз сбрасывая сессии. → `fall 3 rise 2` и интервал не меньше пары секунд.
5. **Sticky sessions как архитектура.** → Один бэкенд перегружен, при деплое все сессии умирают, горизонтально не масштабируется. → Хранить состояние в Redis/БД, sticky — только как временный костыль с явным сроком жизни.
6. **Терминация TLS без `X-Forwarded-Proto`/`X-Forwarded-For`.** → Приложение редиректит на https по кругу, в логах вместо клиентов IP балансировщика. → Пробрасывать заголовки и настроить доверие к ним со стороны приложения.
7. **Один балансировщик.** → Это единая точка отказа: упал — лежит весь сервис. → Пара + VIP (keepalived/VRRP) или DNS с двумя A-записями, для L7 — несколько реплик за L4.
8. **`reload` не проверяя конфиг.** → `nginx -t` забыт → старый конфиг остаётся, а ты уверен, что «задеплоил». → Всегда `nginx -t && systemctl reload nginx` и `haproxy -c -f` перед рестартом.

### Безопасность

- **Admin-порты только на localhost.** Envoy admin `:9901` и `stats admin` HAProxy — это фактически управление сервисом: через admin API Envoy можно менять конфиг и даже остановить процесс. В боевой среде они слушают `127.0.0.1`, доступ — через SSH-туннель (это тема урока 12). Никогда не публикуй их наружу.
- **`stats admin if LOCALHOST`** в лабе стоит именно поэтому; не расширяй это правило на всю сеть.
- **Логи.** В `access_log` не должны попадать `Authorization: Bearer …`, cookie сессий, номера карт. Настраивай формат лога осознанно.
- **TLS.** В этой лабе весь трафик — открытый HTTP. Это допустимо только на изолированном стенде. В работе между балансировщиком и бэкендом либо TLS, либо доверенная сеть — подробно в уроке 11.
- **Секреты в конфиге.** Пароли к бэкендам и API-ключи не хранятся в `haproxy.cfg` в Git. Используй файлы с правами `0400` и/или внешнее хранилище секретов.
- **DHCP/адреса.** Адреса стенда фиксированы — не меняй их, иначе последующие главы (Ingress, Kubernetes) не сойдутся с журналом стенда.

## 7. Вопросы на собеседовании

**1. Чем L4-балансировщик отличается от L7 и когда ты выберешь каждый?**

Ответ: L4 работает на транспортном уровне — видит IP, порты и TCP-сессию, не разбирает полезную нагрузку. Он быстрее, дешевле по CPU, умеет TLS passthrough и любые не-HTTP протоколы (БД, gRPC-поверх-TCP, произвольный бинарь). L7 разбирает HTTP: маршрутизация по Host и пути, sticky по cookie, ретраи, рерайт заголовков, кэш, WAF. Выбор: снаружи как правило L4 (VIP, DDoS, TLS passthrough), внутри — L7 (nginx/Envoy/Ingress) для маршрутизации и наблюдаемости. Часто каскад: L4 → L7 → приложение.
*Что хочет услышать:* чёткое разделение «что видит / что умеет / чем платишь», плюс реальный пример каскада.
*Красный флаг:* «L7 всегда лучше, потому что умнее» без упоминания стоимости и TLS passthrough.

**2. Как работают health checks и чем активные отличаются от пассивных?**

Ответ: пассивные — балансировщик смотрит на реальные ответы пользователям (nginx OSS: `max_fails`/`fail_timeout`, Envoy `outlier_detection`). Активные — отдельный пробник сам опрашивает бэкенд: HAProxy `option httpchk GET /healthz` + `fall`/`rise`, Envoy `health_checks` с `interval`/`unhealthy_threshold`. Важно: в nginx open source активных проверок нет, только в Plus или сторонних модулях. В Kubernetes аналог — readiness/liveness probes: readiness убирает под из балансировки, liveness перезапускает контейнер.
*Что хочет услышать:* что пассивный чек узнаёт о проблеме только через пользователя, поэтому активный предпочтительнее; и что health endpoint должен быть дешёвым.
*Красный флаг:* «в nginx есть директива health_check» без уточнения, что это Plus.

**3. Как сделать sticky sessions и почему это плохо?**

Ответ: nginx OSS — `ip_hash` или `hash $cookie_session consistent`; HAProxy — `cookie SRV insert indirect nocache` + `server ... cookie A`; Envoy — `hash_policy` по cookie. Минусы: неравномерная нагрузка, потеря сессии при падении бэкенда, невозможность спокойного blue-green. Правильно — вынести состояние в Redis/БД и сделать приложение stateless, sticky оставить как временную меру.
*Что хочет услышать:* конкретные директивы + понимание, что это антипаттерн, а не решение.
*Красный флаг:* «sticky — это стандартная практика, всегда так делаем».

**4. Клиент приходит через балансировщик, а приложение видит IP балансировщика. Что делать?**

Ответ: пробрасывать `X-Forwarded-For` (nginx: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`), `X-Real-IP`, `X-Forwarded-Proto` и `Host`. Приложение/фреймворк должно доверять этим заголовкам только от известных прокси. Для не-HTTP протоколов — PROXY protocol (`send-proxy` в HAProxy, `proxy_protocol` в nginx), он добавляет заголовок в начало TCP-соединения.
*Что хочет услышать:* знание обоих механизмов и того, что доверие к заголовкам — вопрос безопасности (подделка IP).
*Красный флаг:* «просто включить PROXY protocol везде» без учёта, что бэкенд должен его поддерживать.

**5. Что произойдёт с активными соединениями при `reload` nginx / перезапуске HAProxy?**

Ответ: nginx `reload` — graceful: мастер читает новый конфиг, поднимает новых воркеров, старые дорабатывают текущие запросы и завершаются; соединения не рвутся, но на время смены конфига памяти вдвое больше. HAProxy `systemctl reload` тоже делает soft-reload через новую сессию, но при жёстком `restart` соединения обрываются. Правильный деплой конфига LB: проверить синтаксис (`nginx -t`, `haproxy -c -f`), сделать graceful reload, посмотреть ошибки в логах и метриках 5xx.
*Что хочет услышать:* разницу reload/restart, обязательность валидации конфига и наблюдение после изменения.
*Красный флаг:* «делаю restart, так надёжнее».

## 8. Источники (официальная документация)

- **nginx.org** — раздел «Module ngx_http_upstream_module» (директивы `upstream`, `server`, `least_conn`, `ip_hash`, `keepalive`, `max_fails`, `fail_timeout`). Поиск: `nginx upstream module max_fails keepalive`.
- **nginx.org** — «ngx_http_proxy_module» (`proxy_next_upstream`, `proxy_set_header`, `proxy_http_version`). Поиск: `nginx proxy_next_upstream proxy_set_header`.
- **haproxy.org** — «HAProxy Configuration Manual» (разделы `balance`, `option httpchk`, `cookie`, `mode tcp`, `stats socket`). Поиск: `haproxy configuration manual cookie stick-table health check`.
- **envoyproxy.io** — «Envoy documentation: Arch overview» и «HTTP health checking», «Cluster manager / outlier detection», «Administration interface». Поиск: `envoy health_checks cluster configuration admin interface`.
- **kubernetes.io** — «Service» (L4-балансировка, kube-proxy) и «Ingress» (L7-маршрутизация). Поиск: `kubernetes service ingress l4 l7`.
- **learn.microsoft.com** — раздел про Application Gateway / Azure Load Balancer, если нужно сравнить облачные L4/L7-решения. Поиск: `azure load balancer vs application gateway l4 l7`.
- Способ установки Envoy через официальный репозиторий или образ — сверяй на **envoyproxy.io**, конкретные команды могут меняться от версии к версии `[ПРОВЕРИТЬ]`.
