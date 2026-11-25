# Ansible: инвентарь, роли, плейбуки для Windows и Linux, управление конфигурацией

## 1. Введение (зачем это нужно в реальной работе)

Заявка в поддержку «Вектора»:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Иван, у меня на машине нет папки `C:\Tools` и файла с настройками, про которые ты говорил. И на WIN10-CL02 у Петровой, говорят, тоже. Ты в прошлый раз руками три раза переделывал, может как-то автоматизировать?»
> **Срочность:** низкая

Ты уже умеешь писать скрипты на PowerShell, Bash и Python. Проблема в том, что скрипт — это «сделай вот это», а не «приведи систему к вот такому состоянию». Скрипт, запущенный дважды, может создать дубль, второй раз перезапустить службу, третий раз упасть на неожиданной ошибке. Скрипт ничего не знает про то, что у тебя 50 машин, часть из них Windows, часть Linux, и на каждой надо получить одинаковый результат.

Ansible решает именно эту задачу: **управление конфигурацией** — ты описываешь желаемое состояние в YAML-файле, а инструмент сам решает, что и где нужно поменять. Он не требует устанавливать агент на машины, работает и с Windows, и с Linux, и его конфигурация лежит в Git как обычный код (ты уже прошёл урок 85).

После главы ты должен уметь: написать инвентарь и `ansible.cfg`, проверить доступность Linux- и Windows-хостов, снять факты, собрать роль с tasks/templates/handlers, прогнать плейбук в режиме проверки и по ограниченному списку хостов, проверить идемпотентность, зашифровать пароль в Vault и объяснить на собеседовании, чем Ansible отличается от «просто скрипта».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ключевая идея — **agentless**: на управляемых хостах не нужен постоянно работающий клиент Ansible. Всё, что нужно, — это SSH на Linux и WinRM на Windows, то есть уже существующие механизмы удалённого администрирования.

Как это выглядит на стенде:

```text
                    ┌─────────────────────────────┐
                    │  Управляющий узел (control) │
                    │  UBU01  10.10.10.30         │
                    │  ansible-core + коллекции   │
                    │  ~/ansible-vektor           │
                    └───────────┬─────────────────┘
                                │
        ┌───────────────────────┼───────────────────────────┐
        │ SSH, TCP/22           │ WinRM, TCP/5985 (HTTP)    │
        │ Linux-хосты           │ или TCP/5986 (HTTPS)      │
        │                       │ Windows-хосты             │
        ▼                       ▼
   UBU01 (local)          FS01  10.10.10.20
   ASTRA01 10.10.10.31    WIN11-CL01 (DHCP, DNS-имя)
                          WIN10-CL02 (DHCP)
                                │
                                │ проверка учётки
                                ▼
                          DC01 10.10.10.10 (AD, Kerberos 88)
```

Как проходит один task:

1. Ansible читает инвентарь, выбирает хосты, которые попадают под play.
2. Для каждого task определяет модуль (например, `ansible.windows.win_service`).
3. Упаковывает модуль (Python-скрипт для Linux или PowerShell-скрипт для Windows) и копирует на целевой хост.
4. Запускает, получает результат в JSON, удаляет временный файл.
5. Показывает `ok` (изменений не нужно), `changed` (что-то поменял), `failed` или `unreachable`.

**Уровни OSI:** вся эта работа — прикладной уровень (L7). Ansible не изобретает свой транспорт — он говорит поверх SSH (Linux) и WinRM (Windows). Поэтому ты сначала чинишь связность и аутентификацию (пинг, порт, учётка), а уже потом лезешь в плейбук.

Основные понятия:

| Термин | Что это |
|---|---|
| Инвентарь (inventory) | Список хостов и групп: `linux`, `windows`, `all` |
| Playbook | YAML-файл: список play («этот play для этой группы хостов») |
| Task | Одна задача = один модуль с параметрами |
| Роль (role) | Переиспользуемый набор tasks, handlers, vars, templates, files |
| Коллекция (collection) | Пакет модулей/ролей, например `ansible.windows` |
| Факты (facts) | Собранные данные о хосте: ОС, IP, память, версия |
| Handler | Задача, которая запускается только если её «дёрнули» через `notify` (обычно перезапуск службы) |
| Vault | Шифрование секретов в файлах Ansible |
| Идемпотентность | Свойство: повторный запуск не меняет ничего и даёт `changed=0` |

