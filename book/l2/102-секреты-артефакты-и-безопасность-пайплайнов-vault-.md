# Секреты, артефакты и безопасность пайплайнов: Vault, Nexus/Harbor, подпись образов

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Открываю ярлык „Отчёты“ — ошибка 500. Вчера в 18:40 всё работало, никто ничего не трогал. Закрытие месяца, помогите.»
> **Срочность:** высокая

L2 идёт на UBU01, смотрит логи сервиса, который формирует отчёты, и видит: `permission denied` при обращении к хранилищу секретов. Дальше — вопрос на миллион: почему сервис вообще ходит за паролем к базе в отдельное хранилище, а не носит пароль в конфиге? Потому что кто-то до тебя сделал правильно. А ещё потому, что пароль в конфиге рано или поздно окажется в Git, в образе контейнера, в бэкапе или в чужом скриншоте.

На L1 ты выдавал доступы и перезагружал сервисы. На L2 ты отвечаешь за то, чтобы секреты жили по правилам: хранились в одном месте, выдавались по TTL, отзывались одной командой и логировались. А ещё — чтобы на прод выкатывался только тот артефакт, который ты сам собрал и подписал, а не «вот этот образ с ноутбука разработчика, там точно всё ок».

Эта глава — мост к DevOps. Здесь ты впервые соединяешь две вещи: **управление секретами** (HashiCorp Vault и его логика) и **управление артефактами** (Nexus, Harbor, локальный registry) с **подписью образов** (cosign/Sigstore). После главы ты будешь уметь: поднять Vault в лаборатории, выдать приложению короткоживущий токен по AppRole, объяснить, почему Kubernetes Secret — это не «секрет» в юридическом смысле, подписать образ и проверить подпись, найти утёкший пароль в слоях образа и настроить проверку секретов в репозитории.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

### 2.1. Жизненный цикл секрета

Секрет — это не «пароль в файле», а объект с жизненным циклом:

```text
создание -> выдача (кому, на сколько) -> ротация -> отзыв -> аудит
```

Плохой вариант: пароль лежит в `docker-compose.yml` и в Git. Хороший: секрет лежит в хранилище, приложение получает его в момент старта по короткоживущему токену, через час токен умирает, а каждое чтение видно в логе аудита.

### 2.2. Как устроен Vault

Vault — это сервер (HTTP API, порт **8200**, TLS в бою), который состоит из слоёв:

```text
клиент (vault CLI / curl / SDK)
        |
        v  [Auth methods]  token, approle, ldap, oidc, kubernetes, cert
        |
   [Policy engine]  кто что может (HCL-политики)
        |
   [Secret engines]  kv, pki, database, transit, ssh, aws
        |
   [Barrier: шифрование + аудит всех операций]
        |
   [Storage backend]  file, raft, consul, (в лабе - in-memory dev)
```

Три ключевых понятия, без которых Vault не понять:

- **Seal/Unseal.** При старте хранилище «запечатано» — данные зашифрованы. Разпечатывают ключами Шамира (по умолчанию 5 долей, нужно 3) или облачным KMS. Dev-режим стартует сразу распечатанным — и это первое, что нельзя тащить в прод.
- **Lease (TTL).** У токена и у выданного секрета есть срок жизни. Истёк — доступ пропал. Это и есть главная защита: утёкший токен живёт не вечно.
- **Audit device.** Vault умеет писать каждую операцию в файл или syslog — с путём и типом запроса, но без самих значений.

Лицензии: с версии 1.15 HashiCorp перевёл Vault на BUSL-1.1, для коммерческого использования условия ограничены `[ПРОВЕРИТЬ актуальную версию и условия]`. Открытый форк — **OpenBao** (Linux Foundation). Для лаборатории и обучения ограничения не мешают, для боевого внедрения в РФ лицензию смотрят юристы, а не сисадмин.

### 2.3. Хранилище артефактов: зачем Nexus и Harbor

Git — не место для бинарников: он не умеет дедупликацию слоёв, не даёт immutability, раздувается от каждого билда. Поэтому артефакты (образы, jar, npm-пакеты, deb/rpm, дистрибутивы 1С-обновлений, ISO) живут в **репозитории артефактов**:

