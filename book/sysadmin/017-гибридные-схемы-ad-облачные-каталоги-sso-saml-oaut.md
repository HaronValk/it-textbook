# Гибридные схемы: AD + облачные каталоги, SSO, SAML, OAuth2/OIDC на практике

## 1. Введение (зачем это нужно в реальной работе)

Тикет в HelpDesk от Марии Петровой (HR, WIN10-CL02):

> «Добрый день! Мы подключили портал согласования отпусков. Теперь у сотрудников третий логин и третий пароль: Windows, 1С и портал. Люди путаются, пишут пароли на стикерах. Можно сделать вход в портал как в Windows — одной учётной записью? Срочность: высокая, до конца недели».

Это классический запрос на **SSO** (Single Sign-On, единый вход). Такая заявка приходит почти в каждой компании, где больше двух внутренних сервисов. Дальше выясняется, что «просто вход как в Windows» — это целая архитектура: каталог-источник (AD), поставщик идентичности (IdP), доверяющие приложения (SP), протоколы SAML 2.0 и OIDC, синхронизация и провижининг пользователей, а ещё — отдельная история про то, что делать, если сервис облачный и находится вне вашего периметра.

После этой главы ты будешь уметь: объяснить разницу между «синхронизацией каталогов» и «федерацией»; поднять IdP и связать его с AD по LDAP(S); провести живые OIDC- и SAML-аутентификации руками, а не «посмотреть в браузере»; читать токены и ассерты; и главное — задавать правильные вопросы на этапе «а нам надо внедрить SSO», а не после.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Гибридная схема** — это когда один и тот же пользователь существует в локальном каталоге (AD на DC01) и в «облачном» каталоге, и обе стороны согласованно понимают, кто он и что ему можно.

Три базовые модели:

1. **Синхронизация каталогов** (в мире Microsoft — Microsoft Entra Connect Sync / Entra Cloud Sync). Локальный объект «копируется» в облако. Варианты аутентификации: **PHS** (Password Hash Sync — хеш пароля синхронизируется, вход проверяет облако), **PTA** (Pass-Through Authentication — облако передаёт проверку агенту внутри сети), **Federation** (AD FS: облако вообще не проверяет пароль, а доверяет SAML-токену).
2. **Федерация / брокеринг** — каталоги не копируются, а доверяют друг другу через протокол. Keycloak, AD FS, Entra ID, ALD Pro — все умеют быть в роли IdP.
3. **Провижининг** — управление жизненным циклом учёток в приложениях по стандарту **SCIM 2.0** (RFC 7643/7644): создать, обновить, отключить. Отдельная от аутентификации задача.

Дальше — протоколы. Держи в голове главное разделение: **OAuth 2.0 — про авторизацию (доступ к ресурсу), OIDC — про аутентификацию (кто ты)**, построен поверх OAuth 2.0. SAML 2.0 — старый, но живой XML-стандарт, который до сих пор используют корпоративные приложения (1С-публикации, Jira, веб-почта, зарубежные SaaS).

```text
 WIN11-CL01 (браузер ivanov)
        │ 1. GET https://portal.vektor.lab
        ▼
 SP (приложение: портал, wiki, SaaS)
        │ 2. 302 → IdP:  SAML AuthnRequest  |  OIDC /authorize
        ▼
 IDP01 (Keycloak, :8080) ──3. LDAPS 636 / Kerberos 88──► DC01 (AD DS)
        │ 4. проверка пароля в AD (bind), чтение групп (memberOf)
        ▼
 SP получает: SAML Assertion (XML, подпись X.509)  ИЛИ  code → id_token + access_token (JWT)
        │ 5. валидирует подпись по сертификату/JWKS, срок, audience, issuer
        ▼
 Пользователь внутрь. Пароль вводился один раз.
```

Ключевые понятия, которые спрашивают на любом интервью:

