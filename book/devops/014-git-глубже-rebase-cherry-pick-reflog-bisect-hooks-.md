# Git глубже: rebase, cherry-pick, reflog, bisect, hooks, submodules, монорепо

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в трекере**
- **Пользователь:** `ivanov` (бухгалтерия, ВМ `WIN11-CL01`)
- **Что написал:** «Я вчера шесть раз сохранял правки в скрипт сверки платежей, вы положили его в Git на GIT01. Сегодня смотрю ветку `fix/reconcile` — там всего два коммита, остальных нет. В пятницу сверка, помогите»
- **Срочность:** средняя

Дальше выясняется классика: кто-то сделал `rebase`, потом `push --force`, и «пропавшие» коммиты на самом деле лежат в reflog. Такие истории случаются в DevOps-работе постоянно: инженер — это человек, который умеет и историю переписать аккуратно, и восстановить её после чужой ошибки, и автоматизировать проверки на входе в репозиторий.

После этой главы ты должен уметь: осознанно выбирать между merge и rebase; переносить отдельный фикс через cherry-pick, не таща за собой всю ветку; находить «первый плохой коммит» бинарным поиском, а не глазами; поднимать локальные и серверные hooks; собирать репозиторий из submodules и понимать, чем монорепо отличается от polyrepo с точки зрения CI. И главное — не бояться, что «Git всё сломал»: почти всё обратимо, пока жив reflog.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Git — это хранилище объектов с адресацией по содержимому. Четыре типа объектов: **blob** (содержимое файла), **tree** (каталог: имена + ссылки на blob/tree), **commit** (указатель на tree + родители + автор/коммиттер + сообщение), **tag**. SHA-1 (в новых репозиториях — SHA-256, если явно включено) считается от содержимого, поэтому любое изменение родителя меняет SHA всех потомков.

Ветка — это просто файл `.git/refs/heads/<имя>` с SHA коммита. `HEAD` — указатель на текущую ветку (или на коммит напрямую, тогда это **detached HEAD**).

```text
commit 9f3c1a
 ├─ tree  →  blob reconcile.sh
 │        →  tree tests/ → blob test_reconcile.sh
 ├─ parent 41ab77
 └─ author / committer / message

refs/heads/main → 9f3c1a
HEAD → refs/heads/main
```

**merge** создаёт новый коммит с двумя родителями и не трогает существующие SHA. **rebase** находит merge-base (общего предка), берёт diff каждого коммита ветки и применяет их заново поверх новой базы — получаются **новые коммиты с новыми SHA**. Author date сохраняется, committer date и SHA меняются.

```text
merge:
  A---B---C-------M      main
       \         /
        D---E---F        feature

rebase feature поверх main:
  A---B---C              main
           \
            D'---E'---F' feature   (новые SHA)
```

**cherry-pick** — это трёхсторонний merge между родителем выбранного коммита и самим коммитом, применённый к текущему HEAD. Один коммит, новая SHA, ветка-источник не затрагивается.

**reflog** — локальный журнал перемещений refs: `.git/logs/HEAD`, `.git/logs/refs/heads/*`. Он не передаётся при `push`/`clone`, но именно он спасает после `reset --hard` и неудачного rebase. Срок жизни записей задаётся `gc.reflogExpire` (по умолчанию 90 дней) и `gc.reflogExpireUnreachable` (30 дней).

**bisect** — бинарный поиск по DAG: каждый шаг отсекает половину диапазона, для 1000 коммитов это ~10 проверок.

**hooks** — исполняемые скрипты, которые Git запускает на события. Клиентские (`pre-commit`, `commit-msg`, `pre-push`, `post-checkout`, `pre-rebase`) лежат в `.git/hooks/` и **не клонируются**. Серверные (`pre-receive`, `update`, `post-receive`) живут на стороне репозитория и проверяют push. Каталог можно переопределить через `core.hooksPath`.

**submodules** — в родительском репозитории хранится не код, а «указатель» (gitlink, режим `160000`) на конкретный коммит другого репозитория, плюс файл `.gitmodules` с URL. Внутри submodule ты по умолчанию в detached HEAD.

**Транспорт:** на стенде GIT01 отдаёт репозитории по SSH (порт 22). HTTPS (443) появится вместе с GitLab в уроке 17, устаревший `git://` (9418) без шифрования не используем.

## 3. Аналогия из жизни

