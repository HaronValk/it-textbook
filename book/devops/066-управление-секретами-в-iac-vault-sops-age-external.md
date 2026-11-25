# Управление секретами в IaC: Vault, SOPS, age, External Secrets Operator, Lockbox

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в поддержку (GIT01, репозиторий `infra-apps`):**

> Пользователь: sidorov (Алексей Сидоров, владелец учебного сервиса `app-sklad`)
> ВМ: k8s-w01
> Что написал: «В `infra-apps` в коммите `a1b2c3` в `values.yaml` лежит пароль от БД открытым текстом. Ревью не пропустило, но коммит уже в main и улетел в origin. Пароль рабочий, база живая. Что делать?»
> Срочность: высокая

Как только вы начинаете описывать инфраструктуру кодом (Ansible, Terraform, Helm, k8s-манифесты), перед вами встаёт вопрос: **где живут пароли, токены и ключи?** В файлах IaC их быть не может — Git помнит всё навсегда, а репозиторий читают все, у кого есть доступ. Держать секреты только «в голове админа» тоже не работает: пайплайн в GitLab (GIT01) должен разворачивать приложение без человека.

Дальше появляется вторая проблема — не «где хранить», а «как доставить»: поду в Kubernetes нужен пароль от БД, Terraform нужен ключ от облака, Ansible — пароль от учётки. Каждый раз вручную копировать — это дрейф, ошибки и утечки. После этой главы вы должны уметь: зашифровать секрет так, чтобы его можно было безопасно хранить в Git (SOPS + age), поднять центральное хранилище (HashiCorp Vault) и выдать из него доступ по политике, синхронизировать секреты в Kubernetes через External Secrets Operator, а также понимать, как то же самое делается в российском облаке через Yandex Lockbox. И главное — уметь действовать, когда пароль уже утёк.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Три базовых подхода к секретам в IaC:

1. **Зашифровано в Git** (SOPS + age / KMS). Секрет лежит в репозитории, но в зашифрованном виде; расшифровать может только тот, у кого есть приватный ключ. Плюс: версионирование, ревью, всё воспроизводимо. Минус: ротация и аудит доступа слабые.
2. **Центральный секрет-стор** (Vault, Lockbox, OpenBao). Секреты вне Git, доступ по политикам, аудит-лог, TTL и динамические секреты. Минус: это ещё один сервис, который надо поднять, запечатать/распечатать (unseal) и забэкапить.
3. **Оператор-синхронизатор** (External Secrets Operator, ESO). Компонент в Kubernetes, который тянет секреты из внешнего стора и создаёт из них обычные `Secret`. Даёт приложениям привычный механизм (`envFrom`, volume), не заставляя их знать про Vault.

Схема взаимодействия на нашем стенде:

```text
                     ┌───────────────────────────────┐
  developer          │  GIT01 (GitLab CE + runner)   │
  ─────────          │                               │
  sops -e  ─────────▶│  infra-apps/                  │
  (age, локально)    │    secrets/app-sklad.enc.yaml │◀── зашифровано, можно в Git
                     │    .sops.yaml                 │
                     └───────────┬───────────────────┘
                                 │ CI job: SOPS_AGE_KEY (masked/file)
                                 ▼
                     ┌───────────────────────────────┐
                     │  UBU01 10.10.10.30            │
                     │  Vault :8200 (kv-v2, policy)  │
                     └───────────┬───────────────────┘
                                 │ HTTP API, token / k8s auth
                                 ▼
   k8s-cp01 10.10.10.61  ┌─────────────────────────┐
   (control plane)       │ External Secrets Operator│
                         │ ClusterSecretStore      │
                         │ ExternalSecret ────────▶│──▶ Secret app-sklad-db
                         └───────────┬─────────────┘        │
                                     │                      ▼
                                     │              Pod app-sklad (envFrom)
                                     ▼
                        k8s-w01 10.10.10.62 / k8s-w02 10.10.10.63
```

