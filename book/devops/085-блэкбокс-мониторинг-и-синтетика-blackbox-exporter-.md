# Блэкбокс-мониторинг и синтетика: blackbox exporter, uptime-проверки, SLO-дашборды

## 1. Введение (зачем это нужно в реальной работе)

**Заявка #4471.** Пользователь: ivanov / ВМ WIN11-CL01. Текст: «Здравствуйте! Сайт web.vektor.lab с утра не открывается, браузер пишет "не удаётся найти сайт". А у вас в мониторинге всё зелёное, вы вообще смотрите?» Срочность: средняя (работа с заявками клиентов).

Ситуация до боли типовая. Мы научились мерить CPU, RAM, диск, количество запросов — это **whitebox-мониторинг**: мы смотрим *внутрь* сервиса, читая его собственные метрики (`/metrics`). Но метрики внутри могут быть идеальны, пока снаружи сервис недоступен: упал DNS, истёк сертификат, сломался маршрут, nginx отдаёт 502, кто-то поменял A-запись. Внутренние метрики об этом не знают — процесс-то жив.

**Blackbox-мониторинг** — это проверка сервиса снаружи, глазами пользователя: мы стучимся по HTTP, TCP, ICMP, DNS и смотрим, ответили ли нам, как быстро и тем ли самым. Такие проверки называют **синтетическими** (synthetic monitoring): искусственный «пользователь» ходит по сайту по расписанию и докладывает результат.

После этой главы ты будешь уметь: поднять blackbox_exporter, правильно подключить его к Prometheus через multi-target pattern (это главный «трюк» темы), настроить пробы HTTP/TCP/ICMP/DNS, превратить результаты проб в SLI и SLO-дашборд с error budget и burn rate, и — самое важное — сделать так, чтобы мониторинг ловил именно те поломки, которые видит пользователь, а не только те, которые удобно мерить.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Ключевая идея: blackbox_exporter — это **не** обычный экспортер, который сам ходит по расписанию. Он пассивный: слушает порт **9115** и ждёт, когда Prometheus попросит его «проверь вон ту цель вот таким модулем». Проверка делается в момент запроса, результат отдаётся как обычные метрики в формате Prometheus.

Запрос выглядит так: `GET http://MON01:9115/probe?target=<цель>&module=<модуль>`. Обязательные параметры передаются как `__param_target` и `__param_module` при scrape — отсюда и растут ноги у relabeling (раздел 4).

```text
                 офисная сеть 10.10.10.0/24              DMZ 10.10.20.0/24
 ┌──────────────┐        scrape /probe?target=...       ┌──────────────┐
 │  Prometheus  │ ────────────────────────────────────► │  WEB01       │
 │  MON01:9090  │        blackbox_exporter :9115        │  10.10.20.10 │
 └──────┬───────┘              │                        └──────────────┘
        │                      │  реальная проба:
        │                      │  L3 ICMP / L4 TCP / L7 HTTP,DNS,gRPC
        │                      ├──────────────► GIT01 10.10.10.50 (https)
        │                      ├──────────────► UBU01 10.10.10.30:22 (tcp)
        │                      └──────────────► DC01  10.10.10.10:53 (dns)
        │
        ▼
   probe_success, probe_duration_seconds, probe_http_status_code,
   probe_ssl_earliest_cert_expiry  ──►  recording rules  ──►  SLO/SLI
                                             │
                                             ▼
                                    Alertmanager ──► Grafana
```

**Multi-target pattern.** Один экземпляр blackbox_exporter обслуживает *сотни* целей: Prometheus для каждой цели делает отдельный scrape, подставляя её в `target`. Внутри конфига экспортера описаны **модули** — именованные наборы правил («http_2xx», «tcp_connect», «icmp», «dns_vektor»), каждый со своим prober'ом, таймаутом и параметрами.

**Где это в OSI.** ICMP-проба — L3 (жив ли хост, есть ли маршрут). TCP-проба — L4 (открыт ли порт, отвечает ли accept). HTTP/DNS/gRPC-проба — L7, плюс отдельно TLS-слой (срок жизни сертификата, handshake). Поэтому набор из трёх проб на один сервис (`icmp` + `tcp_connect` + `http_2xx`) сразу показывает, *на каком слое* сломалось — это половина диагностики.

**Что возвращается.** Основные метрики: `probe_success` (1/0), `probe_duration_seconds` (вся проба), `probe_http_status_code`, `probe_http_duration_seconds{phase="resolve|connect|tls|processing|transfer"}`, `probe_ssl_earliest_cert_expiry` (unix-время), `probe_dns_lookup_time_seconds`. Из них и строятся SLI.

**SLI/SLO в двух словах** (подробно — урок 77). SLI — измеряемая величина, здесь «доля успешных проб за период». SLO — целевое значение, например 99.5% за 30 дней. Error budget — сколько секунд/процентов отказа мы имеем право «потратить»: при 99.5% это 0.5% времени, ~3.6 часа в месяц. Burn rate — скорость траты бюджета: 1.0 = тратим ровно столько, чтобы закончить месяц в ноль; 14.4 = сожжём весь месячный бюджет за час.

