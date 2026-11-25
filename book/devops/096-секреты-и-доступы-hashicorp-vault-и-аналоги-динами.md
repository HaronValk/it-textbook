# Секреты и доступы: HashiCorp Vault, динамические секреты, PKI и ротация

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4821**

> **Пользователь:** sidorov (Алексей Сидоров, склад)
> **ВМ:** GIT01
> **Что написал:** «Разбирал с подрядчиком интеграцию склада с платёжным сервисом и наткнулся на репозиторий `svc-sklad-sync` в GitLab — там в файле `.env` лежит логин и пароль от PostgreSQL. Пароль рабочий, я проверил. И он там уже полгода висит. Так и должно быть?»
> **Срочность:** высокая

Это самая частая форма инцидента, связанного с секретами. Пароль попал в Git — и с этого момента он считается скомпрометированным навсегда: история коммитов хранит его даже после удаления файла. Дальше — стандартный цикл: смена пароля, правка кода, проверка, что больше нигде не осталось, объяснение разработчикам, почему `.env` в репозитории — не «ну так удобнее».

Проблема системная. В среднем проекте секретов десятки: пароли БД, API-ключи платёжных шлюзов, токены интеграций, SSH-ключи, TLS-сертификаты. Если ими управляют вручную, они живут годами, лежат в переписке и в вики, и никто не знает, кто и когда их видел. Vault (и его аналоги) решает три задачи: **централизованное хранение** (одно место, а не двадцать конфигов), **выдача по запросу с TTL** (пароль живёт час, а не три года) и **аудит** (в логе видно, кто какой секрет прочитал и когда).

После этой главы ты должен уметь: поднять Vault на стенде, положить туда статический секрет, выдать его сервису через AppRole, настроить динамические учётки PostgreSQL, выпустить сертификат через встроенный PKI, настроить аудит и понимать, чем `vault operator rotate` отличается от ротации пароля в базе.

## 2. Теория «под капотом»

**Секрет** — это любая строка, дающая доступ: пароль, токен, ключ, сертификат. **Управление секретами (secrets management)** — это про их жизненный цикл: создание → хранение → выдача → ротация → отзыв.

**Как устроен Vault.** Это отдельный сервис с HTTP API на порту **8200** (там же веб-интерфейс), межсерверный кластерный порт — **8201**. Внутри:

- **Storage backend** — где физически лежат данные. В лабе — встроенный **Raft** (`storage "raft"`), то есть данные пишутся на локальный диск. Снаружи они всегда зашифрованы.
- **Barrier** — криптографический слой шифрования. Пока он «запечатан» (**sealed**), Vault не отдаёт ничего. Ключ шифрования восстанавливается из **unseal-ключей** (например, 5 долей, порог 3). Восстановили 3 доли — Vault **unseal**, начал работать. Перезагрузка сервера = снова sealed, пока кто-то не разожмёт.
- **Auth methods** — как клиент доказывает, что он тот, за кого себя выдаёт: `token`, `approle`, `kubernetes`, `userpass`, `jwt/oidc`, `ldap`.
- **Secrets engines** — плагины, которые умеют *генерировать* секреты: `kv` (статическое хранилище), `database` (динамические учётки БД), `pki` (сертификаты), `transit` (шифрование как сервис), `ssh`, `aws`.
- **Lease (аренда)** — у всего выданного есть срок. Кончился TTL — Vault сам отзывает секрет (удаляет пользователя БД, отзывает токен).
- **Audit devices** — журнал всех обращений к API с хешированием самих секретов.

**Динамические секреты** — главная идея Vault. Вместо того чтобы хранить пароль к базе, Vault хранит *права администратора на создание пользователей* и на каждый запрос делает `CREATE ROLE ... VALID UNTIL ...`. Сервис получает уникальный логин вида `v-approle-web01-a1b2c3d4`, который через час просто исчезает из базы. Утечка такого пароля почти безопасна: он мёртв через TTL, и по нему видно, кто его получил.

**PKI.** Vault может быть удостоверяющим центром: держит **корневой CA** (в идеале — офлайн, вне Vault) и **промежуточный CA** (уже внутри) и выдаёт сертификаты по запросу с коротким TTL — сутки, а не год. Короткий TTL — это и есть ротация сертификатов: старый истекает, новый выдаётся автоматически.

**Ротация — три разных вещи, которые путают:**

