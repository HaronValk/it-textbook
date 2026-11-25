# Трейсинг: OpenTelemetry, Jaeger, Tempo — инструментирование и анализ задержек

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку от Сидорова (склад, рабочее место в офисе):

> «Оформлял заявку на перевозку в личном кабинете, нажал “Отправить” — крутилось секунды четыре. Два раза перепроверил, интернет нормальный. Раньше мгновенно было.»

Ты идёшь к разработчикам. Разработчики открывают логи: `POST /order 200 OK, 12 ms`, `POST /charge 200 OK, 8 ms`. Всё зелёное. А пользователь ждёт четыре секунды. Знакомая ситуация? Это классический разрыв между «сервис ответил 200» и «пользователь получил ответ». Метрики говорят «p99 вырос», логи говорят «ошибок нет», и оба молчат о том, **где именно** ушли три секунды: в БД, в очередь, в платёжный шлюз или в сеть между ними.

Трейсинг отвечает ровно на этот вопрос. Трейс — это дерево операций одного запроса через все сервисы, с длительностью каждой. Ты видишь не «запрос длился 4 с», а «запрос 4 с = 50 мс orders + 3500 мс payments + 400 мс запись в БД, причём payments ждал внешний API».

После этой главы ты будешь уметь: поднять OTel Collector + Jaeger + Tempo в Docker Compose на стенде, инструментировать приложение без правок кода (auto-instrumentation), настроить проброс контекста между сервисами, найти медленный спан в UI и через API, настроить сэмплирование, чтобы не утопить хранилище, и связать трейсы с метриками через span metrics. И всё это — как код в Git, а не «накликал в интерфейсе».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Три сущности.** `Trace` — весь путь запроса, идентифицируется `trace_id` (16 байт, 32 hex-символа). `Span` — одна операция внутри трейса: имя, время начала, длительность, атрибуты, `span_id` (8 байт), ссылка на родителя. `SpanContext` — минимальный набор (`trace_id`, `span_id`, флаги), который передаётся между сервисами.

**Как трейс собирается в дерево.** Каждый сервис, получив запрос, создаёт спан и передаёт контекст дальше в заголовке. Без передачи контекста каждый сервис нарисует свой отдельный трейс, и «дерева» не будет — это самая частая поломка в проде.

**Стандарт передачи контекста — W3C Trace Context.** Заголовок `traceparent`:

```text
traceparent: 00-5b8efff798038103d269b633813fc60c-eee19b7ec3c1b174-01
             │  │                                │                │
             │  │                                │                └ flags (01 = sampled)
             │  │                                └ span-id родителя (16 hex)
             │  └ trace-id (32 hex)
             └ версия формата
```

Рядом едет `tracestate` (вендор-специфичные данные). HTTP-заголовки — это L7, прикладной уровень OSI; сам трейсинг живёт целиком в приложении и на сеть не влияет, кроме накладных расходов на передачу.

**OpenTelemetry (OTel)** — это не бэкенд, а стандарт + SDK + коллектор. Он разделяет «как собирать» и «куда отправлять». Инструментировав приложение один раз по стандарту OTel, ты меняешь бэкенд (Jaeger, Tempo, коммерческий вендор) без переписывания кода. OTel покрывает три сигнала: traces, metrics, logs.

**OTLP** — протокол OTel для передачи телеметрии. Порты по умолчанию: `4317` gRPC, `4318` HTTP (protobuf или JSON). Хочешь проверить руками — можно `curl`-ом на 4318 отправить JSON.

**OTel Collector** — отдельный процесс-посредник. Три части пайплайна: `receivers` (принять), `processors` (batch, memory_limiter, фильтрация, tail sampling), `exporters` (отправить). В коллекторе можно централизованно решать, что сэмплировать и какие атрибуты вырезать, — приложение об этом не думает. Для DevOps это ключевая точка: коллектор — конфиг в Git, а не «магия в коде сервиса».

**Jaeger** — бэкенд для трейсов: collector + storage + UI (порт `16686`). Умеет принимать OTLP напрямую. Историческая деталь: собственные клиентские библиотеки Jaeger (jaeger-client) официально переведены в режим поддержки в пользу OTel SDK — новые сервисы инструментируются только через OTel [ПРОВЕРИТЬ точную формулировку статуса]. Jaeger v2 построен уже поверх OTel Collector [ПРОВЕРИТЬ].