- **Sonatype Nexus Repository** — универсальный: docker, maven, npm, pypi, raw, yum/apt-прокси. Community Edition бесплатна с ограничениями по форматам `[ПРОВЕРИТЬ актуальный набор]`. Веб-интерфейс и API на **8081**.
- **Harbor** (CNCF, Apache-2.0) — только Docker/OCI, но с проектами, RBAC, репликацией между инстансами, встроенным сканированием Trivy, неизменяемыми тегами и проверкой подписей. Именно его берут, когда нужен «свой Docker Hub» с политикой.

### 2.4. Подпись образов

Ключевая мысль: **тег (`:v1`, `:latest`) — это не идентичность**. Тег можно перезаписать. Идентичность — это **digest** (`sha256:...`), хеш содержимого образа.

Подпись образа (cosign из проекта Sigstore) работает так: cosign считает digest образа, подписывает его приватным ключом и **кладёт подпись обратно в тот же registry** отдельным OCI-артефактом с тегом вида `sha256-<digest>.sig`. Отдельный сервер подписи не нужен.

```text
Образ:  10.10.10.30:5000/vektor/web:v1
        digest sha256:abc...   <- подписывается именно он
Подпись: 10.10.10.30:5000/vektor/web:sha256-abc....sig
Проверка: cosign verify --key cosign.pub ... -> exit code 0 или не 0
```

Два режима:
- **Ключевая пара** (проще, работает офлайн) — `cosign.key` / `cosign.pub`. Приватный ключ хранится в Vault/KMS/HSM, не в Git.
- **Keyless** — CI предъявляет OIDC-токен (GitLab/GitHub), Fulcio выдаёт короткоживущий сертификат, факт подписи пишется в публичный журнал прозрачности Rekor. Удобно, но требует доверия к внешним сервисам — в закрытом контуре обычно не подходит.

Подпись ≠ сканирование. Скан (Trivy) отвечает «нет ли известных CVE в слоях». Подпись отвечает «этот образ собрал именно наш пайплайн». Часто нужно и то, и другое.

### 2.5. Где это стоит в общей картине

```text
[разработчик] --git push--> [Git/GitLab]
                                  | webhook
                                  v
                            [CI-раннер]
              1. берёт секреты в Vault (AppRole/OIDC, TTL 15 мин)
              2. собирает образ
              3. сканирует: trivy image
              4. подписывает: cosign sign (ключ из Vault/KMS)
              5. кладёт SBOM рядом с образом
                                  |
                                  v
                    [Harbor / Nexus / registry]
              хранит: образ + подпись + скан + SBOM + неизменяемые теги
                                  |
                                  v
                  [Kubernetes / docker host]
              admission-контроль: подпись валидна? -> запуск
              секреты в рантайме: Vault Agent / CSI -> файл в под
```

Порты для запоминания: Vault — 8200, registry (docker-distribution) — 5000, Nexus — 8081, Harbor — 80/443. Всё по HTTPS в бою, кроме случая, о котором ниже пойдёт речь отдельно.

## 3. Аналогия из жизни

Представь не сейф, а **ключницу вахтёра на проходной**. У каждого сотрудника есть пропуск (токен) с ограниченным сроком действия: сегодня он открывает склад, но не бухгалтерию (политика). Ключ от склада вахтёр выдаёт на час и записывает в журнал: кто взял, во сколько, когда вернул (lease и аудит). Если пропуск потеряли — вахтёр его аннулирует, и он перестаёт работать у любого, кто его подобрал (revoke). А ещё вахтёр не выдаёт ключ «просто так по имени»: надо предъявить удостоверение сотрудника (auth method).

Артефакты — это склад готовой продукции рядом. Товар принимают только с пломбой и накладной (подпись и digest), на полке лежит одна конкретная партия с номером, а не «коробка с надписью v1, в которую что угодно можно положить». Пломбу проверяют на выходе со склада (admission-контроль), и если пломба битая — товар не уедет в магазин.

Аналогия ломается в одном месте: вахтёр — человек, а Vault не умеет «по-человечески» решить, что вы ошиблись. Если политика настроена неверно, он просто аккуратно и молча откажет — поэтому политики нужно тестировать, а не писать «на глаз».

## 4. Практика и команды

Ниже — команды для UBU01 (Linux) и WIN11-CL01 (PowerShell). Все действия — на стенде с учебными паролями; в бою Vault только за TLS и никогда в dev-режиме.

**1. Проверка состояния Vault.** Переменные `VAULT_ADDR` и `VAULT_TOKEN` — основной способ указать CLI, куда и с кем идти.

