# Ansible: первый плейбук, управление Windows и Linux

## 1. Введение (зачем это нужно в реальной работе)

**Заявка № 4127.** Пользователь: `ivanov` (бухгалтерия). ВМ: WIN11-CL01. Что написал: «Иван, привет. Нам в отдел пришёл новый сотрудник, нужно поставить ему на компьютер 7-Zip, Adobe Reader и прогу для сканера, и на FS01 создать папку. И ещё у Петровой обновить драйвер принтера. Сколько это займёт?» Срочность: Средняя.

Вы уже умеете ползать по каждой машине руками и писать скрипты на PowerShell (урок 78) и Bash (урок 79). Но когда «поставить 7-Zip» надо на пяти машинах, а «создать папку» — на файловом сервере, вы каждый раз идёте в RDP и делаете одно и то же. Ansible решает ровно это: вы описываете **желаемое состояние** один раз в текстовом файле, а инструмент сам обходит машины и приводит их к этому состоянию.

В этой главе вы поставите Ansible на UBU01, научите его ходить на Linux-машины по SSH и на Windows-серверы по WinRM, соберёте первый плейбук и поймёте, что такое идемпотентность. Для L1 это не «обязанность», а сильный плюс на собеседовании и прямой мост к DevOps. После главы вы должны уметь: поднять control node, написать inventory, прогнать ad-hoc команду, запустить плейбук в режиме проверки и объяснить, почему он упал.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ansible — **агентless**-система управления конфигурациями. На управляемых машинах ничего не устанавливается: Ansible подключается по уже существующему каналу, кладёт туда маленькую программу-модуль (временный файл), выполняет её, забирает JSON-результат и удаляет файл.

Ключевые понятия:

- **Control node** — машина, где установлен Ansible. У нас это `UBU01` (10.10.10.30). На Windows control node не поддерживается — только Linux/macOS/WSL.
- **Managed node (хост)** — машина, которой управляют: `ASTRA01` (10.10.10.31), `DC01` (10.10.10.10), `FS01` (10.10.10.20).
- **Inventory** — список хостов и их параметров подключения (файл `inventory.ini`).
- **Модуль** — «типовая работа»: `copy`, `file`, `apt`, `win_service`, `win_copy`. Модули идемпотентны: они сначала смотрят текущее состояние и меняют что-то только если нужно.
- **Задача (task)** — вызов модуля с параметрами.
- **Плейбук (playbook)** — YAML-файл со списком «пьес» (play), каждая пьеса — набор задач для группы хостов.
- **Идемпотентность** — повторный запуск ничего не меняет. Первый прогон: `changed=3`. Второй: `changed=0`. Это главный признак, что плейбук написан правильно.

Транспорт и порты:

```text
                 UBU01  10.10.10.30   (control node, Ansible)
                  |                 |
        SSH TCP/22|                 |WinRM TCP/5985 (HTTP) или TCP/5986 (HTTPS)
                  v                 v
             ASTRA01            DC01 10.10.10.10
             10.10.10.31        FS01 10.10.10.20
             (Linux, sshd)      (Windows, WinRM + PowerShell)

  Playbook (YAML)  ->  task  ->  модуль  ->  копия на хост  ->  выполнение
                                          <- JSON-результат <-
```

По модели OSI это всё уровень приложения (L7): SSH и WinRM — прикладные протоколы поверх TCP. Ansible не изобретает свой протокол — поэтому и не нужен агент.

**Почему с Windows всё сложнее.** Windows «из коробки» не умеет SSH (точнее, OpenSSH-сервер есть как опциональный компонент, но Ansible-модули для Windows написаны под WinRM). WinRM — это реализация WS-Management от Microsoft: удалённое выполнение PowerShell по HTTP/HTTPS. Ansible ходит туда через Python-библиотеку **pywinrm**. Ей нужен один из механизмов аутентификации: `basic` (логин/пароль в открытом виде — только по HTTPS), `ntlm` (хэш пароля, работает без Kerberos — наш случай) или `kerberos` (нужен домен-joined control node, у нас его нет).

