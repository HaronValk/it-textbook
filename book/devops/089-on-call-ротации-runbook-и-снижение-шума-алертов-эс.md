# On-call: ротации, runbook'и, снижение шума алертов, эскалации, MTTD/MTTR

## 1. Введение (зачем это нужно в реальной работе)

**03:41, вторник.** Тебе на телефон падает уведомление от бота: `[CRITICAL] PortalErrorBudgetBurnFast`. Через две минуты — ещё семнадцать сообщений: `NodeDiskPressureWarning`, `ContainerCPUHigh`, `PodRestartInfo`, `CertificateExpiresIn30Days`. Ещё через десять минут в Service Desk появляется тикет:

> **Пользователь:** petrova (HR)  
> **Что написала:** «Портал записи на отпуск не открывается с вечера, пишет 502. У нас завтра отчёт, очень срочно».  
> **Срочность:** Высокая

Ты открываешь дашборд, потом графану, потом консоль, потом начинаешь вспоминать, какой именно сервис за портал отвечает и где он вообще живёт. К 04:30 ты находишь причину — упавший nginx на WEB01. К 04:40 — починил. То есть **обнаружили за 30 секунд, а чинили полтора часа**, из которых 40 минут ушло на «а что вообще происходит».

Именно эту разницу и лечит тема этой главы. On-call — это не «мужество в 4 утра», а инженерная система: расписание дежурств, заранее написанные инструкции, алерты, которые не спамят, понятная лестница эскалации и две метрики, по которым видно, стало ли лучше. В работе DevOps-инженера это встречается постоянно: как только твой сервис начинает обслуживать других людей, кто-то должен отвечать за него ночью. И этот кто-то должен иметь инструменты, а не героизм.

После этой главы ты будешь уметь: построить ротацию и передачу смены, написать runbook, который реально помогает в 4 утра, настроить маршрутизацию и подавление алертов в Alertmanager, посчитать MTTD и MTTR из своих же данных и объяснить на собеседовании, почему «алертов должно быть меньше» — это не лень, а инженерная позиция.

## 2. Теория «под капотом»

### Где это живёт в системе

On-call — процесс поверх трёх технических слоёв: **измерение** (метрики/пробы), **оповещение** (Alertmanager + канал доставки) и **знание** (runbook в Git). Ломается любой из них — и дежурный остаётся один на один с ночью.

```text
   ИСТОЧНИКИ СИГНАЛА              РЕШЕНИЕ О ТРЕВОГЕ          ДОСТАВКА И РЕАКЦИЯ
┌──────────────────────┐    ┌───────────────────────┐   ┌──────────────────────────┐
│ blackbox exporter    │    │ Prometheus / VM       │   │ Alertmanager :9093       │
│ node-exporter        │───▶│ alerting rules        │──▶│  1. group_by/group_wait  │
│ kube-state-metrics   │    │  for: <сколько ждать> │   │  2. inhibit_rules        │
│ (метрики приложений) │    │  labels: severity     │   │  3. silences             │
└──────────────────────┘    └───────────────────────┘   │  4. routes → receivers   │
                                                         └───────────┬──────────────┘
                                                                     │
                                       ┌─────────────────────────────┼──────────────────────┐
                                       ▼                             ▼                      ▼
                                chat (Mattermost/           телефон/SMS           тикет + runbook_url
                                Telegram), severity=info    severity=critical      (GitLab GIT01)
                                       │                             │                      │
                                       └────────── ack дежурного ────┴──── эскалация через N минут
```

Ключевой принцип: **метрики и алерты — это код в Git**, а не клики в веб-интерфейсе. Правило в Prometheus (`PrometheusRule` в Kubernetes), конфиг Alertmanager, файл ротации, runbook — всё лежит в репозитории, проходит ревью и ревёртится одной кнопкой.

### Пять понятий, которые надо держать в голове

**Ротация (rotation).** Расписание, ктоprimary (первый), кто secondary (второй), когда передача смены (обычно утро понедельника), где handoff-документ. Хорошая ротация — неделя, не больше: дольше — выгорание, короче — не успеваешь вникнуть в контекст.

**Runbook.** Пошаговая инструкция «что делать, когда сработал алерт X». Не документация архитектуры и не эссе — только действия с ожидаемым результатом и точками ветвления. В идеале — исполняемая: часть шагов автоматизирована скриптом или CI-джобом.

**Снижение шума (noise reduction).** Четыре механизма Alertmanager:
- `group_by` + `group_wait` + `group_interval` — склеивание похожих алертов в одно уведомление;
- `inhibit_rules` — «если уже горит причина, не кричи про следствия»;
- `silences` — ручное подавление на время плановых работ (с обязательным сроком и комментарием);
- `severity` + маршруты — разные алерты в разные каналы.