```bash
export VAULT_ADDR=http://10.10.10.30:8200
export VAULT_TOKEN=lab-root-token
vault status
```

```text
Key             Value
---             -----
Seal Type       shamir
Sealed          false
Version         1.x.y
Storage Type    inmem
```
Смотрим `Sealed: false` (иначе нужен unseal) и `Storage Type: inmem` — значит, перезапуск контейнера стирает всё. В бою `raft` или `file`.

**2. Запись и чтение секрета (kv-v2).** В dev-режиме движок `kv-v2` уже смонтирован по пути `secret/`, поэтому в API путь получает приставку `data`.

```bash
vault kv put secret/vektor/db username=app password='Lab#2026-vektor'
vault kv get secret/vektor/db
vault kv get -field=password secret/vektor/db
vault kv metadata get secret/vektor/db
```
`-field` вытаскивает одно поле (удобно в скриптах). `metadata get` показывает версии и `deletion_time` — это ключ к восстановлению после случайного `vault kv delete`.

**3. Политика: минимальные права.** HCL-файл `app-read.hcl`:

```hcl
path "secret/data/vektor/*" {
  capabilities = ["read"]
}
path "secret/metadata/vektor/*" {
  capabilities = ["read", "list"]
}
```

```bash
vault policy write app-read app-read.hcl
vault token create -policy=app-read -ttl=1h -display-name=report-api
vault token lookup <TOKEN>
```
В `token lookup` смотрим `ttl`, `policies`, `display_name`. `ttl 59m59s` — токен живой, `policies [app-read default]` — права на месте.

**4. AppRole: вход для сервиса, без человека.** Это штатный способ для приложений: `role_id` — «логин», `secret_id` — «пароль», оба выдаются автоматизации.

```bash
vault auth enable approle
vault write auth/approle/role/report-api token_policies=app-read token_ttl=1h token_max_ttl=4h
vault read -field=role_id auth/approle/role/report-api/role-id
vault write -f -field=secret_id auth/approle/role/report-api/secret-id
vault write -field=token auth/approle/login role_id=<ID> secret_id=<SID>
```
Ответ логина — `token` и `lease_duration`. `token_max_ttl` не даёт сервису продлевать токен бесконечно.

**5. Аудит.** Включается один раз и навсегда.

```bash
vault audit enable file file_path=/vault/logs/audit.log
docker exec vault tail -n 3 /vault/logs/audit.log
```
В логе — `request.path`, `auth.display_name`, `error`. Значений секретов там нет — и это правильно.

**6. Чтение секрета из Windows (PowerShell + REST API).** Vault — обычный HTTP API.

```powershell
$h = @{ "X-Vault-Token" = "lab-root-token" }
$r = Invoke-RestMethod -Uri "http://10.10.10.30:8200/v1/secret/data/vektor/db" -Headers $h
$r.data.data.password
```
Обрати внимание на двойную вложенность `data.data` — это специфика kv-v2. Ошибка `403` = токен/политика, `404` = неверный путь.

**7. Локальный registry и «небезопасный» HTTP.** Docker по умолчанию требует HTTPS, поэтому для лабораторного registry в `/etc/docker/daemon.json`:

```json
{ "insecure-registries": ["10.10.10.30:5000"] }
```

```bash
sudo systemctl restart docker
docker run -d --name registry --restart unless-stopped -p 5000:5000 registry:2
curl -s http://10.10.10.30:5000/v2/_catalog
```
```json
{"repositories":["vektor/web"]}
```
`_catalog` — список репозиториев. **Важно:** `insecure-registries` отключает проверку TLS, в бою так нельзя.

**8. Публикация артефакта.**

```bash
docker tag nginx:alpine 10.10.10.30:5000/vektor/web:v1
docker push 10.10.10.30:5000/vektor/web:v1
docker inspect --format '{{index .RepoDigests 0}}' 10.10.10.30:5000/vektor/web:v1
```
Последняя команда даёт digest — то, что реально подписывается и что стоит пинить в манифестах деплоя.

**9. Подпись и проверка (cosign).**