- **SP** (Service Provider) — приложение, куда хотят войти. **IdP** (Identity Provider) — тот, кто подтверждает личность.
- **SAML**: `AuthnRequest` (SP → IdP, обычно HTTP-Redirect), `Response` с `Assertion` (IdP → SP, HTTP-POST, XML подписан сертификатом IdP). Внутри — `NameID` (кем представился пользователь) и `AttributeStatement` (роли, группы, почта). Проверять надо подпись, `Audience`, `NotOnOrAfter`, `InResponseTo`, `Destination`.
- **OIDC**: `id_token` (JWT с claims: `sub`, `iss`, `aud`, `exp`, `preferred_username`, `groups`) и `access_token` (для вызова API), плюс `refresh_token`. Подпись RS256 проверяется по **JWKS** (`/protocol/openid-connect/certs`). Все endpoint'ы описаны в `/.well-known/openid-configuration`.
- **PKCE** (RFC 7636) — обязателен для публичных клиентов (SPA, мобильные): `code_challenge` при запросе, `code_verifier` при обмене кода. Защищает от перехвата authorization code.
- **Не используем**: Implicit flow и Resource Owner Password Credentials — устарели и выпилены в OAuth 2.1.
- **Порты и тайминг**: HTTPS 443/8080 в лабе, LDAP 389, LDAPS 636, Kerberos 88, DNS 53. Проверки подписи и токенов **завязаны на время**: расхождение часов больше 5 минут ломает и SAML, и Kerberos.

Про РФ-реалии одной строкой: в 2026 году покупка и продление облачных подписок Microsoft (Entra ID, Microsoft 365) из РФ — проблема, поэтому в лабе мы поднимаем **self-hosted Keycloak**, а в реальных проектах смотришь в сторону **ALD Pro (Astra)**, **РЕД ИДМ**, **FreeIPA/Red Hat IdM** или Samba DC + Keycloak. Концептуально всё то же самое, меняется вендор.

## 3. Аналогия из жизни

Представь завод с проходной. Раньше в каждый цех был свой вахтёр, и на каждый цех — свой бумажный пропуск. Теперь на входе стоит **единая проходная** (IdP): она сверяет твой паспорт с базой отдела кадров (AD) и выдаёт пропуск.

Два вида пропусков. **SAML** — это бумажный бланк с гербовой печатью и подписью начальника: цех смотрит на печать и на то, что в бланке написано твоё имя, а не Петрова. **OIDC** — это электронный QR-пропуск: цех сканирует его своим считывателем и получает из базы ровно те поля, которые ему нужны (имя, отдел, срок действия).

Где аналогия ломается: в реальности «цех» не звонит на проходную, чтобы проверить QR. Он проверяет криптографическую подпись по заранее известному списку ключей (JWKS/сертификат IdP). Доверие асимметричное и офлайновое — поэтому проходная может быть недоступна, а уже выданные пропуска продолжат работать.

## 4. Практика и команды

**1. Discovery: что умеет IdP**

```bash
curl -s http://idp01.vektor.lab:8080/realms/vektor/.well-known/openid-configuration | jq '{issuer,authorization_endpoint,token_endpoint,jwks_uri}'
```

`jq` вытаскивает нужные поля из JSON. Смотри: `issuer` должен совпадать с тем, что IdP кладёт в токен (`iss`) — иначе клиент не примет токен; `jwks_uri` — откуда брать публичные ключи.

**2. Пара PKCE**

```bash
VERIFIER=$(openssl rand -base64 60 | tr -d '\n=+/' | cut -c1-64)
CHALLENGE=$(printf '%s' "$VERIFIER" | openssl dgst -binary -sha256 | openssl base64 | tr '+/' '-_' | tr -d '=')
echo "$VERIFIER"; echo "$CHALLENGE"
```

`-binary -sha256` — хеш, `tr '+/' '-_'` — перевод в base64url (это требование RFC 7636, обычный base64 не примут).

**3. Обмен кода на токены**

```bash
curl -s -X POST "http://idp01.vektor.lab:8080/realms/vektor/protocol/openid-connect/token" \
  -d grant_type=authorization_code \
  -d client_id=wiki-vektor -d client_secret="$SECRET" \
  -d code="$CODE" -d redirect_uri=http://localhost:9999/callback \
  -d code_verifier="$VERIFIER" | jq '{token_type,expires_in,scope}'
```

Типичный вывод: `{"token_type":"Bearer","expires_in":300,"scope":"openid profile email"}`. `expires_in: 300` — access token живёт 5 минут (дефолт Keycloak). Код одноразовый и живёт ~60 секунд.

**4. Service account: client_credentials**

