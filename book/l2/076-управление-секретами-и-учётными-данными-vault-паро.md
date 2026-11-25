# Управление секретами и учётными данными: Vault, парольные менеджеры, ротация, привилегированный доступ

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 9:12. В поддержку пишет Сидоров: «1С на складе не открывается, пишет "не удалось подключиться к базе", у нас отгрузки стоят». Смотришь на SRV1C01 — сервис 1С живой, сеть есть, SQL отвечает. А в логе скрипта, который тянет пароль учётки из Vault, — `403 permission denied`. Токен, который выдали этому скрипту полгода назад «пока так, потом поправим», истёк. Никто не заметил, потому что заметить было некому.

Такие заявки — хлеб L2. Пароли и ключи живут везде: в скриптах, в конфигах, в GPO, в головах админов, в переписке. Каждый раз, когда такой секрет «утекает» или «устаревает», встаёт сервис, а иногда — открывается дверь. После этой главы ты будешь уметь: разворачивать хранилище секретов (HashiCorp Vault Community Edition) и раздавать из него доступ по политикам и короткоживущим токенам; понимать, чем управляемые сервисные учётки (gMSA) лучше «обычной учётки с паролем на стикере»; настраивать автоматическую ротацию локальных админских паролей через Windows LAPS; вести личное хранилище паролей и искать plaintext-секреты по инфраструктуре; объяснять, что такое привилегированный доступ (PAM) и зачем он нужен.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Секрет** — это любая строка, дающая доступ: пароль, токен, API-ключ, приватный ключ SSH, сертификат, строка подключения к БД. У секрета есть жизненный цикл:

```text
создать → сохранить (шифрованно) → выдать потребителю → использовать → ротировать → отозвать → проверить по аудиту
                                    ↑                                        │
                                    └────────── кто и когда брал ────────────┘
```

**Где что лежит по слоям.**

| Слой | Механизм | Кто использует |
|---|---|---|
| ОС Windows | DPAPI, LSA secrets, Credential Manager, `cmdkey` | пользователь, службы на одной машине |
| AD | gMSA (group Managed Service Account), Windows LAPS, krbtgt | сервисные учётки, локальные админы |
| Приложение/инфраструктура | HashiCorp Vault / OpenBao, Kubernetes Secrets (сами по себе не шифрованы!) | скрипты, CI, сервисы |
| Человек | KeePassXC, `pass`, 1Password, «Пассворк» [ПРОВЕРИТЬ актуальность названия] | админы, сотрудники |
| Привилегированный доступ | PAM-система (прокси-сессии, запись экрана, выдача по заявке) | подрядчики, админы, аудит |

**Vault — как он работает.** Это HTTP-сервис (порт **8200**, в проде — только TLS; порт **8201** — служебный кластерный). Внутри:

- **Storage backend** — где физически лежат зашифрованные данные (на стенде `file`, в проде — integrated storage (Raft) или Consul).
- **Barrier** — слой шифрования. Ключ шифрования (master key) сам зашифрован **unseal-ключами** по схеме Шамира: например, 5 частей, любые 3 восстанавливают ключ. После перезагрузки Vault стартует **sealed** (запечатанным) — пока 3 человека не введут свои части, он не отдаёт секреты. Root token и unseal-ключи хранят раздельно, в сейфе/физически у разных людей.
- **Secrets engines** — «движки», дающие секреты: `kv` (просто хранилище «ключ-значение»), `database` (генерирует временного пользователя БД), `pki` (выдаёт сертификаты), `ssh` (подписывает ключи на 5 минут).
- **Auth methods** — как клиент доказывает, что он тот, за кого себя выдаёт: `token`, `approle` (для сервисов), `ldap` (для людей), `kubernetes`, `cert`. На выходе — **токен с TTL** и набором **политик**.
- **Политики (HCL)** — правила вида «этот путь читать можно, писать нельзя».
- **Audit devices** — журнал КАЖДОГО обращения к API (кто, когда, к какому пути, успех/отказ). Без аудита Vault в проде не имеет смысла.