```bash
cosign generate-key-pair
COSIGN_PASSWORD='Lab#2026-vektor' cosign sign --key cosign.key \
  --allow-insecure-registry --tlog-upload=false 10.10.10.30:5000/vektor/web:v1
cosign verify --key cosign.pub --allow-insecure-registry --insecure-ignore-tlog \
  10.10.10.30:5000/vektor/web:v1
```
`--allow-insecure-registry` — для HTTP-registry, `--tlog-upload=false` / `--insecure-ignore-tlog` — чтобы не ходить в публичный Rekor из закрытого контура `[ПРОВЕРИТЬ точные имена флагов для твоей версии cosign: cosign sign --help]`. Успешная проверка возвращает JSON с `critical.identity` и код возврата 0.

**10. Поиск утёкших секретов в образе.**

```bash
docker history --no-trunc 10.10.10.30:5000/vektor/web:v1
docker save 10.10.10.30:5000/vektor/web:v1 -o /tmp/img.tar
mkdir -p /tmp/img && tar -xf /tmp/img.tar -C /tmp/img
grep -ril 'password\|secret\|token' /tmp/img --include='*.json' | head
```
`--no-trunc` показывает полные команды сборки — именно там видно `ENV DB_PASSWORD=...`. `docker save` даёт полный слепок со всеми слоями.

**11. Сканирование репозитория на секреты (gitleaks).**

```bash
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest \
  detect --source=/repo --report-format=json --report-path=/repo/gitleaks.json -v
```
`-v` — подробный вывод, `--report-path` — файл для CI-артефакта. Код возврата ≠ 0 означает находки `[ПРОВЕРИТЬ синтаксис для актуальной версии gitleaks]`.

**12. Сканирование образа на CVE (Trivy).**

```bash
trivy image --insecure --severity HIGH,CRITICAL 10.10.10.30:5000/vektor/web:v1
```
`--insecure` — для HTTP-registry, `--severity` — фильтр. В Harbor то же самое встроено и запускается автоматически при пуше.

**13. Nexus: список репозиториев через API (если поднял Nexus).**

```bash
curl -s -u admin:'Lab#2026-vektor' http://10.10.10.30:8081/service/rest/v1/repositories | head -40
```
Возвращает JSON с `name`, `format`, `type` (`hosted` / `proxy` / `group`). `proxy` — кэш внешнего репозитория, `group` — единая точка входа для клиента.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L2 готов: DC01, FS01, WIN11-CL01, WIN10-CL02, UBU01 (10.10.10.30), ASTRA01.
- На UBU01 установлен Docker (ставили в уроке 96). Если нет: `sudo apt update && sudo apt install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER` (перелогиниться).
- Машины GIT01, HARBOR01 и кластер k8s по плану стенда появляются только на треке DevOps — **здесь мы их не создаём**. Роль Harbor играет локальный registry на UBU01, роль Vault — dev-контейнер. В DevOps-треке ты перенесёшь это на HARBOR01 и нормальный Vault, логика команд не изменится.
- Docker Hub из РФ может быть недоступен; если `docker pull` падает — используй любой образ, уже лежащий локально (`docker images`), или зеркало в облаке.

### Снапшот

В VMware Workstation: правый клик по вкладке ВМ → **Snapshot → Take Snapshot**, имя `before-l2-102`. Сделать на **UBU01** (единственная изменяемая машина). Если планируешь проверку с WIN11-CL01 — снапшот и на ней.

### Шаги

1. **UBU01, SSH.** `ssh student@10.10.10.30`. Создай рабочий каталог: `mkdir -p ~/lab102 && cd ~/lab102`. Ожидаемо: пустой каталог, `docker ps` работает без sudo.
2. **UBU01, поднять Vault в dev-режиме.**
   ```bash
   docker run -d --name vault --cap-add=IPC_LOCK \
     -e VAULT_DEV_ROOT_TOKEN_ID=lab-root-token \
     -p 8200:8200 hashicorp/vault server -dev -dev-listen-address=0.0.0.0:8200
   docker logs --tail 20 vault
   ```
   Ожидаемо: в логах `Root Token: lab-root-token` и `Vault server started`. `--cap-add=IPC_LOCK` нужен, чтобы Vault мог блокировать память (`mlock`).
3. **Проверить и записать секрет.**
   ```bash
   export VAULT_ADDR=http://10.10.10.30:8200
   export VAULT_TOKEN=lab-root-token
   vault status
   vault kv put secret/vektor/db username=app password='Lab#2026-vektor'
   vault kv get secret/vektor/db
   ```
   Ожидаемо: `Sealed false`, версия секрета `version 1`.
