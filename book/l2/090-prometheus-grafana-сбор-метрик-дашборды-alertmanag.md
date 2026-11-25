# Prometheus + Grafana: сбор метрик, дашборды, alertmanager

## 1. Введение (зачем это нужно в реальной работе)

```
Тикет #4127
Пользователь: adm.student (дежурный администратор)
ВМ: MON01 / FS01
Что написал: «Открываю Grafana — на панели FS01 графики за последний час
пустые. И никто не написал, что диск на FS01 забит. Разберись, у нас
бухгалтерия уже жалуется, что не может сохранять файлы.»
Срочность: Высокая
```

В L1/Zabbix ты смотришь на светофор: красный/зелёный. Prometheus — это другой подход: сервер сам ходит к экспортёрам и забирает числа (*pull*-модель), складывает их в свою базу временных рядов и отдаёт язык запросов PromQL. Grafana рисует по этим числам дашборды, Alertmanager решает, кому, когда и как часто звонить про проблему. В Москве Prometheus+Grafana — стандарт в компаниях, где есть контейнеры, Kubernetes, облака и микросервисы; Zabbix чаще остаётся на «классике». На собеседовании на L2 почти гарантированно спросят «чем pull отличается от push» и «что такое экспортёр». После главы ты сможешь: развернуть Prometheus и Alertmanager на MON01, подключить Linux- и Windows-цели, написать осмысленный PromQL-запрос, собрать дашборд и настроить алерт, который реально доходит до человека.

## 2. Теория «под капотом»

Prometheus — это один бинарник-сервер и набор «экспортёров» (exporter) — маленьких агентов, которые отдают метрики по HTTP в текстовом формате. Ключевое отличие от Zabbix: агент ничего не отправляет сам, сервер раз в N секунд приходит и забирает (`scrape`). Поэтому в конфиге всегда видно, кого и как часто опрашивают, а «пропавшая» цель сразу видна как `up == 0`.

```
        scrape (HTTP GET /metrics) каждые 15s
MON01 (10.10.10.40)  ──────────────►  UBU01:9100  node_exporter
  Prometheus :9090   ──────────────►  FS01:9182   windows_exporter
     │ TSDB /var/lib/prometheus      ─► MON01:9100 node_exporter
     │ правила (rules/*.yml)          ◄─ сам себя тоже опрашивает
     ▼
  Alertmanager :9093  ──►  webhook / e-mail / Telegram / Mattermost
     ▲
  Grafana :3000  ── PromQL ──► Prometheus :9090 (как источник данных)
```

Основные понятия:

- **Метрика (metric)** — имя + набор меток (labels) + значение + время. Пример: `node_filesystem_avail_bytes{instance="10.10.10.30:9100",mountpoint="/"}`.
- **Типы метрик**: `counter` (только растёт — число запросов), `gauge` (туда-сюда — температура, свободное место), `histogram` и `summary` (распределение latency).
- **TSDB** — встроенная база временных рядов. Хранение по умолчанию 15 дней (`--storage.tsdb.retention.time`), это настраивается.
- **Экспортёры**: `node_exporter` (Linux, порт 9100), `windows_exporter` (Windows, 9182), `blackbox_exporter` (проверка ICMP/HTTP/TCP извне, 9115), `mysqld_exporter` (9104). Для короткоживущих batch-задач есть Pushgateway — но это исключение, а не норма.
- **PromQL** — язык запросов. `up` — мгновенный вектор, `node_cpu_seconds_total[5m]` — range-вектор, `rate(...[5m])` — функция.
- **Alerting-конвейер**: правило в Prometheus (`alert: ... expr: ... for: ...`) → состояние `pending` → `firing` → отправка в Alertmanager → тот группирует, глушит (silence), ингибирует и рассылает по маршрутам (`route`).

Порты, которые надо помнить (L7 HTTP поверх TCP): 9090 Prometheus, 9093 Alertmanager, 3000 Grafana, 9100/9182 экспортёры. У Alertmanager ещё 9094 — порт кластеризации нескольких реплик; путать его с 9093 — классическая ошибка.

Важно по стенду: на MON01 уже стоит Zabbix из урока 89. Порты не конфликтуют (Zabbix — 80/443/10051), и это нормальная ситуация «два мониторинга живут рядом»: Zabbix исторически смотрят за железом и сетью, Prometheus — за динамикой и приложениями.