**Чего blackbox не видит.** Он не проверяет бизнес-логику, не логинится под пользователем, не ходит по всей воронке. Для этого нужны либо пробы со сценарием (browser-синтетика, Playwright/k6), либо RUM (real user monitoring). Blackbox — это «верхний уровень» проверки доступности.

## 3. Аналогия из жизни

Представь **тайного покупателя**. Магазин (сервис) каждый день отчитывается руководству: «касса работает, товар на полках, электричество есть» — это whitebox-метрики. Но однажды утром тайный покупатель подходит к двери, дёргает ручку, засекает секундомером, сколько ждал, и проверяет, что ему ответили «Добро пожаловать», а не «Закрыто на учёт». Он докладывает строго по форме: дошёл (1/0), сколько секунд, что увидел на двери. Если он ходит каждые 15 секунд и записывает результат, у нас появляется журнал, из которого считается «доля успешных визитов за 30 дней» — это и есть SLI, а договорённость «дверь должна открываться в 99.5% визитов» — SLO.

Аналогия перестаёт работать в одном месте: тайный покупатель видит только витрину и дверь — если внутри магазина кассир считает неправильно, он этого не заметит.

## 4. Практика и команды

Все команды — на **MON01** (10.10.10.40), где живут Prometheus и Grafana (поставлены в уроках 78–79).

**1. Скачать и установить бинарь.** Пакета в apt обычно нет, берём официальный релиз с GitHub.

```bash
VER=0.25.0   # [ПРОВЕРИТЬ] актуальную версию на github.com/prometheus/blackbox_exporter/releases
cd /tmp
curl -fLO https://github.com/prometheus/blackbox_exporter/releases/download/v${VER}/blackbox_exporter-${VER}.linux-amd64.tar.gz
tar xzf blackbox_exporter-${VER}.linux-amd64.tar.gz
sudo install -m 0755 blackbox_exporter-${VER}.linux-amd64/blackbox_exporter /usr/local/bin/
blackbox_exporter --version
```

Ожидаемый вывод (сокращённо): `blackbox_exporter, version 0.25.0 ... build user ... go version go1.21.x`. Смотрим: версия та, что качали, бинарь исполняемый. GitHub из РФ доступен легально, но скорость бывает плохой — качай с зеркала или через прокси, пиратские сборки не бери.

**2. Сервисный пользователь и конфиг с модулями.**

```bash
sudo useradd --no-create-home --shell /usr/sbin/nologin blackbox
sudo mkdir -p /etc/blackbox_exporter
sudo chown -R blackbox:blackbox /etc/blackbox_exporter
```

```yaml
# /etc/blackbox_exporter/blackbox.yml
modules:
  http_2xx:
    prober: http
    timeout: 5s
    http:
      valid_http_versions: ["HTTP/1.1", "HTTP/2.0"]
      valid_status_codes: [200, 204]      # по умолчанию 2xx
      method: GET
      preferred_ip_protocol: ip4
      follow_redirects: true

  http_2xx_tls:                            # та же проба, но обязателен HTTPS
    prober: http
    timeout: 5s
    http:
      fail_if_not_ssl: true
      fail_if_body_not_matches_regexp:
        - "Вектор"                          # проверяем, что отдали наш контент

  tcp_connect:
    prober: tcp
    timeout: 5s

  icmp:
    prober: icmp
    timeout: 5s
    icmp:
      preferred_ip_protocol: ip4

  dns_vektor:
    prober: dns
    timeout: 5s
    dns:
      query_name: corp.vektor.lab
      query_type: A
      transport_protocol: udp
      preferred_ip_protocol: ip4
```

Ключи: `prober` — тип проверки; `timeout` — жёсткий предел (экспортер обрывает пробу сам); `valid_status_codes` — какие коды считать успехом (пустой список = любой 2xx); `fail_if_body_not_matches_regexp` — проверка **содержимого**, спасает от «страницы-заглушки с кодом 200». Флаг `ip_protocol_fallback` в новых версиях менялся — [ПРОВЕРИТЬ] под свою версию.

**3. systemd unit с правом на ICMP.**

```ini
# /etc/systemd/system/blackbox_exporter.service
[Unit]
Description=Blackbox Exporter
After=network-online.target
Wants=network-online.target

[Service]
User=blackbox
Group=blackbox
Type=simple
ExecStart=/usr/local/bin/blackbox_exporter \
  --config.file=/etc/blackbox_exporter/blackbox.yml \
  --web.listen-address=:9115
Restart=on-failure
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

`CAP_NET_RAW` нужен для ICMP: сырые сокеты требуют прав. Запускать экспортер от root — плохая практика; `AmbientCapabilities` даёт только то, что нужно.

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now blackbox_exporter
systemctl status blackbox_exporter --no-pager
ss -lntp | grep 9115
```

