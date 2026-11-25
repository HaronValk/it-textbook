# Английский для DevOps: документация, Stack Overflow, issue'ы, собеседования, технические диалоги

## 1. Введение (зачем это нужно в реальной работе)

В трекере «Вектора» появляется заявка:

```text
Тикет #4821 — интеграция платёжного шлюза
От: инженер партнёра-эквайера (пишет по-английски)
Затронутая ВМ: WEB01 (10.10.20.10)
Текст: "Hi! Since 09:12 MSK our webhook receiver gets 502 from your endpoint.
        Could you check the upstream and confirm once it's fixed?"
Срочность: высокая (заказы не подтверждаются)
```

Дальше вы делаете ровно то, что делают все DevOps-инженеры каждый день: идёте на WEB01, читаете английский лог nginx, ищете точную строку ошибки в поиске, находите ответ на Stack Overflow, правите конфиг, а потом пишете ответ партнёру по-английски — коротко, вежливо и по делу. Через час у вас созвон с разработчиком из другой страны, где надо с ходу объяснить, «what was the root cause».

Английский в DevOps — не «язык для галочки в резюме», а рабочий инструмент, как `kubectl`. Документация Kubernetes, Docker, GitLab, Terraform — английская. Сообщения об ошибках, которые вы копируете в поиск, — английские. Changelog'и, CVE, release notes, GitHub issues, RFC — английские. Вакансии с международными командами, удалёнка на зарубежного заказчика и половина собеседований в российских компаниях с экспортными продуктами — тоже английские. После этой главы вы будете уметь: читать документацию «по скелету», а не построчно; задавать вопросы так, чтобы на них отвечали; писать issue, MR и runbook на понятном инженерном английском; держать технический диалог в инциденте; проходить устную часть интервью.

Важно сразу снять страх: **никто не ждёт от вас литературного английского**. Ждут структуру, точность терминов и отсутствие «вода». Акцент и мелкие грамматические ошибки — норма, над ними смеются только в плохих анекдотах.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Воспринимайте английский как **транспортный слой** вашей инженерной работы. Все 114 предыдущих уроков — ADR, runbook'и, ревью, инциденты — «бегают» поверх него. Если транспорт не работает, содержимое не доезжает.

У языка в нашей профессии четыре разных режима, и у каждого свои правила:

| Артефакт | Что важно | Грамматика |
|---|---|---|
| Коммит / заголовок MR | коротко, императив, ≤ 72 символов | `fix(nginx): reload config after cert renewal` |
| Issue / вопрос на SO | expected / actual / steps to reproduce / environment | past simple + present simple |
| Runbook, ADR | нумерованные шаги, никаких «может быть» | imperative: `Restart the service.` |
| Инцидент (чат, звонок) | статус + что мне нужно от тебя | present perfect: `The deploy has finished` |
| Собеседование | STAR: Situation, Task, Action, Result | past simple + результат цифрой |

Схема «как мысль доезжает до коллеги»:

```text
[мысль в голове]
      │  1. выбрать термин (root cause, rollback, upstream)
      ▼
[английское предложение]  ── ошибка в термине → вас поймут неправильно
      │  2. выбрать артефакт (issue / runbook / чат / звонок)
      ▼
[артефакт в Git]  ── ошибка в структуре → артефакт бесполезен
      │  3. читатель: другой часовой пояс, другой контекст
      ▼
[понял и сделал]  ← цель
```

Три вещи, которые дают 80% результата:

**1. Терминологическое ядро.** В работе крутится ~200 слов. Выучите их — и «английский» внезапно станет понятным: *deploy, roll back, outage, degradation, latency, throughput, root cause, workaround, mitigation, postmortem, on-call, escalate, deprecated, breaking change, flaky, to provision, to scale, to drain, race condition, blameless*.

**2. Скелет чтения документации.** Любая страница docs.k8s.io или docs.docker.com отвечает на 4 вопроса: **What is it / Why use it / How to do it / What if it breaks (Troubleshooting)**. Сначала читайте заголовки и примеры кода, потом текст. Полное чтение «сверху вниз» — потеря времени.

