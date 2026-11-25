# Zabbix в гибридной среде: когда он лучше Prometheus и как их сочетать

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №4127.** Пользователь: Сидоров (склад). ВМ: — (принтер этикеток в офисной сети). Что написал: «Принтер на складе не печатает с пятницы. Узнали только сегодня, когда встала отгрузка — полдня искали, в чём дело. Почему мониторинг не заметил?». Срочность: Высокая.

Вот типичная ситуация: кластер Kubernetes у вас обвешан датчиками, а принтер, коммутатор доступа и ИБП в углу склада — нет. Prometheus прекрасно видит поды и HTTP-хендлеры, но он ничего не знает про SNMP-счётчики картриджа и про то, что у железки кончился тонер. Zabbix это умеет «из коробки», но он плохо переваривает 300 подов с меняющимися именами.

В «Векторе» среда гибридная: Linux-серверы, k8s-кластер, а если Windows-часть стенда у тебя сохранилась — ещё DC01, FS01 и рабочие станции. Одна система мониторинга такое покрывает плохо, две без связи — ещё хуже. После этой главы ты будешь уметь: объяснить разницу моделей данных Zabbix и Prometheus, поставить `zabbix-agent2` на Linux-хост, вытащить метрику Prometheus-экспортера в Zabbix через `Prometheus pattern`, пробросить алерт Alertmanager в Zabbix через `zabbix_sender` и собрать дашборд в Grafana поверх обоих источников.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Zabbix — это монолит с базой данных.** Компоненты:

- `zabbix-server` — мозг: планировщик опроса (poller), обработчик триггеров, планировщик эскалаций. Слушает **10051/tcp** (trapper: сюда шлют данные агенты в активном режиме, прокси и `zabbix_sender`).
- `zabbix-agent2` — агент, слушает **10050/tcp** для пассивных проверок (сервер приходит и спрашивает: «дай `system.cpu.load`»), и сам открывает соединение на 10051 для активных проверок (агент раз в N секунд забирает у сервера список ключей и присылает значения).
- `zabbix-proxy` — тот же 10051 в обе стороны, но с локальным буфером: если канал до центра упал, прокси копит данные и досылает.
- `zabbix-web` (nginx/apache + PHP-FPM), `zabbix-java-gateway` (**10052** — активный обмен с JMX [ПРОВЕРИТЬ], 10053 — пассивный [ПРОВЕРИТЬ]), БД (PostgreSQL/MySQL), API — JSON-RPC по `/api_jsonrpc.php`.
- Дополнительно: SNMP **161/udp** (опрос) и **162/udp** (trap), IPMI **623/udp**, ICMP ping (simple check).

**Модель данных Zabbix:** хост → item (конкретная метрика, например `vfs.fs.size[/,pfree]`) → history → trigger (выражение над item) → event → action (уведомление, эскалация, авто-закрытие). Всё это лежит в реляционной БД и настраивается через UI/API. Отсюда сильные стороны: ACL на хосты, инвентаризация, ACK (подтверждение проблемы), эскалации, SLA-отчёты, готовые шаблоны под железо.

**Модель данных Prometheus:** временной ряд = имя метрики + набор меток (`node_load1{instance="10.10.10.30:9100", job="node"}`). Сервер сам ходит по `scrape_configs` (pull), хранит всё в TSDB, правил и алертов отдельно нет — они в Alertmanager.

```text
                    ┌────────────────────── MON01 10.10.10.40 ──────────────────────┐
                    │ zabbix-server :10051   zabbix-web/nginx :80   PostgreSQL      │
                    │      ▲      │                                                   │
                    │      │      └──► trigger → event → action (медиа/эскалация)    │
                    └──────┼──────────────────────────────────────────────────────────┘
        активные/пассивные проверки :10050 / :10051
   ┌──────────┬───────────┬──────────────┬──────────────┬──────────────────┐
 UBU01 .30  WEB01 .20   GIT01 .50     HARBOR01 .55   коммутатор/принтер
 agent2     agent2      agent2        agent2         SNMP :161, trap :162

 UBU01 node_exporter :9100 ──pull──► Prometheus (k8s) :9090 ──► Alertmanager :9093
                                                                     │ webhook
                                                                     ▼
                                              мост :9095 ──zabbix_sender──► Zabbix :10051
```

**Ключевое различие:** Zabbix спрашивает «как дела у хоста UBU01» — у него есть именованный объект. Prometheus спрашивает «что отдаёт эндпоинт» — у него есть поток чисел с метками. Первое лучше для парка разнородных устройств, второе — для быстро меняющейся динамической среды.

