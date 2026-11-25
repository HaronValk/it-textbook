# Образы и шаблоны: Packer, Cloud-Init, golden image, автоматическая сборка

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №4127.** От: Петрова М. (HR). ВМ: WIN10-CL02. Текст: «В понедельник выходят пять стажёров склада. Нужны рабочие места „как у Иванова“: те же программы, тот же принтер в бухгалтерии, те же папки. И ещё одно такое же — Сидорову на замену ноутбука. Срочность: высокая, дедлайн — пятница».

Вы открываете `stand-log.md` и видите историю: WIN11-CL01 ставился вручную два вечера, FS01 — ещё день. Пять раз по два вечера — это не работа сисадмина, это работа установщика Windows. И главная проблема даже не время, а то, что «как у Иванова» — это не документированный набор софта и настроек, а состояние одной конкретной машины, которое никто не зафиксировал.

После этой главы вы будете уметь: собирать эталонный образ (golden image) автоматически, а не руками; отделять «одинаковое для всех» от «уникального для каждой машины»; персонализировать клон при первом запуске через cloud-init; версионировать образы и обновлять их по расписанию. Это ровно тот навык, который отличает сисадмина, обслуживающего 50 машин, от инженера, обслуживающего 500.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Golden image (эталонный образ)** — это предустановленная и настроенная ОС, из которой делают копии. Ключевое правило: в образе есть всё общее и **нет ничего уникального**. Ни hostname, ни IP, ни паролей, ни ключей, ни лицензионных привязок, ни machine-id, ни SSH host keys, ни SID (в Windows). Уникальное рождается при первом запуске копии — из шаблона персонализации.

**Четыре уровня в цепочке:**

| Уровень | Что это | Кто создаёт уникальность |
|---|---|---|
| ISO | заводской установщик | — |
| Golden image | ОС + пакеты + настройки, «выключенная» | — |
| Template | образ, подключённый как read-only ВМ (в Proxmox — VMID 9000+) | — |
| Clone | рабочая копия | cloud-init (Linux) / sysprep+unattend (Windows) |

**Packer** (HashiCorp) — сборщик образов. Работает по трём сущностям: *builder* (где и из чего собирать: `qemu`, `proxmox-iso`, `proxmox-clone`, `vmware-iso`, `virtualbox-iso`), *provisioner* (что доустановить: `shell`, `ansible`, `file`, `powershell`, `windows-restart`), *post-processor* (что сделать с артефактом: сжать, загрузить). Всё описывается в HCL2-файлах, лежит в Git (урок 74), проверяется `packer validate` и запускается в CI (урок 80).

**Cloud-init** — агент первичной инициализации Linux-машины при первом старте. Он читает *datasource* — источник данных о том, какой эта машина должна стать. Нас интересует **NoCloud**: это либо ISO-образ с меткой тома `CIDATA`, внутри которого лежат файлы `user-data` и `meta-data`, либо SMBIOS-серийник с путём. В Proxmox «CloudInit Drive» — это как раз такой ISO с меткой `cidata`, который гипервизор собирает из полей конфигурации ВМ. Внутри гостя cloud-init проходит стадии: *generator → local → network → config → final*; результат виден в `cloud-init status`.

Минимальный `user-data` (cloud-config, YAML):

```yaml
#cloud-config
hostname: cl-tst-01
users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ssh-ed25519 AAAA... adm.student@VEKTOR
package_update: true
packages: [qemu-guest-agent, nginx]
runcmd:
  - systemctl enable --now qemu-guest-agent
```

`meta-data` — про идентичность (`instance-id`, `local-hostname`). Есть ещё `network-config` (сеть до старта сети) и `vendor-data` (данные провайдера).

**Windows** работает иначе: там нет cloud-init «из коробки», вместо него — `sysprep /generalize` (сбрасывает SID, hostname, драйверные привязки) плюс `unattend.xml` (файл ответов для OOBE). В Proxmox для Windows-шаблонов используют тот же CloudInit Drive, но с Sysprep-совместимым `unattend.xml` — детали зависят от версии.

**Порты и протоколы:** Packer общается с билд-ВМ по SSH (22/TCP) или WinRM (5985/5986); с Proxmox — по API (8006/TCP, HTTPS, токен `PVEAPIToken`); ISO/образы забирает по HTTPS с зеркала; QEMU поднимает локальную ВМ через KVM (`/dev/kvm`).

