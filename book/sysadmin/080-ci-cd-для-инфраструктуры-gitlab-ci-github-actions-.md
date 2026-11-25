# CI/CD для инфраструктуры: GitLab CI/GitHub Actions, проверки, линтеры, автодеплой

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»: **Пользователь:** Сидоров (склад). **ВМ:** UBU01. **Что написал:** «Вчера вечером на UBU01 поменяли motd и заодно что-то с SSH — утром мой скрипт выгрузки накладных падает с `Permission denied (publickey)`. Я ничего не трогал, честно». **Срочность:** высокая.

Дальше выясняется классика: ночью кто-то из админов «руками» поправил плейбук прямо на сервере, запушил, и пайплайн раскатал это на единственный хост. Ни ревью, ни проверок, ни возможности откатиться одной кнопкой.

CI/CD для инфраструктуры — это привычка менять серверы не руками и не «с ноутбука в VPN», а через конвейер: код в Git → автоматические проверки → план изменений → подтверждение человеком → применение → проверка результата. Для системного администратора это переход из «я умею настраивать» в «я умею настраивать так, чтобы это можно было проверить, повторить и откатить». После этой главы ты сможешь: поднять GitLab CE на GIT01, зарегистрировать runner, написать `.gitlab-ci.yml` с линтерами для Ansible/Terraform/скриптов, сделать автодеплой с ручным подтверждением и smoke-тестом, а если завтра компания скажет «мы на GitHub» — перевести то же самое на GitHub Actions без переизобретения логики.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Термины.** CI (Continuous Integration) — каждый коммит автоматически проверяется. CD (Continuous Delivery/Deployment) — проверенный коммит автоматически (или по кнопке) доезжает до серверов. **Pipeline** — весь конвейер, состоит из **stages** (этапов) и **jobs** (заданий). Job выполняется на **runner** — агенте, который по своей инициативе забирает задания с сервера GitLab (исходящее соединение, порт 443/80). **Executor** — то, *как* runner запускает job: `shell` (прямо на хосте, от пользователя `gitlab-runner`), `docker` (в контейнере, изолированно), `ssh`, `kubernetes`.

**Где это живёт по слоям.** Git — L7 (SSH 22 или HTTPS 443). Сам CI/CD — тоже L7: HTTP-API GitLab, HTTPS к registry, SSH к целевым серверам из джобы. Ниже — обычные TCP/IP и сеть стенда. Важная деталь: runnerу нужен только *исходящий* доступ к GitLab; входящих портов на runner открывать не надо — это сильно упрощает безопасность.

```text
[ПК админа]                     [GIT01 10.10.10.50]
 git push  ---22/443--->  ┌──────────────────────────────┐
                          │ GitLab CE (nginx :80/:443)   │
                          │  ├─ repo infra-vektor        │
                          │  ├─ .gitlab-ci.yml           │
                          │  └─ CI/CD API, variables     │
                          └───────────▲──────────────────┘
                                      │ job poll (443)
                          ┌───────────┴──────────────────┐
                          │ gitlab-runner (docker exec)  │
                          │  └─ container: yamllint,     │
                          │     ansible-lint, tofu, ssh  │
                          └───────────┬──────────────────┘
                                      │ ssh 22 (deploy/verify)
                                      ▼
                              [UBU01 10.10.10.30]
```

**Логика инфраструктурного пайплайна** отличается от софтверного. У приложения «собрать → протестировать → выкатить артефакт». У инфраструктуры: «проверить синтаксис и стиль → проверить, что *план* совпадает с ожидаемым → применить (идемпотентно) → проверить фактическое состояние». Слово дня — **идемпотентность**: повторный запуск не меняет результат. Именно поэтому в примерах ниже `--check` (Terraform/OpenTofu `plan`, Ansible `--check --diff`) — обязательный этап: он показывает *что именно* изменится, до того как изменится.

