# Тестирование и политика-как-код: terratest, tflint, checkov, OPA/Conftest, Sentinel-подход

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в Service Desk.**
Пользователь: дежурный администратор (учётка `adm.student`, вторая смена).
ВМ: GIT01 / инфраструктура «Вектор».
Что написал: «Ночью кто-то смержил MR в `main`, пайплайн прошёл зелёным, `terraform apply` отработал. Утром смотрим — на тестовом контуре ресурсы с тегом `env=prod`, а у одного сервиса в конфиге торчит `public = true`. Как это вообще проскочило?»
Срочность: высокая.

Вот это «как проскочило» — и есть тема главы. Terraform (или OpenTofu) сам по себе не знает, что вам можно, а что нельзя: он послушно создаст и прод-тег на стейдже, и публичный бакет, и security group с `0.0.0.0/0` на 22-й порт. `terraform validate` проверяет только синтаксис и типы, `plan` — только то, что конфигурация сходится с провайдером. Дальше начинается зона ответственности инженера: линтеры, сканеры безопасности, тесты и политики.

После этой главы вы будете уметь: гонять `tflint` и `checkov` в пайплайне, писать собственные политики на Rego и проверять ими `plan.json`, писать тесты на Go через terratest и декларативные тесты через `terraform test`, понимать, что такое Sentinel и чем его заменить без платной подписки. И главное — выстроить это так, чтобы пайплайн ловил проблему до `apply`, а не после.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Тестирование IaC — это конвейер из слоёв, от дешёвых и быстрых к дорогим и медленным. Классическая пирамида выглядит так:

```text
                    ▲ дорого, медленно
                    │
        terratest (Go, реальный apply + destroy)
        terraform test (.tftest.hcl, plan/apply в песочнице)
        policy-as-code (OPA/Conftest, Sentinel) по plan.json
        security scan (checkov, trivy config) по HCL + plan.json
        lint (tflint) — правила провайдера, устаревшие аргументы
        fmt + validate — синтаксис, типы, ссылки
                    │
                    ▼ дёшево, быстро (< 5 секунд)
```

**Ключевая идея policy-as-code.** Есть «движок принятия решений» и «документ на входе». Движок — OPA (Open Policy Agent), отдельный бинарник на Go. Политика — файл на языке Rego. Вход — JSON: план Terraform, манифест Kubernetes, Dockerfile, что угодно, что можно сериализовать. На выходе — набор нарушений (или булево «allow/deny»). Движок ничего не знает про Terraform, поэтому одна и та же логика переиспользуется между Terraform, k8s и CI.

**Как получить этот JSON.** `terraform plan -out=tfplan` создаёт бинарный план. `terraform show -json tfplan` превращает его в документ, где есть `resource_changes` (что создаётся/меняется/удаляется и с какими итоговыми значениями), `planned_values`, `variables`, `configuration`. Именно `resource_changes[].change.after` — это то, что реально уедет в облако, со всеми вычисленными `count`, `for_each`, `locals` и значениями переменных. Сканирование одного лишь HCL этого не видит.

**Где тут tflint и checkov.** `tflint` — статический линтер с плагинами под провайдеры: ловит невалидные типы инстансов, устаревшие аргументы, отсутствующие версии модулей. `checkov` (Palo Alto / Prisma Cloud, открытая версия) — сканер безопасности: публичные бакеты, незашифрованные диски, открытые порты. Оба работают по коду, без обращения к облаку.

**Сеть и порты.** Почти всё из этого списка — локальные CLI, сеть им не нужна. Исключения: `tflint --init` тянет плагины с GitHub, `go get` для terratest тянет модули, а OPA в режиме сервера слушает REST на `localhost:8181` (`POST /v1/data/<путь>`). Conftest умеет забирать политики из OCI-реестра — тут пригодится HARBOR01 из вашего стенда.

**Sentinel** — это подход HashiCorp: свой язык политик, встроенный прямо в HCP Terraform / Terraform Enterprise. Политика прикрепляется к workspace через Policy Set и имеет уровень принуждения: `advisory` (только предупреждение в логе), `soft-mandatory` (блокирует, но админ может переопределить с обоснованием), `hard-mandatory` (блокирует всегда, обойти нельзя). Плюс — обойти нельзя, минус — работает только внутри платформы HashiCorp.

```text
   git push
      │
      ▼
 ┌─────────┐   ┌────────┐   ┌──────────┐   ┌──────────────┐   ┌──────────┐
 │fmt/valid│→ │ tflint │→ │ checkov  │→ │ OPA/Conftest │→ │ terraform│
 │         │   │        │   │ (HCL)    │   │ (plan.json)  │   │  test /  │
 └─────────┘   └────────┘   └──────────┘   └──────────────┘   │terratest │
                                                              └────┬─────┘
                                                                   ▼
                                                            terraform apply
```