```bash
curl -s -X POST "http://idp01.vektor.lab:8080/realms/vektor/protocol/openid-connect/token" \
  -d grant_type=client_credentials -d client_id=svc-report -d client_secret="$SECRET" | jq -r .access_token
```

Работает только если у клиента включён `serviceAccountsEnabled`. Так ходят «роботы»: выгрузки, интеграции 1С ↔ портал. Пользователя здесь нет вообще.

**5. Разбор JWT без библиотек**

```bash
echo "$ID_TOKEN" | cut -d. -f2 | tr '_-' '/+' | awk '{l=length($0)%4; if(l==2)print $0"=="; else if(l==3)print $0"="; else print $0}' | base64 -d | jq
```

`cut -d. -f2` — вторая часть JWT (payload), `awk` добивает паддинг до кратности 4, иначе `base64 -d` ругнётся. Смотри claims: `iss`, `aud`, `exp`, `preferred_username`, `groups`. **Подпись так не проверяется** — только читается содержимое.

**6. Keycloak из консоли (kcadm)**

```bash
/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user admin --password '***'
/opt/keycloak/bin/kcadm.sh create realms -s realm=vektor -s enabled=true
/opt/keycloak/bin/kcadm.sh get clients -r vektor -q clientId=wiki-vektor --fields id,secret
```

`config credentials` создаёт сессию админа в файле, дальше `-r` задаёт realm. `--fields` сокращает вывод до нужных полей — удобно для скриптов.

**7. Проверка LDAPS до AD**

```bash
openssl s_client -connect dc01.vektor.lab:636 -servername dc01.vektor.lab </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

Смотри: `issuer` — это твой корпоративный CA из урока 16, `notAfter` — дата окончания. Если IDP01 не доверяет этому CA, LDAP-федерация упадёт с ошибкой сертификата, хотя «по 389 всё работает».

**8. Чтение AD так, как это делает IdP**

```bash
ldapsearch -H ldaps://dc01.vektor.lab -D "svc-ldap@corp.vektor.lab" -W \
  -b "OU=Company,DC=corp,DC=vektor,DC=lab" "(sAMAccountName=ivanov)" \
  sAMAccountName userPrincipalName mail memberOf objectGUID
```

`-D` — bind DN (AD принимает UPN), `-W` — спросить пароль, `-b` — база поиска, в скобках — фильтр. Именно эти атрибуты маппятся в Keycloak: `sAMAccountName` → логин, `objectGUID` → неизменяемый ID, `memberOf` → группы.

**9. Атрибуты и группы на стороне Windows**

```powershell
Get-ADUser ivanov -Properties sAMAccountName,userPrincipalName,mail,memberOf,passwordLastSet,LockedOut |
  Select-Object sAMAccountName,mail,LockedOut,@{n='Groups';e={($_.memberOf -split ',CN=' | Select-Object -Skip 1) -join '; '}}
Get-ADGroupMember GG-Buh | Select-Object name,samAccountName
```

Смотри `mail` — если пусто, `email`-claim в токене не появится и приложение «не узнает» пользователя; `memberOf` — источник групповых ролей.

**10. SAML: метаданные IdP и разбор ассерта**

```bash
curl -s http://idp01.vektor.lab:8080/realms/vektor/protocol/saml/descriptor | xmllint --format - | head -40
base64 -d /tmp/saml-response.b64 | xmllint --format - | grep -E 'NameID|AttributeValue|NotOnOrAfter'
```

В метаданных SP забирает `SingleSignOnService` (куда слать AuthnRequest) и `X509Certificate` (чем проверять подпись). В ассерте смотри `NameID`, `Audience`, `NotOnOrAfter`.

**11. Время — это часть протокола**

```bash
timedatectl status          # на IDP01
```

```powershell
w32tm /query /status        # на DC01
```

Расхождение больше 5 минут = «invalid signature» в SAML и «clock skew» в Kerberos. Половина «загадочных» SSO-инцидентов — это NTP.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **DC01** — AD DS, DNS, DHCP работают, домен `corp.vektor.lab` жив, есть `ivanov`, группа `GG-Buh`. Для LDAPS нужен настроенный AD CS из урока 16; если его нет — либо пройди урок 16, либо на стенде временно используй `ldap://` + StartTLS (в проде так нельзя).
- **WIN11-CL01** — вход под `ivanov` (пароль `Lab#2026-vektor`), DNS указывает на DC01.
- **UBU01** — для `curl`/`ldapsearch`-проверок.
- **Новая ВМ IDP01** (появляется на этом уроке, сверх таблицы стенда): Ubuntu Server 24.04 LTS, 2 vCPU, 4 ГБ RAM, 20 ГБ диск, адрес `10.10.10.45`, шлюз `10.10.10.2`, DNS `10.10.10.10`. Дальше по стенду она остаётся как «IdP компании Вектор».
- Интернет с IDP01 нужен только для скачивания дистрибутивов; если из РФ есть проблемы с загрузкой — бери пакет из зеркала вуза/компании или ставь Keycloak из Docker-образа, зеркалированного локально.