```text
[UBU01 10.10.10.30]  packer build
        |
        | qemu/KVM + seed ISO (метка CIDATA: user-data, meta-data)
        v
   временная ВМ  ---SSH 22---> provisioners (shell/ansible)
        |
        v
  ubuntu2404-golden.qcow2  (артефакт, кладётся в Git LFS / реестр)
        | scp 22
        v
[PVE01 10.10.10.70]
   qm importdisk -> scsi0 -> qm template 9000 (tpl-ubuntu2404)
        |
        | qm clone --full
        v
   VM 301 (10.10.10.81) + CloudInit Drive
        | первый старт
        v
   cloud-init: hostname, IP, ключи, пакеты, machine-id
```

## 3. Аналогия из жизни

Пекарня. Есть форма для печенья (golden image): один раз выточили, дальше штампуешь сколько нужно, и все печенья одинаковые. Начинку (hostname, IP, пользователя) кладут уже в каждое печенье отдельно — иначе у вас пять одинаковых комков теста с одним именем и одним SSH-ключом, и мониторинг сходит с ума. Форму нельзя пачкать уникальным: если вдавить в неё букву «И», то буква «И» отпечатается на всём, что вы потом напечатаете. И форму нужно регулярно перетачивать — патчи безопасности выходят каждый месяц, а старая форма печатает старую уязвимость.

Аналогия ломается в одном: печенье после выпечки уже не меняется, а живая ВМ продолжает жить, дрейфовать и накапливать изменения — поэтому образ надо не «сохранить навсегда», а пересобирать по расписанию.

## 4. Практика и команды

**1. Версия и инициализация плагинов.** С версии Packer 1.7 плагины (в том числе `qemu`) вынесены наружу, поэтому сначала `init`.

```bash
packer version
packer init .
```

Вывод: `Packer v1.x.y` и `Installed plugin github.com/hashicorp/qemu v1.x.x`. `init` читает блок `packer { required_plugins {} }` и качает плагин в `~/.config/packer/plugins`.

**2. Форматирование и валидация — то, что гоняют в CI.**

```bash
packer fmt -check -diff .
packer validate -syntax-only .
```

`fmt -check` возвращает ненулевой код, если файл не отформатирован — идеально для пайплайна. `validate` проверяет схему HCL и значения переменных, но **не** проверяет, что сервер Proxmox доступен.

**3. Описание источника (builder `qemu`) — ядро главы.**

```hcl
packer {
  required_plugins {
    qemu = { source = "github.com/hashicorp/qemu", version = "~> 1.1" }
  }
}

variable "img_url" {
  default = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}

source "qemu" "ubuntu2404" {
  iso_url          = var.img_url
  iso_checksum     = "file:https://cloud-images.ubuntu.com/noble/current/SHA256SUMS"
  disk_image       = true
  output_directory = "output-ubuntu2404"
  vm_name          = "ubuntu2404-golden.qcow2"
  format           = "qcow2"
  accelerator      = "kvm"
  disk_size        = "10240M"
  memory           = 2048
  cpus             = 2
  net_device       = "virtio-net"
  disk_interface   = "virtio"
  communicator     = "ssh"
  ssh_username     = "ubuntu"
  ssh_password     = "Lab#2026-vektor"
  ssh_timeout      = "20m"
  cd_label         = "cidata"
  cd_files         = ["cloud-init/user-data", "cloud-init/meta-data"]
  shutdown_command = "sudo shutdown -P now"
}

build {
  sources = ["source.qemu.ubuntu2404"]
  provisioner "shell" {
    inline = ["sudo cloud-init status --long", "sudo apt-get -y upgrade"]
  }
}
```

Разбор: `disk_image = true` говорит, что вместо установщика мы грузим готовый cloud-образ и дописываем его; `iso_checksum = "file:..."` — Packer скачает файл контрольных сумм и найдёт в нём имя образа (защита от подмены); `cd_label = "cidata"` + `cd_files` — вот она, NoCloud-подложка; `accelerator = "kvm"` требует доступа к `/dev/kvm`.

**4. Сборка.**

```bash
PACKER_LOG=1 packer build -on-error=ask -var "disk_size=15G" .
```