**SOPS** (Secrets OPerationS, Mozilla → CNCF-проект) шифрует *значения* в YAML/JSON/ENV-файлах, оставляя ключи открытыми — поэтому диффы в Git читаемы. Шифрование — конвертное: генерируется data key (DEK), им шифруются значения, а сам DEK шифруется мастер-ключом (age-ключ, KMS-ключ). В файл пишется метаданные `sops:` с указанием, чем зашифрован DEK, и MAC для проверки целостности. **age** — современный инструмент асимметричного шифрования (X25519): приватный ключ у вас, публичным шифруют. Порт не нужен, это чистая криптография файлов.

**Vault** — HTTP API на порту 8200. Ключевые понятия: *secrets engine* (например `kv-v2` — статичные секреты с версиями), *auth method* (token, AppRole, Kubernetes — проверяет, кто вы), *policy* (HCL-правила `path ... capabilities`), *lease/TTL* (динамические секреты из БД живут ограниченное время — это главный аргумент за Vault). Vault при старте «запечатан» (sealed): нужен unseal-ключ, чтобы расшифровать мастер-ключ.

**ESO** — оператор (CRD `SecretStore`/`ClusterSecretStore` + `ExternalSecret`). Раз в `refreshInterval` он читает внешний стор и приводит `Secret` к нужному виду; в `status` пишет, синхронизировано или нет. Ему нужны права в Vault — в лаборатории токеном, в бою — через `vault auth enable kubernetes` с ревью service account токена.

**Yandex Lockbox** — managed-сервис секретов, аналог Vault от облака: секрет = набор версий с ключами, доступ по IAM/сервисным аккаунтам, в Terraform — ресурсы `yandex_lockbox_secret` и `yandex_lockbox_secret_version`. Смысл тот же: секретов в Git нет, в `tf`-коде только ссылки.

## 3. Аналогия из жизни

Представьте банковские ячейки. **Vault** — это банк: чтобы получить свою коробку, вы проходите идентификацию (auth method), охранник смотрит в список разрешённых (policy), а факт выдачи пишется в журнал (audit log). Банк можно взять в аренду у другого владельца (Lockbox), а можно построить свой сейф в подвале.

**SOPS + age** — это коробка, которую вы сами заперли на замок и оставили дома у друга (Git). Ключ только у вас; кто коробку ни увидит, внутрь не попадёт. Удобно, дёшево, но никто не скажет, кто и когда её открывал.

**External Secrets Operator** — курьер, который раз в час ходит в банк по доверенности и приносит копию содержимого в нужный кабинет (Secret в кластере). Если вы поменяли содержимое ячейки, у приложения всё равно старая копия, пока курьер не сходит снова.

Аналогия ломается на динамических секретах: в реальном банке нельзя получить ключ, который через час сам рассыпается в пыль, а Vault так умеет — выдаёт пароль к БД с TTL и отзывает его.

## 4. Практика и команды

**1. Генерация age-ключа (локально, на UBU01 или на своём ПК).**

```bash
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/keys.txt
```

Вывод:

```text
# created: 2026-01-10T10:00:00Z
# public key: age1q5x8...7n2w
AGE-SECRET-KEY-1Q3F...Z9
```

`-o` — куда писать приватный ключ. Публичный (`age1...`) раздаём шифрующим, приватный не уходит никуда: ни в Git, ни в тикеты. Проверить соответствие пары: `age-keygen -y ~/.config/sops/age/keys.txt`.

**2. Правила шифрования `.sops.yaml` (в корне репозитория).**

```yaml
creation_rules:
  - path_regex: secrets/.*\.enc\.yaml$
    encrypted_regex: '^(password|token|secretKey|apiKey)$'
    age: age1q5x8...7n2w
```

`path_regex` — какие файлы SOPS шифрует автоматически; `encrypted_regex` — шифровать только перечисленные ключи, остальное оставить открытым (удобно для ревью); `age` — получатели, которым можно расшифровать.

**3. Шифрование файла.**

```bash
cd ~/repos/infra-apps
sops -e secrets/app-sklad.yaml > secrets/app-sklad.enc.yaml
```

