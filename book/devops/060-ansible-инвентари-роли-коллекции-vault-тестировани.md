# Ansible: инвентари, роли, коллекции, Vault, тестирование (Molecule), AWX/Semaphore

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4471.** Пользователь: дежурный инженер (смотрит Zabbix на MON01). ВМ: WEB01, UBU01.
«После ручных правок на прошлой неделе на WEB01 и UBU01 разъехались таймзона, список пакетов и конфиг nginx. На UBU01 ты сам в ssh-сессии что-то поправил, на WEB01 — я. Теперь не помню, где какая версия. Надо привести к одному виду и сделать так, чтобы в следующий раз не руками. И пароль от basic-auth не храни в открытом виде в гитлабе, а то я видел.»
Срочность: средняя.

Это ровно та ситуация, ради которой существует Ansible. В реальной работе он встречается постоянно: настроить 5 серверов одинаково, раскатать конфиг nginx на все веб-хосты, обновить пакеты, создать пользователей, положить сертификат — и всё это воспроизводимо, из Git, с ревью и без «а я помню, что нажимал». В DevOps-командах Ansible — самый частый инструмент конфигурационного управления: он не требует агентов, работает поверх SSH и читается почти как обычный текст.

После этой главы ты должен уметь: написать инвентарь и объяснить, откуда Ansible берёт хосты; собрать роль с шаблонами и хендлерами; подключить коллекцию и зафиксировать её версию в `requirements.yml`; зашифровать секрет Vault'ом и не закоммитить пароль; прогнать роль через Molecule и ansible-lint; запустить плейбук из веб-интерфейса (Semaphore или AWX) и понимать, зачем он нужен, если есть CI.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ansible — **агентless**-система управления конфигурацией. У неё две стороны: **control node** (машина, где установлен `ansible-core`, у нас GIT01) и **managed nodes** (хосты, которые настраиваем). Никакого демона на целевых машинах нет: control node по SSH (порт 22) заходит на хост, копирует туда временный Python-скрипт-модуль, запускает его, забирает JSON-результат и удаляет. Отсюда требования к цели: SSH + Python 3. (Для Windows — WinRM, порты 5985/5986; на DevOps-треке Windows-часть стенда может отсутствовать, и это нормально.)

Ключевые понятия:

- **Inventory (инвентарь)** — список хостов и групп. Формат INI или YAML, плюс динамические инвентари-плагины (например, из облака или из GitLab).
- **Playbook** — YAML-файл, список *play*: «на группу `web` выполнить такие задачи».
- **Task** — одно действие, вызывает **модуль** (`ansible.builtin.apt`, `copy`, `template`, `service`…).
- **Модуль** — единица работы; правильно написанный модуль **идемпотентен**: если состояние уже такое, какое просят, он вернёт `ok`, а не `changed`.
- **Handler** — задача, которая выполняется один раз в конце play и только если её «дёрнули» через `notify`. Классика: перезагрузить nginx, если изменился конфиг.
- **Facts** — данные о хосте (`ansible_distribution`, `ansible_default_ipv4`…), собираются в начале play, если не отключить `gather_facts`.
- **Роль (role)** — упаковка задач, шаблонов, переменных и хендлеров в стандартную структуру каталогов. Переиспользуется и тестируется.
- **Коллекция (collection)** — пакет из ролей, модулей и плагинов с версией и namespace (`community.general`, `ansible.posix`). Современный способ распространять Ansible-код; ставится через `ansible-galaxy`.
- **Ansible Vault** — шифрование файлов и отдельных значений паролем. Внутри AES-256, ключ выводится из пароля (PBKDF2-HMAC-SHA256).
- **Molecule** — фреймворк для тестирования ролей: поднимает контейнер/ВМ, применяет роль, проверяет результат и **идемпотентность** (второй прогон не должен ничего менять).
- **AWX / Semaphore** — веб-интерфейсы и планировщик запусков плейбуков: кто, что, когда запустил, с каким инвентарём, с каким логом.

```text
        GIT01 (10.10.10.50) — control node
        ┌───────────────────────────────────────────────┐
        │ ansible-core (в venv)                         │
        │  ├─ ansible.cfg        настройки              │
        │  ├─ inventory.ini      КОГО настраиваем       │
        │  ├─ site.yml           ЧТО делаем             │
        │  ├─ roles/*            КАК делаем             │
        │  ├─ group_vars/*       данные  ← Vault        │
        │  └─ collections/       готовые модули         │
        └──────────────┬────────────────────────────────┘
                       │ SSH :22, push, без агента
        ┌──────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼              ▼
     UBU01          WEB01           MON01         k8s-*
   10.10.10.30   10.10.20.10     10.10.10.40
   python3       python3         python3
      │             │               │
   модуль →      модуль →        модуль →
   приводит      приводит        приводит
   к состоянию   к состоянию     к состоянию
```

