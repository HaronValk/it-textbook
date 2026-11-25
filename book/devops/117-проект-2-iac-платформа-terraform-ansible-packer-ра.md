# Проект №2: IaC-платформа — Terraform + Ansible + Packer, развёртывание инфраструктуры в облаке и на on-premise одной командой

## 1. Введение (зачем это нужно в реальной работе)

**Тикет в поддержку.**

> **Пользователь:** sidorov (склад)
> **ВМ:** —
> **Что написал:** «Нам под интеграцию с новой WMS нужен тестовый стенд: веб-приложение, база, конфиг под наш склад. В прошлый раз вы делали вручную три дня, потом что-то отвалилось и никто не помнил, как собирали. Сделайте так, чтобы можно было поднять заново за час и повторить один-в-один. И если понадобится такой же стенд в облаке — тоже.»
> **Срочность:** средняя (к понедельнику)

Вот это «поднять заново и повторить один-в-один» — и есть суть IaC-платформы. В работе DevOps-инженера она встречается постоянно: тестовые окружения под фичи, стенды под нагрузочные тесты, восстановление после аварии (см. урок 122), временные среды под демо, миграция в облако при росте нагрузки или наоборот — уход с зарубежного облака на отечественное. Каждый раз, когда инфраструктуру собирают руками «по памяти», компания получает снежинку: уникальную, невоспроизводимую и непонятную конфигурацию.

После этой главы ты будешь уметь: собирать образы машин кодом (Packer), описывать инфраструктуру кодом (Terraform) так, чтобы один и тот же код работал и на домашнем KVM, и в облаке, доводить машины до нужного состояния конфигурацией (Ansible) и связывать всё это в одну команду `make up`, которую можно запускать в CI. Плюс понимать, где здесь ловушки: state, дрейф конфигурации, секреты, версии.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

IaC-платформа — это три инструмента, у каждого своя зона ответственности. Путать их зоны — главная ошибка новичков.

| Слой | Инструмент | Отвечает на вопрос | Что создаёт |
|------|-----------|--------------------|-------------|
| Образ | **Packer** | «Из чего состоит машина на старте?» | qcow2/OVA/AMI/образ облака |
| Ресурсы | **Terraform** | «Какие машины, сети, диски, DNS-записи существуют?» | объекты у провайдера + state |
| Конфигурация | **Ansible** | «Что настроено внутри машины?» | пакеты, файлы, сервисы, пользователи |

```text
┌────────── один вход: make up ENV=<onprem|cloud> ──────────┐
│                                                           │
│  Packer ──► образ ubuntu-base:<version> (qcow2 / image)   │
│     │            артефакт с версией в имени               │
│     ▼                                                     │
│  Terraform ──► ВМ + сеть + ключи + cloud-init             │
│     │            state в GitLab, outputs: имя + IP        │
│     ▼                                                     │
│  Ansible ──► пакеты, nginx, конфиги, systemd, юзеры       │
│     │            идемпотентно, --check показывает дрейф   │
│     ▼                                                     │
│  готовый стенд + запись в stand-log.md                    │
└───────────────────────────────────────────────────────────┘
```

**Terraform** работает по декларативной модели: ты пишешь, *что должно быть*, а не *как это сделать*. `terraform plan` строит граф зависимостей ресурсов, сравнивает желаемое состояние с реальным и с **state** — файлом, где Terraform помнит, какие объекты он создал и с какими ID. State — это не «конфиг», это карта соответствия «блок в HCL → реальный объект у провайдера». Поэтому state нельзя терять и нельзя держать в Git рядом с кодом: в нём бывают секреты (пароли БД, ключи), а при параллельном запуске двух `apply` без блокировки он ломается. Рабочая схема — remote backend с блокировкой (S3/MinIO, GitLab HTTP backend, Terraform Cloud) [ПРОВЕРИТЬ актуальный набор бэкендов в вашей версии].

Провайдер — плагин, который превращает HCL в вызовы API: `yandex-cloud/yandex`, `bpg/proxmox`, `dmacvicar/libvirt`, `vmware/vsphere`, `vkcs/vkcs` [ПРОВЕРИТЬ имя провайдера VK Cloud]. Один и тот же модуль «сделай N машин» можно написать один раз, а окружения (`envs/onprem`, `envs/cloud`) будут различаться только провайдером и переменными. Это и есть «одной командой в облаке и на on-premise».

**Ansible** — агентless: заходит по SSH (порт 22) и приводит машину к состоянию, описанному в playbook. Ключевое свойство — **идемпотентность**: повторный запуск ничего не меняет, если уже всё в нужном виде. Отсюда `--check --diff` — режим «покажи, что бы ты изменил», главный инструмент поиска дрейфа.

**Packer** собирает образ: берёт ISO или базовый образ, гоняет provisioner'ы (shell/ansible), выключает машину и отдаёт артефакт. Идея **immutable infrastructure**: машина после сборки не «дообучается» месяцами — она пересоздаётся из нового образа. Ansible тогда делает только то, что действительно привязано к окружению (IP, сертификаты, конфиг бэкенда).

**Где это в модели OSI/слоях системы:** Terraform общается с control-plane гипервизора или облака по HTTPS (443), Ansible — по SSH (22), Packer по HTTP отдаёт автоустановщику (`http_directory`, случайный порт на хосте сборки), собранная ВМ при первом старте тянет cloud-init-данные (NoCloud/метаданные провайдера). Всё это — прикладной уровень; ценность в том, что любой из этих каналов можно логировать и проверять.

