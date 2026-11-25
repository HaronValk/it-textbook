# Базы данных для сисадмина: PostgreSQL/MySQL — репликация, бэкапы, мониторинг, тюнинг

## 1. Введение (зачем это нужно в реальной работе)

Тикет в 9:40 утра:

> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01 / **Что написал:** «1С не открывается, пишет "Не удалось подключиться к серверу 1С:Предприятие". У нас закрытие месяца, счёт стоит!» / **Срочность:** Высокая

Клиент 1С ломается редко — ломается то, что под ним. А под ним почти всегда СУБД (система управления базами данных): PostgreSQL, MS SQL или MySQL/MariaDB. Сисадмин L1/L2 обычно «не трогает базу», но именно он первым получает заявку «ничего не работает», именно он первым видит, что диск на DB-сервере забит на 100 %, и именно ему в субботу звонить, если реплика отстала на два часа.

После этой главы ты должен уметь: поднять PostgreSQL, настроить потоковую репликацию, сделать и **проверить** бэкап (а не просто «команда отработала»), подключить СУБД к Zabbix, прочитать план запроса и объяснить разработчику, почему отчёт летает или тормозит. И знать, где в этом всём место российским реалиям: MS SQL официально не продаётся в РФ с 2022 года, поэтому 1С-шники массово переезжают на PostgreSQL и Postgres Pro (последний — в реестре отечественного ПО).

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

СУБД — это серверный процесс, который слушает TCP-порт и общается с клиентами по своему протоколу на прикладном уровне OSI (L7). PostgreSQL — порт **5432**, MySQL/MariaDB — **3306**, PgBouncer (пулер соединений) — **6432**. Это обычный TCP, поэтому файрвол, NAT и балансировщики на них влияют ровно так же, как на HTTP.

Ключевая идея PostgreSQL — **WAL (Write-Ahead Log, журнал предзаписи)**. Прежде чем изменить страницу данных на диске, сервер пишет запись в WAL. Если сервер упадёт, при старте он «докатит» (recovery) изменения из WAL. Из этой же механики растёт всё остальное:

- **Потоковая репликация** — primary отдаёт WAL по обычному соединению (тот же порт 5432, только в `pg_hba.conf` открыта специальная «replication»-строка), standby непрерывно его применяет и находится в режиме recovery — read-only.
- **PITR (Point-In-Time Recovery)** — восстановление на любой момент времени: полная копия + архив WAL.
- **Репликационный слот** — primary держит WAL, пока standby не подтвердил; защищает от разрыва, но если standby умер — WAL копится и съедает диск.

MySQL устроен похоже, но журнал называется **binlog**, а роли — source/replica (до 8.0.23 — master/slave), и есть **GTID** (Global Transaction ID) — сквозная нумерация транзакций, по которой реплику удобно пересоздавать.

```text
 WIN11-CL01 (ivanov)              приложения (WEB01 и др.)
        │ 1С-клиент                      │ TCP 5432
        ▼                                ▼
 SRV1C01 (сервер 1С, L2)  ────►  PgBouncer :6432  ────►  DB01 :5432  primary
                                                             │
                                    WAL по TCP 5432, слот db02_slot
                                                             ▼
                                                         DB02 :5432  standby (read-only)
                                                             │
   DB01 ── archive_command ──► /var/lib/postgresql/wal_archive ──► бэкап-хранилище
   DB01/DB02 ── zabbix-agent2 :10050 ──► MON01 (Zabbix server) :10051
```

Три уровня работы с данными, которые надо различать: **логический бэкап** (`pg_dump` — SQL-дамп объектов), **физический бэкап** (`pg_basebackup` — побайтовая копия кластера) и **реплика** (живая копия, но не бэкап: `DELETE` без `WHERE` мгновенно приедет на standby).

## 3. Аналогия из жизни

База — это бухгалтерский архив в подсобке. WAL — журнал операций на столе: сначала пишем в журнал, потом раскладываем по папкам, и если свет отключился — по журналу восстановим. Реплика — второй архив в соседнем здании, куда курьер возит копии журнала каждые несколько секунд (потоковая репликация). Слот репликации — распоряжение «не выбрасывай копии, пока второй архив их не принял»: курьер заболел — бумаги копятся у двери, пока коридор не забьётся. Бэкап — фотография комнаты, сделанная ночью и отвезённая в другое здание. И главное: второй архив помогает только от пожара в первом здании, но не от бухгалтера, который разом подписал все бумаги неверно.