Место в модели OSI: Ansible живёт на L7 (SSH — прикладной протокол поверх TCP/22). Он не «настраивает сеть», он запускает команды на хосте, а хост уже сам меняет что угодно.

Важная деталь про порядок: Ansible выполняет задачи **последовательно**, play за play, хост за хостом (параллельность регулируется `forks`). Внутри play все хосты идут «пачками» по `forks` штук.

## 3. Аналогия из жизни

Представь выездную бригаду мастеров, которая обслуживает офисы по одному и тому же регламенту. У бригадира есть **список адресов** (инвентарь: «офисы на Тверской — группа `web`, склад на Южной — группа `monitoring`»). Есть **чек-лист на выезд** (playbook): «проверить, что стоит нужный софт; положить инструкцию на стенд; если инструкция изменилась — перезагрузить стойку». Есть **папка с типовыми регламентами по видам работ** (роли): «электрика», «вентиляция» — их можно переиспользовать в других офисах. Есть **склад готовых инструментов от поставщика** (коллекции): не точить напильник самому, а взять стандартный. Есть **сейф с ключами** (Vault): ключи лежат в запертом ящике, а не на гвоздике у входа. Есть **учебный плац** (Molecule): прежде чем ехать в офис, бригада репетирует на макете и проверяет, что при повторном выезде ничего делать уже не надо. И есть **диспетчерская** (AWX/Semaphore), которая выдаёт наряды на выезд, пишет, кто когда выезжал, и не даёт запустить регламент «просто из терминала в пятницу вечером».

Где аналогия ломается: у мастеров есть память и они помнят, что делали в прошлый раз. Ansible памяти не имеет — он каждый раз заново сверяет фактическое состояние с желаемым, и именно поэтому ему нужно, чтобы регламент был написан в терминах состояния, а не «сделай шаг 1, шаг 2».

## 4. Практика и команды

Все команды — на **GIT01** (control node), пользователь `adm.student`.

**1. Установка и проверка версии.** На Ubuntu 24.04 системный pip заблокирован (PEP 668), поэтому ставим в venv или pipx.

```bash
sudo apt update && sudo apt install -y python3-venv sshpass
python3 -m venv ~/venv-ansible
source ~/venv-ansible/bin/activate
pip install --upgrade pip
pip install ansible-core ansible-lint
ansible --version
```

`ansible --version` печатает версию, путь к `ansible.cfg` (важно: какой конфиг реально подхватился), версию Python и список путей к коллекциям. Отдельно `ansible-core` — это движок; пакет `ansible` (community bundle) тянет ещё десятки коллекций. Для работы обычно достаточно `ansible-core` + явно указанные коллекции.

**2. Инвентарь и `ansible-inventory`.** Файл `inventory.ini`:

```ini
[app]
UBU01 ansible_host=10.10.10.30

[web]
WEB01 ansible_host=10.10.20.10

[monitoring]
MON01 ansible_host=10.10.10.40

[linux:children]
app
web
monitoring

[linux:vars]
ansible_user=adm.student
ansible_ssh_private_key_file=~/.ssh/id_ed25519_ansible
```

```bash
ansible-inventory -i inventory.ini --graph
ansible-inventory -i inventory.ini --host WEB01
ansible-inventory -i inventory.ini --list > /tmp/inv.json
```

`--graph` рисует дерево групп и детей, `--host` показывает **итоговые** переменные хоста после слияния всех источников (это спасает при отладке приоритетов), `--list` — полный JSON.

**3. Проверка связи модулем ping.** Это не ICMP, а «дотянулся ли Ansible и смог ли запустить Python-модуль».

```bash
ansible linux -i inventory.ini -m ansible.builtin.ping
```

Ожидаемо: `WEB01 | SUCCESS => {"changed": false, "ping": "pong"}`. `changed: false` — признак идемпотентности.

**4. Ad-hoc команды.** Быстрый одноразовый запуск модуля без плейбука.

```bash
ansible web -i inventory.ini -m ansible.builtin.apt -a "name=nginx state=present update_cache=true" -b
ansible linux -i inventory.ini -m ansible.builtin.command -a "uptime"
ansible linux -i inventory.ini -m ansible.builtin.setup -a "filter=ansible_distribution*"
```

