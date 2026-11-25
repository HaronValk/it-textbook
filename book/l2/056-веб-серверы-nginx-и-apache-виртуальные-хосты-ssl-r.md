# Веб-серверы: Nginx и Apache, виртуальные хосты, SSL, reverse proxy, логи

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №4471.** Пользователь: Петрова (WIN10-CL02, HR). Текст: «Здравствуйте! Открываю portal.vektor.lab — браузер пишет, что соединение не защищено, а если нажать „всё равно перейти“ — белая страница. Складской раздел sklad.vektor.lab открывается нормально. Раньше работало. Нужно до обеда, у меня приказ на увольнение формировать». Срочность: средняя.

Вот так веб-сервер и появляется в жизни L2-инженера: не «поставь nginx», а «половина сайтов работает, половина нет, и разберись почему». В компании «Вектор» на веб-сервере живут внутренний портал, складская система, портал самообслуживания, страница публикации 1С и точка входа в Zabbix. Всё это — виртуальные хосты, сертификаты, проксирование на бэкенды и логи, по которым ты находишь причину за пять минут вместо часа.

После этой главы ты должен уметь: поднять nginx и Apache на одном хосте без конфликта портов, развести несколько сайтов по именам, выпустить и подключить сертификат, настроить reverse proxy на бэкенд, читать access/error-логи обоих серверов и по коду ответа (502/504/403/500) сразу понимать, в каком слое искать проблему.

## 2. Теория «под капотом»

HTTP — протокол прикладного уровня (L7 модели OSI). Он работает поверх TCP (L4), порт 80 для HTTP и 443 для HTTPS. HTTPS — это тот же HTTP, но внутри TLS-туннеля: сначала клиент и сервер договариваются о шифровании (TLS handshake: версия, шифронабор, обмен сертификатом, проверка подписи), и только потом идёт обычный HTTP-запрос. Сертификат подтверждает, что сервер — это действительно тот, за кого себя выдаёт; подписан он удостоверяющим центром (CA), которому доверяет клиент.

**Виртуальный хост** — механизм, когда один IP:порт обслуживает несколько сайтов. Сервер различает их по заголовку `Host` в HTTP-запросе (name-based virtual hosting). Для HTTPS есть нюанс: заголовок `Host` зашифрован, поэтому имя сайта передаётся отдельно, в открытом расширении TLS — **SNI** (Server Name Indication). Без SNI на одном IP был бы только один сертификат.

**Reverse proxy** (обратный прокси) — сервер, который принимает запрос от клиента и сам идёт к внутреннему приложению («upstream»), а ответ возвращает клиенту. Это нужно, чтобы: спрятать бэкенд (например, 1С или приложение на Python) во внутренней сети; терминировать TLS в одном месте; кэшировать статику; балансировать нагрузку; не давать приложению слушать 443 от root. Не путать с forward proxy — тот, наоборот, ходит в интернет *от имени клиента*.

Где что лежит в стенде:

```text
petrova (WIN10-CL02, 10.10.10.1xx)
   │  https://sklad.vektor.lab  (DNS-запрос → DC01 10.10.10.10)
   ▼
GW01 10.10.10.3  — nftables DNAT: tcp 80/443 → 10.10.20.10
   ▼  DMZ 10.10.20.0/24
WEB01 10.10.20.10
   ├─ nginx :443  TLS-терминация + виртуальные хосты (SNI)
   │     ├─ portal.vektor.lab → статика из /var/www/portal
   │     └─ sklad.vektor.lab  → proxy_pass http://127.0.0.1:8080
   └─ apache2 :8080  бэкенд, DocumentRoot /var/www/sklad
```

Про архитектуру двух серверов, коротко и по делу. **Apache** — процессная/потоковая модель: MPM-модули (`prefork` — процесс на соединение, `worker` — потоки, `event` — современный, по умолчанию в Debian/Ubuntu). Много модулей, `.htaccess`, гибкие правила — исторически стандарт для 1С, PHP и «тяжёлых» приложений. **Nginx** — событийная модель, один воркер держит тысячи соединений, отлично отдаёт статику и работает как прокси/балансировщик. В 2026 году в РФ стоит знать и **Angie** — российский форк nginx с совместимым конфигом (проверь актуальный статус и лицензию на angie.software [ПРОВЕРИТЬ]).

