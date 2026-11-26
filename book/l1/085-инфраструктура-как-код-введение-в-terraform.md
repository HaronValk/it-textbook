# Инфраструктура как код: введение в Terraform

## 1. Введение (зачем это нужно в реальной работе)

Заявка в учебной системе «Вектора»:

> **Пользователь:** Иван Иванов, бухгалтерия
> **ВМ:** WIN11-CL01
> **Что написал:** «Мне для тестовой задачи нужна ещё одна папка на сетевом диске и отдельный тестовый пользователь. И коллеге Петровой тоже. Можно к обеду?»
> **Срочность:** средняя

В реальной работе такие просьбы — «сделай мне ещё одну виртуалку», «подними тестовый сервер», «добавь ещё одну папку» — приходят постоянно. И если каждый раз делать это руками через графический интерфейс, то через полгода никто не вспомнит, что именно было создано, с какими правами и почему. А когда что-то сломается, восстановить «как было» будет невозможно.

**Инфраструктура как код (Infrastructure as Code, IaC)** — это подход, при котором серверы, сети, папки, пользователи и права описываются в текстовых файлах, а не создаются кликами мышки. Файлы хранятся в Git, проходят ревью, версионируются. Изменение инфраструктуры = изменение файла + запуск команды.

**Terraform** — самый распространённый в мире инструмент IaC. Он читает твоё описание (файлы `.tf`), сравнивает с тем, что реально существует, и приводит реальность к описанному состоянию. Работает с сотнями провайдеров: VMware, облака (Яндекс Облако, VK Cloud), Docker, Active Directory, DNS и так далее.

После этой главы ты будешь понимать: что такое провайдер, ресурс, состояние (state), план и применение; как описать инфраструктуру в `.tf`-файлах; как безопасно запустить `terraform plan` и `terraform apply`; как читать план и не сломать стенд. На стенде мы потренируемся на самом безопасном провайдере — `local` (создаёт файлы на диске), потому что он не требует ни облачных аккаунтов, ни API-ключей, ни интернета. А в разделе 4 покажу, как это же самое выглядит для реальных провайдеров (VMware, Yandex Cloud), чтобы ты понимал, куда расти.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Terraform — это один бинарник (написан на Go), который запускается на твоей машине. Никакого сервера Terraform нет. Он общается с «мирами» через **провайдеры** — плагины, которые скачиваются при первом запуске.

Схема работы:

```text
   ┌──────────────────────┐
   │  Твои .tf-файлы      │  ← описание «как должно быть»
   │  (main.tf и др.)     │
   └──────────┬───────────┘
              │ terraform plan / apply
              ▼
   ┌──────────────────────┐        ┌──────────────────┐
   │  Terraform (бинарник)│◄──────►│  terraform.tfstate│
   │  + провайдер         │        │  «что есть сейчас»│
   └──────────┬───────────┘        └──────────────────┘
              │ API-вызовы (HTTPS, локально — файлы)
              ▼
   ┌──────────────────────┐
   │  Целевая система     │
   │  VMware / Yandex /   │
   │  Docker / AD / ...   │
   └──────────────────────┘
```

Ключевые понятия:

- **Провайдер (provider)** — плагин, который умеет говорить с конкретной системой. Например, `hashicorp/local` (файлы на диске), `hashicorp/vsphere` (VMware vSphere), `yandex-cloud/yandex`, `kreuzwerker/docker`. Провайдеры скачиваются из реестра `registry.terraform.io` по HTTPS.
- **Ресурс (resource)** — единица инфраструктуры: файл, виртуалка, папка, пользователь. Описывается блоком `resource "тип" "имя" { ... }`.
- **Провайдер + ресурс** — например, `local_file` (файл), `vsphere_virtual_machine` (ВМ в vSphere), `yandex_compute_instance` (ВМ в Yandex Cloud).
- **State (состояние)** — файл `terraform.tfstate`, где Terraform хранит, что он уже создал и с какими ID. Это его «память». Если state потерять — Terraform не узнает свои ресурсы и попытается создать их заново.
- **План (plan)** — Terraform сравнивает `.tf`-файлы со state и с реальностью, показывает, что собирается сделать: `+ create`, `~ update`, `- destroy`.
- **Apply** — выполняет план.
- **Destroy** — удаляет всё, что описано в state.
- **Идемпотентность** — повторный `apply` без изменений в `.tf` ничего не делает. Это главное свойство IaC.

