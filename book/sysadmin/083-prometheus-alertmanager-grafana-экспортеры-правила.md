# Prometheus, Alertmanager, Grafana: экспортеры, правила, дашборды

## 1. Введение (зачем это нужно в реальной работе)

Тикет в общем канале `#alerts`:

> **Иван Иванов (`ivanov`), бухгалтерия, ВМ WIN11-CL01:**
> «Мне в канал за час прилетело 40 сообщений про FS01. У меня уже телефон вибрирует от этого канала. Это так и должно работать?»
> **Срочность:** средняя. **Категория:** мониторинг.

Проблема не в том, что FS01 сломался — он, скорее всего, жив. Проблема в том, что алертинг настроен так, что каждый повтор одного и того же события превращается в новое сообщение, а группа людей получает шум вместо сигнала. В Zabbix (урок 82) вы управляли этим триггерами и действиями; в Prometheus-мире за это отвечает Alertmanager, а за «нарисуй мне нормальный график» — Grafana.

После этой главы вы должны уметь: поднять Prometheus, Alertmanager и Grafana на MON01; поставить `node_exporter` на Linux-хосты и `windows_exporter` на Windows-серверы; написать `prometheus.yml` с несколькими job'ами и метками; написать правила алертов с `for:`; настроить маршрутизацию, группировку и подавление в Alertmanager; подключить Prometheus к Grafana как источник данных и собрать панель. И главное — понимать, почему «просто поставить Prometheus» — это 20% работы.

## 2. Теория «под капотом»

Prometheus — это **pull-система**. Он сам раз в N секунд ходит по HTTP на адрес `/metrics` каждого таргета и забирает текстовый дамп метрик. Таргет ничего не «отправляет» — он просто отдаёт текущее состояние. Из этого следуют три важные вещи: Prometheus должен иметь сетевой доступ к таргету; таргет должен быть в списке (static config или service discovery); и «нет данных» само по себе является событием (`up == 0`).

**Экспортер** — это небольшая программа-переходник, которая читает системные счётчики и публикует их в формате Prometheus. Ядро Linux экспортирует себя через `node_exporter`, Windows — через `windows_exporter`, чёрный ящик (HTTP-пробы снаружи) — через `blackbox_exporter`, базы и приложения — через свои экспортеры.

**Метрика** в Prometheus — это имя + набор меток (labels) + значение + время. Пример: `node_filesystem_avail_bytes{instance="10.10.10.30:9100", mountpoint="/", device="/dev/sda2"} 41234567168`. Имя и набор меток вместе образуют временной ряд (time series). Всё хранится в локальной TSDB (time series database) на диске; по умолчанию retention 15 дней.

**Правила** (recording rules и alerting rules) Prometheus вычисляет сам, раз в `evaluation_interval`. Когда условие выполнено дольше, чем указано в `for:`, рождается алерт и уходит POST'ом в Alertmanager.

**Alertmanager** — отдельный сервис. Он получает алерты от Prometheus, дедуплицирует их по fingerprint, группирует, глушит (silence), подавляет (inhibit) и рассылает в получателей (receivers): e-mail, webhook, Telegram, PagerDuty.

**Grafana** — только визуализация. Она не хранит метрики, она делает запросы PromQL к Prometheus и рисует. Дашборды и источники данных можно (и нужно) описывать конфигами — provisioning, чтобы не кликать руками.

Порты, которые надо знать:

| Сервис | Порт | Протокол |
|---|---|---|
| Prometheus (HTTP + API) | 9090/TCP | HTTP |
| Alertmanager (UI/API) | 9093/TCP | HTTP |
| Alertmanager (gossip-кластер) | 9094/TCP+UDP | — |
| Grafana | 3000/TCP | HTTP |
| node_exporter | 9100/TCP | HTTP |
| windows_exporter | 9182/TCP | HTTP |
| blackbox_exporter | 9115/TCP | HTTP |
| pushgateway (если нужен) | 9091/TCP | HTTP |

Всё это — прикладной уровень (L7 модели OSI) поверх TCP.

Схема стенда после этой главы:

```text
[UBU01   10.10.10.30:9100  node_exporter]   ─┐
[ASTRA01 10.10.10.31:9100  node_exporter]   ─┤   HTTP GET /metrics, каждые 30s
[WEB01   10.10.20.10:9100  node_exporter]   ─┼──►  [MON01: Prometheus :9090]
[DC01    10.10.10.10:9182  windows_exporter]─┤           │
[FS01    10.10.10.20:9182  windows_exporter]┘           │ TSDB + evaluate rules
                                                        ▼
                                            [MON01: Alertmanager :9093]
                                              │ route → group → inhibit → silence
                                              ▼
                                    webhook (chat / e-mail / Telegram)

                        [Grafana :3000] ──PromQL──► Prometheus :9090
```

## 3. Аналогия из жизни

Представьте заводской диспетчерский пункт. Обходчики (экспортеры) не звонят сами: диспетчер (Prometheus) по расписанию сам обходит посты и записывает показания в журнал (TSDB). Если показание вышло за норму и держится так уже 10 минут, диспетчер отрывает листок с записью и кладёт его в лоток «Тревоги».

