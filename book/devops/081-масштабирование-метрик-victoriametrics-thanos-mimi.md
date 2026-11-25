# Масштабирование метрик: VictoriaMetrics, Thanos, Mimir, долгосрочное хранение и федерация

## 1. Введение (зачем это нужно в реальной работе)

**Заявка #4821.** От: тимлид дежурной смены. ВМ: MON01. «На дашборде Capacity за прошлый месяц графики пустые, за последние дни рисуется. Для отчёта по ёмкости нужны данные за год: сколько диск рос, как менялась память. Срочность: средняя, отчёт в пятницу».

Вы открываете конфиг Prometheus и видите: retention 15 дней. Всё, что старше, давно удалено, и восстановить это неоткуда. Проблема не в Grafana и не в запросе — проблема в том, что Prometheus по своей архитектуре одноузловой и «короткопамятный».

Такая заявка прилетает регулярно. Метрики нужны не только «чтобы посмотреть, что сейчас», но и для ретроспективы: разбор инцидента месячной давности, отчёт по ёмкости, план закупки железа, проверка, что SLO за квартал реально выполнялся, а не был нарисован. Плюс сам Prometheus не масштабируется горизонтально: один процесс, один локальный диск, никакой репликации «из коробки».

После этой главы вы будете уметь: считать, сколько места съедят метрики и какой retention вам реально нужен; поднимать долгосрочное хранилище (VictoriaMetrics) и отправлять в него данные через `remote_write`; понимать разницу между Thanos, Mimir и VictoriaMetrics и объяснять её на собеседовании; пользоваться федерацией `/federate` и знать, почему она **не** решает задачу долгосрочного хранения; находить виновника раздутого хранилища — кардинальность.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Вся история живёт на прикладном уровне (L7 модели OSI) поверх TCP: Prometheus и его родственники общаются по HTTP, метрики отдаются простым текстом по `/metrics`, запросы идут через `/api/v1/query`. Порты по умолчанию: Prometheus 9090, node_exporter 9100, Alertmanager 9093, Grafana 3000, VictoriaMetrics single-node 8428, vmagent 8429, vmalert 8880, Thanos sidecar/querier/store/compactor 10902 (HTTP) и 10901 (gRPC), Mimir 8080 (HTTP) и 9095 (gRPC) [ПРОВЕРИТЬ порты vmstorage: 8482 HTTP, 8400/8401 внутренние].

**Где болит у обычного Prometheus.** Он хранит данные в локальном TSDB: блоки по 2 часа, WAL для свежих сэмплов, фоновая компакция. Ограничения зашиты в архитектуру:

- нет горизонтального масштабирования и шардирования «из коробки»;
- нет HA: чтобы пережить падение, ставят два независимых Prometheus с одинаковым конфигом, и они пишут **дубликаты**;
- retention только по времени и размеру, без даунсемплинга;
- диск локальный: потеряли ВМ — потеряли историю;
- один процесс упирается в CPU/RAM при тяжёлых запросах и большой кардинальности.

**Кардинальность** — главный враг. Серия = имя метрики + уникальный набор значений лейблов. `http_requests_total{job="api",status="200"}` — одна серия. Добавили лейбл `user_id` — получили тысячи серий на одну метрику. Рекомендация «держитесь в пределах единиц миллионов активных серий» очень грубая и зависит от RAM [ПРОВЕРИТЬ под свой профиль], но правило простое: каждый уникальный набор лейблов стоит памяти, и график «сколько серий у метрики» надо смотреть раньше, чем график «сколько RAM».

**Арифметика хранения.** При scrape 15 с одна серия даёт 5760 сэмплов в сутки. Миллион серий — 5,76 млрд сэмплов в сутки. Prometheus укладывает сэмпл примерно в 1–2 байта после сжатия [ПРОВЕРИТЬ], значит 6–12 ГБ в сутки и ~200–350 ГБ на 30 дней. VictoriaMetrics заявляет заметно лучшую компрессию за счёт колоночного хранения — но проверять надо на своих данных, а не на маркетинге.

**Три способа выйти за пределы одной ноды.**

```text
                    ┌───────────────────────── масштабирование метрик ─────────────────────────┐
1) remote_write     [Prometheus MON01] ──HTTP POST /api/v1/write──▶ [VictoriaMetrics UBU01]
   + remote_read                                                          ▲
                                                                          │ query
                                                                     [Grafana MON01]

2) Thanos (sidecar)  [Prometheus] ──sidecar──▶ [S3 / объектное хранилище] ◀── [store gateway]
                          │                            ▲                        │
                          └────── StoreAPI ────────────┴──────▶ [thanos query] ◀┘
                                                                (глобальный обзор + дедуп)

3) Mimir (микросервисы)  remote_write ─▶ [distributor] ─▶ [ingester] ──2h блоки──▶ [S3]
                                                ▲                                   │
                                          [querier] ◀────── [store-gateway] ◀───────┘

4) Федерация         [Prometheus A] ──GET /federate?match[]=...──▶ [Prometheus B]
                     (только СВЕЖИЕ сэмплы, истории не передаёт)
```