`-b` — become (sudo), `-m` — модуль, `-a` — аргументы. `command` и `shell` **не идемпотентны** — используем их только для чтения, а не для изменений.

**5. Справка по модулям.**

```bash
ansible-doc -l | grep -i apt
ansible-doc ansible.builtin.template
ansible-doc ansible.builtin.apt -s     # короткая выжимка с примерами
```

**6. `ansible.cfg` — чтобы не таскать флаги руками.** В корне проекта:

```ini
[defaults]
inventory = ./inventory.ini
roles_path = ./roles
collections_path = ./collections
forks = 10
interpreter_python = auto_silent
host_key_checking = True
vault_password_file = ~/.ansible/vault-pass
stdout_callback = yaml

[privilege_escalation]
become = True
become_method = sudo
become_user = root
becoming_ask_pass = False
```

`vault_password_file` — **только для стенда**; в реальной работе так не делают (см. раздел 6). `stdout_callback = yaml` требует коллекции `community.general` — если её нет, будет ошибка, поэтому и держим `requirements.yml` (см. п. 10).

**7. Структура роли.** Скелет создаём командой, а не руками:

```bash
cd ~ && ansible-galaxy role init --role-skeleton nginx roles/nginx 2>/dev/null || ansible-galaxy role init roles/nginx
tree roles/nginx -L 2
```

```text
roles/nginx/
├── defaults/main.yml      # значения по умолчанию (самый низкий приоритет)
├── handlers/main.yml      # reload/restart
├── tasks/main.yml         # что делаем
├── templates/vektor.conf.j2
├── vars/main.yml          # «жёсткие» переменные роли
└── meta/main.yml          # зависимости и метаданные
```

```yaml
# roles/nginx/tasks/main.yml
---
- name: Установить nginx
  ansible.builtin.apt:
    name: nginx
    state: present
    update_cache: true
    cache_valid_time: 3600

- name: Положить конфиг сайта из шаблона
  ansible.builtin.template:
    src: vektor.conf.j2
    dest: /etc/nginx/sites-available/vektor.conf
    owner: root
    group: root
    mode: "0644"
  notify: Reload nginx

- name: Включить сайт симлинком
  ansible.builtin.file:
    src: /etc/nginx/sites-available/vektor.conf
    dest: /etc/nginx/sites-enabled/vektor.conf
    state: link
  notify: Reload nginx

- name: Убрать дефолтный сайт
  ansible.builtin.file:
    path: /etc/nginx/sites-enabled/default
    state: absent
  notify: Reload nginx
```

```yaml
# roles/nginx/handlers/main.yml
---
- name: Reload nginx
  ansible.builtin.service:
    name: nginx
    state: reloaded
```

```yaml
# roles/nginx/defaults/main.yml
---
nginx_port: 80
nginx_server_name: web01.corp.vektor.lab
```

**8. Запуск плейбука: сначала холостой прогон.**

```bash
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --check --diff
ansible-playbook site.yml --diff
ansible-playbook site.yml --limit WEB01 -t nginx -vvv
```

`--check` — режим «покажи, что изменил бы» (не все модули его поддерживают), `--diff` — показать разницу в файлах, `--limit` — сузить до одного хоста, `-t`/`--tags` — только помеченные задачи, `-vvv` — подробный лог с реальными SSH-командами. Итоговая строка `PLAY RECAP` — главное, что смотрим: `ok`, `changed`, `unreachable`, `failed` по каждому хосту. Повторный прогон должен дать `changed=0`.

**9. Vault.**

```bash
mkdir -p ~/.ansible && chmod 700 ~/.ansible
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > ~/.ansible/vault-pass
chmod 600 ~/.ansible/vault-pass

ansible-vault encrypt_string --name 'vault_demo_token' 's3cr3t-lab-token' > group_vars/all/vault.yml
ansible-vault view group_vars/all/vault.yml
ansible-vault edit group_vars/all/vault.yml
```

В файле окажется конструкция вида:

```yaml
vault_demo_token: !vault |
  $ANSIBLE_VAULT;1.1;AES256
  6137353934...
```

В задаче секрет используется как обычная переменная: `{{ vault_demo_token }}`. При запуске: `--vault-password-file ~/.ansible/vault-pass` или интерактивно `--ask-vault-pass`. `ansible-vault rekey` меняет пароль, `ansible-vault decrypt` снимает шифрование (не коммить результат!).

**10. Коллекции.**

```bash
cat > requirements.yml <<'EOF'
---
collections:
  - name: community.general
    version: ">=9.0.0"
  - name: ansible.posix
    version: ">=1.5.4"
EOF
ansible-galaxy collection install -r requirements.yml -p ./collections
ansible-galaxy collection list
```

