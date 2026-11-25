# Ansible в продакшене: роли, инвентари, Vault, коллекции, тестирование (Molecule)

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4127. Иван Иванов (бухгалтерия), ВМ WIN11-CL01.** «Время на компьютере убежало на 7 минут, 1С не даёт закрыть период. И на внутренней wiki (WEB01) в логах время на 3 часа назад — по нему не понять, когда что случилось. Срочность: высокая».

Ты идёшь разбираться и находишь классику: на UBU01 и MON01 часовой пояс `Europe/Moscow`, на WEB01 — `UTC` (ставили «на глаз» при настройке nginx), на ASTRA01 пояс верный, но синхронизация времени выключена, на GW01 всё правильно, потому что настраивали последним и «по свежей памяти». Пять Linux-машин, три разных состояния, ни одного документа. И это ещё мелочь: так же разъезжаются пользователи, пакеты, sshd-конфиги, лимиты, сертификаты.

Ansible в продакшене — это не «запустить плейбук», а инженерная дисциплина: репозиторий как единственный источник правды, роли вместо простыней YAML, секреты в Vault, версии коллекций зафиксированы, изменения проверены линтером и Molecule до прода, раскатка идёт волнами по `serial`. После этой главы ты сможешь собрать такой репозиторий для парка «Вектора», безопасно катить его на DMZ через GW01, хранить пароли так, чтобы они не попали в git, и отвечать на вопросы про идемпотентность не по памяти, а по опыту.

Ручная настройка пяти хостов — это ещё и больно: 2 часа работы и гарантированный дрейф через месяц. Baseline-роль применяется за минуту и, что важнее, применяется повторно.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ansible — **агентless push-оркестратор**. На управляющем узле (control node) лежат playbook'и и инвентарь. Управляемые узлы (managed nodes) ничем не «обвешаны»: control node подключается по SSH (TCP 22) для Linux или по WinRM (TCP 5985/5986) для Windows, копирует туда Python-модуль, выполняет его на удалённой стороне, забирает результат в JSON и удаляет временные файлы.

Требования к узлам: на Linux — Python 3 (на Ubuntu 24.04 и Astra Linux он есть из коробки), на Windows — PowerShell и настроенный WinRM. Это ключевое отличие от Puppet/Chef/Salt-minion, где на каждом хосте живёт постоянный агент: у Ansible нет агента, но нет и непрерывного контроля — состояние применяется в момент запуска (push), а не поддерживается круглосуточно.

Слои и порты: Ansible живёт на L7 (SSH/WinRM — транспорт, YAML+модули — прикладной уровень). Отдельных портов у него нет: всё, что нужно открыть на узле, — 22 или 5985/5986.

```text
                    ┌──────────────────────────────────────────┐
                    │  UBU01  10.10.10.30  (control node)      │
                    │  ansible-core, ansible-lint, molecule    │
                    │  ~/vektor-ansible/                       │
                    └───────────────┬──────────────────────────┘
        SSH:22                  SSH:22│                    WinRM:5985 (лаб!)
   ┌────────────┬─────────────┬───────┴──────┐            ┌──────────────┐
   │ ASTRA01    │ MON01       │ GW01         │            │ FS01         │
   │ .31        │ .40         │ .3 / .20.1   │            │ .20          │
   └────────────┴─────────────┴──────┬───────┘            └──────────────┘
                                    │ SSH ProxyJump через GW01
                              ┌─────┴──────┐
                              │ WEB01      │  DMZ 10.10.20.10
                              └────────────┘
```

**Ключевые сущности.** *Inventory* — список хостов и групп (статические INI/YAML-файлы или динамический плагин, который тянет хосты из VMware/vCenter/облака). *Playbook* — YAML-список плейов: «на каких хостах что сделать». *Module* (`ansible.builtin.template`, `ansible.posix.authorized_key`) — атомарная операция, почти всегда идемпотентная: повторный запуск возвращает `ok`, а не `changed`. *Role* — переиспользуемый пакет задач с переменными, шаблонами, хендлерами и метаданными. *Collection* — упаковка ролей и модулей с неймспейсом (`community.general.timezone`): с ansible-core 2.10 модули вне «базы» живут только в коллекциях, и FQCN-имена (`namespace.collection.module`) обязательны.

**Иерархия переменных** (упрощённо, от слабых к сильным): role defaults → inventory group_vars/all → group_vars/<group> → host_vars/<host> → vars в плейбуке → `--extra-vars` (побеждает всё). 90% «почему подставилось не то» — это неверное понимание этой лестницы.

