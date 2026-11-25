# Проект №4: Наблюдаемость — Prometheus/Grafana/Loki/Tempo, SLO-дашборды, алерты, on-call-процесс

## 1. Введение (зачем это нужно в реальной работе)

Тикет в 3:40 ночи, канал «Дежурство DevOps»:

> **Пользователь:** Иван Иванов (бухгалтерия) — но по факту переслал жалобу от клиента
> **ВМ:** WIN11-CL01 (или просто чат, если Windows-части стенда нет)
> **Что написал:** «Портал заказов `vektor-web` еле дышит, страница грузится 30 секунд, клиенты не могут оплатить. Разберитесь.»
> **Срочность:** высокая, деньги на кону

У тебя есть ровно два пути. Первый: зайти по SSH, посмотреть `kubectl get pods`, ничего не увидеть (поды Running!), позвонить разработчику, разбудить его, вместе смотреть логи через `kubectl logs -f` и гадать. Второй: открыть дашборд SLO, за 30 секунд увидеть, что ошибки 5xx выросли до 4%, значит, бюджет месяца сгорает за сутки, а в трейсах видно, что тормозит конкретный вызов к внешнему платёжному API. Разница между этими путями — не в скорости реакции, а в том, что во втором случае система сама тебе всё рассказала.

Наблюдаемость (observability) — это не «поставить Grafana», а способность отвечать на вопросы, которые ты не предвидел заранее. Метрики говорят «что сломалось», логи — «почему», трейсы — «где именно». А SLO и алерты превращают поток данных в управляемый процесс: кто просыпается, когда и что делает по инструкции.

После этой главы ты будешь уметь: считать SLI из метрик Prometheus, формулировать SLO и бюджет ошибок, писать multi-window burn-rate алерты (чтобы не будили из-за одной ошибки), поднимать Tempo для трейсов и связывать его с Loki, хранить дашборды и правила **как код в Git**, настраивать маршрутизацию алертов в Alertmanager и описывать on-call-ротацию с runbook'ами. Всё это — на стенде «Вектор», продолжая проекты уроков 116–118.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Три столпа наблюдаемости и чем они физически различаются:

| Сигнал | Что это | Транспорт | Стоимость хранения | Инструмент на стенде |
|---|---|---|---|---|
| Метрики | Числа во времени, агрегаты | HTTP pull `GET /metrics` (текстовый формат Prometheus) или OTLP push | Низкая (сэмплы раз в 15–30 с) | Prometheus |
| Логи | Текстовые события | Push в агент (Alloy/Promtail `[ПРОВЕРИТЬ: promtail объявлен deprecated, актуальная замена — Grafana Alloy]`) → HTTP | Средняя/высокая | Loki |
| Трейсы | Путь одного запроса через сервисы, span'ы с длительностями | OTLP: gRPC **4317**, HTTP **4318** | Средняя (нужен sampling) | Tempo |

Prometheus работает по модели **pull**: он сам раз в `scrape_interval` (обычно 15–30 с) стучится на `/metrics` каждого таргета и складывает сэмплы в свою TSDB (time-series database). Таргеты он находит через service discovery — в Kubernetes это CRD `ServiceMonitor`/`PodMonitor`, которые читает Prometheus Operator. Правила алертов Prometheus вычисляет у себя (порт 9090) и отправляет «зажжённые» алерты в **Alertmanager** (порт 9093), а тот занимается группировкой, задержками, подавлением (inhibition) и рассылкой по получателям. Grafana (порт 3000) — только UI и прокси к источникам данных, сама ничего не хранит.

```text
                    ┌──────────────── кластер k8s (10.10.10.61-63) ─────────────────┐
 приложение         │                                                                │
 vektor-web ──/metrics──pull──► Prometheus (9090) ──rules──► Alertmanager (9093)      │
     │              │              │  TSDB                              │            │
     │──OTLP 4317/4318──push──► Tempo (3200 query, 4317/4318 ingest)      │ webhook    │
     │              │              │                                     ▼            │
     └──stdout──► Alloy/Promtail ──push──► Loki (3100) ──┐        UBU01:9099          │
                                                        ▼        on-call receiver     │
                                              Grafana (3000) ◄── datasources         │
                                                   ▲                                   │
                                                   └── дашборды и DS как код из Git    │
                    └────────────────────────────────────────────────────────────────┘
   Отдельно: MON01 (10.10.10.40, Zabbix) — железо, сеть, Windows-хосты, ups, диски.
```