1. **Ротация статического секрета** (KV): поменяли пароль в системе → записали новую версию в Vault → приложения подхватили.
2. **Ротация root-учётки** (`database/rotate-root/...`): Vault меняет пароль администратора, который сам использует для подключения к БД, и дальше знает его только Vault. Никто из людей.
3. **Ротация ключа шифрования** (`vault operator rotate`): Vault генерирует новый ключ шифрования; старые данные перешифровываются лениво, при чтении.

**Место в общей картине.** Это L7: HTTPS-API, TLS между клиентом и Vault, идентичность сервиса вместо пароля в конфиге. Ниже — L4/L3 (порты 8200/8201, маршрутизация до UBU01), ещё ниже — диск.

```text
        GIT01 (CI)          k8s-cp01/w01/w02          WEB01 (nginx)
            |                       |                       |
            | AppRole / JWT         | kubernetes auth       | TLS-сертификат
            v                       v                       ^
   +---------------------------------------------------+    |
   |  UBU01  10.10.10.30 :8200   Vault server          |----+
   |                                                    |
   |  auth:   token, approle, kubernetes, userpass      |
   |  engines: secret/   (KV v2, статические секреты)   |
   |           database/ (динамические учётки PostgreSQL)|
   |           pki/ + pki_int/ (сертификаты)            |
   |           transit/  (шифрование как сервис)        |
   |  audit:  file -> /var/log/vault/audit.log          |
   +------------------------+---------------------------+
                            | storage "raft" (шифровано barrier'ом)
                            v
                     /opt/vault/data
```

**Лицензия.** С августа 2023 года HashiCorp Vault распространяется под BUSL 1.1 — это не «свободная» лицензия в привычном смысле, для внутреннего использования бесплатно, но для перепродажи как сервиса — нет `[ПРОВЕРИТЬ актуальные условия на developer.hashicorp.com]`. Форк под открытой лицензией — **OpenBao** (Linux Foundation), API совместим. Из РФ скачивание бинарников HashiCorp может быть затруднено — легальная альтернатива та же OpenBao либо облачные secret-менеджеры российских провайдеров. Подробно про импортозамещение — урок 102.

## 3. Аналогия из жизни

Представь банковское хранилище с ячейками. Клиент не носит ключ от сейфа с собой — он приходит, показывает паспорт (аутентификация), ему выдают **разовый пропуск** в конкретную ячейку (политика доступа) на 15 минут (TTL). В журнале охраны записано: «Иванов, ячейка 12, 14:03» (аудит). Если Иванов попытается открыть чужую ячейку — охрана не пустит (политика). Если пропуск потерян — он всё равно истечёт через 15 минут (lease revocation). Банк может сам, не спрашивая клиента, переложить содержимое в новый сейф с новым замком (ротация ключа шифрования). А ещё у банка есть нотариальный отдел: он заверяет документы (PKI) — и делает это быстро и на короткий срок, чтобы подделанная доверенность не жила десятилетиями.

Аналогия перестаёт работать в одном месте: пропуск в банке выдают человеку, а в Vault — программе, и программа не может «заглянуть» в хранилище без токена, который она получила на старте; именно поэтому первичная аутентификация сервиса (AppRole, kubernetes auth) — самое уязвимое место всей схемы.

## 4. Практика и команды

Все команды — с UBU01, `VAULT_ADDR=http://10.10.10.30:8200`.

**1. Запуск в dev-режиме (только для экспериментов)**

```bash
vault server -dev -dev-root-token-id=root -dev-listen-address=0.0.0.0:8200
```

`-dev` — хранилище в памяти, автоматически распечатан, root-токен фиксированный. `-dev-listen-address` — чтобы было доступно не только с localhost. В выводе Vault печатает `Root Token: root` и `Api Address`. **В прод так нельзя**: всё в памяти, все секреты открыты тому, кто знает токен `root`.

**2. Продовый конфиг и инициализация**

```hcl
# /etc/vault.d/vault.hcl
ui            = true
api_addr      = "http://10.10.10.30:8200"
cluster_addr  = "http://10.10.10.30:8201"
disable_mlock = true

storage "raft" {
  path    = "/opt/vault/data"
  node_id = "vault01"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
```

```bash
sudo systemctl enable --now vault
vault status
vault operator init -key-shares=5 -key-threshold=3
```