Плюс идеологический уровень: алертить на **симптом** (пользователю плохо: probe_success=0), а не на **причину** (CPU 80%). CPU 80% — это повод посмотреть дашборд, а не звонить ночью.

**Эскалация (escalation).** Лестница: через N минут без ack — следующий уровень. Не «позвонить директору сразу», а «primary в чат → primary на телефон → secondary → инженер сервиса → тимлид». Эскалация — это не наказание, это страховка от того, что человек спит.

**Метрики надёжности:**

```text
T0 ───────────────── T1 ──────── T2 ────────────────────── T3
поломка           первый алерт  взял в работу         восстановлено
   |<--- MTTD --->|             |                      |
                   |<-- MTTA -->|
   |<--------------------- MTTR (time to restore) ---->|

MTTD  = T1 - T0   Mean Time To Detect     — качество мониторинга
MTTA  = T2 - T1   Mean Time To Acknowledge — качество ротации и каналов
MTTR  = T3 - T0   Mean Time To Recover     — качество runbook'ов и автоматизации
```

Три практических следствия. Первое: `for: 10m` в правиле алертинга — это **прямая добавка к MTTD**, закладывай осознанно. Второе: MTTR падает не от того, что инженер быстрее печатает, а от того, что он **быстрее узнаёт, куда смотреть**. Третье: если MTTD уже 30 секунд, а MTTR — час, дальше улучшать мониторинг бессмысленно, узкое место в знаниях и автоматизации.

## 3. Аналогия из жизни

Представь пожарную часть. Есть **караулы** — сегодня дежурит первая смена, завтра вторая, и есть начальник караула, который поднимается по тревоге, если дежурный не отвечает (ротация и эскалация). В здании стоят **датчики дыма**, а не датчики «температура в котельной 78 градусов»: пожарный реагирует на дым, потому что дым — это симптом пожара, а высокая температура в котельной может быть нормой (алертим на симптом, не на причину). Диспетчер **объединяет** сто звонков про один и тот же пожар в один вызов и **не отправляет** вторую машину, если первая уже выехала по этому адресу (группировка и инхибиция). Когда в здании планово меняют проводку, диспетчер вешает табличку «работы до 18:00, реагировать только на реальное задымление» — с датой, подписью и номером наряда (silence). А на стене у каждого пожарного щита висит **инструкция**: «1. Надеть дыхательный аппарат. 2. Проверить, не заперта ли дверь. 3. Если горит электрощит — рубильник, не вода» (runbook). Пожарный не изобретает тактику на месте — он читает проверенный текст.

Где аналогия ломается: у пожара есть физический адрес и его видно глазами, а у инцидента в микросервисах адреса нет — «горит» одновременно пять компонентов, и понять, кто из них причина, а кто следствие, можно только по графу зависимостей и временным корреляциям.

## 4. Практика и команды

Все примеры — для стенда «Вектор»: кластер `k8s-cp01`/`k8s-w01`/`k8s-w02` с `kube-prometheus-stack` (урок 84), портал на WEB01 под блэкбокс-пробой (урок 85), GitLab на GIT01.

### 4.1. Доступ к Alertmanager и проверка состояния

```bash
kubectl -n monitoring get pods | grep -E 'alertmanager|prometheus'
kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 &
curl -s http://127.0.0.1:9093/api/v2/status | jq '.cluster.status'
```

`port-forward` — временный туннель с твоей машины в под (в бою так не делают, там Ingress с авторизацией). `/api/v2/status` — состояние кластера Alertmanager; для одиночного инстанса `disabled`, для HA-пары — `ready`. Имя сервиса в `kube-prometheus-stack` строится как `<release>-alertmanager`; если у тебя релиз назывался иначе — поправь имя.

### 4.2. Валидация конфига Alertmanager

```bash
amtool check-config alertmanager.yml
```

Типичный вывод: `Checking 'alertmanager.yml'  SUCCESS` / `Found: global config, route, 2 receivers, 1 inhibit rule`. Ловит опечатки в матчерах, дубли имён ресиверов, пустые маршруты — до того, как конфиг применён и алерты улетели в никуда.

### 4.3. Маршрутизация по severity (фрагмент `alertmanager.yml`)

```yaml
route:
  receiver: chat-sre
  group_by: ['alertname', 'namespace', 'service']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['severity="critical"']
      receiver: phone-sre
      group_wait: 10s
      repeat_interval: 30m
      continue: false
      routes:
        - matchers: ['service="portal"']
          receiver: phone-sre-portal
    - matchers: ['severity=~"info|warning"']
      receiver: chat-sre
      repeat_interval: 12h
inhibit_rules:
  - source_matchers: ['alertname="PortalDown"', 'severity="critical"']
    target_matchers: ['service="portal"', 'severity=~"warning|info"']
    equal: ['instance']
```