## 3. Аналогия из жизни

Представь больницу. Prometheus — это санитар, который по расписанию обходит палаты с термометром и записывает показания в журнал. Экспортёры — сами пациенты, которые умеют только одно: показать градусник. Alertmanager — старшая медсестра на посту: ей приносят журнал с пометкой «у пациента температура 39 третий час подряд», и уже она решает — будить дежурного врача, звонить заведующему или подождать, потому что этот пациент и так под наблюдением (это и есть silence/группировка). Grafana — это витрина с графиками в коридоре, по которой главврач за 5 секунд понимает, что в отделении творится.

Аналогия перестаёт работать в одном месте: санитар не «ходит быстрее» и не «догадывается» — если пациент (экспортёр) умер, санитар просто запишет `up = 0`, а не побежит выяснять причину.

## 4. Практика и команды

**1. Проверить, что экспортёр отдаёт метрики (базовая проверка любой цели).**

```bash
curl -s http://10.10.10.40:9100/metrics | head -20
curl -s http://10.10.10.30:9100/metrics | grep -c '^node_'
```
`/metrics` — обычный HTTP GET, `grep -c '^node_'` считает строки-метрики. Если ответ `Connection refused` — сервис не запущен или слушает только localhost (`--web.listen-address=:9100`). Если соединение висит — почти всегда файрвол.

**2. Проверить конфиг Prometheus до перезапуска.**

```bash
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/*.yml
```
Вывод в норме: `SUCCESS: ... file is valid`. `promtool` лежит в архиве вместе с Prometheus. Приучить себя гонять эту команду до `systemctl restart` — спасёт от «сервис не поднялся после правки YAML».

**3. Перечитать конфиг без рестарта.**

```bash
curl -X POST http://10.10.10.40:9090/-/reload
```
Работает только если Prometheus запущен с `--web.enable-lifecycle`. Ответ пустой = ок; `405`/`403` = флаг не включён. В боевой среде reload безопаснее рестарта: не теряются скрейпы.

**4. Посмотреть, кто «упал», через API.**

```bash
curl -s http://10.10.10.40:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, instance: .scrapeUrl, health: .health, err: .lastError}'
```
`.health` = `up`/`down`, `lastError` покажет `connection refused` или `context deadline exceeded`. Это первое, что смотрим при «на дашборде пусто».

**5. Мгновенный запрос PromQL через API.**

```bash
curl -s 'http://10.10.10.40:9090/api/v1/query?query=up' | jq '.data.result'
```
Возвращает вектор: `{"metric":{"instance":"10.10.10.20:9182","job":"windows"},"value":[1730000000,"1"]}`. `1` — цель жива, `0` — нет. Удобно для своих скриптов и для проверки перед настройкой алерта.

**6. PromQL, которые нужны в первый день.**

```promql
up == 0                                              -- кто недоступен
100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)  -- % загрузки CPU
node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / 1024^3   -- свободно, ГиБ
rate(node_network_receive_bytes_total{device="ens160"}[5m]) * 8                -- бит/с на интерфейсе
predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[1h], 4*3600) < 0    -- диск кончится за 4 часа
```
`rate()` считает прирост counter'а в секунду за окно; окно `[5m]` должно быть минимум в 4 раза больше интервала скрейпа. `predict_linear` — линейный прогноз: самый полезный запрос для L2, он ловит проблему до того, как она случилась (подробнее про capacity — урок 95).

**7. Журнал сервиса — где живут ошибки.**

```bash
sudo systemctl status prometheus --no-pager
sudo journalctl -u prometheus -n 50 --no-pager
sudo journalctl -u prometheus | grep -i -E 'alert|error|permission'
```
Ищем `level=error`: неверный YAML, нет прав на `/var/lib/prometheus`, не резолвится имя цели.