## 3. Аналогия из жизни

Zabbix — это поликлиника с диспансеризацией. На каждого сотрудника заведена карточка: ФИО, отдел, дата последнего осмотра, свои нормы давления. Врач приходит сам или сотрудник приходит сам — но карточка есть всегда, и по ней видно историю за годы. Prometheus — это сеть датчиков дыма и температуры в здании: датчики не знают, как их зовут, они просто каждые 15 секунд отправляют число на общую панель, а панель уже считает «температура растёт быстрее, чем на 2 градуса за 5 минут».

Аналогия ломается на масштабе: когда в «здании» 10 000 помещений и датчики меняются каждые несколько минут, поликлиника с бумажными карточками захлёбывается — а панель с числами работает как раньше.

## 4. Практика и команды

**1. Локальный тест ключа агентом.** Проверяет, что ключ вообще поддерживается и что возвращает, без участия сервера.

```bash
sudo -u zabbix zabbix_agent2 -t 'system.cpu.load[all,avg1]'
# system.cpu.load[all,avg1]                        [d|0.070000]
```

`-t` — test single key. Если ключ неизвестен — увидишь `ZBX_NOTSUPPORTED` и причину.

**2. Список поддерживаемых ключей.** Полезно, когда шаблон не применился.

```bash
zabbix_agent2 -p | head -20
# vfs.fs.discovery
# vfs.fs.size[/,total]
# net.if.discovery
```

`-p` — print known items (с учётом `UserParameter` из конфига).

**3. Опрос агента снаружи (пассивная проверка).** Ровно то, что делает сервер.

```bash
zabbix_get -s 10.10.10.30 -p 10050 -k 'agent.ping'
# 1
zabbix_get -s 10.10.10.30 -k 'agent.version'
# 7.0.x
```

`-s` — адрес, `-p` — порт, `-k` — ключ. Если `agent.ping` не вернул `1` — проблема в сети/файрволе/конфиге агента, а не в Zabbix.

**4. Отправка значения в Zabbix (trapper).** Так шлют данные скрипты и внешние системы.

```bash
zabbix_sender -z 10.10.10.40 -s Prometheus-AM -k prom.alert.status -o 1 -vv
# Response from "10.10.10.40:10051": "processed: 1; failed: 0"
```

`-z` — сервер, `-s` — имя хоста в Zabbix (должно совпадать!), `-k` — ключ, `-o` — значение. Item должен быть типа **Zabbix trapper**.

**5. Проверка портов и сервисов на сервере.**

```bash
ss -lntp | grep -E ':(80|10050|10051)'
systemctl --no-pager status zabbix-server zabbix-agent2
journalctl -u zabbix-server -f --since "10 min ago"
```

**6. Источник метрик для Prometheus-паттерна.**

```bash
curl -s http://10.10.10.30:9100/metrics | grep -E '^node_load1|^node_memory_MemAvailable_bytes'
# node_load1 0.15
# node_memory_MemAvailable_bytes 6.123e+09
```

Формат `имя_метрики значение` — именно его разбирает preprocessing в Zabbix.

**7. Создание item через Zabbix API** (то же, что руками в UI, но воспроизводимо из Git).

```bash
ZBX=http://10.10.10.40/api_jsonrpc.php
ZBX_TOKEN='<токен из UI: Users → API tokens>'

curl -s -X POST -H 'Content-Type: application/json-rpc' \
  -H "Authorization: Bearer $ZBX_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"host.get","params":{"output":["hostid","host"]},"id":1}' \
  "$ZBX"
# {"result":[{"hostid":"10084","host":"UBU01"}, ...]}
```

Bearer-токены поддерживаются начиная с Zabbix 5.4 [ПРОВЕРИТЬ поведение в 7.x].

**8. Перечитать конфигурацию сервера без рестарта.**

```bash
sudo -u zabbix zabbix_server -R config_cache_reload
# zabbix_server [12345]: runtime control: Forcing a configuration cache reload
```

`-R` — runtime control. Для агента аналогично `zabbix_agent2 -R userparameter_reload` [ПРОВЕРИТЬ полный список опций в 7.x].

**9. Проверка скрейпа со стороны Prometheus.**

```bash
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 &
curl -s 'http://127.0.0.1:9090/api/v1/targets?state=active' | jq '.data.activeTargets[].scrapeUrl'
```

**10. Ручная отправка тестового алерта в Alertmanager.**