Аналогия ломается на «откате назад во времени»: у второго архива нет машины времени, а у бэкапа с архивом WAL она есть — PITR.

## 4. Практика и команды

**1. Быстрая диагностика «жива ли база»**

```bash
sudo -u postgres pg_lsclusters
sudo -u postgres pg_isready -h 10.10.10.32 -p 5432
```

```text
Ver Cluster Port Status Owner    Data directory              Log file
16  main    5432 online postgres /var/lib/postgresql/16/main /var/log/postgresql/postgresql-16-main.log
10.10.10.32:5432 - accepting connections
```

`pg_isready` возвращает код 0 (принимает соединения), 1 (работает, но не принимает), 2 (не отвечает). В скриптах мониторинга проверяй `$?`.

**2. Вход и метакоманды psql**

```bash
sudo -u postgres psql -d vektor_db
```
```text
vektor_db=# \l+        -- список БД с размерами
vektor_db=# \dt+       -- таблицы с размерами
vektor_db=# \du+       -- роли и их атрибуты
vektor_db=# \timing    -- включаем замер времени запросов
vektor_db=# \watch 5   -- повторять последний запрос каждые 5 секунд
```
`\x auto` — вертикальный вывод, спасает на широких таблицах.

**3. Кто сейчас грузит базу и кого блокируют**

```sql
SELECT pid, usename, application_name, client_addr, state,
       now() - query_start AS dur, wait_event_type, wait_event,
       left(query, 60) AS q, pg_blocking_pids(pid) AS blocked_by
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY dur DESC LIMIT 10;
```
Смотрим на `state = 'active'` с большим `dur`, и на непустой `blocked_by` — это классические «зависшие» блокировки. `pg_cancel_backend(pid)` — отменить запрос, `pg_terminate_backend(pid)` — убить сессию (уже грубее).

**4. Топ тяжёлых запросов**

```ini
# /etc/postgresql/16/main/postgresql.conf
shared_preload_libraries = 'pg_stat_statements'   # требует рестарта
pg_stat_statements.max = 10000
pg_stat_statements.track = all
```
```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SELECT calls, round(total_exec_time::numeric,1) AS ms,
       round(mean_exec_time::numeric,1) AS avg_ms, rows, left(query,70)
FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
```

**5. Логический бэкап и — обязательно — проверка**

```bash
sudo -u postgres pg_dump -Fc -d vektor_db -f /var/backups/vektor_db_$(date +%F).dump
sudo -u postgres pg_dumpall --globals-only -f /var/backups/globals_$(date +%F).sql
sudo -u postgres pg_restore -l /var/backups/vektor_db_2026-01-01.dump | head -20
```
`-Fc` — custom-формат (сжатый, восстанавливается выборочно и параллельно `-j 4`). `--globals-only` — роли и их пароли, они **не** входят в `pg_dump` отдельной БД. `pg_restore -l` читает оглавление — это первый признак, что дамп не битый. Настоящая проверка — восстановление в отдельную БД: `createdb vektor_restore && pg_restore -d vektor_restore <dump>`.

**6. Физический бэкап и архив WAL (основа PITR)**

```bash
sudo mkdir -p /var/lib/postgresql/wal_archive && sudo chown postgres:postgres /var/lib/postgresql/wal_archive
sudo -u postgres pg_basebackup -h 10.10.10.32 -U replicator -D /var/backups/base -Fp -Xs -P -R
```
```ini
archive_mode = on
archive_command = 'test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'
```
`-Xs` — WAL стримится вместе с копией, `-P` — прогресс, `-R` — записать настройки подключения (нужно для клонирования standby). `%p`/`%f` подставляет PostgreSQL. Проверка архивации: `SELECT * FROM pg_stat_archiver;` — колонка `failed_count` должна быть 0.

**7. Состояние репликации**

```sql
-- на primary (DB01)
SELECT client_addr, state, sync_state,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS lag
FROM pg_stat_replication;
```
```sql
-- на standby (DB02)
SELECT pg_is_in_recovery(), status, sender_host, latest_end_lsn FROM pg_stat_wal_receiver;
```
`lag` растёт — standby не успевает. Десятки-сотни килобайт в норме; гигабайты — авария.

**8. Переключение ролей (switchover/promote)**

