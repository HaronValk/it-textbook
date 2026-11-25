# Code review и качество: pull/merge request, линтеры, pre-commit, conventional commits, CODEOWNERS

## 1. Введение (зачем это нужно в реальной работе)

Тикет в трекере «Вектора»:

> **Пользователь:** sidorov (Алексей Сидоров, склад — он же пишет у нас скрипты интеграции с транспортной компанией)
> **ВМ:** GIT01 / WEB01
> **Что написал:** «Вчера залил в main правку nginx-конфига для WEB01, сайт в DMZ не поднялся. В итоге я там в комментарии пароль от БД оставил и табов наставил. Можно сделать так, чтобы такое больше не долетало до main? А то я же не один такой»
> **Срочность:** высокая

Вот это и есть будни DevOps: код пишут несколько человек (и ты в их числе), а падает прод — один на всех. Code review, линтеры и хуки придуманы не «для галочки», а чтобы ловить такие вещи за секунды до того, как они станут инцидентом.

После этой главы ты должен уметь: объяснить, чем pull request отличается от merge request (и почему это одно и то же); настроить pre-commit так, чтобы он проверял файлы до создания коммита; заставить Git отклонять коммиты с сообщением вида «fix»; описать в `CODEOWNERS`, кто обязан ревьюить какие папки; и понимать, что клиентские проверки можно обойти флагом `--no-verify`, поэтому нужен ещё и серверный контроль.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Code review** — это чтение чужого диффа человеком до слияния в основную ветку. Технически ничего магического: у тебя есть ветка в общем репозитории, у ревьюера — тот же репозиторий и `git diff main...твоя-ветка`.

**Pull request (PR)** — термин GitHub; **merge request (MR)** — термин GitLab и Gitea. Суть одна: «я запушил ветку, посмотрите и разрешите влить». Форж (GitHub/GitLab/Gitea) — это просто веб-обёртка над bare-репозиторием: она хранит метаданные (кто открыл, кто одобрил, какие проверки прошли) в своей БД, а сами объекты Git остаются обычными объектами Git. Поэтому отсутствие форжа не отменяет code review — оно только делает его неудобнее (почтой или руками).

**Защищённая ветка (protected branch)** — настройка «в main пушим не все и не напрямую». Кнопка Merge в веб-интерфейсе тоже не что иное, как `git merge` + push с правами мейнтейнера.

**Хуки (hooks)** — скрипты, которые Git запускает в определённые моменты.

| Тип хука | Где живёт | Что может |
|---|---|---|
| `pre-commit`, `commit-msg`, `pre-push` | на машине автора, в `.git/hooks/` | заблокировать коммит/пуш локально |
| `pre-receive`, `update`, `post-receive` | на сервере, в `hooks/` bare-репозитория | отклонить пуш от любого клиента |

Ключевая мысль: клиентские хуки — удобство и скорость обратной связи, **не** линия обороны (их обходят флагом `--no-verify` и отключают). Линия обороны — серверные хуки и CI.

**pre-commit** (фреймворк, ставится через pip/Python) — стандарт де-факто для клиентских проверок. Он берёт конфиг `.pre-commit-config.yaml`, под каждый хук создаёт отдельное окружение в `~/.cache/pre-commit`, ставит нужную версию репозитория с хуком (пин `rev`) и запускает только на изменённых файлах. Отсюда две вещи, которые надо запомнить: `pre-commit install` **не** распространяется через `git clone`, и на CI его гоняют командой `pre-commit run --all-files`.

**Линтеры** — статический анализ: `shellcheck` (bash), `yamllint`/`check-yaml` (YAML), `hadolint` (Dockerfile), `nginx -t` (синтаксис nginx), `gitleaks`/`git-secrets` (поиск секретов). Они проверяют форму, а не смысл — смысл проверяет человек.