Почему Zabbix остаётся: он уже стоит с L2-трека и хорошо делает то, чего Prometheus не умеет «из коробки» — SNMP-опрос сетевого оборудования, агенты на Windows-хостах, простые триггеры для инфраструктуры. Нормальная практика в РФ: Zabbix — инфраструктура, Prometheus/Grafana — приложение и Kubernetes. Дублировать одно и то же в двух системах не надо.

**Ключевая математика SLO.** SLI (service level indicator) — измеряемый показатель, например «доля успешных HTTP-запросов». SLO (service level objective) — цель по этому показателю, например «99,9% успешных за 30 дней». Бюджет ошибок (error budget) = `1 − SLO` = 0,1% = примерно **43 минуты** недоступности в месяц. Пока бюджет не сожжён — можно катить релизы; сожжён — замораживаем фичи и чиним надёжность. Это и есть язык, на котором DevOps разговаривает с продуктом.

Скорость траты бюджета называется **burn rate**. Burn rate = 1 значит «тратим ровно столько, чтобы исчерпать бюджет за 30 дней». Burn rate 14,4 (для 30-дневного окна) означает «2% бюджета за 1 час» — это пожар, надо будить человека. Отсюда стандартная схема multi-window multi-burn-rate из SRE Workbook (Google):

| Что | Длинное окно | Короткое окно | Burn rate | Реакция |
|---|---|---|---|---|
| Быстрое выгорание | 1 ч | 5 мин | 14,4 | Page (будим) |
| Среднее | 6 ч | 30 мин | 6 | Page / тикет |
| Медленное | 3 дня | 6 ч | 1 | Тикет на неделю |

Короткое окно нужно, чтобы алерт **переставал** гореть, когда проблема ушла, а не висел ещё час.

## 3. Аналогия из жизни

Представь приёмный покой больницы. **Метрики** — это датчики на мониторе: пульс, давление, температура. Медсестра смотрит на цифры и сразу видит, что пациент стабилен или что давление падает. Смотреть на монитор дёшево, можно хранить сутки-месяцы записей. **Логи** — это история болезни: подробно, дословно, «в 03:12 пожаловался на боль в груди, дали нитроглицерин». Точно и однозначно, но читать долго и объём большой. **Трейсы** — рентген или УЗИ одного конкретного приступа: видно, что именно происходило внутри, по шагам и с таймингами.

**SLO** — это медицинский норматив: «пациент должен быть выписан живым в 99,9% случаев». **Бюджет ошибок** — сколько случаев за месяц больница имеет право «завалить», не потеряв лицензию. **Burn rate** — насколько быстро мы этот лимит проедаем. **Alertmanager** — служба вызова дежурного врача: она решает, кого будить, по какому телефону и через сколько минут, а также умеет не звонить дважды про одно и то же. **Runbook** — инструкция на стене ординаторской: «если давление ниже 90/60 — делай А, потом Б».

Где аналогия ломается: у врача один пациент, а у сервиса — сотни подов, которые рождаются и умирают каждую минуту, и «пациент» сам состоит из десятка микросервисов, каждый со своим здоровьем.

## 4. Практика и команды

Ниже — минимум, который нужно уметь руками. Всё выполняем с `k8s-cp01` (или через `ssh adm.student@10.10.10.61`).

**1. Проверить, что стек жив (имена релизов зависят от вашей установки — сверяйтесь).**

```bash
helm -n monitoring list
kubectl -n monitoring get pods
kubectl -n monitoring get svc
```

Смотрим: поды `prometheus-*`, `alertmanager-*`, `grafana-*`, `loki-*` в `Running`. Имена сервисов понадобятся для port-forward — запишите их.

**2. Найти таргет и убедиться, что Prometheus его скрапит.**

```bash
kubectl -n monitoring get servicemonitors,podmonitors
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
```

В браузере `http://localhost:9090` → **Status → Targets**. Ищем `job="vektor-web"`, состояние `UP`, `Last Scrape` — секунды назад. Если `DOWN` — смотрим сообщение об ошибке (`connection refused`, `404`, `context deadline exceeded`): это три разных диагноза.

**3. Запрос к API Prometheus без UI (пригодится в скриптах и CI).**

```bash
curl -sG http://localhost:9090/api/v1/query \
  --data-urlencode 'query=up{job="vektor-web"}' | jq '.data.result[].value'
```

`-G` с `--data-urlencode` аккуратно кодирует PromQL. Ответ: `["<timestamp>", "1"]` — 1 значит «таргет доступен».

**4. SLI «доля ошибок 5xx» — с этого начинается любой SLO.**

