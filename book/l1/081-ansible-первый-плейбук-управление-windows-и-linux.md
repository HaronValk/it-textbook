# Ansible: первый плейбук, управление Windows и Linux

## 1. Введение (зачем это нужно в реальной работе)

Заявка в учебной очереди `C:\Support\Tickets`:

> **Пользователь:** Мария Петрова (HR), ВМ WIN10-CL02
> **Что написал:** «Иван из бухгалтерии говорит, что у него на WIN11-CL01 пропала папка с ярлыками на рабочем столе, а у меня на WIN10-CL02 не открывается общая папка `\\FS01\HR`. И ещё: у нас же в компании 50 человек, вы что, каждому вручную одно и то же настраиваете?»
> **Срочность:** средняя

Последний вопрос Марии — самый важный. Когда машин две, можно бегать руками. Когда их пятьдесят, а типовых действий десяток (создать пользователя, положить ярлык, проверить службу, обновить пакет), руками ты утонешь и обязательно где-то ошибёшься. Именно для этого придумали **Ansible** — инструмент, который описывает «как должно быть» в текстовом файле и сам приводит машины к этому состоянию.

После этой главы ты будешь уметь: поставить Ansible на UBU01, настроить инвентарь (список машин), проверить связь с Linux по SSH и с Windows по WinRM, написать первый плейбук, который создаёт файл на Linux и на Windows, и запускать его повторно, видя, что «ничего не изменилось» — это и есть идемпотентность, главная идея Ansible. Это прямой мост к DevOps: там без Ansible (или его аналогов) не обходится почти ни один проект.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ansible — **агентless** (без агента). На управляемых машинах ничего ставить не надо. Управляющая машина (у нас UBU01) подключается к целевой по уже существующему каналу:

- **Linux** — по **SSH** (порт 22), под учёткой `adm.student`, с sudo для привилегированных действий.
- **Windows** — по **WinRM** (Windows Remote Management, порт 5985 для HTTP или 5986 для HTTPS), под доменной или локальной учёткой с правами администратора.

Схема:

```text
                +-------------------+
                |   UBU01 (control) |
                |   ansible-core    |
                |   inventory.ini   |
                |   playbook.yml    |
                +---------+---------+
                          |
        +-----------------+------------------+
        |                                    |
   SSH :22                             WinRM :5985
        |                                    |
  +-----v------+   +-----------+      +------v------+   +-----------+
  |  DC01      |   |  FS01     |      | WIN11-CL01  |   | WIN10-CL02|
  | (Linux?   |   | (Linux?   |      |  Windows 11 |   | Windows 10|
  |  нет, Win) |   |  нет, Win)|      |  ivanov     |   |  petrova  |
  +------------+   +-----------+      +-------------+   +-----------+
        \             /
         \___________/
              |
         тоже WinRM :5985
```

Ключевые понятия:

- **Inventory (инвентарь)** — файл со списком машин и групп. Бывает INI или YAML.
- **Playbook (плейбук)** — YAML-файл со списком **play** (сценариев). Каждый play применяется к группе хостов и содержит **tasks** (задачи).
- **Task (задача)** — вызов **модуля** с параметрами. Модуль — это готовая функция (например, `ansible.builtin.file`, `ansible.windows.win_file`).
- **Module (модуль)** — маленький кусок кода, который Ansible копирует на целевую машину, выполняет и удаляет. Для Linux — `ansible.builtin.*`, для Windows — `ansible.windows.*`.
- **Idempotency (идемпотентность)** — повторный запуск того же плейбука не меняет систему, если она уже в нужном состоянии. Ansible сообщает `ok`, а не `changed`.
- **Facts (факты)** — сведения о машине (ОС, IP, память), которые Ansible собирает автоматически.
- **Handler (обработчик)** — задача, которая запускается только если другая задача что-то изменила (например, «перезапустить службу, если конфиг поменялся»).

Порты и требования:

| Что | Linux | Windows |
|-----|-------|---------|
| Транспорт | SSH | WinRM |
| Порт | 22 | 5985 (HTTP) / 5986 (HTTPS) |
| Учётка | `adm.student` + sudo | `adm.student` (админ) |
| Что нужно на целевой | SSH-сервер, Python 3 | WinRM, PowerShell 3+ |