- **`remote_write` / `remote_read`** — встроенный в Prometheus механизм. Prometheus отправляет сэмплы по HTTP на внешний эндпоинт и (опционально) умеет читать оттуда при запросе. Это самый простой путь к долгосрочному хранению.
- **Федерация** — Prometheus скрапит `/federate` другого Prometheus с параметром `match[]` (какие серии тянуть). Это иерархическая схема «региональные Prometheus → центральный». Важно: `/federate` отдаёт только текущие значения из головы TSDB, истории там нет. Поэтому федерация — про агрегацию, а не про архив.
- **Thanos** — набор компонентов вокруг **существующего** Prometheus: sidecar выгружает готовые блоки в объектное хранилище (S3-совместимое, у нас такое было в уроке 73), store gateway отдаёт их обратно, querier собирает единую картину со всех sidecar и gateway, compactor делает компакцию, ретеншн и **даунсемплинг** до 5m и 1h. HA-пары дедуплицируются по лейблу реплики.
- **Grafana Mimir** — горизонтально масштабируемый, многотенантный TSDB, наследник Cortex. Данные принимают distributor'ы, держат ingester'ы (память + WAL), каждые ~2 часа shipper выгружает блок в объектное хранилище, читают querier + store-gateway. Тенант передаётся заголовком `X-Scope-OrgID`. Тяжёлый в эксплуатации, но это выбор для больших инсталляций.
- **VictoriaMetrics** — один бинарник, локальный диск, высокая компрессия, PromQL-совместимый (MetricsQL). Есть кластерная версия из трёх ролей: vminsert (приём), vmstorage (хранение), vmselect (чтение).

## 3. Аналогия из жизни

Prometheus — это сменный журнал дежурного на посту. Дежурный каждые 15 секунд записывает показания приборов в толстую тетрадь. Тетрадь конечная: через 15 дней её сдают в макулатуру, потому что полка одна. Пока нужно «что горит прямо сейчас» — идеально. Как только приходит аудитор и просит записи за прошлый квартал, выясняется, что их нет.

Появляются три разных решения. **Thanos** — курьер, который каждые два часа аккуратно сшивает исписанные страницы в том, увозит в арендованный склад (объектное хранилище) и дополнительно делает краткие конспекты: подробный (5 минут) и совсем краткий (1 час) — чтобы через год можно было посмотреть динамику, не листая миллиард строчек. **Mimir** — целый архивный комбинат: одно окно приёма, несколько бригад, которые держат свежие страницы у себя, а затем пакуют в те же склады; заказов много, можно обслуживать сразу несколько организаций (тенантов). **VictoriaMetrics** — один шкаф-трансформер: ставится в углу, страницы укладывает в пять раз плотнее и почти не требует обслуживания.

**Федерация** — это не архив, а выписка. Центральный дежурный звонит в филиал и просит: «продиктуй только текущие показания по этим трём приборам». Быстро, дёшево, но за прошлый месяц так ничего не восстановишь.

Где аналогия ломается: в реальном архиве книгу можно достать и перечитать целиком, а в TSDB старые сэмплы после даунсемплинга уже не восстановить в исходном разрешении — конспект 5m навсегда останется конспектом.

## 4. Практика и команды

**4.1. Проверить конфиг и увидеть текущий retention Prometheus.**

```bash
promtool check config /etc/prometheus/prometheus.yml
ps -o args= -C prometheus | tr ' ' '\n' | grep -E 'retention|storage.tsdb'
```

`promtool check config` — валидация YAML и правил; `ps` показывает фактические флаги запуска. По умолчанию retention 15 дней (`--storage.tsdb.retention.time=15d`), `--storage.tsdb.retention.size` по умолчанию выключен.

**4.2. Посмотреть, кто съел диск: статус TSDB.**

```bash
curl -s http://10.10.10.40:9090/api/v1/status/tsdb \
  | jq '{series: .data.seriesCountByMetricName[0:5], mem: .data.memoryInBytesByLabelName[0:5]}'
```

В ответе `seriesCountByMetricName` (топ метрик по числу серий), `labelValueCountByLabelName` (лейблы с взрывным числом значений), `memoryInBytesByLabelName`. Смотреть в первую очередь на лейблы вида `path`, `user_id`, `request_id`, `url`.