- `group_by` — по каким меткам склеивать; `alertname`+`service` даёт одно сообщение на сервис вместо пятидесяти.
- `group_wait: 30s` — сколько ждать, чтобы дособрать пачку (это же время добавляется к MTTD!).
- `group_interval: 5m` — как часто слать обновления по уже открытой пачке.
- `repeat_interval: 4h` — «алерт всё ещё горит, напомни». Не ставь 5 минут — получишь спам; не ставь 24 часа — проворонишь.
- `continue: false` — остановить обработку после совпадения (важно: по умолчанию тоже false, но лучше писать явно).
- `inhibit_rules`: пока `PortalDown` активен, не шлём `warning`-алерты по тому же `instance` — они почти гарантированно следствия.

Поле `matchers` — синтаксис Alertmanager 0.22+. В старых версиях было `match`/`match_re`; если конфиг не принимается — проверь версию.

### 4.4. Проверка маршрута без отправки

```bash
amtool config routes test --config.file=alertmanager.yml --verify severity=critical service=portal
```

Показывает, какой ресивер получит алерт с такими метками. `--verify` дополнительно проверяет, что все ресиверы, упомянутые в маршрутах, определены. `[ПРОВЕРИТЬ]` набор флагов в твоей версии amtool — сверься с `amtool config routes test --help`.

### 4.5. Silence на плановые работы

```bash
amtool --alertmanager.url=http://127.0.0.1:9093 silence add \
  alertname="PortalProbeFailed" instance=~"https://portal.*" \
  --author="adm.student" \
  --comment="CHG-1042 плановое обновление nginx на WEB01" \
  --duration=2h

amtool --alertmanager.url=http://127.0.0.1:9093 silence query
amtool --alertmanager.url=http://127.0.0.1:9093 silence expire <SILENCE_ID>
```

`--duration` (например `2h`, `45m`) или `--expire` с точной датой в RFC3339. Обязательно `--author` и `--comment` со ссылкой на заявку на изменение: silence без тикета — это способ тихо проспать аварию. Вывод `silence query` показывает ID, матчеры и время окончания — смотри, чтобы не висело «до 2030 года».

### 4.6. Просмотр активных алертов

```bash
amtool --alertmanager.url=http://127.0.0.1:9093 alert query
amtool --alertmanager.url=http://127.0.0.1:9093 alert query severity=critical
```

Вывод: имя алерта, `startsAt`, состояние (`active` / `suppressed` / `unprocessed`). `suppressed` — значит сработал silence или инхибиция. Это первое, что проверяют при «мониторинг молчит»: не подавлен ли сигнал.

### 4.7. Правило с SLO и burn rate (multi-window)

```yaml
groups:
  - name: slo-portal
    rules:
      - alert: PortalErrorBudgetBurnFast
        expr: |
          (1 - avg_over_time(probe_success{job="blackbox-http",instance=~"https://portal.*"}[5m])) > (14.4 * 0.001)
          and
          (1 - avg_over_time(probe_success{job="blackbox-http",instance=~"https://portal.*"}[1h])) > (14.4 * 0.001)
        for: 2m
        labels:
          severity: critical
          service: portal
        annotations:
          summary: "Портал: бюджет ошибок горит со скоростью 14.4x"
          runbook_url: "https://git01.corp.vektor.lab/sre/runbooks/-/blob/main/portal/availability.md"
      - alert: PortalDown
        expr: probe_success{job="blackbox-http",instance=~"https://portal.*"} == 0
        for: 1m
        labels:
          severity: critical
          service: portal
        annotations:
          summary: "Портал недоступен (blackbox-проба не проходит)"
          runbook_url: "https://git01.corp.vektor.lab/sre/runbooks/-/blob/main/portal/down.md"
```

Смысл multi-window: короткое окно (5m) даёт быструю реакцию, длинное (1h) отсекает одиночные всплески. `14.4` — множитель прожига для SLO 99.9% (бюджет 0.001), классическая пара «2% бюджета за час». Порог и окна подбирай под свой SLO и трафик — цифры не догма.

### 4.8. Валидация и юнит-тест правил

```bash
yq '.spec.groups' prometheusrule-portal.yaml > /tmp/groups.yml
promtool check rules /tmp/groups.yml
promtool test rules portal.rules.test.yml
```

`check rules` — синтаксис и уникальность имён. `test rules` — прогон на синтетических временных рядах (`/bin/promtool` есть внутри образа `prom/prometheus`). В тесте описываешь `input_series` и ожидаемые алерты в `alert_rule_test`; набор меток в `exp_labels` должен совпадать точно — запусти, посмотри вывод и поправь.