## 3. Аналогия из жизни

Ansible — это прораб с бланком наряда. У прораба есть список объектов (inventory) и типовой перечень работ: «покрасить забор», «вкрутить лампочку» (модули). Он приезжает на объект, смотрит глазами: забор уже покрашен? Тогда не красит, ставит галочку «ok». Не покрашен — красит и пишет «changed». Приехал второй раз — снова ноль изменений, потому что работа сделана.

Плохой прораб — это `shell`/`command`: он не смотрит, а просто каждый раз красит заново, и забор в итоге облупился в три слоя.

Аналогия перестаёт работать в одном месте: у настоящего прораба бригада и объекты параллельно, а Ansible по умолчанию обходит хосты по очереди (параллельность задаётся параметром `forks` в `ansible.cfg`).

## 4. Практика и команды

Все команды — на **UBU01**, из каталога проекта `~/ansible-vektor`.

**1. Установка и версия**

```bash
sudo apt update
sudo apt install -y ansible python3-pywinrm
ansible --version
```

`apt install ansible` в Ubuntu 24.04 ставит пакет `ansible` (community-сборка: ядро + популярные коллекции) из репозитория universe [ПРОВЕРИТЬ версию]. `python3-pywinrm` — библиотека для WinRM [ПРОВЕРИТЬ имя пакета]. Альтернатива из официальной документации — `pipx install --include-deps ansible`, тогда pywinrm добавляется через `pipx inject ansible pywinrm`.

Вывод (сокращённо):

```text
ansible [core 2.16.x]
  config file = None
  python version = 3.12.x
```

Смотрим: `config file = None` — значит `ansible.cfg` ещё нет и берутся умолчания.

**2. Inventory и его проверка**

```ini
# ~/ansible-vektor/inventory.ini
[linux]
ubu01    ansible_host=10.10.10.30
astra01  ansible_host=10.10.10.31

[windows]
dc01     ansible_host=10.10.10.10
fs01     ansible_host=10.10.10.20

[linux:vars]
ansible_user=<ваш пользователь на UBU01/ASTRA01>
ansible_become_password="Lab#2026-vektor"

[windows:vars]
ansible_user=adm.student
ansible_password="Lab#2026-vektor"
ansible_connection=winrm
ansible_port=5986
ansible_winrm_transport=ntlm
ansible_winrm_server_cert_validation=ignore
```

`ansible-inventory -i inventory.ini --graph` — показывает дерево групп. `--list` — то же в JSON со всеми переменными. Смотреть: все 4 хоста на месте, пароли подставились (в `--list` они видны — не запускайте это на чужом экране).

**3. Проверка Linux-связности**

```bash
ansible linux -i inventory.ini -m ansible.builtin.ping
```

`linux` — имя группы, `-m` — модуль. `ping` здесь не ICMP, а «подключился, выполнил Python, получил ответ».

```text
ubu01 | SUCCESS => { "changed": false, "ping": "pong" }
```

`SUCCESS` + `pong` — канал работает. `UNREACHABLE` — проблема с SSH/сетью, `FAILED` — подключились, но упало внутри (например, нет python3).

**4. Проверка Windows-связности**

```bash
ansible windows -i inventory.ini -m ansible.windows.win_ping
```

`ansible.windows.win_ping` — отдельный модуль для Windows, `ansible.builtin.ping` туда не пойдёт (там нет Python). Ответ такой же: `"ping": "pong"`.

**5. Ad-hoc сбор фактов**

```bash
ansible linux -i inventory.ini -m ansible.builtin.setup -a 'filter=ansible_distribution*'
ansible windows -i inventory.ini -m ansible.windows.setup -a 'filter=ansible_os_*'
```

`-a` — аргументы модуля. `setup` собирает факты о системе; `filter` оставляет только нужные. Пригодится, когда надо быстро узнать версии ОС по парку без RDP.

**6. Ad-hoc команда на Windows**

```bash
ansible windows -i inventory.ini -m ansible.windows.win_shell -a 'Get-Service Spooler | Select-Object Name,Status'
```