4. **Политика и токен с TTL.** Создать `app-read.hcl` (см. раздел 4, п.3), выполнить `vault policy write app-read app-read.hcl`, затем `vault token create -policy=app-read -ttl=1h -display-name=report-api`. Сохрани выданный токен в переменную `APP_TOKEN`. Ожидаемо: `ttl 1h`, `policies [app-read default]`.
5. **Проверить, что прав не больше, чем нужно.**
   ```bash
   VAULT_TOKEN=$APP_TOKEN vault kv get secret/vektor/db     # работает
   VAULT_TOKEN=$APP_TOKEN vault kv put secret/vektor/db username=hack   # 403
   ```
   Ожидаемо: чтение успешно, запись — `permission denied`.
6. **AppRole для сервиса.**
   ```bash
   vault auth enable approle
   vault write auth/approle/role/report-api token_policies=app-read token_ttl=1h token_max_ttl=4h
   vault read -field=role_id auth/approle/role/report-api/role-id > ~/lab102/role_id
   vault write -f -field=secret_id auth/approle/role/report-api/secret-id > ~/lab102/secret_id
   ```
   Ожидаемо: два файла с идентификаторами. Здесь же честно скажи себе: файл `secret_id` на диске — это костыль; правильное решение — Vault Agent, k8s-auth или workload identity.
7. **Скрипт-«сервис», который берёт секрет.** Создай `~/lab102/report-secret-check.sh`:
   ```bash
   #!/bin/bash
   VAULT=http://10.10.10.30:8200
   TOKEN=$(curl -s --request POST \
     --data "{\"role_id\":\"$(cat ~/lab102/role_id)\",\"secret_id\":\"$(cat ~/lab102/secret_id)\"}" \
     $VAULT/v1/auth/approle/login | sed -n 's/.*"client_token":"\([^"]*\)".*/\1/p')
   [ -z "$TOKEN" ] && { echo "ERR: login failed"; exit 1; }
   PW=$(curl -s -H "X-Vault-Token: $TOKEN" $VAULT/v1/secret/data/vektor/db \
     | sed -n 's/.*"password":"\([^"]*\)".*/\1/p')
   [ -z "$PW" ] && { echo "ERR: secret empty"; exit 1; }
   echo "OK: secret received, length=${#PW}"
   ```
   `chmod +x`, затем `./report-secret-check.sh`. Ожидаемо: `OK: secret received, length=14`.
8. **Локальный registry.**
   ```bash
   sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
   { "insecure-registries": ["10.10.10.30:5000"] }
   EOF
   sudo systemctl restart docker
   docker start vault
   docker run -d --name registry --restart unless-stopped -p 5000:5000 registry:2
   curl -s http://10.10.10.30:5000/v2/_catalog
   ```
   Ожидаемо: `{"repositories":[]}`. Рестарт Docker остановил Vault — поэтому `docker start vault` (в бою ставят `--restart unless-stopped` всем критичным контейнерам).
9. **Опубликовать образ.**
   ```bash
   docker tag nginx:alpine 10.10.10.30:5000/vektor/web:v1
   docker push 10.10.10.30:5000/vektor/web:v1
   curl -s http://10.10.10.30:5000/v2/_catalog
   ```
   Ожидаемо: в каталоге появился `vektor/web`.
10. **Подписать и проверить.**
    ```bash
    cd ~/lab102 && cosign generate-key-pair
    COSIGN_PASSWORD='Lab#2026-vektor' cosign sign --key cosign.key \
      --allow-insecure-registry --tlog-upload=false 10.10.10.30:5000/vektor/web:v1
    cosign verify --key cosign.pub --allow-insecure-registry --insecure-ignore-tlog \
      10.10.10.30:5000/vektor/web:v1
    ```
    Ожидаемо: `cosign verify` печатает JSON с `"critical"` и завершается кодом 0 (`echo $?`).
11. **Scan и поиск утечек.**
    ```bash
    docker history --no-trunc 10.10.10.30:5000/vektor/web:v1 | head
    trivy image --insecure --severity HIGH,CRITICAL 10.10.10.30:5000/vektor/web:v1
    docker run --rm -v "$HOME/lab102:/repo" zricethezav/gitleaks:latest detect --source=/repo -v
    ```
    Ожидаемо: в `history` видны команды сборки (паролей нет — хорошо), Trivy выдаёт таблицу CVE по базовым пакетам, gitleaks молчит по скрипту, но найдёт `role_id`/`secret_id`, если они лежат в отслеживаемом каталоге — это правильный повод задуматься.