Дальше работает дежурный по тревогам (Alertmanager). Он не бегает к каждому станку сам — он смотрит на лоток и решает: пять листков про один и тот же станок — склеить в один звонок начальнику смены; листок про упавшую сеть — не звонить, потому что уже есть листок «сеть лежит, все станки молчат» (это inhibition); листок, который уже разбирают по наряд-заданию, — подколоть и не звонить (silence). А информационное табло в цеху (Grafana) просто красиво рисует журнал диспетчера — оно ничего не решает и ни на что не влияет.

Аналогия ломается на том, что у живого дежурного есть здравый смысл, а у Alertmanager — только правила маршрутизации: если правило написано криво, он будет добросовестно звонить по любому поводу.

## 4. Практика и команды

**1. Проверка конфига и правил до перезапуска.**

```bash
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/*.yml
```

`promtool` — утилита из поставки Prometheus. Первая команда валидирует основной YAML (и заодно подтягивает пути из `rule_files`), вторая — только файлы правил. Типичный вывод:

```text
Checking /etc/prometheus/prometheus.yml
  SUCCESS: 1 rule files found
SUCCESS: /etc/prometheus/prometheus.yml is valid prometheus config file syntax
```

Смотрим на `SUCCESS` и на количество найденных групп. Синтаксическая ошибка в правиле = Prometheus стартует, но правило молча не работает — поэтому проверять надо **каждый раз**.

**2. Применение конфига без перезапуска.**

```bash
curl -X POST http://10.10.10.40:9090/-/reload
```

Работает только если Prometheus запущен с флагом `--web.enable-lifecycle`. Ответ — пустое тело с кодом 200. Без флага — 405. Это безопаснее, чем `systemctl restart`: TSDB не закрывается/переоткрывается.

**3. Проверка живости и готовности.**

```bash
curl -s http://10.10.10.40:9090/-/healthy
curl -s http://10.10.10.40:9090/-/ready
```

`/-/healthy` отвечает `Prometheus Server is Healthy.` — процесс жив. `/-/ready` отвечает `Prometheus Server is Ready.` — сервер уже перечитал конфиг и готов принимать запросы. Разница важна при автоматизации: healthcheck в балансировщике ставят на `/-/ready`.

**4. Список таргетов и их состояние.**

```bash
curl -s http://10.10.10.40:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job)\t\(.scrapeUrl)\t\(.health)\t\(.lastError)"'
```

`jq` — консольный парсер JSON. В `health` смотрим `up`/`down`, в `lastError` — причину (таймаут, 403, DNS). То же самое видно в веб-интерфейсе: **Status → Targets**.

**5. Что реально отдаёт экспортер.**

```bash
curl -s http://10.10.10.30:9100/metrics | grep -E '^(node_load1|node_boot_time_seconds|node_filesystem_avail_bytes)'
```

Типичный вывод:

```text
node_boot_time_seconds 1.7289e+09
node_filesystem_avail_bytes{device="/dev/sda2",fstype="ext4",mountpoint="/"} 4.1234567168e+10
node_load1 0.08
```

Смотрим: метрика вообще есть, метки на месте, значения похожи на правду. Если `/metrics` не отвечает — проблема в экспортере или файрволе, а не в Prometheus.

**6. Разовый запрос PromQL через API.**

```bash
curl -s -G http://10.10.10.40:9090/api/v1/query \
  --data-urlencode 'query=up == 0' | jq '.data.result'
```

`-G` + `--data-urlencode` — правильный способ передать PromQL с пробелами и кавычками (иначе шелл и URL их съедят). В ответе `data.result` — массив рядов; пустой массив означает «всё в порядке».

**7. То же через promtool.**

```bash
promtool query instant http://10.10.10.40:9090 'up == 0'
```

Удобно в скриптах и CI: возвращает ненулевой код, если запрос не удался. Есть также `promtool query range` и `promtool query series`. [ПРОВЕРИТЬ] набор подкоманд в вашей версии — он расширялся между релизами.

**8. Базовые PromQL-приёмы.**

```promql
up
up{job="node"} == 0
100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
(node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}) * 100
topk(3, node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)
histogram_quantile(0.9, sum by(le) (rate(http_request_duration_seconds_bucket[5m])))
absent(up{job="node"})
```

`rate(...[5m])` — скорость роста счётчика за 5 минут (для счётчиков, которые только растут). `by(instance)` — группировка. `topk(3, ...)` — три худших. `absent()` — «рядов вообще нет», ловит случай, когда таргет удалён из конфига и `up == 0` никогда не сработает.

**9. Управление алертами и silence в Alertmanager.**

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
amtool --alertmanager.url=http://10.10.10.40:9093 alert query
amtool --alertmanager.url=http://10.10.10.40:9093 silence add alertname=DiskSpaceLow \
  --comment="ticket #1234, чистим FS01" --duration=2h
