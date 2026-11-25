# Веб-сервисы и reverse proxy: Nginx, Apache, Let's Encrypt, WAF

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»: **Пользователь:** sidorov (склад). **ВМ:** WIN11-CL01. **Что написал:** «Открываю портал `portal.corp.vektor.lab` — браузер пишет "Подключение не защищено", а у петровой вообще ERR_CONNECTION_TIMED_OUT. Мне туда заявки на отгрузку заносить, а оно не работает». **Срочность:** средняя.

Внутренний веб-портал, вики, панель 1С, веб-морда СЭД, API для интеграций, публичный сайт — всё это живёт на веб-серверах, и админ среднего уровня почти всегда оказывается тем человеком, кто ставит перед приложением обратный прокси, прикручивает TLS, закрывает сайт от ботов и SQL-инъекций, и потом полгода следит, чтобы сертификат не протух в субботу вечером.

После этой главы ты должен уметь: поднять nginx как reverse proxy перед бэкендом (Apache или приложение), выпустить и установить сертификат (в лаборатории — от внутреннего УЦ, в бою — Let's Encrypt), настроить базовую защиту через WAF на ModSecurity + OWASP CRS, читать логи nginx и Apache и понимать, куда смотреть, когда пользователь видит 502.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Reverse proxy (обратный прокси)** — сервер, который стоит между клиентом и приложением: клиент думает, что говорит с сайтом, а на самом деле говорит с nginx, а nginx пересылает запрос приложению (Apache, Tomcat, Node.js, 1С-веб-публикация) по внутренней сети или на localhost. Обратный — потому что клиент не знает, что за ним стоит прокси, в отличие от «прямого» прокси на стороне клиента.

Зачем он нужен:
- **Единая точка входа TLS.** Сертификат и шифрование — на прокси, бэкенд работает по HTTP внутри.
- **Маршрутизация** по домену или пути: `portal.…` → приложение А, `api.…` → приложение Б.
- **Защита**: скрываем внутренние порты, ставим лимиты запросов, WAF, базовую аутентификацию на стенд.
- **Отдача статики** быстро и кэширование, не нагружая бэкенд.
- **Обновление без простоя**: приложение можно перезапустить, а прокси отдаст заглушку.

Слои: HTTP/1.1 и HTTP/2, TLS 1.2/1.3 — это прикладной уровень и «сессионно-представительский» (в модели OSI L7 и L5/6), порты 80 и 443. WAF работает на L7, потому что ему нужно разбирать URL, тело запроса, заголовки и куки.

**Nginx vs Apache.** Nginx — событийная архитектура (event-driven), один воркер на ядро обслуживает тысячи соединений; идеален для статики и проксирования. Apache — процессно-потоковый (MPM prefork/event), с `.htaccess`, богатой модульной экосистемой, mod_php, mod_security «из коробки». В бою часто комбинируют: nginx на фронте, Apache/приложение сзади.

**Let's Encrypt и ACME.** Let's Encrypt — бесплатный удостоверяющий центр, работающий по протоколу ACME (Automatic Certificate Management Environment). Клиент (certbot, acme.sh) доказывает владение доменом одним из способов: **HTTP-01** (кладём файл на `http://домен/.well-known/acme-challenge/…`, нужен открытый 80 порт и публичный DNS), **DNS-01** (TXT-запись, работает и для wildcard-сертификатов), **TLS-ALPN-01** (редко). Сертификат живёт 90 дней, обновление — по таймеру. Для внутренних имён вроде `corp.vektor.lab` Let's Encrypt **не выдаст** сертификат никогда: центр проверяет домен через публичный DNS.

**WAF (Web Application Firewall)** — фильтр прикладного уровня, который смотрит на содержимое запроса и блокирует атаки по сигнатурам: SQL-инъекции, XSS, path traversal, RCE, сканеры. Классика — **ModSecurity** с набором правил **OWASP CRS** (Core Rule Set). Режимы: `DetectionOnly` (только логирует) и `On` (блокирует). CRS работает по **anomaly scoring**: каждое сработавшее правило даёт баллы, и если сумма выше порога — запрос блокируется. Есть **paranoia level** (PL1–PL4): чем выше, тем строже и тем больше ложных срабатываний.

```text
                      DMZ 10.10.20.0/24
  WIN11-CL01   GW01  ┌──────────────────────────────────────────┐
  10.10.10.x ────────►│  WEB01 10.10.20.10                       │
  (офис)  nftables    │                                          │
          NAT/forward │  nginx :443  (TLS, HSTS, limit_req)      │
                      │      │  proxy_pass http://127.0.0.1:8080 │
                      │      ▼                                   │
                      │  Apache :8080 (только loopback)          │
                      │   + ModSecurity + OWASP CRS = WAF        │
                      │      │                                   │
                      │      ▼                                   │
                      │  /var/www/portal (приложение)            │
                      └──────────────────────────────────────────┘
   DNS: portal.corp.vektor.lab A 10.10.20.10  (зона на DC01)
   Сертификат: внутренний УЦ Vektor Lab Root CA (в бою — Let's Encrypt)
```

## 3. Аналогия из жизни

Reverse proxy — это **ресепшен в бизнес-центре**. Клиент приходит на один адрес, охранник на входе (TLS) проверяет пропуск, секретарь (nginx) смотрит, в какую компанию ему нужно, и провожает до нужной двери. Внутренние двери (порты приложения) наружу не выходят — с улицы видно только ресепшен. Металлодетектор у входа — это WAF: он досматривает содержимое сумки, а не только факт входа. Журнал посещений — access.log, а если досмотр что-то нашёл, пишется отдельный акт — modsec_audit.log. Сертификат — это пропуск с датой действия: пока он действителен, охрана пускает; протух — и всё встало.

Аналогия ломается на кэшировании и на «прокси сам решает, отдать ответ из памяти»: ресепшен не имеет права отвечать клиенту вместо компании, а nginx с настроенным `proxy_cache` — имеет.

## 4. Практика и команды

**1. Кто реально слушает порты.** Первое, что смотрим всегда.
```bash
sudo ss -tlnp
```
`-t` TCP, `-l` listening, `-n` без резолва имён, `-p` процесс. Вывод: `LISTEN 0 511 0.0.0.0:443 users:(("nginx",pid=812,...))`. Смотрим: на каких адресах слушает (0.0.0.0 — со всех, 127.0.0.1 — только локально), нет ли двух процессов на одном порту.

**2. Проверка и дамп конфига nginx.**
```bash
sudo nginx -t          # синтаксис + доступность файлов сертификатов
sudo nginx -T | less   # полный итоговый конфиг со всеми include
```
`-t` обязателен перед каждым reload. `-T` показывает, что реально попало в конфиг после всех `include` — незаменимо, когда «я же добавил server, почему не работает».

**3. Применение конфига без разрыва соединений.**
```bash
sudo systemctl reload nginx     # graceful: старые соединения доживают
sudo systemctl restart nginx    # жёстко, с обрывом
journalctl -u nginx -n 50 --no-pager
```
Reload — норма, restart — когда меняли модули или systemd-юнит.

**4. Проверка Apache.**
```bash
sudo apache2ctl configtest          # Syntax OK
sudo apache2ctl -S                  # какие VirtualHost, на каких адресах:портах
sudo apache2ctl -M | grep -i security
```
`-S` показывает раскладку vhost'ов и какой из них дефолтный. `-M` — список загруженных модулей, ищем `security2_module`.

**5. Тест виртуального хоста без правки DNS.**
```bash
curl -I --resolve portal.corp.vektor.lab:443:10.10.20.10 https://portal.corp.vektor.lab/
curl -I -k https://10.10.20.10/            # -k = игнорировать проверку сертификата
curl -v https://portal.corp.vektor.lab/ 2>&1 | grep -Ei 'subject|issuer|expire|SSL'
```
`--resolve` подставляет IP для конкретного имени — так тестируют vhost до появления DNS-записи. `-I` — только заголовки.

**6. Что реально отдаёт TLS.**
```bash
openssl s_client -connect 10.10.20.10:443 -servername portal.corp.vektor.lab -showcerts </dev/null
```
Смотрим: `subject=CN`, `issuer` (кто подписал), `Verify return code: 0 (ok)`, протокол `TLSv1.3`, и что цепочка полная. `-servername` обязателен для SNI-хостов, иначе получишь не тот сертификат.

**7. Разбор самого сертификата.**
```bash
openssl x509 -in /etc/nginx/tls/portal.crt -noout -subject -issuer -dates -ext subjectAltName
```
`-dates` покажет `notBefore/notAfter` — подписывай это в мониторинг. `subjectAltName` должен содержать DNS-имя; современные браузеры смотрят только на SAN, а не на CN.

**8. Выпуск сертификата Let's Encrypt (боевой сценарий).**
```bash
sudo certbot --nginx -d portal.example.ru --dry-run     # прогон в staging
sudo certbot --nginx -d portal.example.ru               # боевой выпуск + правка конфига
sudo certbot certificates                               # список и сроки
sudo certbot renew --dry-run                            # проверка автопродления
systemctl list-timers | grep certbot
```
`--nginx` — плагин сам правит конфиг и делает reload; `--dry-run` обращается к staging-окружению и не расходует лимиты. Продление: пакет ставит таймер `certbot.timer`, который дважды в сутки дергает `renew` и обновляет то, что истекает в ближайшие 30 дней. Лимиты Let's Encrypt (примерно 50 сертификатов на домен в неделю и 5 неудачных валидаций в час — **уточняй актуальные на letsencrypt.org**) — причина не гонять продление руками по десять раз.

**9. Топ источников запросов из access.log.**
```bash
sudo awk '{print $1}' /var/log/nginx/portal_access.log | sort | uniq -c | sort -rn | head -10
sudo awk '$9>=400 {print $1, $7, $9}' /var/log/nginx/portal_access.log | sort | uniq -c | sort -rn | head
```
Первое — кто долбит сайт (кандидаты на блокировку). Второе — кто получает ошибки 4xx/5xx по конкретным URL.

**10. Проверка WAF и чтение аудита.**
```bash
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8080/?id=1%27%20OR%20%271%27%3D%271"
sudo tail -n 40 /var/log/apache2/modsec_audit.log
sudo grep -c '\[id "942100"\]' /var/log/apache2/modsec_audit.log
```
Код 403 — WAF сработал. В аудите ищем `rule id` (942100 — классика на SQLi в CRS), `severity`, `msg`, `data`. Так же проверяют и ложные срабатывания: правило 942100 на форме поиска, где пользователь реально вводит апостроф.

**11. Закрыть стенд паролем (staging).**
```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd student
```
В `location` добавляется `auth_basic "Vektor staging"; auth_basic_user_file /etc/nginx/.htpasswd;`. Так закрывают тестовые площадки, которые «временно» смотрят в интернет.

**12. Доверие внутреннему УЦ на Windows.**
```cmd
certutil -addstore -f Root lab-ca.crt
certutil -store Root | findstr /i vektor
```
GUI: `certlm.msc` → Доверенные корневые центры сертификации → Импорт. В домене правильнее раскатывать сертификат через GPO (Конфигурация компьютера → Политики → Параметры Windows → Параметры безопасности → Политики открытых ключей).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **WEB01** (Ubuntu Server 24.04, 10.10.20.10) — создан на треке L2, nginx установлен, в DMZ.
- **GW01** (Debian 12, 10.10.10.3 / 10.10.20.1) — маршрутизатор между офисной сетью и DMZ, nftables настроен на L2.
- **DC01** (10.10.10.10) — AD DS и DNS, зона `corp.vektor.lab` существует.
- **WIN11-CL01** — рабочее место ivanov, получает адрес по DHCP, введено в домен.
- Учётки: `adm.student` (админ домена), единый пароль стенда `Lab#2026-vektor`.
- Если маршрутизации с 10.10.10.0/24 в DMZ ещё нет — этот шаг придётся добить на месте (сниппет в шаге 2).

### Снапшот
В VMware Workstation на каждой затрагиваемой ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-sysadmin-067`. Машины: **WEB01**, **GW01**, **DC01**, **WIN11-CL01**. Пока ВМ включена, снапшот памяти можно не делать — достаточно состояния диска.

### Шаги

1. **GW01 — проверяем маршрутизацию в DMZ.**
   ```bash
   ip -br a
   sudo nft list ruleset
   ```
   Ожидаем: два интерфейса с адресами 10.10.10.3 и 10.10.20.1, в ruleset есть forward и masquerade для 10.10.10.0/24 → 10.10.20.0/24. Если нет — добавляем в `/etc/nftables.conf` (имена интерфейсов подставь свои, смотри `ip -br a`):
   ```nft
   table inet labfw {
     chain forward {
       type filter hook forward priority 0; policy drop;
       iifname "ens33" oifname "ens34" ip saddr 10.10.10.0/24 \
         ip daddr 10.10.20.10 tcp dport { 80, 443 } accept
       iifname "ens34" oifname "ens33" ct state established,related accept
     }
     chain postrouting {
       type nat hook postrouting priority srcnat; policy accept;
       ip saddr 10.10.10.0/24 ip daddr 10.10.20.10 oifname "ens34" masquerade
     }
   }
   ```
   Затем `sudo nft -f /etc/nftables.conf && sudo systemctl enable --now nftables`. Ожидаем: `sudo nft list ruleset` показывает таблицу.

2. **DC01 — DNS-запись для портала.** `dnsmgmt.msc` → Forward Lookup Zones → `corp.vektor.lab` → New Host (A) → имя `portal`, IP `10.10.20.10`. На WIN11-CL01 проверяем:
   ```cmd
   nslookup portal.corp.vektor.lab
   ```
   Ожидаем: `Address: 10.10.20.10`. Если нет — `ipconfig /flushdns`.

3. **WEB01 — ставим Apache как бэкенд на loopback.**
   ```bash
   sudo apt update && sudo apt install -y apache2
   sudo sed -i 's/^Listen 80$/Listen 127.0.0.1:8080/' /etc/apache2/ports.conf
   sudo mkdir -p /var/www/portal
   echo '<h1>Vektor Portal</h1>' | sudo tee /var/www/portal/index.html
   ```
   Создаём `/etc/apache2/sites-available/portal.conf`:
   ```apache
   <VirtualHost 127.0.0.1:8080>
       ServerName portal.corp.vektor.lab
       DocumentRoot /var/www/portal
       <Directory /var/www/portal>
           Require all granted
           AllowOverride None
       </Directory>
       ErrorLog ${APACHE_LOG_DIR}/portal_error.log
       CustomLog ${APACHE_LOG_DIR}/portal_access.log combined
   </VirtualHost>
   ```
   ```bash
   sudo a2dissite 000-default && sudo a2ensite portal && sudo apache2ctl configtest && sudo systemctl restart apache2
   curl -I http://127.0.0.1:8080/
   ```
   Ожидаем: `HTTP/1.1 200 OK`. Порт 8080 наружу не смотрит — только loopback.

4. **WEB01 — внутренний УЦ и сертификат для портала.**
   ```bash
   sudo mkdir -p /opt/lab-ca /etc/nginx/tls && cd /opt/lab-ca
   sudo openssl req -x509 -newkey rsa:4096 -days 3650 -nodes \
     -keyout lab-ca.key -out lab-ca.crt -subj "/CN=Vektor Lab Root CA/O=Vektor"
   sudo openssl req -newkey rsa:2048 -nodes -keyout /etc/nginx/tls/portal.key \
     -out /etc/nginx/tls/portal.csr -subj "/CN=portal.corp.vektor.lab" \
     -addext "subjectAltName=DNS:portal.corp.vektor.lab,DNS:web01.corp.vektor.lab"
   sudo openssl x509 -req -in /etc/nginx/tls/portal.csr -CA lab-ca.crt -CAkey lab-ca.key \
     -CAcreateserial -days 397 -copy_extensions copy -out /etc/nginx/tls/portal.crt
   sudo chmod 600 /etc/nginx/tls/portal.key
   ```
   Ожидаем два файла в `/etc/nginx/tls/`; проверка — команда №7 из раздела 4. `-copy_extensions copy` требует OpenSSL 3.x (в Ubuntu 24.04 он и есть); на 1.1.1 используй `-extfile` с секцией SAN **[ПРОВЕРИТЬ под свою версию]**.

5. **WEB01 — конфиг nginx: терминация TLS + reverse proxy.** В `/etc/nginx/conf.d/` кладём общий лимит, в `/etc/nginx/sites-available/portal`:
   ```nginx
   limit_req_zone $binary_remote_addr zone=portal_rl:10m rate=10r/s;

   server {
       listen 80;
       server_name portal.corp.vektor.lab;
       return 301 https://$host$request_uri;
   }

   server {
       listen 443 ssl http2;
       server_name portal.corp.vektor.lab;

       ssl_certificate     /etc/nginx/tls/portal.crt;
       ssl_certificate_key /etc/nginx/tls/portal.key;
       ssl_protocols       TLSv1.2 TLSv1.3;
       ssl_session_cache   shared:SSL:10m;

       add_header Strict-Transport-Security "max-age=31536000" always;
       add_header X-Content-Type-Options nosniff always;
       add_header X-Frame-Options SAMEORIGIN always;

       client_max_body_size 20m;
       access_log /var/log/nginx/portal_access.log;
       error_log  /var/log/nginx/portal_error.log warn;

       location / {
           limit_req zone=portal_rl burst=20 nodelay;
           proxy_pass http://127.0.0.1:8080;
           proxy_http_version 1.1;
           proxy_set_header Host              $host;
           proxy_set_header X-Real-IP         $remote_addr;
           proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_read_timeout 60s;
       }
   }
   ```
   `http2` в директиве `listen` — для nginx 1.24 (Ubuntu 24.04); в nginx 1.25.1+ он вынесен в отдельную директорию `http2 on;` **[ПРОВЕРИТЬ версию у себя: `nginx -v`]**. Активируем:
   ```bash
   sudo ln -s /etc/nginx/sites-available/portal /etc/nginx/sites-enabled/portal
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```

6. **WIN11-CL01 — доверяем внутреннему УЦ.** Копируем `/opt/lab-ca/lab-ca.crt` на WIN11-CL01 (можно через общую папку `\\FS01\Obshchee`), в cmd от администратора:
   ```cmd
   certutil -addstore -f Root lab-ca.crt
   ```
   Открываем `https://portal.corp.vektor.lab/` в браузере. Ожидаем: страница «Vektor Portal», замок зелёный, издатель «Vektor Lab Root CA». Проверяем редирект: `http://portal.corp.vektor.lab/` уводит на https.

7. **WEB01 — ставим WAF на Apache.**
   ```bash
   sudo apt install -y libapache2-mod-security2 modsecurity-crs
   sudo cp /etc/modsecurity/modsecurity.conf-recommended /etc/modsecurity/modsecurity.conf
   sudo sed -i 's/^SecRuleEngine .*/SecRuleEngine On/' /etc/modsecurity/modsecurity.conf
   sudo sed -i 's/^SecAuditEngine .*/SecAuditEngine RelevantOnly/' /etc/modsecurity/modsecurity.conf
   sudo a2enmod security2 && sudo systemctl restart apache2
   sudo apache2ctl -M | grep -i security
   ```
   Проверяем, что CRS подключён: в `/etc/apache2/mods-enabled/security2.conf` должна быть строка вида `IncludeOptional /usr/share/modsecurity-crs/*.load` **[ПРОВЕРИТЬ путь: в вашей сборке CRS может лежать в `/etc/modsecurity/crs/`]**. Ожидаем `security2_module (shared)`.

8. **Проверяем, что WAF реально блокирует.**
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8080/?id=1%27%20OR%20%271%27%3D%271"
   sudo tail -n 40 /var/log/apache2/modsec_audit.log | grep -E 'id "942|severity'
   ```
   Ожидаем `403` и запись в `modsec_audit.log`. Через браузер на WIN11-CL01 открываем `https://portal.corp.vektor.lab/?id=1' OR '1'='1` — тоже 403 (nginx прокинул, Apache заблокировал). Если 200 — проверь `SecRuleEngine` и `SecDefaultAction` в `crs-setup.conf`.

9. **Демонстрация Let's Encrypt (без реального выпуска).** На WEB01:
   ```bash
   sudo apt install -y certbot python3-certbot-nginx
   sudo certbot --nginx -d portal.example.ru --dry-run
   sudo certbot renew --dry-run
   ```
   Ожидаем сообщение о неудачной валидации: домена `example.ru` у нас нет, наружу 80-й порт не смотрит. Это **нормальный** результат для стенда — команда показывает весь путь ACME. Записываем в `stand-log.md`: «Let's Encrypt требует публичный домен и доступность 80/443 из интернета; на стенде используем внутренний УЦ».

10. **Нагрузочная проверка лимита запросов.** С WIN11-CL01 в PowerShell:
    ```powershell
    1..30 | ForEach-Object -Parallel { curl.exe -s -o NUL -w "%{http_code} " https://portal.corp.vektor.lab/ } -ThrottleLimit 30
    ```
    Ожидаем часть `503` — сработал `limit_req`. Смотрим `portal_error.log`: `limiting requests, excess: ...`.

11. **Фиксируем результат.** В `stand-log.md`: DNS-запись, порт 8080 только на loopback, файлы TLS, включённый ModSecurity с CRS, доверенный корневой сертификат на WIN11-CL01.

### Проверка
- [ ] `nslookup portal.corp.vektor.lab` с WIN11-CL01 отдаёт 10.10.20.10.
- [ ] `curl -I http://portal.corp.vektor.lab/` возвращает `301` на https.
- [ ] В браузере WIN11-CL01 портал открывается без предупреждений о сертификате.
- [ ] `openssl s_client … | grep "Verify return code"` → `0 (ok)`.
- [ ] `ss -tlnp` на WEB01: 443 — nginx (0.0.0.0), 8080 — apache2 (127.0.0.1).
- [ ] `sudo apache2ctl -M | grep security2` — модуль загружен.
- [ ] Запрос с `' OR '1'='1` даёт 403 и запись в `modsec_audit.log`.
- [ ] Серия из 30 быстрых запросов даёт несколько 503 (лимит работает).
- [ ] В `stand-log.md` появилась запись об изменениях.

### Сломай и почини
**Пользователь:** petrova (HR). **ВМ:** WIN10-CL02. **Что написала:** «Портал был открыт, а теперь пишет 502 Bad Gateway. Мне через час отправлять отчёт, а формы не грузятся». **Срочность:** высокая.

**Воспроизводим поломку** на WEB01:
```bash
sudo systemctl stop apache2
```
**Диагностика по порядку (сверху вниз, не перескакивая):**
1. `curl -I http://127.0.0.1:8080/` на WEB01 → `Connection refused`. Значит, проблема не в сети и не в nginx.
2. `sudo ss -tlnp | grep 8080` → пусто. Бэкенд не слушает.
3. `systemctl status apache2 --no-pager` → `inactive (dead)`.
4. `sudo tail -n 50 /var/log/apache2/error.log` → причина падения (например, синтаксис после правки конфига).
5. `sudo journalctl -u apache2 -n 50 --no-pager` → кто и когда остановил/уронил сервис.
6. Параллельно убеждаемся, что nginx жив: `sudo tail -n 20 /var/log/nginx/portal_error.log` → `connect() failed (111: Connection refused) while connecting to upstream`. Это подтверждает: 502 = «прокси не смог достучаться до бэкенда», а не «упал nginx».

**Решение:** устранить причину (исправить конфиг, если он битый), затем `sudo apache2ctl configtest && sudo systemctl start apache2`, проверить `curl -I http://127.0.0.1:8080/` и `curl -I https://portal.corp.vektor.lab/` (200). Плюс на будущее: включить `apache2` в автозапуск `sudo systemctl enable apache2` и добавить проверку порта 8080 в мониторинг.

**Ответ пользователю:** «Мария, портал восстановлен, 502 была из-за остановки внутреннего сервиса приложения. Сейчас формы открываются, проверьте, пожалуйста, отправку отчёта. Если снова увидите 502 — пришлите скриншот и время, я посмотрю логи».

**Закрытие заявки:** категория «Веб-сервисы → 502», решение «остановлен бэкенд apache2», время восстановления, запись в постмортем: «добавить health-check бэкенда на 127.0.0.1:8080 в Zabbix (MON01)».

### Что изменилось на стенде
- **GW01:** правила nftables forward/NAT из 10.10.10.0/24 в 10.10.20.10 по tcp 80/443 (если их не было).
- **DC01:** A-запись `portal.corp.vektor.lab` → 10.10.20.10 в зоне `corp.vektor.lab`.
- **WEB01:** установлен apache2, слушает только 127.0.0.1:8080, docroot `/var/www/portal`, сайт `portal.conf`; nginx слушает 80 (redirect) и 443, конфиг `/etc/nginx/sites-available/portal`, логи `/var/log/nginx/portal_*.log`; внутренний УЦ в `/opt/lab-ca/` (CA-ключ и сертификат), серверный сертификат в `/etc/nginx/tls/portal.crt|key` (SAN: portal.corp.vektor.lab, web01.corp.vektor.lab); установлен libapache2-mod-security2 + modsecurity-crs, `SecRuleEngine On`, аудит в `/var/log/apache2/modsec_audit.log`.
- **WIN11-CL01:** корневой сертификат «Vektor Lab Root CA» в хранилище «Доверенные корневые ЦС» локального компьютера.
- **Не менялось:** FS01, WIN10-CL02, UBU01, ASTRA01, PVE01.

### Откат
VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-sysadmin-067` → **Revert**. Порядок: сначала WIN11-CL01, затем WEB01, затем GW01 и DC01 (если откатываешь DC01 — после отката проверь, что DNS-зона в том состоянии, которое ожидают следующие главы). Если откатывать не хочется — оставь как есть, но обязательно допиши в `stand-log.md`, что именно осталось.

## 6. Ошибки новичков и безопасность

1. **`systemctl restart nginx` вместо `reload` на бою** → обрыв активных соединений, пользователи видят ошибки при загрузке файлов. → Всегда `nginx -t` + `reload`; restart — только при смене модулей и в окно обслуживания.
2. **Бэкенд слушает `0.0.0.0:8080` и смотрит в сеть** → приложение доступно в обход TLS, WAF и лимитов, любой в DMZ ходит напрямую. → Бэкенд только на `127.0.0.1` или внутренний интерфейс + фаервол на порт.
3. **Самоподписанный сертификат «навсегда» без SAN** → браузеры ругаются, часть клиентов (мобильные, curl, Java) отказывается работать: SAN обязателен. → SAN с нужными DNS-именами, распространение корневого сертификата через GPO.
4. **Let's Encrypt для внутреннего имени** → бесконечные неудачные валидации, расход лимитов, потеря времени. → Внутренние имена — внутренний УЦ; Let's Encrypt — только публичные домены. Для закрытых сервисов есть DNS-01 и wildcard.
5. **WAF включён сразу в `On` на проде без прогона** → блокировка легитимных форм (особенно где вводят апострофы, файлы, длинные JSON), поток заявок «сайт не работает». → Сначала `DetectionOnly` 1–2 недели, разбор аудита, тюнинг исключений, потом `On`.
6. **Нет автопродления и мониторинга срока сертификата** → сайт «упал в субботу» из-за истёкшего сертификата. → Таймер `certbot.timer`, алерт за 21–30 дней, `openssl x509 -enddate` в мониторинге.
7. **`--dry-run` не используют, гоняют боевой выпуск по кругу** → упираются в rate limit Let's Encrypt и не могут выпустить сертификат, когда он реально нужен. → Все эксперименты — на staging.
8. **Логи nginx/Apache растут без ротации или содержат чувствительные данные** → диск переполняется; в логах токены в URL и куки. → `logrotate` (для apt-пакетов уже настроен), вычистка query string в `log_format`, ограничение доступа к каталогу логов.

### Безопасность
В бою недопустимо: держать `SecRuleEngine DetectionOnly` «временно» годами; отключать WAF-правило целиком вместо точечного исключения; выкладывать дамп конфига `nginx -T` в тикет (там пути и ключи); хранить приватный ключ с правами 644; ставить `ssl_protocols TLSv1 TLSv1.1`; закрывать staging basic-auth паролем от домена. Отдельно про РФ: для публичных сервисов проверяй требования 152-ФЗ к размещению персональных данных, а для сайтов, где нужен российский сертификат, смотри бесплатные TLS-сертификаты от УЦ Минцифры и цепочки доверия в отечественных браузерах **[ПРОВЕРИТЬ актуальный порядок получения]**. Из отечественных и импортозамещающих вариантов: **Angie** (nginx-совместимый форк, реестр российского ПО **[ПРОВЕРИТЬ статус]**), **PT Application Firewall**, **Wallarm**, а также WAF в составе UTM/межсетевых экранов. Apache и nginx — свободное ПО, доступное без ограничений; сложности с загрузкой из РФ возможны, легальная альтернатива — пакеты из репозиториев РЕД ОС / Astra Linux.

## 7. Вопросы на собеседовании

**1. Чем nginx отличается от Apache и что выберете для reverse proxy?**
Ответ: nginx событийный, один воркер обслуживает много соединений, отлично держит статику и проксирование, конфиг декларативный, `.htaccess` нет. Apache — MPM prefork/event, богатая модульность, mod_php, mod_security, `.htaccess` для shared-хостинга. Для reverse proxy и TLS-терминации беру nginx, для приложения на PHP с плотной модульной обвязкой или WAF на ModSecurity — Apache за ним. Часто комбинирую: nginx на фронте, Apache сзади.
*Что хочет услышать:* понимание архитектуры (события vs процессы), а не «nginx быстрее».
*Красный флаг:* «Apache устарел, им никто не пользуется» или путаница в том, кто терминирует TLS.

**2. Как работает Let's Encrypt и что делать, если 80-й порт закрыт?**
Ответ: ACME-протокол, клиент доказывает владение доменом. HTTP-01 требует доступность `/.well-known/acme-challenge/` по 80 порту из интернета. Если 80 закрыт — DNS-01 (TXT-запись `_acme-challenge`, работает для wildcard, нужен доступ к API DNS-провайдера), либо TLS-ALPN-01 на 443. Сертификат на 90 дней, продление автоматическое по таймеру, `--dry-run` для проверки, staging-окружение для тестов из-за rate limit.
*Что хочет услышать:* знание типов challenge, автоматизации продления и лимитов.
*Красный флаг:* «продлеваю руками раз в 3 месяца по напоминанию в календаре».

**3. Что такое WAF и чем он отличается от сетевого фаервола?**
Ответ: сетевой фаервол фильтрует по L3/L4 — адреса, порты, состояния. WAF работает на L7 и разбирает содержимое: URL, параметры, тело, заголовки, куки. Ловит SQLi, XSS, path traversal, RCE, сканеры — по сигнатурам (OWASP CRS) или поведенческим моделям. Режимы DetectionOnly и On, anomaly scoring, paranoia level. WAF не заменяет исправление уязвимостей в коде, но даёт virtual patching, пока патч не вышел. Главная боль — ложные срабатывания, поэтому начинаем с детекта.
*Что хочет услышать:* L7 vs L3/L4, сигнатуры, риск false positive, staging-прогон.
*Красный флаг:* «WAF полностью защищает от взлома» или «включил и забыл».

**4. Какие заголовки прокидывать бэкенду и зачем нужен `X-Forwarded-For`?**
Ответ: `Host` (чтобы приложение строило правильные ссылки и виртуальные хосты), `X-Real-IP` и `X-Forwarded-For` (реальный IP клиента — иначе в логах приложения и в аудите WAF будет 127.0.0.1, и блокировки по IP не сработают), `X-Forwarded-Proto` (приложение понимает, что снаружи https, и не делает редирект на http). Важно: приложению нельзя слепо доверять входящему `X-Forwarded-For` от клиента — прокси должен перезаписывать его (`$proxy_add_x_forwarded_for` добавляет, но доверять стоит только цепочке своих прокси).
*Что хочет услышать:* понимание, что за прокси теряется IP клиента и ломается https-детект.
*Красный флаг:* «достаточно `proxy_pass`, остальное неважно».

**5. Nginx отдаёт 502 — ваши действия?**
Ответ: 502 = прокси не смог получить ответ от upstream. Смотрю `error.log` nginx (`connect() failed`, `upstream timed out`), проверяю `ss -tlnp` и `curl` на адрес бэкенда, статус сервиса, `journalctl -u <backend>`, лимиты (`worker_connections`, `proxy_read_timeout`), не упал ли бэкенд по памяти. Диагностика идёт снизу вверх от бэкенда к клиенту, а не наоборот. Отдельно проверяю, что не сменился порт/адрес после деплоя.
*Что хочет услышать:* чёткий порядок диагностики и понимание, что 502 — это не «nginx сломался».
*Красный флаг:* «перезапущу nginx и посмотрим» как единственный шаг.

## 8. Источники (официальная документация)

- **nginx.org** — раздел «Documentation», модули `ngx_http_proxy_module`, `ngx_http_ssl_module`, `ngx_http_limit_req_module`. Поиск: `nginx reverse proxy configuration example`, `nginx limit_req directive`.
- **httpd.apache.org** — «Documentation → 2.4», разделы `mod_proxy`, `mod_ssl`, `mod_security`. Поиск: `apache mod_proxy reverse proxy guide`, `apache2ctl options`.
- **letsencrypt.org** — «Documentation»: Challenge Types, Rate Limits, ACME Protocol. Поиск: `lets encrypt challenge types http-01 dns-01`, `lets encrypt rate limits`.
- **certbot.eff.org** — инструкции по установке и использованию certbot для nginx и apache. Поиск: `certbot nginx instructions`, `certbot renew dry-run`.
- **github.com/coreruleset** — репозиторий OWASP ModSecurity Core Rule Set, файл `crs-setup.conf.example`, паранойя-уровни. Поиск: `OWASP CRS paranoia level setup`, `crs rule 942100`.
- **owasp.org** — OWASP Top 10, OWASP ModSecurity Core Rule Set project page. Поиск: `OWASP Top 10 web application security risks`.
- **ubuntu.com/server/docs** — документация по nginx и apache на Ubuntu. Поиск: `ubuntu server nginx documentation`.
- **angie.software** — документация nginx-совместимого форка Angie **[ПРОВЕРИТЬ актуальность статуса в реестре российского ПО]**. Поиск: `angie web server documentation`.
- Реестр российского ПО — официальный портал реестра **[ПРОВЕРИТЬ актуальный домен]**; поиск: `реестр российского ПО официальный сайт`.
- Раздел про TLS-сертификаты УЦ Минцифры — сайт УЦ **[ПРОВЕРИТЬ актуальный адрес и порядок выдачи]**; поиск: `сертификаты Минцифры TLS получение`.
