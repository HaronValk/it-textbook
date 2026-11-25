# Packer и Cloud-Init: golden images, автоматическая сборка образов ВМ

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №412.** Пользователь: Петрова (HR, ВМ WIN10-CL02). Текст: «Мне для проверки нового портала нужен тестовый сервер, я просила ещё на прошлой неделе. Иванцов сказал, что вы "поднимаете вручную и это долго"». Срочность: средняя, но с пометкой «руководитель спрашивал дважды».

Знакомая история? Приходит задача «дай ВМ», и начинается: скачать ISO, потыкать установщик, выбрать пакеты, поставить `qemu-guest-agent`, добавить ключ, настроить `chrony`, выключить, поставить снапшот. Через месяц выясняется, что на одном стенде Ubuntu 22.04 с одной версией nginx, на другом 24.04 с другой, а на третьем кто-то «быстренько поправил руками» и забыл. Это и есть дрейф конфигурации из урока 59, только на уровне образов.

Packer и cloud-init решают две разные половины одной задачи. Packer собирает **golden image** — эталонный образ, который собирается из кода, повторяемо, с версией и контрольной суммой. Cloud-init доводит этот образ до конкретной машины на первом запуске: задаёт hostname, сеть, пользователей, SSH-ключи. После главы вы сможете: написать шаблон Packer на HCL2, собрать образ Ubuntu через QEMU-билдер, разложить настройки между «запечь в образ» и «отдать cloud-init», проверить образ end-to-end и положить всё в Git. Это база для урока 62 (Terraform создаёт ВМ из вашего образа) и для урока 65 (политики на образы).

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Immutable infrastructure.** Идея: сервер не «обслуживают» руками, а заменяют новым экземпляром из свежего образа. Образ собирается один раз и становится неизменяемым артефактом — как Docker-образ, только для ВМ. Отсюда три правила: сборка описана кодом, версия образа фиксирована, изменения вносятся пересборкой, а не правкой живого сервера.

**Архитектура Packer.** Packer — один бинарник, работает по шаблону (HCL2 или устаревший JSON). В шаблоне три сущности:

- **builder** — где и из чего собираем. `qemu` (локально, наш случай), `vmware-iso` (VMware Workstation/Player на той же машине), `vsphere-iso` (vCenter), `amazon-ebs`, `yandex` и т.д. Билдер запускает ВМ, поднимает её и ждёт, пока она станет доступна.
- **communicator** — как Packer попадает внутрь. Для Linux — SSH (порт 22), для Windows — WinRM (5985/5986). Логин/ключ передаются параметрами `ssh_username`, `ssh_private_key_file`, `ssh_timeout`.
- **provisioner** — что делает внутри: `shell` (скрипты), `ansible` (переиспользуем роли из урока 60), `file`, `windows-restart`. **post-processor** — что делает с результатом: `compress`, `vagrant`, `manifest` (пишет JSON с именем и чек-суммой артефакта — берите в CI всегда).

**Cloud-init.** Это сервис внутри Linux-образа, который при первом (и только при первом) старте читает «семя» (seed) с конфигурацией и применяет её. Стадии запускаются systemd-юнитами строго по порядку:

```text
файл-источник данных (datasource)
  ├─ NoCloud   : диск или ISO с меткой тома CIDATA -> user-data + meta-data
  ├─ ConfigDrive: аналог из облака
  └─ None      : ничего не найдено -> всё по умолчанию (частый источник сюрпризов)

systemd:
 cloud-init-local.service  -> находит datasource, правит сеть (до сети)
 cloud-init.service        -> модули "network": users, packages, write_files
 cloud-config.service      -> модули конфигурации: hostname, apt, timezone
 cloud-final.service       -> runcmd, reboot, всё пользовательское
```

Ключевая мысль и главный навык: **образ = то, что одинаково для всех** (пакеты, ядро, агент, базовые настройки безопасности). **cloud-init = то, что уникально** (hostname, IP, ключи, роли). Если запечь в образ hostname и IP — вы получите клоны, которые дерутся друг с другом в сети.

```text
cloud image Ubuntu (noble-server-cloudimg-amd64.img)
   noble .img  +  seed-build.iso (label CIDATA)
        \             /
         \           /
          Packer (builder qemu, disk_image=true)
                   |
            provisioner shell: 01-cleanup.sh
                   |  (apt clean, machine-id, ssh host keys, cloud-init clean)
            post-processor: manifest
                   |
        ubuntu-base-<дата>-<git-sha>.qcow2   <- неизменяемый артефакт
                   |
        +----------+-----------+
        |                      |
  qemu-img -> vmdk       HCP Packer / vSphere Content Library /
  (VMware Workstation)   Yandex Cloud image / OpenStack Glance
                   |
        первый старт: cloud-init читает seed-runtime.iso
        -> hostname, сеть, пользователь adm.student, SSH-ключ
```