`-on-error=ask` оставляет ВМ живой при падении и ждёт вашего решения — это спасает часы отладки. В конце: `Build 'qemu.ubuntu2404' finished after 12 minutes`, артефакт в `output-ubuntu2404/`.

**5. Проверка артефакта без запуска.**

```bash
qemu-img info output-ubuntu2404/ubuntu2404-golden.qcow2
```

Смотрим `virtual size` и `disk size` — если virtual 10 GiB, а реальный 900 MiB, образ хорошо сжат (thin).

**6. Точечная правка образа без загрузки — `virt-customize`.**

```bash
sudo apt install -y libguestfs-tools
sudo virt-customize -a ubuntu2404-golden.qcow2 \
  --install qemu-guest-agent,curl \
  --run-command 'systemctl enable qemu-guest-agent' \
  --truncate /etc/machine-id
```

`virt-sysprep -a image.qcow2` — LINUX-аналог sysprep: чистит логи, историю bash, SSH host keys, machine-id. Прогонять перед упаковкой в шаблон.

**7. Импорт в Proxmox (на PVE01).**

```bash
qm create 9000 --name tpl-ubuntu2404 --memory 2048 --cores 2 \
  --net0 virtio,bridge=vmbr0 --ostype l26 --scsihw virtio-scsi-pci
qm importdisk 9000 /root/ubuntu2404-golden.qcow2 local-lvm
qm set 9000 --scsi0 local-lvm:vm-9000-disk-0 --boot c --bootdisk scsi0 \
  --serial0 socket --vga serial0
```

`importdisk` печатает `Successfully imported disk as 'unused0:local-lvm:vm-9000-disk-0'` — имя тома берите из этой строки. `local-lvm` — проверьте своим `pvesm status`, у разных установок бывает `local-lvm` или `local-zfs`.

**8. Превращение в шаблон и клонирование.**

```bash
qm set 9000 --ide2 local-lvm:cloudinit
qm template 9000
qm clone 9000 301 --name cl-tst-01 --full --storage local-lvm
```

`qm template` делает ВМ read-only базой. `--full` — полная копия (независимая), без `--full` — linked clone (быстро, но зависит от базы: удалите базу — умрут все клоны).

**9. Персонализация клона через поля cloud-init.**

```bash
qm set 301 --ciuser ubuntu --cipassword 'Lab#2026-vektor' \
  --sshkeys /root/adm.pub \
  --ipconfig0 ip=10.10.10.81/24,gw=10.10.10.2 --nameserver 10.10.10.10
qm cloudinit update 301
qm start 301
```

**10. Что именно гипервизор положил в конверт гостя.**

```bash
qm cloudinit dump 301 user
qm cloudinit dump 301 network
```

Печатает сгенерированный `user-data` и `network-config` — этим вы проверяете, что пароль/ключ/IP реально попали в ISO, ещё до старта.

**11. Диагностика внутри гостя.**

```bash
cloud-init status --long
cloud-init analyze show
sudo journalctl -u cloud-init --no-pager | tail -40
ls /var/lib/cloud/instances/
```

`status --long` → `status: done`, и список стадий. `analyze show` показывает, сколько секунд заняла каждая стадия — если `network` висит 120 с, ищите проблему с DNS/DHCP. `instances/` — каталог с `instance-id`; пусто → cloud-init не нашёл datasource.

**12. Сброс перед повторной «первой» загрузкой.**

```bash
sudo cloud-init clean --logs --seed
sudo truncate -s 0 /etc/machine-id
sudo rm -f /etc/ssh/ssh_host_*
```

Без этого клон останется «уже инициализированным» и персонализации не будет.

**13. Windows: подготовка эталона к клонированию.**

```powershell
C:\Windows\System32\Sysprep\sysprep.exe /generalize /oobe /shutdown /unattend:C:\unattend.xml
```

После выключения ВМ — конвертировать в шаблон. Лог разбора ошибок: `C:\Windows\Panther\setupact.log` и `setuperr.log`. Проверка образа перед шаблоном: `Get-WindowsImage -ImagePath D:\sources\install.wim` (для WIM-сборок).

**14. API-токен для Packer вместо пароля root.**

