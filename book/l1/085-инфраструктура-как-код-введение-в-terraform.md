# Инфраструктура как код: введение в Terraform

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в Service Desk:**

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Добрый день! Вчера ссылка http://10.10.10.30:8080 открывала тестовую страницу, сегодня браузер пишет "сайт недоступен". Мне туда резюме загружать, помогите.»
> **Срочность:** Средняя

Ты идёшь на UBU01 и видишь: контейнер с тестовой страницей живой, но слушает порт 8081, а не 8080. Кто-то «поправил конфиг руками» — и никто не помнит, кто и когда. Восстановил — через неделю та же история.

Именно от этой боли лечит **Infrastructure as Code (IaC, «инфраструктура как код»)**: описание того, как должна выглядеть инфраструктура, лежит в текстовом файле в Git, а специальная программа приводит реальность к этому описанию. Terraform — самый распространённый в мире инструмент IaC.

После этой главы ты будешь уметь: понимать модель «провайдер + ресурс + состояние», установить Terraform на Ubuntu, написать первый конфиг из нескольких ресурсов, безопасно прогонять `init → fmt → validate → plan → apply`, читать план изменений, находить расхождения между кодом и реальностью и объяснять на собеседовании, чем Terraform отличается от Ansible. Глубже (модули, remote state, workspaces) — это уже L2/DevOps, здесь только фундамент.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Terraform — один бинарник, написанный на Go. Никакой базы данных, никакого агента на целевых машинах не нужно. Работает по схеме «бинарник + файлы в каталоге проекта».

Из чего он состоит:

- **HCL (HashiCorp Configuration Language)** — язык описания. Файлы `*.tf` с блоками `terraform`, `provider`, `resource`, `variable`, `output`.
- **Core** — ядро: строит граф зависимостей, вычисляет разницу между «что в коде» и «что в state», составляет план.
- **Провайдеры (providers)** — отдельные плагины-бинарники, каждый умеет говорить с API конкретной системы: Docker, VMware vSphere, Yandex Cloud, Active Directory и т.д. Ставятся из реестра `registry.terraform.io` при `terraform init`.
- **State (состояние)** — файл `terraform.tfstate` (по умолчанию JSON в каталоге проекта). Это «память» Terraform: какие ресурсы он уже создал и какие у них идентификаторы в реальной системе. **Без state Terraform не знает, что уже существует.**

```
   твой каталог проекта
   ┌──────────────────────────────┐
   │ main.tf  variables.tf        │   HCL — что должно быть
   │ outputs.tf  terraform.tfvars │
   └──────────────┬───────────────┘
                  │ читает
              ┌───▼────┐   сравнивает   ┌──────────────┐
              │  Core  │◄──────────────►│ tfstate      │
              │ / план │                │ что создано  │
              └───┬────┘                └──────────────┘
                  │ gRPC (локально)
        ┌─────────▼─────────┐
        │ provider docker   │  → unix:///var/run/docker.sock  (локально)
        │ provider vsphere  │  → https://vcenter:443
        │ provider yandex   │  → https://api.cloud.yandex.net:443
        └───────────────────┘
```

Что важно для L1:

- Terraform **декларативный**: ты пишешь «должен быть контейнер с именем X на порту 8080», а не «выполни шаг 1, шаг 2». Он сам решит, создать, изменить или удалить.
- Он **не настраивает внутри** ОС: не ставит пакеты, не правит `sshd_config`. Это работа Ansible (урок 81). Классическое разделение: **Terraform — provisioning (создать), Ansible — configuration (настроить)**.
- В модель OSI Terraform не влезает: он живёт на уровне **management plane**, выше прикладного. Его сетевой трафик — обычный HTTPS/443 (или HTTPS на 8443/9443 у некоторых API), а для Docker — локальный unix-сокет.

**Отдельно про VMware Workstation на нашем стенде.** Официального провайдера для Workstation нет: `hashicorp/vsphere` работает с vCenter/ESXi, а не с десктопным гипервизором. Поэтому ВМ стенда (DC01, FS01, WIN11-CL01…) создаются руками, как раньше, — и это нормально. Terraform подключается там, где есть API: контейнеры в Docker на UBU01, облака из урока 84, файлы конфигурации. На стенде мы будем управлять контейнером и генерировать inventory для Ansible.

