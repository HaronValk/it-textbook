# Grafana: дашборды, переменные, алерты, provisioning как код, Grafana Operator

## 1. Введение (зачем это нужно в реальной работе)

Тикет в учебной очереди «Вектор»:

> **Пользователь:** тимлид сопровождения (смотрит графики на MON01)
> **ВМ:** MON01
> **Что написал:** «Нужно глянуть нагрузку на WEB01 — открыл дашборд, а там всё по UBU01. Начал править запрос руками, сохранил как “node-overview-copy”, потом “copy-2”. Сейчас на сервере четыре разных дашборда, какой из них правда, непонятно. И ещё: когда UBU01 лёг ночью, никто не узнал — алерта не было. Сделайте нормально, чтобы не руками и чтобы из Git».
> **Срочность:** средняя (но алерт — уже боль)

Grafana — это то, куда в 90% компаний смотрят люди, когда что-то сломалось: и техподдержка, и разработка, и руководители. Prometheus метрики собирает и хранит, но сам по себе он показывает только текстовые таблицы и простенький graph-режим с ручными PromQL-запросами. Grafana превращает это в панели, по которым за 5 секунд понятно, «горит» или нет.

Проблема начинается на второй неделе: дашборды, собранные мышкой, живут в базе Grafana, у каждого своя копия, при переезде или потере базы всё исчезает. Поэтому в зрелых командах дашборды, источники данных и правила алертов — это файлы в Git, которые применяются автоматически. А если Grafana живёт в Kubernetes — это Custom Resources и Grafana Operator.

После главы ты должен уметь: поставить Grafana, подключить Prometheus как datasource, собрать дашборд с переменными, описать datasource/dashboard/alert файлами provisioning, положить это в GitLab на GIT01 с проверкой в CI, и развернуть Grafana в кластере через оператор.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Grafana — это **Go-бэкенд + SPA-фронтенд** (React в браузере). Своих метрик она не хранит: она хранит только **свою конфигурацию** — дашборды, папки, пользователей, организации, правила алертов — в базе `grafana.db` (SQLite по умолчанию) или в PostgreSQL/MySQL. Сами данные всегда лежат в источнике.

Путь одного запроса:

```text
[браузер]  --HTTP(S) 3000-->  [Grafana backend]
   |                                |  (datasource plugin, Go)
   |  панель: PromQL-запрос         v
   |                          [Prometheus 10.10.10.40:9090]
   |                                |  /api/v1/query_range
   |                                v
   |                          [экспортеры: node_exporter :9100]
   |<---- JSON с series --- [transformations] --- [panel render]
```

Ключевые слои:

- **Datasource** — плагин-адаптер к источнику: Prometheus, Loki, Elasticsearch, Zabbix, PostgreSQL, Alertmanager. У каждого есть **uid**; дашборды ссылаются именно на uid, а не на имя. Поменял uid — панели покажут «Datasource not found».
- **Dashboard** — JSON-документ: `panels[]`, `templating.list[]`, `time`, `refresh`, `uid`, `schemaVersion`.
- **Variables (переменные)** — шаблонные подстановки: `$instance`, `$env`. Типы: `query` (список значений из datasource), `custom`, `constant`, `interval`, `textbox`. Внутри панели используется `$instance` или `${instance:regex}`.
- **Provisioning** — механизм, при котором Grafana читает YAML/JSON из `/etc/grafana/provisioning/` при старте и каждые N секунд (`updateIntervalSeconds`). Такие объекты **не редактируются в UI** — источник истины файл.
- **Unified alerting** (с Grafana 9, в 11–12 основная модель): планировщик Grafana сам выполняет запрос по расписанию, сравнивает с условием, держит состояние `pending → firing`, шлёт в contact point. Альтернатива — оставить алерты в Prometheus, а Grafana использовать как витрину (подробнее в уроке 80).
- **Grafana Operator** — контроллер в Kubernetes, который следит за CRD `Grafana`, `GrafanaDatasource`, `GrafanaDashboard`, `GrafanaFolder`, `GrafanaAlertRuleGroup`, `GrafanaContactPoint` и приводит реальную Grafana к описанному состоянию (reconcile loop).