## 3. Аналогия из жизни

Досмотр в аэропорту. `terraform fmt` — это проверка, что билет не помят. `terraform validate` — что рейс вообще существует. `tflint` — рамка металлодетектора: быстро, дёшево, ловит грубое. `checkov` — список запрещённых предметов, который сверяют вручную: жидкости, ножи, литиевые батареи. `OPA/Conftest` — правила конкретной авиакомпании: «в бизнес-класс с самокатом нельзя», они гибкие и меняются каждую неделю. `Sentinel` — это служба безопасности самого аэропорта, которая стоит до выхода на посадку и которую обойти нельзя даже начальнику смены, если политика `hard-mandatory`. `terratest` — контрольный полёт: дорого, долго, зато проверяет, что самолёт реально летит, а не только что документы в порядке.

Аналогия ломается в одном месте: в аэропорту досматривают уже собранный багаж, а политика над IaC может отменить сам рейс — то есть решение принимается до того, как инфраструктура создана.

## 4. Практика и команды

Все примеры — для структуры репозитория `infra-lab/`, которую мы соберём в лабораторной: `modules/lab-app/`, `policy/`, `env/stage.tfvars`.

**1. Формат и валидация — первый фильтр.**

```bash
terraform fmt -check -recursive -diff
terraform -chdir=modules/lab-app init -backend=false -input=false
terraform -chdir=modules/lab-app validate
```

`-check` — не переписывать файлы, а вернуть код 1, если форматирование неверное (в CI нужно именно это). `-recursive` — пройти по подкаталогам. `-diff` — показать, что именно поправится. `-backend=false` при `init` — не тянуть state из S3/облака, для проверки синтаксиса он не нужен. `-input=false` — запретить интерактивные вопросы, иначе CI повиснет.

**2. План в JSON — топливо для политик.**

```bash
cd modules/lab-app
terraform plan -out=tfplan -input=false -var-file=../../env/stage.tfvars
terraform show -json tfplan > plan.json
jq '.resource_changes[] | {address, actions: .change.actions}' plan.json
```

Вывод (сокращённо):

```json
{"address":"terraform_data.config","actions":["create"]}
```

Смотрим на `actions`: `["create"]`, `["update"]`, `["delete"]`, `["delete","create"]` (replace), `["no-op"]`. Все политики «не удалять в prod» строятся на этом поле.

**3. tflint — линтер с плагинами.**

```hcl
# .tflint.hcl в корне репозитория
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
```

```bash
tflint --init
tflint --recursive --format=compact
```

`--init` скачивает плагины (нужен интернет; в закрытом контуре — свой mirror `[ПРОВЕРИТЬ]`). `--recursive` рекурсивно обходит каталоги. `--format=compact` даёт вывод вида `file.tf:12:5: Warning - ...`, удобный для парсинга. Типичный вывод:

```text
main.tf:14:1: Warning - terraform_data.config: variable "env" is declared but never used (terraform_unused_declarations)
```

Что смотреть: `Warning` сборку обычно не роняет — если хотите ронять, добавьте `--minimum-failure-severity=warning`.

**4. `terraform test` — родные тесты без Go.**

```hcl
# modules/lab-app/tests/basic.tftest.hcl
run "config_rendered_for_stage" {
  command = plan

  variables {
    app_name = "vektor-api"
    env      = "stage"
    owner    = "team-platform"
  }

  assert {
    condition     = output.config.env == "stage"
    error_message = "env не дошёл до конфигурации"
  }
}
```

```bash
terraform -chdir=modules/lab-app test -verbose
```

`command = plan` — прогон без создания ресурсов; `command = apply` — с реальным созданием и автоматическим destroy после теста. `-verbose` печатает каждый run. Тестовый файл называется `*.tftest.hcl` и лежит в `tests/` (Terraform 1.7+; в 1.6 — рядом с `main.tf`) `[ПРОВЕРИТЬ версию]`. В OpenTofu аналог — `tofu test`, синтаксис очень близкий `[ПРОВЕРИТЬ]`.

**5. checkov — сканер безопасности.**

```bash
checkov -d . --framework terraform --compact --output-file-path checkov.json
checkov -d . --framework terraform --check CKV_AWS_20 --skip-check CKV_AWS_144
```