Представь издательство. Коммит — это глава рукописи, лежащая в папке с номером; ветка — закладка на конкретной главе.

**merge** — сшить две версии книги в одну, добавив страницу «здесь сходятся обе линии».
**rebase** — перепечатать свои главы заново на новой вёрстке: текст тот же, но номера страниц другие, и у читателей старых копий ссылки уже не совпадут.
**cherry-pick** — вырезать одну удачную правку из чужой рукописи и вклеить в свою, не забирая остальное.
**reflog** — журнал редактора, куда секретарь записывает каждое перемещение закладки; даже если главу вырвали, по журналу видно, где она лежала.
**bisect** — корректор ищет опечатку, открывая книгу на середине: «до этого места чисто, значит, дальше».
**hooks** — корректор у двери типографии, который не пропустит страницу с опечаткой или чужим штампом.
**submodules** — вклеенные отдельные брошюры, у каждой своя редакция; **монорепо** — одна толстая книга, где все главы правятся одним тиражом.

Аналогия ломается на одном: в издательстве перепечатка главы не меняет её идентификатор, а в Git rebase меняет SHA, и для всех, у кого была старая копия, это уже другой коммит.

## 4. Практика и команды

**1. Карта истории**

```bash
git log --graph --oneline --decorate --all
git log --first-parent --oneline main
```
`--graph` рисует DAG символами, `--decorate` показывает ветки/теги, `--all` — все refs, `--first-parent` — только основную линию (удобно читать main после merge-стратегии).

```text
* 9f3c1a (HEAD -> fix/reconcile) fix: разделитель ;
* 41ab77 feat: тест сверки
| * 2e0d9b (main) chore: README
|/
* 7c55aa feat: первый коммит
```
Смотрим: где ветки разошлись (точка `|/`) и сколько коммитов на каждой стороне.

**2. Работа с веткой и коммитом**

```bash
git switch -c feature/reconcile-csv
git add reconcile.sh
git commit -m "feat: разбор csv"
git commit --amend --no-edit      # дописать в последний коммит, не меняя сообщение
```
`switch -c` = создать и перейти. `--amend` перезаписывает последний коммит (новая SHA!) — только для ещё не опубликованных коммитов.

**3. Интерактивный rebase: причесать ветку**

```bash
git rebase -i HEAD~3
```

```text
pick 41ab77 feat: разбор csv
squash 5c1e02 wip
reword 9f3c1a fix: разделитель
```
Команды в todo-листе: `pick` (оставить), `reword` (переписать сообщение), `edit` (остановиться и доправить), `squash` (склеить с предыдущим, объединив сообщения), `fixup` (склеить, выбросив сообщение), `drop` (удалить). Проверять результат — `git log --oneline` и `git range-diff`.

**4. Rebase на актуальный main и разбор конфликтов**

```bash
git switch feature/reconcile-csv
git rebase main
# конфликт в reconcile.sh
git status
git add reconcile.sh
git rebase --continue     # продолжить
git rebase --skip         # пропустить «пустой» коммит
git rebase --abort        # вернуть всё как было
git config --global rerere.enabled true   # Git запоминает, как ты решил конфликт
```
`rerere` (reuse recorded resolution) — при повторном rebase той же ветки Git применит твоё же решение автоматически.

**5. Rebase с переносом на другую базу**

```bash
git rebase --onto main feature-base feature/reconcile-csv
```
Синтаксис: `--onto <новая база> <что отрезать> <что переносим>`. Берём коммиты, которые есть в `feature/reconcile-csv`, но нет в `feature-base`, и кладём их на `main`. Типичный кейс — вытащить подветку из чужой.

**6. Cherry-pick одного фикса**

```bash
git switch main
git cherry-pick -x 9f3c1a
git cherry-pick -n 1a2b3c        # применить в индекс+рабочее дерево, но не коммитить
git cherry-pick 4c1d0e..9f3c1a   # диапазон (левый конец НЕ включается)
```
`-x` дописывает в сообщение строку `(cherry picked from commit 9f3c1a)` — потом видно, откуда фикс. Типичный вывод при конфликте: `error: could not apply 9f3c1a` → правим, `git add`, `git cherry-pick --continue`.

**7. Сравнить ветку до и после rebase**

```bash
git branch backup/fix-before-rebase
git rebase main
git range-diff main..backup/fix-before-rebase main..fix/reconcile
```
`range-diff` показывает соответствие «старый коммит ↔ новый коммит» и что изменилось по содержанию. Незаменимо при ревью переписанных веток.