`-e` — encrypt в stdout, `-d` — decrypt. Интерактивно править зашифрованный файл: `sops secrets/app-sklad.enc.yaml` (откроется редактор, на выходе снова зашифровано). В выводе видно:

```yaml
db:
    password: ENC[AES256_GCM,data:9tK...,iv:...,tag:...,type:str]
sops:
    age:
        - recipient: age1q5x8...7n2w
          enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
```

Смотреть надо: (а) что чувствительные поля действительно `ENC[...]`, (б) что в `sops.age.recipient` ваши ключи, а не чужие.

**4. Расшифровка и подстановка в процесс.**

```bash
sops -d secrets/app-sklad.enc.yaml
sops exec-env secrets/app.env.enc 'bash -c "psql -h 10.10.10.30 -U app -c \"select 1\""'
```

`sops exec-env <file> <cmd>` расшифровывает ENV-файл в переменные окружения только на время команды — секреты не остаются на диске и не видны в `ps` как аргументы.

**5. Ротация получателей после увольнения коллеги.**

```bash
# убрали его публичный ключ из .sops.yaml, добавили нового
sops updatekeys secrets/app-sklad.enc.yaml
```

`updatekeys` перешифровывает DEK под новый список получателей, не меняя сами значения. Принудительно перегенерировать DEK: `sops rotate -i secrets/app-sklad.enc.yaml` [ПРОВЕРИТЬ: точный синтаксис rotate в вашей версии SOPS].

**6. Поиск утечек в репозитории.**

```bash
gitleaks detect --source . --verbose
git log -S 'Lab#2026-vektor' --oneline --all
```

`gitleaks` ищет по регуляркам и энтропии (`--source` — где сканировать); `git log -S` ищет коммиты, где строка появлялась/исчезала. Второе — самое быстрое, когда известно значение.

**7. Поднимаем Vault dev-сервер на UBU01.**

```bash
docker run -d --name vault --cap-add=IPC_LOCK \
  -e 'VAULT_DEV_ROOT_TOKEN_ID=root' \
  -p 8200:8200 hashicorp/vault:1.15 server -dev
export VAULT_ADDR=http://10.10.10.30:8200
export VAULT_TOKEN=root
vault status
```

Dev-режим: в памяти, автоматически распечатан, kv-v2 смонтирован в `secret/`, root-токен задан вручную. **Это только для стенда** — данные исчезнут при рестарте контейнера, а root-токен знает весь интернет. Тег образа `[ПРОВЕРИТЬ]` под текущую версию. Лицензия Vault — BUSL 1.1, для внутреннего использования бесплатна; в компании, где важна чистая открытая лицензия, смотрят на OpenBao (форк от Linux Foundation).

**8. Запись и чтение секрета.**

```bash
vault kv put secret/app-sklad/db username=app password='Lab#2026-vektor'
vault kv get -format=json secret/app-sklad/db | jq .data.data.password
vault kv get -version=1 secret/app-sklad/db
```

`kv put` создаёт новую версию, `-version=N` читает конкретную; в JSON-выводе kv-v2 значения лежат в `.data.data`, метаданные — в `.data.metadata`. Метрика `data.metadata.version` — то, что вы будете проверять при «почему приложение видит старый пароль».

**9. Политика и токен для приложения.**

```bash
cat > app-sklad-policy.hcl <<'EOF'
path "secret/data/app-sklad/*" {
  capabilities = ["read"]
}
EOF
vault policy write app-sklad-read app-sklad-policy.hcl
vault token create -policy=app-sklad-read -ttl=1h
```

Важно: у kv-v2 реальный путь данных — `secret/data/...`, а `secret/app-sklad/...` вернёт 403. `-ttl=1h` — токен умрёт сам. `vault token lookup` покажет политики и остаток TTL.