Конфиги: nginx — `/etc/nginx/nginx.conf` плюс подключаемые `sites-available/` → симлинки в `sites-enabled/`; Apache (Debian/Ubuntu) — `/etc/apache2/apache2.conf`, `ports.conf`, сайты в `sites-available/` + `a2ensite/a2enmod`. Логи: у nginx `/var/log/nginx/access.log` и `error.log`, у Apache `/var/log/apache2/access.log` и `error.log`; плюс всё дублируется в journald (`journalctl -u nginx`). Ротацию делает logrotate (`/etc/logrotate.d/nginx`, `/etc/logrotate.d/apache2`).

## 3. Аналогия из жизни

Представь бизнес-центр. На входе — ресепшн (nginx). Ты говоришь: «Я в бухгалтерию» — тебя отправляют на 3-й этаж, «Я в складской отдел» — на 5-й. Ресепшн сам не бухгалтер и не кладовщик, он только принимает посетителей, проверяет пропуск (TLS-сертификат — это пропуск, выданный охраной здания, то есть CA) и направляет по нужному этажу по названию организации в твоём пропуске (заголовок `Host` / SNI). Если на 5-м этаже закрыто, ресепшн честно говорит «туда сейчас нельзя» — это 502. А журнал посещений на ресепшене — это access.log: кто, когда, куда шёл и с каким результатом.

Аналогия ломается на кэшировании и keep-alive: ресепшн не запоминает ответы и не держит дверь открытой между посетителями, а nginx именно это и делает.

## 4. Практика и команды

**1. Кто слушает порты.**
```bash
sudo ss -tlnp | grep -E ':(80|443|8080)\s'
```
`-t` TCP, `-l` listening, `-n` без имён, `-p` процесс. Вывод:
```text
LISTEN 0 511 0.0.0.0:80   0.0.0.0:*  users:(("nginx",pid=1210,fd=6))
LISTEN 0 511 0.0.0.0:443  0.0.0.0:*  users:(("nginx",pid=1210,fd=7))
LISTEN 0 511 0.0.0.0:8080 0.0.0.0:*  users:(("apache2",pid=1455,fd=4))
```
Смотрим: не занял ли Apache порт 80 (тогда nginx не стартует), слушает ли бэкенд только loopback или всё на свете.

**2. Проверка и дамп конфига nginx.**
```bash
sudo nginx -t          # синтаксис + какие файлы прочитаны
sudo nginx -T | less   # полный итоговый конфиг со всеми include
```
`nginx -T` — спасение: видно, какой `server`-блок реально победил при конфликте `server_name`.

**3. Применение конфига без разрыва соединений.**
```bash
sudo nginx -s reload      # то же, что systemctl reload nginx
sudo systemctl status nginx --no-pager
```
`reload` — graceful: старые воркеры дорабатывают текущие запросы. `restart` рвёт соединения — на боевом так не делают.

**4. Виртуальные хосты Apache и тест конфига.**
```bash
sudo apache2ctl configtest
sudo apache2ctl -S        # какой vhost на каком IP:port и с каким именем
```
`-S` (он же `-t -D DUMP_VHOSTS`) выдаёт `port 8080 namevhost localhost (/etc/apache2/sites-enabled/000-default.conf:1)` — сразу видно, включён ли сайт и куда указывает `DocumentRoot`.

**5. Проверка сайта с клиента curl.**
```bash
curl -I https://portal.vektor.lab/            # только заголовки
curl -vk https://10.10.20.10/                 # -v подробно, -k не проверять сертификат (ТОЛЬКО для диагностики)
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' https://sklad.vektor.lab/
```
Смотрим код ответа (`200/301/403/502`), `Server:`, `Location:` при редиректе, и время — 30 секунд вместо 0.05 явно указывают на таймаут до upstream.

**6. Обход DNS при проверке (до того как появилась A-запись).**
```bash
curl --resolve portal.vektor.lab:443:10.10.20.10 https://portal.vektor.lab/ -I
```
`--resolve` подменяет DNS-ответ и не трогает SNI/`Host` — именно то, что нужно, чтобы проверить vhost до правки зоны.