Важный факт, о который спотыкаются все: **управляющий узел Ansible должен быть Linux/macOS/WSL. На Windows его не поставить.** Поэтому на стенде «мозгом» будет UBU01, а Windows-машины — только целями. Проверить доступность Windows можно только при выполнении двух условий: на цели настроен WinRM и на управляющем узле стоит библиотека `pywinrm`.

## 3. Аналогия из жизни

Представь прораба с наряд-заданием. Прораб не работает руками — у него есть исполнители (модули), каждому он выдаёт ровно одну операцию: «закрути болт до метки», «проверь, что дверь заперта». Перед тем как что-то делать, исполнитель смотрит на текущее состояние: если болт уже закручен до метки — ничего не трогает и пишет «изменений нет». Если нет — закручивает и пишет «сделано». Бригада (роль) — это пачка таких наряд-заданий, которая выдаётся всем объектам определённого типа, а журнал работ (handler) — то, что делается только по факту изменения, например «после замены замка перевыпустить ключи».

В конце: аналогия перестаёт работать в том, что прораб может импровизировать и сам догадаться, что дверь перекосило, — Ansible делает ровно то, что написано, и молча считает «всё хорошо», если написанное выполнено, даже когда написанное изначально было неправильным.

## 4. Практика и команды

**1. Установка управляющего узла.** На UBU01 в изолированном окружении (Ubuntu 24.04 не даст ставить pip-пакеты в системный Python — это защита PEP 668):

```bash
sudo apt update && sudo apt install -y python3-venv git
python3 -m venv ~/venv-ansible
source ~/venv-ansible/bin/activate
pip install ansible-core pywinrm
ansible --version
```

`pywinrm` — обязательная библиотека для подключения к Windows. Альтернатива: `sudo apt install ansible-core` из репозитория Ubuntu `[ПРОВЕРИТЬ имя пакета в вашем зеркале]`. В выводе `ansible --version` смотри три строки: версия `[core 2.x]`, какой `config file` подхватился, версия Python.

**2. Инвентарь.** Файл `inventory.ini` в каталоге проекта:

```ini
[linux]
UBU01 ansible_connection=local

[windows]
FS01
WIN11-CL01

[all:vars]
ansible_python_interpreter=auto_silent
```

Для `UBU01` ставим `local` (команды выполняются на самом UBU01, SSH не нужен). Для Windows имена хостов = DNS-имена: DC01 раздаёт DNS, и с UBU01 имена `FS01` и `WIN11-CL01` должны резолвиться. Проверить: `nslookup WIN11-CL01`. Если DNS ещё не настроен — добавь `ansible_host=<IP из ipconfig>`.

**3. `ansible.cfg`** — чтобы не писать ключи каждый раз:

```ini
[defaults]
inventory = ./inventory.ini
host_key_checking = True
deprecation_warnings = False
stdout_callback = yaml   # [ПРОВЕРИТЬ] в некоторых версиях доступен только после установки community.general
```

**4. Просмотр инвентаря:**

```bash
ansible-inventory --graph
ansible-inventory --host FS01
```

```text
@all:
  |--@linux:
  |  |--UBU01
  |--@windows:
  |  |--FS01
  |  |--WIN11-CL01
```

Смотреть: попал ли хост в нужную группу. Половина «мистических» ошибок — опечатка в имени группы.

**5. Проверка Linux-хоста:**

```bash
ansible linux -m ansible.builtin.ping
```

```text
UBU01 | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
```

Модуль `ping` — не ICMP! Он реально подключается по SSH, запускает Python и возвращает `pong`. Если он прошёл — связность и Python на хосте в порядке.

**6. Проверка Windows-хоста:**

```bash
ansible windows -m ansible.windows.win_ping
```

```text
FS01 | SUCCESS => {"changed": false, "ping": "pong"}
```

Модуль из коллекции `ansible.windows`. Если его нет — `ansible-galaxy collection install ansible.windows`.

**7. Просмотр документации по модулям:**

```bash
ansible-doc -l ansible.windows
ansible-doc ansible.windows.win_service
ansible-doc ansible.builtin.apt
```

