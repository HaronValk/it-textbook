# Базы данных в эксплуатации: PostgreSQL (Patroni, pgBouncer), Redis, ClickHouse, MongoDB

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4471**
- **Пользователь:** sidorov (склад)
- **ВМ:** — (работал через веб-кабинет отгрузок в браузере)
- **Что написал:** «Кабинет отгрузок с утра висит. Жмёшь «Сохранить отгрузку» — крутится минуту, потом «Ошибка 502». Началось вчера к вечеру, сегодня у всей смены. Склад стоит.»
- **Срочность:** высокая, есть простой бизнес-процесса.

Для L1 это «перезапусти сервис». Для DevOps это первый сигнал, что база упёрлась в лимит соединений, реплика отстала или диск на узле с PostgreSQL закончился. Базы — единственный компонент, где «перезапустить» почти всегда означает «потерять данные», поэтому их эксплуатация крутится вокруг трёх вещей: **резервные копии, которые реально восстанавливаются**, **репликация, которая реально переключается**, и **ограничители, которые не дают одному кривому приложению положить весь сервис**.

После этой главы ты будешь уметь: читать состояние PostgreSQL и его реплик, настраивать пул соединений pgBouncer, понимать Patroni как «бригадира смены», поднимать PostgreSQL-кластер оператором в Kubernetes с бэкапом в S3, делать и проверять бэкапы Redis, ClickHouse и MongoDB, и объяснять на собеседовании разницу между RPO и RTO. Опираемся на машины стенда: `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63), `UBU01` (10.10.10.30) как рабочая станция с `kubectl` и клиентами, `MON01` (10.10.10.40) как площадка для проверки скачанного бэкапа.

## 2. Теория «под капотом»

**Четыре разных зверя.** PostgreSQL — OLTP: транзакции, ACID, строки. Redis — in-memory хранилище: кэш, сессии, очереди, счётчики. ClickHouse — OLAP: колоночная аналитика на миллиарды строк. MongoDB — документная БД: JSON-подобные документы без жёсткой схемы. Эксплуатация у них разная, а принципы одни.

**Бэкап и две цифры.** RPO (Recovery Point Objective) — сколько данных мы готовы потерять, RTO (Recovery Time Objective) — как быстро поднимемся. Логический бэкап (`pg_dump`, `mongodump`) даёт консистентный снимок и переносим между версиями, но восстанавливается долго. Физический (`pg_basebackup`, снапшот диска) — быстро, но только на ту же версию и архитектуру. PITR (point-in-time recovery) — «отмотать на 14:03:20» — работает за счёт непрерывной архивации журнала транзакций (WAL) плюс базовой копии.

**Репликация PostgreSQL** — потоковая, на уровне WAL: праймари отправляет журнал, реплика применяет. Асинхронная — быстро, но при падении праймари теряются последние транзакции. Синхронная — праймари ждёт подтверждения от реплики (потерять нельзя, но при медленной реплике встанет запись). Отставание видно в `pg_stat_replication` (`replay_lag`).

**Split-brain и Patroni.** Если праймари «завис» на сети и реплика стала новой, а старый вернулся — получаем двух праймари с разными данными. Patroni решает это через DCS (Distributed Configuration Store — etcd, Consul или ZooKeeper): лидерство держится на lease с TTL, продлевается только при наличии кворума; потерял кворум — добровольно уходит в read-only. Дополнительно — `pg_rewind` для возврата старого узла и watchdog для «fencing» (гарантированного выключения).

**Пул соединений.** В PostgreSQL каждое соединение — отдельный процесс ОС, поэтому 1000 «спящих» коннектов от приложения съедают память и CPU. pgBouncer держит свой пул и мультиплексирует клиентов: `session` (один клиент = одно серверное соединение до отключения), `transaction` (соединение отдаётся на время транзакции — лучший вариант для обычных CRUD), `statement` (редко).

**Базы в Kubernetes.** Под с базой — StatefulSet с постоянным PVC и стабильным именем; узлы должны быть разнесены (podAntiAffinity), при дренаже узла нужен PodDisruptionBudget, а бэкап нельзя писать в тот же PVC. Управляют этим **оператором** — контроллером, который читает кастомные ресурсы (CRD) и приводит реальность к описанному состоянию: CloudNativePG (PostgreSQL), Altinity/Presslabs (ClickHouse), Percona/MongoDB Community (MongoDB), Spotahome (Redis).

**Порты, которые надо помнить:** PostgreSQL 5432, pgBouncer 6432, Patroni REST API 8008, etcd 2379, Redis 6379, MongoDB 27017, ClickHouse HTTP 8123 и native 9000, MinIO S3 9000/9001.

```text
        ┌──────────────────────── k8s (10.10.10.61–63) ────────────────────────┐
        │  ns: db                                                              │
        │                                                                      │
        │  pod приложения ──► Svc pg-vektor-pooler-rw:5432                     │
        │                              │                                       │
        │                              ▼                                       │
        │                    Pooler (pgBouncer) x2  :6432  :6432               │
        │                              │                                       │
        │                              ▼                                       │
        │              pg-vektor-1 (primary, RW) ◄── WAL ──┐                   │
        │              pg-vektor-2 (replica, RO) ◄─────────┼── streaming       │
        │              pg-vektor-3 (replica, RO) ◄─────────┘                   │
        │                       │                                              │
        │                       ▼ archive_command / barman-cloud               │
        │              S3: MinIO (PVC 10Gi)  s3://backups/pg-vektor/           │
        └──────────────────────────────────────────────────────────────────────┘
   VM-вариант без k8s:  app ─► HAProxy :5000/:5001 ─► Patroni+PostgreSQL x3
                                                 └──► etcd x3 (кворум)