**Порядок поиска конфига:** `ANSIBLE_CONFIG` → `./ansible.cfg` → `~/.ansible.cfg` → `/etc/ansible/ansible.cfg`. Первый найденный выигрывает — берётся ровно один.

**Vault** — штатное шифрование Ansible: файл или отдельное значение шифруется AES256, в начале файла появляется маркер `$ANSIBLE_VAULT;1.1;AES256`. Ключ (пароль vault) в репозитории не лежит: он вводится интерактивно, берётся из файла с правами `0600` или из внешнего хранилища. `--vault-id dev@prompt` позволяет держать разные ключи для dev/prod.

**Тестирование.** `ansible-lint` ловит антипаттерны (shell вместо модуля, отсутствие `mode`, `latest` в пакетах), `yamllint` — синтаксис YAML. *Molecule* — фреймворк для проверки ролей в изолированном окружении: создаёт контейнер/ВМ, применяет роль (converge), проверяет идемпотентность (второй прогон должен дать `changed=0`), запускает верификатор (Testinfra/Ansible verify) и уничтожает окружение. Драйверы: `docker`, `podman`, `delegated` (реальный хост). Молекула — это unit-тест роли; полноценный integration-тест — прогон на стенде.

## 3. Аналогия из жизни

Ansible — это бригадир с чек-листом, который приезжает на объект. У него есть список адресов (inventory), папка с инструкциями (playbook), набор инструментов под каждую операцию (модули), толстая книга типовых работ «как обустроить бытовку» (роль) и сейф с кодами от щитков (Vault). Бригадир не сидит на объекте постоянно — он приехал, сверил по чек-листу: свет есть, розетки стоят, дверь висит — ставит галочки и уезжает. Приезжает через месяц — всё ещё висит, значит ничего не трогает (это и есть идемпотентность: `changed=0`). Перед выездом на реальный объект он репетирует на макете в подсобке (Molecule), а на объект с сотней квартир заходит по подъездам, а не во все сразу (`serial`).

Аналогия ломается на одном: бригадир не мешает жильцам переставлять мебель между визитами — а в ИТ именно это и происходит, поэтому «дрейф конфигурации» между прогонами Ansible не отслеживает, его надо ловить отдельно (`--check` по расписанию, мониторинг, CMDB).

## 4. Практика и команды

**1) Проверка окружения и конфига**
```bash
ansible --version
ansible-config dump --only-changed
```
`--only-changed` показывает только отличающиеся от дефолта параметры. Смотреть: `config file = /home/<you>/vektor-ansible/ansible.cfg` — если путь другой, ты работаешь не с тем репозиторием (самая частая причина «инвентарь не тот»). Версия core в выводе — сверяй с зафиксированной в `requirements.txt` `[ПРОВЕРИТЬ актуальную версию]`.

**2) Инвентарь: кто вообще есть**
```bash
ansible-inventory --graph
ansible-inventory --host web01
ansible-inventory --list -y
```
`--graph` — дерево групп; `--host` — все переменные конкретного хоста (тут видно, подхватился ли `group_vars/dmz.yml`); `--list -y` — полный инвентарь в YAML для грепа. Первое, что проверяют при «хост не найден»: `ansible-inventory --graph | grep web01`.

**3) Пинг и ad-hoc**
```bash
ansible all -m ansible.builtin.ping
ansible linux -m ansible.builtin.ping -u <linux-user> -k
ansible dmz -m ansible.builtin.command -a 'uptime' --limit web01
```
`-m` — модуль, `-a` — аргументы, `-u` — удалённый пользователь, `-k` — спросить SSH-пароль, `--limit` — сузить до одного хоста. Вывод `SUCCESS => {"ping": "pong"}` = доступ по SSH и Python в порядке. `UNREACHABLE` — сеть/ключи/порт, `FAILED` — дошёл, но упал (обычно sudo или отсутствующий модуль).

**4) Факты о хосте**
```bash
ansible astra01 -m ansible.builtin.setup -a 'filter=ansible_distribution*,ansible_python*'
```
`filter` экономит время: без него прилетит ~100 КБ JSON. Смотреть `ansible_distribution` (Astra/Debian/Ubuntu — от этого зависят имена пакетов) и `ansible_python_version` (если 3.9 на Astra — часть коллекций может ругаться).