**10. Ставим External Secrets Operator (с рабочей станции с kubectl на k8s-cp01).**

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace
kubectl -n external-secrets get pods
```

Ждём три пода в `Running` (controller, webhook, cert-controller).

**11. Описываем хранилище и секрет в Kubernetes.**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: vault-token
  namespace: external-secrets
type: Opaque
stringData:
  token: root
---
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: vault-backend
spec:
  provider:
    vault:
      server: "http://10.10.10.30:8200"
      path: "secret"
      version: "v2"
      auth:
        tokenSecretRef:
          name: vault-token
          key: token
          namespace: external-secrets
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: app-sklad-db
  namespace: app-sklad
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: app-sklad-db
    creationPolicy: Owner
  data:
    - secretKey: DB_PASSWORD
      remoteRef:
        key: app-sklad/db
        property: password
```

`target.creationPolicy: Owner` — ESO владеет Secret и перезапишет его при расхождении; `refreshInterval` — период сверки; `remoteRef.key` — путь внутри `secret/` без префикса `data`. Версия API у ESO менялась (`v1beta1` → `v1`) — сверьте с вашей версией чарта [ПРОВЕРИТЬ].

**12. Диагностика синхронизации.**

```bash
kubectl -n app-sklad get externalsecret
kubectl -n app-sklad describe externalsecret app-sklad-db
kubectl -n app-sklad get secret app-sklad-db -o jsonpath='{.data.DB_PASSWORD}' | base64 -d; echo
```

В `describe` смотрим `Status.Conditions`: `Ready=True, Reason=SecretSynced` — хорошо; `SecretSyncedError` — читаем сообщение (403, 404, «connection refused»). В `Events` будет текст ошибки от провайдера Vault.

**13. Terraform: секреты не в коде, а в переменных и в облачном сторе.**

```hcl
variable "db_password" {
  type      = string
  sensitive = true
}

resource "yandex_lockbox_secret" "app_sklad_db" {
  name = "app-sklad-db"
}

resource "yandex_lockbox_secret_version" "app_sklad_db_v1" {
  secret_id = yandex_lockbox_secret.app_sklad_db.id
  entries {
    key        = "password"
    text_value = var.db_password
  }
}
```

Значение приходит из `TF_VAR_db_password` (CI-переменная GitLab, masked) или из Vault-провайдера, а не из `terraform.tfvars` в репозитории. `sensitive = true` лишь скрывает значение в выводе плана — **в state секрет всё равно попадёт**, поэтому backend шифруется и доступен узкому кругу. Точные имена полей Lockbox-ресурсов [ПРОВЕРИТЬ] по документации провайдера Yandex Cloud.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны: **UBU01** (10.10.10.30, Ubuntu 24.04, Docker и kubectl настроен на кластер), **GIT01** (10.10.10.50, GitLab CE + runner, репозиторий `infra-apps`), **k8s-cp01** (10.10.10.61, кластер с рабочими k8s-w01/k8s-w02 из модуля Kubernetes). Windows-часть стенда не используется. В кластере должен быть namespace `app-sklad` (создайте: `kubectl create ns app-sklad`).

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-066`. Машины: **UBU01**, **GIT01**, **k8s-cp01** (снапшот control plane; состояние worker-ов при необходимости откатывается отдельно — или делайте снапшоты всех трёх узлов).

### Шаги

1. **UBU01.** Ставим инструменты: `sudo apt update && sudo apt install -y age jq` и бинарник SOPS с официальных релизов GitHub [ПРОВЕРИТЬ версию]. Проверка: `sops --version`, `age --version` — обе команды отвечают.
2. **UBU01.** Генерируем ключ: `age-keygen -o ~/.config/sops/age/keys.txt`, копируем `age1...` из вывода. Ожидаемо: файл с правами 600 и публичный ключ в консоли.
3. **UBU01.** Клонируем репозиторий: `git clone git@10.10.10.50:adm.student/infra-apps.git`, создаём `secrets/app-sklad.yaml`:

```yaml
db:
  username: app
  password: Lab#2026-vektor
api:
  token: lab-token-123