**Про лицензию.** В августе 2023 HashiCorp перевёл Terraform с MPL 2.0 на лицензию BUSL 1.1. Появился форк **OpenTofu** под эгидой Linux Foundation (MPL 2.0, полностью открытый). Команды у него те же — `tofu init`, `tofu plan` и т.д., синтаксис HCL совместим. Для РФ-2026 это существенно: OpenTofu — легальная и часто предпочтительная альтернатива, и упоминать его на собеседовании — плюс.

## 3. Аналогия из жизни

Представь прораба на стройке. У него есть **чертёж** (файлы `.tf`) — что должно стоять на площадке: два дома, забор, гараж. Есть **папка приёмки** (`terraform.tfstate`) — где записано, что уже построено и под какими номерами это числится в кадастре. И есть **бригады** (провайдеры) — каждая умеет своё: одни кладут кирпич, другие тянут электричество.

Прораб не «выполняет команды по списку». Он приходит на площадку, сверяет чертёж с папкой приёмки и с тем, что реально стоит, и говорит: «гараж уже есть — не трогаем; забор в чертеже сдвинули на метр — переставим; баню из чертежа убрали — снесём». Ты смотришь его смету (`terraform plan`), говоришь «согласен» — и он работает (`terraform apply`).

Где аналогия ломается: прораб помнит стройку сам и может разобраться без бумаг, а Terraform без state беспомощен — он честно скажет «ничего не построено» и начнёт строить второй такой же гараж поверх первого.

## 4. Практика и команды

Все команды выполняются **на UBU01** (10.10.10.30) из каталога проекта, если не сказано иное.

**1. Установка и проверка версии**

```bash
# официальный apt-репозиторий HashiCorp (Ubuntu 24.04 = noble)
wget -O- https://apt.releases.hashicorp.com/gpg | \
  sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | \
  sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install -y terraform
terraform version
```

```text
Terraform v1.x.y
on linux_amd64
```

Смотрим: версия 1.x (0.12 и ниже — ancient, в вакансиях не встречается). Если apt-репозиторий недоступен — скачай zip с `releases.hashicorp.com` через браузер на хосте, распакуй и положи бинарник в `/usr/local/bin/terraform` (`sudo install -m 0755 terraform /usr/local/bin/`). Версию в ссылке не выдумывай — бери актуальную со страницы релизов. Альтернатива без HashiCorp вообще — OpenTofu (`tofu`) из репозитория `packages.opentofu.org` `[ПРОВЕРИТЬ]`.

На Windows-хосте: `winget install --id Hashicorp.Terraform -e` `[ПРОВЕРИТЬ]` или `choco install terraform -y`, затем `where.exe terraform`.

**2. `terraform init` — подготовка каталога**

```bash
terraform init
```

```text
Initializing the backend...
Initializing provider plugins...
- Finding kreuzwerker/docker versions matching "~> 3.0"...
- Installing kreuzwerker/docker v3.x.y...
- Installed kreuzwerker/docker v3.x.y (signed by HashiCorp)
Terraform has created a lock file .terraform.lock.hcl to record the provider
selections it made above.
Terraform has been successfully initialized!
```

Что происходит: Terraform читает блок `required_providers`, качает плагины в `.terraform/` и фиксирует точные версии в `.terraform.lock.hcl`. **Lock-файл коммитят в Git**, каталог `.terraform/` — нет. `terraform init -upgrade` — обновить провайдеры до свежих версий в рамках ограничений.

**3. `terraform fmt` — форматирование кода**

```bash
terraform fmt -recursive      # отформатировать
terraform fmt -check -recursive   # только проверить (exit code 3, если не отформатировано)
```

Зачем: единый стиль, читаемые диффы в Git. Ставь в CI как проверку — как договаривались в уроке 83.

**4. `terraform validate` — синтаксис и типы**

```bash
terraform validate
```

```text
Success! The configuration is valid.
```

Проверяет только корректность HCL и типов переменных, **не** доступность API и не существование образов. Ошибку в имени аргумента поймает, недоступный Docker — нет.