**7. Сертификат: что реально отдаёт сервер.**
```bash
openssl s_client -connect 10.10.20.10:443 -servername sklad.vektor.lab </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```
`-servername` задаёт SNI — без него получишь дефолтный сертификат и будешь полчаса искать, «почему не тот сайт». В выводе важны: `subject` (CN), `issuer` (кто подписал), `notAfter` (не истёк ли), и **SAN** — современные браузеры смотрят только на subjectAltName, CN давно игнорируют.

**8. Выпуск учебного самоподписанного сертификата.**
```bash
sudo mkdir -p /etc/nginx/ssl && cd /etc/nginx/ssl
sudo openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout vektor.key -out vektor.crt \
  -subj "/C=RU/O=Vektor Lab/CN=portal.vektor.lab" \
  -addext "subjectAltName=DNS:portal.vektor.lab,DNS:sklad.vektor.lab"
sudo chmod 600 vektor.key && sudo chmod 644 vektor.crt
```
`-x509` самоподписанный, `-nodes` без пароля на ключ (иначе сервис не сможет стартовать без ввода), `-addext subjectAltName` — оба имени. На стенде это нормально; в боевой среде — только внутренний CA или Let's Encrypt (для внешних доменов).

**9. Логи: два сервера, два места.**
```bash
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
sudo tail -f /var/log/apache2/error.log
sudo journalctl -u nginx --since "30 min ago" -p warning
```
`-p warning` фильтр по важности. В `error.log` nginx строка `connect() failed (111: Connection refused) while connecting to upstream` — это ровно наш завтрашний инцидент.

**10. Ротация логов — проверка без применения.**
```bash
sudo logrotate -d /etc/logrotate.d/nginx
```
`-d` = debug/dry-run: показывает, что сделает, но ничего не трогает. Если лог не ротируется — через месяц кончится место на разделе `/var`.

**11. SELinux/AppArmor на пути прокси (RHEL-семейство, Astra с SELinux).**
```bash
getsebool -a | grep httpd_can_network
sudo setsebool -P httpd_can_network_connect on   # ровно то, что нужно для reverse proxy
```
`-P` — навсегда (в политику). В Ubuntu/Astra роль играет AppArmor: там смотри `aa-status`, `dmesg | grep -i denied` и профиль в `/etc/apparmor.d/` — набор профилей и их включённость отличаются по версиям, проверяй на месте [ПРОВЕРИТЬ].

## 5. Лабораторная работа

### Предусловия
Нужны ВМ: **DC01** (AD DS + DNS, 10.10.10.10), **GW01** (Debian 12, 10.10.10.3 и 10.10.20.1, nftables, ip_forward), **WEB01** (Ubuntu Server 24.04, 10.10.20.10, шлюз по умолчанию 10.10.20.1), **UBU01** (10.10.10.30, для тестов), **WIN11-CL01 / WIN10-CL02** (браузер). Учётки — `adm.student` для домена, локальный `student` с sudo на Linux-машинах. Пароль стенда единый.

Если GW01 или WEB01 ещё не созданы (появляются на треке L2), сделай минимальную настройку: GW01 — Debian 12, два адаптера (VMnet8 → 10.10.10.3/24 gw 10.10.10.2, VMnet10 → 10.10.20.1/24), `sysctl -w net.ipv4.ip_forward=1` и запись в `/etc/sysctl.d/`, пакет `nftables`. WEB01 — Ubuntu Server 24.04, один адаптер VMnet10, адрес 10.10.20.10/24, шлюз 10.10.20.1. Маршрутизацию и bonding разбирали в уроке 53 — если что-то забыл, вернись туда.

### Снапшот
На хосте VMware Workstation: выдели DC01, GW01, WEB01 → правый клик → **Snapshot → Take Snapshot**, имя `before-l2-056`. Повтори для каждой из трёх ВМ (одно имя — по одному снапшоту на машину). Для ВМ, которая запущена, VMware предложит включить память в снапшот — для этого урока достаточно снапшота «диск» (галочка памяти не нужна).

### Шаги