**Grafana Tempo** — бэкенд трейсов, оптимизированный под дешёвое хранение в объектном хранилище (S3/MinIO/GCS/локальная ФС). Идея: Tempo хранит трейсы пачками блоков и ищет по `trace_id` «из коробки», а полноценный поиск по атрибутам — через TraceQL и индекс. Tempo отлично дружит с Grafana (один дашборд: метрики из Prometheus + логи из Loki + трейсы из Tempo, переход по `trace_id`). Дополнительно Tempo умеет `metrics-generator`: превращать трейсы в RED-метрики (`traces_spanmetrics_*`) и service graph — это мостик между трейсами и Prometheus.

**Сэмплирование.** Head-based — решение принимается в начале трейса (в SDK), дешёво, но можно просэмплировать неинтересный «быстрый» трейс и пропустить медленный. Tail-based — решение принимается в коллекторе после того, как трейс собран целиком: «оставить все трейсы с ошибкой и все, что дольше 500 мс». Дороже по памяти (нужно буферизовать), зато ловит именно аномалии.

```text
                      ┌──────────────────────────────────────────────┐
  UBU01 10.10.10.30   │            MON01 10.10.10.40                 │
                      │                                              │
 ┌─────────┐  OTLP    │  ┌───────────────┐    ┌──────────────┐      │
 │ orders  │──gRPC───►│  │ OTel Collector│───►│ Jaeger       │      │
 │ :5000   │  :4317   │  │ 4317/4318     │    │ :16686 UI    │      │
 └────┬────┘          │  │ batch         │    └──────────────┘      │
      │ HTTP          │  │ tailsampling  │    ┌──────────────┐      │
      │ traceparent   │  │ attributes    │───►│ Tempo        │      │
 ┌────▼────┐          │  └───────────────┘    │ :3200 API    │      │
 │payments │          │        │              └──────┬───────┘      │
 │ :5001   │──────────┼────────┘                     │              │
 └─────────┘  OTLP    │                       ┌──────▼───────┐      │
                      │                       │ Grafana      │      │
                      │                       │ Prometheus   │      │
                      │                       └──────────────┘      │
                      └──────────────────────────────────────────────┘
```

## 3. Аналогия из жизни

Курьерская доставка посылки. Трейс — это номер накладной. Каждый этап: склад «Вектора» принял → сортировочный центр → магистральная машина → городской курьер → вручение. На каждом этапе ставят штамп со временем. Смотришь на накладную и сразу видишь: посылка 3 дня пролежала в сортировочном центре, хотя «склад» и «вручение» отработали за час. Логи — это записи в журнале каждого склада по отдельности («принято 14 вагонов»), метрики — это график «сколько посылок в час проходит через центр». И только накладная показывает, что 3 дня — это ожидание на конкретном перевалочном пункте у конкретного подрядчика.

Аналогия перестаёт работать в одном месте: посылку можно физически найти и потрогать, а трейс — это набор записей, которые разные сервисы сделали независимо, и если один сервис забыл поставить «штамп» (не пробросил контекст), то в накладной просто не будет целого этапа, и никто об этом не узнает.

## 4. Практика и команды

Ниже — всё, что нужно руками, на стенде. Конфиги держим в Git-репозитории `vektor/observability` на GIT01.

**1. Поднять бэкенды на MON01.**

```bash
cd /opt/observability
docker compose up -d
docker compose ps
```

`up -d` — создать и запустить в фоне, `ps` — статус. Ожидаемо: три контейнера `running`, порты `4317`, `4318`, `16686`, `3200` проброшены на хост.

**2. Проверить, что коллектор живой и принимает спаны.**

```bash
docker compose logs --tail=50 otel-collector | grep -i "Everything is ready"
curl -s localhost:8889/metrics | grep otelcol_receiver_accepted_spans
```

Первое — лог старта коллектора (фраза «Everything is ready» — стандартная для OTel Collector [ПРОВЕРИТЬ формулировку в твоей версии]). Второе — внутренние метрики самого коллектора на порту `8889`: если `accepted_spans` растёт, значит спаны реально приходят.

