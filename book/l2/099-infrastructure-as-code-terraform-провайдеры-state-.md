# Infrastructure as Code: Terraform, провайдеры, state, модули

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **Что написал:** «Здравствуйте. В `\\FS01\Obshchee\stand-inventory.md` у сервера MON01 указан адрес 10.10.10.14, а мы настраивали 10.10.10.40. И WEB01 в таблице нет. Мне это для описи оборудования, пришлите правильный файл.»
> **Срочность:** средняя.

Дальше выясняется классика: файл инвентаря кто-то «поправил руками», список серверов живёт в трёх местах, и никто не помнит, какая версия настоящая. Именно эту боль решает Infrastructure as Code.

**Infrastructure as Code (IaC)** — подход, при котором серверы, сети, DNS-записи, учётные записи и прочая инфраструктура описываются текстовыми файлами в репозитории, а не создаются руками через веб-морду. Файлы ревьюятся, версионируются, и из них можно воспроизвести стенд с нуля.

Terraform — самый распространённый инструмент этого класса. Формально он про облака и виртуализацию, но на уровне L2 ты столкнёшься с ним так: посмотреть, почему `terraform plan` в пайплайне падает; понять, что «поехало» в конфиге после чужого merge request; объяснить, почему нельзя руками править сервер, который описан в коде; не дать положить state-файл в git.

После главы ты должен уметь: читать HCL-конфиг, осознанно запускать `init/plan/apply`, работать со state (в том числе `state mv`/`state rm`/`import`), понимать, что такое провайдер и модуль, и диагностировать расхождение «код vs реальность» (drift).

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ключевая идея — **декларативность**. Ты пишешь «хочу три файла с такими данными», а не «создай файл, если его нет, потом проверь, потом...». Terraform сам строит граф зависимостей (DAG), параллельно выполняет независимые шаги и приводит мир к описанному состоянию. Повторный запуск ничего не меняет — это называется **идемпотентность**.

Terraform состоит из двух частей:

- **core (CLI `terraform`)** — читает HCL, строит граф, сравнивает желаемое с состоянием, считает план. Сам он почти ничего не умеет делать с реальным миром.
- **провайдеры (providers)** — отдельные бинарные плагины, которые умеют разговаривать с конкретной системой: `hashicorp/local` (файлы на диске), `hashicorp/random` (случайные значения), `hashicorp/vsphere`, `yandex-cloud/yandex`, `bpg/proxmox` `[ПРОВЕРИТЬ актуальное имя провайдера]`. Core общается с плагинами по gRPC через localhost, плагины — с внешним миром по HTTPS/443 или по API вендора.

**State** — файл `terraform.tfstate` в формате JSON. Это память Terraform: какие объекты он создал и какие у них идентификаторы в реальной системе. Без state он не знает, что «этот файл — уже мой», и попытается создать его заново. Рядом лежит `terraform.tfstate.backup` — предыдущая версия (спасательный круг).

**Backend** — где хранится state. По умолчанию `local` (файл рядом с конфигом). В командах используют удалённые: S3-совместимое хранилище, GitLab-managed, Consul, PostgreSQL и др. Удалённый backend даёт блокировку (lock), чтобы два инженера или два пайплайна не применили изменения одновременно. Локальный state от параллельных запусков не защищает.

```text
  *.tf / *.tfvars  (HCL, желаемое состояние)
          |
          v
  +-------------------------------+        gRPC (localhost)
  |  terraform CLI (core)         |----------------------------+
  |  init / plan / apply          |                            v
  |  граф зависимостей, diff      |                 +----------------------+
  +---------------+---------------+                 | providers (плагины): |
                  |                               | local, random,       |
                  |  читает/пишет                 | vsphere, yandex...   |
                  v                               +----------+-----------+
       terraform.tfstate  (JSON, память)                    | HTTPS/443, API
       + .backup                                            v
                                                  +----------------------+
                                                  | реальная инфра:      |
                                                  | файлы, ВМ, DNS, S3   |
                                                  +----------------------+
```

Жизненный цикл: `init` (скачать провайдеры, записать lock-файл) → `plan` (refresh: опросить реальность, сравнить со state и с конфигом, показать diff) → `apply` (выполнить изменения, обновить state) → `destroy` (удалить всё, что описано).