**5. `terraform plan` — смета изменений**

```bash
terraform plan -out=tfplan
```

```text
Terraform will perform the following actions:

  # docker_container.web will be created
  + resource "docker_container" "web" {
      + name  = "vektor-web-test"
      + ports { + external = 8080  + internal = 80 }
    }

Plan: 2 to add, 0 to change, 0 to destroy.

Saved the plan to: tfplan
```

Символы: `+` создать, `~` изменить на месте, `-/+` пересоздать (destroy+create), `-` удалить. **Самое важное — последняя строка.** Всегда читай её и всегда смотри, нет ли в плане неожиданных `destroy`. Флаг `-out` сохраняет план, чтобы применить ровно то, что ты видел.

**6. `terraform show` — человекочитаемый план/состояние**

```bash
terraform show tfplan        # развернуть сохранённый план
terraform show               # показать текущее состояние
```

Удобно, когда вывод `plan` длинный и уехал за пределы экрана.

**7. `terraform apply` — привести реальность к коду**

```bash
terraform apply tfplan       # применить сохранённый план, без лишних вопросов
terraform apply              # заново показать план и спросить "Enter a value: yes"
```

```text
docker_container.web: Creating...
docker_container.web: Creation complete after 1s [id=3f9c...]
Apply complete! Resources: 2 added, 0 changed, 0 destroyed.

Outputs:

web_url = "http://10.10.10.30:8080"
```

`apply` без файла плана безопаснее для новичка: он ещё раз показывает дифф и требует ввести `yes`. `-auto-approve` — только в CI, где человек не сидит.

**8. `terraform state` — заглянуть в память**

```bash
terraform state list
terraform state show docker_container.web
```

```text
docker_container.web
docker_image.web
local_file.ansible_inventory

# docker_container.web:
resource "docker_container" "web" {
    id    = "3f9c..."
    name  = "vektor-web-test"
    ports { external = 8080  internal = 80 }
}
```

`state list` — быстрый ответ на вопрос «а что вообще Terraform считает своим?». Если ресурса тут нет, Terraform про него «не знает» и создаст дубль.

**9. `terraform output` — вытащить значения**

```bash
terraform output
terraform output -raw web_url
```

```text
web_url = "http://10.10.10.30:8080"
http://10.10.10.30:8080
```

`-raw` — без кавычек, удобно подставлять в скрипты и в ответ пользователю.

**10. `terraform apply -replace` — пересоздать ресурс**

```bash
terraform apply -replace=docker_container.web
```

Старый способ — `terraform taint docker_container.web` — устарел. Нужно, когда контейнер «залип»: проще удалить и создать заново, чем чинить.

**11. `terraform destroy` — снести всё из state**

```bash
terraform destroy
```

```text
Plan: 0 to add, 0 to change, 3 to destroy.
Do you really want to destroy all resources?
  Enter a value: yes
Destroy complete! Resources: 3 destroyed.
```

Удаляет **только то, что в state**. Ручные ресурсы, созданные мимо Terraform, останутся.

**12. Точечно убрать ресурс из-под управления**

```bash
terraform state rm docker_container.web
```

После этого контейнер живёт, но Terraform про него забыл. Опасно: следующий `apply` попробует создать второй. Применяй, только если точно понимаешь зачем.

**13. Быстрые проверки провайдеров**

```bash
terraform providers
terraform console   # интерактивно поэкспериментировать с выражениями, выход — Ctrl+D
```

**GUI-путь:** у Terraform его нет — это CLI-инструмент. «Графический интерфейс» — это Git-репозиторий с `.tf`-файлами, а истории `plan`/`apply` смотри в веб-интерфейсе GitLab CI (урок 83) или в pull request.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30), Ubuntu Server 24.04, SSH работает.
- На UBU01 установлен **Docker** из урока 82, пользователь `adm.student` (или твой sudo-пользователь) входит в группу `docker` (проверка: `docker ps` без sudo).
- Terraform **ещё не установлен** — ставим в шаге 2.
- DC01, FS01 и Windows-клиенты не нужны.
- Если урока 82 у тебя не было — сначала пройди его: без Docker этот сценарий не соберётся.

### Снапшот