**3. Сгенерировать спаны без приложения — сырым OTLP/HTTP.**

```bash
curl -s -X POST http://10.10.10.40:4318/v1/traces \
  -H 'Content-Type: application/json' \
  -d '{"resourceSpans":[{"resource":{"attributes":[
        {"key":"service.name","value":{"stringValue":"manual-test"}}]},
      "scopeSpans":[{"scope":{"name":"manual"},"spans":[{
        "traceId":"5b8efff798038103d269b633813fc60c",
        "spanId":"eee19b7ec3c1b174",
        "name":"manual-span","kind":1,
        "startTimeUnixNano":"1735689600000000000",
        "endTimeUnixNano":"1735689600500000000"}]}]}]}' -w '%{http_code}\n'
```

Разбор: `traceId` — ровно 32 hex, `spanId` — 16 hex (иначе 400), время в наносекундах Unix. Ответ `200` — принято, `{}` в теле. Это же и проверка, что порт не закрыт файрволом.

**4. Нагрузить демо-сервис и посмотреть сервисы в Jaeger API.**

```bash
for i in $(seq 1 20); do curl -s "http://10.10.10.30:5000/order?id=$i" >/dev/null; done
curl -s http://10.10.10.40:16686/api/services | jq
```

`/api/services` возвращает список имён сервисов, по которым есть трейсы. Ожидаемо: `["orders","payments","manual-test"]`.

**5. Достать самый медленный трейс через Jaeger API.**

```bash
curl -s "http://10.10.10.40:16686/api/traces?service=orders&limit=20&lookback=1h" \
 | jq -r '.data[] | .spans[] | [.operationName, (.duration/1000|floor)] | @tsv' | sort -k2 -nr | head
```

Важно: `duration` в Jaeger API — в **микросекундах**, поэтому делим на 1000 и получаем миллисекунды. Смотрим, какая операция первая в списке — это и есть узкое место.

**6. Поиск по TraceQL в Tempo (поиск по условию, а не по id).**

```bash
curl -sG http://10.10.10.40:3200/api/search \
  --data-urlencode 'q={ resource.service.name = "orders" && duration > 300ms }' \
  --data-urlencode 'limit=5' | jq '.traces[] | {traceID, rootServiceName, durationMs}'
```

`-G` + `--data-urlencode` — обязательная связка, иначе `{`, `&&` и `>` поломают URL. В Grafana Explore то же самое пишется в поле query: `{ resource.service.name = "payments" && status = error }`.

**7. Пайплайн коллектора: приём, обработка, раздача в два бэкенда.**

```yaml
# /opt/observability/otel-collector.yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 400
  batch:
    timeout: 2s
    send_batch_size: 512
  attributes/cleanup:
    actions:
      - key: http.user_agent
        action: delete

exporters:
  otlp/jaeger:
    endpoint: jaeger:4317
    tls: { insecure: true }
  otlphttp/tempo:
    endpoint: http://tempo:4318
  debug:
    verbosity: basic

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, attributes/cleanup, batch]
      exporters: [otlp/jaeger, otlphttp/tempo, debug]
```

Ключевое: `tls.insecure: true` обязателен для Jaeger all-in-one — экспортёр OTel по умолчанию идёт по TLS и молча падает. `memory_limiter` первым в цепочке — защита от OOM при всплеске. `batch` — меньше сетевых вызовов. `debug` — чтобы видеть спаны в `docker compose logs`.

**8. Tail-based сэмплирование: оставить все ошибки и всё, что дольше 300 мс.**

```yaml
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 50000
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow
        type: latency
        latency: { threshold_ms: 300 }
      - name: baseline
        type: probabilistic
        probabilistic: { sampling_percentage: 1 }
```

`decision_wait` — сколько ждать полного трейса перед решением; это и есть цена tail sampling по памяти. Порядок политик = OR: сработала любая — трейс сохранён.

**9. Конфиг Tempo: локальное хранилище + span metrics.**