Фиксация версии в `requirements.yml` — это ваша защита от «вчера работало, сегодня сломалось после обновления коллекции». В `ansible.cfg` уже прописан `collections_path = ./collections`, поэтому проект самодостаточен.

**11. Линт и Molecule.**

```bash
ansible-lint site.yml roles/nginx
cd roles/nginx && molecule init scenario --driver-name docker
molecule converge      # поднять контейнер и применить роль
molecule verify        # прогнать тесты (verify.yml)
molecule test          # полный цикл, включая проверку идемпотентности
molecule destroy
```

`molecule test` по умолчанию гоняет роль дважды: второй прогон обязан дать `changed=0`, иначе Molecule падает на шаге `idempotence`. Это и есть автоматическая проверка того, что роль написана правильно.

**12. Запуск из веб-интерфейса.** Semaphore поднимается в Docker на GIT01:

```yaml
# ~/semaphore/docker-compose.yml
services:
  semaphore:
    image: semaphoreui/semaphore:latest
    ports:
      - "3000:3000"
    environment:
      SEMAPHORE_DB_DIALECT: bolt
      SEMAPHORE_ADMIN: admin
      SEMAPHORE_ADMIN_NAME: admin
      SEMAPHORE_ADMIN_EMAIL: admin@corp.vektor.lab
      SEMAPHORE_ADMIN_PASSWORD: "Lab#2026-vektor"
      SEMAPHORE_ACCESS_KEY_ENCRYPTION: "CHANGE_ME_RANDOM_32_BYTES"
    volumes:
      - semaphore_data:/var/lib/semaphore
volumes:
  semaphore_data:
```

```bash
docker compose up -d && docker compose logs -f semaphore
```

Интерфейс на `http://10.10.10.50:3000` [ПРОВЕРИТЬ имена переменных окружения по актуальной документации Semaphore]. Дальше в UI: Project → Key (SSH-ключ) → Repository (ваш GitLab) → Inventory → Environment → Task Template, и кнопка Run. AWX — это upstream Red Hat Ansible Automation Platform, ставится оператором в Kubernetes; на стенде это делается на кластере `k8s-cp01`, требует заметно больше ресурсов и отдельной главы.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GIT01** (10.10.10.50, Ubuntu Server, GitLab CE + runner) — отсюда работаем, здесь control node. Есть Docker (нужен для Molecule и Semaphore).
- **UBU01** (10.10.10.30), **WEB01** (10.10.20.10, nginx из темы L2), **MON01** (10.10.10.40) — доступны по SSH.
- На всех Linux-хостах есть пользователь `adm.student` с sudo. Если на каком-то хосте его нет — создайте на месте: `sudo adduser adm.student && sudo usermod -aG sudo adm.student`.
- Windows-часть стенда (DC01, FS01, WIN11-CL01) для этой лабораторной **не нужна** — Ansible здесь работает только с Linux.
- В GitLab есть проект `ansible-vektor` (или создайте пустой).

### Снапшот

В VMware Workstation: для каждой затрагиваемой ВМ — `VM → Snapshot → Take Snapshot`, имя `before-devops-060`.

Машины: **GIT01, UBU01, WEB01, MON01**. Можно из терминала хоста, если установлен `vmrun` [ПРОВЕРИТЬ путь к vmrun в вашей версии Workstation]:

```bash
vmrun -T ws snapshot "/path/to/GIT01/GIT01.vmx" before-devops-060
```

### Шаги

1. **GIT01.** Проверяем, что Ansible не стоит «случайный», и ставим в venv (команды из п. 4.1).
   *Ожидаемо:* `ansible --version` печатает версию и путь к конфигу `~/ansible-vektor/ansible.cfg` после того, как мы его создадим; пока — глобальный.