**4.3. То же самое через PromQL (работает и на удалённом хранилище).**

```promql
topk(10, count by (__name__) ({__name__=~".+"}))
```

Считаем число активных серий по каждой метрике. Если наверху `http_request_duration_seconds_bucket` — нормально (их много по определению). Если наверху что-то с `_id` в лейблах — это авария.

**4.4. Запустить VictoriaMetrics single-node.**

```bash
cd /opt
sudo tar -xzf victoria-metrics-linux-amd64-*.tar.gz -C /opt/victoria-metrics
sudo /opt/victoria-metrics/victoria-metrics-prod \
  -storageDataPath=/var/lib/victoria-metrics-data \
  -retentionPeriod=12 \
  -httpListenAddr=:8428 \
  -memory.allowedPercent=60
```

`-retentionPeriod` — срок хранения; значение без суффикса трактуется как месяцы (12 = год) [ПРОВЕРИТЬ для вашей версии]. `-memory.allowedPercent=60` — сколько RAM отдать под кэши, остальное оставить ОС. `-httpListenAddr` — адрес и порт HTTP API. Версию дистрибутива фиксируйте явно и проверяйте на официальном релизе [ПРОВЕРИТЬ актуальный номер].

**4.5. Оформить как сервис (Infrastructure as Code в миниатюре).**

```ini
# /etc/systemd/system/victoria-metrics.service
[Unit]
Description=VictoriaMetrics single-node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=victoriametrics
Group=victoriametrics
ExecStart=/opt/victoria-metrics/victoria-metrics-prod \
  -storageDataPath=/var/lib/victoria-metrics-data \
  -retentionPeriod=12 \
  -httpListenAddr=:8428 \
  -memory.allowedPercent=60 \
  -dedup.minScrapeInterval=15s
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

`-dedup.minScrapeInterval=15s` — дедупликация, если в VM пишутся две HA-реплики Prometheus с одинаковыми данными.

**4.6. Проверить, что жив и что внутри.**

```bash
curl -s http://10.10.10.30:8428/health
curl -sG 'http://10.10.10.30:8428/api/v1/query' \
  --data-urlencode 'query=count(up)' | jq '.data.result'
```

`/health` возвращает `OK`. Эндпоинты `/api/v1/query`, `/api/v1/query_range`, `/api/v1/labels`, `/api/v1/series` совместимы с Prometheus — поэтому Grafana подключается к VM как к обычному источнику типа Prometheus.

**4.7. Включить `remote_write` на Prometheus (MON01).**

```yaml
# /etc/prometheus/prometheus.yml (фрагмент)
remote_write:
  - url: http://10.10.10.30:8428/api/v1/write
    queue_config:
      max_samples_per_send: 10000
      capacity: 10000
    write_relabel_configs:
      - source_labels: [__name__]
        regex: 'go_.*|process_.*'
        action: drop
```

`write_relabel_configs` фильтрует то, что уходит в архив, не трогая локальные данные. Дропать служебные `go_*`/`process_*` — типовая экономия. `queue_config` управляет буфером отправки на стороне Prometheus.

**4.8. Применить конфиг и посмотреть здоровье отправки.**

```bash
promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://10.10.10.40:9090/-/reload
```

Работает только если Prometheus запущен с `--web.enable-lifecycle`. Метрики здоровья:

```promql
prometheus_remote_storage_samples_failed_total
prometheus_remote_storage_samples_pending
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
```

Первая должна стоять на нуле, вторая — не расти бесконечно, третья — догонять текущее время.

**4.9. Подключить VM в Grafana как источник (provisioning, а не клики).**

```yaml
# /etc/grafana/provisioning/datasources/vm-lts.yaml
apiVersion: 1
datasources:
  - name: VictoriaMetrics-LTS
    uid: vm-lts
    type: prometheus
    access: proxy
    url: http://10.10.10.30:8428
    isDefault: false
    jsonData:
      timeInterval: 30s
```

Префикс `[ПРОВЕРИТЬ стиль для вашей версии Grafana]`: путь провижининга по умолчанию `/etc/grafana/provisioning/datasources/`.

**4.10. Федерация: посмотреть и настроить.**

```bash
curl -sG 'http://10.10.10.40:9090/federate' \
  --data-urlencode 'match[]={job="node"}' \
  --data-urlencode 'match[]=up' | head -15
```

```yaml
# scrape_config на «верхнем» Prometheus
scrape_configs:
  - job_name: federate-mon01
    honor_labels: true
    metrics_path: /federate
    params:
      'match[]':
        - '{job="node"}'
        - 'up'
    static_configs:
      - targets: ['10.10.10.40:9090']