Термины, которые нужны на собеседовании:

- **provider** — плагин доступа к системе;
- **resource** — объект, которым управляем (`local_file.host`);
- **data source** — «только прочитать» существующий объект;
- **variable / output** — вход и выход модуля;
- **module** — папка с `.tf`-файлами, которую вызывают из корня через `module "..." { source = "./modules/..." }`;
- **drift** — реальность ушла от state (кто-то удалил файл/ВМ руками).

Порты и сеть: Terraform не слушает входящие. Ему нужны исходящие 443 к `registry.terraform.io` и к CDN с бинарниками провайдеров, плюс 443 к API той системы, которой он управляет. Если из РФ реестр или CDN недоступны — это одна строка боли: легальные варианты — зеркало/офлайн-кэш провайдеров (`terraform providers mirror`) или открытый форк **OpenTofu** (лицензия MPL-2.0, команды те же, бинарник `tofu`). Сам Terraform с версии 1.6 распространяется по лицензии BUSL 1.1 — для внутреннего использования это обычно допустимо, но в компаниях с строгой политикой часто выбирают OpenTofu.

## 3. Аналогия из жизни

Представь ремонт квартиры по дизайн-проекту. **Дизайн-проект** — это твои `.tf`-файлы: чётко написано, где розетки, какой ширины дверь. **Прораб** — Terraform: он не спрашивает «а может, сдвинем?», он делает ровно по проекту. **Субподрядчики** — провайдеры: электрик умеет только электрику, плиточник только плитку, и прораб не будет сам штробить стены.

**Акт выполненных работ** — это state: список того, что уже сделано и где именно (конкретная стена, конкретный щиток). Если акт потерять, прораб приедет на объект и скажет: «Розеток не вижу, буду делать новые» — и просверлит вторые рядом с первыми. Если акт править от руки, не переделав работу, — получишь бумажку, которая врёт.

**Типовой проект** («санузел 3 м², серия Б») — это модуль: один раз описал, потом подключаешь в десяти квартирах с разными параметрами.

Аналогия ломается здесь: прораб, увидев, что кто-то самовольно переставил дверь, будет ругаться и звонить тебе, — а Terraform молча приведёт дверь обратно к проекту, даже если «так было удобнее».

## 4. Практика и команды

Все примеры — с UBU01 (Ubuntu Server 24.04), проект в `~/iac-lab`. Linux и Windows-клиент показаны там, где это уместно.

**1. Версия и проверка установки**

```bash
terraform version
# Terraform v1.x.y
# + provider registry.terraform.io/hashicorp/local v2.x.y
```

`version` показывает и CLI, и уже скачанные в `.terraform/` провайдеры. Полезно, когда «у меня работает, у тебя нет»: сравни версии. В OpenTofu та же команда — `tofu version`.

**2. `terraform init` — подготовка рабочего каталога**

```bash
cd ~/iac-lab
terraform init
# Initializing the backend...
# Initializing provider plugins...
# - Installing hashicorp/local v2.5.x...
# Terraform has created a lock file .terraform.lock.hcl
```

Ключи: `-upgrade` — обновить провайдеры в рамках ограничений из `required_providers`; `-reconfigure` — перечитать backend после смены настроек; `-backend=false` — только провайдеры, без backend. Что смотреть в выводе: ошибки «Failed to query available provider packages» = нет доступа к registry (сеть/прокси/блокировка). Файл `.terraform.lock.hcl` **коммитится в git**: он фиксирует точные версии и контрольные суммы.

**3. Форматирование и валидация**

```bash
terraform fmt -recursive -check
terraform validate
# Success! The configuration is valid.
```

`fmt` приводит отступы к канону (`-check` только проверяет, удобно в CI), `validate` ловит синтаксис и ошибки в ссылках на переменные/ресурсы, но не проверяет, существует ли объект в реальности. В пайплайне (урок 98) эти две команды ставят до `plan`.

**4. `terraform plan` — «что изменится»**

```bash
terraform plan -out=tfplan
# No changes. Your infrastructure matches the configuration.
# или:
# Plan: 1 to add, 0 to change, 0 to destroy.
```