```bash
kubectl -n monitoring port-forward svc/prometheus-kube-prometheus-alertmanager 9093:9093 &
amtool --alertmanager.url=http://127.0.0.1:9093 alert add alertname="TestBridge" instance="UBU01" severity="warning"
amtool --alertmanager.url=http://127.0.0.1:9093 alert query
```

**11. Windows-агент** (если Windows-часть стенда у тебя есть; на DevOps-треке она не обязательна).

```powershell
& "C:\Program Files\Zabbix Agent 2\zabbix_agent2.exe" -t "service.info[Spooler,state]"
Get-Service "Zabbix Agent 2"
```

**12. Установка плагина Zabbix для Grafana.**

```bash
grafana cli plugins install alexanderzobnin-zabbix-app
systemctl restart grafana-server
```

Плагин community-ный, в Grafana 11 команда называется `grafana cli` [ПРОВЕРИТЬ под свою версию].

## 5. Лабораторная работа

### Предусловия

- **MON01 10.10.10.40** — установлен Zabbix (server, frontend, PostgreSQL), доступен веб-интерфейс, пароль `Lab#2026-vektor` для `Admin` [ПРОВЕРИТЬ имя учётки в своей установке]. Это состояние пришло с L2-трека.
- **UBU01 10.10.10.30** — Ubuntu Server 24.04, SSH под своим пользователем с sudo, агента Zabbix ещё нет.
- **k8s-cp01 10.10.10.61** — кластер с `kube-prometheus-stack` (модуль 84), Prometheus и Alertmanager работают.
- Windows-часть не нужна. Если Zabbix не установлен — пройди сначала установку Zabbix-сервера с L2, без него лабораторная не имеет смысла.

### Снапшот

В VMware Workstation на **каждой** затрагиваемой ВМ: `VM → Snapshot → Take Snapshot`, имя `before-devops-086` — на MON01, UBU01, k8s-cp01. Изменения в кластере откатить снапшотом отдельной ВМ не получится, поэтому конфиги Alertmanager сразу кладём в Git (GIT01 10.10.10.50).

### Шаги

1. **MON01.** Проверь, что Zabbix жив: `systemctl status zabbix-server zabbix-agent2` и `ss -lntp | grep 10051`. Ожидаемо: оба `active (running)`, порт 10051 слушается.
2. **UBU01.** Поставь агент. На `zabbix.com/download` выбери Zabbix 7.0 LTS, Ubuntu 24.04, Agent 2 — получишь точные команды; далее: `sudo dpkg -i zabbix-release_*_ubuntu24.04_all.deb`, `sudo apt update`, `sudo apt install -y zabbix-agent2`. Ожидаемо: пакет установлен, сервис в состоянии `active`.
3. **UBU01.** В `/etc/zabbix/zabbix_agent2.conf` задай `Server=10.10.10.40`, `ServerActive=10.10.10.40`, `Hostname=UBU01`, `ListenPort=10050`. Ожидаемо: `zabbix_agent2 -t agent.ping` → `1`.
4. **UBU01.** Открой порт только для сервера: `sudo ufw allow from 10.10.10.40 to any port 10050 proto tcp`. Ожидаемо: `ufw status` показывает правило.
5. **MON01.** `zabbix_get -s 10.10.10.30 -k agent.ping` → `1`. Если `1` — связка есть, дальше настраиваем UI.
6. **Zabbix UI.** `Data collection → Hosts → Create host`: Host name `UBU01`, Host group `Linux servers`, интерфейс Agent с IP `10.10.10.30` и портом `10050`, шаблон `Linux by Zabbix agent 2`. Ожидаемо: через 1–2 минуты индикатор **ZBX** зелёный, в `Monitoring → Latest data` появляются метрики.
7. **UBU01.** Если `node_exporter` с урока 78 не стоит — поставь и запусти, проверь `curl -s localhost:9100/metrics | head`. Ожидаемо: метрики отдаются на 9100.
8. **Zabbix UI.** `Data collection → Hosts → Create host`: имя `UBU01-PROM`, группа `Linux servers`, интерфейс — не нужен. Создай item: Type `HTTP agent`, Key `node.load1`, URL `http://10.10.10.30:9100/metrics`, Request type `GET`, Update interval `30s`. В Preprocessing добавь шаг **Prometheus pattern**, Pattern `node_load1`, Processing type `Value`. Ожидаемо: в Latest data появляется число, совпадающее с `node_load1` из curl.
9. **MON01.** Положи мост `/opt/zbx-bridge/bridge.py` (принимает webhook Alertmanager и пересылает в Zabbix через `zabbix_sender`; пакет `zabbix-sender` ставится из того же репозитория) и unit `zbx-bridge.service`, запусти, проверь `ss -lntp | grep 9095`. Ожидаемо: сервис активен, порт слушается.

