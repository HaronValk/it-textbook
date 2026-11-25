# CI/CD на практике: GitLab CI / GitHub Actions, пайплайн сборки и деплоя

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

```text
Пользователь: ivanov
ВМ: WIN11-CL01
Что написал: «Открываю внутренний сайт vektor-site — вижу старую версию, формы заявки нет.
Вчера писали, что выложили новую. Коллеги тоже видят старую. Пароль в GitLab не подходит,
пайплайн не запускается. Срочно, у нас отчёт».
Срочность: Высокая
```

Для L1 это выглядело бы как «сайт не обновился». Для L2 это сразу несколько гипотез: не запустился пайплайн, упал деплой, деплой был ручной и его не нажали, nginx на WEB01 смотрит не в ту папку, на сервере остался старый артефакт, а может, и правда проблема с доступом к GitLab.

CI/CD — это не «инструмент разработчиков». Это способ доставлять изменения на серверы предсказуемо: один и тот же набор шагов, из версионированного файла, с логами и возможностью отката. Инженер поддержки L2 постоянно сталкивается с пайплайнами: читает логи упавших job’ов, проверяет runner’ы, чинит SSH-доступы, объясняет пользователям, почему «код есть, а сайт старый», и участвует в изменениях по ITIL. После этой главы ты должен уметь поднять GitLab CE с runner’ом, написать простой пайплайн сборки, теста и деплоя на nginx, прочитать лог упавшей задачи, откатить деплой и не положить секреты в репозиторий.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

CI (Continuous Integration) — непрерывная интеграция: каждый коммит автоматически собирается и проверяется. CD — Continuous Delivery/Deployment: тот же артефакт автоматически или по кнопке доставляется в среду. Пайплайн — это декларативное описание стадий и задач в файле `.gitlab-ci.yml` или `.github/workflows/*.yml`, который лежит рядом с кодом и версионируется вместе с ним.

Ключевые сущности GitLab CI:

- **pipeline** — запуск по коммиту, тегу, расписанию или кнопке;
- **stage** — этап: build, test, deploy; задачи внутри этапа идут параллельно, этапы — последовательно;
- **job** — конкретная задача, выполняется runner’ом;
- **runner** — агент, который забирает задачу у GitLab и выполняет её; сам инициирует соединение, входящий порт ему не нужен;
- **artifact** — файлы, которые job передаёт следующим этапам и сохраняет после завершения;
- **cache** — ускоряет повторные сборки, но не заменяет artifact;
- **environment** — логическая среда (staging, production), к которой привязан деплой;
- **variable** — переменная пайплайна; секреты должны быть masked/protected, а не в YAML.

Схема стенда:

```text
Разработчик / админ
       |
       | git push (HTTPS 80/443 или SSH 22)
       v
+----------------------------+        HTTPS 80/443        +---------------------------+
| GIT01 10.10.10.50          | <--------------------------> | GitLab Runner             |
| GitLab CE: repo, pipeline  |   runner сам опрашивает API  | shell executor на GIT01   |
+----------------------------+                              +-------------+-------------+
                                                                          |
                                                                          | SSH 22 + rsync
                                                                          v
                                                              +---------------------------+
                                                              | WEB01 10.10.20.10         |
                                                              | nginx, /var/www/vektor-site|
                                                              +---------------------------+

VMnet8 10.10.10.0/24  ---  GW01 10.10.10.3 / 10.10.20.1  ---  VMnet10 10.10.20.0/24
```

Где это лежит в общей картине: Git хранит исходники, CI/CD превращает коммит в проверенный артефакт и доставляет его на WEB01. Дальше артефакт может стать Docker-образом (урок 96) и уехать в Kubernetes (урок 97), но принцип тот же. С управлением изменениями (урок 93) пайплайн связан напрямую: успешный деплой — это изменение в CMDB, а упавший — инцидент. С мониторингом (урок 90) и логами (урок 91) — через smoke-тест и алерты после выката.

Протоколы и порты: HTTP/HTTPS — L7, GitLab UI и API; SSH — L7, Git и деплой; TCP — L4; маршрутизация между VMnet8 и VMnet10 — L3 на GW01. DNS (53) нужен для имени `gitlab.corp.vektor.lab`, NTP (123) — чтобы не разъезжались времени и TLS-проверки.