**8. Reflog: найти и вернуть потерянное**

```bash
git reflog
git reflog show fix/reconcile
git reset --hard HEAD@{2}
```

```text
9f3c1a HEAD@{0}: rebase (finish): returning to refs/heads/fix/reconcile
41ab77 HEAD@{1}: rebase (start): checkout main
5c1e02 HEAD@{2}: commit: wip
```
`HEAD@{n}` — состояние n шагов назад, `HEAD@{2.hours.ago}` — по времени. Только локально: reflog не уезжает на сервер и хранится ~90 дней.

**9. Если коммит вообще ни на что не ссылается**

```bash
git fsck --unreachable --no-reflogs
git fsck --lost-found
# объекты-коммиты появятся в .git/lost-found/commit/
git show <sha>
git branch rescue/<дата> <sha>
```
Последний рубеж, когда reflog уже почищен: ищем «висячие» объекты и вешаем на них ветку.

**10. Bisect: найти коммит, который всё сломал**

```bash
git bisect start
git bisect bad                 # текущий HEAD плохой
git bisect good v1.0           # тег/коммит, где было хорошо
# Git переключает по одному коммиту: проверяем → git bisect good / bad
git bisect reset               # вернуться на исходную ветку
```
Автоматически — `git bisect run`:

```bash
git bisect start HEAD v1.0
git bisect run ./tests/test_reconcile.sh
```
Код возврата скрипта: `0` — good, `1..127` (кроме `125`) — bad, `125` — skip (сломанная сборка, пропустить). Вывод в конце: `<sha> is the first bad commit`.

**11. Hooks: проверка перед коммитом**

```bash
mkdir -p .githooks
cat > .githooks/pre-commit <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# 1. синтаксис всех shell-скриптов, попавших в коммит
git diff --cached --name-only --diff-filter=ACM | grep -E '\.sh$' | while read -r f; do
  bash -n "$f"
done
# 2. грубая проверка на утёкшие секреты
if git diff --cached | grep -nE 'Lab#2026-vektor|password\s*=|token\s*='; then
  echo "В коммите похоже на секрет — отклонено." >&2
  exit 1
fi
EOF
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
git commit -m "chore: hooks" --no-verify   # одноразовый обход
```
`core.hooksPath` указывает Git на каталог с хуками — так их можно версионировать вместе с кодом (сам конфиг, к сожалению, не клонируется: новый разработчик выполняет `git config core.hooksPath .githooks` один раз). `--no-verify` отключает `pre-commit` и `commit-msg`.

**12. Submodules**

```bash
git submodule add ssh://adm.student@10.10.10.50/srv/git/lib-common.git vendor/lib-common
git commit -m "chore: подключили lib-common"
git submodule status
git submodule update --init --recursive
git clone --recurse-submodules ssh://adm.student@10.10.10.50/srv/git/vektor-scripts.git
git submodule update --remote vendor/lib-common   # подтянуть свежий коммит из upstream
```
`git submodule status`:

```text
 9f3c1a2b... vendor/lib-common (v1.2)
+41ab77c0... vendor/lib-common (v1.3)   # + значит локальный коммит отличается от записанного
-2e0d9b11... vendor/lib-common          # - значит submodule ещё не инициализирован
```
Пробел = всё синхронно. Внутри submodule ты в detached HEAD: чтобы что-то поменять, сначала `git switch main` внутри него, потом `push`, потом в родителе закоммитить новый gitlink.

**13. Монорепо: sparse-checkout и частичный клон**

```bash
git config --global uploadpack.allowFilter true   # на стороне сервера GIT01 (bare-репо)
git clone --filter=blob:none ssh://adm.student@10.10.10.50/srv/git/vektor-mono.git
cd vektor-mono
git sparse-checkout set services/billing libs/common
git sparse-checkout list
git sparse-checkout disable
```
`--filter=blob:none` — «ленивый» клон (деревья и коммиты есть, файлы докачиваются по требованию). `sparse-checkout set` оставляет в рабочем каталоге только нужные пути — это то, что делает жизнь в монорепо терпимой.

**14. Worktree: фикс без переключения веток**