В статусе ищем `active (running)` и отсутствие строк `level=error`. `ss` должен показать `LISTEN 0 ... :9115`.

**4. Проверить конфиг без запуска.**

```bash
blackbox_exporter --config.check --config.file=/etc/blackbox_exporter/blackbox.yml
```

Флаг `--config.check` [ПРОВЕРИТЬ в `blackbox_exporter --help`] прогоняет YAML и печатает `Config file is valid` либо ошибку с номером строки.

**5. Ручная проба с отладкой — главный инструмент диагностики.**

```bash
curl -s "http://127.0.0.1:9115/probe?target=http://10.10.20.10&module=http_2xx&debug=true"
```

Сокращённый типичный вывод:

```text
Logs for the probe:
ts=... level=info msg="Beginning probe" probe=http timeout_seconds=5
ts=... level=info msg="Resolving target address" target=10.10.20.10 ip_protocol=ip4
ts=... level=info msg="Resolved target address" ip=10.10.20.10
ts=... level=info msg="Making HTTP request" url=http://10.10.20.10 method=GET
ts=... level=info msg="Received HTTP response" status_code=200
ts=... level=info msg="Response timings for round trip" duration_seconds=0.012
Metrics that would have been returned:
probe_duration_seconds 0.0128
probe_http_status_code 200
probe_success 1
```

Что смотреть: `Beginning probe` (модуль подхватился), `Resolving target address` (если зависло тут — проблема DNS), `Received HTTP response` (код), финальный `probe_success`. Без `debug=true` в ответе будут только метрики.

**6. Метрики самого экспортера.**

```bash
curl -s http://127.0.0.1:9115/metrics | grep -E '^(blackbox_exporter_build_info|promhttp_metric_handler_requests_total)'
```

Здесь полезен `blackbox_exporter_build_info{version="0.25.0"}` и счётчики запросов к хендлеру. Это то, что скрейпит job «blackbox-exporter» в Prometheus (мониторим сам мониторинг).

**7. Подключение к Prometheus — multi-target pattern.**

```yaml
# /etc/prometheus/prometheus.yml  (фрагмент)
scrape_configs:
  - job_name: 'blackbox-http'
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - http://10.10.20.10
          - http://web.vektor.lab
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target      # цель уезжает в query-параметр
      - source_labels: [__param_target]
        target_label: instance           # в метках видно, ЧТО проверяли
      - source_labels: [__param_module]
        target_label: module             # удобно для алертов и дашбордов
      - target_label: __address__
        replacement: 127.0.0.1:9115      # а скрейпим сам экспортер

  - job_name: 'blackbox-tcp'
    metrics_path: /probe
    params:
      module: [tcp_connect]
    static_configs:
      - targets:
          - 10.10.10.30:22    # UBU01 SSH
          - 10.10.10.50:443   # GIT01 GitLab
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: 127.0.0.1:9115

  - job_name: 'blackbox-icmp'
    metrics_path: /probe
    params:
      module: [icmp]
    static_configs:
      - targets: ['10.10.20.10', '10.10.10.30', '10.10.10.50']
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: 127.0.0.1:9115
```

Разбор: `__address__` — служебная метка «куда идти за метриками»; мы перекладываем её значение в `__param_target` (Prometheus подставит его в URL как `?target=`), а в `__address__` кладём адрес экспортера. `__param_module` — из блока `params`. Если забыть третий relabel (`instance`), все цели схлопнутся в одну метку `instance="127.0.0.1:9115"` — и дашборд будет бесполезен.

**8. Таргеты из файла (file_sd) — чтобы не переписывать prometheus.yml.**

```yaml
# /etc/prometheus/targets/blackbox-http.yml
- targets:
    - http://web.vektor.lab
    - https://git.vektor.lab
  labels:
    env: dmz
    team: devops
```

```yaml
# в prometheus.yml вместо static_configs
    file_sd_configs:
      - files: ['/etc/prometheus/targets/blackbox-http.yml']
        refresh_interval: 30s
```

Плюс: файл таргетов можно генерировать автоматикой и хранить в Git (GIT01).

**9. Проверить и перезагрузить Prometheus.**

```bash
promtool check config /etc/prometheus/prometheus.yml
curl -sX POST http://127.0.0.1:9090/-/reload && echo reloaded
```

`promtool` должен сказать `SUCCESS`. Дальше: в UI Prometheus → **Status → Targets**, ищем job'ы `blackbox-*`, состояние должно быть `UP`. В Graph проверяем `probe_success{job="blackbox-http"}`.

**10. PromQL: доступность и задержка.**

```promql
# доля успешных проб за 30 дней, % (SLI доступности)
avg_over_time(probe_success{job="blackbox-http", instance="http://web.vektor.lab"}[30d]) * 100

# 95-й процентиль времени ответа за сутки
histogram_quantile(0.95, sum by (le, instance) (rate(probe_http_duration_seconds_bucket[1d])))
```