Порты: **3000/tcp** — Grafana UI и API; 9090 — Prometheus; 3100 — Loki; 10051 — Zabbix (у тебя на MON01 он тоже есть, мирно живут на разных портах). Grafana работает по HTTP/HTTPS, API — REST с токеном `Authorization: Bearer` или Basic auth.

## 3. Аналогия из жизни

Grafana — это **щитовая с приборами** на заводе. Сами датчики (экспортеры) стоят на станках, провода (Prometheus) тянутся к шкафу с логами измерений, а в щитовой ты видишь стрелки и лампочки, скомпонованные так, как удобно оператору. Можно по-разному расположить приборы на панели (дашборды), можно поставить переключатель «показать цех №1 / цех №2» (переменная `$instance`), а можно повесить сирену, которая орёт, когда стрелка уходит в красную зону (алерт).

Provisioning как код — это **чертёж щита**: если щит разбили, его собирают заново по чертежу, а не «как в прошлый раз кто-то помнил». Grafana Operator — мастер, который ходит по цеху с этим чертежом и сам докручивает недостающие винты.

Где аналогия ломается: приборы ничего не измеряют сами — Grafana только отображает; если датчик сдох, щит будет честно показывать ноль, и без отдельного алерта «нет данных» (`NoData`) ты этого не заметишь.

## 4. Практика и команды

**1. Установка Grafana на MON01 (10.10.10.40) из официального репозитория.**

```bash
sudo apt-get install -y apt-transport-https software-properties-common wget gpg
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install -y grafana
```

`apt.grafana.com` — официальный APT-репозиторий (OSS-сборка, бесплатная). Если из РФ репозиторий недоступен — скачай `.deb` с того же домена вручную и ставь `sudo apt install ./grafana_*.deb`; пиратские сборки не нужны и опасны.

**2. Запуск и проверка здоровья.**

```bash
sudo systemctl enable --now grafana-server
systemctl status grafana-server --no-pager
sudo ss -tlnp | grep 3000
curl -s http://10.10.10.40:3000/api/health | jq
```

```json
{ "commit": "abc1234", "database": "ok", "version": "11.x.y" }
```

Смотрим: `database: ok` — база открылась; если `database: failing` — права на `/var/lib/grafana` или битый `grafana.db`.

**3. Сброс пароля администратора (если забыл).**

```bash
sudo grafana cli admin reset-admin-password 'Lab#2026-vektor'
# в старых версиях бинарь называется grafana-cli — [ПРОВЕРИТЬ] для вашей версии
```

Пароль стенда — только для стенда; в реальной среде admin-пароль хранится в секрет-хранилище и не равен паролю домена.

**4. Datasource как код.** Файл `/etc/grafana/provisioning/datasources/prometheus.yaml`:

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prom-main          # uid фиксируем руками — на него ссылаются дашборды
    type: prometheus
    access: proxy           # запрос идёт через бэкенд Grafana, а не из браузера
    url: http://10.10.10.40:9090
    isDefault: true
    jsonData:
      timeInterval: 15s
      httpMethod: POST
```

Применение: `sudo systemctl restart grafana-server`. `access: proxy` важен: браузер пользователя не должен иметь прямой доступ к Prometheus.

**5. Провайдер дашбордов.** `/etc/grafana/provisioning/dashboards/vektor.yaml`:

```yaml
apiVersion: 1
providers:
  - name: 'vektor-git'
    orgId: 1
    folder: 'VEKTOR'
    type: file
    disableDeletion: true
    allowUiUpdates: false       # в UI нельзя «сохранить» поверх файла
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: true
```

`disableDeletion: true` не даст удалить дашборд из UI, `allowUiUpdates: false` — главный предохранитель от «сохранил в UI, а после рестарта пропало».

**6. Переменная в JSON дашборда (фрагмент).**

```json
"templating": {
  "list": [
    {
      "name": "instance",
      "label": "Сервер",
      "type": "query",
      "datasource": { "type": "prometheus", "uid": "prom-main" },
      "query": { "query": "label_values(up, instance)", "refId": "StandardVariableQuery" },
      "refresh": 2,
      "multi": true,
      "includeAll": true,
      "allValue": ".*"
    }
  ]
}
```

`refresh: 2` — обновлять список при загрузке дашборда и при смене интервала времени. `query` с `label_values(...)` возвращает уникальные значения метки — так переменная сама подхватит новый сервер.

**7. Экспорт дашборда из UI-базы в файл через API.**

```bash
TOKEN='<service-account-token>'
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://10.10.10.40:3000/api/search?query=node' | jq '.[] | {uid,title,folderTitle}'
curl -s -H "Authorization: Bearer $TOKEN" \
  http://10.10.10.40:3000/api/dashboards/uid/vektor-node | jq '.dashboard' > node-overview.json