Отдельная реалия РФ 2026: после смены лицензии Terraform на BUSL 1.1 появился открытый форк **OpenTofu** (MPL 2.0, под эгидой Linux Foundation). Команды (`tofu init/plan/apply`) совместимы, реестр провайдеров — `registry.opentofu.org`. В части компаний выбирают OpenTofu именно из-за лицензии; в резюме полезно иметь оба.

## 3. Аналогия из жизни

Представь, что ты строишь не ИТ-стенд, а поселок под ключ. **Packer** — это завод ЖБИ: он делает типовые блоки и панели по чертежу, штампует их партией с номером (`ubuntu-base:1.0`). Панели всегда одинаковые, брак ловится на заводе, а не на стройке. **Terraform** — это проект и прораб с планом участка: сколько домов, где забор, где подъездная дорога, какие коммуникации подведены. Прораб ведёт журнал (state): «дом №3 — вот этот, на участке таком-то». Если журнал потерять, прораб не узнает свои дома и попытается построить дубли. **Ansible** — отделочная бригада, которая приходит в уже стоящий дом и делает внутри: проводка, плитка, покраска. Бригада приходит по расписанию, смотрит на список работ и делает только то, чего ещё нет — если плитка уже лежит, второй раз её не кладут.

Аналогия перестаёт работать в одном месте: в жизни бригаду зовут один раз, а в ИТ Ansible запускают регулярно и он каждый раз проверяет весь дом целиком — и это норма, а не переделка.

## 4. Практика и команды

Ниже — рабочий минимум. Версии инструментов фиксируй (`required_version`, `version` у провайдеров, тег образа), иначе «одна команда» через полгода даст другой результат.

**1. `packer init` + `packer validate` + `packer build`** — собрать образ.

```bash
cd packer
packer init .
packer validate -var-file=versions.pkrvars.hcl .
packer build -var-file=versions.pkrvars.hcl -force .
```

`init` скачивает плагины из `required_plugins` (аналог `terraform init`). `validate` проверяет синтаксис HCL и обязательные переменные, но не собирает. `build` запускает сборку; `-force` нужен, если артефакт с таким именем уже есть. Типичный вывод:

```text
qemu.ubuntu_base: output will be in this color.
==> qemu.ubuntu_base: Retrieving ISO
==> qemu.ubuntu_base: Starting HTTP server on port 8265
==> qemu.ubuntu_base: Starting VM, booting from CD-ROM
==> qemu.ubuntu_base: Waiting for SSH to become available...
==> qemu.ubuntu_base: Provisioning with shell script: scripts/01-base.sh
Build 'qemu.ubuntu_base' finished after 7 minutes 12 seconds.
--> qemu.ubuntu_base: build/ubuntu-base-1.0/ubuntu-base-1.0.qcow2
```

Смотрим: имя артефакта содержит версию, время сборки адекватное (если 40+ минут — скорее всего упал на `accelerator = "tcg"`, нет KVM), и что SSH-этап прошёл, а не «таймаут автоустановки».

**2. `terraform init -backend-config`** — подключить удалённый state.

```hcl
# envs/onprem/backend.hcl
address        = "https://10.10.10.50/api/v4/projects/<PROJECT_ID>/terraform/state/vektor-iac-onprem"
lock_address   = "https://10.10.10.50/api/v4/projects/<PROJECT_ID>/terraform/state/vektor-iac-onprem/lock"
unlock_address = "https://10.10.10.50/api/v4/projects/<PROJECT_ID>/terraform/state/vektor-iac-onprem/lock"
lock_method    = "POST"
unlock_method  = "DELETE"
username       = "adm.student"
password       = "<personal access token>"
```

```bash
terraform -chdir=envs/onprem init -input=false -backend-config=backend.hcl
```

Блок `terraform { backend "http" {} }` остаётся пустым, параметры приходят из файла — так один код живёт с разными бэкендами. GitLab-бэкенд даёт и хранение, и блокировку [ПРОВЕРИТЬ на своей версии GitLab, что Terraform state включён в проекте].

**3. `terraform fmt -check -recursive` и `validate`** — гигиена в CI.

```bash
terraform fmt -check -recursive -diff
terraform -chdir=envs/onprem validate
```

`fmt -check` возвращает код 1, если файлы не отформатированы, `-diff` показывает правки. `validate` не ходит в API — значит, работает в CI без кредов. Именно эти две команды ставь первым шагом пайплайна: они ловят 80% глупых ошибок за секунды.

**4. `terraform plan -out=tfplan -detailed-exitcode`** — посмотреть, что будет, и остановить пайплайн при неожиданностях.

```bash
terraform -chdir=envs/onprem plan -input=false -out=tfplan -detailed-exitcode
echo "exit=$?"   # 0 — изменений нет, 1 — ошибка, 2 — есть изменения
terraform -chdir=envs/onprem show tfplan | head -40
```

Фрагмент плана:

```text
  # libvirt_domain.app will be created
  + resource "libvirt_domain" "app" {
      + name   = "app-onprem-01"
      + memory = 2048
      + vcpu   = 2
    }

Plan: 2 to add, 0 to change, 0 to destroy.
```

Смотрим на строку `Plan:` и на символы `+ ~ -`: `+` создать, `~` изменить на месте, `-/+` пересоздать (уничтожить и создать). Пересоздание боевой ВМ — красный флаг, читай причину в атрибутах.

