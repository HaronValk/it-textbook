# Terraform и OpenTofu: провайдеры, state, remote backend, модули, workspaces, import

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 9:12. В очередь поддержки падает тикет: «Сидоров, склад. Второй день не выгружаются накладные, тестовый контур сервиса выгрузки не поднимается. Срочно, у нас отгрузки». Дежурный инженер открывает панель и видит: ВМ на месте, сервис запущен, но адрес и теги не те, что были в пятницу. Кто-то ночью «поправил руками» — а конфигурация, которая описывает эту инфраструктуру, об этом не знает. Дальше классика: инфраструктура в одном состоянии, код в другом, третий коллега в третьем — и никто не может сказать, «как правильно».

Именно эту проблему решает Terraform и его свободный форк OpenTofu. Это инструменты **IaC** (Infrastructure as Code — инфраструктура как код): вы описываете серверы, сети, DNS-записи, бакеты, неймспейсы Kubernetes текстовыми файлами, а инструмент приводит реальность к описанному виду. В L1-поддержке вы такое скорее встретите как «руками не трогать, всё через пайплайн», а в DevOps это ежедневная работа: любой стенд, любое облако, любой кластер поднимается одной командой из Git.

После этой главы вы будете уметь: читать HCL-конфигурацию и понимать, что она сделает; работать с state и не терять его; выносить state в remote backend с блокировкой; раскладывать конфигурацию по модулям; разделять окружения; затаскивать под управление уже существующие объекты через `import`. Провайдеры конкретных облаков и Proxmox разберём в уроке 62, здесь — ядро.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Язык.** Конфигурация пишется на **HCL** (HashiCorp Configuration Language) — декларативный язык: вы описываете *что должно быть*, а не *как это сделать* (разница декларативного и императивного подходов — урок 59). Файлы `*.tf` — обычный текст, живут в Git.

**Архитектура.** Бинарник `terraform`/`tofu` — это ядро. Оно умеет: читать HCL, строить **граф зависимостей** (DAG), сравнивать желаемое с состоянием и вызывать провайдеры. Само ядро ничего не знает про VMware, Yandex Cloud или GitLab — за это отвечают **провайдеры** (providers): отдельные исполняемые плагины, которые ядро запускает как дочерние процессы и общается с ними по gRPC. Провайдер — это переводчик между HCL-ресурсами и REST API конкретной системы.

```text
   ваш Git                         машина инженера / runner
┌────────────────┐            ┌──────────────────────────────────────┐
│  *.tf, *.hcl   │  git clone │  tofu (ядро)                         │
│  модули        │ ─────────► │   1. читает HCL                      │
│  .terraform.   │            │   2. строит граф зависимостей (DAG)  │
│  lock.hcl      │            │   3. refresh: опрашивает провайдеры  │
└────────────────┘            │   4. diff: план изменений            │
                              │         │ gRPC                       │
                              │   ┌─────┴──────┬───────────┐         │
                              │   ▼            ▼           ▼         │
                              │ provider     provider    provider    │
                              │ (gitlab)     (local)     (yandex)    │
                              └─────┬──────────┬───────────┬─────────┘
                                    ▼          ▼           ▼
                              GIT01:443   файлы ФС    API облака
                                    │
                              ┌─────┴──────────────────────┐
                              │  REMOTE BACKEND (state)    │
                              │  GitLab / S3 + блокировка  │
                              └────────────────────────────┘
```

**Жизненный цикл.** `init` — скачать провайдеры и модули (реестр `registry.opentofu.org` / `registry.terraform.io` по HTTPS 443), инициализировать backend. `plan` — опросить реальность (refresh), сравнить со state, показать diff. `apply` — выполнить изменения в порядке графа, обновить state. `destroy` — снести всё, что описано.

**State** (состояние) — самый важный файл. Это JSON-карта: «адрес ресурса в коде» → «реальный объект и все его атрибуты». Внутри: `version`, `terraform_version`, `serial` (счётчик ревизий), `lineage` (идентификатор цепочки состояний), `resources[]` с адресами вида `module.server_sheet["k8s-cp01"].local_file.sheet`. State нужен по трём причинам: связать код с реальными ID объектов, хранить вычисленные атрибуты (их не пересчитать чтением HCL), знать зависимости. **В state лежат секреты в открытом виде** — пароли БД, ключи, токены.