**gMSA (group Managed Service Account)** — учётка AD без известного людям пароля. Пароль (240 байт, меняется каждые 30 дней) вычисляет контроллер домена из **KDS root key**, а забрать его может только та машина, которой разрешено (`PrincipalsAllowedToRetrieveManagedPassword`). Протокол — Kerberos (порт 88) плюс обычный LDAP (389/636) для чтения атрибутов.

**Windows LAPS** — механизм, при котором у каждой машины свой уникальный пароль локального администратора, он лежит в AD в атрибуте компьютера, ротируется по расписанию и читается только тем, кому выдано право. Раньше это был отдельный MSI (Legacy LAPS), сейчас встроен в Windows 11 23H2+ и Windows Server 2022 с обновлениями [ПРОВЕРИТЬ точный KB для вашей сборки].

Как это выглядит на нашем стенде:

```text
        ЗАПИСЬ                     ХРАНЕНИЕ                       ЧТЕНИЕ
 adm.student ──► pass/KeePassXC (UBU01)          ──► человек вводит руками
 SRV1C01 (1С) ──► Vault kv secret/srv1c01/db     ──► AppRole role_id+secret_id ──► токен 1ч
 SRV1C01 (служба) ──► AD gMSA gmsa-1c01$         ──► Kerberos, пароль не знает никто
 WIN11-CL01 ──► LAPS (пароль локального админа)  ──► Get-LapsADPassword (только adm.student)
```

## 3. Аналогия из жизни

Представь банковский депозитарий. Раньше каждый сотрудник носил ключ от своей ячейки в кармане и иногда терял его на улице (пароль в скрипте). Теперь есть банк (Vault): ты приходишь, показываешь пропуск (AppRole), и тебе выдают **разовый ключ с таймером** — через час он рассыпается (TTL токена). Внутрь ячейки зайти можно только с этим ключом и только в свою ячейку (политика). Каждое открытие записывается на камеру (audit log). А сам сейф открывается только тремя ключами из пяти, которые лежат у трёх разных людей (unseal-ключи по схеме Шамира). Аналогия ломается в одном: в банке ключ выдаёт человек, а Vault — программа, поэтому утечка `secret_id` на сервере означает, что «пропуск» украли, и смотреть надо не на людей, а на логи и сроки жизни учётных данных.

## 4. Практика и команды

Все команды — с UBU01 (10.10.10.30, пользователь `adm.student`).

**1. Установка Vault и проверка версии.** Официальный репозиторий HashiCorp; нужен интернет, а он у нас есть через NAT VMnet8.

```bash
sudo apt install -y gpg wget
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y vault
vault -version
```
`lsb_release -cs` подставляет кодовое имя (для 24.04 — `noble`); если репозиторий для вашего кодового имени недоступен — скачайте бинарник с releases.hashicorp.com [ПРОВЕРИТЬ]. С версии 1.15 Vault CE распространяется под лицензией BUSL-1.1: для внутреннего использования бесплатен, но это не «свободная» лицензия. Полностью открытый форк — OpenBao (Linux Foundation), команды почти те же.

**2. Запуск в dev-режиме (только для первого знакомства).**

```bash
vault server -dev -dev-root-token-id=root &
export VAULT_ADDR='http://127.0.0.1:8200'
vault status
```
В выводе смотреть `Seal Type: shamir`, `Sealed: false`, `Storage: inmem`. Dev-режим держит данные в памяти и автоматически разанпечатан — в работе так нельзя, только «пощупать».

**3. Запись и чтение секрета (kv-v2).**

```bash
vault secrets enable -path=secret -version=2 kv
vault kv put secret/srv1c01/db username=svc-1c01 password='Lab#2026-vektor'
vault kv get secret/srv1c01/db
vault kv get -field=password secret/srv1c01/db      # только значение, удобно для скриптов
vault kv metadata get secret/srv1c01/db             # версии, дата создания
```
Ключевое: у kv-v2 физический путь — `secret/data/srv1c01/db`, а метаданные — `secret/metadata/...`. Это важно для политик, и на этом ловят новичков.

**4. Политика доступа (HCL).** Файл `srv1c01-read.hcl`:

