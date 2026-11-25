# Terraform на практике: Yandex Cloud, VK Cloud, Selectel, Proxmox, VMware vSphere

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди: «Пользователь: ivanov / ВМ: tf-lab01 (10.10.10.75) / Что написал: "Вчера тестовый сервис открывался, сегодня страница не грузится, а в понедельник нам показывать заказчику" / Срочность: средняя». Ты заходишь на PVE01 — виртуальной машины нет. В логах панели Proxmox видно, что её удалили руками через веб-интерфейс. А теперь главный вопрос: `terraform plan` в репозитории `vektor-iac` покажет «create», потому что в state машина ещё есть, а в реальности её нет.

Вот это и есть быт DevOps-инженера: инфраструктура живёт в пяти разных местах — в российском облаке, в гипервизоре на своём железе, в vSphere у заказчика — и везде Terraform работает по-разному. Один и тот же HCL-синтаксис, но разные провайдеры, разные способы аутентификации, разные «подводные камни» при изменении параметров. После этой главы ты будешь уметь: подключать провайдеры Yandex Cloud, VK Cloud, Selectel, Proxmox и vSphere, настраивать их аутентификацию без паролей в Git, писать конфигурацию ВМ для каждого, понимать, где изменение применяется на месте, а где требует пересоздания, и диагностировать расхождение между кодом и реальностью. Теорию провайдеров, state, модулей и workspaces мы уже прошли в уроке 61 — здесь только практика.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Terraform (и его форк OpenTofu) — это по сути три части: **ядро** (читает HCL, строит граф зависимостей, считает diff), **state** (файл-реестр соответствия «блок в коде → реальный объект с ID») и **провайдеры** — отдельные бинарники-плагины, которые общаются с ядром по gRPC, а с внешним миром — по HTTPS через API вендора.

```text
                ┌──────────────────────────────────────────┐
   *.tf (HCL)   │  Ядро Terraform / OpenTofu               │
      │         │  граф зависимостей + diff                │
      └────────►│                                          │
                └───────┬──────────────────────┬───────────┘
                        │ gRPC                 │ state (JSON)
                        ▼                      ▼
              ┌──────────────────┐    backend: local / S3+MinIO /
              │  Провайдер-плагин│    GitLab-managed / Postgres (урок 61)
              └────────┬─────────┘
                       │ HTTPS (443, у Proxmox — 8006)
                       ▼
   yandex     → api.yandex-cloud.* [ПРОВЕРИТЬ]   auth: IAM-токен / ключ сервисного аккаунта
   vkcs       → infra.mail.ru:35357 (Keystone) [ПРОВЕРИТЬ]  auth: логин/пароль/проект
   selectel   → api.selectel.ru + OpenStack     auth: SERVICE-токен аккаунта
   bpg/proxmox→ https://10.10.10.70:8006        auth: API-токен (user@realm!id=secret)
   vsphere    → https://vcenter:443             auth: логин/пароль в vCenter
```

Ключевая идея: провайдер — это **тонкая обёртка над API**. Он не «настраивает» ничего внутри ВМ (это Ansible из урока 60 или cloud-init из урока 63), он только вызывает create/read/update/delete. Отсюда все различия:

- **Облака (Yandex, VK, Selectel)** — API идемпотентный, ресурсы почти иммутабельны: поменял размер диска или flavor — обычно пересоздание. Плоские сети, security groups, «плавающие» IP — отдельные ресурсы.
- **Proxmox** — своя REST-панель на порту 8006, токены с правами на пути (`/`, `/pool/...`), клонирование из шаблона, cloud-init диск. Многое меняется на месте (`cores`, `memory`).
- **vSphere** — провайдер разговаривает с **vCenter**, а не с отдельным ESXi (у бесплатного ESXi API в основном read-only `[ПРОВЕРИТЬ]`). Клон шаблона, datastore, resource pool — обязательные data-источники перед созданием ВМ.

Ещё два понятия, без которых практика не работает: **lock-файл** (`.terraform.lock.hcl`) — фиксирует версии и хеши провайдеров, чтобы на GIT01 и на ноутбуке был один и тот же бинарник; **дрейф** — расхождение реальности и state (глава 59), лечится `plan`/`-refresh-only`/`import`.

## 3. Аналогия из жизни