Смотреть секции `OPTIONS` (что обязательно) и `RETURN` (что вернёт). Это быстрее, чем гуглить.

**8. Сбор фактов:**

```bash
ansible windows -m ansible.windows.setup -a "filter=ansible_os_name"
ansible UBU01 -m ansible.builtin.setup -a "filter=ansible_distribution*"
```

```text
WIN11-CL01 | SUCCESS => {
    "ansible_facts": {"ansible_os_name": "Microsoft Windows 11 Enterprise"},
    "changed": false
}
```

Факты потом используются в шаблонах и условиях `when:`.

**9. Запуск плейбука с проверкой:**

```bash
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --check --diff --limit WIN11-CL01
```

`--syntax-check` — только разбор YAML/структуры. `--check` — «прогон без изменений» (Ansible спрашивает у модулей, что бы они сделали). `--diff` — показать разницу в файлах. `--limit` — ограничить хосты. Начинать всегда с этой тройки на одной машине.

**10. Теги и выбор задач:**

```bash
ansible-playbook site.yml --list-tags
ansible-playbook site.yml --tags services
ansible-playbook site.yml --start-at-task "Создать каталог C:\Tools"
```

**11. Ад-хок команда «починить сейчас»:** например, поднять службу печати на всех Windows сразу:

```bash
ansible windows -m ansible.windows.win_service -a "name=Spooler state=started"
```

После этого обязательно убедись, что изменение попало в плейбук/роль, — иначе «ручная правка» разъедется с конфигурацией.

**12. Секреты в Vault:**

```bash
ansible-vault create group_vars/windows/vault.yml
ansible-vault edit group_vars/windows/vault.yml
ansible-vault view group_vars/windows/vault.yml
ansible-vault encrypt_string 'Lab#2026-vektor' --name 'vault_win_password'
```

Внутри `vault.yml` лежит одна строка:

```yaml
vault_win_password: 'Lab#2026-vektor'
```

Пароль от хранилища — в файл `.vault_pass` с правами `600` и в `.gitignore`. Запуск: `ansible-playbook site.yml --vault-password-file .vault_pass`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30) включён, есть sudo, доступ в интернет или локальное зеркало apt/pip.
- **FS01** (10.10.10.20) и **WIN11-CL01** включены, введены в домен `corp.vektor.lab`, вход под `VEKTOR\adm.student` возможен, пароль `Lab#2026-vektor`.
- DC01 (10.10.10.10) включён — от него зависит DNS-резолвинг имён.
- **DC01 как цель Ansible не используем** (на нём AD; ломать его ради лабораторной не нужно). DC01 участвует только как DNS и контроллер домена.
- WEB01/MON01 для этой главы не нужны. Если они у тебя уже есть, потом можно добавить их в группу `linux`, но сначала отработай на UBU01.

### Снапшот

В VMware Workstation для каждой затрагиваемой ВМ (**UBU01, FS01, WIN11-CL01**): меню `VM → Snapshot → Take Snapshot`, имя `before-l2-086`. Галочку «Snapshot the virtual machine's memory» лучше снять — так снапшот консистентнее и легче.

### Шаги

1. **UBU01.** Ставим управляющий узел по командам из п.4.1–4.2. Ожидаем: `ansible --version` печатает `[core 2.x]`, `pip list` содержит `pywinrm`.
2. **FS01, локально в консоли VMware, PowerShell от имени администратора.** Готовим WinRM:
   ```powershell
   Enable-PSRemoting -Force
   winrm enumerate winrm/config/listener
   ```
   Ожидаем: в выводе `Transport = HTTP`, `Port = 5985`, `Enabled = true`, `ListeningOn = 10.10.10.20`.
3. **FS01.** Проверяем профиль сети и правило фаервола (WinRM открывается только для профилей Домен/Частная):
   ```powershell
   Get-NetConnectionProfile
   Get-NetFirewallRule -DisplayGroup "Windows Remote Management" | Select DisplayName,Enabled,Profile
   ```
   Ожидаем профиль `DomainAuthenticated`. Если `Public` — `Set-NetConnectionProfile -InterfaceAlias <алиас> -NetworkCategory Private`.