1. **GW01 — контроль сети.** Проверь, что маршрутизация уже живая:
   ```bash
   sysctl net.ipv4.ip_forward
   ip -br a
   ping -c2 10.10.20.10 && ping -c2 10.10.10.10
   ```
   Ожидаемо: `net.ipv4.ip_forward = 1`, два интерфейса с адресами `.3` и `.1`, оба ping успешны. Если ping до 10.10.20.10 не идёт — сначала закрывай маршрутизацию (урок 53), веб-сервер тут ни при чём.

2. **DC01 — A-записи.** DNS Manager (`dnsmgmt.msc`) → Forward Lookup Zones → `corp.vektor.lab` → New Host (A): `portal` → 10.10.10.3, `sklad` → 10.10.10.3. Через PowerShell:
   ```powershell
   Add-DnsServerResourceRecordA -Name portal -ZoneName corp.vektor.lab -IPv4Address 10.10.10.3
   Add-DnsServerResourceRecordA -Name sklad  -ZoneName corp.vektor.lab -IPv4Address 10.10.10.3
   ```
   Ожидаемо: записи видны в зоне. На клиентах — `ipconfig /flushdns` и `nslookup portal.vektor.lab` → ответ 10.10.10.3.

3. **GW01 — публикация наружу из DMZ (DNAT).**
   ```bash
   cat > /root/vektor-nat.nft <<'EOF'
   table ip vektor_nat {
     chain prerouting {
       type nat hook prerouting priority dstnat; policy accept;
       ip saddr 10.10.10.0/24 tcp dport { 80, 443 } dnat to 10.10.20.10
     }
     chain forward {
       type filter hook forward priority filter; policy accept;
       ip daddr 10.10.20.10 tcp dport { 80, 443 } accept
       ip saddr 10.10.20.10 accept
     }
   }
   EOF
   nft -f /root/vektor-nat.nft
   nft list table ip vektor_nat
   ```
   Политика `accept` в forward — **учебное упрощение**; в бою так делать нельзя, правила пишутся «запрещено всё, кроме явно разрешённого». Для сохранения после ребута добавь `include "/root/vektor-nat.nft"` в `/etc/nftables.conf` и `systemctl enable --now nftables` (проверь синтаксис include в своей версии [ПРОВЕРИТЬ]).

4. **WEB01 — установка двух серверов без конфликта портов.**
   ```bash
   sudo apt update && sudo apt install -y nginx apache2
   sudo sed -i 's/^Listen 80$/Listen 8080/' /etc/apache2/ports.conf
   sudo sed -i 's/<VirtualHost \*:80>/<VirtualHost *:8080>/' /etc/apache2/sites-available/000-default.conf
   sudo systemctl restart apache2 && sudo systemctl restart nginx
   sudo ss -tlnp | grep -E ':(80|8080|443)\s'
   ```
   Ожидаемо: nginx на 80, apache2 на 8080, конфликта нет. Если apache2 не стартует — `journalctl -u apache2 -n 30`, почти наверняка остался `Listen 80`.

5. **WEB01 — контент и бэкенд.**
   ```bash
   sudo mkdir -p /var/www/portal /var/www/sklad
   echo "<h1>Portal Vektor</h1>" | sudo tee /var/www/portal/index.html
   echo "<h1>Sklad Vektor (apache backend)</h1>" | sudo tee /var/www/sklad/index.html
   sudo sed -i 's#DocumentRoot /var/www/html#DocumentRoot /var/www/sklad#' /etc/apache2/sites-available/000-default.conf
   sudo systemctl reload apache2
   sudo curl -s http://127.0.0.1:8080/ | head -1
   ```
   Ожидаемо: `curl` на loopback:8080 возвращает заголовок sklad. Значит бэкенд живой.

6. **WEB01 — сертификат.** Выполни команду из раздела 4, пункт 8. Проверь:
   ```bash
   openssl x509 -in /etc/nginx/ssl/vektor.crt -noout -subject -ext subjectAltName
   ```
   Ожидаемо: в SAN оба имени — `portal.vektor.lab` и `sklad.vektor.lab`.