Terraform — это прораб с наряд-заказом. В наряд-заказе (HCL) написано: «две комнаты, одна дверь, розетка у окна». Прораб не строит сам — у него бригады-субподрядчики (провайдеры), и с каждой он говорит на её языке: с облачной бригадой по телефону с токеном, с местной (Proxmox) — через вахтёра с пропуском, с vSphere — через управляющую компанию. Журнал обмеров (state) прораб ведёт сам: там записано, что «дверь №7 уже стоит», поэтому во второй приезд он не ставит вторую дверь, а только проверяет, что №7 на месте. Если дверь кто-то снял без его ведома — он это увидит при следующем обходе и поставит новую.

Аналогия ломается в одном месте: прораб может по ходу дела что-то подкрутить «по месту», а Terraform так не умеет — он либо приводит объект к описанному виду, либо пересоздаёт его, но никогда не «договаривается» и не лезет внутрь.

## 4. Практика и команды

**1. Единый файл версий провайдеров.** Один `versions.tf` на репозиторий — все провайдеры с точными ограничениями (версии ниже — пример, сверяйся с реестром).

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    yandex    = { source = "yandex-cloud/yandex", version = "~> 0.130" }   # [ПРОВЕРИТЬ]
    vkcs      = { source = "vkcs/vkcs",           version = "~> 0.5" }     # [ПРОВЕРИТЬ]
    selectel  = { source = "selectel/selectel",   version = "~> 5.0" }     # [ПРОВЕРИТЬ]
    openstack = { source = "terraform-provider-openstack/openstack", version = "~> 3.0" } # [ПРОВЕРИТЬ]
    proxmox   = { source = "bpg/proxmox",         version = "~> 0.60" }    # [ПРОВЕРИТЬ]
    vsphere   = { source = "hashicorp/vsphere",   version = "~> 2.9" }     # [ПРОВЕРИТЬ]
  }
}
```

Смысл: без `version` провайдер подтянется последний и может сломать план на ровном месте. Для Proxmox есть два провайдера: старый `Telmate/proxmox` (ресурс `proxmox_vm_qemu`) и новый `bpg/proxmox` (ресурс `proxmox_virtual_environment_vm`, лучше поддерживает cloud-init и API-токены). Мы берём `bpg`.

**2. Инициализация и фиксация версий под несколько платформ.**

```bash
tofu init
tofu providers lock -platform=linux_amd64 -platform=windows_amd64
```

`init` скачивает провайдеры и создаёт `.terraform.lock.hcl`; `providers lock` дописывает хеши для других ОС — важно, чтобы CI на Linux и твой ноутбук на Windows пользовались одним набором.

**3. Yandex Cloud: провайдер и ВМ.**

```bash
yc config profile get default          # folder-id, cloud-id — их подставит провайдер
export YC_TOKEN=$(yc iam create-token) # короткоживущий токен для локального запуска
```

```hcl
provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
  zone      = "ru-central1-a"
}

resource "yandex_vpc_network" "office" { name = "vektor-office" }

resource "yandex_compute_instance" "web" {
  name        = "web01"
  platform_id = "standard-v3"
  zone        = "ru-central1-a"
  resources { cores = 2  memory = 2  core_fraction = 20 }
  boot_disk { initialize { image_id = data.yandex_compute_image.ubuntu.id } }
  network_interface {
    subnet_id = yandex_vpc_subnet.dmz.id
    nat       = true
  }
  metadata = { ssh-keys = "ubuntu:${file("~/.ssh/id_ed25519.pub")}" }
}
```

`core_fraction` — доля vCPU (20/50/100 %), это законная экономия. `nat = true` даёт публичный IP (он тарифицируется — см. FinOps, урок 58). В CI вместо `YC_TOKEN` правильнее ключ сервисного аккаунта в переменной `YC_SERVICE_ACCOUNT_KEY_FILE`.

**4. VK Cloud: провайдер и ВМ.**

```hcl
provider "vkcs" {
  username   = var.vkcs_user
  password   = var.vkcs_password
  project_id = var.vkcs_project_id
  region     = "Москва"
}

resource "vkcs_compute_instance" "web" {
  name              = "web01"
  flavor_name       = "STD2-2-4"        # [ПРОВЕРИТЬ актуальные flavor]
  image_name        = "Ubuntu 24.04 LTS" # [ПРОВЕРИТЬ]
  availability_zone = "GZ1"
  network { uuid = vkcs_networking_network.net.id }
  security_groups = [vkcs_networking_secgroup.default.id]
}
```

Имя источника провайдера в реестре менялось исторически, поэтому `source` проверяй глазами по реестру `[ПРОВЕРИТЬ]`. Аутентификация — логин/пароль сервисного пользователя + `project_id`; заведи отдельного пользователя под Terraform, а не свой личный.

**5. Selectel: два провайдера в одной конфигурации.**

```hcl
provider "selectel" {
  token        = var.selectel_token      # лучше через env SEL_TOKEN [ПРОВЕРИТЬ]
  account_name = var.selectel_account    # SEL_ACCOUNT
}