4. **WIN11-CL01.** Повторяем шаги 2–3. Дополнительно узнаём адрес: `ipconfig` → `IPv4`. Проверяем, что имя резолвится: на UBU01 `nslookup WIN11-CL01`.
5. **UBU01.** Проверяем сетевую доступность порта:
   ```bash
   nc -zv 10.10.10.20 5985
   nc -zv 10.10.10.101 5985   # подставь реальный адрес WIN11-CL01 из шага 4
   ```
   Ожидаем `succeeded`/`open`. Если `Connection refused` — WinRM не слушает; если таймаут — фаервол.
6. **UBU01.** Создаём проект:
   ```bash
   mkdir -p ~/ansible-vektor && cd ~/ansible-vektor
   git init
   mkdir -p group_vars/windows
   ```
   Кладём `inventory.ini`, `ansible.cfg` (п. 4.2–4.3), а в `group_vars/windows.yml`:
   ```yaml
   ansible_connection: winrm
   ansible_port: 5985
   ansible_winrm_scheme: http
   ansible_winrm_transport: ntlm
   ansible_winrm_server_cert_validation: ignore
   ansible_user: 'VEKTOR\adm.student'
   ansible_password: "{{ vault_win_password }}"
   ```
   Обрати внимание: обратный слэш в YAML — только в одинарных кавычках. HTTP-порт 5985 и `cert_validation: ignore` — **учебное упрощение**; в боевой среде WinRM настраивают на 5986 с валидным сертификатом.
7. **UBU01.** Шифруем пароль:
   ```bash
   ansible-vault create group_vars/windows/vault.yml
   ```
   Внутрь: `vault_win_password: 'Lab#2026-vektor'`. Пароль хранилища сохраняем в `.vault_pass` (`chmod 600`), добавляем его в `.gitignore`.
8. **UBU01.** Ставим коллекцию и проверяем связь:
   ```bash
   source ~/venv-ansible/bin/activate
   ansible-galaxy collection install ansible.windows
   ansible-inventory --graph
   ansible UBU01 -m ansible.builtin.ping --ask-become-pass
   ansible windows -m ansible.windows.win_ping --vault-password-file .vault_pass
   ```
   Ожидаем `pong` от трёх хостов. Если от Windows приходит `401 Unauthorized` или `Access is denied` — вернись к учётке и шагу 2.
9. **UBU01.** Создаём роли:
   ```bash
   ansible-galaxy role init common_linux --init-path roles
   ansible-galaxy role init common_windows --init-path roles
   ```
   В `roles/common_windows/tasks/main.yml`:
   ```yaml
   ---
   - name: Создать каталог C:\Tools
     ansible.windows.win_file:
       path: C:\Tools
       state: directory

   - name: Положить файл-маркер управления
     ansible.windows.win_copy:
       content: "Управляется Ansible с UBU01. Хост: {{ inventory_hostname }}\r\n"
       dest: C:\Tools\managed-by-ansible.txt

   - name: Разложить конфиг агента печати
     ansible.windows.win_template:
       src: printer-agent.ini.j2
       dest: C:\Tools\printer-agent.ini
     notify: Перезапустить Spooler

   - name: Служба печати должна быть запущена
     ansible.windows.win_service:
       name: Spooler
       state: started
       start_mode: auto
   ```
   В `roles/common_windows/handlers/main.yml`:
   ```yaml
   ---
   - name: Перезапустить Spooler
     ansible.windows.win_service:
       name: Spooler
       state: restarted
   ```
   В `roles/common_windows/templates/printer-agent.ini.j2`:
   ```text
   host={{ inventory_hostname }}
   address={{ ansible_host | default(inventory_hostname) }}
   server=FS01
   ```
10. **UBU01.** Пишем `roles/common_linux/tasks/main.yml` (пакеты, `/etc/motd` из шаблона `motd.j2`, служба `ssh` включена) и корневой `site.yml`:
    ```yaml
    ---
    - name: Базовая настройка Linux
      hosts: linux
      become: true
      roles:
        - common_linux
      tags: [linux, base]

    - name: Базовая настройка Windows
      hosts: windows
      roles:
        - common_windows
      tags: [windows, base]
    ```