**Conventional Commits** — соглашение о сообщениях коммитов: `type(scope): описание`. Типы: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`. Связь с семантическим версионированием (урок 15): `feat` → MINOR, `fix` → PATCH, футер `BREAKING CHANGE:` или `!` после типа → MAJOR. Именно поэтому по истории можно машинно генерировать CHANGELOG и номер релиза.

**CODEOWNERS** — файл, в котором путям в репозитории сопоставлены пользователи/команды, обязанные ревьюить. Работает **только вместе** с защитой ветки: сам файл ничего не запрещает, он лишь говорит форжу, кого автоматически подставить в ревьюеры и с кого требовать approve. На GitHub файл ищется в `.github/`, в корне и в `docs/`; в GitLab — в корне, `docs/` и `.gitlab/` [ПРОВЕРИТЬ актуальный список для вашей версии]. Побеждает последний подходящий шаблон.

Схема процесса:

```text
 АВТОР (UBU01)                 СЕРВЕР (GIT01)                    РЕВЬЮЕР / МЕЙНТЕЙНЕР
 ─────────────                 ──────────────                    ─────────────────────
 git commit
   │ pre-commit   (клиент) ──► «грязный» конфиг не коммитится
   │ commit-msg   (клиент) ──► «fix» не коммитится
   ▼
 git push origin feat/gzip-web01 ──► pre-receive (сервер)
                                        │ в main напрямую — ОТКАЗ
                                        ▼
                                    bare-репо /srv/git/vektor-web.git
                                        │
                                    Merge Request  ◄──── diff, комментарии,
                                        │               CODEOWNERS → нужный
                                        │               ревьюер на путь
                                        ▼
                                    CI: линтеры, тесты
                                        │
                                  approve + merge
                                        ▼
                                      main
```

По слоям OSI тут всё просто: `git@host:path` — это SSH, TCP/22, L7; веб-интерфейс — HTTPS, TCP/443, L7/TLS. Сам Git-протокол (порт 9418) не используем — он без аутентификации.

## 3. Аналогия из жизни

Представь приёмку товара на складе «Вектор». Поставщик привозит машину коробок — это твой коммит. Кладовщик с чек-листом проверяет: коробки целые, накладные заполнены по форме, нет подозрительных пломб — это `pre-commit` и `commit-msg`: тупая формальная проверка, которая не пускает мусор дальше порога. Дальше приёмщик вскрывает коробки и смотрит, тот ли вообще товар и не подсунули ли просрочку — это ревьюер с диффом. Если в коробке ценное или опасное (конфиг nginx, CI-пайплайн), по регламенту расписываться обязан начальник склада — это `CODEOWNERS` и обязательный approve. Накладная должна быть оформлена по единой форме, иначе бухгалтерия не примет — это conventional commits. И только после всех подписей коробки ставятся на полку — merge в main.

Аналогия ломается в одном месте: кладовщик с чек-листом не спорит о смысле, а ревьюер — спорит, и иногда полезнее выслушать его замечание, чем протолкнуть свою «правильную» правку.

## 4. Практика и команды

**1) Коммит по Conventional Commits**

```bash
git switch -c feat/gzip-web01
git add nginx/web01.vektor.conf
git commit -m "feat(nginx): enable gzip for web01 vhost" \
           -m "Ускорение отдачи статики в DMZ. Замер до/после — в docs/gzip.md"
```

Разбор: `type(scope)!: описание`. `feat` — новая функциональность, `(nginx)` — область, `!` перед двоеточием означал бы breaking change. Первая строка до 72 символов, тело — через пустую строку, отдельным `-m`. Футер `Refs: SV-482` тоже допустим.

**2) Проверить историю на соответствие соглашению**

```bash
git log --oneline main..HEAD
git log --pretty=format:'%h %s' -20
```

Что смотреть: читается ли история как changelog. Плохо: `a1b2 fix`, `c3d4 fix2`, `e5f6 asdf`.

**3) Конфиг pre-commit**

```yaml
# .pre-commit-config.yaml
default_install_hook_types: [pre-commit, commit-msg]
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0            # версию проверяйте: pre-commit autoupdate
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-merge-conflict
      - id: check-added-large-files
  - repo: https://github.com/adrienverge/yamllint
    rev: v1.35.1
    hooks:
      - id: yamllint
  - repo: https://github.com/koalaman/shellcheck-precommit
    rev: v0.10.0
    hooks:
      - id: shellcheck
  - repo: local
    hooks:
      - id: no-secrets
        name: пароли и ключи в коммите
        entry: '(?i)(password|passwd|secret|api[_-]?key)\s*[:=]'
        language: pygrep
        types: [text]
      - id: conventional-commit
        name: conventional commit
        entry: scripts/check-commit-msg.sh
        language: script
        stages: [commit-msg]