```

## 3. Аналогия из жизни

База — это ресторан. **PostgreSQL** — основная кухня: повара (процессы) готовят заказы строго по журналу заказов (WAL), и по этому журналу можно восстановить весь вечер. **pgBouncer** — метрдотель: в зал пускает сто гостей, а на кухню отправляет только двадцать поваров, потому что больше физически не помещается. **Patroni** — бригадир смены с рацией и жёстким правилом: «шеф-повар тот, у кого в руках рация с зарядом; потерял рацию — иди в подсобку и не трогай плиту». **Реплика** — второй повар, который читает тот же журнал заказов с небольшой задержкой и может подхватить смену. **Redis** — витрина с уже готовыми блюдами: отдаёт мгновенно, но если её опрокинуть, горячее придётся готовить заново. **ClickHouse** — бухгалтер в подсобке, который за ночь считает выручку по всем чекам за год. **MongoDB** — картотека с папками разного формата, где каждая папка сама решает, какие в ней поля.

Аналогия ломается там, где начинается PITR: ресторан нельзя «отмотать» на 14:03:20 и подать гостям те же блюда заново — а базу можно, если журнал цел.

## 4. Практика и команды

**1. Кто я и кто вокруг (PostgreSQL).**
```bash
sudo -u postgres psql -c "select version();"
sudo -u postgres psql -c "select client_addr, state, count(*) from pg_stat_activity group by 1,2 order by 3 desc;"
```
`pg_stat_activity` — все соединения. Смотрим колонку `state`: сотни `idle` с одного адреса — приложение не закрывает коннекты, идём в пул.

**2. Состояние репликации (на праймари).**
```bash
sudo -u postgres psql -c "select client_addr, state, sync_state, replay_lag from pg_stat_replication;"
```
`replay_lag` — отставание реплики. Растёт → сеть, диск или реплика не успевает применять WAL.

**3. Физический бэкап.**
```bash
pg_basebackup -h 10.10.10.30 -U replicator -D /backup/base -Ft -z -X stream -P
```
`-Ft` — tar, `-z` — gzip, `-X stream` — тянуть WAL параллельно, `-P` — прогресс. Это база будущего PITR.

**4. Логический бэкап и восстановление.**
```bash
pg_dump -Fc -d app -f /backup/app_$(date +%F).dump
pg_restore -d app_restore --clean --if-exists /backup/app_2026-01-01.dump
```
`-Fc` — custom format (сжатый, восстанавливается параллельно через `-j`).

**5. Архивация WAL (в `postgresql.conf`).**
```text
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /var/lib/postgresql/wal-archive/%f && cp %p /var/lib/postgresql/wal-archive/%f'
```
`%p` — путь к файлу, `%f` — имя. `test ! -f` защищает от перезаписи. В бою `archive_command` отправляет WAL в S3 (`barman-cloud-wal-archive`, `pgBackRest`).

**6. Пул соединений pgBouncer (`pgbouncer.ini`).**
```ini
[databases]
app = host=10.10.10.30 port=5432 dbname=app