`avg_over_time` по ряду из 0/1 даёт долю единиц — это и есть SLI. Внимание: `[30d]` и `[1d]` требуют, чтобы Prometheus столько хранил; по умолчанию retention 15 дней — для долгих окон нужен Thanos/Mimir/VictoriaMetrics из урока 81.

**11. PromQL: срок жизни сертификата.**

```promql
(probe_ssl_earliest_cert_expiry{job="blackbox-http"} - time()) / 86400
```

Результат — сколько **дней** осталось. 86400 — секунд в сутках. Классический алерт — меньше 21 дня (warning) и 7 дней (critical), чтобы успели перевыпустить.

**12. PromQL: burn rate и остаток error budget.**

```promql
# SLO 99.5%. Burn rate за 1 час: сколько раз тратим бюджет быстрее нормы
(1 - avg_over_time(probe_success{job="blackbox-http", instance="http://web.vektor.lab"}[1h])) / (1 - 0.995)

# Остаток бюджета в долях (1 = целый, 0 = сожгли весь месячный)
1 - (1 - avg_over_time(probe_success{job="blackbox-http", instance="http://web.vektor.lab"}[30d])) / (1 - 0.995)
```

Порог `> 14.4` означает «с такой скоростью сожжём весь 30-дневный бюджет за час» (2% за час) — повод будить дежурного. `> 6` — «за 6 часов сожжём 5%» (тикет на разбор). Цифры 14.4 и 6 — стандартные из SRE Workbook (multi-window multi-burn-rate).

**13. Recording и alerting rules.**

```yaml
# /etc/prometheus/rules/blackbox-slo.yml
groups:
  - name: blackbox-slo
    interval: 30s
    rules:
      - record: slo:probe_success:ratio_5m
        expr: avg_over_time(probe_success{job="blackbox-http"}[5m])
      - record: slo:probe_success:ratio_1h
        expr: avg_over_time(probe_success{job="blackbox-http"}[1h])
      - record: slo:burnrate_1h
        expr: (1 - slo:probe_success:ratio_1h) / (1 - 0.995)

  - name: blackbox-alerts
    rules:
      - alert: BlackboxProbeFailed
        expr: probe_success == 0
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Проба {{ $labels.instance }} падает 2 минуты"
          description: "Модуль {{ $labels.module }}, job {{ $labels.job }}"

      - alert: SLOBurnRateHigh
        expr: slo:burnrate_1h{instance="http://web.vektor.lab"} > 14.4
        for: 2m
        labels:
          severity: page
        annotations:
          summary: "SLO веб-сервиса выгорает в 14+ раз быстрее нормы"
```

`for: 2m` гасит одиночные «моргания» сети. `{{ $labels.module }}` работает только если ты добавил `module` через relabel (пункт 7).

**14. Панели в Grafana.** Через provisioning (урок 79) или руками — Grafana UI → Dashboards → New. Панель «Uptime 30d» (Stat): запрос из п.10, единицы `percent`, пороги 99.5 / 99.0. Панель «Burn rate 1h» (Gauge): п.12, пороги 1 / 6 / 14.4. Панель «Сертификат, дней» (Stat): п.11. Панель «probe_success по целям» (State timeline): `probe_success` с `legendFormat={{instance}}`.

## 5. Лабораторная работа

### Предусловия