В VMware Workstation: выдели UBU01 → **VM → Snapshot → Take Snapshot**, имя `before-l1-085`. На хосте в `stand-log.md` добавь строку «затронута UBU01». Снапшот делаем **при выключенной или работающей ВМ**, но лучше предварительно погасить её командой `sudo shutdown -h now` и снимать «холодный» снапшот — так предсказуемее.

### Шаги

1. **UBU01.** Входим по SSH: `ssh adm.student@10.10.10.30`. Проверяем Docker: `docker ps`. Ожидаемо — таблица с заголовками `CONTAINER ID  IMAGE ...` и, возможно, пустой список. Ошибка `permission denied` — добавь себя в группу `docker` и перелогинься.
2. **UBU01.** Ставим Terraform по инструкции из раздела 4 (команда 1). Ожидаемо: `terraform version` печатает `Terraform v1.x.y` и `on linux_amd64`.
3. **UBU01.** Готовим каталог: `mkdir -p ~/tf-lab && cd ~/tf-lab`. Сразу создаём `.gitignore` (мы уже умеем вести документацию в Git, урок 77):

```text
.terraform/
*.tfstate
*.tfstate.*
terraform.tfvars
tfplan
crash.log
```

Важно: `.terraform.lock.hcl` в `.gitignore` **не** добавляем — его коммитят.

4. **UBU01.** Создаём `versions.tf`:

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "docker" {
  host = "unix:///var/run/docker.sock"
}
```

Версии провайдеров `[ПРОВЕРИТЬ]` на `registry.terraform.io` перед запуском — они обновляются.

5. **UBU01.** Создаём `variables.tf`:

```hcl
variable "web_port" {
  type        = number
  description = "Внешний порт тестовой страницы на UBU01"
  default     = 8080
  validation {
    condition     = var.web_port > 1024 && var.web_port < 65535
    error_message = "Порт должен быть в диапазоне 1025-65534."
  }
}

variable "container_name" {
  type    = string
  default = "vektor-web-test"
}
```

6. **UBU01.** Создаём `main.tf`:

```hcl
resource "docker_image" "web" {
  name = "nginx:alpine"
}

resource "docker_container" "web" {
  name  = var.container_name
  image = docker_image.web.image_id

  ports {
    internal = 80
    external = var.web_port
  }

  restart = "unless-stopped"
}

resource "local_file" "ansible_inventory" {
  filename = "${path.module}/inventory.ini"
  content  = <<-EOT
    [web]
    ubu01 ansible_host=10.10.10.30
  EOT
}
```

7. **UBU01.** Создаём `outputs.tf`:

```hcl
output "web_url" {
  value = "http://10.10.10.30:${var.web_port}"
}
```

8. **UBU01.** `terraform init`. Ожидаемо: скачивание двух провайдеров и строка `Terraform has been successfully initialized!`. Если `registry.terraform.io` недоступен — `[ПРОВЕРИТЬ]` зеркало провайдеров или OpenTofu; сообщи об этом одной строкой в журнале стенда.
9. **UBU01.** `terraform fmt -recursive && terraform validate`. Ожидаемо: имена файлов, которые отформатированы (или пусто), затем `Success! The configuration is valid.`
10. **UBU01.** `terraform plan -out=tfplan`. Ожидаемо: три ресурса к созданию, финальная строка `Plan: 3 to add, 0 to change, 0 to destroy.` и `Saved the plan to: tfplan`.
11. **UBU01.** `terraform apply tfplan`. Ожидаемо: `Apply complete! Resources: 3 added...` и вывод `web_url = "http://10.10.10.30:8080"`.
12. **UBU01.** Проверяем руками: `docker ps --format '{{.Names}}\t{{.Ports}}'` → `vektor-web-test  0.0.0.0:8080->80/tcp`; `cat inventory.ini` → две строки с секцией `[web]`; `curl -sI http://127.0.0.1:8080` → первая строка `HTTP/1.1 200 OK` (у nginx:alpine).
13. **UBU01.** Меняем порт: `terraform apply -var="web_port=8081"`. Ожидаемо: `1 to change` — контейнер **пересоздаётся** (`-/+`), потому что смена порта требует замены. Затем `terraform output -raw web_url` → `http://10.10.10.30:8081`.
14. **UBU01.** Возвращаем как было: `terraform apply -var="web_port=8080"`. Ожидаемо снова `1 to change` и рабочий `curl` на 8080.
15. **UBU01.** `terraform destroy`. Ожидаемо: `Destroy complete! Resources: 3 destroyed.` `docker ps` пуст, `inventory.ini` исчез. Записываем результат в `stand-log.md`.