**Backend** — где лежит state. `local` (файл рядом с конфигом) годится только для одного человека на одном ноутбуке. `remote` — state во внешнем хранилище: HTTP (в т.ч. GitLab), S3/S3-совместимый (Yandex Object Storage, VK, Selectel), Terraform Cloud, Postgres. Удалённый backend даёт две вещи, ради которых он нужен: **единый источник правды для команды и CI** и **блокировку (state locking)** — пока один `apply` работает, второй получит отказ, а не перезапишет state. Для S3-совместимых бэкендов блокировка настраивается отдельным механизмом (в AWS — таблица DynamoDB); для объектных хранилищ РФ поддержку блокировки уточняйте в их документации `[ПРОВЕРИТЬ]`.

**Модули** — переиспользуемые папки с `.tf`-файлами. Root-модуль — то, что вы запускаете; child-модули подключаются блоком `module`, с `source` (локальный путь, Git, реестр) и `version`. Модуль принимает `variable`, отдаёт `output`.

**Workspaces** — именованные «копии» одного и того же конфига, у каждой свой state. Внутри кода доступно `terraform.workspace`. Удобно для быстрых одинаковых окружений, но плохо для прод/dev с разной инфраструктурой — об этом в разделе 6.

**Import** — механизм «усыновления»: объект, созданный руками, добавляется в state, и дальше Terraform считает его своим. Обратной операции «создать заново и забыть» нет — только `state rm` с последствиями.

**Terraform vs OpenTofu.** В августе 2023 HashiCorp перевела Terraform на лицензию BUSL 1.1, ограничивающую коммерческое использование. Сообщество форкнуло код под эгидой Linux Foundation — получился **OpenTofu** (лицензия MPL 2.0). Он совместим с Terraform: те же провайдеры (зеркало в `registry.opentofu.org`), тот же формат state, CLI `tofu` как замена `terraform`. Для РФ-контекста это важно: нет лицензионных рисков для коммерческого использования и нет привязки к одному вендору. Плюс OpenTofu добавил **шифрование state на стороне клиента** `[ПРОВЕРИТЬ: синтаксис блока encryption и версия]`.

## 3. Аналогия из жизни

Представьте стройку склада. Есть **проект** (ваши `.tf`-файлы) — что и где построить. Есть **исполнительная документация** — толстая папка, где записано, что реально залито, какой марки бетон, какие номера у ворот и куда подведён кабель (это **state**). Есть **прораб** (ядро Terraform): он берёт проект, открывает исполнительную документацию, идёт на площадку и сверяет — ворота №3 стоят или нет. Есть **бригады** (провайдеры): прораб сам бетон не льёт, он отдаёт команды «сварщикам» и «электрикам», каждый из которых знает только своё API. **Сейф в офисе**, где хранится папка документации и куда имеет доступ вся команда, — это **remote backend**, а **журнал «кто сейчас на площадке»** на двери — блокировка: пока один прораб внутри, второй ждёт. **Import** — это когда вы приходите на объект, где ворота уже стоят, и вписываете их в документацию, чтобы прораб не решил, что их надо снести и поставить заново.

Аналогия ломается в одном месте: прораб может «на глаз» догадаться, что забор покосился. Terraform без state не догадывается ни о чём — **нет state, значит для него этих объектов не существует**, и он спокойно создаст дубликаты.

## 4. Практика и команды

Все примеры — для UBU01 (Ubuntu 24.04), каталог `~/iac/vektor-lab`. Синтаксис `tofu` и `terraform` идентичен.

**1. Установка и версии.**

```bash
# официальный установщик OpenTofu [ПРОВЕРИТЬ актуальный URL в opentofu.org]
curl --proto '=https' --tlsv1.2 -fsSL https://get.opentofu.org/install-opentofu.sh -o install-opentofu.sh
chmod +x install-opentofu.sh && sudo ./install-opentofu.sh --install-method standalone
tofu version
tofu providers          # что реально установлено в этом каталоге
```

`tofu version` печатает версию ядра и список провайдеров из lock-файла. Смотрим: версия ≥ 1.8, все провайдеры на месте.

**2. Каркас конфигурации.**