Порты и протоколы: для локального провайдера — ничего (просто файлы). Для облачных — HTTPS (443) к API провайдера. Для vSphere — HTTPS к vCenter (443). Terraform сам ничего не слушает.

**Где это в работе L1.** L1 сам Terraform обычно не пишет — этим занимаются DevOps и сисадмины. Но L1:
- читает `plan` и объясняет, что изменится;
- запускает `terraform apply` по инструкции;
- понимает, почему «сервер пропал из Terraform» — потому что кто-то удалил его руками, и теперь state рассинхронизирован;
- не редактирует `.tf`-файлы в одиночку, а идёт через ревью.

## 3. Аналогия из жизни

Представь, что ты собираешь шкаф из IKEA. В коробке лежит **инструкция** — это твои `.tf`-файлы. В инструкции написано: «полка A, полка B, 4 ножки, 8 винтов». Ты собираешь — это `apply`. Готовый шкаф — это твоя инфраструктура. А **чек-лист**, куда ты галочками отмечаешь, что уже прикрутил, — это `terraform.tfstate`.

Если через месяц ты решишь добавить ещё одну полку, ты не разбираешь шкаф целиком — ты открываешь инструкцию, дописываешь «полка C», смотришь, куда её вставить (это `plan`), и прикручиваешь (это `apply`). Terraform точно так же не пересоздаёт всё, а меняет только то, что изменилось.

Аналогия ломается в одном: в IKEA инструкция одна и та же для всех, а в Terraform файлы у каждой компании свои, и их пишут люди — с ошибками, ревью и обсуждениями в Git.

## 4. Практика и команды

Все команды — на хосте в PowerShell (Terraform под Windows ставится как обычный `.exe`). На Linux-хосте — те же команды в bash.

### 4.1. Установка Terraform

Скачай официальный архив с `developer.hashicorp.com/terraform/downloads` (Windows amd64), распакуй `terraform.exe` в `C:\Lab\vektor\tools\terraform\` и добавь эту папку в `PATH` (или запускай по полному пути). Проверка:

```powershell
terraform version
```

Пример вывода (версия у тебя может быть новее):

```text
Terraform v1.9.x
on windows_amd64
```

Если команда не находится — проблема в `PATH`. Проверь `$env:Path -split ';'`.

### 4.2. `terraform init` — инициализация рабочей папки

```powershell
cd C:\Lab\vektor\terraform\demo
terraform init
```

`init` читает `.tf`-файлы, находит провайдеры, скачивает их в скрытую папку `.terraform\` и создаёт `.terraform.lock.hcl` (фиксация версий). Запускается один раз на папку и после смены провайдеров.

Пример вывода (примерно):

```text
Initializing the backend...
Initializing provider plugins...
- Installing hashicorp/local v2.5.x...
Terraform has been successfully initialized!
```

### 4.3. `terraform fmt` — форматирование

```powershell
terraform fmt -recursive
```

Приводит `.tf`-файлы к каноническому стилю (отступы, выравнивание). В Git-ревью это обязательный шаг — иначе коллеги замучают.

### 4.4. `terraform validate` — проверка синтаксиса

```powershell
terraform validate
```

Проверяет, что файлы синтаксически корректны и ссылки между ресурсами валидны. Не ходит в целевую систему. Пример:

```text
Success! The configuration is valid.
```

### 4.5. `terraform plan` — что изменится

```powershell
terraform plan
```

Terraform читает `.tf`, читает `terraform.tfstate` (если есть), опрашивает провайдер и показывает diff. Пример для одного нового файла:

```text
Terraform will perform the following actions:

  # local_file.hello will be created
  + resource "local_file" "hello" {
      + content  = "Privet, Vektor!"
      + filename = "C:/Lab/vektor/terraform/demo/hello.txt"
      + id       = (known after apply)
    }