**8. Alertmanager: проверка конфига и живых алертов.**

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
amtool --alertmanager.url=http://10.10.10.40:9093 alert query
amtool --alertmanager.url=http://10.10.10.40:9093 silence add alertname=InstanceDown --duration=1h --comment="плановые работы на FS01"
```
`amtool` (идёт в архиве Alertmanager) — главный инструмент L2: он проверяет YAML, показывает, что сейчас горит, и ставит/снимает заглушки из консоли, без похода в веб-интерфейс.

**9. Grafana: проверка источника данных и дашбордов по API.**

```bash
TOKEN=<service-account-token>
curl -s -H "Authorization: Bearer $TOKEN" http://10.10.10.40:3000/api/health
curl -s -H "Authorization: Bearer $TOKEN" http://10.10.10.40:3000/api/datasources | jq '.[] | {name, type, url}'
```
Токен создаётся в UI: *Administration → Users and access → Service accounts*. Если `url` источника `http://localhost:9090`, а Grafana стоит не на MON01 — все панели будут пустыми, хотя Prometheus жив.

**10. Windows-сторона (FS01, PowerShell от админа).**

```powershell
Get-Service windows_exporter
Get-NetTCPConnection -LocalPort 9182 -State Listen
Invoke-WebRequest http://localhost:9182/metrics -UseBasicParsing | Select-Object -ExpandProperty Content | Select-String 'windows_cs_physical_memory_bytes'
New-NetFirewallRule -DisplayName "windows_exporter 9182" -Direction Inbound -Protocol TCP -LocalPort 9182 -Action Allow
```
MSI-установщик обычно сам создаёт правило файрвола `[ПРОВЕРИТЬ на своей версии]` — если снаружи не отвечает, а локально отвечает, дело именно в правиле.

**11. Сброс пароля админа Grafana (GUI, если пароль потерян).**

```bash
sudo grafana-cli admin reset-admin-password 'Lab#2026-vektor'
```
Утилита `grafana-cli` может называться `grafana cli` в новых пакетах `[ПРОВЕРИТЬ под свою версию]`. В боевой среде сброс делают через SSO/LDAP, а не локального админа.

## 5. Лабораторная работа

### Предусловия

- Стенд L2 собран: DC01 (10.10.10.10), FS01 (10.10.10.20), UBU01 (10.10.10.30), MON01 (10.10.10.40, Ubuntu Server 24.04, Zabbix из урока 89).
- Учётка `adm.student` с правами локального админа на FS01, sudo на MON01 и UBU01.
- SSH с MON01 на UBU01 работает по ключу (настраивали в Linux-темах).
- Интернет на MON01 для скачивания архивов `prometheus.io/download` и Grafana. Из РФ доступ к GitHub/grafana.com может быть нестабилен — если не качается, скачай архивы на хост и перенеси в ВМ через общую папку VMware; легальная альтернатива — пакеты из репозитория Ubuntu (`prometheus`, `prometheus-node-exporter`) `[ПРОВЕРИТЬ актуальность версии в 24.04]`.

### Снапшот

В VMware Workstation: правый клик по вкладке ВМ → **Snapshot → Take Snapshot**, имя `before-l2-090`. Сделать на **MON01, FS01, UBU01**.

### Шаги

1. **MON01.** Создать пользователя и каталоги:
   ```bash
   sudo useradd --no-create-home --shell /bin/false prometheus
   sudo mkdir -p /etc/prometheus/rules /var/lib/prometheus /etc/alertmanager
   ```
   Ожидаемо: команды без вывода, `prometheus` появился в `/etc/passwd`.
2. **MON01.** Скачать и распаковать Prometheus (подставь актуальную версию с prometheus.io/download, ветка 3.x `[ПРОВЕРИТЬ номер]`):
   ```bash
   cd /tmp && curl -LO https://github.com/prometheus/prometheus/releases/download/v<X.Y.Z>/prometheus-<X.Y.Z>.linux-amd64.tar.gz
   tar xzf prometheus-*.tar.gz && cd prometheus-*.linux-amd64
   sudo install -m 0755 prometheus promtool /usr/local/bin/
   ```
   Ожидаемо: `prometheus --version` печатает версию и `go1.x`.