[pgbouncer]
listen_port = 6432
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 25
```
```bash
psql -h 127.0.0.1 -p 6432 -U pgbouncer_admin pgbouncer -c "SHOW POOLS;"
```
В `SHOW POOLS` смотрим `cl_waiting` (клиенты ждут свободное серверное соединение) и `sv_active`. Растущий `cl_waiting` — пул мал или запросы долгие.

**7. Patroni.**
```bash
patronictl -c /etc/patroni/config.yml list
patronictl -c /etc/patroni/config.yml switchover --master pg-vektor-1 --candidate pg-vektor-2 --force
```
`list` показывает Cluster/Member/Host/Role/State/Lag. `switchover` — плановое переключение (в новых версиях флаг `--master` переименован в `--primary` `[ПРОВЕРИТЬ]`).

**8. Redis: репликация и бэкап.**
```bash
redis-cli INFO replication
redis-cli BGSAVE && redis-cli LASTSAVE
redis-cli --rdb /backup/redis-$(date +%F).rdb
redis-cli REPLICAOF 10.10.10.30 6379
```
`INFO replication` — роль, `connected_slaves`, `master_link_status:up/down`. `--rdb` безопасно копирует дамп по сети. `REPLICAOF` делает текущий узел репликой (старое имя команды — `SLAVEOF`).

**9. Redis: защита от OOM.**
```bash
redis-cli CONFIG SET maxmemory 512mb
redis-cli CONFIG SET maxmemory-policy allkeys-lru
redis-cli CONFIG REWRITE
```
Без `maxmemory` Redis съест всю RAM и попадёт под OOM-killer. `CONFIG REWRITE` фиксирует значения в файле.

**10. ClickHouse: реплики и куски данных.**
```bash
clickhouse-client --query "SELECT database, table, is_leader, total_replicas, active_replicas, absolute_delay FROM system.replicas"
clickhouse-client --query "SELECT table, count() parts, sum(rows) rows FROM system.parts WHERE active GROUP BY table"
```
`absolute_delay` > 0 — реплика отстаёт. Много мелких `parts` — проблема с мерджами.

**11. ClickHouse: бэкап в S3 (с версии 22.8 есть `BACKUP`).**
```bash
clickhouse-client --query "BACKUP TABLE app.events TO S3('http://minio.db.svc:9000/backups/ch/events.zip','minio','Lab#2026-vektor')"
```
Альтернатива для больших объёмов — утилита `clickhouse-backup` (Altinity) `[ПРОВЕРИТЬ актуальность репозитория]`.

**12. MongoDB: набор реплик.**
```bash
mongosh --host rs0/10.10.10.61:27017 -u admin -p --authenticationDatabase admin
rs.status().members.map(m => ({name: m.name, state: m.stateStr, lag: m.optimeDate}))
rs.stepDown(60)
```
`PRIMARY`/`SECONDARY` — роли, `lag` — отставание. `stepDown` — корректная передача лидерства (проверка, что приложение переживает смену).

**13. MongoDB: бэкап с оплогом.**
```bash
mongodump --uri="mongodb://backup:***@10.10.10.61:27017/?replicaSet=rs0&authSource=admin" --gzip --archive=/backup/mongo-$(date +%F).gz --oplog
mongorestore --gzip --archive=/backup/mongo-2026-01-01.gz --oplogReplay
```
`--oplog` снимает согласованный срез на момент конца дампа — это даёт PITR-совместимость.

**14. Оператор в Kubernetes.**
```bash
kubectl get cluster,pods,pvc -n db
kubectl cnpg status pg-vektor
kubectl cnpg backup pg-vektor
kubectl get backup,scheduledbackup -n db
```
`kubectl cnpg` — плагин (`kubectl krew install cnpg`). `status` показывает роли, лаг и последний бэкап.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
Нужны `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63) — рабочий кластер Kubernetes из предыдущих глав трека, минимум по 2 vCPU / 4 ГБ RAM и 20 ГБ диска. `UBU01` (10.10.10.30) — с установленными `kubectl`, `postgresql-client`, `redis-tools`. `MON01` (10.10.10.40) — понадобится для проверки скачанного бэкапа. Windows-часть стенда не нужна.