**5. `terraform apply tfplan`** — применить ровно то, что видел.

```bash
terraform -chdir=envs/onprem apply -input=false tfplan
terraform -chdir=envs/onprem output
```

Применение сохранённого плана гарантирует, что между ревью и запуском ничего не поменялось. `-auto-approve` без файла плана — только в лабораторной.

**6. `terraform output -json` → инвентарь Ansible.** Связка слоёв, самое ценное место.

```bash
{ echo "[app]";
  terraform -chdir=envs/onprem output -json app_hosts \
    | jq -r 'to_entries[] | "\(.key) ansible_host=\(.value) ansible_user=ubuntu"';
} > ansible/inventory/onprem.ini
cat ansible/inventory/onprem.ini
```

```text
[app]
app-onprem-01 ansible_host=192.168.122.45 ansible_user=ubuntu
```

Никаких ручных списков хостов: IP всегда берётся из фактического результата Terraform.

**7. `terraform state list` и `state show`** — понять, чем Terraform владеет.

```bash
terraform -chdir=envs/onprem state list
terraform -chdir=envs/onprem state show libvirt_domain.app
```

```text
libvirt_cloudinit_disk.commoninit
libvirt_domain.app
libvirt_volume.app_disk
```

Если ресурс есть в облаке, но не в этом списке — Terraform его «не видит» и создаст дубль.

**8. `terraform import`** — принять существующий объект под управление.

```bash
terraform -chdir=envs/onprem import libvirt_domain.app app-onprem-01
terraform -chdir=envs/onprem plan   # должно быть без изменений
```

После импорта обязательно проверь план: если он хочет что-то пересоздать — значит, HCL не совпадает с реальностью.

**9. `terraform force-unlock`** — снять застрявшую блокировку.

```bash
terraform -chdir=envs/onprem force-unlock 8f3c1a2e-...
```

ID берётся из сообщения `Error acquiring the state lock`. Но сначала убедись, что тот, кто держит лок, реально умер (упавший runner), а не работает прямо сейчас — иначе получишь два параллельных apply.

**10. `ansible-inventory --graph` и `ansible -m ping`** — проверить связность до конфигурации.

```bash
ansible-inventory -i ansible/inventory/onprem.ini --graph
ansible -i ansible/inventory/onprem.ini app -m ping
```

```text
app-onprem-01 | SUCCESS => {
    "ping": "pong"
}
```

`--graph` покажет дерево групп; если хост попал не в ту группу — дальше идти бессмысленно.

**11. `ansible-playbook --check --diff` и `--limit`** — прогон без изменений и точечно.

```bash
ansible-playbook -i ansible/inventory/onprem.ini ansible/playbooks/site.yml --check --diff
ansible-playbook -i ansible/inventory/onprem.ini ansible/playbooks/site.yml --limit app-onprem-01 --tags nginx
```

`--check` («dry run») показывает задачи, которые изменили бы систему; `--diff` печатает диффы файлов. `--limit` сужает до одной машины, `--tags` — до части ролей.

**12. Одна команда — Makefile.**

```make
ENV    ?= onprem
TF_DIR := envs/$(ENV)
INV    := ansible/inventory/$(ENV).ini

.PHONY: image init plan apply inventory configure up destroy

image:
	cd packer && packer init . && packer build -force -var-file=versions.pkrvars.hcl .

init:
	terraform -chdir=$(TF_DIR) init -input=false -backend-config=backend.hcl

plan:
	terraform -chdir=$(TF_DIR) plan -input=false -out=tfplan

apply:
	terraform -chdir=$(TF_DIR) apply -input=false tfplan

inventory:
	@mkdir -p ansible/inventory
	@{ echo "[app]"; \
	   terraform -chdir=$(TF_DIR) output -json app_hosts \
	     | jq -r 'to_entries[] | "\(.key) ansible_host=\(.value) ansible_user=ubuntu"'; } > $(INV)

configure: inventory
	ansible-playbook -i $(INV) ansible/playbooks/site.yml --diff

up: init plan apply configure

destroy:
	terraform -chdir=$(TF_DIR) destroy -auto-approve
```

```bash
make up ENV=onprem     # весь стенд
make up ENV=cloud      # тот же код, другой провайдер
make destroy ENV=onprem
```

## 5. Лабораторная работа

### Предусловия

- **GIT01 (10.10.10.50)** — GitLab CE + runner, есть `git`, доступ по SSH. Это control node: здесь лежат репозиторий `iac-platform` и установленные terraform/packer/ansible.
- **UBU01 (10.10.10.30)** — SSH, root/sudo, и главное — **вложенная виртуализация** (в VMware Workstation у ВМ должен быть включён флажок «Virtualize Intel VT-x/EPT»). UBU01 будет on-prem гипервизором (KVM + libvirt).
- **WEB01 (10.10.20.10)** — нужен только для проверки, что Ansible вообще ходит по стенду; в основной сценарий не входит.
- **HARBOR01 (10.10.10.55)** — опционально, чтобы хранить артефакты и образы с версией.
- Windows-часть стенда (DC01/FS01) **не нужна**, на DevOps-треке её может не быть — ни одна команда ниже от неё не зависит.
- Учётка на UBU01/GIT01: `adm.student`, пароль стенда `Lab#2026-vektor`. Если у тебя на этих машинах другой пользователь — подставь своего, остальное не меняется.