`-d .` — каталог, `--framework terraform` — только Terraform (есть ещё `kubernetes`, `dockerfile`, `cloudformation`, `helm`). `--compact` — по одной строке на находку. `--check`/`--skip-check` — включить/исключить конкретные ID. Код возврата `1` = есть провалы (по умолчанию).

```text
terraform scan results:
Passed checks: 41, Failed checks: 3, Skipped checks: 0
Check: CKV_AWS_20: "S3 Bucket has an ACL defined which allows public READ access"
	FAILED for resource: aws_s3_bucket.logs
```

**6. checkov: подавление с обоснованием и baseline.**

```hcl
# исключение прямо в коде — только с причиной и владельцем
resource "aws_s3_bucket" "logs" {
  #checkov:skip=CKV_AWS_20:логи публичны по требованию аудита, владелец team-platform, тикет SEC-113
  bucket = "vektor-logs"
}
```

```bash
checkov -d . --create-baseline   # зафиксировать текущие находки
checkov -d . --baseline .checkov.baseline
```

Baseline — способ включить сканер в проекте, где уже 300 находок, не останавливая работу: старое игнорируется, новое блокируется. Опасность — baseline быстро превращается в свалку, чистить его нужно по расписанию.

**7. OPA: политика на Rego.**

```rego
# policy/terraform.rego
package main

import rego.v1

# правило-нарушение: у ресурса не указан owner
deny contains msg if {
    some rc in input.resource_changes
    owner := object.get(object.get(rc.change.after, "input", {}), "owner", "")
    owner == ""
    msg := sprintf("%s: не указан owner", [rc.address])
}

# удаление чего-либо в prod запрещено
deny contains msg if {
    input.variables.env.value == "prod"
    some rc in input.resource_changes
    "delete" in rc.change.actions
    msg := sprintf("%s: удаление в prod запрещено", [rc.address])
}
```

Ключевое: `import rego.v1` и синтаксис с `if` — это OPA 1.x. На старых сборках (< 0.59) `if` и `contains` не работают, там `deny[msg] { ... }` `[ПРОВЕРИТЬ версию у себя: opa version]`. `object.get(..., "input", {})` защищает от падения, когда `after` содержит `null` (удаление).

**8. OPA: запуск политики вручную.**

```bash
opa eval -d policy -i plan.json 'data.main.deny' --format pretty
```

Вывод:

```json
{
  "result": [
    {
      "expressions": [
        { "value": ["terraform_data.config: не указан owner"], "text": "data.main.deny" }
      ]
    }
  ]
}
```

Пустой массив `[]` в `value` — политика довольна. `-d` — каталог с политиками, `-i` — входной JSON.

**9. Conftest — обёртка над OPA для конфигов.**

```bash
conftest test plan.json --policy policy --output table
conftest verify --policy policy        # юнит-тесты самих политик (*_test.rego)
conftest parse plan.json               # проверить, что входной файл вообще парсится
```

Вывод:

```text
FAIL - plan.json - main - terraform_data.config: не указан owner

2 tests, 1 passed, 0 warnings, 1 failure, 0 exceptions
```

Важно: Conftest считает провалом правила, чьё имя начинается с `deny` или `violation`, а `warn*` — предупреждением (не роняет, если не указан `--fail-on-warn`). Переименовали правило — и проверка молча перестала работать. Это ровно та поломка, которую мы разберём в лабораторной.

**10. Sentinel: язык и уровни принуждения.**

```sentinel
# restrict_types.sentinel
import "tfplan/v2" as tfplan

allowed_types = ["terraform_data", "null_resource", "local_file"]

no_forbidden_types = rule {
  all tfplan.resource_changes as _, rc {
    rc.type in allowed_types
  }
}

main = rule { no_forbidden_types }
```

```bash
sentinel test -verbose      # прогон политики против mock-файлов pass.hcl / fail.hcl
sentinel apply policy.sentinel
```

`tfplan/v2` — официальный импорт, дающий доступ к плану; есть также `tfconfig/v2` (конфигурация), `tfstate/v2` (state), `tfrun`. Mock-файлы кладутся в `test/<имя_политики>/pass.hcl` и `fail.hcl`. Локальный Sentinel CLI скачивается свободно, но прикрутить политику к пайплайну можно только в HCP Terraform (платный план) или Terraform Enterprise — на бесплатном тарифе Sentinel-политик нет `[ПРОВЕРИТЬ актуальные тарифы]`. Легальная замена без подписки — OPA/Conftest, ровно с той же логикой.

**11. terratest — тест на Go с реальным apply.**