**GitLab CI vs GitHub Actions.** GitLab: один файл `.gitlab-ci.yml`, stages + jobs, runner'ы регистрируются на проект/группу/инстанс. GitHub: `.github/workflows/*.yml`, jobs + steps, runner'ы (`runs-on: [self-hosted, ...]`), секреты в Settings → Secrets and variables → Actions, ручное подтверждение — через Environments с required reviewers. Логика одна, синтаксис разный.

## 3. Аналогия из жизни

Инфраструктурный пайплайн — это издательство. Автор приносит рукопись (**git push**). Корректор и редактор ловят опечатки и «так не пишут» (**линтеры**). Печатают сигнальный экземпляр — посмотреть, как выглядит на бумаге (**plan / `--check`**). Главный редактор ставит подпись «в тираж» (**manual approval в джобе deploy**). Типография печатает (**apply**). Первый экземпляр из тиража проверяют вручную — не поплыла ли краска (**smoke-тест после деплоя**). Если брак — тираж отзывают (**revert и повторный прогон**).

Где аналогия ломается: испорченный тираж книги не влияет на уже проданные экземпляры, а неудачный деплой меняет живую систему — «отозвать» нельзя, можно только откатить, и откат тоже надо уметь делать быстро.

## 4. Практика и команды

**1. Установка GitLab CE на GIT01 (Ubuntu Server 24.04).** Официальный omnibus-пакет, всё в одном: nginx, PostgreSQL, Redis, Rails, Gitaly.

```bash
sudo apt update && sudo apt install -y curl openssh-server ca-certificates
curl -fsSL https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | sudo bash
sudo EXTERNAL_URL="http://gitlab.corp.vektor.lab" apt install -y gitlab-ce
```
`EXTERNAL_URL` — адрес, по которому GitLab будет себя рекламировать (в ссылках, в клонировании, в CI). Если указать IP — потом менять больно, поэтому сразу DNS-имя. Ресурсы: ~4 vCPU, 6–8 ГБ RAM, 30 ГБ диска, установка 10–20 минут. Официальные минимальные требования сверяй на docs.gitlab.com `[ПРОВЕРИТЬ]`.

**2. Стартовый пароль и состояние служб.**

```bash
sudo cat /etc/gitlab/initial_root_password
sudo gitlab-ctl status
sudo gitlab-ctl reconfigure
```
В `status` смотри, что все компоненты `run` (а не `down`/`fail`). `reconfigure` перечитывает `/etc/gitlab/gitlab.rb` — им ты меняешь настройки (например, таймзону), не переставляя пакет.

**3. Установка и регистрация runner.**

```bash
curl -fsSL https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh | sudo bash
sudo apt install -y gitlab-runner
sudo gitlab-runner register \
  --url http://gitlab.corp.vektor.lab \
  --token glrt-XXXXXXXXXXXX \
  --executor docker \
  --docker-image alpine:latest \
  --tag-list vektor-docker \
  --description "vektor-docker-runner"
```
`--token` берётся в UI: проект → **Settings → CI/CD → Runners → Create project runner**. В новых версиях GitLab это authentication token с префиксом `glrt-`, старый registration token уходит в прошлое — сверь пункты меню под свою версию `[ПРОВЕРИТЬ]`. `--tag-list` важен: джобы с `tags: [vektor-docker]` пойдут только на этот runner.

**4. Проверка конфигурации runner.**

```bash
sudo gitlab-runner list
sudo cat /etc/gitlab-runner/config.toml
```
В `config.toml` смотри `concurrent` (сколько джоб параллельно), `url`, `executor`, `privileged = false` (для линтеров привилегированный режим не нужен) и `volumes`. `gitlab-runner list` покажет зарегистрированные runner'ы и их executor.

**5. Линтеры локально — до пуша.** Привычка: если линтер падает у тебя локально, он не должен падать в CI.