7. **WEB01 — виртуальные хосты nginx + reverse proxy.**
   ```bash
   sudo tee /etc/nginx/sites-available/vektor.conf > /dev/null <<'EOF'
   server {
       listen 80;
       server_name portal.vektor.lab sklad.vektor.lab;
       return 301 https://$host$request_uri;
   }
   server {
       listen 443 ssl;
       server_name portal.vektor.lab;
       ssl_certificate     /etc/nginx/ssl/vektor.crt;
       ssl_certificate_key /etc/nginx/ssl/vektor.key;
       root /var/www/portal;
       index index.html;
       access_log /var/log/nginx/portal.access.log;
   }
   server {
       listen 443 ssl;
       server_name sklad.vektor.lab;
       ssl_certificate     /etc/nginx/ssl/vektor.crt;
       ssl_certificate_key /etc/nginx/ssl/vektor.key;
       access_log /var/log/nginx/sklad.access.log;
       error_log  /var/log/nginx/sklad.error.log;
       location / {
           proxy_pass http://127.0.0.1:8080;
           proxy_set_header Host              $host;
           proxy_set_header X-Real-IP         $remote_addr;
           proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   EOF
   sudo ln -s /etc/nginx/sites-available/vektor.conf /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```
   Ожидаемо: `syntax is ok`, `test is successful`. Если nginx пишет `duplicate default server` — не удалён симлинк `default`.
   Замечание по версиям: директива `http2 on;` появилась в nginx 1.25.1; на 1.24 и старше включай как `listen 443 ssl http2;` (с предупреждением об устаревании). Проверь `nginx -v` [ПРОВЕРИТЬ].

8. **UBU01 — проверка снаружи.**
   ```bash
   curl -sI https://portal.vektor.lab/ | head -3
   curl -s https://sklad.vektor.lab/ | head -1
   curl -s -o /dev/null -w '%{http_code}\n' http://portal.vektor.lab/   # ожидаем 301
   ```
   Ожидаемо: `HTTP/2 200` (или `HTTP/1.1 200`), для sklad — `<h1>Sklad Vektor (apache backend)</h1>`, для http — `301`.

9. **WIN10-CL02 — глазами пользователя.** Открой `https://portal.vektor.lab` в браузере. Ожидаемо: страница открывается, но с предупреждением «Подключение не защищено» — браузер не доверяет самоподписанному сертификату. Это правильно. В боевой среде такой сертификат выдают внутренним CA и распространяют корневой сертификат через GPO — тогда предупреждения нет. Проверь оба сайта и убедись, что на одном IP отдаются **разные** страницы (работает SNI + `Host`).

10. **WEB01 — логи в деле.**
    ```bash
    sudo tail -n 5 /var/log/nginx/portal.access.log
    curl -s -o /dev/null -w '%{http_code}\n' https://portal.vektor.lab/net-takoy-stranicy
    sudo tail -n 3 /var/log/nginx/error.log
    ```
    Ожидаемо: в access-логе строки формата combined (IP, время, метод, путь, код, user-agent); после запроса несуществующей страницы — `404` в access и `open() ... failed (2: No such file or directory)` в error.

11. **WEB01 — ротация.**
    ```bash
    sudo logrotate -d /etc/logrotate.d/nginx 2>&1 | tail -20
    ```
    Ожидаемо: в dry-run видно `rotating pattern: /var/log/nginx/*.log`, `considering log ...`, без ошибок. Наш отдельный `sklad.access.log` попадает под маску `*.log` — это и нужно.

12. **stand-log.md.** Запиши: добавлены A-записи portal/sklad на 10.10.10.3; на GW01 — таблица `ip vektor_nat`; на WEB01 — nginx (443, два vhost) + apache2 (8080, бэкенд), сертификат `/etc/nginx/ssl/vektor.crt`, конфиг `/etc/nginx/sites-available/vektor.conf`, каталоги `/var/www/portal`, `/var/www/sklad`.

### Проверка
- [ ] `nslookup portal.vektor.lab` и `nslookup sklad.vektor.lab` с WIN10-CL02 возвращают 10.10.10.3
- [ ] `sudo nginx -t` — `test is successful`
- [ ] `sudo ss -tlnp` на WEB01: nginx на 80 и 443, apache2 на 8080, конфликтов нет
- [ ] `curl -sI https://portal.vektor.lab/` с UBU01 → 200, `server: nginx/...`
- [ ] `curl -s https://sklad.vektor.lab/` → страница из Apache (reverse proxy работает)
- [ ] `http://portal.vektor.lab` → 301 на https
- [ ] `openssl s_client -servername ...` показывает SAN с двумя именами и срок годности в будущем
- [ ] В браузере на WIN10-CL02 оба сайта отдают **разное** содержимое
- [ ] В `/var/log/nginx/portal.access.log` видны запросы с IP клиентов (а не только 127.0.0.1)
- [ ] `logrotate -d` проходит без ошибок