### Снапшот

В VMware Workstation: выделить ВМ → правый клик → **Snapshot → Take Snapshot**, имя `before-sysadmin-017`. Сделать для **DC01** и **WIN11-CL01**. Для IDP01 (новая ВМ) снапшот делается сразу после установки ОС и сети, тоже с именем `before-sysadmin-017` — так откат вернёт тебя к «чистой Ubuntu с сетью».

### Шаги

1. **IDP01.** Создать ВМ, установить Ubuntu Server 24.04. Настроить статику через netplan (`/etc/netplan/50-cloud-init.yaml`), `sudo netplan apply`. Ожидаемо: `ping dc01.vektor.lab` и `ping corp.vektor.lab` отвечают.

```bash
sudo hostnamectl set-hostname idp01
sudo apt update && sudo apt install -y openjdk-21-jre-headless curl jq unzip ldap-utils ufw
timedatectl status   # System clock synchronized: yes
```

2. **DC01 — DNS-запись для IdP.** Чтобы браузер с WIN11-CL01 видел `idp01.vektor.lab`:

```powershell
Add-DnsServerResourceRecordA -Name idp01 -ZoneName corp.vektor.lab -IPv4Address 10.10.10.45
```

Ожидаемо на CL01: `nslookup idp01.vektor.lab` → `10.10.10.45`.

3. **DC01 — сервисная учётка для LDAP-bind.**

```powershell
$p = ConvertTo-SecureString 'Svc#Ldap-2026' -AsPlainText -Force
New-ADUser -Name "svc-ldap" -SamAccountName "svc-ldap" -UserPrincipalName "svc-ldap@corp.vektor.lab" `
  -AccountPassword $p -Enabled $true -PasswordNeverExpires $true `
  -Description "Keycloak LDAP federation (lab)" -Path "OU=IT,OU=Company,DC=corp,DC=vektor,DC=lab"
```

Ожидаемо: `Get-ADUser svc-ldap` возвращает объект. *В проде вместо этого — gMSA (Group Managed Service Account), `PasswordNeverExpires` там не нужен.*

4. **IDP01 — проверка LDAPS и bind.**

```bash
openssl s_client -connect dc01.vektor.lab:636 -servername dc01.vektor.lab </dev/null 2>/dev/null | openssl x509 -noout -issuer -dates
ldapsearch -H ldaps://dc01.vektor.lab -D "svc-ldap@corp.vektor.lab" -W -b "DC=corp,DC=vektor,DC=lab" "(sAMAccountName=ivanov)" sAMAccountName memberOf
```

Ожидаемо: сертификат от корпоративного CA, `memberOf: CN=GG-Buh,...`. Если ошибка `unable to get local issuer certificate` — поставь корневой CA из урока 16 в `/usr/local/share/ca-certificates/` и `sudo update-ca-certificates`.

5. **IDP01 — установка и запуск Keycloak.** Скачать актуальный `keycloak-<версия>.tar.gz` с keycloak.org [ПРОВЕРИТЬ актуальную версию], распаковать в `/opt/keycloak`, `chown -R keycloak:keycloak`. Запуск в dev-режиме:

```bash
sudo KC_BOOTSTRAP_ADMIN_USERNAME=admin KC_BOOTSTRAP_ADMIN_PASSWORD='Lab#2026-vektor' \
  /opt/keycloak/bin/kc.sh start-dev --http-port=8080
```

В старых версиях переменные называются `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` [ПРОВЕРИТЬ для своей версии]. Ожидаемо: в логе `Listening on: http://0.0.0.0:8080`, `start-dev` использует встроенную H2 и **без TLS** — только для лабы.