```

Первый вызов — поиск (uid по названию), второй — полный JSON. Из него руками убираем `"id": N` (иначе импорт привяжется к конкретной базе), оставляем `uid`.

**8. Service account и токен для CI.**

```bash
curl -s -u 'admin:Lab#2026-vektor' -H 'Content-Type: application/json' \
  -X POST http://10.10.10.40:3000/api/serviceaccounts \
  -d '{"name":"gitops-ci","role":"Editor"}'
curl -s -u 'admin:Lab#2026-vektor' -H 'Content-Type: application/json' \
  -X POST http://10.10.10.40:3000/api/serviceaccounts/1/tokens -d '{"name":"ci-token"}' | jq -r '.key'
```

Сервис-аккаунт с ролью `Editor` вместо `Admin` — принцип минимальных прав. Токен в Git не коммитим, только в CI-переменную (Masked + Protected).

**9. Пуш дашборда через API (для Grafana Cloud / внешней Grafana, где нет файлового provisioning).**

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -n --slurpfile d node-overview.json \
        '{dashboard: $d[0], overwrite: true, folderUid: "vektor", message: "ci deploy"}')" \
  http://10.10.10.40:3000/api/dashboards/db | jq '{status, uid, version}'
```

`overwrite: true` — идемпотентный апдейт, `version` увеличится на 1. Это и есть «GitOps push» без ручного клика.

**10. Алерт как код.** `/etc/grafana/provisioning/alerting/contactpoints.yaml`:

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: vektor-oncall
    receivers:
      - uid: oncall-email
        type: email
        settings:
          addresses: oncall@vektor.lab
        disableResolveMessage: false
```

`/etc/grafana/provisioning/alerting/rules.yaml`:

```yaml
apiVersion: 1
groups:
  - orgId: 1
    name: node-basics
    folder: VEKTOR
    interval: 1m
    rules:
      - uid: node-high-cpu
        title: "CPU > 80% 10 минут"
        condition: C
        for: 10m
        noDataState: NoData
        execErrState: Error
        data:
          - refId: A
            relativeTimeRange: { from: 600, to: 0 }
            datasourceUid: prom-main
            model:
              expr: 100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
              refId: A
          - refId: C
            datasourceUid: __expr__
            model:
              type: threshold
              expression: A
              conditions: [{ evaluator: { params: [80], type: gt } }]
```

`__expr__` — встроенный «datasource» для математики над результатом запроса. `for: 10m` гасит мигание. Точные имена полей сверяй с документацией своей версии — формат менялся между 10 и 11 `[ПРОВЕРИТЬ]`.

**11. Grafana Operator: установка и CR.**

```bash
kubectl create namespace monitoring
# официальный quickstart из релизов оператора — ссылку сверьте на github.com/grafana/grafana-operator [ПРОВЕРИТЬ]
kubectl apply -f https://github.com/grafana/grafana-operator/releases/latest/download/quickstart-v5.yaml
kubectl -n monitoring get pods
kubectl get crd | grep grafana.integreatly.org
```

```yaml
apiVersion: grafana.integreatly.org/v1beta1
kind: Grafana
metadata:
  name: grafana-vektor
  namespace: monitoring
  labels: { dashboards: grafana-vektor }
spec:
  deployment:
    spec:
      template:
        spec:
          containers:
            - name: grafana
              envFrom:
                - secretRef: { name: grafana-admin }
```

```bash
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=GF_SECURITY_ADMIN_PASSWORD='Lab#2026-vektor'
kubectl -n monitoring get grafana grafana-vektor
kubectl -n monitoring port-forward svc/grafana-vektor-service 3000:3000
```

**12. Дашборд в кластере как CR.**

```yaml
apiVersion: grafana.integreatly.org/v1beta1
kind: GrafanaDashboard
metadata:
  name: node-overview
  namespace: monitoring