```

`honor_labels: true` сохраняет исходные лейблы, а не перезаписывает их именем джобы. `match[]` обязателен — без него эндпоинт вернёт ошибку.

**4.11. Thanos: sidecar рядом с Prometheus и выгрузка в S3.**

```yaml
# /etc/thanos/objstore.yml
type: S3
config:
  bucket: "thanos-blocks"
  endpoint: "s3.corp.vektor.lab"
  access_key: "REPLACE_ME"
  secret_key: "REPLACE_ME"
  insecure: true
```

```bash
thanos sidecar \
  --tsdb.path=/var/lib/prometheus \
  --prometheus.url=http://127.0.0.1:9090 \
  --objstore.config-file=/etc/thanos/objstore.yml \
  --http-address=0.0.0.0:10902 \
  --grpc-address=0.0.0.0:10901
```

```bash
thanos query --http-address=0.0.0.0:10902 --grpc-address=0.0.0.0:10901 \
  --store=10.10.10.40:10901 \
  --query.replica-label=prometheus_replica
```

`--query.replica-label` включает дедупликацию HA-пары по указанному лейблу. Для корректной работы sidecar блоки Prometheus должны быть фиксированного размера: `--storage.tsdb.min-block-duration=2h --storage.tsdb.max-block-duration=2h` [ПРОВЕРИТЬ для вашей версии]. Компактор с ретеншном и даунсемплингом:

```bash
thanos compact --data-dir=/var/thanos/compact \
  --objstore.config-file=/etc/thanos/objstore.yml \
  --retention.resolution-raw=30d --retention.resolution-5m=180d --retention.resolution-1h=0d
```

**4.12. Mimir: минимальный конфиг для однонодового стенда.**

```yaml
# /etc/mimir/mimir.yaml (фрагмент, ключи проверяйте под свою версию [ПРОВЕРИТЬ])
multitenancy_enabled: false
blocks_storage:
  backend: s3
  s3:
    endpoint: s3.corp.vektor.lab
    bucket_name: mimir-blocks
    access_key_id: REPLACE_ME
    secret_access_key: REPLACE_ME
    insecure: true
  tsdb:
    dir: /data/tsdb
  bucket_store:
    sync_dir: /data/tsdb-sync
```

```bash
mimir -config.file=/etc/mimir/mimir.yaml -target=all
```

`-target=all` — режим «всё в одном процессе», только для тестов и лабораторий; в продакшене роли разделяют.

**4.13. Перенос истории из Prometheus в VM (если очень надо).**

```bash
vmctl prometheus --prom-snapshot=/var/lib/prometheus/snapshots/<TS> \
  --vm-addr=http://10.10.10.30:8428