**5) Роль: каркас**
```bash
cd ~/vektor-ansible
ansible-galaxy init roles/baseline
ansible-galaxy role install -r roles/requirements.yml -p roles
```
`init` создаёт стандартную структуру (`defaults/ vars/ tasks/ handlers/ templates/ files/ meta/ molecule/`). `role install -r` — установка внешних ролей строго по списку с версиями (`-p roles` — куда). Правило прода: роли из Galaxy ставятся только с пином версии.

**6) Коллекции**
```bash
ansible-galaxy collection install -r collections/requirements.yml -p ./collections
ansible-galaxy collection list
ansible-galaxy collection download -r collections/requirements.yml -p ./offline
```
`download` кладёт `.tar.gz` локально — это ваш способ жить в закрытом контуре без galaxy.ansible.com (реалии из РФ: доступ к внешним репозиториям может быть нестабильным или закрыт политикой — тогда возим архивом). `collection list` показывает реально установленные версии: расхождение с `requirements.yml` — источник «вчера работало».

**7) Vault**
```bash
ansible-vault create group_vars/all/vault.yml --vault-id dev@prompt
ansible-vault view   group_vars/all/vault.yml --vault-id dev@prompt
ansible-vault encrypt_string 'Lab#2026-vektor' --name 'vault_win_password' --vault-id dev@prompt
ansible-vault rekey  group_vars/all/vault.yml --vault-id dev@prompt --new-vault-id prod@prompt
```
`create/edit/view` — работа с зашифрованным файлом целиком; `encrypt_string` — одна переменная, которую можно вставить в обычный YAML; `rekey` — смена пароля vault (ротация). Всегда указывай `--vault-id`: это позволяет не смешивать ключи dev и prod. В `ansible.cfg` путь к паролю не прописываем — только через `--vault-id`/`--ask-vault-pass`, иначе секрет утечёт вместе с репо.

**8) Прогон плейбука: от синтаксиса к делу**
```bash
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --list-tasks --list-hosts
ansible-playbook site.yml --check --diff --limit dmz
ansible-playbook site.yml --tags time --limit web01 --vault-id dev@prompt
ansible-playbook site.yml --start-at-task 'Set timezone' -vvv
```
`--check` — сухой прогон (модули, поддерживающие check mode, покажут, что изменили бы), `--diff` — построчный дифф файлов; `--tags/--start-at-task` — точечный ремонт без полного прогона; `-vvv` — детальный лог подключения и модулей. В проде порядок: `--check --diff` → прогон на одном хосте → волнами.

**9) Линтеры**
```bash
ansible-lint site.yml
yamllint -c .yamllint .
```
`ansible-lint` по умолчанию использует профиль `production` `[ПРОВЕРИТЬ профиль в твоей версии]`. Читать вывод снизу вверх: сначала критичные правила (`no-changed-when`, `command-instead-of-module`, `risky-file-permissions`), потом стиль.

**10) Molecule**
```bash
python3 -m pip install --user molecule "molecule-plugins[podman]" ansible-lint
molecule init role vektor.baseline --driver-name podman
cd vektor.baseline && molecule test
molecule converge && molecule login && molecule destroy
```
`molecule test` — полный цикл (create → converge → idempotence → verify → destroy). `converge` — только применение роли (быстрый цикл разработки), `login` — зайти внутрь контейнера посмотреть, что получилось, `destroy` — прибрать. Драйвер `podman` не требует демона; `docker` — если он у тебя уже есть.

**11) Windows-узел**
```bash
ansible windows -m ansible.windows.win_ping -vvv
```
Требует `pip install pywinrm` и переменных подключения в `group_vars/windows.yml` (см. лабу). Ответ `pong` = WinRM, аутентификация и права в порядке.

**12) Документация по модулю**
```bash
ansible-doc community.general.timezone
ansible-doc -l | grep -i timezone
```
Быстрее, чем гуглить: показывает все параметры, `choices`, и пример блока YAML, который можно скопировать в задачу.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
Стенд L2 + PVE01 (трек «сисадмин»). Нужны: **UBU01** (10.10.10.30) — делаем его control node (на треке DevOps эту роль играл бы GIT01, у нас его нет); **ASTRA01** (10.10.10.31), **MON01** (10.10.10.40), **GW01** (10.10.10.3), **WEB01** (10.10.20.10) — managed nodes; **FS01** (10.10.10.20) — Windows-managed node (по желанию, шаг 10). Домен, учётки `adm.student` и пароль стенда `Lab#2026-vektor` уже есть. На Linux-хостах ты заходишь под своей локальной учёткой, созданной в предыдущих треках — ниже обозначена как `<linux-user>`; подставь свою. WEB01 доступен только из DMZ, поэтому ходим через GW01 как jump-host.

