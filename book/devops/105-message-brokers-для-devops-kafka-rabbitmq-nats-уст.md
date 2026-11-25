# Message brokers для DevOps: Kafka, RabbitMQ, NATS — установка, мониторинг, типовые проблемы

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 09:40. В поддержку падает тикет:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «С утра в 1С не подтягиваются новые платежи, заказы висят в статусе "Ожидание". Вчера всё работало.»
> **Срочность:** Высокая

Дальше выясняется классика: платёжный сервис исправно публикует события об оплате, а интеграция с 1С их не получает. Приложение живое, сервер живой, сеть живая. Сломано ровно одно звено — **брокер сообщений**, через который сервисы общаются асинхронно.

Брокер сообщений (message broker) — это промежуточный слой, который принимает сообщения от одних программ (продюсеров) и отдаёт их другим (консьюмерам), не требуя, чтобы обе стороны были доступны одновременно. Как только в компании появляется больше двух сервисов, брокер становится таким же критичным элементом, как база данных: он есть в любой схеме «оплата → биллинг → 1С → склад → уведомления».

Для DevOps это означает три вещи. Первая: брокер надо уметь поставить воспроизводимо — в контейнере, compose-файле или Helm-чарте, а не «руками по статье из интернета». Вторая: его надо мониторить не по принципу «процесс запущен», а по метрикам — длина очереди, лаг консьюмера, количество неподтверждённых сообщений, алерты по диску и памяти. Третья: 80% инцидентов с брокерами — это не «брокер упал», а «брокер работает, но сообщения не идут», и надо за 10 минут понять почему.

После этой главы ты будешь: понимать разницу между очередью, логом и subject-моделью; поднимать RabbitMQ, Kafka и NATS на стенде в виде кода; включать у них метрики и снимать их Prometheus-совместимым способом; диагностировать типовые аварии — блокировку публикации по диску, застрявшие unacked-сообщения, рост consumer lag, «slow consumer».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Все три системы — прикладной уровень (L7 модели OSI), работают поверх TCP и обычно требуют собственных портов, открытых в файрволе. Но модели внутри у них принципиально разные.

**RabbitMQ** — «умный брокер, глупый консьюмер». Реализует протокол AMQP 0-9-1 (плюс MQTT, STOMP, AMQP 1.0 через плагины). Есть обменники (exchange), которые по routing key раскладывают сообщение в очереди (queue). Брокер сам отслеживает состояние каждого сообщения: `ready` → выдано консьюмеру → `unacked` → после подтверждения (ack) удалено. Есть виртуальные хосты (vhost), политики, TTL, dead-letter exchange (DLX — «очередь для мёртвых писем»), quorum-очереди с репликацией.

**Kafka** — «глупый брокер, умный консьюмер». Это распределённый **лог**: сообщения пишутся в партиции топика последовательно и не удаляются после прочтения, а живут по политике хранения (retention, например 7 дней или 100 ГБ). Каждый консьюмер в группе хранит свой **offset** — номер последнего прочитанного сообщения. Отсюда ключевая метрика — **consumer lag** = log-end-offset минус current-offset. Порядок гарантируется только внутри партиции, поэтому число партиций = максимальный параллелизм чтения.

**NATS** — самый лёгкий. В базовом режиме это publish/subscribe и request/reply «по рации»: сообщение доставляется только тем, кто подписан **сейчас**, без персистентности (at-most-once). Плюс есть подсистема **JetStream**, которая добавляет потоки (streams), хранилище на диске, повторную доставку и дедупликацию — то есть превращает NATS в полноценный персистентный брокер.

```text
                    ┌──────────────────────────────────────────────┐
 Продюсер           │  Брокер на UBU01 (10.10.10.30)               │      Консьюмер
 (сервис оплаты)    │                                              │      (интеграция 1С)
      │             │  RabbitMQ  :5672 AMQP                        │             ▲
      ├──publish───►│            :15672 web UI                     │──consume────┤
      │             │            :15692 /metrics (плагин)           │             │
      │             │  Kafka     :9092 client                     │             │
      ├──produce───►│            :9093 controller (KRaft)          │──fetch──────┤
      │             │            :9308 exporter / :9404 JMX        │             │
      │             │  NATS      :4222 client                     │             │
      └──pub────────►│           :8222 monitoring (/varz, /jsz)    │──subscribe──┘
                    │            :7777 prometheus-nats-exporter    │
                    └──────────────────────┬───────────────────────┘
                                           │ scrape
                                           ▼
                              Prometheus / Zabbix на MON01
                                           │
                                           ▼
                                   Alertmanager → Telegram
```