```bash
sudo pg_ctlcluster 16 main promote        # на DB02: standby становится primary
sudo -u postgres psql -c "SELECT pg_is_in_recovery();"   # false = уже primary
```
В бою это делает Patroni + etcd (порты 2379/2380) — но руками ты должен уметь тоже, иначе в 3 часа ночи будешь читать документацию вместо того, чтобы поднимать сервис.

**9. Кто занимает место и что не используется**

```sql
SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY 2 DESC;
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) AS total
FROM pg_class WHERE relkind = 'r' ORDER BY pg_total_relation_size(oid) DESC LIMIT 10;
SELECT relname, indexrelname, idx_scan FROM pg_stat_user_indexes ORDER BY idx_scan LIMIT 10;
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 10;
```
Индекс с `idx_scan = 0` за месяц — кандидат на удаление. Большой `n_dead_tup` — автовакуум не успевает, таблица «распухает» (bloat).

**10. План запроса и базовый тюнинг**

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM shipments WHERE created_at > now() - interval '7 days';
```
Смотрим: `Seq Scan` на большой таблице при наличии индекса — плохо; расхождение `rows` (оценка) и `actual rows` в разы — устаревшая статистика, лечится `ANALYZE`.

```ini
# стартовая точка для DB01 с 8 ГБ RAM, потом меряем и правим
shared_buffers = 2GB              # ~25% RAM
effective_cache_size = 6GB        # ~75% RAM, это «подсказка» планировщику, память не занимает
work_mem = 32MB                   # ВНИМАНИЕ: на каждый узел плана каждого соединения
maintenance_work_mem = 512MB
random_page_cost = 1.1            # SSD, а не 4.0 «как у HDD»
effective_io_concurrency = 200    # SSD
max_connections = 100             # больше — только через PgBouncer
wal_compression = on
```

**11. MySQL/MariaDB — те же задачи другими командами**

```bash
mysqldump --single-transaction --routines --triggers --events \
  -u root -p vektor_db > /var/backups/vektor_db.sql
