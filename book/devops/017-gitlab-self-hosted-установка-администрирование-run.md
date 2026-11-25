# GitLab self-hosted: установка, администрирование, runners, группы, права, бэкапы

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку: **Пользователь:** ivanov (Иван Иванов, бухгалтерия, ВМ WIN11-CL01). **Что написал:** «Мы с подрядчиком пишем скрипты для сверки платежей. Сейчас версии лежат в папке `\\FS01\Obshchee\Сверка\final_v3_НОВЫЙ`, половина файлов затёрта, никто не помнит, что где. Нужно место, где хранятся версии, где видно, кто что менял, и чтобы проверки запускались сами после каждого изменения. И чтобы это было внутри нашей сети, наружу ничего не уходило. Срочность: средняя, но через две недели аудит».

С такой заявкой вы будете сталкиваться регулярно: разработка внутри компании хочет «свой GitHub», безопасность — чтобы код и данные не уезжали в публичное облако, а вы — чтобы это не превратилось в зоопарк из десяти несвязанных сервисов. GitLab self-hosted — самый частый ответ: один сервер, внутри которого уже есть репозитории, веб-интерфейс, issue-трекер, CI/CD-движок (GitLab CI) и реестр Docker-образов. После этой главы вы умеете: поставить GitLab CE на Ubuntu Server, разумно настроить его на скромной ВМ, завести группы и проекты, выдать людям права по ролям, подключить и отладить GitLab Runner, сделать и проверить бэкап, а также аргументированно ответить, почему в РФ 2026 года GitLab — не единственный и не всегда лучший выбор.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

GitLab, установленный «омнибусом» (omnibus-пакетом `gitlab-ce`), — это не одно приложение, а десяток сервисов, которые живут на одной ВМ и управляются единым супервизором `runit` через команду `gitlab-ctl`. Это важнейшее отличие от «поставил nginx + gitea», и именно из него растут 80% проблем администрирования.

```text
   браузер (HTTPS 443 / HTTP 80)      git push по SSH (22)
              |                              |
        [ nginx + Workhorse ]  <----  [ система sshd + gitlab-shell ]
              |                              |
      [ Puma, Ruby on Rails ]        [ Gitaly ] -- репозитории на диске
        (веб, REST API, /api/v4)      /var/opt/gitlab/git-data/repositories
              |
      [ Sidekiq ] — фоновые задачи: пайплайны, письма, импорт, бэкапы
              |
      [ PostgreSQL 16 ] [ Redis ] [ Prometheus (опц.) ]

   GitLab Runner — ОТДЕЛЬНЫЙ агент (своя ВМ или контейнер):
   опрашивает GitLab по HTTP(S) API -> получает job -> клонирует репо
   -> выполняет скрипт (shell / docker / kubernetes executor) -> шлёт статус и логи
```

Ключевые компоненты:

- **Gitaly** — сервис, который один владеет Git-репозиториями. Все остальные обращаются к нему по gRPC. Поэтому «зайти в папку и сделать `git gc` руками» — плохая идея: состояние кешей Gitaly разъедется.
- **Puma** — веб-приложение на Ruby on Rails. Работает на unix-сокете, наружу его выставляет только nginx.
- **Sidekiq** — очередь фоновых задач на Redis. Сюда попадают CI-пайплайны, отправка писем, создание бэкапа. Если Sidekiq лежит, сайт открывается, но пайплайны «висят».
- **Workhorse** — прослойка на Go между nginx и Puma: отдаёт большие файлы, git-запросы по HTTP, upload-ы артефактов, не нагружая Ruby.
- **PostgreSQL и Redis** — встроенные, слушают только localhost (Redis — вообще unix-сокет).
- **GitLab Runner** — отдельный бинарник. По умолчанию сам ничего не слушает: он **сам ходит** к GitLab через long-polling API (исходящие 80/443), поэтому входящий порт ему открывать не нужно — удобно с точки зрения firewall и DMZ.

Раскладка на диске, которую надо знать наизусть: `/etc/gitlab` — конфиг и **секреты** (`gitlab.rb`, `gitlab-secrets.json`), `/var/opt/gitlab` — данные (репозитории, БД, бэкапы), `/var/log/gitlab` — логи, `/opt/gitlab` — сам код и встроенные бинарники (там же `git`, `psql`, `redis-cli`).

