# Базы данных для сисадмина: PostgreSQL и MySQL — установка, бэкапы, пользователи, мониторинг

## 1. Введение (зачем это нужно в реальной работе)

8:47 утра, ты ещё не допил кофе. В тикете новое сообщение:

```
Пользователь: ivanov
ВМ: WIN11-CL01
Что написал: «Не могу открыть 1С, пишет "Не удалось установить
соединение с сервером баз данных". У Петровой то же самое.
Закрываем месяц, срочно!»
Срочность: высокая
```

Через полчаса выясняется: приложение живо, сеть жива, сервер 1С на SRV1C01 отвечает. Упало звено, о котором обычно вспоминают последним — СУБД. Кто-то поменял пароль пользователя БД, а в конфиге приложения остался старый. Или закончилось место на диске, и PostgreSQL перешёл в режим «только чтение». Или ночной бэкап случайно перезаписал `pg_hba.conf`.

В работе L2-инженера база данных встречается постоянно, даже если ты не DBA. Ты ставишь СУБД под 1С, под Zabbix, под внутренние сервисы. Ты создаёшь пользователя для приложения и выдаёшь ровно те права, которые нужны. Ты настраиваешь бэкап и — что важнее — проверяешь, что из него реально восстанавливается. Ты смотришь, кто занял все соединения и почему база тормозит в час пик. Ты не проектируешь схемы и не пишешь сложный SQL — этим занимается разработчик или 1С-ник, — но когда «всё лежит», именно ты открываешь логи и находишь причину.

После этой главы ты сможешь: поставить PostgreSQL и MySQL на UBU01, создав отдельных пользователей вместо `root`, настроить сетевой доступ через `pg_hba.conf` и `bind-address`, снять логический бэкап и восстановить его в проверочную базу, снять базовые метрики и ответить на вопрос «база упала или приложение».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

СУБД (система управления базами данных) — это серверный процесс, который хранит данные в файлах на диске и принимает запросы от клиентов по сети. В нашем стенде выстраивается такая цепочка:

```text
   WIN11-CL01                 SRV1C01
   (клиент 1С)                (сервер 1С)
        |                         |
        |  TCP 1540/1541 [ПРОВЕРИТЬ]  — протокол 1С
        +------------------------>|
                                  |  TCP 5432 — протокол PostgreSQL
                                  v
                     +----------------------------+
                     |  UBU01  10.10.10.30        |
                     |  postgresql@16-main :5432  |
                     |  mysqld             :3306  |
                     +-------------+--------------+
                                   |
             /var/lib/postgresql/16/main   /var/lib/mysql
             /var/log/postgresql/...       /var/log/mysql/error.log
             /var/backups/db  <-- сюда кладём pg_dump и mysqldump
```

С точки зрения модели OSI это прикладной уровень (L7): у PostgreSQL и MySQL свои бинарные протоколы поверх TCP. Транспорт — L4, порты **5432** (PostgreSQL) и **3306** (MySQL, плюс 33060 для X Protocol, его можно не открывать). Локально клиенты часто ходят не по TCP, а через Unix-сокет — это важно, потому что «локально работает, по сети нет» почти всегда означает разницу именно в этом.

**PostgreSQL: как устроен.** После старта работает процесс `postmaster`, который слушает порт и на каждое подключение порождает отдельный процесс-backend. Память между ними общая (`shared_buffers`), поэтому число соединений напрямую бьёт по памяти: параметр `max_connections` по умолчанию 100, каждое соединение — это процесс. Все изменения сначала пишутся в журнал предзаписи **WAL** (`pg_wal/`), а потом в файлы данных; это даёт возможность восстановления после сбоя и нужно для репликации.

Ключевая особенность, ломающая мозг новичку: в PostgreSQL **кластер** — это не «несколько серверов», а один экземпляр СУБД с одним каталогом данных, внутри которого живут несколько баз. Роли (`ROLE`) — это и пользователи, и группы одновременно: `CREATE ROLE ... LOGIN` делает из роли пользователя. Доступ проверяется по цепочке:

```text
клиент --> pg_hba.conf (пускать ли этот IP/пользователь/базу и как проверять пароль)
       --> pg_ident / пароль (SCRAM-SHA-256 по умолчанию с PG 14)
       --> привилегии в самой БД (GRANT на схему, таблицу)
```

Файл `pg_hba.conf` читается **сверху вниз, первое совпадение выигрывает** — это причина половины «мистических» отказов доступа.