Что важно понимать про гарантии доставки — это любимая тема интервью:

- **at-most-once** — «максимум один раз», можно потерять. NATS core.
- **at-least-once** — «минимум один раз», можно получить дубль. RabbitMQ с publisher confirms + ручной ack, Kafka без transactional API. Практический дефолт в 90% систем.
- **exactly-once** — «ровно один раз». Дорого: транзакции в Kafka (`isolation.level=read_committed`), идемпотентный продюсер, дедупликация в JetStream по `Nats-Msg-Id`. Почти всегда проще сделать консьюмер идемпотентным по бизнес-ключу, чем выжимать exactly-once из брокера.

И отдельно — **модель отказа**. У брокера есть три ресурса, которые заканчиваются: диск, память, файловые дескрипторы. RabbitMQ при нехватке диска или памяти **блокирует продюсеров** (connection переходит в состояние `blocked`) — это защитный механизм, чтобы не убить ноду. Kafka при заполнении диска просто падает партиция. Помни это: «брокер жив, но не принимает» — почти всегда про disk/memory alarm.

## 3. Аналогия из жизни

Представь сортировочный центр логистики — как раз профиль «Вектора».

**RabbitMQ** — это центр с диспетчером. Курьер привозит посылку, диспетчер смотрит на адрес и кладёт её в конкретный стеллаж (очередь). Курьер-получатель приходит, забирает посылку и **расписывается в накладной** — только после подписи посылка считается доставленной и со стеллажа исчезает. Если курьер ушёл и не расписался, диспетчер через время отдаёт посылку другому. Умный диспетчер знает всё про каждую коробку.

**Kafka** — это конвейерная лента с журналом отгрузки. Коробки едут по ленте (партиции) и с ленты **никто их не снимает** — они лежат там, пока не истечёт срок хранения, после чего лента сама их утилизирует. Каждый получатель имеет свою закладку в журнале («я прочитал до записи №4173») и читает с неё в своём темпе. Хочешь — перечитай с начала. Отставание закладки от конца ленты — это и есть lag.

**NATS** — это рация. Крикнул в эфир — услышали те, кто сейчас на волне. Никто не услышал — сообщение пропало. А **JetStream** — это диктофон, включённый на той же рации: записал, и можно переслушать позже.

Где аналогия ломается: на реальном складе потерянная коробка — это материальный ущерб и разбирательство, а в брокере потеря сообщения — это штатная ситуация, заложенная в выбранную тобой модель гарантий. Ответственность за то, что «ничего не потерялось», лежит на конфигурации, а не на «железности» брокера.

## 4. Практика и команды

Ниже — набор, который закрывает 90% ежедневной работы. Всё выполняется на **UBU01 (10.10.10.30)**.

**1. Установка RabbitMQ из репозитория Ubuntu**

```bash
sudo apt update && sudo apt install -y rabbitmq-server
sudo systemctl enable --now rabbitmq-server
sudo systemctl status rabbitmq-server --no-pager
sudo rabbitmqctl version
```

`apt install rabbitmq-server` тянет Erlang и ставит сервис под systemd. Версия в репозитории Ubuntu 24.04 — 3.12.x `[ПРОВЕРИТЬ актуальную]`. Для продакшена официально рекомендуют ставить из репозитория RabbitMQ (rabbitmq.com), а не из дистрибутива — версия свежее.

**2. Включаем management UI и метрики**

```bash
sudo rabbitmq-plugins enable rabbitmq_management rabbitmq_prometheus
sudo ss -tlnp | grep -E '5672|15672|15692'
```

`rabbitmq_management` даёт веб-интерфейс и REST API на 15672, `rabbitmq_prometheus` — эндпоинт `/metrics` на 15692. Если портов в выводе `ss` нет — плагин не поднялся, смотри `journalctl -u rabbitmq-server`.

**3. Учётка администратора и удаление guest**

```bash
sudo rabbitmqctl add_user adm.student 'Lab#2026-vektor'
sudo rabbitmqctl set_user_tags adm.student administrator
sudo rabbitmqctl set_permissions -p / adm.student ".*" ".*" ".*"
sudo rabbitmqctl delete_user guest
sudo rabbitmqctl list_users
```

`guest/guest` по умолчанию может логиниться **только с localhost**, но всё равно в любом мало-мальски реальном окружении его удаляют первым делом. `set_permissions -p / <user> <configure> <write> <read>` — три регулярки на vhost `/`.