11. **UBU01.** Прогон по одному хосту в режиме проверки, затем по-настоящему:
    ```bash
    ansible-playbook site.yml --syntax-check
    ansible-playbook site.yml --check --diff --limit WIN11-CL01 --vault-password-file .vault_pass
    ansible-playbook site.yml --limit WIN11-CL01 --vault-password-file .vault_pass
    ```
    Ожидаем в `PLAY RECAP`: `changed=3` (файлы + handler), `failed=0`, `unreachable=0`.
12. **UBU01.** Повторный полный прогон:
    ```bash
    ansible-playbook site.yml --vault-password-file .vault_pass
    ```
    Ожидаем **`changed=0`** на обоих Windows-хостах — это и есть проверка идемпотентности.
13. **Запись в `stand-log.md`.**

### Проверка

- [ ] `ansible --version` работает на UBU01, `pywinrm` в списке пакетов venv.
- [ ] `ansible-inventory --graph` показывает `UBU01` в `linux`, `FS01` и `WIN11-CL01` в `windows`.
- [ ] `ansible windows -m ansible.windows.win_ping` даёт `pong` от обеих Windows-машин.
- [ ] На FS01 и WIN11-CL01 существует `C:\Tools\managed-by-ansible.txt` с именем своего хоста.
- [ ] На FS01 и WIN11-CL01 существует `C:\Tools\printer-agent.ini`, в нём правильные `host=` и `server=FS01`.
- [ ] Повторный прогон даёт `changed=0`.
- [ ] `group_vars/windows/vault.yml` в Git лежит в зашифрованном виде (`$ANSIBLE_VAULT;1.1;AES256` в первой строке), пароль в открытом виде не встречается нигде: `grep -r "Lab#2026" .` ничего не находит.
- [ ] В `stand-log.md` появилась запись про эту лабораторную.

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Иван, не могу распечатать платёжку: задания висят в очереди и не уходят. Принтер в списке есть. На FS01, говорят, то же самое. Вчера печаталось. Нужно до 18:00.»
> **Срочность:** средняя

**Как воспроизвести поломку.** В `roles/common_windows/tasks/main.yml` поменяй в задаче про Spooler `state: started` на `state: stopped`. Закоммить («временное изменение», как это обычно и бывает) и прогони:

```bash
ansible-playbook site.yml --limit windows --vault-password-file .vault_pass
```

Служба печати останавливается сразу на FS01 и WIN11-CL01.

**Диагностика по порядку:**

1. **Масштаб.** Жалоба пришла от пользователя одной ВМ, но ты слышишь «и на FS01 тоже». Сразу проверяем оба хоста — если проблема на нескольких машинах одновременно, это почти всегда централизованное изменение, а не поломка железа или драйвера.
   ```bash
   ansible windows -m ansible.windows.win_shell -a "Get-Service Spooler | Select-Object Name,Status,StartType" --vault-password-file .vault_pass
   ```
   Ожидаемо: `Stopped`/`Stopped`.