```yaml
# /opt/observability/tempo.yaml
server:
  http_listen_port: 3200
distributor:
  receivers:
    otlp:
      protocols:
        grpc: { endpoint: 0.0.0.0:4317 }
        http: { endpoint: 0.0.0.0:4318 }
ingester:
  max_block_duration: 5m
compactor:
  compaction:
    block_retention: 24h          # учебный ретеншн; в проде — недели
storage:
  trace:
    backend: local
    wal: { path: /var/tempo/wal }
    local: { path: /var/tempo/blocks }
overrides:
  metrics_generator_processors: [service-graphs, span-metrics]
```

**10. Проверить готовность Tempo и запросить span-метрики в Prometheus.**

```bash
curl -s http://10.10.10.40:3200/ready
curl -sG http://10.10.10.40:9090/api/v1/query \
  --data-urlencode 'query=histogram_quantile(0.95, sum by (le,service)(rate(traces_spanmetrics_latency_bucket[5m])))'
```

Первое — `ready`. Второе — p95 задержки по сервисам, посчитанный **из трейсов**, а не из приложения. Имена метрик `traces_spanmetrics_latency_bucket` и `traces_spanmetrics_calls_total` [ПРОВЕРИТЬ в твоей версии Tempo].

**11. Подключить Tempo и Jaeger как datasource в Grafana (provisioning, не руками).**

```yaml
# /etc/grafana/provisioning/datasources/tracing.yml
apiVersion: 1
datasources:
  - name: Tempo
    type: tempo
    url: http://10.10.10.40:3200
    jsonData:
      tracesToMetrics:
        datasourceUid: prometheus
      serviceMap:
        datasourceUid: prometheus
  - name: Jaeger
    type: jaeger
    url: http://10.10.10.40:16686
```

**12. Связать логи с трейсами.** В приложении логируй `trace_id` и ищи в Loki:

```bash
logcli query '{app="orders"} |= "5b8efff798038103d269b633813fc60c"'
```

## 5. Лабораторная работа

### Предусловия

- **MON01** (10.10.10.40): Docker + Docker Compose, Grafana и Prometheus из уроков 78–79. Если в твоём журнале стенда эти сервисы живут на другой ВМ — используй её и зафиксируй это в `stand-log.md`, имена машин не меняй.
- **UBU01** (10.10.10.30): Docker. Здесь поднимем учебный демо-сервис приёма заявок «Вектора»: `orders` (:5000) и `payments` (:5001).
- **GIT01** (10.10.10.50): GitLab, доступ к нему есть, репозиторий `vektor/observability`.
- Windows-часть стенда не нужна.
- Свободно ~2 ГБ RAM на MON01, ~1 ГБ на UBU01.

### Снапшот

ВМ выключить, затем либо GUI: `VM → Snapshot → Take Snapshot`, имя `before-devops-083`, либо из терминала хоста:

```bash
vmrun snapshot "/path/to/MON01/MON01.vmx" before-devops-083
vmrun snapshot "/path/to/UBU01/UBU01.vmx" before-devops-083
vmrun snapshot "/path/to/GIT01/GIT01.vmx" before-devops-083
vmrun listSnapshots "/path/to/MON01/MON01.vmx"
```

### Шаги

1. **GIT01.** В репозитории `vektor/observability` создай каталоги `tracing/` (конфиги бэкендов) и `apps/orders-demo/` (код демо-сервиса). Закоммить пустую структуру с `README.md`. Ожидаемо: `git push` прошёл, в GitLab видно оба каталога.

2. **GIT01.** Положи в `tracing/` файлы `otel-collector.yaml`, `tempo.yaml`, `docker-compose.yml` из раздела 4 (пункты 7, 8, 9 + compose с сервисами `otel-collector`, `jaeger` с `COLLECTOR_OTLP_ENABLED=true` и портом `16686`, `tempo` с портом `3200`). Закоммить. Ожидаемо: конфиги в Git, а не в блокноте.

3. **MON01.** Клонируй репозиторий и подними бэкенды:

```bash
sudo git clone http://10.10.10.50/vektor/observability.git /opt/observability-src
cd /opt/observability-src/tracing
docker compose config >/dev/null && echo "compose OK"
docker compose up -d
docker compose ps
```

Ожидаемо: `compose OK` и три контейнера в состоянии `running`. Если `compose config` ругается — синтаксис YAML, правь в Git, а не на сервере.