Если вложенная виртуализация недоступна — лабораторную всё равно проходишь: в Packer ставь `accelerator = "tcg"` (медленно, 30–60 минут) и в Terraform используй `qemu:///system` локально на GIT01 вместо `qemu+ssh://...`. Логика платформы та же.

### Снапшот

В VMware Workstation для **GIT01**, **UBU01** и **WEB01**: правый клик по ВМ → *Snapshot* → *Take Snapshot*, имя — `before-devops-117`. Одинаковое имя на всех трёх: так потом одним действием понятно, куда откатываться.

### Шаги

1. **GIT01.** Создай проект `vektor/iac-platform` (Private), скопируй `PROJECT_ID` из *Settings → General*. Структура репозитория:
   ```text
   iac-platform/
   ├── Makefile
   ├── packer/{ubuntu-base.pkr.hcl,http/,scripts/}
   ├── terraform/modules/vm/{main.tf,variables.tf,outputs.tf}
   ├── terraform/envs/onprem/{main.tf,backend.hcl,terraform.tfvars}
   ├── terraform/envs/cloud/{main.tf,backend.hcl,terraform.tfvars}
   └── ansible/{ansible.cfg,playbooks/site.yml,roles/nginx/}
   ```
   Ожидаемо: пустой проект с веткой `main`, `git clone` работает.

2. **GIT01.** Ставим инструменты с фиксацией версий (не «последние», иначе через месяц другой результат):
   ```bash
   sudo apt-get update && sudo apt-get install -y jq make unzip
   # версии ниже — [ПРОВЕРИТЬ актуальные на момент прохождения]
   TERRAFORM_VER=1.9.8; PACKER_VER=1.11.2
   curl -fsSLo tf.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VER}/terraform_${TERRAFORM_VER}_linux_amd64.zip"
   sudo unzip -o tf.zip -d /usr/local/bin && rm tf.zip
   curl -fsSLo pk.zip "https://releases.hashicorp.com/packer/${PACKER_VER}/packer_${PACKER_VER}_linux_amd64.zip"
   sudo unzip -o pk.zip -d /usr/local/bin && rm pk.zip
   sudo apt-get install -y ansible-core
   terraform version && packer version && ansible --version | head -1
   ```
   Ожидаемо: три строки версий. Если корпоративный прокси/санкционные ограничения на `releases.hashicorp.com` — легальная альтернатива: пакеты из зеркал или OpenTofu с `github.com/opentofu/opentofu/releases` (MPL 2.0) [ПРОВЕРИТЬ доступность из вашей сети].

3. **GIT01.** Настрой SSH-доступ к UBU01 без пароля (по ключу) и проверь удалённый libvirt:
   ```bash
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519   # если ещё нет
   ssh-copy-id adm.student@10.10.10.30
   ssh adm.student@10.10.10.30 'sudo apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients; sudo virsh net-list --all'
   ```
   Ожидаемо на UBU01: сеть `default` в состоянии `active` (если `inactive` — `sudo virsh net-start default && sudo virsh net-autostart default`).

4. **GIT01.** Пишешь `packer/ubuntu-base.pkr.hcl` (сокращённо, ключевое — версия в имени артефакта и HTTP-раздача автоустановки):
   ```hcl
   packer {
     required_plugins {
       qemu = { source = "github.com/hashicorp/qemu", version = ">= 1.0" } # [ПРОВЕРИТЬ]
     }
   }
   variable "image_version" { type = string }
   source "qemu" "ubuntu_base" {
     iso_url          = "http://<внутреннее-зеркало>/ubuntu-24.04-live-server-amd64.iso"
     iso_checksum     = "file:http://<внутреннее-зеркало>/SHA256SUMS"      # [ПРОВЕРИТЬ]
     output_directory = "build/ubuntu-base-${var.image_version}"
     format           = "qcow2"
     disk_size        = "10240M"
     accelerator      = "kvm"
     memory           = 2048
     cpus             = 2
     http_directory   = "http"
     ssh_username     = "ubuntu"
     ssh_password     = "Lab#2026-vektor"      # только стенд! в бою — ключ/cloud-init
     shutdown_command = "sudo shutdown -P now"
     boot_command     = ["<esc><wait>",
       "linux /casper/vmlinuz autoinstall ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/",
       "<enter>"]                                # [ПРОВЕРИТЬ] под свою версию ISO
   }
   build {
     sources      = ["source.qemu.ubuntu_base"]
     provisioner "shell" { scripts = ["scripts/01-base.sh"] }
   }
   ```
   `packer init . && packer validate .` → `The configuration is valid`. Точный `boot_command` для autoinstall Ubuntu зависит от версии загрузчика — сверяйся с документацией Ubuntu Autoinstall; это место, где новички теряют больше всего времени.

5. **GIT01.** Собери образ и положи его на UBU01 в пул libvirt:
   ```bash
   packer build -var image_version=1.0 .
   scp build/ubuntu-base-1.0/ubuntu-base-1.0.qcow2 adm.student@10.10.10.30:/tmp/
   ssh adm.student@10.10.10.30 'sudo mv /tmp/ubuntu-base-1.0.qcow2 /var/lib/libvirt/images/ && sudo chown libvirt-qemu:kvm /var/lib/libvirt/images/ubuntu-base-1.0.qcow2'
   ```
   Ожидаемо: файл ~1.5–2 ГБ на UBU01, владелец `libvirt-qemu`.