```python
#!/usr/bin/env python3
import json, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

ZBX_SERVER, ZBX_HOST = "10.10.10.40", "Prometheus-AM"

def send(key, value):
    subprocess.run(["zabbix_sender", "-z", ZBX_SERVER, "-s", ZBX_HOST,
                    "-k", key, "-o", str(value)], check=False)

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}")
        for a in payload.get("alerts", []):
            labels = a.get("labels", {})
            send("prom.alert.status", 1 if a.get("status") == "firing" else 0)
            send("prom.alert.name", labels.get("alertname", "unknown"))
            send("prom.alert.instance", labels.get("instance", "-"))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

HTTPServer(("0.0.0.0", 9095), H).serve_forever()
```

10. **Zabbix UI.** Хост `Prometheus-AM`: три item'а типа `Zabbix trapper` — `prom.alert.status` (Numeric unsigned), `prom.alert.name` (Text), `prom.alert.instance` (Text). Триггер: `last(/Prometheus-AM/prom.alert.status)=1` с severity `Warning`. Ожидаемо: хост создан, триггер в списке.
11. **k8s-cp01.** В конфиг Alertmanager добавь receiver:

```yaml
route:
  receiver: zabbix-bridge
  group_by: [alertname]
  group_wait: 30s
  repeat_interval: 4h
receivers:
  - name: zabbix-bridge
    webhook_configs:
      - url: http://10.10.10.40:9095/alert
        send_resolved: true
```

Примени (для `kube-prometheus-stack` — через `alertmanager.config` в values; точный ключ зависит от версии чарта [ПРОВЕРИТЬ]). Ожидаемо: под Alertmanager перезапустился, в логах нет ошибок конфига.
12. **k8s-cp01.** `amtool alert add alertname="TestBridge" instance="UBU01" severity="warning"`. Ожидаемо: в Zabbix в `Monitoring → Problems` появляется проблема, через несколько секунд после гашения алерта (`amtool alert query` покажет пусто) — закрывается. Запиши изменения в `stand-log.md`.

### Проверка

- [ ] `zabbix_get -s 10.10.10.30 -k agent.ping` возвращает `1`
- [ ] В Zabbix у хоста `UBU01` индикатор ZBX зелёный, в Latest data есть CPU и память
- [ ] У хоста `UBU01-PROM` item `node.load1` отдаёт число, совпадающее с `curl .../metrics | grep node_load1`
- [ ] `systemctl status zbx-bridge` — `active (running)`, порт 9095 слушается
- [ ] `amtool alert add` порождает проблему в Zabbix с корректным именем алерта
- [ ] В `stand-log.md` записаны: новый хост UBU01, хост UBU01-PROM, хост Prometheus-AM, мост на 9095

### Сломай и почини

**Пользователь:** дежурный L1. **ВМ:** MON01 / UBU01. **Что написал:** «В Zabbix у хоста UBU01 всё серое, Latest data пустая, а по SSH машина жива и я там только что был».

**Как воспроизвести:** на UBU01 верни `Hostname=zabbix-agent` в конфиге и перезапусти агент — активные проверки перестанут матчиться с именем хоста в UI.

**Диагностика по порядку:**
1. `systemctl status zabbix-agent2` — жив ли агент.
2. `ss -lntp | grep 10050` — слушает ли порт (если `Server=` пустой, пассивные проверки не работают [ПРОВЕРИТЬ поведение своей версии]).
3. С MON01: `zabbix_get -s 10.10.10.30 -k agent.ping` — проходит ли пассивная проверка. Проходит → сеть и файрвол в порядке, копаем в активные.
4. Сравни `Hostname` в конфиге агента и `Host name` в Zabbix — самое частое расхождение.
5. `journalctl -u zabbix-agent2 -n 50` — ищи `no active checks on server`, `host not found`, `cannot connect to 10.10.10.40:10051`.
6. На MON01 `journalctl -u zabbix-server | grep UBU01` — видит ли сервер входящие соединения.

**Решение:** вернуть `Hostname=UBU01`, `systemctl restart zabbix-agent2`, при необходимости `zabbix_server -R config_cache_reload` на MON01.