```

`language: pygrep` — встроенный тип хука pre-commit: берёт регулярку из `entry` и ищет её в файлах. `stages: [commit-msg]` означает, что этот скрипт вызывается на этапе проверки сообщения коммита, а не файлов.

**4) Установка и запуск**

```bash
pipx install pre-commit        # или python3 -m venv .venv && .venv/bin/pip install pre-commit
pre-commit install --install-hooks
pre-commit run --all-files     # так же запускается на CI
pre-commit autoupdate          # поднять rev до свежих версий
```

Типичный вывод:

```text
trim trailing whitespace.................................................Failed
- hook id: trailing-whitespace
- exit code: 1
- files were modified by this hook
```

Что смотреть: `Failed` + `files were modified` — хук сам поправил файл, нужно `git add` и повторить. `Passed` — молча идём дальше.

**5) Свой commit-msg хук**

```bash
#!/usr/bin/env bash
# scripts/check-commit-msg.sh
msg_file="$1"
first_line=$(head -n1 "$msg_file")
pattern='^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9._/-]+\))?!?: .{1,72}$'

if ! printf '%s' "$first_line" | grep -Eq "$pattern"; then
  echo "✖ Не по Conventional Commits: $first_line"
  echo "  Пример: fix(nginx): add missing semicolon in gzip block"
  exit 1
fi
```

Результат на `git commit -m "fix"`:

```text
✖ Не по Conventional Commits: fix
  Пример: fix(nginx): add missing semicolon in gzip block
```

**6) Что видит ревьюер: дифф ветки относительно main**

```bash
git fetch origin
git log --oneline origin/main..origin/feat/gzip-web01
git diff --stat origin/main...origin/feat/gzip-web01
git diff origin/main...origin/feat/gzip-web01 -- nginx/
```

Три точки (`main...ветка`) — сравнение от точки расхождения, а не от верхушки main. Смотреть сначала `--stat` (объём и файлы), потом по файлам. Если дифф на 2000 строк — ревью не будет.

**7) Workflow без форжа: запрос на ревью почтой**

```bash
git request-pull origin/main git@10.10.10.50:/srv/git/vektor-web.git feat/gzip-web01
```

Выдаёт текст вида «The following changes since commit ... are available in the Git repository at ...» — классический способ попросить ревью, когда GitLab ещё не поставлен. Альтернатива — патчи:

```bash
git format-patch origin/main --stdout > 016-gzip.patch   # автор
git am 016-gzip.patch                                    # ревьюер/мейнтейнер
```

**8) Создание MR из терминала (GitLab, пригодится с урока 17)**

```bash
git push -o merge_request.create \
         -o merge_request.target=main \
         -o merge_request.title="feat(nginx): enable gzip for web01" \
         origin feat/gzip-web01
```

Push options — это штатный механизм Git (передаются серверу в переменных `GIT_PUSH_OPTION_*`) и одновременно фича GitLab. Через GUI: **Merge requests → New merge request**, source `feat/gzip-web01`, target `main`.

**9) Шаблон описания MR**

```markdown
<!-- .gitlab/merge_request_templates/default.md -->
## Что и зачем
<!-- одна-две строки, ссылка на тикет: Refs: SV-482 -->

## Как проверял
- [ ] pre-commit run --all-files — зелёный
- [ ] nginx -t на WEB01 — syntax is ok

## Риски и откат
<!-- что сломается, если не сработает; как откатываем -->
```

**10) CODEOWNERS**

```text
# CODEOWNERS — последний подходящий шаблон побеждает
*                          @vektor/infra
/nginx/                    @vektor/infra @adm.student
/.pre-commit-config.yaml   @adm.student
/docs/                     @vektor/docs
```

На GitHub команды пишутся как `@org/team`, на GitLab — `@group/subgroup`. Проверить, что правило сработало, можно только в UI (список ревьюеров у MR) — файл сам ничего не блокирует.

**11) Серверный хук: запрет прямого пуша в main**

```bash
#!/usr/bin/env bash
# /srv/git/vektor-web.git/hooks/pre-receive
# Прямой push в main запрещён. Обход только для мейнтейнера:
#   git push -o merge-approved origin main
PROTECTED="refs/heads/main"
APPROVED="no"