```hcl
path "secret/data/srv1c01/*" {
  capabilities = ["read"]
}
path "secret/metadata/srv1c01/*" {
  capabilities = ["read", "list"]
}
```

```bash
vault policy write srv1c01-read srv1c01-read.hcl
vault policy list
vault policy read srv1c01-read
```
`capabilities` бывают `create, read, update, delete, list, sudo, deny`. Принцип: одна политика — одна задача, никакого `*` на всё.

**5. AppRole — «логин для сервиса».**

```bash
vault auth enable approle
vault write auth/approle/role/srv1c01 token_policies="srv1c01-read" token_ttl=1h token_max_ttl=4h secret_id_ttl=24h
vault read auth/approle/role/srv1c01/role-id
vault write -f auth/approle/role/srv1c01/secret-id
vault write auth/approle/login role_id=<role_id> secret_id=<secret_id>
```
`role_id` — «логин» (не секрет), `secret_id` — «пароль» (секрет, короткоживущий), на выходе — токен с TTL 1 час. `token_max_ttl` не даёт продлевать бесконечно.

**6. Работа с токеном и отзыв.**

```bash
vault token lookup
vault token renew            # продлить в пределах max_ttl
vault token revoke -self     # выйти
vault token revoke <token>   # отозвать чужой (нужны права)
vault list auth/approle/role/srv1c01/secret-id   # посмотреть выданные secret_id
```
После `revoke` все запросы с этим токеном — `403`.

**7. Аудит.**

```bash
sudo mkdir -p /var/log/vault && sudo chown vault:vault /var/log/vault
vault audit enable file file_path=/var/log/vault/audit.log
vault audit list
sudo tail -f /var/log/vault/audit.log
```
ОСТОРОЖНО: если Vault не сможет писать в файл аудита, он **перестанет отвечать на запросы**. Проверяйте права заранее; отключить — `vault audit disable file/`.

**8. Поиск plaintext-секретов (Linux).**

```bash
sudo grep -rniE '(pass(word|wd)?|secret|api[_-]?key|token)\s*=' /etc /opt /home 2>/dev/null | grep -viE '\.example|\.sample' | head -50
```
Смотрим на скрипты, `.env`, `*.ini`, `web.config`.

**9. Поиск plaintext-секретов (Windows).**

```powershell
Get-ChildItem C:\Scripts,C:\inetpub -Recurse -Include *.ps1,*.bat,*.cmd,*.config,*.ini -ErrorAction SilentlyContinue |
  Select-String -Pattern 'password\s*=', 'pwd=', 'connectionString' |
  Select-Object Path, LineNumber, Line
```
Найденное — в тикет на вынос в Vault, а не «оставим, работает же».

**10. gMSA на DC01.**

```powershell
Add-KdsRootKey -EffectiveTime ((Get-Date).AddHours(-10))
New-ADServiceAccount -Name gmsa-1c01 -DNSHostName gmsa-1c01.corp.vektor.lab `
  -PrincipalsAllowedToRetrieveManagedPassword "SRV1C01$"