```bash
git worktree add ../vektor-hotfix -b hotfix/payments main
git worktree list
git worktree remove ../vektor-hotfix
```
Отдельный рабочий каталог на другую ветку из того же репозитория. Удобно для срочного хотфикса, пока в основной копии сборка «в процессе».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд DevOps: должны быть готовы `UBU01` (10.10.10.30, Ubuntu Server 24.04, SSH, установлен `git`) и `MON01`/`WEB01` по желанию.
- `GIT01` (10.10.10.50) создаём **на этом уроке** — это первая машина Git-трека. Полноценный GitLab CE ставится в уроке 17, здесь ставим только `git` и bare-репозитории по SSH.
- SSH-ключи `UBU01 ↔ GIT01` настроены (глава 12). Все команды — под учётной записью `adm.student` (та, под которой ты работаешь на Linux-машинах стенда с главы 6; если у тебя она называется иначе — подставь своё имя).
- Установленный `git` на UBU01 и GIT01 версии 2.40+ (`git --version`).

### Снапшот

На `UBU01`:

```bash
sudo shutdown -P now
```

В VMware Workstation: **VM → Snapshot → Take Snapshot**, имя `before-devops-014`. Или из консоли хоста:

```bash
vmrun snapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-014
```
То же имя — когда создашь `GIT01` и перед первым изменением сделаешь ей снапшот.

### Шаги

1. **GIT01: базовая ВМ.** VirtualBox/VMware: 2 vCPU, 2 ГБ RAM, 20 ГБ диск, сеть VMnet8, статический адрес `10.10.10.50/24`, шлюз `10.10.10.2`, DNS `10.10.10.10`. Проверка: `ping 10.10.10.10` и `ssh adm.student@10.10.10.50` с UBU01. Ожидаемо: отвечает.

2. **GIT01: ставим git и создаём bare-репозитории.**

```bash
sudo apt update && sudo apt install -y git
sudo mkdir -p /srv/git && sudo chown "$USER":"$USER" /srv/git
git init --bare /srv/git/vektor-scripts.git
git init --bare /srv/git/lib-common.git
git init --bare /srv/git/vektor-mono.git
git config --global uploadpack.allowFilter true   # для частичных клонов
```
Ожидаемо: `Initialized empty Git repository in /srv/git/vektor-scripts.git/`. Каталоги с окончанием `.git` — соглашение, чтобы путь в SSH-URL сразу читался как репозиторий.

3. **UBU01: локальный репозиторий и первый push.**

```bash
git config --global user.name "adm.student"
git config --global user.email "adm.student@corp.vektor.lab"
git config --global init.defaultBranch main
mkdir -p ~/work && cd ~/work
git clone ssh://adm.student@10.10.10.50/srv/git/vektor-scripts.git
cd vektor-scripts
```
Ожидаемо: `warning: You appear to have cloned an empty repository.` — это нормально.

4. **Наполняем репозиторий (скрипт сверки из заявки ivanov).**

```bash
cat > reconcile.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# Сумма колонки amount (3-я) из CSV с разделителем ';'
awk -F';' 'NR>1 {sum+=$3} END {printf "%.2f\n", sum}' "$1"
EOF
chmod +x reconcile.sh
mkdir -p tests
cat > tests/payments.csv <<'EOF'
id;date;amount
1;2026-01-10;500.25
2;2026-01-11;750.25
EOF
cat > tests/test_reconcile.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
expected="1250.50"
out="$(./reconcile.sh tests/payments.csv)"
[[ "$out" == "$expected" ]] || { echo "FAIL: ожидали $expected, получили $out"; exit 1; }
echo "OK: $out"
EOF
chmod +x tests/test_reconcile.sh
git add . && git commit -m "feat: сверка платежей" && git push -u origin main
```
Ожидаемо: `./tests/test_reconcile.sh` печатает `OK: 1250.50`.

5. **Интерактивный rebase: склеиваем черновики.** Создай ветку `feature/reconcile-csv`, сделай три коммита («wip», «wip2», «fix: разделитель ;»), затем `git rebase -i HEAD~3`, оставь `pick` у первого, `fixup` у второго, `reword` у третьего. Ожидаемо: `git log --oneline` показывает два коммита вместо трёх, сообщения осмысленные.

6. **Конфликт при rebase.** В `main` поменяй строку `awk` (например, `printf "%.2f\n"` → `printf "%.0f\n"`), в ветке поменяй ту же строку иначе, затем `git switch feature/reconcile-csv && git rebase main`. Ожидаемо: `CONFLICT (content): Merge conflict in reconcile.sh`. Разреши вручную, `git add reconcile.sh`, `git rebase --continue`. Проверь `./tests/test_reconcile.sh`.