provider "openstack" {
  auth_url            = "https://api.selvpc.ru/identity/v3"  # [ПРОВЕРИТЬ]
  user_name           = var.selectel_service_user
  password            = var.selectel_service_password
  tenant_id           = selectel_vpc_project_v2.project.id
  region              = "ru-9"
}
```

В Selectel разделение: провайдер `selectel` управляет аккаунтом, проектами, сетями, квотами, плавающими IP; а сами ВМ создаются через **OpenStack-провайдер** (`openstack_compute_instance_v2`), потому что под капотом у них OpenStack. Это типичный вопрос на собеседовании: «почему в Selectel два провайдера».

**6. Proxmox (`bpg/proxmox`): провайдер + клон шаблона с cloud-init.**

```hcl
provider "proxmox" {
  endpoint  = "https://10.10.10.70:8006/"
  api_token = var.pve_api_token   # "terraform@pve!tf=<секрет>"
  insecure  = true                # только для стенда с самоподписанным сертификатом
}

resource "proxmox_virtual_environment_vm" "lab" {
  name      = "tf-lab01"
  node_name = "pve01"
  vm_id     = 200

  clone { vm_id = 9000  full = true }

  agent  { enabled = true }
  cpu    { cores = 2 }
  memory { dedicated = 2048 }

  initialization {
    ip_config {
      ipv4 { address = "10.10.10.75/24"  gateway = "10.10.10.2" }
    }
    dns { domain = "corp.vektor.lab"  servers = ["10.10.10.10"] }
    user_account {
      username = "student"
      password = var.lab_password
      keys     = [trimspace(file("~/.ssh/id_ed25519.pub"))]
    }
  }
}
```

`insecure = true` отключает проверку сертификата — в боевой среде так делать нельзя (MITM), ставь нормальный сертификат. `ssh { ... }` блок нужен, если провайдер не может залить cloud-init диск через API на некоторых конфигурациях `[ПРОВЕРИТЬ]`.

**7. VMware vSphere: data-источники + клон.**

```hcl
provider "vsphere" {
  user                 = var.vsphere_user
  password             = var.vsphere_password
  vsphere_server       = var.vsphere_server
  allow_unverified_ssl = false
}

data "vsphere_datacenter" "dc"      { name = "DC-VEKTOR" }
data "vsphere_datastore"  "ds"      { name = "ds01"  datacenter_id = data.vsphere_datacenter.dc.id }
data "vsphere_compute_cluster" "cl" { name = "CL01"  datacenter_id = data.vsphere_datacenter.dc.id }
data "vsphere_network"    "net"     { name = "VM Network" datacenter_id = data.vsphere_datacenter.dc.id }
data "vsphere_virtual_machine" "tpl" {
  name = "ubuntu-2404-tpl"
  datacenter_id = data.vsphere_datacenter.dc.id
}