```yaml
# portal.rules.test.yml
rule_files:
  - /tmp/groups.yml
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: 'probe_success{job="blackbox-http",instance="https://portal.corp.vektor.lab"}'
        values: '1x5 0x10'
    alert_rule_test:
      - eval_time: 12m
        alertname: PortalDown
        exp_alerts:
          - exp_labels:
              severity: critical
              service: portal
              job: blackbox-http
              instance: https://portal.corp.vektor.lab
```

### 4.9. Правила как код: проверка на каждом MR (GitLab CI на GIT01)

```yaml
# .gitlab-ci.yml в репозитории sre/runbooks
stages: [lint]

validate-alerts:
  stage: lint
  image:
    name: prom/prometheus:latest
    entrypoint: [""]
  script:
    - /bin/promtool check rules alerts/*.rules.yml
    - /bin/promtool test rules alerts/*.test.yml
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      changes: [alerts/**/*]
```

Теперь сломанное правило не попадёт в main: ревьюер видит зелёный пайплайн или красный. `entrypoint: [""]` нужен потому, что образ prometheus по умолчанию стартует как сервер.

### 4.10. MTTD и MTTR из данных Alertmanager

```bash
curl -s http://127.0.0.1:9093/api/v2/alerts | jq -r '
  .[] | [.labels.alertname, .labels.severity, .startsAt, (.endsAt // "open")] | @tsv'
```

Даёт таблицу «алерт / важность / начало / конец». Разница между `endsAt` и `startsAt` — длительность инцидента на уровне сигнала (полезно как прокси MTTR, но не путай: алерт может закрыться по `for`-условию, когда симптом ушёл, а последствия остались). Для настоящего MTTR нужен трекер инцидентов; на стенде сойдёт ручная таблица в `stand-log.md`.

MTTD считают иначе — от реального момента поломки до первого алерта. На стенде: посмотри в Grafana график `probe_success` и сравни время первого провала с `startsAt` алерта. Разница = `for:` + `group_wait` + задержка скрейпа. Это позволяет честно ответить на вопрос «почему мы узнали через 3 минуты».

### 4.11. Ротация как код (`oncall/rotation.yaml`)

```yaml
rotation:
  name: devops-primary
  timezone: Europe/Moscow
  handoff: "Пн 10:00"
  primary:
    - {week: "2026-W10", engineer: "adm.student", backup: "adm.student"}
  escalation:
    - {step: 1, after: "0m",  action: "чат #sre-alerts"}
    - {step: 2, after: "10m", action: "звонок primary"}
    - {step: 3, after: "25m", action: "звонок secondary + тимлид"}
```

На стенде дежурный один (ты), и `backup` совпадает с `primary` — это честно отражает реальность учебного стенда. В рабочей среде такого быть не должно: минимум два человека в ротации, иначе отпуск/болезнь = отсутствие on-call.

### 4.12. Runbook как файл в Git

```bash
git switch -c runbook/portal-down
# создаём portal/down.md по шаблону RUNBOOK_TEMPLATE.md
git add portal/down.md && git commit -m "runbook: PortalDown (portal)"
git push -u origin runbook/portal-down
# далее Merge Request в GitLab UI: Merge requests → New merge request
```

Путь в GUI GitLab: проект `sre/runbooks` → **Code → Branches / Merge requests → New merge request**. Ревью runbook'а вторым инженером — не бюрократия: человек, который не писал инструкцию, найдёт в ней пропущенные шаги.

## 5. Лабораторная работа

### Предусловия

- Кластер `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63) — рабочий, с установленным `kube-prometheus-stack` из урока 84.
- WEB01 (10.10.20.10, nginx) — доступен, блэкбокс-проба на портал настроена (урок 85), алерт по недоступности существует хотя бы в черновом виде.
- GIT01 (10.10.10.50, GitLab CE + runner) — доступен, у `adm.student` есть права создать группу `sre`.
- Рабочая станция для `kubectl` — UBU01 (10.10.10.30), kubeconfig настроен.
- Прочитаны уроки 84–88. Если кластера ещё нет — сначала урок 84, иначе лабораторная не соберётся.

### Снапшот

В VMware Workstation на **k8s-cp01, k8s-w01, k8s-w02, GIT01, WEB01**: `VM → Snapshot → Take Snapshot`, имя `before-devops-089` (для всех пяти — одинаковое имя, чтобы откат был одной операцией).

### Шаги

1. **UBU01.** Проверь доступ к кластеру и подними туннель:
   `kubectl get nodes` → три узла `Ready`; `kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 &`
   **Ожидаемо:** `curl -s http://127.0.0.1:9093/api/v2/status` возвращает JSON со `status`.