```
```sql
SHOW REPLICA STATUS\G   -- Replica_IO_Running, Replica_SQL_Running, Seconds_Behind_Source
SHOW ENGINE INNODB STATUS\G   -- блокировки, последний deadlock, состояние buffer pool
```
`--single-transaction` даёт консистентный снимок InnoDB без блокировки таблиц. Физический горячий бэкап — Percona XtraBackup. Ключевой параметр тюнинга: `innodb_buffer_pool_size` ≈ 70–80 % RAM.

**12. Подключение к Zabbix**

```bash
sudo apt install zabbix-agent2
sudo nano /etc/zabbix/zabbix_agent2.conf     # Server=10.10.10.40, Hostname=DB01
sudo systemctl enable --now zabbix-agent2
```
На MON01 (Zabbix) добавляем хост DB01 и шаблон **PostgreSQL by Zabbix agent 2** — он сам собирает `pgsql.ping`, connections, размер БД, репликацию `[ПРОВЕРИТЬ точный набор ключей для вашей версии Zabbix]`. Плюс обязательный триггер на свободное место и на лаг репликации.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 развёрнут: DC01, FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01, MON01 (Zabbix), SRV1C01 (1С). Домены и учётки — по описанию стенда.

В таблице машин стенда серверов БД нет, поэтому эту роль добавляем явно — как разрешено для трека сисадмина:

| Имя | ОС | Адрес | Роль |
|-----|----|-------|------|
| DB01 | Ubuntu Server 24.04 LTS | 10.10.10.32 | PostgreSQL 16, primary |
| DB02 | Ubuntu Server 24.04 LTS | 10.10.10.33 | PostgreSQL 16, standby |

Обе — по 2 vCPU / 4–8 ГБ RAM / 40 ГБ диск, сеть VMnet8, IP статикой, шлюз 10.10.10.2, DNS 10.10.10.10. PostgreSQL 16 есть в штатном репозитории Ubuntu 24.04 — ничего скачивать сомнительного не нужно.

### Снапшот

Сначала ставим на DB01/DB02 только ОС, обновления и сеть (шаги 1–2), **затем** — снапшот `before-sysadmin-066`. VMware Workstation: ПКМ по вкладке ВМ → Snapshot → Take Snapshot → имя `before-sysadmin-066`. Снять на **DB01, DB02, DC01 и MON01** (три последних затрагиваем по DNS и мониторингу).

### Шаги

1. **DB01, DB02.** Ставим Ubuntu Server 24.04, задаём hostname `db01`/`db02`, статический адрес, в `/etc/hosts` добавляем обе строки. Проверка: `ping -c2 db01` и `ping -c2 10.10.10.10`.
2. **DC01.** Добавляем A-записи в DNS:
   ```powershell
   Add-DnsServerResourceRecordA -Name db01 -ZoneName corp.vektor.lab -IPv4Address 10.10.10.32
   Add-DnsServerResourceRecordA -Name db02 -ZoneName corp.vektor.lab -IPv4Address 10.10.10.33
   ```
   Ожидаемо: `Resolve-DnsName db01.corp.vektor.lab` на WIN11-CL01 отдаёт 10.10.10.32.
3. **DB01, DB02.** `sudo apt update && sudo apt install -y postgresql postgresql-contrib`. Ожидаемо: `pg_lsclusters` показывает кластер `16 main` в статусе `online`.
4. **DB01.** Создаём роли и БД:
   ```bash
   sudo -u postgres psql -c "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'Repl_Lab#2026';"
   sudo -u postgres psql -c "CREATE ROLE app_vektor LOGIN PASSWORD 'Lab#2026-vektor';"
   sudo -u postgres psql -c "CREATE DATABASE vektor_db OWNER app_vektor;"
   ```
   Ожидаемо: `\du` показывает обе роли, у `replicator` атрибут `Replication`.
5. **DB01.** Правим `/etc/postgresql/16/main/pg_hba.conf` (строки **выше** остальных):
   ```text
   host  replication  replicator  10.10.10.33/32  scram-sha-256
   host  all          app_vektor  10.10.10.0/24  scram-sha-256
   ```
   И `postgresql.conf`: `listen_addresses = '*'`, `wal_level = replica`, `max_wal_senders = 10`, `max_replication_slots = 10`, `wal_keep_size = 512MB`, `archive_mode = on`, `archive_command = ...` (из раздела 4, п.6). `sudo pg_ctlcluster 16 main restart`. Ожидаемо: `ss -lntp | grep 5432` слушает `0.0.0.0`.
6. **DB01.** Создаём слот и тестовую таблицу:
   ```bash
   sudo -u postgres psql -c "SELECT pg_create_physical_replication_slot('db02_slot');"
   sudo -u postgres psql -d vektor_db -c "CREATE TABLE shipments(id serial primary key, created_at timestamptz default now(), note text);"
   ```
7. **DB02.** Останавливаем кластер, очищаем каталог данных под копию:
   ```bash
   sudo pg_ctlcluster 16 main stop
   sudo mv /var/lib/postgresql/16/main /var/lib/postgresql/16/main.orig
   sudo install -d -o postgres -g postgres -m 700 /var/lib/postgresql/16/main
   ```
   Затем клонируем с primary:
   ```bash
   sudo -u postgres pg_basebackup -h 10.10.10.32 -U replicator -D /var/lib/postgresql/16/main \
        -Fp -Xs -P -R -S db02_slot
   sudo pg_ctlcluster 16 main start
   ```
   Ожидаемо: копия 100 %, кластер `online`, в `postgresql.auto.conf` появился `primary_conninfo`, в каталоге данных — файл `standby.signal`.
8. **Проверка репликации.** На DB01 — `SELECT * FROM pg_stat_replication;` (строка `streaming`, `lag` в килобайтах). На DB02 — `SELECT pg_is_in_recovery();` → `t`. Пишем на DB01 `INSERT INTO shipments(note) VALUES ('test');`, читаем на DB02 — строка видна.
9. **Бэкапы.** `pg_dump -Fc` + `pg_dumpall --globals-only`, восстановление в `vektor_restore`, проверка `pg_stat_archiver.failed_count = 0`.
10. **Мониторинг.** Ставим `zabbix-agent2` на DB01 и DB02, добавляем хосты на MON01, цепляем шаблон PostgreSQL, ждём первые данные (1–2 минуты).
11. **Тюнинг.** Применяем параметры из раздела 4 п.10, рестарт, проверяем `SHOW shared_buffers;` → `2GB`. Прогоняем `pgbench -i -s 10 vektor_db` и `pgbench -c 10 -T 60 vektor_db` до и после — фиксируем TPS в `stand-log.md`.
12. **Switchover.** `sudo pg_ctlcluster 16 main promote` на DB02, проверяем запись в базу уже там, затем возвращаем стенд в исходную топологию (пересоздаём standby из п.7).

### Проверка

- [ ] `pg_isready -h 10.10.10.32` и `-h 10.10.10.33` возвращают код 0
- [ ] `pg_stat_replication` на DB01 показывает `state = streaming`
- [ ] `pg_is_in_recovery()` на DB02 = `t` до promote
- [ ] INSERT на DB01 виден на DB02 в течение секунды
- [ ] `pg_dump`-файл существует, `pg_restore -l` его читает, тестовое восстановление прошло
- [ ] `SELECT * FROM pg_stat_archiver` — `failed_count = 0`
- [ ] В Zabbix хост DB01 в статусе Enabled/Available, есть графики
- [ ] `SHOW shared_buffers` показывает применённое значение
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

> **Пользователь:** ivanov / **ВМ:** WIN11-CL01 / **Что написал:** «1С вылетела, пишет "Не удалось подключиться к серверу 1С:Предприятие", и сайт склада тоже. Закрытие месяца!» / **Срочность:** Высокая

**Воспроизведение поломки (на стенде):**
```bash
# DB02: имитируем «реплику выключили на выходные»
sudo pg_ctlcluster 16 main stop
# DB01: генерируем WAL, пока слот держит всё у себя
for i in $(seq 1 20); do
  sudo -u postgres psql -d vektor_db -c \
   "INSERT INTO shipments(note) SELECT repeat('x',500) FROM generate_series(1,20000);"
  sudo -u postgres psql -c "SELECT pg_switch_wal();"