3. **MON01.** Написать `/etc/prometheus/prometheus.yml`:
   ```yaml
   global:
     scrape_interval: 15s
     evaluation_interval: 15s
     external_labels:
       stand: vektor-lab
   alerting:
     alertmanagers:
       - static_configs:
           - targets: ['10.10.10.40:9093']
   rule_files:
     - /etc/prometheus/rules/*.yml
   scrape_configs:
     - job_name: prometheus
       static_configs: [{targets: ['localhost:9090']}]
     - job_name: node
       static_configs:
         - targets: ['10.10.10.40:9100']
           labels: {instance_name: MON01}
         - targets: ['10.10.10.30:9100']
           labels: {instance_name: UBU01}
     - job_name: windows
       static_configs:
         - targets: ['10.10.10.20:9182']
           labels: {instance_name: FS01}
   ```
   Ожидаемо: `promtool check config /etc/prometheus/prometheus.yml` → `SUCCESS`.
4. **MON01.** Создать unit `/etc/systemd/system/prometheus.service` с `ExecStart=/usr/local/bin/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/var/lib/prometheus --web.enable-lifecycle`, затем:
   ```bash
   sudo chown -R prometheus:prometheus /var/lib/prometheus /etc/prometheus
   sudo systemctl daemon-reload && sudo systemctl enable --now prometheus
   ```
   Ожидаемо: `systemctl status` = `active (running)`, `curl -s localhost:9090/-/healthy` → `Prometheus Server is Healthy.`
5. **MON01.** Развернуть `node_exporter` (бинарник из архива) в `/usr/local/bin`, свой unit `node_exporter.service` на порт 9100, запустить. Ожидаемо: `curl -s localhost:9100/metrics | wc -l` → несколько сотен строк.
6. **UBU01.** Скопировать бинарник и unit с MON01:
   ```bash
   scp /usr/local/bin/node_exporter adm.student@10.10.10.30:/tmp/
   ssh adm.student@10.10.10.30 'sudo install -m0755 /tmp/node_exporter /usr/local/bin/ && sudo systemctl enable --now node_exporter && curl -s localhost:9100/metrics | head -3'
   ```
   Ожидаемо: на UBU01 виден вывод `# HELP node_...`.
7. **FS01.** Установить `windows_exporter` (MSI с github.com/prometheus-community/windows_exporter/releases), проверить службу, порт 9182 и правило файрвола (команды из раздела 4, пункт 10). Ожидаемо: с MON01 `curl -s http://10.10.10.20:9182/metrics | head -3` отдаёт метрики.
8. **MON01.** Открыть в браузере с хостовой машины `http://10.10.10.40:9090/targets`. Ожидаемо: три джобы, пять целей, все `UP`, `Last Scrape` — секунды назад.
9. **MON01.** Развернуть Alertmanager (архив с prometheus.io/download), конфиг `/etc/alertmanager/alertmanager.yml` с `route` и `receivers` (webhook на `http://10.10.10.40:5001/alert`), unit на `--config.file=/etc/alertmanager/alertmanager.yml`. Проверить: `amtool check-config` → `SUCCESS`. Ожидаемо: `curl -s localhost:9093/-/healthy` → `OK`.
10. **MON01.** Написать правило `/etc/prometheus/rules/stand.yml`:
    ```yaml
    groups:
      - name: stand-basic
        rules:
          - alert: InstanceDown
            expr: up == 0
            for: 2m
            labels: {severity: critical}
            annotations:
              summary: "{{ $labels.instance_name }} недоступен"
          - alert: DiskWillFillIn4Hours
            expr: predict_linear(node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}[1h], 4*3600) < 0
            for: 10m
            labels: {severity: warning}
            annotations:
              summary: "Диск на {{ $labels.instance_name }} кончится за 4 часа"
    ```
    Проверить `promtool check rules`, затем `curl -X POST localhost:9090/-/reload`. Ожидаемо: в UI `localhost:9090/rules` появились два правила.
11. **MON01.** Поставить Grafana (`apt` репозиторий apt.grafana.com `[ПРОВЕРИТЬ инструкцию под свою версию]`), войти на `http://10.10.10.40:3000` (admin/admin, пароль сменить на `Lab#2026-vektor`), добавить источник данных Prometheus с URL `http://localhost:9090`, нажать **Save & test** → «Data source is working».
12. **MON01 (Grafana).** Импортировать дашборд «Node Exporter Full» (ID 1860 с grafana.com/grafana/dashboards) или собрать вручную панель с запросом `100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)`. Ожидаемо: два графика — MON01 и UBU01.