Модель прав — роли на трёх уровнях: **instance** (весь GitLab), **group** (и вложенные подгруппы) и **project**. Пять стандартных ролей: Guest (10), Reporter (20), Developer (30), Maintainer (40), Owner (50) — числа — это `access_level` в API, их вы будете писать в `curl`. Отдельно есть видимость проекта (Private/Internal/Public) и защищённые ветки (protected branches), которые ограничивают не «кто читает», а «кто пушит и мержит».

## 3. Аналогия из жизни

GitLab omnibus — это не набор отдельных мастерских, а один большой цех под одной крышей: на первом этаже склад с верстаками (Gitaly), на втором офис менеджеров, принимающих заказы (Puma), в подвале бригада грузчиков, которая разносит коробки по расписанию (Sidekiq), на входе вахтёр с журналом (nginx + Workhorse), а в углу — бухгалтерия и радиоузел (PostgreSQL и Redis). Нанимаете вы всё это одним договором аренды, и управляет всем один комендант — `gitlab-ctl`. Runner же — это курьер, которого вы можете посадить где угодно за пределами цеха: он сам приезжает за заданиями, ему не нужно строить отдельный вход. Аналогия перестаёт работать, когда цех начинает гореть: вы не можете «выключить только кухню и продолжать принимать заказы» — половина сервисов жёстко связана, и остановка PostgreSQL кладёт весь сайт.

## 4. Практика и команды

**1. Установка из официального репозитория (Ubuntu 24.04).**

```bash
sudo apt update && sudo apt -y upgrade
curl --location "https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh" | sudo bash
sudo EXTERNAL_URL="http://gitlab.vektor.lab" apt-get install -y gitlab-ce
```

Скрипт подключает репозиторий и GPG-ключ `packages.gitlab.com`. `EXTERNAL_URL` — адрес, по которому GitLab будет доступен *снаружи*; он попадает в конфиг и в ссылки для клонирования навсегда, менять его потом без последствий можно, но аккуратно (см. раздел 6). Ожидаемый вывод: `It looks like GitLab has not been configured yet; skipping the upgrade script...` и затем `Thank you for installing GitLab!`. Обратите внимание: HTTPS здесь нет — для лабы это осознанное упрощение, в боевой среде так нельзя (см. урок 11 про TLS).

**2. Первый запуск и проверка состояния.**

```bash
sudo gitlab-ctl reconfigure
sudo gitlab-ctl status
sudo ss -ltnp | grep -E ':(80|443|22|9090)\b'
```

`reconfigure` — главная команда администрирования: читает `/etc/gitlab/gitlab.rb`, генерирует конфиги всех компонентов и перезапускает то, что изменилось. Вывод `status` — по строке на сервис, смотрите, чтобы везде было `run:` и не было `down:`:

```text
run: gitaly: (pid 2143) 61s; run: log: (pid 1988) 62s
run: nginx: (pid 2201) 60s; run: log: (pid 1999) 62s
run: puma: (pid 2277) 15s; run: log: (pid 2001) 62s
run: sidekiq: (pid 2291) 14s; run: log: (pid 2005) 62s
```

**3. Основные настройки в `/etc/gitlab/gitlab.rb`.**

```ruby
external_url 'http://gitlab.vektor.lab'

# Экономим память на учебной ВМ 8 ГБ
puma['worker_processes'] = 2
sidekiq['max_concurrency'] = 10
prometheus_monitoring['enable'] = false

postgresql['shared_buffers'] = "512MB"

# Храним бэкапы 7 дней (значение в секундах)
gitlab_rails['backup_keep_time'] = 604800
```

После правки — всегда `sudo gitlab-ctl reconfigure`. Файл на Ruby-синтаксисе: пропущенная кавычка ломает весь конфиг, читайте вывод на `There was an error running gitlab-ctl reconfigure`.

**4. Первый вход и сброс пароля root.**

```bash
sudo cat /etc/gitlab/initial_root_password
sudo gitlab-rake "gitlab:password:reset[root]"
```

Пароль root лежит в файле и **автоматически удаляется через 24 часа** после первого `reconfigure`. Если забыли — вторая команда сбросит его интерактивно (в свежих версиях синтаксис `gitlab-rake "gitlab:password:reset"` ещё работает, но вариант с `[root]` надёжнее; [ПРОВЕРИТЬ] для 18.x).

**5. Консоль Rails — «админка за пределами админки».**

```bash
sudo gitlab-rails runner "puts User.count; puts Project.pluck(:path).last(5)"
```