2. **GIT01.** Готовим SSH-ключ и раскладываем его на хосты.
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_ansible -N "" -C "ansible@git01"
   for h in 10.10.10.30 10.10.20.10 10.10.10.40; do ssh-copy-id -i ~/.ssh/id_ed25519_ansible.pub adm.student@$h; done
   ```
   *Ожидаемо:* `ssh -i ~/.ssh/id_ed25519_ansible adm.student@10.10.20.10` заходит без пароля.
   *Безопасность:* ключ без пароля — только для стенда; в работе ключ с passphrase + ssh-agent.

3. **GIT01.** Создаём проект.
   ```bash
   mkdir -p ~/ansible-vektor && cd ~/ansible-vektor && git init
   mkdir -p group_vars/all group_vars/web group_vars/linux roles
   ```
   Создаём `ansible.cfg` (п. 4.6), `inventory.ini` (п. 4.2), `group_vars/linux/main.yml`:
   ```yaml
   ---
   vektor_timezone: Europe/Moscow
   vektor_base_packages:
     - curl
     - htop
     - vim
   ```
   *Ожидаемо:* `ansible-inventory --graph` рисует три группы и `linux` как родителя.

4. **GIT01.** Проверяем связь.
   ```bash
   ansible linux -m ansible.builtin.ping
   ansible linux -m ansible.builtin.setup -a "filter=ansible_distribution*"
   ```
   *Ожидаемо:* три `SUCCESS`/`pong`, факты показывают `Ubuntu` и версию.

5. **GIT01.** Создаём роль `common`.
   ```bash
   ansible-galaxy role init roles/common
   ```
   В `roles/common/tasks/main.yml` — три задачи: `ansible.builtin.timezone` (name `{{ vektor_timezone }}`), `ansible.builtin.apt` для `vektor_base_packages`, `ansible.builtin.copy` с `content: "token={{ vault_demo_token }}\n"`, `dest: /etc/vektor/token`, `mode: "0600"`, `no_log: true`.
   *Ожидаемо:* файл появился, в логе нет значения токена.

6. **GIT01.** Шифруем секрет.
   ```bash
   ansible-vault encrypt_string --name 'vault_demo_token' 's3cr3t-lab-token' > group_vars/all/vault.yml
   echo "group_vars/all/vault.yml" >> .gitignore   # НЕ ДЕЛАЙТЕ ТАК В РАБОТЕ
   ```
   На самом деле `vault.yml` как раз **коммитят** — он зашифрован. В `.gitignore` должен быть только файл с паролем. Уберите строку, добавьте `vault-pass*`.
   *Ожидаемо:* `ansible-vault view group_vars/all/vault.yml` спрашивает пароль и показывает токен.

7. **GIT01.** Роль `nginx` + `site.yml`.
   ```yaml
   # site.yml
   ---
   - name: Базовая настройка Linux-хостов
     hosts: linux
     become: true
     roles:
       - common

   - name: Веб-серверы
     hosts: web
     become: true
     roles:
       - nginx
   ```
   Шаблон `roles/nginx/templates/vektor.conf.j2`:
   ```jinja2
   server {
       listen {{ nginx_port }};
       server_name {{ nginx_server_name }};
       root /var/www/vektor;
       index index.html;
       location / { try_files $uri $uri/ =404; }
   }
   ```
   *Ожидаемо:* `ansible-playbook site.yml --syntax-check` проходит без ошибок.

8. **GIT01.** Холостой прогон и реальный.
   ```bash
   ansible-playbook site.yml --check --diff
   ansible-playbook site.yml --diff
   ansible-playbook site.yml --diff
   ```
   *Ожидаемо:* первый прогон — куча `changed`, второй — `changed=0`. Если второй прогон что-то меняет, роль не идемпотентна — ищите `command`/`shell` или `state: latest`.

9. **GIT01.** Проверяем результат на WEB01.
   ```bash
   curl -sI http://10.10.20.10/ | head -5
   ansible web -m ansible.builtin.command -a "nginx -T" | head -30
   ansible linux -m ansible.builtin.command -a "cat /etc/vektor/token" -b
   ```
   *Ожидаемо:* HTTP 200 или 403 (каталога `/var/www/vektor` может не быть — это нормально, конфиг применён), в `nginx -T` фигурирует `listen 80`, токен на месте.

10. **GIT01.** Коллекции.
    ```bash
    ansible-galaxy collection install -r requirements.yml -p ./collections
    ansible-galaxy collection list | grep -E "community.general|ansible.posix"
    ```
    *Ожидаемо:* коллекции видны в `./collections/ansible_collections/`, повторный `ansible-playbook site.yml` не сломался.

11. **GIT01.** Линт и Molecule для роли `nginx`.
    ```bash
    ansible-lint roles/nginx
    cd roles/nginx && molecule init scenario --driver-name docker
    molecule test
    ```
    В `molecule/default/molecule.yml` укажите образ Ubuntu с systemd, например `geerlingguy/docker-ubuntu2404-ansible` [ПРОВЕРИТЬ актуальное имя образа], `privileged: true`, `command: /lib/systemd/systemd`.
    *Ожидаемо:* `molecule test` доходит до конца; шаг `idempotence` зелёный.

12. **GIT01.** Коммит и пуш.
    ```bash
    git add . && git commit -m "Ansible: common + nginx roles, vault, molecule" && git push -u origin main
    ```
    *Ожидаемо:* в GitLab виден проект, `vault.yml` в репозитории зашифрован, пароля в истории нет (`git log -p | grep -i "s3cr3t"` пусто).

13. **GIT01.** Semaphore.
    ```bash
    mkdir -p ~/semaphore && cd ~/semaphore
    # docker-compose.yml из п. 4.12
    docker compose up -d
    ```
    В UI: Project `vektor` → Key (приватный `id_ed25519_ansible`) → Repository (URL GitLab, ветка `main`) → Inventory (`inventory.ini`) → Task Template (`site.yml`) → Run.
    *Ожидаемо:* задача завершается `Success`, лог виден в интерфейсе.

### Проверка

- [ ] `ansible-inventory --graph` показывает группы `app`, `web`, `monitoring` и родителя `linux`.
- [ ] `ansible linux -m ping` — три `pong`.
- [ ] `ansible-playbook site.yml` второй раз даёт `changed=0` на всех хостах.
- [ ] На WEB01 в `/etc/nginx/sites-enabled/` нет `default`, есть `vektor.conf`.
- [ ] `curl -I http://10.10.20.10/` отдаёт HTTP-ответ (200/403), а не `connection refused`.
- [ ] `/etc/vektor/token` существует с правами `0600` и содержит токен.
- [ ] `ansible-vault view group_vars/all/vault.yml` требует пароль и показывает секрет.
- [ ] `grep -r "s3cr3t" .` в репозитории ничего не находит.
- [ ] `ansible-lint site.yml` без ошибок уровня `error`.
- [ ] `molecule test` в роли `nginx` — зелёный.
- [ ] Semaphore открывается на `http://10.10.10.50:3000`, задача из UI отрабатывает.