```

4. **UBU01.** Кладём в корень `.sops.yaml` из раздела 4 (свой публичный ключ) и шифруем: `sops -e secrets/app-sklad.yaml > secrets/app-sklad.enc.yaml`. Ожидаемо: значения в `ENC[...]`, исходный файл добавьте в `.gitignore`.
5. **UBU01.** Коммитим только `.enc.yaml` и `.sops.yaml`: `git add .sops.yaml secrets/app-sklad.enc.yaml && git commit -m "add encrypted app-sklad secrets" && git push`. Ожидаемо: пуш проходит, в веб-интерфейсе GIT01 значения нечитаемы.
6. **GIT01.** В GitLab: **Settings → CI/CD → Variables** добавляем `SOPS_AGE_KEY` (тип **File**, masked, protected) с содержимым `keys.txt`. В `.gitlab-ci.yml` джоба `deploy-check` вызывает `sops -d secrets/app-sklad.enc.yaml > /dev/null && echo OK`. Ожидаемо: пайплайн зелёный, в логах нет plaintext.
7. **UBU01.** Поднимаем Vault dev: команда из п.7 раздела 4. Проверяем `vault status` → `Sealed false`, `Storage Type inmem`.
8. **UBU01.** Кладём секрет: `vault kv put secret/app-sklad/db username=app password='Lab#2026-vektor'`. Проверяем `vault kv get secret/app-sklad/db` — видим `version 1`.
9. **UBU01.** Создаём политику и токен (п.9 раздела 4), сохраняем выданный `hvs...` токен в файл. Ожидаемо: `vault token lookup` показывает `policies [app-sklad-read default]`.
10. **UBU01 (kubectl).** Ставим ESO чартом (п.10). Ожидаемо: `kubectl -n external-secrets get pods` — 3 пода `Running`.
11. **UBU01.** Применяем манифесты из п.11 раздела 4 (в `Secret vault-token` подставьте токен из шага 9, не `root`). Ожидаемо: `kubectl -n app-sklad get externalsecret` → `STATUS SecretSynced`, `READY True`.
12. **UBU01.** Проверяем, что под реально получает пароль: создайте `Deployment` с `envFrom: - secretRef: {name: app-sklad-db}` и командой `sleep 3600`, затем `kubectl -n app-sklad exec deploy/app-sklad -- env | grep DB_PASSWORD`. Ожидаемо: `DB_PASSWORD=Lab#2026-vektor`.
13. **UBU01.** Ротация: `vault kv put secret/app-sklad/db username=app password='Lab#2026-vektor-v2'`, форсируем сверку аннотацией (см. п. «Сломай и почини») и убеждаемся, что Secret обновился. Ожидаемо: в `kubectl get secret ... -o jsonpath` новый пароль, но в уже запущенном поде — старый, пока под не перезапустится.

### Проверка

- [ ] `sops -d secrets/app-sklad.enc.yaml` на UBU01 отдаёт открытый YAML, а в веб-интерфейсе GIT01 файл нечитаем.
- [ ] `git log --all -S 'Lab#2026-vektor'` в репозитории не находит совпадений в закоммиченных файлах.
- [ ] `vault status` — `Sealed false`, `vault kv get secret/app-sklad/db` отдаёт версию 1.
- [ ] `vault token lookup` для токена приложения показывает только `app-sklad-read` и `default`.
- [ ] `kubectl -n app-sklad get externalsecret` — `SecretSynced / True`.
- [ ] `kubectl -n app-sklad exec deploy/app-sklad -- env | grep DB_PASSWORD` отдаёт пароль.
- [ ] После смены пароля в Vault Secret в кластере обновился в пределах `refreshInterval` (или после force-sync).
- [ ] В `stand-log.md` записаны: Vault на UBU01:8200 (dev), ESO в namespace `external-secrets`, `ClusterSecretStore vault-backend`, `ExternalSecret app-sklad-db`.

### Сломай и почини

> **Пользователь:** sidorov (владелец сервиса `app-sklad`)
> **ВМ:** k8s-w01
> **Что написал:** «Под `app-sklad` не поднимается, в событиях `CreateContainerConfigError: secret "app-sklad-db" not found`. Полчаса назад всё работало. Выкатывали только правку ExternalSecret.»
> **Срочность:** высокая