4. **MON01.** Проверь приём спанов вручную (пункт 3 из раздела 4, `curl` на `4318`). Ожидаемо: HTTP `200`. Открой в браузере с хоста `http://10.10.10.40:16686` — в списке сервисов появился `manual-test`.

5. **UBU01.** Собери демо-сервис:

```bash
sudo git clone http://10.10.10.50/vektor/observability.git /opt/observability-src
cd /opt/observability-src/apps/orders-demo
docker compose build
```

Ожидаемо: два образа собраны, в логе видно, что `opentelemetry-bootstrap -a install` доставил `opentelemetry-instrumentation-flask` и `opentelemetry-instrumentation-requests`.

6. **UBU01.** Запусти сервисы с переменными окружения:

```bash
docker compose up -d
docker compose exec orders env | grep -E 'OTEL_(SERVICE_NAME|EXPORTER_OTLP_ENDPOINT|TRACES_SAMPLER)'
```

Ожидаемо: `OTEL_SERVICE_NAME=orders`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://10.10.10.40:4317`, `OTEL_TRACES_SAMPLER=parentbased_traceidratio`.

7. **UBU01.** Сгенерируй трафик: 20 обычных заказов и 3 «медленных» (`?id=slow`, в коде обработчик делает `sleep` на 3.5 с). Ожидаемо: `curl` отвечает `{"order_id":...}`.

8. **MON01.** Зайди в Jaeger UI → Service `orders` → Find Traces. Открой самый долгий трейс. Ожидаемо: дерево из 3+ спанов, у `payments` длительность ~3.5 с, у `orders` — сумма детей. Это и есть «где ушли секунды».

9. **Grafana.** Добавь datasource Tempo и Jaeger (provisioning-файл из п.11 раздела 4), перезапусти Grafana. В Explore → Tempo выполни `{ resource.service.name = "payments" && duration > 300ms }`. Ожидаемо: 3 трейса.

10. **Grafana.** В Jaeger-datasource построй панель «длительность по операциям», в Tempo — Service Graph (нужен Prometheus с включённым remote-write приёмником, иначе граф пустой — это нормально для лабы, зафиксируй в журнале).

11. **GIT01.** Закоммить финальные конфиги и `docker-compose.yml` с зафиксированными тегами образов (не `latest`!). В `stand-log.md` запиши: MON01 — добавлены otel-collector, jaeger, tempo, порты 4317/4318/16686/3200/8889; UBU01 — контейнеры orders/payments, порты 5000/5001.

### Проверка

- [ ] `docker compose ps` на MON01: три контейнера `running`, ни один не в `restarting`.
- [ ] `curl http://10.10.10.40:16686/api/services` содержит `orders` и `payments`.
- [ ] В Jaeger трейс заказа `slow` содержит спан `payments` длительностью > 3 с.
- [ ] `trace_id` в Jaeger совпадает с `trace_id`, который ты видишь в JSON трейса из Tempo API.
- [ ] `curl -s http://10.10.10.40:3200/ready` отвечает `ready`.
- [ ] В Grafana Explore запрос TraceQL возвращает непустой список.
- [ ] В `docker compose logs otel-collector` видны строки `debug`-экспортёра со спанами обоих сервисов.
- [ ] Конфиги в Git, теги образов фиксированные, `stand-log.md` обновлён.

### Сломай и почини

> **Пользователь:** разработчик подрядчика (работает на UBU01)
> **ВМ:** UBU01
> **Что написал:** «Открываю трейс заказа — в нём только `orders`, никакого `payments`. И latency 400 мс вместо 4 секунд. Как будто платежей вообще не было, хотя заказ создался.»
> **Срочность:** средняя, релиз на следующей неделе.

**Как воспроизвести.** На UBU01 убери инструментирование `requests` и обнули проброс контекста:

```bash
docker compose run --rm orders sh -c 'pip uninstall -y opentelemetry-instrumentation-requests'
# либо в docker-compose.yml: OTEL_PROPAGATORS=none для сервиса orders
docker compose up -d
```

**Диагностика по порядку.**