```
`-EffectiveTime` в прошлом — лабораторный трюк, чтобы ключ заработал сразу; в проде KDS root key просто создают и ждут 10 часов. На потребителе: `Install-ADServiceAccount -Identity gmsa-1c01; Test-ADServiceAccount -Identity gmsa-1c01`. В службе указывается `VEKTOR\gmsa-1c01$` **без пароля**.

**11. Windows LAPS.**

```powershell
Update-LapsADSchema
Set-LapsADComputerSelfPermission -Identity "OU=Компьютеры,DC=corp,DC=vektor,DC=lab"
Get-LapsADPassword -Identity WIN11-CL01 -AsPlainText
Reset-LapsADPassword -Identity WIN11-CL01 -AsPlainText
```
[ПРОВЕРИТЬ] DN вашего OU `Компьютеры` — он зависит от того, вложен ли он в `Company`. `Get-LapsADPassword` требует права `Set-LapsADReadPasswordPermission` (или членства в группе, которой оно выдано).

**12. Личное хранилище на `pass` (CLI, GPG).**

```bash
sudo apt install -y pass
gpg --full-generate-key                 # если ключа adm.student ещё нет
pass init "adm.student@corp.vektor.lab"
pass insert -m infra/fs01/local-admin
pass generate infra/ubu01/root 24
pass show -c infra/fs01/local-admin     # скопировать в буфер, не светя на экране
pass git init                           # версионирование хранилища
```
Файлы лежат в `~/.password-store`, каждый зашифрован GPG. Аналог с GUI и общими папками — KeePassXC (файл `.kdbx` + пароль + key-файл); CLI-версия ставится пакетом `keepassxc` [ПРОВЕРИТЬ: в части дистрибутивов `keepassxc-cli` вынесен в отдельный пакет].

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L2 готов: DC01 (10.10.10.10, AD DS/DNS/DHCP), FS01 (10.10.10.20), WIN11-CL01 (DHCP, рабочее место `ivanov`), UBU01 (10.10.10.30, SSH, вход `adm.student` с sudo).
- SRV1C01 (10.10.10.25) — если ещё не создан, шаги 8–9 делаем на FS01 и в журнале помечаем это; на SRV1C01 вернёмся, когда он появится (это отдельная глава трека L2).
- На DC01 ты ходишь под `adm.student`, пароль стенда `Lab#2026-vektor`.
- Интернет на UBU01 проверен: `ping -c2 apt.releases.hashicorp.com`.

### Снапшот

В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot, имя **`before-l2-076`** — на UBU01, DC01, WIN11-CL01 и SRV1C01 (если есть). В `stand-log.md` строка: `2026-XX-XX: снапшот before-l2-076 снят на UBU01, DC01, WIN11-CL01, SRV1C01`.

### Шаги

1. **UBU01.** Устанавливаем Vault (команды из п.4.1), проверяем `vault -version`. Ожидаемо: `Vault v1.x.x`.
2. **UBU01.** Создаём каталоги и конфиг `/etc/vault.d/vault.hcl` (см. блок ниже), `sudo systemctl enable --now vault`. Ожидаемо: `systemctl status vault` — `active (running)`.

```hcl
ui            = true
disable_mlock = true          # на стенде; в проде mlock оставляют включённым

storage "file" {
  path = "/opt/vault/data"
}

listener "tcp" {
  address     = "10.10.10.30:8200"
  tls_disable = true          # ТОЛЬКО стенд! В проде — TLS-сертификат
}

api_addr = "http://10.10.10.30:8200"
```