**Как воспроизвести:** в `ExternalSecret` поменяйте `remoteRef.key` на несуществующий путь `app-sklad/db-old`, примените (`kubectl apply -f externalsecret.yaml`). Через несколько секунд Secret перестанет обновляться, а если удалить его руками — под не стартует.

**Диагностика по порядку:**

1. `kubectl -n app-sklad get externalsecret app-sklad-db` → `STATUS SecretSyncedError`.
2. `kubectl -n app-sklad describe externalsecret app-sklad-db` → в `Conditions`/`Events` читаем причину: `secret not found` / `permission denied` / `connection refused`.
3. `kubectl get clustersecretstore vault-backend -o yaml` → `Ready=True`? Если `False` — проблема в токене/сети, а не в пути.
4. Проверяем сам Vault: `vault kv get secret/app-sklad/db-old` → `No value found at secret/data/app-sklad/db-old` — путь неверный, вот и причина.
5. Отдельно проверяем права: `VAULT_TOKEN=<токен приложения> vault kv get secret/app-sklad/db` — если 403, виновата политика.

**Решение:** вернуть корректный `remoteRef.key: app-sklad/db` и применить. Если Secret уже удалён, ESO пересоздаст его сам; чтобы не ждать `refreshInterval`, форсируем:

```bash
kubectl -n app-sklad annotate externalsecret app-sklad-db \
  force-sync="$(date +%s)" --overwrite
kubectl -n app-sklad rollout restart deploy/app-sklad
```

**Что ответить пользователю:** «Причина — опечатка в пути к секрету в ExternalSecret (`app-sklad/db-old` вместо `app-sklad/db`), Vault отдавал 404, поэтому Secret не создавался. Путь исправлен, секрет синхронизирован, под перезапущен и в `Running`. Добавили в CI-проверку `kubectl get externalsecret` после деплоя, чтобы такое ловилось до вас».

**Закрытие заявки:** категория «конфигурация / IaC», в комментарий — коммит с фиксом, в постмортем — пункт «проверять статус ExternalSecret в пайплайне (`kubectl wait --for=condition=Ready externalsecret/...`)».

### Что изменилось на стенде