### Проверка

- [ ] `promtool check config` и `promtool check rules` — `SUCCESS`.
- [ ] На `http://10.10.10.40:9090/targets` все 5 целей `UP`, `lastError` пустой.
- [ ] Запрос `up` в UI отдаёт пять рядов со значением `1`.
- [ ] В Grafana источник данных Prometheus проходит **Save & test**.
- [ ] На дашборде видны графики CPU/RAM/диска минимум для MON01 и UBU01.
- [ ] `amtool alert query` отрабатывает без ошибки соединения.
- [ ] В `stand-log.md` записано: MON01 получил Prometheus/Grafana/Alertmanager, UBU01 и FS01 — экспортёры.

### Сломай и почини

```
Пользователь / ВМ: adm.student, дежурный / MON01, FS01
Что написал: «Панель FS01 в Grafana пустая уже час. И ни одного алерта
не пришло, хотя я специально остановил службу. Проверь, почему молчит.»
Срочность: Высокая
```

**Как воспроизвести поломку (учебная, делаем осознанно):**
1. На MON01 в `/etc/prometheus/prometheus.yml` поменять адрес Alertmanager на `10.10.10.40:9094` (порт кластеризации, там никто не слушает) и выполнить `curl -X POST localhost:9090/-/reload`.
2. На FS01: `Stop-Service windows_exporter`.
3. Подождать 3–4 минуты (`for: 2m` плюс пара скрейпов).

**Диагностика по порядку:**
1. `http://10.10.10.40:9090/targets` — цель `10.10.10.20:9182` в `DOWN`. Grafana пустая именно поэтому, это следствие.
2. На FS01 `Get-Service windows_exporter` — `Stopped`. Причина «нет данных», но не причина «нет алерта».
3. `http://10.10.10.40:9090/alerts` — алерт `InstanceDown` в состоянии **FIRING**. Значит правила работают, проблема на стыке Prometheus → Alertmanager.
4. `http://10.10.10.40:9093` — пусто. Подтверждает: до Alertmanager ничего не дошло.
5. `sudo journalctl -u prometheus -n 30 | grep -i alert` — `level=warn ... error="Post \"http://10.10.10.40:9094/api/v2/alerts\": dial tcp ... connection refused"`. Вот и корень.
6. `curl -s http://10.10.10.40:9093/-/healthy` → `OK` (сам Alertmanager жив), `ss -lntp | grep 909` показывает, кто реально слушает порты.

**Решение:** вернуть в `alerting.alertmanagers` адрес `10.10.10.40:9093`, `promtool check config`, `curl -X POST localhost:9090/-/reload`, на FS01 `Start-Service windows_exporter`. Через 2–3 минуты цель снова `UP`, а алерт уходит в Alertmanager со статусом `resolved` (`send_resolved: true`).

**Ответ пользователю:** «Панель FS01 была пустой, потому что на FS01 остановлена служба windows_exporter — я её запустил, метрики пошли. Алерты не приходили по другой причине: в конфиге Prometheus был указан неверный порт Alertmanager (9094 вместо 9093), поэтому уведомления не уходили. Исправил, проверил тестовым отключением службы — уведомление пришло. Чтобы не повторилось, добавил проверку конфига в чек-лист изменений.»

**Закрытие заявки:** категория «Мониторинг», приоритет «Высокий» (влияние на бухгалтерию), в решении — причина + что сделано + как проверено. Если это повторяется, оформляем как problem-тикет: «изменения конфига Prometheus без `promtool check`».

### Что изменилось на стенде