- **MON01** (10.10.10.40) — Ubuntu Server, установлены Prometheus (:9090) и Grafana (:3000) из уроков 78–79, Alertmanager из урока 80. Есть маршрут в DMZ: `10.10.20.0/24 via 10.10.10.3` (GW01) — проверь `ip route`.
- **WEB01** (10.10.20.10) — nginx из L2-трека, отдаёт страницу на 80 порту.
- **GIT01** (10.10.10.50) — GitLab CE, доступен по HTTPS на 443.
- **UBU01** (10.10.10.30) — SSH на 22.
- **DC01** и Windows-часть для этой лабы не обязательны. Если DC01 есть и в его DNS заведена A-запись `web.vektor.lab` → 10.10.20.10, используй имя. Если нет — добавь на MON01 строку `10.10.20.10 web.vektor.lab` в `/etc/hosts` и запиши в `stand-log.md`, что имя резолвится локально (учебный костыль, в бою так не делают — DNS должен быть настоящий).
- Доступ `sudo` на MON01.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-devops-085`. Машины: **MON01**, **WEB01**, **GIT01**. Плюс `git commit` текущего состояния репозитория мониторинга на GIT01, чтобы откат конфигов был и в коде.

### Шаги

1. **MON01.** Установи blackbox_exporter по пунктам 1–2 раздела 4. Ожидаемо: `blackbox_exporter --version` печатает версию, `/etc/blackbox_exporter/blackbox.yml` создан.
2. **MON01.** Создай unit-файл (п.3), выполни `daemon-reload`, `enable --now`, проверь `systemctl status` и `ss -lntp | grep 9115`. Ожидаемо: сервис активен, порт слушается.
3. **MON01.** Ручная проба в обе стороны:
   ```bash
   curl -s "http://127.0.0.1:9115/probe?target=http://10.10.20.10&module=http_2xx&debug=true" | tail -5
   curl -s "http://127.0.0.1:9115/probe?target=http://10.10.20.99&module=http_2xx" | grep probe_success
   ```
   Ожидаемо: первая — `probe_success 1`, вторая — `probe_success 0`. Если обе 0 — проблема с маршрутом до DMZ, проверь `ip route` и `ping 10.10.20.10`.
4. **MON01.** Добавь в `blackbox.yml` модули `icmp`, `tcp_connect`, `dns_vektor`, перезапусти сервис. Проверь ICMP руками: `curl -s "http://127.0.0.1:9115/probe?target=10.10.10.30&module=icmp" | grep probe_success`. Ожидаемо: `1`. Если `0` и в логах `socket: operation not permitted` — не хватает `CAP_NET_RAW`.
5. **MON01.** Добавь в `prometheus.yml` три job'а из п.7 (или file_sd из п.8), выполни `promtool check config` и reload. Ожидаемо: `SUCCESS`, в Prometheus UI → Status → Targets все `blackbox-*` в состоянии UP.
6. **MON01.** В Prometheus Graph выполни `probe_success` и `probe_duration_seconds`. Ожидаемо: по одной серии на каждую цель с корректной меткой `instance` (не `127.0.0.1:9115`!). Если все серии схлопнулись — вернись к relabel-правилам.
7. **MON01.** Создай `/etc/prometheus/rules/blackbox-slo.yml` из п.13, подключи в `prometheus.yml` (`rule_files:`), сделай reload. Проверь в UI → Status → Rules: группы `blackbox-slo` и `blackbox-alerts` загружены, recording-правила дают значения.
8. **MON01.** Собери дашборд Grafana «SLO: Blackbox» из четырёх панелей (п.14). Ожидаемо: Uptime ≈ 100%, burn rate ≈ 0, сертификат GIT01 показывает десятки дней.
9. **GIT01.** Залей конфиги в репозиторий `monitoring/`:
   ```bash
   git add blackbox/blackbox.yml prometheus/prometheus.yml prometheus/rules/blackbox-slo.yml grafana/dashboards/slo-blackbox.json
   git commit -m "lesson 85: blackbox exporter, synthetic probes, SLO dashboard"
   git push
   ```
   Ожидаемо: коммит в GitLab виден в веб-интерфейсе.
10. **MON01.** Допиши в `stand-log.md`: установлен blackbox_exporter :9115, модули, job'ы `blackbox-http/tcp/icmp`, новые правила и дашборд.

### Проверка

- [ ] `systemctl status blackbox_exporter` — `active (running)`, в логах нет `level=error`.
- [ ] `curl "http://127.0.0.1:9115/probe?target=http://10.10.20.10&module=http_2xx"` возвращает `probe_success 1`.
- [ ] ICMP-проба до 10.10.10.30 возвращает `probe_success 1`.
- [ ] TCP-проба `10.10.10.30:22` возвращает `1`, а `10.10.10.30:2222` — `0`.
- [ ] В Prometheus Status → Targets все job'ы `blackbox-*` в состоянии UP.
- [ ] `probe_success{job="blackbox-http"}` имеет разные значения `instance` для разных целей.
- [ ] В Status → Rules видны обе группы, `slo:burnrate_1h` считается.
- [ ] На дашборде Grafana есть панели uptime, burn rate, срока сертификата и `probe_success` по целям.
- [ ] Конфиги закоммичены в GIT01, `stand-log.md` обновлён.

### Сломай и почини

> **Пользователь / ВМ:** ivanov / WIN11-CL01
> **Что написал:** «Сайт web.vektor.lab не открывается, браузер пишет "не удаётся найти сайт". В мониторинге у вас всё зелёное, проверьте уже.»
> **Срочность:** средняя

**Как воспроизвести поломку.** На MON01 проба настроена на `http://10.10.20.10` (по IP), а пользователь ходит по имени. Сымитируй расхождение: если есть DC01 — поменяй A-запись `web.vektor.lab` на `10.10.20.99`; если DNS на стенде нет — на MON01 в `/etc/hosts` поставь `10.10.20.99 web.vektor.lab` (не забудь потом вернуть). Сервис на WEB01 при этом полностью жив.

**Диагностика по порядку:**