**4. Состояние брокера и очередь глазами CLI**

```bash
sudo rabbitmqctl status | head -30
sudo rabbitmqctl list_queues name messages messages_ready messages_unacknowledged consumers
sudo rabbitmqctl list_connections name user peer_host state
```

Ключевые поля: `messages_ready` — лежит и ждёт; `messages_unacknowledged` — выдано, но не подтверждено; `consumers` — сколько подписчиков. Если `ready` растёт, а `consumers` = 0 — консьюмер умер, а продюсер продолжает лить. Если `unacknowledged` замерло на большом числе — консьюмер завис, не отвечая ack'ом.

**5. То же через REST API (удобно для скриптов и проверок в CI)**

```bash
curl -s -u adm.student:'Lab#2026-vektor' http://10.10.10.30:15672/api/overview | jq '.queue_totals'
curl -s -u adm.student:'Lab#2026-vektor' http://10.10.10.30:15672/api/queues/%2F/payments.new | jq '{messages, consumers}'
```

Vhost `/` в URL кодируется как `%2F` — на этом спотыкаются почти все. `jq` не обязателен, но без него глазами читать JSON больно.

**6. Объявление очереди с TTL и лимитом доставок**

```bash
sudo rabbitmqadmin declare queue name=payments.new durable=true \
  arguments='{"x-queue-type":"quorum","x-delivery-limit":3,"x-message-ttl":86400000}'
sudo rabbitmqctl list_queues name type durable arguments
```

`durable=true` — очередь переживёт рестарт; `x-message-ttl` в миллисекундах (86400000 = сутки); `x-delivery-limit` — сколько раз можно провалить доставку, прежде чем сообщение уйдёт в dead-letter. Тип `quorum` — реплицируемая очередь, дефолт для RabbitMQ 4.x `[ПРОВЕРИТЬ для твоей версии]`.

**7. Kafka в KRaft-режиме через compose (файл `~/mq/kafka-compose.yml`)**

```yaml
services:
  kafka:
    image: apache/kafka:3.9.0   # свежий тег смотри на hub.docker.com/r/apache/kafka [ПРОВЕРИТЬ]
    container_name: kafka
    restart: unless-stopped
    ports:
      - "10.10.10.30:9092:9092"
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      KAFKA_LISTENERS: PLAINTEXT://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://10.10.10.30:9092
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@localhost:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      CLUSTER_ID: 5L6g3nShT-eMCtK--X86sw
```

```bash
cd ~/mq && docker compose -f kafka-compose.yml up -d && docker logs --tail 20 kafka
```

`KRaft` — режим без ZooKeeper (начиная с Kafka 4.0 он единственный `[ПРОВЕРИТЬ]`). `ADVERTISED_LISTENERS` должен содержать адрес, по которому до брокера дотянутся клиенты, иначе метаданные вернут `localhost` и внешний клиент не подключится — классическая ошибка.

**8. Топик и проверка продюсер/консьюмер**

```bash
docker exec -it kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
  --create --topic payments --partitions 3 --replication-factor 1
docker exec -it kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic payments
docker exec -it kafka /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic payments
docker exec -it kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic payments --group billing --from-beginning
```

`--replication-factor 1` допустим только на стенде с одним брокером; в проде это потеря данных при падении ноды.

**9. Consumer lag — главная метрика Kafka**

```bash
docker exec -it kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group billing
```

```text
GROUP    TOPIC     PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG   CONSUMER-ID
billing  payments  0          42              42              0     consumer-billing-1
billing  payments  1          17              180             163   consumer-billing-1
billing  payments  2          8               9               1     -
```

Смотрим `LAG` по каждой партиции: ноль — догнали; растущий лаг — консьюмер не успевает; `CONSUMER-ID = -` — партиция вообще никем не читается (консьюмеров меньше, чем партиций, или группа «мертва»).

**10. NATS с JetStream в контейнере**

```bash
docker run -d --name nats --restart unless-stopped \
  -p 10.10.10.30:4222:4222 -p 10.10.10.30:8222:8222 \
  -v nats-data:/data nats:2.10 -js -sd /data -m 8222
docker logs --tail 20 nats
```

`-js` включает JetStream, `-sd /data` задаёт каталог хранилища (обязательно на volume, иначе потеряешь данные вместе с контейнером), `-m 8222` — HTTP-мониторинг.

**11. Работа с NATS через nats CLI**