resource "vsphere_virtual_machine" "vm" {
  name             = "tf-lab02"
  resource_pool_id = data.vsphere_compute_cluster.cl.resource_pool_id
  datastore_id     = data.vsphere_datastore.ds.id
  num_cpus         = 2
  memory           = 2048
  guest_id         = data.vsphere_virtual_machine.tpl.guest_id
  network_interface { network_id = data.vsphere_network.net.id }
  disk {
    label            = "disk0"
    size             = data.vsphere_virtual_machine.tpl.disks[0].size
    thin_provisioned = true
  }
  clone { template_uuid = data.vsphere_virtual_machine.tpl.id }
}
```

Смотреть надо на `guest_id` и размер диска из шаблона: если поставить диск меньше шаблона — план упадёт с ошибкой.

**8. План как артефакт и его машинный вид.**

```bash
tofu plan -out=tfplan
tofu show -json tfplan | jq '.resource_changes[] | {addr: .address, act: .change.actions}'
```

`-out` фиксирует план, чтобы применить ровно то, что ревьюили; JSON — вход для политик из урока 65 (checkov/OPA). В CI-пайплайне это стандарт: план публикуется в MR, apply запускается только после approve.

**9. Работа со state на практике.**

```bash
tofu state list
tofu state show proxmox_virtual_environment_vm.lab
tofu state mv proxmox_virtual_environment_vm.lab proxmox_virtual_environment_vm.tf_lab01
tofu plan -refresh-only      # показать расхождения с реальностью
tofu apply -refresh-only     # принять реальность в state
```

`state mv` — когда переименовал блок в коде, чтобы Terraform не решил «удалить старое и создать новое». `-refresh-only` — когда объект удалили руками: посмотреть и осознанно принять.

**10. Отладка провайдера.**

```bash
TF_LOG=DEBUG tofu plan 2> tf-debug.log
grep -iE "401|403|timeout|dial" tf-debug.log | head
```

Три самых частых ответа: 401 (неверный токен), 403 (токен есть, прав нет — ACL в Proxmox, роль в облаке), timeout (сеть/файрвол/порт 8006).

**11. Защита от случайного удаления.**

```hcl
lifecycle {
  prevent_destroy = true
  ignore_changes  = [clone[0].vm_id]   # пример: не реагировать на внешние правки
}
```

`prevent_destroy` заставит `destroy` упасть — полезно для «боевых» ВМ. `ignore_changes` применять точечно: если «заигнорить» всё, дрейф перестанет ловиться.

**12. Зеркало провайдеров (реалии РФ).** Реестр Terraform может быть медленным или недоступным; легально поднять внутреннее зеркало:

```hcl
# ~/.tofurc или .terraformrc
provider_installation {
  filesystem_mirror {
    path    = "/srv/terraform-providers"
    include = ["registry.terraform.io/*/*"]
  }
}
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GIT01** 10.10.10.50 (GitLab CE + runner) — рабочая машина: там живёт репозиторий и оттуда запускаем Terraform/OpenTofu. SSH-доступ под своей учёткой есть.
- **UBU01** 10.10.10.30 — понадобится для проверки связности (`ping`, `ssh` до созданной ВМ).
- **PVE01** 10.10.10.70 — гипервизор Proxmox VE на вложенной виртуализации. На DevOps-треке он появляется именно здесь: если его ещё нет, разверни по инструкции из сисадминского трека (ВМ 4 vCPU / 8 ГБ RAM / 60 ГБ, VT-x/EPT включён, второй адаптер в VMnet8, адрес 10.10.10.70/24, шлюз 10.10.10.2). Proxmox VE в бесплатном варианте без подписки — легально, но репозиторий `enterprise` без подписки не работает, используй `no-subscription` `[ПРОВЕРИТЬ]`.
- Windows-часть стенда не нужна.
- Облачные аккаунты Yandex Cloud / VK Cloud / Selectel **не обязательны**: их конфигурации мы только валидируем. Если хочешь потрогать по-настоящему — у всех трёх есть приветственные гранты/тестовый период `[ПРОВЕРИТЬ условия на 2026]`, но помни: это реальные деньги после гранта.
- В GitLab уже есть твой пользователь и возможность создавать проекты.

### Снапшот

На выключенных (или работающих — Workstation умеет и так) ВМ GIT01, UBU01, PVE01: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-062`. Из командной строки хоста:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\GIT01\GIT01.vmx" before-devops-062
```

Повторить для `UBU01.vmx` и `PVE01.vmx`.

### Шаги

1. **PVE01.** Проверь версию: `pveversion`. Ожидаемо: `pve-manager/8.x.x` `[ПРОВЕРИТЬ актуальную]`.
2. **PVE01.** Создай роль, пользователя и API-токен:

```bash
pveum role add TFProvisioner -privs "Datastore.AllocateSpace Datastore.Audit VM.Allocate VM.Audit VM.Clone VM.Config.CPU VM.Config.Disk VM.Config.Memory VM.Config.Network VM.Config.Options VM.Config.Cloudinit VM.PowerMgmt Pool.Audit Sys.Audit"
pveum user add terraform@pve --comment "Terraform provider"
pveum acl modify / --users terraform@pve --roles TFProvisioner
pveum user token add terraform@pve tf --privsep 0
```

   Ожидаемый результат: вывод с `value` — это секрет токена, он показывается **один раз**. `--privsep 0` означает, что токен наследует права пользователя — для лабы удобно, в бою лучше `privsep 1` и отдельные права на токен.
3. **PVE01.** Подготовь шаблон 9000 из cloud-образа Ubuntu 24.04 (в уроке 63 это автоматизирует Packer):