Если кластер ещё не поднят — вернись к главе, где он ставился. Минимальная замена: k3s на одной ВМ `UBU01`, но тогда разнеси pods по anti-affinity не получится — отметь это в `stand-log.md`.

Проверь StorageClass: `kubectl get sc`. Если пусто — поставь `local-path-provisioner` (Rancher) или создай статические PV на hostPath `[ПРОВЕРИТЬ имя чарта/манифеста под свою версию]`.

### Снапшот
В VMware Workstation на `k8s-cp01`, `k8s-w01`, `k8s-w02` и `UBU01`:
```text
VM → Snapshot → Take Snapshot… → имя: before-devops-106
```
Снапшот делаем на **выключенных** или хотя бы на простаивающих ВМ, чтобы диск был консистентен.

### Шаги

1. **`k8s-cp01`.** `kubectl create namespace db` → `namespace/db created`.
2. **`k8s-cp01`.** Ставим оператор CloudNativePG:
   ```bash
   kubectl apply --server-side -f https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.25.1/cnpg-1.25.1.yaml
   kubectl -n cnpg-system get pods
   ```
   `[ПРОВЕРИТЬ актуальную версию на github.com/cloudnative-pg/cloudnative-pg/releases]`. Ожидаем `Running` у пода `cnpg-controller-manager`.
3. **`k8s-cp01`.** Поднимаем учебный S3 (MinIO) в namespace `db`: секрет с логином/паролем, Deployment `minio/minio` с аргументом `server /data` и PVC 10Gi, Service `minio` на порту 9000. Ждём `kubectl -n db get pods` → `minio-... 1/1 Running`.
4. **`k8s-cp01`.** Создаём бакет:
   ```bash
   kubectl -n db run mc --rm -it --image=minio/mc --restart=Never -- sh
   # внутри: mc alias set loc http://minio:9000 minio 'Lab#2026-vektor'
   #         mc mb loc/backups && mc ls loc
   ```
   Ожидаем пустой список или созданный `backups`.
5. **`k8s-cp01`.** Манифест `pg-vektor.yaml`: `kind: Cluster` (apiVersion `postgresql.cnpg.io/v1`), `instances: 3`, `imageName` с образом PostgreSQL 16 `[ПРОВЕРИТЬ тег в документации CNPG]`, `storage.size: 5Gi`, `bootstrap.initdb` с базой `app` и владельцем `app`, блок `backup.barmanObjectStore` с `destinationPath: s3://backups/pg-vektor`, `endpointURL: http://minio.db.svc:9000`, креды из Secret, `wal.compression: gzip`. Плюс `affinity.podAntiAffinityType: required`. Применяем: `kubectl apply -f pg-vektor.yaml`.
6. **`k8s-cp01`.** Ждём готовности и смотрим статус:
   ```bash
   kubectl cnpg status pg-vektor
   ```
   Ожидаем: три инстанса, один `Primary`, два `Replica`, `Replication Lag` около нуля.
7. **`k8s-cp01`.** Добавляем пул:
   ```yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Pooler
   metadata:
     name: pg-vektor-pooler-rw
     namespace: db
   spec:
     cluster: {name: pg-vektor}
     instances: 2
     type: rw
     pgbouncer:
       poolMode: transaction
       parameters:
         max_client_conn: "1000"
         default_pool_size: "25"
   ```
   `kubectl apply -f pooler.yaml` → `kubectl -n db get pooler` показывает `pg-vektor-pooler-rw`.
8. **`k8s-cp01`.** Проверяем подключение через пул (из пода-клиента):
   ```bash
   kubectl -n db run psql --rm -it --image=postgres:16 --restart=Never -- \
     psql "host=pg-vektor-pooler-rw-rw.db.svc.cluster.local user=app dbname=app" -c "select inet_server_addr(), version();"
   ```
   Ожидаем строку версии и внутренний адрес пода с базой. `[ПРОВЕРИТЬ точное имя сервиса пула в своей версии оператора]`.