Выполняет произвольный Ruby в контексте GitLab. Мощно и опасно: без `sudo gitlab-rails console` (интерактивно) ничего не откатывается, `DELETE`-запросы идут напрямую в БД.

**6. Группа и проект через API — то, что вы будете автоматизировать.**

```bash
TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"   # Settings -> Access tokens, scope: api
curl --request POST --header "PRIVATE-TOKEN: $TOKEN" \
  --data "name=vektor-dev&path=vektor-dev&visibility=private" \
  "http://gitlab.vektor.lab/api/v4/groups"

curl --request POST --header "PRIVATE-TOKEN: $TOKEN" \
  --data "name=payment-reconciliation&path=payment-reconciliation&namespace_id=1" \
  "http://gitlab.vektor.lab/api/v4/projects"
```

Коды ответа: `201` — создано, `400` — path занят или невалиден, `401` — токен протух/без scope, `403` — нет прав. В `/api/v4/groups?search=vektor` можно проверить, что группа реально есть.

**7. Права: добавление участника в группу.**

```bash
curl --request POST --header "PRIVATE-TOKEN: $TOKEN" \
  --data "user_id=3&access_level=30" \
  "http://gitlab.vektor.lab/api/v4/groups/vektor-dev/members"
```

`access_level=30` — Developer (пушит в незащищённые ветки, запускает пайплайны), `20` — Reporter (только чтение), `40` — Maintainer (настройки проекта, защищённые ветки). Разница между Developer и Maintainer в собеседованиях спрашивается постоянно.

**8. Защищённая ветка `main`.**

GUI: Project → Settings → Repository → Protected branches → выбрать `main`, Allowed to merge: Maintainers, Allowed to push: Maintainers. Через API:

```bash
curl --request POST --header "PRIVATE-TOKEN: $TOKEN" \
  --data "name=main&push_access_level=40&merge_access_level=40" \
  "http://gitlab.vektor.lab/api/v4/projects/1/protected_branches"
```

Типичный результат: разработчик с Developer получает `remote: GitLab: You are not allowed to push code to protected branches on this project` — это не поломка, а работающая защита.

**9. Установка GitLab Runner на отдельной ВМ (UBU01).**

```bash
curl --location "https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh" | sudo bash
sudo apt-get install -y gitlab-runner docker.io
sudo usermod -aG docker gitlab-runner
sudo systemctl restart gitlab-runner
```

Runner — отдельный пакет, к `gitlab-ce` не привязан. `usermod -aG docker` обязателен для docker-исполнителя, иначе задачи упадут с `permission denied while trying to connect to the Docker daemon socket`.

**10. Регистрация раннера (новый поток, GitLab 16+).**

В UI: Admin Area (или группа) → Build → Runners → New instance runner → выбираем теги и получаем токен `glrt-...`.

```bash
sudo gitlab-runner register \
  --url "http://gitlab.vektor.lab" \
  --token "glrt-xxxxxxxxxxxxxxxx" \
  --executor "docker" \
  --docker-image "alpine:latest" \
  --description "ubu01-docker-1"
```

Старый способ через registration token из `CI/CD → Runners` объявлен устаревшим (с 16.0) и удалён в 17.0 [ПРОВЕРИТЬ точную версию удаления] — если видите инструкции с `--registration-token`, это прошлый век. Проверка: `sudo gitlab-runner list`, `sudo gitlab-runner verify`.

**11. Конфиг раннера и простой пайплайн.**

```toml
# /etc/gitlab-runner/config.toml
concurrent = 2
[[runners]]
  name = "ubu01-docker-1"
  url = "http://gitlab.vektor.lab"
  executor = "docker"
  [runners.docker]
    image = "alpine:latest"
    privileged = false
    volumes = ["/cache"]
```

```yaml
# .gitlab-ci.yml
stages: [lint, test]

shellcheck:
  stage: lint
  image: koalaman/shellcheck-alpine:stable
  script:
    - shellcheck ./scripts/*.sh

smoke:
  stage: test
  needs: [shellcheck]
  tags: [docker]
  script:
    - echo "ok"
```

`tags` — единственный механизм, которым job находит раннер: если у job тег `docker`, а у всех раннеров тегов нет, job встанет в статус *pending* навсегда. `needs` задаёт DAG вместо строгой последовательности стадий.

**12. Бэкап и восстановление.**