`-key-shares` — на сколько частей делится unseal-ключ, `-key-threshold` — сколько частей нужно. `init` **один раз**: повторный вызов на инициализированном хранилище выдаст ошибку. `vault status` показывает `Sealed: true/false`, `HA Mode`, версию. `disable_mlock = true` разрешён только потому, что на ВМ нет прав на mlock и swap не критичен — в проде это прямой риск утечки ключей в swap-файл, там mlock обязателен.

**3. Распечатывание и вход**

```bash
vault operator unseal            # три раза, каждый раз вводим свою долю
vault login                      # вставляем initial root token
vault token lookup
```

`vault token lookup` покажет `policies`, `ttl`, `creation_ttl`, `display_name`. Смотреть надо на `ttl` — если там `0s` при `explicit_max_ttl=0`, это root-токен без срока, и с ним нельзя ходить по серверам.

**4. KV v2: статические секреты и версии**

```bash
vault secrets enable -path=secret kv-v2
vault kv put secret/vektor/svc-sklad-sync db_user=svc_sklad db_password='Lab#2026-vektor'
vault kv get secret/vektor/svc-sklad-sync
vault kv get -field=db_password secret/vektor/svc-sklad-sync
vault kv metadata get secret/vektor/svc-sklad-sync
vault kv delete -versions=1 secret/vektor/svc-sklad-sync
```

`-path` — где смонтирован движок (по умолчанию `secret/`). `kv put` создаёт **новую версию**, а не перезаписывает: `metadata get` покажет `current_version` и историю, `delete -versions=1` помечает версию удалённой (данные ещё можно `undelete`), `destroy` — стирает безвозвратно. Это и есть механика «откатить секрет на предыдущую версию, если новая не подошла».

**5. Политика: кому что можно**

```hcl
# /etc/vault/policies/svc-sklad-sync.hcl
path "secret/data/vektor/svc-sklad-sync" {
  capabilities = ["read"]
}
path "database/creds/sklad-sync-ro" {
  capabilities = ["read"]
}
path "secret/metadata/vektor/svc-sklad-sync" {
  capabilities = ["read", "list"]
}
```

```bash
vault policy write svc-sklad-sync /etc/vault/policies/svc-sklad-sync.hcl
vault policy list
```

Важно: для KV v2 путь в политике — `secret/data/...` (данные) и `secret/metadata/...` (метаданные/список версий), а не `secret/...`. Классическая ошибка — дать `["read","list"]` на `secret/*` и открыть сервису все секреты стенда.

**6. AppRole: как сервис логинится без человека**

```bash
vault auth enable approle
vault write auth/approle/role/svc-sklad-sync \
  token_policies="svc-sklad-sync" token_ttl=20m token_max_ttl=1h secret_id_ttl=10m
vault read -field=role_id auth/approle/role/svc-sklad-sync/role-id
vault write -f -field=secret_id auth/approle/role/svc-sklad-sync/secret-id
vault write -field=token auth/approle/login role_id="$ROLE_ID" secret_id="$SECRET_ID"
```

`role_id` — «логин», он статичен и может лежать в конфиге. `secret_id` — «пароль», он короткоживущий (`secret_id_ttl=10m`) и выдаётся один раз (`-f` = force, создать новый). `token_ttl`/`token_max_ttl` ограничивают токен, который получит сервис: 20 минут, продлевать до часа максимум.

**7. Динамические учётки PostgreSQL**

```bash
vault secrets enable database
vault write database/config/vektor-pg \
  plugin_name=postgresql-database-plugin \
  allowed_roles="sklad-sync-ro" \
  connection_url="postgresql://{{username}}:{{password}}@127.0.0.1:5432/postgres?sslmode=disable" \
  username="vault_admin" password="$PG_VAULT_ADMIN_PASS"

vault write database/roles/sklad-sync-ro \
  db_name=vektor-pg default_ttl=1h max_ttl=24h \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{{name}}\";"

vault read database/creds/sklad-sync-ro
```

`{{name}}`, `{{password}}`, `{{expiration}}` — шаблоны, которые Vault подставляет сам. Вывод:

```text
lease_id       database/creds/sklad-sync-ro/3fA9x...
lease_duration 1h
username       v-approle-sklad-sy-Ab12CdE
password       A1b2C3d4E5f6-9XyZ
```