На стенде Python 3 на UBU01 уже есть (Ubuntu 24.04 ставит его по умолчанию). На Windows 10/11 PowerShell 5.1 тоже есть. Не хватает только WinRM — его надо включить.

## 3. Аналогия из жизни

Представь, что ты — бригадир, а у тебя десять квартир, в каждой надо повесить полку. Можно ходить с молотком и гвоздями по каждой квартире (руками). А можно написать **инструкцию**: «в квартире №1 полка на высоте 1.5 м, в квартире №2 — на 1.6 м, гвоздь 60 мм». Отдаёшь инструкцию прорабу (Ansible), он идёт по списку квартир (inventory), в каждой делает что написано (task), и если полка уже висит на нужной высоте — ничего не трогает (идемпотентность). Если ты поменял высоту в инструкции — прораб перевесит только там, где надо.

Аналогия ломается на одном: прораб не «думает», он выполняет ровно то, что написано. Если в инструкции ошибка — он аккуратно и быстро сломает все десять квартир одинаково.

## 4. Практика и команды

Все команды — на **UBU01** под `adm.student`, если не сказано иное. Ansible ставим из репозитория Ubuntu через `apt`.

### 4.1. Установка Ansible

```bash
sudo apt update
sudo apt install -y ansible
ansible --version
```

`ansible` — метапакет, ставит `ansible-core` и коллекцию `ansible.builtin`. Вывод примерно:

```text
ansible [core 2.16.x]
  config file = /etc/ansible/ansible.cfg
  python version = 3.12.x
```

Смотрим версию `core` — от неё зависит синтаксис. Если версия ниже 2.14 — часть модулей может отличаться `[ПРОВЕРИТЬ]`.

### 4.2. Инвентарь

Создай `~/ansible/inventory.ini`:

```ini
[linux]
UBU01 ansible_host=10.10.10.30 ansible_user=adm.student

[windows]
WIN11-CL01 ansible_host=10.10.10.51 ansible_user=adm.student ansible_connection=winrm ansible_port=5985 ansible_winrm_transport=ntlm ansible_winrm_server_cert_validation=ignore

[all:vars]
ansible_python_interpreter=/usr/bin/python3
```

Разбор:
- `[linux]`, `[windows]` — группы.
- `ansible_host` — IP, `ansible_user` — учётка.
- `ansible_connection=winrm` — сказать Ansible, что это Windows.
- `ansible_winrm_transport=ntlm` — тип аутентификации; для локальных учёток на не-домене NTLM работает, для домена лучше `kerberos` `[ПРОВЕРИТЬ]`.
- `ansible_winrm_server_cert_validation=ignore` — только для учебного HTTP-варианта; в бою — HTTPS и проверка сертификата.

### 4.3. Проверка связи

```bash
ansible -i inventory.ini linux -m ansible.builtin.ping
ansible -i inventory.ini windows -m ansible.windows.win_ping
```

`ping` (builtin) — не ICMP, а проверка, что Ansible может подключиться и выполнить Python. `win_ping` — то же для Windows через WinRM. Ожидаемо: `SUCCESS => {"ping": "pong"}`.

### 4.4. Сбор фактов

```bash
ansible -i inventory.ini all -m ansible.builtin.setup -a "filter=ansible_os_family"
```

`setup` собирает факты. `filter` оставляет только нужное. Вывод примерно:

```text
UBU01 | SUCCESS => { "ansible_facts": { "ansible_os_family": "Debian" } }
WIN11-CL01 | SUCCESS => { "ansible_facts": { "ansible_os_family": "Windows" } }
```

Смотрим `os_family` — по нему в плейбуках делают ветвление.

### 4.5. Ad-hoc команда на Linux

```bash
ansible -i inventory.ini linux -m ansible.builtin.command -a "uptime" -b
```

`-m` — модуль, `-a` — аргументы, `-b` — become (sudo). `command` не идемпотентен, поэтому для «сделать один раз» лучше `shell`/`command`, а для состояния — специальные модули.