```hcl
# main.tf
terraform {
  required_version = ">= 1.8.0"
  required_providers {
    local  = { source = "hashicorp/local",  version = "~> 2.5" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

locals {
  env   = terraform.workspace
  hosts = { "k8s-cp01" = "control-plane", "k8s-w01" = "worker" }
}

resource "random_id" "run" { byte_length = 4 }

module "server_sheet" {
  source   = "./modules/server_sheet"
  for_each = local.hosts
  hostname = each.key
  role     = each.value
  env      = local.env
}
```

`required_providers` фиксирует источник и версию (`~> 2.5` = «2.5 и выше, но не 3.0»). `for_each` по карте даёт адреса с ключами: `module.server_sheet["k8s-w01"]`.

**3. Форматирование и проверка синтаксиса.**

```bash
tofu fmt -recursive        # выравнивает HCL; -check — только проверка для CI
tofu validate              # проверяет конфиг без обращения к API
```

`validate` ловит опечатки в аргументах и незакрытые ссылки. API он не трогает — можно гонять в пайплайне до всяких доступов.

**4. `init` — инициализация.**

```bash
tofu init                  # провайдеры + модули + backend
tofu init -upgrade         # обновить провайдеры в рамках ограничений версий
```

Типичный вывод: `Installing hashicorp/local v2.5.x...`, затем `OpenTofu has been successfully initialized!`. Смотрим на ошибки версий и на созданный `.terraform.lock.hcl` — **этот файл коммитится в Git**, он фиксирует контрольные суммы провайдеров.

**5. План и его машинный разбор.**

```bash
tofu plan -out=tfplan
tofu show -json tfplan | jq -r '.resource_changes[] | "\(.address) \(.change.actions|join(","))"'
```

`-out` сохраняет план в файл, который потом применяется байт-в-байт (никаких «а вдруг поменялось между планом и применением»). Вывод `jq`: `create`, `update`, `delete`, `replace` (последнее — реальное пересоздание с простоем!). `replace` в плане прода — красная тряпка, читаем причину.

**6. Применение и точечная замена.**

```bash
tofu apply tfplan
tofu apply -replace='module.server_sheet["k8s-w01"].local_file.sheet'
```

`-replace` помечает один объект на пересоздание, не трогая остальные — штатный способ «переделать этот, не ломая всё». Без аргумента `apply` переспросит подтверждение; `-auto-approve` в проде — плохая идея (см. раздел 6).

**7. Чтение state.**

```bash
tofu state list
tofu state show 'module.server_sheet["k8s-cp01"].local_file.sheet'
tofu state pull > backup-$(date +%F).tfstate
```

`state show` печатает все атрибуты объекта, включая вычисленные (`id`). `state pull` — снять копию state перед рискованной операцией. В `terraform.tfstate` смотрим `"serial"` (растёт с каждым изменением) и `"lineage"` (должен совпадать с бэкендом).

**8. Перенос и удаление из state.**

```bash
tofu state mv 'module.server_sheet["k8s-w01"]' module.web
tofu state rm 'random_id.run'
```

`state mv` — переименовать/переложить адрес, не трогая реальный объект. `state rm` — **забыть** объект: он останется жив, но Terraform про него больше не знает (и на следующем плане, если блок остался в коде, создаст дубль).

**9. Outputs.**

```bash
tofu output
tofu output -json sheets | jq .
```

Outputs — то, что модуль/стек отдаёт наружу: IP, имена, строки подключения. В CI их удобно передавать следующему шагу пайплайна.

**10. Workspaces.**

```bash
tofu workspace list
tofu workspace new dev
tofu workspace select default
tofu workspace show          # → dev
```

У нестандартного workspace state лежит в `terraform.tfstate.d/dev/terraform.tfstate`. В коде — `terraform.workspace`; по умолчанию это `"default"`.

**11. Import.**

```bash
# 1) сначала описать ресурс в конфиге (или сгенерировать)
tofu plan -generate-config-out=generated.tf   # OpenTofu 1.6+ / TF 1.5+ [ПРОВЕРИТЬ]
# 2) затем усыновить объект по его ID во внешней системе
tofu import gitlab_project.vektor_legacy 12
tofu plan                                     # должен быть "No changes"
```

Блочный вариант (без CLI, удобно для ревью):