```bash
sudo gitlab-backup create SKIP=registry
sudo gitlab-ctl backup-etc
sudo ls -lh /var/opt/gitlab/backups /etc/gitlab/config_backup
```

`gitlab-backup create` пакует БД, репозитории, upload-ы и артефакты в `/var/opt/gitlab/backups`; файл называется `<timestamp>_<дата>_<версия>_gitlab_backup.tar`. **Секреты (`gitlab-secrets.json`) в него не входят** — их снимает вторая команда (tar в `/etc/gitlab/config_backup`). Восстановление:

```bash
sudo gitlab-ctl stop puma && sudo gitlab-ctl stop sidekiq
sudo gitlab-backup restore BACKUP=1750000000_2026_01_15_17.5.0-ce
sudo gitlab-ctl reconfigure && sudo gitlab-ctl restart
sudo gitlab-rake gitlab:check SANITIZE=true   # [ПРОВЕРИТЬ] актуальность в 18.x
```

`BACKUP=` принимает имя файла **без** суффикса `_gitlab_backup.tar`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GIT01** — ещё не создана. Создаём: Ubuntu Server 24.04 LTS, 4 vCPU, 8 ГБ RAM (если хост не тянет — 4 ГБ RAM + 4 ГБ swap, но будет медленно и часть шагов придётся ждать по 3–5 минут), диск 60 ГБ, один адаптер в VMnet8, статический адрес **10.10.10.50/24**, шлюз 10.10.10.2, DNS — 10.10.10.10, если DC01 в стенде есть; если Windows-части нет (трек DevOps её не гарантирует) — 1.1.1.1 или локальный резолвер. Открыт SSH, учётка `adm.student` с sudo.
- **UBU01** — 10.10.10.30, Ubuntu Server 24.04, SSH работает (глава про SSH пройдена).
- Остальные машины стенда не затрагиваются.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-017`. Сделать на **UBU01** (существующая ВМ) и на **GIT01** сразу после создания ВМ и настройки сети, но **до** установки пакета `gitlab-ce`. Имя снапшота одинаковое на всех ВМ.

### Шаги

1. **GIT01.** Настраиваем хостнейм и hosts:
   ```bash
   sudo hostnamectl set-hostname git01
   echo "10.10.10.50 gitlab.vektor.lab" | sudo tee -a /etc/hosts
   sudo apt update && sudo apt -y upgrade
   ```
   Ожидаемо: `hostname` возвращает `git01`, `ping -c1 gitlab.vektor.lab` отвечает с 10.10.10.50.

2. **GIT01.** Добавляем swap, если RAM меньше 8 ГБ:
   ```bash
   sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   free -h
   ```
   Ожидаемо: в строке `Swap` ненулевой объём.

3. **GIT01.** Подключаем репозиторий GitLab CE и ставим пакет с `EXTERNAL_URL` (команда №1 из раздела 4). Ожидаемо: `Thank you for installing GitLab!` и подсказка про `/etc/gitlab/initial_root_password`.

4. **GIT01.** `sudo gitlab-ctl reconfigure`, затем `sudo gitlab-ctl status`. Ожидаемо: все строки `run:`, ни одной `down:`. Если `puma` падает в цикле — почти всегда не хватило RAM.

5. **GIT01.** Открываем `http://gitlab.vektor.lab` в браузере с хоста (для этого добавьте строку `10.10.10.50 gitlab.vektor.lab` в `C:\Windows\System32\drivers\etc\hosts` на своём ПК). Логин `root`, пароль из `sudo cat /etc/gitlab/initial_root_password`. Ожидаемо: интерфейс, предложение сменить пароль. Меняем на `Lab#2026-vektor`.

6. **GIT01 (UI).** Создаём группу `vektor-dev` (Visibility: Private), в ней подгруппу `payment`, в ней проект `payment-reconciliation`. Ожидаемо: в списке проектов путь `vektor-dev/payment/payment-reconciliation`.

7. **GIT01 (UI).** Admin Area → Users → New user: `ivanov` (Developer) и `petrova` (Reporter), пароль `Lab#2026-vektor`, снять галку «Force password reset». Добавляем обоих в группу `vektor-dev` с соответствующими ролями. Ожидаемо: в Members группы две записи с разными ролями. Это **локальные** учётки GitLab, не путать с одноимёнными пользователями AD — синхронизация с AD через LDAP здесь не настраивается (это отдельная тема).