### 4.6. Ad-hoc команда на Windows

```bash
ansible -i inventory.ini windows -m ansible.windows.win_command -a "hostname"
```

`win_command` — аналог `command` для Windows. Ожидаемо: `WIN11-CL01`.

### 4.7. Первый плейбук

`~/ansible/first.yml`:

```yaml
---
- name: Linux — создать файл-маркер
  hosts: linux
  become: true
  tasks:
    - name: Создать /tmp/ansible-marker
      ansible.builtin.file:
        path: /tmp/ansible-marker
        state: touch
        mode: "0644"

- name: Windows — создать файл-маркер
  hosts: windows
  tasks:
    - name: Создать C:\Temp\ansible-marker.txt
      ansible.windows.win_file:
        path: C:\Temp\ansible-marker.txt
        state: touch
```

Запуск:

```bash
ansible-playbook -i inventory.ini first.yml
```

Первый запуск: `changed=1` в каждой группе. Второй запуск: `ok=1, changed=0` — идемпотентность в действии.

### 4.8. Проверка синтаксиса и «сухой прогон»

```bash
ansible-playbook -i inventory.ini first.yml --syntax-check
ansible-playbook -i inventory.ini first.yml --check --diff
```

`--syntax-check` — только парсинг YAML. `--check` — прогон без изменений (dry-run), `--diff` — показать, что бы изменилось. Не все модули поддерживают `--check`, но `file`/`win_file` поддерживают.

### 4.9. Включение WinRM на Windows (через PowerShell от админа)

На WIN11-CL01 и WIN10-CL02 (позже — на DC01, FS01):

```powershell
Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Item WSMan:\localhost\Client\TrustedHosts -Value "10.10.10.30" -Force
```

`Enable-PSRemoting` включает службу WinRM и настраивает слушатель на 5985. `TrustedHosts` нужен, если нет домена (до L1 №42) — иначе клиент не доверяет UBU01. После L1 №42 и ввода в домен это можно убрать.

Проверка с UBU01:

```bash
curl -s http://10.10.10.51:5985/wsman | head -1
```

Ожидаемо: пустой ответ или 401 — значит порт открыт. Если `Connection refused` — WinRM не запущен или брандмауэр режет.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд в состоянии **перед L1 №81** по хронологии: DC01 в домене `corp.vektor.lab`, WIN11-CL01 и WIN10-CL02 введены в домен (L1 №44), GPO настроены (L1 №45), FS01 существует (L1 №46), Zabbix временно на UBU01 (L1 №51), на WIN11-CL01 стоит прикладное ПО (L1 №52–61), BitLocker включён (L1 №66), GLPI на UBU01 (L1 №70), Git/PowerShell/Bash/Python пройдены (L1 №77–80).
- UBU01: Ubuntu Server 24.04, `10.10.10.30`, SSH, `adm.student` с sudo.
- WIN11-CL01: `10.10.10.51` (DHCP), доменная учётка `ivanov` или `adm.student`.
- WIN10-CL02: `10.10.10.52` (DHCP), доменная `petrova` или `adm.student`.
- DC01: `10.10.10.10`, домен `corp.vektor.lab`, `adm.student` — доменный админ.
- Все ВМ включены, ping между ними проходит (правило ICMPv4 разрешено).

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ (**UBU01, WIN11-CL01, WIN10-CL02, DC01**) сделай снапшот с именем `before-l1-081`. Через GUI: правый клик по ВМ → Snapshot → Take Snapshot. Через `vmrun` с хоста:

```powershell
vmrun -T ws snapshot "C:\Lab\vektor\VMs\UBU01\UBU01.vmx" before-l1-081
vmrun -T ws snapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-081
vmrun -T ws snapshot "C:\Lab\vektor\VMs\WIN10-CL02\WIN10-CL02.vmx" before-l1-081
vmrun -T ws snapshot "C:\Lab\vektor\VMs\DC01\DC01.vmx" before-l1-081
```

Для WIN11-CL01 (зашифрована из-за vTPM) добавь пароль шифрования `[ПРОВЕРИТЬ синтаксис]`:

```powershell
vmrun -T ws -vp "Lab#2026-vektor" snapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-081
```