9. **`k8s-cp01`.** Создаём тестовые данные и бэкап:
   ```bash
   kubectl cnpg backup pg-vektor
   kubectl -n db get backup
   kubectl -n db logs job/backup-pg-vektor-... --tail=30
   ```
   Ожидаем статус `completed` и объекты в MinIO (`mc ls loc/backups/pg-vektor`).
10. **`k8s-cp01`.** Плановый бэкап: `ScheduledBackup` с `schedule: "0 2 * * *"` и `backupOwnerReference: cluster`. `kubectl -n db get scheduledbackup` → одна запись.
11. **`UBU01`.** Тренируем восстановление (главный шаг главы): создаём кластер-«копию» из бэкапа — `bootstrap.recovery` с `externalClusters` (barmanObjectStore, `serverName: pg-vektor`), имя `pg-vektor-restore`. Ждём `kubectl cnpg status pg-vektor-restore` → `Primary` поднялся. `kubectl -n db exec -it pg-vektor-restore-1 -- psql -U postgres -d app -c "select count(*) from your_table;"` → данные на месте.
12. **`k8s-cp01`.** Redis для кэша: StatefulSet `cache/redis:7` `[ПРОВЕРИТЬ тег]` с `appendonly yes`, PVC 2Gi, Service `redis` :6379. Проверка из `UBU01`: `kubectl -n db port-forward svc/redis 6379:6379` и `redis-cli INFO replication` → `role:master`. Затем `redis-cli --rdb /backup/redis.rdb` на `UBU01`.
13. **`UBU01`.** Скачиваем бэкап PostgreSQL из MinIO на `MON01` и проверяем, что архив не битый: `pg_restore -l` по дампу либо распаковка tar-архива `base.tar.gz` и `pg_verifybackup`. Это и есть «бэкап проверен».
14. **`MON01`.** Фиксируем результат в `stand-log.md`: namespace `db`, оператор, кластер, пул, MinIO, Redis.

### Проверка
- [ ] `kubectl -n cnpg-system get pods` — оператор в `Running`.
- [ ] `kubectl cnpg status pg-vektor` — 3 инстанса, 1 primary, 2 replica, лаг < 1 с.
- [ ] `kubectl -n db get pods -o wide` — поды баз на разных узлах (anti-affinity работает).
- [ ] `kubectl -n db get pooler` — пул создан, `max_client_conn` = 1000.
- [ ] `psql` через сервис пула возвращает версию и отвечает на `select 1`.
- [ ] `kubectl -n db get backup` — бэкап `completed`.
- [ ] В MinIO есть каталог `pg-vektor/base` и файлы WAL.
- [ ] Кластер `pg-vektor-restore` поднялся и данные видны — восстановление реально работает.
- [ ] `redis-cli INFO replication` отвечает, `--rdb` создал файл ненулевого размера.
- [ ] Всё записано в `stand-log.md`.

### Сломай и почини
**Заявка:** sidorov / ВМ: — (веб-кабинет) / «Кабинет отгрузок висит, потом 502, у всей смены. Склад стоит!» / Срочность: высокая.

**Как воспроизвести поломку.** С эмулятора нагрузки открываем много «спящих» соединений напрямую в обход пула:
```bash
kubectl -n db run pgstress --rm -it --image=postgres:16 --restart=Never -- bash
# внутри:
for i in $(seq 1 120); do psql "host=pg-vektor-rw.db.svc.cluster.local user=app dbname=app" -c "begin; select 1; select pg_sleep(600);" & done
```
Приложение ходит через пул, но часть кода — напрямую в праймари. Пул исчерпан, кабинет получает таймауты.

**Диагностика по порядку:**
1. `kubectl cnpg status pg-vektor` — смотрим число соединений и не растёт ли лаг.
2. `kubectl -n db exec -it pg-vektor-1 -- psql -U postgres -c "select state, count(*) from pg_stat_activity group by 1 order by 2 desc;"` — видим десятки `idle in transaction`.
3. `... -c "select client_addr, usename, count(*) from pg_stat_activity group by 1,2 order by 3 desc limit 5;"` — источник один (под `pgstress`).
4. `kubectl -n db get pooler` и `SHOW POOLS` через пул — `cl_waiting` > 0, `sv_active` у предела.
5. `kubectl -n db exec -it pg-vektor-1 -- psql -U postgres -c "show max_connections;"` — не упёрлись ли в общий лимит.