### Снапшот
В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot, имя `before-sysadmin-075` на **UBU01, ASTRA01, MON01, GW01, WEB01, FS01**. Плюс скопируй будущий репозиторий на хост (`\\vmware-host\Shared Folders\...` или общая папка), иначе откат снапшота UBU01 унесёт и его.

### Шаги

1. **UBU01: ставим инструменты.** `sudo apt update && sudo apt install -y python3-venv podman`, затем `python3 -m venv ~/venv-ansible`, `source ~/venv-ansible/bin/activate`, `pip install ansible-core ansible-lint molecule "molecule-plugins[podman]" pywinrm`. Ожидаемо: `ansible --version` печатает версию core и путь к python из venv. Активацию venv добавляем в `~/.bashrc`.

2. **UBU01: SSH-ключ и доступ.** `ssh-keygen -t ed25519 -C "ansible@vektor" -f ~/.ssh/ansible_ed25519 -N ""`, затем по каждому хосту `ssh-copy-id -i ~/.ssh/ansible_ed25519.pub <linux-user>@10.10.10.31` и т.д. Проверка: `ssh -i ~/.ssh/ansible_ed25519 <linux-user>@10.10.10.31 'hostname'` возвращает `astra01`. Для DMZ проверяем `ssh -J <linux-user>@10.10.10.3 -i ...`.

3. **UBU01: скелет репозитория.**
```bash
mkdir -p ~/vektor-ansible/{inventory/group_vars,inventory/host_vars,roles,collections,logs}
cd ~/vektor-ansible && git init
printf 'logs/\ncollections/\n*.retry\n.vault_pass*\n' > .gitignore
```
`ansible.cfg`:
```ini
[defaults]
inventory = ./inventory
roles_path = ./roles
collections_path = ./collections
host_key_checking = True
forks = 10
stdout_callback = yaml
log_path = ./logs/ansible.log
interpreter_python = auto_silent

[privilege_escalation]
become = True
become_method = sudo
become_ask_pass = False
```
`stdout_callback = yaml` требует `community.general` — если коллекции ещё нет, закомментируй строку. Ожидаемо: `ansible-config dump --only-changed` показывает твой `config file`.

4. **UBU01: инвентарь.** `inventory/hosts.ini`:
```ini
[linux]
astra01  ansible_host=10.10.10.31
mon01    ansible_host=10.10.10.40

[gateway]
gw01     ansible_host=10.10.10.3

[dmz]
web01    ansible_host=10.10.20.10

[windows]
fs01     ansible_host=10.10.10.20

[linux:children]
dmz

[all:vars]
ansible_user=<linux-user>
ansible_ssh_private_key_file=/home/<linux-user>/.ssh/ansible_ed25519

[dmz:vars]
ansible_ssh_common_args=-o ProxyJump=<linux-user>@10.10.10.3
```
`inventory/group_vars/all.yml`: `baseline_timezone: Europe/Moscow`, `baseline_packages: [chrony, vim, curl]`. Ожидаемо: `ansible-inventory --graph` рисует `dmz` внутри `linux`, а `ansible-inventory --host web01` отдаёт ProxyJump.

5. **UBU01: пинг всего парка.** `ansible linux -m ansible.builtin.ping` → пять `pong` (astra01, mon01, web01, gw01, fs01 не входит). Если web01 `UNREACHABLE` — сначала проверь руками `ssh -J`, потом `ansible web01 -m ping -vvv`.

6. **UBU01: роль baseline.** `ansible-galaxy init roles/baseline`. Заполняем:
```yaml
# roles/baseline/defaults/main.yml
baseline_timezone: Europe/Moscow
baseline_packages: [chrony, vim, curl]
baseline_motd_owner: "{{ ansible_facts['distribution'] }} {{ ansible_facts['distribution_version'] }}"

# roles/baseline/tasks/main.yml
---
- name: Install baseline packages
  ansible.builtin.package:
    name: "{{ baseline_packages }}"
    state: present
  tags: [packages]

- name: Set timezone
  community.general.timezone:
    name: "{{ baseline_timezone }}"
  notify: Restart chrony
  tags: [time]

- name: Deploy /etc/motd
  ansible.builtin.template:
    src: motd.j2
    dest: /etc/motd
    owner: root
    group: root
    mode: "0644"
  tags: [motd]

# roles/baseline/handlers/main.yml
---
- name: Restart chrony
  ansible.builtin.service:
    name: chrony
    state: restarted
```
`templates/motd.j2`: `{{ inventory_hostname }} | {{ baseline_motd_owner }} | {{ baseline_timezone }}`. В `meta/main.yml` — `galaxy_info` с автором и `min_ansible_version`.