amtool --alertmanager.url=http://10.10.10.40:9093 silence query
```

`amtool` — штатный CLI Alertmanager. `alert query` показывает активные алерты с `fingerprint` (устойчивый хеш набора меток — по нему видно, что 40 сообщений это один и тот же алерт). `silence add` глушит по набору меток на время.

**10. Логи и порты.**

```bash
journalctl -u prometheus -u alertmanager -u grafana-server --since "15 min ago" -p warning
ss -lntp | grep -E ':(9090|9093|9100|9182|3000)\b'
```

Первая — только предупреждения и ошибки за 15 минут. Вторая — кто реально слушает порты (`ss -lntp` = listening, numeric, tcp, process).

**11. Grafana: здоровье и сброс пароля.**

```bash
curl -s http://10.10.10.40:3000/api/health
grafana cli admin reset-admin-password 'Lab#2026-vektor'
```

`/api/health` отдаёт `{"database":"ok","version":"...","commit":"..."}` — база Grafana жива. `grafana cli` работает только при остановленном сервисе и только от root. [ПРОВЕРИТЬ] точное имя бинаря в вашей версии (`grafana-cli` vs `grafana cli`) — в 10.x/11.x переходили между вариантами.

**12. Windows-сторона: проверка экспортера и правило файрвола.**

```powershell
Get-Service windows_exporter
Invoke-WebRequest -UseBasicParsing http://10.10.10.10:9182/metrics | Select-Object -ExpandProperty Content | Select-String '^windows_cs_hostname'
New-NetFirewallRule -DisplayName "windows_exporter from MON01" -Direction Inbound `
  -Protocol TCP -LocalPort 9182 -Action Allow -RemoteAddress 10.10.10.40
```

`-RemoteAddress 10.10.10.40` — открываем порт **только** для MON01, а не для всей сети. В боевой среде так и надо делать всегда.

**13. Blackbox-проба (проверка снаружи, а не изнутри).**

```bash
curl -s "http://10.10.10.40:9115/probe?target=https://10.10.20.10&module=http_2xx" | grep -E '^probe_success'
```

`probe_success 1` — сайт отдаёт 2xx. Blackbox проверяет доступность так, как её видит клиент, а не как её видит сам сервер.

## 5. Лабораторная работа

### Предусловия

- **MON01** (10.10.10.40, Ubuntu Server) — уже существует с L2, на нём крутится Zabbix. Prometheus ставим рядом, Zabbix не трогаем: в реальности так и происходит — постепенная миграция, а не «выключили всё и поставили новое».
- **UBU01** (10.10.10.30), **ASTRA01** (10.10.10.31), **WEB01** (10.10.20.10) — Linux-хосты, доступ по SSH.
- **DC01** (10.10.10.10), **FS01** (10.10.10.20) — Windows Server, доступ по RDP, есть локальный администратор.
- Если MON01 у вас ещё нет — сначала пройдите главу, где он появляется (модуль мониторинга L2), либо поднимите минимальную Ubuntu Server 24.04 с адресом 10.10.10.40 вручную.
- Все команды на Linux — от пользователя с `sudo`. Пароль стенда `Lab#2026-vektor`.

> Замечание про доступность дистрибутивов: бинарники Prometheus/Alertmanager/экспортеров лежат на GitHub Releases, из РФ доступ может быть нестабильным. Легальная альтернатива — зеркала и пакеты из репозиториев вашего дистрибутива (`prometheus`, `prometheus-node-exporter` в apt) либо внутреннее зеркало компании. Версии в ссылках ниже помечены `[ПРОВЕРИТЬ]` — их нужно сверять со страницей релизов на момент установки.

### Снапшот

В VMware Workstation для **MON01, UBU01, WEB01, DC01** (и ASTRA01, если будете её добавлять):

```text
VM → Snapshot → Take Snapshot…
Name: before-sysadmin-083
Description: до установки Prometheus/Alertmanager/Grafana и экспортеров
```

Снимок делать на **выключенных** ВМ (или хотя бы без записи в TSDB) — иначе снапшот займёт много места.

### Шаги

**1. MON01. Готовим пользователей и каталоги.**

```bash
sudo useradd --no-create-home --shell /usr/sbin/nologin prometheus
sudo useradd --no-create-home --shell /usr/sbin/nologin alertmanager
sudo mkdir -p /etc/prometheus/rules /var/lib/prometheus /etc/alertmanager /var/lib/alertmanager
sudo chown -R prometheus:prometheus /etc/prometheus /var/lib/prometheus
sudo chown -R alertmanager:alertmanager /etc/alertmanager /var/lib/alertmanager
```

Ожидаемо: команды проходят без вывода, `id prometheus` показывает системного пользователя без shell.

**2. MON01. Ставим Prometheus и Alertmanager из архивов.**

```bash
cd /tmp
VER_PROM="[ПРОВЕРИТЬ-версия]"      # например 3.x.y с github.com/prometheus/prometheus/releases
VER_AM="[ПРОВЕРИТЬ-версия]"
curl -LO https://github.com/prometheus/prometheus/releases/download/v${VER_PROM}/prometheus-${VER_PROM}.linux-amd64.tar.gz
curl -LO https://github.com/prometheus/alertmanager/releases/download/v${VER_AM}/alertmanager-${VER_AM}.linux-amd64.tar.gz
tar xzf prometheus-${VER_PROM}.linux-amd64.tar.gz
tar xzf alertmanager-${VER_AM}.linux-amd64.tar.gz
sudo install -m 0755 prometheus-${VER_PROM}.linux-amd64/prometheus /usr/local/bin/
sudo install -m 0755 prometheus-${VER_PROM}.linux-amd64/promtool /usr/local/bin/
sudo install -m 0755 alertmanager-${VER_AM}.linux-amd64/alertmanager /usr/local/bin/
sudo install -m 0755 alertmanager-${VER_AM}.linux-amd64/amtool /usr/local/bin/
prometheus --version && alertmanager --version
```