```go
// test/lab_app_test.go
package test

import (
    "testing"
    "github.com/gruntwork-io/terratest/modules/terraform"
    "github.com/stretchr/testify/assert"
)

func TestLabAppConfig(t *testing.T) {
    t.Parallel()

    opts := &terraform.Options{
        TerraformDir: "../modules/lab-app",
        Vars: map[string]interface{}{
            "app_name": "vektor-api",
            "env":      "stage",
            "owner":    "team-platform",
        },
    }

    defer terraform.Destroy(t, opts) // ОБЯЗАТЕЛЬНО, иначе ресурсы утекут
    terraform.InitAndApply(t, opts)

    out := terraform.Output(t, opts, "config_json")
    assert.Contains(t, out, "team-platform")
}
```

```bash
cd test
go mod init vektor/infra-lab-test
go get github.com/gruntwork-io/terratest/modules/terraform
go test -v -timeout 30m ./...
```

`terraform.Options` описывает каталог и переменные, `InitAndApply` делает `init` + `apply`, `Output` вытаскивает output, `defer Destroy` убирает за собой даже при падении теста. `-timeout 30m` обязателен: Go по умолчанию убивает тест через 10 минут, а облачный apply легко дольше. Для проверки идемпотентности есть `terraform.InitAndApplyAndIdempotent` `[ПРОВЕРИТЬ точное имя в godoc]`. Если `proxy.golang.org` недоступен из РФ, настройте `GOPROXY` на корпоративное зеркало `[ПРОВЕРИТЬ]`.

**12. trivy как «всё в одном» (на случай, если checkov тяжеловат).**

```bash
trivy config . --severity HIGH,CRITICAL
```

`trivy config` сканирует IaC на мисконфигурации. Отдельный проект `tfsec` с 2023 года влит в Trivy и как самостоятельный инструмент больше не развивается — не начинайте с него новые проекты.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — рабочая машина инженера. Нужны: Terraform (или OpenTofu) версии ≥ 1.6 из урока 61, `git`, `jq`, Python 3, Go.
- **GIT01** (10.10.10.50, GitLab CE + runner из урока 58) — репозиторий и пайплайн. Раннер с тегом `ubu01` (shell-исполнитель на UBU01 или docker-исполнитель).
- Windows-часть стенда не нужна.
- Облачных кредов не требуется: учебный модуль использует встроенный ресурс `terraform_data`, которому провайдер не нужен. На реальном проекте вы подставляете конфигурацию из уроков 61–62 — команды те же, поменяются только типы ресурсов внутри политик.

### Снапшот

В VMware Workstation на **UBU01** и **GIT01**: правый клик по ВМ → Snapshot → Take Snapshot, имя `before-devops-065`. Если ВМ запущена — предварительно лучше выключить или использовать «Snapshot the virtual machine's memory» осознанно: снапшот с памятью весит много.

### Шаги

1. **UBU01.** Ставим инструменты.

   ```bash
   python3 -m venv ~/venvs/policy && source ~/venvs/policy/bin/activate
   pip install --upgrade pip checkov
   deactivate
   ```

   Ожидаемо: `checkov --version` печатает версию. Venv обязателен — Ubuntu 24.04 блокирует `pip install` в системный Python (PEP 668).

2. **UBU01.** Ставим tflint, conftest, opa из релизов GitHub (`github.com/terraform-linters/tflint/releases`, `github.com/open-policy-agent/conftest/releases`, `openpolicyagent.org/downloads`). Кладём бинарники в `~/.local/bin`, проверяем `tflint --version`, `conftest --version`, `opa version`. Ожидаемо: три версии без ошибок. Если GitHub недоступен — тяните пакеты через корпоративное зеркало `[ПРОВЕРИТЬ]`.

3. **GIT01.** В GitLab создаём проект `vektor/infra-lab`, клонируем на UBU01, создаём структуру: `modules/lab-app/`, `policy/`, `env/`, `test/`, `.gitlab-ci.yml`.

4. **UBU01.** Пишем модуль.

   ```hcl
   # modules/lab-app/main.tf
   variable "app_name"       { type = string }
   variable "env"            { type = string }
   variable "owner"          { type = string }
   variable "expose_public"  { type = bool, default = false }

   locals {
     config = {
       app    = var.app_name
       env    = var.env
       owner  = var.owner
       public = var.expose_public
     }
   }

   resource "terraform_data" "config" {
     input = local.config
   }

   output "config"      { value = local.config }
   output "config_json" { value = jsonencode(local.config) }
   ```

   Ожидаемо: `terraform -chdir=modules/lab-app init -backend=false` завершается «Terraform has been successfully initialized».