6. **GIT01.** Пишешь `terraform/envs/onprem/main.tf` — провайдер и ВМ из собранного образа:
   ```hcl
   terraform {
     required_version = ">= 1.6"
     backend "http" {}
     required_providers {
       libvirt = { source = "dmacvicar/libvirt", version = "~> 0.8" }  # [ПРОВЕРИТЬ]
     }
   }
   variable "image_path" { type = string }
   provider "libvirt" { uri = "qemu+ssh://adm.student@10.10.10.30/system" }

   resource "libvirt_volume" "app_disk" {
     name   = "app-onprem-01.qcow2"
     pool   = "default"
     source = var.image_path
     format = "qcow2"
   }
   resource "libvirt_domain" "app" {
     name   = "app-onprem-01"
     memory = 2048
     vcpu   = 2
     disk { volume_id = libvirt_volume.app_disk.id }
     network_interface { network_name = "default" }
   }
   output "app_hosts" {
     value = { (libvirt_domain.app.name) = libvirt_domain.app.network_interface[0].addresses[0] }
   }
   ```
   **Важно:** `app-onprem-01` — временный объект этой лабораторной, в таблице стенда его нет. Он живёт только внутри лабы и в конце уничтожается; в `stand-log.md` пишем его отдельным блоком «временное».

7. **GIT01.** Инициализация с GitLab-бэкендом и план:
   ```bash
   terraform -chdir=terraform/envs/onprem init -backend-config=backend.hcl
   terraform -chdir=terraform/envs/onprem plan -out=tfplan -detailed-exitcode; echo "exit=$?"
   ```
   Ожидаемо: `exit=2`, в плане `Plan: 2 to add, 0 to change, 0 to destroy.` В GitLab в *Operate → Terraform states* появился state.

8. **GIT01.** Применяем и смотрим выходы:
   ```bash
   terraform -chdir=terraform/envs/onprem apply tfplan
   terraform -chdir=terraform/envs/onprem output -json app_hosts | jq
   ```
   Ожидаемо: JSON вида `{"app-onprem-01": "192.168.122.…"}`. ВМ живёт в сети libvirt по умолчанию `192.168.122.0/24` — она локальна для UBU01 и учебные сети стенда `10.10.10.0/24`/`10.10.20.0/24` не трогает.

9. **GIT01.** Генерируем инвентарь и проверяем доступность:
   ```bash
   make inventory ENV=onprem
   ansible -i ansible/inventory/onprem.ini app -m ping
   ```
   Ожидаемо: `pong`. Если `UNREACHABLE` — разбирайся с ключом/пользователем, а не с ролью.

10. **GIT01.** Playbook и сухой прогон перед применением:
    ```yaml
    # ansible/playbooks/site.yml
    - name: Configure app hosts
      hosts: app
      become: true
      roles:
        - role: nginx
    ```
    ```bash
    ansible-playbook -i ansible/inventory/onprem.ini ansible/playbooks/site.yml --check --diff
    ansible-playbook -i ansible/inventory/onprem.ini ansible/playbooks/site.yml
    ```
    Ожидаемо: первый прогон — `changed=3` и диффы, второй запуск того же playbook — `changed=0`. Это и есть проверка идемпотентности.

11. **GIT01.** Проверяем результат снаружи и через одну команду:
    ```bash
    curl -sI "http://$(terraform -chdir=terraform/envs/onprem output -json app_hosts | jq -r '.[]')" | head -1
    make destroy ENV=onprem && make up ENV=onprem
    ```
    Ожидаемо: сначала `HTTP/1.1 200 OK`, затем ВМ уничтожена и создана заново — с тем же результатом. Это и есть «повторить один-в-один».

12. **GIT01.** Ветка облака: тот же код, другой провайдер.
    ```hcl
    # terraform/envs/cloud/main.tf (фрагмент)
    provider "yandex" {
      cloud_id  = var.cloud_id
      folder_id = var.folder_id
      zone      = "ru-central1-a"        # [ПРОВЕРИТЬ зону в аккаунте]
    }
    resource "yandex_compute_instance" "app" {
      name  = "app-cloud-${count.index + 1}"
      count = var.instance_count
      resources { cores = 2, memory = 2 }
      boot_disk { initialize_params { image_id = var.image_id } }
      network_interface { subnet_id = var.subnet_id, nat = true }
    }
    ```
    ```bash
    terraform -chdir=terraform/envs/cloud validate
    terraform -chdir=terraform/envs/cloud plan      # нужны креды (OAuth/сервисный аккаунт)
    ```
    Ожидаемо: `validate` — `Success`. `plan` — только если есть аккаунт в облаке и ты готов платить; для лабораторной достаточно `validate` + `fmt`. Отечественные альтернативы: VK Cloud, Selectel, Yandex Cloud — все дают Terraform-провайдеры [ПРОВЕРИТЬ актуальные имена и версии].

13. **GIT01.** Коммит и запись в журнал стенда:
    ```bash
    git add -A && git commit -m "IaC platform: packer image + terraform onprem/cloud + ansible + make" && git push
    ```
    Ожидаемо: `backend.hcl` и `*.tfstate*` в `.gitignore`, в репозитории только код.

### Проверка