```hcl
import {
  to = gitlab_project.vektor_legacy
  id = "12"
}
```

После `import` **обязателен** `plan` с `No changes`. Если план хочет что-то поменять — конфиг не совпадает с реальностью.

**12. Блокировка и её снятие.**

```bash
tofu force-unlock 1734567890-abcd-1234
```

ID берётся из сообщения `Error acquiring the state lock`. Ключ и безрассудство: снимать блокировку, не убедившись, что процесс реально мёртв.

**13. Remote backend: GitLab (HTTP).**

```hcl
# backend/dev.hcl — передаётся через -backend-config, в Git не коммитим (там токен)
address        = "http://10.10.10.50/api/v4/projects/12/terraform/state/vektor-dev"
lock_address   = "http://10.10.10.50/api/v4/projects/12/terraform/state/vektor-dev/lock"
unlock_address = "http://10.10.10.50/api/v4/projects/12/terraform/state/vektor-dev/lock"
username       = "tf-state"
password       = "glpat-XXXXXXXXXXXX"
retry_wait_min = 5
```

```hcl
# в main.tf
terraform {
  backend "http" {}
}
```

```bash
export TF_HTTP_PASSWORD='glpat-XXXX'   # лучше, чем пароль в файле
tofu init -backend-config=backend/dev.hcl -migrate-state
```

`[ПРОВЕРИТЬ]` точный формат URL и параметры GitLab-бэкенда по документации GitLab. `-migrate-state` переносит текущий state в новый backend; `-reconfigure` — просто перечитать настройки без переноса.

**14. Remote backend: S3-совместимый (облака РФ, вариант урока 62).**

```hcl
terraform {
  backend "s3" {
    bucket                      = "vektor-tfstate"
    key                         = "dev/vektor.tfstate"
    region                      = "ru-central1"
    endpoints                   = { s3 = "https://storage.yandexcloud.net" }
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
  }
}
```

`[ПРОВЕРИТЬ]` endpoint, регион и поддержку блокировки у выбранного провайдера. Ключи доступа — только через переменные окружения или OIDC в CI, никогда в HCL.

**15. Снос.**

```bash
tofu plan -destroy
tofu destroy
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04): доступ по SSH под своей учётной записью, `sudo`, установлен `jq` (`sudo apt install -y jq`), есть Git и curl.
- **GIT01** (10.10.10.50, GitLab CE): веб-интерфейс открывается, у вас есть учётка администратора или Maintainer в своей группе. GIT01 появляется на DevOps-треке, к этому уроку он уже должен быть поднят (ставили в модулях CI/CD).
- Кластер k8s (`k8s-cp01`/`k8s-w01`) сегодня **не нужен**: мы не создаём реальные ВМ, а тренируем ядро Terraform — провайдер, state, backend, модули, workspaces, import. Реальный провижининг в облака и Proxmox — урок 62. Объектом управления берём «паспорта серверов» (текстовые файлы через провайдер `local`) и проект в GitLab (провайдер `gitlab`).
- Если GIT01 ещё не готов — шаги 8–9 пропустите, сделайте остальное на `local` backend и вернитесь к ним позже.

### Снапшот

В VMware Workstation на **UBU01** и **GIT01**: правый клик по ВМ → *Snapshot* → *Take Snapshot* → имя `before-devops-061`. (При выключенной ВМ доступно *Snapshot Manager* → *Take Snapshot*.) Одна и та же метка на обеих машинах — так вы потом поймёте, что откат согласован.

### Шаги

1. **UBU01.** Поставьте OpenTofu (команда из раздела 4, п.1). Ожидаемо: `tofu version` печатает `OpenTofu v1.8.x` (или новее) и `on linux_amd64`.
2. **UBU01.** `mkdir -p ~/iac/vektor-lab/modules/server_sheet ~/iac/vektor-lab/backend ~/iac/vektor-lab/out && cd ~/iac/vektor-lab`, создайте `main.tf` из раздела 4, п.2, плюс `outputs.tf`:

```hcl
output "sheets" {
  value = { for k, m in module.server_sheet : k => m.path }
}
```

3. **UBU01.** Создайте модуль. Ожидаемо: `tofu init` скачивает `hashicorp/local` и `hashicorp/random`, появляется `.terraform.lock.hcl`.

```hcl
# modules/server_sheet/main.tf
variable "hostname" { type = string }
variable "role"     { type = string }
variable "env"      { type = string }