### Проверка

- [ ] `terraform version` на UBU01 отвечает и показывает `linux_amd64`
- [ ] `terraform validate` возвращает `Success!`
- [ ] `terraform state list` показывает ровно три адреса ресурсов
- [ ] `docker ps` показывает `vektor-web-test` с портом `8080->80/tcp`
- [ ] `curl -sI http://10.10.10.30:8080` отдаёт `HTTP/1.1 200 OK`
- [ ] `terraform output -raw web_url` печатает `http://10.10.10.30:8080`
- [ ] `inventory.ini` создан рядом с `.tf`-файлами и содержит секцию `[web]`
- [ ] после `terraform destroy` в `state list` пусто, контейнера нет
- [ ] `terraform fmt -check -recursive` проходит без ошибок
- [ ] в `.gitignore` есть `*.tfstate`, а `.terraform.lock.hcl` не игнорируется

### Сломай и почини

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Вчера ссылка http://10.10.10.30:8080 открывала тестовую страницу, сегодня браузер пишет "сайт недоступен". Мне туда резюме загружать.»
> **Срочность:** Средняя

**Воспроизводим поломку.** На UBU01 в `~/tf-lab` возвращаем стенд в рабочее состояние (`terraform apply`) и «случайно» меняем порт, как в шаге 13, но **не сообщаем пользователю**: `terraform apply -var="web_port=8081"`. Снаружи сайт на 8080 перестал отвечать — ровно как в тикете.

**Диагностика по шагам.**

1. Уточняем у пользователя: ссылка та же? когда работало? ошибка «не удаётся подключиться» или «404»? Это сразу делит проблему на «порт/сеть» и «приложение».
2. На UBU01: `ss -tlnp | grep -E '8080|8081'`. Видим, что слушается только `*:8081`. Порт 8080 никто не слушает — уже половина ответа.
3. `docker ps` → контейнер `vektor-web-test` жив, но проброс `0.0.0.0:8081->80/tcp`. Значит, проблема не во «сайте», а в публикации порта.
4. Проверяем, что дело не в файрволе: `sudo ufw status` (на стенде обычно `inactive`; если включён — `sudo ufw allow 8080/tcp` на стенде допустимо, в проде так делать не спешим).
5. Идём в `~/tf-lab`: `git log --oneline -5` (если каталог в Git) и `git diff`. Видим правку `web_port`.
6. `terraform state show docker_container.web` → `external = 8081`. Подтверждаем: изменение сделано через Terraform, а не руками.
7. `terraform output` → `http://10.10.10.30:8081`. Расхождение с документацией для пользователей очевидно.

**Решение.** Возвращаем код к согласованному значению: `terraform apply -var="web_port=8080"` (в правильной жизни — не через `-var` в командной строке, а коммитом в `terraform.tfvars`/Git, чтобы состояние было воспроизводимо). Проверяем: `curl -sI http://10.10.10.30:8080` → `200 OK`.

**Ответ пользователю:**

> Мария, добрый день! Тестовая страница снова доступна: http://10.10.10.30:8080. Причина — вчера при обновлении конфигурации поменялся порт публикации сервиса, и старый адрес перестал работать. Конфигурацию вернули, ссылка прежняя. Если снова не откроется — напишите, пожалуйста, точный текст ошибки и время.