**Ответ пользователю:** «Отклонение в конфиге агента: имя хоста не совпадало с карточкой в Zabbix, из-за этого активные проверки отбрасывались. Исправил, метрики по UBU01 пошли. Заявку закрываю, если в течение 15 минут данных не появится — пингуй».

**Закрытие:** перевести проблему в Resolved вручную, если она висела, и добавить в заявку запись о причине.

### Что изменилось на стенде

- **UBU01 10.10.10.30:** установлен `zabbix-agent2`, `Hostname=UBU01`, открыт 10050/tcp с 10.10.10.40; работает `node_exporter` на 9100.
- **MON01 10.10.10.40:** сервис `zbx-bridge` на порту 9095 (`/opt/zbx-bridge/bridge.py`), установлен `zabbix-sender`.
- **Zabbix:** хосты `UBU01` (шаблон `Linux by Zabbix agent 2`), `UBU01-PROM` (HTTP agent + Prometheus pattern), `Prometheus-AM` (trapper items и триггер).
- **k8s-cp01:** Alertmanager отправляет webhook на `http://10.10.10.40:9095/alert`; конфиг закоммичен в Git на GIT01 10.10.10.50.
- Windows-часть стенда не затрагивалась.

### Откат

VMware Workstation: `VM → Snapshot → Snapshot Manager` → выбери `before-devops-086` → **Restore Snapshot** — по очереди на MON01, UBU01, k8s-cp01. После отката верни конфиг Alertmanager из Git (`git -C <repo> checkout -- alertmanager/`) и перезапусти под. Проверь, что старые хосты не остались полудохлыми: в Zabbix удали `UBU01-PROM` и `Prometheus-AM`, если откатывался только один узел.

## 6. Ошибки новичков и безопасность

1. **`Hostname` в агенте ≠ `Host name` в UI** → активные проверки молча отбрасываются, хост «серый» → всегда сверяй оба поля, для активных проверок они обязаны совпадать байт в байт.
2. **Забыть `Server=` в конфиге агента** → пассивные проверки не работают, порт 10050 не слушается → задавай `Server` и `ServerActive` явно и проверяй `ss -lntp`.
3. **`EnableRemoteCommands=1` и ключ `system.run[*]`** → агент превращается в удалённый исполнитель команд, при утечке PSK это RCE на всех хостах → в норме `AllowKey`/`DenyKey` и категорический запрет произвольных команд.
4. **Оставить учётку `Admin` с дефолтным паролем и открыть фронтенд в офисную сеть** → через API переписывают конфиг и ломают мониторинг → меняй пароль, заводи отдельные API-токены под автоматизацию с минимальными ролями, фронтенд — за обратным прокси.
5. **Опрос всех item'ов раз в 5 секунд на 500 хостах** → база и поллеры захлёбываются, история растёт гигабайтами → интервалы по классу метрики (CPU 1m, диски 5m), history/trends с разумным retention.
6. **Пытаться пихать в Zabbix высококардинальные метки (по каждому поду)** → десятки тысяч новых item'ов, БД пухнет → высококардинальное оставляй в Prometheus, в Zabbix — агрегаты.
7. **Дублировать алерты из Prometheus и из Zabbix на один инцидент** → дежурный получает два тикета на одну проблему и перестаёт читать оба → один источник истины на класс событий, второй — только как enrichment.
8. **Открыть trapper 10051 в интернет без TLS PSK** → любой может прислать фейковые значения и «погасить» реальную аварию → только внутренняя сеть, PSK-шифрование между сервером и агентом/прокси.

**Безопасность отдельно.** На боевой среде Zabbix-сервер — это система с доступом во все сегменты сети, поэтому: отдельный сервисный аккаунт для БД без прав суперпользователя, файл конфига с паролем БД не world-readable, агенты только с PSK, правило файрвола «10050 только с адреса сервера/прокси», а веб-интерфейс — за SSO или хотя бы за VPN. Снапшот-лаборатория с единым паролем `Lab#2026-vektor` — это стенд, а не образец.

## 7. Вопросы на собеседовании

**1. «Чем Zabbix отличается от Prometheus и когда вы выберете каждый?»**
Ответ: разные модели. Zabbix — агентский/опросный мониторинг именованных объектов с реляционной БД, встроенными триггерами, эскалациями и ACK. Prometheus — pull-система временных рядов с метками, service discovery и PromQL. Zabbix беру для гетерогенного парка: сеть, SNMP-устройства, принтеры, ИБП, Windows-службы, филиалы за прокси. Prometheus — для Kubernetes, контейнеров, высококардинальных метрик и SLO на `histogram_quantile`.
*Что хочет услышать:* что вы не «выбираете религию», а смотрите на характер объектов и на модель данных.
*Красный флаг:* «Prometheus современный, Zabbix устарел» без единого аргумента про SNMP, кардинальность и UI для дежурного.