**MySQL: как устроен.** Один процесс `mysqld`, на каждое подключение — поток (thread). Здесь «база данных» и «схема» — синонимы, а учётная запись задаётся парой `'user'@'host'`: `app_vektor'@'10.10.10.%'` и `'app_vektor'@'localhost'` — **разные** пользователи с разными паролями. Права хранятся в системной схеме `mysql` и управляются через `GRANT`. Движок по умолчанию — InnoDB (транзакции, блокировки на уровне строк, свой журнал redo log). Аутентификация в MySQL 8.0 по умолчанию — `caching_sha2_password`, и это частая причина «приложение не подключается, а `mysql` из консоли заходит»: старый драйвер не умеет этот плагин (об этом ниже).

**Бэкапы: два разных мира.**

| Тип | Чем делаем (PostgreSQL) | Чем делаем (MySQL) | Плюсы / минусы |
|-----|------------------------|--------------------|----------------|
| Логический | `pg_dump`, `pg_dumpall` | `mysqldump` | переносится между версиями, но медленно восстанавливается и «замораживает» данные |
| Физический | `pg_basebackup` + архив WAL | `mariabackup` (MariaDB), файловый снапшот ВМ | быстро, но привязан к версии и платформе |

Важно: логический дамп — это **не** резервная копия сервера. В `pg_dump` не попадают роли и настройки, в `mysqldump` без `--databases` — сама схема создания базы.

## 3. Аналогия из жизни

Представь архив крупной компании. Внутри — стеллажи с делами (таблицы), у входа сидит вахтёр с толстым списком (это `pg_hba.conf`: кому и по какому пропуску вообще разрешено войти), а внутри уже кладовщик смотрит, к каким именно стеллажам у тебя допуск (это `GRANT`). Пропуск — это учётная запись. Каждое перемещение дела фиксируется в журнале выдачи — это WAL: даже если в зале погас свет посреди работы, по журналу можно понять, что успели переставить.

Логический бэкап — переписать содержимое всех дел от руки в тетрадку: долго, зато тетрадку можно прочитать где угодно. Физический — сфотографировать весь зал целиком вместе со стеллажами: мгновенно, но разворачивать фотографию придётся в точно такой же зал.

Аналогия перестаёт работать в одном месте: кладовщик не устаёт, а PostgreSQL при нехватке `max_connections` просто перестаёт пускать людей, и выясняется это только по журналу.

## 4. Практика и команды

Ниже — всё на UBU01 (10.10.10.30, Ubuntu Server 24.04). Версия PostgreSQL в репозитории Ubuntu 24.04 — 16, MySQL — 8.0.x `[ПРОВЕРИТЬ актуальную минорную версию]`.

**1. Установка PostgreSQL и проверка статуса**

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
pg_lsclusters
systemctl status postgresql@16-main --no-pager
ss -tlnp | grep 5432
```

`pg_lsclusters` показывает кластеры: имя `main`, версия, порт, статус. `ss -tlnp` — что реально слушается. Ожидаемо: `LISTEN 0 244 127.0.0.1:5432` — только локально, это безопасный дефолт. Если видишь `0.0.0.0:5432` сразу после установки — кто-то уже правил конфиг.

**2. Подключение и мета-команды psql**

```bash
sudo -u postgres psql
```

```text
postgres=# \l        -- список баз
postgres=# \du       -- список ролей
postgres=# \c vektor_db   -- переключиться на базу
vektor_db=# \dt      -- таблицы
vektor_db=# \x        -- расширенный вывод построчно
postgres=# \q
```

`sudo -u postgres` нужен потому, что в дефолтном `pg_hba.conf` для локального сокета стоит метод `peer`: СУБД пускает только если системный пользователь совпадает с именем роли. Роли `postgres` соответствует одноимённый системный пользователь.

**3. Создание роли и базы (никогда не работаем приложением под `postgres`)**

```bash
sudo -u postgres psql -c "CREATE ROLE app_vektor LOGIN PASSWORD 'Lab#2026-vektor';"
sudo -u postgres psql -c "CREATE DATABASE vektor_db OWNER app_vektor;"
sudo -u postgres psql -d vektor_db -c "REVOKE ALL ON SCHEMA public FROM PUBLIC;"
```

Ожидаемый вывод — `CREATE ROLE`, `CREATE DATABASE`. `REVOKE ... FROM PUBLIC` убирает доступ «всем» к схеме `public`: по умолчанию в старых версиях PostgreSQL это была дыра.

**4. Разрешаем сеть: `listen_addresses` и `pg_hba.conf`**

```bash
sudo nano /etc/postgresql/16/main/postgresql.conf
# listen_addresses = '10.10.10.30'   (раскомментировать и заменить)