spec:
  instanceSelector:
    matchLabels: { dashboards: grafana-vektor }
  json: |
    { "title": "Node Overview", "uid": "vektor-node", "panels": [] }
```

```bash
kubectl -n monitoring get grafanadashboards
kubectl -n monitoring describe grafanadashboard node-overview | tail -20
```

`instanceSelector` — обязательное поле: без него оператор не поймёт, в какой инстанс Grafana класть дашборд (и в старых версиях мог разложить во все).

**13. Проверка конфигов в GitLab CI на GIT01.**

```yaml
stages: [lint, deploy]
lint:
  stage: lint
  image: alpine:3.20
  script:
    - apk add --no-cache jq yamllint
    - find dashboards -name '*.json' -print0 | xargs -0 -n1 jq -e . > /dev/null
    - yamllint -d relaxed provisioning/
deploy:
  stage: deploy
  rules: [{ if: '$CI_COMMIT_BRANCH == "main"' }]
  script:
    - ./scripts/sync-to-mon01.sh
```

## 5. Лабораторная работа

### Предусловия

- **MON01** (`10.10.10.40`, Ubuntu Server 24.04): работает Prometheus из урока 78, слушает `:9090`, в `targets` есть `UBU01` и/или `WEB01` с `node_exporter` на `:9100`. Если Prometheus установлен на другой машине — подставляй её адрес. Zabbix на MON01 не мешает.
- **GIT01** (`10.10.10.50`): GitLab CE + зарегистрированный runner (из модуля Git/CI). Есть твой пользователь и право создавать проекты.
- **k8s-cp01** (`10.10.10.61`): кластер из модуля Kubernetes в состоянии Ready, `kubectl` работает с `k8s-cp01`. Узлы `k8s-w01`/`k8s-w02` — в Ready.
- **UBU01** (`10.10.10.30`): для провокации алерта по CPU.
- Windows-часть не нужна.

### Снапшот

На **MON01**, **GIT01** и **k8s-cp01** (можно и на воркерах) в VMware Workstation: `VM → Snapshot → Take Snapshot`, имя **`before-devops-079`**. Пока ВМ включены — лучше выключить или делать снапшот «с памятью» (`Snapshot of the virtual machine's memory`), чтобы не ловить несогласованность диска.

### Шаги

1. **MON01.** Установи Grafana по команде из п.4.1, затем `sudo systemctl enable --now grafana-server` и `curl -s localhost:3000/api/health`. **Ожидаемо:** `database: ok`, порт 3000 слушается.
2. **MON01.** Открой с рабочей машины `http://10.10.10.40:3000`, войди `admin/admin`, смени пароль на `Lab#2026-vektor`. **Ожидаемо:** попал на Home. *Это небезопасно: единый пароль и логин admin — только для стенда.*
3. **GIT01.** В GitLab создай проект `grafana-config`, склонируй его в домашнюю папку на MON01: `git clone http://10.10.10.50/<твой-логин>/grafana-config.git ~/grafana-config`. Создай структуру:

```text
grafana-config/
├── provisioning/
│   ├── datasources/prometheus.yaml
│   ├── dashboards/vektor.yaml
│   └── alerting/{contactpoints.yaml, rules.yaml}
├── dashboards/vektor/node-overview.json
├── k8s/grafana-operator/{grafana.yaml, dashboard.yaml}
├── scripts/sync-to-mon01.sh
└── .gitlab-ci.yml
```

**Ожидаемо:** `git status` видит файлы, `git push` уходит на GIT01.