Смотреть надо на `lease_duration`: через час роль `v-approle-...` исчезнет из PostgreSQL сама.

**8. Управление арендами**

```bash
vault list sys/leases/lookup/database/creds/sklad-sync-ro
vault lease renew database/creds/sklad-sync-ro/3fA9x...
vault lease revoke database/creds/sklad-sync-ro/3fA9x...
vault lease revoke -prefix database/creds/sklad-sync-ro
```

`-prefix` отзывает **все** аренды роли разом — это аварийная кнопка «уволить всех, кто ходил в базу этим путём» и одновременно способ уронить сервис, который не умеет переподключаться.

**9. PKI: свой удостоверяющий центр**

```bash
vault secrets enable pki
vault secrets tune -max-lease-ttl=87600h pki
vault write pki/root/generate/internal common_name="Vektor Lab Root CA" ttl=87600h

vault secrets enable -path=pki_int pki
vault secrets tune -max-lease-ttl=43800h pki_int
vault write -format=json pki_int/intermediate/generate/internal \
  common_name="Vektor Lab Intermediate CA" | jq -r '.data.csr' > /tmp/pki_int.csr
vault write -format=json pki/root/sign-intermediate csr=@/tmp/pki_int.csr \
  format=pem_bundle ttl=43800h | jq -r '.data.certificate' > /tmp/pki_int.crt
vault write pki_int/intermediate/set-signed certificate=@/tmp/pki_int.crt

vault write pki_int/roles/vektor-dot-lab \
  allowed_domains="vektor.lab" allow_subdomains=true max_ttl="720h"
vault write pki_int/issue/vektor-dot-lab common_name="web01.vektor.lab" ttl="24h"
```

`generate/internal` создаёт ключ + самоподписанный сертификат прямо в Vault. `sign-intermediate` подписывает CSR промежуточного CA корневым. `allowed_domains` — критично: без него Vault подпишет сертификат на `sberbank.ru`, если попросить. Ответ `issue` содержит `certificate`, `private_key`, `issuing_ca`, `ca_chain`, `serial_number`.

**10. Аудит и ротация**

```bash
vault audit enable file file_path=/var/log/vault/audit.log
vault operator rotate
vault write -f database/rotate-root/vektor-pg
sudo tail -n 2 /var/log/vault/audit.log
```

`audit enable` — делать **до** того, как положишь первый реальный секрет. `vault operator rotate` перевыпускает ключ шифрования барьера (данные перешифровываются по мере чтения). `database/rotate-root/vektor-pg` меняет пароль `vault_admin` в PostgreSQL и никому его не показывает.

**11. Kubernetes и CI (кратко, без дублей с уроками 99–100)**

```bash
vault auth enable kubernetes
vault write auth/kubernetes/role/web01 \
  bound_service_account_names=web01 bound_service_account_namespaces=default \
  policies=svc-sklad-sync ttl=1h
```

Под с `serviceAccountName: web01` предъявляет Vault'у свой JWT, получает токен — паролей в манифестах нет вообще. Для CI на GIT01 схема похожая, но через JWT GitLab'а `[ПРОВЕРИТЬ актуальное имя переменной с ID-токеном в вашей версии GitLab]`.

## 5. Лабораторная работа

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — SSH под своей учётной записью, `sudo`, доступ в интернет или локальное зеркало пакетов. На ней ставим Vault и PostgreSQL 16 `[ПРОВЕРИТЬ версию в репозитории 24.04]`.
- **WEB01** (10.10.20.10, nginx) — существует с уровня L2, пригодится в шаге 7 для проверки сертификата. Если её нет — шаг 7 проверяем только через `openssl`, это нормально.
- **GIT01** (10.10.10.50) — только для шага 10 (опционально).
- Домен `vektor.lab` и правило: `vault.vektor.lab` в лабе не настраиваем, ходим по IP — DNS-зону под это не заводим.

Установка Vault (вариант с официального репозитория; если из РФ недоступен — бинарник с releases.hashicorp.com или OpenBao):