7. **UBU01: плейбук `site.yml`.**
```yaml
---
- name: Baseline for Linux fleet
  hosts: linux
  become: true
  serial: 1
  max_fail_percentage: 0
  roles:
    - baseline
```
`serial: 1` — по одному хосту (в проде `serial: "25%"`), `max_fail_percentage: 0` — остановиться на первой ошибке. Прогон: `--syntax-check` → `--check --diff` → реальный. Ожидаемо: первый прогон `changed=3` на хост, второй — `changed=0`.

8. **UBU01: Vault.** `ansible-vault create group_vars/all/vault.yml --vault-id dev@prompt` → внутри `vault_lab_db_password: Lab#2026-vektor`. Добавляем в роль задачу с `no_log`:
```yaml
- name: Render lab secret file
  ansible.builtin.copy:
    content: "db_password={{ vault_lab_db_password }}\n"
    dest: /etc/vektor/lab-secret
    owner: root
    group: root
    mode: "0600"
  no_log: true
```
Запуск: `ansible-playbook site.yml --vault-id dev@prompt`. Ожидаемо: файл создан, в логе вместо значения — `censored`. Проверка на хосте: `sudo cat /etc/vektor/lab-secret` (только в лабе!).

9. **UBU01: коллекции.** `collections/requirements.yml`:
```yaml
collections:
  - name: community.general
    version: ">=8.0.0"
  - name: ansible.posix
    version: ">=1.5.0"
```
`ansible-galaxy collection install -r collections/requirements.yml -p ./collections`. Ожидаемо: `collection list` показывает обе версии; роль использует FQCN `community.general.timezone`, `ansible.builtin.copy`.

10. **UBU01 + FS01: Windows-узел (по желанию).** На FS01 в PowerShell (админ): `winrm quickconfig -q`, `Enable-PSRemoting -SkipNetworkProfileCheck -Force`, затем для лабы `winrm set winrm/config/service/auth '@{Basic="true"}'` и `winrm set winrm/config/service '@{AllowUnencrypted="true"}'`. `inventory/group_vars/windows.yml`:
```yaml
ansible_connection: winrm
ansible_port: 5985
ansible_winrm_transport: basic
ansible_winrm_scheme: http
ansible_user: 'VEKTOR\adm.student'
ansible_password: "{{ vault_win_password }}"
ansible_winrm_server_cert_validation: ignore
```
`vault_win_password` кладём в vault через `encrypt_string`. Проверка: `ansible windows -m ansible.windows.win_ping` → `pong`. **Это небезопасно и только для лабы:** basic + unencrypted по 5985 передаёт пароль почти открытым текстом; в работе — 5986/HTTPS + Kerberos/NTLM (`ansible_winrm_transport: kerberos`), либо `credssp` `[ПРОВЕРИТЬ настройку сертификата в вашей среде]`.

11. **UBU01: линтеры.** `ansible-lint site.yml roles/baseline` и `yamllint .`. Правим всё, что ругается на `risky-file-permissions`, `command-instead-of-module`, `no-changed-when`. Ожидаемо: `Passed: 0 failure(s)`.

12. **UBU01: Molecule.** `cd ~ && molecule init role vektor.baseline --driver-name podman`, переносим задачи/шаблоны из `roles/baseline`, в `molecule/default/molecule.yml` задаём образ `docker.io/library/ubuntu:24.04` и платформу. `molecule converge` → внутри контейнера `/etc/motd` содержит `Ubuntu`; `molecule test` проходит целиком, включая шаг `idempotence`. Если Astra-специфичные пакеты в контейнере недоступны — выноси их в отдельную переменную и в Molecule переопределяй `baseline_packages: [curl]`.