1. Смотрим мониторинг: `probe_success{job="blackbox-http"}` → `1`, `probe_http_status_code` → `200`. Алертов нет.
2. Ключевой вопрос: **что именно** проверяет проба? Смотрим метку `instance` — там `http://10.10.20.10`, то есть IP, а не имя, которым пользуются люди. Мониторинг смотрит не туда, куда смотрит пользователь.
3. Проверяем глазами пользователя с MON01:
   ```bash
   curl -sv --max-time 5 http://web.vektor.lab/ 2>&1 | head -20
   getent hosts web.vektor.lab
   ```
   Ожидаемо: `Could not resolve host` или адрес `10.10.20.99` и таймаут. `getent` покажет неверный IP.
4. Проверяем, что сервис жив: `curl -sI http://10.10.20.10/` → `HTTP/1.1 200 OK`. Значит, виноват не nginx, а разрешение имени.
5. Проверяем DNS-слой отдельно: `dig @10.10.10.10 web.vektor.lab +short` (если DC01/DNS есть) или смотрим `/etc/hosts`. Находим подменённую запись.
6. Вывод: **сломан DNS/резолвинг, а не сервис**. Мониторинг был зелёным, потому что проверял IP в обход имени.

**Решение.** Вернуть корректную A-запись (или строку в `/etc/hosts`), проверить `getent hosts web.vektor.lab` → `10.10.20.10`, затем `curl -sI http://web.vektor.lab/` → 200. Затем **починить мониторинг как код** (через GIT01, коммит с описанием инцидента): добавить в `file_sd` цель `http://web.vektor.lab` и отдельную DNS-пробу модулем `dns_vektor` на 10.10.10.10 со `query_name: web.vektor.lab`. Проба по IP остаётся, но перестаёт быть единственной.

**Что написать пользователю:** «Иван, проблема была не в сайте, а в разрешении имени: ваше рабочее место получало неверный адрес. Сейчас исправлено, проверьте, пожалуйста, страницу (возможно, нужно закрыть вкладку и открыть заново). Дополнительно мы добавили проверку по имени, чтобы такие сбои ловились автоматически, а не только по жалобе».

**Закрытие заявки.** Категория «Сеть и DNS», решение «исправлена запись DNS», ссылка на коммит `monitoring` в GIT01, комментарий про добавленные пробы.

### Что изменилось на стенде

- **MON01:** установлен `blackbox_exporter` (бинарь `/usr/local/bin/`, конфиг `/etc/blackbox_exporter/blackbox.yml`, unit `blackbox_exporter.service`, порт **9115**, пользователь `blackbox`, capability `CAP_NET_RAW`).
- **MON01:** в `prometheus.yml` добавлены job'ы `blackbox-http`, `blackbox-tcp`, `blackbox-icmp`, `blackbox-dns`; появился каталог `/etc/prometheus/targets/`.
- **MON01:** добавлен файл правил `/etc/prometheus/rules/blackbox-slo.yml` (recording `slo:*`, алерты `BlackboxProbeFailed`, `SLOBurnRateHigh`).
- **MON01 (Grafana):** дашборд «SLO: Blackbox» с панелями uptime / burn rate / cert expiry / probe_success.
- **GIT01:** репозиторий `monitoring` с каталогами `blackbox/`, `prometheus/`, `grafana/`; конфиги версионируются.
- **Цели под наблюдением:** 10.10.20.10 (WEB01, http+icmp), web.vektor.lab, 10.10.10.30:22, 10.10.10.50:443, 10.10.10.10 (DNS).
- **SLO по умолчанию:** доступность 99.5% за 30 дней для WEB01.
- **Изменения в WEB01:** нет (сервис не трогали).

### Откат