### Шаги

1. **На UBU01** установи Ansible:
    ```bash
    sudo apt update && sudo apt install -y ansible
    ansible --version
    ```
    Ожидаемо: строка с `ansible [core 2.16.x]` (или близкая версия).

2. **На UBU01** создай рабочую папку и инвентарь:
    ```bash
    mkdir -p ~/ansible && cd ~/ansible
    nano inventory.ini
    ```
    Вставь содержимое из раздела 4.2. Сохрани (`Ctrl+O`, `Enter`, `Ctrl+X`).

3. **На WIN11-CL01** открой PowerShell от администратора и включи WinRM:
    ```powershell
    Enable-PSRemoting -Force -SkipNetworkProfileCheck
    Set-Item WSMan:\localhost\Client\TrustedHosts -Value "10.10.10.30" -Force
    Get-Service WinRM
    ```
    Ожидаемо: `Status : Running`.

4. **На WIN10-CL02** повтори шаг 3. Ожидаемо: то же самое.

5. **На DC01** повтори шаг 3 (позже пригодится для проверки домена). Ожидаемо: `WinRM` запущен.

6. **На UBU01** проверь связь с Linux:
    ```bash
    cd ~/ansible
    ansible -i inventory.ini linux -m ansible.builtin.ping
    ```
    Ожидаемо: `UBU01 | SUCCESS => {"ping": "pong"}`.

7. **На UBU01** проверь связь с Windows:
    ```bash
    ansible -i inventory.ini windows -m ansible.windows.win_ping
    ```
    Ожидаемо: `WIN11-CL01 | SUCCESS => {"ping": "pong"}`. Если `UNREACHABLE` — вернись к шагу 3, проверь брандмауэр и `TrustedHosts`.

8. **На UBU01** создай `first.yml` из раздела 4.7 и запусти:
    ```bash
    ansible-playbook -i inventory.ini first.yml
    ```
    Ожидаемо: `changed=1` для каждой группы.

9. **На UBU01** запусти ещё раз:
    ```bash
    ansible-playbook -i inventory.ini first.yml
    ```
    Ожидаемо: `ok=1, changed=0` — идемпотентность.

10. **Проверка результата**: на UBU01 `ls -l /tmp/ansible-marker`, на WIN11-CL01 `Test-Path C:\Temp\ansible-marker.txt`. Ожидаемо: файл есть, размер 0.

11. **На UBU01** запусти `--check --diff`:
    ```bash
    ansible-playbook -i inventory.ini first.yml --check --diff
    ```
    Ожидаемо: `ok=1, changed=0` (файлы уже есть).

12. **Запись в журнал**: в `C:\Lab\vektor\stand-log.md` добавь блок «L1 №81: Ansible установлен на UBU01, WinRM включён на WIN11-CL01, WIN10-CL02, DC01, созданы `/tmp/ansible-marker` и `C:\Temp\ansible-marker.txt`».

### Проверка

- [ ] `ansible --version` на UBU01 показывает `core 2.16.x` (или близко).
- [ ] `ansible -i inventory.ini linux -m ansible.builtin.ping` → `pong`.
- [ ] `ansible -i inventory.ini windows -m ansible.windows.win_ping` → `pong`.
- [ ] `Get-Service WinRM` на WIN11-CL01, WIN10-CL02, DC01 → `Running`.
- [ ] Первый запуск `first.yml` → `changed=1`.
- [ ] Второй запуск → `changed=0`.
- [ ] Файлы `/tmp/ansible-marker` и `C:\Temp\ansible-marker.txt` существуют.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Заявка:**