`win_shell` выполняет строку в PowerShell, `win_command` — запускает exe без shell. Обе **не идемпотентны** — для проверок годятся, для настройки нет.

**7. Сухой прогон плейбука**

```bash
ansible-playbook -i inventory.ini site.yml --check --diff
```

`--check` — «показать, что изменилось бы, но не менять»; `--diff` — показать разницу в файлах. Обязательная привычка перед боевым запуском. Учтите: часть модулей в `--check` работает в ограниченном режиме.

**8. Запуск с ограничением по хостам**

```bash
ansible-playbook -i inventory.ini site.yml --limit fs01 -v
```

`--limit` — применить только к одному хосту (или группе), `-v/-vv/-vvv` — подробный вывод, нужен при разборе ошибок подключения.

**9. Справка по модулю**

```bash
ansible-doc ansible.windows.win_copy
ansible-galaxy collection list | grep -i windows
```

`ansible-doc` — офлайн-документация по модулю со всеми параметрами. Если `ansible.windows` не найден: `ansible-galaxy collection install ansible.windows`.

**10. Синтаксическая проверка**

```bash
ansible-playbook -i inventory.ini site.yml --syntax-check
```

Ловит ошибки YAML и опечатки в именах модулей до подключения к хостам.

**Первый плейбук `site.yml`:**

```yaml
---
- name: Linux: базовая настройка
  hosts: linux
  become: true
  vars:
    base_dir: /opt/vektor
  tasks:
    - name: Показать версию ОС
      ansible.builtin.debug:
        msg: "{{ inventory_hostname }}: {{ ansible_distribution }} {{ ansible_distribution_version }}"

    - name: Создать каталог
      ansible.builtin.file:
        path: "{{ base_dir }}"
        state: directory
        mode: "0755"

    - name: Положить файл-визитку
      ansible.builtin.copy:
        content: "Хост {{ inventory_hostname }} управляется Ansible\n"
        dest: "{{ base_dir }}/info.txt"
        mode: "0644"

- name: Windows: базовая настройка
  hosts: windows
  gather_facts: false
  tasks:
    - name: Проверить доступность
      ansible.windows.win_ping:

    - name: Создать каталог
      ansible.windows.win_file:
        path: C:\Windows\Temp\vektor
        state: directory

    - name: Положить файл-визитку
      ansible.windows.win_copy:
        content: "Хост {{ inventory_hostname }} управляется Ansible\r\n"
        dest: C:\Windows\Temp\vektor\info.txt
```

`---` — начало YAML-документа; `hosts` — на какую группу; `become: true` — повышение прав через sudo; `{{ ... }}` — подстановка переменной. Задачи выполняются сверху вниз, каждая должна иметь `name` — это ваш отчёт о работе.

Для Linux нужен SSH-ключ без пароля: `ssh-keygen -t ed25519` и `ssh-copy-id <ваш пользователь>@10.10.10.31`. `ansible_become_password` в inventory — учебный костыль (пароль лежит открытым текстом); в нормальной среде делают отдельного сервисного пользователя с `NOPASSWD` в sudoers только на нужные команды.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- `UBU01` 10.10.10.30 — Ubuntu Server 24.04, доступ по SSH, есть выход в интернет через NAT.
- `ASTRA01` 10.10.10.31 — Astra Linux, SSH включён.
- `DC01` 10.10.10.10 и `FS01` 10.10.10.20 — Windows Server 2022, домен `corp.vektor.lab`, учётка `adm.student`.
- Все машины в сети VMnet8, пингуются друг друга.

> ⚠️ В лабе мы даём Ansible доменную админскую учётку `adm.student` — это удобно, но **небезопасно**. В реальной работе под автоматизацию заводят отдельного сервисного пользователя с минимальными правами (например, только локальный админ на серверах), а пароль хранят в Ansible Vault.

### Снапшот

В VMware Workstation: выделить ВМ → **VM → Snapshot → Take Snapshot**, имя `before-l1-081`. Сделать на `UBU01`, `DC01`, `FS01` и `ASTRA01`. `WIN11-CL01` и `WIN10-CL02` не затрагиваем — снапшот не нужен.