### Сломай и почини

> **Пользователь:** дежурный инженер (Zabbix на MON01)
> **ВМ:** WEB01
> **Что написал:** «После ночного прогона плейбука nginx на WEB01 отвечает только на 8080, на 80 — connection refused. Мониторинг сыпет алертами. На UBU01 всё нормально.»
> **Срочность:** средняя (стенд, не прод)

**Воспроизводим поломку** (на GIT01):

```bash
echo 'nginx_port: 8080' > group_vars/web/main.yml
ansible-playbook site.yml --limit WEB01 --diff
curl -sI http://10.10.20.10/ ; curl -sI http://10.10.20.10:8080/
```

**Диагностика по шагам:**

1. Сначала факт, а не догадка: `curl -I` на 80 и на 8080. Подтверждаем, что 80 закрыт, 8080 открыт.
2. Что слушает процесс: `ansible web -m ansible.builtin.command -a "ss -tlnp" -b`. Видим `0.0.0.0:8080`.
3. Что реально в конфиге: `ansible web -m ansible.builtin.command -a "nginx -T"`. Видим `listen 8080` — конфиг отрендерился не тот.
4. Откуда взялось значение: `ansible-inventory --host WEB01` и `ansible web -m ansible.builtin.debug -a "var=nginx_port"`. Показывает `8080`.
5. Приоритет переменных: в `roles/nginx/defaults/main.yml` было `80`, в `group_vars/web/main.yml` — `8080`. `group_vars` **перебивает** `defaults`. Роль не виновата — виноваты данные.
6. Кто и когда поменял: `git log -p group_vars/web/main.yml`. Находим коммит «временно, для теста».

**Решение:**

```bash
git revert <commit>        # или правим nginx_port обратно на 80
ansible-playbook site.yml --limit WEB01 --diff
curl -sI http://10.10.20.10/
ansible web -m ansible.builtin.command -a "ss -tlnp" -b | grep ':80'
```

Хендлер `Reload nginx` сработает автоматически, потому что шаблон изменился. Если по какой-то причине не сработал — `ansible web -m ansible.builtin.service -a "name=nginx state=restarted" -b`, но это уже признак того, что `notify` написан неправильно.

**Ответ пользователю:** «Причина — переопределение `nginx_port` в `group_vars/web/main.yml`, добавленное тестовым коммитом. Значение откатили, конфиг приведён к 80, сервис перезагружен, порт 80 слушается, алерт должен закрыться. Добавили шаг `--check --diff` в пайплайн перед применением, чтобы такое ловилось на ревью.»

**Закрытие заявки:** фиксируем в `stand-log.md`, коммитим фикс, прикладываем ссылку на коммит и вывод `curl -I` в тикет.

### Что изменилось на стенде