- [ ] `packer validate` проходит, артефакт называется с версией (`ubuntu-base-1.0.qcow2`).
- [ ] `terraform fmt -check -recursive` возвращает 0.
- [ ] `terraform plan -detailed-exitcode` после apply возвращает 0 (изменений нет).
- [ ] State лежит в GitLab (*Operate → Terraform states*), локального `terraform.tfstate` в репозитории нет.
- [ ] `ansible -m ping` до `app-onprem-01` — `pong`.
- [ ] Повторный `ansible-playbook` даёт `changed=0`.
- [ ] `curl` на приложение отдаёт `200 OK`.
- [ ] `make destroy && make up` даёт идентичный результат.
- [ ] `terraform -chdir=terraform/envs/cloud validate` проходит без кредов.
- [ ] `backend.hcl`, `*.tfvars` с секретами и `*.tfstate*` — в `.gitignore`.
- [ ] В `stand-log.md` записано: временная ВМ, её сеть, что осталось, что уничтожено.

### Сломай и почини

> **Пользователь:** sidorov
> **ВМ:** app-onprem-01 (стенд интеграции с WMS)
> **Что написал:** «После ночных работ страница стенда отдаёт 502, до этого всё открывалось. И ещё: вчера кто-то поправил конфиг nginx руками, чтобы попробовать — теперь никто не помнит, что там.»
> **Срочность:** высокая (приёмка у заказчика в 15:00)

**Как воспроизвести поломку намеренно.** Двумя способами сразу, потому что классы проблем разные:
1. На UBU01 «убить» ВМ мимо Terraform: `sudo virsh destroy app-onprem-01 && sudo virsh undefine app-onprem-01 --remove-all-storage` — имитация «кто-то почистил хост руками».
2. На ВМ поправить конфиг nginx вручную: `sudo sed -i 's/listen 80;/listen 8080;/' /etc/nginx/sites-enabled/default && sudo systemctl restart nginx` — имитация «ручной правки».

**Диагностика строго по порядку.**
1. `curl -sI http://<IP>` — код ответа: `000` значит «на машине нет сервиса/машины нет», `502/504` — обычно прокси или упавший апстрим, `200` — уже не воспроизводится.
2. `virsh list --all` на UBU01 — есть ли домен вообще. Нет домена → проблема уровня Terraform, идём в шаг 3. Есть, но `shut off` → `virsh start`.
3. `terraform -chdir=terraform/envs/onprem plan -detailed-exitcode; echo $?` — `2` означает дрейф на уровне ресурсов: Terraform видит, что объявленного домена нет, и предлагает `+ create`. Это правильный сигнал.
4. Если план чистый, а сайт лежит — проблема внутри машины: `ansible-playbook ... --check --diff --tags nginx` покажет, что `listen 80` в конфиге не совпадает, и предложит правку.
5. Проверить автозапуск при старте: `sudo systemctl is-enabled nginx` и `cloud-init status --long` — не зависла ли инициализация.
6. Проверить, что инвентарь свежий: если ВМ пересоздалась, IP мог смениться, а старый файл инвентаря остался → `make inventory`.

**Решение.** Восстановить ресурсы и конфигурацию из кода: `make up ENV=onprem` (или `terraform apply` + `make configure`). Ручную правку не «залечивать» — Ansible вернёт конфиг из роли. Если хочется, чтобы правка осталась — она идёт через MR в роль, а не через SSH.

**Что написать пользователю.**

> «Стенд восстановили из кода: машина была удалена на гипервизоре в обход Terraform, плюс кто-то менял конфиг nginx вручную. Сейчас всё поднято заново из репозитория, конфиг приведён к версии в Git. По просьбе — теперь стенд пересобирается одной командой `make up`, ручные правки на нём не сохраняются: если нужно поменять настройки, пришлите заявку, внесём через merge request и всё развернётся само. Прошу в 14:30 15 минут на финальную проверку перед приёмкой.»

**Как закрыть заявку.** В комментарии: причина (ручное удаление ВМ на гипервизоре + дрейф конфига), что сделано, ссылка на коммит/MR, что изменено в процессе (запрет ручных правок, доступ к `virsh` только у дежурного), время восстановления. Категория — «конфигурация / инфраструктура», не «сеть».

### Что изменилось на стенде

- **GIT01:** установлены `terraform` (1.9.x [ПРОВЕРИТЬ]), `packer` (1.11.x [ПРОВЕРИТЬ]), `ansible-core`, `jq`, `make`; создан проект `vektor/iac-platform`; в нём включён Terraform state; добавлен репозиторий с ролями и шаблонами.
- **UBU01:** установлены `qemu-kvm`, `libvirt-daemon-system`, `libvirt-clients`; активирована сеть libvirt `default` (192.168.122.0/24, локальна для UBU01); в пуле `default` лежит образ `ubuntu-base-1.0.qcow2`.
- **Временные объекты (только на время лабораторной):** ВМ `app-onprem-01` на UBU01 в сети libvirt; state `vektor-iac-onprem` в GitLab.
- **Постоянные артефакты:** `Makefile`-контракт `make up ENV=<onprem|cloud>`, `ansible/inventory/*.ini` генерируется автоматически (руками не редактируется), `.gitignore` исключает `*.tfstate*`, `backend.hcl`, `*.tfvars` с секретами.
- **Не менялось:** адреса и роли DC01/FS01/WEB01/MON01/HARBOR01, домен `corp.vektor.lab`, учётные записи и общие папки — платформа их не трогает.

### Откат

Порядок важен: **сначала код, потом снапшот**. Если сначала откатить ВМ, state в GitLab останется и будет ссылаться на несуществующие объекты.