**Решение.** Убить зависшие сессии и включить ограничители:
```sql
select pg_terminate_backend(pid) from pg_stat_activity
where state = 'idle in transaction' and state_change < now() - interval '5 minutes';
alter system set idle_in_transaction_session_timeout = '60s';
alter system set statement_timeout = '30s';
select pg_reload_conf();
```
Затем увеличить пул: `kubectl -n db patch pooler pg-vektor-pooler-rw --type merge -p '{"spec":{"pgbouncer":{"parameters":{"default_pool_size":"50"}}}}'` `[ПРОВЕРИТЬ путь к полю в своей версии]`. Удалить `pgstress`.

**Ответ пользователю:** «Сервис восстановлен. Причина — исчерпание пула соединений базой: часть запросов держала транзакции открытыми. Убил зависшие сессии, увеличил пул и включил таймауты (60 с на простаивающую транзакцию). Если снова увидите «крутится и 502» — сразу пишите, время фиксируйте».

**Закрытие:** тип «инцидент», причина «exhaustion of DB connections pool», действие «timeouts + pool size», запись в `stand-log.md`.

### Что изменилось на стенде
- Namespace `db` на кластере k8s (10.10.10.61–63).
- Namespace `cnpg-system`, оператор CloudNativePG.
- Кластер PostgreSQL `pg-vektor`: 3 инстанса, сервисы `pg-vektor-rw` / `pg-vektor-ro` / `pg-vektor-r` (имена уточнить в статусе), PVC по 5 Gi на узлах `k8s-w01`/`k8s-w02`.
- Pooler `pg-vektor-pooler-rw` (pgBouncer, transaction mode).
- MinIO в namespace `db`: Service `minio:9000`, PVC 10 Gi, бакет `backups`, префикс `pg-vektor/`.
- `ScheduledBackup` ежедневно в 02:00.
- Тестовый кластер `pg-vektor-restore` (можно удалить после проверки).
- Redis StatefulSet `redis` в namespace `db`, AOF включён, PVC 2 Gi.
- Учётные данные: `app` (владелец базы `app`), `replicator` (репликация), `minio` — пароль стенда `Lab#2026-vektor`, только для лаборатории.

### Откат
1. `kubectl delete cluster pg-vektor pg-vektor-restore -n db` и `kubectl delete pooler --all -n db` (дождаться удаления PVC или удалить их вручную).
2. `kubectl delete ns db` — сносит MinIO, Redis и все PVC.
3. `kubectl delete -f cnpg-1.25.1.yaml` и при необходимости `kubectl delete crd` для ресурсов CNPG `[ПРОВЕРИТЬ, что CRD не используются другими кластерами]`.
4. Если кластер поехал — выключить `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01` и восстановить снапшот `before-devops-106` в VMware Workstation.

## 6. Ошибки новичков и безопасность

1. **«Бэкап есть — значит всё хорошо».** → Бэкап, который ни разу не восстанавливали, — это не бэкап, а надежда. → Раз в месяц (и в этой лабе) делать реальный restore в отдельный кластер.
2. **`pg_dump` в cron без проверки кода возврата и мониторинга.** → Месяцами пишутся пустые/битые файлы. → `set -euo pipefail`, проверка размера, алерт в Zabbix на `MON01` при неудаче.
3. **Бэкап на тот же диск/в тот же PVC.** → Потеря узла = потеря и данных, и копий. → Отдельный S3/лента/другая площадка, офсайт-копия.
4. **`pool_mode = transaction` и «сломанные» фичи.** → В transaction-режиме сессионные вещи (advisory locks, `SET`, временные таблицы, `LISTEN/NOTIFY`) ведут себя иначе, чем ожидает разработчик `[ПРОВЕРИТЬ для вашей версии pgBouncer и prepared statements]`. → Согласовать режим с разработкой, для таких задач — отдельная база пула в `session`.
5. **Отключили синхронную репликацию «чтобы быстрее».** → При аварии праймари теряются последние транзакции, а бизнес узнаёт об этом в отчёте. → Явно фиксировать `synchronous_commit` и число синхронных реплик исходя из RPO.
6. **Redis без `maxmemory`.** → OOM-killer убивает процесс, кэш теряется, сессии пользователей падают. → `maxmemory` + политика вытеснения + мониторинг `used_memory`.
7. **ClickHouse: лавина мелких INSERT и мутаций.** → Фон сливает куски, диск и CPU в потолок. → Батчинг вставок, `SELECT ... FROM system.mutations WHERE is_done = 0`, лимиты на `ALTER DELETE/UPDATE`.
8. **MongoDB без keyfile/авторизации.** → Любой в кластере читает и меняет всё. → `security.keyFile`, `authorization: enabled`, отдельные пользователи на чтение/запись, `mongodump` под backup-пользователем с ролью `backup`.