```bash
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y vault postgresql
```

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-096`. Сделать на **UBU01**, **WEB01**, **GIT01** (если трогаем). Если ВМ выключена — снапшот можно снять и на выключенной, это быстрее и чище.

### Шаги

1. **UBU01.** Проверить, что порт 8200 свободен: `sudo ss -tulpn | grep 8200` — пусто. Ожидаемо: вывода нет.
2. **UBU01.** Создать `/etc/vault.d/vault.hcl` (конфиг из раздела 4, п.2), каталоги `sudo mkdir -p /opt/vault/data /var/log/vault && sudo chown -R vault:vault /opt/vault /var/log/vault`, запустить `sudo systemctl enable --now vault`. Ожидаемо: `vault status` → `Sealed: true`, `Initialized: false`.
3. **UBU01.** `vault operator init -key-shares=5 -key-threshold=3 -format=json > ~/vault-init.json && chmod 600 ~/vault-init.json`. Ожидаемо: в файле 5 `unseal_keys_b64` и `root_token`. Сразу добавить `vault-init.json` в `.gitignore` и мысленно представить, что в проде этот файл распечатывают и кладут в сейф, а не в домашний каталог `vault`.
4. **UBU01.** Три раза `vault operator unseal` разными ключами, затем `vault login` root-токеном, `vault token lookup`. Ожидаемо: `Sealed: false`, в `token lookup` политика `root`.
5. **UBU01.** Создать «админский» токен для работы вместо root `[ПРОВЕРИТЬ синтаксис `vault token create -policy=admin`]`, включить KV v2, положить секрет, включить аудит. Ожидаемо: `vault audit list` показывает `file/`, в `/var/log/vault/audit.log` появились строки.
6. **UBU01.** Политика + AppRole из раздела 4 (пп. 5–6), залогиниться и прочитать секрет выданным токеном. Ожидаемо: `vault kv get secret/vektor/svc-sklad-sync` работает, а `vault kv get secret/vektor/other` → `403 permission denied`.
7. **UBU01.** PostgreSQL: `sudo -u postgres psql -c "CREATE ROLE vault_admin LOGIN SUPERUSER PASSWORD 'Lab#2026-vektor';"` (SUPERUSER — только для лабы!), затем включить `database/`, настроить `config` и роль `sklad-sync-ro`, прочитать `database/creds/sklad-sync-ro`. Ожидаемо: логин `v-approle-...` и пароль; `sudo -u postgres psql -c "\du"` показывает новую роль.
8. **UBU01.** `sudo -u postgres psql -c "SELECT usename, valuntil FROM pg_user WHERE usename LIKE 'v-%';"` — зафиксировать время окончания. Подождать/отозвать: `vault lease revoke -prefix database/creds/sklad-sync-ro`, повторить запрос. Ожидаемо: роль исчезла.
9. **UBU01.** PKI: root + intermediate + роль + выпуск сертификата для `web01.vektor.lab`. Сохранить в файлы и проверить `openssl x509 -in web01.crt -noout -text | grep -E "Issuer|Subject|Not After"`. Ожидаемо: Issuer = `Vektor Lab Intermediate CA`, срок — 24 часа.
10. **WEB01 (если есть).** Положить сертификат и ключ в `/etc/nginx/tls/`, прописать в `server { listen 443 ssl; server_name web01.vektor.lab; ... }`, `sudo nginx -t && sudo systemctl reload nginx`. С UBU01: `curl -k --resolve web01.vektor.lab:443:10.10.20.10 https://web01.vektor.lab/` → отдаётся страница nginx.
11. **UBU01.** Ротация: `vault operator rotate`, затем `vault write -f database/rotate-root/vektor-pg`, затем проверить, что сервисная учётка читает секреты по-прежнему (`vault read database/creds/sklad-sync-ro`).
12. **UBU01.** Записать в `~/stand-log.md`: что установлено, какие движки включены, где лежит `vault-init.json` (факт, не содержимое), какой TTL у токенов и аренд.

### Проверка

- [ ] `vault status` → `Initialized: true`, `Sealed: false`, `Storage Type: raft`
- [ ] `vault policy list` содержит `svc-sklad-sync`
- [ ] `vault read auth/approle/role/svc-sklad-sync` показывает `token_ttl=20m`
- [ ] `vault read database/creds/sklad-sync-ro` выдаёт `lease_duration` = 1h
- [ ] `vault list sys/leases/lookup/database/creds/sklad-sync-ro` показывает активные аренды
- [ ] `vault list pki_int/certs` содержит выпущенный сертификат
- [ ] `vault audit list` не пуст, в логе есть записи `request.path`
- [ ] `ls -l ~/vault-init.json` → права `600`
- [ ] в `pg_user` после `lease revoke` не осталось ролей `v-%`