4. **MON01.** Положи файлы datasource-провайдера и dashboard-провайдера (п.4.4 и 4.5) в `provisioning/`, затем `sudo rsync -a ~/grafana-config/provisioning/ /etc/grafana/provisioning/ && sudo systemctl restart grafana-server`. **Ожидаемо:** в UI `Connections → Data sources` есть `Prometheus`, кнопки «Save & test» нет — datasource provisioned.
5. **MON01.** В UI собери быстрый дашборд «Node Overview» в папке `VEKTOR`: панель `100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))*100)` и панель `node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes * 100`. Проверь, что данные есть. **Ожидаемо:** два графика с линиями по `UBU01`/`WEB01`.
6. **MON01.** Верни дашборд в Git кодом: экспортируй его через API (п.4.7) в `dashboards/vektor/node-overview.json`, убери `"id"`, добавь переменную `instance` (п.4.6) и в запросах панелей замени жёсткий фильтр на `{instance=~"$instance"}`. Скопируй файл в `/var/lib/grafana/dashboards/vektor/`. **Ожидаемо:** через ≤30 с (`updateIntervalSeconds`) дашборд обновился, сверху появился выпадающий список серверов с `All`.
7. **MON01.** Проверь защиту от «сохранений в UI»: открой дашборд, нажми `Save` → **Ожидаемо:** предупреждение, что дашборд provisioned и сохранить нельзя (или изменения не переживут рестарт).
8. **MON01.** Добавь файлы алертов (п.4.10), `rsync` в `/etc/grafana/provisioning/alerting/`, рестарт. В UI `Alerting → Alert rules` найди правило `CPU > 80% 10 минут` с пометкой provisioned. **Ожидаемо:** правило в состоянии `Normal`.
9. **UBU01.** Спровоцируй нагрузку: `sudo apt install -y stress-ng` (или `sudo stress-ng --cpu 4 --timeout 900`), при недоступности пакета — `for i in $(seq 4); do timeout 900 bash -c 'while :; do :; done' & done`. **Ожидаемо:** на дашборде CPU UBU01 уходит за 80%, через ~10–11 минут правило переходит в `Pending` → `Firing`.
10. **GIT01/MON01.** Закоммить всё: `git add -A && git commit -m 'grafana: datasources, dashboard, alert as code' && git push`. Убедись, что пайплайн `.gitlab-ci.yml` (п.4.13) зелёный: JSON валиден, YAML без ошибок. **Ожидаемо:** job `lint` passed.
11. **k8s-cp01.** Установи Grafana Operator (п.4.11), примени `k8s/grafana-operator/grafana.yaml` и секрет. **Ожидаемо:** `kubectl -n monitoring get grafana` → `grafana-vektor`, под в Running.
12. **k8s-cp01.** Примени `GrafanaDashboard` из п.4.12 (json возьми из `node-overview.json`), сделай `port-forward` и залогинься. **Ожидаемо:** дашборд появился в кластерной Grafana; удали CR (`kubectl delete grafanadashboard node-overview`) — дашборд исчезнет, это и есть reconcile.
13. **MON01.** Запиши изменения в `stand-log.md`.

### Проверка

- [ ] `curl -s http://10.10.10.40:3000/api/health` возвращает `"database":"ok"`.
- [ ] Datasource `Prometheus` в UI помечен как provisioned, uid = `prom-main`.
- [ ] В дашборде работает переменная `instance`, значения — реальные инстансы из Prometheus.
- [ ] `grep -r "node-overview" /etc/grafana/provisioning/dashboards/` показывает провайдера, указывающего на `/var/lib/grafana/dashboards`.
- [ ] Правило `node-high-cpu` видно в `Alerting → Alert rules` и оно provisioned.
- [ ] После `stress-ng` на UBU01 алерт ушёл в Firing (проверь `Alerting → Alert rules` и `Contact points`).
- [ ] Пайплайн `grafana-config` на GIT01 зелёный.
- [ ] `kubectl -n monitoring get grafanadashboards` показывает `node-overview`; удаление CR убирает дашборд.

### Сломай и почини

> **Пользователь:** дежурный инженер сопровождения
> **ВМ:** MON01
> **Что написал:** «После вашего пуша дашборд Node Overview показывает No data на всех панелях. Переменная “Сервер” пустая. У нас через час окно у клиента, нужны графики.»
> **Срочность:** высокая

**Как воспроизвести поломку (намеренно):** в `provisioning/datasources/prometheus.yaml` поменяй `uid: prom-main` на `uid: prometheus-1`, закоммить, сделай `rsync` + `systemctl restart grafana-server`. Дашборд ссылается на старый uid → панели «No data», переменная `label_values` не отрабатывает.

**Диагностика по порядку:**

1. Grafana вообще жива? `curl -s http://10.10.10.40:3000/api/health` → `database`.

2. Что в логах? `journalctl -u grafana-server -n 80 --no-pager | grep -i -E 'datasource|error|provision'` — ищем `failed to provision` или `datasource not found`.