```promql
sum(rate(http_requests_total{job="vektor-web", code=~"5.."}[5m]))
/
sum(rate(http_requests_total{job="vektor-web"}[5m]))
```

`rate(...[5m])` — средняя скорость роста счётчика за 5 минут. Делим ошибки на всё. Результат `0.04` = 4% ошибок.

**5. SLI по задержке (гистограмма).** Считаем долю запросов быстрее 300 мс:

```promql
sum(rate(http_request_duration_seconds_bucket{job="vektor-web", le="0.3"}[5m]))
/
sum(rate(http_request_duration_seconds_count{job="vektor-web"}[5m]))
```

`le="0.3"` — «less or equal», накопленный бакет гистограммы. `_count` — общее число запросов. Если метрики задержки нет — её надо добавить в приложение (гистограмма, не summary: гистограмму можно агрегировать по подам).

**6. Burn rate вручную — чтобы понять, что увидит алерт.**

```promql
# SLO 99.9% => бюджет ошибок 0.001
slo:sli_error:ratio_rate1h{service="vektor-web"} / 0.001
```

Значение `>14.4` — пожар. `0.3` — всё спокойно, бюджет тратится медленнее плана.

**7. Проверить правила перед применением — обязательно.**

```bash
promtool check rules obs/rules/vektor-web-slo.yaml
promtool test rules obs/rules/tests/vektor-web-slo-test.yaml
```

`check` ловит опечатки в PromQL и структуре YAML, `test` прогоняет правила на синтетических данных (unit-тесты для алертов — то, что отличает взрослый репозиторий от «написал в UI и забыл»).

**8. Найти пожирателей кардинальности.**

```promql
topk(10, count by (__name__) ({__name__=~".+"}))
```

Показывает топ-10 метрик по числу серий. Если в топе метрика с лейблом `user_id` или `request_id` — Prometheus скоро упадёт по памяти. Норма для стенда — десятки тысяч серий, для продакшена считайте по факту.

**9. LogQL: найти ошибки в Loki.**

```logql
{namespace="prod", app="vektor-web"} |= "level=error"
```

`{}` — селектор по лейблам (только низкокардинальные: namespace, app, pod). `|=` — фильтр подстроки. Для подсчёта:

```logql
sum by (app) (rate({namespace="prod"} |= "level=error" [5m]))
```

**10. TraceQL: найти медленные и упавшие трейсы в Tempo.**

```text
{ status = error && duration > 2s }
```

Ищем span'ы с ошибкой длиннее 2 секунд. `[ПРОВЕРИТЬ]` точный синтаксис атрибутов (`span.http.status_code`) зависит от версии Tempo — сверяйтесь с docs. Открыв трейс, вы увидите waterfall: какой из вызовов внутри `vektor-web` съел 28 из 30 секунд.

**11. Alertmanager: посмотреть активные алерты и заглушить шум на время работ.**

```bash
kubectl -n monitoring port-forward svc/alertmanager-operated 9093:9093   # [ПРОВЕРИТЬ имя svc]
amtool --alertmanager.url=http://localhost:9093 alert query
amtool --alertmanager.url=http://localhost:9093 silence add \
  alertname="VektorWebAvailabilityFastBurn" --duration=2h --comment="DEV-119 учебная работа"
```

Silence — не «отключить мониторинг», а явная запись «я знаю, что горит, и это ожидаемо до 05:40». В боевой среде silence без комментария и без срока — грубое нарушение.

**12. Grafana как код: дашборд через ConfigMap.**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboard-vektor-web-slo
  namespace: monitoring
  labels:
    grafana_dashboard: "1"      # sidecar сам подхватит и положит в /tmp/dashboards
data:
  vektor-web-slo.json: |
    { "title": "Vektor Web — SLO", "uid": "vektor-web-slo", "panels": [ ... ] }
```

JSON удобнее собирать в UI и потом экспортировать (**Dashboard → Share → Export**), но в Git лежит именно JSON. Путь в UI для проверки: **Dashboards → Browse → Vektor Web — SLO**.

**13. Доставка в Git и синк.**

```bash
git add obs/ && git commit -m "obs: SLO rules, tempo datasource, burn-rate alerts for vektor-web"
git push origin main
argocd app sync platform-observability    # [ПРОВЕРИТЬ имя Application]
```

Всё, что вы руками сделали в UI Grafana и не вынесли в Git, после переустановки стека исчезнет. Правило трека: **UI для экспериментов, Git для истины**.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 118: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), namespace `monitoring` с `kube-prometheus-stack`, Loki и приложение `vektor-web` в namespace `prod`.
- Если в 118 мониторинг ставился иначе или не ставился — минимальная настройка на месте: `helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update && helm -n monitoring upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack --create-namespace`. Дальше глава работает без изменений.
- `GIT01` (10.10.10.50) с репозиторием `devops/platform-gitops` из урока 116/117, доступ по SSH-ключу настроен.
- `UBU01` (10.10.10.30) — свободная ВМ для приёмника алертов. `MON01` (10.10.10.40) — оставляем как есть, Zabbix не трогаем.
- Windows-часть стенда для этой главы **не нужна**, опираться на неё не будем.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-devops-119`.