1. Смотрим список сервисов в Jaeger: `payments` есть, но трейсы у него отдельные, со своим `trace_id` и коротким временем. Значит, сервис жив, спаны шлёт, а связь потеряна.
2. Берём `trace_id` трейса `orders` и ищем его в Tempo TraceQL: `{ trace:id = "..." }` — видим единственный спан `orders`. Подтверждает: дочерние спаны не привязались.
3. Проверяем заголовок на входе в `payments`: в логах контейнера ищем `traceparent`. Пусто → контекст не пробрасывается.
4. Проверяем переменные: `docker compose exec orders env | grep OTEL_PROPAGATORS` → `none`. Причина найдена.
5. Проверяем, установлен ли `opentelemetry-instrumentation-requests`: `docker compose exec orders pip list | grep requests` — если инструментирования нет, HTTP-клиент не добавляет `traceparent` автоматически.

**Решение.** Вернуть `OTEL_PROPAGATORS=tracecontext,baggage` (это значение по умолчанию, `none` ставить нельзя), пересобрать образ с `opentelemetry-instrumentation-requests` в зависимостях, `docker compose up -d --build`. Проверить, что в новом трейсе `payments` — дочерний спан, а суммарная длительность соответствует реальной.

**Что ответить пользователю:** «Причина найдена: у сервиса заказов был отключён проброс trace-контекста, поэтому вызов платежей не попадал в общий трейс — выглядело как “быстрый запрос”. Исправлено, перепроверь на новом заказе, старые трейсы останутся “рваными”, это ожидаемо».

**Закрытие заявки.** В тикете: причина, коммит с фиксом, ссылка на новый корректный трейс, добавили проверку `OTEL_PROPAGATORS` в чек-лист ревью конфигов.

### Что изменилось на стенде

- **MON01 (10.10.10.40):** добавлены контейнеры `otel-collector` (4317/4318/8889), `jaeger` (16686), `tempo` (3200, том `tempo-data`); каталог `/opt/observability-src`; в Grafana — datasource Tempo и Jaeger.
- **UBU01 (10.10.10.30):** контейнеры `orders` (5000) и `payments` (5001), образы собраны из `vektor/observability/apps/orders-demo`.
- **GIT01 (10.10.10.50):** репозиторий `vektor/observability` с каталогами `tracing/` и `apps/orders-demo/`.
- **Прочее:** новых учёток, адресов и общих папок не появилось; порты 4317/4318/16686/3200/8889 открыты только внутри учебной сети 10.10.10.0/24.

### Откат

```bash
# на MON01 и UBU01 погасить и убрать тома
docker compose down -v
# затем откат ВМ
vmrun revertToSnapshot "/path/to/MON01/MON01.vmx" before-devops-083
vmrun revertToSnapshot "/path/to/UBU01/UBU01.vmx" before-devops-083
vmrun revertToSnapshot "/path/to/GIT01/GIT01.vmx" before-devops-083
```

В GUI: `VM → Snapshot → Snapshot Manager → before-devops-083 → Go To`. Не забудь после отката дописать в `stand-log.md` строку «откатился на before-devops-083, состояние такое-то».

## 6. Ошибки новичков и безопасность

1. **`user_id`, `order_id`, `session_id` в имени спана или в атрибутах без ограничений** → к миллионам уникальных значений; бэкенд раздувается, поиск деградирует, алерты по метрикам из трейсов «плывут». Правильно: в имени спана — только шаблон маршрута (`GET /order/:id`), идентификатор — в атрибут, и следи за кардинальностью.

2. **Трассировать 100% запросов «чтобы всё видеть»** → накладные расходы по CPU/сети и счёт за хранилище. Правильно: head-based 1–10% на входе + tail-based для ошибок и медленных трейсов.

3. **Нет проброса контекста в асинхронных вызовах** → трейс рвётся на очереди (Kafka/RabbitMQ) или в фоновой задаче. Правильно: инструментировать продюсера и консьюмера, передавать контекст в заголовках сообщения, а не только в HTTP.

4. **Синхронный экспорт из приложения** → каждый запрос ждёт ответа коллектора, при недоступности бэкенда падает латентность сервиса. Правильно: batch-экспортёр + буфер в памяти + таймауты, а ещё лучше — экспорт в локальный sidecar-коллектор.