3. Источник жив? `curl -s 'http://10.10.10.40:9090/-/healthy'` и `curl -s 'http://10.10.10.40:9090/api/v1/query?query=up' | jq '.data.result | length'` — если `up` пуст, проблема не в Grafana, а в Prometheus/экспортерах.

4. Что реально в базе? `curl -s -H "Authorization: Bearer $TOKEN" http://10.10.10.40:3000/api/datasources | jq '.[] | {name,uid,type}'` — видим `prometheus-1` вместо `prom-main`.

5. Ссылка из дашборда: `jq '.templating.list[0].datasource, .panels[0].datasource' dashboards/vektor/node-overview.json` — там `prom-main`.

6. Проверка здоровья datasource: `curl -s -H "Authorization: Bearer $TOKEN" http://10.10.10.40:3000/api/datasources/uid/prometheus-1/health | jq` — вернёт статус подключения.

**Решение:** вернуть uid `prom-main` (uid — это контракт, его меняют только вместе с дашбордами), `rsync` + рестарт, проверить дашборд. Дополнительно — добавить в CI шаг, который сверяет uid в `provisioning/datasources/*.yaml` со ссылками в `dashboards/*.json` (`grep -o 'prom-main' ...`).

**Ответ пользователю:** «Причина: в источнике данных сменился uid, дашборд ссылался на старый. Вернул uid, конфиг в Git, дашборд снова показывает графики по UBU01/WEB01. Добавил проверку в CI, чтобы не повторилось. Проверьте, пожалуйста, и подтвердите.»

**Закрытие:** статус Resolved, в комментарии — ссылка на коммит и MR, тег `monitoring`, запись в `stand-log.md`.

### Что изменилось на стенде

- **MON01 (10.10.10.40):** установлена Grafana (порт 3000/tcp), `grafana-server` в автозапуске; каталоги `/etc/grafana/provisioning/{datasources,dashboards,alerting}/` заполнены файлами; дашборды из `/var/lib/grafana/dashboards/vektor/`.
- **MON01:** datasource `Prometheus` с uid `prom-main`, папка `VEKTOR`, дашборд `vektor-node` (Node Overview) с переменной `instance`.
- **MON01:** contact point `vektor-oncall` (email `oncall@vektor.lab`), правило алерта `node-high-cpu`.
- **GIT01 (10.10.10.50):** проект `grafana-config` — источник истины по Grafana, пайплайн `lint` (+ `deploy`).
- **k8s-cp01 (10.10.10.61):** namespace `monitoring`, Grafana Operator (CRD `grafana.integreatly.org`), инстанс `grafana-vektor`, секрет `grafana-admin`, CR `GrafanaDashboard/node-overview`.
- **UBU01:** пакет `stress-ng` (или использован bash-цикл) — следов в конфиге нет.

### Откат

- **MON01/GIT01/k8s-cp01:** VMware `VM → Snapshot → Snapshot Manager` → выбрать `before-devops-079` → `Restore`. Перед восстановлением выключить ВМ.
- Частичный откат без снапшота: `sudo systemctl stop grafana-server && sudo apt-get remove --purge grafana` и `sudo rm -rf /etc/grafana /var/lib/grafana`; в кластере — `kubectl delete -f quickstart-v5.yaml` и `kubectl delete ns monitoring`.
- Откат только дашборда: `git revert <commit>` + `rsync` + рестарт.

## 6. Ошибки новичков и безопасность

1. **Собирать всё мышкой и не хранить в Git** → потеря базы = потеря всех дашбордов; копии расходятся. Правильно: provisioning из репозитория, UI только для черновиков.
2. **`allowUiUpdates: true` «чтобы удобно править»** → правки в UI молча перетираются файлом через 30 секунд или после рестарта, люди спорят «кто сломал». Правильно: `false`, правки только через MR.
3. **Менять `uid` datasource** → все дашборды и алерты разом показывают No data. Правильно: uid — часть контракта, меняется вместе с дашбордами и проверяется в CI.
4. **Токен сервис-аккаунта или admin-пароль в git/values.yaml** → утечка = полный контроль над мониторингом у любого, кто видит репозиторий. Правильно: CI-переменные Masked/Protected, Sealed Secrets / Vault / External Secrets для k8s.
5. **Алерт без `for` и по слишком узкой метрике** → шторм уведомлений и «алерты игнорируют». Правильно: `for: 5–10m`, threshold с запасом, `noDataState` осознанно; политика эскалации — урок 80.
6. **`includeAll: true` на переменной с тысячами значений** → один дашборд кладёт Prometheus тяжёлым regex-запросом. Правильно: ограничивать выборку через `label_values(..., job="node")` или `custom`-список.
7. **GrafanaDashboard без `instanceSelector`** → дашборд уезжает во все инстансы, включая прод-кластер. Правильно: всегда явный selector по label инстанса.
8. **SQLite + несколько реплик Grafana** → рассинхрон, «пропавшие» дашборды, битые сессии. Правильно: PostgreSQL и общий provisioning-том, тогда HA работает (урок про отказоустойчивость).