Ожидаемо: обе команды печатают версию и ветку сборки.

**3. MON01. Конфиг Prometheus.**

```yaml
# /etc/prometheus/prometheus.yml
global:
  scrape_interval: 30s
  evaluation_interval: 30s
  external_labels:
    site: vektor-lab

rule_files:
  - /etc/prometheus/rules/*.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: ['10.10.10.40:9093']

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['localhost:9090']

  - job_name: node
    static_configs:
      - targets:
          - '10.10.10.40:9100'
          - '10.10.10.30:9100'
          - '10.10.10.31:9100'
          - '10.10.20.10:9100'
        labels:
          env: prod

  - job_name: windows
    static_configs:
      - targets:
          - '10.10.10.10:9182'
          - '10.10.10.20:9182'
        labels:
          env: prod
```

`external_labels` — метки, которые Prometheus добавляет ко всем данным (нужны при federation/remote write). `rule_files` с glob'ом — все `.yml` из каталога. Ожидаемо: `promtool check config /etc/prometheus/prometheus.yml` → `SUCCESS`.

**4. MON01. systemd-юниты.**

```ini
# /etc/systemd/system/prometheus.service
[Unit]
Description=Prometheus
Wants=network-online.target
After=network-online.target

[Service]
User=prometheus
Group=prometheus
Type=simple
ExecStart=/usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus/ \
  --storage.tsdb.retention.time=30d \
  --web.enable-lifecycle

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/alertmanager.service
[Unit]
Description=Alertmanager
Wants=network-online.target
After=network-online.target

[Service]
User=alertmanager
Group=alertmanager
Type=simple
ExecStart=/usr/local/bin/alertmanager \
  --config.file=/etc/alertmanager/alertmanager.yml \
  --storage.path=/var/lib/alertmanager/

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now prometheus alertmanager
systemctl status prometheus --no-pager -l
```

Ожидаемо: `active (running)`, в логах строка `Server is ready to receive web requests`.

**5. UBU01 и WEB01. Ставим node_exporter.**

```bash
sudo useradd --no-create-home --shell /usr/sbin/nologin node_exporter
VER_NE="[ПРОВЕРИТЬ-версия]"
curl -LO https://github.com/prometheus/node_exporter/releases/download/v${VER_NE}/node_exporter-${VER_NE}.linux-amd64.tar.gz
tar xzf node_exporter-${VER_NE}.linux-amd64.tar.gz
sudo install -m 0755 node_exporter-${VER_NE}.linux-amd64/node_exporter /usr/local/bin/
```

```ini
# /etc/systemd/system/node_exporter.service
[Unit]
Description=Node Exporter
After=network-online.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/usr/local/bin/node_exporter

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now node_exporter
curl -s localhost:9100/metrics | head -5
```

Ожидаемо: текстовый дамп метрик с комментариями `# HELP` / `# TYPE`. То же проделать на ASTRA01 (там может понадобиться `apt install curl` — команды те же).

**6. DC01 и FS01. Ставим windows_exporter.**

```powershell
# на DC01, PowerShell от администратора
msiexec /i "C:\Temp\windows_exporter-[ПРОВЕРИТЬ-версия]-amd64.msi" /qn `
  ENABLED_COLLECTORS="cpu,cs,logical_disk,memory,net,os,service,system" LISTEN_PORT=9182
Get-Service windows_exporter
New-NetFirewallRule -DisplayName "windows_exporter from MON01" -Direction Inbound `
  -Protocol TCP -LocalPort 9182 -Action Allow -RemoteAddress 10.10.10.40
```

Ожидаемо: служба `windows_exporter` в состоянии `Running`, `Invoke-WebRequest http://localhost:9182/metrics` отдаёт метрики с префиксом `windows_`. Повторить на FS01 (там так же открыть порт для MON01). MSI-файл брать с github.com/prometheus-community/windows_exporter/releases.

**7. MON01. Проверяем, что все таргеты поднялись.**

```bash
curl -s http://10.10.10.40:9090/api/v1/targets \
  | jq -r '.data.activeTargets[] | "\(.labels.job)\t\(.scrapeUrl)\t\(.health)"'
```

Ожидаемо: 7 строк, у всех `health: "up"`. Если `windows` таргеты `down` — почти всегда файрвол на Windows или забытый `-RemoteAddress`.

**8. MON01. Правила алертов.**

```yaml
# /etc/prometheus/rules/vektor.yml
groups:
  - name: availability
    rules:
      - alert: InstanceDown
        expr: up == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Недоступен {{ $labels.instance }} (job {{ $labels.job }})"
          description: "Таргет не отвечает более 2 минут."

  - name: resources
    rules:
      - alert: DiskSpaceLow
        expr: |
          (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay",mountpoint="/"}
           / node_filesystem_size_bytes{fstype!~"tmpfs|overlay",mountpoint="/"}) < 0.15
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Мало места на {{ $labels.instance }}"
          description: "Свободно менее 15% на {{ $labels.mountpoint }}."

      - alert: HighCPU
        expr: |
          100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 85
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Высокая загрузка CPU на {{ $labels.instance }}"
```