12. **Проверка с Windows (WIN11-CL01).** В PowerShell от `adm.student`:
    ```powershell
    $h = @{ "X-Vault-Token" = "<APP_TOKEN из шага 4>" }
    (Invoke-RestMethod -Uri "http://10.10.10.30:8200/v1/secret/data/vektor/db" -Headers $h).data.data.password
    ```
    Ожидаемо: `Lab#2026-vektor`. Это тот же секрет, что видит Linux-сервис — единый источник правды.
13. **Записать в `stand-log.md`** всё, что появилось (см. раздел «Что изменилось на стенде»).

### Проверка

- [ ] `vault status` на UBU01 отвечает, `Sealed: false`.
- [ ] `vault kv get secret/vektor/db` возвращает `version 1` (или выше).
- [ ] Токен `app-read` читает секрет и получает 403 на запись.
- [ ] `report-secret-check.sh` печатает `OK: secret received`.
- [ ] `curl http://10.10.10.30:5000/v2/_catalog` содержит `vektor/web`.
- [ ] `cosign verify` с `cosign.pub` завершается кодом 0.
- [ ] `cosign verify` без ключа (или с чужим `cosign.pub`) завершается ошибкой.
- [ ] Trivy выдаёт отчёт, gitleaks — отчёт в JSON.
- [ ] `stand-log.md` дополнен.

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Отчёты не формируются с 9 утра, ярлык отдаёт 500. Вчера вечером всё было нормально. Закрытие месяца, очень срочно.»
> **Срочность:** высокая

**Воспроизводим поломку** (на UBU01, от `adm.student`, «как будто коллега чистил хранилище»):

```bash
vault kv delete secret/vektor/db
~/lab102/report-secret-check.sh    # -> ERR: secret empty
```

**Диагностика по порядку (не перескакивать):**

1. **Что изменилось?** `stand-log.md` и `vault kv metadata get secret/vektor/db` — видим `deletion_time` у последней версии. Половина инцидентов закрывается на этом шаге.
2. **Где болит?** Проверяем сам сервис, а не догадки: `systemctl status`, логи приложения, `docker logs`. В логе — `secret empty` / `404`. Это значит: сеть до Vault есть, а секрета нет — сузили круг.
3. **Проверяем доступ отдельно от сервиса.** Логин по AppRole вручную, затем `curl -H "X-Vault-Token: $TOKEN" $VAULT/v1/secret/data/vektor/db`. Ответ `404` → проблема в данных, не в правах. Если был бы `403` — смотрели бы `vault token lookup` и политику.
4. **Проверяем версии:** `vault kv metadata get secret/vektor/db` — видим `version 1: deletion_time 2026-...`. Это soft delete: данные физически на месте.
5. **Чиним:** `vault kv undelete -versions=1 secret/vektor/db`, затем `vault kv get secret/vektor/db`.
6. **Проверяем сквозняком:** `./report-secret-check.sh` → `OK`, затем просим ivanov открыть ярлык и подтвердить.
7. **Постмортем:** кто и зачем удалил; почему нет алерта на 404 от Vault; почему у сервиса нет кэша последнего значения и fallback; включаем аудит (`vault audit enable file ...`) и проверяем, виден ли инцидент в логе.

**Ответ пользователю:**

> Иван, добрый день. Причина — пропал секрет, который сервис отчётов берёт из хранилища (сам сервис и сеть в порядке). Секрет восстановлен, отчёты формируются. Пожалуйста, откройте ярлык и подтвердите. По итогам добавим алерт, чтобы такое ловилось до обращений.

**Закрытие заявки:** категория «Сервис отчётов / конфигурация», причина «случайное удаление секрета в Vault», решение «undelete версии + алерт». Не забудь, что `vault kv delete` в kv-v2 — мягкое удаление, а `vault kv destroy` — окончательное, и вот его откатить нельзя.

### Что изменилось на стенде

- **UBU01 (10.10.10.30):**
  - контейнер `vault` (dev-режим, in-memory, порт 8200, root-токен `lab-root-token` — учебный);
  - контейнер `registry` (`registry:2`, порт 5000, каталог `vektor/web`);
  - `/etc/docker/daemon.json` с `insecure-registries: ["10.10.10.30:5000"]`;
  - каталог `~/lab102`: `cosign.key`, `cosign.pub`, `role_id`, `secret_id`, `report-secret-check.sh`;
  - в Vault: auth-метод `approle`, роль `report-api`, политика `app-read`, секрет `secret/vektor/db`.