**Порты и протоколы, которые реально встретятся:** SSH/22 (communicator Packer в Linux), WinRM/5985-5986 (Windows-образы), HTTP/80 — временный веб-сервер, который Packer поднимает по `http_directory` для `boot_command` (в cloud-образах обычно не нужен), и user-mode сеть QEMU (внутри гостя 10.0.2.15, наружу — через NAT процесса qemu). KVM-ускорение требует `/dev/kvm` и поддержки VT-x/EPT (AMD-V) со стороны VMware.

## 3. Аналогия из жизни

Представьте фабрику-кухню, которая кормит 50 офисов. Никто не режет лук на каждой точке — повара на фабрике делают **заготовки** (mise en place): варёные овощи, бульон, базовый соус, всё в вакуумных пакетах с датой и номером партии. На месте, в маленькой кухне, повар только вскрывает пакет и добавляет то, что нужно конкретному заказу: соль, специи, соус к конкретному блюду. Если заготовка испорчена — её видно по номеру партии и партию отзывают целиком, а не чинят в каждой точке.

Packer — это фабрика-кухня, cloud-init — повар на точке. Аналогия ломается в одном месте: пакет с заготовкой у фабрики конечный и портится от времени, а образ требует **регулярной пересборки** — устаревший образ не «скиснет» сам, он просто тихо накопит CVE и продолжит работать месяцами.

## 4. Практика и команды

Все команды — на **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS), вход под учёткой из вашего стенда с sudo.

**1. Ставим QEMU, утилиты образов и генератор seed.**

```bash
sudo apt-get update
sudo apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils
```

- `qemu-system-x86` — сам гипервизор (его использует builder `qemu`).
- `qemu-utils` — `qemu-img` (информация о диске, конвертация форматов).
- `cloud-image-utils` — `cloud-localds`, собирает ISO с `user-data`/`meta-data`.

**2. Проверяем, будет ли KVM-ускорение (иначе сборка идёт на эмуляции и занимает в 10–20 раз дольше).**

```bash
ls -l /dev/kvm
egrep -c '(vmx|svm)' /proc/cpuinfo
```

Ожидаемо: `crw-rw---- 1 root kvm ... /dev/kvm` и число больше нуля. Если `/dev/kvm` нет — включите в VMware Workstation «Virtualize Intel VT-x/EPT or AMD-V/RVI» (в настройках ВМ → Processors). Если не получается — `accelerator = "tcg"` в шаблоне, сборка будет долгой.

**3. Ставим Packer из официального репозитория HashiCorp.**

```bash
wget -O- https://apt.releases.hashicorp.com/gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt-get update && sudo apt-get install -y packer
packer version
```

`gpg --dearmor` превращает скачанный ASCII-ключ в бинарный keyring, `signed-by=` привязывает репозиторий именно к этому ключу — так вы не поставите пакет из чужого источника. Актуальная версия Packer — `[ПРОВЕРИТЬ]` на developer.hashicorp.com; билдеры и плагины у 1.9+ совместимы с примерами ниже. Если доступ к `apt.releases.hashicorp.com` из РФ нестабилен — легальная альтернатива: скачать релизный архив с `developer.hashicorp.com/packer/install` (или из официального GitHub Releases) и **сверить SHA256SUMS** перед распаковкой.

**4. Инициализация плагинов.**

```bash
cd ~/vektor-infra/packer/ubuntu-base
packer init .
```

`packer init` читает блок `required_plugins` и скачивает плагины в `~/.config/packer/plugins` `[ПРОВЕРИТЬ путь для вашей версии]`. Смотреть: строки `Installed plugin github.com/hashicorp/qemu vX.Y.Z`.

**5. Форматирование и проверка синтаксиса.**

```bash
packer fmt -recursive -check .
packer validate .
```

`fmt` — аналог `terraform fmt`; `-check` не правит файлы, а возвращает код ошибки (нужно в CI). `validate` проверяет HCL, типы переменных и доступность плагинов, но **не** запускает сборку.

**6. Что вообще соберётся — без запуска.**

```bash
packer inspect .
```

Показывает переменные, билдеры и провиженеры шаблона. Удобно на ревью: сразу видно, что в шаблоне появился новый билдер.

**7. Сборка.**