```bash
sudo ufw allow from 10.10.10.0/24 to any port 8080 proto tcp
sudo ufw allow from 10.10.10.0/24 to any port 8081 proto tcp
```

6. **Создать realm `vektor`.** В браузере на хосте: `http://idp01.vektor.lab:8080` → логин `admin`. Слева выпадающий список realm → **Create Realm** → Realm name `vektor`. Ожидаемо: пустой realm с надписью `vektor`.

7. **Федерация с AD.** В realm `vektor`: **User federation → Add provider → ldap**. Заполнить (ключи конфигурации сверяй с подсказками формы [ПРОВЕРИТЬ]):
   - Vendor: `Active Directory`, Connection URL: `ldaps://dc01.vektor.lab:636`, Truststore: `Only for ldaps`;
   - Bind DN: `svc-ldap@corp.vektor.lab`, Bind credential: пароль, Edit mode: `READ_ONLY`;
   - Users DN: `OU=Company,DC=corp,DC=vektor,DC=lab`, Username attribute: `sAMAccountName`, UUID attribute: `objectGUID`, RDN: `cn`, Object classes: `person,organizationalPerson,user`.

Нажать **Test connection** → `Success`, **Test authentication** → `Success`, затем **Sync all users**. Ожидаемо: в **Users** появились `ivanov`, `petrova`, `sidorov`.

8. **Маппинг групп.** В той же карточке федерации → вкладка **Mappers → create** → Mapper type `group-ldap-mapper`: Groups DN `OU=Company,DC=corp,DC=vektor,DC=lab`, Group name attribute `cn`, Membership attribute `member`, Group object classes `group`, Preserve group inheritance `ON` [ПРОВЕРИТЬ набор полей]. Нажать **Sync all users**. Ожидаемо: в **Groups** появились `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT`, и `ivanov` состоит в `GG-Buh`.

9. **OIDC-клиент и живой вход.** **Clients → Create client**: Client ID `wiki-vektor`, Client authentication `ON` (конфиденциальный), Standard flow `ON`, Direct access grants `OFF`, Valid redirect URIs `http://localhost:9999/callback`, Web origins `+`. Со вкладки **Credentials** забрать secret. Сгенерировать PKCE-пару (команда №2 из раздела 4), собрать в браузере URL:

```text
http://idp01.vektor.lab:8080/realms/vektor/protocol/openid-connect/auth?client_id=wiki-vektor&response_type=code&scope=openid%20profile%20email&redirect_uri=http%3A%2F%2Flocalhost%3A9999%2Fcallback&code_challenge=<CHALLENGE>&code_challenge_method=S256
```

Войти как `ivanov` / `Lab#2026-vektor`. Браузер уйдёт на `http://localhost:9999/callback?code=...` и покажет ошибку соединения — это нормально: **скопируй `code` из адресной строки**. Обменять код на токены (команда №3) и разобрать `id_token` (команда №5). Ожидаемо в claims: `preferred_username: ivanov`, `iss` с `…/realms/vektor`.

10. **SAML-клиент и разбор ассерта.** Создать **Clients → Create client → SAML**: Client ID `saml-demo`, Valid redirect URI / ACS: `http://idp01.vektor.lab:8081/sp`, Name ID format `username`, Signature algorithm `RSA_SHA256` [ПРОВЕРИТЬ названия полей в своей версии]. На IDP01 положить тестовый SP:

```python
# /opt/sp/sp.py  — учебный приёмник SAML, НЕ валидирует подпись!
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        q = parse_qs(self.rfile.read(n).decode())
        open('/tmp/saml-response.b64', 'w').write(q.get('SAMLResponse', [''])[0])
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
        self.wfile.write('<h1>OK</h1><p>Saved to /tmp/saml-response.b64</p>'.encode())
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'SP ready')

HTTPServer(('0.0.0.0', 8081), H).serve_forever()
```

```bash
sudo nohup python3 /opt/sp/sp.py >/var/log/sp.log 2>&1 &
```

Открыть в браузере `http://idp01.vektor.lab:8080/realms/vektor/protocol/saml/clients/saml-demo`, войти `ivanov`. Ожидаемо: страница `OK`, файл `/tmp/saml-response.b64` создан. Разобрать (команда №10) — увидишь `NameID` и `AttributeStatement`.