Plan: 1 to add, 0 to change, 0 to destroy.
```

Символы: `+` создать, `~` изменить на месте, `-/+` пересоздать (удалить и создать заново), `-` удалить. **Всегда читай план перед apply.**

### 4.6. `terraform apply` — применить

```powershell
terraform apply
```

Показывает тот же план и спрашивает `Do you want to perform these actions?` — надо ввести `yes` (именно слово, не `y`). Для автоматизации — `terraform apply -auto-approve` (в боевой среде так делать нельзя, только в CI с ревью).

### 4.7. `terraform show` и `terraform state list` — что создано

```powershell
terraform show
terraform state list
```

`show` показывает текущее состояние в человекочитаемом виде. `state list` — просто список адресов ресурсов:

```text
local_file.hello
```

### 4.8. `terraform destroy` — удалить всё

```powershell
terraform destroy
```

Удаляет всё, что описано в state. Тоже спрашивает `yes`. **Опасно**: если в state лежит боевая ВМ — она будет удалена.

### 4.9. `terraform output` — вытащить значения

Если в `.tf` объявлены `output`-блоки, их значения можно получить:

```powershell
terraform output
terraform output -raw file_path
```

### 4.10. Как это выглядит для реальных провайдеров (теория)

Для VMware vSphere (не путать с VMware Workstation — Terraform-провайдер `hashicorp/vsphere` работает с vCenter, а не с Workstation):

```hcl
provider "vsphere" {
  user                 = "adm.student@corp.vektor.lab"
  password             = var.vsphere_password
  vsphere_server       = "vcenter.corp.vektor.lab"
  allow_unverified_ssl = false
}

resource "vsphere_virtual_machine" "test" {
  name             = "test-vm"
  resource_pool_id = data.vsphere_resource_pool.pool.id
  datastore_id     = data.vsphere_datastore.ds.id
  num_cpus         = 2
  memory           = 4096
  guest_id         = "ubuntu64Guest"
}
```

Для Yandex Cloud:

```hcl
provider "yandex" {
  cloud_id  = var.yc_cloud_id
  folder_id = var.yc_folder_id
  zone      = "ru-central1-a"
}