```bash
PACKER_LOG=1 packer build -var-file=dev.pkrvars.hcl -on-error=ask .
```

- `-var-file` — значения переменных (URL образа, чек-сумма) отдельно от кода.
- `-on-error=ask` — при падении Packer не убивает ВМ, а спрашивает: `retry` / `cleanup` / `abort`. Главный ключ отладки.
- `-debug` — пошаговый режим с паузами.
- `-only=ubuntu-base.qemu.ubuntu_base` — собрать только один источник из нескольких.
- `PACKER_LOG=1` — подробный лог в stderr, без него причины падений SSH-подключения не видны.

Ожидаемый хвост вывода:

```text
==> ubuntu-base.qemu.ubuntu_base: Waiting for SSH to become available...
==> ubuntu-base.qemu.ubuntu_base: Connected to SSH!
==> ubuntu-base.qemu.ubuntu_base: Running post-processor: manifest
Build 'ubuntu-base.qemu.ubuntu_base' finished after 4 minutes 12 seconds.
```

**8. Сборка seed-диска для cloud-init.**

```bash
cloud-localds seed-build.iso http/user-data http/meta-data
blkid seed-build.iso
```

`cloud-localds` делает ISO9660 с меткой тома `CIDATA` — именно по этой метке NoCloud находит конфигурацию. Проверка: в выводе `blkid` должно быть `LABEL="CIDATA"`. Формат: `cloud-localds <выходной iso> <user-data> <meta-data>`.

**9. Информация об артефакте и конвертация форматов.**

```bash
qemu-img info output-ubuntu-base/ubuntu-base.qcow2
qemu-img convert -O vmdk -c output-ubuntu-base/ubuntu-base.qcow2 ubuntu-base.vmdk
qemu-img resize output-ubuntu-base/ubuntu-base.qcow2 +5G
```

`-O vmdk` — формат для VMware Workstation; `-c` — сжатие (медленнее читается, но образ меньше); `resize +5G` — увеличить виртуальный диск. В `info` смотрите `virtual size` (сколько видит гость) и `disk size` (сколько реально занято).

**10. Диагностика cloud-init внутри запущенной машины.**

```bash
cloud-init status --wait          # ждать завершения и показать статус
cloud-init status --long          # плюс список отработавших стадий
cloud-init schema --config-file runtime/user-data   # проверка YAML до запуска
sudo cloud-init clean --logs --seed   # сбросить состояние (для пересборки образа)
sudo tail -f /var/log/cloud-init-output.log
```

- `status --wait` возвращает `status: done` и ненулевой код при `error`.
- `schema` — валидация конфига **до** запуска ВМ: ловит 80% проблем с отступами.
- `clean --logs --seed` — машина снова «первый раз в жизни», cloud-init отработает заново. Обязательный шаг при подготовке образа.

**11. Где искать datasource, если cloud-init «ничего не нашёл».**

```bash
grep -r datasource_list /etc/cloud/cloud.cfg.d/
sudo grep -i -m5 'datasource' /var/log/cloud-init.log
ls -l /var/lib/cloud/seed/nocloud-net/ 2>/dev/null
```

Если в логе `DataSourceNone` — seed не подключён, метка не `CIDATA` или YAML битый. Это самая частая проблема на стендах.