while IFS= read -r opt; do
  [ "$opt" = "merge-approved" ] && APPROVED="yes"
done < <(env | sed -n 's/^GIT_PUSH_OPTION_[0-9]\+=//p')

status=0
while read -r oldrev newrev refname; do
  if [ "$refname" = "$PROTECTED" ] && [ "$APPROVED" != "yes" ]; then
    echo "ОТКАЗ: прямой push в main запрещён (protected branch)."
    echo "1) git push origin <ваша-ветка>  2) получить approve"
    echo "3) мейнтейнеру: git push -o merge-approved origin main"
    status=1
  fi
done

exit $status
```

Не забыть `chmod +x`. `pre-receive` читает со stdin строки `<old-sha> <new-sha> <ref>` и на ненулевом коде выхода отменяет **весь** пуш.

**12) Правка после ревью и повторное ревью**

```bash
git commit --amend -m "feat(nginx): enable gzip for web01 vhost"   # или новый fixup-коммит
git push --force-with-lease origin feat/gzip-web01
git range-diff origin/main...feat/gzip-web01@{1} origin/main...feat/gzip-web01
```

`--force-with-lease` не затрёт чужие изменения, в отличие от `--force`. `range-diff` показывает, **что именно изменилось между двумя версиями ветки** — то, что ревьюер хочет видеть после force-push, вместо повторного чтения всего диффа.

**13) Линтеры руками**

```bash
yamllint .gitlab-ci.yml
shellcheck scripts/check-commit-msg.sh
sudo nginx -t                 # на WEB01: проверка синтаксиса конфига
```

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **GIT01** (10.10.10.50, Ubuntu Server 24.04) — включён, `git` установлен, SSH с UBU01 настроен по ключу (урок 12). GitLab CE здесь **ещё не стоит** — он в уроке 17. Сегодня роль форжа играет bare-репозиторий: всё, что мы делаем руками, в уроке 17 станет кнопками.
- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — рабочая машина «разработчика», есть `python3`, `pipx`, `git`. Работаем под своей обычной учётной записью (дальше — `student`, если вы назвали её иначе, подставляйте своё имя).
- **WEB01** (10.10.20.10, nginx) — источник файла конфига. В этой главе мы его **не меняем** до последнего шага.
- Пройдены уроки 14 (хуки, rebase, `reflog`) и 15 (ветвление, SemVer).

### Снапшот

В VMware Workstation на каждой из ВМ **GIT01**, **UBU01**, **WEB01**:

```text
VM → Snapshot → Take Snapshot… → Name: before-devops-016 → Take Snapshot
```

Или из CLI (если установлен `vmrun`):

```bash
vmrun -T ws snapshot "/path/to/GIT01/GIT01.vmx" before-devops-016
vmrun -T ws snapshot "/path/to/UBU01/UBU01.vmx" before-devops-016
vmrun -T ws snapshot "/path/to/WEB01/WEB01.vmx" before-devops-016
```

### Шаги

1. **GIT01.** Создаём сервисного пользователя и bare-репозиторий.
   ```bash
   sudo adduser --system --group --shell /bin/bash --home /home/git git
   sudo mkdir -p /home/git /srv/git
   sudo chown -R git:git /home/git /srv/git
   sudo -u git git init --bare /srv/git/vektor-web.git
   ```
   Ожидаемо: `Initialized empty Git repository in /srv/git/vektor-web.git/`.

2. **GIT01.** Кладём публичный ключ UBU01 в `authorized_keys` пользователя `git` (на UBU01 ключ уже есть с урока 12, иначе `ssh-keygen -t ed25519 -C "student@ubu01"`, затем `ssh-copy-id -i ~/.ssh/id_ed25519.pub git@10.10.10.50`). Проверка: `ssh git@10.10.10.50` пускает.

3. **UBU01.** Клонируем, настраиваем identity.
   ```bash
   git clone git@10.10.10.50:/srv/git/vektor-web.git
   cd vektor-web
   git config user.name "adm.student"
   git config user.email "adm.student@corp.vektor.lab"
   git config init.defaultBranch main
   ```

4. **UBU01.** Наполняем репозиторий: `mkdir -p nginx docs .gitlab/merge_request_templates scripts`, копируем конфиг с WEB01 `scp student@10.10.20.10:/etc/nginx/sites-available/web01.vektor.conf nginx/`, создаём `README.md`, шаблон MR из п.4.9 и `CODEOWNERS` из п.4.10. Коммит `chore: bootstrap web config repo` и `git push -u origin main`. Это единственный прямой пуш в main за всю главу — серверный хук появится на шаге 8.

5. **UBU01.** Ставим pre-commit и конфиг.
   ```bash
   pipx install pre-commit
   # создаём .pre-commit-config.yaml из п.4.3 и scripts/check-commit-msg.sh из п.4.5
   chmod +x scripts/check-commit-msg.sh
   pre-commit install --install-hooks
   ```
   Ожидаемо: `pre-commit installed at .git/hooks/pre-commit` и `... commit-msg`.

6. **UBU01.** Проверяем, что защита работает. Добавляем в `nginx/web01.vektor.conf` строку `# password: Lab#2026-vektor`, затем `git add . && git commit -m "feat(nginx): add gzip"`. Ожидаемо: хук `пароли и ключи в коммите` → `Failed`, коммит не создан (`git log` не изменился).