sudo nano /etc/postgresql/16/main/pg_hba.conf
# добавить в конец:
# host    vektor_db    app_vektor    10.10.10.0/24    scram-sha-256

sudo systemctl reload postgresql@16-main
```

`reload` перечитывает конфиг без разрыва соединений; `restart` нужен только для `listen_addresses`. Проверка: `ss -tlnp | grep 5432` → `10.10.10.30:5432`.

**5. Логический бэкап и восстановление PostgreSQL**

```bash
sudo mkdir -p /var/backups/db
sudo -u postgres pg_dump -Fc -d vektor_db -f /var/backups/db/vektor_db_$(date +%F).dump
sudo -u postgres createdb vektor_db_restore
sudo -u postgres pg_restore -d vektor_db_restore /var/backups/db/vektor_db_2026-XX-XX.dump
sudo -u postgres psql -d vektor_db_restore -c "SELECT count(*) FROM payments;"
```

`-Fc` — формат custom: сжатый, восстанавливается через `pg_restore` выборочно (можно одну таблицу). Без `-Fc` получишь plain SQL, который заливается через `psql -f`. Роли в дамп не входят — для них `pg_dumpall --globals-only -f roles.sql`.

**6. Установка MySQL и первичная защита**

```bash
sudo apt install -y mysql-server
sudo mysql_secure_installation
sudo mysql -e "SELECT VERSION();"
```

`mysql_secure_installation` спросит про валидацию паролей, анонимных пользователей, удалённый root и тестовую базу — на все отвечай `Y`. На Ubuntu `root@localhost` использует плагин `auth_socket`, поэтому `sudo mysql` работает без пароля, а шаг смены пароля root можно пропустить; в боевой среде так оставлять нельзя.

**7. Пользователь и права в MySQL**

```sql
CREATE DATABASE vektor_hr CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'app_vektor'@'10.10.10.%' IDENTIFIED BY 'Lab#2026-vektor';
GRANT SELECT, INSERT, UPDATE, DELETE ON vektor_hr.* TO 'app_vektor'@'10.10.10.%';
SHOW GRANTS FOR 'app_vektor'@'10.10.10.%';
```

Ожидаемый вывод `SHOW GRANTS` — `GRANT SELECT, INSERT, UPDATE, DELETE ON \`vektor_hr\`.* TO \`app_vektor\`@\`10.10.10.%\``. `FLUSH PRIVILEGES` здесь **не нужен**: он требуется только если ты правишь таблицы `mysql.*` напрямую SQL-ем. Обрати внимание на `'10.10.10.%'` — с `'%'` пользователь сможет прийти с любого адреса.

**8. Бэкап и восстановление MySQL**

```bash
sudo mysqldump --single-transaction --routines --triggers --databases vektor_hr \
  > /var/backups/db/vektor_hr_$(date +%F).sql
sudo mysql < /var/backups/db/vektor_hr_2026-XX-XX.sql
sudo mysql -e "SELECT COUNT(*) FROM vektor_hr.<таблица>;"
```

`--single-transaction` даёт согласованный снимок InnoDB без блокировки таблиц — обязательно для живой базы. `--databases` добавляет `CREATE DATABASE` и `USE` в дамп. `--routines` и `--triggers` — иначе хранимые процедуры и триггеры потеряются.

**9. Мониторинг PostgreSQL**

```sql
SELECT pid, usename, application_name, client_addr, state,
       now() - query_start AS duration, left(query, 60)
FROM pg_stat_activity WHERE backend_type = 'client backend';

SELECT pg_size_pretty(pg_database_size('vektor_db'));
SELECT count(*) FROM pg_locks WHERE NOT granted;
SELECT sum(blks_hit)*100.0/nullif(sum(blks_hit+blks_read),0) AS cache_hit_pct
FROM pg_stat_database;
```

Смотрим: `duration` больше нескольких секунд и `state = 'active'` — что-то зависло; `pg_locks WHERE NOT granted` — очередь ожидания блокировок; `cache_hit_pct` ниже 90% — мало `shared_buffers`.

**10. Мониторинг MySQL**

```sql
SHOW PROCESSLIST;
SHOW STATUS LIKE 'Threads_connected';
SHOW VARIABLES LIKE 'max_connections';
SHOW ENGINE INNODB STATUS\G
SET GLOBAL slow_query_log = ON;
SET GLOBAL long_query_time = 1;
```

`Threads_connected` близко к `max_connections` — приложение не закрывает соединения (утечка пула). В `INNODB STATUS` ищи секцию `TRANSACTIONS` и строки `LOCK WAIT`. Медленный лог пишется в `/var/log/mysql/slow.log` (путь можно задать `slow_query_log_file`).