3. **UBU01.** `export VAULT_ADDR='http://10.10.10.30:8200'`, затем `vault operator init -key-shares=5 -key-threshold=3`. Ожидаемо: 5 unseal-ключей и Initial Root Token. Сохраняем в файл `~/vault-init.txt` с правами `600` — и проговариваем вслух: **в реальности root token уходит в сейф, а unseal-ключи — трём разным людям**.
4. **UBU01.** Трижды `vault operator unseal` с разными ключами, затем `vault login <root_token>` и `vault status`. Ожидаемо: `Sealed: false`, `Total Shares: 5`, `Threshold: 3`.
5. **UBU01.** `vault secrets enable -path=secret -version=2 kv`, создаём секрет: `vault kv put secret/srv1c01/db username=svc-1c01 password='Lab#2026-vektor'`. Проверяем `vault kv get secret/srv1c01/db`. Ожидаемо: значения в выводе, `Version: 1`.
6. **UBU01.** Пишем политику `srv1c01-read.hcl` (п.4.4), `vault policy write srv1c01-read srv1c01-read.hcl`. Включаем AppRole, создаём роль, получаем `role_id` и `secret_id`, логинимся. Ожидаемо: токен с `token_policies: ["default" "srv1c01-read"]` и TTL 1h.
7. **UBU01.** Проверяем, что политика реально ограничивает: под токеном AppRole `vault kv get secret/srv1c01/db` — успех, `vault kv get secret/buh/payroll` — `403 permission denied`. Затем `vault token revoke -self` и снова чтение — снова ошибка. Ожидаемо: две разные ошибки, обе ожидаемые.
8. **DC01 (PowerShell от админа домена).** `Add-KdsRootKey -EffectiveTime ((Get-Date).AddHours(-10))`, затем `New-ADServiceAccount -Name gmsa-1c01 -DNSHostName gmsa-1c01.corp.vektor.lab -PrincipalsAllowedToRetrieveManagedPassword "SRV1C01$"`. Ожидаемо: команда без ошибок, `Get-ADServiceAccount gmsa-1c01` показывает объект.
9. **SRV1C01 (или FS01).** `Install-ADServiceAccount -Identity gmsa-1c01`, `Test-ADServiceAccount -Identity gmsa-1c01`. Ожидаемо: `True`. Пробуем настроить любую службу от `VEKTOR\gmsa-1c01$` с пустым паролем — запускается.
10. **DC01.** `Update-LapsADSchema`, `Set-LapsADComputerSelfPermission` на OU компьютеров, `Set-LapsADReadPasswordPermission ... -AllowedPrincipals "adm.student"`. Ожидаемо: схема обновлена без ошибок [ПРОВЕРИТЬ сообщения команд на вашей сборке].
11. **WIN11-CL01.** Через GPO (доменная политика на OU компьютеров) включаем LAPS: `Computer Configuration → Administrative Templates → System → LAPS` [ПРОВЕРИТЬ точный путь для вашей сборки], применяем `gpupdate /force`, перезагрузка. На DC01 проверяем `Get-LapsADPassword -Identity WIN11-CL01 -AsPlainText`. Ожидаемо: пароль из ~20 символов, `PasswordUpdateTime` — свежая.
12. **UBU01.** Ставим `pass`, создаём хранилище (п.4.12), заводим 3 записи: `infra/fs01/local-admin`, `infra/dc01/dsrм` [исправь на `infra/dc01/dsrm`], `web/regru`. Проверяем `pass ls` и `pass show infra/fs01/local-admin`.
13. **UBU01 + FS01.** Гигиена: запускаем поиск plaintext-секретов (п.4.8) и на FS01 (`\\FS01\Obshchee` в том числе) — PowerShell-вариант из п.4.9. Найденное записываем в тикеты, **ничего не удаляем молча**.
14. **Все.** Заполняем `stand-log.md`.

### Проверка

- [ ] `vault status` на UBU01: `Sealed: false`, `Storage: file`, `HA Enabled: false`.
- [ ] `vault kv get -field=password secret/srv1c01/db` возвращает `Lab#2026-vektor`.
- [ ] Под токеном AppRole чтение `secret/srv1c01/*` работает, чтение других путей — `403`.
- [ ] После `vault token revoke -self` токен недействителен.
- [ ] `/var/log/vault/audit.log` содержит записи `request.path` и `auth.display_name`.
- [ ] `Test-ADServiceAccount -Identity gmsa-1c01` = `True`.
- [ ] `Get-LapsADPassword -Identity WIN11-CL01` отдаёт пароль, а под обычным пользователем (`ivanov`) — отказ.
- [ ] `pass ls` показывает созданные записи, файлы в `~/.password-store` — бинарные (зашифрованы).
- [ ] В `stand-log.md` появилась запись про `before-l2-076` и все изменения.

### Сломай и почини

**Заявка.**

```text
Пользователь: Сидоров (склад) / ВМ: SRV1C01
Что написал: «1С не открывается, пишет "не удалось установить соединение с базой".
             С 9 утра не можем отгружать. Вчера всё работало.»
Срочность: Высокая (остановлены отгрузки)
```

**Как воспроизвести поломку (на UBU01, заранее).** В шаге 6 мы выдали `secret_id` с `secret_id_ttl=24h`. Имитируем «полгода прошло»: `vault write -f auth/approle/role/srv1c01/secret-id` создаёт новый, а старый не продлеваем — или просто `vault token revoke <token_from_service>`. На SRV1C01 кладём скрипт `get-db-pass.sh`, который логинится по сохранённым `role_id/secret_id` и падает с `403`.

**Диагностика по шагам (от простого к сложному).**