```bash
cd /var/lib/vz/template/iso
wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img   # [ПРОВЕРИТЬ актуальный путь]
qm create 9000 --name ubu-tpl --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0
qm importdisk 9000 noble-server-cloudimg-amd64.img local-lvm
qm set 9000 --scsihw virtio-scsi-pci --scsi0 local-lvm:vm-9000-disk-0
qm set 9000 --ide2 local-lvm:cloudinit
qm set 9000 --boot c --bootdisk scsi0
qm set 9000 --serial0 socket --vga serial0
qm template 9000
```

   Проверка: `qm list` показывает `9000 ubu-tpl ... template`.
4. **GIT01.** Установи OpenTofu (или Terraform — синтаксис тот же, команда `terraform` вместо `tofu`):

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://get.opentofu.org/install-opentofu.sh -o install-opentofu.sh   # [ПРОВЕРИТЬ URL]
sudo bash install-opentofu.sh --install-method deb
tofu version
```

   Ожидаемо: `OpenTofu v1.x.x` `[ПРОВЕРИТЬ]`. Terraform качать с `releases.hashicorp.com` из РФ может быть медленно — OpenTofu здесь ещё и практичнее (форк под эгидой Linux Foundation, MPL-2.0).
5. **GIT01.** Создай проект `vektor-iac`, склонируй и разложи структуру: `proxmox/`, `clouds/yandex/`, `clouds/vkcs/`, `clouds/selectel/`, `clouds/vsphere/`, `modules/`, `.gitignore` со строками `.terraform/`, `*.tfstate*`, `*.tfvars`, `tfplan`, `*.log`. Ожидаемо: `git status` чистый после первого коммита.
6. **GIT01.** В `proxmox/` создай `versions.tf` (фрагмент 1 из раздела 4), `variables.tf` (`pve_api_token` с `sensitive = true`, `lab_password`), `main.tf` с провайдером и ВМ из фрагмента 6 (адрес 10.10.10.75/24, шлюз 10.10.10.2, DNS 10.10.10.10, домен `corp.vektor.lab`).
7. **GIT01.** Задай секреты через окружение, не через файл в Git:

```bash
export TF_VAR_pve_api_token='terraform@pve!tf=<секрет из шага 2>'
export TF_VAR_lab_password='Lab#2026-vektor'
tofu -chdir=proxmox init
tofu -chdir=proxmox providers lock -platform=linux_amd64
```

   Ожидаемо: «Terraform has been successfully initialized!», появился `.terraform.lock.hcl`.
8. **GIT01.** `tofu -chdir=proxmox fmt -check -recursive && tofu -chdir=proxmox validate`. Ожидаемо: «Success! The configuration is valid.» Если ошибка — читаем номер строки, чаще всего это опечатка в имени аргумента.
9. **GIT01.** `tofu -chdir=proxmox plan -out=tfplan`. Ожидаемо: `Plan: 1 to add, 0 to change, 0 to destroy.` В плане видно `+ resource "proxmox_virtual_environment_vm" "lab"` и `initialization` с нашим IP.
10. **GIT01.** `tofu -chdir=proxmox apply tfplan`. Ожидаемо: «Apply complete! Resources: 1 added». Проверь на PVE01 `qm list` — появилась ВМ `200 tf-lab01 running`.
11. **UBU01.** `ping -c3 10.10.10.75` и `ssh student@10.10.10.75` (пароль `Lab#2026-vektor`). Ожидаемо: пинг идёт, SSH пускает — значит cloud-init применился.
12. **GIT01.** Повтори `tofu -chdir=proxmox plan`. Ожидаемо: **«No changes. Your infrastructure matches the configuration.»** — это и есть идемпотентность из урока 59.
13. **GIT01.** Поменяй `cores = 2` на `cores = 4`, снова `plan`. Ожидаемо: `~ update in place` (не пересоздание). Примени и проверь на PVE01: `qm config 200 | grep cores`.
14. **GIT01.** Сложи облачные конфигурации (фрагменты 3–5, 7) в `clouds/*/`. Без кредов запусти:

```bash
for d in clouds/*/; do tofu -chdir="$d" init -backend=false && tofu -chdir="$d" validate || echo "FAIL $d"; done
```

   Ожидаемо: все четыре — «valid». `plan` для них потребует токенов, и это нормально.
15. **GIT01.** Закоммить всё, допиши в `stand-log.md` изменения (см. ниже).