ВМ: `k8s-cp01`, `k8s-w01`, `k8s-w02`, `GIT01`, `UBU01`. Перед снапшотом желательно выключить или приостановить кластер (`sudo poweroff` на всех трёх узлах), чтобы снапшот был консистентным — иначе etcd может ругаться при откате.

### Шаги

1. **`k8s-cp01`.** Проверяем базовый стек: `helm -n monitoring list` и `kubectl -n monitoring get pods`. Ожидаем: релиз `kube-prometheus-stack`, поды `Running`. Записываем точные имена сервисов Prometheus/Grafana/Alertmanager в `stand-log.md`.
2. **`k8s-cp01`.** Клонируем GitOps-репозиторий: `git clone git@10.10.10.50:devops/platform-gitops.git ~/platform-gitops && cd ~/platform-gitops && mkdir -p obs/{rules,dashboards,tempo,oncall,runbooks}`. Ожидаем: пустая структура каталогов, `git status` чистый.
3. **`k8s-cp01`.** Добавляем `ServiceMonitor` для `vektor-web` (namespace `prod`, порт `http`, путь `/metrics`, `interval: 15s`, label `release: kube-prometheus-stack`). Коммитим, ждём синка ArgoCD. Ожидаем: в Prometheus **Status → Targets** появился `job="vektor-web"` в состоянии `UP`.
4. **`k8s-cp01`.** Описываем SLO файлом `obs/slo/vektor-web.yaml` — сервис `vektor-web`, цель 99,9% по доступности и 95% запросов быстрее 300 мс за 30 дней. Генерируем правила: `sloth generate -i obs/slo/vektor-web.yaml -o obs/rules/vektor-web-slo.yaml` `[ПРОВЕРИТЬ: установка sloth из GitHub-релиза slok/sloth]`. Ожидаем: YAML с recording rules `slo:sli_error:ratio_rate5m/1h/6h/3d` и алертами burn rate.
5. **`k8s-cp01`.** Валидируем и применяем: `promtool check rules obs/rules/vektor-web-slo.yaml`, затем `kubectl apply -f obs/rules/vektor-web-slo.yaml` (или через ArgoCD). Ожидаем: `kubectl -n monitoring get prometheusrule` показывает новый объект, через 1–2 минуты запрос `slo:sli_error:ratio_rate5m{service="vektor-web"}` в Prometheus отдаёт число.
6. **`k8s-cp01`.** Ставим Tempo single-binary: `helm repo add grafana https://grafana.github.io/helm-charts && helm repo update && helm -n monitoring install tempo grafana/tempo -f obs/tempo/values.yaml` `[ПРОВЕРИТЬ: актуальные ключи values и имя чарта]`. В values включаем OTLP-приёмники `4317`/`4318` и локальное хранилище. Ожидаем: под `tempo-0` в `Running`, `kubectl -n monitoring get svc | grep tempo` показывает сервис.
7. **`k8s-cp01`.** Добавляем datasource Tempo в Grafana как код — ConfigMap с лейблом `grafana_datasource: "1"`, `url: http://tempo.monitoring.svc:3200`, связка `tracesToLogsV2` на Loki для перехода «трейс → логи по trace_id» `[ПРОВЕРИТЬ: имена полей в вашей версии Grafana]`. Ожидаем: в Grafana **Connections → Data sources** появился Tempo без ручных кликов.
8. **`k8s-cp01`.** Генерируем трафик и трейсы: `kubectl -n prod run load --rm -it --image=curlimages/curl -- sh -c 'while true; do curl -s -o /dev/null http://vektor-web.prod.svc/orders; sleep 1; done'`. Ожидаем: в Grafana **Explore → Tempo → Search** появляются трейсы, TraceQL `{ status = error }` что-то находит (или пусто, если приложение не отдаёт ошибки — тогда проверьте `{ duration > 500ms }`).
9. **`k8s-cp01`.** Кладём дашборд SLO JSON в `obs/dashboards/vektor-web-slo.json` и ConfigMap с лейблом `grafana_dashboard: "1"`. Ожидаем: дашборд автоматически появился в Grafana (панели: текущий SLI, остаток бюджета, burn rate за 1ч/6ч/3д, топ-5 ошибок из Loki).
10. **`UBU01`.** Поднимаем приёмник алертов: `sudo apt install -y python3-venv`, `python3 -m venv ~/oncall && ~/oncall/bin/pip install flask`, сохраняем `webhook_receiver.py` (см. ниже), запускаем `~/oncall/bin/python webhook_receiver.py`. Ожидаем: `curl -X POST -d '{}' http://10.10.10.30:9099/alerts` отвечает `{"status":"received"}`.
11. **`k8s-cp01`.** Настраиваем Alertmanager: route с `group_by: [alertname, namespace, slo]`, `repeat_interval: 4h`, отдельная ветка для `severity="ticket"` (тикет, не будить), inhibition: быстрый burn подавляет медленный по тому же сервису. Применяем через values `kube-prometheus-stack` или `AlertmanagerConfig` `[ПРОВЕРИТЬ: какой способ поддерживает ваша версия]`. Ожидаем: `amtool alert query` показывает алерты, в `/var/log/vektor/alerts.jsonl` на UBU01 появляются записи.
12. **`k8s-cp01`.** Оформляем on-call: `obs/oncall/rota.yaml` (кто дежурит, часы, эскалация через 15 минут) и `obs/runbooks/vektor-web.md` (симптом → что проверить → что сделать). В аннотацию алерта добавляем `runbook_url`. Коммитим, синкаем, пишем изменения в `stand-log.md`. Ожидаем: в присланном JSON алерта есть `runbook_url`, а в Git — два новых файла.