```bash
promtool check rules /etc/prometheus/rules/vektor.yml
curl -X POST http://10.10.10.40:9090/-/reload
```

Ожидаемо: `SUCCESS: 2 rules found`, затем 200 на reload. Для Windows-хостов при необходимости добавьте отдельное правило на `windows_logical_disk_free_bytes` / `windows_logical_disk_size_bytes` [ПРОВЕРИТЬ имена метрик в вашей версии коллектора `logical_disk`].

**9. MON01. Alertmanager и приёмник-заглушка.**

```yaml
# /etc/alertmanager/alertmanager.yml
global:
  resolve_timeout: 5m

route:
  receiver: webhook
  group_by: ['alertname', 'instance']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h

receivers:
  - name: webhook
    webhook_configs:
      - url: 'http://10.10.10.40:5001/alerts'
        send_resolved: true

inhibit_rules:
  - source_matchers: ['severity="critical"']
    target_matchers: ['severity="warning"']
    equal: ['instance']
```

```bash
cat > /opt/alert-sink.py <<'EOF'
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(n)
        with open('/var/log/alert-sink.log', 'ab') as f:
            f.write(body + b'\n\n')
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
HTTPServer(('0.0.0.0', 5001), H).serve_forever()
EOF
sudo -u alertmanager python3 /opt/alert-sink.py &
sudo -u alertmanager amtool check-config /etc/alertmanager/alertmanager.yml
sudo systemctl restart alertmanager
```

`group_by` — по каким меткам склеивать алерты в одно уведомление. `group_wait` — сколько подождать, чтобы собрать пачку. `group_interval` — минимальный интервал между уведомлениями по одной группе. `repeat_interval` — как часто напоминать про нерешённый алерт. Ожидаемо: `SUCCESS: - route: ...` от `amtool check-config`.

**10. MON01. Grafana.**

```bash
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update && sudo apt-get install -y grafana
sudo systemctl enable --now grafana-server
```

Проверяем: `curl -s http://10.10.10.40:3000/api/health` → `"database":"ok"`. Открываем `http://10.10.10.40:3000`, вход `admin` / `admin`, Grafana потребует сменить пароль — ставим `Lab#2026-vektor` (только стенд!).

**11. MON01. Источник данных через provisioning, а не мышкой.**

```yaml
# /etc/grafana/provisioning/datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://10.10.10.40:9090
    isDefault: true
```

```bash
sudo systemctl restart grafana-server
```

Ожидаемо: в Grafana → **Connections → Data sources** появился Prometheus с зелёной плашкой «Data source is working». Смысл provisioning: конфиг в git, установка воспроизводима, руками ничего не кликаем.

**12. Grafana. Панель и импорт готового дашборда.**

Создать дашборд → **Add visualization** → источник Prometheus → в поле запроса:

```promql
100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

Панель типа Time series, unit — `percent (0-100)`. Затем **Dashboards → Import**, вставить ID готового community-дашборда для node_exporter (классический — 1860 «Node Exporter Full» [ПРОВЕРИТЬ, что дашборд актуален для вашей версии метрик]) и выбрать источник Prometheus. Ожидаемо: 10+ графиков по CPU/памяти/диску/сети для всех ваших Linux-хостов.

**13. Проверка алертинга целиком.**

```bash
# на UBU01
sudo systemctl stop node_exporter
```

Ждём ~3 минуты и смотрим на MON01:

```bash
curl -s http://10.10.10.40:9090/api/v1/alerts | jq '.data.alerts[] | {name: .labels.alertname, state: .state}'
sudo tail -n 20 /var/log/alert-sink.log
```

Ожидаемо: `state: "firing"` для `InstanceDown`, а в `/var/log/alert-sink.log` — JSON-webhook с массивом `alerts`. Затем `sudo systemctl start node_exporter` и через пару минут — сообщение с `status: "resolved"`.

### Проверка

- [ ] На MON01 `systemctl is-active prometheus alertmanager grafana-server` возвращает `active` для всех трёх.
- [ ] `curl -s http://10.10.10.40:9090/api/v1/targets` показывает 7 таргетов, все `up`.
- [ ] `promtool check config` и `promtool check rules` — оба `SUCCESS`.
- [ ] `amtool alert query` отрабатывает без ошибок подключения.
- [ ] В Grafana источник Prometheus зелёный, дашборд node_exporter рисует графики по 4 Linux-хостам.
- [ ] Остановка `node_exporter` на UBU01 даёт `InstanceDown` через ~2 минуты, запись в `/var/log/alert-sink.log` есть.
- [ ] Запуск обратно даёт `resolved` в том же логе.
- [ ] В `stand-log.md` появилась запись про эту лабораторную.

### Сломай и почини

> **Пользователь:** Иван Иванов (`ivanov`), ВМ WIN11-CL01
> **Что написал:** «В общий канал `#alerts` за последний час прилетело 40 сообщений про FS01. Это же один и тот же диск. Отключите меня от канала или почините рассылку.»
> **Срочность:** средняя (шум, не авария)