- **GIT01:** установлен `ansible-core` в `~/venv-ansible`, создан проект `~/ansible-vektor` (роли `common`, `nginx`, `site.yml`, `inventory.ini`, `ansible.cfg`, `requirements.yml`), репозиторий запушен в GitLab, SSH-ключ `~/.ssh/id_ed25519_ansible` разложен на UBU01, WEB01, MON01, файл пароля Vault `~/.ansible/vault-pass` (chmod 600), развёрнут Semaphore в Docker на порту 3000.
- **UBU01:** включена таймзона Europe/Moscow, установлены `curl`, `htop`, `vim`, создан `/etc/vektor/token` (0600).
- **WEB01:** конфиг `/etc/nginx/sites-available/vektor.conf` управляется ролью, дефолтный сайт отключён, симлинк в `sites-enabled`, таймзона и пакеты — как на UBU01.
- **MON01:** применена роль `common` (таймзона, пакеты, токен).
- **Учётки/адреса:** без изменений; используются существующие `adm.student` и адреса стенда.

### Откат

VMware Workstation → правый клик по ВМ → `Snapshots` → выбрать `before-devops-060` → `Go To`. Для GIT01, UBU01, WEB01, MON01. Или `vmrun -T ws revertToSnapshot "/path/GIT01.vmx" before-devops-060` [ПРОВЕРИТЬ]. После отката удалите локальные ветки/коммиты, сделанные в ходе лабораторной, если они не нужны.

## 6. Ошибки новичков и безопасность

1. **Секреты в открытом виде в Git.** → Утечка, даже если коммит потом удалён (история остаётся, форки, кэши CI). → Только `ansible-vault` или внешний менеджер секретов; перед пушем — `git diff --cached | grep -i pass`.
2. **Пароль Vault в репозитории или в `ansible.cfg`.** → Шифрование превращается в декорацию. → Пароль вне Git: переменная CI (masked/protected), файл вне репозитория, в идеале — внешний Vault/`--vault-id` с разными паролями по окружениям.
3. **`shell`/`command` вместо модулей.** → Нет идемпотентности, `--check` не работает, каждый прогон `changed`. → `apt`, `copy`, `template`, `file`, `service`; `command` — только с `creates:`/`changed_when:`/`failed_when:` и только для чтения.
4. **`state: latest` везде.** → Инфраструктура обновляется в случайный момент, ломается совместимость. → `state: present` + управление версиями осознанно.
5. **Нет `notify` на конфиг.** → Файл на диске новый, сервис в памяти старый. Классический «у меня всё применилось, а не работает». → Хендлер на каждое изменение конфига.
6. **`ignore_errors: yes` «чтобы прошло».** → Плейбук зелёный, инфраструктура сломана. → Убирать причину, а не ошибку; `failed_when` с явным условием.
7. **Незафиксированные версии коллекций.** → `ansible-galaxy install community.general` без версии через месяц приносит breaking change. → `requirements.yml` с версиями + `--force-with-deps` в CI на зафиксированном наборе.
8. **Приоритет переменных наугад.** → Значение «не подхватилось» или «перебилось». → Знать порядок: `defaults` роли < `inventory group_vars` < `host_vars` < `vars` play < `extra vars` (`-e`). Проверять через `ansible-inventory --host`.

**Безопасность отдельно.** В боевой среде нельзя: запускать плейбуки от `root` по SSH с паролем (только ключи + `become`, желательно с ограниченным sudoers); держать один Vault-пароль на dev и prod (компрометация dev = компрометация prod); давать CI-раннеру доступ ко всем хостам сразу (принцип наименьших привилегий: отдельные инвентари и отдельные ключи по окружениям); запускать `ansible-playbook` с рабочей машины разработчика в прод (только через пайплайн или AWX/Semaphore с RBAC и аудитом); хранить `vault_password_file` в `ansible.cfg` с правами 644.

## 7. Вопросы на собеседовании

**1. Чем Ansible отличается от Puppet/Chef и почему его так любят?**
Ответ: Ansible — agentless и push-модель: control node по SSH заходит на хост и запускает модуль, на целевой машине не нужен постоянный демон и не нужен открытый входящий порт, кроме SSH. Puppet/Chef — pull-модель с агентом, который периодически сам забирает каталог и применяет его. Плюсы Ansible: низкий порог входа (YAML), быстрый старт, не нужно ставить софт на хосты, удобно для разовых и «полу-разовых» задач. Минусы: производительность на тысячах хостов, отсутствие непрерывной сходимости (дрейф появится, если не запускать playbook по расписанию), состояние не хранится.
*Что хочет услышать интервьюер:* понимание push/pull и agentless, а не заученный список плюсов.
*Красный флаг:* «Ansible лучше, потому что бесплатный» — без понимания модели.