1. Сеть и порты: `Test-NetConnection 10.10.10.25 -Port 1433` (или порт вашей 1С) — живы ли БД и сервис. Если нет — это не секреты, закрываем как сетевую заявку.
2. Служба 1С: `Get-Service` на SRV1C01, журнал приложения (`Get-WinEvent -LogName Application -MaxEvents 50`). Ищем текст ошибки аутентификации.
3. Лог скрипта, который берёт секрет: `Get-Content C:\Scripts\get-db-pass.log -Tail 20`. Видим `403 permission denied` — значит, дело не в 1С и не в SQL.
4. Состояние Vault: `vault status` с UBU01. Если `Sealed: true` (например, UBU01 перезагрузили) — сервис и не мог получить секрет. Разанпечатываем тремя ключами.
5. Проверяем токен/secret_id: `vault token lookup <token>` → `403` или `token not found`; `vault write auth/approle/login role_id=... secret_id=...` → `invalid secret id`. Причина найдена: истёк `secret_id`.
6. Смотрим аудит: `sudo grep srv1c01 /var/log/vault/audit.log | tail` — видно, когда доступ работал, а когда начались отказы. Это же и доказательство для ИБ.
7. Решение: `vault write -f auth/approle/role/srv1c01/secret-id`, кладём новый `secret_id` в конфиг сервиса с правами только для сервисной учётки, перезапускаем скрипт/службу, проверяем подключение 1С.
8. Постоянное лечение: продлить TTL, поставить мониторинг на «secret_id истекает через N часов» (Zabbix на MON01, 10.10.10.40), а лучше — перейти на Vault Agent с авто-аутентификацией и обновлением токена [ПРОВЕРИТЬ синтаксис `auto_auth` для вашей версии].

**Ответ пользователю.**

```text
Алексей, добрый день. Причина — истёк срок действия служебного ключа,
через который сервис 1С получает пароль к базе. Ключ перевыпущен,
доступ восстановлен, отгрузки можно проводить. Чтобы не повторилось,
поставил контроль срока действия ключа и уведомление за сутки до истечения.
Приношу извинения за простой.
```

**Закрытие заявки.** Категория «Доступы/учётные данные», приоритет высокий, в решении — что сделано, root cause («истёк secret_id AppRole из-за отсутствия продления»), профилактика (мониторинг + план перехода на Vault Agent), ссылка на записи аудита. Запись в `stand-log.md` обязательна.

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен Vault CE, служба `vault`, конфиг `/etc/vault.d/vault.hcl`, данные `/opt/vault/data`, слушает 8200 по HTTP (без TLS — только стенд). Инициализирован: 5 unseal-ключей, порог 3; unseal-ключи и root token в `~/vault-init.txt` (600). Включены: `secret/` (kv-v2), audit-файл `/var/log/vault/audit.log`, auth method `approle`, политика `srv1c01-read`, роль `srv1c01` (TTL 1h/4h, secret_id 24h). Секрет `secret/srv1c01/db`. Установлен `pass`, хранилище `~/.password-store` с записями `infra/fs01/local-admin`, `infra/dc01/dsrm`, `web/regru`.
- **DC01 (10.10.10.10):** создан KDS root key; создана gMSA `gmsa-1c01$` с правом чтения пароля для `SRV1C01$`; обновлена схема AD под Windows LAPS, выданы права на OU компьютеров для `adm.student`.
- **WIN11-CL01:** управляется Windows LAPS через доменную GPO, пароль локального админа хранится в AD.
- **SRV1C01 (10.10.10.25):** установлена gMSA `gmsa-1c01`, `Test-ADServiceAccount` = True; скрипт `C:\Scripts\get-db-pass.sh` + лог, получает пароль из Vault по AppRole.
- **FS01:** без изменений в ролях; результаты поиска plaintext-секретов зафиксированы как тикеты.

### Откат

VMware Workstation → правый клик по ВМ → Snapshot Manager → выбрать `before-l2-076` → **Revert**. Порядок: сначала UBU01 (там Vault и pass), затем SRV1C01, WIN11-CL01, DC01 (откат DC01 откатывает KDS root key и схему LAPS). Если схему LAPS откатывать не хочется — снапшот DC01 можно не трогать: сама схема безвредна, но помни, что `Update-LapsADSchema` необратима. После откатa обнови `stand-log.md`: «откат к before-l2-076, Vault и gMSA удалены».