Код приёмника (`webhook_receiver.py`):

```python
from flask import Flask, request, jsonify
import datetime, json, pathlib

app = Flask(__name__)
LOG = pathlib.Path("/var/log/vektor/alerts.jsonl")
LOG.parent.mkdir(parents=True, exist_ok=True)

@app.post("/alerts")
def alerts():
    payload = request.get_json(force=True, silent=True) or {}
    record = {"ts": datetime.datetime.utcnow().isoformat(), "payload": payload}
    with LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return jsonify({"status": "received", "count": len(payload.get("alerts", []))})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9099)
```

Это учебная заглушка вместо PagerDuty/Grafana OnCall `[ПРОВЕРИТЬ: текущий статус OSS-версии Grafana OnCall]`: в РФ внешние SaaS-пейджеры часто недоступны, поэтому в реальности ставят self-hosted IRM или связку Alertmanager → мессенджер (Mattermost) с ботом.

### Проверка

- [ ] `kubectl -n monitoring get pods` — Prometheus, Alertmanager, Grafana, Loki, Tempo в `Running`
- [ ] В Prometheus **Status → Targets** таргет `job="vektor-web"` в состоянии `UP`
- [ ] Запрос `slo:sli_error:ratio_rate5m{service="vektor-web"}` возвращает число, а не «no data»
- [ ] `promtool check rules obs/rules/vektor-web-slo.yaml` — `SUCCESS`
- [ ] В Grafana есть datasource Tempo и дашборд `Vektor Web — SLO`, созданные без ручных кликов
- [ ] TraceQL-поиск в Tempo отдаёт трейсы, из трейса есть переход в логи Loki по `trace_id`
- [ ] `amtool alert query` показывает алерты с корректными `severity`
- [ ] На UBU01 в `/var/log/vektor/alerts.jsonl` есть записи, в JSON присутствует `runbook_url`
- [ ] `git log --oneline -5` в `platform-gitops` содержит коммиты с правилами, дашбордами, ротацией
- [ ] `stand-log.md` дополнен

### Сломай и почини

> **Пользователь:** Иван Иванов / VM: WIN11-CL01 (или чат дежурства) / **Что написал:** «Портал заказов вообще не открывается, 502. Клиенты пишут в поддержку.» / **Срочность:** критическая, клиентские платежи

**Как воспроизвести поломку** (на `k8s-cp01`):

```bash
kubectl -n prod scale deploy/vektor-web --replicas=0
```

Через 1–2 минуты (пока не истечёт `for:` у алерта) должен загореться быстрый burn rate: 100% ошибок — бюджет сгорает мгновенно.

**Диагностика по порядку:**