```

Работает по снапшоту TSDB, а не по живой директории. Точные имена флагов сверяйте с документацией своей версии `vmctl` [ПРОВЕРИТЬ].

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **MON01 (10.10.10.40)** — Prometheus из урока 78 и Grafana из урока 79 установлены и работают; Prometheus скрапит хотя бы node_exporter на UBU01/WEB01.
- **UBU01 (10.10.10.30)** — Ubuntu Server 24.04, SSH, не менее 20 ГБ свободного места (`df -h /`), доступ в интернет или локальное зеркало пакетов. Windows-часть стенда не нужна.
- На обеих ВМ есть пользователь с `sudo` (тот, под которым вы работали в Linux-главах), и настроен вход по SSH.
- Если Prometheus ещё не установлен — сначала пройдите урок 78, без него лабораторная не имеет смысла.

### Снапшот

В VMware Workstation на **MON01** и **UBU01**: `VM → Snapshot → Take Snapshot`, имя `before-devops-081`. Память в снапшот лучше не включать (галочка «Snapshot the virtual machine's memory» снята) — так откат чище.

```bash
vmrun -T ws snapshot "C:\VMs\MON01\MON01.vmx" before-devops-081
vmrun -T ws snapshot "C:\VMs\UBU01\UBU01.vmx" before-devops-081
```

### Шаги

1. **MON01.** Зафиксировать исходное состояние: `du -sh /var/lib/prometheus`, `ps -o args= -C prometheus`. Ожидаемо: размер TSDB единицы ГБ, в аргументах видно `--storage.tsdb.path` и, возможно, `--storage.tsdb.retention.time`.
2. **MON01.** Снять «фотографию» кардинальности по команде 4.2 и 4.3. Ожидаемо: топ-5 метрик по числу серий; запомните цифры — это базовая линия.
3. **UBU01.** Скачать и распаковать VictoriaMetrics в `/opt/victoria-metrics` (команда 4.4, без запуска). Ожидаемо: в каталоге лежит бинарник `victoria-metrics-prod`. Если GitHub из вашей сети недоступен — возьмите зеркало или образ `victoriametrics/victoria-metrics` из своего HARBOR01.
4. **UBU01.** Создать пользователя сервиса и каталог данных: `sudo useradd -r -s /usr/sbin/nologin victoriametrics`, `sudo mkdir -p /var/lib/victoria-metrics-data && sudo chown victoriametrics: /var/lib/victoria-metrics-data`. Ожидаемо: каталог существует, владелец — сервисный пользователь.
5. **UBU01.** Положить unit-файл из 4.5, выполнить `sudo systemctl daemon-reload && sudo systemctl enable --now victoria-metrics`. Ожидаемо: `systemctl is-active victoria-metrics` → `active`.
6. **UBU01.** Проверить API по 4.6 и `ss -lntp | grep 8428`. Ожидаемо: `/health` → `OK`, порт слушается на `0.0.0.0:8428` (или на адресе 10.10.10.30).
7. **MON01.** Добавить блок `remote_write` из 4.7 в `prometheus.yml`, проверить `promtool check config`, перезагрузить по 4.8. Ожидаемо: Prometheus стартует без ошибок, в логах нет `error while sending`.
8. **MON01.** Проверить, что данные доехали: `curl -sG 'http://10.10.10.30:8428/api/v1/query' --data-urlencode 'query=up' | jq '.data.result | length'` и метрику `prometheus_remote_storage_queue_highest_sent_timestamp_seconds`. Ожидаемо: число серий > 0, метрика догоняет `time()`.
9. **MON01.** Добавить источник в Grafana провижинингом (4.9), перезапустить Grafana, проверить `Save & test` в UI. Ожидаемо: «Data source is working».
10. **MON01.** В Grafana построить панель с запросом `count(up)` и диапазоном «Last 30 days», источник — `VictoriaMetrics-LTS`. Ожидаемо: график рисуется. Для честности сравните с источником Prometheus на том же диапазоне — там будет обрыв на 15-й день.
11. **MON01.** Посмотреть федерацию «как есть» командой 4.10 и приложить конфиг из 4.10 в комментарий (второй Prometheus не поднимаем). Ожидаемо: в выводе текстовые строки метрик `node_*` и `up`.
12. **MON01 + UBU01.** Записать изменения в `stand-log.md`. Ожидаемо: в журнале появились обе ВМ с новыми ролями и портом 8428.

### Проверка

- [ ] `systemctl is-enabled victoria-metrics` → `enabled`, `is-active` → `active`.
- [ ] `curl -s http://10.10.10.30:8428/health` → `OK`.
- [ ] `curl -sG 'http://10.10.10.30:8428/api/v1/query' --data-urlencode 'query=count(up)'` возвращает число > 0.
- [ ] `prometheus_remote_storage_samples_failed_total` на MON01 равен 0.
- [ ] В Grafana источник `VictoriaMetrics-LTS` проходит `Save & test`.
- [ ] Панель с диапазоном 30 дней на источнике VM рисуется, на источнике Prometheus — обрывается.
- [ ] `du -sh /var/lib/victoria-metrics-data` растёт со временем.
- [ ] В `stand-log.md` записаны: роль UBU01, порт 8428, правка `prometheus.yml`, новый datasource.
- [ ] Снапшот `before-devops-081` существует на обеих ВМ.

### Сломай и почини

> **Пользователь:** тимлид дежурной смены
> **ВМ:** MON01 + UBU01
> **Что написал:** «Дашборд Capacity: за последние дни данные есть, а с сегодняшнего утра новые точки не появляются. В Grafana по источнику `VictoriaMetrics-LTS` иногда `No data`. Отчёт готовим к пятнице.»
> **Срочность:** высокая (простой наблюдаемости)

**Как воспроизвести поломку.** На UBU01 остановить сервис `sudo systemctl stop victoria-metrics`. Дополнительно (чтобы было интереснее) на MON01 в `prometheus.yml` заменить URL remote_write на несуществующий порт `http://10.10.10.30:8429/api/v1/write` и перезагрузить Prometheus. Ждём 5–10 минут, пока накопится очередь отправки.

**Диагностика по порядку.**

1. Сузить проблему: спросить/посмотреть, какие именно панели и с какого времени. Смотрим в Grafana: «за последние дни есть» — значит старые данные в VM живы, отвалилась **запись**, а не чтение.
2. Проверить доступность VM: `curl -sv http://10.10.10.30:8428/health` с MON01. Если `Connection refused` — идём на UBU01.
3. На UBU01: `ss -lntp | grep 8428` — порт не слушается.
4. `systemctl status victoria-metrics` — здесь видно `inactive (dead)` или причину падения.
5. `journalctl -u victoria-metrics -n 80 --no-pager` — ошибки запуска, `cannot open storage`, `no space left on device`.
6. `df -h /var/lib/victoria-metrics-data` и `dmesg -T | grep -i oom` — исключить переполнение диска и OOM-killer.
7. На MON01: `promql: prometheus_remote_storage_samples_failed_total` растёт, `prometheus_remote_storage_samples_pending` растёт — подтверждение, что Prometheus не может доставить.
8. `journalctl -u prometheus -n 50 | grep -i 'remote'` — в логах типично `non-recoverable error` или `connection refused` с адресом и портом, что сразу выдаёт опечатку в URL.