## 6. Ошибки новичков и безопасность

1. **Пароль в теле скрипта/в `.env` в git** → копится в истории репозитория и в бэкапах, утечка = компромат на годы → секреты только из хранилища, в коде — ссылка на путь в Vault; историю git чистить и считать скомпрометированной.
2. **Передача паролей в тикетах, почте и мессенджерах** → они остаются в базе заявок навсегда → первичный пароль выдаётся через менеджер паролей или одноразовую ссылку, принудительная смена при первом входе.
3. **Один root token Vault «на всё», без срока жизни** → его утечка = утечка всех секретов, и нет следов, кто что делал → root token только для первичной настройки, дальше — политики + AppRole + TTL, root token в сейфе.
4. **Ротация без учёта зависимых сервисов** → «поменяли пароль сервисной учётки» = лежащий сервис и ночной звонок → сначала карта потребителей (кто использует), потом ротация в окно, с планом откатa.
5. **Секреты в аргументах командной строки** → видны в `ps aux`, в истории bash, в логах процесса → читать через переменные окружения/файлы с правами 600, а лучше — через API хранилища.
6. **Класть KeePass-базу в `\\FS01\Obshchee`** (общая папка для всех) → файл скачивает любой сотрудник и брутфорсит офлайн → база на защищённом ресурсе, отдельный ACL, пароль + key-файл, журнал доступа.
7. **Отключить аудит Vault «чтобы не тормозило»** → нет доказательств, кто читал секреты, ИБ-расследование невозможно → аудит включён всегда, ротация файлов через logrotate, отправка в SIEM (глава 75).
8. **Дать политику `path "secret/*"`** → фактически полный доступ ко всем секретам → путь до конкретного префикса + отдельные политики на чтение/запись.

### Безопасность

В бою нельзя: держать Vault на HTTP без TLS (на стенде `tls_disable = true` — это только лаборатория); хранить unseal-ключи рядом с данными Vault (тогда шифрование бессмысленно); выдавать `adm.student` права читать все LAPS-пароли; оставлять gMSA с правом чтения пароля на всех машинах (`PrincipalsAllowedToRetrieveManagedPassword` — только нужные хосты); писать секреты в журнал стенда. Помни главное правило L2: **если ты видишь секрет — ты уже участник инцидента**, фиксируй факт, не пересылай его дальше и не копируй в тикет. В нормальной среде доступ к привилегированным секретам идёт через PAM-систему с заявкой, одобрением, ограничением по времени и записью сессии.

## 7. Вопросы на собеседовании

**1. «Зачем нужен Vault, если есть KeePass?»**

Ответ: KeePass — для людей, файл с паролями, ручное копирование. Vault — для машин: API, политики, короткоживущие токены (TTL), автоматическая ротация (движки `database`, `pki`), полный аудит каждого чтения, отзыв доступа в один шаг. KeePass не умеет выдавать секрет сервису по политике и не умеет отзывать доступ через час. На практике они сосуществуют: люди — в менеджере паролей (или в Vault через LDAP-auth), сервисы — в Vault.

*Что хочет услышать интервьюер:* понимание разницы «человеческий сейф» vs «машинный API с TTL и аудитом».
*Красный флаг:* «Vault — это просто менеджер паролей, только платный».

**2. «Что будет с Vault после перезагрузки сервера и что такое unseal?»**

Ответ: Vault стартует в состоянии `sealed` — master key в памяти отсутствует, данные на диске зашифрованы. Master key восстанавливается из unseal-ключей по схеме Шамира (например, 3 из 5). После ввода порога ключей Vault разанпечатывается и работает. Поэтому в проде unseal-ключи хранят раздельно (сейф, разные ответственные, иногда auto-unseal через KMS/HSM), а в кластере каждый узел разанпечатывается отдельно. Если Vault запечатан, все потребители секретов получают ошибки — типичная причина «внезапно упавшего сервиса» после ребута.

*Что хочет услышать интервьюер:* что он понимает модель шифрования и знает про sealed-состояние как реальную причину инцидента.
*Красный флаг:* «Vault сам расшифровывается, ключи не нужны».