```bash
pveum user add packer@pve --comment "Packer builder"
pveum acl modify / --users packer@pve --roles PVEVMAdmin
pveum user token add packer@pve build --privsep 1
pveum acl modify / --tokens 'packer@pve!build' --roles PVEVMAdmin
```

Токен показывается один раз — сохраните. Дальше `builder "proxmox-iso"` использует `proxmox_url = "https://10.10.10.70:8006/api2/json"`, `token = "packer@pve!build=xxxxxxxx"`. Пароль root в CI не кладём никогда.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **PVE01** (10.10.10.70, Proxmox VE) — работает, `vmbr0` смотрит в VMnet8, SSH root доступен. Это машина трека «сисадмин», она у вас уже есть.
- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — билд-машина, SSH-доступ изнутри стенда.
- На хосте в VMware для PVE01 включена вложенная виртуализация: *VM → Settings → Processors → Virtualize Intel VT-x/EPT*. **Если она выключена, `accelerator = "kvm"` не заработает** — тогда поставьте в HCL `accelerator = "tcg"` (соберётся, но в 5–10 раз медленнее).
- Учётная запись `adm.student`, домен `corp.vektor.lab` — для единообразия; `ubuntu` в билд-образе — служебная учётка cloud-образа, пароль учебный.

### Снапшот

В VMware Workstation на **PVE01** и **UBU01**: *VM → Snapshot → Take Snapshot*, имя `before-sysadmin-077`. Снапшот PVE01 тяжёлый по диску — это нормально, откат важнее.

### Шаги

1. **UBU01.** Ставим инструменты: `sudo apt update && sudo apt install -y packer qemu-system-x86 qemu-utils cloud-image-utils genisoimage`. Ожидаемо: `packer version` печатает `Packer v1.x`. Если пакета `packer` нет в вашем зеркале Ubuntu — подключите официальный репозиторий HashiCorp (`apt.releases.hashicorp.com`) `[ПРОВЕРИТЬ актуальный способ]`.
2. **UBU01.** Готовим структуру репозитория: `mkdir -p ~/golden/cloud-init && cd ~/golden && git init`. Дальше всё в Git — образ без истории изменений бесполезен.
3. **UBU01.** Генерируем хеш пароля: `openssl passwd -6 'Lab#2026-vektor'` → строка `$6$...`. Записываем `cloud-init/user-data` (см. п.4 теории, добавьте блок `ssh_pwauth: true` и `chpasswd: { expire: false, users: [{name: ubuntu, password: "<хеш>", type: hash}] }`).
4. **UBU01.** Пишем `cloud-init/meta-data`:

```yaml
instance-id: iid-golden-2404
local-hostname: golden-2404
```

5. **UBU01.** Кладём файл `ubuntu2404.pkr.hcl` из п.4.4 практики. Запускаем `packer init . && packer fmt . && packer validate .`. Ожидаемо: `The configuration is valid.`
6. **UBU01.** `PACKER_LOG=1 packer build -on-error=ask .`. Ждём 10–25 минут. В логе видим: загрузку образа, проверку контрольной суммы, SSH-handshake, выполнение provisioner'ов, `sudo shutdown -P now`, `Build 'qemu.ubuntu2404' finished`. В каталоге появляется `output-ubuntu2404/ubuntu2404-golden.qcow2`.
7. **UBU01.** `qemu-img info output-ubuntu2404/ubuntu2404-golden.qcow2` — фиксируем virtual size. Прогоняем `sudo virt-sysprep -a output-ubuntu2404/ubuntu2404-golden.qcow2`.
8. **UBU01 → PVE01.** `scp output-ubuntu2404/ubuntu2404-golden.qcow2 root@10.10.10.70:/root/`. Ожидаемо: прогресс 100%, файл на месте (`ls -lh /root/*.qcow2` на PVE01).
9. **PVE01.** `qm create 9000 --name tpl-ubuntu2404 --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 --ostype l26 --scsihw virtio-scsi-pci`. Затем `qm importdisk 9000 /root/ubuntu2404-golden.qcow2 local-lvm` → `Successfully imported disk as 'unused0:local-lvm:vm-9000-disk-0'`.
10. **PVE01.** `qm set 9000 --scsi0 local-lvm:vm-9000-disk-0 --boot c --bootdisk scsi0 --serial0 socket --vga serial0 --ide2 local-lvm:cloudinit`. Затем `qm template 9000`. В UI ВМ стала иконкой-шаблоном, кнопка Start недоступна.
11. **PVE01.** `qm clone 9000 301 --name cl-tst-01 --full --storage local-lvm` и `qm set 301 --ciuser ubuntu --cipassword 'Lab#2026-vektor' --ipconfig0 ip=10.10.10.81/24,gw=10.10.10.2 --nameserver 10.10.10.10`. Затем `qm cloudinit update 301 && qm start 301`.
12. **Внутри клона (через консоль PVE).** `hostname` → `cl-tst-01`; `ip a` → 10.10.10.81/24; `cloud-init status --long` → `status: done`; `cat /etc/machine-id` — сравниваем с выводом `cat /etc/machine-id` на втором клоне (сделайте `qm clone 9000 302 --name cl-tst-02`, IP 10.10.10.82) — **должны различаться**.
13. **PVE01.** `qm cloudinit dump 301 user` — убеждаемся, что в `user-data` есть `ubuntu`, а в `network` — статический IP.
14. **Записываем в `stand-log.md`** всё, что перечислено ниже.