7. **Cherry-pick хотфикса.** На ветке сделай коммит «fix: считать только оплаченные строки». Вернись в `main`, выполни `git cherry-pick -x <sha>`. Ожидаемо: в `main` появился тот же фикс, в сообщении строка `(cherry picked from commit ...)`, ветка `feature/reconcile-csv` не изменилась.

8. **Ломаем и восстанавливаем через reflog.** На `feature/reconcile-csv` выполни `git reset --hard HEAD~2` (коммиты «пропали»). Затем `git reflog`, найди нужный `HEAD@{n}`, выполни `git reset --hard HEAD@{n}`. Ожидаемо: коммиты вернулись, `git log --oneline` снова показывает их.

9. **Bisect.** В `main` сделай 8 мелких коммитов, в одном (например, 4-м) поменяй `-F';'` на `-F','` — тест начнёт падать. Потом `git bisect start HEAD v1.0` (тег на заведомо хорошем коммите) и `git bisect run ./tests/test_reconcile.sh`. Ожидаемо: Git сам найдёт коммит и напечатает `<sha> is the first bad commit`. Заверши `git bisect reset` и почини `git revert <sha>`.

10. **Hooks.** Настрой `.githooks/pre-commit` из раздела 4 (шаг 11), `chmod +x`, `git config core.hooksPath .githooks`. Проверка: добавь в `reconcile.sh` строку `password=Lab#2026-vektor` и попробуй закоммитить. Ожидаемо: коммит отклонён. Убери строку — коммит проходит. Затем проверь обход: `git commit --no-verify`.

11. **Submodule.** На UBU01 склонируй `lib-common`, добавь `format.sh` (например, `printf '%s\n' "$1"`), запушь. В `vektor-scripts`: `git submodule add ssh://adm.student@10.10.10.50/srv/git/lib-common.git vendor/lib-common`, закоммить и запушь. Проверь чистый клон: `cd /tmp && git clone --recurse-submodules ssh://...vektor-scripts.git check && ls check/vendor/lib-common`. Ожидаемо: `format.sh` на месте.

12. **Монорепо.** Создай `vektor-mono` с каталогами `services/billing`, `services/tracking`, `libs/common`, запушь. Склонируй с `--filter=blob:none`, включи `git sparse-checkout set services/billing libs/common`. Ожидаемо: `ls services` показывает только `billing`, при этом `git log --oneline` видит всю историю.

13. **Запись в `stand-log.md`.** Строки: «GIT01 10.10.10.50 — Ubuntu 24.04, git 2.4x, bare-репо `/srv/git/{vektor-scripts,lib-common,vektor-mono}.git`, `uploadpack.allowFilter=true`»; «UBU01: `~/work/vektor-scripts` с `core.hooksPath=.githooks`, submodule `vendor/lib-common`».

### Проверка

- [ ] `ssh adm.student@10.10.10.50 'git --version'` отвечает на UBU01 без пароля
- [ ] `git -C ~/work/vektor-scripts log --oneline` показывает слитые после `rebase -i` коммиты (их меньше, чем было)
- [ ] `git -C ~/work/vektor-scripts log --oneline main` содержит `(cherry picked from commit ...)`
- [ ] `git reflog` содержит строки `rebase (start)` / `rebase (finish)` / `reset: moving to`
- [ ] `git bisect log` (после reset — в истории терминала) зафиксировал найденный bad-коммит
- [ ] `git config --get core.hooksPath` возвращает `.githooks`, а коммит с `Lab#2026-vektor` отклоняется
- [ ] `git submodule status` в родителе начинается с пробела (синхронизировано)
- [ ] `git sparse-checkout list` в монорепо показывает два пути
- [ ] `stand-log.md` дополнен

### Сломай и почини

**Пользователь:** `ivanov`
**ВМ:** `WIN11-CL01` (пишет), работа идёт на `GIT01` и `UBU01`
**Что написал:** «В ветке `fix/reconcile` пропали мои коммиты. Я делал `git pull --rebase`, что-то пошло не так, теперь вижу только два коммита из шести. В пятницу сверка»
**Срочность:** высокая

**Как воспроизвести поломку (на UBU01):**