resource "local_file" "sheet" {
  filename = "${path.root}/out/${var.env}/${var.hostname}.txt"
  content  = <<-EOT
    hostname: ${var.hostname}
    role: ${var.role}
    env: ${var.env}
  EOT
}

output "path" { value = local_file.sheet.filename }
```

4. **UBU01.** `tofu fmt -recursive && tofu validate && tofu plan -out=tfplan`. Ожидаемо: `Plan: 3 to add, 0 to change, 0 to destroy.` (два файла-паспорта + `random_id`).
5. **UBU01.** `tofu apply tfplan`, затем `tofu state list` и `tofu output`. Ожидаемо: три адреса в state, в `out/default/` лежат два txt-файла.
6. **UBU01.** Повторите `tofu plan`. Ожидаемо: **`No changes. Your infrastructure matches the configuration.`** Это и есть идемпотентность (урок 59).
7. **UBU01. Дрейф.** Удалите файл руками: `rm out/default/k8s-w01.txt`, затем `tofu plan`. Ожидаемо: план хочет создать файл заново. Верните `tofu apply`.
8. **UBU01. Workspaces.** `tofu workspace new dev`, `tofu plan` — в путях теперь `out/dev/`. `tofu apply`, `tofu workspace new prod`, `tofu apply`. Ожидаемо: `terraform.tfstate.d/dev/terraform.tfstate` и `.../prod/...`. Затем `tofu workspace select default`.
9. **GIT01. Внешний state.** Создайте проект `iac-vektor` (New project → Create blank project, видимость Private). Запомните его **ID** (на странице проекта рядом с названием). Создайте **Project Access Token**: *Settings → Access Tokens*, роль Maintainer, scope `api`, срок 30 дней. Скопируйте токен — он показывается один раз.
10. **UBU01.** Создайте `backend/dev.hcl` (раздел 4, п.13) со своим project ID и токеном, добавьте в `main.tf` блок `backend "http" {}`, выполните:

```bash
export TF_HTTP_PASSWORD='glpat-...'
tofu init -backend-config=backend/dev.hcl -migrate-state
tofu state list && tofu plan
```

Ожидаемо: OpenTofu спрашивает подтверждение копирования state, затем `state list` работает, `plan` — `No changes`. Со стороны GitLab: *Operate → Terraform States* — там появился `vektor-dev`.
11. **UBU01. Блокировка.** Создайте `slow.tf` с `resource "time_sleep" "wait" { create_duration = "300s" }` (провайдер `hashicorp/time`, добавьте в `required_providers`), запустите `tofu apply` и на 10-й секунде убейте процесс: `Ctrl+Z`, затем `kill -9 %1`. Ожидаемо: следующий `tofu plan` падает с `Error acquiring the state lock`.
12. **UBU01. Снятие блокировки.** `ps -ef | grep tofu` — процесса нет. `tofu state pull > backup.tfstate`, затем `tofu force-unlock <ID из сообщения>`, удалите `slow.tf`, `tofu plan`. Ожидаемо: блокировка снята, план чистый.
13. **GIT01. Объект «из прошлого».** Создайте руками второй проект `iac-legacy` (в нём ничего делать не будем). **UBU01:** добавьте в `main.tf` провайдер `gitlabhq/gitlab` `[ПРОВЕРИТЬ актуальную мажорную версию]`, блок `provider "gitlab" { base_url = "http://10.10.10.50/api/v4/" token = var.gitlab_token }` и ресурс:

```hcl
resource "gitlab_project" "vektor_legacy" {
  name         = "iac-legacy"
  path         = "iac-legacy"
  description  = "Managed by OpenTofu"
}
```

Затем `tofu import gitlab_project.vektor_legacy <ID проекта iac-legacy>` и `tofu plan`. Ожидаемо: `No changes` (или только `description` — тогда примените). Fallback, если GitLab-провайдер не заводится: импортируйте `null_resource` — смысл команды тот же.

### Проверка

- [ ] `tofu version` работает, `.terraform.lock.hcl` существует и закоммичен.
- [ ] `tofu plan` после `apply` даёт `No changes`.
- [ ] Удалённый вручную файл возвращается `apply` (дрейф обнаружен).
- [ ] `tofu workspace list` показывает `default`, `dev`, `prod`; state'ы `dev`/`prod` лежат в `terraform.tfstate.d/`.
- [ ] В GitLab в *Operate → Terraform States* виден `vektor-dev`, локальный state больше не используется.
- [ ] Блокировка воспроизведена и снята через `force-unlock` после проверки `ps`.
- [ ] Проект `iac-legacy` импортирован, `plan` по нему чистый.
- [ ] В `stand-log.md` записаны: проект `iac-vektor`, токен (без самого значения!), каталог `~/iac/vektor-lab`.

### Сломай и почини

> **Пользователь:** Сидоров Алексей (склад) — через дежурного DevOps.
> **ВМ:** GIT01 (пайплайн) + UBU01.
> **Что написал:** «Не выходит выкатить обновление: пайплайн падает на шаге `tofu plan` с красным текстом про lock. Уже час так. Отгрузки стоят».
> **Срочность:** Высокая.

**Как воспроизвести.** На UBU01 повторите шаг 11: запустите `tofu apply` со `time_sleep` и убейте процесс `kill -9`. В GitLab останется запись блокировки.

**Диагностика по порядку:**

1. Читаем ошибку целиком, а не первую строку. Там есть `Lock Info:` с полями `ID`, `Who`, `Created`, `Operation`. Записываем ID.
2. Проверяем, жив ли владелец блокировки: на UBU01 `ps -ef | grep -E 'tofu|terraform'`; в GitLab — *Build → Pipelines*, смотрим, нет ли реально идущего job'а (`running`).
3. Если job живой — **ничего не снимаем**. Ждём или останавливаем job штатно (Cancel), затем смотрим, не остался ли state в промежуточном виде.
4. Если процесса нет — берём `tofu state pull > backup.tfstate` (страховка), затем `tofu force-unlock <ID>`.
5. `tofu plan` → должен быть чистый. Если план показывает «создать» объекты, которые реально есть, — state разошёлся с реальностью, разбираемся через `tofu state list` и `import`.
6. Проверяем сам пайплайн: перезапускаем job, убеждаемся, что он зелёный.

**Решение.** Снимаем устаревшую блокировку, удаляем `slow.tf` из конфигурации, применяем `tofu apply`.

**Ответ пользователю:** «Алексей, проблема была не в выгрузке, а в зависшей блокировке на стороне инфраструктурного пайплайна — прошлый запуск был прерван и не отпустил state. Блокировку сняли, пайплайн перезапущен и зелёный. Проверьте, пожалуйста, выгрузку накладных на своей стороне».

**Закрытие заявки:** категория «CI/CD», в комментарии — ID блокировки, причина (прерванный job), действие (force-unlock после проверки), профилактика (таймаут job'а в CI, отдельный шаг `tofu plan` с коротким `retry_wait_min`).

### Что изменилось на стенде

- **UBU01:** установлен OpenTofu; каталог `~/iac/vektor-lab` с `main.tf`, `variables.tf`, `outputs.tf`, модулем `modules/server_sheet`, `backend/dev.hcl`; файлы `out/default|dev|prod/*.txt`; `terraform.tfstate.d/` с локальными state `dev` и `prod`.
- **GIT01:** проекты `iac-vektor` (ID записать в журнал) и `iac-legacy`; Project Access Token `tf-state` (scope `api`, записать срок и не хранить значение в Git); state-файл `vektor-dev` в *Operate → Terraform States*.
- **Связи:** UBU01 ходит на `http://10.10.10.50/api/v4/` (443/80). Оба проекта — приватные.
- Дальше урок 62 опирается на каталог `~/iac/vektor-lab` и на работоспособный GitLab-бэкенд.

### Откат

VMware Workstation → правый клик по ВМ → *Snapshot Manager* → выбрать `before-devops-061` → *Go to*. Порядок важен: сначала откатывайте **GIT01**, потом **UBU01** — иначе получится конфиг с бэкендом, которого больше нет. Если откатили только GIT01: на UBU01 верните в `main.tf` пустой `terraform {}` без блока `backend` и выполните `tofu init -reconfigure` (state придётся восстановить из `backup.tfstate` или пересоздать `tofu apply`). Перед откатом снимите `tofu state pull > backup-$(date +%F).tfstate`, если в state есть что-то ценное.

## 6. Ошибки новичков и безопасность

1. **Коммит `terraform.tfstate` в Git** → секреты в истории репозитория, конфликты при каждом `pull`. Правильно: `.gitignore` с `*.tfstate*`, `*.tfplan`, `.terraform/`, `*.tfvars` — и **не** игнорировать `.terraform.lock.hcl`.
2. **`tofu apply -auto-approve` в прод-пайплайне без ревью плана** → незамеченное `replace` сносит живую ВМ. Правильно: `plan -out` → approval вручную или политика (урок 65) → `apply tfplan`.
3. **Правка state руками** → рассинхрон, дубликаты, «фантомные» ресурсы, за которые платят деньги. Правильно: `state mv/rm` только осознанно, всегда после `state pull` с бэкапом.
4. **Один state на dev и prod** → ошибка в dev-конфиге меняет прод. Правильно: отдельный state на окружение (отдельный backend-конфиг или отдельная папка), workspaces — только для однотипных эфемерных сред.
5. **Незапиненные версии провайдеров и `tofu init -upgrade` в CI** → утром пайплайн красный, потому что вышла новая мажорная версия. Правильно: `~>` в `required_providers` + коммит lock-файла.
6. **Секреты в `variables.tf` и `terraform.tfvars` в Git** → токены утекают. Правильно: `TF_VAR_*` из окружения, OIDC в CI, Vault/SOPS (урок 66).
7. **`force-unlock` «на всякий случай»** → два параллельных `apply` пишут в один state, последний затирает первого. Правильно: сначала доказать, что процесс мёртв.
8. **Игнорирование дрейфа** → «в коде одно, в жизни другое», и первый же `apply` после релиза сносит чужую ручную правку. Правильно: регулярный `plan` по расписанию + запрет ручных изменений управляемых объектов.

**Безопасность.** В боевой среде: state содержит пароли и ключи в открытом виде — backend только по TLS, с шифрованием на диске, узким списком доступа и аудитом; никакого `http://` и публичных бакетов. Права на `apply` в прод — у ограниченного круга (или только у CI-сервисной учётки), у разработчиков — `plan` и `read`. Сервисные токены — короткоживущие, с минимальными scope; никогда не в `backend/*.hcl` в Git. Если ради лабы вы, как здесь, кладёте токен в файл или используете HTTP вместо HTTPS — это только для стенда `10.10.20.0/24`/`10.10.10.0/24`, в рабочей среде так нельзя.

## 7. Вопросы на собеседовании

**1. Зачем Terraform нужен state и почему нельзя просто «читать реальность»?**

**Ответ:** state — это карта соответствия адресов в коде реальным объектам с их ID и вычисленными атрибутами. Без него Terraform не знает, что объект в облаке создан именно им: он попытается создать дубликат, а существующий останется неуправляемым. Кроме того, state хранит зависимости и результаты, которые нельзя вычислить из API, и позволяет не опрашивать все ресурсы при каждом запуске. Поэтому state — версионируемый артефакт, который хранится в remote backend, а не в Git, потому что там секреты и потому что Git не даёт блокировки.

*Что хочет услышать интервьюер:* понимание «state = mapping + metadata», а не «файл, который нельзя терять».

*Красный флаг:* «state можно удалить и восстановить через `refresh`» — нет, refresh не восстанавливает сопоставление и не вернёт вычисленные значения.

**2. Как работает блокировка state и что вы сделаете при `Error acquiring the state lock`?**

**Ответ:** backend при операции берёт блокировку с уникальным ID; второй процесс получает отказ с этим ID. Порядок: прочитать `Lock Info`, проверить `ps -ef`/статус CI-job'а, убедиться, что владелец мёртв, снять копию `state pull`, выполнить `force-unlock <ID>`, затем `plan` и проверка консистентности. Если job живой — ждём или штатно отменяем, снимать блокировку нельзя. Профилактика: таймауты job'ов, `retry_wait_min`, `-lock-timeout`.

*Что хочет услышать интервьюер:* что кандидат сначала проверяет, а потом снимает, и что он делает бэкап.

*Красный флаг:* «сразу `force-unlock`» или «удалю state и сделаю заново».

**3. Как вы организуете dev/stage/prod: workspaces, отдельные каталоги или отдельные репозитории?**

**Ответ:** окружения с разной инфраструктурой — отдельные state и отдельные backend-конфиги, обычно отдельные каталоги (`envs/dev`, `envs/prod`), ссылающиеся на общие модули. Workspaces годятся, когда конфиг идентичен и различается только именем (эфемерные фича-стенды, нагрузочные копии): у них один код, разделение только по `terraform.workspace`. Для прода workspaces опасны — легко применить не в том workspace, нет явного ревью конфигурации окружения, и всё окружение описано неявно через `if`.

*Что хочет услышать интервьюер:* что кандидат понимает компромисс и умеет обосновать выбор, а не «всегда workspaces».

*Красный флаг:* «workspaces = окружения, всегда так делаю».

**4. Как затащить под Terraform существующую инфраструктуру?**

**Ответ:** `import`: объект описывается в HCL (или генерируется через `import`-блок с `-generate-config-out`), затем `terraform import <адрес> <ID>`. Критично: после импорта `plan` должен показать `No changes` — если он хочет что-то изменить, конфиг не соответствует реальности и `apply` что-нибудь сломает. Массовый вариант — `import`-блок с `for_each`. Импорт делается в отдельной ветке, с бэкапом state и начиная с некритичных объектов.

*Что хочет услышать интервьюер:* обязательный шаг «проверить план после импорта» и осторожность.

*Красный флаг:* «импортировал и сразу `apply`» или «импорт сам создаст конфиг, смотреть не надо».

**5. Чем OpenTofu отличается от Terraform и почему компания может его выбрать?**

**Ответ:** OpenTofu — форк Terraform, созданный после перехода HashiCorp на лицензию BUSL 1.1, развивается под Linux Foundation под MPL 2.0. Совместим по HCL, провайдерам и формату state, CLI `tofu` — прямая замена. Отличия: свободная лицензия без ограничений на коммерческое использование, собственный реестр `registry.opentofu.org`, шифрование state на стороне клиента `[ПРОВЕРИТЬ версию]`, часть команд с улучшениями. Выбирают из-за лицензионных рисков, требования импортозамещения/отсутствия вендор-лока и из-за шифрования state. Минус — часть новых функций HashiCorp приходит с задержкой, и корпоративные обёртки (Terraform Cloud, Sentinel) с ним не работают.

*Что хочет услышать интервьюер:* знание причины форка и того, что миграция — смена бинарника, а не переписывание кода.

*Красный флаг:* «OpenTofu — это другой инструмент, конфиги несовместимы» или путаница в лицензиях.

## 8. Источники (официальная документация)

- **OpenTofu** — `opentofu.org`: разделы *Docs → Language*, *Docs → CLI Commands* (`init`, `state`, `workspaces`, `import`), *Registry*. Поиск: `opentofu state encryption`, `opentofu install linux`.
- **HashiCorp Terraform** — `developer.hashicorp.com` (раздел Terraform): *Language → State*, *Backend configuration*, *Modules*, *Import*. Поиск: `terraform backend http`, `terraform state lock`, `terraform import block generate config`.
- **Terraform Registry** — `registry.terraform.io`: провайдеры `hashicorp/local`, `hashicorp/random`, `gitlabhq/gitlab`, `hashicorp/time`. Поиск: `terraform registry gitlab provider`.
- **GitLab Docs** — `docs.gitlab.com`: *Terraform integration / Terraform state* (HTTP backend, project access tokens, Terraform States UI). Поиск: `gitlab terraform http backend state lock`.
- **Linux Foundation** — `linuxfoundation.org`: страница проекта OpenTofu (лицензия, управление). Поиск: `OpenTofu Linux Foundation announcement`.
- **Yandex Cloud** — `yandex.cloud/ru/docs`: *Object Storage → Terraform backend*, руководства по Terraform-провайдеру. Поиск: `yandex cloud terraform s3 backend state`. `[ПРОВЕРИТЬ поддержку state locking]`
- **Astra Linux / РЕД ОС** — `astralinux.ru`, `red-soft.ru`: документация по пакетным репозиториям, если ставите OpenTofu на отечественную ОС `[ПРОВЕРИТЬ наличие пакета в их репозиториях]`.