### Проверка
- [ ] `ansible-inventory --graph` показывает группы `linux`, `gateway`, `dmz`, `windows`, и `dmz` вложена в `linux`.
- [ ] `ansible linux -m ansible.builtin.ping` — все `pong` без ввода пароля.
- [ ] Второй прогон `site.yml` даёт `changed=0` на всех хостах.
- [ ] `date` и `timedatectl` на ASTRA01, MON01, WEB01 показывают `Europe/Moscow` и синхронизацию включённой.
- [ ] `/etc/motd` на каждом хосте содержит его hostname.
- [ ] `git status` не показывает `vault.yml` в открытом виде; в diff лежит `$ANSIBLE_VAULT;1.1;AES256`.
- [ ] На WEB01 `/etc/vektor/lab-secret` с правами `-rw------- root root`.
- [ ] `ansible-galaxy collection list` содержит `community.general` и `ansible.posix` с версиями.
- [ ] `ansible-lint` — без ошибок; `molecule test` — зелёный.
- [ ] В `stand-log.md` записаны изменения.

### Сломай и почини
**Пользователь:** дежурный по внутреннему порталу. **ВМ:** WEB01 (10.10.20.10). **Что написал:** «Сайт wiki открывается, но в логах nginx время на 3 часа назад, а uptime в мониторинге скачет. И ещё: после обеда по WEB01 не проходят твои автоматические проверки — Zabbix пишет „нет данных по SSH“». **Срочность:** средняя.

**Воспроизводим:**
1. На WEB01: `sudo timedatectl set-timezone UTC` и `sudo timedatectl set-ntp false`.
2. На WEB01: `chmod 700 ~` и `chmod 600 ~/.ssh` → `~/.ssh` без права входа для владельца ломает чтение ключа; либо проще — закомментируй строку ключа в `~/.ssh/authorized_keys`. Второй вариант чище: `sed -i 's/^ssh-ed25519/#ssh-ed25519/' ~/.ssh/authorized_keys`.
3. На GW01, чтобы усложнить: `sudo systemctl stop ssh` (DMZ-хост останется недоступен).

**Диагностика по порядку:**
1. С UBU01: `nc -zv 10.10.10.3 22` — GW01 отвечает? Если нет, проблема в jump-хосте, DMZ пока не трогаем.
2. `ssh -J <linux-user>@10.10.10.3 <linux-user>@10.10.20.10` — видим `Permission denied (publickey)` → ключ/права, а не Ansible.
3. `ansible web01 -m ansible.builtin.ping -vvv` — `UNREACHABLE` + строка `SSH: EXEC ssh -o ProxyJump=...` показывает, что именно ушло на провод.
4. Починили GW01 (`systemctl start ssh`) и ключ (`ssh-copy-id -i ~/.ssh/ansible_ed25519.pub ...`).
5. `ansible web01 -m ansible.builtin.ping` → `pong`. Теперь про время: `ansible web01 -m ansible.builtin.command -a 'timedatectl'` → видим `Time zone: UTC` и `NTP service: inactive`.
6. `ansible-playbook site.yml --check --diff --limit web01 --tags time` — check mode показывает, что роль хочет изменить (это и есть детект дрейфа).

**Решение:** `ansible-playbook site.yml --limit web01 --tags time,motd --vault-id dev@prompt`, затем контрольный прогон — `changed=0`.

**Ответ пользователю:** «На WEB01 был сбит часовой пояс и отключена синхронизация времени — вернул на Europe/Moscow, NTP включён. Причина: настройки на этом хосте правились вручную в обход конфигурации. Доступ по SSH восстановлен, мониторинг снова получает данные. Проверь, пожалуйста, что в логах время совпадает с московским.»

**Закрытие:** в заявке — причина (дрейф конфигурации + сломанный ключ), что сделано, ссылка на коммит с ролью; в `stand-log.md` — запись; если хост снова «уплывёт», следующий шаг — `--check` по расписанию в cron и алерт.

### Что изменилось на стенде
- **UBU01** — control node: venv `~/venv-ansible` (ansible-core, ansible-lint, molecule, molecule-plugins[podman], pywinrm), podman, репозиторий `~/vektor-ansible` (git), ключ `~/.ssh/ansible_ed25519`, лог `logs/ansible.log`.
- **ASTRA01, MON01, GW01, WEB01** — в `authorized_keys` добавлен публичный ключ с UBU01; установлены `chrony, vim, curl`; часовой пояс `Europe/Moscow`; `/etc/motd` управляется шаблоном; на WEB01 — `/etc/vektor/lab-secret` (0600).
- **FS01** — включён WinRM (5985, basic/unencrypted — только лаба), в инвентаре группа `windows`, доступ под `VEKTOR\adm.student`, пароль в Vault.
- **Репозиторий** — `inventory/hosts.ini`, `group_vars/all.yml`, `group_vars/windows.yml`, `group_vars/all/vault.yml` (зашифрован), роль `baseline`, `collections/requirements.yml`, `site.yml`, `ansible.cfg`, `.ansible-lint`, `.yamllint`.