Ключи: `-out=tfplan` — сохранить план и применить ровно его; `-target=module.inventory["DC01"]` — ограничить область (аварийный инструмент, не норма); `-refresh=false` — не опрашивать реальность; `-var-file=prod.tfvars`. Что смотреть: строки `+ create`, `~ update in-place`, `-/+ must be replaced` (последнее — уничтожение и пересоздание, часто с простоем!) и итоговую сводку.

**5. `terraform apply`**

```bash
terraform apply tfplan        # применить сохранённый план, без вопросов
terraform apply -auto-approve # в лабе; в бою — только после review
terraform apply -replace='module.inventory["MON01"].local_file.host'
```

`-replace=ADDRESS` — принудительно пересоздать объект (замена устаревшему `terraform taint`). В рабочей среде `-auto-approve` в руках человека — почти всегда ошибка.

**6. Выходные значения — `output`**

```bash
terraform output
terraform output -json
terraform output stand_id
# "vektor-l2-1a2b3c"
```

Удобно для пайплайнов: следующий шаг забирает IP или имя из `-json`, а не парсит логи.

**7. Работа со state — `state list` / `show`**

```bash
terraform state list
# module.inventory["DC01"].local_file.host
# module.inventory["FS01"].local_file.host
# random_id.stand
terraform state show 'module.inventory["MON01"].local_file.host'
# resource "local_file" "host" {
#     content  = <<-EOT ...
#     filename = "out/hosts/mon01.txt"
# }
```

Это первые команды при разборе «Terraform говорит, что объект есть, а его нет». `state list` показывает, что Terraform вообще считает своим.

**8. Перенос ресурса — `state mv` и удаление из памяти — `state rm`**

```bash
terraform state mv 'module.inventory["DC01"]' 'module.inventory["dc01"]'
terraform state rm 'module.inventory["MON01"].local_file.host'
```

`state mv` нужен, когда переименовал ключ/ресурс в коде: без него Terraform покажет `destroy` + `create` (потеря/пересоздание объекта), с ним — «ничего не изменилось». `state rm` **не удаляет объект в реальности**, он лишь говорит Terraform «забудь про него» — после этого объект перестаёт управляться и обычно создаётся заново при следующем apply. Перед любым `state mv/rm` делай копию: `cp terraform.tfstate terraform.tfstate.bak-$(date +%F)`.

**9. Импорт существующего объекта**

```bash
terraform import 'module.inventory["WEB01"].local_file.host' out/hosts/web01.txt
```

Синтаксис: `terraform import ADDRESS ID`, где ID — идентификатор в терминах провайдера (для `local_file` это путь к файлу). В версии 1.5+ появился декларативный `import`-блок — импорт становится частью кода и проходит через `plan`:

```hcl
import {
  to = module.inventory["WEB01"].local_file.host
  id = "out/hosts/web01.txt"
}
```

**10. Провайдеры и их версии**

```bash
terraform providers
# ├── provider[registry.terraform.io/hashicorp/local] ~> 2.5
terraform providers lock -platform=linux_amd64 -platform=windows_amd64
```

`providers lock` добавляет в lock-файл контрольные суммы для чужих платформ — нужно, если `plan` гоняют в Linux-контейнере, а `apply` с ноутбука на Windows. Без ограничения версии (`version = "~> 2.5"`) `init -upgrade` однажды притащит мажорное обновление провайдера и сломает план.

**11. Диагностика: логи и консоль**

```bash
TF_LOG=DEBUG TF_LOG_PATH=/tmp/tf-debug.log terraform plan
terraform console
> var.machines["MON01"].ip
"10.10.10.40"
```

`TF_LOG` — главный инструмент L2, когда «plan висит» или «Terraform не видит API»: в логе видно, к какому URL он стучится и какой получает ответ (401/403/timeout). `terraform console` — REPL для проверки выражений HCL без запуска плана. Ещё есть `terraform destroy` (удалить всё из state) и `terraform graph` (диаграмма зависимостей в формате DOT).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны: **UBU01** (10.10.10.30, Ubuntu Server 24.04, SSH работает, интернет через NAT VMnet8) — основная машина; **WIN11-CL01** — как SSH-клиент; **FS01** (10.10.10.20) — только чтобы положить итоговый файл в `\\FS01\Obshchee`. Роли AD, DNS, DHCP не затрагиваем.