8. **GIT01 (UI).** Project → Settings → Repository → Protected branches: `main`, merge/push — Maintainers. Ожидаемо: под `ivanov` push в `main` отклоняется, а в новую ветку `feature/x` — проходит.

9. **UBU01.** Ставим раннер и Docker (команда №9), регистрируем его под группой `vektor-dev` с тегом `docker` (команда №10, токен берём в Group → Build → Runners). Ожидаемо: `sudo gitlab-runner list` показывает `ubu01-docker-1`, а в UI раннер зелёный.

10. **GIT01 (UI/локально).** Клонируем проект под `ivanov`, добавляем `scripts/check.sh` и `.gitlab-ci.yml` из команды №11, пушим ветку, создаём merge request. Ожидаемо: пайплайн из двух стадий зелёный, в логах job видно `Using Docker executor with image koalaman/shellcheck-alpine:stable`.

11. **GIT01.** Делаем бэкап (команда №12). Ожидаемо: в `/var/opt/gitlab/backups` файл ~несколько десятков МБ, в `/etc/gitlab/config_backup` — tar с конфигом и секретами. Записываем оба имени файла в `stand-log.md`.

12. **Проверка восстановления без разрушения стенда:** распаковываем `config_backup`-tar в `/tmp` (`tar -tf`, проверить наличие `gitlab-secrets.json` внутри) и убеждаемся, что `gitlab-secrets.json` **не** лежит в основном бэкапе (`tar -tf <backup>.tar | grep -c secrets` → 0). Ожидаемо: понимание, что два файла нужны вместе.

### Проверка

- [ ] `sudo gitlab-ctl status` на GIT01 — все сервисы в состоянии `run:`.
- [ ] `http://gitlab.vektor.lab` открывается, вход под `root` работает.
- [ ] `curl -s -o /dev/null -w "%{http_code}" http://gitlab.vektor.lab/-/health` возвращает `200` [ПРОВЕРИТЬ endpoint для вашей версии].
- [ ] Группа `vektor-dev` с подгруппой `payment` и проектом существует, видимость Private.
- [ ] `ivanov` = Developer, `petrova` = Reporter: `petrova` не может пушить, `ivanov` не может пушить в `main`.
- [ ] Раннер `ubu01-docker-1` в статусе online, тег `docker` виден в UI.
- [ ] Пайплайн по merge request зелёный, лог job содержит имя docker-образа.
- [ ] В `/var/opt/gitlab/backups` есть свежий `.tar`, в `/etc/gitlab/config_backup` — архив конфига.
- [ ] В `stand-log.md` записаны: адрес GIT01, имена групп/проектов, имена бэкап-файлов, тег раннера.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **ВМ/сервис:** GitLab на GIT01, проект `payment-reconciliation`
> **Что написал:** «Залил изменения, создал merge request, а пайплайн висит уже 40 минут — `This job is stuck because there are no runners online with the assigned tags`. Ничего не менял, вчера работало».
> **Срочность:** высокая (блокирует релиз скриптов сверки).

**Как воспроизвести поломку.** На UBU01: `sudo gitlab-runner pause ubu01-docker-1` (или `sudo systemctl stop gitlab-runner`). Либо в `.gitlab-ci.yml` поменять `tags: [docker]` на `tags: [shell]` — раннера с таким тегом нет.

**Диагностика по порядку:**
1. Читаем текст ошибки буквально: проблема не в Git, не в сети, а в сопоставлении job ↔ runner.
2. UI: Project → Build → Runners и Group → Build → Runners. Смотрим статус (paused / offline / online) и список тегов.
3. Смотрим теги самого job: `.gitlab-ci.yml`, ключ `tags`.
4. На UBU01: `sudo gitlab-runner list` (виден ли конфиг), `sudo systemctl status gitlab-runner`, `sudo journalctl -u gitlab-runner -n 50`.
5. `sudo gitlab-runner verify` — проверяет, что токен раннера ещё валиден на сервере.
6. Если раннер «online», но job не берёт — проверяем в UI галку «Run untagged jobs» и `concurrent` в `config.toml` (при `concurrent = 0` задачи не запускаются вообще).
7. Если задачи стартуют и падают на `permission denied ... docker.sock` — `sudo usermod -aG docker gitlab-runner && sudo systemctl restart gitlab-runner`.

**Решение.** Если раннер на паузе — `sudo gitlab-runner resume ubu01-docker-1` (или `sudo systemctl start gitlab-runner`), затем в UI нажать Retry на job. Если рассогласованы теги — привести `tags` в `.gitlab-ci.yml` к тегам раннера через новую ветку и MR.