2. **GIT01 (браузер).** Создай группу `sre` и проект `runbooks` (видимость Internal, README включён). Склонируй на UBU01 в `~/work/runbooks`.
   **Ожидаемо:** `git remote -v` показывает `git@git01.corp.vektor.lab:sre/runbooks.git` (или http-URL).

3. **UBU01.** Создай скелет: каталоги `portal/`, `alerts/`, `oncall/` и файл `RUNBOOK_TEMPLATE.md` с блоками: «Симптомы», «Влияние на пользователей», «Быстрая проверка (2 минуты)», «Диагностика», «Восстановление», «Эскалация», «Связанные дашборды».
   **Ожидаемо:** `tree -L 2` показывает структуру, файлы закоммичены и запушены.

4. **UBU01.** Напиши `portal/down.md` по шаблону, конкретно под стенд: первая проверка — `curl -kI https://10.10.20.10/` с UBU01; вторая — `systemctl status nginx` на WEB01; третья — `ss -ltnp | grep :443`.
   **Ожидаемо:** в файле ≥6 шагов, у каждого указана ВМ и ожидаемый результат.

5. **UBU01.** Добавь `runbook_url` в аннотации `PortalDown` и `PortalErrorBudgetBurnFast`, примени: `kubectl apply -f alerts/prometheusrule-portal.yaml`, затем `kubectl -n monitoring get prometheusrule -o yaml | grep runbook_url`.
   **Ожидаемо:** ссылка присутствует в живом объекте, `kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &` → `curl -s localhost:9090/api/v1/rules | jq '.data.groups[].rules[].annotations'` показывает тот же URL.

6. **UBU01.** Опиши маршруты и инхибицию (п. 4.3) в `alerts/alertmanager-values.yaml`, примени через `helm upgrade kube-prometheus-stack ... -f alerts/alertmanager-values.yaml`, дождись `kubectl -n monitoring rollout status sts/alertmanager-kube-prometheus-stack-alertmanager`.
   **Ожидаемо:** под Alertmanager перезапустился, `amtool check-config` на извлечённом конфиге даёт SUCCESS.

7. **UBU01.** Прогони `amtool config routes test --config.file=alertmanager.yml --verify severity=critical service=portal` и то же для `severity=warning`.
   **Ожидаемо:** для critical — ресивер `phone-sre-portal`, для warning — `chat-sre`.

8. **UBU01.** Добавь `alerts/portal.rules.test.yml` и CI-джоб из п. 4.9, запушь в ветку и открой MR.
   **Ожидаемо:** пайплайн `validate-alerts` зелёный; специально сломай `expr` (убери скобку), запушь — пайплайн красный, верни обратно.

9. **UBU01.** Создай `oncall/rotation.yaml` и `oncall/escalation.md` (лестница из п. 4.11 + правило «эскалируй, если через 15 минут нет гипотезы»). Закоммить.
   **Ожидаемо:** файлы в main, MR смержен.

10. **WEB01.** Учебная тревога: `sudo systemctl stop nginx`. Засеки время. Смотри, когда придёт уведомление в канал.
    **Ожидаемо:** `PortalDown` в статусе `active` (проверь `amtool alert query`), уведомление в чате появилось через ~1–2 минуты (`for: 1m` + `group_wait`).

11. **UBU01.** Пройди свой же runbook по шагам, восстанови `sudo systemctl start nginx` на WEB01, зафиксируй T3.
    **Ожидаемо:** `curl -kI https://10.10.20.10/` возвращает `200`, алерт уходит в `resolved`, в Grafana `probe_success` снова 1.

12. **UBU01.** Поставь silence на 30 минут по `alertname="PortalProbeFailed"` с комментарием «тест silence, урок 89», снова останови nginx, проверь `amtool alert query` (алерт `suppressed`), затем `amtool silence expire <ID>` и запусти nginx.
    **Ожидаемо:** при активном silence уведомлений в чате нет; после `expire` алерт снова виден и уведомление уходит.

13. **UBU01.** Посчитай MTTD и MTTR: выгрузи алерты (п. 4.10), сопоставь с временем `systemctl stop/start`. Запиши три числа в `stand-log.md`: MTTD, MTTA (когда заметил/подтвердил), MTTR.
    **Ожидаемо:** таблица в `stand-log.md` и абзац «что было узким местом» — обычно это не диагностика, а поиск нужной ВМ.

### Проверка

- [ ] `curl -s http://127.0.0.1:9093/api/v2/status` отвечает, Alertmanager жив
- [ ] В GIT01 есть проект `sre/runbooks` с `RUNBOOK_TEMPLATE.md`, `portal/down.md`, `oncall/rotation.yaml`
- [ ] `runbook_url` виден в живом `PrometheusRule` и в `/api/v1/rules`
- [ ] В Alertmanager два ресивера, маршрут по `severity` проверен `amtool config routes test`
- [ ] `inhibit_rules` содержат правило «PortalDown подавляет warning по тому же instance»
- [ ] `promtool check rules` и `promtool test rules` проходят локально и в пайплайне MR
- [ ] Учебная тревога: алерт `active`, уведомление пришло, алерт ушёл в `resolved` после починки
- [ ] Silence ставится и снимается, в `silence query` есть автор и комментарий
- [ ] В `stand-log.md` записаны MTTD/MTTA/MTTR и вывод «где узкое место»