### Сломай и почини

> **Пользователь:** sidorov
> **ВМ:** UBU01
> **Что написал:** «Сервис интеграции склада падает: `FATAL: password authentication failed for user "v-approle-sklad-sy-Ab12CdE"`. Вчера работало, ничего не меняли. Склад стоит, накладные не уходят.»
> **Срочность:** высокая

**Как воспроизвести поломку (намеренно):** на UBU01 отозвать все аренды роли, не трогая сервис: `vault lease revoke -prefix database/creds/sklad-sync-ro`. Сервис остался с паролем от уже удалённого пользователя.

**Диагностика по порядку:**

1. `vault status` — Vault жив и распечатан? Если `Sealed: true` (например, после `systemctl restart`), это совсем другая причина: поднимаем Vault, а не ищем пароли.
2. `sudo -u postgres psql -c "\du" | grep v-` — существует ли пользователь из ошибки. Не существует → аренда истекла или отозвана.
3. `vault list sys/leases/lookup/database/creds/sklad-sync-ro` — сколько живых аренд. Пусто → все отозваны/истекли.
4. `sudo grep 'database/creds/sklad-sync-ro' /var/log/vault/audit.log | tail -n 5` — кто и когда запрашивал и отзывал (в аудите видно `request.path`, `remote_address`, `display_name`).
5. Проверить `default_ttl` роли: `vault read database/roles/sklad-sync-ro`. Если `1h`, а сервис держит коннект дольше и не умеет переподключаться — корень проблемы в сервисе, а не в Vault.
6. `sudo journalctl -u sklad-sync --since "1 hour ago"` (или лог сервиса) — есть ли попытки логина после TTL.

**Решение:** перезапустить сервис, чтобы он получил новые креды (`sudo systemctl restart sklad-sync`), убедиться, что `vault read database/creds/sklad-sync-ro` выдаёт новую учётку, и зафиксировать в runbook'е: «сервис обязан перечитывать креды до истечения lease_duration как минимум за 10 минут». По-хорошему — поставить рядом Vault Agent с шаблоном, который сам обновляет файл с паролем и шлёт SIGHUP сервису.

**Ответ пользователю:** «Причина — истёк/отозван временный пользователь БД, сервис держал устаревший пароль. Сейчас работает, данные не потеряны. Дополнительно настроим автопродление, чтобы не повторилось; при следующем таком случае — перезапуск сервиса восстанавливает работу за минуту, пароль в базе искать не нужно».