**Ответ пользователю:** «Пайплайн стоял из-за недоступного CI-раннера: он был переведён в паузу при обслуживании, задача ждала исполнителя. Раннер включён, джоба перезапущена, пайплайн зелёный. На будущее: статус `pending` дольше 5 минут — это всегда раннеры или теги, пишите сразу, разберёмся быстрее».

**Закрытие заявки:** категория «CI/CD», решение «runner paused / tag mismatch», в комментарии — ссылка на зелёный пайплайн и короткая заметка в базу знаний «что делать, если job stuck».

### Что изменилось на стенде

- Появилась ВМ **GIT01** (10.10.10.50), Ubuntu Server 24.04, установлен `gitlab-ce`, внешний адрес `http://gitlab.vektor.lab` (резолвится через `/etc/hosts` на GIT01/UBU01 и через `hosts` на ПК).
- Локальные пользователи GitLab: `root`, `ivanov` (Developer), `petrova` (Reporter). Пароль стенда `Lab#2026-vektor`. **Это отдельные учётки, не AD.**
- Группа `vektor-dev` → подгруппа `payment` → проект `payment-reconciliation`, ветка `main` защищена.
- На **UBU01** установлены `gitlab-runner` и `docker.io`; зарегистрирован раннер `ubu01-docker-1` (executor docker, тег `docker`), пользователь `gitlab-runner` добавлен в группу `docker`.
- Бэкапы: `/var/opt/gitlab/backups/*_gitlab_backup.tar` и `/etc/gitlab/config_backup/*.tar` — имена записаны в `stand-log.md`.
- Порт 5050 (Container Registry) **не** включался — он появится в главе про реестры образов (HARBOR01).

### Откат

VMware Workstation → правый клик по GIT01 → **Snapshot Manager** → выбрать `before-devops-017` → **Revert**. То же для UBU01. Учтите: откат GIT01 уничтожит всё, что вы закоммитили в GitLab после снапшота — если код нужен, сначала выгрузите его (`git clone --mirror` на UBU01 или архив в `/home/adm.student`). После отката обновите `stand-log.md` строкой «GitLab откатан к состоянию до урока 17».

## 6. Ошибки новичков и безопасность

- **Ставить GitLab на 2 vCPU / 2 ГБ RAM.** → OOM-killer убивает Puma, сайт отдаёт 502, а `gitlab-ctl status` мигает `down`. → Минимум 4 vCPU и 8 ГБ RAM (документация GitLab прямо говорит про 8 ГБ как рекомендацию для небольших инсталляций; 4 ГБ — только с swap и молитвами) [ПРОВЕРИТЬ актуальные цифры для вашей версии].
- **Менять `external_url` «на живую» и не делать `reconfigure`.** → ссылки для клонирования, CI-переменные и OAuth-редиректы указывают на старый адрес, часть интеграций ломается. → Менять адрес осознанно, сразу `gitlab-ctl reconfigure`, а в проектах — `git remote set-url`.
- **Считать `gitlab-backup create` полным бэкапом.** → при восстановлении на новом сервере GitLab не может расшифровать CI-переменные и токены, потому что нет `gitlab-secrets.json`. → Всегда пара: `gitlab-backup create` + `gitlab-ctl backup-etc`, обе копии — за пределы ВМ.
- **Ни разу не восстанавливать бэкап.** → в час X выясняется, что архив битый или версия не совпадает. → Раз в квартал — тестовое восстановление на отдельной ВМ той же версии.
- **`shell`-executor на самом сервере GitLab.** → любой, кто может запушить в репозиторий с пайплайном, выполняет код от пользователя `gitlab-runner` на продовой машине. → Docker-исполнитель на отдельной ВМ, по возможности — в отдельном сетевом сегменте.
- **Раздавать Owner и Maintainer «чтобы не мешало».** → один случайный force-push в `main` или удаление проекта. → Developer по умолчанию, Maintainer — по заявке, `main` защищён, удаление проектов отложено (Settings → Advanced → Delay project deletion).
- **Игнорировать диск `/var/opt/gitlab`.** → репозитории и артефакты съедают место, GitLab падает на записали в БД. → Мониторинг через MON01 (глава про Zabbix), алерт на 80%, `gitlab_rails['backup_keep_time']`.
- **Секреты в `.gitlab-ci.yml`.** → пароли к БД и токены утекают в историю Git. → CI/CD Variables с флагом Masked + Protected, интеграция с Vault, отдельно — переменные уровня окружения.

