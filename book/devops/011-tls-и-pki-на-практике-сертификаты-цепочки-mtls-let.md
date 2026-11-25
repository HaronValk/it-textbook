# TLS и PKI на практике: сертификаты, цепочки, mTLS, Let's Encrypt, ACME, cert-manager

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01 (или любой браузер на машине, где ты проверяешь)
> **Что написал:** «Открываю https://web01.corp.vektor.lab — браузер пишет "Соединение не защищено, ERR_CERT_AUTHORITY_INVALID". Вчера работало. Мне к 15:00 нужен доступ к тестовому стенду.»
> **Срочность:** средняя

Половина инцидентов «сайт не открывается» в реальной работе — это TLS: истёк сертификат, у него не тот SAN, сервер отдаёт неполную цепочку, клиент не доверяет внутреннему УЦ, продление сломалось, кто-то отключил проверку в конфиге. DevOps отвечает за то, чтобы сертификаты выпускались, продлевались и ротировались автоматически, а не «руками раз в год по напоминанию в календаре».

После главы ты будешь: понимать, из чего состоит X.509-сертификат и цепочка доверия; уметь поднять собственный УЦ (CA) и выпустить сертификаты для сервера и клиента; настраивать TLS и mTLS в nginx; знать, как работает ACME и почему Let's Encrypt нельзя выпустить для внутреннего имени; уметь поставить cert-manager в Kubernetes и объяснить на собеседовании, как всё это живёт в проде.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Что такое PKI.** Public Key Infrastructure — набор правил, ролей и форматов, по которым одна сторона доверяет публичному ключу другой. Доверие не «вычисляется», оно *делегируется*: ты доверяешь корневому УЦ (Root CA), он подписал промежуточный (Intermediate / Issuing CA), тот подписал конечный сертификат сервера. Это и есть **цепочка доверия**.

```text
  ┌────────────────────────────────────┐
  │ Root CA  "Vektor Lab Root CA"      │  самоподписан, 10 лет, ключ офлайн
  └───────────────┬────────────────────┘
                  │ подписывает
  ┌───────────────▼────────────────────┐
  │ Issuing CA (intermediate)          │  5 лет, pathlen:0
  │ может выпускать только leaf-серты  │
  └───────┬───────────────────┬────────┘
          │                   │
 ┌────────▼─────────┐  ┌──────▼──────────────┐
 │ web01            │  │ ivanov              │
 │ EKU: serverAuth  │  │ EKU: clientAuth     │
 │ SAN: DNS/IP      │  │ SAN: email          │
 └──────────────────┘  └─────────────────────┘
```

Корневой сертификат заранее лежит в trust store клиента (в Linux — `/etc/ssl/certs`, в Windows — «Доверенные корневые центры сертификации»). Всё остальное клиент получает по сети во время рукопожатия.

**Что внутри X.509-сертификата.** Версия, серийный номер, издатель (Issuer), субъект (Subject), срок действия (notBefore/notAfter), публичный ключ, и — самое важное для диагностики — расширения:

- `subjectAltName` (SAN) — список имён, для которых сертификат валиден. Именно SAN, а не CN, проверяют все современные клиенты; CN оставлен для совместимости.
- `extendedKeyUsage` (EKU) — `serverAuth` (сервер), `clientAuth` (клиент), `codeSigning` и т.д. Сертификат с `clientAuth` не пройдёт проверку как серверный.
- `keyUsage` — что можно делать ключом: `digitalSignature`, `keyEncipherment`, `keyCertSign` (только для CA).
- `basicConstraints` — `CA:TRUE/FALSE`, `pathlen` — сколько промежуточных CA может быть ниже.
- `subjectKeyIdentifier` / `authorityKeyIdentifier` — связывают сертификат с ключом и с издателем.

**Отзыв.** Сертификат может быть скомпрометирован до истечения срока. Способы: CRL (список отозванных, файл, обновляется с задержкой) и OCSP (онлайн-запрос к УЦ по HTTP). В корпоративной среде чаще просто делают короткоживущие сертификаты (30–90 дней) и не полагаются на отзыв.

**TLS-рукопожатие.** В TLS 1.2 обмен: ClientHello → ServerHello + Certificate + ServerKeyExchange → проверка цепочки и имени → обмен ключами → Finished. В TLS 1.3 (RFC 8446) это 1-RTT, убраны слабые алгоритмы (RSA-KeyExchange, CBC, SHA-1), а сертификат сервер отдаёт уже зашифрованным. Порт — TCP 443.

**mTLS (mutual TLS).** Обычный TLS: клиент проверяет сервер. mTLS: сервер тоже требует сертификат клиента и проверяет его против своего списка доверенных CA. Используется для service-to-service внутри кластера (service mesh), для API-шлюзов, для админских эндпоинтов.