### Проверка

- [ ] `tofu version` на GIT01 отвечает, версия зафиксирована в README.
- [ ] `.terraform.lock.hcl` в Git, `*.tfstate` и `*.tfvars` — в `.gitignore` и не в индексе (`git ls-files | grep tfstate` пусто).
- [ ] `pveum user token list terraform@pve` показывает токен `tf`.
- [ ] `qm list` на PVE01: шаблон `9000 ubu-tpl` и ВМ `200 tf-lab01`.
- [ ] `ping 10.10.10.75` с UBU01 проходит, `hostname` на ВМ — `tf-lab01`.
- [ ] Повторный `plan` — «No changes».
- [ ] `tofu -chdir=clouds/yandex validate` (и остальные три) — success без кредов.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** tf-lab01 (10.10.10.75)
> **Что написал:** «Вчера тестовый сервис открывался по адресу, сегодня страница не грузится. В понедельник показываем заказчику.»
> **Срочность:** средняя

**Воспроизводим поломку.** На PVE01: `qm destroy 200` (или через веб-панель: выбрать ВМ → More → Remove). Это имитация «коллега прибрался руками».

**Диагностика по шагам:**
1. `ping 10.10.10.75` с UBU01 — не отвечает. Пока это только симптом.
2. На PVE01 `qm list | grep 200` — пусто. Значит проблема не в сервисе, а в самой ВМ.
3. На GIT01 `tofu -chdir=proxmox state list` — ресурс `proxmox_virtual_environment_vm.lab` **есть**. Вот и дрейф: код и state говорят «есть», реальность — «нет».
4. `tofu -chdir=proxmox plan` — показывает `1 to add`. Terraform ещё не знает, что объекта нет: план построен по устаревшему state. Здесь важно не жать `apply` слепо, а сначала понять, куда делся объект.
5. `tofu -chdir=proxmox plan -refresh-only` — вот теперь видно `-/+ ... (because ... was deleted)`. Решение осознанное: `tofu -chdir=proxmox apply -refresh-only` — принимаем реальность, объект уходит из state.
6. Восстанавливаем: `tofu -chdir=proxmox apply -auto-approve` (в лабе можно, в бою — только через MR и approve).
7. Проверяем: `qm list`, `ping`, `ssh student@10.10.10.75`.

**Если ВМ на месте, а в state её нет** (обратный случай) — не создавать вторую, а импортировать: `tofu -chdir=proxmox import proxmox_virtual_environment_vm.lab pve01/200` `[ПРОВЕРИТЬ формат адреса импорта у bpg]`, затем `plan` и выровнять расхождения в коде.

**Профилактика, которую добавляем сразу:** `lifecycle { prevent_destroy = true }` на ВМ, которую нельзя терять, и правило «удаление только через Terraform, руками — никогда». Плюс заметка в runbook.

**Ответ пользователю:** «Добрый день! Причина — тестовая ВМ была удалена с гипервизора вручную. Восстановил из шаблона через Terraform, адрес тот же — 10.10.10.75, доступ уже работает, проверьте. Чтобы не повторилось: удаление и изменение тестовых машин теперь только через репозиторий, руками на гипервизоре — нельзя. Если что-то ещё не открывается — напишите, посмотрю.»

**Закрытие заявки:** категория «инфраструктура/виртуализация», решение — restore from template via IaC, в комментарии ссылка на коммит в `vektor-iac`, root cause — «ручное изменение вне IaC», preventative action — `prevent_destroy` + правка прав `terraform@pve`.

### Что изменилось на стенде

- **PVE01**: пользователь `terraform@pve`, роль `TFProvisioner` (ACL на `/`), API-токен `tf`; шаблон ВМ `9000 ubu-tpl`; ВМ `200 tf-lab01` — 4 vCPU / 2 ГБ / 10.10.10.75, шлюз 10.10.10.2, DNS 10.10.10.10. `tf-lab01` — временная лаб-машина, в базовую таблицу стенда не входит.
- **GIT01**: установлен OpenTofu, проект `vektor-iac` со структурой `proxmox/` и `clouds/{yandex,vkcs,selectel,vsphere}/`, локальный state в `proxmox/` (в бою — remote backend, урок 61).
- **UBU01**: изменений нет, использовалась для проверки связности.
- **Облака**: реальных ресурсов не создано, только `validate`.
- Запись в `stand-log.md` обязательна — на `tf-lab01` и шаблон `9000` будем опираться в уроке 63.