5. **Трейсы хранятся годами «на всякий случай»** → дорого и бесполезно: трейс нужен для разбора инцидента, а не для аудита. Правильно: 3–7 дней горячих трейсов + span-метрики в Prometheus на месяцы.

6. **В атрибутах — пароли, токены, номера карт, ФИО клиентов** → утечка PII в систему наблюдаемости, к которой доступ шире, чем к БД. Правильно: allow-list атрибутов в коллекторе, `attributes`-процессор с удалением, отдельный процесс на ревью семантических конвенций.

7. **Трейсы не связаны с логами и метриками** → инженер на инциденте прыгает между тремя окнами и вручную сопоставляет время. Правильно: `trace_id` в структурированных логах, exemplars в метриках, `tracesToLogs`/`tracesToMetrics` в Grafana.

8. **`latest` в теге образа бэкенда** → после `docker compose pull` конфиг перестаёт подходить, и непонятно, что сломалось. Правильно: фиксированные теги и обновление осознанно, через Merge Request.

### Безопасность

- **OTLP без аутентификации.** На стенде коллектор слушает `0.0.0.0:4317` без TLS и без токена — кто угодно в сети может лить фальшивые спаны и мусорить в хранилище. В боевой среде: mTLS между приложениями и коллектором, отдельная сеть, приём только от доверенных источников.
- **UI Jaeger без авторизации.** `16686` не должен смотреть в интернет и в общую сеть — только через reverse-proxy с аутентификацией или VPN.
- **Данные в трейсах.** Трейс содержит URL, заголовки, тела SQL-запросов. Это фактически лог с высоким уровнем детализации — обращайся с ним как с персональными данными.
- **Нельзя** отключать файрвол на MON01 «чтобы проверить» — открывай конкретные порты для конкретных адресов.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем трейс отличается от лога и метрики и когда ты идёшь именно в трейсы?**

Ответ: метрика — агрегат за период (счётчик, гистограмма), отвечает «сколько/как быстро в среднем». Лог — дискретное событие с текстом и полями. Трейс — причинно-связанное дерево операций одного запроса с длительностями. В трейсы идут, когда известен конкретный медленный или упавший запрос, но неясно, **где** внутри распределённой цепочки он тормозит; метрики при этом показывают «p99 вырос», логи — «ошибок нет». Трейс даёт критический путь: сумма длительностей по узлам, а не «в среднем по сервису».

*Что хочет услышать интервьюер:* понимание границ инструментов, а не «трейсы лучше, потому что новые».

*Красный флаг:* «трейсы заменяют логи» или «по трейсам строю биллинг».

**Вопрос 2. Что такое context propagation и что делать с очередями и фоновыми задачами?**

Ответ: передача `trace_id`/`span_id` между участниками обработки запроса по стандарту W3C Trace Context — заголовок `traceparent` (версия-trace_id-span_id-flags) плюс `tracestate`. В синхронном HTTP это делает auto-instrumentation клиента. В асинхронщине (Kafka, RabbitMQ, Celery, фоновые воркеры) контекст надо внедрять в заголовки сообщения на продюсере и извлекать на консьюмере; иначе трейс распадётся. Отдельный случай — «fire-and-forget» задачи без входящего запроса: там трейс начинается с нового корневого спана.

*Что хочет услышать интервьюер:* что проблема рваных трейсов на очередях тебе знакома и что ты знаешь про W3C-стандарт, а не «какой-то ID в хедере».

*Красный флаг:* «контекст передаётся автоматически всегда».

**Вопрос 3. Head-based и tail-based сэмплирование — что выберешь и почему?**

Ответ: head-based — решение в SDK на входе, дёшево, детерминированно (по `trace_id`), но вероятность 1% одинаково режет и быстрые, и аварийные трейсы. Tail-based — решение в OTel Collector после сборки трейса целиком: политики «все ошибки», «всё дольше 300 мс», «1% остального». Даёт осмысленную выборку, но требует буферизации (`decision_wait`, `num_traces`), памяти и того, чтобы **все спаны одного трейса** попали в один инстанс коллектора (иначе — `loadbalancingexporter` по `trace_id`). На практике: head-based на входе с разумной долей + tail-based в коллекторе для аномалий.