**Закрытие заявки:** категория «Доступы/секреты», причина «expired lease + отсутствие renewer», в постмортем — задача «внедрить Vault Agent / renewer для всех сервисов, потребляющих динамические секреты».

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен `vault` (пакет), сервис `vault.service` включён, конфиг `/etc/vault.d/vault.hcl`, storage Raft в `/opt/vault/data`, API на `:8200`, UI включён.
- **UBU01:** установлен `postgresql`, создана роль `vault_admin`, включён движок `database/` с конфигом `vektor-pg` и ролью `sklad-sync-ro`.
- **UBU01:** включены `secret/` (KV v2), `pki/` (root `Vektor Lab Root CA`), `pki_int/` (intermediate), `auth/approle`, политика `svc-sklad-sync`, аудит-файл `/var/log/vault/audit.log`.
- **Файлы:** `~/vault-init.json` (права 600), `/etc/vault/policies/svc-sklad-sync.hcl`, сертификат `web01.vektor.lab` (TTL 24h).
- **WEB01 (10.10.20.10):** nginx слушает 443, сертификат из Vault PKI в `/etc/nginx/tls/` (если шаг 10 выполнялся).
- **Секреты стенда:** `secret/vektor/svc-sklad-sync` содержит `db_user` и `db_password`; в Git **не попадает**.

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-096` → **Go to**. Повторить для UBU01, WEB01, GIT01. Помни: откат вернёт и удалённый пакет Vault, и PostgreSQL, и все секреты — если что-то уже используется дальше по треку, лучше не откатывать, а чинить по месту.

## 6. Ошибки новичков и безопасность

1. **Unseal-ключи лежат рядом с сервером** (в `/root/keys.txt` на той же ВМ) → к чему: кто получил доступ к ВМ, получил и все секреты → как правильно: ключи вне сервера, лучше 5 долей у 5 разных людей/в разных местах, в проде — auto-unseal через KMS.
2. **Root-токен в CI-переменных** → к чему: любой, кто видит пайплайн, — администратор Vault → как правильно: короткоживущие токены через AppRole/JWT, root-токен только для первичной настройки, потом `revoke`.
3. **Dev-режим «на время, потом переделаем»** → к чему: данные в памяти, фиксированный root-токен, потеря всего при перезапуске → как правильно: raft/файловое хранилище и настоящий `init` с первого дня, даже на стенде.
4. **Политика `path "*" { capabilities = ["sudo"] }`** → к чему: сервис может отключить аудит и удалить секреты → как правильно: принцип минимальных прав, путь на конкретный секрет, только `read`.
5. **Секреты в переменных окружения контейнера** → к чему: видны в `docker inspect`, в `/proc/<pid>/environ`, в дампах и в логах падений → как правильно: файл в tmpfs, монтируемый Vault Agent'ом, или CSI-драйвер secrets-store.
6. **TTL «на год, чтобы не падало»** → к чему: ровно та же проблема, что с `.env`, только в другом месте → как правильно: короткий TTL + renewer; падать должно не «внезапно», а управляемо.
7. **Нет бэкапа Raft-снапшота** → к чему: потеря `/opt/vault/data` = потеря всех секретов и CA → как правильно: `vault operator raft snapshot save` по расписанию, снапшот в отдельное хранилище, регулярные учения по восстановлению (см. урок 92).
8. **TLS выключен «пока тестируем»** → к чему: токены и пароли ходят по сети открытым текстом, любой сниффер на VMnet8 их видит → как правильно: TLS на listener'е с сертификатом от того же PKI, `tls_disable = false`.

**Безопасность.** В этой лабе мы сознательно: используем `disable_mlock = true`, `tls_disable = true`, `SUPERUSER` для `vault_admin` и учебный пароль `Lab#2026-vektor`. В боевой среде каждое из этого — находка аудита: mlock защищает ключи от выгрузки в swap; без TLS секреты уходят в сеть в открытом виде; SUPERUSER у Vault означает, что компрометация Vault = компрометация всей СУБД. Правильно: `CREATEROLE` + владение схемой, TLS, mlock, отдельный сетевой сегмент для Vault, firewall только с известных адресов. И помни: как только пароль попал в Git — он скомпрометирован, менять надо сразу, а не «после релиза».

## 7. Вопросы на собеседовании

**1. Чем динамические секреты отличаются от статических и зачем они нужны?**

Ответ: статический секрет хранится в Vault (KV) и живёт, пока его не поменяют руками — у него нет срока и нет привязки к потребителю. Динамический Vault генерирует в момент запроса: создаёт пользователя в БД/сертификат/ключ и выдаёт под **lease** с TTL, по истечении которого сам всё отзывает. Плюсы: уникальность (по логину видно, кто получил), короткое окно компрометации, автоматическая ротация, невозможность «забыть пароль в конфиге навсегда». Минус: сервис обязан уметь обновлять креды, иначе он падает по расписанию.

*Что хочет услышать интервьюер:* понимание lease/TTL и того, что динамические секреты — это про **отзыв**, а не только про хранение; и что это меняет архитектуру приложения.

*Красный флаг:* «динамические — это те, что часто меняются» без упоминания generation и revocation.

**2. Что такое seal/unseal и почему unseal-ключи нельзя держать на самом сервере?**

Ответ: данные в storage всегда зашифрованы. Ключ шифрования (root key) зашифрован мастер-ключом, который собирается из unseal-ключей по схеме Шамира (например, 3 из 5). После старта Vault sealed — он не расшифрует ничего, пока не собраны доли. Если доли лежат на том же диске, то кража диска/ВМ = кража всех секретов, и весь смысл барьера исчезает. В проде используют auto-unseal: мастер-ключ завёрнут в KMS/HSM, сервер сам разжимается при старте, а доли остаются только как break-glass.

*Что хочет услышать интервьюер:* барьер, Шамир, auto-unseal, break-glass-процедура.

*Красный флаг:* «unseal-ключ — это пароль администратора» или незнание, что после рестарта Vault снова sealed.

**3. Как Vault выдаёт TLS-сертификаты и почему TTL короткий?**