### Сломай и почини

> **Пользователь:** petrova (HR)  
> **ВМ:** WIN10-CL02 → портал на WEB01  
> **Что написала:** «Портал записи на отпуск не открывается с 9 утра, 502. У нас отчёт, очень срочно. Почему вы узнаёте об этом от меня?»  
> **Срочность:** Высокая

**Как воспроизвести поломку (намеренно):**

1. На UBU01 поставь «забытый» silence на сутки: `amtool --alertmanager.url=http://127.0.0.1:9093 silence add alertname="PortalDown" --author="adm.student" --comment="разбираюсь" --duration=24h`
2. В `alertmanager-values.yaml` поменяй `repeat_interval: 24h` и `group_wait: 15m`, примени.
3. На WEB01: `sudo systemctl stop nginx`.

**Диагностика по порядку:**

1. `amtool alert query service=portal` — есть ли алерт и в каком он статусе. Видим `suppressed` → **сразу** `amtool silence query`, ищем активный silence по `PortalDown`. Это шаг №1, потому что «мониторинг молчит» чаще всего означает подавление, а не отсутствие правила.
2. `amtool config routes test --config.file=alertmanager.yml --verify severity=critical service=portal` — маршрут жив? Если ресивер не тот — проблема в конфиге, а не в silence.
3. `curl -kI https://10.10.20.10/` с UBU01 — подтверждаем, что портал правда лежит (жалоба пользователя — не всегда правда).
4. На WEB01: `systemctl status nginx`, `journalctl -u nginx -n 50 --no-pager` — ищем причину.
5. `kubectl -n monitoring get prometheusrule` + `curl -s localhost:9090/api/v1/rules | jq` — правило вообще загружено?
6. Grafana → дашборд портала → `probe_success` за последние 6 часов: когда реально начался провал (это T0 для MTTD).

**Решение:**

```bash
amtool --alertmanager.url=http://127.0.0.1:9093 silence expire <SILENCE_ID>
# вернуть group_wait: 30s и repeat_interval: 30m для critical, применить values
sudo systemctl start nginx   # на WEB01
curl -kI https://10.10.20.10/   # ожидаем 200
```

**Что написать пользователю:** «Мария, портал восстановлен, проверьте, пожалуйста. Причина — остановка веб-сервера, доступ уже есть. Мы отдельно разбираем, почему оповещение не сработало вовремя, и вернёмся с итогом до конца дня».

**Закрытие заявки:** категория «Сервис недоступен», в описании — ссылка на `portal/down.md` и номер silence, который помешал обнаружению. Отдельной задачей — в `alerts/` добавить в CI-проверку предупреждение, если silence создан без ссылки на тикет (простая grep-проверка в пайплайне), и пункт в runbook: «шаг 0 — проверь активные silences».

### Что изменилось на стенде

- **GIT01:** появились группа `sre` и проект `runbooks` с каталогами `portal/`, `alerts/`, `oncall/`, шаблоном `RUNBOOK_TEMPLATE.md`, CI-джобом `validate-alerts`.
- **k8s-cp01/w01/w02:** Alertmanager перенастроен (маршруты по severity, `group_wait`/`group_interval`/`repeat_interval`, `inhibit_rules`); добавлены/обновлены `PrometheusRule` с `runbook_url` и burn-rate алертом; применены unit-тесты правил.
- **WEB01:** без постоянных изменений (nginx останавливался и запускался в рамках учений).
- **UBU01:** локальная копия репозитория в `~/work/runbooks`, настроен `kubectl port-forward`, в `stand-log.md` добавлена таблица MTTD/MTTA/MTTR по учебной тревоге.
- **Учётные записи/адреса:** без изменений (по-прежнему `adm.student`, 10.10.10.61–63, 10.10.20.10, 10.10.10.50).

### Откат

VMware Workstation → выбери ВМ → `VM → Snapshot → Snapshot Manager` → выбери `before-devops-089` → **Revert To Snapshot**. Повтори для k8s-cp01, k8s-w01, k8s-w02, GIT01, WEB01. Если откатываешь только конфиг: `git revert <commit>` в `sre/runbooks` и `helm rollback kube-prometheus-stack` в namespace `monitoring`. Не забудь удалить активные silence — они живут в данных Alertmanager и после отката снапшота исчезнут вместе с ним.