Важное разделение: **CI-сервер не должен иметь прямой доступ к production-хосту под root**. В учебной лабе мы сделаем отдельного пользователя `deploy` и ключ только для rsync в одну папку. В боевой среде — ещё и защищённые ветки, ручное подтверждение, отдельный runner и аудит.

## 3. Аналогия из жизни

Представь типографию. Автор приносит рукопись (коммит). Корректор проверяет орфографию и вёрстку (test), печатник делает сигнальный экземпляр (build), курьер везёт тираж в магазин (deploy). На каждом этапе есть накладная: что за тираж, какая версия, кто принял. Если корректор нашёл ошибку, дальше рукопись не идёт. Если курьер потерял ключ от склада, тираж не выгружают, и магазин остаётся со старой книгой — ровно как WEB01 со старым сайтом.

Аналогия перестаёт работать на откате: бумажный тираж нельзя «развернуть обратно», а в CI/CD предыдущий артефакт можно сохранить и выкатить за минуты — если ты заранее позаботился о версионировании.

## 4. Практика и команды

### 1. Состояние GitLab и его логов

```bash
sudo gitlab-ctl status
sudo gitlab-ctl tail
```

`status` показывает компоненты Omnibus-установки: nginx, puma, sidekiq, postgresql, redis. `tail` — живой хвост логов. Смотреть: все ли `run`, нет ли `down`, какие ошибки сыплются при загрузке страницы.

### 2. Список и проверка runner’ов

```bash
sudo gitlab-runner list
sudo gitlab-runner verify
```

`list` показывает зарегистрированные runner’ы и executor. `verify` проверяет, что runner может связаться с GitLab. В выводе важны `Url`, `Executor`, `Tags` и отсутствие `ERROR`.

### 3. Регистрация runner’а

```bash
sudo gitlab-runner register \
  --non-interactive \
  --url "http://gitlab.corp.vektor.lab" \
  --token "glrt-xxxxxxxxxxxxxxxxxxxx" \
  --executor "shell" \
  --description "gitlab01-shell" \
  --tag-list "vektor,shell"
```

`--token` — authentication token нового runner’а из UI GitLab (Admin Area → CI/CD → Runners → New instance runner). Старый `--registration-token` в новых версиях не используется. `--executor shell` запускает job прямо на хосте от пользователя `gitlab-runner`; для лабы просто, для боевой — небезопасно. Точные пункты меню зависят от версии GitLab — `[ПРОВЕРИТЬ]`.

### 4. Проверка синтаксиса `.gitlab-ci.yml` через API

```bash
curl --request POST \
  --header "PRIVATE-TOKEN: <твой_PAT>" \
  --data-urlencode "content@.gitlab-ci.yml" \
  "http://gitlab.corp.vektor.lab/api/v4/ci/lint"
```

POST на `/api/v4/ci/lint` возвращает JSON с `valid: true/false` и списком ошибок. Удобно, когда не хочется каждый раз коммитить заведомо кривой YAML. PAT создаётся в профиле пользователя, права — `api`.

### 5. История коммитов и теги релизов

```bash
git log --oneline --graph --decorate -10
git tag -a v1.0.0 -m "vektor-site 1.0.0"
git push origin v1.0.0
```

Смотреть, какой коммит ушёл в `main`, есть ли тег на текущем релизе. Тег — точка, к которой можно вернуться. В GitLab по тегу можно запускать отдельный пайплайн.

### 6. Сухой прогон rsync перед деплоем

```bash
rsync -az --delete --dry-run build/ deploy@10.10.20.10:/var/www/vektor-site/
```

`-a` — архивный режим, `-z` — сжатие, `--delete` — удалить на цели то, чего нет в источнике, `--dry-run` — только показать, что будет сделано. Всегда сначала dry-run, особенно с `--delete`.

### 7. Проверка SSH-доступа под пользователем runner’а

```bash
sudo -u gitlab-runner ssh -o BatchMode=yes deploy@10.10.20.10 "ls -la /var/www/vektor-site"
sudo -u gitlab-runner ssh-keyscan -H 10.10.20.10 >> /home/gitlab-runner/.ssh/known_hosts
```

`BatchMode=yes` запрещает интерактивный пароль — так ты сразу увидишь, работает ли ключ. `ssh-keyscan` добавляет host key, иначе job упадёт с `Host key verification failed`.