Никаких новых ВМ для этой главы не создаём: у стенда пока нет ни облака (это урок 100), ни vCenter — а официального Terraform-провайдера для VMware Workstation не существует. Поэтому тренируемся на провайдерах `local` и `random`: они полностью бесплатны и дают те же самые механики (провайдер, state, модули, drift), что и «взрослые».

### Снапшот

В VMware Workstation выключи гостей или делай снапшот на горячую (VM → Snapshot → Take Snapshot):
- `UBU01` → `before-l2-099`
- `WIN11-CL01` → `before-l2-099`

### Шаги

1. **WIN11-CL01.** Открой PowerShell, подключись к UBU01:
   ```powershell
   ssh <твой_linux_пользователь>@10.10.10.30
   ```
   Ожидаемо: приглашение UBU01.

2. **UBU01.** Проверь интернет и поставь Terraform:
   ```bash
   curl -sI https://registry.terraform.io | head -1
   sudo apt update && sudo apt install -y unzip curl gnupg lsb_release
   curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
   echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
   sudo apt update && sudo apt install -y terraform
   terraform version
   ```
   Ожидаемо: `Terraform v1.x.y`. Кодовое имя `noble` в репозитории HashiCorp `[ПРОВЕРИТЬ]`; если пакета нет — ставь OpenTofu из `packages.opentofu.org` `[ПРОВЕРИТЬ актуальную инструкцию]` и далее заменяй `terraform` на `tofu`, всё остальное идентично.

3. **UBU01.** Создай проект и каталог вывода:
   ```bash
   mkdir -p ~/iac-lab/modules/inventory ~/iac-lab/out/hosts
   cd ~/iac-lab
   ```

4. **UBU01.** Создай `main.tf`:
   ```hcl
   terraform {
     required_version = ">= 1.5"
     required_providers {
       local  = { source = "hashicorp/local",  version = "~> 2.5" }
       random = { source = "hashicorp/random", version = "~> 3.6" }
     }
   }

   provider "local" {}

   resource "random_id" "stand" {
     byte_length = 3
   }

   locals {
     stand_id = "${var.stand_name}-${random_id.stand.hex}"
   }

   module "inventory" {
     source   = "./modules/inventory"
     for_each = var.machines

     host_name = each.key
     host_ip   = each.value.ip
     host_role = each.value.role
     stand_id  = local.stand_id
     out_dir   = var.out_dir
   }

   resource "local_file" "stand_inventory" {
     filename = "${var.out_dir}/stand-inventory.md"
     content = join("\n", concat(
       ["# Инвентарь стенда ${local.stand_id}", "", "| машина | адрес | роль |", "|---|---|---|"],
       [for name, m in var.machines : "| ${name} | ${m.ip} | ${m.role} |"]
     ))
   }
   ```
   Ожидаемо: файл создан. `for_each` создаёт по одному экземпляру модуля на каждую машину.

5. **UBU01.** Создай `variables.tf` и `outputs.tf`:
   ```hcl
   variable "stand_name" { type = string, default = "vektor-l2" }
   variable "out_dir"    { type = string, default = "out" }
   variable "machines" {
     type = map(object({ ip = string, role = string }))
   }
   ```
   ```hcl
   output "stand_id"   { value = local.stand_id }
   output "host_files" { value = { for n, m in module.inventory : n => m.path } }
   ```

6. **UBU01.** Создай модуль `modules/inventory/main.tf` (переменные + ресурс + выход в одном файле — так тоже можно, все `.tf` в каталоге склеиваются):
   ```hcl
   variable "host_name" { type = string }
   variable "host_ip"   { type = string }
   variable "host_role" { type = string }
   variable "stand_id"  { type = string }
   variable "out_dir"   { type = string }

   resource "local_file" "host" {
     filename        = "${var.out_dir}/hosts/${lower(var.host_name)}.txt"
     file_permission = "0644"
     content         = <<-EOT
       hostname: ${var.host_name}
       ip:       ${var.host_ip}
       role:     ${var.host_role}
       stand:    ${var.stand_id}
     EOT
   }

   output "path" { value = local_file.host.filename }
   ```