### Откат
VMware Workstation → правый клик по ВМ → Snapshot Manager → выбрать `before-sysadmin-075` → **Revert To Snapshot** на UBU01, ASTRA01, MON01, GW01, WEB01, FS01. Помни: откат UBU01 удалит репозиторий и venv — заранее выгрузи `~/vektor-ansible` на хост (`scp -r` или общая папка), чтобы не собирать заново. После отката на FS01 WinRM снова выключен — шаг 10 придётся повторить в следующей лабе.

## 6. Ошибки новичков и безопасность

1. **Плейбук на 400 строк в одном `tasks/main.yml`** → через месяц его боятся править, а переиспользовать нельзя → разбивай на роли, файлы подключай через `import_tasks`, общее — в `defaults`.
2. **Пароли открытым текстом в `group_vars`** → попадают в git, в историю, в бэкапы, в логи CI → только Vault (или внешний секрет-стор), `no_log: true` на задачах с секретами, `.gitignore` для `*.vault_pass`.
3. **`ansible-playbook site.yml` без `--limit` и без `--check`** → одна опечатка в шаблоне кладёт nginx на всём парке → всегда `--check --diff` → один хост → `serial` волнами.
4. **`shell`/`command` вместо модуля** → нет идемпотентности, каждый прогон `changed`, а иногда и повторное применение → `ansible.builtin.copy/template/service/package`; если без `command` никак — `creates:` или `changed_when:`.
5. **`lineinfile` вместо `template`** → файл «дополняется» при каждом прогоне, конфиг дрейфует → целые файлы — шаблоном, точечно — `blockinfile` с маркерами.
6. **`ignore_errors: yes` везде** → падения скрыты, «зелёный» прогон врёт → игнорируй осознанно, с `failed_when`/`register` и явной проверкой.
7. **Незапиненные коллекции и роли** → обновился `community.general` — сломался плейбук → версии в `requirements.yml`, установка из локального артефакта, обновление как отдельное ревью.
8. **Правки прямо на проде «руками, потом закоммичу»** → репозиторий перестаёт быть источником правды → сначала PR, потом прогон; ручные правки фиксируются как инцидент.

**Безопасность.** Не ходи по SSH под root: отдельный пользователь + `become` с ограниченным sudoers. Не оставляй `host_key_checking = False` навсегда — это отключение проверки подлинности хоста и открытая дверь для MITM. Не храни vault-пароль рядом с репозиторием и не передавай его в `-J`/переменных окружения CI в открытом виде. В боевой среде WinRM basic + unencrypted на 5985 недопустим. Учитывай, что Vault шифрует данные, но не разграничивает доступ: кто получил пароль vault и репозиторий — видит все секреты, поэтому для продакшена смотрят в сторону внешних хранилищ (HashiCorp Vault, корпоративные PAM-решения). И помни про аудит: `log_path` + логи CI — единственный способ потом ответить «кто и когда менял конфиг на WEB01».

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**Вопрос 1. Чем роль отличается от плейбука и от коллекции? Где хранить переменные?**
Ответ: плейбук — сценарий «на каких хостах что сделать», роль — переиспользуемый пакет задач/шаблонов/хендлеров с дефолтами и метаданными, коллекция — упаковка ролей и модулей с неймспейсом и версией, ставится через `ansible-galaxy`. Переменные: дефолты роли (легко переопределяемые) в `defaults/main.yml`, «внутренние» константы — в `vars/main.yml`, данные по окружениям — в `group_vars`/`host_vars`, секреты — в Vault-файлах. Приоритет: defaults < group_vars < host_vars < vars плейбука < `--extra-vars`.
*Что хочет услышать интервьюер:* понимание, что `defaults` существуют именно для переопределения, и что секреты не лежат в `vars`.
*Красный флаг:* «всё в один playbook, переменные в `vars` — так проще».