**12. Проверка ключа и версия образа в имени артефакта.**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_vektor-lab -C "packer@vektor-lab" -N ""
GIT_SHA=$(git -C ~/vektor-infra rev-parse --short HEAD)
echo "ubuntu-base-$(date +%Y%m%d)-${GIT_SHA}"
```

Ключ без пароля (`-N ""`) — только для стенда. Имя образа с датой и git-хешем — минимальная гигиена: по имени артефакта всегда понятно, из какого коммита он собран.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Цель: собрать воспроизводимый golden image Ubuntu 24.04 Packer'ом, проверить его первым запуском с runtime-конфигом cloud-init и положить шаблоны в Git.

### Предусловия

- **UBU01** (10.10.10.30), Ubuntu Server 24.04 LTS — основная машина лабораторной. Нужно: sudo, доступ в интернет (через NAT VMnet8, шлюз 10.10.10.2), минимум 4 vCPU, 4 ГБ RAM, 25 ГБ свободного места (`df -h /`).
- **GIT01** (10.10.10.50) — репозиторий, который вы завели в уроке 61 (там уже лежит каталог с Terraform-конфигурацией). Если репозиторий называется иначе — используйте своё имя, главное не создавайте второй параллельный.
- **Nested virtualization** включена в настройках ВМ UBU01 в VMware Workstation: VM → Settings → Processors → галка «Virtualize Intel VT-x/EPT or AMD-V/RVI». Без неё сборка пойдёт без KVM (медленно, но результат тот же).
- Windows-часть стенда (DC01, FS01) **не нужна** — лабораторная полностью на Linux.

### Снапшот

На **UBU01** (и на **GIT01**, если будете коммитить):

VM → Snapshot → Take Snapshot, имя: `before-devops-063`

```bash
# быстрая проверка, что состояние зафиксировано до изменений
sudo apt-get install -y --no-install-recommends qemu-utils 2>/dev/null || true
df -h / | tail -1
```

Запишите в `stand-log.md` строку: `2026-XX-XX урок 063 — снапшот before-devops-063 на UBU01/GIT01`.

### Шаги

1. **UBU01: проверяем KVM.** `ls -l /dev/kvm` и `egrep -c '(vmx|svm)' /proc/cpuinfo`. Ожидаемо: устройство есть, счётчик > 0. Если нет — включить nested virt в VMware, перезагрузить UBU01 и повторить.

2. **UBU01: ставим пакеты.** `sudo apt-get update && sudo apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils packer` (Packer — по инструкции из раздела 4, п.3). Ожидаемо: `packer version` печатает версию, `qemu-system-x86_64 --version` — версию QEMU.

3. **UBU01: готовим каталог шаблона в репозитории.**

```bash
cd ~/vektor-infra && git pull --ff-only || true
mkdir -p packer/ubuntu-base/{http,runtime,scripts}
cd packer/ubuntu-base
```

Ожидаемо: `tree .` показывает `http/`, `runtime/`, `scripts/`.

4. **UBU01: пишем шаблон `ubuntu-base.pkr.hcl`.**

```hcl
packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = ">= 1.0.0"
    }
  }
}

variable "image_url" {
  type    = string
  default = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}

variable "ssh_private_key_file" {
  type      = string
  default   = "/home/adm.student/.ssh/id_ed25519_vektor-lab"
  sensitive = true
}

source "qemu" "ubuntu_base" {
  iso_url          = var.image_url
  iso_checksum     = "file:https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
  disk_image       = true
  output_directory = "output-ubuntu-base"
  vm_name          = "ubuntu-base"
  format           = "qcow2"
  accelerator      = "kvm"        # нет /dev/kvm -> "tcg"
  cpus             = 2
  memory           = 2048
  headless         = true
  ssh_username         = "adm.student"
  ssh_private_key_file = var.ssh_private_key_file
  ssh_timeout          = "10m"
  qemuargs = [
    ["-drive", "file=seed-build.iso,if=virtio,format=raw"]
  ]
}

build {
  name    = "ubuntu-base"
  sources = ["source.qemu.ubuntu_base"]

  provisioner "shell" {
    execute_command = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
    scripts         = ["scripts/01-cleanup.sh"]
  }

  post-processor "manifest" {
    output = "manifest.json"
  }
}
```

Ключевое: `disk_image = true` говорит, что `iso_url` — это уже готовый диск, а не установочный ISO (Ubuntu cloud image грузится без установщика). `iso_checksum = "file:<URL>"` заставляет Packer скачать SHA256SUMS и сверить хеш — защита от подмены образа.

5. **UBU01: пишем build-time конфиг cloud-init `http/user-data`.**

```yaml
#cloud-config
hostname: packer-build
fqdn: packer-build.corp.vektor.lab
prefer_fqdn_over_hostname: true
users:
  - name: adm.student
    gecos: "Vektor lab admin (build-time)"
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - REPLACE_WITH_YOUR_PUBLIC_KEY
ssh_pwauth: false
disable_root: true
package_update: true
packages:
  - qemu-guest-agent
  - chrony
  - curl
  - vim
  - htop
  - ca-certificates
write_files:
  - path: /etc/motd.d/vektor-lab
    content: |
      Учебный стенд «Вектор». Образ собран Packer, настроен cloud-init.
runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
```

`REPLACE_WITH_YOUR_PUBLIC_KEY` замените на содержимое `~/.ssh/id_ed25519_vektor-lab.pub`. Пароля нет намеренно: Packer заходит по ключу, `lock_passwd: true` блокирует парольный вход.

6. **UBU01: `http/meta-data` и seed.**

```yaml
# http/meta-data
instance-id: packer-build-001
local-hostname: packer-build
```

```bash
cloud-localds seed-build.iso http/user-data http/meta-data
blkid seed-build.iso | grep CIDATA
cloud-init schema --config-file http/user-data
```

Ожидаемо: `LABEL="CIDATA"` и `Valid cloud-config: user-data`. Если schema ругается — правьте YAML **до** сборки, это дешевле 5 минут ожидания.

7. **UBU01: init + validate.**

```bash
packer init .
packer fmt -recursive .
packer validate .
```

Ожидаемо: `The configuration is valid.`

8. **UBU01: сборка.**

```bash
PACKER_LOG=1 packer build -on-error=ask .
```

Ожидаемо: `Waiting for SSH...` → `Connected to SSH!` → `Running post-processor: manifest` → `finished after N minutes`. Сборка качает ~600 МБ образа в `packer_cache/` — на первом запуске это основное время.

9. **UBU01: скрипт очистки `scripts/01-cleanup.sh`.** Он выполняется уже внутри собираемой ВМ, поэтому создаём его после первого прогона (или сразу, если хотите собрать с первого раза):

```bash
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get -y update
apt-get -y upgrade
apt-get -y autoremove --purge
apt-get -y clean
rm -rf /var/lib/apt/lists/*
cloud-init clean --logs --seed || true
truncate -s 0 /etc/machine-id
rm -f /etc/ssh/ssh_host_*
rm -f /root/.bash_history /home/*/.bash_history
```

Ожидаемо: повторная сборка проходит, в логе — вывод `apt-get`, в конце `Build ... finished`. Обязательный `chmod +x scripts/01-cleanup.sh`.

10. **UBU01: проверяем артефакт.**

```bash
ls -la output-ubuntu-base/
cat manifest.json
qemu-img info output-ubuntu-base/ubuntu-base.qcow2
```

Ожидаемо: файл образа (точное имя — `ubuntu-base` или `ubuntu-base.qcow2`, `[ПРОВЕРИТЬ]` по вашей версии плагина `qemu`), `manifest.json` с `artifact_id` и sha256. В `qemu-img info`: `file format: qcow2`, `virtual size` около 3–4 ГБ.

11. **UBU01: runtime-конфиг и тестовый первый запуск.** Создаём `runtime/user-data` — то, что получит **любая** машина из образа:

```yaml
#cloud-config
hostname: img-test
fqdn: img-test.corp.vektor.lab
prefer_fqdn_over_hostname: true
users:
  - name: adm.student
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
    ssh_authorized_keys:
      - REPLACE_WITH_YOUR_PUBLIC_KEY
chpasswd:
  expire: true
  users:
    - {name: adm.student, password: "Lab#2026-vektor", type: text}
ssh_pwauth: true
runcmd:
  - [ sh, -c, "echo 'runtime cloud-init ok' > /tmp/ci-ok" ]
```

```bash
printf 'instance-id: img-test-001\nlocal-hostname: img-test\n' > runtime/meta-data
cloud-localds seed-runtime.iso runtime/user-data runtime/meta-data
qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 \
  -drive file=output-ubuntu-base/ubuntu-base.qcow2,if=virtio,format=qcow2 \
  -drive file=seed-runtime.iso,if=virtio,format=raw \
  -netdev user,id=n0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=n0 \
  -nographic