7. **UBU01.** Заполни `terraform.tfvars` — источник истины по составу стенда:
   ```hcl
   machines = {
     DC01    = { ip = "10.10.10.10", role = "AD DS, DNS, DHCP" }
     DC02    = { ip = "10.10.10.11", role = "AD DS реплика" }
     FS01    = { ip = "10.10.10.20", role = "файлы и печать" }
     SRV1C01 = { ip = "10.10.10.25", role = "1С" }
     UBU01   = { ip = "10.10.10.30", role = "Linux-практика, SSH" }
     ASTRA01 = { ip = "10.10.10.31", role = "Astra Linux" }
     MON01   = { ip = "10.10.10.40", role = "Zabbix" }
     GW01    = { ip = "10.10.10.3",  role = "шлюз nftables, DMZ 10.10.20.1" }
     WEB01   = { ip = "10.10.20.10", role = "nginx в DMZ" }
   }
   ```

8. **UBU01.** Запусти конвейер:
   ```bash
   terraform init
   terraform fmt -recursive
   terraform validate
   terraform plan -out=tfplan
   terraform apply tfplan
   ```
   Ожидаемо в плане: `Plan: 11 to add, 0 to change, 0 to destroy.` (9 модулей + сводный файл + `random_id`). После apply: `Apply complete! Resources: 11 added`.

9. **UBU01.** Проверь результат:
   ```bash
   ls out/hosts
   cat out/stand-inventory.md
   terraform output stand_id
   terraform state list | head
   ```
   Ожидаемо: 9 файлов `dc01.txt … web01.txt`, таблица из 9 строк, `stand_id` вида `vektor-l2-1a2b3c`.

10. **UBU01.** Добавь машину и посмотри на diff:
    ```bash
    sed -i 's|WEB01   = .*|WEB01   = { ip = "10.10.20.10", role = "nginx в DMZ" }\n  PVE01   = { ip = "10.10.10.70", role = "Proxmox (стенд сисадмина)" }|' terraform.tfvars
    terraform plan
    ```
    Ожидаемо: `Plan: 2 to add, 1 to change, 0 to destroy.` — новый модуль и обновление сводной таблицы. Затем `terraform apply -auto-approve`.

11. **UBU01.** Сымитируй drift — удали файл руками:
    ```bash
    rm out/hosts/mon01.txt
    terraform plan
    ```
    Ожидаемо: Terraform увидит, что объекта нет, и запланирует создание `module.inventory["MON01"].local_file.host`. Верни как было: `terraform apply -auto-approve`.

12. **UBU01.** Переименуй ключ в конфиге и «спаси» ресурс от пересоздания:
    ```bash
    sed -i 's/DC01    =/dc01    =/' terraform.tfvars
    terraform plan        # увидишь destroy + create — это плохо
    terraform state mv 'module.inventory["DC01"]' 'module.inventory["dc01"]'
    terraform plan        # No changes
    ```

13. **WIN11-CL01.** Опубликуй инвентарь в общую папку и закрой журнал стенда:
    ```powershell
    scp <твой_linux_пользователь>@10.10.10.30:~/iac-lab/out/stand-inventory.md "$env:TEMP\stand-inventory.md"
    Copy-Item "$env:TEMP\stand-inventory.md" "\\FS01\Obshchee\stand-inventory.md"
    ```
    Ожидаемо: файл открывается у пользователей с правами на `\\FS01\Obshchee`. Допиши изменения в `stand-log.md`.

### Проверка

- [ ] `terraform version` отвечает, `terraform init` завершился без ошибок, `.terraform.lock.hcl` создан.
- [ ] После первого apply в `~/iac-lab/out/hosts` лежат 9 файлов, `stand-inventory.md` содержит таблицу.
- [ ] Повторный `terraform plan` даёт `No changes`.
- [ ] После удаления `mon01.txt` план снова хочет создать этот файл.
- [ ] `terraform state list` содержит `module.inventory["dc01"].local_file.host` (в нижнем регистре) после `state mv`.
- [ ] `terraform output -json` отдаёт валидный JSON.
- [ ] Файл `stand-inventory.md` доступен в `\\FS01\Obshchee`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «В `\\FS01\Obshchee\stand-inventory.md` у MON01 адрес 10.10.10.14. У нас же мониторинг на 10.10.10.40. И почему-то после вашего обновления. Пришлите правильный файл, мне до вечера.»
> **Срочность:** средняя