**Как воспроизвести намеренно.** На MON01 в `/etc/alertmanager/alertmanager.yml` заменить блок `route` на:

```yaml
route:
  receiver: webhook
  group_by: []
  group_wait: 1s
  group_interval: 1m
  repeat_interval: 1m
```

и в правиле `DiskSpaceLow` поставить `for: 0m`, порог `< 0.60`. Перезапустить Alertmanager и перезагрузить Prometheus. На FS01 любым способом занять место на диске (например, скопировать большой файл в `C:\Temp`) — или просто дождаться, пока порог 60% сам сработает на любом хосте.

**Диагностика по порядку.**

1. Что вообще происходит — смотрим активные алерты и их fingerprint:

```bash
amtool --alertmanager.url=http://10.10.10.40:9093 alert query
```

Если у 40 сообщений **один и тот же fingerprint** — это не 40 разных аварий, а один алерт, который повторяется. Это ключевой вывод.

2. Смотрим текущий конфиг маршрутизации прямо у Alertmanager (а не в файле — файл мог не примениться):

```bash
curl -s http://10.10.10.40:9093/api/v2/status | jq '.config.original' | head -40
```

Видим `group_by: []`, `repeat_interval: 1m` — вот источник шума.

3. Проверяем, нет ли наложенного silence, который маскирует картину: `amtool silence query`. Пусто — значит, никто ничего не глушил.

4. Смотрим на само правило: `curl -s localhost:9090/api/v1/rules | jq '.data.groups[].rules[] | {name, duration: .duration}'`. `duration: 0s` при `for: 0m` — алерт срабатывает мгновенно на любом колебании, без «подтверждения» проблемой.

5. Проверяем, действительно ли место на FS01 ниже порога: в Prometheus UI (или через API) запрос `windows_logical_disk_free_bytes` / `windows_logical_disk_size_bytes` [ПРОВЕРИТЬ имена], либо просто `df`-эквивалент на самой ВМ. Убеждаемся, что проблема реальная, но не аварийная.

**Решение.**

1. Вернуть `group_by: ['alertname', 'instance']`, `group_wait: 30s`, `group_interval: 5m`, `repeat_interval: 4h`.
2. В правиле вернуть `for: 10m` и разумный порог (15%). Правило «срабатывает мгновенно» оставляем только для `InstanceDown`, и то с `for: 2m`.
3. Добавить `inhibit_rules`: если по хосту уже есть `critical`, не присылать `warning` (в конфиге выше это уже есть).
4. На время разбора — заглушить: `amtool silence add instance="10.10.10.20:9182" --comment="ticket #1234" --duration=2h`.
5. `promtool check config`, `amtool check-config`, reload, наблюдать 15 минут — в `/var/log/alert-sink.log` должно быть одно сообщение на группу, а не поток.

**Что написать пользователю.** «Спасибо, это наша ошибка в настройке рассылки, а не авария. Один и тот же сигнал по диску FS01 повторялся каждую минуту вместо раза в 4 часа и не группировался. Настроили группировку и интервал повтора, на время разбора сигнал заглушён. Место на FS01 под контролем, чистим планово. Если повторится — пишите сразу в заявку, а не в канал.»

**Как закрыть заявку.** Категория «Мониторинг / алертинг», решение — «настроена группировка и repeat_interval в Alertmanager, добавлен `for:` в правило, наложен silence на время разбора». В `stand-log.md` — запись про изменённый `alertmanager.yml`.

### Что изменилось на стенде

- **MON01 (10.10.10.40):** установлены и включены в автозапуск `prometheus` (порт 9090, TSDB в `/var/lib/prometheus`, retention 30 дней), `alertmanager` (9093), `grafana-server` (3000). Каталоги `/etc/prometheus`, `/etc/prometheus/rules`, `/etc/alertmanager`. Системные пользователи `prometheus`, `alertmanager`. Grafana: источник данных Prometheus через provisioning, дашборд node_exporter, пароль администратора сменён на стендовый.
- **UBU01 (10.10.10.30), ASTRA01 (10.10.10.31), WEB01 (10.10.20.10):** установлен `node_exporter`, systemd-юнит `node_exporter`, порт 9100.
- **DC01 (10.10.10.10), FS01 (10.10.10.20):** установлен `windows_exporter`, порт 9182, входящее правило файрвола только с 10.10.10.40.
- **Заглушка приёмника:** `/opt/alert-sink.py` и лог `/var/log/alert-sink.log` на MON01 — учебный webhook вместо почты/чата.
- **Zabbix на MON01 не тронут** — продолжает работать параллельно.
- Открытые порты 9090/9093/9100/9182/3000 — только внутри 10.10.10.0/24 (в боевой среде закрывать ACL).

### Откат