1. Открываем Grafana → дашборд SLO. Видим: SLI = 0%, burn rate сильно выше 14,4, бюджет сгорел. Симптом подтверждён.
2. Смотрим, что именно упало: `up{job="vektor-web"}`. Если `0` или пусто — проблема не в приложении, а в доступности таргета. Проверяем `absent(up{job="vektor-web"})`: пусто = метрики вообще не собираются (сломали ServiceMonitor), есть = таргет есть, но недоступен.
3. `kubectl -n prod get deploy,pods -l app=vektor-web` — видим `0/0` реплик. Причина найдена: кто-то отмасштабировал в ноль.
4. Проверяем, не результат ли это чьей-то правки: `kubectl -n prod rollout history deploy/vektor-web` и `git log -p -- obs/ prod/`. Если реплики меняли руками через `kubectl`, в Git этого не будет — повод перейти на ArgoCD-only изменения (self-heal).
5. Смотрим логи Loki за последние 15 минут, чтобы убедиться, что до scale-to-zero приложение не сыпало ошибками: `{namespace="prod"} |= "error"`.
6. Поднимаем обратно: `kubectl -n prod scale deploy/vektor-web --replicas=2`.
7. Ждём 1–2 минуты, проверяем `up{job="vektor-web"} == 1` по обоим подам и что SLI вернулся к 100%.
8. Убеждаемся, что алерт ушёл в `resolved` (в Alertmanager `amtool alert query` пусто, в JSON на UBU01 пришёл `status: resolved`).

**Что написать пользователю:** «Сервис `vektor-web` был недоступен с 03:41 до 03:52 из-за нулевого количества реплик (ошибочное масштабирование). Сейчас работает в штатном режиме, 2 реплики, задержка в норме. Причина разбирается, чтобы не повторилось; бюджет ошибок месяца потрачен на 3%, отчёт приложу.»

**Как закрыть заявку:** перевести в Resolved, приложить ссылку на инцидент, создать postmortem в `obs/runbooks/incidents/2026-xx-xx-vektor-web.md` по шаблону blameless (таймлайн, влияние, root cause, action items с владельцами и сроками), добавить action item «запретить прямые `kubectl scale` в prod, только через Git + ArgoCD self-heal».

### Что изменилось на стенде

- `k8s-cp01/w01/w02`: в namespace `monitoring` добавлен Tempo (OTLP 4317/4318, query 3200), новые `PrometheusRule` с SLO-правилами для `vektor-web`, ConfigMap'ы дашборда и datasource Tempo, обновлён конфиг Alertmanager (маршруты page/ticket, inhibition).
- `prod`: `vektor-web` получил `ServiceMonitor`; на время лабы — 2 реплики.
- `UBU01` (10.10.10.30): Flask-приёмник алертов на порту **9099**, лог `/var/log/vektor/alerts.jsonl`, venv `~/oncall`.
- `GIT01` (10.10.10.50): в `devops/platform-gitops` появились каталоги `obs/{slo,rules,dashboards,tempo,oncall,runbooks}`, файл ротации `obs/oncall/rota.yaml`, runbook `obs/runbooks/vektor-web.md`.
- SLO-контракт: `vektor-web` — 99,9% успешных запросов и 95% быстрее 300 мс за 30 дней; бюджет ошибок ≈43 минуты в месяц.
- `MON01` (10.10.10.40): без изменений, остаётся Zabbix для инфраструктуры.

### Откат

1. Выключить ВМ `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01`, `GIT01`.
2. VMware Workstation → **VM → Snapshot → Snapshot Manager** → выбрать `before-devops-119` → **Go to**. Повторить для всех пяти ВМ.
3. Включить ВМ, проверить `kubectl get nodes` (все `Ready`) и `helm -n monitoring list`.
4. Мягкий вариант без отката снапшота: `helm -n monitoring uninstall tempo`, `kubectl delete prometheusrule -n monitoring -l service=vektor-web`, `git revert <commit>` и `argocd app sync`. Данные Prometheus за период лабы останутся — это нормально.

## 6. Ошибки новичков и безопасность