```bash
docker run --rm -it --network host natsio/nats-box
# внутри:
nats server report jetstream
nats stream add ORDERS --subjects 'orders.>' --storage file --retention limits --max-age 24h --dupe-window 2m
nats pub orders.created '{"order":1001,"sum":12500}'
nats sub 'orders.>' --stream ORDERS
nats stream report
```

Флаги `stream add` в разных версиях CLI меняются — сверяйся с интерактивным режимом (просто `nats stream add` без аргументов задаст вопросы) `[ПРОВЕРИТЬ под свою версию]`. `--dupe-window` — окно дедупликации по заголовку `Nats-Msg-Id`.

**12. Метрики всех трёх одной командой**

```bash
curl -s http://10.10.10.30:15692/metrics | grep -E '^rabbitmq_queue_messages' | head
curl -s http://10.10.10.30:8222/varz | jq '{connections, in_msgs, out_msgs, slow_consumers}'
curl -s http://10.10.10.30:8222/jsz  | jq '{streams, messages, bytes}'
docker run -d --name kafka-exporter -p 9308:9308 danielqsj/kafka-exporter \
  --kafka.server=10.10.10.30:9092     # порт 9308 — дефолт экспортёра [ПРОВЕРИТЬ]
curl -s http://10.10.10.30:9308/metrics | grep kafka_consumergroup_lag | head
```

`/varz` — общая статистика NATS-сервера, `/jsz` — состояние JetStream. `slow_consumers > 0` — признак того, что кто-то не успевает читать и его буфер переполняется.

**13. Конфиг скрейпа Prometheus**

```yaml
scrape_configs:
  - job_name: rabbitmq
    static_configs:
      - targets: ['10.10.10.30:15692']
  - job_name: kafka
    static_configs:
      - targets: ['10.10.10.30:9308']
  - job_name: nats
    static_configs:
      - targets: ['10.10.10.30:7777']   # prometheus-nats-exporter, дефолтный порт [ПРОВЕРИТЬ]
```

**14. Диагностика «брокер жив, но сообщения не идут»**

```bash
sudo rabbitmq-diagnostics alarms
sudo rabbitmqctl status | grep -A3 -i alarm
df -h /var/lib/rabbitmq
sudo journalctl -u rabbitmq-server -n 100 --no-pager | tail -40
docker logs --tail 100 kafka
```

`rabbitmq-diagnostics alarms` — первое, что запускают при «очередь не растёт и не убывает». Пустой вывод = алармов нет, копаем в консьюмера.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS): доступ по SSH, установлен Docker + плагин Compose (главы про контейнеры), свободно минимум 20 ГБ диска, открыты порты 5672, 15672, 15692, 9092, 9308, 4222, 8222, 7777.
- **MON01** (10.10.10.40, Zabbix): используется как точка съёма метрик. Prometheus на стенде может отсутствовать — тогда метрики снимаем `curl`'ом и добавляем Zabbix-шаблоны, полный стек Prometheus+Grafana — тема отдельной главы.
- **k8s-w01** (10.10.10.62): используется только как «внешний клиент» для проверки сетевой доступности.
- Профильные ВМ Windows (DC01, FS01, WIN11-CL01, WIN10-CL02) в этой лабораторной **не участвуют**: трек DevOps опирается только на Linux-часть стенда.

### Снапшот

В VMware Workstation для каждой затрагиваемой ВМ: выделить ВМ → меню **VM → Snapshot → Take Snapshot…** → имя `before-devops-105` → OK.

Снапшоты делаем на **UBU01**, **k8s-w01**, **MON01**.

### Шаги

1. **UBU01.** Проверить свободное место и занятость портов до установки:
   `df -h / && sudo ss -tlnp | grep -E '5672|15672|9092|4222|8222'`
   Ожидаемо: места >20 ГБ, вывод `ss` пустой. Если порт занят — меняем порт в конфиге, а не «убиваем процесс».

2. **UBU01.** Установить RabbitMQ и включить плагины (команды 1 и 2 из раздела 4).
   Ожидаемо: `systemctl status` — `active (running)`, в `ss` видны 5672, 15672, 15692.

3. **UBU01.** Создать админа `adm.student`, удалить `guest` (команда 3).
   Ожидаемо: `rabbitmqctl list_users` показывает только `adm.student` с тегом `[administrator]`.

4. **k8s-w01.** Проверить доступность брокера по сети:
   `nc -zv 10.10.10.30 5672 && nc -zv 10.10.10.30 15672`
   Ожидаемо: `succeeded!` на обоих портах. Если нет — проверяем `sudo ufw status` на UBU01 и маршрут до 10.10.10.0/24.