VMware Workstation → выбрать ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-sysadmin-083` → **Go to**. Повторить для MON01, UBU01, ASTRA01, WEB01, DC01, FS01. Если откат не нужен, но нужно убрать конкретный компонент — `systemctl disable --now prometheus alertmanager grafana-server node_exporter` и удалить юниты из `/etc/systemd/system`, затем `systemctl daemon-reload`. Данные TSDB складывать в архив, а не «rm -rf» — вдруг пригодятся для разбора.

## 6. Ошибки новичков и безопасность

1. **Правило без `for:`.** → Один короткий всплеск CPU рвёт вам чат. → Любое правило, кроме `InstanceDown`, ставить с `for: 5m`–`15m`; `InstanceDown` — минимум `2m`.
2. **`repeat_interval: 1m` «чтобы не пропустить».** → 60 сообщений в час по одной проблеме, люди перестают читать канал, и настоящая авария тонет в шуме. → `repeat_interval` 4 часа для warning, 1 час для critical — и то не всегда.
3. **`group_by: []`.** → Каждый алерт — отдельное уведомление, даже если это 20 хостов в одной стойке. → Всегда `group_by` хотя бы `['alertname', 'instance']`, для инфраструктурных событий — `['alertname', 'cluster']`.
4. **Мониторинг только «изнутри».** → Prometheus стоит в той же сети, что и таргеты; сеть легла — и алертов нет, и мониторинг «всё зелёное». → Blackbox-пробы снаружи, dead man's switch (`absent(up{job="prometheus"})` + внешний watchdog), второй инстанс Prometheus.
5. **Порог «диск < 15%» на всех дисках сразу.** → На 2-ТБ диске 15% — это 300 ГБ, паника на ровном месте. → Считать абсолютный остаток (`node_filesystem_avail_bytes < 10e9`) вместе с процентом, либо разные пороги для разных mountpoint.
6. **Хранить всё вечно в локальной TSDB.** → Через полгода диск MON01 кончился, Prometheus упал. → Retention явно (`--storage.tsdb.retention.time`), долгосрочное хранение — через remote write в VictoriaMetrics/Mimir/Thanos.
7. **Дашборды и алерты настроены мышкой.** → Сервер переставили — всё потеряно. → Всё в git: `prometheus.yml`, `rules/*.yml`, `alertmanager.yml`, provisioning Grafana. Проверка в CI через `promtool`/`amtool`.
8. **`up == 0` как единственный алерт.** → Таргет удалили из конфига — `up` для него больше не существует, алерта нет. → Дополнять `absent()` и следить за количеством таргетов.

### Безопасность

- **`/metrics` — это разведданные.** Там версии ОС, список служб, имена дисков, сетевые интерфейсы, иногда — имена баз и бакетов. Не выставляйте экспортеры в интернет и не открывайте 9100/9182 «на всю сеть»: в лабе мы сделали правило только для MON01, в бою — ACL + отдельный management-VLAN.
- **У Prometheus и Alertmanager по умолчанию нет аутентификации.** Любой, кто достучится до 9090, увидит все метрики, а кто достучится до 9093 — сможет создать silence и **заглушить настоящую аварию**. Закрывайте веб-интерфейсы ACL'ом или reverse-proxy с аутентификацией (`--web.config.file` в Prometheus [ПРОВЕРИТЬ в вашей версии]).
- **Grafana: `admin/admin` — приглашение войти.** Меняем сразу, включаем RBAC, при интеграции с LDAP/AD — не выдаём Editor всем подряд: испорченный дашборд дежурного хуже отсутствующего.
- **Webhook-приёмник без аутентификации.** Наш `/opt/alert-sink.py` слушает 0.0.0.0 — в лабе допустимо, в бою приёмник должен быть за прокси, с токеном в URL или mTLS.
- **Секреты в конфигах.** Токены Telegram, SMTP-пароли, пароли к БД — не в открытом `alertmanager.yml`. Ansible Vault (урок 75) или systemd `EnvironmentFile` с правами 0600.

## 7. Вопросы на собеседовании

**1. Чем Prometheus отличается от Zabbix и когда вы выберете что?**

Ответ: Prometheus — pull-модель, многомерные метки, локальная TSDB, PromQL, нет встроенного «из коробки» набора шаблонов — конфиг и правила пишутся руками (или генерятся). Zabbix — push/pull, агенты с активными проверками, реляционная БД, шаблоны и автообнаружение, встроенный веб-интерфейс и алертинг, исторически сильнее в Windows/SNMP/агентском мониторинге и в «поставил и работает». Выбор: Prometheus — облачно-ориентированная, динамическая, контейнерная среда, где важны кастомные метрики приложений и гибкие запросы; Zabbix — гетерогенная инфраструктура с Windows, SNMP-устройствами, сетевым оборудованием и требованием к UI «для дежурных». На практике часто живут вместе: Zabbix на железо и сеть, Prometheus на Kubernetes и приложения.

*Что хочет услышать интервьюер:* что вы не «религиозны», понимаете модель данных и ограничения обеих систем, умеете считать стоимость эксплуатации.
*Красный флаг:* «Prometheus лучше, потому что современный» без единого аргумента про модель данных и сценарий.

**2. Как устроен алертинг в Prometheus+Alertmanager и зачем нужен отдельный сервис?**

Ответ: Prometheus вычисляет alerting rules по расписанию `evaluation_interval`, при выполнении условия дольше `for:` создаёт алерт и отправляет его в Alertmanager. Alertmanager дедуплицирует по fingerprint, группирует (`group_by`, `group_wait`, `group_interval`), подавляет (`inhibit_rules` — например, не слать warning, если по хосту уже есть critical), глушит по расписанию или вручную (`silences`) и маршрутизирует по дереву `route` в `receivers`. Смысл разделения: Prometheus — stateless-вычислитель, Alertmanager — stateful-«диспетчер» с памятью о том, что уже отправлено, и с HA-кластером через gossip на 9094.

*Что хочет услышать интервьюер:* понимание, что «алерт» и «уведомление» — разные сущности, и что дедупликация/группировка живут в Alertmanager.
*Красный флаг:* «Prometheus сам шлёт письма, Alertmanager не нужен».

**3. Что такое экспортер, и почему нельзя просто поставить node_exporter на Windows?**

Ответ: экспортер — процесс, который читает системные/прикладные счётчики и публикует их по HTTP в текстовом формате Prometheus. `node_exporter` написан под Linux и читает `/proc` и `/sys` — на Windows он не работает в принципе. Для Windows есть `windows_exporter` (форк wmi_exporter), который отдаёт метрики через WMI/Performance Counters с префиксом `windows_` и набором коллекторов (`cpu`, `cs`, `logical_disk`, `memory`, `net`, `service`, `system`), настраиваемых через MSI-параметр `ENABLED_COLLECTORS`. Порт по умолчанию 9182.

*Что хочет услышать интервьюер:* что вы понимаете: экспортер — это адаптер к источнику данных, а не «агент вообще», и что для каждой платформы он свой.
*Красный флаг:* «поставим node_exporter через WSL» или попытка запустить linux-бинарь на Windows.

**4. У вас 40 уведомлений в час по одному хосту. Ваши действия?**

Ответ: сначала понять, это 40 разных алертов или один повторяющийся — смотрю fingerprint в `amtool alert query` / Alertmanager UI. Если один — проблема в `repeat_interval` и `group_by`; правлю маршрут. Если разные, но по одному хосту — добавляю `group_by` по `instance`/`alertname`, настраиваю `inhibit_rules`, чтобы критичные подавляли вторичные. Проверяю, нет ли правила без `for:` — оно даёт дребезг. Проверяю, не флапает ли сам таргет (сеть, DNS, таймауты скрейпа). На время разбора — silence с комментарием и номером тикета, чтобы не потерять сигнал совсем. И обязательно записываю в постмортем, какой шум и почему возник.

*Что хочет услышать интервьюер:* системный порядок диагностики, а не «отключу алерты».
*Красный флаг:* «уберу правило, чтобы не мешало».

**5. Как вы обеспечите отказоустойчивость самого мониторинга?**

Ответ: Prometheus — stateless по конфигу, но stateful по TSDB, поэтому HA строится как два одинаковых инстанса с идентичным конфигом, каждый со своей локальной TSDB (данные не реплицируются), плюс Alertmanager в кластере из 3 узлов через gossip на 9094 — он дедуплицирует уведомления между инстансами. Сверху — балансировщик или отдельный «query»-слой. Долгосрочное хранение и глобальный обзор — remote write в Thanos/Mimir/VictoriaMetrics. Обязательно: внешний watchdog на «сам мониторинг жив» (dead man's switch), мониторинг диска под TSDB, регулярная проверка, что правила и дашборды восстанавливаются из git, и учения по отказу MON01 (урок 89).

*Что хочет услышать интервьюер:* что вы понимаете: Prometheus не реплицирует данные, и «два Prometheus» ≠ «HA из коробки».
*Красный флаг:* «поставлю Prometheus в Docker с restart always — этого достаточно».

## 8. Источники (официальная документация)

- **prometheus.io** — раздел *Documentation*: «Getting started», «Configuration», «PromQL basics», «Alerting rules», «Storage». Поиск: `prometheus configuration scrape_configs relabel_configs`.
- **prometheus.io** — раздел *Alerting / Alertmanager*: «Configuration», «Routing tree», «Silences», «Inhibition». Поиск: `alertmanager route group_by repeat_interval inhibit_rules`.
- **github.com/prometheus** — репозитории `prometheus`, `alertmanager`, `node_exporter`, `blackbox_exporter`: страницы *Releases* (актуальные версии и артефакты) и README с флагами запуска. Поиск: `node_exporter releases linux-amd64`.
- **github.com/prometheus-community/windows_exporter** — README: коллекторы, MSI-параметры, порт по умолчанию. Поиск: `windows_exporter ENABLED_COLLECTORS msi`.
- **grafana.com** — раздел *Docs → Grafana → Data sources → Prometheus* и *Provisioning*: «Provision data sources», «Provision dashboards». Поиск: `grafana provisioning datasources prometheus yaml`.
- **grafana.com/grafana/dashboards** — каталог готовых дашбордов; страница дашборда Node Exporter Full (ID 1860) [ПРОВЕРИТЬ актуальность ID и совместимость с вашей версией метрик].
- **grafana.com** — *Docs → Setup → Install Grafana on Debian or Ubuntu*: актуальные команды добавления apt-репозитория `apt.grafana.com`. Поиск: `grafana install debian apt repository gpg key`.
- **ubuntu.com / man7.org** — `systemd.service(5)`, `systemd.exec(5)` — если нужно уточнить параметры юнитов; `man 8 ss`, `man 1 journalctl`.
- **itil / ГОСТ-контекст** — при описании регламента алертинга и эскалаций опирайтесь на материалы урока 85 (ITIL 4) и урок 88 (эксплуатационная документация).