### Шаги

1. **DC01, PowerShell от администратора** — включить remoting:
   ```powershell
   Enable-PSRemoting -Force
   ```
   Ожидаемо: `WinRM has been updated to receive requests` и создан HTTP-листener на 5985.

2. **DC01** — выпустить самоподписанный сертификат и повесить HTTPS-листener:
   ```powershell
   $cert = New-SelfSignedCertificate -DnsName "dc01.corp.vektor.lab" -CertStoreLocation Cert:\LocalMachine\My
   New-WSManInstance -ResourceURI winrm/config/Listener -SelectorSet @{Address="*";Transport="HTTPS"} `
     -ValueSet @{Hostname="dc01.corp.vektor.lab";CertificateThumbprint=$cert.Thumbprint}
   New-NetFirewallRule -DisplayName "WinRM HTTPS 5986" -Direction Inbound -Protocol TCP -LocalPort 5986 -Profile Domain,Private -Action Allow
   ```
   Ожидаемо: без ошибок. Сертификат самоподписанный — поэтому в inventory стоит `ansible_winrm_server_cert_validation=ignore` (в боевой среде сертификат берут у внутреннего CA).

3. **DC01** — проверить, что листener есть:
   ```powershell
   winrm enumerate winrm/config/listener
   ```
   Ожидаемо: два блока — `Transport = HTTP` (5985) и `Transport = HTTPS` (5986).

4. **FS01** — повторить шаги 1–3, подставив `fs01.corp.vektor.lab`.

5. **UBU01** — установить Ansible и pywinrm (команда из раздела 4, п.1). Ожидаемо: `ansible --version` печатает версию без ошибок.

6. **UBU01** — создать проект:
   ```bash
   mkdir -p ~/ansible-vektor && cd ~/ansible-vektor
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
   ssh-copy-id <ваш пользователь>@10.10.10.31
   ```
   Ожидаемо: `ssh <ваш пользователь>@10.10.10.31` заходит без пароля.

7. **UBU01** — создать `inventory.ini` и `ansible.cfg`:
   ```ini
   # ansible.cfg
   [defaults]
   inventory = ./inventory.ini
   host_key_checking = False
   interpreter_python = auto_silent
   ```
   Ожидаемо: `ansible-inventory --graph` показывает `@linux: ubu01, astra01` и `@windows: dc01, fs01`.

8. **UBU01** — прогнать ad-hoc проверки из раздела 4 (п.3 и п.4). Ожидаемо: четыре `SUCCESS` с `"ping": "pong"`. Если `dc01`/`fs01` дают `UNREACHABLE` — вернуться к шагу 3 и проверить firewall на порт 5986.

9. **UBU01** — создать `site.yml` из раздела 4 и проверить синтаксис:
   ```bash
   ansible-playbook site.yml --syntax-check
   ```
   Ожидаемо: `playbook: site.yml` без ошибок.

10. **UBU01** — сухой прогон, затем боевой:
    ```bash
    ansible-playbook site.yml --check --diff
    ansible-playbook site.yml
    ```
    Ожидаемо: первый прогон — `changed` на задачах создания каталога и файла; итог `failed=0`. Второй прогон — тот же плейбук даёт `changed=0`. Это и есть идемпотентность.

11. **UBU01** — убедиться в результате:
    ```bash
    ansible linux -m ansible.builtin.command -a 'cat /opt/vektor/info.txt'
    ansible windows -m ansible.windows.win_shell -a 'Get-Content C:\Windows\Temp\vektor\info.txt'
    ```
    Ожидаемо: по строке с именем каждого хоста.

12. **Записать в `stand-log.md`**: что сделано, какие листener'ы созданы, где лежат файлы.

### Проверка

- [ ] `ansible --version` и `ansible --version | grep winrm` не дают ошибок
- [ ] `ansible-inventory --graph` видит 4 хоста в двух группах
- [ ] `ansible linux -m ping` → `pong` на `ubu01` и `astra01`
- [ ] `ansible windows -m ansible.windows.win_ping` → `pong` на `dc01` и `fs01`
- [ ] `winrm enumerate winrm/config/listener` на DC01 и FS01 показывает HTTPS 5986
- [ ] Первый прогон `site.yml` → `failed=0`, есть `changed`
- [ ] Второй прогон → `changed=0`
- [ ] Файлы `/opt/vektor/info.txt` и `C:\Windows\Temp\vektor\info.txt` существуют
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

**Заявка № 4131.** Пользователь: тимлид ИТ (внутренняя заявка). ВМ: UBU01 / FS01. Что написал: «Твой `site.yml` с ночи валится на FS01 с `UNREACHABLE`. FS01 перезагружали под обновления. Разберись до конца смены, на нём печать висит». Срочность: Высокая.

**Как воспроизвести поломку.** На FS01 (или в его сетевых настройках) сменить сетевой профиль так, чтобы он перестал быть доменным:

```powershell
Get-NetConnectionProfile
Set-NetConnectionProfile -InterfaceIndex <номер> -NetworkCategory Public
```

Правило firewall `WinRM HTTPS 5986` мы создавали с `-Profile Domain,Private` — на профиле Public порт 5986 закрывается. Плейбук падает.

**Диагностика по шагам (сверху вниз, от простого к сложному):**

1. `ping 10.10.10.20` с UBU01 — хост вообще жив? Если нет — это уже не Ansible, а сеть/питание, эскалация.
2. `nc -zv 10.10.10.20 5986` (или `Test-NetConnection fs01 -Port 5986` с любой Windows-машины) — порт открыт? `Connection refused/timeout` → идём в firewall.
3. На FS01: `Get-Service WinRM` — служба `Running`? Если `Stopped`: `Start-Service WinRM; Set-Service WinRM -StartupType Automatic`.
4. На FS01: `winrm enumerate winrm/config/listener` — HTTPS-листener на месте и с тем же сертификатом?
5. На FS01: `Get-NetConnectionProfile` — вот и причина: `NetworkCategory = Public`.
6. На FS01: `Get-NetFirewallRule -DisplayName "WinRM HTTPS 5986" | Get-NetFirewallPortFilter` — убедиться, что правило существует и слушает 5986.
7. `ansible windows -m ansible.windows.win_ping --limit fs01 -vvv` с UBU01 — при необходимости посмотреть, на каком этапе рвётся.

**Решение:**

```powershell
Set-NetConnectionProfile -InterfaceIndex <номер> -NetworkCategory DomainAuthenticated
```

Если FS01 не видит DC, профиль сам не станет доменным — тогда временно `Private` (это уже компромисс), а параллельно чинить доверие к домену (`Test-ComputerSecureChannel`). Затем с UBU01:

```bash
ansible windows -m ansible.windows.win_ping --limit fs01
ansible-playbook site.yml --limit fs01
```

**Ответ пользователю:** «Разобрался. После перезагрузки FS01 сетевой профиль слетел на «Общественный», и правило файрвола для WinRM перестало применяться — поэтому автоматизация не могла подключиться. Вернул профиль в доменный, проверил: FS01 отвечает, плейбук проходит, печать работает. Причина — профиль сети, не сам Ansible. Рекомендация: после обновлений проверять `Get-NetConnectionProfile` на серверах; могу добавить это в чек-лист.»

**Закрытие заявки:** категория «Сеть / конфигурация», решение — «смена сетевого профиля, проверка firewall-правила», корневая причина — «профиль сети Public после перезагрузки», профилактика — «добавить проверку в пост-обновленческий чек-лист».

### Что изменилось на стенде

- `UBU01` (10.10.10.30) — установлены `ansible`, `python3-pywinrm`; создан каталог `~/ansible-vektor` (`inventory.ini`, `ansible.cfg`, `site.yml`); сгенерирован SSH-ключ `~/.ssh/id_ed25519`, ключ скопирован на `ASTRA01`.
- `DC01` (10.10.10.10) и `FS01` (10.10.10.20) — включён PowerShell Remoting, созданы WinRM листener'ы HTTP/5985 и HTTPS/5986, выпущены самоподписанные сертификаты, добавлено firewall-правило `WinRM HTTPS 5986` (профили Domain, Private).
- На `UBU01` и `ASTRA01` создан каталог `/opt/vektor` с файлом `info.txt`.
- На `DC01` и `FS01` создан каталог `C:\Windows\Temp\vektor` с файлом `info.txt`.
- `DC01`/`FS01` — учётка `adm.student` используется как «сервисная» для Ansible (учебное упрощение).

### Откат

VMware Workstation → выбрать ВМ → **VM → Snapshot → Snapshot Manager** → выбрать `before-l1-081` → **Go To** (или **Revert**). Повторить для `UBU01`, `DC01`, `FS01`, `ASTRA01`. Если откатывать не хочется, достаточно удалить каталоги `/opt/vektor` и `C:\Windows\Temp\vektor` и остановить WinRM — но снапшот надёжнее и быстрее.

## 6. Ошибки новичков и безопасность

1. **Пароль в открытом виде в inventory, закоммиченном в Git** → утечка учётки доменного админа в репозиторий. → `ansible-vault encrypt_string 'Lab#2026-vektor' --name 'ansible_password'` и хранить только зашифрованный блок; `inventory.ini` — в `.gitignore`.
2. **Использовать `adm.student` для автоматизации** → один скомпрометированный файл = весь домен. → Отдельный сервисный аккаунт, права только там, где нужно.
3. **`hosts: all` без `--limit`** → плейбук «на всякий случай» проходит по DC и роняет прод. → Всегда `--check` и `--limit` на первом прогоне, `hosts` — узкая группа.
4. **`shell`/`command` для настройки** → плейбук не идемпотентен: файл дописывается, служба перезапускается каждый раз. → Искать штатный модуль (`win_service`, `lineinfile`, `copy`), а `shell` — только для чтения/проверки.
5. **`ansible_winrm_server_cert_validation: ignore` в боевой среде** → отключается проверка сертификата, возможен MITM. → В лабе допустимо, в работе — сертификат от внутреннего CA или `kerberos`.
6. **`AllowUnencrypted = true` + `basic` на 5985** → пароль и данные идут открытым текстом. → Только HTTPS 5986 + `ntlm`/`kerberos`.
7. **Нет снапшота и нет `--check`** → непонятно, что именно сломалось и что откатывать. → Снапшот `before-l1-081` + `--diff` перед запуском.
8. **`interpreter_python` не настроен / на Astra стоит старый Python** → `FAILED: MODULE FAILURE`. → `ansible -m setup` показывает версию Python; при необходимости `ansible_python_interpreter=/usr/bin/python3`.

**Безопасность.** Никогда не храните пароли в плейбуках и в Git, не запускайте плейбук по всему парку «на удачу», не открывайте WinRM в интернет и не отключайте файрвол «чтобы работало». В учебном стенде мы ослабили два места (пароль в inventory, игнор сертификата) — оба помечены и оба недопустимы в реальной работе. Пароль `Lab#2026-vektor` — только для этого стенда.