### Сломай и почини
**Заявка №4472.** Пользователь: Петрова (WIN10-CL02). ВМ: WEB01. Что написал: «Портал открывается, а складской раздел — нет. Страница: **502 Bad Gateway**. Утром работало, я ничего не трогала». Срочность: высокая.

**Как воспроизвести поломку** (сделай это до «получения заявки», чтобы честно пройти диагностику):
```bash
sudo systemctl stop apache2
sudo systemctl disable apache2
```
Теперь `https://sklad.vektor.lab/` отдаёт 502, а портал продолжает работать — ровно как в тикете.

**Диагностика по шагам (в этом порядке, не перескакивая):**
1. Воспроизводим: с UBU01 `curl -sI https://sklad.vektor.lab/` → `502`. Портал `200` — значит DNS, GW01, nftables, nginx и TLS в порядке, проблема за прокси.
2. Смотрим, что говорит nginx: `sudo tail -n 20 /var/log/nginx/sklad.error.log` → `connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://127.0.0.1:8080/"`. Вывод: nginx жив, бэкенд не отвечает на 8080.
3. Проверяем, слушает ли кто-то 8080: `sudo ss -tlnp | grep :8080` → пусто. Значит процесс не запущен, а не «файрвол режет» (файрвол дал бы timeout, а не refused).
4. Смотрим сервис: `systemctl status apache2 --no-pager` → `inactive (dead)`. `journalctl -u apache2 -n 30 --no-pager` — причину падения в юните не видно, значит остановлен вручную.
5. Проверяем автозапуск: `systemctl is-enabled apache2` → `disabled`. Вот и корневая причина: сервис не поднимается после перезагрузки ВМ, а вчерашний ребут после обновления это просто «вскрыл».
6. Побочно проверяем конфиг, чтобы не запустить сломанное: `sudo apache2ctl configtest` → `Syntax OK`.

**Решение:**
```bash
sudo systemctl enable --now apache2
sudo ss -tlnp | grep :8080
curl -s https://sklad.vektor.lab/ | head -1
```
Ожидаемо: apache2 `active (running)` и `enabled`, порт 8080 слушается, страница склада отдаётся. `reload nginx` не нужен — конфиг nginx не менялся.

**Ответ пользователю:** «Мария, добрый день. Причина найдена: на веб-сервере был остановлен внутренний сервис, который отдаёт складской раздел. Подняли и включили в автозапуск, сейчас раздел открывается. Проверьте, пожалуйста, у себя. Портал не затрагивали. Заявку закрываю — если повторится, напишите, будем разбираться глубже».

**Закрытие:** в комментарии к заявке фиксируем «корневая причина: сервис apache2 в состоянии disabled, не запускался после перезагрузки ВМ; действие: systemctl enable --now apache2; проверено: sklad.vektor.lab 200». Отдельным тикетом — задача «проверить `is-enabled` у всех критичных сервисов на Linux-хостах», чтобы такое не повторялось (детали — урок 54).

### Что изменилось на стенде
- **DC01:** в зоне `corp.vektor.lab` две A-записи — `portal` и `sklad` → 10.10.10.3.
- **GW01:** таблица nftables `ip vektor_nat` с DNAT 80/443 на 10.10.20.10 и разрешающими правилами forward; файл `/root/vektor-nat.nft`.
- **WEB01 (10.10.20.10):** nginx слушает 80 и 443, apache2 — 8080; конфиг `/etc/nginx/sites-available/vektor.conf` + симлинк в `sites-enabled`; сайт по умолчанию nginx отключён; сертификат и ключ в `/etc/nginx/ssl/`; каталоги `/var/www/portal`, `/var/www/sklad`; individual-логи `portal.access.log`, `sklad.access.log`, `sklad.error.log`; apache2 переведён в `enabled` с `DocumentRoot /var/www/sklad`.
- **UBU01:** изменений нет (использовалась только как клиент для curl).