resource "yandex_compute_instance" "vm" {
  name        = "test-vm"
  platform_id = "standard-v3"
  resources {
    cores  = 2
    memory = 2
  }
  boot_disk {
    initialize_params { image_id = var.image_id }
  }
  network_interface {
    subnet_id = var.subnet_id
  }
}
```

Синтаксис тот же самый, что и для `local_file`. Меняется только провайдер и набор полей. Именно поэтому Terraform универсален.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд в состоянии после L1 №84: есть WIN11-CL01, WIN10-CL02, UBU01, DC01, ASTRA01, FS01, SRV1C01, DC02, GW01, MON01, ESXI01, PVE01, WEB01, PVE02, PBS01, GIT01, HARBOR01, k8s-узлы. Домен `corp.vektor.lab` работает, DC01 — контроллер домена.
- На хосте есть папки `C:\Lab\vektor` и `C:\Support`.
- На хосте установлен Terraform (см. 4.1). Если нет — установи сейчас, это часть лабораторной.
- На хосте есть Git (ставили в L1 №77) и настроен `user.name`/`user.email`.
- Ты умеешь открывать PowerShell от имени администратора.

**Почему не VMware Workstation.** Провайдер `hashicorp/vsphere` работает с vCenter, а не с Workstation. Провайдера для Workstation в официальном реестре нет. Поэтому лабораторная — на провайдере `local`: он безопасен, работает без интернета (после `init`), и на нём видно все ключевые механики Terraform (ресурс, state, plan, apply, destroy). Работа с реальными провайдерами — это та же самая последовательность команд, только с другим `.tf`-файлом.

### Снапшот

Снапшот `before-l1-085` на затрагиваемых ВМ делать не нужно: лабораторная не меняет ни одну ВМ. Она работает только на хосте в папке `C:\Lab\vektor\terraform\`. Но по правилам трека — сделай снапшот `before-l1-085` на **WIN11-CL01** и **UBU01** (на случай, если ты захочешь потом экспериментировать с провайдерами на этих машинах). Команда на хосте:

```powershell
vmrun -T ws -vp "Lab#2026-vektor" snapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-085
vmrun -T ws snapshot "C:\Lab\vektor\VMs\UBU01\UBU01.vmx" before-l1-085
```

[ПРОВЕРИТЬ] синтаксис `vmrun snapshot` в твоей версии VMware Workstation — команда может отличаться (`snapshot` vs `snapshot create`).

### Шаги

1. **Создай рабочую папку и репозиторий.** На хосте в PowerShell:

    ```powershell
    New-Item -ItemType Directory -Force C:\Lab\vektor\terraform\demo | Out-Null
    cd C:\Lab\vektor\terraform\demo
    git init
    ```

    Ожидаемо: папка создана, `git init` пишет `Initialized empty Git repository in C:/Lab/vektor/terraform/demo/.git/`.

2. **Создай `.gitignore`.** В этой же папке создай файл `.gitignore`:

    ```text
    .terraform/
    *.tfstate
    *.tfstate.*
    *.tfstate.backup
    .terraform.lock.hcl
    ```

    Ожидаемо: файл появился. **Важно:** state и `.terraform/` в Git не кладём — там могут быть секреты и большие бинарники провайдеров. `.terraform.lock.hcl` в реальной практике обычно **кладут** в Git (фиксирует версии провайдеров), но для учебного демо можно игнорировать — [ПРОВЕРИТЬ] в вашей команде принято ли коммитить lock-файл.

3. **Создай `main.tf`.** В папке `C:\Lab\vektor\terraform\demo` создай файл `main.tf`:

    ```hcl
    terraform {
      required_version = ">= 1.5.0"
      required_providers {
        local = {
          source  = "hashicorp/local"
          version = "~> 2.5"
        }
      }
    }

    resource "local_file" "hello" {
      filename = "${path.module}/hello.txt"
      content  = "Privet, Vektor! Eto fajl, sozdannyj Terraform.\n"
    }

    resource "local_file" "ticket_note" {
      filename = "${path.module}/ticket-ivanov.txt"
      content  = "Zayavka ot Ivanova: nuzhna papka na setevom diske.\n"
    }

    output "hello_path" {
      value = local_file.hello.filename
    }
    ```

    Ожидаемо: файл сохранён. Обрати внимание: `path.module` — встроенная переменная, путь к папке с модулем; `${...}` — интерполяция.

4. **Инициализируй.** В PowerShell:

    ```powershell
    terraform init
    ```

    Ожидаемо (примерно): скачивание провайдера `hashicorp/local`, сообщение `Terraform has been successfully initialized!`. В папке появились `.terraform\` и `.terraform.lock.hcl`. Если интернета нет — `init` упадёт с ошибкой скачивания; тогда подключи хост к сети и повтори.

5. **Проверь синтаксис и отформатируй.**

    ```powershell
    terraform fmt
    terraform validate
    ```

    Ожидаемо: `fmt` может ничего не вывести (файл уже отформатирован) или вывести имя изменённого файла; `validate` — `Success! The configuration is valid.`

6. **Посмотри план.**

    ```powershell
    terraform plan
    ```

    Ожидаемо (примерно): `Plan: 2 to add, 0 to change, 0 to destroy.` Два ресурса `local_file.hello` и `local_file.ticket_note` помечены `+`. Файлов `hello.txt` и `ticket-ivanov.txt` в папке пока **нет** — план ничего не создаёт.

7. **Примени.**

    ```powershell
    terraform apply
    ```

    Terraform покажет тот же план и спросит подтверждение. Введи `yes`. Ожидаемо: `Apply complete! Resources: 2 added, 0 changed, 0 destroyed.` и вывод `hello_path = "C:/Lab/vektor/terraform/demo/hello.txt"`. Проверь:

    ```powershell
    Get-ChildItem C:\Lab\vektor\terraform\demo
    Get-Content C:\Lab\vektor\terraform\demo\hello.txt
    ```

    Ожидаемо: оба файла на месте, содержимое совпадает с `content` в `main.tf`.

8. **Проверь идемпотентность.** Снова:

    ```powershell
    terraform plan
    ```

    Ожидаемо: `No changes. Your infrastructure matches the configuration.` Это ключевое свойство IaC — повторный запуск ничего не меняет.

9. **Измени описание и посмотри diff.** Открой `main.tf`, поменяй `content` у `local_file.hello` на `"Privet, Vektor! Versiya 2.\n"`. Сохрани. Затем:

    ```powershell
    terraform plan
    ```

    Ожидаемо: `~ update in-place` для `local_file.hello` (Terraform покажет `-` старую строку и `+` новую), `Plan: 0 to add, 1 to change, 0 to destroy.` Применяй:

    ```powershell
    terraform apply -auto-approve
    Get-Content C:\Lab\vektor\terraform\demo\hello.txt
    ```

    Ожидаемо: содержимое файла обновилось.

10. **Посмотри state.**

    ```powershell
    terraform state list
    terraform show
    ```

    Ожидаемо: два адреса `local_file.hello` и `local_file.ticket_note`, а `show` показывает их атрибуты (включая `id`).

11. **Закоммить в Git.**

    ```powershell
    git add .gitignore main.tf
    git commit -m "Terraform demo: two local_file resources"
    ```

    Ожидаемо: коммит создан, `git status` чистый (state и `.terraform/` не попали — их отсекает `.gitignore`).

12. **Убери всё.**

    ```powershell
    terraform destroy -auto-approve
    Get-ChildItem C:\Lab\vektor\terraform\demo
    ```

    Ожидаемо: `Destroy complete! Resources: 2 destroyed.` Файлы `hello.txt` и `ticket-ivanov.txt` удалены, `.tf`-файлы и state остались (state теперь пустой).

13. **Запиши в журнал.** Открой `C:\Lab\vektor\stand-log.md` и добавь запись по разделу «Что изменилось на стенде» ниже.

### Проверка

- [ ] `terraform version` показывает версию 1.5 или новее.
- [ ] В `C:\Lab\vektor\terraform\demo` есть `main.tf`, `.gitignore`, `.terraform.lock.hcl`, `.terraform\`, `terraform.tfstate`.
- [ ] `terraform validate` возвращает `Success!`.
- [ ] Первый `terraform plan` показывает `2 to add`.
- [ ] После `apply` в папке есть `hello.txt` и `ticket-ivanov.txt` с ожидаемым содержимым.
- [ ] Повторный `plan` — `No changes`.
- [ ] После правки `content` — `plan` показывает `1 to change`.
- [ ] `git log --oneline` содержит коммит с `main.tf`.
- [ ] `terraform destroy` удалил оба файла, `.tf`-файлы остались.
- [ ] В `stand-log.md` есть запись о лабораторной.

### Сломай и почини

**Заявка:**

> **Пользователь:** Мария Петрова, HR
> **ВМ:** WIN10-CL02 (но проблема на хосте в папке Terraform)
> **Что написал:** «Коллега из IT сказал, что мой тестовый файл должен лежать в папке `C:\Lab\vektor\terraform\demo`, но его там нет. И ещё он говорит, что `terraform plan` показывает какие-то изменения, хотя ничего не трогали. Помогите разобраться.»
> **Срочность:** низкая

**Как воспроизвести поломку.** На хосте в папке `C:\Lab\vektor\terraform\demo` после успешного `apply` (шаг 7) удали файл `hello.txt` вручную:

```powershell
Remove-Item C:\Lab\vektor\terraform\demo\hello.txt
```

Теперь `terraform plan` покажет `1 to add` — Terraform видит, что в state ресурс есть, а в реальности файла нет, и хочет его пересоздать. Это классический **дрейф (drift)** — расхождение между state и реальностью.

**Диагностика по шагам.**

1. Проверь, что файла действительно нет:

    ```powershell
    Test-Path C:\Lab\vektor\terraform\demo\hello.txt
    ```

    Ожидаемо: `False`.

2. Посмотри, что Terraform думает:

    ```powershell
    cd C:\Lab\vektor\terraform\demo
    terraform plan
    ```

    Ожидаемо: `Plan: 1 to add, 0 to change, 0 to destroy.` — Terraform хочет создать `local_file.hello` заново.

3. Проверь, что state не пустой:

    ```powershell
    terraform state list
    ```

    Ожидаемо: `local_file.hello` и `local_file.ticket_note` в списке. Значит, state в порядке, проблема в реальности.

4. Проверь, что в `main.tf` нет случайных правок (например, кто-то поменял `filename`):

    ```powershell
    git diff main.tf
    ```

    Ожидаемо: пусто (если ты закоммитил на шаге 11). Если есть diff — сначала разберись с ним.

**Решение.** Привести реальность к описанному состоянию:

```powershell
terraform apply -auto-approve
Test-Path C:\Lab\vektor\terraform\demo\hello.txt
```

Ожидаемо: `Apply complete! Resources: 1 added, 0 changed, 0 destroyed.` и `True`. Файл восстановлен.

**Ответ пользователю.** «Мария, разобрались. Файл был удалён вручную, а Terraform об этом не знал — он хранит своё состояние отдельно. Мы запустили `terraform apply`, он увидел расхождение и восстановил файл. Впредь файлы, созданные Terraform, руками не удаляем — если нужно убрать, правим `.tf`-файл и запускаем `apply`.»

**Закрытие заявки.** Категория: «Инфраструктура / IaC». Решение: «Дрейф state — файл удалён вручную, восстановлен через `terraform apply`». Время: 10 минут. Профилактика: в инструкции для команды добавить пункт «не редактировать ресурсы, созданные Terraform, вручную».

### Что изменилось на стенде

- На хосте создана папка `C:\Lab\vektor\terraform\demo` с Git-репозиторием.
- В ней: `main.tf`, `.gitignore`, `.terraform.lock.hcl`, `.terraform\`, `terraform.tfstate` (после `destroy` — пустой state).
- Установлен Terraform на хост (папка `C:\Lab\vektor\tools\terraform\`, добавлена в `PATH`).
- В Git-репозитории `C:\Lab\vektor\terraform\demo` — один коммит с `main.tf` и `.gitignore`.
- ВМ, роли, домен, пользователи, адреса — **не менялись**.
- В `stand-log.md` добавлена запись о лабораторной.

### Откат

1. Удалить папку `C:\Lab\vektor\terraform\demo` целиком (в ней только учебные файлы):

    ```powershell
    Remove-Item -Recurse -Force C:\Lab\vektor\terraform\demo
    ```

2. Удалить папку `C:\Lab\vektor\tools\terraform\` и убрать её из `PATH` (если не планируешь пользоваться Terraform дальше).
3. Если делал снапшоты `before-l1-085` на WIN11-CL01 и UBU01 — откатиться к ним:

    ```powershell
    vmrun -T ws -vp "Lab#2026-vektor" revertToSnapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-085
    vmrun -T ws revertToSnapshot "C:\Lab\vektor\VMs\UBU01\UBU01.vmx" before-l1-085
    ```

    [ПРОВЕРИТЬ] синтаксис `revertToSnapshot` в твоей версии VMware Workstation.

## 6. Ошибки новичков и безопасность

1. **Коммитить `terraform.tfstate` в Git.** → В state могут быть пароли, ключи, IP-адреса; любой, у кого доступ к репозиторию, получает доступ к инфраструктуре. → Всегда `.gitignore` для `*.tfstate*`. Для командной работы — удалённый backend (S3, Yandex Object Storage, Terraform Cloud), но это уже за пределами L1.

2. **Запускать `terraform apply` без чтения плана.** → Можно случайно удалить боевую ВМ (`-/+` в плане означает пересоздание). → Всегда читай `plan` целиком, особенно строки с `-` и `-/+`.

3. **`terraform apply -auto-approve` в боевой среде.** → Нет паузы на подтверждение, ошибка в `.tf` применяется мгновенно. → В боевой среде `apply` только через CI с ревью и ручным подтверждением.

4. **Ручное изменение ресурсов, созданных Terraform.** → Дрейф state, `plan` показывает неожиданные изменения, `apply` может «откатить» ручную правку. → Всё, что описано в `.tf`, меняется только через `.tf`.

5. **Удаление `.terraform\` или `terraform.tfstate` «чтобы почистить».** → Terraform теряет связь с ресурсами, при следующем `apply` попытается создать их заново (конфликт имён, дубли). → State — это память Terraform, обращаться с ним аккуратно; для восстановления есть `terraform state` подкоманды.

6. **Использование `latest` версии провайдера без фиксации.** → Через месяц `init` скачает другую версию, поведение изменится. → Фиксируй версию в `required_providers` (`version = "~> 2.5"`) и коммить `.terraform.lock.hcl`.

7. **Хранение паролей прямо в `.tf`.** → Пароль в Git, в истории, в логах. → Переменные (`variable`), `terraform.tfvars` в `.gitignore`, секреты — из vault/CI-переменных.

8. **Работа без снапшота на учебном стенде.** → `terraform destroy` снёс то, что не должен был. → Снапшот `before-<уровень>-<номер>` перед любой лабораторной, где есть `apply`/`destroy`.

### Безопасность

В боевой среде **нельзя**:
- запускать `terraform apply`/`destroy` от своего личного аккаунта без ревью — только через пайплайн с подтверждением;
- хранить state в открытом виде в общем репозитории;
- давать L1 права на `apply` в прод — L1 обычно имеет read-only доступ к планам и право запускать пайплайн по инструкции;
- использовать `-auto-approve` в проде;
- коммитить `.tfvars` с реальными паролями.

Учебный стенд «Вектора» — исключение: пароль `Lab#2026-vektor` учебный, и в реальной работе так делать нельзя.