7. **UBU01.** Убираем строку с «паролем», пробуем `git commit -m "fix"`. Ожидаемо: `✖ Не по Conventional Commits: fix`. Затем `git commit -m "feat(nginx): add gzip and cache headers for web01"` — проходит. Пуш: `git switch -c feat/gzip-web01 && git push -u origin feat/gzip-web01`.

8. **GIT01.** Ставим серверный хук из п.4.11:
   ```bash
   sudo -u git tee /srv/git/vektor-web.git/hooks/pre-receive >/dev/null <<'EOF'
   ... текст хука ...
   EOF
   sudo chmod +x /srv/git/vektor-web.git/hooks/pre-receive
   ```

9. **UBU01.** Проверяем защиту main:
   ```bash
   git switch main
   echo "test" >> README.md && git commit -am "docs: test direct push"
   git push origin main
   ```
   Ожидаемо: `ОТКАЗ: прямой push в main запрещён (protected branch).` — и это ровно то, что в GitLab делает неактивная кнопка Merge. Возвращаемся: `git reset --hard origin/main`.

10. **UBU01 (ревьюер).** Смотрим дифф как ревьюер: `git fetch`, `git diff --stat origin/main...origin/feat/gzip-web01`, `git diff origin/main...origin/feat/gzip-web01 -- nginx/`, `git request-pull origin/main git@10.10.10.50:/srv/git/vektor-web.git feat/gzip-web01`. Оставляем одно замечание (например: «нет `gzip_types` для `application/json`»).

11. **UBU01 (автор).** Правим и обновляем ветку: `git commit --amend` (или новый коммит), `git push --force-with-lease origin feat/gzip-web01`. Ревьюер проверяет дельту: `git range-diff origin/main...feat/gzip-web01@{1} origin/main...feat/gzip-web01`.

12. **UBU01 (мейнтейнер, после approve).**
    ```bash
    git switch main && git pull
    git merge --no-ff feat/gzip-web01 -m "Merge branch 'feat/gzip-web01'"
    git push -o merge-approved origin main
    ```
    Ожидаемо: пуш проходит, в `git log --graph --oneline -5` виден merge-коммит.

13. **WEB01 (по желанию).** Применяем проверенный конфиг: `sudo cp /etc/nginx/sites-available/web01.vektor.conf /etc/nginx/sites-available/web01.vektor.conf.bak`, копируем новую версию, `sudo nginx -t` → `syntax is ok`, затем `sudo systemctl reload nginx`. Если `nginx -t` ругается — не перезагружаем, откатываем `.bak`.

### Проверка