### Откат

1. На GIT01: `tofu -chdir=proxmox destroy` (если `prevent_destroy` уже добавлен — временно убрать) — сначала убираем ВМ через код, чтобы не оставить «сирот».
2. Если destroy не идёт — откат снапшотом: VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-062` → **Go to**. Или из консоли хоста: `vmrun -T ws revertToSnapshot "D:\VMs\PVE01\PVE01.vmx" before-devops-062`.
3. После отката удали локальный каталог `.terraform/` и `terraform.tfstate*` на GIT01, если они остались от неудачных прогонов, и выполни `tofu init` заново.

## 6. Ошибки новичков и безопасность

1. **Провайдер без ограничения версии.** → `tofu init` подтягивает свежий мажор, план внезапно хочет пересоздать всё. → Всегда `version` в `required_providers` + `providers lock`, обновление — отдельным MR с чтением changelog.
2. **Токены и пароли в `main.tf` или `terraform.tfvars` в Git.** → утечка ключа Proxmox = полный контроль над гипервизором. → `TF_VAR_*`/секреты CI, `.gitignore` на `*.tfvars`, `sensitive = true`, ротация токена после любой утечки (подробно — урок 66).
3. **Логин `root@pam` в провайдере Proxmox.** → любой, кто получил конфиг, — админ гипервизора. → Отдельный пользователь + API-токен + кастомная роль с минимальными привилегиями.
4. **`allow_unverified_ssl = true` / `insecure = true` «пока так».** → MITM: подмена ответа API, и Terraform послушно применит чужой план. → Нормальный сертификат или внутренний CA; `insecure` только на изолированном стенде, с комментарием в коде.
5. **`apply` с ноутбука одновременно с CI.** → два процесса правят state, взаимные перезаписи и «фантомные» ресурсы. → State locking (урок 61), apply только из пайплайна, локально — только `plan`.
6. **`-target` и `-auto-approve` в проде.** → «быстро поправить одну ВМ» ломает зависимости и обходит ревью. → Только полный план в MR + approve; `-target` — для отладки на стенде.
7. **`ignore_changes = all` вместо точечных полей.** → дрейф никогда не обнаруживается, IaC превращается в декорацию. → Перечисляй конкретные атрибуты и понимай, почему они меняются.
8. **Забытые ресурсы в облаке.** → грант кончился, счёт капает за диск и публичный IP. → `labels`/теги с владельцем, отдельный `dev`-проект, `destroy` после лабы, регулярный аудит через `plan` в CI.

**Безопасность.** В боевой среде нельзя: держать один «универсальный» сервисный аккаунт с правами администратора на весь облачный аккаунт; хранить state в открытом бакете (в state лежат пароли и приватные ключи cloud-init в открытом виде); давать CI-раннеру постоянные долгоживущие креды. Правильно: по сервисному аккаунту на окружение, минимальные роли, короткоживущие токены (или OIDC-федерация с облаком `[ПРОВЕРИТЬ поддержку у конкретного провайдера]`), шифрование backend, аудит (`yc`/VK/Selectel audit log, лог задач Proxmox).

## 7. Вопросы на собеседовании

**1. «Чем Terraform-подход к ВМ в облаке отличается от Proxmox или vSphere?»**
Ответ: в облаках провайдер работает с публичным API, ресурсы почти иммутабельны — смена flavor или типа диска обычно означает пересоздание, а сеть, публичный IP и security groups — отдельные ресурсы со своими жизненными циклами. В Proxmox и vSphere типичный паттерн — клон шаблона с cloud-init, и многие параметры (CPU, память) меняются на месте, что видно по знаку `~` в плане. Плюс разница в аутентификации: в облаке — сервисный аккаунт и IAM, в Proxmox — API-токен с ACL на путь, в vSphere — учётка vCenter и data-источники (datacenter, datastore, cluster, network), которые обязательно нужно объявить перед созданием ВМ.
*Что хочет услышать:* понимание «провайдер = обёртка над API», иммутабельность против in-place изменений, обязательные data-источники в vSphere.
*Красный флаг:* «везде одинаково, просто поменять имя провайдера».

**2. «Как правильно передавать креды Terraform в CI?»**
Ответ: никогда не в коде и не в `.tfvars` в Git. Варианты по убыванию предпочтительности: федерация/OIDC — раннер получает короткоживущий токен от облака без статических секретов; затем — ключ сервисного аккаунта в защищённых переменных CI (masked, protected, доступен только на защищённых ветках); для Proxmox — API-токен с `privsep` и минимальной ролью; для vSphere — отдельный пользователь только на нужный кластер. Секреты передаются через `TF_VAR_*` или внешний vault, а в коде — только `variable` с `sensitive = true`. Плюс ротация и запрет вывода секретов в `output` без `sensitive`.
*Что хочет услышать:* короткоживущие креды, разделение по окружениям, отсутствие секретов в Git и в логах плана.
*Красный флаг:* «положу в terraform.tfvars, репозиторий приватный».

**3. «Провайдер обновился и сломал план. Твои действия?»**
Ответ: сначала смотрю, что реально изменилось — `plan` и changelog провайдера. Откатываю версию в `required_providers` до рабочей (она зафиксирована в `.terraform.lock.hcl`), фиксирую это коммитом, чтобы пайплайн разблокировать. Потом отдельной задачей делаю осознанный апгрейд: читаю breaking changes, поднимаю версию, прогоняю `init -upgrade`, `providers lock` под все платформы, проверяю план на «нет ли пересоздания», и только затем apply через MR.
*Что хочет услышать:* lock-файл, разделение «починить сейчас» и «обновить осознанно», чтение changelog.
*Красный флаг:* «снесу state и создам заново».

**4. «Почему нельзя применять Terraform с ноутбука в прод и как это ограничить?»**
Ответ: нет гарантии единственности (два инженера/ноут + CI = конфликт state), нет ревью, нет истории, локальные креды не аудируются, а результат зависит от версии провайдера на конкретной машине. Ограничение: remote backend с блокировкой, apply только из пайплайна на защищённой ветке, план как артефакт в MR, обязательный approve, политики (OPA/checkov) на JSON плана, права на прод-креды только у CI-джобы. Локально разрешён `plan` и работа на лаб-стенде.
*Что хочет услышать:* state locking, GitOps-процесс, policy-as-code, минимум прав.
*Красный флаг:* «у нас один админ, конфликтов не бывает».

**5. «Объект создали руками в панели Proxmox. Что делать?»**
Ответ: это дрейф в обратную сторону. Определяю, должен ли объект жить: если нет — удаляю руками и делаю `plan`, чтобы убедиться, что state сходится. Если да — описываю его в коде и делаю `import` (для bpg-провайдера адрес вида `pve01/<vmid>` `[ПРОВЕРИТЬ]`), затем `plan` и правлю конфигурацию, пока план не станет пустым. Для переименования блоков — `moved` или `state mv`, чтобы не было destroy+create.
*Что хочет услышать:* import вместо пересоздания, `moved`/`state mv`, план как критерий сходимости.
*Красный флаг:* «удалю руками и создам заново» — на бою это простой.

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — раздел «Terraform»: «Providers», «Provider Configuration», «State», «Command: plan/apply/import», «Lifecycle Meta-Arguments». Поиск: `terraform provider configuration lifecycle meta-arguments`.
- **registry.terraform.io** — карточки провайдеров и их документация: `yandex-cloud/yandex`, `vkcs/vkcs`, `selectel/selectel`, `terraform-provider-openstack/openstack`, `bpg/proxmox`, `hashicorp/vsphere`. Поиск: `terraform registry bpg proxmox virtual environment vm`. Точные версии и имена аргументов сверяй там — они меняются чаще, чем книги.
- **opentofu.org** — раздел «Docs» (тот же CLI, отличия в реестре и части функций), «Installation». Поиск: `opentofu install debian`.
- **yandex.cloud/ru/docs** — раздел про Terraform-провайдер, сервисные аккаунты и IAM-токены. Поиск: `yandex cloud terraform provider compute instance`.
- **cloud.vk.com/docs** — раздел про Terraform и сервисные пользователи. Поиск: `vk cloud terraform provider compute instance`.
- **docs.selectel.ru** — разделы «Terraform-провайдер Selectel» и «OpenStack API». Поиск: `selectel terraform provider vpc project`.
- **pve.proxmox.com** — «Proxmox VE Administration Guide» (разделы про пользователей, токены, `pveum`, cloud-init, `qm`), «API Viewer». Поиск: `proxmox ve api tokens pveum cloud-init`.
- **developer.broadcom.com** (документация VMware после смены вендора) — «vSphere Web Services API», статьи про права и шаблоны `[ПРОВЕРИТЬ актуальный раздел и доступ]`. Поиск: `vsphere terraform provider clone template guest_id`.