11. **Записать в `stand-log.md`** всё, что создано (см. блок «Что изменилось на стенде»).

### Проверка

- [ ] `nslookup idp01.vektor.lab` с WIN11-CL01 возвращает `10.10.10.45`.
- [ ] `timedatectl status` на IDP01 показывает синхронизацию; `w32tm /query /status` на DC01 без больших отклонений.
- [ ] В Keycloak в **Users** видны `ivanov`, `petrova`, `sidorov` из AD, пароли в AD не менялись.
- [ ] `ivanov` входит в Keycloak **доменным паролем** `Lab#2026-vektor`, локальной копии пароля нет.
- [ ] В **Groups** есть `GG-Buh`, и `ivanov` в ней; в `id_token` приходит `groups` (если нет — добавь mapper `Group Membership` в client scope `profile`).
- [ ] OIDC: код обменян на токены, `expires_in = 300`, `iss` совпадает с discovery.
- [ ] SAML: `/tmp/saml-response.b64` не пустой, `base64 -d | xmllint --format -` показывает `Audience = saml-demo`.
- [ ] В `stand-log.md` появилась запись про IDP01, realm `vektor`, `svc-ldap`, клиентов `wiki-vektor` и `saml-demo`.

### Сломай и почини

> **Пользователь:** Иван Иванов / WIN11-CL01
> **Что написал:** «Портал согласования счетов пишет „Invalid username or password“, хотя в Windows я захожу под тем же паролем. Вчера всё работало.»
> **Срочность:** Высокая (закрытие месяца).

**Воспроизведение поломки:** на DC01 сменить пароль сервисной учётки, не меняя его в Keycloak:

```powershell
Set-ADAccountPassword -Identity svc-ldap -Reset -NewPassword (ConvertTo-SecureString 'Svc#Ldap-BROKEN' -AsPlainText -Force)
```

**Диагностика по порядку:**

1. Проверить, что проблема не в самом Иванове: он входит в Windows и в 1С? Если да — AD и его пароль живы, дело в цепочке IdP.
2. В логах Keycloak (`kc.sh` в консоли или `journalctl`) найти ошибку LDAP: типовой код — `[LDAP: error code 49 - 80090308 ... AcceptSecurityContext error, data 52e]` = неверные учётные данные bind.
3. Воспроизвести руками с IDP01: `ldapsearch -H ldaps://dc01.vektor.lab -D "svc-ldap@corp.vektor.lab" -W -b "DC=corp,DC=vektor,DC=lab" "(sAMAccountName=ivanov)" sAMAccountName` → `Invalid credentials`. Значит, ломается не пользователь, а служебный bind.
4. Проверить состояние учётки и срок действия сертификата DC:

```powershell
Get-ADUser svc-ldap -Properties LockedOut,PasswordExpired,PasswordLastSet,Enabled | Select-Object Name,LockedOut,PasswordExpired,Enabled,PasswordLastSet
Search-ADAccount -LockedOut
```

5. Если бы пароль не менялся, а bind падал — смотреть `openssl s_client` на `:636` (истёк сертификат AD CS) и время на IDP01.

**Решение:** задать `svc-ldap` новый пароль и прописать его в **User federation → Bind credential** → **Test authentication** → `Success`. Перезайти в портал — вход проходит.

**Ответ пользователю:** «Иван, добрый день. Проблема была на стороне интеграции портала с доменом: сервисная учётная запись, через которую портал читает каталог, потеряла доступ. Уже исправил, вход работает. Пароль от Windows менять не нужно — попробуйте сейчас и напишите, если что-то не так».

**Закрытие:** тип «Инцидент», причина «смена пароля сервисной учётки без обновления в IdP», действие «обновить credential + добавить проверку bind в мониторинг». Профилактика: gMSA или `PasswordNeverExpires` + алерт на `error code 49` в логах IdP.

### Что изменилось на стенде

- Новая ВМ **IDP01**: Ubuntu Server 24.04 LTS, `10.10.10.45`, hostname `idp01`, Keycloak в dev-режиме на `:8080`, тестовый SP на `:8081`, каталог `/opt/sp`.
- **DC01**: A-запись `idp01` в зоне `corp.vektor.lab`; учётка `svc-ldap` в `OU=IT,OU=Company`; открыт `636/tcp` (если AD CS настроен).
- **Keycloak**: realm `vektor`, LDAP-федерация `dc01.vektor.lab` (read-only), синхронизированы пользователи и группы `GG-*`; клиенты `wiki-vektor` (OIDC, confidential, PKCE) и `saml-demo` (SAML).
- **WIN11-CL01**: без изменений, использовалась как клиент для входа `ivanov`.