**3. «Чем gMSA лучше обычной сервисной учётки?»**

Ответ: пароль gMSA не знает никто — его вычисляет контроллер домена из KDS root key и отдаёт только разрешённым компьютерам по Kerberos; смена каждые 30 дней автоматически, без перезапуска службы. Обычная сервисная учётка: пароль знает админ, он живёт годами, лежит в конфиге, а ротация ломает сервис и поэтому её не делают. Ограничение: gMSA работает только с доменными компьютерами и не подходит для мультиплатформенных сценариев (например, Linux-сервис вне AD) — там Vault или отдельное хранилище.

*Что хочет услышать интервьюер:* «пароль не знает человек» + автоматическая ротация + ограничения применимости.
*Красный флаг:* «gMSA — это просто группа в AD».

**4. «Как вы построите ротацию паролей в компании на 50 серверов и 200 сотрудников?»**

Ответ: сначала инвентаризация: какие учётки есть (человеческие, сервисные, привилегированные, локальные админы), кто их потребитель, где хранятся. Дальше по классам: пользовательские — политика паролей домена + MFA, принудительная смена при компрометации; локальные админские — Windows LAPS (уникальный пароль на машину, ротация по расписанию, чтение по праву); сервисные — gMSA там, где возможно, иначе Vault с динамическими учётками или коротким TTL; привилегированные человеческие — PAM/менеджер паролей с выдачей по заявке. Ключевое: ротация планируется с картой зависимостей, контролем (мониторинг успешности) и тестовым окном, а не «в пятницу вечером».

*Что хочет услышать интервьюер:* системность — инвентаризация → классификация → механизм на класс → контроль и окно работ.
*Красный флаг:* «поставлю GPO смену пароля каждые 30 дней, этого достаточно».

**5. «Что такое привилегированный доступ и зачем PAM, если есть LAPS и Vault?»**

Ответ: привилегированный доступ — это вход с повышенными правами (доменный админ, root, доступ к 1С-серверу, к коммутатору). PAM-система ставит между человеком и целевой системой прокси: админ не знает пароль вообще, получает доступ по заявке на время, сессия записывается, все действия логируются, а доступ можно отозвать мгновенно. LAPS закрывает узкую задачу — пароли локальных админов; Vault — секреты для машин. PAM — про людей и их сессии, и он же даёт ИБ ответ на вопрос «кто и когда заходил на сервер 1С».

*Что хочет услышать интервьюер:* разделение зон ответственности LAPS / Vault / PAM и понимание, что PAM — это про контроль сессий, а не про хранение паролей.
*Красный флаг:* «PAM — это то же самое, что менеджер паролей».

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — раздел Vault Documentation: Concepts (Seal/Unseal, Leases, Policies), Secrets Engines → KV v2, Auth Methods → AppRole. Поиск: `vault kv-v2 approle policy tutorial`.
- **learn.microsoft.com** — Group Managed Service Accounts overview; Windows LAPS overview и раздел про PowerShell-модуль LAPS (`Get-LapsADPassword`, `Update-LapsADSchema`). Поиск: `group managed service accounts overview`, `Windows LAPS PowerShell`.
- **openbao.org** — документация OpenBao (открытый форк Vault, Linux Foundation). Поиск: `openbao documentation getting started`.
- **keepassxc.org** — User Guide, раздел про `keepassxc-cli` и настройки безопасности базы.
- **passwordstore.org** — `pass`: Password Store, man page (`pass insert`, `pass generate`, `pass git`).
- **cisecurity.org** — CIS Controls v8, Control 5 «Account Management» и Control 4 «Secure Configuration» [ПРОВЕРИТЬ нумерацию в текущей версии Controls].
- **nist.gov** — NIST SP 800-57 Part 1 Rev.5 (управление ключами) [ПРОВЕРИТЬ актуальную ревизию], SP 800-63B (аутентификация). Поиск: `NIST SP 800-57 key management`.
- Разделы по управлению доступом в документации на отечественные PAM-решения (например, «Блокхост», «Атлант»/Indeed PAM, «Солар»/Dallas Lock) — сверяй по сайту вендора, названия и версии меняются [ПРОВЕРИТЬ].