**Вопрос 2. Как вы обеспечиваете идемпотентность и как отлаживаете плейбук, который «работает не так»?**
Ответ: идемпотентность — использовать декларативные модули (`template`, `service`, `package`), а не `shell`; для неизбежного `command` — `creates`/`removes`/`changed_when`. Проверка — повторный прогон должен давать `changed=0`. Отладка: `--syntax-check`, `--check --diff`, `--list-tasks`, `--start-at-task`, `--step`, `-vvv` для коннекта, `ansible-doc` для параметров, `ansible-lint` до прода, Molecule для самой роли.
*Что хочет услышать:* что `--check --diff` — ежедневный инструмент, а не экзотика, и что `changed=1` на втором прогоне — баг.
*Красный флаг:* «добавляю `ignore_errors` и смотрю, где упадёт».

**Вопрос 3. Как храните секреты и как их ротируете?**
Ответ: `ansible-vault` — файл или `encrypt_string`, разные `--vault-id` для dev/prod, пароль vault вне репозитория (файл 0600, интерактивный ввод, CI-переменная с маскированием), `no_log: true` на задачах с секретами, ротация — `ansible-vault rekey` для смены пароля vault и перевыпуск самих секретов. Для зрелой среды — внешний секрет-стор (HashiCorp Vault, корпоративный PAM) с короткоживущими токенами, Ansible только читает.
*Что хочет услышать:* что Vault — про «зашифровано в репозитории», а не про управление доступом, и что это разные задачи.
*Красный флаг:* «пароль vault лежит в репозитории рядом, чтобы CI не спрашивал».

**Вопрос 4. Как безопасно выкатить изменение на 50 серверов?**
Ответ: `--check --diff` на всём парке → прогон на канареечном хосте (`--limit`) → `serial: "10%"` (или по 1 для stateful), `max_fail_percentage`, `any_errors_fatal` по ситуации → мониторинг между волнами → окно и уведомление. Плюс теги для точечного ремонта, `--start-at-task` при разборе, снапшот/бэкап перед изменением, план отката (реверт-плейбук или снапшот ВМ) и запись в CMDB/журнал изменений.
*Что хочет услышать:* серийность, канарейка, наблюдаемость и заранее продуманный откат.
*Красный флаг:* «запущу на всех, посмотрим».

**Вопрос 5. Ansible без агента — в чём плюсы, минусы и когда он не подходит?**
Ответ: плюсы — нечего ставить на узлы, минимум требований (SSH + Python), низкий порог входа, хорошо ложится на сеть, сетевое оборудование и разовые миграции. Минусы — нет непрерывного enforcement (дрейф между прогонами), скорость на тысячах хостов падает (forks, SSH-накладные), сложнее с «постоянным состоянием» и с хостами без Python. Не подходит там, где нужен строгий непрерывный контроль и огромный парк — там смотрят на Puppet/Chef/Salt или на гибрид (Ansible для оркестрации, агент для compliance).
*Что хочет услышать:* честное «push-модель не равна управлению конфигурацией 24/7», без религии.
*Красный флаг:* «Ansible умеет всё, других инструментов не нужно».

## 8. Источники (официальная документация)

- **docs.ansible.com** — раздел «Ansible Documentation»: «Using variables» (приоритеты), «Roles», «Using collections», «Protecting sensitive data with Ansible Vault», «ansible-inventory», страницы модулей `ansible.builtin.*` и `community.general.*`. Поиск: `ansible docs vault encrypt_string`, `ansible variable precedence`.
- **docs.ansible.com** — «Ansible Configuration Settings» (все ключи `ansible.cfg`). Поиск: `ansible configuration settings defaults`.
- **galaxy.ansible.com** — каталог ролей и коллекций; смотреть версии и дату последнего релиза перед пином.
- **ansible-lint.readthedocs.io** — список правил и профилей, конфиг `.ansible-lint`. Поиск: `ansible-lint rules profiles`.
- **molecule.readthedocs.io** — «Getting started», раздел про драйверы и `molecule test`. Поиск: `molecule documentation drivers podman`.
- **learn.microsoft.com** — «Windows Remote Management», «Installation and Configuration for Windows Remote Management», документация по WinRM и `Enable-PSRemoting`. Поиск: `winrm quickconfig powershell remoting microsoft docs`.
- **ubuntu.com / astralinux.ru** — установка пакетов `ansible-core`, `podman` в целевых ОС; для Astra Linux смотреть локальную документацию по режимам защищённости `[ПРОВЕРИТЬ: влияние мандатного контроля доступа на пути /etc и systemd-юниты]`.
- **github.com/ansible/awx** — свободный upstream веб-интерфейса автоматизации (платная корпоративная версия — Red Hat Ansible Automation Platform) `[ПРОВЕРИТЬ актуальность релизов]`.