## 7. Вопросы на собеседовании

**Вопрос 1. «Что такое Terraform и зачем он нужен?»**

Ответ: «Terraform — это инструмент Infrastructure as Code. Инфраструктура описывается в текстовых `.tf`-файлах, которые хранятся в Git. Terraform читает описание, сравнивает с тем, что реально существует (через state и API провайдера), и приводит реальность к описанному состоянию. Главные плюсы: версионирование, ревью, воспроизводимость, идемпотентность. На стенде я делал это на провайдере `local` — создавал файлы через `resource "local_file"`, смотрел `plan`, применял `apply`, проверял, что повторный `plan` даёт `No changes`.»

**Что хочет услышать интервьюер:** понимание, что Terraform — декларативный (описываешь «что», а не «как»), и что state — ключевая часть.

**Красный флаг:** «Terraform — это язык программирования» или «Terraform создаёт виртуалки в VMware Workstation» (нет, для Workstation провайдера нет).

**Вопрос 2. «Что такое terraform.tfstate и почему его нельзя коммитить в Git?»**

Ответ: «State — это файл, где Terraform хранит, какие ресурсы он создал и с какими ID. Без него Terraform не узнает свои ресурсы и попытается создать их заново. В state могут быть чувствительные данные: пароли, ключи, IP. Поэтому его не коммитят, а хранят в удалённом backend с ограниченным доступом. В `.gitignore` у меня `*.tfstate*`.»