5. **UBU01.** `env/stage.tfvars`:

   ```hcl
   app_name = "vektor-api"
   env      = "stage"
   owner    = "team-platform"
   ```

   Запускаем `terraform plan -out=tfplan -var-file=../../env/stage.tfvars` и `terraform show -json tfplan > plan.json`. Ожидаемо: `plan.json` содержит `"resource_changes"` с `terraform_data.config` и `"actions": ["create"]`.

6. **UBU01.** Линтер: создаём `.tflint.hcl`, выполняем `tflint --init`, затем `tflint --recursive --format=compact`. Ожидаемо: либо пусто, либо предупреждения с путём и номером строки.

7. **UBU01.** Родной тест: `modules/lab-app/tests/basic.tftest.hcl` из раздела 4, запуск `terraform test -verbose`. Ожидаемо: `Success! 1 passed, 0 failed`.

8. **UBU01.** Сканер: `checkov -d . --framework terraform --compact`. Ожидаемо: секция `terraform scan results` со счётчиками `Passed/Failed`. Честно примите: на `terraform_data` встроенных проверок почти нет, поэтому часть находок вы увидите только после того, как подставите реальную облачную конфигурацию из урока 62.

9. **UBU01.** Политика: создаём `policy/terraform.rego` из раздела 4, гоняем

   ```bash
   opa eval -d policy -i modules/lab-app/plan.json 'data.main.deny' --format pretty
   conftest test modules/lab-app/plan.json --policy policy --output table
   ```

   Ожидаемо на «правильном» tfvars: пустой `value: []` и `0 failures`. Затем временно закомментируйте `owner` в `stage.tfvars`, пересоберите план — ожидаемо `1 failure` с текстом «не указан owner».

10. **UBU01.** Тест на Go из раздела 4: `go mod init`, `go get`, `go test -v -timeout 30m ./...`. Ожидаемо: `--- PASS` и `ok`.

11. **GIT01.** Кладём `.gitlab-ci.yml`:

    ```yaml
    stages: [validate, lint, security, policy, test]

    variables:
      TF_IN_AUTOMATION: "1"
      TF_INPUT: "0"

    fmt:
      stage: validate
      tags: [ubu01]
      script:
        - terraform fmt -check -recursive -diff
        - terraform -chdir=modules/lab-app init -backend=false -input=false
        - terraform -chdir=modules/lab-app validate

    tflint:
      stage: lint
      tags: [ubu01]
      script:
        - tflint --init
        - tflint --recursive --format=compact

    checkov:
      stage: security
      tags: [ubu01]
      script:
        - checkov -d . --framework terraform --compact --output-file-path checkov.json
      artifacts:
        when: always
        paths: [checkov.json]
        expire_in: 1 week

    policy:
      stage: policy
      tags: [ubu01]
      script:
        - cd modules/lab-app
        - terraform init -backend=false -input=false
        - terraform plan -out=tfplan -input=false -var-file=../../env/stage.tfvars
        - terraform show -json tfplan > plan.json
        - conftest test plan.json --policy ../../policy --output table
      artifacts:
        when: always
        paths: [modules/lab-app/plan.json]
        expire_in: 1 day
    ```

    Коммитим, пушим в `main`. Ожидаемо: зелёный пайплайн из пяти стадий.

12. **UBU01.** Ломаем политику намеренно (см. следующий подраздел) и убеждаемся, что пайплайн стал красным. Возвращаем как было. Записываем всё в `stand-log.md`.

### Проверка

- [ ] `tflint --version`, `conftest --version`, `opa version`, `checkov --version` отвечают без ошибок.
- [ ] `terraform fmt -check -recursive` возвращает 0.
- [ ] `modules/lab-app/plan.json` существует и содержит `"resource_changes"`.
- [ ] `terraform test -verbose` — `1 passed, 0 failed`.
- [ ] `conftest test plan.json --policy policy` на корректном tfvars даёт `0 failures`.
- [ ] После удаления `owner` из tfvars `conftest` даёт ровно 1 failure с адресом `terraform_data.config`.
- [ ] `go test ./...` — `PASS`.
- [ ] В GitLab пайплайн по `main` зелёный, в артефактах лежат `checkov.json` и `plan.json`.
- [ ] В `stand-log.md` появилась запись про установленные на UBU01 инструменты и проект на GIT01.

### Сломай и почини

**Пользователь:** дежурный администратор (`adm.student`, вторая смена).
**ВМ:** GIT01.
**Что написал:** «Пайплайн зелёный, но в `main` вмержили MR, где у ресурса нет `owner`. Conftest при этом пишет `0 failures`. Политика же есть, я её видел глазами».
**Срочность:** высокая.