## 7. Вопросы на собеседовании

**1. «Чем Ansible отличается от обычных скриптов на PowerShell или Bash?»**
Ответ: Ansible не требует агента — работает по SSH/WinRM с уже существующими каналами. Главное отличие — декларативность и идемпотентность: я описываю желаемое состояние («служба запущена», «каталог существует»), а не последовательность команд; модуль сам проверяет текущее состояние и меняет только то, что нужно. Скрипт же выполняется целиком каждый раз и обычно не проверяет, сделано ли уже.
*Что хочет услышать интервьюер:* понимание agentless-модели и идемпотентности, а не заученное «Ansible — это конфигурационный менеджмент».
*Красный флаг:* «Ansible ставится на каждую машину» или путаница с Puppet/Chef.

**2. «Как Ansible подключается к Windows и что нужно подготовить на стороне хоста?»**
Ответ: по WinRM через библиотеку pywinrm. На хосте: включён PowerShell Remoting (`Enable-PSRemoting -Force`), поднят листener (5985 HTTP или 5986 HTTPS), открыт порт в файрволе с учётом сетевого профиля, учётка с правами администратора. В inventory: `ansible_connection=winrm`, `ansible_port=5986`, `ansible_winrm_transport=ntlm` (или kerberos), для самоподписанного сертификата — `ansible_winrm_server_cert_validation=ignore`. Ansible для Windows использует модули коллекции `ansible.windows`.
*Что хочет услышать интервьюер:* WinRM + питон-библиотека + порты + права.
*Красный флаг:* «по SSH» без уточнения, что OpenSSH-сервер надо отдельно ставить и настраивать; или «через RDP».