```bash
cd ~/work/vektor-scripts
git switch fix/reconcile
git reset --hard HEAD~2
git push --force origin fix/reconcile
```

**Диагностика по порядку:**

1. `git status` — убедиться, что рабочее дерево не грязное и мы на той ветке.
2. `git reflog show fix/reconcile` — искать запись до `reset`. Если пользователь делал rebase, там будет `rebase (start)` и предыдущий HEAD.
3. Если в локальном reflog пусто — идти на сервер: `cd /srv/git/vektor-scripts.git && git reflog show fix/reconcile`. **Важно:** в bare-репозитории reflog по умолчанию может не вестись; проверь `git config core.logAllRefUpdates` и включи `git config core.logAllRefUpdates true` — на будущее.
4. Если и там пусто — `git fsck --unreachable --no-reflogs` на сервере, искать «висячие» коммиты.
5. После того как SHA найден — `git cat-file -p <sha>` и `git log --oneline <sha>`: убедиться, что это действительно нужные коммиты, а не черновики.

**Решение:**

```bash
git switch fix/reconcile
git branch backup/fix-reconcile-broken          # страховка
git reset --hard <sha-из-reflog>                # восстановили локально
git push --force-with-lease origin fix/reconcile
./tests/test_reconcile.sh                       # проверка, что код живой
```

**Ответ пользователю:** «Коммиты восстановлены, ветка `fix/reconcile` снова содержит все шесть. Причина — `push --force` после rebase перезаписал ветку на сервере; я вернул её из журнала ссылок и проверил тестами. На будущее: force-push только с `--force-with-lease`, и перед rebase делаем резервную ветку».

**Закрытие заявки:** тип «инцидент», причина «force-push в общую ветку», решение «recovery из reflog», профилактика — защищённые ветки в GitLab (урок 17) и `--force-with-lease` в инструкции для команды. Запись в `stand-log.md`.

### Что изменилось на стенде

- Новая ВМ **GIT01** — Ubuntu Server 24.04 LTS, `10.10.10.50`, пакет `git`, каталог `/srv/git`.
- Bare-репозитории: `/srv/git/vektor-scripts.git`, `/srv/git/lib-common.git`, `/srv/git/vektor-mono.git`.
- На GIT01: `git config --global uploadpack.allowFilter true` (для `--filter=blob:none`).
- На UBU01: `~/work/vektor-scripts` (клон), `~/work/lib-common`, `~/work/vektor-mono`; глобальные `user.name`/`user.email`; `core.hooksPath=.githooks` в `vektor-scripts`.
- Ветки в `vektor-scripts`: `main`, `feature/reconcile-csv`, `fix/reconcile`, `backup/fix-reconcile-broken`, тег `v1.0`.
- Submodule `vendor/lib-common` в `vektor-scripts` + файл `.gitmodules`.
- Репозиторий `vektor-mono` со структурой `services/billing`, `services/tracking`, `libs/common`.

### Откат

```bash
# в VMware Workstation: VM → Snapshot → Snapshot Manager → before-devops-014 → Revert
vmrun revertToSnapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-014
```
То же для `GIT01`. **Помни:** откат снапшота стирает репозитории на GIT01 и клоны на UBU01. Если что-то нужно сохранить — сначала выгрузи историю в файл:

```bash
git bundle create ~/vektor-scripts-$(date +%F).bundle --all
git clone vektor-scripts-2026-01-15.bundle restored
```
`git bundle` — это один файл со всей историей, который можно склонировать как обычный репозиторий.

## 6. Ошибки новичков и безопасность

1. **Rebase публичной ветки → `push --force` в main.** Коллеги получают расхождение истории, их `pull` ломается, коммиты «исчезают». Правильно: rebase только своих локальных веток; в общие — merge; если force неизбежен — `--force-with-lease`, а main защищён правилом ветки.

2. **Cherry-pick вместо merge целой фичи.** Пять коммитов перенесены по одному → при следующем merge те же изменения приезжают второй раз, конфликты и дубли. Правильно: cherry-pick — для хотфиксов и бэкпортов в релизные ветки, и всегда с `-x`; целиком фичу — merge или rebase.

3. **`git reset --hard` без проверки, что в рабочем дереве.** Незакоммиченные правки не попадают в reflog и не восстанавливаются. Правильно: `git status` и `git stash` перед любой «жестокой» командой.