> **Пользователь:** Иван Иванов (бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «После обеда перестал работать наш новый инструмент — Ansible с UBU01 не подключается к моей машине, пишет UNREACHABLE. До обеда всё работало. Я ничего не трогал.»
> **Срочность:** высокая

**Как воспроизвести поломку** (на WIN11-CL01, PowerShell от админа):

```powershell
Stop-Service WinRM
Set-Service WinRM -StartupType Disabled
```

Это имитирует «службу выключили» — например, после обновления или ручной чистки. Заявка совпадает: до этого `win_ping` работал, теперь нет.

**Диагностика (по шагам):**

1. На UBU01:
    ```bash
    ansible -i inventory.ini windows -m ansible.windows.win_ping
    ```
    Ожидаемо: `WIN11-CL01 | UNREACHABLE! => {"msg": "winrm connection error..."}`.

2. Проверь, что машина вообще жива:
    ```bash
    ping -c 2 10.10.10.51
    ```
    Ожидаемо: пакеты идут. Значит, сеть в порядке, проблема в WinRM.

3. Проверь порт:
    ```bash
    nc -zv 10.10.10.51 5985
    ```
    Ожидаемо: `Connection refused` — служба не слушает.

4. На WIN11-CL01 (через консоль VMware, потому что удалённо уже не зайти):
    ```powershell
    Get-Service WinRM
    ```
    Ожидаемо: `Status : Stopped`, `StartType : Disabled`.

**Решение** (на WIN11-CL01):

```powershell
Set-Service WinRM -StartupType Automatic
Start-Service WinRM
Get-Service WinRM
```

Ожидаемо: `Running`, `Automatic`.

**Проверка** (на UBU01):

```bash
ansible -i inventory.ini windows -m ansible.windows.win_ping
```

Ожидаемо: `pong`.

**Ответ пользователю:**

> Иван, добрый день. Причина — на вашей машине была остановлена и отключена служба WinRM (Windows Remote Management). Это та служба, через которую система управления Ansible подключается к компьютеру. Я её включил и поставил в автозапуск. Сейчас всё работает, проверьте, пожалуйста, и напишите, если снова возникнет.

**Закрытие заявки:** категория «Службы Windows», решение «WinRM остановлен и отключён, включён обратно, автозапуск восстановлен», время — 15 минут. В журнал `stand-log.md` добавь строку «WIN11-CL01: WinRM был Disabled, восстановлен в Automatic».

### Что изменилось на стенде

- **UBU01**: установлен пакет `ansible` (метапакет, тянет `ansible-core`), создана папка `/home/adm.student/ansible` с `inventory.ini` и `first.yml`, создан файл `/tmp/ansible-marker`.
- **WIN11-CL01**: включён WinRM (`Enable-PSRemoting`), добавлен `TrustedHosts` для `10.10.10.30`, создан файл `C:\Temp\ansible-marker.txt`.
- **WIN10-CL02**: включён WinRM, добавлен `TrustedHosts` для `10.10.10.30`.
- **DC01**: включён WinRM, добавлен `TrustedHosts` для `10.10.10.30`.
- **Хост**: снапшоты `before-l1-081` на UBU01, WIN11-CL01, WIN10-CL02, DC01; запись в `C:\Lab\vektor\stand-log.md`.
- **Новых ВМ, ролей, доменов, учёток не появилось.**

### Откат

В VMware Workstation: правый клик по ВМ → Snapshot Manager → выбери `before-l1-081` → **Revert**. Через `vmrun` с хоста:

```powershell
vmrun -T ws revertToSnapshot "C:\Lab\vektor\VMs\UBU01\UBU01.vmx" before-l1-081
vmrun -T ws revertToSnapshot "C:\Lab\vektor\VMs\WIN10-CL02\WIN10-CL02.vmx" before-l1-081
vmrun -T ws revertToSnapshot "C:\Lab\vektor\VMs\DC01\DC01.vmx" before-l1-081
vmrun -T ws -vp "Lab#2026-vektor" revertToSnapshot "C:\Lab\vektor\VMs\WIN11-CL01\WIN11-CL01.vmx" before-l1-081
```

После отката не забудь убрать из `stand-log.md` запись про L1 №81, чтобы журнал соответствовал реальному состоянию.

## 6. Ошибки новичков и безопасность

1. **Ошибка:** запускать `ansible-playbook` без `--check` на проде.
   **К чему приводит:** плейбук с ошибкой в параметре (например, `state: absent` вместо `state: present`) снесёт файл на всех машинах сразу.
   **Как правильно:** сначала `--syntax-check`, потом `--check --diff`, потом на одной машине (`--limit WIN11-CL01`), потом на всех.

2. **Ошибка:** хранить пароли в `inventory.ini` открытым текстом.
   **К чему приводит:** пароль утекает в Git, в бэкапы, в скриншоты.
   **Как правильно:** `ansible-vault` для секретов, `ansible_ssh_private_key_file` для SSH-ключей. На учебном стенде пароль `Lab#2026-vektor` — только потому что стенд.

3. **Ошибка:** использовать `ansible.builtin.command`/`shell` там, где есть специальный модуль.
   **К чему приводит:** нет идемпотентности, каждый запуск `changed=1`, скрипт «делает» одно и то же по кругу.
   **Как правильно:** для файлов — `file`/`copy`/`template`, для пакетов — `apt`/`win_chocolatey`, для служб — `service`/`win_service`.

4. **Ошибка:** забыть `become: true` для задач, требующих root на Linux.
   **К чему приводит:** `Permission denied` в середине плейбука, часть задач выполнена, часть нет.
   **Как правильно:** `become: true` на уровне play или task, и проверять `--check` заранее.

5. **Ошибка:** `ansible_winrm_server_cert_validation=ignore` в боевой среде.
   **К чему приводит:** MITM-атака на WinRM-трафик, перехват учётных данных.
   **Как правильно:** HTTPS-слушатель WinRM (5986) с валидным сертификатом от внутреннего CA.

6. **Ошибка:** запускать плейбук против группы `all`, когда нужна одна машина.
   **К чему приводит:** неожиданные изменения на DC01, FS01 и т.д.
   **Как правильно:** `--limit` или отдельные группы в инвентаре.

7. **Ошибка:** не читать вывод `changed`/`ok`/`failed`.
   **К чему приводит:** плейбук «прошёл», а по факту упал на середине, и ты этого не заметил.
   **Как правильно:** смотреть `PLAY RECAP` в конце, `failed=0` — обязательное условие успеха.

### Безопасность

- **Не храни пароли в плейбуках и инвентаре.** В боевой среде — `ansible-vault`, внешние хранилища секретов (HashiCorp Vault, CyberArk), интеграция с AD.
- **Не отключай проверку сертификата WinRM в проде.** На стенде `ignore` — потому что HTTP и самоподписанный, в бою — только HTTPS.
- **Не давай `adm.student` полные права на всё.** В реальной работе Ansible-учётка — отдельная, с минимально нужными правами (например, только на OU `Компьютеры`).
- **Не запускай плейбуки с машины, где ты сидишь за чашкой кофе.** Ansible — это «одна команда на 50 машин». Ошибка стоит дорого.
- **Логируй запуски.** В проде — `log_path` в `ansible.cfg` и/или централизованный сбор логов (в нашем стенде это будет Zabbix/GLPI, но по-хорошему — SIEM).

## 7. Вопросы на собеседовании

**Вопрос 1. Что такое Ansible и почему его называют agentless?**

- **Ответ:** Ansible — система управления конфигурациями. Она описывает желаемое состояние инфраструктуры в YAML-файлах (плейбуках) и приводит машины к этому состоянию. Agentless — потому что на управляемых машинах не нужен постоянный агент: Ansible подключается по уже существующим каналам (SSH для Linux, WinRM для Windows), копирует модуль, выполняет его и удаляет. Это снижает порог входа и не требует ставить софт на каждый хост.
- **Что хочет услышать интервьюер:** понимание, что Ansible не «ходит по SSH и выполняет команды», а использует модули и идемпотентность.
- **Красный флаг:** «Ansible — это как SSH, только с YAML». Это неполно, теряется суть модулей и идемпотентности.

**Вопрос 2. Что такое идемпотентность и почему это важно?**

- **Ответ:** Идемпотентность — свойство операции давать один и тот же результат при повторном выполнении. В Ansible это значит: если система уже в нужном состоянии, повторный запуск плейбука ничего не меняет и сообщает `ok`, а не `changed`. Это критично, потому что позволяет безопасно перезапускать плейбуки, не боясь сломать систему, и видеть, где реально произошли изменения. На стенде я это проверял: первый запуск `first.yml` дал `changed=1`, второй — `changed=0`.
- **Что хочет услышать интервьюер:** понимание, что идемпотентность — не «фича», а основа безопасной автоматизации.
- **Красный флаг:** «Идемпотентность — это когда скрипт можно запускать дважды». Неполно, не хватает «и результат не меняется».

**Вопрос 3. Как Ansible управляет Windows и что для этого нужно?**

- **Ответ:** Через WinRM (Windows Remote Management) — встроенный в Windows протокол на базе SOAP/WS-Management, порт 5985 (HTTP) или 5986 (HTTPS). На целевой машине нужно: включить WinRM (`Enable-PSRemoting`), настроить слушатель, при отсутствии домена — добавить управляющую машину в `TrustedHosts`, дать учётке права администратора. В инвентаре указывается `ansible_connection=winrm`, `ansible_winrm_transport=ntlm` (или `kerberos` в домене). Модули для Windows — из коллекции `ansible.windows` (`win_file`, `win_service`, `win_command` и т.д.).
- **Что хочет услышать интервьюер:** знание, что WinRM — не SSH, и что для Windows нужны отдельные модули.
- **Красный флаг:** «Ansible работает с Windows по SSH». Не работает без дополнительных костылей (OpenSSH for Windows), и это не стандартный путь.

**Вопрос 4. Чем отличается `command` от `shell` и когда что использовать?**

- **Ответ:** `command` запускает команду напрямую, без shell-обёртки: не работают пайпы, редиректы, переменные окружения, `&&`. `shell` запускает через `/bin/sh` (или PowerShell на Windows), поэтому всё это работает. Оба не идемпотентны — каждый запуск `changed=1`. Использовать их стоит только когда нет специального модуля: например, для `systemctl status` или для вызова утилиты без модуля. Для файлов, пакетов, служб — специальные модули (`file`, `apt`, `service`).
- **Что хочет услышать интервьюер:** понимание, что `command`/`shell` — «последнее средство», а не основной инструмент.
- **Красный флаг:** «Всегда использую `shell`, он универсальнее». Это прямой путь к неидемпотентным плейбукам.

**Вопрос 5. Что такое inventory и какие бывают способы его описания?**

- **Ответ:** Inventory — список управляемых хостов и их групп. Бывает статический (INI или YAML-файл) и динамический (скрипт или плагин, который тянет список из внешнего источника — облака, CMDB, LDAP). В статическом INI группы задаются в `[квадратных скобках]`, переменные — через `ansible_host`, `ansible_user` и т.д. или в секции `[group:vars]`. На стенде я использовал статический INI с группами `linux` и `windows`.
- **Что хочет услышать интервьюер:** что кандидат знает про динамический инвентарь — это признак, что он не только «домашний» Ansible видел.
- **Красный флаг:** «Inventory — это просто файл со списком IP». Неполно, теряются группы, переменные, динамический инвентарь.

## 8. Источники (официальная документация)

- **Ansible Documentation** (docs.ansible.com) — раздел «Ansible concepts», статья «How Ansible works»; поиск: `ansible how it works`.
- **Ansible Documentation** — раздел «Using Ansible inventory», статья «How to build your inventory»; поиск: `ansible inventory ini yaml`.
- **Ansible Documentation** — раздел «Using Ansible playbooks», статья «Ansible playbooks»; поиск: `ansible playbook yaml syntax`.
- **Ansible Documentation** — коллекция `ansible.windows`, раздел «Windows Remote Management»; поиск: `ansible windows winrm setup`.
- **Microsoft Learn** (learn.microsoft.com) — раздел «Windows Remote Management», статья «Installation and Configuration for Windows Remote Management»; поиск: `winrm installation configuration`.
- **Ansible Documentation** — раздел «Ansible Vault», статья «Encrypting content with Ansible Vault»; поиск: `ansible vault encrypt`.
- **Ansible Documentation** — раздел «Working with playbooks», статья «Validating tasks: check mode and diff mode»; поиск: `ansible check mode diff`.

Версии `ansible-core` и точный синтаксис `vmrun -vp` для зашифрованных ВМ — `[ПРОВЕРИТЬ]` в твоей версии VMware Workstation и Ubuntu 24.04.