**Что хочет услышать интервьюер:** понимание, что state — это память Terraform, и что там могут быть секреты.

**Красный флаг:** «State можно удалить, Terraform сам восстановит» — нет, не восстановит.

**Вопрос 3. «Чем plan отличается от apply?»**

Ответ: «`plan` — это предпросмотр: Terraform показывает, что собирается сделать (`+` создать, `~` изменить, `-` удалить, `-/+` пересоздать), но ничего не меняет. `apply` выполняет план. В боевой среде `plan` смотрят на ревью, а `apply` запускают через пайплайн с подтверждением. Я всегда читаю план перед apply — особенно строки с `-` и `-/+`.»

**Что хочет услышать интервьюер:** что кандидат не запускает `apply` вслепую.

**Красный флаг:** «Plan и apply — одно и то же, просто apply с подтверждением».

**Вопрос 4. «Что такое дрейф (drift) и как его лечить?»**

Ответ: «Дрейф — расхождение между state и реальностью. Например, кто-то удалил файл или ВМ вручную, а Terraform об этом не знает. `plan` покажет, что ресурс нужно создать заново. Лечится `terraform apply` — Terraform приведёт реальность к описанному состоянию. Если наоборот — ресурс создан вручную и не описан в `.tf`, — его нужно либо импортировать (`terraform import`), либо удалить. На стенде я воспроизводил дрейф: удалял `hello.txt` руками, `plan` показывал `1 to add`, `apply` восстанавливал файл.»