**Как воспроизвести поломку.** На UBU01 в `policy/terraform.rego` переименуйте правило `deny` в `owner_check` — имена переменных внутри оставьте. Закоммитьте, запушьте MR без `owner` в tfvars. Пайплайн пройдёт зелёным.

**Диагностика по шагам:**

1. `conftest test modules/lab-app/plan.json --policy policy --output json` — видим пустой массив результатов. Значит, Conftest вообще не нашёл ни одного правила, которое надо оценивать.
2. `opa eval -d policy -i modules/lab-app/plan.json 'data.main'` — смотрим, какие правила вообще существуют в пакете. Видим `owner_check` вместо `deny`.
3. `grep -n "^deny\|^violation\|^warn" policy/*.rego` — подтверждаем: ни одного правила с нужным префиксом.
4. `conftest verify --policy policy` — юнит-тестов политики нет, поэтому и этот шаг ничего не поймал.

**Решение.** Вернуть имя `deny` (или `violation`). И добавить защиту от повторения — два изменения:

```bash
# policy/terraform_test.rego — тест самой политики
cat > policy/terraform_test.rego <<'EOF'
package main

import rego.v1

test_deny_missing_owner if {
    input := {"variables": {"env": {"value": "stage"}},
              "resource_changes": [{"address": "terraform_data.config",
                                    "change": {"actions": ["create"], "after": {"input": {}}}}]}
    count(deny) == 1
}

test_allow_with_owner if {
    input := {"variables": {"env": {"value": "stage"}},
              "resource_changes": [{"address": "terraform_data.config",
                                    "change": {"actions": ["create"],
                                               "after": {"input": {"owner": "team-platform"}}}}]}
    count(deny) == 0
}
EOF
conftest verify --policy policy
```

И в `.gitlab-ci.yml` в стадию `policy` добавить первым шагом `conftest verify --policy ../../policy`. Теперь если правило переименуют — упадут тесты политики, а не продакшн.

**Что написать пользователю:** «Проверка отключилась не из-за конфигурации, а из-за переименования правила в политике: Conftest реагирует только на правила с префиксами `deny`/`violation`/`warn`, остальные молча игнорирует. Вернул имя, добавил юнит-тесты политики и шаг `conftest verify` в пайплайн. MR можно перезапускать».

**Закрытие заявки:** категория «IaC / CI-CD», решение — «исправлена политика, добавлено негативное тестирование», привязка к коммиту с `conftest verify`.

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлены `checkov` (в venv `~/venvs/policy`), `tflint`, `conftest`, `opa`, `go`; каталог `~/infra-lab/` с модулем `modules/lab-app`, политиками `policy/` и тестами `test/`.
- **GIT01 (10.10.10.50):** новый проект `vektor/infra-lab`, `.gitlab-ci.yml` с пятью стадиями, артефакты `checkov.json` (7 дней) и `plan.json` (1 день).
- **Новые артефакты на UBU01:** `modules/lab-app/tfplan` (бинарный план) и `plan.json`. Оба содержат значения переменных — на стенде они безобидные, в работе это потенциальная утечка.
- **Политики:** `policy/terraform.rego` (2 deny-правила), `policy/terraform_test.rego` (2 юнит-теста).
- Ничего не удалено, Windows-часть стенда не затрагивалась.

### Откат

VMware Workstation → правый клик по UBU01 → Snapshot Manager → выбрать `before-devops-065` → Revert. То же для GIT01. Если откатываете только UBU01, а GIT01 оставляете — пайплайн на GIT01 будет падать на отсутствующем `conftest`: откатывайте обе машины согласованно либо удалите проект в GitLab вручную (Settings → General → Advanced → Delete project).

## 6. Ошибки новичков и безопасность