**Решение.** На MON01 исправить URL в `prometheus.yml` на `http://10.10.10.30:8428/api/v1/write`, `promtool check config`, `curl -X POST http://10.10.10.40:9090/-/reload`. На UBU01 — `sudo systemctl start victoria-metrics`, затем `sudo systemctl status victoria-metrics` и повторный `curl /health`. Через 2–3 минуты проверить, что `prometheus_remote_storage_samples_pending` падает до нуля, а в VM появились свежие точки.

**Что ответить пользователю.** «Данные не терялись: отвалилась отправка метрик в долгосрочное хранилище — сервис на UBU01 был остановлен, плюс в конфиге Prometheus оказался неверный порт. Исправил адрес, поднял сервис, очередь дописалась. Проверьте дашборд за сегодня — точки должны появиться в течение 5 минут. Ставлю алерт на недоступность хранилища и на рост ошибок отправки, чтобы в следующий раз узнавать об этом раньше вас.»

**Закрытие заявки.** Категория «Мониторинг», решение — «восстановлена доставка метрик», корневая причина — «остановка сервиса + ошибка конфигурации», профилактика — алерты `up{job="victoria-metrics"} == 0` и `rate(prometheus_remote_storage_samples_failed_total[5m]) > 0`, плюс `df` на разделе данных в мониторинге.

### Что изменилось на стенде

- **UBU01 (10.10.10.30)** — новая роль: долгосрочное хранилище метрик. Установлен VictoriaMetrics single-node в `/opt/victoria-metrics`, данные в `/var/lib/victoria-metrics-data`, systemd-юнит `victoria-metrics`, retention 12 месяцев, порт 8428, дедупликация `15s`.
- **MON01 (10.10.10.40)** — в `/etc/prometheus/prometheus.yml` появился блок `remote_write` на `http://10.10.10.30:8428/api/v1/write` с фильтром `go_*|process_*`. Локальный retention Prometheus не менялся.
- **Grafana на MON01** — новый провижиненный datasource `VictoriaMetrics-LTS` (uid `vm-lts`), тип prometheus.
- **stand-log.md** — запись: дата, урок 81, что добавлено, порты, снапшот `before-devops-081`.
- Объектное хранилище под Thanos/Mimir **не разворачивалось** — они отработаны только конфигами; если в главе 73 у вас есть S3-совместимый сервис, зафиксируйте его адрес в журнале как потенциальный бэкенд.

### Откат

```bash
vmrun -T ws revertToSnapshot "C:\VMs\UBU01\UBU01.vmx" before-devops-081
vmrun -T ws revertToSnapshot "C:\VMs\MON01\MON01.vmx" before-devops-081
```

Либо в GUI: `VM → Snapshot → Snapshot Manager → before-devops-081 → Revert`. Откат вернёт и конфиг Prometheus, и удалит сервис на UBU01. Перед откатом допишите в `stand-log.md` строку «откат к before-devops-081, причина», иначе через месяц вы не вспомните, почему хранилища нет.

## 6. Ошибки новичков и безопасность

1. **Тащить в долгосрочное хранилище всё подряд.** → Диск и RAM кончаются в разы быстрее, чем планировалось. → Фильтровать через `write_relabel_configs`, дропать служебные метрики, не тащить high-cardinality (`*_id`, `path`, `url`) без необходимости.
2. **Путать федерацию с архивом.** → Настраивают `/federate` и ждут истории за год; получают «No data» и ищут баг. → Федерация отдаёт только свежие сэмплы; для истории — `remote_write` в VM/Thanos/Mimir.
3. **Ставить retention «на глазок», без арифметики.** → Либо диск кончается (сервис падает), либо через месяц выясняется, что данных за квартал нет. → Считать по формуле «серии × 5760 × размер сэмпла × дни» и согласовывать retention с требованиями отчётов и SLO.
4. **Не настроить дедупликацию при HA-паре Prometheus.** → Два одинаковых ряда данных, раздутый объём, странные графики при `sum()`. → `-dedup.minScrapeInterval` в VM или `--query.replica-label` в Thanos; у реплик должен быть различимый лейбл.
5. **Забыть про даунсемплинг.** → Хранят год сырых 15-секундных данных, когда для годового тренда хватило бы 5 минут. → В Thanos — `--retention.resolution-*` и даунсемплинг; в VictoriaMetrics даунсемплинг — Enterprise-функция [ПРОВЕРИТЬ], планируйте объём заранее.
6. **Ставить Thanos compactor больше чем в одном экземпляре.** → Два компактора на одном бакете ломают блоки. → Ровно один compactor на бакет, это классическая ошибка на собеседовании и в проде.
7. **Хранить секреты доступа к S3 прямо в конфиге в Git.** → Утечка ключей с полным доступом к бакету. → Секреты в переменных окружения/secret-хранилище, в Git — только ссылки на них; ключи с минимальными правами на конкретный бакет.
8. **Не мониторить сам мониторинг.** → Хранилище тихо умерло, узнали об этом от тимлида перед отчётом. → Алерты на `up` компонентов, на ошибки отправки, на рост `df` раздела данных.