```bash
yamllint -c .yamllint ansible tofu .gitlab-ci.yml
ansible-lint ansible/site.yml
shellcheck scripts/*.sh
tofu -chdir=tofu fmt -check -recursive
tofu -chdir=tofu validate
tflint --chdir=tofu
checkov -d tofu --compact
```
`yamllint` ловит отступы/длину строк в YAML (частая причина «пайплайн не парсится»). `ansible-lint` — антипаттерны Ansible (`command` вместо модуля, отсутствие `mode`, отсутствие `name`). `shellcheck` — кавычки, `rm -rf` без проверки переменной. `tofu fmt -check` — форматирование (ненулевой exit, если не отформатировано). `tofu validate` — синтаксис и ссылки на переменные/ресурсы. `tflint` — провайдер-специфичные ошибки (несуществующий тип инстанса). `checkov` — security-скан (открытый порт, публичный бакет). Часть инструментов требует `tofu init` перед `validate` — в CI делай `init -backend=false`.

**6. Сухая проверка Ansible.**

```bash
ansible-playbook -i ansible/inventories/lab/hosts.yml ansible/site.yml --syntax-check
ansible-playbook -i ansible/inventories/lab/hosts.yml ansible/site.yml --check --diff
```
`--syntax-check` — только парсинг. `--check --diff` — прогон без изменений с показом диффов. Не все модули поддерживают check mode (например, `command` без `creates`), такие места помечай `check_mode: false`.

**7. Валидация самого `.gitlab-ci.yml`.**

```bash
curl -s --request POST \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  --header "Content-Type: application/json" \
  --data "{\"content\": $(jq -Rs . < .gitlab-ci.yml)}" \
  "http://gitlab.corp.vektor.lab/api/v4/ci/lint" | jq .valid
```
`jq -Rs .` заворачивает файл в JSON-строку. Ответ: `true/false` плюс список ошибок. В UI есть то же самое: **CI/CD → Editor → Validate**.

**8. Коммит, пуш, релизный тег.**

```bash
git add . && git commit -m "ci: lint+deploy pipeline"
git push origin main
git tag -a v1.4.0 -m "release 1.4.0" && git push origin v1.4.0
```
Тег в пайплайне ловится правилом `if: $CI_COMMIT_TAG` — удобно для «релизных» деплоев отдельно от обычных.

**9. Ручной запуск пайплайна через API (для дежурного/интеграций).**

```bash
curl -X POST -F token=$TRIGGER_TOKEN -F ref=main \
  "http://gitlab.corp.vektor.lab/api/v4/projects/1/trigger/pipeline"
```
`token` — pipeline trigger token (Settings → CI/CD → Pipeline trigger tokens), `ref` — ветка или тег. Возвращает JSON с `id`, `status`, `web_url`.

**10. Диагностика джобы на runner.**