1. **`--soft-fail` в CI «чтобы не мешало».** → Сканер навсегда перестаёт блокировать что-либо, находки никто не читает. → Правильно: `--soft-fail` только на первый месяц внедрения, с датой в комментарии и напоминанием в календаре.
2. **Политика без негативного теста.** → Правило переименовали/сломали — проверка молча выключилась, как в лабораторной. → Правильно: `conftest verify` / `opa test` / `sentinel test` в пайплайне на каждый PR с политикой.
3. **Сканировать только HCL.** → `checkov` по коду не видит значений, вычисленных из `count`, `for_each`, `locals` и переменных: false negative. → Правильно: HCL для быстрой обратной связи, `plan.json` — для блокирующей проверки.
4. **Сразу `hard-mandatory` на всё.** → Первый же релиз встаёт, команда идёт к админу просить исключение, через месяц политики отключены. → Правильно: `advisory` → отчёт по нарушениям → `soft-mandatory` для критичного → `hard-mandatory` для двух-трёх правил, которые действительно нельзя нарушать.
5. **`plan.json` в артефактах на 30 дней.** → План содержит значения переменных, включая секреты, в открытом виде; артефакты читает вся команда. → Правильно: короткий `expire_in`, отдельный защищённый проект, секреты — только из Vault/CI-переменных masked.
6. **terratest без `defer terraform.Destroy`.** → После падения теста остаются ВМ и диски, счёт растёт. → Правильно: `defer` первой строкой после `opts`, плюс тег `ttl` на все ресурсы, где провайдер это умеет.
7. **Тесты против prod-окружения.** → Тест может что-то удалить или изменить в бою. → Правильно: отдельный аккаунт/проект/подписка под тесты, отдельные креды, запрет на prod в CI-переменных по умолчанию.
8. **Огромный список исключений в baseline без владельца.** → Через полгода никто не знает, почему 200 проверок отключены. → Правильно: каждое исключение — с owner, причиной и TTL; ревью baseline раз в квартал.

**Безопасность.** Не давайте CI-раннеру креды с правами на prod по умолчанию: только protected-ветки и protected environments GitLab, только ручной запуск. OPA в серверном режиме (`--server`) без аутентификации — это открытый HTTP API на 8181: в бою только за реверс-прокси с аутентификацией либо локальный запуск в пайплайне. И не превращайте `checkov`/`conftest` в единственную линию обороны: это статические проверки, они не заменяют и не отменяют ограничений IAM в облаке — права должны быть минимальными независимо от политик.

## 7. Вопросы на собеседовании

**1. Чем линтер отличается от сканера безопасности и от policy-as-code? Что и в каком порядке запускать?**

Ответ: три разных слоя. Линтер (`tflint`) проверяет корректность и качество кода для конкретного провайдера: валидные типы инстансов, устаревшие аргументы, неиспользуемые переменные — про безопасность почти ничего. Сканер (`checkov`, `trivy config`) ищет известные мисконфигурации безопасности по базе правил: публичный доступ, отсутствие шифрования, открытые порты. Policy-as-code (`OPA`/`Conftest`, `Sentinel`) — ваши собственные организационные правила: обязательные теги, допустимые регионы, запрет удаления в prod, нейминг. Порядок в пайплайне: `fmt` → `validate` → `tflint` (секунды) → `checkov` по HCL → `plan` + policy по `plan.json` → тесты. От дешёвого к дорогому, чтобы разработчик получал быструю обратную связь.

*Что хочет услышать интервьюер:* понимание, что инструменты не дублируют друг друга и что их порядок определяется стоимостью проверки.
*Красный флаг:* «checkov делает всё, tflint не нужен» или «политики пишутся вручную в bash-скрипте».

**2. Почему политики проверяют по `plan.json`, а не по HCL-файлам?**

Ответ: HCL — это шаблон с переменными, `count`, `for_each`, `locals`, модулями и условными выражениями. По статическому коду нельзя достоверно сказать, что получится: `count = var.env == "prod" ? 3 : 1` в HCL выглядит безобидно, а в плане даёт три ресурса. `plan.json` содержит `resource_changes[].change.after` — уже вычисленные итоговые значения и `actions`, то есть ровно то, что уедет в облако. Плюс в нём есть `variables` и `prior_state`, что позволяет писать правила вида «запретить удаление при `env == prod`». Ограничение: для получения плана нужны рабочие креды и инициализированные провайдеры, а сам `plan.json` может содержать секреты в открытом виде.

*Что хочет услышать интервьюер:* что вы понимаете разницу между кодом и планом и знаете про утечку чувствительных значений в артефактах.
*Красный флаг:* «plan.json нужен, чтобы быстрее работало» — не понимает сути.

**3. Чем terratest отличается от `terraform test` и когда что использовать?**

Ответ: `terraform test` — встроенный в Terraform фреймворк: файлы `*.tftest.hcl`, декларативные `assert`-блоки, `command = plan` или `apply`, никакого дополнительного языка. Быстро разворачивается, отлично ложится в пайплайн, проверяет план и выходные значения. Ограничение — проверяет только Terraform: нельзя дёрнуть HTTP-эндпоинт созданного сервиса, зайти по SSH, проверить, что приложение реально отвечает. terratest — библиотека на Go: тест может сделать `terraform.InitAndApply`, потом `http.DoGetWithRetry`, `ssh.CheckPrivateKey`, запрос в базу, и обязан сделать `terraform.Destroy` в `defer`. Это интеграционный уровень: дорого, медленно, требует настоящих кредов. Практика: `terraform test` — на каждый PR, terratest — на ночной прогон и перед релизом модуля.