2. **Логи.** На WIN11-CL01 смотрим журнал System по источнику Service Control Manager:
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'} -MaxEvents 30 |
     Where-Object Message -match 'Print Spooler'
   ```
   Ищем время остановки и «инициатора». Остановка совпадает по времени с запуском плейбука — версия подтверждается.
3. **Кто менял и когда.** Идём в репозиторий плейбуков (навык из урока 85):
   ```bash
   cd ~/ansible-vektor
   git log --oneline -5
   git diff HEAD~1 -- roles/common_windows/tasks/main.yml
   ```
   Видим `- state: started` / `+ state: stopped`. Корневая причина найдена — **ошибка в конфигурации, а не сбой службы**.
4. **Что ещё затронуто.** Проверяем, не прилетело ли это на другие хосты и не изменилось ли что-то ещё:
   ```bash
   git diff HEAD~1 --stat
   ansible-playbook site.yml --check --diff --limit windows --vault-password-file .vault_pass
   ```
5. **Почему не поймали раньше.** `--check --diff` перед прогоном показал бы `state: stopped` в плане. Значит, процесс «проверка перед запуском» был пропущен.

**Решение.** Правим `state: stopped` обратно на `state: started`, коммитим нормальным сообщением (`fix(windows): вернуть запуск Spooler, ошибочно остановлен`), и приводим хосты к нужному состоянию тем же плейбуком:

```bash
ansible-playbook site.yml --limit windows --vault-password-file .vault_pass
ansible windows -m ansible.windows.win_service -a "name=Spooler state=started" --vault-password-file .vault_pass
```

Проверяем: `Get-Service Spooler` = `Running`, `Get-PrintJob -PrinterName <имя>` — очередь пуста или задания ушли.

**Что написать пользователю:**
> Иван, добрый день. Причина найдена: вчерашнее изменение конфигурации автоматизации ошибочно останавливало службу печати на WIN11-CL01 и FS01. Исправил, служба запущена, очередь печати обработана, ваш документ ушёл на печать. Если задание не выйдет в течение 5 минут — напишите, перезапущу очередь. Приношу извинения за неудобства.

**Закрытие заявки.** Категория: «Печать», приоритет средний. Корневая причина: «Ошибочное значение `state: stopped` в роли `common_windows`, применённое на группу `windows`; служба Spooler остановлена на FS01 и WIN11-CL01». Решение: «Конфигурация исправлена в репозитории, хосты приведены к целевому состоянию плейбуком, очередь печати очищена». Профилактика: `--check --diff --limit` как обязательный шаг, code review перед прогоном на группу.

### Что изменилось на стенде

- **UBU01**: создан venv `~/venv-ansible` (`ansible-core`, `pywinrm`), установлена коллекция `ansible.windows`, создан каталог `~/ansible-vektor` (git-репозиторий, `inventory.ini`, `ansible.cfg`, `site.yml`, `group_vars/`, `roles/common_linux`, `roles/common_windows`), есть файл `.vault_pass` (права 600, вне Git). Роль `common_linux` управляет пакетами и `/etc/motd`.
- **FS01** (10.10.10.20) и **WIN11-CL01**: включён и слушает WinRM на TCP/5985 (HTTP), разрешено правило фаервола «Windows Remote Management», создан каталог `C:\Tools`, в нём `managed-by-ansible.txt` и `printer-agent.ini`. Служба Spooler управляется Ansible (не менять руками — менять в роли).
- **Учётки**: для автоматизации используется доменная `VEKTOR\adm.student` (учебное упрощение, см. раздел 6).
- **Каталог проекта** теперь единая точка правды для настроек Windows-клиентов и Linux-хостов.

### Откат

Если нужно вернуть стенд в исходное состояние: в VMware Workstation выбери ВМ → `VM → Snapshot → Snapshot Manager` → снапшот `before-l2-086` → `Revert To Snapshot`. Повтори для UBU01, FS01 и WIN11-CL01. Откатывать нужно все три — иначе в инвентаре останутся хосты без WinRM.

Частичный откат без снапшота (когда остальное терять нельзя): выключить WinRM (`Disable-PSRemoting -Force`) на Windows-хостах, удалить каталог `C:\Tools`, а на UBU01 — `~/ansible-vektor` и venv. После любого отката — запись в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Автоматизация под доменным администратором.** Каждый плейбук ходит в систему с правами `Domain Admin`. → Одна опечатка в плейбуке роняет весь домен разом. → Заведи отдельную сервисную учётку `svc-ansible` с минимумом прав: членство в группе `Remote Management Users` на целевых хостах, права только на нужные объекты. `adm.student` — только для обучения на стенде.
2. **Пароль в открытом виде в инвентаре и в Git.** → Утечка = полный доступ ко всем машинам. → `ansible-vault`, `--vault-password-file`, `.gitignore` для `.vault_pass`. Пароли не печатать в вывод: `no_log: true` в задачах с секретами.
3. **Прогон сразу на всей группе без `--check` и `--limit`.** → Один неверный task меняет состояние всех хостов одновременно (как в лабораторной со Spooler). → Сначала `--syntax-check`, потом `--check --diff --limit <один хост>`, потом полный прогон. Всегда держать под рукой снапшот.
4. **«Сделать связь любой ценой»: включить basic-аутентификацию на WinRM, снять HTTPS-требования, отключить `host_key_checking`.** → Логин и пароль идут практически открытым текстом, SSH-перехват не детектируется. → На стенде допустимо, в бою — NTLM/Kerberos и HTTPS (5986) с валидным сертификатом; `host_key_checking` не отключать, а управлять `known_hosts`.
5. **Всё делать через `win_shell`/`shell`/`command`.** → Теряется идемпотентность, `--check` бесполезен, `changed` всегда 1, ошибки не отлавливаются. → Сначала искать модуль (`win_service`, `win_file`, `apt`, `service`); `shell` — последнее средство, и тогда с `creates:`/`when:`.
6. **Не проверять идемпотентность.** → Плейбук «работает», но при каждом запуске перезапускает службы и рвёт сессии пользователей. → Второй прогон должен давать `changed=0`; перезапуски — только через `handlers`.
7. **Ожидать, что Ansible «сам догадается».** → Задачи без явных `state:`, без `when:`, шаблоны с захардкоженными путями; при добавлении нового хоста всё падает. → Явно описывать целевое состояние, использовать `defaults/` для переменных и `group_vars/` для групп.
8. **Нет версионирования и пиннинга коллекций.** → На другом управляющем узле (или после обновления) модуль ведёт себя иначе. → `requirements.yml` с версиями коллекций, плейбуки — в Git, изменения — через PR и ревью (урок 85).

**Безопасность.** Всё, что даёт Ansible, — это массовое выполнение команд с высокими правами. Поэтому: инвентарь и плейбуки — такой же защищаемый актив, как контроллер домена (репозиторий с ограниченным доступом, ревью, аудит коммитов); управляющий узел UBU01 — критичный хост, его компрометация = компрометация всей инфраструктуры; пароль Vault не хранится рядом с зашифрованными файлами в одном бэкапе. Отдельно: `win_shell` с шаблонной подстановкой пользовательских данных — это путь к инъекции команд на целевых хостах.

## 7. Вопросы на собеседовании

**1. Чем Ansible отличается от Puppet/Chef и почему его называют agentless?**

Ответ: Ansible не устанавливает на целевые хосты постоянный агент. Управление идёт по уже существующим каналам: SSH для Linux, WinRM для Windows. Модуль (Python- или PowerShell-скрипт) копируется на хост в момент выполнения, отрабатывает, возвращает JSON и удаляется. Из этого следует: быстрый старт, меньше компонентов для обновления и защиты, но и ограничения — нет постоянного «стража», который сам приводит хост к состоянию (это делается регулярным запуском плейбука из планировщика или CI), и есть требования к каналу: SSH-ключи, WinRM + `pywinrm`, PowerShell 3.0+ и .NET 4.0+ на Windows.

*Что хочет услышать интервьюер:* понимание модели push + agentless и её цены (нужен планировщик, нужен настроенный WinRM), а не заученное «Ansible лучше».
*Красный флаг:* «Ansible работает без ничего, просто по SSH, и с Windows тоже по SSH» — на стенде Windows управляется через WinRM, а управляющий узел вообще не ставится на Windows.

**2. Что такое идемпотентность и как ты её проверяешь?**

Ответ: это свойство задачи приводить систему к заданному состоянию и не делать ничего, если состояние уже такое. Проверяю тремя способами: повторный прогон плейбука должен давать `changed=0` в `PLAY RECAP`; `--check` показывает планируемые изменения без применения; `--diff` показывает, какие файлы и как изменятся. Для этого и нужны модули вместо `shell`: `win_service` сам смотрит текущее состояние службы, а `win_shell "sc start Spooler"` всегда рапортует изменение.

*Что хочет услышать интервьюер:* что человек понимает разницу «changed vs ok» и не строит автоматизацию на скриптах-однодневках.
*Красный флаг:* «идемпотентность — это когда плейбук можно запускать много раз» без упоминания, как это измеряется и почему `shell` её ломает.

**3. Что нужно подготовить, чтобы управлять Windows-хостом из Ansible?**

Ответ: со стороны цели — включённый WinRM (`Enable-PSRemoting -Force`), слушатель на 5985 (HTTP) или 5986 (HTTPS), разрешающее правило фаервола для нужного сетевого профиля, PowerShell 3.0+ и .NET 4.0+, учётка с правами локального администратора (или в группе `Remote Management Users` для минимальных прав). Со стороны управляющего узла — Linux-хост с `ansible-core`, библиотека `pywinrm` (для Kerberos — `pywinrm[kerberos]`), коллекция `ansible.windows`, в инвентаре `ansible_connection: winrm`, `ansible_port`, `ansible_winrm_transport: ntlm` или `kerberos`, формат учётки `VEKTOR\adm.student` или `adm.student@CORP.VEKTOR.LAB`. Проверка — `ansible.windows.win_ping`.

*Что хочет услышать интервьюер:* конкретика по портам, транспортам и библиотеке, плюс понимание, что HTTPS/Kerberos — правильный выбор, а HTTP/NTLM — упрощение для тестового стенда.
*Красный флаг:* «просто указываю пароль и всё работает» без знания, что нужен `pywinrm` и настроенный WinRM.

**4. Из чего состоит роль и зачем вообще роли и коллекции?**

Ответ: роль — это каталог с фиксированной структурой: `tasks/main.yml` (что делать), `handlers/main.yml` (реакции на изменения), `defaults/main.yml` (переменные, легко переопределяемые), `vars/main.yml` (переменные с высоким приоритетом), `templates/`, `files/`, `meta/main.yml` (зависимости). Создаётся командой `ansible-galaxy role init`. Роли нужны для переиспользования: одна роль `common_windows` применяется к любой группе хостов, а различия выносятся в переменные. Коллекция — упаковка модулей, ролей и плагинов от вендора или сообщества (`ansible.windows`, `community.windows`), ставится через `ansible-galaxy collection install` и пиннится в `requirements.yml`, чтобы у всех был одинаковый набор.

*Что хочет услышать интервьюер:* что человек не пишет один монолитный YAML на 500 строк, а структурирует конфигурацию и умеет разделять «логику» и «данные».
*Красный флаг:* путаница `defaults` и `vars`, «роль — это просто файл с задачами».

**5. Как безопасно хранить пароли и секреты в Ansible?**

Ответ: `ansible-vault` — шифрует файлы и отдельные строки (`ansible-vault create/edit/view`, `encrypt_string --name`). Расшифровка при запуске: `--ask-vault-pass` или `--vault-password-file`, пароль хранится вне репозитория с правами 600. Секреты кладутся в `group_vars/<группа>/vault.yml` и подставляются в обычные переменные, в Git попадает только шифротекст (`$ANSIBLE_VAULT;1.1;AES256`). Для задач с секретами — `no_log: true`, чтобы значения не попали в вывод и логи. В идеале — внешний менеджер секретов (Vault от HashiCorp и аналоги), а Ansible Vault — базовый уровень.

*Что хочет услышать интервьюер:* что секреты не лежат в инвентаре открытым текстом и что человек думает про логи и Git-историю.
*Красный флаг:* «пароль в `group_vars/all.yml`, репозиторий приватный, ничего не случится».

## 8. Источники (официальная документация)

- **docs.ansible.com** — официальная документация Ansible. Разделы: «Ansible concepts», «How to build your inventory», «Using variables», «Roles», «Using collections», «Ansible Vault», «Working with playbooks». Поиск: `ansible inventory guide`, `ansible roles directory structure`, `ansible vault encrypt_string`.
- **docs.ansible.com — Ansible for Windows** — раздел про управление Windows, WinRM, транспорты и `ansible.windows`. Поиск: `ansible windows winrm setup`, `ansible.windows win_service module`.
- **galaxy.ansible.com** — каталог коллекций и ролей (`ansible.windows`, `community.windows`). Поиск: `ansible galaxy ansible.windows collection`.
- **learn.microsoft.com** — документация Microsoft по WinRM и PowerShell Remoting: статьи «Windows Remote Management», «about_Remote_Troubleshooting», `Enable-PSRemoting`, `Test-WSMan`, `winrm enumerate winrm/config/listener`. Поиск: `winrm listener configuration powershell remoting`.
- **github.com/ansible/ansible** — репозиторий проекта; там же исторически лежит скрипт `ConfigureRemotingForAnsible.ps1` (каталог `examples/scripts`) `[ПРОВЕРИТЬ актуальный путь в текущей ветке]`.
- **ubuntu.com / help.ubuntu.com** — установка пакетов, `python3-venv`, работа с apt в Ubuntu Server 24.04. Поиск: `ubuntu 24.04 python venv pip externally managed environment`.
- **git-scm.com** — для версионирования плейбуков (перекликается с уроком 85). Поиск: `git diff between commits`, `git log oneline`.