*Что хочет услышать интервьюер:* понимание цены tail sampling и проблемы маршрутизации спанов.

*Красный флаг:* «tail sampling бесплатный» или «сэмплирую 100% всегда».

**Вопрос 4. Зачем нужен OTel Collector, если SDK умеет отправлять напрямую?**

Ответ: коллектор развязывает приложение и бэкенд: смена бэкенда — правка конфига, а не релиз сервиса. В нём централизованно живут batch, memory_limiter, фильтрация и обрезка атрибутов, tail sampling, fan-out в несколько бэкендов (Jaeger + Tempo) и приём от разных языков по одному протоколу. Плюс приложение меньше зависит от сети до бэкенда — экспорт идёт в локальный агент. Минус — ещё один компонент, который надо масштабировать и мониторить (`:8889/metrics`, метрика `otelcol_receiver_refused_spans` — первый признак перегрузки).

*Что хочет услышать интервьюер:* «коллектор — это про развязку и централизованную политику», а также знание, что коллектор надо самому мониторить.

*Красный флаг:* «коллектор не нужен, шлём напрямую в прод, так проще».

**Вопрос 5. Jaeger или Tempo — что выберешь для «Вектора» и почему?**

Ответ: Jaeger — самодостаточный бэкенд с UI и своим хранилищем (Cassandra/Elasticsearch/Badger), быстрый старт, хорош для средних объёмов и когда нужен UI «из коробки». Tempo — дешёвое долгое хранение в объектном хранилище, нативный друг Grafana, TraceQL для поиска по атрибутам, `metrics-generator` для span-метрик и service graph, то есть мостик в Prometheus. Для компании, где уже есть стек Grafana + Prometheus (наш стенд), Tempo логичнее как основной долгосрочный бэкенд, а Jaeger — как быстрый инструмент разбора инцидентов и для команд, привыкших к его UI. Часто держат оба: коллектор делает fan-out.

*Что хочет услышать интервьюер:* выбор по контексту (уже есть Grafana? объёмы? стоимость хранения?), а не «X лучше».

*Красный флаг:* «Tempo умеет всё то же самое бесплатно» без упоминания TraceQL-специфики и объектного хранилища.

## 8. Источники (официальная документация)

- **OpenTelemetry** — `opentelemetry.io`. Разделы: «Concepts / Signals / Traces», «Instrumentation / Zero-code», «Collector / Configuration». Поиск: `opentelemetry collector configuration receivers processors exporters`, `opentelemetry traces spans concepts`.
- **W3C Trace Context** — `w3.org`. Спецификация «Trace Context». Поиск: `w3c trace context traceparent header format`.
- **Jaeger** — `jaegertracing.io`. Разделы: «Getting Started», «Deployment / all-in-one», «APIs», «Sampling». Поиск: `jaeger all-in-one docker COLLECTOR_OTLP_ENABLED`, `jaeger http api traces`.
- **Grafana Tempo** — `grafana.com/docs/tempo`. Разделы: «Get started / docker-compose example», «Configuration», «TraceQL», «Metrics-generator». Поиск: `tempo monolithic local storage config example`, `tempo traceql query examples`, `tempo span metrics prometheus`.
- **Grafana datasources** — `grafana.com/docs/grafana`. Разделы: «Data sources / Tempo», «Data sources / Jaeger», «Provisioning». Поиск: `grafana tempo datasource provisioning tracesToMetrics`.
- **OpenTelemetry Semantic Conventions** — `opentelemetry.io/docs/specs/semconv`. Раздел «HTTP» (там же — история переименований атрибутов). Поиск: `opentelemetry semantic conventions http attributes`. [ПРОВЕРИТЬ актуальные имена атрибутов на дату чтения — их переименовывали]
- **Пример стека OTel + Tempo + Grafana** — репозиторий `github.com/grafana/tempo` (каталог `example/docker-compose`). [ПРОВЕРИТЬ путь в актуальной ветке]
- **OTLP-спецификация** — `opentelemetry.io/docs/specs/otlp`. Поиск: `otlp specification http json encoding`, `otlp exporter default ports 4317 4318`.