- [ ] `pre-commit run --all-files` на UBU01 завершается без `Failed`.
- [ ] Коммит со строкой `password: ...` блокируется.
- [ ] Коммит с сообщением `fix` блокируется, с `fix(nginx): ...` — проходит.
- [ ] `git push origin main` отклоняется сервером с текстом про protected branch.
- [ ] Пуш ветки `feat/*` проходит.
- [ ] В репозитории есть `CODEOWNERS` и `.gitlab/merge_request_templates/default.md`.
- [ ] В `main` появился merge-коммит (`--no-ff`), отправленный с `-o merge-approved`.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** sidorov
> **ВМ:** WEB01
> **Что написал:** «Сайт в DMZ отдаёт 500, конфиг я не трогал. Вчера вечером только смержил свою ветку в main»
> **Срочность:** высокая

**Как воспроизвести.** На UBU01 в ветке `feat/broken` убери точку с запятой в `gzip on` внутри конфига и закоммить в обход хука: `git commit --no-verify -m "feat(nginx): tweak gzip"`. Запушь ветку, смержь и запушь в main с `-o merge-approved`, затем примени конфиг на WEB01 (шаг 13). Получишь ровно то, что описал пользователь.

**Диагностика по порядку.**
1. `sudo nginx -t` на WEB01 — покажет файл и строку: `unexpected end of file, expecting ";" or "}"`.
2. `sudo systemctl status nginx --no-pager` и `sudo journalctl -u nginx -n 30 --no-pager` — подтверждаем, что сервис не перезагрузился/лежит.
3. `git log --oneline -5` и `git log -p -- nginx/web01.vektor.conf | head -80` на UBU01 — находим коммит-виновника.
4. Проверяем, почему не поймали: `git log -1 --format='%h %an %s'` — и вопрос автору, не было ли `--no-verify`. Клиентский хук не сработал по определению: он живёт в `.git/hooks` и обходится флагом.

**Решение.** На UBU01: `git revert -m 1 <sha-merge-коммита>` в main (или точечный revert коммита с конфигом), пуш с `-o merge-approved`; на WEB01 — вернуть `.bak` и `sudo systemctl reload nginx`. Дальше — закрыть системную дырку: в `pre-receive` добавить минимальную проверку (наличие `password`/`secret` в диффе, `check-yaml` вручную через `git show`), а полноценную проверку `nginx -t` вынести в CI-джобу в контейнере (следующие уроки). Плюс правило: мержит только мейнтейнер.

**Ответ пользователю:** «Сайт поднял, конфиг откатил до предыдущей версии, причина — отсутствующая `;` в блоке gzip, попала в main вчерашним мержем в обход локальных проверок. Добавляю серверную проверку, чтобы это не повторилось. От тебя: перед мержем всегда смотри `git diff` и не пушить с `--no-verify`.»

**Закрытие заявки:** категория «конфигурация веб-сервера», решение — откат + превентивная мера, ссылка на тикет-первопричину, в `stand-log.md` строка про инцидент.

### Что изменилось на стенде

- **GIT01:** появился системный пользователь `git` и bare-репозиторий `/srv/git/vektor-web.git`; в нём серверный хук `pre-receive`, защищающий `main` (обход — пуш-опция `merge-approved`).
- **UBU01:** локальный клон `~/vektor-web`; установлен `pre-commit` (pipx); файлы `.pre-commit-config.yaml`, `scripts/check-commit-msg.sh`, `CODEOWNERS`, `.gitlab/merge_request_templates/default.md`, `nginx/web01.vektor.conf`, `docs/`.
- **WEB01:** (если выполняли шаг 13) обновлённый конфиг виртуального хоста + бэкап `web01.vektor.conf.bak`.
- **Учётки/адреса:** новых AD-учёток нет; 10.10.10.50 = git-сервер по SSH.

### Откат

В VMware Workstation: `VM → Snapshot → Snapshot Manager` → выбрать `before-devops-016` → `Go To`. Для CLI: `vmrun -T ws revertToSnapshot "/path/to/UBU01/UBU01.vmx" before-devops-016`. Откатывать нужно все три ВМ, иначе на UBU01 останется клон репозитория, которого нет на GIT01. Если откатывать не хочется — удалить `/srv/git/vektor-web.git` и `~/vektor-web`, снять `pre-commit uninstall` (или просто откатить снапшотом — правило стенда «ничего не удаляем насовсем» это допускает).

## 6. Ошибки новичков и безопасность