### Безопасность

- Grafana, открытая в интернет без TLS и reverse-proxy, — это карта инфраструктуры для атакующего. В боевой среде: только за nginx/ingress с TLS, `root_url` с https, `cookie_secure = true`.
- Никогда не оставляй `[auth.anonymous] enabled = true` и роль `Viewer` без аутентификации — это утечка имён хостов, метрик и бизнес-показателей. На стенде допустимо только чтобы быстро проверить дашборд, и сразу выключать.
- Логин `admin` с паролем домена `Lab#2026-vektor` — исключительно стенд. В работе: интеграция с AD/LDAP или OIDC, роли по группам (`GG-IT` → Editor, остальные → Viewer), отдельные сервис-аккаунты на каждый CI.
- Grafana читает данные от имени своих datasource — datasource-токены (например, токен VictoriaMetrics или Prometheus с basic auth) храни в секретах, а не в YAML в Git.
- Установка сторонних плагинов с интернета — цепочка поставок: плагин исполняется в бэкенде Grafana. Ставь только те, что нужны, из официального каталога, и фиксируй версии.

## 7. Вопросы на собеседовании

**1. Чем Grafana отличается от Prometheus и почему нельзя обойтись только Grafana?**

Ответ: Prometheus — система сбора и хранения time series плюс язык запросов PromQL и правила алертов. Grafana — слой визуализации и агрегации по многим источникам: она ничего не хранит (кроме своей конфигурации) и не умеет собирать метрики с экспортеров. Grafana без источника данных — пустой щит. Обычная связка: экспортеры → Prometheus → Grafana как UI; алерты либо в Prometheus+Alertmanager, либо в unified alerting Grafana.

*Что хочет услышать интервьюер:* чёткое разделение ролей «сбор/хранение» и «отображение», понимание, что Grafana мультиисточниковая (Prometheus + Loki + Zabbix в одном дашборде).

*Красный флаг:* «Grafana собирает метрики» или «Grafana хранит метрики в своей базе».

**2. Как вы версионируете дашборды и алерты и какие есть варианты?**

Ответ: три-четыре подхода. (1) Файловый provisioning: YAML-провайдер + JSON дашборды в каталоге, обновление по `updateIntervalSeconds` — работает в любой инсталляции. (2) HTTP API push из CI (`/api/dashboards/db` с `overwrite: true`) — нужен для Grafana Cloud и внешних инсталляций. (3) Terraform-провайдер Grafana. (4) Grafana Operator в Kubernetes — CRD описывают Grafana, datasource, dashboard, alert rule group, оператор reconcile’ит. Ключевые правила: фиксированный `uid`, папки как код, `allowUiUpdates: false`, валидация JSON/YAML в CI, деплой только из main.

*Что хочет услышать интервьюер:* «конфигурация в Git, применение автоматическое и идемпотентное», знание trade-off файлов против API против оператора.

*Красный флаг:* «экспортирую JSON руками раз в месяц» или путаница «GitOps = просто git push».

**3. Чем unified alerting Grafana отличается от алертов Prometheus+Alertmanager и что выбрать?**

Ответ: в Prometheus правила вычисляются в самом Prometheus, Alertmanager занимается дедупликацией, группировкой, silences и маршрутизацией. В Grafana alert rule может опрашивать любой datasource (Prometheus, Loki, SQL, Zabbix), а condition строится визуально через `__expr__`; contact points и notification policies живут в Grafana. Минусы Grafana-алертинга: планировщик и состояние — в Grafana (нужна HA-конфигурация с Postgres), меньше возможностей группировки, чем у Alertmanager. Практика: инфраструктурные алерты — в Prometheus/Alertmanager (надёжнее, ближе к данным), продуктовые и «по логам/SQL» — в Grafana; либо единая точка — Grafana с внешним Alertmanager как datasource.