**11. Логи СУБД**

```bash
sudo tail -n 50 /var/log/postgresql/postgresql-16-main.log
sudo tail -n 50 /var/log/mysql/error.log
journalctl -u postgresql@16-main -n 50 --no-pager
```

Именно здесь появляется `FATAL: no pg_hba.conf entry for host ...` или `Access denied for user ...` — точная причина, которую приложение в тикете скрыло за фразой «ошибка соединения».

**12. Проверка фаервола**

```bash
sudo ufw status verbose
sudo ufw allow from 10.10.10.0/24 to any port 5432 proto tcp
sudo ufw allow from 10.10.10.0/24 to any port 3306 proto tcp
```

Обрати внимание: `ufw` не заменяет `pg_hba.conf` и `bind-address`, это два независимых слоя.

GUI-варианты для тех же задач (бесплатные): pgAdmin для PostgreSQL, MySQL Workbench или DBeaver для обеих.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS) — установлена, SSH настроен, вход по ключу под учёткой с `sudo`, статический адрес из урока 53. Рекомендуется 4 ГБ RAM и 25 ГБ диска: две СУБД на 2 ГБ будут постоянно уходить в swap. Проверить: `free -h`, `df -h`.
- `ufw` включён (урок 55). Если нет — `sudo ufw enable`.
- **DC01**, **FS01**, клиентские ВМ не нужны. **SRV1C01** (10.10.10.25) в этой лабораторной не настраиваем — мы готовим для него базу, но связку 1С ↔ PostgreSQL разберём, когда дойдём до сервисов 1С.
- Если UBU01 ещё не создана — вернись к модулю установки Linux трека L1.

### Снапшот

В VMware Workstation при выключенной или работающей ВМ: правый клик по UBU01 → **Snapshot → Take Snapshot**, имя:

```text
before-l2-057
```

Если UBU01 запущена, для консистентности лучше сначала `sudo shutdown -h now`, снять снапшот, потом включить. Записывать в `stand-log.md` пока нечего, но строку «снапшот before-l2-057 снят» добавь.

### Шаги

1. **UBU01.** Проверяем ресурсы и обновляем пакеты.
   ```bash
   free -h; df -h /; sudo apt update
   ```
   Ожидаемо: свободной RAM больше 3 ГБ, на `/` больше 15 ГБ, `apt update` завершился без ошибок репозиториев.

2. **UBU01.** Ставим PostgreSQL.
   ```bash
   sudo apt install -y postgresql postgresql-contrib
   pg_lsclusters
   ```
   Ожидаемо: строка `16  main  5432  online  postgres  /var/lib/postgresql/16/main`.

3. **UBU01.** Убеждаемся, что по умолчанию база слушает только localhost.
   ```bash
   ss -tlnp | grep 5432
   ```
   Ожидаемо: `127.0.0.1:5432`. Это исходное безопасное состояние — зафиксируй его в журнале стенда.

4. **UBU01.** Создаём роль, базу и тестовую таблицу.
   ```bash
   sudo -u postgres psql -c "CREATE ROLE app_vektor LOGIN PASSWORD 'Lab#2026-vektor';"
   sudo -u postgres psql -c "CREATE DATABASE vektor_db OWNER app_vektor;"
   sudo -u postgres psql -d vektor_db <<'SQL'
   CREATE TABLE payments (
     id bigserial PRIMARY KEY,
     doc_num text NOT NULL,
     amount numeric(12,2) NOT NULL,
     created_at timestamptz DEFAULT now()
   );
   INSERT INTO payments (doc_num, amount)
   SELECT 'PD-'||g, (random()*100000)::numeric(12,2) FROM generate_series(1,1000) g;
   SQL
   sudo -u postgres psql -d vektor_db -c "SELECT count(*) FROM payments;"
   ```
   Ожидаемо: `1000`.

5. **UBU01.** Открываем сетевой доступ.
   ```bash
   sudo sed -i "s/^#listen_addresses.*/listen_addresses = '10.10.10.30'/" /etc/postgresql/16/main/postgresql.conf
   echo "host vektor_db app_vektor 10.10.10.0/24 scram-sha-256" | sudo tee -a /etc/postgresql/16/main/pg_hba.conf
   sudo systemctl restart postgresql@16-main
   ss -tlnp | grep 5432
   ```
   Ожидаемо: `10.10.10.30:5432`.