**Безопасность.** В лаборатории пароль `Lab#2026-vektor` лежит в манифестах и в истории команд — в бою так нельзя: секреты только через Secret + внешний vault (в РФ-стеке — Vault-совместимые решения или штатный секрет-стор облака), TLS на 5432/6379/27017 (`sslmode=verify-full`, `scram-sha-256`), запрет прямого доступа приложений в праймари в обход пула, аудит `pg_audit`. Если в базе персональные данные — включаются требования 152-ФЗ (см. урок 101): ограничение доступа, журналирование, шифрование на диске, регламент восстановления. Открывать 5432/27017 в интернет нельзя ни на минуту — это самый частый сценарий шифровальщиков.

**Лицензии и импортозамещение (одной строкой).** PostgreSQL — свободная лицензия, российские сборки (Postgres Pro, РЕД БД, Tantor) легальны и сертифицируемы; MongoDB — SSPL (не OSI), в 152-ФЗ-контурах чаще берут PostgreSQL/FerretDB или российские СУБД; Redis в 2024 сменил лицензию, свободная альтернатива — Valkey (BSD) `[ПРОВЕРИТЬ актуальный статус лицензий на момент чтения]`; ClickHouse — Apache 2.0.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем логический бэкап отличается от физического и когда вы выберете какой?**
Ответ: логический (`pg_dump`, `mongodump`) — это набор SQL/BSON-объектов, даёт переносимость между версиями, точечное восстановление таблицы, но долгий на больших базах и не даёт PITR. Физический (`pg_basebackup`, снапшот тома) — побайтовая копия кластера плюс архив WAL: быстрое восстановление и PITR, но требует совпадения версии и платформы. В работе обычно физический + WAL как основа восстановления, логический — для миграций и точечных вытаскиваний.
*Интервьюер хочет услышать:* RPO/RTO, PITR, понимание цены каждого варианта.
*Красный флаг:* «делаем pg_dump раз в сутки, этого достаточно» без упоминания RPO.

**Вопрос 2. Зачем Patroni нужен DCS и что будет при потере кворума?**
Ответ: DCS (etcd/Consul/ZooKeeper) хранит лидерский ключ с TTL. Patroni продлевает lease, только пока у него есть кворум DCS; потерял связь — не может продлить — перестаёт быть лидером и переводит PostgreSQL в read-only. Это защита от split-brain. Плюс `pg_rewind` для возврата отставшего узла, watchdog для fencing и настройка `synchronous_mode` для синхронной репликации.
*Интервьюер хочет услышать:* понимание кворума, lease/TTL, split-brain и fencing.
*Красный флаг:* «Patroni сам выбирает мастера по heartbeat между нодами» — это как раз путь к двум праймари.

**Вопрос 3. Что даёт pgBouncer и какие подводные камни у transaction-режима?**
Ответ: мультиплексирование клиентских соединений в пул серверных — экономия памяти и защита от «1000 коннектов»; `max_client_conn` и `default_pool_size` развязывают клиентов и бэкенд. В transaction-режиме серверное соединение возвращается в пул после COMMIT, поэтому сессионное состояние (advisory locks, `SET`, temp tables, prepared statements в старых версиях) может «теряться» или протекать между клиентами. Решение — session-пул для таких приложений или настройка `server_reset_query` / prepared statements в новых версиях `[ПРОВЕРИТЬ]`.
*Интервьюер хочет услышать:* конкретику про режимы и осознанные границы.
*Красный флаг:* «pgBouncer ускоряет запросы» — он не ускоряет запросы, он экономит соединения.