## 6. Ошибки новичков и безопасность

1. **«Поставлю silence на всякий случай, на неделю»** → реальная авария проходит мимо, а через три дня никто не помнит, зачем silence. Правильно: silence только с `--author`, `--comment` со ссылкой на CHG/тикет и `--duration` не больше окна работ.
2. **Алерт на причину: `CPU > 80%`** → дежурного будят из-за нормального ночного бэкапа, а когда портал лежит — молчание. Правильно: алертить на симптом (`probe_success == 0`, рост доли ошибок), а CPU выводить на дашборд.
3. **Один канал на всё, все алерты `critical`** → 60 уведомлений за ночь, дежурный перестаёт читать. Правильно: три уровня (`info`/`warning`/`critical`), три канала, в телефон — только то, что требует действия прямо сейчас.
4. **Runbook на 40 шагов, написанный год назад** → на 12-м шаге кнопка называется иначе, дежурный бросает инструкцию и импровизирует. Правильно: короткий runbook, проверяемый на учениях (шаг 11 лабораторной — именно это), с датой последней проверки в шапке.
5. **Эскалация «сразу звонок тимлиду»** → тимлид перестаёт брать трубку. Правильно: чат → телефон primary → secondary → владелец сервиса, с интервалами 10–15 минут.
6. **Дежурство «пока не починим» без ротации** → выгорание за месяц и ноль интереса к автоматизации. Правильно: недельная ротация, передача смены с handoff-документом, обязательный отсыпной после ночного инцидента.
7. **MTTR как KPI и гонка за цифрами** → инциденты закрывают формально, не устранив причину. Правильно: MTTR — диагностическая метрика, а не рейтинг; смотреть вместе с MTTD и числом повторных инцидентов.
8. **`for: 0m` на всём** → флаппинг сети превращается в шторм алертов. Правильно: `for` подбирать по типу сигнала (1m для полной недоступности, 10–15m для деградации), а флаппинг лечить инхибицией.

**Безопасность.** В боевой среде нельзя: держать в runbook пароли и ключи в открытом виде (только ссылки на хранилище секретов) — runbook лежит в Git и его читают все; давать дежурному постоянный доступ к прод-БД «на всякий случай» — доступ выдаётся по процедуре и логируется; ставить silence на security-алерты (подозрительный вход, отключение аудита) — их не глушат никогда, только разбирают; отключать алерты, потому что «шумят», без записи в тикете — это скрытая деградация мониторинга, которая всплывёт на первом же постмортеме.

## 7. Вопросы на собеседовании

**1. Чем MTTD отличается от MTTR и что вы улучшали бы в первую очередь?**

**Ответ.** MTTD — среднее время от начала сбоя до момента, когда система (или человек) узнал о нём; MTTR — от начала сбоя до восстановления сервиса. MTTD характеризует мониторинг, MTTR — способность диагностировать и чинить, включая runbook'и и автоматизацию. Приоритет зависит от того, где узкое место: если MTTD 30 секунд, а MTTR час — улучшать алертинг бессмысленно, надо сокращать путь «алерт → гипотеза → действие». Если MTTD 40 минут, а MTTR 5 — наоборот, надо чинить обнаружение: добавить пробы, снизить `for`, настроить синтетику. На практике MTTD дешевле улучшать, поэтому с него и начинают.

**Что хочет услышать интервьюер:** что кандидат считает метрики, а не оперирует лозунгами, и что он связывает метрику с конкретным техническим рычагом (пробa, `for`, `group_wait`, runbook, автоматизация).

**Красный флаг:** «MTTR — это когда инцидент закрыт в Jira, у нас он 20 минут» без понимания, что метрика меряет процесс, а не реальность.

**2. Что такое alert fatigue и как вы снижаете шум, не теряя сигнал?**

**Ответ.** Alert fatigue — привыкание к потоку уведомлений, при котором дежурный перестаёт реагировать на любое из них. Снижаем четырьмя механизмами: группировка (`group_by` по сервису/namespace + `group_wait`, чтобы получить одно сообщение вместо пятидесяти), инхибиция (пока горит `PortalDown`, не шлём warning-следствия), silence на плановые работы с обязательным сроком и тикетом, и маршрутизация по severity — в телефон только то, что требует немедленного действия. Плюс идеологический слой: алертим на симптомы, которые видит пользователь, а не на каждую метрику. Проверяем результат по доле действий: если алерт за месяц ни разу не привёл к действию — его удаляют или понижают в severity.

**Что хочет услышать интервьюер:** конкретные механизмы Alertmanager, а не «мы стараемся меньше шуметь»; понимание, что удаление алерта — нормальная операция, а не потеря.

**Красный флаг:** «Мы всё отключили, стало тихо» — классический способ проспать аварию.