**Безопасность.** В лабе мы поднимаем GitLab по HTTP и оставляем вход по паролю `root` — в боевой среде это недопустимо: только HTTPS с валидным сертификатом (внутренний CA из урока 11), вход через LDAP/AD или SSO, обязательный 2FA для админов, регулярные обновления (`apt upgrade gitlab-ce` — GitLab выпускает security-релизы ежемесячно, второго вторника [ПРОВЕРИТЬ]), SSH-доступ к серверу только по ключам и с bastion (урок 12). Публично доступный GitLab без обновлений — классическая точка входа: CVE в Rails-стеке эксплуатируются ботнетами в течение суток после публикации.

**Про РФ 2026.** GitLab **CE** распространяется под лицензией MIT (Community Edition) — ставить её легально и бесплатно можно; **EE** — коммерческая подписка, и с 2022 года GitLab Inc. ограничил продажи и поддержку в РФ [ПРОВЕРИТЬ актуальный статус на 2026]. Практические следствия: пакеты CE доступны на `packages.gitlab.com`, но из РФ возможны проблемы с доступом/скоростью — держите локальное зеркало или собирайте из исходников; официальной вендорской поддержки по контракту не будет. Если компания требует ПО из реестра российского ПО или поддержку по договору — смотрите GitFlic, GitVerse, а также Gitea/Forgejo (следующий урок 18). В реестре российского ПО GitLab CE, насколько мне известно, не значится — [ПРОВЕРИТЬ] перед разговором с закупками.

## 7. Вопросы на собеседовании

**1. Из каких компонентов состоит GitLab, установленный omnibus-пакетом, и что произойдёт, если упадёт Gitaly?**

Ответ: nginx + Workhorse (вход), Puma (Rails-приложение и API), Sidekiq (фоновые задачи: пайплайны, письма, бэкапы), Gitaly (единственный владелец git-репозиториев, общение по gRPC), PostgreSQL (метаданные), Redis (очереди и кеш), плюс опциональные Prometheus, Registry, Pages. Всё управляется `gitlab-ctl`. Падение Gitaly означает, что веб-интерфейс может открываться (метаданные в PostgreSQL живы), но любой `git clone/push`, отображение файлов и запуск пайплайнов ломаются — репозитории недоступны. Лечится `gitlab-ctl status gitaly` → `gitlab-ctl tail gitaly` → проверка места на диске и прав на `/var/opt/gitlab/git-data`.
*Что хочет услышать интервьюер:* понимание, что GitLab — это связка сервисов, а не монолит, и что Gitaly — точка отказа для всей git-функциональности.
*Красный флаг:* «GitLab — это просто Rails-приложение и папка с репозиториями».

**2. Как вы делаете и проверяете бэкап GitLab?**

Ответ: две части. Данные — `gitlab-backup create` (БД, репозитории, upload-ы, артефакты) в `/var/opt/gitlab/backups`; конфиг и секреты — `gitlab-ctl backup-etc` в `/etc/gitlab/config_backup`. Обе копии вывозятся с сервера (S3, отдельный NAS), retention настраивается через `gitlab_rails['backup_keep_time']`. Восстановление: остановить `puma` и `sidekiq`, `gitlab-backup restore BACKUP=<имя без суффикса>`, `gitlab-ctl reconfigure`, `restart`, проверка `gitlab-rake gitlab:check SANITIZE=true`. Обязательно регулярное тестовое восстановление на отдельной ВМ той же версии — иначе бэкап не бэкап. Для больших инсталляций — `backup_skip` для registry/artifacts и снапшоты диска как дополнение, не замена.
*Что хочет услышать интервьюер:* что вы помните про секреты и про проверку восстановления.
*Красный флаг:* «делаю `gitlab-rake gitlab:backup:create` по крону и всё» — без secrets и без теста восстановления.

**3. Чем отличаются shell, docker и kubernetes executor у GitLab Runner и когда какой брать?**