**Вопрос 4. Можно ли держать продовую БД в Kubernetes?**
Ответ: да, но с оговорками: StatefulSet + PVC с быстрым локальным SSD, обязательный anti-affinity, PodDisruptionBudget, оператор (CloudNativePG/Percona/Altinity) для фейловера и бэкапов, бэкап — вне кластера (S3), ресурсные лимиты, понимание, что при потере узла с local-path данные уедут вместе с ним. Для небольших и средних нагрузок это нормально; для очень больших — часто ВМ/выделенные серверы.
*Интервьюер хочет услышать:* StatefulSet, PVC, operator, anti-affinity, бэкап вне кластера.
*Красный флаг:* «в k8s базы нельзя» или «PVC = бэкап».

**Вопрос 5. Как вы поймёте, что реплика не отстала, а бэкап пригоден?**
Ответ: лаг — `pg_stat_replication.replay_lag` (PostgreSQL), `system.replicas.absolute_delay` (ClickHouse), `rs.status()` optime lag (MongoDB), `INFO replication` (Redis); алерт на лаг и на «реплика отключена». Пригодность бэкапа — регламент restore-дриллов: поднять копию в отдельном namespace/хосте, сверить контрольные суммы/количество строк, зафиксировать время восстановления и сравнить с RTO.
*Интервьюер хочет услышать:* метрики + регулярные учения по восстановлению.
*Красный флаг:* «смотрю, что файл создался».

## 8. Источники (официальная документация)

- **postgresql.org** — разделы «Backup and Restore», «High Availability, Load Balancing, and Replication», «Continuous Archiving and Point-in-Time Recovery (PITR)», «Monitoring Database Activity». Поиск: `postgresql wal archive pitr documentation`.
- **postgresql.org** — справочник функций: `pg_stat_replication`, `pg_stat_activity`, `pg_terminate_backend`. Поиск: `postgresql 16 monitoring stats views`.
- **pgbouncer.org** — «Configuration» (pool_mode, max_client_conn, default_pool_size), «Commands» (SHOW POOLS). Поиск: `pgbouncer configuration pool_mode`.
- **patroni.readthedocs.io** — «Settings», «patronictl», «Replication modes», «Watchdog». Поиск: `patroni patronictl switchover dcs`.
- **cloudnative-pg.io** — «Installation», «Cluster resource», «Pooler», «Backup» (barmanObjectStore), «Recovery». Поиск: `cloudnative-pg pooler pgbouncer backup s3`. Версии и точные имена полей меняются — сверять с документацией своей версии `[ПРОВЕРИТЬ]`.
- **kubernetes.io** — «StatefulSets», «Persistent Volumes», «Operator pattern», «Pod Disruption Budget». Поиск: `kubernetes statefulset operator pattern`.
- **redis.io** — «Persistence» (RDB/AOF), «Replication», «Memory optimization»; альтернатива **valkey.io** — те же разделы. Поиск: `redis persistence rdb aof replication`.
- **clickhouse.com/docs** — «Backup and restore» (`BACKUP`/`RESTORE`), «Replication», «system.replicas», «system.parts». Поиск: `clickhouse backup to s3 system replicas`.
- **mongodb.com/docs** — «Replication», «Back Up and Restore with MongoDB Tools», «Replica Set Deployment». Поиск: `mongodb backup mongodump oplog replica set`.
- **github.com** — репозитории операторов: `cloudnative-pg/cloudnative-pg`, `Altinity/clickhouse-operator`, `mongodb/mongodb-kubernetes-operator`, `spotahome/redis-operator`, `Percona/percona-server-mongodb-operator`. Поиск: `cloudnative-pg github releases`.

Всё, что касается конкретных версий, имён чартов и точных полей CRD, помечено `[ПРОВЕРИТЬ]` — сверяйся с документацией на день выполнения лабораторной.