- **UBU01:** установлены `sops`, `age`, `gitleaks`; создан `~/.config/sops/age/keys.txt` (ключ стенда); запущен контейнер `vault` в dev-режиме на порту 8200, root-токен `root` (учебный); смонтирован kv-v2 `secret/`, создан путь `secret/app-sklad/db`, политика `app-sklad-read`, токен приложения.
- **GIT01:** в `infra-apps` появились `.sops.yaml`, `secrets/app-sklad.enc.yaml`, `.gitignore`, джоба `deploy-check`; в CI/CD Variables — file-переменная `SOPS_AGE_KEY`.
- **k8s (cp01/w01/w02):** namespace `external-secrets` с ESO; namespace `app-sklad` с `ClusterSecretStore vault-backend`, `ExternalSecret app-sklad-db`, `Secret app-sklad-db`, Deployment `app-sklad`.
- Пароль БД стенда для `app-sklad`: `Lab#2026-vektor` (версия 1), после ротации — `Lab#2026-vektor-v2` (версия 2).

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-066` → **Restore**. Откатывать нужно все три ВМ (UBU01, GIT01, k8s-cp01, при снапшотах — и воркеры), иначе получите рассинхрон: например, ESO остался в кластере, а Vault на UBU01 исчез, и `ExternalSecret` начнёт сыпать ошибками. После отката допишите в `stand-log.md` строку «откат к before-devops-066, изменения от <дата> отменены».

## 6. Ошибки новичков и безопасность

1. **Коммит `keys.txt` рядом с `.enc.yaml`** → всё шифрование бессмысленно, приватный ключ в истории Git. Правильно: приватный ключ только в `~/.config/sops/age/` и в CI-переменной типа File; в репозитории — лишь публичный `age1...`.
2. **Правка зашифрованного файла «руками»** → SOPS не расшифрует, MAC не сойдётся. Правильно: `sops <file>` (редактор) или `sops -d` → правка → `sops -e`.
3. **Vault в dev-режиме «пока сойдёт» на проде** → данные в памяти, root-токен у всех, нет unseal, нет бэкапа. Правильно: нормальный storage backend, auto-unseal через KMS, root-токен в сейф, приложения — через AppRole/Kubernetes auth с TTL.
4. **Токен Vault положен в `Secret` в Git рядом с манифестами** → та же утечка, только в k8s. Правильно: в лаборатории — руками, в бою — ESO с `auth.kubernetes` (service account), без статичных токенов.
5. **Слишком широкая политика `path "secret/*"`** → приложение читает чужие секреты. Правильно: `secret/data/app-sklad/*` + `capabilities = ["read"]`, по принципу минимальных привилегий.
6. **`sensitive = true` в Terraform воспринимают как «секрет защищён»** → он всё равно в plaintext в state и в `terraform.tfstate.backup`. Правильно: шифрованный remote backend, ограниченный доступ, при необходимости — эфемерные ресурсы/внешние источники.
7. **Ждут, что под обновит пароль сам** → env-переменные из Secret не меняются в живом контейнере. Правильно: либо `rollout restart` при ротации (в бою — автоматизация), либо монтировать файлом и перечитывать, либо использовать динамические секреты с реконнектом.
8. **Утечка уже случилась, а пароль просто удаляют из файла новым коммитом** → в истории он остался. Правильно: сначала **ротация секрета**, затем чистка истории (`git filter-repo`) и добавление сканера (`gitleaks`) в пайплайн.

**Безопасность (что нельзя в боевой среде):** нельзя хранить приватные ключи и root-токены в репозитории и в тикетах; нельзя отключать TLS у Vault (`http://` допустим только на изолированном стенде); нельзя давать приложениям root-токен и политику `*`; нельзя оставлять секреты в логах CI — GitLab маскирует переменные, но `sops -d` в `echo` их сведёт на нет. Отдельно: с 2023 года Vault под BUSL 1.1 — для внутреннего использования это бесплатно, но если юристы компании против такой лицензии, легальная альтернатива — OpenBao или облачный Lockbox.

## 7. Вопросы на собеседовании

**1. Чем SOPS + age отличается от Vault и когда вы выберете что?**

Ответ: SOPS — про секреты, которые должны лежать в Git рядом с кодом: конфиги приложений, небольшие наборы кредов, где важны версионирование и ревью. Расшифровка — по наличию приватного ключа, аудита и TTL нет. Vault — централизованный сервис: политики, аудит, TTL, динамические секреты к БД, множество приложений и команд, ротация. Типичная связка: Vault как источник истины, SOPS — для бутстрапа (например, чтобы положить токен для Ansible) или там, где Vault избыточен. *Интервьюер хочет услышать*: понимание, что это разные классы инструментов, а не конкуренты, и критерии выбора. *Красный флаг*: «Vault всегда лучше» без упоминания сложности эксплуатации, unseal и бэкапов.

**2. Как секрет попадает в под и почему нельзя запекать его в образ?**

Ответ: образ неизменяем и раздаётся в реестр (HARBOR01) — секрет в слое образa видели бы все, у кого есть pull, и он остался бы в истории. Правильно: секрет приходит извне во время запуска — через `Secret`/`ConfigMap`, смонтированный volume или env, а `Secret` наполняется либо `kubectl create secret` из CI, либо ESO из Vault/Lockbox. ESO создаёт `Secret` по `ExternalSecret` и обновляет его по `refreshInterval`; приложение читает при старте. *Интервьюер хочет услышать*: разделение «артефакт» и «конфигурация», плюс понимание, что env не обновляется в живом поде. *Красный флаг*: `ENV PASSWORD=...` в Dockerfile.

**3. Пароль закоммитили в Git. Ваши действия по шагам?**

Ответ: 1) считать секрет скомпрометированным и **сразу ротировать** (в Vault/БД/сервисе), 2) оценить, куда ушёл репозиторий (форки, клоны, CI-логи, артефакты), 3) удалить из рабочего дерева и добавить в `.gitignore`, 4) почистить историю (`git filter-repo`/BFG) и согласовать force-push со всеми, 5) включить сканирование (`gitleaks`) в pre-commit и пайплайн, 6) зафиксировать инцидент и выводы. Ключевое: ротация **до** чистки истории, потому что старый пароль уже утёк. *Интервьюер хочет услышать*: порядок и понимание, что история Git — это публикация. *Красный флаг*: «просто сделаю revert коммита».

**4. Как ротировать секреты без простоя и как понять, что приложение подхватило новый?**

Ответ: варианты — (а) `Secret` + `rollout restart` (простой на время рестарта, приемлемо для большинства), (б) монтирование файлом с перечитыванием по SIGHUP/периодически, (в) динамические секреты Vault с коротким TTL и переподключением (приложение должно уметь), (г) blue-green/rolling update за балансировщиком. Проверка: сравнить версию в `vault kv get` / `metadata.version` с содержимым `Secret` и с тем, что реально видит процесс (`exec ... env`). *Интервьюер хочет услышать*: осознанный выбор по критичности сервиса и проверяемость. *Красный флаг*: «ротация не нужна, пароль сложный».

**5. Как приложение аутентифицируется в Vault и что такое secret zero?**

Ответ: в бою — не статичным токеном. Для VM — AppRole (`role_id` + `secret_id`, короткоживущий токен), для Kubernetes — `vault auth enable kubernetes`, Vault проверяет service account токен через TokenReview и выдаёт токен по роли с привязкой к namespace/SA. Проблема «secret zero»: чтобы получить первый секрет, приложению уже нужен какой-то секрет (role_id, сертификат, SA-токен). Решается тем, что платформа даёт идентичность автоматически (k8s SA, IAM-роль инстанса, mTLS-сертификат от PKI), и уже на её основе Vault выдаёт остальное; плюс короткий TTL и аудит. *Интервьюер хочет услышать*: понимание идентичности и TTL/lease, а не «токен в переменной окружения». *Красный флаг*: «положим root-токен в конфиг-файл».

## 8. Источники (официальная документация)

- **HashiCorp Developer** (developer.hashicorp.com) — раздел **Vault → Secrets Engines → KV**, **Vault → Auth Methods → Kubernetes / AppRole**, **Vault → Concepts → Policies, Leases**. Поиск: `vault kv-v2 versioned secrets`, `vault kubernetes auth method`.
- **External Secrets Operator** (external-secrets.io) — разделы **Introduction**, **Provider → Vault**, **API → SecretStore / ExternalSecret**, **Guides → Multi Tenancy**. Поиск: `external secrets operator vault provider tokenSecretRef`.
- **Mozilla SOPS** (github.com/getsops/sops) — README, разделы **Encrypting with age**, **`.sops.yaml` creation rules**, **`encrypted_regex`**, **`updatekeys`**. Поиск: `sops creation_rules encrypted_regex age`.
- **age** (github.com/FiloSottile/age) — README, разделы **age-keygen**, **X25519 recipients**. Поиск: `age encryption age-keygen public key`.
- **Kubernetes** (kubernetes.io) — **Concepts → Configuration → Secrets**, **Tasks → Managing Secrets**. Поиск: `kubernetes secret envFrom secretRef`.
- **Yandex Cloud** (yandex.cloud/ru/docs) — **Lockbox → Обзор и управление секретами**, **Terraform-провайдер Yandex Cloud → yandex_lockbox_secret**. Поиск: `yandex cloud lockbox terraform secret version`. Точные имена полей ресурсов и версии провайдера [ПРОВЕРИТЬ] по актуальной документации.
- **OpenBao** (openbao.org) — форк Vault под эгидой Linux Foundation, раздел **Concepts** (если компания не принимает BUSL). Поиск: `openbao vault fork`.
- **Gitleaks** (github.com/gitleaks/gitleaks) — README, разделы **Usage**, **Configuration**. Поиск: `gitleaks detect source pre-commit`.