**3. Формула вопроса.** Плохой вопрос: «Help, nginx not working». Хороший — четыре блока: **What I expected / What actually happened / Steps to reproduce / Environment** + точная строка ошибки в кавычках. Именно по этой формуле устроены и Stack Overflow, и GitLab issues.

Отдельно про **регистр вежливости**. В английском он выражается модальными глаголами: `Could you please…`, `Would it be possible to…`, `I'd suggest…`. Прямое «Do this now» в международной команде читается как агрессия. И наоборот: бесконечные извинения (`Sorry to bother you, I'm so sorry for my English`) съедают время и снижают доверие — достаточно одного `Thanks!` в конце.

## 3. Аналогия из жизни

Представьте морской контейнер ISO. Почему контейнеры так удобны? Потому что у них **стандартные размеры**: один и тот же ящик грузят краном на судно, ставят на фуру, тащат на склад — без переупаковки. Ваша мысль про инцидент — это груз. Английский — это контейнер: стандартная «тара» (expected/actual/repro, imperative-шаги, STAR), в которой груз без потерь едет в документацию, на Stack Overflow, в issue и на созвон.

Где аналогия ломается: контейнер, который на 5 см не влез, не примут вообще. А в языке вас простят за `I have restart nginx` — но **не простят за перепутанный термин**: если вы написали `upstream` вместо `backend` или `rollback` вместо `revert`, груз уехал не в тот порт, и получатель сделает не то.

## 4. Практика и команды

### 4.1. Заставить систему говорить по-английски

```bash
export LC_ALL=C
locale
```

`LC_ALL` перекрывает все остальные локали; `C` — «базовая» локаль, в ней все сообщения ядра и утилит на английском. Это критично: русское сообщение об ошибке вы не найдёте в Google. Проверяйте `locale` — в выводе должно быть `LC_ALL=C`.

Типичный вывод (сокращённо):

```text
LANG=ru_RU.UTF-8
LC_ALL=C
```

Смотреть на строку `LC_ALL` — она должна быть `C`. В боевой среде ничего не меняем, это только для своей сессии.

### 4.2. Поиск по man-страницам на английском

```bash
man -k "container"
man systemctl
```

`man -k` (он же `apropos`) ищет по кратким описаниям всех man-страниц. Ключ `-k` = keyword. Вывод — список `имя (раздел) — краткое описание`. Смотреть на имя страницы, потом открывать её.

### 4.3. Встроенная английская документация Kubernetes

```bash
kubectl explain deployment.spec.strategy --recursive
```

`explain` печатает описание поля прямо из API-схемы кластера. `--recursive` разворачивает вложенные поля. Это самый быстрый способ узнать, что делает параметр, не открывая браузер.

### 4.4. Краткие английские примеры вместо man

```bash
tldr rsync
```

`tldr` — клиент одноимённого проекта: 5–7 самых частых примеров вместо 3000 строк man. В Ubuntu 24.04 ставится из репозитория `[ПРОВЕРИТЬ: пакет tldr в вашем зеркале; альтернативы — npm i -g tldr, pip install tldr]`. Смотреть на строки с комментариями — они и есть английский «по делу».

### 4.5. Читаем английский лог и сразу ищем строку

```bash
journalctl -u nginx -n 50 --no-pager | grep -i "error\|failed"
```

`-u nginx` — юнит, `-n 50` — последние 50 строк, `--no-pager` — без интерактивного просмотрщика. Типичный вывод:

```text
connect() failed (111: Connection refused) while connecting to upstream,
client: 10.10.20.1, server: _, request: "POST /webhook HTTP/1.1",
upstream: "http://127.0.0.1:8080/webhook"
```

Смотреть: код в скобках (`111` = connection refused → никто не слушает порт), адрес upstream, время. Фразу `upstream prematurely closed connection` / `Connection refused` копируйте в поиск целиком, в кавычках.

### 4.6. Синтаксис поиска Stack Overflow

```text
[nginx] is:answer "connect() failed (111: Connection refused)" score:3
```