**Безопасность.** На стенде мы оставляем VictoriaMetrics без аутентификации и слушаем на всех интерфейсах — это допустимо только в изолированной лабораторной сети. В боевой среде так нельзя: хранилище метрик содержит имена хостов, сервисов, версии ПО и косвенно карту инфраструктуры. Минимум — basic auth или обратный прокси с TLS, сетевые политики «принимать записи только с Prometheus», отдельный сервисный пользователь без лишних прав, шифрование бакета S3 и включённое версионирование. Пароли вида `Lab#2026-vektor` — только для этого стенда, никогда для рабочей среды. И не выключайте файрвол «чтобы быстрее проверить»: на стенде это допустимо на минуту с последующим возвратом, в продакшене — инцидент.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. «Prometheus же хранит метрики. Зачем нам что-то ещё?»**

Ответ. Prometheus — одноузловой TSDB с локальным диском: нет горизонтального масштабирования, нет встроенной HA (две реплики пишут дубли), нет даунсемплинга, retention ограничен размером локального диска, история теряется вместе с ВМ. Для оперативного мониторинга («что сейчас») этого достаточно, для ретроспективы на месяцы и годы — нет. Дальше идут варианты: `remote_write` во внешний TSDB (VictoriaMetrics — самый лёгкий вход), Thanos поверх существующего Prometheus с выгрузкой блоков в S3, Mimir для больших многотенантных инсталляций. Плюс отдельно задача агрегации множества Prometheus — тут помогает федерация или глобальный querier.

*Что хочет услышать интервьюер:* что вы понимаете ограничения как архитектурные, а не как «мало места на диске», и можете назвать 2–3 варианта с критериями выбора.

*Красный флаг:* «просто увеличу retention и докуплю диск на сервере Prometheus» — как единственное решение.

**2. «Чем Thanos отличается от Mimir и VictoriaMetrics? Что выберете?»**

Ответ. Thanos — это надстройка над существующим Prometheus: sidecar выгружает блоки в объектное хранилище, store gateway их отдаёт, querier строит глобальный обзор, compactor делает ретеншн и даунсемплинг. Плюс — минимальные изменения, Prometheus остаётся как есть, есть HA-дедуп. Минус — зоопарк компонентов и обязательное объектное хранилище. Mimir — самостоятельный масштабируемый TSDB с разделением на роли (distributor/ingester/querier/store-gateway/compactor), многотенантностью через `X-Scope-OrgID`, хранением блоков в S3; мощнее и сложнее в эксплуатации. VictoriaMetrics — один бинарник, локальный диск, высокая компрессия, MetricsQL, очень простой вход; кластерная версия — vminsert/vmstorage/vmselect. Для маленькой команды без S3 я выберу VM, при уже работающем Prometheus и наличии S3 — Thanos, при десятках тенантов и большом потоке — Mimir.

*Что хочет услышать интервьюер:* сравнение по критериям (сложность эксплуатации, требования к хранилищу, HA, тенантность), а не заученный список компонентов.

*Красный флаг:* уверенное «Mimir — это то же самое, что Thanos» или путаница в том, где именно лежат данные.

**3. «Что такое федерация Prometheus и почему она не заменяет долгосрочное хранение?»**

Ответ. Федерация — это когда Prometheus скрапит эндпоинт `/federate` другого Prometheus с параметром `match[]`, указывающим, какие серии тянуть; обычно с `honor_labels: true`. Применяется для иерархии: много региональных Prometheus, один центральный с агрегатами. Ключевое ограничение: `/federate` отдаёт только текущие значения из головы TSDB, истории там нет. Поэтому для архива используют `remote_write` во внешний TSDB или выгрузку блоков (Thanos/Mimir). Федерация — про агрегацию запросов, не про ретеншн.