**Что хочет услышать интервьюер:** понимание, что IaC требует дисциплины: всё через код.

**Красный флаг:** «Дрейф — это когда Terraform сломался».

**Вопрос 5. «Какие провайдеры Terraform ты знаешь?»**

Ответ: «Из тех, что видел: `hashicorp/local` (файлы), `hashicorp/vsphere` (VMware vSphere через vCenter), `yandex-cloud/yandex` (Yandex Cloud), `kreuzwerker/docker` (Docker). Синтаксис одинаковый, меняется только провайдер и набор полей ресурса. Для VMware Workstation провайдера нет — Workstation не имеет API, с которым можно работать так же, как с vCenter.»

**Что хочет услышать интервьюер:** что кандидат понимает разницу между Workstation и vSphere и не выдумывает несуществующие провайдеры.

**Красный флаг:** «Terraform работает с VMware Workstation» — нет.

## 8. Источники (официальная документация)

- **HashiCorp Developer** (`developer.hashicorp.com`), раздел **Terraform → Documentation → Configuration Language**. Поиск: `terraform configuration language`.
- **HashiCorp Developer**, раздел **Terraform → Registry → Providers → hashicorp/local**. Поиск: `terraform local provider`.
- **HashiCorp Developer**, раздел **Terraform → Documentation → State**. Поиск: `terraform state explained`.
- **HashiCorp Developer**, раздел **Terraform → Documentation → Commands → plan / apply / destroy / state**. Поиск: `terraform plan command`.
- **HashiCorp Developer**, раздел **Terraform → Registry → Providers → hashicorp/vsphere**. Поиск: `terraform vsphere provider`.
- **Yandex Cloud** (`yandex.cloud`), раздел **Документация → Terraform**. Поиск: `yandex cloud terraform provider`.
- **VMware** (`developer.vmware.com` или `techdocs.broadcom.com`), раздел про vSphere Automation SDK — для понимания, что провайдер `vsphere` работает с vCenter, а не с Workstation. Поиск: `vsphere automation sdk rest api`. [ПРОВЕРИТЬ] актуальный домен документации VMware после смены владельца.