### 8. Smoke-тест сайта

```bash
curl -fsS -I http://10.10.20.10/
curl -fsS http://10.10.20.10/version.txt
```

`-f` — ошибка HTTP как ошибка curl, `-s` — тихо, `-S` — показать ошибку, `-I` — только заголовки. Смотреть код `200`, `Server: nginx`, и совпадает ли `version.txt` с последним коммитом.

### 9. Логи runner’а

```bash
sudo journalctl -u gitlab-runner -n 100 --no-pager
sudo journalctl -u gitlab-runner -f
```

Здесь видно, как runner взял job, каким пользователем запустил, какие команды выполнял и на чём упал. Это первый источник при ошибках `Permission denied`, `command not found`, `Host key verification failed`.

### 10. Кто слушает порты

```bash
sudo ss -tlnp | grep -E ':(80|443|22)\b'
```

Проверяем, что nginx слушает 80, GitLab — 80/443, SSH — 22. Если порт занят чужим процессом, GitLab не поднимется.

### 11. GitHub Actions: запуски и логи

```bash
gh run list --limit 5
gh run watch
gh run view <run-id> --log-failed
```

`gh` — GitHub CLI. `run list` показывает статусы workflow, `watch` — живой прогресс, `view --log-failed` — лог только упавших шагов. Для стенда с приватными адресами нужен self-hosted runner: облачный runner GitHub не достучится до `10.10.20.10`.

### 12. Ручной запуск пайплайна через API

```bash
curl --request POST \
  --form token="<trigger-token>" \
  --form ref="main" \
  "http://gitlab.corp.vektor.lab/api/v4/projects/1/trigger/pipeline"
```

Trigger token создаётся в настройках проекта → CI/CD → Pipeline triggers. Удобно для внешних систем: например, после изменения DNS или конфига. Смотреть ответ: `id`, `status`, `web_url`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Нужны:

- **DC01** — AD DS и DNS, домен `corp.vektor.lab`, DNS-зона `corp.vektor.lab`;
- **GIT01** — Ubuntu Server 24.04, `10.10.10.50`, статический IP, DNS `10.10.10.10`, минимум 2 vCPU / 4 ГБ RAM и 4 ГБ swap (лучше 4 vCPU / 8 ГБ). Формально GIT01 в таблице стенда помечен треком DevOps. Урок 98 — это и есть «мост», поэтому здесь мы поднимаем его досрочно; на треке DevOps он уже будет готов. Это исключение фиксируем в `stand-log.md`;
- **WEB01** — Ubuntu Server с nginx, `10.10.20.10`, доступ по SSH;
- **GW01** — маршрутизатор между VMnet8 и VMnet10, `10.10.10.3` / `10.10.20.1`, forwarding и NAT уже настроены в сетевом модуле L2. Проверка: с GIT01 `ping 10.10.20.10` проходит. Если нет — сначала вернись к лабораторной по GW01, не правь nftables наугад;
- локальный `git` на твоём ПК или на GIT01.

Если GitLab CE ещё не поддерживает Ubuntu 24.04 в твоей версии — используй Ubuntu 22.04 LTS `[ПРОВЕРИТЬ]`. Дистрибутив GitLab CE свободно скачивается, лицензия Omnibus CE бесплатна.

### Снапшот

В VMware Workstation для каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-l2-098`. Сделай на DC01, GIT01, WEB01 и GW01. Если предпочитаешь CLI, `vmrun` есть в составе Workstation, но путь к нему зависит от версии `[ПРОВЕРИТЬ]`; GUI надёжнее.

### Шаги

1. **DC01: DNS-запись.** DNS Manager → Forward Lookup Zones → `corp.vektor.lab` → New Host (A) → имя `gitlab`, IP `10.10.10.50`. С GIT01 проверь: `nslookup gitlab.corp.vektor.lab 10.10.10.10`. Ожидаемо: `Address: 10.10.10.50`.

2. **GIT01: установка GitLab CE.**

   ```bash
   sudo apt update
   sudo apt install -y curl openssh-server ca-certificates tzdata
   curl https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | sudo bash
   sudo EXTERNAL_URL="http://gitlab.corp.vektor.lab" apt install -y gitlab-ce
   sudo gitlab-ctl reconfigure
   ```

   Ожидаемо: `gitlab-ctl status` показывает запущенные компоненты. Первый вход — `http://gitlab.corp.vektor.lab`, логин `root`, пароль из `sudo cat /etc/gitlab/initial_root_password` (файл живёт 24 часа, потом удаляется). Смени пароль сразу.