- **`git commit --no-verify` как привычка.** → Всё, что должно было отловиться, попадает в историю. → Правильно: обходить хук можно только осознанно и с записью в тикете, почему.
- **Полагаться только на pre-commit.** → Хуки не клонируются вместе с репозиторием: новый коллега клонировал, ничего не установил, и его «грязный» коммит улетел в main. → Правильно: строка `pre-commit install` в README/`Makefile bootstrap`, плюс серверные проверки и CI.
- **«LGTM» за 5 секунд.** → Ревью есть, пользы нет. → Правильно: сначала `--stat`, потом по файлам; отдельно смотреть конфиги, секреты, миграции БД, изменения прав.
- **Гигантский MR на 1500 строк.** → Никто не читает, все апрувят вслепую. → Правильно: MR до 300–400 строк, разбивать по смыслу; рефакторинг отдельно от функциональности.
- **`fix`, `update`, `asdf` в истории.** → Невозможно собрать changelog и понять, что сломалось при revert. → Правильно: conventional commits + `commit-msg` хук + `commit.template` в конфиге.
- **CODEOWNERS есть, но ветка не защищена.** → Файл существует, правила не enforced, мержит кто угодно. → Правильно: CODEOWNERS **плюс** protected branch с required approvals и required status checks.
- **`git push --force` вместо `--force-with-lease`.** → Затирается чужая работа в общей ветке. → Правильно: `--force-with-lease`, а лучше `--force-if-includes` [ПРОВЕРИТЬ доступность в вашей версии Git].
- **Ревью только «по коду», без проверки конфигов.** → Битый YAML или nginx.conf уходит в main, потому что «это же не код». → Правильно: в чек-лист MR добавить `nginx -t`, `yamllint`, `shellcheck`.

**Безопасность.** Никогда не коммить пароли, токены, приватные ключи и `.env` — даже в ветку, даже «на минуту»: объект останется в истории и в рефлогах у всех, кто клонировал. Если секрет утёк — недостаточно `git rm`: нужна ротация секрета и очистка истории (`git filter-repo`), а в боевой среде ещё и разбор инцидента. Клиентские хуки и линтеры — это про качество и скорость, а не про безопасность: любой обходит их одной командой. Настоящий контроль — серверные хуки, CI с обязательными проверками и запрет прямого пуша в защищённые ветки. И отдельно: не ставьте на стенд «крякнутые» плагины и не тяните хук-репозитории сомнительного происхождения — pre-commit запускает их код у вас на машине.

## 7. Вопросы на собеседовании

**1. Чем pull request отличается от merge request и зачем он вообще нужен, если есть `git merge`?**

Ответ: технически это одно и то же — запрос на слияние ветки, разные названия в GitHub и GitLab/Gitea. Ценность не в слиянии, а в точке контроля: MR фиксирует дифф, обсуждение, результат проверок CI, факт и автора approve. Только после этого мейнтейнер делает merge, и в истории остаётся связка «код + кто и когда одобрил». `git merge` руками этого не даёт.

*Что хочет услышать интервьюер:* понимание, что MR — процесс и аудит, а не кнопка.
*Красный флаг:* «PR и MR — это разные вещи, в GitLab другая механика слияния».

**2. Зачем нужны pre-commit хуки, если есть CI?**

Ответ: скорость обратной связи и экономия ресурсов. Хук ловит пробелы, битый YAML, синтаксис bash и случайный пароль за секунду на машине автора, а не через три минуты в пайплайне и не после пуша. Но полагаться только на клиентские хуки нельзя: они не версионируются вместе с репозиторием (`.git/hooks` не в Git), требуют установки у каждого и обходятся `--no-verify`. Поэтому pre-commit — первый фильтр, CI с тем же конфигом (`pre-commit run --all-files`) — второй, серверные хуки и protected branch — третий.

*Что хочет услышать интервьюер:* «клиентское — удобство, серверное — гарантия».
*Красный флаг:* «Хуков достаточно, CI не нужен».

**3. Что дают conventional commits на практике, кроме красивой истории?**

Ответ: машиночитаемую историю. Из типов напрямую выводится SemVer: `feat` → MINOR, `fix` → PATCH, `BREAKING CHANGE:` или `!` → MAJOR. По ней генерируется CHANGELOG и номер следующего релиза (semantic-release, `git-cliff` и аналоги), а ещё можно фильтровать, что реально попадает в пользовательские release notes. Плюс дисциплина: автор формулирует смысл изменения одной строкой.