- MON01 (10.10.10.40): + `prometheus` (9090), `alertmanager` (9093), `node_exporter` (9100), `grafana-server` (3000); каталоги `/etc/prometheus`, `/etc/prometheus/rules`, `/var/lib/prometheus`, `/etc/alertmanager`; системный пользователь `prometheus`.
- UBU01 (10.10.10.30): + `node_exporter` (9100), unit `node_exporter.service`.
- FS01 (10.10.10.20): + `windows_exporter` (9182), правило файрвола, служба `windows_exporter`.
- Grafana: источник данных `Prometheus (10.10.10.40:9090)`, дашборд Node Exporter Full.
- Правила алертов: `InstanceDown` (critical, 2m), `DiskWillFillIn4Hours` (warning, 10m); webhook-приёмник на `10.10.10.40:5001`.

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-l2-090` → **Restore**. Откатывать MON01, FS01 и UBU01 вместе, иначе на UBU01 останется экспортёр, которого Prometheus не увидит, а на FS01 — служба без мониторинга. После отката обновить `stand-log.md` строкой «откат к before-l2-090».

## 6. Ошибки новичков и безопасность

1. **Скрейп-интервал меньше, чем окно `rate()`.** `rate(x[1m])` при `scrape_interval: 15s` даёт рваные графики → всегда бери окно минимум в 4 интервала.
2. **Метки-«убийцы кардинальности».** Добавить в `labels` что-то вроде `user_id` или `request_id` → TSDB раздувается, Prometheus падает по памяти. Метки — только для измерений с малым числом значений.
3. **Алерт на «сырое» условие без `for`.** `expr: node_load1 > 4` без `for` стреляет от любого всплеска → дежурный перестаёт читать уведомления. Всегда ставь `for: 2m`–`15m` в зависимости от критичности.
4. **Правка YAML без `promtool check config`.** Одна лишняя табуляция — сервис не стартует после рестарта, и в момент инцидента мониторинга нет. Проверяй до перезапуска.
5. **Grafana с источником `localhost` при установке не на том хосте.** Классика: панели пустые, а Prometheus жив. Проверяй `api/datasources`.
6. **Игнор `resolved`-уведомлений.** Без `send_resolved: true` дежурный получает «упало» и никогда «поднялось» — люди перестают реагировать.
7. **Один канал уведомлений на всё.** Критичное и warning в один чат → шум. Разделяй через `route`/`matchers`.
8. **Проект «мониторинг ради мониторинга».** 300 графиков, которыми никто не пользуется, и ни одного SLO. Отталкивайся от урока 88: сначала SLI, потом панели.

**Безопасность.** У Prometheus, Alertmanager и большинства экспортёров **нет аутентификации** — кто достучался до 9090, тот видит всю вашу инфраструктуру и может дёрнуть `/-/reload`. На стенде это допустимо (изолированные VMnet), но по-хорошему Prometheus слушает внутренний интерфейс, доступ закрыт файрволом, а снаружи — только через reverse-proxy с авторизацией. Grafana по умолчанию `admin/admin` — менять пароль сразу, ставить `[auth]`/SSO, не выставлять 3000 в интернет. Webhook-приёмник без проверки источника — открытая дверь, в реальной среде нужен секрет в URL или mTLS. И не храни в `external_labels` имена хостов с внутренними IP, если метрики уходят во внешний сервис.

## 7. Вопросы на собеседовании

**1. Чем pull-модель Prometheus отличается от push-модели Zabbix и когда что лучше?**
Ответ: в Prometheus сервер сам опрашивает цели по HTTP `/metrics` с заданным интервалом; агент пассивный. Плюсы: видно, кого опрашиваешь (список целей = конфиг), легко ловить «цель пропала» через `up == 0`, нет проблемы «агент молча умер». В Zabbix агент/активный чек отправляет данные — удобно для хостов за NAT и для мгновенных событий. Для batch-задач в Prometheus используют Pushgateway, но это костыль, потому что pushgateway навсегда сохраняет последнее значение «умершей» задачи.
*Что хочет услышать интервьюер:* понимание компромисса и слова `up`/`exporter`/`pushgateway`, а не заученное «Prometheus лучше».
*Красный флаг:* «Prometheus работает по SNMP» или «агент отправляет метрики на сервер каждые 15 секунд».

**2. Что такое экспортёр и как ты подключишь новый сервер к мониторингу?**
Ответ: экспортёр — процесс, который выставляет метрики в текстовом формате Prometheus на HTTP-эндпоинте. Для Linux — `node_exporter` (9100), для Windows — `windows_exporter` (9182), для БД/приложений — свои. Подключение: скачать бинарник/MSI, оформить systemd-юнит или службу, открыть порт только для IP Prometheus, добавить цель в `scrape_configs` с лейблом `instance_name`, `promtool check config`, `/-/reload`, проверить на `/targets` статус `UP` и что метрики появились в `/graph`.
*Что хочет услышать:* порядок действий и мысль «сначала проверить `/metrics` руками через curl, потом лезть в конфиг».
*Красный флаг:* «просто добавлю в конфиг» без проверки, что эндпоинт вообще отвечает.

**3. Что делает Alertmanager, если правила алертов живут в Prometheus?**
Ответ: Prometheus вычисляет правила и отправляет `firing`/`resolved` в Alertmanager. Alertmanager занимается доставкой: группировка (`group_by`), задержки (`group_wait`, `group_interval`, `repeat_interval`), маршруты (`route`), подавление (`inhibit_rules`), ручные заглушки (`silences`). Это разделение сознательное: правила — про логику, Alertmanager — про людей и дежурства.
*Что хочет услышать:* что дедупликация и «не спамить» — работа Alertmanager, и что silence ≠ отключение правила.
*Красный флаг:* «Alertmanager сам считает метрики» или незнание про `repeat_interval` — из-за него алерты либо спамят, либо теряются.

**4. Пользователь говорит «дашборд пустой». Твои действия по шагам?**
Ответ: 1) `/targets` в Prometheus — цель `UP`? 2) если `DOWN` — `curl` до `/metrics` с сервера Prometheus, смотреть службу и файрвол; 3) если цель `UP` — проверить сам PromQL в `/graph`: возможно, неверные метки или окно; 4) в Grafana — время дашборда (стоит «last 5 minutes» при простое), источник данных (**Save & test**), переменные шаблона; 5) если данные есть в Prometheus, но не в Grafana — смотреть `grafana.log` и права сервисной учётки.
*Что хочет услышать:* дисциплину «от источника к отображению», а не «перезагружу Grafana».
*Красный флаг:* начинать с рестарта Grafana.

**5. Как настроить алерт на заполнение диска так, чтобы он срабатывал заранее?**
Ответ: не по «занято %», а по прогнозу: `predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[1h], 4*3600) < 0` с `for: 10m` — «при текущем темпе места хватит меньше чем на 4 часа». Дополнительно — статический порог 90% как страховка. Важно исключить `tmpfs`/`overlay` через `fstype!~`, иначе будут ложные срабатывания на виртуальных ФС. Уведомление — warning в рабочий чат, а critical — дежурному.
*Что хочет услышать:* понимание `predict_linear` и разницы между «уже плохо» и «станет плохо».
*Красный флаг:* «поставлю порог 99%» — алерт сработает, когда действовать уже поздно.

## 8. Источники

- **prometheus.io** — раздел *Documentation*: «Getting started», «Configuration», «Alerting rules», «Querying basics»; поиск: `prometheus configuration scrape_configs`.
- **prometheus.io** — раздел *Docs → Instrumenting → Exporters and integrations*; поиск: `prometheus exporters list node_exporter`.
- **prometheus.io** — *Alerting → Alertmanager* и *Configuration* (маршруты, `inhibit_rules`, silences); поиск: `alertmanager configuration route receivers`.
- **github.com/prometheus** — репозитории `prometheus`, `alertmanager`, `node_exporter`, `pushgateway`; раздел *Releases* для скачивания бинарников; поиск: `prometheus github releases linux-amd64`.
- **github.com/prometheus-community** — репозиторий `windows_exporter`, раздел *README / Releases* (флаги коллекторов, MSI); поиск: `windows_exporter collectors flags`.
- **grafana.com/docs** — *Grafana OSS → Installation → Ubuntu/Debian* и *Data sources → Prometheus*; поиск: `grafana install ubuntu apt repository`.
- **grafana.com/grafana/dashboards** — дашборд «Node Exporter Full», ID 1860; поиск: `node exporter full dashboard grafana`.
- **prometheus.io/docs/prometheus/latest/querying/functions** — `rate`, `predict_linear`, `histogram_quantile`; поиск: `promql predict_linear function`.
- **ubuntu.com/server/docs** — установка серверных пакетов, systemd-юниты; поиск: `ubuntu server systemd service unit`.
- Точные номера версий Prometheus/Grafana/Alertmanager на 2026 год и совместимость пакетов из репозитория Ubuntu 24.04 — `[ПРОВЕРИТЬ]` на страницах загрузки перед началом лабы.