1. **Высококардинальные лейблы** (`user_id`, `url`, `request_id`, `trace_id` в метках Prometheus/Loki) → **TSDB растёт нелинейно, Prometheus падает по OOM, Loki тормозит на запросах** → в метках только измерения с малым числом значений (namespace, service, code, method); идентификаторы — в логи и трейсы, а не в лейблы.
2. **Алерт на причину, а не на симптом:** «CPU > 80%», «свободно памяти < 1 ГБ» → **шум, привыкание, дежурный перестаёт реагировать (alert fatigue)** → страницы (page) только на пользовательский эффект (burn rate SLO, ошибки, недоступность), ресурсные пороги — в тикеты и на дашборды.
3. **SLO 99,99% «на всякий случай»** → **бюджет 4 минуты в месяц, любая выкатка его сжигает, конфликт с продуктом** → SLO выводят из реального поведения и ожиданий бизнеса, на старте ставят 99,5–99,9% и повышают осознанно.
4. **Алерт без `for:` и без короткого окна** → **одиночный всплеск будит ночью, а зависший алерт горит часами после починки** → multi-window burn rate: длинное окно для «пора будить», короткое для «пора успокоиться», плюс `for: 2m` как страховка.
5. **Всё настроено мышкой в UI Grafana** → **после переустановки стека не осталось ни дашбордов, ни алертов** → datasources, dashboards, rules, Alertmanager config только как код в Git и через ArgoCD.
6. **Один общий получатель алертов «на всех»** → **инфраструктурные шумы приходят вместе с платежами, критичное тонет** → маршруты по `severity` и `team`, `group_by`, inhibition, отдельные тикет-каналы.
7. **Трейсинг с sampling 100% в проде** → **объём и стоимость хранения взрываются** → head sampling 1–10% на входе или tail sampling (сохранять все ошибки и медленные, остальное — выборочно).
8. **Нет runbook'а и владельца у алерта** → **дежурный будится и 40 минут выясняет, что делать, эскалация отсутствует** → у каждого page-алерта есть `runbook_url`, владелец (team) и прописанная эскалация в `rota.yaml`.

**Безопасность.** В боевой среде: Grafana не должна смотреть в интернет с дефолтным `admin/admin` — сразу SSO (OIDC/LDAP) и RBAC; Prometheus и Alertmanager не выставляем наружу, только через Ingress с аутентификацией и NetworkPolicy — в них нет встроенной авторизации, и открытый `/api/v1/query` = утечка метрик и имён инфраструктуры. Webhook-URL и токены (Slack/Mattermost/телеграм-бот) не кладём в Git открытым текстом — SOPS/Sealed Secrets/Vault (урок 120). В логи не должны попадать ПДн, номера карт и токены (152-ФЗ, PCI DSS): маскируйте на этапе приложения, а не в Grafana. `/metrics` тоже не всегда безобиден — там бывают имена хостов, версии, пути; закрывайте basic auth или сетевой политикой. На стенде мы сознательно не ставим TLS и авторизацию — это только для изолированной VMnet8 и с явной пометкой в `stand-log.md`.

## 7. Вопросы на собеседовании

**1. Чем метрики отличаются от логов и трейсов, и что вы выберете для каждой задачи?**
Ответ: три разных сигнала с разной ценой. Метрики — числовые агрегаты, дешёвые, хорошо сжимаются, идеальны для SLO, алертов и дашбордов, но не отвечают на «почему». Логи — дискретные события с контекстом, нужны для разбора причин, дорогие по объёму, требуют структурирования (JSON + level) и контроля кардинальности лейблов. Трейсы — распределённый путь запроса, нужны для микросервисов, чтобы найти узкое место и увидеть causation; требуют инструментирования (OpenTelemetry) и sampling. Практика: алерт по метрике → в дашборде/трейсе нашли медленный/падающий вызов → в логах по `trace_id` узнали точную причину.
*Что хочет услышать интервьюер:* понимание стоимости и кардинальности, а не заучивание трёх слов.
*Красный флаг:* «логи и метрики — одно и то же, просто логи подробнее».

**2. Что такое SLO, SLI и error budget, и как выбрать целевое значение?**
Ответ: SLI — измеряемый показатель (доля успешных запросов, доля быстрее 300 мс), SLO — целевое значение за период (99,9% за 30 дней), error budget = 1 − SLO (0,1% ≈ 43 минуты в месяц). Выбираем не «максимально возможное», а исходя из ожиданий пользователей и экономики: 99,9% на старте — хороший баланс; 99,99% резко удорожает архитектуру (HA, резервирование, отказ от рискованных релизов). Бюджет — инструмент управления: не сожгли — катим фичи быстрее, сожгли — замораживаем и чиним надёжность. SLO пересматривают раз в квартал по факту.
*Что хочет услышать интервьюер:* SLO привязан к пользователю и к бизнесу, есть политика расходования бюджета.
*Красный флаг:* «ставим 100%, чтобы точно не падало».