**ACME.** Automated Certificate Management Environment — протокол автоматического выпуска сертификатов (им же работает Let's Encrypt). Порты: 80 (challenge http-01), 53 (dns-01), 443 (tls-alpn-01).

```text
certbot / cert-manager
  1. GET  /directory                       → адреса ресурсов
  2. POST /new-account                     → аккаунт (ключ аккаунта, JWS-подпись)
  3. POST /new-order  {dnsNames: [...]}    → заказ
  4. POST /authz/{id}  → выбираем challenge
  5. выкладываем токен: /.well-known/acme-challenge/<token>
  6. POST /challenge/{id}                  → CA идёт на :80 и читает токен
  7. POST /finalize  {CSR}                 → подпись сертификата
  8. POST /certificate                     → fullchain.pem
```

Важно: **Let's Encrypt проверяет домен снаружи**, из интернета. Он не может выпустить сертификат для `corp.vektor.lab` — это не публичное имя, и до твоего стенда снаружи не достучаться. В лабе мы поэтому делаем свой CA, а ACME отрабатываем на staging-окружении/тестовом ACME-сервере. Логика и код одинаковые — меняется только URL директории.

**cert-manager.** Kubernetes-оператор, который реализует ACME и не только. Архитектура: контроллеры в namespace `cert-manager` + CRD:

- `Issuer` / `ClusterIssuer` — «кто и как подписывает» (CA, ACME, Vault, selfSigned);
- `Certificate` — «что выпустить» (dnsNames, срок, куда положить secret);
- `CertificateRequest`, `Order`, `Challenge` — служебные объекты, по ним видно, на каком шаге застряло;
- результат всегда кладётся в `Secret` типа `kubernetes.io/tls` (`tls.crt`, `tls.key`, `ca.crt`). Продление — автоматическое, за `renewBefore` до истечения.

## 3. Аналогия из жизни

Паспортная система. Есть государство (Root CA) — ему доверяют все с рождения (trust store). Само государство паспорта гражданам не печатает, оно делегировало это паспортным столам (Intermediate CA) и выдало им лицензию с печатью. Паспортный стол печатает конкретный паспорт (leaf-сертификат) с фотографией, сроком действия и перечнем «где действителен» (SAN). На КПП охранник не звонит в министерство — он смотрит печать паспортного стола, а лицензию паспортного стола проверяет по печати государства. **mTLS** — это когда охранник тоже показывает своё удостоверение, а не просто требует твой паспорт. **Отзыв** — это когда паспорт объявили недействительным, но охранник на дальнем КПП об этом узнает из обновлённого списка с задержкой.

Аналогия ломается в одном: паспорт нельзя «перевыпустить за 30 секунд по API», а сертификат — можно, и именно на этой скорости держится вся современная инфраструктура.

## 4. Практика и команды

**1. Создать корневой CA (самоподписанный).**

```bash
openssl genrsa -aes256 -out root-ca.key 4096
openssl req -x509 -new -key root-ca.key -sha256 -days 3650 \
  -subj "/C=RU/O=Vektor Lab/CN=Vektor Lab Root CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out root-ca.crt
```

`-x509` — сделать самоподписанный серт вместо CSR, `-days 3650` — срок, `-addext` — расширения, `-aes256` — шифрует ключ паролем (спросит пароль; на стенде — `Lab#2026-vektor`). Ключ корневого CA в проде лежит офлайн (HSM/флешка в сейфе), а не рядом с сертом.

**2. Сгенерировать ключ и CSR для промежуточного CA.**

```bash
openssl genrsa -aes256 -out issuing-ca.key 4096
openssl req -new -key issuing-ca.key \
  -subj "/C=RU/O=Vektor Lab/CN=Vektor Lab Issuing CA" -out issuing-ca.csr
```

CSR (Certificate Signing Request) — «заявка на подпись»: публичный ключ + субъект, подписанные приватным ключом заявителя.

**3. Подписать промежуточный CA корневым.**

```bash
cat > issuing-ca.ext <<'EOF'
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage         = critical, keyCertSign, cRLSign
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always
EOF
openssl x509 -req -in issuing-ca.csr -CA root-ca.crt -CAkey root-ca.key \
  -CAcreateserial -days 1825 -sha256 -extfile issuing-ca.ext -out issuing-ca.crt
```

`pathlen:0` — ниже этого CA может быть только leaf, не ещё один CA. Смотреть в выводе: `Certificate request self-signature ok`.

**4. Выпустить серверный сертификат с SAN.**

```bash
openssl genrsa -out web01.key 2048
openssl req -new -key web01.key -subj "/C=RU/O=Vektor Lab/CN=web01.corp.vektor.lab" -out web01.csr
cat > web01.ext <<'EOF'
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = DNS:web01.corp.vektor.lab, DNS:web01, DNS:localhost, IP:10.10.20.10
EOF
openssl x509 -req -in web01.csr -CA issuing-ca.crt -CAkey issuing-ca.key \
  -CAcreateserial -days 397 -sha256 -extfile web01.ext -out web01.crt
cat web01.crt issuing-ca.crt > web01-fullchain.crt
```

397 дней — потолок для публичных сертификатов у Apple/Chrome (398 дней). Тренд мировых регуляторов — сокращать срок жизни публичных сертификатов дальше (до 200, потом 100, потом 47 дней) **[ПРОВЕРИТЬ актуальные даты]**; внутренние серты делай сразу короткими (90 дней) с автопродлением.

**5. Проверить цепочку.**

```bash
openssl verify -CAfile root-ca.crt -untrusted issuing-ca.crt web01.crt
# web01.crt: OK
```

`-untrusted` — файл с промежуточными сертами, которые не в trust store. Если получаешь `unable to get local issuer certificate` — не хватает промежуточного, если `certificate has expired` — срок.

**6. Посмотреть, что реально в сертификате.**

```bash
openssl x509 -in web01.crt -noout -subject -issuer -dates -ext subjectAltName,extendedKeyUsage
```

Типичный вывод: `subject=C = RU, O = Vektor Lab, CN = web01.corp.vektor.lab`, `notAfter=...`, `X509v3 Subject Alternative Name: DNS:web01.corp.vektor.lab, ...`. Смотреть: имя в SAN совпадает с тем, что набирает клиент; EKU = `serverAuth`; срок не истёк.

**7. Проверить, что отдаёт сервер по сети.**

```bash
openssl s_client -connect 10.10.20.10:443 -servername web01.corp.vektor.lab \
  -CAfile root-ca.crt -showcerts -tls1_3 </dev/null
```

`-servername` — SNI (без него сервер может отдать другой сертификат), `-showcerts` — печатает всю присланную цепочку (считай блоки `s: / i:` — их должно быть два: leaf + intermediate), `-tls1_3`/`-tls1_2` — проверка версии протокола. Ищи строку `Verify return code: 0 (ok)`; код `20` (unable to get local issuer) — неполная цепочка, `10` (certificate has expired) — просрочка.

**8. Проверить с клиента через curl.**

```bash
curl -v --cacert root-ca.crt https://web01.corp.vektor.lab/ \
     --resolve web01.corp.vektor.lab:443:10.10.20.10
```

`--cacert` — подменить trust store на наш корневой CA, `--resolve` — принудительно направить имя на IP, не трогая DNS. В выводе смотри `SSL certificate verify ok`, `SSL connection using TLSv1.3`, `subject:`.

**9. mTLS: клиентский сертификат и проверка.**

```bash
openssl genrsa -out client-ivanov.key 2048
openssl req -new -key client-ivanov.key -subj "/C=RU/O=Vektor Lab/CN=ivanov" -out client-ivanov.csr
cat > client.ext <<'EOF'
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature
extendedKeyUsage = clientAuth
subjectAltName   = email:ivanov@corp.vektor.lab
EOF
openssl x509 -req -in client-ivanov.csr -CA issuing-ca.crt -CAkey issuing-ca.key \
  -CAcreateserial -days 90 -sha256 -extfile client.ext -out client-ivanov.crt

curl --cacert root-ca.crt --cert client-ivanov.crt --key client-ivanov.key \
     --resolve web01.corp.vektor.lab:443:10.10.20.10 https://web01.corp.vektor.lab/secure/
```

EKU здесь строго `clientAuth` — серверный серт для mTLS-клиента не подойдёт.

**10. Сконвертировать в PKCS#12 (для Windows/Java).**

```bash
openssl pkcs12 -export -out ivanov.p12 -inkey client-ivanov.key \
  -in client-ivanov.crt -certfile issuing-ca.crt
```

`-certfile` — включает промежуточные в архив, чтобы Windows не ругался на неполную цепочку при импорте.

**11. Let's Encrypt / certbot (на публичном домене).**

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot certonly --webroot -w /var/www/html -d demo.example.com \
  --server https://acme-staging-v02.api.letsencrypt.org/directory
sudo certbot renew --dry-run
```

`certonly` — только получить серт, конфиг nginx не менять; `--server` — staging-окружение (не расходует лимиты, но серт «недоверенный» — это нормально для теста); `--dry-run` — тренировка продления. Для `corp.vektor.lab` это не сработает принципиально: нет публичной A-записи.

**12. cert-manager в кластере.**

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl -n cert-manager get pods
kubectl get clusterissuer
kubectl get certificate,certificaterequest,order,challenge -A
kubectl describe certificate web-demo-tls
```

`latest/download` удобно для обучения, но для воспроизводимости закрепляй конкретную версию (`.../download/v1.16.1/cert-manager.yaml` — **номер версии [ПРОВЕРИТЬ] на момент чтения**). `describe certificate` показывает Conditions: `Ready=True` и `Renewal Time`; по `order`/`challenge` видно, если ACME застрял на проверке домена.

**13. Диагностика портов и шифров.**

```bash
ss -tlnp | grep -E ':443|:80'
openssl ciphers -v 'TLSv1.3' | head
```

`ss -tlnp` — кто слушает порт (для nginx — мастер-процесс), вторая команда — какие наборы шифров реально доступны в твоей сборке OpenSSL.

## 5. Лабораторная работа

### Предусловия

- **UBU01** (`10.10.10.30`, Ubuntu Server 24.04) — здесь живёт наш CA и отсюда же тестируем. OpenSSL уже стоит.
- **WEB01** (`10.10.20.10`, nginx из урока 10) — сюда ставим сертификат.
- **k8s-cp01** (`10.10.10.61`) — только для шага 10; если кластер ещё не собран (он появится в модуле Kubernetes), шаг 10 пропусти и вернись к нему позже.
- С UBU01 должен пинговаться `10.10.20.10`. Если нет — маршрут между `10.10.10.0/24` и `10.10.20.0/24` идёт через GW01 (`10.10.10.3`), который поднимался на уровне L2. **Если GW01 у тебя нет**, выполни лабу на самом UBU01: nginx слушает `8443`, все обращения — на `https://ubu01.corp.vektor.lab:8443`.
- DNS-имя `web01.corp.vektor.lab` настраиваем через `/etc/hosts` — Windows-DNS на DevOps-стенде может отсутствовать.

### Снапшот

В VMware Workstation для каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-devops-011`. Машины: **UBU01**, **WEB01**, **k8s-cp01** (если есть).

### Шаги

1. **UBU01.** Создай рабочую папку и проверь инструмент:
   ```bash
   mkdir -p ~/pki/vektor && cd ~/pki/vektor && openssl version
   ```
   Ожидаемо: `OpenSSL 3.0.x` (Ubuntu 24.04). Всё дальнейшее — в этой папке.

2. **UBU01.** Создай корневой CA командами из п.4.1. Ожидаемо: два файла `root-ca.key` (зашифрован) и `root-ca.crt`, при просмотре `openssl x509 -in root-ca.crt -noout -subject` — `subject=C = RU, O = Vektor Lab, CN = Vektor Lab Root CA`, и `CA:TRUE` в расширениях.

3. **UBU01.** Создай и подпиши промежуточный CA (п.4.2–4.3). Ожидаемо: `issuing-ca.crt` с `issuer=...Root CA` и `subject=...Issuing CA`.

4. **UBU01.** Выпусти серверный сертификат для WEB01 (п.4.4). Ожидаемо: `web01.crt` + `web01-fullchain.crt`, где внутри **два** PEM-блока `BEGIN CERTIFICATE`. Проверь: `grep -c 'BEGIN CERTIFICATE' web01-fullchain.crt` → `2`.

5. **UBU01.** Проверь цепочку (п.4.5) и содержимое (п.4.6). Ожидаемо: `web01.crt: OK` и SAN с четырьмя именами, EKU `TLS Web Server Authentication`.

6. **WEB01.** Скопируй файлы и разложи по местам:
   ```bash
   sudo mkdir -p /etc/nginx/ssl
   scp ~/pki/vektor/web01-fullchain.crt ~/pki/vektor/web01.key \
       ~/pki/vektor/issuing-ca.crt adm.student@10.10.20.10:/tmp/
   # на WEB01:
   sudo mv /tmp/web01-fullchain.crt /tmp/web01.key /tmp/issuing-ca.crt /etc/nginx/ssl/
   sudo chown root:root /etc/nginx/ssl/* && sudo chmod 600 /etc/nginx/ssl/web01.key
   ```
   Ожидаемо: файлы на месте, приватный ключ читает только root.

7. **WEB01.** Опиши сайт в `/etc/nginx/sites-available/vektor-tls` (или в `conf.d`), включи и перезагрузи:
   ```nginx
   server {
       listen 443 ssl http2;              # nginx >= 1.25.1: слушай "listen 443 ssl;" + "http2 on;"
       server_name web01.corp.vektor.lab;

       ssl_certificate     /etc/nginx/ssl/web01-fullchain.crt;
       ssl_certificate_key /etc/nginx/ssl/web01.key;

       ssl_protocols       TLSv1.2 TLSv1.3;
       ssl_prefer_server_ciphers off;
       ssl_session_cache   shared:SSL:10m;
       ssl_session_timeout 1h;
       ssl_session_tickets off;
       add_header Strict-Transport-Security "max-age=31536000" always;

       location / {
           root  /var/www/html;
           index index.html;
       }

       location /secure/ {
           ssl_client_certificate /etc/nginx/ssl/issuing-ca.crt;
           ssl_verify_client on;
           default_type text/plain;
           return 200 "mTLS OK: $ssl_client_s_dn\n";
       }
   }
   ```
   ```bash
   sudo ln -s /etc/nginx/sites-available/vektor-tls /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx && ss -tlnp | grep :443
   ```
   Ожидаемо: `syntax is ok / test is successful` и процесс nginx на `*:443`. Если `nginx -t` ругается на `http2` — у тебя nginx 1.24, используй старую форму из комментария **[ПРОВЕРИТЬ версию: `nginx -v`]**.

8. **UBU01.** Пропиши имя и проверь TLS снаружи:
   ```bash
   echo "10.10.20.10 web01.corp.vektor.lab web01" | sudo tee -a /etc/hosts
   openssl s_client -connect web01.corp.vektor.lab:443 -servername web01.corp.vektor.lab \
     -CAfile ~/pki/vektor/root-ca.crt -showcerts -tls1_3 </dev/null | tail -n 20
   curl -v --cacert ~/pki/vektor/root-ca.crt https://web01.corp.vektor.lab/
   ```
   Ожидаемо: `Verify return code: 0 (ok)`, `SSL connection using TLSv1.3`, в `-showcerts` два сертификата, curl отдаёт HTML без предупреждений.

9. **UBU01.** Выпусти клиентский сертификат (п.4.9) и проверь mTLS:
   ```bash
   curl -i --cacert root-ca.crt --cert client-ivanov.crt --key client-ivanov.key \
        https://web01.corp.vektor.lab/secure/
   curl -i --cacert root-ca.crt https://web01.corp.vektor.lab/secure/
   ```
   Ожидаемо: первая команда — `200` и `mTLS OK: /C=RU/O=Vektor Lab/CN=ivanov`; вторая — `400 Bad Request` и `No required SSL certificate was sent`. Это и есть доказательство, что mTLS работает.

10. **UBU01.** Автоматизируй проверку срока (напоминание: урок 8 про systemd):
    ```bash
    sudo tee /usr/local/bin/vektor-cert-check.sh >/dev/null <<'EOF'
    #!/usr/bin/env bash
    set -euo pipefail
    CERT=/etc/ssl/vektor/web01.crt
    if ! openssl x509 -in "$CERT" -noout -checkend $((30*86400)); then
      echo "WARN: $CERT истекает в ближайшие 30 дней" >&2
      exit 1
    fi
    echo "OK: $CERT действителен более 30 дней"
    EOF
    sudo chmod +x /usr/local/bin/vektor-cert-check.sh
    ```
    Плюс юнит `vektor-cert-check.service` и таймер с `OnCalendar=daily` (по образцу урока 8). Ожидаемо: `systemctl start vektor-cert-check` и `journalctl -u vektor-cert-check` показывают `OK: ...`.

11. **k8s-cp01 (если кластер есть).** Поставь cert-manager и свой CA-issuer:
    ```bash
    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
    kubectl -n cert-manager wait --for=condition=Available deploy --all --timeout=180s

    openssl rsa -in ~/pki/vektor/issuing-ca.key -out /tmp/issuing-ca-plain.key   # снять пароль
    kubectl -n cert-manager create secret tls vektor-issuing-ca \
      --cert=~/pki/vektor/issuing-ca.crt --key=/tmp/issuing-ca-plain.key
    ```
    ```yaml
    apiVersion: cert-manager.io/v1
    kind: ClusterIssuer
    metadata:
      name: vektor-ca
    spec:
      ca:
        secretName: vektor-issuing-ca
    ```
    ```yaml
    apiVersion: cert-manager.io/v1
    kind: Certificate
    metadata:
      name: web-demo-tls
      namespace: default
    spec:
      secretName: web-demo-tls
      duration: 2160h        # 90 дней
      renewBefore: 720h      # продлить за 30 дней
      commonName: web-demo.corp.vektor.lab
      dnsNames:
        - web-demo.corp.vektor.lab
      issuerRef:
        name: vektor-ca
        kind: ClusterIssuer
    ```
    ```bash
    kubectl apply -f issuer.yaml -f certificate.yaml
    kubectl get certificate web-demo-tls -w
    kubectl get secret web-demo-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -subject -issuer -dates
    ```
    Ожидаемо: `READY=True`, в secret лежит серт, подписанный нашим Issuing CA.

12. **UBU01.** Допиши в `stand-log.md`, что появилось (см. следующий раздел).

### Проверка

- [ ] `openssl verify -CAfile root-ca.crt -untrusted issuing-ca.crt web01.crt` → `OK`
- [ ] `grep -c 'BEGIN CERTIFICATE' web01-fullchain.crt` → `2`
- [ ] `openssl x509 -in web01.crt -noout -ext subjectAltName` содержит `web01.corp.vektor.lab` и `IP Address:10.10.20.10`
- [ ] `openssl s_client ... -tls1_3` → `Verify return code: 0 (ok)` и `Protocol: TLSv1.3`
- [ ] `curl --cacert root-ca.crt https://web01.corp.vektor.lab/` → 200 без предупреждений
- [ ] `/secure/` с клиентским сертом → 200 и DN `CN=ivanov`; без серта → 400
- [ ] `openssl s_client -tls1` (или `-tls1_1`) → отказ, `no protocols available` / `handshake failure`
- [ ] В `/etc/nginx/ssl/web01.key` права `600`, владелец root
- [ ] `systemctl list-timers | grep vektor` показывает таймер (шаг 10)
- [ ] (если был k8s) `kubectl get certificate` → `READY=True`

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01 (или браузер на твоей машине)
> **Что написал:** «Сайт https://web01.corp.vektor.lab перестал открываться: `ERR_CERT_AUTHORITY_INVALID`. Причём с самого сервера curl работает. Что происходит?»
> **Срочность:** средняя

**Как воспроизвести.** На WEB01 подмени конфиг так, чтобы сервер отдавал **только leaf** без промежуточного CA — это самая частая ошибка при переносе конфига с Apache (там цепочка задаётся отдельной директивой):

```bash
sudo cp /etc/nginx/ssl/web01-fullchain.crt /tmp/fullchain.bak
sudo cp /etc/nginx/ssl/web01.crt /etc/nginx/ssl/web01-fullchain.crt
sudo nginx -t && sudo systemctl reload nginx
```

С точки зрения сервера всё «ок», в логах nginx ошибок нет. С точки зрения клиента — цепочка обрывается.

**Диагностика по шагам (в этом порядке):**

1. С клиента: `curl -v --cacert root-ca.crt https://web01.corp.vektor.lab/` → `SSL certificate problem: unable to get local issuer certificate`. Запомни код ошибки, он однозначный.
2. С UBU01: `openssl s_client -connect web01.corp.vektor.lab:443 -servername web01.corp.vektor.lab -CAfile root-ca.crt -showcerts </dev/null | grep -E '^ [0-9]+ s:|Verify return code'` → в цепочке **один** сертификат (`0 s:...`) вместо двух, `Verify return code: 20 (unable to get local issuer certificate)`.
3. Проверь, что сервер вообще настраивался на fullchain: `grep -n ssl_certificate /etc/nginx/sites-available/vektor-tls` → видим `web01-fullchain.crt`, но файл подменён — `grep -c 'BEGIN CERTIFICATE' /etc/nginx/ssl/web01-fullchain.crt` → **1**.
4. Убедись, что дело именно в цепочке, а не в SAN/сроке: `openssl x509 -in /etc/nginx/ssl/web01.crt -noout -dates -ext subjectAltName` — всё в порядке.
5. Проверь логи на всякий случай: `journalctl -u nginx -n 50 --no-pager` — пусто, что подтверждает: проблема на стороне цепочки, а не сервиса.

**Решение.**

```bash
sudo cp /tmp/fullchain.bak /etc/nginx/ssl/web01-fullchain.crt
sudo nginx -t && sudo systemctl reload nginx
```

**Ответ пользователю:** «Проблема была на сервере: он отдавал сертификат без промежуточного центра, поэтому браузер не мог построить цепочку до доверенного корня. Сейчас исправлено, почисти кэш и перезайди на https://web01.corp.vektor.lab. Если браузер всё ещё ругается — проверь, установлен ли в "Доверенные корневые центры" наш `Vektor Lab Root CA` (файл `root-ca.crt`): для внутренних сайтов это обязательный шаг один раз на машину.»

**Закрытие заявки:** тип «Инцидент», причина — «неполная цепочка сертификатов в конфигурации nginx», действие — «исправлена конфигурация, добавлена проверка `grep -c 'BEGIN CERTIFICATE'` в чек-лист выкатки». Заведи в GitLab issue «добавить в CI проверку цепочки после выкатки».

### Что изменилось на стенде

- **UBU01:** создан собственный PKI в `~/pki/vektor/` — `root-ca.crt/.key`, `issuing-ca.crt/.key/.csr`, `web01.key/.csr/.crt/fullchain.crt`, `client-ivanov.key/.crt`, `*.ext`; в `/etc/hosts` добавлена запись `10.10.20.10 web01.corp.vektor.lab web01`; установлен `/usr/local/bin/vektor-cert-check.sh` + systemd-юнит и таймер `vektor-cert-check`.
- **WEB01:** каталог `/etc/nginx/ssl/` (`web01-fullchain.crt`, `web01.key` 600 root, `issuing-ca.crt`); новый сайт `vektor-tls` на 443 с TLS 1.2/1.3 и локацией `/secure/` под mTLS.
- **Доверие:** `root-ca.crt` — единственный корень, которому теперь должны доверять клиенты стенда.
- Серверные и клиентские сертификаты выпускаются только нашим Issuing CA, срок 397 и 90 дней соответственно.

### Откат

VMware Workstation → выбрать ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-devops-011` → **Go To**. Так по очереди для WEB01, UBU01 и k8s-cp01. Если откатывать не хочется, достаточно вручную: на WEB01 — `sudo rm /etc/nginx/sites-enabled/vektor-tls && sudo systemctl reload nginx`, на UBU01 — `rm -rf ~/pki/vektor` и удалить строку из `/etc/hosts`. Откат снапшотом предпочтителен: он гарантирует, что в следующей главе стенд ровно такой, каким описан в журнале.

## 6. Ошибки новичков и безопасность

- **Класть `root-ca.key` рядом с сертом на том же сервере** → компрометация сервера = компрометация всего PKI, любой может выпускать сертификаты от твоего имени → корневой ключ храни офлайн, подпись — на отдельной машине/в HSM, а на серверах лежит только leaf-ключ.
- **Отдавать неполную цепочку (`ssl_certificate` без intermediate)** → работает в браузере и падает в curl/Java/мобильных клиентах → всегда `fullchain.pem`, а в чек-лист выкатки — проверка `-showcerts`.
- **Сертификат на CN без SAN** → современные клиенты игнорируют CN и валятся с `no alternative certificate subject name` → имена только в `subjectAltName`, причём все, по которым реально ходят.
- **Самоподписанный серт «на глаз» вместо CA** → каждый клиент требует `--insecure`, и это привычка, которая переезжает в прод → делай нормальный CA + распространяй корень централизованно (GPO, Ansible, MDM).
- **`curl -k` / `verify=False` в CI и мониторинге** → проверка TLS фактически отключена, MITM не замечается → `-k` допустим один раз при отладке, никогда в скрипте, который останется в репозитории.
- **Сертификат на 10 лет** → о компрометации узнаешь через год-два, отзыв на практике не работает → 90 дней и автопродление.
- **Продление «руками по календарю»** → однажды релиз в пятницу вечером, и сайт лежит всё воскресенье → ACME/cert-manager/таймер, `--dry-run` в CI.
- **TLS 1.0/1.1 и слабые шифры «чтобы старый клиент подключился»** → уязвимости протокола → `TLSv1.2 TLSv1.3`, отключение слабых ciphers, исключения — точечно.

**Безопасность.** На стенде мы осознанно упрощаем: пароль `Lab#2026-vektor` на ключе CA, один CA на всё, сертификаты лежат в `~/pki`. В боевой среде так нельзя: ключ CA — только в HSM/Vault с аудитом доступа, приватные ключи не покидают хост, `Secret` в Kubernetes шифруется в etcd (EncryptionConfiguration) или тянется из внешнего хранилища (Vault, Yandex/VK-облачные секрет-менеджеры). И отдельно: **никогда не отключай проверку сертификата в проде**, чтобы «быстро починить» — это ровно то, чего добивается атакующий в MITM-сценарии.

Про РФ-контекст одной строкой: для публичных сайтов на территории РФ исторически добавляется доверие национальному УЦ Минцифры **[ПРОВЕРИТЬ актуальный порядок]**, а в контурах с требованиями импортозамещения встречается ГОСТ-TLS (КриптоПро) — это отдельная тема, но знать о её существовании на собеседовании полезно.

## 7. Вопросы на собеседовании

**1. «Что происходит, когда браузер открывает https://example.com?»**

Ответ: DNS → TCP 443 → ClientHello (SNI, версии, шифры) → ServerHello + Certificate (цепочка) → клиент строит цепочку от leaf до корня из trust store, проверяет подписи, срок, SAN, EKU, статус отзыва → проверка проходит → обмен ключами (в TLS 1.3 — ECDHE) → Finished → шифрованный HTTP. Браузер ещё проверяет HSTS, CT-логи для публичных сертов и делает OCSP/CRLite.

*Что хочет услышать:* что проверка — это построение цепочки до **корня в trust store**, а не «сертификат подписан, значит ок», и что имя берётся из SAN. *Красный флаг:* «браузер спрашивает у сайта, легальный ли он» или путаница между шифрованием и аутентификацией.

**2. «Почему Let's Encrypt не выпустит сертификат для внутреннего домена и что делать корпоративному сервису?»**

Ответ: ACME удостоверяется в контроле над доменом через http-01/dns-01/tls-alpn-01, а LE валидирует снаружи, из интернета, и выдаёт только для публично резолвящихся имён; `*.corp.local` не подходит. Варианты: (а) внутренний CA (step-ca, EJBCA, Microsoft AD CS) + тот же ACME-протокол — step-ca, например, умеет ACME для приватных имён; (б) публичный домен с DNS-01 и wildcard для внутренних сервисов; (в) Vault PKI / cert-manager CA issuer. Продление — автоматически, срок 90 дней.

*Что хочет услышать:* понимание, что ACME — это протокол, а не «Let's Encrypt», и что dns-01 позволяет выпускать wildcard. *Красный флаг:* «надо просто отключить проверку» или «поставить самоподписанный и забить».

**3. «Как работает mTLS и чем он лучше токена в заголовке?»**

Ответ: сервер в `CertificateRequest` (TLS 1.2) / в запросе сертификата (1.3) просит клиентский серт, клиент присылает свой, сервер проверяет его против `ssl_client_certificate` (список доверенных CA) и опционально сверяет CN/SAN с правами. Плюс: аутентификация на уровне транспорта, до приложения, ключ не уходит по сети, работает для любого протокола поверх TLS (не только HTTP), легко ротировать и привязать к сервисному аккаунту. Минусы: управление клиентскими сертами (распространение, отзыв), сложнее отладка, прокси должны уметь пробрасывать.

*Что хочет услышать:* где проверка живёт (транспорт, не приложение), и про EKU `clientAuth`. *Красный флаг:* «mTLS = Basic Auth по сертификату» или незнание, что клиенту нужен отдельный ключ.

**4. «cert-manager: какие объекты он создаёт и как продлевает сертификаты?»**

Ответ: CRD `Issuer`/`ClusterIssuer` (кто подписывает), `Certificate` (что выпустить и в какой Secret), служебные `CertificateRequest`, для ACME — `Order` и `Challenge`. Результат — `Secret` типа `kubernetes.io/tls` (`tls.crt`, `tls.key`, `ca.crt`). Контроллер следит за `renewBefore`/сроком и за 30 дней (по умолчанию `renewBefore` — треть срока) перевыпускает, обновляя Secret; Ingress-контроллер (nginx) подхватывает изменение и перечитывает серт без рестарта. Если Secret удалить — cert-manager выпустит заново; если удалить `Certificate` — Secret останется, но продлеваться не будет.

*Что хочет услышать:* что за состояние отвечают Conditions, и что диагностика идёт по `describe certificate` → `order` → `challenge`. *Красный флаг:* «cert-manager сам настраивает DNS» или «после продления нужен рестарт всех подов».

**5. «Прод лежит в 3 часа ночи: curl говорит `certificate has expired`. Твои действия?»**

Ответ: сначала стабилизировать — проверить `openssl s_client -servername ... | openssl x509 -noout -dates` (подтвердить, что истёк именно лист, а не промежуточный), посмотреть, есть ли готовый новый серт у балансировщика/в секрете; если есть — выкатить его (это минуты). Если нет — временно выпустить новый у того же CA/через ACME, для непубличного имени — из внутреннего CA. Потом — разбор: почему не сработало автопродление (истёк API-токен DNS-провайдера, сломался таймер, закончились лимиты LE, кто-то отключил cron). После — алерт на 30/14/7 дней до истечения и `--dry-run` в CI.

*Что хочет услышать:* порядок «сначала починить, потом разбираться» + конкретные команды проверки + постмортем и мониторинг. *Красный флаг:* сразу лезть в конфиг nginx, не подтвердив, какой именно сертификат истёк, или предлагать `-k`.

## 8. Источники (официальная документация)

- **OpenSSL** — `openssl.org`, раздел Documentation → manpages (`openssl-req`, `openssl-x509`, `openssl-verify`, `openssl-s_client`, `openssl-pkcs12`). Поиск: `openssl x509 manpage`, `openssl verify untrusted`.
- **Let's Encrypt** — `letsencrypt.org`, разделы Documentation → «Challenge Types», «Rate Limits», «Staging Environment». Поиск: `letsencrypt challenge types dns-01`, `letsencrypt rate limits`.
- **ACME (IETF)** — `datatracker.ietf.org`, RFC 8555 «Automatic Certificate Management Environment». Поиск: `RFC 8555 ACME`.
- **TLS 1.3 (IETF)** — `datatracker.ietf.org`, RFC 8446. Поиск: `RFC 8446 TLS 1.3`.
- **cert-manager** — `cert-manager.io`, разделы Documentation → «CA Issuer», «ACME», «Troubleshooting». Поиск: `cert-manager CA issuer`, `cert-manager troubleshooting certificate not ready`.
- **nginx** — `nginx.org`, раздел Documentation → `ngx_http_ssl_module` (`ssl_certificate`, `ssl_client_certificate`, `ssl_verify_client`). Поиск: `nginx ssl_verify_client mtls`.
- **Kubernetes** — `kubernetes.io`, раздел Concepts → «Managing TLS certificates in a cluster» / Secrets. Поиск: `kubernetes tls secret`.
- **Smallstep step-ca** (внутренний CA с ACME) — `smallstep.com`, Documentation → step-ca. Поиск: `step-ca acme provisioner` **[ПРОВЕРИТЬ актуальность раздела]**.
- **Минцифры РФ / национальный УЦ** — `digital.gov.ru`, раздел про удостоверяющие центры **[ПРОВЕРИТЬ актуальный раздел и порядок]**.
- **testssl.sh** (диагностика TLS) — `github.com/drwetter/testssl.sh`, README.