3. **GIT01: группа и проект.** В UI создай группу `vektor` и проект `vektor-site` (visibility Private, default branch `main`). Ожидаемо: пустой репозиторий с URL `http://gitlab.corp.vektor.lab/vektor/vektor-site.git`.

4. **WEB01: nginx и пользователь деплоя.**

   ```bash
   sudo apt update && sudo apt install -y nginx rsync
   sudo adduser deploy
   sudo usermod -aG www-data deploy
   sudo mkdir -p /var/www/vektor-site
   sudo chown -R deploy:www-data /var/www/vektor-site
   sudo chmod -R 775 /var/www/vektor-site
   ```

   Создай `/etc/nginx/sites-available/vektor-site`:

   ```nginx
   server {
       listen 80 default_server;
       server_name _;
       root /var/www/vektor-site;
       index index.html;
   }
   ```

   ```bash
   sudo ln -s /etc/nginx/sites-available/vektor-site /etc/nginx/sites-enabled/vektor-site
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   sudo ufw allow 80/tcp && sudo ufw allow 22/tcp
   ```

   Ожидаемо: `curl -I http://10.10.20.10/` отдаёт `200` или `403`, но nginx отвечает.

5. **GIT01: установка и регистрация runner’а.**

   ```bash
   curl -L "https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh" | sudo bash
   sudo apt install -y gitlab-runner
   ```

   В GitLab: Admin Area → CI/CD → Runners → New instance runner, скопируй token. Затем:

   ```bash
   sudo gitlab-runner register \
     --non-interactive \
     --url "http://gitlab.corp.vektor.lab" \
     --token "glrt-xxxxxxxx" \
     --executor "shell" \
     --description "gitlab01-shell" \
     --tag-list "vektor,shell"
   ```

   Ожидаемо: `gitlab-runner list` показывает `gitlab01-shell`, в UI runner зелёный.

6. **GIT01: SSH-ключ для деплоя.**

   ```bash
   sudo -u gitlab-runner ssh-keygen -t ed25519 -N "" -f /home/gitlab-runner/.ssh/id_ed25519
   sudo -u gitlab-runner ssh-copy-id deploy@10.10.20.10
   sudo -u gitlab-runner ssh-keyscan -H 10.10.20.10 >> /home/gitlab-runner/.ssh/known_hosts
   sudo -u gitlab-runner ssh -o BatchMode=yes deploy@10.10.20.10 "echo OK"
   ```

   Ожидаемо: `OK` без запроса пароля. Пароль `deploy` — учебный `Lab#2026-vektor`; в реальной среде так нельзя, там отдельный ключ и запрет паролей.

7. **Репозиторий: файлы сайта.**

   ```bash
   git clone http://gitlab.corp.vektor.lab/vektor/vektor-site.git
   cd vektor-site
   mkdir -p site
   cat > site/index.html <<'EOF'
   <!doctype html>
   <html><body><h1>Vektor site</h1><p>Учебная версия</p></body></html>
   EOF
   git add . && git commit -m "initial site" && git push origin main
   ```

   Если GitLab не принимает пароль root по HTTP, создай Personal Access Token с правом `write_repository` и используй его вместо пароля. Ожидаемо: файлы видны в UI.

8. **GIT01: `.gitlab-ci.yml`.**

   ```yaml
   stages:
     - build
     - test
     - deploy

   default:
     tags:
       - vektor

   variables:
     DEPLOY_HOST: "deploy@10.10.20.10"
     DEPLOY_PATH: "/var/www/vektor-site"

   build:
     stage: build
     script:
       - rm -rf build
       - mkdir -p build
       - cp -r site/. build/
       - echo "$CI_COMMIT_SHORT_SHA" > build/version.txt
     artifacts:
       paths:
         - build/
       expire_in: 1 week

   test:
     stage: test
     needs: ["build"]
     script:
       - test -f build/index.html
       - grep -q "Vektor" build/index.html
       - test -s build/version.txt

   deploy:
     stage: deploy
     needs: ["build", "test"]
     rules:
       - if: '$CI_COMMIT_BRANCH == "main"'
         when: manual
     environment:
       name: production
       url: http://10.10.20.10
     script:
       - rsync -az --delete build/ "$DEPLOY_HOST:$DEPLOY_PATH/"
       - curl -fsS http://10.10.20.10/version.txt
   ```

   ```bash
   git add .gitlab-ci.yml && git commit -m "add pipeline" && git push origin main
   ```

   Ожидаемо: pipeline `build` → `test` зелёные, `deploy` в статусе manual.