5. **UBU01.** Объявить очередь `payments.new` (команда 6) и зайти в веб-интерфейс `http://10.10.10.30:15672` под `adm.student`.
   Ожидаемо: очередь видна во вкладке **Queues and Streams**, тип `quorum`, consumers = 0.

6. **UBU01.** Опубликовать 5 сообщений: в UI открыть очередь → **Publish message** → payload `{"order":1001}` → Publish (5 раз).
   Ожидаемо: `messages_ready = 5`. Теперь вычитать без консьюмера:
   `sudo rabbitmqadmin get queue=payments.new count=5 ackmode=ack_requeue_false`
   Ожидаемо: 5 сообщений в выводе, после этого `messages_ready = 0`.

7. **UBU01.** Поднять Kafka (команда 7), создать топик `payments` на 3 партиции (команда 8).
   Ожидаемо: `--describe` показывает 3 партиции, Leader = 1, Replicas = 1, Isr = 1.

8. **UBU01.** Запустить консьюмер группы `billing`, отправить 10 сообщений через продюсер, затем остановить консьюмер (Ctrl+C) и отправить ещё 20.
   Ожидаемо: в `kafka-consumer-groups.sh --describe` лаг по партициям суммарно ≈20 и он не уменьшается.

9. **UBU01.** Поднять NATS с JetStream, создать стрим `ORDERS`, опубликовать 3 сообщения (команда 11).
   Ожидаемо: `nats stream report` показывает `ORDERS`, 3 сообщения, размер хранилища >0.

10. **UBU01 + MON01.** Снять метрики (команда 12) и, если Prometheus/Zabbix настроены, добавить таргеты (команда 13).
    Ожидаемо: `/metrics` отдаёт метрики RabbitMQ; `/varz` и `/jsz` — валидный JSON; экспортёр Kafka отдаёт `kafka_consumergroup_lag`.

11. **UBU01.** Записать в `stand-log.md` всё, что появилось: сервисы, порты, имена очередей/топиков/стримов, учётки.

### Проверка

- [ ] `systemctl is-active rabbitmq-server` → `active`
- [ ] `rabbitmqctl list_users` содержит `adm.student` и **не содержит** `guest`
- [ ] `curl -s -o /dev/null -w '%{http_code}' http://10.10.10.30:15672/` → `200`
- [ ] `curl -s http://10.10.10.30:15692/metrics | grep -c '^rabbitmq_'` → число больше нуля
- [ ] `kafka-topics.sh --describe` показывает топик `payments` с 3 партициями
- [ ] `kafka-consumer-groups.sh --describe --group billing` показывает ненулевой LAG после остановки консьюмера
- [ ] `curl -s http://10.10.10.30:8222/jsz | jq '.streams'` → ≥ 1
- [ ] С k8s-w01 доступны порты 5672, 9092, 4222
- [ ] Записи в `stand-log.md` внесены

### Сломай и почини

```text
Пользователь: ivanov (Иван Иванов)
ВМ: WIN11-CL01
Что написал: «С 10 утра новые платежи не появляются в 1С, у всех заказов статус "Ожидание".
              Вчера работало. Очень срочно, закрываем период.»
Срочность: Высокая
```

**Воспроизводим поломку** (безопасно, диск не забиваем — просто поднимаем порог, который брокер считает критическим):

```bash
sudo rabbitmqctl set_disk_free_limit 100GB
```

Через несколько секунд RabbitMQ объявляет disk alarm и **блокирует всех продюсеров**. В веб-интерфейсе наверху появляется красная плашка про disk free limit, а соединения переходят в состояние `blocked`. Сообщения перестают поступать, консьюмер ничего не получает, `messages_ready` не растёт — снаружи выглядит ровно как «интеграция сломалась».

**Диагностика по шагам — строго в этом порядке:**

1. Брокер вообще жив? `systemctl status rabbitmq-server` и `nc -zv 10.10.10.30 5672`. Жив — значит, не «упал».
2. Есть ли алармы? `sudo rabbitmq-diagnostics alarms`. Вот и ответ: `resource alarm`.
3. Кто именно болит — диск или память? `df -h /var/lib/rabbitmq` и `free -m`. Если диск реально полон — чистим логи/старые данные, а не «поднимаем лимит».
4. Смотрим журнал: `journalctl -u rabbitmq-server -n 200 --no-pager | grep -i -E 'alarm|blocked'`.
5. Проверяем соединения: `sudo rabbitmqctl list_connections name user state` → `blocked` подтверждает гипотезу.