### Проверка

- [ ] `packer build` завершился со статусом «finished», артефакт `.qcow2` существует и открывается `qemu-img info`.
- [ ] На PVE01 есть ВМ 9000 со статусом template, диск `scsi0` подключён, `ide2` — cloudinit.
- [ ] Клоны 301/302 стартуют и получают разные hostname.
- [ ] `/etc/machine-id` у клонов разный и не совпадает с образцом.
- [ ] `cloud-init status --long` на клонах — `done`, без `error`.
- [ ] Клоны пингуются с DC01 (`ping 10.10.10.81`) и видят шлюз 10.10.10.2.
- [ ] `qm cloudinit dump` показывает ожидаемые значения.
- [ ] В `~/golden` сделан минимум один коммит с HCL и cloud-init файлами.

### Сломай и почини

**Пользователь:** Сидоров А. (склад) / ВМ: cl-tst-02 / **Что написал:** «Скопировали шаблон на второй сервер, а он в мониторинге подписан как первый. И SSH орёт, что ключ хоста подменён. Срочность: средняя, но SSH не пускает вообще».

**Как воспроизвести.** На PVE01 отвязать cloud-init от шаблона: `qm set 9000 --delete ide2`. Затем `qm clone 9000 302 --name cl-tst-02 --full` и старт. Клон унаследует hostname и machine-id образа, cloud-init не запустится.

**Диагностика по порядку.**
1. Конфиг ВМ: `qm config 302 | grep -Ei 'ide2|ciuser|ipconfig'` — пусто, значит персонализации ждать неоткуда.
2. В гостя через консоль: `cloud-init status --long`. Если `status: disabled` или нет записи — сервис не нашёл datasource.
3. `ls /var/lib/cloud/instances/` — пусто или один старый каталог.
4. `hostname` → `golden-2404` (имя из meta-data сборки) вместо `cl-tst-02`.
5. `cat /etc/machine-id` на 301 и 302 — одинаковые.
6. `sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` на обоих — одинаковый fingerprint.

**Решение.** `qm set 302 --ide2 local-lvm:cloudinit --ciuser ubuntu --ipconfig0 ip=10.10.10.82/24,gw=10.10.10.2 --nameserver 10.10.10.10`, затем `qm cloudinit update 302`. В гостя: `sudo cloud-init clean --logs --seed && sudo truncate -s 0 /etc/machine-id && sudo rm -f /etc/ssh/ssh_host_* && sudo reboot`. После ребута проверяем п.12 лабораторной.

**Ответ пользователю:** «Разобрался: второй сервер был клонирован с шаблона без подключённого конфигурационного диска, поэтому он не прошёл первичную настройку и унаследовал имя и ключи первого. Подключил конфигурацию, переинициализировал — хост в мониторинге теперь `cl-tst-02`, адрес 10.10.10.82, SSH пускает. Проверьте, пожалуйста, доступ к `\\FS01\Obshchee` — со стороны сервера доступ есть».

**Закрытие:** категория «инфраструктура/шаблоны», решение в базу знаний, тикет-шаблон «найден клон без cloud-init», и обязательный пункт в чек-лист сборки шаблона: «`ide2` подключён до `qm template`».