Операторы: `[тег]` — ограничить тегом, `is:answer` — только ответы (не вопросы), `score:3` — с рейтингом ≥3, кавычки — точная фраза. Это превращает поток мусора в 5 релевантных ссылок.

### 4.7. Коммит на английском в императиве

```bash
git commit -m "fix(web01): restore webhook upstream after stub crash"
```

Формат Conventional Commits: `тип(область): что сделано`. Тип — `feat`, `fix`, `docs`, `chore`, `refactor`, `ci`. После двоеточия — **императив** (`restore`, а не `restored`/`restores`): мысленно подставляйте «This commit will…». Лимит заголовка — 72 символа.

### 4.8. Проверить историю глазами ревьюера

```bash
git log --oneline --format="%h %s"
```

`%h` — короткий хеш, `%s` — subject. Смотреть, читается ли история как список действий: если да — ревьюер вас поймёт без переписки.

### 4.9. Шаблон issue на английском (GitLab)

```yaml
# .gitlab/issue_templates/bug.md
## Summary
One sentence: what is broken.

## Expected behavior
The webhook returns HTTP 200.

## Actual behavior
The webhook returns HTTP 502 since 09:12 MSK.

## Steps to reproduce
1. `curl -i -X POST http://10.10.20.10/webhook`
2. Observe the status code.

## Environment
- Host: WEB01 (10.10.20.10), nginx [версия]
- Related: HARBOR01, k8s-cp01
```

GitLab подхватывает файлы из `.gitlab/issue_templates/` — при создании issue появляется выпадающий список шаблонов `[ПРОВЕРИТЬ: настройка шаблона по умолчанию в вашей версии GitLab CE]`.

### 4.10. Шаблон merge request на английском

```markdown
<!-- .gitlab/merge_request_templates/default.md -->
## What changed
Restarted the webhook stub on WEB01 and pinned it to port 8080.

## Why
Partner integration received 502 for 40 minutes.

## How to verify
`curl -i -X POST http://10.10.20.10/webhook` → 200

## Checklist
- [ ] Runbook updated
- [ ] No secrets in the diff
```

### 4.11. Открыть MR одной командой с английским заголовком

```bash
git push -o merge_request.create \
         -o merge_request.title="docs(115): add English runbook for WEB01" \
         -o merge_request.target=main