*Что хочет услышать интервьюер:* связь commit types → версии → changelog/релиз.
*Красный флаг:* «Это просто соглашение о стиле, никакой технической роли не играет».

**4. Ревьюер просит правки. Как обновить MR и не устроить при этом хаос в истории?**

Ответ: правки — отдельными коммитами (`fixup!`), затем `git rebase -i --autosquash` перед мержем, либо сразу `--amend`, если ветка локальная и никто её не трогал. Пуш после переписывания — только `--force-with-lease`, чтобы не затереть чужое. Ревьюеру показать дельту через `git range-diff`, а не заставлять перечитывать весь дифф. В main при этом обычно приходит один аккуратный коммит (squash) или merge-коммит — это уже политика команды.

*Что хочет услышать интервьюер:* fixup/rebase, force-with-lease, range-diff, понимание, что force-push в общую ветку — отдельная опасность.
*Красный флаг:* «Просто делаю `git push --force` в main, там же всё равно моё».

**5. Как работает CODEOWNERS и почему у вас он может «не работать»?**

Ответ: это файл с шаблонами путей и владельцами (`/nginx/ @vektor/infra @adm.student`), последний подходящий шаблон побеждает. Форж по нему автоматически подставляет ревьюеров и, если включено, требует approve от владельца. Сам по себе файл ничего не блокирует: нужна защита ветки с обязательными ревью и, в зависимости от платформы и тарифа, включённое правило «require approval from code owners» [ПРОВЕРИТЬ для вашей версии GitLab]. Частые причины «не работает»: файл лежит не в поддерживаемом месте (корень, `docs/`, `.gitlab/` / `.github/`), опечатка в имени пользователя или команды, правка пришла в ветке, где файл ещё старый, либо required approvals просто не включены.

*Что хочет услышать интервьюер:* CODEOWNERS — декларация, enforcement — в настройках ветки.
*Красный флаг:* «Достаточно положить файл в корень, и доступ к папке закроется».

## 8. Источники (официальная документация)

- **git-scm.com** — книга Pro Git, разделы «Git Hooks» (глава 8) и «Git Attributes»; поиск: `pro git customizing git hooks`.
- **git-scm.com/docs** — справка по `git request-pull`, `git format-patch`, `git range-diff`, `git push` (раздел про `--push-option`); поиск: `git push options documentation`.
- **conventionalcommits.org** — спецификация Conventional Commits 1.0.0, раздел «Summary»; поиск: `conventional commits 1.0.0 summary`.
- **pre-commit.com** — официальный сайт фреймворка, разделы «Quick start», «Plugins / pygrep», «Usage»; поиск: `pre-commit pygrep language hook`.
- **docs.gitlab.com** — разделы «Merge requests», «Merge request approvals», «Code Owners», «Push options», «Protected branches»; поиск: `gitlab docs code owners approval rules`. Часть возможностей Code Owners/approvals зависит от тарифа — проверяйте по вашей редакции `[ПРОВЕРИТЬ]`.
- **docs.github.com** — разделы «About protected branches», «About CODEOWNERS», «About status checks»; поиск: `github docs about codeowners`.
- **yamllint.readthedocs.io** — правила конфигурации линтера; поиск: `yamllint configuration file rules`.
- **github.com/koalaman/shellcheck** — README и интеграция с pre-commit (репозиторий `koalaman/shellcheck-precommit`); поиск: `shellcheck precommit hook`.
- **github.com/gitleaks/gitleaks** — сканер секретов, раздел «Usage»; поиск: `gitleaks detect pre-commit` `[ПРОВЕРИТЬ актуальные команды и версии]`.

Российский контекст: self-hosted GitLab доступен как CE и разворачивается из официальных пакетов, но скачивание образов и пакетов из-за рубежа может быть нестабильным — держите локальное зеркало либо рассматривайте Gitea/Forgejo и отечественные GitFlic/GitVerse (сравнение — в уроке 18) `[ПРОВЕРИТЬ актуальные условия поставки и тарифы]`.