VMware Workstation → выбери ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-devops-085` → **Go To**. Так на MON01, WEB01 и GIT01. Учти: откат стирает текущее состояние диска с момента снапшота — сначала сохрани нужные файлы на хост. Если нужно откатить только конфиги, но оставить экспортер: `git checkout` нужной ревизии в репозитории `monitoring` на GIT01 и скопировать файлы обратно на MON01, затем `systemctl reload-or-restart` для Prometheus и blackbox_exporter.

## 6. Ошибки новичков и безопасность

1. **Ошибка:** проверять сервис по IP и порту напрямую. → **К чему:** мониторинг зелёный, пользователи без сервиса (как в лабе выше). → **Как правильно:** проба должна идти тем путём, которым ходит пользователь — по имени, через тот же балансировщик и те же правила firewall.
2. **Ошибка:** забыть relabel `instance`. → **К чему:** все цели превращаются в одну серию `instance="127.0.0.1:9115"`; дашборд бесполезен, алерт не говорит, что упало. → **Как правильно:** всегда три правила relabel (п.7) и проверка в Graph.
3. **Ошибка:** считать успехом только `200` и не проверять тело ответа. → **К чему:** страница-заглушка или страница ошибки с кодом 200 даёт `probe_success 1`. → **Как правильно:** `fail_if_body_matches_regexp` / `fail_if_body_not_matches_regexp` для ключевых сервисов, плюс проба на функциональный endpoint, а не на `/`.
4. **Ошибка:** алерт `probe_success == 0` без `for:`. → **К чему:** один потерянный пакет или деплой на 20 секунд — и дежурного будят ночью. → **Как правильно:** `for: 2m`–`5m` для одиночных проб, multi-window burn rate для SLO-алертов.
5. **Ошибка:** запускать blackbox_exporter от root «чтобы ICMP работал». → **К чему:** скомпрометированный экспортер = root на хосте; экспортер слушает порт и парсит внешние ответы, поверхность атаки ненулевая. → **Как правильно:** `CapabilityBoundingSet`/`AmbientCapabilities` c `CAP_NET_RAW` и отдельный непривилегированный пользователь.
6. **Ошибка:** ставить проверки «на всё» каждые 5 секунд. → **К чему:** тысячи лишних серий, нагрузка на цели, шум в TSDB. → **Как правильно:** разные интервалы: критичные имена — 15–30 с, остальное — 60 с; long-term storage (урок 81) для 30-дневных окон.
7. **Ошибка:** ставить SLO 100%. → **К чему:** error budget равен нулю, любой сбой — нарушение SLO, релизы останавливаются, а команда перестаёт верить в цифры. → **Как правильно:** SLO чуть ниже реальных возможностей (99.5 / 99.9), бюджет тратится осознанно.
8. **Ошибка:** пробовать только «снаружи-внутрь» и не смотреть, что происходит внутри при падении пробы. → **К чему:** алерт есть, причины нет; MTTD маленький, MTTR огромный. → **Как правильно:** в дашборде рядом с `probe_success` держать `probe_http_duration_seconds{phase=...}` и внутренние метрики сервиса — blackbox говорит «что», whitebox подсказывает «почему».

**Безопасность.** Экспортер умеет ходить на произвольный `target` из query-параметра — это фактически SSRF-инструмент. В боевой среде: порт 9115 никогда не публикуется в интернет, доступ к `/probe` разрешён только адресу Prometheus (firewall/nftables), иначе через него можно сканировать внутреннюю сеть. Пробы по HTTP-логину не должны использовать реальные учётки администратора — только отдельный сервисный аккаунт с минимальными правами. Логи с `debug=true` могут содержать заголовки и токены — не включай их на постоянку. И не превращай синтетику в способ обхода лицензий и внешних лимитов чужих API.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем blackbox-мониторинг отличается от мониторинга экспортерами и когда нужен каждый?**

**Ответ.** Экспортер (node_exporter, mysqld_exporter, `/metrics` приложения) — это whitebox: он отдаёт метрики о *внутреннем состоянии* компонента, потому что живёт рядом с ним или внутри. Blackbox-экспортер ничего не знает о внутренностях: он делает реальный сетевой запрос снаружи и фиксирует факт и время ответа. Whitebox отвечает на вопрос «почему», blackbox — «доступно ли это пользователю». Нужны оба: blackbox — как первый и самый дешёвый слой (доступность, сертификаты, DNS, латентность по фазам HTTP), whitebox — для диагностики и прогнозирования. Часто blackbox-пробу ставят на внешнюю точку, а whitebox — внутри сети, и разница между ними сама по себе является метрикой.

**Что хочет услышать интервьюер:** понимание, что blackbox не заменяет внутренние метрики, а дополняет их; что «проба как пользователь» — единственный способ поймать DNS/TLS/маршрутные проблемы.

**Красный флаг:** «blackbox лучше, потому что проверяет по-настоящему» или «это то же самое, только другой экспортер».

**Вопрос 2. Объясни, как один blackbox_exporter проверяет сто целей. Что делает Prometheus?**

**Ответ.** Экспортер пассивный: он не ходит по расписанию, он отвечает на `GET /probe?target=...&module=...`. Prometheus в `scrape_configs` для каждой цели делает отдельный scrape. Так как настоящий адрес — это экспортер, применяем relabeling: `__address__` (цель) перекладываем в `__param_target` (Prometheus подставит его query-параметром), `__param_target` дублируем в `instance`, а в `__address__` пишем адрес экспортера. Модуль задаётся через `params: module: [...]` и тоже может стать меткой через `__param_module`. Итог: N целей, один процесс, у каждой цели своя серия с понятной меткой `instance`.

**Что хочет услышать интервьюер:** что кандидат сам писал эти relabel_configs и понимает роль служебных меток `__address__`, `__param_*`.

**Красный флаг:** «поднимаем отдельный экспортер на каждую цель» или неспособность объяснить, откуда в URL берётся `target`.

**Вопрос 3. `probe_success == 1`, а пользователи жалуются. Назови минимум три причины и что делать.**

**Ответ.** Причины: (1) проба ходит по IP или внутреннему адресу в обход DNS/балансировщика, а пользователи — по имени; (2) проверяется только код ответа, а сервис отдаёт страницу-заглушку или ошибку с кодом 200; (3) проба идёт из той же сети/хоста, что и сервис, и не видит проблему внешнего доступа; (4) проба не ходит по критичному пути (не логинится, не дёргает API); (5) проба по IPv4, а у пользователей ломается IPv6; (6) редирект на страницу логина считается успехом. Что делать: привести пробу к пользовательскому пути (имя, тот же вход), проверять тело ответа регуляркой, добавить пробы на функциональные endpoint'ы с авторизацией под сервисным аккаунтом, поставить вторую точку проверки снаружи, добавить браузерную синтетику для критичных сценариев.

**Что хочет услышать интервьюер:** что мониторинг — это модель пользовательского опыта, и её надо периодически сверять с реальностью (постмортемы, «зелёный мониторинг при инциденте» — отдельный разбор).

**Красный флаг:** «такого не бывает» или «надо просто увеличить частоту проб».

**Вопрос 4. Как построить SLO-дашборд на основе blackbox и что такое burn rate?**

**Ответ.** SLI = доля успешных проб за окно, `avg_over_time(probe_success[30d])` (для 0/1 это и есть доля). Задаём SLO, например 99.5% за 30 дней; error budget = 0.5% времени, ~3.6 часа в месяц. Burn rate = фактическая скорость расхода бюджета; считается как `(1 - SLI_окно) / (1 - SLO)`. При 1.0 бюджет истратится ровно к концу окна; при 14.4 — за час сожжём весь месячный бюджет. На дашборде: текущий uptime за 30 дней, остаток бюджета, burn rate за 5m/1h/6h тремя панелями, график `probe_success` по целям и таблица худших целей. Алерты строим multi-window: быстрый (1h > 14.4) — page, медленный (6h > 6) — тикет. Долгие окна требуют либо precomputed recording rules, либо long-term storage (Thanos/Mimir/VictoriaMetrics).

**Что хочет услышать интервьюер:** связка SLI → SLO → budget → burn rate, и понимание, почему алертят на burn rate, а не на каждый единичный сбой.

**Красный флаг:** «SLO = 100%, чтобы точно не пропустить»; путаница между SLA и SLO; алерт на каждый `probe_success == 0`.

**Вопрос 5. Какие проверки ты поставишь новому сервису в DMZ и как будешь защищать сам экспортер?**

**Ответ.** Три слоя на сервис: `icmp` (жив ли хост, есть ли маршрут через GW01), `tcp_connect` на порт (открыт ли L4, работает ли firewall), `http_2xx` по внешнему имени с проверкой тела ответа и обязательным TLS, плюс метрика `probe_ssl_earliest_cert_expiry` с алертами на 21 и 7 дней. Для DNS — отдельная проба модулем `dns` на внутренний резолвер. По защите: экспортер слушает только внутренний интерфейс, порт 9115 закрыт firewall'ом для всех, кроме Prometheus; работает от непривилегированного пользователя с `CAP_NET_RAW`; `debug` выключен в бою; секреты для проб (basic auth, токены) — в файле с правами 0600 или в secret-хранилище, а не в query-строке; сам экспортер тоже мониторится (`up`, build_info, счётчик запросов).

**Что хочет услышать интервьюер:** системность (L3/L4/L7 + TLS + DNS), внимание к безопасности экспортера как SSRF-инструмента.

**Красный флаг:** «поставим `curl` в cron и будем писать в лог»; открытый наружу 9115 без ограничений.

## 8. Источники (официальная документация)

- **prometheus.io** — раздел «Exporters» и «Configuration» (официальные доки Prometheus: `relabel_configs`, `params`, `scrape_configs`). Поиск: `prometheus configuration relabel_configs`.
- **github.com/prometheus/blackbox_exporter** — README и `CONFIGURATION.md` (модули, prober'ы, multi-target pattern, параметры `http`, `icmp`, `dns`, `tcp`, `grpc`). Поиск: `blackbox exporter configuration modules`.
- **prometheus.io** — «PromQL» / «Querying basics» (`avg_over_time`, `histogram_quantile`, range-векторы). Поиск: `promql avg_over_time functions`.
- **sre.google** — «The SRE Book», глава «Service Level Objectives»; «The SRE Workbook», главы «Alerting on SLOs» и «Implementing SLOs» (burn rate, multi-window multi-burn-rate, значения 14.4 / 6). Поиск: `sre workbook alerting on slos burn rate`.
- **grafana.com/docs** — «Provisioning Grafana», «Dashboards» (дашборды как код, панели Stat/Gauge/State timeline). Поиск: `grafana provisioning dashboards`.
- **systemd.io** / **freedesktop.org** — `systemd.exec(5)`, директивы `AmbientCapabilities`, `CapabilityBoundingSet`. Поиск: `systemd.exec AmbientCapabilities`.
- **nginx.org** — «nginx documentation», раздел про `location` и `return` (пригодится, когда будешь ломать WEB01 в лабе). Поиск: `nginx location directive return`.
- **RFC 8615** (well-known URIs) — если будешь выносить `/.well-known/health` [ПРОВЕРИТЬ релевантность для твоей задачи].