done
# DB01: добиваем диск под завязку (размер подставь под свой)
sudo fallocate -l 20G /var/fill.img
```

**Диагностика — строго по порядку:**
1. `systemctl status postgresql@16-main` → `failed`. База не работает, значит 1С и склад валятся следом.
2. `sudo tail -50 /var/log/postgresql/postgresql-16-main.log` → `PANIC: could not write to file "pg_wal/...": No space left on device`.
3. `df -h /var` → 100 %. Это причина, а не следствие.
4. `sudo du -sh /var/lib/postgresql/16/main/pg_wal` → десятки гигабайт.
5. ```sql
   SELECT slot_name, active,
          pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
   FROM pg_replication_slots;
   ```
   → `db02_slot | f | 38 GB`. Вот виновник: standby выключен, слот не даёт удалить WAL.

**Решение:** `sudo rm /var/fill.img` → освободили место → `sudo pg_ctlcluster 16 main start` → `sudo pg_ctlcluster 16 main start` на DB02 → догоняет WAL из слота, `pg_stat_replication` снова `streaming`, WAL сам ужимается. Если standby не поднять — `SELECT pg_drop_replication_slot('db02_slot');` и пересоздать реплику с нуля через `pg_basebackup`.

**Ответ пользователю:** «Иван, добрый день. Проблема на сервере базы данных — закончилось место на диске, из-за этого останавливалась база. Место освободили, база запущена, реплика догоняет. 1С откроется через 5–10 минут, ничего не потеряно. Напишите, если после 11:00 будут ошибки — я на связи».

**Закрытие:** категория «Инфраструктура / СУБД», решение в описании, корневая причина — «нет алерта на диск и на лаг репликации». Заводим задачу: триггер в Zabbix на `free space < 15 %` и на `inactive replication slot`.

### Что изменилось на стенде

- Добавлены ВМ **DB01 (10.10.10.32)** и **DB02 (10.10.10.33)**, Ubuntu Server 24.04 LTS, PostgreSQL 16.
- На DC01 — DNS A-записи `db01` и `db02` в зоне `corp.vektor.lab`.
- Роли PostgreSQL: `replicator` (REPLICATION), `app_vektor` (владелец `vektor_db`).
- БД `vektor_db`, таблица `shipments`; тестовая `vektor_restore`.
- Физический слот `db02_slot`, потоковая репликация DB01 → DB02.
- Каталог архива WAL `/var/lib/postgresql/wal_archive`; каталог бэкапов `/var/backups`.
- На MON01 — хосты DB01/DB02 с шаблоном PostgreSQL by Zabbix agent 2.
- Изменённые параметры `postgresql.conf` — перечислить в журнале значениями.

### Откат

VMware Workstation → ПКМ по ВМ → Snapshot Manager → выбрать `before-sysadmin-066` → **Revert**. Откатывать на DB01, DB02, DC01 и MON01 — иначе останутся DNS-записи и мёртвые хосты в мониторинге. Если ВМ сломана так, что не грузится — откат делать в выключенном состоянии.

## 6. Ошибки новичков и безопасность

1. **«Бэкап есть» = команда выполнилась.** → при аварии дамп оказывается битым или без нужных ролей. → Всегда восстанавливать в тестовую БД, для отдельных БД — `pg_dumpall --globals-only` вместе с дампом, раз в квартал — учения по восстановлению (урок 61).
2. **`shared_buffers = 80% RAM` из чужого конфига.** → OOM-killer, сервер ребутится под нагрузкой. → 25 % RAM, остальное через `effective_cache_size`, а не «побольше выделить».
3. **Слот репликации без мониторинга.** → диск забит WAL, база встала (наш кейс выше). → Алерт на `pg_replication_slots.active = false` и на размер `pg_wal`.
4. **Реплика вместо бэкапа.** → `DELETE` без `WHERE` мгновенно удалён и на standby, и на источнике. → Реплика — это HA, бэкап — отдельная изолированная копия.
5. **Отключённый автовакуум.** → таблицы и индексы распухают, запросы деградируют месяцами. → Не трогать `autovacuum = on`, при массовых операциях настраивать `autovacuum_vacuum_scale_factor` для крупных таблиц.
6. **Апгрейд мажорной версии «в лоб».** → кластер не стартует. → `pg_upgrade --check` на копии, всегда снапшот и бэкап перед обновлением.
7. **Приложение ходит под `postgres` (суперюзером).** → любая уязвимость в приложении = полный контроль над СУБД и ОС. → Отдельная роль-владелец БД с минимальными правами.
8. **`max_connections = 500` «на всякий случай».** → каждый коннект ест память, сервер ложится при пике. → PgBouncer, `max_connections` 100–200.

**Безопасность.** Федеральная база с ПДн или КИИ-сервис (152-ФЗ, 187-ФЗ, приказы ФСТЭК №17/21 — урок 58) требует: шифрование канала (`ssl = on`, сертификаты), разграничение по ролям и `pg_hba.conf` по IP, аудит доступа (`pgaudit` или логирование), пароли только через `.pgpass` с правами `0600` и никогда — в тексте скриптов и в git. Строку `trust` в `pg_hba.conf` и открытый в офисную сеть порт 5432 в боевой среде не оставляют — только через localhost/VPN/PgBouncer с ограничением по адресам. Помни: в РФ MS SQL официально недоступен, поэтому в регулируемом контуре реалистичный набор — **PostgreSQL**, **Postgres Pro** (в реестре отечественного ПО) или **MariaDB** в составе Astra Linux/РЕД ОС; Firebird-совместимая **Red Database** тоже в реестре.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Чем физическая репликация отличается от логической и когда что применять?**

Ответ: физическая передаёт WAL побайтово — standby точная копия кластера, read-only, версия и ОС те же; дёшево и быстро, идеально для HA и отказоустойчивости. Логическая передаёт изменения строк через публикации/подписки: можно реплицировать отдельные таблицы, между разными мажорными версиями и на другую архитектуру, реплика доступна на запись; но не переносит DDL, последовательности и крупные объекты автоматически, сложнее в сопровождении. Типичный выбор: HA → физическая, миграция на новую версию или сбор данных с нескольких источников → логическая.
*Что хочет услышать интервьюер:* что физическая не заменяет бэкап, и что логическая не даёт отказоустойчивости «из коробки».
*Красный флаг:* «реплика — это и есть бэкап».

**2. Как вы поймёте, что реплика отстаёт, и что будете делать?**

Ответ: на primary смотрю `pg_stat_replication` (колонки `state`, `replay_lsn`, разница `pg_current_wal_lsn() - replay_lsn`), на standby — `pg_stat_wal_receiver` и `pg_last_wal_replay_lsn()`. В Zabbix — триггер на лаг. Если лаг растёт: проверяю сеть между узлами, нагрузку записи, `max_wal_senders`, не ушёл ли standby в вечный recovery из-за конфликта, есть ли длинная транзакция на реплике, блокирующая применение. При разрыве и нехватке WAL — пересоздаю реплику через `pg_basebackup` и слот.
*Что хочет услышать интервьюер:* конкретные вьюхи и порядок действий, а не «посмотрю в мониторинге».
*Красный флаг:* «перезагружу реплику, обычно помогает».

**3. Зачем нужен `pg_basebackup`, если есть `pg_dump`, и как получить PITR?**

Ответ: `pg_dump` — логическая копия объектов, восстанавливается в другую версию, но не даёт отката на момент времени и на большой базе идёт часами. `pg_basebackup` — физическая копия кластера. PITR = физический бэкап + непрерывный архив WAL (`archive_mode = on`, `archive_command`), при восстановлении — `restore_command` и `recovery_target_time` в `postgresql.conf`/`postgresql.auto.conf`. Проверка обязательна: `pg_verifybackup` и учебное восстановление.
*Что хочет услышать интервьюер:* связку «бэкап + WAL = возможность откатиться на 10:37:12».
*Красный флаг:* «PITR — это когда есть реплика».

**4. С чего начнёте тюнинг незнакомой PostgreSQL под 1С?**

Ответ: сначала измерения — `pg_stat_statements`, `pg_stat_activity`, медленный журнал (`log_min_duration_statement`), свободное место, автовакуум, план проблемного запроса через `EXPLAIN (ANALYZE, BUFFERS)`. Потом базовая конфигурация по памяти: `shared_buffers` 25 %, `effective_cache_size` 50–75 %, `work_mem` осторожно, `random_page_cost = 1.1` на SSD, `max_connections` + PgBouncer. Дальше — индексы и запросы, а не «крутить параметры». Обязательно фиксирую «до/после» замером (`pgbench` или реальные метрики).
*Что хочет услышать интервьюер:* что тюнинг начинается с измерений, а не с конфига из интернета.
*Красный флаг:* «поставлю pgtune и всё».

**5. База легла в рабочее время. Ваши действия?**

Ответ: подтвердить проблему (`pg_isready`, `systemctl status`), оценить масштаб (кто ещё затронут), поднять сервис или переключиться на standby, если primary не восстановить быстро. Параллельно — сообщить пользователям и руководству по регламенту, не молчать. После восстановления — зафиксировать причину (логи, `df -h`, `dmesg`), оформить постмортем и задачу на устранение причины. Приоритет: сначала сервис, потом разбор.
*Что хочет услышать интервьюер:* порядок «стабилизировать → коммуницировать → разбираться», и что инцидент закрывается постмортемом.
*Красный флаг:* «сразу полезу разбираться, почему упало, никого не предупредив».

## 8. Источники (официальная документация)

- **postgresql.org** — Documentation, PostgreSQL 16: разделы *High Availability, Load Balancing, and Replication*, *Backup and Restore*, *Write-Ahead Logging (WAL)*, *Monitoring Database Activity*, *Routine Vacuuming*, *Server Configuration*. Поиск: `postgresql 16 streaming replication pg_basebackup`.
- **postgresql.org** — *pg_dump*, *pg_restore*, *pg_basebackup*, *pg_stat_statements*, *pg_stat_replication* (страницы Reference). Поиск: `postgresql pg_stat_replication replay_lsn lag`.
- **postgrespro.ru** — документация Postgres Pro на русском (перевод руководства PostgreSQL + разделы про 1С). Поиск: `Postgres Pro документация репликация`.
- **dev.mysql.com** — MySQL 8.0 Reference Manual: *Replication*, *Backup and Recovery*, *Optimization*, *InnoDB*. Поиск: `mysql 8.0 replication CHANGE REPLICATION SOURCE TO gtid`.
- **mariadb.com** — MariaDB Documentation, разделы *Replication*, *Backup and Restore*. Поиск: `mariadb knowledge base mysqldump single-transaction`.
- **docs.percona.com** — Percona XtraBackup documentation. Поиск: `percona xtrabackup 8.0 incremental backup`.
- **zabbix.com** — Documentation, *Templates* → PostgreSQL by Zabbix agent 2 / MySQL by Zabbix agent 2. Поиск: `zabbix template PostgreSQL by Zabbix agent 2`.
- **pgtune.leopard.in.ua** — генератор стартовых значений `postgresql.conf` (только как черновик, цифры проверять замером).
- **wiki.astralinux.ru** `[ПРОВЕРИТЬ]` — состав репозиториев Astra Linux и поддерживаемые версии PostgreSQL/MariaDB.
- **reestr.digital.gov.ru** `[ПРОВЕРИТЬ актуальность интерфейса]` — реестр отечественного ПО, поиск: `Postgres Pro`, `Red Database`.