- **WIN11-CL01:** изменений нет (только проверка через PowerShell).
- **Важно для следующих глав:** dev-Vault теряет данные при перезапуске контейнера; при переходе на DevOps-трек всё это переносится на HARBOR01 и полноценный Vault с файловым/raft-хранилищем.

### Откат

VMware Workstation → правый клик по UBU01 → **Snapshot Manager** → выбрать `before-l2-102` → **Restore**. Машина вернётся к состоянию до контейнеров Vault/registry и до правок `daemon.json`. Если откат не нужен, оставь стенд как есть — но обязательно допиши изменения в `stand-log.md`, иначе следующая глава начнётся с сюрприза.

## 6. Ошибки новичков и безопасность

1. **Секрет в `ENV`/`ARG` Dockerfile или в `docker-compose.yml`** → попадает в слой образа и в Git, живёт там вечно (образы immutable). Правильно: секрет приходит в рантайме из Vault/переменных окружения CI, а скомпрометированный — немедленно ротируется.
2. **Vault в dev-режиме «на минутку» на боевом сервере** → данные в памяти, root-токен всем известен, TLS нет. Правильно: dev только локально; в бою — TLS, отдельный storage backend, unseal через KMS/Шамир, root-токен сразу отозван после первичной настройки.
3. **Один root-токен на всё** → нет аудита «кто именно», компромисс = компромисс всего хранилища. Правильно: отдельные политики под каждую задачу, TTL, `token_max_ttl`, AppRole/OIDC для сервисов.
4. **`insecure-registries` и `--allow-insecure-registry` в проде** → трафик и подписи можно перехватить и подменить. Правильно: только HTTPS и валидные сертификаты; HTTP допустим лишь в изолированной лабе.
5. **`latest` без digest** → непонятно, что именно запущено; тег перезаписывается. Правильно: пинить по digest, включить tag immutability в Harbor, подписывать digest.
6. **Подпись есть, проверки нет** → подпись создаёт ощущение безопасности, но ничего не блокирует. Правильно: enforcement на входе в кластер (admission-контроль: Kyverno/Gatekeeper/Connaisseur или политики Harbor) `[ПРОВЕРИТЬ актуальные инструменты]`.
7. **Приватный ключ cosign в Git или в CI-переменных навсегда** → утечка = возможность подписать что угодно. Правильно: ключ в Vault/KMS/HSM, короткоживущие ключи или keyless с OIDC.
8. **«Kubernetes Secret — значит секретный»** → по умолчанию это base64 в etcd без шифрования диска. Правильно: encryption at rest в etcd, RBAC на `get secrets`, External Secrets Operator или Vault Agent/CSI, а лучше — вообще не хранить долгоживущие секреты в кластере.

### Безопасность (отдельно)

В боевой среде нельзя: выводить секреты в `stdout` контейнера (`docker logs` читают многие), отключать аудит Vault «чтобы не рос лог», давать разработчикам root-токен, хранить `secret_id` рядом с приложением на диске, пушить в registry без аутентификации. Учебный пароль `Lab#2026-vektor` — только для стенда: в реальной работе он был бы нарушением парольной политики, а dev-режим Vault на сетевом интерфейсе — incident-репортом в первый же день.

## 7. Вопросы на собеседовании

**1. Зачем нужен Vault, если есть переменные окружения и CI-секреты?**

Ответ: переменные CI решают только этап сборки и не дают TTL, отзыва, аудита и ротации; они копируются в каждый пайплайн и легко утекают в логи. Vault даёт единое хранилище, политики доступа, короткоживущие токены и lease, аудит каждого чтения, динамические секреты (например, временные учётки БД) и движки вроде `pki`/`transit`. Для L2-уровня достаточно уметь объяснить жизненный цикл секрета и показать AppRole.

*Что хочет услышать интервьюер:* понимание TTL/ротации/аудита, а не «ну это сейф для паролей».
*Красный флаг:* «Vault — это просто шифрованный файл с паролями» или «мы храним всё в `.env`, норм».

**2. Чем подпись образа отличается от сканирования уязвимостей?**