**3. Как устроена эскалация и какие у неё типичные ошибки?**

**Ответ.** Эскалация — это лестница с таймерами: primary получает уведомление в чат, через 10–15 минут без ack — звонок на телефон primary, ещё через 10–15 — secondary или владелец сервиса, дальше — тимлид/инцидент-командир. Ключевое: таймеры зафиксированы заранее, а не определяются на ходу, и каждый уровень знает, что делать. Типичные ошибки: эскалировать сразу на руководителя (быстро отучит брать трубку), отсутствие secondary (отпуск = нет on-call), эскалация без контекста («что-то упало» — приложи алерт, ссылку на runbook и что уже проверил) и отсутствие правила «эскалируй, если через 15 минут нет гипотезы» — люди тянут до последнего из страха побеспокоить.

**Что хочет услышать интервьюер:** понимание, что эскалация защищает людей, а не наказывает; наличие формальных таймеров и контекста в уведомлении.

**Красный флаг:** «Эскалирую, когда пойму, что сам не справляюсь» — то есть никогда вовремя.

**4. Что обязательно должно быть в runbook и как держать его актуальным?**

**Ответ.** Минимум: симптом и как его подтвердить, влияние на пользователей (кому плохо и как сильно), быстрая проверка на 2 минуты, пошаговая диагностика с точками ветвления, конкретные команды восстановления с ожидаемым результатом, условие эскалации («если шаг 3 не дал результата за 10 минут — звони владельцу сервиса»), ссылки на дашборды. Актуальность поддерживается процедурой, а не совестью: runbook лежит в Git рядом с кодом, `runbook_url` зашит в аннотацию алерта, каждое учение (и каждый реальный инцидент) заканчивается правкой runbook'а в том же MR, а в шапке файла стоит дата последней проверки.

**Что хочет услышать интервьюер:** что runbook — исполняемый артефакт в репозитории с владельцем и датой проверки, а не вики-страница из 2021 года.

**Красный флаг:** «У нас есть Confluence» без ответа на вопрос, кто и когда его обновляет.

**5. Как вы поймёте, что on-call в команде организован хорошо?**

**Ответ.** По нескольким признакам одновременно: уведомлений ночью мало и каждое требует действия; дежурный открывает алерт и сразу видит в нём severity, сервис и ссылку на runbook; есть явная ротация с secondary и handoff-документом; MTTD и MTTR измеряются и имеют тренд; после инцидентов появляются изменения в коде или алертах, а не только в постмортем-документе; люди не боятся дежурить. Обратный индикатор — «тихая ночь, но мы не знаем, работает ли сервис»: значит, мониторинг настроен на причины, а не на симптомы.

**Что хочет услышать интервьюер:** что надёжность — продукт с измеримым качеством, и что кандидат думает о людях, а не только о конфигах.

**Красный флаг:** «Хороший on-call — это когда никто не звонит» (путаница между тишиной и отсутствием мониторинга).

## 8. Источники (официальная документация)

- **Google SRE** — `sre.google`. Книга «Site Reliability Engineering», главы *Being On-Call*, *Managing Incidents*, *Postmortem Culture*; рабочая тетрадь «The Site Reliability Workbook», глава *Alerting on SLOs*. Поиск: `site reliability workbook alerting on slos multiwindow`.
- **Prometheus** — `prometheus.io`. Разделы *Alerting* → *Alerting rules*, *Configuration* (`alerting`, `rule_files`), *Unit Testing for Rules*. Поиск: `prometheus unit testing for rules promtool`.
- **Alertmanager** — `prometheus.io` (раздел Alerting → Alertmanager, Configuration, Notification template reference). Поиск: `alertmanager configuration inhibit_rules group_by`.
- **amtool** — документация в репозитории Prometheus Alertmanager (`github.com/prometheus/alertmanager`), раздел `amtool`. `[ПРОВЕРИТЬ]` актуальный набор флагов `silence add` и `config routes test` в вашей версии.
- **Kubernetes** — `kubernetes.io`. Раздел *Monitoring, Logging, and Debugging*, а также *API reference* по `PrometheusRule` (это CRD из prometheus-operator, документация — `prometheus-operator.dev`). Поиск: `prometheus operator prometheusrule api`.
- **GitLab Docs** — `docs.gitlab.com`. Разделы *CI/CD pipelines*, *Merge requests*, *Code review*. Поиск: `gitlab ci merge request pipelines rules changes`.
- **PagerDuty Incident Response** — `response.pagerduty.com`. Разделы *On-Call*, *Severity Levels*, *Runbooks*. Поиск: `pagerduty incident response on call runbook`.
- **Atlassian Incident Management Handbook** — `atlassian.com` (раздел Incident Management). `[ПРОВЕРИТЬ]` актуальный URL-путь.