*Что хочет услышать интервьюер:* понимание, что инструменты дополняют друг друга, и что terratest всегда с `defer Destroy`.
*Красный флаг:* «terratest устарел, всё покрывается `terraform test`» — не видел интеграционных сценариев.

**4. Что такое Sentinel, чем он отличается от OPA/Conftest и что делать, если нет Terraform Enterprise?**

Ответ: Sentinel — язык политик и движок от HashiCorp, встроенный в HCP Terraform и Terraform Enterprise. Работает не только с Terraform, но и с Vault, Consul, Nomad. Политики цепляются к workspace через Policy Sets, есть три уровня принуждения: `advisory` (только пишет в лог), `soft-mandatory` (блокирует, но админ может переопределить с обоснованием), `hard-mandatory` (блокирует всегда). Импорты `tfplan/v2`, `tfconfig/v2`, `tfstate/v2`. Локальный CLI `sentinel test` бесплатен, но интеграция в пайплайн требует платной подписки. OPA/Conftest — открытый аналог: Rego, любой входной JSON, локальный запуск в CI без подписки, но и «непробиваемого» уровня принуждения нет — всё держится на том, что пайплайн нельзя обойти. Если TFE нет (типичная картина в РФ 2026), легальный и рабочий путь — `plan.json` + OPA/Conftest + защищённые ветки и окружения в GitLab, плюс запрет ручного `apply` из ноутбуков.

*Что хочет услышать интервьюер:* что вы не привязаны к одному вендору и умеете воспроизвести ту же логику на open-source.
*Красный флаг:* «без Sentinel policy-as-code невозможен» или предложение «найти ключ к TFE».

**5. Как выкатить policy-as-code в компании с пятью командами и не остановить разработку?**

Ответ: поэтапно, с измерением. Сначала — инвентаризация: включить `checkov` и `conftest` в режиме предупреждений (`advisory`, `--soft-fail`), собрать статистику по командам за две-три недели. Затем зафиксировать текущее состояние в baseline, чтобы новые нарушения блокировались, а старые горели в отчёте. Дальше выбрать 2–3 правила с минимальным количеством ложных срабатываний (например, «нет owner-тега», «нет версии провайдера») и перевести их в блокирующие в `dev`, потом в `stage`, потом в `prod`. Каждое исключение — только через код: строка `#checkov:skip=` или блок исключений в политике с обязательным полем owner и датой пересмотра. Сами политики тестировать: `conftest verify` / `opa test` с позитивными и негативными фикстурами на каждый PR. Раз в квартал — ревью baseline и удаление мёртвых исключений.

*Что хочет услышать интервьюер:* постепенность, метрики, владелец у каждого исключения, тесты на сами политики.
*Красный флаг:* «включаем hard-mandatory сразу на всё» — показывает отсутствие опыта внедрения в живых командах.

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — раздел Terraform → «Testing Terraform configuration», «terraform test command», «Commands: plan / show». Поиск: `terraform test tftest.hcl`.
- **developer.hashicorp.com** (Sentinel) — «Sentinel Language», «Sentinel imports: tfplan/v2», «Policy enforcement levels». Поиск: `sentinel tfplan v2 import`. Конкретный адрес страницы про policy enforcement в HCP Terraform уточнить `[ПРОВЕРИТЬ]`.
- **github.com/terraform-linters/tflint** — «TFLint User Guide», «Configuring TFLint», «tflint-ruleset-terraform». Поиск: `tflint config hcl plugin terraform preset`.
- **github.com/bridgecrewio/checkov** — «Checkov documentation», «Custom policies», «Suppressing checks», «Baseline». Поиск: `checkov custom policy yaml attribute`.
- **openpolicyagent.org** — «Policy Language», «Rego», «OPA CLI: eval», «Rego v1». Поиск: `rego v1 import future keywords`.
- **conftest.dev** — «Conftest: test command», «verify», «Supported parsers», «OCI policies». Поиск: `conftest verify policy unit test`.
- **github.com/gruntwork-io/terratest** — «Terratest», раздел Terraform module, godoc-документация пакета `modules/terraform`. Поиск: `terratest terraform InitAndApply options`.
- **trivy.dev** — «Misconfiguration scanning», «Trivy config». Поиск: `trivy config terraform severity`.
- **kubernetes.io** — «Validating Admission Policy» (как policy-as-code встроен в k8s; пригодится в уроке 67). Поиск: `kubernetes validating admission policy`.