**Закрываем заявку:** категория «Веб-сервис → недоступность», решение «откат изменения конфигурации (порт)», в комментарии — ссылка на коммит/план. Профилактика: добавить в чек-лист изменений пункт «после apply проверить URL из `terraform output` и предупредить пользователей».

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен Terraform (пакет из apt-репозитория HashiCorp либо бинарник в `/usr/local/bin`), добавлен apt-репозиторий `apt.releases.hashicorp.com`.
- **UBU01:** каталог `~/tf-lab` — `versions.tf`, `variables.tf`, `main.tf`, `outputs.tf`, `.gitignore`, `.terraform.lock.hcl`, каталог `.terraform/`.
- **UBU01:** в Docker работал контейнер `vektor-web-test` (образ `nginx:alpine`), порт 8080 на UBU01. После `destroy` контейнер и `inventory.ini` удалены — для следующих глав отмечаем, что Docker на UBU01 остался рабочим.
- **DC01/FS01/Windows-клиенты:** без изменений.
- Запись в `stand-log.md`: «Урок 85. Terraform на UBU01. Каталог ~/tf-lab. Рабочее состояние — destroy.»

### Откат

Если что-то пошло не так (сломался Docker, `apply` удалил лишнее, Terraform качает провайдеров в бесконечность):

1. Мягкий путь: `terraform destroy` в `~/tf-lab`, затем `sudo apt remove terraform`, `rm -rf ~/tf-lab` — стенд почти как был.
2. Жёсткий путь: в VMware Workstation на UBU01 — **VM → Snapshot → Snapshot Manager**, выбрать `before-l1-085`, кнопка **Revert**. ВМ вернётся ровно к состоянию перед уроком; всё, что было сделано после, исчезнет.
3. Если Docker после откатов «забыл» образы — это нормально, `docker pull` восстановит.

## 6. Ошибки новичков и безопасность

- **Закоммитить `terraform.tfstate` в Git.** → В репозитории оказываются идентификаторы ресурсов и часто пароли в открытом виде; у коллег конфликтующие state. → В `.gitignore` `*.tfstate*`, а для командной работы — remote backend (S3-совместимое хранилище, GitLab-managed state). На стенде — просто не публиковать.
- **`terraform destroy` без чтения плана.** → Снести рабочую инфраструктуру в один `yes`. → Всегда читать последнюю строку плана и список `-` перед подтверждением; в проде — только через ревью и пайплайн.
- **Править ресурсы руками после `apply`.** → Дрейф конфигурации: реальность разошлась с кодом, следующий `apply` «неожиданно» пересоздаёт контейнер. → Правим только код, руками — никогда; дрейф лечим `terraform plan` и `-replace`.
- **Хардкодить пароли и ключи в `.tf`.** → Секреты уезжают в Git и в state. → Переменные + секретное хранилище (Vault, GitLab CI variables), `.tfvars` — в `.gitignore`.
- **`apply` без `-out` после «примерно такого» плана.** → Применяется не то, что ты смотрел пять минут назад. → `plan -out=tfplan` → `apply tfplan`.
- **Удалить/потерять state при живых ресурсах.** → Terraform считает, что ничего нет, и создаёт дубли (второй контейнер, второй диск — и счёт в облаке). → Бэкап state, remote backend с версионированием; восстановление через `terraform import` (это уже L2).
- **`init` без lock-файла у разных людей.** → У тебя провайдер v3.0, у коллеги v3.5, поведение разное. → `.terraform.lock.hcl` коммитим, `init` без `-upgrade` в обычной работе.
- **Открыть Docker API в сеть на 2375/TCP без TLS.** → Любой в сети управляет контейнерами от root. → Только локальный unix-сокет; удалённо — 2376 с TLS-сертификатами, а в бою — через ограниченный доступ.

**Безопасность.** На стенде мы работаем от учебного пароля `Lab#2026-vektor` и через локальный unix-сокет Docker — это допустимо только в лаборатории. В реальной среде state-файл приравнивается к секрету (права `0600`, не в Git, шифрование в backend), а Terraform запускается под отдельной сервисной учётной записью с минимально нужными правами, а не под `adm.student`-администратором домена.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Что такое Terraform и чем он отличается от Ansible?**

Terraform — инструмент Infrastructure as Code, который описывает **целевое состояние** инфраструктуры декларативно и приводит к нему реальность, храня состояние в `tfstate`. Ansible — система управления конфигурацией: он **процедурно** проходит по задачам плейбука и настраивает то, что уже существует, обычно без постоянного state. На практике: Terraform создаёт ВМ, сети, диски, DNS-записи; Ansible ставит на созданные ВМ пакеты и правит конфиги. Их часто используют вместе.