Ответ: **shell** — job выполняется прямо на хосте от пользователя `gitlab-runner`, максимально просто и быстро, но нет изоляции: любой пуш с `.gitlab-ci.yml` = выполнение кода на этой машине. **docker** — каждый job в свежем контейнере, окружение воспроизводимо, легко кешировать через volume `/cache`; нужно ставить Docker, следить за диском под образы, а `privileged = false` обязателен, если не нужен DinD. **kubernetes** — job-ы как поды в кластере, масштабирование и эфемерность из коробки, но нужен кластер и понимание RBAC/ResourceQuota. Правило: shell — только на одноразовой изолированной ВМ, docker — дефолт для команды, kubernetes — когда runners должны масштабироваться под нагрузку.
*Что хочет услышать интервьюер:* что вы понимаете риск shell-executor и осознанно выбираете изоляцию.
*Красный флаг:* «shell, потому что проще, у нас же приватный GitLab».

**4. Developer и Maintainer — в чём разница и как вы защищаете `main`?**

Ответ: Developer (30) пушит в незащищённые ветки, создаёт MR, запускает пайплайны, но не меняет настройки проекта, не управляет участниками и не пушит в защищённые ветки. Maintainer (40) — настройки репозитория, protected branches, CI/CD variables, runner-ы проекта. Owner (50) — только на уровне группы, управляет самой группой и её удалением. `main` защищается через Protected branches: push/merge — Maintainers, опционально «Allowed to force push: No», обязательные апрувы, «Code owner approval», плюс запрет merge при красном пайплайне («Pipelines must succeed»). Дальше — CODEOWNERS для обязательного ревью, как в уроке 16.
*Что хочет услышать интервьюер:* чёткое разделение прав + защита ветки как техническая мера, а не «мы договорились».
*Красный флаг:* «все Developer, а в main не пушим по договорённости».

**5. Почему в России в 2026 году компания может выбрать не GitLab, и что вы предложите?**

Ответ: GitLab CE бесплатен (MIT) и ставится легально, но коммерческая поддержка и EE-подписка из РФ фактически недоступны, обновления безопасности приходится тянуть самому, а закупки иногда требуют ПО из реестра российского ПО. Если нужна поддержка по контракту и соответствие требованиям импортозамещения — смотрим GitFlic, GitVerse, а также Gitea/Forgejo как лёгкую альтернативу с самостоятельным хостингом (урок 18). Критерии выбора: набор функций (нужен ли встроенный CI, registry, wiki), требования ИБ и 152-ФЗ, стоимость владения, наличие интеграторов, наличие экспорта данных (не запирать себя). Разумный ход — пилот на двух решениях на одинаковом наборе сценариев: репозитории + MR + пайплайн + бэкап/восстановление.
*Что хочет услышать интервьюер:* инженерную оценку вместо политики, знание альтернатив и понимание рисков отсутствия вендорской поддержки.
*Красный флаг:* «GitLab всё равно лучше, остальное несерьёзно» или «поставим пиратский EE».

## 8. Источники (официальная документация)

- **docs.gitlab.com** — раздел *Install GitLab* (подразделы *Linux package / Omnibus*, *Installation requirements*, *Configuration options*), раздел *Administration* → *Back up and restore GitLab*, *Repository storage / Gitaly*, раздел *Runner* → *Registering runners*, раздел *User management* → *Permissions and roles*. Поисковые фразы: `gitlab omnibus installation requirements`, `gitlab backup restore rake`, `gitlab runner register authentication token`.
- **about.gitlab.com** — раздел *Install* / *Pricing* (сравнение CE и EE), страница про лицензирование. Поиск: `gitlab community edition license MIT`.
- **packages.gitlab.com** — репозитории пакетов `gitlab/gitlab-ce` и `runner/gitlab-runner`; здесь же скрипты подключения репозитория.
- **ubuntu.com** — документация по Ubuntu Server 24.04 (установка системы, netplan, ufw). Поиск: `ubuntu server 24.04 netplan static ip`.
- **docs.docker.com** — *Install Docker Engine on Ubuntu*, раздел про группу `docker` и права доступа к сокету. Поиск: `docker engine install ubuntu post-installation steps`.
- **gitflic.ru**, **gitverse.ru**, **forgejo.org**, **gitea.com** — официальные сайты альтернатив для сравнения (детали — в уроке 18). Наличие и условия редакций проверяйте на самих сайтах, они меняются: [ПРОВЕРИТЬ].
- Статус присутствия GitLab в РФ и в реестре российского ПО — проверять по реестру (reestr.digital.gov.ru [ПРОВЕРИТЬ домен]) и по официальным заявлениям вендора, а не по статьям в блогах: [ПРОВЕРИТЬ].