**Как воспроизвести поломку** (UBU01):

```bash
sed -i 's/10.10.10.40/10.10.10.14/' terraform.tfvars
terraform apply -auto-approve          # Terraform честно применил опечатку
scp out/stand-inventory.md <user>@10.10.10.30:~/  # и обновил копию в шаре
```

**Диагностика по шагам:**

1. Смотрим факт: `cat out/stand-inventory.md` и `cat out/hosts/mon01.txt` — оба показывают 10.10.10.14. Значит проблема не в публикации, а в источнике.
2. `terraform plan` → `No changes`. Важный вывод: state и конфиг согласованы, значит «сломано не в Terraform».
3. Идём в источник: `grep -n MON01 terraform.tfvars` → `10.10.10.14`. Вот корневая причина.
4. `terraform state show 'module.inventory["MON01"].local_file.host'` — в state ровно то же, что в файле. Terraform сделал то, что попросили.
5. Если проект в git — `git log -p terraform.tfvars | grep -n 10.10.10.14` покажет, когда и кем внесена правка. Нет git — смотрим `ls -l --time-style=full-iso terraform.tfvars`.

**Решение:**

```bash
sed -i 's/10.10.10.14/10.10.10.40/' terraform.tfvars
terraform plan           # ожидаем ~1 to change
terraform apply          # перезаписывает файлы по конфигу
grep MON01 out/stand-inventory.md   # 10.10.10.40
```

Затем с WIN11-CL01 повторить `scp` + `Copy-Item` в `\\FS01\Obshchee`.

**Ответ пользователю:** «Иван, разобрался: в конфигурации инвентаря была опечатка в адресе MON01 (10.10.10.14 вместо 10.10.10.40), она и попала в опубликованный файл. Исправил в источнике, пересобрал инвентарь, файл в `\\FS01\Obshchee\stand-inventory.md` обновлён — проверь, пожалуйста. Причина: изменение конфигурации применили без проверки плана, отдельно разберём процесс, чтобы не повторилось.»

**Закрытие заявки:** категория «Конфигурация / внутренние сервисы», резолюция «Опечатка в IaC-конфиге (terraform.tfvars), исправлена, `terraform apply` выполнен, документ обновлён в `\\FS01\Obshchee`», привязка к change-записи из урока 93. Плюс заметка в `stand-log.md` и предложение добавить `terraform plan` + ревью в пайплайн (урок 98).

### Что изменилось на стенде

- На **UBU01** установлен Terraform (или OpenTofu) как отдельный бинарник, пакет из официального репозитория.
- Создан каталог проекта `~/iac-lab`: `main.tf`, `variables.tf`, `outputs.tf`, `terraform.tfvars`, `modules/inventory/`, `.terraform/`, `.terraform.lock.hcl`, `terraform.tfstate` (+ `.backup`).
- Сгенерированы артефакты: `~/iac-lab/out/stand-inventory.md` и 9 файлов в `~/iac-lab/out/hosts/`.
- Ключ `DC01` в конфиге переименован в `dc01` (state перенесён через `state mv`).
- На **FS01** в `\\FS01\Obshchee` лежит копия `stand-inventory.md` — это новый общий артефакт, его можно обновлять в следующих главах.
- `stand-log.md` дополнен пунктом «IaC-проект на UBU01, источник истины по инвентарю — `terraform.tfvars`».

### Откат

VMware Workstation → выбери ВМ → Snapshot Manager → снапшот **`before-l2-099`** → **Revert**. Сделай для `UBU01` (и `WIN11-CL01`, если правил что-то там). После отката на FS01 останется скопированный `stand-inventory.md` — либо удали его вручную с WIN11-CL01 (нужны права на `\\FS01\Obshchee`), либо оставь как артефакт, но зафиксируй это в `stand-log.md`. Если откат не нужен, а нужно просто «обнулить» проект — `terraform destroy` в `~/iac-lab` удалит только сгенерированные файлы, сам Terraform и каталог проекта останутся.