*Что хочет услышать интервьюер:* понимание «provisioning vs configuration management» и что оба живут в Git.
*Красный флаг:* «Terraform и Ansible — одно и то же, только команды разные».

**2. Что такое state и что будет, если его потерять?**

`terraform.tfstate` — файл (по умолчанию JSON), где Terraform хранит соответствие «адрес ресурса в коде → реальный идентификатор в системе». При каждом запуске он сравнивает код, state и реальность и строит план. При потере state Terraform считает, что ничего не создано, и попытается создать всё заново — дубли ресурсов, а в облаке это ещё и деньги. Поэтому state бэкапят, кладут в remote backend с версионированием и блокировкой, а не держат на ноутбуке. Восстановление — `terraform import` по одному ресурсу.

*Что хочет услышать интервьюер:* что state — не «кэш», а критичный артефакт, и что в нём бывают секреты.
*Красный флаг:* «state можно удалить, Terraform всё равно разберётся».

**3. Зачем `terraform plan` и зачем флаг `-out`?**

`plan` — сухой прогон: показывает, что именно будет создано (`+`), изменено (`~`), пересоздано (`-/+`) или удалено (`-`). Это возможность поймать опасное изменение до применения. `-out=tfplan` сохраняет ровно этот план в файл, и `terraform apply tfplan` применяет именно его, без пересчёта — гарантия, что применено то, что проверено.

*Что хочет услышать интервьюер:* дисциплина «сначала план, потом применение», особенно в CI.
*Красный флаг:* «plan нужен только для красоты, apply сам всё покажет».

**4. Что такое провайдер и как Terraform с ним общается?**

Провайдер — отдельный плагин, реализующий API конкретной системы. Terraform скачивает его при `init` из реестра, фиксирует версию в lock-файле и общается с ним локально по gRPC, а провайдер уже ходит в API системы: Docker — через unix-сокет, vSphere и облака — по HTTPS/443. Один конфиг может использовать несколько провайдеров одновременно.

*Что хочет услышать интервьюер:* что сам Terraform «не умеет» ни Docker, ни облака — всё делает провайдер.
*Красный флаг:* «Terraform сам подключается к серверам по SSH и всё настраивает».

**5. Terraform или OpenTofu — в чём разница?**

После перехода Terraform на лицензию BUSL 1.1 в 2023 году сообщество форкнуло его как OpenTofu под MPL 2.0 (Linux Foundation). Синтаксис HCL и CLI-команды совместимы (`tofu init/plan/apply`), большинство провайдеров работает. Различия — в лицензии, управлении проектом и части новых функций. Для компании это вопрос юридический и организационный, а не технический.

*Что хочет услышать интервьюер:* что ты знаешь о лицензионной ситуации и не считаешь её «деталью».
*Красный флаг:* «OpenTofu — пиратская копия Terraform».

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — раздел **Terraform → Documentation → Terraform Language** (основы HCL, ресурсы, переменные, outputs). Поиск: `terraform language resources variables outputs`.
- **developer.hashicorp.com** — **Terraform → CLI** (команды `init`, `plan`, `apply`, `state`, `fmt`, `validate`). Поиск: `terraform cli commands init plan apply`.
- **developer.hashicorp.com** — **Terraform → State** (что такое state, remote backend, блокировки). Поиск: `terraform state remote backend`.
- **registry.terraform.io** — реестр провайдеров; страницы `kreuzwerker/docker` и `hashicorp/local` с примерами и актуальными версиями. Поиск: `terraform registry docker provider kreuzwerker`.
- **opentofu.org** — сайт и документация OpenTofu, раздел **Docs**. Поиск: `opentofu documentation getting started`.
- **docs.docker.com** — **Engine → Security / API** (почему не открывают 2375/tcp наружу). Поиск: `docker engine api protect the daemon socket`.
- **learn.microsoft.com** — обзор IaC в контексте Azure, полезен как общая теория. Поиск: `what is infrastructure as code microsoft learn`.

Всё, что касается точных версий Terraform, провайдеров и команд установки пакетов, помечено `[ПРОВЕРИТЬ]` — сверяй на момент чтения.