9. **Запуск деплоя.** В GitLab: Build → Pipelines → последний pipeline → job `deploy` → Run. Ожидаемо: job зелёный, `curl http://10.10.20.10/version.txt` возвращает short SHA коммита.

10. **Проверка изменения.** Поменяй текст в `site/index.html`, закоммить в `main`, дождись `build`/`test`, снова нажми `deploy`. Ожидаемо: на сайте новый текст, `version.txt` совпадает с новым SHA.

### Проверка

- [ ] `nslookup gitlab.corp.vektor.lab 10.10.10.10` возвращает `10.10.10.50`.
- [ ] `sudo gitlab-ctl status` — все компоненты `run`.
- [ ] `sudo gitlab-runner list` показывает runner `gitlab01-shell` с тегом `vektor`.
- [ ] `sudo -u gitlab-runner ssh -o BatchMode=yes deploy@10.10.20.10 "echo OK"` возвращает `OK`.
- [ ] Pipeline на `main` проходит `build` и `test`.
- [ ] `deploy` запускается вручную и завершается успешно.
- [ ] `curl http://10.10.20.10/version.txt` совпадает с `git rev-parse --short HEAD`.
- [ ] В `stand-log.md` записаны GIT01, WEB01, DNS-запись и проект.

### Сломай и почини

```text
Пользователь: petrova
ВМ: WIN10-CL02
Что написал: «Сайт vektor-site показывает старую версию, хотя в GitLab коммит новый.
Пайплайн вроде зелёный. Проверьте, пожалуйста».
Срочность: Средняя
```

**Воспроизведение.** На WEB01 убери authorized_keys у `deploy`:

```bash
sudo mv /home/deploy/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys.bak
```

Внеси изменение в `site/index.html`, закоммить в `main`, запусти pipeline и нажми `deploy`. Job упадёт на `rsync` с `Permission denied (publickey)`.

**Диагностика по порядку:**

1. В GitLab открой pipeline: `build` и `test` зелёные, `deploy` красный. Прочитай лог job’а — там `Permission denied (publickey)`.
2. На WEB01 проверь, что сайт действительно старый: `curl -fsS http://10.10.20.10/version.txt` и `ls -la /var/www/vektor-site`.
3. С GIT01 проверь ключ в неинтерактивном режиме: `sudo -u gitlab-runner ssh -o BatchMode=yes deploy@10.10.20.10 "echo OK"`. Ожидаемо: отказ.
4. Проверь права: `/home/gitlab-runner/.ssh` — `700`, ключ — `600`; на WEB01 `/home/deploy/.ssh` — `700`.
5. На WEB01 посмотри, на месте ли `authorized_keys`: `ls -la /home/deploy/.ssh/`. Видишь только `.bak` — причина найдена.
6. Проверь, не менялся ли ключ: `sudo -u gitlab-runner ssh-keygen -lf /home/gitlab-runner/.ssh/id_ed25519.pub` и сравни с содержимым `authorized_keys.bak`.

**Решение.** Верни файл на место, выставь права и владельца, повтори job:

```bash
sudo mv /home/deploy/.ssh/authorized_keys.bak /home/deploy/.ssh/authorized_keys
sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

В GitLab открой упавший job → Retry. Проверь `curl` и `version.txt`.

**Ответ пользователю:** «Проблема была не в сайте, а в ключе деплоя: пайплайн не мог скопировать новую версию на WEB01. Ключ восстановлен, деплой выполнен, сейчас на сайте версия <SHA>. Добавили проверку SSH перед деплоем, чтобы такое ловилось раньше».

**Закрытие заявки:** категория «CI/CD», решение «восстановлен SSH-ключ deploy, выполнен повторный деплой», ссылка на pipeline и коммит. Если в компании есть change-management, оформи как изменение с планом отката — пусть и учебное.

### Что изменилось на стенде

- **GIT01**: установлен GitLab CE, внешний URL `http://gitlab.corp.vektor.lab`, создана группа `vektor`, проект `vektor-site`, runner `gitlab01-shell` (shell executor, тег `vektor`), SSH-ключ пользователя `gitlab-runner`.
- **DC01**: в зоне `corp.vektor.lab` добавлена A-запись `gitlab` → `10.10.10.50`.
- **WEB01**: установлен nginx, создан пользователь `deploy`, каталог `/var/www/vektor-site`, сайт `vektor-site`, в `authorized_keys` добавлен ключ runner’а.
- **GW01**: конфигурация не менялась, использовалась существующая маршрутизация VMnet8 ↔ VMnet10.
- **Проект**: файлы `site/index.html`, `.gitlab-ci.yml`, pipeline с этапами build/test/deploy, artifact `build/`, ручной деплой на `main`.
- **stand-log.md**: зафиксируй, что GIT01 поднят досрочно как мост к DevOps, иначе на треке DevOps будет путаница.

### Откат

Если всё сломалось настолько, что проще вернуть стенд: выключи ВМ и в VMware Workstation выбери **VM → Snapshot → Revert to Snapshot `before-l2-098`** на WEB01, GIT01, DC01 и GW01. Порядок не важен, но после отката проверь, что DNS-запись и SSH-ключи соответствуют состоянию до лабораторной. Если сломан только пайплайн, откат не нужен: отмени коммит в GitLab (`git revert`) или перезапусти предыдущий успешный artifact. После любого отката обнови `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Секреты в `.gitlab-ci.yml` или в репозитории.** Пароль от SSH, токен GitLab, ключ доступа оказываются в истории Git навсегда. Правильно: masked/protected CI/CD variables, файловые переменные, Vault или OIDC — подробнее в уроке 102.
2. **Shell executor на общем GitLab-сервере.** Job’ы выполняются от `gitlab-runner` на той же машине, где лежит GitLab; один вредный job может прочитать чужие файлы и ключи. В лабе это допустимо, в бою — отдельный runner, Docker executor, изоляция.
3. **Деплой с любой ветки.** Любой коммит в feature-ветку уезжает на WEB01. Правильно: protected branch `main`, `rules`/`only`, environment с ручным подтверждением.
4. **Нет артефакта и версии.** Собрали «на месте», выкатили, а что именно — непонятно. Правильно: immutable artifact, `version.txt`/тег, хранение и ротация.
5. **`rsync --delete` без dry-run.** Удалили на сервере файлы, которых не было в сборке. Сначала `--dry-run`, потом реальный запуск; для критичных сред — бэкап.
6. **Ручные правки на WEB01.** Поправили конфиг «быстро» — и он разошёлся с репозиторием. Правильно: изменения только через пайплайн/IaC, ручное — через change-management.
7. **Runner под root или с широким sudo.** Компрометация runner’а = компрометация хоста. Отдельный пользователь, минимум прав, только нужные команды.
8. **Игнорирование упавших job’ов и слепой Retry.** Ошибка `Permission denied` не исчезнет от перезапуска. Читай лог, проверяй ключи, права, DNS, маршрут.

### Безопасность

В боевой среде нельзя: хранить пароли в YAML, давать runner’у root, открывать SSH для всего интернета, использовать один ключ на все серверы, деплоить без ревью и без возможности отката. Для учебного стенда пароль `Lab#2026-vektor` допустим только потому, что стенд изолирован. В реальной работе SSH-ключи должны быть персональными, с ограничением `from=`/`command=` там, где возможно, а доступ к production — через защищённые среды и аудит. Отдельно помни про цепочку поставок: образы и зависимости тоже надо проверять и подписывать (урок 102).

## 7. Вопросы на собеседовании

**1. Чем CI отличается от CD и чем пайплайн отличается от простого скрипта?**

CI — это автоматическая сборка и проверка каждого изменения, CD — доставка проверенного артефакта в среду. Пайплайн — версионированное декларативное описание стадий и условий, а не набор команд в голове админа: его можно прочитать, повторить, сравнить и откатить. Скрипт тоже автоматизирует, но пайплайн даёт статусы, артефакты, окружения и аудит.