**3. «Что такое идемпотентность и как её проверить?»**
Ответ: свойство операции, при котором повторное выполнение не меняет результат. Проверяется вторым прогоном плейбука: должно быть `changed=0`. Ломают её `command`/`shell`/`win_shell`, потому что они не анализируют состояние, а просто выполняют команду.
*Что хочет услышать интервьюер:* что кандидат реально видел `changed=0` и понимает, зачем это нужно (предсказуемость, безопасные повторные запуски).
*Красный флаг:* «идемпотентность — это когда скрипт не падает при повторном запуске».

**4. «Плейбук падает с UNREACHABLE. Ваши действия?»**
Ответ: UNREACHABLE — проблема канала, а не задачи. Порядок: ping хоста → проверка порта (22 или 5985/5986) → служба sshd/WinRM запущена → firewall и сетевой профиль → листener и сертификат (Windows) → права/ключи (Linux) → запуск с `-vvv` для деталей. FAILED — это уже другая история: подключились, но упало внутри (нет прав, нет пакета, ошибка в параметрах).
*Что хочет услышать интервьюер:* чёткий порядок «снизу вверх» и различение UNREACHABLE/FAILED.
*Красный флаг:* «перезапущу Ansible» или «переустановлю плейбук».

**5. «Зачем нужен inventory и чем ad-hoc отличается от playbook?»**
Ответ: inventory — список управляемых хостов с группами и переменными подключения; без него Ansible не знает, куда идти. Ad-hoc — разовая команда по одному модулю (`ansible -m`), нужна для проверок и быстрых действий. Playbook — YAML-файл с набором задач, версионируется в Git, повторяем и документирован. Правило: если действие понадобится больше одного раза — оно становится плейбуком.
*Что хочет услышать интервьюер:* что ad-hoc — инструмент диагностики, а не способ настройки.
*Красный флаг:* «ad-hoc и playbook — одно и то же, просто разный синтаксис».