```

Push options GitLab: `-o` передаёт инструкции серверу. `merge_request.create` создаёт MR, `merge_request.title` задаёт заголовок `[ПРОВЕРИТЬ: набор опций merge_request.* в вашей версии GitLab]`.

### 4.12. Фразы для инцидента (это тоже «конфиг» диалога)

```text
Status:    The webhook is back up since 09:54 MSK.
Cause:     The upstream process had stopped; nginx returned 502.
Action:    Restarted the service and added a health check.
Risk:      No data loss confirmed; we are monitoring for 1 hour.
Need:      Please re-send the last 3 callbacks and confirm on your side.
```

Структура Status → Cause → Action → Risk → Need работает в чате, в звонке и в postmortem.

### 4.13. PowerShell: справка на английском (если Windows-часть стенда сохранилась)

```powershell
Get-Help Get-Service -Examples
Get-Help about_Execution_Policies -Full
```

`-Examples` — только примеры, `-Full` — полная статья. Английская справка PowerShell — самый полный источник по командлетам.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Тема не техническая, поэтому лабораторная — это **практикум по рабочему английскому на вашем же стенде**: вы создадите настоящий репозиторий с англоязычными шаблонами, напишете runbook, issue и MR, отработаете поиск на Stack Overflow и проведёте два мок-диалога с записью.

### Предусловия

- Готовы ВМ DevOps-трека: **GIT01** (10.10.10.50, GitLab CE + runner), **WEB01** (10.10.20.10, nginx в DMZ, доступ через GW01), **UBU01** (10.10.10.30, SSH). Windows-часть может отсутствовать — она здесь не нужна.
- Вы входите в GitLab CE на GIT01 под своей учётной записью; репозиторий `platform-docs` создаётся в шаге 2.
- Если обратного прокси на WEB01 у вас ещё нет — минимальная настройка на месте: заглушка на `127.0.0.1:8080` и `location /webhook` в nginx, проксирующий на неё (это 6 строк конфига, показываю в шаге 4).
- Учётные записи и адреса — только со стенда «Вектор». Пароли — учебные.

### Снапшот

На **GIT01**, **WEB01** и **UBU01**: VMware Workstation → меню VM → Snapshot → Take Snapshot, имя `before-devops-115`. Для каждой ВМ — свой снапшот с этим именем. Пока ВМ включена, снапшот делать можно, но перед откатом её лучше выключить.

### Шаги

1. **UBU01.** Включаем английские сообщения: `export LC_ALL=C`, проверяем `locale`. Ожидаемо: в выводе `LC_ALL=C`, все последующие ошибки — на английском. Сразу добавляем строку в `~/.bashrc`, чтобы не повторять `[ПРОВЕРИТЬ: не всем нравится постоянно английская локаль — при желании делайте export только в рабочих сессиях]`.
2. **GIT01.** В GitLab создаём проект `vektor/platform-docs` (Visibility: Private). Ожидаемо: пустой репозиторий с инструкцией по `git clone`.
3. **UBU01 (или ваш рабочий ПК).** `git clone git@10.10.10.50:vektor/platform-docs.git`, создаём ветку `docs/115-english`. Ожидаемо: ветка создана, `git status` чистый.
4. **WEB01.** Готовим объект для документации: заглушка `python3 -m http.server 8080 --bind 127.0.0.1` через systemd-юнит `webhook-stub.service` и в nginx — `location /webhook { proxy_pass http://127.0.0.1:8080; }`. Проверяем `curl -i -X POST http://10.10.20.10/webhook`. Ожидаемо: `HTTP/1.1 501` от `http.server` (он не умеет POST) — годится: главное, что запрос доходит и проксируется.
5. **GIT01 (в репозитории).** Создаём `.gitlab/issue_templates/bug.md` и `.gitlab/merge_request_templates/default.md` из раздела 4.9–4.10 — **на английском**. Ожидаемо: файлы видны в веб-интерфейсе, при создании issue появляется выбор шаблона.
6. **GIT01.** Создаём `docs/runbooks/web01-nginx-restart.md` на английском: 5–7 нумерованных шагов в императиве, блок `Expected result` после каждого. Ожидаемо: runbook читается человеком, который эту ВМ никогда не видел.
7. **GIT01.** Заводим issue по шаблону: «Partner receives 502 from /webhook» с блоками Expected/Actual/Repro/Environment. Ожидаемо: issue создан, номер записан (например, #1).
8. **GIT01.** Коммитим: `git commit -m "docs(115): add English runbook and issue templates"`, пушим ветку с push options из 4.11. Ожидаемо: MR создан с английским заголовком, CI-runner запускает пайплайн (если он у вас настроен — иначе просто MR).
9. **Ревью на английском.** Оставляем в своём MR два комментария-замечания в вежливом регистре (`Could we add the expected status code here?`) и правим по ним документ. Ожидаемо: второй коммит `docs(115): clarify expected status code`, история читается.
10. **Поиск на Stack Overflow.** Берём реальную строку из лога (`connect() failed (111: Connection refused) while connecting to upstream`) и ищем по синтаксису 4.6. Сохраняем 2–3 ссылки в `docs/glossary-en.md` рядом с термином + коротким переводом. Ожидаемо: минимум 20 терминов в глоссарии.
11. **Мок-инцидент (5 минут).** Записываете себя на диктофон: 60 секунд статуса по схеме Status → Cause → Action → Risk → Need. Слушаете, отмечаете, где не хватило слова.
12. **Мок-собеседование (10 минут).** Отвечаете вслух по-английски на 5 вопросов из раздела 7, по 1,5–2 минуты каждый, по схеме STAR. Запись — на телефон или на хост (звук в ВМ обычно не настроен `[ПРОВЕРИТЬ: проверьте наличие аудиоустройства у UBU01]`).
13. **Журнал.** Дописываете в `stand-log.md` изменения (см. ниже) и одну строку рефлексии: «какая фраза далась тяжелее всего».

### Проверка

- [ ] На UBU01 `locale` показывает `LC_ALL=C`, ошибки утилит — на английском
- [ ] Репозиторий `vektor/platform-docs` существует и клонируется по SSH
- [ ] В `.gitlab/issue_templates/bug.md` и `.gitlab/merge_request_templates/default.md` нет русского текста
- [ ] Issue #1 содержит Expected, Actual, Steps to reproduce и Environment
- [ ] Runbook `web01-nginx-restart.md` состоит из нумерованных императивных шагов и блока Expected result
- [ ] MR создан, заголовок в формате Conventional Commits, длина ≤ 72 символов
- [ ] `git log --oneline` читается как список действий без пояснений
- [ ] В `docs/glossary-en.md` ≥ 20 терминов с переводом и хотя бы 2 ссылки на источники
- [ ] Есть две записи: мок-инцидент и мок-собеседование
- [ ] Запись добавлена в `stand-log.md`

### Сломай и почини

```text
Пользователь: инженер партнёра-эквайера (внешний)
ВМ:           WEB01 (10.10.20.10)
Что написал:  "Hi, our webhook receiver gets 502 from your endpoint since 09:12 MSK.
               Order confirmations are stuck. Could you check the upstream?"
Срочность:    высокая
```

**Как воспроизвести поломку.** На WEB01: `sudo systemctl stop webhook-stub` (или `sudo pkill -f "http.server 8080"`). nginx при этом жив, но проксировать некуда.

**Диагностика по шагам.**

1. Подтверждаем симптом снаружи: `curl -i -X POST http://10.10.20.10/webhook` → `HTTP/1.1 502 Bad Gateway`. Симптом воспроизведён, идём внутрь.
2. Смотрим, жив ли сам nginx: `systemctl is-active nginx` → `active`. Значит, проблема не в nginx, а в upstream.
3. Читаем лог: `journalctl -u nginx -n 50 --no-pager | grep -i "error\|failed"` → `connect() failed (111: Connection refused) while connecting to upstream, upstream: "http://127.0.0.1:8080/webhook"`. Код `111` + адрес = никто не слушает 8080.
4. Проверяем, слушает ли кто-то порт: `ss -ltnp | grep 8080` → пусто.
5. Проверяем статус сервиса: `systemctl status webhook-stub --no-pager` → `inactive (dead)`, в логе — падение.
6. Поднимаем: `sudo systemctl start webhook-stub && systemctl is-active webhook-stub` → `active`.
7. Контрольный выстрел: `curl -i -X POST http://10.10.20.10/webhook` → `HTTP/1.1 501` (заглушка не умеет POST, но связность есть) — фиксируем это в ответе, чтобы партнёр не удивился.

**Ответ пользователю (по-английски, шаблон):**

```text
Hi [name],

Status: the endpoint is back up since [HH:MM] MSK.
Cause:  the upstream process behind nginx had stopped, so nginx returned 502.
Action: we restarted the service and added a health check to catch this earlier.
Risk:   we see no evidence of lost callbacks, but please re-send the last three
        and confirm they are processed on your side.
We are monitoring for the next hour. Sorry for the disruption.

Best regards,
[имя], DevOps, Vektor
```

**Закрытие заявки:** в тикете — резюме на английском (1 абзац), ссылка на issue #1, ссылка на обновлённый runbook, отметка «monitoring 1h», затем закрытие через час при отсутствии повторов.

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** новый проект GitLab `vektor/platform-docs`; ветка `docs/115-english`; файлы `docs/runbooks/web01-nginx-restart.md`, `docs/glossary-en.md`, `.gitlab/issue_templates/bug.md`, `.gitlab/merge_request_templates/default.md`; issue #1, MR #1.
- **WEB01 (10.10.20.10):** systemd-юнит `webhook-stub.service`, слушающий `127.0.0.1:8080`; в nginx добавлен `location /webhook` с `proxy_pass` на заглушку.
- **UBU01 (10.10.10.30):** `LC_ALL=C` в `~/.bashrc`; локальная копия репозитория `platform-docs`.
- **Прочее:** записи мок-инцидента и мок-собеседования; запись в `stand-log.md`.

### Откат

VMware Workstation → выбрать ВМ → Snapshot Manager → снапшот `before-devops-115` → Revert. Повторить на GIT01, WEB01 и UBU01. Если откатывать не хочется (стенд в рабочем состоянии), оставьте всё как есть — проект и шаблоны пригодятся в следующих главах; в этом случае обязательно зафиксируйте изменения в `stand-log.md`, иначе через неделю не вспомните, откуда на WEB01 взялся непонятный `webhook-stub`.

## 6. Ошибки новичков и безопасность

1. **«Сначала переведу всё на русский, потом буду думать»** → двойная работа и потеря точных терминов. Правильно: работайте в английских терминах, перевод — только для себя, в глоссарии.
2. **Вопрос без контекста: «Help, nginx 502»** → на Stack Overflow минусуют и закрывают, в чате переспрашивают трижды. Правильно: Expected / Actual / Repro / Environment + точная строка ошибки в кавычках.
3. **Машинный перевод документации без проверки** → «roll back» превращается в «откатить назад» (тавтология), а `deprecated` — в «устаревший» там, где имеется в виду «ещё работает, но скоро исчезнет». Правильно: переводите смысл, а не слова; технические термины оставляйте как есть.
4. **Извинения вместо сути** («Sorry for my bad English, sorry to disturb…») → собеседник не понимает, чего вы хотите. Правильно: один `Thanks!` в конце, в начале — суть.
5. **Заголовок коммита в прошедшем времени** (`fixed bug`) → история не читается как список действий. Правильно: императив, `fix(web01): …`.
6. **Смешивание языков в одном артефакте** → половина команды не понимает половину документа. Правильно: один артефакт — один язык; в международном проекте — английский полностью, включая названия веток.
7. **«ASAP» без часового пояса** → вас ждут в 9 утра по Берлину, а вы в это время спите. Правильно: `by 14:00 MSK (12:00 CET)`.
8. **Устный ответ без структуры** → на интервью кажется, что вы не понимаете, о чём говорите. Правильно: STAR и одно предложение-итог в конце.

### Безопасность

Публичный Stack Overflow и GitHub issues — это **внешние площадки, которые индексируются навсегда**. Нельзя: вставлять в вопрос реальные IP (`10.10.10.x`, `10.10.20.x`), внутренние имена хостов (`GIT01`, `HARBOR01`, `k8s-cp01`), токены, `.env`, kubeconfig, приватные ключи, куски логов с ФИО и номерами заказов. В лабораторной выше всё это безопасно, потому что стенд вымышленный — но привычку формируйте сразу: перед публикацией прогоняйте текст через `sed`-замену адресов на `x.x.x.x`, а секреты заменяйте на `<REDACTED>`. В боевой среде утечка kubeconfig в публичный вопрос = компрометация кластера, а утечка клиентских данных — ещё и юридические последствия. И отдельно: не устанавливайте «удобные» плагины и «ускорители английского» из непроверенных источников на рабочие машины — только официальные пакеты и магазины расширений.

## 7. Вопросы на собеседовании

**1. "Could you tell me about yourself and your DevOps experience?"**

Ответ: `I'm a system administrator moving into DevOps. In my current lab I run a small platform: GitLab CE with a CI runner on GIT01, a Harbor registry, a three-node Kubernetes cluster, and monitoring on MON01. I automated image builds and deployments with GitLab CI and described the infrastructure in Git — runbooks, issue templates and ADRs live in the same repository. Right now I'm deepening my knowledge of observability and reliability.`

*Что хочет услышать интервьюер:* 60–90 секунд, конкретика (инструменты, что именно сделали), движение к цели. *Красный флаг:* пересказ резюме слово в слово на пять минут и «I don't know, my English is bad» в первых 10 секундах.

**2. "Describe a production incident you handled. What was the root cause and how did you fix it?"**

Ответ: `We had a 502 on the webhook endpoint. The symptom was reported by a partner. I reproduced it with curl, then checked nginx logs and saw "connect() failed (111: Connection refused)" — so nginx was fine and the upstream was not listening. The root cause was a crashed upstream process. I restarted the service, added a health check, and confirmed with the partner that no callbacks were lost. I also added a runbook entry so the next on-call engineer can do it in two minutes.`

*Что хочет услышать интервьюер:* структуру (симптом → диагностика → причина → фикс → профилактика) и владение терминами. *Красный флаг:* «это была не моя вина, всё сломал разработчик» и отсутствие цифр/деталей.

**3. "How do you document your work? Do you write runbooks or ADRs?"**

Ответ: `Yes. I keep three kinds of documents: ADRs for decisions and their trade-offs, runbooks for step-by-step recovery, and a short onboarding guide. All of them live in Git next to the code, in Markdown, in English, so they are versioned and reviewed like code. A runbook has numbered imperative steps and an expected result after each step, because it's read at 3 a.m. by a tired on-call engineer.`

*Что хочет услышать интервьюер:* что документация для вас — часть инженерной работы, а не «потом напишу». *Красный флаг:* «документация в Confluence, но она устарела, я туда не захожу».

**4. "Imagine I'm a developer and I tell you the deploy is broken. Walk me through the conversation."**

Ответ: `First I ask what exactly they see: error message, environment, since when. Then I check whether the pipeline failed or the deployment succeeded but the app misbehaves — those are different problems. I share what I see, for example "the pipeline is green, but the pod is in CrashLoopBackOff", and we agree on the next step and the time when I report back. I always end with a concrete status: what I'm doing, what I need from them, and when I'll update.`

*Что хочет услышать интервьюер:* что вы не спорите, а сужаете проблему и держите собеседника в курсе. *Красный флаг:* «я бы посмотрел логи и сказал, что это не моя зона».

**5. "Your English is intermediate. How do you handle a call when you don't understand something?"**

Ответ: `I ask directly: "Could you repeat that more slowly?" or "Do you mean the ingress or the service?" If it's a long call, I write key points in the chat and confirm them: "So the plan is A, B, C — correct?" That way miscommunication is caught immediately. And I prepare a short agenda before the call, so I always know the vocabulary I'll need.`

*Что хочет услышать интервьюер:* зрелость — вы не прячетесь и не киваете из вежливости, а закрываете непонимание инструментами. *Красный флаг:* «I will say yes and then figure it out» — это прямой путь к инциденту.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Documentation → Concepts* и справка по `kubectl explain`. Поиск: `kubernetes documentation concepts`. Основной сайт документации K8s.
- **docs.gitlab.com** (домен `docs.gitlab.com`) — статьи *Description templates* и *Push options*. Поиск: `gitlab description templates issue templates`. `[ПРОВЕРИТЬ: точные названия разделов в текущей версии]`
- **git-scm.com** — книга *Pro Git*, глава про написание сообщений коммитов. Поиск: `pro git commit guidelines 50 72`.
- **conventionalcommits.org** — спецификация Conventional Commits. Поиск: `conventional commits specification`.
- **stackoverflow.com** — справочный центр, статьи *How do I ask a good question?* и *How to create a Minimal, Reproducible Example*. Поиск: `how to create a minimal reproducible example`.
- **rfc-editor.org** — документ *RFC 2119* (ключевые слова MUST / SHOULD / MAY — их вы встретите в любой спецификации). Поиск: `rfc 2119 key words for use in rfcs`.
- **developers.google.com** — курс *Technical Writing One / Technical Writing Two*. Поиск: `google technical writing course`. `[ПРОВЕРИТЬ: актуальный URL курса]`
- **learn.microsoft.com** — раздел *PowerShell → Get-Help* и статьи `about_*`. Поиск: `about comment based help powershell`.
- **dictionary.cambridge.org** — словарь с коллокациями и озвучкой; ищите не отдельное слово, а коллокацию (`handle an incident`). Поиск: `cambridge dictionary collocations`.
- **Оценка уровня по CEFR (A1–C2):** официальные таблицы самооценки публикует Совет Европы, домен `coe.int`. `[ПРОВЕРИТЬ: точный адрес страницы самооценки]` Поиск: `cefr self-assessment grid`.