6. **UBU01.** Проверяем вход по сети с паролем.
   ```bash
   PGPASSWORD='Lab#2026-vektor' psql -h 10.10.10.30 -U app_vektor -d vektor_db -c "SELECT current_user, now();"
   ```
   Ожидаемо: строка с `app_vektor`. Если видишь `no pg_hba.conf entry` — правило не совпало (проверь порядок и адрес), если `password authentication failed` — пароль или метод шифрования.

7. **UBU01.** Разрешаем порт в ufw и снимаем бэкап.
   ```bash
   sudo ufw allow from 10.10.10.0/24 to any port 5432 proto tcp
   sudo mkdir -p /var/backups/db && sudo chown postgres:postgres /var/backups/db
   sudo -u postgres pg_dump -Fc -d vektor_db -f /var/backups/db/vektor_db.dump
   sudo -u postgres pg_restore -l /var/backups/db/vektor_db.dump | head
   ```
   Ожидаемо: `pg_restore -l` печатает оглавление дампа (таблица, sequence, данные).

8. **UBU01.** Тестируем восстановление — обязательно в отдельную базу.
   ```bash
   sudo -u postgres createdb vektor_db_restore
   sudo -u postgres pg_restore -d vektor_db_restore /var/backups/db/vektor_db.dump
   sudo -u postgres psql -d vektor_db_restore -c "SELECT count(*) FROM payments;"
   ```
   Ожидаемо: `1000`. Если число другое — бэкап неполный, и это надо поймать сейчас, а не в аварию.

9. **UBU01.** Ставим MySQL и защищаем.
   ```bash
   sudo apt install -y mysql-server
   sudo mysql_secure_installation
   sudo mysql -e "SELECT VERSION(); SHOW VARIABLES LIKE 'bind-address';"
   ```
   Ожидаемо: версия `8.0.x`, `bind-address` = `127.0.0.1`.

10. **UBU01.** Создаём базу, пользователя и выдаём минимальные права.
    ```bash
    sudo mysql <<'SQL'
    CREATE DATABASE vektor_hr CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    CREATE TABLE vektor_hr.staff (id INT AUTO_INCREMENT PRIMARY KEY, fio VARCHAR(120));
    INSERT INTO vektor_hr.staff (fio) VALUES ('Иванов И.И.'),('Петрова М.А.'),('Сидоров А.П.');
    CREATE USER 'app_vektor'@'10.10.10.%' IDENTIFIED BY 'Lab#2026-vektor';
    GRANT SELECT, INSERT, UPDATE, DELETE ON vektor_hr.* TO 'app_vektor'@'10.10.10.%';
    SHOW GRANTS FOR 'app_vektor'@'10.10.10.%';
    SQL
    ```
    Ожидаемо: ровно один `GRANT` с четырьмя правами и без `WITH GRANT OPTION`.

11. **UBU01.** Бэкап MySQL и восстановление.
    ```bash
    sudo mysqldump --single-transaction --routines --triggers --databases vektor_hr > /var/backups/db/vektor_hr.sql
    sudo mysql -e "DROP DATABASE vektor_hr;"
    sudo mysql < /var/backups/db/vektor_hr.sql
    sudo mysql -e "SELECT COUNT(*) FROM vektor_hr.staff;"
    ```
    Ожидаемо: `3`.

12. **UBU01.** Снимаем метрики обеих СУБД и записываем в `stand-log.md`.
    ```bash
    sudo -u postgres psql -d vektor_db -c "SELECT pg_size_pretty(pg_database_size('vektor_db'));"
    sudo -u postgres psql -c "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend';"
    sudo mysql -e "SHOW STATUS LIKE 'Threads_connected'; SHOW VARIABLES LIKE 'max_connections';"
    ```
    Ожидаемо: размер в килобайтах/мегабайтах, число соединений 1–2, `max_connections` = 151.

### Проверка

- [ ] `pg_lsclusters` показывает кластер 16/main в статусе `online`.
- [ ] `ss -tlnp` показывает `10.10.10.30:5432` и `127.0.0.1:3306`.
- [ ] `psql -h 10.10.10.30 -U app_vektor` подключается по паролю.
- [ ] В `vektor_db_restore` ровно 1000 строк — бэкап проверен.
- [ ] `SHOW GRANTS FOR 'app_vektor'@'10.10.10.%'` не содержит `ALL PRIVILEGES` и `WITH GRANT OPTION`.
- [ ] В `/var/backups/db/` лежат `vektor_db.dump` и `vektor_hr.sql`.
- [ ] В `stand-log.md` записаны: установленные СУБД, роли, базы, порты, правила ufw.

### Сломай и почини