```

`img-test` — **временная** машина вне стенда, живёт только внутри лабораторной. Внутри (серийная консоль; если её нет — `[ПРОВЕРИТЬ]`, заходите по `ssh -p 2222 adm.student@localhost`): `hostname` → `img-test`, `cloud-init status --long` → `done`, `cat /tmp/ci-ok` → строка есть. Выход из QEMU — `Ctrl-A`, затем `X`.

12. **UBU01 → GIT01: коммит шаблонов и фиксация в журнале.**

```bash
cd ~/vektor-infra
cat > packer/ubuntu-base/.gitignore <<'EOF'
packer_cache/
output-*/
manifest.json
seed-*.iso
*.qcow2
*.vmdk
EOF
git add packer/ubuntu-base
git commit -m "packer: golden image ubuntu-base (cloud-init + qemu builder)"
git push
```

Ожидаемо: в GitLab на GIT01 появился коммит с шаблоном; секретов и образов в репозитории нет. После этого записываем изменения в `stand-log.md`.

### Проверка

- [ ] `packer version` и `qemu-system-x86_64 --version` отвечают на UBU01.
- [ ] `packer validate .` возвращает `The configuration is valid.`
- [ ] `blkid seed-build.iso` содержит `LABEL="CIDATA"`.
- [ ] `cloud-init schema --config-file http/user-data` не выдаёт ошибок.
- [ ] Сборка завершилась строкой `Build ... finished`, в `output-ubuntu-base/` есть файл образа.
- [ ] `manifest.json` содержит sha256 артефакта.
- [ ] Тестовый запуск: `hostname` = `img-test`, `cloud-init status --long` = `done`.
- [ ] В образе **не** осталось `packer-build` в hostname, следов пароля и `/etc/ssh/ssh_host_*`.
- [ ] Шаблоны закоммичены в Git на GIT01, `.gitignore` исключает `seed-*.iso` и `*.qcow2`.

### Сломай и почини

**Пользователь:** adm.student (он же исполнитель). **ВМ:** UBU01 + временная `img-test`. **Что написал:** «Собрал образ, запустил с runtime-seed — вместо `img-test` машина зовётся `ubuntu`, пользователя `adm.student` нет, SSH по ключу не пускает. В консоли "cloud-init ... error"». **Срочность:** высокая (блокирует выдачу тестового сервера Петровой).

**Как воспроизвести поломку.** В `runtime/user-data` сломайте отступ у `ssh_authorized_keys` (сдвиньте на один пробел меньше, чем нужно по вложенности), пересоберите seed и запустите временную ВМ из шага 11.

**Диагностика по порядку:**

1. `cloud-init status --long` в консоли `img-test`. Видим `status: error` и стадию, где упало. Если `status: done`, но hostname `ubuntu` — cloud-init не нашёл datasource, а не «сломался».
2. `blkid` внутри гостя — есть ли диск с `LABEL="CIDATA"`. Нет → проблема в QEMU-команде (не подключили второй `-drive`).
3. `sudo cat /var/log/cloud-init.log | grep -i -m10 datasource` — ищем `DataSourceNoCloud` (хорошо) или `DataSourceNone` (плохо).
4. `sudo cloud-init schema --config-file /var/lib/cloud/instance/user-data.txt` — здесь и всплывает YAML-ошибка вида `yaml: line 9: did not find expected key`.
5. `sudo cat /var/log/cloud-init-output.log | tail -30` — что именно не выполнилось.

**Решение.** Исправить отступ, `cloud-init schema --config-file runtime/user-data` (валидно → идём дальше), пересоздать `cloud-localds seed-runtime.iso ...`, перезапустить QEMU. Обязательно внутри гостя `sudo cloud-init clean --logs --seed` перед следующей попыткой, иначе cloud-init решит, что уже отработал, и ничего не сделает.

**Ответ пользователю:** «Проблема была в конфиге cloud-init, который выдаётся при первом запуске: битый YAML, из-за этого машина стартовала с дефолтами. Образ при этом целый, правим только runtime-конфиг. Тестовый сервер будет сегодня, жду вашего подтверждения по размеру диска». **Закрытие заявки:** тип «конфигурация cloud-init», в решении — ссылка на коммит с исправлением и на пункт «добавить `cloud-init schema` в CI-проверку перед сборкой образа».

### Что изменилось на стенде

- **UBU01:** установлены `qemu-system-x86`, `qemu-utils`, `cloud-image-utils`, `packer`; каталог `~/vektor-infra/packer/ubuntu-base/` с шаблоном, `http/`, `runtime/`, `scripts/`; кэш `packer_cache/`; артефакт `output-ubuntu-base/` (golden image Ubuntu 24.04 + `manifest.json`).
- **UBU01:** создан SSH-ключ `~/.ssh/id_ed25519_vektor-lab` (+ `.pub`), используется как ключ доступа в образы.
- **GIT01:** в репозитории появился каталог `packer/` с коммитом шаблонов и `.gitignore`.
- **Временная ВМ `img-test`** — вне стенда, после лабораторной удаляется (файл образа остаётся как артефакт).
- **Не менялись:** DC01, FS01, WIN11-CL01, WIN10-CL02, ASTRA01, WEB01, MON01, HARBOR01, k8s-узлы.

### Откат

1. Выключить временную ВМ `img-test` (`Ctrl-A`, затем `X` в окне QEMU) и удалить `seed-runtime.iso`, если он мешает.
2. На **UBU01**: VM → Snapshot → Snapshot Manager → выбрать `before-devops-063` → **Revert**. То же на **GIT01**, если коммит получился «грязным» (в нём оказались образы или ключи).
3. Если откатываться не хочется, а мусор убрать нужно: `sudo apt-get remove --purge packer qemu-system-x86 cloud-image-utils && rm -rf ~/vektor-infra/packer/ubuntu-base/packer_cache output-*`.
4. После любого отката — запись в `stand-log.md`: что откатили и почему.

## 6. Ошибки новичков и безопасность

1. **Запекают в образ уникальное** (hostname, IP, SSH host keys, `machine-id`) → клоны конфликтуют в сети, DHCP выдаёт одни и те же имена, SSH ругается на смену ключа хоста. Правильно: уникальное — только через cloud-init на первом старте, в образе всё вычищено скриптом `01-cleanup.sh`.
2. **Кладут секреты в шаблон и `user-data`** → секрет навсегда в истории Git и в слоях образа. Правильно: `sensitive = true` для переменных, пароли/токены — из Vault (урок 66) или переменных CI, в user-data — только публичный ключ.
3. **Парольный SSH-вход для Packer (`ssh_password`)** → пароль попадает в лог `PACKER_LOG=1` и в историю команд. Правильно: временный build-пользователь + ключ, после сборки пользователь удаляется/остаётся без пароля.
4. **Собирают образ «когда вспомнят»** → образ живёт полгода с непропатченным OpenSSH. Правильно: регулярная пересборка по расписанию в CI (GIT01 + runner из урока про CI/CD), версия образа = дата + git-sha.
5. **Не проверяют чек-сумму исходного образа** → `iso_checksum` забыт, скачался битый или подменённый файл. Правильно: `iso_checksum = "file:<URL SHA256SUMS>"` и всегда `post-processor manifest`.
6. **Ставят пакеты без фиксации версий** → две сборки из одного шаблона дают разные образы. Правильно: пинить ключевые версии, а всё остальное добирать `apt-get upgrade` в контролируемый момент и фиксировать в манифесте.
7. **Чинят золотой образ руками после сборки** → образ перестаёт быть воспроизводимым, «дрейф» вернулся. Правильно: любое изменение — коммит в шаблон и новая сборка.

**Безопасность.** В боевой среде категорически нельзя: собирать образы на своей рабочей ВМ (builder видит ваши ключи и сеть), оставлять в образе `sudo NOPASSWD:ALL` для build-пользователя, хранить seed-ISO с паролями на общем файловом ресурсе, использовать образы «с рук» без проверки подписи/чек-суммы. Packer-хост в нормальной компании — отдельная изолированная машина или CI-раннер, у него есть доступ только к репозиторию шаблонов и к хранилищу образов; сам образ после сборки подписывается и сканируется на CVE (это тема урока 65). И про лицензии: для Windows-образов берите только Evaluation-редакции (Windows Server 2022 Evaluation, Windows 11 Enterprise Evaluation) и официальные ISO с сайта Microsoft — никаких «сборок от Васи» и активаторов.

## 7. Вопросы на собеседовании

**1. «Чем golden image лучше, чем "настроил ВМ руками и сделал клон"?»**

Ответ: golden image — версионируемый артефакт, собранный из кода. Его можно пересобрать, он одинаков на dev/stage/prod, у него есть дата сборки и чек-сумма, его можно просканировать на CVE и отозвать. Клон ручной ВМ тащит внутрь весь «мусор»: историю команд, лишние пакеты, следы ручных правок, а также уникальные идентификаторы, из-за которых клоны конфликтуют. Плюс клон невоспроизводим: через месяц никто не скажет, почему на нём стоит именно этот конфиг. Golden image — это применение принципа immutable infrastructure к ВМ: изменения через пересборку, а не через SSH.

*Что хочет услышать:* слова «воспроизводимость», «версионирование», «immutable», «отсутствие дрейфа». *Красный флаг:* «ну клонировать быстрее, а так одно и то же» — человек не понимает, зачем IaC вообще.

**2. «Как Packer работает под капотом? Что такое builder, provisioner, post-processor?»**

Ответ: Packer читает HCL-шаблон, по блоку `source` поднимает ВМ через выбранный builder (`qemu`, `vsphere-iso`, `amazon-ebs`), подключается внутрь через communicator (SSH для Linux, WinRM для Windows), выполняет provisioner'ы (shell, ansible, file), затем post-processor'ы (compress, manifest, vagrant) и гасит ВМ. Каждый билд идёт параллельно по всем источникам; `-only` и `-parallel-builds` управляют этим. Плагины ставятся через `packer init` из `required_plugins`, версии фиксируются. Проверки в CI: `packer fmt -check`, `packer validate`, потом `build`.

*Что хочет услышать:* понимание жизненного цикла билда, роли communicator и того, что артефакт — это файл, а не «настроенная машина». *Красный флаг:* путаница Packer с Terraform («Packer создаёт инфраструктуру») — Packer создаёт **образ**, ВМ создаёт Terraform.

**3. «Где граница между образом и cloud-init? Что запекать, а что отдавать на первый старт?»**

Ответ: в образе — всё, что одинаково для всех экземпляров и меняется редко: базовые пакеты, обновления безопасности, агент гипервизора, мониторинг-агент, настройки `chrony`, политики SSH (базовые), сертификаты. В cloud-init — всё уникальное и зависящее от окружения: hostname/FQDN, сеть, пользователи и SSH-ключи, привязка к конфигурации приложения, `runcmd` для первичной инициализации, монтирование дисков. Признак правильного деления: если значение может отличаться у двух ВМ из одного образа — оно не должно быть запечено.

*Что хочет услышать:* чёткое правило и понимание, что cloud-init выполняется **один раз** (плюс `per-instance` модули). *Красный флаг:* «всё запекаю в образ, cloud-init не нужен» — значит, каждая ВМ требует ручной доводки.

**4. «Как вы обновляете golden images и следите за их безопасностью?»**

Ответ: пересборка по расписанию (еженедельно/по мере выхода CVE) и по каждому изменению шаблона в CI; версия образа = дата + git-sha; `manifest.json` с чек-суммой и публикация в хранилище образов (vSphere Content Library, HCP Packer registry, образ в облаке); сканирование образа на CVE и проверка политиками (OPA/Conftest, checkov) до публикации; старые версии не удаляются сразу, а помечаются deprecated, чтобы можно было откатиться; в облаке — «золотой» образ используется как база для Terraform-модуля, поэтому обновление образа = смена версии переменной, а не ручная правка.

*Что хочет услышать:* регулярность, версионирование, автоматизация в CI, план отката. *Красный флаг:* «обновляем, когда что-то сломается».

**5. «Пользователь говорит: "машина поднялась, но hostname дефолтный и меня не пускает по ключу". Ваши действия?»**

Ответ: сначала `cloud-init status --long` — done или error. Если error — смотрим `/var/log/cloud-init.log` и `/var/log/cloud-init-output.log`. Если done, но hostname дефолтный — почти наверняка `DataSourceNone`: не подключён seed, неверная метка тома (должна быть `CIDATA`), битый YAML или cloud-init уже отработал ранее (`/var/lib/cloud/` не очищен). Проверяю `blkid` на наличие CIDATA, `cloud-init schema --config-file` на валидность YAML, при необходимости `cloud-init clean --logs --seed` и перезапуск. Дальше — правка runtime-конфига, пересоздание seed, перезапуск; в перспективе — валидация `user-data` в CI перед выдачей.

*Что хочет услышать:* порядок диагностики «статус → лог → datasource → схема YAML» и понимание, что образ тут скорее всего ни при чём. *Красный флаг:* «пересоберу образ» сразу, без диагностики.

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — документация Packer. Разделы: «Packer Documentation → Builders → QEMU Builder», «Provisioners → Shell», «Post-Processors → Manifest», «Templates → HCL reference», «Commands → build / init / validate / fmt / inspect», «HCP Packer». Поиск: `packer qemu builder disk_image`, `packer iso_checksum file url`, `packer manifest post-processor`.
- **developer.hashicorp.com/packer/install** — установка через apt-репозиторий HashiCorp и проверка контрольных сумм релизов. Поиск: `hashicorp apt repository install packer`.
- **cloud-init.readthedocs.io** — официальная документация cloud-init. Разделы: «Datasources → NoCloud», «Modules» (users, packages, write_files, runcmd, chpasswd), «Debugging cloud-init», «CLI commands». Поиск: `cloud-init NoCloud datasource CIDATA`, `cloud-init debugging status long`.
- **cloud-images.ubuntu.com** — официальные cloud-образы Ubuntu и файлы `SHA256SUMS`. Раздел: `noble/current/`. Поиск: `ubuntu cloud images noble current`.
- **ubuntu.com** — раздел про cloud-init на Ubuntu и cloud images (общие статьи, политика поддержки). Поиск: `ubuntu cloud-init documentation`.
- **qemu.org** — документация QEMU: `qemu-system` (параметры `-drive`, `-netdev user`, `-nographic`) и `qemu-img` (`convert`, `info`, `resize`). Поиск: `qemu-img convert vmdk`, `qemu user mode networking hostfwd`.
- **learn.microsoft.com** — на будущее для Windows-образов: «Autounattend.xml», «Sysprep», «WinRM» и Evaluation-версии Windows Server. Поиск: `windows server evaluation download`, `sysprep generalize`.
- Точная версия Packer и плагина `qemu` на момент чтения — `[ПРОВЕРИТЬ]` на developer.hashicorp.com; путь установки плагинов и точное имя выходного файла образа — `[ПРОВЕРИТЬ]` командой `ls` после первой сборки.