```bash
sudo gitlab-ctl tail gitlab-rails
sudo journalctl -u gitlab-runner -n 100 --no-pager
docker ps -a --filter "name=runner"
```
Смотри в `journalctl` строки `Checking for jobs... received` (job забрали) и `Job failed (system failure)` (проблема runner'а, а не кода). В `docker ps -a` — контейнеры упавших джоб: можно зайти внутрь и повторить команду руками.

**11. Smoke-тест после деплоя.**

```bash
ssh -i ~/.ssh/id_ed25519 adm.student@10.10.10.30 'cat /etc/motd.d/vektor.txt; systemctl is-active ssh'
```
Проверяет, что хост жив, SSH отвечает и файл создан. Такой тест ловит 80% «а после деплоя не работает».

**12. Локальные git-хуки как первый барьер.**

```bash
pip install --quiet pre-commit
pre-commit install
pre-commit run --all-files
```
`pre-commit` запускает те же линтеры до коммита (конфиг `.pre-commit-config.yaml`). Дешевле починить до пуша.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L2 готов: DC01 (10.10.10.10, AD/DNS/DHCP), UBU01 (10.10.10.30, SSH), домен `corp.vektor.lab`, учётка `adm.student`, единый пароль стенда `Lab#2026-vektor`.
- Материалы уроков 74–76: репозиторий с конфигурациями (Ansible-роль/плейбук и/или OpenTofu-конфиг) и навык работы с Docker (урок 78).
- **Новая ВМ этой главы:** `GIT01`, Ubuntu Server 24.04, **10.10.10.50**, 4 vCPU, 6–8 ГБ RAM, 30 ГБ диск, +4 ГБ swap. По таблице стенда GIT01 относится к DevOps-треку, но тема CI/CD без сервера Git не раскрывается — поэтому ставим его здесь и фиксируем в `stand-log.md` как «появился на sysadmin-треке».
- На UBU01: пользователь `adm.student` с `sudo` (без пароля для деплой-джобы — на стенде допустимо) и SSH-ключ для входа с GIT01.
- На DC01 в зоне `corp.vektor.lab` создать A-запись: `gitlab` → `10.10.10.50` (DNS-консоль → Forward Lookup Zones → corp.vektor.lab → New Host). Проверка: `nslookup gitlab.corp.vektor.lab 10.10.10.10`.

### Снапшот

```text
VMware Workstation → ВМ → Snapshot → Take Snapshot
Имя: before-sysadmin-080
Снять на: UBU01, DC01, GIT01 (если ВМ ещё не создана — снимаем на UBU01 и DC01,
GIT01 создаём после снапшота и помечаем это в stand-log.md)
```

### Шаги

1. **DC01:** добавить A-запись `gitlab` → `10.10.10.50`. Ожидаемо: `nslookup gitlab.corp.vektor.lab 10.10.10.10` с любой ВМ отдаёт `10.10.10.50`.
2. **GIT01:** задать статический адрес 10.10.10.50/24, шлюз 10.10.10.2, DNS 10.10.10.10 (netplan). Ожидаемо: `ip a` показывает адрес, `ping ya.ru` идёт (через NAT VMnet8).
3. **GIT01:** установить GitLab CE командами из раздела 4 (пункт 1), затем `sudo gitlab-ctl reconfigure`. Ожидаемо: `sudo gitlab-ctl status` — все компоненты `run`.
4. **GIT01:** взять пароль `sudo cat /etc/gitlab/initial_root_password`, зайти в `http://gitlab.corp.vektor.lab` под `root`, **сразу сменить пароль**, создать пользователя `adm.student` с правами Maintainer. Ожидаемо: вход под `adm.student` работает.
5. **GIT01:** установить и зарегистрировать runner (пункт 3), тег `vektor-docker`, executor `docker`. Ожидаемо: в проекте **Settings → CI/CD → Runners** runner зелёный (`online`).
6. **GIT01:** создать проект `infra-vektor` (Private), склонировать: `git clone http://gitlab.corp.vektor.lab/adm.student/infra-vektor.git`. Ожидаемо: пустой репозиторий локально.
7. **GIT01:** перенести конфиги из уроков 74–76 в структуру `ansible/`, `tofu/`, `scripts/`, добавить `.yamllint` и плейбук `ansible/site.yml`, который раскладывает `/etc/motd.d/vektor.txt` с версией релиза. Ожидаемо: `ansible-lint` и `yamllint` локально проходят без ошибок.
8. **GIT01:** сгенерировать деплой-ключ `ssh-keygen -t ed25519 -f ~/.ssh/deploy_ubu01 -N ""`, публичную часть положить в `~/.ssh/authorized_keys` пользователя `adm.student` **на UBU01**. Ожидаемо: `ssh -i ~/.ssh/deploy_ubu01 adm.student@10.10.10.30 hostname` возвращает `ubu01`.
9. **GIT01:** в проекте **Settings → CI/CD → Variables** добавить `SSH_PRIVATE_KEY` (тип *File* или *Variable*, флаги **Masked** + **Protected**) и `SSH_KNOWN_HOSTS` (вывод `ssh-keyscan -H 10.10.10.30`). Ожидаемо: переменные видны в списке, значения скрыты.
10. **GIT01:** создать `.gitlab-ci.yml` со стадиями `lint → plan → deploy → verify`, закоммитить и запушить в `main`. Ожидаемо: в **Build → Pipelines** появился пайплайн, стадии `lint` и `plan` завершились зелёным, `deploy` стоит в статусе *manual* (серая кнопка Play).
11. **GIT01:** нажать Play у джобы `deploy-ubu01`, затем дождаться `verify-ubu01`. Ожидаемо: `verify` зелёный, а на UBU01 появился `/etc/motd.d/vektor.txt` с SHA коммита.
12. **Открыть на UBU01** `/etc/motd.d/vektor.txt` (`cat`) и сравнить версию с `git log --oneline -1` в репозитории. Ожидаемо: хеши совпадают — деплой прошёл из кода, а не руками.
13. Записать изменения в `stand-log.md`.

Ключевой фрагмент `.gitlab-ci.yml`:

```yaml
stages: [lint, plan, deploy, verify]

default:
  tags: [vektor-docker]
  before_script:
    - echo "pipeline=$CI_PIPELINE_ID ref=$CI_COMMIT_REF_NAME sha=$CI_COMMIT_SHORT_SHA"

yamllint:
  stage: lint
  image: python:3.12-slim          # тег образа закрепи под свою версию [ПРОВЕРИТЬ]
  script:
    - pip install --quiet yamllint
    - yamllint -c .yamllint ansible tofu .gitlab-ci.yml

ansible-lint:
  stage: lint
  image: python:3.12-slim
  script:
    - pip install --quiet ansible-core ansible-lint
    - ansible-lint ansible/site.yml

shellcheck:
  stage: lint
  image: koalaman/shellcheck-alpine:stable   # [ПРОВЕРИТЬ] актуальный тег
  script:
    - shellcheck scripts/*.sh

tofu-validate:
  stage: lint
  image: ghcr.io/opentofu/opentofu:latest    # [ПРОВЕРИТЬ] тег образа
  script:
    - tofu -chdir=tofu init -backend=false
    - tofu -chdir=tofu fmt -check -recursive
    - tofu -chdir=tofu validate

ansible-check:
  stage: plan
  image: python:3.12-slim
  script:
    - pip install --quiet ansible-core
    - mkdir -p ~/.ssh && chmod 700 ~/.ssh
    - cp "$SSH_PRIVATE_KEY" ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
    - cp "$SSH_KNOWN_HOSTS" ~/.ssh/known_hosts
    - ansible-playbook -i ansible/inventories/lab/hosts.yml ansible/site.yml --syntax-check
    - ansible-playbook -i ansible/inventories/lab/hosts.yml ansible/site.yml --check --diff

deploy-ubu01:
  stage: deploy
  image: python:3.12-slim
  environment:
    name: lab-ubu01
  script:
    - pip install --quiet ansible-core
    - mkdir -p ~/.ssh && chmod 700 ~/.ssh
    - cp "$SSH_PRIVATE_KEY" ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
    - cp "$SSH_KNOWN_HOSTS" ~/.ssh/known_hosts
    - ansible-playbook -i ansible/inventories/lab/hosts.yml ansible/site.yml
        --limit ubu01 -e vektor_release=$CI_COMMIT_SHORT_SHA
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: manual
      allow_failure: false

verify-ubu01:
  stage: verify
  image: python:3.12-slim
  script:
    - mkdir -p ~/.ssh && chmod 700 ~/.ssh
    - cp "$SSH_PRIVATE_KEY" ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
    - cp "$SSH_KNOWN_HOSTS" ~/.ssh/known_hosts
    - ssh -o BatchMode=yes adm.student@10.10.10.30 'grep -q "$CI_COMMIT_SHORT_SHA" /etc/motd.d/vektor.txt || grep -q . /etc/motd.d/vektor.txt'
    - ssh -o BatchMode=yes adm.student@10.10.10.30 'systemctl is-active ssh'
```

### Проверка

- [ ] `nslookup gitlab.corp.vektor.lab 10.10.10.10` отдаёт 10.10.10.50
- [ ] `sudo gitlab-ctl status` на GIT01 — все компоненты `run`
- [ ] Runner в проекте в статусе `online`, тег `vektor-docker` совпадает с `tags` в CI-файле
- [ ] Пайплайн в `main` прошёл стадии `lint` и `plan` без ошибок
- [ ] Джоба `deploy-ubu01` требовала ручного нажатия Play
- [ ] `ssh adm.student@10.10.10.30 cat /etc/motd.d/vektor.txt` содержит SHA последнего коммита `main`
- [ ] `git log --oneline -1` и содержимое motd совпадают
- [ ] В `stand-log.md` записаны: GIT01 (10.10.10.50), A-запись `gitlab`, проект `infra-vektor`, runner `vektor-docker`, переменные `SSH_PRIVATE_KEY`/`SSH_KNOWN_HOSTS`

### Сломай и почини

**Пользователь:** Сидоров (склад). **ВМ:** UBU01. **Что написал:** «После ночного деплоя не могу зайти по своему ключу, пишет `Permission denied (publickey)`. Мне до обеда отчёт выгрузить». **Срочность:** высокая.

**Как воспроизвести.** В плейбуке `ansible/site.yml` добавь задачу управления ключами с `exclusive: true`:

```yaml
    - name: Ключи админа
      ansible.builtin.authorized_key:
        user: adm.student
        key: "{{ lookup('file', 'files/deploy_ubu01.pub') }}"
        exclusive: true
```

Запушь в `main`, нажми Play. `exclusive: true` удалит все ключи, кроме указанного, — ключ Сидорова исчезнет.

**Диагностика по порядку.**
1. Что менялось: `git log --oneline -5` в репозитории и список пайплайнов в UI — ищем последний успешный `deploy` после жалобы.
2. Симптом: `ssh -vvv sidorov@10.10.10.30` → в выводе `Offering public key` и `Authentications that can continue: publickey`, затем `Permission denied`.
3. На сервере: `sudo tail -n 50 /var/log/auth.log` → строки `Failed publickey for sidorov` или `Connection closed by authenticating user` — ключ не принят.
4. Факт состояния: `sudo cat /home/sidorov/.ssh/authorized_keys` (или ключ лежит в другом файле — проверь `sudo sshd -T | grep -i authorizedkeysfile`).
5. Конфиг демона: `sudo sshd -T | grep -Ei 'pubkeyauth|authorizedkeysfile'` — убедиться, что дело не в `PubkeyAuthentication no`.
6. Причина в коде: `git show HEAD -- ansible/` — видим `exclusive: true`.

**Решение.** Быстрый возврат сервиса — вернуть ключ вручную (`sudo nano /home/sidorov/.ssh/authorized_keys`, вставить ключ из тикета/бэкапа, `chmod 600`, `chown`). Потом — правильно: `git revert <commit>` → пуш → пайплайн (проверки + ручной Play) возвращает согласованное состояние. Из плейбука `exclusive: true` убираем, вместо этого — отдельный список управляемых ключей с явным комментарием и без `exclusive`. Дополнительно: добавить в `verify` проверку `ssh` под сервисной учёткой и пост-деплойный чек «количество ключей не уменьшилось».

**Ответ пользователю:** «Алексей, извини — ночной деплой перезаписал список SSH-ключей. Ключ уже вернул, доступ должен работать в течение пары минут, проверь, пожалуйста. Причину устранил, чтобы не повторилось; если снова не пустит — сразу звони».

**Закрытие заявки:** категория «доступ/SSH», корневая причина «плейбук с `authorized_key: exclusive` в автоматическом деплое», действие «revert + правка плейбука + smoke-тест на ключи», профилактика «добавлено правило в ansible-lint/ревью и проверка в стадии verify».

### Что изменилось на стенде

- Новая ВМ **GIT01** — Ubuntu Server 24.04, 10.10.10.50, GitLab CE, runner `vektor-docker` (docker executor).
- На DC01 в зоне `corp.vektor.lab` — A-запись `gitlab` → 10.10.10.50.
- GIT01: учётка `adm.student` (Maintainer), проект `infra-vektor`, переменные `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS` (masked+protected), ветка `main` защищена.
- UBU01: пользователь `adm.student` принимает деплой-ключ с GIT01; появился файл `/etc/motd.d/vektor.txt`, управляемый пайплайном.
- Репозиторий `infra-vektor` содержит `ansible/`, `tofu/`, `scripts/`, `.yamllint`, `.gitlab-ci.yml`.

### Откат

VMware Workstation → ВМ → Snapshot Manager → выбрать `before-sysadmin-080` → **Revert**. Откатывать на UBU01 и DC01; GIT01 либо откатить к снапшоту (если он был), либо удалить ВМ и записать в `stand-log.md`, что GitLab снят. После отката — обновить `stand-log.md` строкой «откат к before-sysadmin-080, причина: …».

## 6. Ошибки новичков и безопасность

1. **Секреты в `.gitlab-ci.yml` или в репозитории** → утечка ключей навсегда (история Git не забывает) → только переменные CI, masked + protected, а для продакшена — Vault/OIDC.
2. **`echo $SSH_PRIVATE_KEY` в джобе для отладки** → ключ в логах, доступном всей команде → отладка через `ssh -vvv` на локальной копии, а не печать секретов.
3. **Runner с `shell`-executor на боевом хосте** → любой коммит в ветку = код от пользователя `gitlab-runner` на сервере → docker executor, отдельная ВМ/сегмент, non-privileged.
4. **Автодеплой без ручного подтверждения в прод** → одна опечатка в `--limit` или в `become` ломает всё сразу → `when: manual`, защищённые ветки, environment с ревьюером.
5. **Нет шага `plan/--check`** → деплой «сюрпризом»: изменилось больше, чем ожидалось → всегда показывать дифф до применения.
6. **Игнорирование падающих линтеров** (`allow_failure: true` на всё) → линтеры превращаются в декорацию → `allow_failure` только для косметики, security-сканы — блокирующие.
7. **Нет smoke-теста после деплоя** → «пайплайн зелёный, сервис лежит» → стадия `verify` с проверкой реального состояния.
8. **Деплой из feature-ветки на прод** → правила `rules`/`only` не настроены → деплой только с `main`/тега.

**Безопасность (что нельзя в боевой среде).** Не хранить пароль `root` GitLab и деплой-ключи в заметках/чатах. Не давать runner'у права `sudo` без ограничений и не монтировать `/var/run/docker.sock` в джобы без необходимости. Не использовать `ssh-keyscan` в пайплайне «на удачу» — отпечаток хоста должен быть известен заранее, иначе возможен MITM. Не запускать деплой-джобы из merge request'ов от внешних контрибьюторов с доступом к protected-переменным — GitLab как раз поэтому не отдаёт protected-переменные в незащищённые ветки. Не отключать линтеры «потому что мешают» — это единственный автоматический барьер между черновиком и продом.

## 7. Вопросы на собеседовании

**1. Чем CI/CD для инфраструктуры отличается от CI/CD для приложения?**
Ответ: у приложения результат — артефакт (бинарник, образ), который катится на неизменяемую платформу. У инфраструктуры результат — изменение живой системы, поэтому ключевые этапы: статическая проверка (линтеры), `plan`/`--check` с диффом, идемпотентное применение и проверка фактического состояния после. Плюс критичны дрейф (drift), откат и ручное подтверждение на прод-среде.
*Что хочет услышать интервьюер:* понимание plan/apply, идемпотентности, роли ревью и отката.
*Красный флаг:* «разницы нет, просто запускаем ansible-playbook из пайплайна».

**2. Как хранить секреты в пайплайне?**
Ответ: переменные CI с флагами masked (скрытие в логах) и protected (доступны только защищённым веткам/тегам), переменные уровня группы/инстанса, для продакшена — внешний секрет-менеджер (HashiCorp Vault, Ansible Vault, OIDC-обмен на короткоживущий токен). В GitHub Actions — Secrets и Environments. Ключи — только файлом/через API, без печати в лог, с ротацией.
*Что хочет услышать:* masked ≠ безопасно навсегда; принцип наименьших привилегий и ротация.
*Красный флаг:* «положу ключ в репозиторий, репа приватная».

**3. Как сделать автодеплой, который можно быстро откатить?**
Ответ: деплой только с защищённой ветки или тега, ручное подтверждение через `when: manual` или environment с ревьюером; перед применением — `plan`/`--check` с диффом; после — smoke-тест. Версионирование: тег на каждый релиз, деплой по SHA. Откат — `git revert` + повторный прогон (декларативные конфиги возвращают предыдущее состояние), плюс заранее известный план ручного отката на случай, если пайплайн сам недоступен.
*Что хочет услышать:* «откат — это тоже пайплайн, отрепетированный заранее».
*Красный флаг:* «просто перезалью снапшот».

**4. Что линтеры НЕ ловят и как это закрывать?**
Ответ: логические ошибки (не тот хост в `--limit`, `become: true` на всех сразу), проблемы производительности, дрейф конфигурации на живых серверах, смысловые баги в шаблонах. Закрывается: `plan`/`--check`, тестами ролей (Molecule), integration-тестами на одноразовой ВМ, smoke-тестами после деплоя, мониторингом (Zabbix на MON01) и ревью человеком.
*Что хочет услышать:* линтер — фильтр синтаксиса и стиля, не замена тестам.
*Красный флаг:* «ansible-lint зелёный, значит всё ок».

**5. Почему runner на shell-executor на прод-сервере — плохая идея?**
Ответ: джобы выполняются от пользователя `gitlab-runner` прямо на хосте: компрометация runner'а = компрометация сервера, а при наличии `sudo` — root. Нет изоляции между проектами, остаются файлы и процессы, `concurrent` джобы дерутся за ресурсы. Правильно: отдельная ВМ/сегмент, docker executor без `privileged`, эфемерные runner'ы (autoscaling), ограниченный сервисный аккаунт, сетевые ограничения «только к целевым хостам».
*Что хочет услышать:* изоляция, минимальные привилегии, сетевая сегментация.
*Красный флаг:* «удобно же, всё под рукой».

## 8. Источники (официальная документация)

- **docs.gitlab.com** — разделы «CI/CD pipelines», «GitLab Runner», «CI/CD variables», «Pipeline editor and CI Lint». Поиск: `gitlab ci cd yaml reference`, `gitlab runner register docker executor`.
- **docs.github.com** — разделы «Actions», «Workflows», «Encrypted secrets», «Self-hosted runners», «Environments». Поиск: `github actions workflow syntax`, `self-hosted runner`.
- **docs.ansible.com** — «ansible-playbook» (ключи `--check`, `--diff`, `--syntax-check`), «Playbook best practices». Поиск: `ansible check mode diff`.
- **ansible-lint.readthedocs.io** — «Usage», «Profiles», «Configuration». Поиск: `ansible-lint profiles config`.
- **yamllint.readthedocs.io** — «Configuration», «Rules». Поиск: `yamllint configuration file`.
- **opentofu.org** — «CLI commands: init, validate, fmt, plan»; **developer.hashicorp.com** — раздел Terraform «CLI commands». Поиск: `opentofu cli validate fmt`, `terraform plan out`.
- **shellcheck.net** — «ShellCheck wiki» (коды SCxxxx). Поиск: `shellcheck SC2086`.
- **checkov.io** (проект Bridgecrew/Prisma Cloud) — «Ansible and Terraform scanning». Поиск: `checkov terraform scan compact` `[ПРОВЕРИТЬ актуальный домен]`.
- **tflint.readthedocs.io** — «Usage», «Rulesets». Поиск: `tflint ruleset terraform` `[ПРОВЕРИТЬ]`.
- **pre-commit.com** — «Quick start», «Adding hooks». Поиск: `pre-commit install configuration`.
- **gitlab.com / github.com** — репозитории установочных скриптов `gitlab-ce`, `gitlab-runner` (packages.gitlab.com). Поиск: `gitlab ce install ubuntu packages.gitlab.com`.

Где стоит ориентироваться на 2026 год: доступность `packages.gitlab.com` и `ghcr.io` из РФ может быть нестабильной — легальная альтернатива: self-hosted Gitea/Forgejo с Gitea Actions (синтаксис близок к GitHub Actions) `[ПРОВЕРИТЬ]` или GitLab, развёрнутый из зеркала пакетов внутри компании.