## 6. Ошибки новичков и безопасность

- **`terraform.tfstate` в git** → в state лежат все параметры ресурсов, часто с паролями и ключами в открытом виде; плюс вечные конфликты слияния. Правильно: `.gitignore` с `*.tfstate*` и `.terraform/`, а state — в удалённый backend.
- **Правка `.tfstate` руками в редакторе** → битый JSON, потерянные ресурсы, «сироты» в инфраструктуре. Правильно: только `terraform state mv/rm/import`, и перед этим `cp terraform.tfstate terraform.tfstate.bak-$(date +%F)`.
- **`apply -auto-approve` не читая план** → `-/+ must be replaced` тихо удаляет боевую ВМ. Правильно: читать сводку и строки `must be replaced`, спорные изменения выносить на ревью.
- **Ручные правки объекта, которым управляет Terraform** → drift: следующий apply затрёт твою правку, а «почему вернулось старое» будет загадкой. Правильно: менять конфиг и применять.
- **Нет ограничения версий провайдеров** → `init -upgrade` приносит мажорную версию, план ломается в самый неподходящий момент. Правильно: `version = "~> 2.5"` и закоммиченный `.terraform.lock.hcl`.
- **`terraform apply` без remote backend в команде из двух человек** → два разных state, часть ресурсов потеряна из управления. Правильно: удалённый backend с блокировкой, один state на окружение.
- **`provisioner "local-exec"` для всего подряд** → идемпотентность теряется, ошибки не ловятся. Правильно: искать провайдер, а `local-exec` — только как костыль с явным комментарием.
- **Импорт «на глазок»** → `import` без последующей сверки плана даёт state, не совпадающий с конфигом. Правильно: импорт → `plan` → добиться `No changes`.

### Безопасность

- State — это хранилище секретов по факту. `sensitive = true` в переменных и выходах **скрывает значение в консоли, но не шифрует state**. В рабочей среде state шифруют на стороне backend и ограничивают доступ к нему, а секреты держат в Vault (урок 102).
- Модули из публичного Registry — это чужой код с доступом к твоим API-токенам. В боевой среде модуль сначала читают, фиксируют версию (`?ref=v1.2.3`) и по возможности кладут в свой приватный реестр.
- Учётные данные провайдера (service account в облаке, токен vCenter) должны иметь минимально нужные права и не лежать в `terraform.tfvars` в git — только переменные окружения CI или секреты пайплайна.
- `terraform destroy` на общем стенде — это инцидент, а не «уборка». На учебном стенде это норма, в бою — только через change-management (урок 93) и с бэкапом state.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Чем Terraform отличается от Ansible?**

Ответ: Terraform — инструмент провижининга, он создаёт инфраструктуру (ВМ, сети, DNS, бакеты) и хранит её состояние в state, работая декларативно: «хочу, чтобы было так». Ansible — конфигурационный менеджмент, он настраивает уже существующие машины (пакеты, конфиги, сервисы), обычно по SSH и по порядку шагов. Они дополняют друг друга: Terraform поднял ВМ — Ansible её настроил. Оба идемпотентны, но Terraform знает о ресурсе, который создал, а Ansible каждый раз просто описывает желаемое состояние.

*Что хочет услышать интервьюер:* чёткое разделение «создание инфраструктуры» vs «настройка внутри», понимание декларативности и роли state. *Красный флаг:* «Terraform — это то же самое, что Ansible, только для Linux».

**2. Что такое state и почему его нельзя хранить в git?**

Ответ: state — JSON-файл, в котором Terraform помнит, какие объекты он создал и под какими идентификаторами; без него он не отличит «свой» объект от «чужого» и попытается создать дубли. В git его нельзя хранить, потому что: там бывают секреты в открытом виде; при работе двух человек возникают конфликты слияния, которые невозможно безопасно разрешить; теряется блокировка. Правильно — удалённый backend (S3-совместимое хранилище, GitLab-managed и т.п.) с блокировкой, а в репозитории только код и `.terraform.lock.hcl`.