**Решение:**

```bash
sudo rabbitmqctl set_disk_free_limit 2GB
sleep 5
sudo rabbitmq-diagnostics alarms           # ожидаем пустой вывод
sudo rabbitmqctl list_connections state    # ожидаем running
```

Продюсеры разблокируются автоматически, накопленные в приложении сообщения уедут в очередь. Если же диск был реально переполнен — сначала освободить место (ротация логов, `rabbitmqctl purge_queue` для мусорных очередей, удаление старых индексов), потом вернуть лимит.

**Ответ пользователю:**

> Иван, добрый день. Причина найдена: сервер очередей уходил в защитный режим из-за нехватки свободного места и перестал принимать новые события от платёжного сервиса. Сейчас ограничение скорректировано, приём сообщений восстановлен. Пожалуйста, проверьте в 1С поступление платежей за последние 2 часа — очередь должна разгрузиться в течение 10–15 минут. Если какие-то заказы останутся в «Ожидании» после 11:30, напишите номера, разберём точечно.

**Закрытие заявки:** тип — «Инфраструктура / Брокер сообщений», причина — «срабатывание resource alarm, блокировка продюсеров», действие — «настройка disk_free_limit, освобождение места», профилактика — «алерт на `rabbitmq_alarms_disk_space_watermark` и на `disk_free < 15%`, отдельный тикет на ротацию логов».

### Что изменилось на стенде

- **UBU01 (10.10.10.30)** — становится хостом брокеров сообщений. Установлен `rabbitmq-server`, включены плагины `rabbitmq_management`, `rabbitmq_prometheus`. RabbitMQ-пользователь `adm.student` (тег administrator), `guest` удалён. Очередь `payments.new` (quorum, durable, TTL 24 ч).
- **UBU01** — контейнер `kafka` (apache/kafka, KRaft, node id 1, слушатель 9092, controller 9093, advertised `10.10.10.30:9092`), топик `payments` (3 партиции, RF=1), консьюмер-группа `billing`.
- **UBU01** — контейнер `nats` (JetStream, 4222/8222, volume `nats-data`), стрим `ORDERS` с субъектом `orders.>`.
- **UBU01** — контейнер `kafka-exporter` (порт 9308).
- **Порты, открытые на UBU01:** 5672, 15672, 15692, 9092, 9308, 4222, 8222, 7777.
- **MON01** — новые таргеты scrape (или Zabbix-шаблоны) для трёх брокеров.
- **Новые файлы:** `~/mq/kafka-compose.yml` на UBU01, запись в `stand-log.md`.

### Откат

- **Полный откат:** выключить ВМ → **VM → Snapshot → Snapshot Manager** → выбрать `before-devops-105` → **Go to** (или **Revert**). Для UBU01, k8s-w01, MON01.
- **Точечный откат без снапшота:**

```bash
sudo rabbitmqctl set_disk_free_limit 2GB
docker rm -f kafka nats kafka-exporter
docker volume rm nats-data
sudo apt purge -y rabbitmq-server && sudo apt autoremove -y
sudo rm -rf /var/lib/rabbitmq /etc/rabbitmq ~/mq
```

Помни: `docker rm -f` с volume удаляет данные JetStream, а `apt purge` — все очереди RabbitMQ. На стенде это допустимо, в проде — только через снапшот/бэкап.

## 6. Ошибки новичков и безопасность

1. **Оставить `guest/guest` и открыть 15672 в сеть.** → Кто угодно в сети получает полный контроль над брокером. → Правильно: удалить `guest`, создать персональные учётки, ограничить доступ файрволом, для прод-контура — TLS на 5671 и отдельный vhost на команду.

2. **Очередь без `durable` и сообщения без `persistent`.** → После рестарта RabbitMQ очередь и сообщения исчезают, «платежи потерялись». → Правильно: durable-очередь + persistent-сообщения (в quorum-очередях персистентность по умолчанию иная — сверься с докой своей версии) + publisher confirms на стороне продюсера.

3. **Kafka с `replication-factor=1` в проде.** → Падение одной ноды = потеря партиции и данных. → Правильно: RF=3, `min.insync.replicas=2`, `acks=all` для критичных топиков.

4. **Нет dead-letter очереди и TTL.** → Одно «ядовитое» сообщение бесконечно переотправляется и блокирует очередь, лаг растёт, никто не понимает почему. → Правильно: `x-delivery-limit` + DLX, отдельный разбор DLQ, алерт на её рост.