```
Пользователь: ivanov (Иван Иванов, бухгалтерия)
ВМ: WIN11-CL01
Что написал: «С утра 1С не открывается: "Не удалось установить соединение
с сервером баз данных". У Петровой то же. Закрываем месяц!»
Срочность: высокая
```

**Как воспроизвести поломку (на UBU01):**

```bash
sudo systemctl stop postgresql@16-main
```

Затем, после первой «починки», добавим второй слой:

```bash
sudo systemctl start postgresql@16-main
sudo sed -i 's/^host vektor_db app_vektor 10.10.10.0\/24 scram-sha-256/#&/' /etc/postgresql/16/main/pg_hba.conf
sudo systemctl reload postgresql@16-main
```

**Диагностика по порядку (снизу вверх по стеку):**

1. **Сеть жива?** `ping 10.10.10.30` с клиента, `ip a` на UBU01. Не тратим время на СУБД, пока не убедились, что хост пингуется.
2. **Порт слушается?** `ss -tlnp | grep 5432` на UBU01. Пусто → проблема в сервисе или конфиге, а не в правах.
3. **Сервис запущен?** `systemctl status postgresql@16-main --no-pager` → `inactive (dead)` в первом сценарии. `sudo systemctl start postgresql@16-main`.
4. **Что сказал журнал?** `journalctl -u postgresql@16-main -n 50 --no-pager` и `sudo tail -50 /var/log/postgresql/postgresql-16-main.log`. Ищем `FATAL:`.
5. **Локально заходится?** `sudo -u postgres psql -c "SELECT 1;"`. Работает по сокету, но не по TCP → смотрим `listen_addresses` и `pg_hba.conf`.
6. **Пробуем тем же пользователем, что и приложение:** `PGPASSWORD='...' psql -h 10.10.10.30 -U app_vektor -d vektor_db`. Ошибка `no pg_hba.conf entry for host "10.10.10.101"` → правило закомментировано/не совпало по маске. `password authentication failed` → пароль роли отличается от того, что прописан в приложении.
7. **Проверяем сам пароль роли, а не догадки:** `sudo -u postgres psql -c "\du app_vektor"` (роль существует, `Login` = yes), при необходимости `ALTER ROLE app_vektor PASSWORD 'Lab#2026-vektor';`.
8. **Финальная проверка сквозным подключением** и повторный `pg_restore -l` бэкапа — убедиться, что данные на месте.

**Решение:** раскомментировать/исправить строку в `pg_hba.conf`, `sudo systemctl reload postgresql@16-main`, при необходимости сбросить пароль роли. Всё логируем в тикет.

**Ответ пользователю:**

> Иван Иванович, добрый день. Причина найдена: сервер баз данных не принимал подключения с рабочей сети (сбой в настройках доступа после вчерашних работ). Сейчас доступ восстановлен, 1С открывается. Проверьте, пожалуйста, проведение документов — если что-то не сохранилось за утро, напишите, восстановим. Приношу извинения за простой.

**Закрытие заявки:** категория «Базы данных / PostgreSQL», приоритет высокий, в решении — что сделано, когда, кем, и preventive-действие («добавить в мониторинг проверку подключения к 5432 и алерт на `systemctl is-active`»). Урок 60 даст инструмент, чтобы автоматизировать проверку.

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен PostgreSQL 16, кластер `main`, порт 5432, слушает `10.10.10.30`; установлен MySQL 8.0.x, порт 3306, только `127.0.0.1`.
- **PostgreSQL:** роль `app_vektor`, база `vektor_db` (владелец `app_vektor`), таблица `payments` (1000 строк), проверочная база `vektor_db_restore`.
- **MySQL:** база `vektor_hr`, таблица `staff` (3 строки), пользователь `'app_vektor'@'10.10.10.%'` с правами SELECT/INSERT/UPDATE/DELETE.
- **Файлы:** `/var/backups/db/vektor_db.dump`, `/var/backups/db/vektor_hr.sql`.
- **Сеть:** ufw разрешает 5432/tcp из 10.10.10.0/24; 3306 снаружи закрыт.
- **Конфиги, которые правили:** `/etc/postgresql/16/main/postgresql.conf`, `/etc/postgresql/16/main/pg_hba.conf`.
- **Не делали:** не настраивали cron-бэкап (урок 60), не подключали 1С на SRV1C01 (отдельная тема), не настраивали репликацию.

### Откат

Полный откат: в VMware Workstation правый клик по UBU01 → **Snapshot → Snapshot Manager** → выбрать `before-l2-057` → **Restore**. Стенд вернётся к состоянию «до установки СУБД»: пакеты, базы, правила ufw и файлы бэкапов исчезнут.