4. **Хуки настроены только у одного разработчика.** В репозитории `.git/hooks/*.sample`, никто не поставил `core.hooksPath` → проверок нет ни у кого. Правильно: хуки в `.githooks` в репозитории + строка в README + дублирование проверки в CI.

5. **Хук без `chmod +x`.** Git молча его не запускает, и все думают, что проверка есть. Правильно: `git ls-files -s .githooks` должен показывать режим `100755`.

6. **Привычка к `--no-verify`.** Линтеры и проверка секретов обходятся систематически, в историю уезжает `.env` с паролями. Правильно: `--no-verify` только когда точно знаешь, что проверка ложно-срабатывает; секреты — через pre-commit-фреймворк или CI-сканер.

7. **Submodule «застрял в detached HEAD».** Разработчик коммитит внутри submodule, забывает запушить, а в родителе коммитит gitlink на несуществующий на сервере SHA — у всех остальных `git submodule update` падает. Правильно: порядок «запушить submodule → потом родитель», в CI — `--recurse-submodules` и явная проверка `git submodule status`.

8. **Монорепо без sparse-checkout и фильтров CI.** Клон на 4 ГБ, каждый пайплайн собирает всё. Правильно: `--filter=blob:none`, `sparse-checkout`, path-фильтры в CI.

### Безопасность

- **Hooks — это исполнение произвольного кода.** Скрипт из `.githooks` запускается при твоём `git commit` с твоими правами. В чужом репозитории перед работой проверь `git config --list --show-origin` и `ls .git/hooks`, а лучше — работай в контейнере/на отдельной ВМ вроде UBU01.
- **Force-push — это удаление чужих данных.** В боевой среде main/release защищены правилами, force-push разрешён только мейнтейнерам, и только `--force-with-lease`.
- **Секреты в истории.** Даже если удалить файл следующим коммитом, он остаётся в истории и в reflog. В реальной работе чистят `git filter-repo` `[ПРОВЕРИТЬ: актуальное имя/версия инструмента]` и **обязательно** ротируют скомпрометированные пароли/токены.
- **Учебный пароль `Lab#2026-vektor`** стоит в стенде везде. В боевой среде один пароль на все системы — грубое нарушение; здесь он нужен только чтобы не плодить сущности в лабе.
- **Submodule из недоверенного URL** — это код, который приедет в твою сборочную среду. Проверяй `.gitmodules`, особенно при мерже внешних PR.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**Вопрос 1. Чем rebase отличается от merge и когда вы выбираете что?**

Ответ: merge сохраняет историю как есть и создаёт коммит слияния с двумя родителями; SHA существующих коммитов не меняются. Rebase переписывает коммиты ветки поверх новой базы — получаются новые SHA. Merge безопасен для общих веток, но засоряет историю «паутиной»; rebase даёт линейную читаемую историю, зато переписывает коммиты и требует force-push. Практика: локальную ветку перед PR — rebase на main; вливание в main — merge (или squash-merge); rebase уже опубликованной ветки, на которой работают другие, — запрещён.

*Что хочет услышать интервьюер:* что кандидат понимает смену SHA и социальные последствия переписывания, а не просто «rebase красивее».

*Красный флаг:* «Всегда делаю rebase, история должна быть линейной» без оговорок про публичные ветки.

**Вопрос 2. Разработчик сделал `reset --hard` и потерял три коммита. Ваши действия?**

Ответ: `git reflog` (или `git reflog show <branch>`) — найти SHA до потери, затем `git reset --hard HEAD@{n}` или `git branch rescue <sha>`. Помнить, что reflog локальный и живёт ограниченное время (по умолчанию 90 дней для достижимых, 30 — для недостижимых). Если reflog почищен — `git fsck --lost-found` / `--unreachable` и ручное восстановление висячих коммитов. Если работа была на сервере — проверить reflog на серверной стороне (у bare-репозитория он может быть выключен) и, в крайнем случае, бэкапы.

*Что хочет услышать интервьюер:* спокойствие и конкретный алгоритм, знание, что reflog — не «магия», а файл в `.git/logs`.

*Красный флаг:* «Восстановить нельзя», «надо клонировать заново».

**Вопрос 3. Как найти коммит, который сломал сборку, если между хорошим и плохим состоянием 500 коммитов?**