5. **Консьюмер без идемпотентности при at-least-once.** → Дубли платежей: один и тот же заказ оплачен дважды в учёте. → Правильно: идемпотентная обработка по бизнес-ключу (`order_id` + уникальный индекс), а не надежда на «exactly-once из коробки».

6. **Мониторинг «процесс запущен» вместо метрик.** → Брокер работает, очередь растёт третий час, узнаёте от бухгалтерии. → Правильно: алерты на `messages_ready`, `messages_unacknowledged`, `consumer lag`, свободное место, `slow_consumers`.

7. **`max.poll.interval.ms` и долгая обработка в Kafka.** → Консьюмер выпадает из группы, начинается бесконечный rebalance, лаг не падает никогда. → Правильно: обработка батчами, увеличение таймаутов осознанно, `max.poll.records` под реальное время обработки `[ПРОВЕРИТЬ дефолты для версии клиента]`.

8. **Слишком большие сообщения (десятки МБ).** → Рост latency, OOM брокера, отказ в приёме по `max_payload`/`message.max.bytes`. → Правильно: передавать в брокере ссылку на объект в S3-совместимом хранилище, а не сам файл.

### Безопасность

В боевой среде **нельзя**: держать брокер в открытом виде в DMZ без TLS и аутентификации; использовать один общий пароль на всех продюсеров (невозможно отозвать у одного); давать консьюмерам права администратора (им хватает read на свой vhost); отключать `publisher confirms` и ack'и «ради скорости» на платёжных потоках; хранить в сообщениях персональные данные и номера карт в открытом виде — это прямое нарушение 152-ФЗ и PCI DSS (маскирование/токенизация на стороне продюсера). Отдельно: с 2025 года требования к защите ПДн ужесточились, а брокер — это ещё одно место, где данные лежат на диске в открытом виде. В корпоративном контуре включай шифрование volume'ов и ограничивай retention.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем Kafka отличается от RabbitMQ и что вы выберете для платёжных событий?**

Ответ: RabbitMQ — брокер очередей с маршрутизацией через exchange'и и удалением сообщения после ack: хорош для сложной маршрутизации, приоритетов, отложенной доставки, RPC-паттернов и задач с небольшим числом consumer'ов. Kafka — распределённый лог с retention и offset'ами: хороша для потоков событий, реплея, нескольких независимых потребителей одного потока и больших объёмов (сотни тысяч сообщений/с). Для платёжных событий, где нужен аудит, повторная обработка и несколько потребителей (биллинг, 1С, антифрод, аналитика), я бы выбрал Kafka с RF=3 и `acks=all`. Если нужен просто надёжный work queue с ретраями и DLQ и объёмы умеренные — RabbitMQ проще в эксплуатации. NATS беру, когда нужна минимальная задержка и лёгкость, плюс JetStream, если нужна персистентность.

*Что хочет услышать интервьюер:* не «Kafka круче», а понимание моделей (очередь vs лог), сценариев и эксплуатационной цены.
*Красный флаг:* «Kafka быстрее, поэтому всегда Kafka» без привязки к задаче.

**Вопрос 2. Что такое consumer lag и как вы его мониторите?**

Ответ: лаг — разница между последним записанным в партицию сообщением (log-end-offset) и последним зафиксированным offset'ом группы. Показывает, насколько консьюмер отстал. Снимаю через `kafka-consumer-groups.sh --describe`, в постоянном режиме — `kafka_exporter`/JMX exporter, метрика вида `kafka_consumergroup_lag`, алерт на «лаг растёт N минут подряд» и на абсолютный порог по критичным топикам. Важно смотреть по партициям, а не только суммарно: суммарный ноль при одной мёртвой партиции — типичная слепая зона. Для RabbitMQ аналог — `messages_ready` и `messages_unacknowledged`.

*Что хочет услышать:* понимание, что лаг — это бизнес-метрика свежести данных, и что мониторить надо по партициям.
*Красный флаг:* «смотрю в интерфейсе, когда жалуются».

**Вопрос 3. Какие гарантии доставки бывают и как сделать at-least-once?**

Ответ: at-most-once (можно потерять), at-least-once (можно получить дубль), exactly-once (дорого и с ограничениями). At-least-once в RabbitMQ: durable-очередь + persistent-сообщения + publisher confirms на продюсере + ручной ack после успешной обработки; в Kafka: `acks=all`, `enable.auto.commit=false`, коммит offset'а **после** обработки. Since дубли неизбежны, обязательна идемпотентность консьюмера по бизнес-ключу. Exactly-once — через транзакционный API Kafka или дедупликацию JetStream, но обычно проще сделать идемпотентную обработку.