*Что хочет услышать:* про секреты, конфликты и блокировку; про то, что state — память, а не «источник истины». *Красный флаг:* «state можно спокойно коммитить, это же просто конфиг».

**3. Что делает `terraform plan` и что такое drift?**

Ответ: `plan` делает refresh — опрашивает реальные объекты, сверяет их со state и с конфигом — и показывает diff: что будет создано, изменено, уничтожено, пересоздано. Drift — расхождение реальности и state: кто-то удалил ВМ или поправил файл руками. Обнаруживается при refresh, и Terraform по умолчанию приводит объект обратно к описанному в конфиге. Если реальность нужно сохранить — правят конфиг или делают `import`, а не «привыкают к дрейфу».

*Что хочет услышать:* порядок «refresh → diff → apply», понимание, что код — источник истины. *Красный флаг:* «plan просто показывает, что будет, реальность он не смотрит».

**4. Что такое провайдер и что такое модуль? В чём разница?**

Ответ: провайдер — плагин, который умеет разговаривать с конкретной системой (его ресурсы и data source — это «словарь» возможностей). Модуль — переиспользуемый набор ресурсов с переменными и выходами, то есть «шаблон конфигурации», написанный на HCL. Модуль использует провайдеры, но сам провайдером не является. Модуль может быть локальным (`./modules/inventory`) или из реестра; версию внешнего модуля надо фиксировать.

*Что хочет услышать:* отделение «доступ к API» от «переиспользуемого шаблона». *Красный флаг:* «модуль и провайдер — это одно и то же, просто по-разному называется».

**5. Apply прошёл с ошибкой на середине. Что делать?**

Ответ: state уже частично обновлён, поэтому паниковать не надо — Terraform хранит то, что успел создать. Смотрим сообщение об ошибке и итог (`X added, Y changed, Z failed`), проверяем `terraform state list` — какие ресурсы уже в управлении; устраняем причину (квоты, права, конфликт имён, сеть) и запускаем `plan`/`apply` снова: он доделает остаток. Если объект создан, но не попал в state — `terraform import`. Если состояние явно неверное — `state rm` и повторное создание. Ручное удаление ресурса без `state rm` гарантированно даёт расхождение.

*Что хочет услышать:* знание, что apply инкрементальный и повторяемый, а инструменты починки — `state` и `import`. *Красный флаг:* «надо удалить всё и поднять заново».

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — Terraform Documentation: разделы «Language» (HCL, variables, modules), «CLI Commands» (`plan`, `apply`, `state`, `import`), «State», «Backends». Поиск: `terraform state command documentation`, `terraform import block`.
- **registry.terraform.io** — реестр провайдеров и модулей; страницы провайдеров `hashicorp/local`, `hashicorp/random`, `hashicorp/vsphere`, `yandex-cloud/yandex`. Поиск: `terraform registry local provider resource local_file`.
- **opentofu.org** — документация открытого форка OpenTofu и инструкция по миграции с Terraform. Поиск: `opentofu migrate from terraform`, `opentofu apt install`.
- **developer.hashicorp.com/terraform/tutorials** — официальные обучающие сценарии «Get Started» `[ПРОВЕРИТЬ актуальный путь, HashiCorp переносил tutorials]`. Поиск: `terraform tutorials get started aws`.
- **hashicorp.com** — раздел про лицензирование (BUSL 1.1 для Terraform начиная с 1.6) и FAQ. Поиск: `hashicorp license faq terraform busl`.
- **apt.releases.hashicorp.com** и **packages.opentofu.org** — официальные репозитории пакетов для установки CLI в Linux. Поиск: `install terraform ubuntu apt hashicorp repository`, `install opentofu ubuntu packages.opentofu.org`.
- **cloud.yandex.ru / yandex.cloud (документация)** — раздел про Terraform-провайдер Yandex Cloud (пригодится к уроку 100). Поиск: `yandex cloud terraform provider getting started`.
- Документация по провайдеру Proxmox: `bpg/proxmox` `[ПРОВЕРИТЬ имя и статус провайдера на 2026 год]`. Поиск: `terraform provider proxmox bpg`.