**2. «Как масштабировать Zabbix на филиалы с плохими каналами?»**
Ответ: Zabbix proxy в филиале, агенты в активном режиме сами идут на прокси по 10051, прокси буферизует данные при обрыве и досылает в центр. Прокси не хранит историю, но держит буфер; версия прокси должна соответствовать версии сервера. БД и поллеры сервера — по нагрузке, при необходимости разделение frontend/backend и партиционирование истории.
*Что хочет услышать:* про буферизацию и активные проверки, а не «поставим ещё один сервер».
*Красный флаг:* «поставим несколько независимых Zabbix-серверов и будем смотреть в каждый отдельно».

**3. «Экспортер отдаёт метрики в формате Prometheus. Как забрать их в Zabbix?»**
Ответ: item типа HTTP agent на URL `/metrics`, preprocessing шаг «Prometheus pattern» с именем метрики, тип обработки Value (или Table, если нужен разбор по меткам). Дальше обычный триггер.
*Что хочет услышать:* понимание, что Zabbix умеет потреблять Prometheus-формат, и что для единичных метрик это дешевле, чем поднимать второй сборщик.
*Красный флаг:* «никак, форматы несовместимы».

**4. «У вас алерты и в Prometheus, и в Zabbix. Как сделать одну точку входа для дежурного?»**
Ответ: выбрать владельца инцидента. Обычно Zabbix как UI/эскалации: Alertmanager через webhook отправляет в мост, мост вызывает `zabbix_sender` в trapper item, триггер создаёт проблему. Обратное направление — медиатип Zabbix с webhook на Alertmanager или напрямую в чат. Дедупликация — по `alertname` + `instance`, подавление — maintenance-окна синхронизировать в обеих системах.
*Что хочет услышать:* что кто-то один владеет жизненным циклом инцидента, второй — только поставляет данные.
*Красный флаг:* «настроим оба, дежурный разберётся».

**5. «Что такое высокая кардинальность и почему Zabbix тут проигрывает?»**
Ответ: кардинальность — количество уникальных комбинаций меток. В Prometheus серии индексируются по меткам, добавление нового пода — просто новая серия. В Zabbix метрика привязана к хосту и item'у, и каждый новый под — это новые объекты конфигурации и строки истории; на десятках тысяч объектов это дорого и по БД, и по кэшу конфигурации.
*Что хочет услышать:* конкретику про items vs series и про то, что для k8s берут Prometheus.
*Красный флаг:* «кардинальность — это про Grafana».

## 8. Источники (официальная документация)

- **zabbix.com/documentation** — раздел «Zabbix agent 2 (UNIX)», «Zabbix proxy», «Item types» (особенно HTTP agent и Zabbix trapper), «Preprocessing» (шаг Prometheus pattern), «API» (JSON-RPC, токены), «Media types». Поиск: `zabbix prometheus preprocessing pattern`.
- **zabbix.com/download** — генератор команд установки под конкретную ОС и версию; здесь же проверяй актуальную LTS-ветку [ПРОВЕРИТЬ].
- **prometheus.io/docs** — «Configuration» (`scrape_configs`, `alerting`), «HTTP API» (`/api/v1/targets`). Поиск: `prometheus configuration scrape_configs`.
- **prometheus.io/docs/alerting** — конфигурация Alertmanager, receiver `webhook_config`, `webhook_configs` поля. Поиск: `alertmanager webhook_configs send_resolved`.
- **grafana.com/docs** — «Data sources» (Zabbix), «Plugin installation». Поиск: `grafana zabbix data source plugin`.
- **kubernetes.io/docs** — «Monitoring, Logging, and Tracing» как общая рамка; конкретика по `kube-prometheus-stack` — в документации Helm-чарта на github.com/prometheus-community/helm-charts [ПРОВЕРИТЬ актуальный ключ `alertmanager.config`].
- Отдельно: список изменений по версиям Zabbix — `zabbix.com/documentation` → «What's new», там проверяй всё, что помечено `[ПРОВЕРИТЬ]` (номера портов Java gateway, поведение агента при пустом `Server`, синтаксис `-R`).