## 8. Источники (официальная документация)

- **docs.ansible.com** — раздел «Ansible concepts» (inventory, playbook, module); «Ansible playbooks» — введение в YAML-синтаксис; «Windows Remote Management» (WinRM setup, HTTPS listener, `ansible_winrm_transport`); «Ansible Vault» — шифрование секретов. Поиск: `ansible winrm setup windows`, `ansible playbook intro`, `ansible vault encrypt string`.
- **docs.ansible.com** — коллекция `ansible.windows`: справочник по `win_ping`, `win_copy`, `win_file`, `win_shell`, `win_command`, `setup`. Поиск: `ansible.windows collection modules`.
- **learn.microsoft.com** — «PowerShell Remoting over WinRM», `Enable-PSRemoting`, `New-WSManInstance`, `winrm enumerate winrm/config/listener`, «Windows Remote Management (WinRM)». Поиск: `about remote troubleshooting`, `winrm listeners powershell`.
- **ubuntu.com / help.ubuntu.com** — «Ubuntu Server documentation»: установка пакетов, настройка SSH, sudoers. Поиск: `ubuntu server openssh`.
- **pipx.pypa.io** — официальный способ установки Ansible через `pipx` [ПРОВЕРИТЬ актуальность рекомендации для вашей версии Ansible].
- Названия пакетов `ansible` и `python3-pywinrm` в репозитории Ubuntu 24.04 и их версии — `[ПРОВЕРИТЬ]` на момент чтения через `apt policy ansible python3-pywinrm`.