*Что хочет услышать:* связку «гарантия доставки = конфигурация продюсера + конфигурация консьюмера + идемпотентность».
*Красный флаг:* «брокер сам гарантирует, что сообщение придёт один раз».

**Вопрос 4. Брокер жив, но сообщения перестали поступать. Ваши действия?**

Ответ: иду по слоям. Сеть: доступен ли порт (`nc -zv`). Процесс: `systemctl status`, не рестартовал ли. Ресурсы: `rabbitmq-diagnostics alarms`, `df -h`, `free -m` — чаще всего это disk/memory watermark и блокировка продюсеров. Соединения: `rabbitmqctl list_connections state` — `blocked`. Очереди: `list_queues name messages messages_ready messages_unacknowledged consumers` — если `unacknowledged` стоит на месте, консьюмер завис и не отвечает ack'ом (в RabbitMQ это упирается в `consumer_timeout`). Логи: `journalctl` / `docker logs`. В Kafka параллельно смотрю lag по партициям и не идёт ли rebalance. Дальше — гипотеза, проверка, фикс, и обязательный ответ пользователю с оценкой времени восстановления.

*Что хочет услышать:* методичный порядок диагностики от L1 к L7 и знание типовых причин.
*Красный флаг:* «перезапущу брокер и посмотрю» первым действием.

**Вопрос 5. Как эксплуатировать брокер в Kubernetes?**

Ответ: оператором, а не голыми манифестами. Strimzi Kafka Operator для Kafka, RabbitMQ Cluster Kubernetes Operator для RabbitMQ, официальный Helm-чарт `nats` + NATS Operator для NATS `[ПРОВЕРИТЬ названия чартов в актуальной доке]`. Оператор даёт декларативные CRD (Kafka, KafkaTopic, RabbitmqCluster), сам делает StatefulSet с PVC, rolling-обновления, репликацию, Service и Prometheus-метрики. Обязательно: PVC на быстром диске, PodDisruptionBudget, anti-affinity по нодам, resource requests/limits, NetworkPolicy, отдельные креды в Secret (в проде — из Vault), и алерты на lag/ready/disk. Проверять всё на staging, потому что изменение размера PVC и числа партиций — не всегда онлайн-операции.

*Что хочет услышать:* «оператор + декларативные CRD + storage + наблюдаемость», а не «kubectl apply из чужой статьи».
*Красный флаг:* «разверну StatefulSet вручную, оператор не нужен».

## 8. Источники (официальная документация)

- **kafka.apache.org** — раздел *Documentation → Design / Implementation* (модель лога, партиции, гарантии доставки) и *Operations* (репликация, rebalance, тюнинг). Поисковая фраза: `kafka design consumer group rebalance documentation`.
- **kafka.apache.org** — раздел *Quickstart* и *KRaft*: поиск `kafka kraft quickstart`.
- **rabbitmq.com** — разделы *Documentation → Tutorials*, *Queues*, *Quorum Queues*, *Monitoring* и *Prometheus*: поиск `rabbitmq quorum queues documentation`, `rabbitmq prometheus plugin`.
- **rabbitmq.com** — *Resource Alarms* (disk/memory watermark, блокировка продюсеров): поиск `rabbitmq resource alarms memory disk`.
- **docs.nats.io** — *JetStream* (streams, consumers, дедупликация) и *Monitoring* (`/varz`, `/jsz`): поиск `nats jetstream documentation`, `nats monitoring endpoints`.
- **strimzi.io** — оператор Kafka для Kubernetes: поиск `strimzi kafka operator quickstart`.
- **github.com** — репозитории `nats-io/natscli`, `nats-io/prometheus-nats-exporter`, `danielqsj/kafka_exporter`: смотреть README и релизы (там актуальные флаги и порты по умолчанию).
- **hub.docker.com** — официальные образы `apache/kafka`, `library/rabbitmq`, `library/nats`: смотреть теги и переменные окружения (в тексте они помечены `[ПРОВЕРИТЬ]` — сверь с актуальным тегом).
- **prometheus.io** — *Configuration → scrape_config*: поиск `prometheus scrape_config static_configs`.
- **learn.microsoft.com** — если брокер разворачивается в Azure (Event Hubs как Kafka-совместимый эндпоинт — тема для облачных глав): поиск `azure event hubs kafka protocol` `[ПРОВЕРИТЬ актуальность раздела]`.