### Что изменилось на стенде

- **UBU01**: установлены `packer`, `qemu-system-x86`, `qemu-utils`, `libguestfs-tools`; создан репозиторий `~/golden` (HCL + cloud-init); локально собран артефакт `ubuntu2404-golden.qcow2`.
- **PVE01**: ВМ 9000 `tpl-ubuntu2404` в статусе template (2 vCPU / 2 ГБ / scsi0 на `local-lvm` + CloudInit Drive); клоны 301 `cl-tst-01` (10.10.10.81) и 302 `cl-tst-02` (10.10.10.82).
- **DC01**: без изменений ролей; адреса .81/.82 вне пула DHCP 10.10.10.100–150 — конфликтов нет.
- **Домен**: новых учёток нет. Клоны **не введены в домен** — это осознанно, вводить будете отдельно и только после проверки уникальности SID/machine-id.
- **Новые сущности для следующих глав**: шаблон 9000 как база для Terraform (урок 76) и Ansible (урок 75).

### Откат

Если что-то пошло не так: в VMware *VM → Snapshot Manager → before-sysadmin-077 → Revert* на PVE01 и UBU01. Если снапшот не нужен, а мусор остался — на PVE01: `qm destroy 301`, `qm destroy 302`, `qm destroy 9000` (сначала клоны, потом шаблон). Файлы в `~/golden` не удаляем — это ваш портфолио-артефакт.

## 6. Ошибки новичков и безопасность

1. **Запекают в образ пароли, ключи и токены.** → Утечка при первой же передаче образа; смена пароля означает пересборку всего. → Секреты живут в Vault/Ansible Vault (урок 75) или в полях cloud-init при клонировании.
2. **Не сбрасывают machine-id и SSH host keys.** → Два хоста с одинаковым ID ломают DHCP, мониторинг, systemd-journald-персистентность; SSH ругается на подмену ключа. → `cloud-init clean --logs --seed`, `truncate -s 0 /etc/machine-id`, удалить `/etc/ssh/ssh_host_*` до `qm template`.
3. **Клонируют Windows-шаблон без sysprep.** → Одинаковый SID, конфликты имён в AD, проблемы с WSUS и антивирусными консолями. → `sysprep /generalize /oobe /shutdown` перед превращением в шаблон.
4. **Собирают образ «один раз и навсегда».** → Через полгода шаблон содержит уязвимости с известными CVE. → Пересборка по расписанию (ежемесячно) + `apt-get upgrade` в provisioner, версия образа в имени файла.
5. **Правят шаблон вместо клона.** → Все будущие клоны рождаются с чужими настройками, воспроизводимость потеряна. → Шаблон read-only, изменения только через Packer и Git.
6. **Хранят образы только на гипервизоре.** → Потеря ноды = потеря эталона. → Артефакт в Git LFS/Harbor/бэкап, плюс контрольная сумма в репозитории.
7. **Делают linked clone в продакшене.** → Удаление или поломка базы убивает все клоны, дисковая цепочка растёт и деградирует. → В продакшене `--full`, linked — только для быстрых тестовых сред.
8. **Ставят в образ лицензируемое ПО «на всякий случай».** → 1С-клиент, антивирус, КриптоПро требуют лицензий/привязок; в образе они размножаются бесконтрольно. → Клиент 1С и СКЗИ ставим после развёртывания, привязку ключа делаем на конкретной машине.

**Безопасность.** В боевой среде нельзя: передавать образы по открытому HTTP, хранить образ в публичном реестре, вшивать в него учётные записи с широкими правами, отключать SELinux/AppArmor «чтобы собралось». Образ — это код в продакшене: он должен подписываться и проверяться по контрольной сумме, а доступ к артефактам — по RBAC. И да, в этой лабе мы использовали пароль `Lab#2026-vektor` в открытом виде в HCL и в полях гипервизора — так делать только на стенде, в реальности — SSH-ключи и токены.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем golden image отличается от шаблона и от клона?**
Ответ: golden image — это подготовленный образ ОС с общим набором ПО и настроек, без уникальных идентификаторов. Шаблон — это тот же образ, зарегистрированный в гипервизоре как read-only ВМ, из которой разрешено клонировать. Клон — рабочая копия, получающая уникальность при первом запуске через cloud-init или sysprep. Образ — артефакт в хранилище, шаблон — объект гипервизора, клон — единица инвентаря.
*Что хочет услышать:* понимание границы «общее vs уникальное» и того, что шаблон нельзя запускать.
*Красный флаг:* «это одно и то же, просто слова разные».