*Что хочет услышать интервьюер:* что вы понимаете разницу между «собрать текущее со многих источников» и «хранить долго».

*Красный флаг:* «настроим федерацию, и будет история за год».

**4. «Prometheus падает по OOM. Ваши действия?»**

Ответ. Сначала выясняю, это кардинальность, тяжёлые запросы или просто мало RAM. Смотрю `/api/v1/status/tsdb`: топ метрик по числу серий и по потреблению памяти по лейблам; затем `topk(10, count by (__name__)({__name__=~".+"}))`. Дальше: убираю high-cardinality лейблы в приложении или через `metric_relabel_configs`; дропаю ненужное через `write_relabel_configs` для удалённого хранилища; заменяю тяжёлые ad-hoc запросы recording rules; ограничиваю `--query.max-samples`, таймауты; выношу чтение на отдельный узел (Thanos querier / vmselect) и включаю `remote_write` в долгосрочное хранилище, чтобы Prometheus держал короткий retention. В крайнем случае — шардирование скрапа по нескольким Prometheus с последующей агрегацией.

*Что хочет услышать интервьюер:* системную диагностику по порядку и понимание, что OOM — чаще симптом кардинальности, чем нехватки железа.

*Красный флаг:* «перезапущу и добавлю RAM» без анализа серий.

**5. «Как посчитать, сколько диска нужно под метрики, и выбрать retention?»**

Ответ. Считаю число активных серий (из `/api/v1/status/tsdb` или запросом), умножаю на число сэмплов в сутки при моём интервале scrape (86400/15 = 5760), получаю сэмплов в сутки; умножаю на средний размер сэмпла после сжатия (порядка 1–2 байт для Prometheus, для VM обычно меньше — проверяю на своих данных) и на число дней. Добавляю запас 30–50% на WAL, компакцию и рост. Retention выбираю от требований: оперативный разбор инцидентов — 15–30 дней сырых данных; отчёты по ёмкости и проверка SLO за квартал/год — даунсемплинг до 5m/1h и хранение агрегатов годами. Обязательно фиксирую расчёт в репозитории и пересчитываю при росте числа сервисов.

*Что хочет услышать интервьюер:* формулу, а не «ну гигабайт 500 хватит», и связь retention с бизнес-требованиями.

*Красный флаг:* называет цифры без единиц или не знает, что такое даунсемплинг.

## 8. Источники (официальная документация)

- **Prometheus** — `prometheus.io`. Разделы: «Storage» (retention, TSDB), «Configuration» → `remote_write`/`remote_read`, «Federation» (`/federate`, `match[]`), «HTTP API» (`/api/v1/status/tsdb`). Поиск: `prometheus federation match[]`, `prometheus remote_write configuration`, `prometheus tsdb status api`.
- **VictoriaMetrics** — `docs.victoriametrics.com`. Разделы: «Single-node version», «Cluster version», «vmagent», «vmctl», «Retention», «Deduplication». Поиск: `victoriametrics retentionPeriod flag`, `victoriametrics dedup.minScrapeInterval`, `vmctl prometheus migration` [ПРОВЕРИТЬ актуальные имена флагов].
- **Thanos** — `thanos.io` (документация в `thanos.io/tip/thanos/`). Разделы: «Sidecar», «Querier», «Compactor», «Object Storage» (`objstore.yml`), «Downsampling and Retention». Поиск: `thanos sidecar objstore config`, `thanos compact retention resolution`.
- **Grafana Mimir** — `grafana.com/docs/mimir/`. Разделы: «Architecture», «Configuration» (`blocks_storage`, `s3`), «Migrating from Prometheus», «Multi-tenancy» (`X-Scope-OrgID`). Поиск: `grafana mimir architecture`, `mimir blocks_storage s3 config`, `mimir multi-tenancy header`.
- **Grafana** — `grafana.com/docs/grafana/`. Раздел: «Provisioning → Data sources». Поиск: `grafana provisioning datasource yaml prometheus`.
- **S3-совместимое хранилище** — документация вашего провайдера/дистрибутива (MinIO: `min.io/docs`, раздел «Bucket retention / versioning»). Поиск: `minio bucket versioning lifecycle`.
- **VMware Workstation** — `techdocs.broadcom.com` [ПРОВЕРИТЬ актуальный раздел по Workstation], тема «Snapshots», и `vmrun` в разделе «Using vmrun». Поиск: `vmware workstation vmrun snapshot revertToSnapshot`.

Всё, что помечено `[ПРОВЕРИТЬ]`, сверяйте с документацией под свою версию ПО перед тем, как переносить конфиг на что-то, кроме учебного стенда.