*Что хочет услышать интервьюер:* понимание воспроизводимости и того, что CI/CD — часть процесса изменения, а не «кнопка деплоя».  
*Красный флаг:* «CI — это Jenkins, CD — это Docker» или «пайплайн нужен, чтобы разработчики не звонили».

**2. Что такое runner и почему shell executor считается опасным?**

Runner — агент, который забирает job у GitLab и выполняет его. Shell executor запускает команды прямо на хосте от пользователя `gitlab-runner`, без контейнерной изоляции. Если в пайплайн попадёт вредоносный код или секрет, он получит доступ к файловой системе, кэшу и ключам на этом хосте. В лабе удобно, в продакшене — Docker executor, отдельная машина, минимальные права.

*Что хочет услышать интервьюер:* понимание модели угроз и изоляции.  
*Красный флаг:* «Runner — это сам GitLab» или «shell безопасен, потому что репозиторий приватный».

**3. Как безопасно хранить пароли и ключи для пайплайна?**

Только не в репозитории и не в открытом YAML. CI/CD variables с маской и защитой, отдельные токены с минимальными правами, файловые переменные для ключей, Vault/секрет-хранилище, короткоживущие токены через OIDC. Секреты не печатать в логах, ротировать, не переиспользовать между средами.

*Что хочет услышать интервьюер:* «секрет не в коде» и «минимальные права».  
*Красный флаг:* «Положу в `.gitlab-ci.yml`, репозиторий же приватный».

**4. Пайплайн зелёный, а на сайте старая версия. Твои действия?**

Проверяю, тот ли pipeline и тот ли коммит; не был ли `deploy` ручным и не пропущен ли; смотрю лог деплоя и артефакт; проверяю `version.txt` на WEB01, root у nginx, права, `curl` с нужным Host; смотрю, нет ли балансировщика/кэша/DNS. Если деплой был, но файлы старые — ищу, куда именно писал rsync. Только потом перезапускаю.

*Что хочет услышать интервьюер:* системную диагностику, а не «перезапущу всё».  
*Красный флаг:* «Сразу сделаю Retry всех job’ов» или «Это точно кэш, почищу».

**5. Как откатить деплой?**

Если артефакты версионированы — развернуть предыдущий артефакт/тег. Если blue-green — переключить трафик обратно. Если canary — убрать новую версию из балансировки. `git revert` откатывает код, но не данные и не состояние сервера; миграции БД должны быть обратно совместимыми. Откат — это заранее прописанная процедура, а не импровизация в инциденте.

*Что хочет услышать интервьюер:* понимание immutable-артефактов и того, что откат кодом ≠ откат системы.  
*Красный флаг:* «Сделаю `git revert` — и всё само вернётся».

## 8. Источники (официальная документация)

- **GitLab Docs** — `docs.gitlab.com`. Разделы: «GitLab CI/CD pipeline configuration reference», «Registering runners», «CI/CD variables», «Shell executor». Поиск: `gitlab ci cd yaml reference`, `gitlab runner register authentication token`.
- **GitHub Docs** — `docs.github.com`. Разделы: «Understanding GitHub Actions», «Workflow syntax for GitHub Actions», «Self-hosted runners». Поиск: `github actions workflow syntax`, `github actions self-hosted runner`.
- **nginx documentation** — `nginx.org`. Раздел «Beginner’s Guide», настройка `server`/`root`. Поиск: `nginx beginner guide server root`.
- **OpenSSH manual pages** — `openssh.com`. `ssh-keygen`, `ssh-copy-id`, `sshd_config`, `authorized_keys`. Поиск: `openssh ssh-keygen manual`, `authorized_keys file format`.
- **rsync** — `rsync.samba.org`. Man page, опции `-a`, `-z`, `--delete`, `--dry-run`. Поиск: `rsync man page delete dry run`.
- **The Twelve-Factor App** — `12factor.net`. Раздел «Build, release, run». Поиск: `twelve factor build release run`.
- **GitLab Runner Docs** — `docs.gitlab.com/runner/`. Раздел «Executors» `[ПРОВЕРИТЬ актуальные названия разделов для твоей версии]`. Поиск: `gitlab runner executors shell docker`.