### Откат
VMware Workstation → выбери ВМ → **Snapshot Manager** → снапшот `before-l2-056` → **Revert To Snapshot**. По порядку: WEB01, GW01, DC01 (сначала откатывай зависимости-клиентов, потом источники). После отката на WIN10-CL02 выполни `ipconfig /flushdns` — иначе клиент будет держать старый DNS-кэш и «сайт не открывается» уже после восстановления. Запиши откат в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Править конфиг и сразу `reload` без `nginx -t`** → синтаксическая ошибка, nginx не перечитывает конфиг или падает при рестарте, сайт лежит. Правильно: `nginx -t && systemctl reload nginx` одной строкой, всегда.
2. **Ставить nginx и Apache оба на 80** → второй не стартует, в `journalctl` «Address already in use». Правильно: развести порты (443/8080) или использовать разные IP.
3. **`systemctl start` без `enable`** → после ребута сервис мёртв, а виноват «сломавшийся сервер». Правильно: `enable --now`, и отдельно проверять автозапуск у всех критичных юнитов.
4. **Один `server`-блок на все сайты** → все имена отдают один контент. Правильно: отдельный `server`/`VirtualHost` на каждое имя, проверка `nginx -T` или `apache2ctl -S`.
5. **Сертификат без SAN, только с CN** → браузеры ругаются, хотя «всё сделано по учебнику 2010 года». Правильно: `subjectAltName` со всеми именами.
6. **`curl -k` и «принять риск» в браузере как постоянная практика** → привычка игнорировать предупреждения TLS, а вместе с ней и реальные атаки MITM. `-k` — только для быстрой диагностики на стенде.
7. **Не читать логи, а гадать** → «наверное, файрвол». Правильно: сначала код ответа + строка в error.log, потом гипотезы.
8. **Логи без ротации и ключи с правами 644** → диск забит, приватный ключ читает любой пользователь. Правильно: logrotate (обычно уже настроен пакетом) и `chmod 600` на ключ, владелец root.

**Безопасность.** В лабораторной мы поставили `policy accept` в forward и открыли 80/443 всей офисной сети — это учебное упрощение, в бою так нельзя: правила должны быть «deny by default», а доступ к админке — только с доверенных адресов. Нельзя: держать самоподписанный сертификат в проде (пользователей приучают кликать «всё равно продолжить»); отключать TLS-проверку в скриптах и мониторинге; хранить ключи в git; логировать пароли, токены и cookie (в access-логе их быть не должно, а query-строки с токенами — отдельная головная боль); отдавать наружу `/server-status`, `/nginx_status` и листинг каталогов (`autoindex on`); запускать приложение от root. В нормальной среде: TLS 1.2+, HSTS, `server_tokens off`, отдельный непривилегированный пользователь, rate limiting на формы входа, fail2ban для nginx (урок 55), ежедневная проверка срока действия сертификатов в мониторинге (MON01, Zabbix).

## 7. Вопросы на собеседовании

**1. Чем nginx отличается от Apache и что выберешь под 1С?**
Ответ: nginx — событийная модель, один процесс обслуживает много соединений, силён в статике, проксировании и балансировке. Apache — MPM-модель (prefork/worker/event), богатая модульность, `.htaccess`, историческая совместимость с PHP и модулем веб-расширения 1С. Под 1С обычно публикуют через Apache (или IIS), а nginx ставят спереди как TLS-терминатор и кэш статики.
*Что хочет услышать:* что ты понимаешь модель конкурентности и не считаешь один сервер «лучше» другого вообще, без контекста.
*Красный флаг:* «Apache устарел, им никто не пользуется» — сразу видно, что человек не работал с 1С и legacy.

**2. Как сервер понимает, какой из десяти сайтов отдать, если IP один?**
Ответ: по заголовку `Host` в HTTP-запросе — name-based virtual hosting. Для HTTPS имя передаётся в расширении TLS SNI, потому что HTTP-заголовки уже зашифрованы; поэтому без SNI на один IP:443 — один сертификат. Проверяется через `nginx -T`, `apache2ctl -S`, `curl --resolve`, `openssl s_client -servername`.
*Что хочет услышать:* понимание связки `Host` + SNI и умение проверить это руками.
*Красный флаг:* «по IP-адресу» — значит, с TLS и виртуальными хостами не сталкивался.