Ответ: `git bisect` — бинарный поиск, ~9–10 итераций на 500 коммитов. `git bisect start`, `git bisect bad`, `git bisect good <known-good>`, дальше либо вручную good/bad, либо `git bisect run ./tests/test.sh` (код возврата 0 = good, 1–127 = bad, 125 = skip). В конце Git печатает первый плохой коммит, потом `git bisect reset`. Дополнительно: `git log -S'<строка>'` для поиска появления/исчезновения строки, `git blame` для конкретной строки. Важно, что bisect требует чистого рабочего дерева и воспроизводимого теста.

*Что хочет услышать интервьюер:* что кандидат автоматизирует поиск скриптом, а не листает `git log`.

*Красный флаг:* «Посмотрю diff всех 500 коммитов».

**Вопрос 4. Как работают git hooks, почему их нельзя просто положить в репозиторий и как сделать проверки обязательными для всех?**

Ответ: hooks — исполняемые скрипты на события, клиентские (`pre-commit`, `commit-msg`, `pre-push`) и серверные (`pre-receive`, `update`, `post-receive`). Каталог `.git/hooks` не версионируется, поэтому скрипты либо распространяют через `core.hooksPath` (каталог `.githooks` в репозитории + одна команда настройки у разработчика), либо через фреймворк с автоустановкой, либо переносят проверку на сервер. По-настоящему обязательные проверки — серверные (в GitLab — server hooks / CI-пайплайн и правила защищённых веток), потому что клиентские легко обходятся `--no-verify`. `[ПРОВЕРИТЬ: точный путь server hooks в актуальной версии GitLab — см. урок 17]`

*Что хочет услышать интервьюер:* понимание, что клиентские хуки — удобство, а не контроль; контроль живёт на сервере.

*Красный флаг:* «Настроим pre-commit, и никто не сможет запушить плохой код».

**Вопрос 5. Submodules, subtrees или монорепо — что выберете и почему?**

Ответ: зависит от того, как команда релизит. Submodules — когда есть отдельно версионируемая библиотека со своим релизным циклом и своими потребителями; цена — detached HEAD, порядок пушей, забытые `--init --recursive`. Subtrees — когда хочется, чтобы код физически лежал в репозитории без внешних ссылок, ценой раздутой истории и более сложных обновлений (`git subtree pull/push`). Монорепо — когда сервисы меняются вместе и нужны атомарные коммиты «API + клиент»; цена — размер клона и время CI, лечится `--filter=blob:none`, `sparse-checkout` и path-фильтрами пайплайнов. На собеседовании важно не назвать инструмент, а объяснить критерий: связанность изменений, независимость релизов, размер команды.

*Что хочет услышать интервьюер:* трезвый разбор компромиссов и упоминание практических мер (частичный клон, фильтры CI).

*Красный флаг:* «Монорепо всегда лучше, Google же так делает».

## 8. Источники (официальная документация)

- **git-scm.com** — официальный сайт Git. Раздел **«Git Reference»**: страницы команд `git-rebase`, `git-cherry-pick`, `git-reflog`, `git-bisect`, `git-submodule`, `git-sparse-checkout`, `git-worktree`, `git-fsck`, `git-range-diff`, `git-bundle`. Поисковые фразы: `git rebase documentation`, `git bisect run exit code`, `git reflog expire`.
- **git-scm.com/book** — книга **Pro Git** (бесплатная, на русском тоже есть). Главы: «Git Branching — Rebasing», «Git Tools — Rewriting History», «Git Tools — Debugging with Git», «Git Tools — Submodules», «Customizing Git — Git Hooks», «Git Internals — Git Objects». Поисковые фразы: `pro git rebasing`, `pro git git hooks`, `pro git submodules`.
- **git-scm.com/docs/githooks** — полный список хуков и порядок их вызова. Поиск: `githooks list pre-receive update post-receive`.
- **docs.gitlab.com** — раздел про server hooks и protected branches (пригодится в уроке 17). Поиск: `gitlab server hooks`, `gitlab protected branches force push`. Точные пути и названия настроек в актуальной версии — `[ПРОВЕРИТЬ]` перед лабой.
- **docs.github.com** — раздел про `.gitmodules`, submodules и rebase/merge в веб-интерфейсе; удобен как краткая справка. Поиск: `github working with submodules`.
- **man7.org** — `git-bisect(1)`, `git-rebase(1)` в формате man-страниц, если нужен офлайн-справочник. Поиск: `man git-bisect`.