*Что хочет услышать интервьюер:* понимание, где живёт состояние и планировщик, и осознанный выбор, а не «что красивее в UI».

*Красный флаг:* «Grafana может всё, Alertmanager не нужен» без обсуждения HA и группировки.

**4. У вас 5 окружений и 40 хостов. Как сделать один дашборд вместо сорока копий?**

Ответ: переменные. `query`-переменная `env` со значениями из метки (`label_values(up, env)`), `query`-переменная `instance` с каскадной фильтрацией (`label_values(up{env=~"$env"}, instance)`), `multi: true` + `includeAll: true` с `allValue: ".*"`, в запросах — `{env=~"$env", instance=~"$instance"}`. Повторяющиеся панели — через `repeat` по переменной. Один JSON, ноль копий, деплой из Git.

*Что хочет услышать интервьюер:* «переменные + regex-матчинг в PromQL», понимание каскада и проблем кардинальности.

*Красный флаг:* «сделаю копию дашборда на каждое окружение».

**5. Как работает Grafana Operator и что произойдёт, если удалить Custom Resource?**

Ответ: оператор устанавливает CRD (`Grafana`, `GrafanaDatasource`, `GrafanaDashboard`, `GrafanaAlertRuleGroup`, `GrafanaContactPoint` и др.) и запускает контроллер, который в цикле сверяет desired state (CR) с actual (API Grafana) и досоздаёт/обновляет объекты. Связь CR с инстансом — через `instanceSelector` по labels. Удалил `GrafanaDashboard` — финализатор удалит дашборд из Grafana; удалил `Grafana` — по умолчанию удалится Deployment, но PVC с базой может остаться (зависит от настроек). Плюсы: всё в Git, один ArgoCD/Flux применяет манифесты, дрейф устраняется сам. Минусы: оператор — ещё один компонент, который надо обновлять и мониторить, и его версия должна совпадать с версией Grafana.

*Что хочет услышать интервьюер:* понимание reconcile-цикла, селекторов и того, что оператор — это тоже ПО, требующее поддержки.

*Красный флаг:* «оператор сам поставит Grafana и всё настроит» без понимания CRD и селекторов.

## 8. Источники (официальная документация)

- **grafana.com/docs** — основной сайт документации Grafana. Разделы: «Provisioning Grafana» (datasources, dashboards, alerting), «Grafana Alerting» (fundamentals, contact points, provisioned alert rules), «HTTP API» (dashboards, service accounts, datasources), «Configure Grafana» (grafana.ini), «Set up Grafana» (install on Debian/Ubuntu). Поиск: `grafana provisioning alerting files apiVersion 1`.
- **grafana.com/grafana/dashboards** — каталог готовых дашбордов (например, Node Exporter Full, id 1860). Поиск: `grafana dashboards node exporter full`. *Проверяй, что дашборд совместим с твоей версией и не требует плагина.*
- **grafana.com/docs/grafana/latest/dashboards/variables** — переменные и шаблоны. Поиск: `grafana dashboard variables label_values includeAll`.
- **github.com/grafana/grafana-operator** — репозиторий и документация оператора, примеры CR, quickstart. Поиск: `grafana operator quickstart v5 crd example`. *Актуальные URL релизов и имена CRD проверяй на этой странице — они меняются между v4 и v5.* `[ПРОВЕРИТЬ]`
- **kubernetes.io/docs** — раздел «Concepts → Extend Kubernetes → Custom Resources» и «Custom Resource Definitions». Поиск: `kubernetes custom resources operator pattern`. Нужно, чтобы понимать, что такое CRD и reconcile, а не заучивать.
- **prometheus.io/docs** — раздел «Querying basics» и «Alerting». Поиск: `prometheus promql rate node_cpu_seconds_total`. Нужно для выражений в панелях и алертах.
- **gitlab.com/help** (docs.gitlab.com) — раздел «CI/CD pipelines», «CI/CD variables». Поиск: `gitlab ci masked protected variables`. `[ПРОВЕРИТЬ]` актуальный домен документации GitLab.