1. `make destroy ENV=onprem` — Terraform аккуратно удалит ВМ и диск (если state уже «сломан» — `terraform -chdir=terraform/envs/onprem destroy -refresh=false`).
2. В GitLab: *Operate → Terraform states* → удалить state `vektor-iac-onprem` (или оставить, если планируешь продолжить).
3. VMware Workstation: для **GIT01**, **UBU01**, **WEB01** → *Snapshot Manager* → выбрать `before-devops-117` → *Restore*.
4. `git` в проекте можно не откатывать: история коммитов полезна, а лишние ветки удаляются отдельно.

## 6. Ошибки новичков и безопасность

1. **State в Git рядом с кодом.** → Утечка секретов и неразрешимые конфликты при работе вдвоём; при потере state Terraform создаёт дубли ресурсов. → Remote backend с блокировкой (GitLab/S3/MinIO), `*.tfstate*` в `.gitignore`, локальный state только для экспериментов.
2. **Секреты в `terraform.tfvars` и в Packer-шаблонах.** → Пароль уезжает в историю Git навсегда. → Переменные окружения (`TF_VAR_*`), masked/protected CI-переменные GitLab, Vault (урок 120). Учебный пароль стенда в шаблоне допустим только потому, что стенд изолирован — в бою так нельзя.
3. **`terraform apply -auto-approve` без сохранённого плана.** → Применяется не то, что ревьюили: кто-то поменял переменную между plan и apply. → Всегда `plan -out=tfplan` → артефакт → `apply tfplan`.
4. **Ручные правки на машинах, созданных Terraform/Ansible.** → Дрейф: код и реальность расходятся, следующий apply «затирает» правку в самый неподходящий момент. → Правка только через репозиторий; `plan -detailed-exitcode` по расписанию в CI.
5. **Незафиксированные версии провайдеров и образов.** → Через месяц `init` тянет новую версию, план внезапно хочет пересоздать ВМ. → `required_version`, `version = "~> X.Y"` у провайдеров, `terraform.lock.hcl` в Git, версия в имени образа.
6. **Образы-снежинки.** → Packer собрал «универсальный» образ, а Ansible донастраивает 300 параметров — воспроизводимость теряется, время сборки растёт. → Всё, что не зависит от окружения, — в образ; в Ansible только то, что зависит (IP, секреты, адреса бэкендов).
7. **`ANSIBLE_HOST_KEY_CHECKING=False` и `StrictHostKeyChecking=no` «чтобы работало».** → Отключается защита от MITM, привычка уезжает в прод. → Хранить known_hosts, раздавать ключи через CI, отключение — только в явно помеченной лабе.
8. **`terraform destroy` без `lifecycle { prevent_destroy = true }` на stateful-ресурсах.** → Одна команда сносит диск с данными. → `prevent_destroy` для томов и БД, отдельный env для состояния, бэкапы (урок 122).

### Безопасность

В боевой среде: приватный SSH-ключ к гипервизору/облаку не лежит в репозитории и не печатается в логах CI; у CI-раннера — отдельный сервисный аккаунт с минимальными правами, а не твой личный; root-SSH запрещён, вход по ключу; токен GitLab для state — с ограниченным сроком и scope `api`; `virsh`/Proxmox API доступны только из доверенной сети. Правило простое: **кто может запустить `apply`, тот управляет инфраструктурой** — поэтому `apply` в прод идёт только из пайплайна по MR, а не с ноутбука.

## 7. Вопросы на собеседовании

**Вопрос 1. Зачем нужны и Terraform, и Ansible? Почему не взять что-то одно?**

Ответ: у них разные зоны ответственности. Terraform — про *существование* ресурсов: создать ВМ, сеть, диск, DNS-запись, балансировщик; он ведёт state и умеет вычислять план изменений и зависимости. Ansible — про *состояние внутри* уже существующей машины: пакеты, файлы, сервисы, пользователи; он без state, работает по SSH и идемпотентен за счёт модулей. Terraform можно заставить конфигурировать через `remote-exec`, но это плохая практика: нет идемпотентности, состояние внутри ОС не отслеживается. Ansible может создавать ВМ через модули облака, но у него нет нормального графа зависимостей и плана изменений. Поэтому связка: Terraform создаёт и отдаёт outputs (IP, имена), Ansible по сгенерированному инвентарю настраивает. Packer нужен третьим слоем, чтобы машина появлялась уже готовой, а не «донастраивалась» 20 минут.

*Что хочет услышать интервьюер:* чёткое разделение provisioning vs configuration management, понимание, что state — ключевое отличие, и что связывать их надо через outputs/инвентарь.

*Красный флаг:* «Terraform всё умеет, Ansible не нужен» или путаница, кто из них хранит state.

**Вопрос 2. Что такое Terraform state, почему его нельзя хранить в Git и что такое блокировка?**

Ответ: state — файл (JSON), где Terraform хранит соответствие «ресурс в конфигурации → реальный объект у провайдера» плюс атрибуты и зависимости. Он нужен, чтобы понимать, что уже создано, и вычислять дифф. В Git нельзя: во-первых, там могут быть секреты в атрибутах; во-вторых, при работе нескольких инженеров и CI получаются конфликты слияния, а «смёрженный» state ломает картину; в-третьих, нет блокировки — два одновременных `apply` могут создать дубли или разрушить ресурсы. Поэтому remote backend: хранение + блокировка. Если процесс упал — лок остаётся, его снимают `terraform force-unlock <LOCK_ID>`, предварительно убедившись, что реально никто не работает. Ещё есть дрейф: state говорит одно, в облаке руками поменяли другое — `plan` это покажет, и это надо ловить регулярным прогоном в CI.