Частичный откат без снапшота (если нужно сохранить остальное):

```bash
sudo systemctl stop postgresql@16-main mysql
sudo apt purge -y postgresql* mysql-server* 
sudo rm -rf /var/lib/postgresql /var/lib/mysql /etc/postgresql /etc/mysql /var/backups/db
sudo ufw delete allow from 10.10.10.0/24 to any port 5432 proto tcp
```

⚠️ `apt purge` и `rm -rf` удаляют данные безвозвратно — на стенде это допустимо, в боевой среде так делать нельзя никогда без подтверждённого бэкапа.

## 6. Ошибки новичков и безопасность

1. **Приложение работает под `postgres`/`root`.** → Одна SQL-инъекция или баг в приложении = полный контроль над всеми базами сервера. → Отдельная роль с правами только на свою базу и только на нужные операции.
2. **`GRANT ALL ON *.* ... WITH GRANT OPTION` «чтобы точно заработало».** → Пользователь может выдать права дальше и удалить чужие базы. → Перечисляй права поимённо, `WITH GRANT OPTION` — никогда для приложения.
3. **`bind-address = 0.0.0.0` + правило ufw `allow 3306` без источника.** → База открыта всей сети, включая гостевой Wi-Fi. → Слушать конкретный адрес (`10.10.10.30`) и разрешать порт только из нужной подсети; в `pg_hba.conf` — тоже по подсети.
4. **Бэкап есть, восстановление не проверялось.** → В момент аварии выясняется, что дамп пустой или снят не с той базы. → Восстанавливать в отдельную базу после каждого изменения схемы бэкапирования; фиксировать результат.
5. **`mysqldump` без `--single-transaction` на живой InnoDB.** → Дамп «рвётся» между таблицами, внешние ключи не сходятся. → Всегда `--single-transaction`, для больших баз — `mariabackup`/физический бэкап.
6. **Правка `pg_hba.conf` без понимания порядка правил.** → Строка `host all all 0.0.0.0/0 reject` выше нужной блокирует всех, и это выглядит как «база сломалась». → Правила читаются сверху вниз, частные — выше общих; после правки `reload` и проверка подключением.
7. **`systemctl restart` вместо `reload` «на всякий случай».** → Разрыв всех активных соединений и незавершённые транзакции в час пик. → `reload` для `pg_hba.conf`/прав, `restart` — только для параметров, требующих перезапуска, и в окно обслуживания.
8. **Игнорирование роста `pg_wal` и журналов.** → Диск забит, PostgreSQL уходит в read-only, MySQL не может писать. → Мониторить `df -h`, размер `/var/lib/pgsql`/`pg_wal`, ротацию логов.

### Безопасность

В боевой среде **нельзя**: держать базу доступной из интернета; хранить пароли в `~/.pgpass` или `~/.my.cnf` с правами шире `600`; использовать `mysql_native_password` без крайней необходимости (в MySQL 8.0 это deprecated, в 8.4 может быть отключён по умолчанию `[ПРОВЕРИТЬ]`); работать без TLS между приложением и БД. Нормальная практика: `ssl = on` в PostgreSQL, `require_secure_transport = ON` в MySQL, аутентификация SCRAM-SHA-256, отдельный бэкап-пользователь только на чтение, регулярный аудит `\du` и `SHOW GRANTS`. Для 1С на SRV1C01 пароль БД хранится в конфигурации сервера 1С — доступ к этой ВМ должен быть ограничен.

## 7. Вопросы на собеседовании

**1. Чем логический бэкап базы отличается от физического? Что выберете для базы 1С размером 50 ГБ?**

Ответ: логический (`pg_dump`, `mysqldump`) выгружает объекты и данные в переносимом виде — можно восстановить на другой версии, выборочно, но долго и с блокировками. Физический (`pg_basebackup` + WAL, `mariabackup`) копирует файлы — быстро, позволяет point-in-time recovery, но привязан к версии и платформе. Для 50 ГБ я бы сделал физический как основной (ежедневно) плюс логический раз в неделю для переносимости, и обязательно тестовое восстановление раз в квартал.

*Что хочет услышать:* понимание компромисса «скорость/гибкость», упоминание PITR и тестового восстановления.

*Красный флаг:* «делаю `pg_dump` каждый час, этого достаточно» — без проверки восстановления и без учёта времени восстановления.

**2. Приложение пишет «не удаётся подключиться к БД». Ваши первые действия?**