### Откат

VMware Workstation → выбрать ВМ → **Snapshot → Snapshot Manager** → `before-sysadmin-017` → **Go to**. Для IDP01 откат вернёт «чистую Ubuntu с сетью», для DC01 — состояние без записи `idp01` и без `svc-ldap`. Если откатывать не хочется (стенд нужен для следующих глав) — оставь как есть и просто внеси правки в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Поставить IdP «на том же сервере, что и всё остальное»** → падение DC01 или FS01 валит вход во все сервисы сразу. Правильно: IdP — отдельный узел (лучше два + балансировщик) и вне домена-контроллера.
2. **Забыть про время** → «invalid signature», «clock skew», случайные отказы раз в неделю. Правильно: NTP на всех узлах, мониторинг расхождения.
3. **`start-dev` в проде** → H2-база, отсутствие TLS, отсутствие кластеризации. Правильно: PostgreSQL, TLS-терминация, `start` вместо `start-dev`, `--hostname` с реальным FQDN.
4. **Публичный клиент + client_secret в JavaScript** → секрет виден любому в DevTools. Правильно: для SPA — public client + PKCE, без секрета; секреты — только на бэкенде.
5. **Слишком широкий `redirect_uri`** (wildcard, «любой путь») → перехват authorization code через чужой домен. Правильно: точный список URI, без `*`.
6. **Bind-учётка с правами Domain Admin** → один украденный пароль = весь домен. Правильно: минимальные права на чтение нужных OU, gMSA, ротация.
7. **«Синхронизировали один раз»** → уволенные остаются активными в IdP и заходят в сервисы. Правильно: регулярная синхронизация + SCIM-депро­вижининг + отчёт по «сиротам».
8. **Никаких claim/group-маппингов не задокументировано** → через полгода никто не знает, почему `sidorov` видит бухгалтерские отчёты. Правильно: таблица «атрибут AD → claim → роль в приложении» в вики.

**Безопасность.** В боевой среде нельзя: гонять LDAP simple bind по `389` без TLS (пароль сервисной учётки уходит открытым текстом); отключать проверку подписи SAML «чтобы заработало» (мы это делали в учебном `sp.py` — так делать нельзя, ассерт обязан проверяться по сертификату IdP с контролем `Audience`, `NotOnOrAfter`, `InResponseTo`); хранить секреты клиентов в git; оставлять `admin/admin` и дефолтный мастер-realm; выдавать доступ по принципу «все в одну группу». И обязательное: **break-glass администратор** в локальной базе IdP, чтобы не оказаться в ситуации «AD лёг — войти некуда».

## 7. Вопросы на собеседовании

**Вопрос 1. Чем OIDC отличается от OAuth 2.0 и почему нельзя «просто использовать OAuth для логина»?**
Ответ: OAuth 2.0 — про делегирование доступа к ресурсу, он выдаёт access_token и ничего не говорит о личности пользователя. OIDC — надстройка, которая добавляет аутентификацию: `id_token` (JWT с `sub`, `iss`, `aud`, `exp`), endpoint `userinfo` и стандартные scopes `openid profile email`. Логин по «голому» OAuth — это подмена понятий: приложение получает токен доступа и делает вид, что знает, кто перед ним.
*Что хочет услышать интервьюер:* чёткое разделение authn/authz и знание, что `id_token` — не access_token, его нельзя слать в API.
*Красный флаг:* «OAuth и OIDC — это одно и то же, просто новее».

**Вопрос 2. У вас AD и облачный каталог. Как выбрать между PHS, PTA и федерацией?**
Ответ: PHS — самый простой и устойчивый (облако проверяет хеш, работает даже при падении локальной сети), но хеш уходит в облако и локальные политики паролей частично теряются. PTA — пароль не покидает периметр, проверку делает агент, но появляется зависимость от доступности агента. Федерация (AD FS/сторонний IdP) — максимум контроля и свои политики, но самая дорогая в эксплуатации и с наибольшим числом точек отказа. В РФ-реалиях 2026 облачная часть часто недоступна, поэтому чаще строят self-hosted IdP с федерацией по LDAP/SAML.
*Что хочет услышать интервьюер:* сравнение по осям «простота / безопасность / отказоустойчивость / стоимость эксплуатации».
*Красный флаг:* «всегда берём федерацию, она же правильная».