Ответ: включается `pki` secrets engine. Корневой CA лучше держать офлайн, в Vault — промежуточный, подписанный корнем. Дальше создаётся роль с `allowed_domains` и `max_ttl`, и по `pki_int/issue/<role>` с `common_name` Vault генерирует ключевую пару и подписывает сертификат на заданный TTL (часы/сутки). Короткий TTL = автоматическая ротация и малое окно для украденного ключа; цена — нужна автоматика (cert-manager, Vault Agent, hook в nginx) и нормально работающий CRL/OCSP-эндпоинт.

*Что хочет услышать интервьюер:* разделение root/intermediate, `allowed_domains` как защита от выписки на чужой домен, автоматизация перевыпуска.

*Красный флаг:* «держим корневой CA в Vault и выдаём сертификаты на 10 лет».

**4. Что будет, если Vault недоступен или sealed? Как строить систему, чтобы это не положило прод?**

Ответ: приложение не должно ходить в Vault на каждый запрос. Паттерны: кэширование секрета в памяти/файле на время TTL, Vault Agent с авто-аутентификацией и шаблонами, HA-кластер Vault (3–5 нод, Raft), auto-unseal, чтобы рестарт не требовал человека, и заранее описанный break-glass-доступ на случай полного отказа. Плюс сервис должен уметь пережить недоступность Vault: не падать сразу, а ретраить с backoff и работать на закэшированных кредах, пока они живы.

*Что хочет услышать интервьюер:* что кандидат думает про Vault как про SPOF и знает про кэш, HA и auto-unseal.

*Красный флаг:* «Vault всегда доступен, у нас же HA».

**5. Как безопасно доставить секрет в Kubernetes-под и в CI-пайплайн?**

Ответ: в Kubernetes — `kubernetes` auth method: под предъявляет свой ServiceAccount JWT, Vault проверяет его через TokenReview и выдаёт токен с политикой, привязанной к `bound_service_account_names` и неймспейсу. Дальше либо Vault Agent Injector/CSI-драйвер монтирует секрет в файл, либо External Secrets Operator синхронизирует его в обычный Secret (компромисс: Secret лежит в etcd). В CI — короткоживущий JWT джобы обменивается на токен Vault (JWT/OIDC auth) с узкой политикой; root-токены и долгоживущие AppRole `secret_id` в переменных пайплайна не хранят. Дополнительно — response wrapping, чтобы `secret_id` доставлялся одноразово.

*Что хочет услышать интервьюер:* идентичность рабочей нагрузки вместо статического пароля, ограничение по неймспейсу/SA, отсутствие долгоживущих кредов в CI.

*Красный флаг:* «кладём root-токен в masked variable, GitLab же маскирует».

## 8. Источники

- **developer.hashicorp.com** — раздел **Vault Documentation**: «Concepts» (seal/unseal, leases, tokens), «Secrets Engines → KV / Database / PKI», «Auth Methods → AppRole / Kubernetes», «Configuration». Поиск: `vault database secrets engine postgresql`, `vault pki intermediate ca`, `vault approle role-id secret-id`.
- **developer.hashicorp.com** — Vault **API Reference** (эндпоинты `database/creds/:name`, `pki_int/issue/:role`, `sys/leases`). Поиск: `vault api docs leases revoke prefix`.
- **openbao.org** — OpenBao documentation (open-source форк Vault, совместимый API). Поиск: `openbao getting started raft storage`.
- **postgresql.org** — PostgreSQL Documentation, разделы «CREATE ROLE», «Role Attributes», «Privileges». Поиск: `postgresql create role valid until`.
- **kubernetes.io** — раздел «Managing Secrets» и «Service Accounts»; **external-secrets.io** — External Secrets Operator docs. Поиск: `kubernetes service account token volume projection`.
- **cheatsheetseries.owasp.org** — OWASP **Secrets Management Cheat Sheet**. Поиск: `owasp secrets management cheat sheet`.
- **docs.gitlab.com** — GitLab CI/CD: «ID tokens» / «Authenticate with HashiCorp Vault» `[ПРОВЕРИТЬ актуальное название статьи и имя переменной токена]`.
- **csrc.nist.gov** — NIST SP 800-57 Part 1 Rev.5, «Recommendation for Key Management» `[ПРОВЕРИТЬ номер ревизии]`. Поиск: `NIST SP 800-57 key management cryptoperiods`.