**2. Как Ansible обеспечивает идемпотентность и что её ломает?**
Ответ: идемпотентность обеспечивают модули: они описывают желаемое состояние (`state: present`, `mode: "0644"`) и сами проверяют текущее, возвращая `ok`, если менять нечего, и `changed`, если меняли. Ломают её `command`/`shell` без `creates`/`changed_when`, `state: latest`, самописные скрипты, `lineinfile` без корректного `regexp`, задачи, которые всегда пишут файл с таймстампом. Проверяется вторым прогоном (`changed=0`), `--check` и шагом `idempotence` в Molecule.
*Что хочет услышать интервьюер:* что идемпотентность — свойство модуля и данных, а не «Ansible такой».
*Красный флаг:* «Ansible всегда идемпотентен».

**3. Где хранить секреты в Ansible?**
Ответ: базовый вариант — Ansible Vault: файл/значение шифруется AES-256, пароль вне репозитория, отдельные `--vault-id` для dev/stage/prod. Лучше — внешний менеджер секретов (HashiCorp Vault, облачный секрет-сервис) и lookup-плагин: тогда секрет не лежит в Git вовсе, есть ротация и аудит доступа. Промежуточный вариант — CI-переменные (masked, protected) и сборка `vault.yml` на этапе пайплайна.
*Что хочет услышать интервьюер:* понимание, что Vault в Git — компромисс, а не идеал; про ротацию и разделение окружений.
*Красный флаг:* «Пароли в group_vars, репозиторий приватный».

**4. Как вы тестируете роль перед тем, как пустить её в прод?**
Ответ: `ansible-lint` на стиль и типовые ошибки; `ansible-playbook --syntax-check`; `--check --diff` на тестовом стенде; Molecule — поднять контейнер с целевой ОС, применить роль (`converge`), проверить результат (`verify`, например `ansible.builtin.assert` на порт и конфиг), обязательно проверить идемпотентность вторым прогоном; далее — прогон на stage-окружении и только потом prod, всё из CI по коммиту/тегу.
*Что хочет услышать интервьюер:* что тест включает идемпотентность и что promotion идёт по окружениям.
*Красный флаг:* «Проверяю руками на проде, там же быстрее».

**5. Зачем AWX/Semaphore, если есть CI и `ansible-playbook`?**
Ответ: CI хорош для «событие в Git → прогон». AWX/Semaphore дают RBAC (кто имеет право запускать что и на каком инвентаре), аудит (кто, когда, с какими extra vars), централизованные креденшелы (никто не видит ключ, он хранится в системе), расписания, ручной запуск «нажать кнопку» для дежурного, ограничение одновременных запусков и историю логов. Часто их используют вместе: CI собирает и линтит, AWX/Semaphore применяет в прод.
*Что хочет услышать интервьюер:* разделение ответственности «сборка vs применение с правами и аудитом».
*Красный флаг:* «Это просто красивая морда, не нужна».

## 8. Источники (официальная документация)

- **docs.ansible.com** — основной сайт документации Ansible. Разделы: «Ansible playbooks», «Using variables / Variable precedence», «Roles», «Collections», «Encrypting content with Ansible Vault», «Working with inventory». Поиск: `ansible variable precedence`, `ansible vault encrypt_string`.
- **galaxy.ansible.com** — коллекции и роли, версии и зависимости. Поиск: `community.general`, `ansible.posix`.
- **ansible.readthedocs.io** — документация Molecule. Раздел «Getting started», «Configuration». Поиск: `molecule test idempotence`.
- **ansible-lint.readthedocs.io** — правила линтера. Поиск: `ansible-lint rules`.
- **github.com/ansible/awx** — исходники AWX, раздел про AWX Operator и установку в Kubernetes. Поиск: `awx operator install kubernetes`.
- **semaphoreui.com** и **docs.semaphoreui.com** — Semaphore UI, установка в Docker, переменные окружения. Поиск: `semaphore ui docker compose environment`.
- **docs.docker.com** — `docker compose` и разворачивание сервисов. Поиск: `docker compose file reference`.
- **learn.microsoft.com** — на случай, если понадобится Windows-часть: «Ansible on Windows», WinRM. Поиск: `ansible windows winrm setup`.

Всё, что касается точных имён образов для Molecule, имён переменных окружения Semaphore и путей к `vmrun` — помечено `[ПРОВЕРИТЬ]`: проверяйте по своей версии и по официальной странице, а не по статьям-пересказам.