**3. Как устроены multi-window burn-rate алерты и почему не алертят на одну ошибку?**
Ответ: burn rate — скорость траты бюджета относительно плана; 1 = бюджет кончится ровно за период SLO. Стандартная схема: 14,4 за 1 ч (2% бюджета) — page, 6 за 6 ч (5%) — page/тикет, 1 за 3 дня (10%) — тикет. Каждый уровень считается по длинному и короткому окну одновременно (`AND`): длинное ловит реальную проблему, короткое быстро снимает алерт после починки. Так мы будим человека только когда теряем бюджет ощутимо, и не будим из-за единичного 5xx.
*Что хочет услышать интервьюер:* понимание цифр и причины короткого окна, связка «алерт = бюджет», а не «алерт = порог».
*Красный флаг:* «алерт на 1% ошибок за 5 минут» без обоснования или путаница между burn rate и долей ошибок.

**4. Алерт «сервис недоступен» сработал. Как действуете по шагам?**
Ответ: сначала оцениваю масштаб и влияние по дашборду SLO (сколько пользователей затронуто, с какой скоростью горит бюджет), затем проверяю слой за слоем: метрики таргета (`up`), затем поды (`kubectl get pods`, events, restart count), затем логи Loki на ошибки и OOM, затем трейсы на верхнеуровневую деградацию, затем внешние зависимости (DNS, ingress, БД, платёжный API). Параллельно смотрю, не связано ли с недавним деплоем/конфиг-изменением (`rollout history`, коммиты) — если да, откат самый быстрый путь. Проверяю, не сломаны ли сами метрики: `absent()` помогает отличить «всё умерло» от «мы ослепли». Фиксирую таймлайн в инциденте.
*Что хочет услышать интервьюер:* порядок, от влияния к причине, откат как валидное решение, различение «сервис упал» и «мониторинг ослеп».
*Красный флаг:* «сразу лезу в логи всех подов» или «начинаю с перезапуска всего».

**5. Опишите on-call-процесс: ротация, эскалация, runbook, постмортем.**
Ответ: ротация на неделю, опубликованная заранее и лежащая в Git; у каждого page-алерта есть severity, владелец и runbook с шагами диагностики. Эскалация: если первичный дежурный не взял алерт за 10–15 минут — эскалация на вторичного, дальше тимлид. Silence и acknowledgement обязательны с комментарием и сроком. После инцидента — blameless postmortem: таймлайн, влияние на пользователей и бюджет ошибок, root cause, action items с владельцами и сроками; постмортем без поиска виноватых, иначе люди начнут скрывать проблемы. Обязательна компенсация дежурств и лимит ночных вызовов, иначе выгорание (урок 113).
*Что хочет услышать интервьюер:* процесс, а не героизм; наличие runbook, эскалации и разбора инцидентов.
*Красный флаг:* «мы просто пишем в чат и кто увидел, тот и чинит»; постмортем «найти виноватого и наказать».

## 8. Источники (официальная документация)

- **prometheus.io** — раздел *Prometheus Documentation*: «Getting started», «Querying basics» (PromQL), «Alerting rules», «Configuration» (scrape_configs, alerting, remote_write). Поиск: `prometheus alerting rules examples`, `promql rate histogram_quantile`.
- **grafana.com/docs** — разделы *Grafana Loki* (LogQL: log queries, metric queries), *Grafana Tempo* (TraceQL, OTLP ingestion), *Grafana* (Provisioning: data sources и dashboards as code, Alerting). Поиск: `grafana provision dashboards configmap`, `logql metric queries`.
- **prometheus-operator.dev** `[ПРОВЕРИТЬ домен]` — документация Prometheus Operator: CRD `ServiceMonitor`, `PodMonitor`, `PrometheusRule`, `AlertmanagerConfig`. Поиск: `prometheus operator ServiceMonitor example`.
- **github.com/prometheus/alertmanager** — *Alertmanager Documentation*: configuration, routing tree, inhibition, silences, `amtool`. Поиск: `alertmanager route matchers inhibition example`.
- **sre.google** — *Site Reliability Engineering* и *The Site Reliability Workbook*: главы про SLI/SLO/error budget и «Alerting on SLOs» (multi-window multi-burn-rate). Поиск: `sre workbook alerting on slos burn rate`.
- **opentelemetry.io** — разделы *Concepts* (traces, metrics, logs, sampling) и *Collector* (receivers/exporters, OTLP). Поиск: `opentelemetry otlp receiver collector config`.
- **github.com/slok/sloth** `[ПРОВЕРИТЬ актуальность домена sloth.dev]` — генерация Prometheus-правил SLO из YAML-спеки, примеры `PrometheusServiceLevel`. Поиск: `sloth generate slo prometheus rules`.
- **kubernetes.io** — раздел *Concepts → Observability* (логи, метрики, трейсы в кластере) как общая рамка. Поиск: `kubernetes observability metrics logs traces`.