**Вопрос 2. Как cloud-init узнаёт, каким должен стать сервер?**
Ответ: через datasource. В Proxmox и в QEMU-сборках чаще всего NoCloud: ISO с меткой тома `CIDATA`, внутри `user-data`, `meta-data`, `network-config`. Агент при первом старте находит источник, проходит стадии local/network/config/final, применяет пользователей, ключи, пакеты, сеть, и создаёт `/var/lib/cloud/instances/<id>`, после чего при повторных стартах ничего не делает. Есть и другие datasources — ConfigDrive, EC2-метаданные, SMBIOS-серийник.
*Что хочет услышать:* «datasource», «NoCloud», «один раз при первом запуске», «маркер в /var/lib/cloud».
*Красный флаг:* «это как ansible, только при установке».

**Вопрос 3. Как сделать сборку образов воспроизводимой и поддерживаемой?**
Ответ: Packer-шаблоны в Git рядом с cloud-init/ansible-провижинерами, версия образа в имени артефакта, `packer fmt -check` и `packer validate` в CI, автоматическая сборка по расписанию (ежемесячно + по выходу критичных патчей), публикация артефакта в хранилище с контрольной суммой, CHANGELOG и тег. Руками в образ ничего не докручивается.
*Что хочет услышать:* CI, версионирование, регламент пересборки, отсутствие ручных правок.
*Красный флаг:* «у меня есть скрипт, я его запускаю, когда надо».

**Вопрос 4. Что категорически нельзя запекать в образ?**
Ответ: пароли, приватные ключи, токены, лицензионные ключи с привязкой к железу, machine-id и SSH host keys, записи в AD, привязку к конкретному IP/hostname, временные файлы и логи, историю bash с секретами. Всё перечисленное либо выдаётся при первом запуске, либо вообще не должно попадать в образ.
*Что хочет услышать:* «образ — публичный артефакт, всё, что попало в образ, считается скомпрометированным».
*Красный флаг:* «пароль админа вшил, чтобы не настраивать каждый раз».

**Вопрос 5. Full clone или linked clone — когда что?**
Ответ: linked clone использует базу как read-only и хранит только дельту — создаётся секунды, экономит место, но зависит от базы, цепочка дельт растёт и деградирует по производительности. Full — независимая полная копия: дороже по диску, но безопасна для продакшена. Linked — для временных тестовых окружений и быстрых проверок, full — для всего, что живёт дольше дня.
*Что хочет услышать:* понимание зависимости от базы и цены диска.
*Красный флаг:* «linked всегда, он же быстрее».

## 8. Источники (официальная документация)

- **developer.hashicorp.com** — раздел Packer Docs: «Builders → QEMU», «Builders → Proxmox», «Provisioners», «HCL templates». Поиск: `packer qemu builder disk_image cloud image`.
- **cloud-init.readthedocs.io** — раздел «Datasources → NoCloud», «User-data formats / cloud-config», «Modules». Поиск: `cloud-init NoCloud CIDATA user-data meta-data`.
- **cloud-images.ubuntu.com** — официальные cloud-образы Ubuntu и файлы `SHA256SUMS`. Поиск: `ubuntu noble server cloudimg amd64`.
- **pve.proxmox.com** (Proxmox VE Administration Guide) — разделы «Cloud-Init Support», «VM Templates and Clones», «qm command-line». Поиск: `proxmox cloud-init template clone qm importdisk`.
- **learn.microsoft.com** — разделы «Sysprep», «Unattended Windows Setup Answer Files». Поиск: `sysprep generalize oobe unattend.xml`.
- **libguestfs.org** — `virt-customize`, `virt-sysprep`. Поиск: `virt-sysprep machine-id ssh host keys`.
- **gitlab.com / github.com** — официальные примеры шаблонов Packer от HashiCorp (`hashicorp/packer` в GitHub, каталог `examples`). Поиск: `packer examples proxmox ubuntu cloud-init` `[ПРОВЕРИТЬ актуальный путь к примерам]`.