**3. Зачем нужен reverse proxy? И чем 502 отличается от 504?**
Ответ: reverse proxy прячет бэкенд, терминирует TLS в одной точке, кэширует, балансирует, даёт единую точку логирования и WAF. 502 Bad Gateway — прокси получил от upstream невалидный ответ или не смог подключиться (connection refused, упавший сервис); 504 Gateway Timeout — подключился, но upstream не ответил за отведённое время (`proxy_read_timeout`).
*Что хочет услышать:* что 502/504 — это про связку «прокси ↔ бэкенд», а не про клиента и не про DNS.
*Красный флаг:* «502 — это ошибка клиента» или путаница 502/500.

**4. Пришла заявка «сайт не работает». Твои первые пять действий?**
Ответ: 1) воспроизвести и зафиксировать код ответа (`curl -sI`) — 200/301/403/404/500/502/504; 2) посмотреть `access.log` (доходит ли запрос до сервера) и `error.log` (что сервер говорит); 3) проверить, слушается ли порт (`ss -tlnp`) и живы ли сервисы (`systemctl status`); 4) проверить путь до сервера: DNS, маршрут, DNAT/файрвол; 5) проверить срок и SAN сертификата (`openssl s_client`). Только после этого — гипотезы и правки.
*Что хочет услышать:* дисциплину «сначала факты и логи, потом изменения».
*Красный флаг:* «перезапущу nginx, вдруг поможет» — как первое и единственное действие.

**5. Как организовать HTTPS для внутренних сервисов компании?**
Ответ: внутренний удостоверяющий центр (Active Directory Certificate Services или отдельный CA), сертификаты на SAN с внутренними именами, корневой сертификат распространяется через GPO в «Доверенные корневые центры»; продление автоматизируется, срок отслеживается мониторингом. Внутренний CA не выйдет «в мир», но для внутренних имён это и не нужно. Для внешних доменов — публичный CA. Для ГОСТ-TLS (КриптоПро, требования регуляторов) — отдельный стек и отдельная история, там нужен профильный опыт [ПРОВЕРИТЬ].
*Что хочет услышать:* понимание разницы между самоподписанным сертификатом, внутренним CA и публичным, плюс автоматизацию продления.
*Красный флаг:* «поставим самоподписанный, и так сойдёт» без упоминания GPO/доверия и сроков.

## 8. Источники

- **nginx.org** — раздел *Documentation*: «Beginner's Guide», «ngx_http_ssl_module», «ngx_http_proxy_module», «Configuring HTTPS servers». Поиск: `nginx beginner's guide`, `nginx ssl_certificate server_name SNI`.
- **httpd.apache.org** — раздел *Documentation → 2.4*: «Virtual Hosts», «SSL/TLS Strong Encryption: How-To», «Log Files». Поиск: `apache 2.4 virtual host examples`, `apache ssl howto`.
- **ubuntu.com** — *Ubuntu Server documentation → Web servers*: «HTTPD — Apache2 Web Server», «nginx». Поиск: `ubuntu server nginx documentation`.
- **debian.org** — *Debian Administrator's Handbook*, глава про веб-серверы [ПРОВЕРИТЬ номер главы в актуальной редакции]. Поиск: `debian administrator's handbook web servers`.
- **openssl.org** — *Docs → manpages*: `openssl-req`, `openssl-s_client`, `openssl-x509`. Поиск: `openssl req addext subjectAltName`.
- **ssl-config.mozilla.org** — генератор безопасных конфигов TLS для nginx/Apache. Поиск: `mozilla ssl configuration generator`.
- **man7.org / man-страницы системы** — `man nginx`, `man apache2ctl`, `man logrotate`, `man nft`. Поиск: `man logrotate debug dry run`.
- **angie.software** — документация российского форка nginx (проверь актуальность и условия лицензии) [ПРОВЕРИТЬ]. Поиск: `angie web server documentation`.
- **Astra Linux** — официальная документация по установке и настройке веб-серверов, особенности мандатного доступа [ПРОВЕРИТЬ раздел]. Поиск: `Astra Linux nginx настройка документация`.