Ответ: иду по стеку снизу вверх. Сеть (`ping`, `ip a`), затем порт (`ss -tlnp`), затем сервис (`systemctl status`), затем журнал (`journalctl`, файл лога СУБД) на предмет `FATAL`/`Access denied`, затем проверяю подключение тем же пользователем вручную, затем `pg_hba.conf`/`bind-address` и пароль роли. Отдельно уточняю у пользователя точный текст ошибки — приложение часто скрывает реальную причину.

*Что хочет услышать:* системный порядок, а не «перезапущу и посмотрю».

*Красный флаг:* «сразу перезапускаю PostgreSQL» — это уничтожает улики и может оборвать транзакции.

**3. Чем с точки зрения администратора PostgreSQL отличается от MySQL?**

Ответ: в PostgreSQL один инстанс — несколько баз внутри одного каталога данных; роли общие, «кластер» = экземпляр. В MySQL «база» = схема, а учётная запись — пара `user`+`host`, и `'app'@'localhost'` ≠ `'app'@'10.10.10.%'`. Аутентификация: PostgreSQL — `pg_hba.conf` + SCRAM, локально часто `peer`; MySQL — плагины (`auth_socket`, `caching_sha2_password`). Процессная модель: PostgreSQL — процесс на соединение, MySQL — поток. Бэкап: `pg_dump`/`pg_basebackup` vs `mysqldump`/`mariabackup`.

*Что хочет услышать:* конкретику про `user@host` и `pg_hba.conf`, понимание, что команды не взаимозаменяемы.

*Красный флаг:* «это же одно и то же, просто логотипы разные».

**4. Как понять, что база тормозит и кто именно её грузит?**

Ответ: PostgreSQL — `pg_stat_activity` (долгие активные запросы, `client_addr`), `pg_locks WHERE NOT granted` (ожидание блокировок), `pg_stat_statements` для топа по времени, cache hit ratio из `pg_stat_database`. MySQL — `SHOW PROCESSLIST`, `SHOW ENGINE INNODB STATUS` (секция `TRANSACTIONS`, `LOCK WAIT`), slow query log с `long_query_time`. Плюс общий контекст: `Threads_connected` vs `max_connections`, `df -h`, `iostat`.

*Что хочет услышать:* конкретные вьюхи и запросы, а не «посмотрю в Zabbix».

*Красный флаг:* «убью самый долгий запрос» без выяснения, что это за запрос и нужен ли он.

**5. Как выдать приложению доступ к БД и при этом не дать ему лишнего?**

Ответ: отдельная роль/пользователь с `LOGIN`, но без суперправ. Своя база, где эта роль — владелец схемы, либо отдельная схема с `GRANT USAGE` + `GRANT SELECT, INSERT, UPDATE, DELETE` на нужные таблицы. `REVOKE ALL ON SCHEMA public FROM PUBLIC`. Только `CREATE USER`, без `WITH GRANT OPTION`, привязка к конкретной подсети (`'10.10.10.%'`). Пароль — случайный, из хранилища секретов, ротация по регламенту.

*Что хочет услышать:* принцип наименьших привилегий + понимание разницы между «владелец базы» и «суперпользователь».

*Красный флаг:* «дам `ALL PRIVILEGES` на `*.*`, потом разберёмся».

## 8. Источники (официальная документация)

- **postgresql.org** — раздел Documentation, актуальная версия PostgreSQL (16): главы «Backup and Restore», «Client Authentication» (`pg_hba.conf`), «Server Configuration», «Monitoring Database Activity». Поиск: `postgresql 16 pg_hba.conf documentation`, `postgresql pg_dump custom format`.
- **dev.mysql.com** — MySQL 8.0 Reference Manual: «Backup and Recovery», «Access Control and Account Management», «Server Status Variables», «The Slow Query Log». Поиск: `mysql 8.0 mysqldump single-transaction reference manual`.
- **mariadb.com/kb** — если в вашей компании используется MariaDB как замена MySQL: разделы про `mariabackup` и `mysql_secure_installation`. Поиск: `mariadb backup and restore mariabackup`.
- **ubuntu.com / help.ubuntu.com** — Server Guide, раздел про базы данных и пакеты Ubuntu. Поиск: `ubuntu server guide postgresql mysql`.
- **docs.astralinux.ru** `[ПРОВЕРИТЬ]` — документация Astra Linux: особенности репозиториев и состав пакетов СУБД, пригодится в уроке 59.
- **its.1c.ru** `[ПРОВЕРИТЬ доступность и раздел]` — документация 1С:Предприятие, раздел про сервер 1С и поддерживаемые СУБД; используется, когда будем связывать SRV1C01 с PostgreSQL.