*Что хочет услышать:* state как «карта ресурсов», риски, remote backend + lock, `force-unlock` с осторожностью, понимание дрейфа.

*Красный флаг:* «state в git, так удобнее» или «мы просто удаляем state, если что-то не так».

**Вопрос 3. Как сделать инфраструктуру воспроизводимой: какую роль играет Packer и что такое immutable infrastructure?**

Ответ: воспроизводимость = одинаковый вход даёт одинаковый выход. Значит, фиксируем версии (Terraform, провайдеры через lock-файл, версия образа, версии ролей Ansible), а образы собираем кодом. Packer берёт базовый ISO/образ, гоняет provisioner'ы, ставит пакеты, обновления, агента мониторинга, базовую защиту — и выдаёт артефакт с версией. Дальше машина при первом старте получает только то, что уникально: hostname, IP, ключи, адрес бэкенда — через cloud-init/metadata. Это immutable infrastructure: машину не «лечат» годами, а пересобирают из нового образа, поэтому дрейф ограничен, откат — смена тега образа. Практический бонус: время от создания ВМ до готовности падает с десятков минут до секунд.

*Что хочет услышать:* версионирование, «образ — артефакт», cloud-init для уникального, откат через тег.

*Красный флаг:* «мы просто запускаем Ansible на голую ОС и всё».

**Вопрос 4. Как организовать один и тот же код для облака и on-premise?**

Ответ: разделяем переиспользуемое и специфичное. Общие модули (`modules/vm`, `modules/network`) описывают то, что одинаково; окружения (`envs/onprem`, `envs/cloud`) содержат только провайдер, backend и переменные. Отличие провайдера — это деталь реализации: libvirt/Proxmox/vSphere на своей площадке, Yandex Cloud/VK Cloud/Selectel в облаке. State у окружений обязательно разный, иначе один `apply` будет пытаться управлять чужими ресурсами. Вход — одна команда (`make up ENV=cloud`) или джоба CI с параметром окружения. Секреты приходят из окружения (CI-переменные, Vault), а не из кода. Инвентарь Ansible генерируется из outputs того окружения, которое только что развернули.

*Что хочет услышать:* модули + envs, разные state на окружение, параметризация провайдера, единый entrypoint.

*Красный флаг:* «копируем папку и правим руками» или один общий state на всё.

**Вопрос 5. Как поймать, что кто-то изменил инфраструктуру руками?**

Ответ: два уровня проверки. Уровень ресурсов — `terraform plan -detailed-exitcode` в CI по расписанию: код возврата 2 означает «есть изменения», то есть инфраструктура разошлась с кодом; дальше смотрим план, и либо применяем (если изменение легитимное), либо откатываем. Уровень конфигурации — `ansible-playbook --check --diff` по расписанию: показывает задачи, которые изменили бы систему, и диффы файлов. Плюс внешние признаки: алерты мониторинга, изменения в Git-истории, аудит в облаке (кто и когда делал изменения через API). Хорошая практика — запретить ручные изменения политикой и правами, а дрейф считать инцидентом, а не нормой.

*Что хочет услышать:* `-detailed-exitcode`, `--check --diff`, регулярный прогон, различие «дрейф ресурсов» и «дрейф конфигурации», плюс организационные меры.

*Красный флаг:* «никак, мы всё делаем только через Terraform» — без проверки это утверждение ничем не подкреплено.

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — документация Terraform: разделы *Terraform Language*, *State: Backends and State Locking*, *CLI Commands* (`plan`, `apply`, `import`, `state`, `force-unlock`). Поиск: `terraform backend http state locking`, `terraform plan detailed-exitcode`.
- **developer.hashicorp.com** — документация Packer: *Builders → QEMU*, *Builders → VMware*, *Provisioners*, *HCL templates*. Поиск: `packer qemu builder boot_command autoinstall`.
- **opentofu.org** — сайт и документация OpenTofu (MPL 2.0-форк Terraform), реестр `registry.opentofu.org`. Поиск: `opentofu migration from terraform`.
- **docs.ansible.com** — документация Ansible: *Inventory*, *Playbooks*, *Roles*, *check mode and diff mode*, *ansible-inventory*. Поиск: `ansible check mode diff idempotency`.
- **ubuntu.com** — *Ubuntu Server Autoinstall* (cloud-init/autoinstall, разметка, `user-data`). Поиск: `ubuntu autoinstall reference nocloud`.
- **registry.terraform.io** — страницы провайдеров `dmacvicar/libvirt`, `bpg/proxmox`, `yandex-cloud/yandex`, `vmware/vsphere`. Поиск по имени провайдера в реестре.
- **docs.gitlab.com** — *Terraform state* и *Terraform integration* (HTTP backend, блокировка, merge request workflow). Поиск: `gitlab terraform http backend state`.
- **docs.libvirt.org** — *libvirt daemon and remote access*, *networking* (сеть `default`, URI `qemu+ssh://`). Поиск: `libvirt remote access qemu+ssh`.
- **yandex.cloud/ru/docs** — провайдер Terraform для Yandex Cloud, `yandex_compute_instance`, зоны доступности. Поиск: `yandex cloud terraform provider compute instance` [ПРОВЕРИТЬ актуальные разделы].
- **libvirt.org / registry.terraform.io** — атрибуты `libvirt_domain.network_interface.addresses` [ПРОВЕРИТЬ в документации провайдера на момент прохождения].