Ответ: скан (Trivy и аналоги) отвечает на вопрос «есть ли в образе известные CVE», но ничего не говорит о происхождении. Подпись (cosign/Sigstore) подтверждает, что конкретный digest собрал именно наш пайплайн и с тех пор его не подменяли. Нужны оба: подписанный образ может быть уязвимым, а «чистый» по CVE — собранным злоумышленником.

*Что хочет услышать интервьюер:* разделение «integrity/provenance» и «vulnerability».
*Красный флаг:* «подпись проверяет, что в образе нет вирусов».

**3. Kubernetes Secret — это безопасное хранилище?**

Ответ: нет. По умолчанию это base64 в etcd без шифрования; защита строится на RBAC, шифровании etcd at rest, ограничении доступа к нодам и на внешнем хранилище (Vault + CSI/External Secrets, Sealed Secrets, SOPS). Плюс важно не путать маскировку с шифрованием.

*Что хочет услышать интервьюер:* «base64 — это кодирование, не шифрование» и знание вариантов внешнего хранилища.
*Красный флаг:* «Secret в k8s зашифрован, всё ок».

**4. Как построить пайплайн без долгоживущих статических ключей?**

Ответ: CI получает короткоживущую идентичность — OIDC-токен от GitLab/GitHub, по нему вход в Vault (JWT/OIDC auth) или в облако (federation); либо AppRole с коротким TTL и `secret_id` с ограничением по IP/использованиям. Дальше — только временные креды: динамические учётки БД, STS-токены для S3, сертификаты на минуты. Статические ключи, если совсем без них нельзя, живут в Vault и ротируются.

*Что хочет услышать интервьюер:* понимание federation/OIDC и принцип «креды живут минуты».
*Красный флаг:* «положим AWS-ключ в переменные GitLab и замаскируем».

**5. Зачем Nexus или Harbor, если есть Git и Docker Hub?**

Ответ: Git не подходит для бинарников (нет дедупликации слоёв, нет proxy-кэша, репозиторий раздувается), а внешний Docker Hub — это зависимость от чужого сервиса и чужих лимитов (плюс возможные ограничения доступа из РФ). Свой репозиторий даёт контроль: единая точка для всех форматов, кэш внешних репозиториев, ретеншн, RBAC, неизменяемые теги, встроенное сканирование и хранение подписей рядом с образом.

*Что хочет услышать интервьюер:* «единая точка поставки артефактов + политика + офлайн-независимость».
*Красный флаг:* «чтобы был свой Docker Hub, потому что так модно».

## 8. Источники (официальная документация)

- **HashiCorp Developer** — `developer.hashicorp.com`. Разделы: «Vault / Concepts» (seal/unseal, leases, policies), «Vault / Auth Methods / AppRole», «Vault / Secrets Engines / KV», «Vault / Audit Devices». Поиск: `hashicorp vault approle tutorial`, `vault kv-v2 versioned secrets`.
- **Sigstore / cosign** — `docs.sigstore.dev`. Разделы: «Signing containers», «Verifying signatures», «Keyless». Поиск: `cosign sign verify key pair documentation`.
- **Harbor** — `goharbor.io` (документация: `goharbor.io/docs`). Разделы: «Vulnerability scanning», «Content trust / signature», «Tag immutability», «Replication». Поиск: `harbor cosign signature verification policy`.
- **Sonatype Nexus Repository** — `help.sonatype.com`. Раздел «Repository Manager / Repository Management». Поиск: `nexus repository 3 rest api repositories endpoint`.
- **Aqua Security Trivy** — `trivy.dev` (ранее `aquasecurity.github.io/trivy`). Раздел «Image scanning / Registry authentication». Поиск: `trivy image insecure registry scan`.
- **Gitleaks** — `github.com/gitleaks/gitleaks`. Раздел «Usage / detect». Поиск: `gitleaks detect report format configuration`.
- **Kubernetes** — `kubernetes.io/docs`. Разделы: «Secrets», «Encrypting data at rest», «Admission controllers». Поиск: `kubernetes secrets encryption at rest configuration`.
- **GitLab Docs** — `docs.gitlab.com`. Разделы: «CI/CD variables», «ID tokens / OIDC», «Protected branches». Поиск: `gitlab ci id token hashicorp vault`.
- **CNCF / OpenBao** — `openbao.org` (форк Vault) `[ПРОВЕРИТЬ актуальность документации и лицензионных условий на момент чтения]`.

Все флаги `cosign`, `gitleaks` и номера версий проверяй по `--help` и актуальной документации: они меняются чаще, чем выходят новые главы.