**Вопрос 3. Пользователь не может войти в приложение через SSO. Ваши действия?**
Ответ: сузить область — входит ли он в Windows (жив ли AD), входит ли в другие SSO-приложения (жив ли IdP), воспроизводится ли проблема на другом пользователе/браузере. Дальше по цепочке: логи IdP (bind к LDAP, ошибки валидации), время на узлах, срок сертификатов (LDAPS, подписи SAML), маппинг групп/claims, состояние сервисной учётки, `redirect_uri`. Отдельно проверить, не истёк ли пароль/не заблокирована ли учётка.
*Что хочет услышать интервьюер:* методичная диагностика по слоям, а не «перезагрузил IdP — заработало».
*Красный флаг:* сразу менять конфиг федерации без сужения проблемы.

**Вопрос 4. Что обязательно проверить в SAML-ассерте на стороне SP?**
Ответ: подпись (сертификатом IdP, из метаданных), `Audience` = мой SP, `Destination`/ACS URL, `NotBefore`/`NotOnOrAfter` с учётом clock skew, `InResponseTo` = выданный мной ID запроса (защита от replay), `NameID` и атрибуты, а также то, что ассерт получен по HTTPS и не принят повторно. Для IdP-initiated сценария `InResponseTo` не проверяется — поэтому он и считается менее безопасным.
*Что хочет услышать интервьюер:* параметры валидации, а не «смотрим имя».
*Красный флаг:* «проверяем только подпись, остальное неважно».

**Вопрос 5. Как вы обеспечите отключение сотрудника во всех сервисах в день увольнения?**
Ответ: единый источник истины — AD; IdP синхронизируется с ним (LDAP-федерация + периодическая синхронизация), приложения получают членство через группы, а не через ручные списки; для внешних SaaS — SCIM-провижининг или регулярный отчёт о расхождениях; плюс сокращённое время жизни сессии и refresh-токенов, чтобы «уже выданный» доступ умирал быстро; блокировка учётки в AD запускает цепочку. Проверяется тестом: уволили тестового пользователя — через сколько минут он потерял доступ везде.
*Что хочет услышать интервьюер:* понимание жизненного цикла учётки и того, что SSO без депро­вижининга — дыра.
*Красный флаг:* «отключаем вручную в каждом сервисе по списку от HR».

## 8. Источники (официальная документация)

- **keycloak.org** — раздел *Server Administration Guide* (User Federation / LDAP, Identity Brokering and Federation, Groups and mappers), *Securing Applications and Services Guide* (OIDC и SAML клиенты). Поиск: `keycloak ldap user federation active directory`, `keycloak saml client configuration`.
- **learn.microsoft.com** — *Microsoft Entra Connect Sync: Understand and customize synchronization*, *Microsoft Entra hybrid identity documentation*, *What is Pass-through authentication*, *Seamless Single Sign-On*. Поиск: `entra connect sync hybrid identity options`, `password hash sync vs pass-through authentication`.
- **openid.net** — *OpenID Connect Core 1.0* и *OpenID Connect Discovery*. Поиск: `openid connect core spec id_token`.
- **rfc-editor.org** — RFC 6749 (OAuth 2.0), RFC 7636 (PKCE), RFC 7519 (JWT), RFC 7643/7644 (SCIM 2.0). Поиск: `rfc 7636 pkce`.
- **docs.oasis-open.org** — *SAML V2.0 Core* и *Bindings*. Поиск: `saml 2.0 core assertion specification`.
- **ubuntu.com** — *Ubuntu Server documentation* (netplan, systemd-timesyncd, ufw). Поиск: `ubuntu server netplan static ip`.
- **Документация отечественных решений** (ALD Pro — astralinux.ru, РЕД ИДМ — red-soft.ru): разделы про интеграцию с AD и SSO. Прямые ссылки и точные названия разделов [ПРОВЕРИТЬ] — сверяй на сайте вендора, линейка и структура документации меняются.
