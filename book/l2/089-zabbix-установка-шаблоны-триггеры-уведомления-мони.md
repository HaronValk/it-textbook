# Zabbix: установка, шаблоны, триггеры, уведомления и мониторинг Windows/Linux/сети

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 09:12. В поддержку падает тикет:

> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01
> **Что написал:** «В пятницу вечером компьютер завис намертво, я потерял несохранённый отчёт. Никто не заметил, пока я не позвонил. Разве вы не мониторите компьютеры?»
> **Срочность:** средняя

Вот это «разве вы не мониторите» — и есть тема урока. На уровне L1 админ узнаёт о проблеме из звонка. На уровне L2 он узнаёт о ней из алерта — раньше пользователя. Zabbix — самая распространённая в российском SMB система мониторинга: бесплатная (GPLv2), ставится на один Linux-сервер, умеет смотреть Windows-хосты, Linux-хосты, сеть по SNMP, ловит события журналов и умеет будить дежурного письмом или в Telegram.

После этой главы ты должен уметь: развернуть Zabbix-сервер с нуля на Ubuntu, подключить к нему агента на Linux и Windows, включить сетевой хост по SNMP, написать собственный item и триггер, настроить уведомление по e-mail и проверить, что оно реально доходит. И, что важнее для L2, — уметь разобрать ситуацию «алерт есть, а проблемы нет» и «проблема есть, а алерта нет».

## 2. Теория «под капотом»

Zabbix — это **pull-система**: сервер сам опрашивает хосты по расписанию, а не ждёт, пока они что-то пришлют (push тоже есть, но как дополнение).

Компоненты:

- **zabbix_server** — ядро. Набор процессов-поллеров: `poller` (пассивные опросы), `unreachable poller` (хосты, которые не ответили), `trapper` (приём push-данных), `pinger` (ICMP), `housekeeper` (чистка старых данных), `history syncer` (запись в БД).
- **База данных** — MariaDB/MySQL или PostgreSQL. Хранит конфигурацию, историю метрик и события. Zabbix «тяжёлый по диску» — это нормально.
- **zabbix-frontend** — веб-интерфейс на PHP (Apache/nginx). Сам ничего не опрашивает, только читает БД и пишет конфигурацию.
- **zabbix_agent / agent2** — демон на хосте. `agent2` (на Go) — современный, с плагинами (WMI, Docker, systemd); `agent` (классический, на C) до сих пор используется.
- **zabbix_proxy** — «филиал» сервера, если хосты за медленным каналом.
- **zabbix_get / zabbix_sender** — утилиты ручной проверки: забрать значение с агента и отправить значение на сервер.

```text
                ┌───────────────── MON01 10.10.10.40 ──────────────────┐
                │ zabbix_server (poller/trapper/pinger/housekeeper)     │
                │ zabbix-frontend (PHP+Apache)   MariaDB (history)      │
                └───▲───────────────▲─────────────────▲──────────▲──────┘
   TCP 10050 passive│  TCP 10051 active│  TCP 10051 push │  UDP 161 │SNMP
                    │                 │                 │          │
              WIN11-CL01        UBU01 (agent2)   bash/python   GW01 (snmpd)
              DC01, FS01        ASTRA01          zabbix_sender
```

**Порты, которые надо помнить:**

| Порт | Протокол | Что это |
|---|---|---|
| 10051 | TCP | сервер/прокси: trapper + приём данных от активных агентов |
| 10050 | TCP | агент: пассивные проверки (сервер приходит к агенту) |
| 10052 | TCP | Zabbix Java Gateway (JMX) |
| 161 | UDP | SNMP-опрос устройств |
| 162 | UDP | SNMP trap принимает `snmptrapd`, а Zabbix **читает файл** его лога, а не слушает порт |
| 623 | UDP | IPMI (BMC серверов) |

**Цепочка данных:** *item* (что измеряем) → *history/trends* (куда пишем) → *trigger* (при каком условии это «проблема») → *event* → *action* (кому и как сообщить) → *media type* (e-mail, Telegram, скрипт).

**Пассивные и активные проверки.** Пассивная — сервер стучится в агент на 10050. Активная — агент сам подключается к серверу на 10051 и спрашивает «что мне собирать?»; для активных проверок критично, чтобы `Hostname` в конфиге агента **точно совпадал** с именем хоста в веб-интерфейсе. Это источник половины проблем «No data».

**Шаблоны (templates).** Набор item'ов, триггеров и макросов, который цепляется к хосту. В комплекте идут «Linux by Zabbix agent», «Windows by Zabbix agent», «Generic by SNMP» и десятки других. Внутри шаблонов пороги вынесены в макросы вида `{$VFS.FS.PUSED.MAX.WARN}` — их переопределяют на уровне хоста, не трогая шаблон.

**Единицы уровня:** модель OSI тут почти не нужна. Полезнее думать слоями: железо/ОС (агент, IPMI) → сервисы (порты, процессы) → приложение (логи, HTTP-проверки) → сеть (ICMP, SNMP).

## 3. Аналогия из жизни

Zabbix — это диспетчерская пультовая в больнице. Медсёстры (агенты) стоят у каждой палаты и по расписанию меряют температуру и давление (items), записывая цифры в карту (база данных). На стене висят правила, написанные главврачом: «температура выше 38.5 держится 10 минут — звонить дежурному» (trigger). Дальше срабатывает регламент: сначала звонок дежурному врачу, через 15 минут без реакции — заведующему отделению (action + escalation), утром — сообщение в сводку «пациент поправился» (recovery operation). На время планового ремонта крыла правила отключают, чтобы не поднимать всех зря (maintenance).

Аналогия ломается там, где заканчивается формальность: медсестра видит пациента и может сказать «ему плохо, хотя цифры нормальные», а триггер видит только цифры — поэтому «умный» мониторинг всегда требует человека, который периодически пересматривает пороги.

## 4. Практика и команды

**1. Установка сервера Zabbix на Ubuntu 24.04 (MON01).**
```bash
# официальный репозиторий Zabbix (актуальная LTS-ветка — 7.0; 7.2/7.4 новее — [ПРОВЕРИТЬ])
wget https://repo.zabbix.com/zabbix/7.0/ubuntu/pool/main/z/zabbix-release/zabbix-release_latest_7.0+ubuntu24.04_all.deb
dpkg -i zabbix-release_latest_7.0+ubuntu24.04_all.deb
apt update
apt install -y zabbix-server-mysql zabbix-frontend-php zabbix-apache-conf zabbix-sql-scripts zabbix-agent2 mariadb-server
```
Точное имя файла `zabbix-release_*.deb` меняется от версии к версии — сверяй на странице загрузки `zabbix.com/download` [ПРОВЕРИТЬ].

**2. База данных и импорт схемы.**
```bash
mysql -uroot -p
```
```sql
create database zabbix character set utf8mb4 collate utf8mb4_bin;
create user 'zabbix'@'localhost' identified by 'Lab#2026-vektor';
grant all privileges on zabbix.* to 'zabbix'@'localhost';
set global log_bin_trust_function_creators = 1;
quit;
```
```bash
zcat /usr/share/zabbix-sql-scripts/mysql/server.sql.gz | mysql --default-character-set=utf8mb4 -uzabbix -p zabbix
mysql -uroot -p -e "set global log_bin_trust_function_creators = 0;"
```
`utf8mb4_bin` — обязательное требование Zabbix, на другом collation ругается при установке. Флаг `log_bin_trust_function_creators` нужен только на время импорта.

**3. Конфиг сервера и запуск.**
```bash
sed -i 's/^# DBPassword=.*/DBPassword=Lab#2026-vektor/' /etc/zabbix/zabbix_server.conf
sed -i 's|^# php_value date.timezone.*|php_value date.timezone Europe/Moscow|' /etc/zabbix/apache.conf
systemctl restart zabbix-server zabbix-agent2 apache2
systemctl enable zabbix-server zabbix-agent2 apache2
```
Смотреть: `systemctl status` — `active (running)`, а не `activating (auto-restart)`. Веб-интерфейс: `http://10.10.10.40/zabbix`.

**4. Диагностика сервера по логу.**
```bash
tail -n 50 /var/log/zabbix/zabbix_server.log
grep -iE "cannot|error|failed" /var/log/zabbix/zabbix_server.log | tail
```
Типичный вывод при неверном пароле БД: `cannot connect to database: Access denied for user 'zabbix'@'localhost'`. При нехватке памяти под кэш: `cannot allocate shared memory`. В вебе при этом висит баннер «Zabbix server is not running: the information displayed may not be current».

**5. Проверка агента «снаружи» (с MON01).**
```bash
zabbix_get -s 10.10.10.30 -k agent.ping
# 1 — агент жив и ключ поддержан
zabbix_get -s 10.10.10.30 -k vfs.fs.size[/,pfree]
# 42.317812 — процент свободного места
zabbix_get -s 10.10.10.30 -k 'system.cpu.util[,idle]'
# 96.5
```
`ZBX_NOTSUPPORTED` — ключ неверный или запрещён `DenyKey`. `Timeout while executing a shell script` — таймаут. `Connection refused` — агент не слушает 10050 или файрвол.

**6. Проверка ключа локально, на самом хосте.**
```bash
zabbix_agent2 -t 'vfs.fs.size[/,pfree]'     # agent2
zabbix_agentd -t 'system.cpu.load[avg1]'     # классический agent
```
Показывает значение и тип (`[d|42.31]` — float). Лучший способ отладить ключ до добавления хоста в веб.

**7. Windows: установка агента и правило файрвола.**
```powershell
msiexec /i "zabbix_agent-7.0.0-windows-amd64-openssl.msi" /qn SERVER=10.10.10.40 SERVERACTIVE=10.10.10.40 HOSTNAME=WIN11-CL01
New-NetFirewallRule -DisplayName "Zabbix agent 10050" -Direction Inbound -Protocol TCP -LocalPort 10050 -Action Allow
Restart-Service "Zabbix Agent"
Get-Service "Zabbix Agent" | Select Name,Status
```
Параметры MSI: `SERVER` (кто может опрашивать), `SERVERACTIVE` (куда агенту подключаться для активных проверок), `HOSTNAME` (имя хоста в Zabbix). Установка без `/qn` даёт мастер. Конфиг лежит в `C:\Program Files\Zabbix Agent\zabbix_agentd.exe --config ... --install` — служебные ключи `-i` (установить), `-s` (старт), `-x` (стоп), `-d` (удалить) [ПРОВЕРИТЬ для конкретной версии].

**8. Проверка Windows-ключей с сервера.**
```bash
zabbix_get -s 10.10.10.100 -k 'perf_counter["\Processor(_Total)\% Processor Time"]'
# 7.8234  (адрес WIN11-CL01 выдаст DHCP; смотри его в интерфейсе DC01)
zabbix_get -s 10.10.10.100 -k 'service.info[Spooler,state]'
# 0 — служба работает (6 = остановлена)
```
`perf_counter` — мост к счётчикам производительности Windows (то же, что `Get-Counter` из урока 81). Ключ требует кавычек из-за `\` и `%`. Журнал событий: `eventlog[System,,Error]` — синтаксис параметров severity различается по версиям, проверяй по документации [ПРОВЕРИТЬ].

**9. WMI через agent2 (аналог `Get-CimInstance`).**
```bash
zabbix_get -s 10.10.10.100 -k 'wmi.get[root\cimv2,select FreeSpace from Win32_LogicalDisk where DeviceID="C:"]'
# 128374525952
```
Требует agent2 на Windows и прав учётной записи службы.

**10. Перечитать конфигурацию и запустить чистку без перезапуска.**
```bash
zabbix_server -R config_cache_reload
zabbix_server -R housekeeper_execute
```
Первое нужно после массовых изменений (импорт шаблона), второе — если ждёшь место на диске. Ключи `-R` работают через сокет и не рвут мониторинг.

**11. Push-значение откуда угодно (траппер).**
```bash
zabbix_sender -z 10.10.10.40 -s UBU01 -k custom.backup.status -o 1
# Response from "10.10.10.40:10051": "processed: 1; failed: 0; total: 1..."
```
`-z` сервер, `-s` имя хоста (ровно как в Zabbix), `-k` ключ item'а типа «Zabbix trapper», `-o` значение. Так отдают данные твои bash/python-скрипты из уроков 83–84: сделал бэкап — отправил 1, не сделал — 0.

**12. Сеть: SNMP-опрос и ICMP.**
```bash
apt install -y snmp snmpd fping
# на GW01 в /etc/snmp/snmpd.conf: agentAddress udp:161 и rocommunity vektorlab 10.10.10.40
snmpwalk -v2c -c vektorlab 10.10.10.3 system
# SNMPv2-MIB::sysUpTime.0 = Timeticks: (812331) 2:15:23.31
zabbix_get -s 10.10.10.3 -k 'icmpping'
# 1
```
Для «простых проверок» (`icmpping`, `icmppingloss`, `icmppingsec`) серверу нужен `fping` и путь к нему в `FpingLocation=/usr/sbin/fping`; утилите нужны права на raw-сокеты (`setcap cap_net_raw+ep /usr/sbin/fping`) [ПРОВЕРИТЬ]. SNMPv2c передаёт community открытым текстом — в бою нужен SNMPv3.

## 5. Лабораторная работа

### Предусловия

- **MON01** (10.10.10.40, Ubuntu Server 24.04) — существует, ставим Zabbix. Машина появляется на треке L2.
- **UBU01** (10.10.10.30) — Linux-хост для агента.
- **WIN11-CL01** — Windows-хост для агента (адрес по DHCP с DC01).
- **GW01** (10.10.10.3 / 10.10.20.1, Debian 12) — цель SNMP. Если глава про маршрутизатор ещё не пройдена и GW01 нет — делай SNMP-часть на UBU01 (`apt install snmpd`), логика та же.
- **DC01** — нужен его DNS/DHCP, чтобы WIN11-CL01 получил адрес.
- Доступ в интернет с MON01 для `repo.zabbix.com` и пакетов Ubuntu. Если репозиторий недоступен из РФ — легальная альтернатива: официальные Docker-образы `zabbix/zabbix-server-mysql` и `zabbix/zabbix-web-nginx-mysql` (Zabbix распространяется по GPLv2).

### Снапшот

В VMware Workstation на **MON01, UBU01, WIN11-CL01, GW01**: `VM → Snapshot → Take Snapshot`, имя `before-l2-089`. До старта Zabbix-сервера снапшот MON01 делается обязательно — установка с БД занимает 20–30 минут.

### Шаги

1. **MON01.** Установи пакеты (команды 1–3 из раздела 4), создай БД, импортируй схему, задай `DBPassword`, перезапусти службы. Ожидаемо: `systemctl status zabbix-server` → `active (running)`, в логе `zabbix_server started`.
2. **MON01.** Открой `http://10.10.10.40/zabbix`, войди как `Admin` (пароль по умолчанию `zabbix`), смени пароль на `Lab#2026-vektor`. Ожидаемо: мастер настройки → «Database: MySQL, host localhost, name zabbix» → успешная установка, затем логин в дашборд.
3. **Создай структуру.** `Data collection → Host groups` → группа `Vektor Linux`, `Vektor Windows`, `Vektor Network`. (В Zabbix 7.0 меню называется `Data collection`, в 6.0 — `Configuration`; пути немного отличаются по версиям.) Ожидаемо: три группы в списке.
4. **UBU01.** `apt install zabbix-agent2`, в `/etc/zabbix/zabbix_agent2.conf` задай `Server=10.10.10.40`, `ServerActive=10.10.10.40`, `Hostname=UBU01`, `systemctl restart zabbix-agent2`. Ожидаемо: `ss -lntp | grep 10050` показывает слушающий порт.
5. **MON01.** `zabbix_get -s 10.10.10.30 -k agent.ping` → `1`. Ожидаемо: единица; если `refused` — вернись к шагу 4 и проверь файрвол.
6. **Веб.** `Data collection → Hosts → Create host`: имя `UBU01`, группа `Vektor Linux`, интерфейс Agent IP `10.10.10.30` порт `10050`, шаблон `Linux by Zabbix agent`. Ожидаемо: через 1–2 минуты колонка `Availability` зелёная `ZBX`, в `Monitoring → Latest data` появились метрики CPU, памяти, дисков.
7. **WIN11-CL01.** Установи агент MSI (команда 7), открой порт, перезапусти службу. С MON01 проверь `perf_counter` (команда 8). Ожидаемо: число вместо ошибки.
8. **Веб.** Создай хост `WIN11-CL01`, группа `Vektor Windows`, интерфейс Agent с IP из DHCP, шаблон `Windows by Zabbix agent`. Ожидаемо: `ZBX` зелёный, в Latest data видны `vfs.fs.size[C:,pfree]`, `service.info[Spooler,state]`.
9. **Свой item и триггер.** На `WIN11-CL01`: `Items → Create item`, имя `Free space C: %`, ключ `vfs.fs.size[C:,pfree]`, тип Numeric (float), интервал `1m`. Затем `Triggers → Create trigger`: имя `C: free space < 10%`, severity `High`, выражение
   `min(/WIN11-CL01/vfs.fs.size[C:,pfree],5m)<10`
   Ожидаемо: триггер в состоянии OK (места на учебной ВМ много).
10. **Сеть.** На GW01 поставь `snmpd`, настрой community, проверь `snmpwalk` с MON01 (команда 12). В вебе создай хост `GW01`, группа `Vektor Network`, интерфейс **SNMP** версии 2, community `vektorlab`, порт `161`. Добавь item `sysUpTime` с ключом `1.3.6.1.2.1.1.3.0` (тип Numeric unsigned, единицы `uptime`). Ожидаемо: значение растёт в Latest data. Дополнительно повесь на `DC01` simple check `icmpping` и триггер `nodata(/DC01/icmpping,5m)=1` — «хост недоступен 5 минут».
11. **Уведомления.** На MON01: `apt install -y postfix mailutils`, при установке выбери «Internet Site», system mail name `mon01.corp.vektor.lab`. `echo test | mail -s "postfix test" root@localhost` и проверь `mail`. Затем в вебе: `Alerts → Media types → Email` — SMTP `127.0.0.1`, порт `25`, From `zabbix@mon01.corp.vektor.lab`, включено. Создай пользователя `adm.student` (группа `Zabbix administrators`) с media Email → `root@localhost`, период `1-7,00:00-24:00`, severity все.
12. **Действие (action).** `Alerts → Actions → Trigger actions → Create action`: имя `Vektor alerts`, условие `Trigger severity is greater than or equal to High`, операция «Send message to user `adm.student` via Email», текст с макросами `{HOST.NAME}`, `{EVENT.NAME}`, `{EVENT.SEVERITY}`. Ожидаемо: действие в списке со статусом Enabled.
13. **Проверка end-to-end.** На `WIN11-CL01` переопредели макрос шаблона (`Host macros`) `{$VFS.FS.PUSED.MAX.WARN}` нельзя — он в шаблоне; проще временно поставь свой триггер из шага 9 на `pfree<99`. Ожидаемо: в `Monitoring → Problems` появляется PROBLEM, через минуту в `/var/mail/root` на MON01 приходит письмо.
14. **Откат проверки и журнал.** Верни триггер в исходное выражение, дождись RESOLVED. Запиши изменения в `stand-log.md`.

### Проверка

- [ ] `systemctl status zabbix-server` на MON01 — `active (running)`, в логе нет `cannot connect to database`.
- [ ] `zabbix_get -s 10.10.10.30 -k agent.ping` возвращает `1`.
- [ ] В `Monitoring → Hosts` три хоста (`UBU01`, `WIN11-CL01`, `GW01`) с зелёным статусом доступности.
- [ ] В Latest data идут свежие значения по CPU, диску и `sysUpTime`.
- [ ] Свой триггер на свободное место виден в списке триггеров хоста с severity High.
- [ ] В `/var/mail/root` на MON01 есть письмо от Zabbix с именем хоста и текстом события.
- [ ] В `Alerts → Actions` действие включено, в `Reports → Action log` есть запись `sent`.
- [ ] Изменения записаны в `stand-log.md`.

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия) / **ВМ:** WIN11-CL01
> **Что написал:** «Мне каждую ночь около 02:10 приходят письма от Zabbix "PROBLEM: C: free space less than 10%" и через 15 минут "RESOLVED". Утром всё нормально. Уберите это, я не могу спать.»
> **Срочность:** средняя

**Как воспроизвести.** На хосте `WIN11-CL01` в `Host macros` переопредели `{$VFS.FS.PUSED.MAX.WARN}` значением `30` (вместо дефолтных 80) — это «ошибочно ужесточённый порог». Плюс на самом деле ночью VSS создаёт теневую копию и свободное место реально проседает. Триггер начинает дёргаться: `PROBLEM` → `RESOLVED` → `PROBLEM`.

**Диагностика по порядку.**

1. `Monitoring → Problems` с фильтром по хосту: смотрим, сколько событий и с какой периодичностью. Десятки PROBLEM/RESOLVED = флаппинг, а не авария.
2. `Monitoring → Latest data → vfs.fs.size[C:,pfree]` → Graph. Видим пилу: значение колеблется вокруг порога. Смотрим, в какие часы — 02:05–02:20, то есть ночной бэкап.
3. Открываем триггер: выражение `min(...,5m)<{$VFS.FS.PUSED.MAX.WARN}`. Проверяем макрос на хосте — `30`. Вот и причина ложных срабатываний.
4. Проверяем, реальна ли деградация: `Get-Volume C` и `vssadmin list shadows` на WIN11-CL01 (или `Get-WinEvent -LogName System` по источнику VSS). Теневая копия занимает несколько гигабайт и удаляется — это нормальная работа, а не инцидент.
5. Проверяем, не «спрятано» ли что-то важное: есть ли триггер `nodata` на агенте, есть ли условие по severity в action. Если action настроен на `>= High`, то Warning-события не придут — это отдельная мина.

**Решение.** Вернуть `{$VFS.FS.PUSED.MAX.WARN}` = `80` (Warning) и добавить второй триггер на `pfree<10` с severity High — он и должен будить. Для ночного окна бэкапа, если колебания всё же пересекают порог, создать `Data collection → Maintenance` с типом «With data collection» на 02:00–02:30 — подавление известного окна, а не отключение мониторинга. Дополнительно: гистерезис — не отправлять RESOLVED, если проблема вернулась в течение 10 минут (`Recovery expression` / интервал восстановления).

**Ответ пользователю:** «Иван Сергеевич, ночные письма приходили из-за слишком жёсткого порога на диске и ночного резервного копирования — сам компьютер в порядке. Порог исправлен, на ночное окно бэкапа настроено подавление. Настоящая тревога по диску осталась и срабатывает только при реальном риске заполнения. Проверьте, пожалуйста, что письма перестали приходить после 02:10 сегодня.»

**Закрытие заявки.** Категория «Мониторинг / ложные срабатывания», решение в описании, ссылка на событие в Zabbix, запись в `stand-log.md`. Если такие тикеты повторяются — повод завести постмортем по уроку 92 и пересмотреть пороги по всем хостам.

### Что изменилось на стенде

- **MON01** (10.10.10.40): установлены `zabbix-server-mysql`, `zabbix-frontend-php`, `zabbix-apache-conf`, `zabbix-sql-scripts`, `zabbix-agent2`, MariaDB, Postfix, `fping`.
- **MON01**: создана БД `zabbix` и пользователь БД `zabbix`; веб-интерфейс доступен по `http://10.10.10.40/zabbix`.
- **UBU01**: установлен `zabbix-agent2`, настроен на сервер `10.10.10.40`, `Hostname=UBU01`.
- **WIN11-CL01**: установлен Zabbix Agent, правило файрвола TCP 10050, `HOSTNAME=WIN11-CL01`.
- **GW01**: установлен `snmpd`, community `vektorlab`, доступ только с 10.10.10.40 (или SNMP-часть сделана на UBU01).
- **Zabbix:** группы хостов `Vektor Linux`, `Vektor Windows`, `Vektor Network`; хосты `UBU01`, `WIN11-CL01`, `GW01`; свои item и триггеры; media type Email; пользователь `adm.student` с media; action `Vektor alerts`.
- Все пароли — учебные из описания стенда.

### Откат

VMware Workstation → правый клик по ВМ → `Snapshots` → выбрать `before-l2-089` → `Revert`. Откатывать нужно **MON01** (там вся установка) и, если менял конфиги, **UBU01 / WIN11-CL01 / GW01**. Помни: откат MON01 уносит базу Zabbix целиком — сначала допиши `stand-log.md` и, если нужно, сделай дамп: `mysqldump -uzabbix -p zabbix > ~/zabbix-$(date +%F).sql`.

## 6. Ошибки новичков и безопасность

1. **Ставят Zabbix на тот же сервер, где БД продакшн-1С.** → Мониторинг и учётная система конкурируют за диск и память, при пике мониторинг роняет бизнес-систему. → Отдельная ВМ (у нас MON01), БД локально на ней.
2. **Не совпадает `Hostname` агента и имя хоста в вебе.** → Активные проверки не работают, статус «No data», а пассивные при этом зеленеют — сбивает с толку. → Имя в конфиге агента = имя хоста в Zabbix, символ в символ.
3. **Ставят порог «по умолчанию» и не пересматривают.** → Флаппинг и сотни писем, дежурный перестаёт читать алерты (alert fatigue). → Пороги под конкретный хост, оценка по 5–10 минутам (`min`/`avg`), а не по мгновенному `last`.
4. **Отключают триггер вместо починки.** → Реальная проблема приходит в тот момент, когда триггер выключен, и её никто не видит. → Для известных окон — `Maintenance`, для мусора — корректировка выражения, не Disable.
5. **Оставляют дефолтный пароль `Admin/zabbix` и порт 80 без TLS.** → Веб-интерфейс — это доступ ко всей инфраструктуре. → Смена пароля, отдельные пользователи вместо общей учётки, HTTPS, ограничение доступа по сети.
6. **Разрешают `system.run[*]` в агенте.** → Это удалённое выполнение команд на хосте для всех, кто дошёл до 10050. → Только `AllowKey` с узким списком, лучше — `UserParameter` с фиксированной командой.
7. **Игнорируют housekeeping и размер БД.** → Через месяц история съедает диск, сервер падает в `disk full`. → Периодически `zabbix_server -R housekeeper_execute`, контроль `Keep history/trends`, мониторинг самого MON01.
8. **SNMPv2c с community `public` на всю сеть.** → Любой в сети читает конфигурацию и трафик устройства. → SNMPv3 с авторизацией, community — только с IP сервера мониторинга.

**Безопасность.** В учебном стенде мы ставим простые пароли и SNMPv2c ради скорости — в боевой среде так нельзя: Zabbix-сервер это «центр наблюдения», его компрометация даёт карту всей сети. Минимум: HTTPS на фронтенде, отдельный сервисный пользователь БД только с правами на свою базу, агенты принимают подключения только с IP сервера (`Server=`), а из уведомлений не утекают внутренние адреса и учётные записи наружу.

## 7. Вопросы на собеседовании

**1. Чем пассивная проверка Zabbix отличается от активной и где вы это используете?**
Ответ: пассивная — сервер инициирует соединение к агенту на TCP 10050 и забирает значение по ключу; активная — агент сам подключается к серверу на 10051, получает список ключей и присылает данные. Активные удобны, когда хостов много или они за NAT/файрволом: входящие соединения к агенту не нужны. Классическая ошибка — расхождение `Hostname` и имени хоста в вебе, из-за чего активные проверки молчат.
*Что хочет услышать интервьюер:* понимание направления соединения и портов, а не заученное определение.
*Красный флаг:* «это одно и то же, просто галочка в настройках».

**2. Пользователь говорит, что алерты не приходят. Как разбираете?**
Ответ: иду по цепочке сверху вниз. Есть ли событие: `Monitoring → Problems` и `Events` — если события нет, проблема в item/триггере (нет данных, неверное выражение). Событие есть — проверяю action: включён ли, проходит ли по условиям (группа хостов, severity, теги), не стоит ли хост в maintenance. Дальше — получатель: назначены ли media пользователю, включены ли они, попадает ли severity в диапазон media, не в `Disabled` ли пользователь. Затем media type: тест кнопкой `Test` в `Alerts → Media types`, смотрю `Reports → Action log` (`sent` / `failed`) и логи (`zabbix_server.log`, для писем — `/var/log/mail.log`).
*Что хочет услышать интервьюер:* системный обход цепочки, а не «перезапустил сервер».
*Красный флаг:* сразу лезет в конфиг сервера, минуя проверку факта события.

**3. Что такое шаблон и зачем макросы `{$...}`?**
Ответ: шаблон — переиспользуемый набор item'ов, триггеров, правил discovery и макросов; хост может быть привязан к нескольким шаблонам, конфликты решаются привязкой к конкретному хосту. Макросы выносят пороги из логики: триггер один, а значение `{$VFS.FS.PUSED.MAX.WARN}` своё для сервера БД и для файлового сервера. Переопределяем на уровне хоста, шаблон не трогаем — иначе следующее обновление шаблона затрёт правки.
*Что хочет услышать интервьюер:* понимание, что шаблоны общие, а правки — на хосте.
*Красный флаг:* «правлю триггер прямо в шаблоне, так быстрее».

**4. Как мониторить сеть и сетевые устройства?**
Ответ: для доступности — простые проверки `icmpping`, `icmppingloss`, `icmppingsec` (нужен `fping` на сервере), для оборудования — SNMP: интерфейс типа SNMP, community/v3, OID'ы стандартных MIB (uptime, ifInOctets/ifOutOctets, ошибки на портах), шаблоны серии «by SNMP». Трапы принимает `snmptrapd`, Zabbix читает его лог. Триггер на `nodata` ловит «устройство замолчало», триггеры на утилизацию интерфейса — «канал забит».
*Что хочет услышать интервьюер:* разницу между ICMP-доступностью и SNMP-метриками, упоминание `nodata`.
*Красный флаг:* «поставил пинговать и хватит».

**5. Сервер Zabbix через сутки после установки перестал показывать данные. Ваши действия?**
Ответ: сначала факты. Веб-баннер «Zabbix server is not running» → `systemctl status zabbix-server` и хвост `/var/log/zabbix/zabbix_server.log` (падение, OOM, потеря БД). Если сервер жив, но данных нет — `df -h` на MON01 (классика: кончилось место из-за истории), проверка housekeeping, `zabbix_get` до агента вручную (сеть/файрвол/агент), проверка времени на хостах, `zabbix_server -R config_cache_reload` после массовых изменений. И отдельно — не истёк ли срок/не отвалился ли доступ к внешнему сервису, если используем webhook-уведомления.
*Что хочет услышать интервьюер:* порядок «мониторинг самого мониторинга» и типовые причины (диск, БД, сеть).
*Красный флаг:* «переустановлю Zabbix».

## 8. Источники (официальная документация)

- **Zabbix Documentation** (zabbix.com) — раздел «Installation from packages», поиск: *zabbix 7.0 install ubuntu mysql packages*.
- **Zabbix Documentation** — «Zabbix agent / Zabbix agent 2», «Windows-specific item keys» (`perf_counter`, `service.info`, `eventlog`), поиск: *zabbix agent 2 windows item keys perf_counter*.
- **Zabbix Documentation** — «Trigger functions», «Expressions and operators», поиск: *zabbix trigger functions nodata min avg*.
- **Zabbix Documentation** — «Media types», «Actions», «Maintenance», поиск: *zabbix trigger actions escalation media types e-mail*.
- **Zabbix Documentation** — «SNMP monitoring», «Simple checks (icmpping)», поиск: *zabbix snmp monitoring simple checks fping*.
- **ubuntu.com** — Server documentation, раздел про пакетный менеджмент и systemd, поиск: *ubuntu server 24.04 apt systemd service*.
- **mariadb.org** — Knowledge Base, «How to set up MariaDB for Zabbix» [ПРОВЕРИТЬ название статьи], поиск: *mariadb utf8mb4_bin zabbix database setup*.
- **Net-SNMP** (net-snmp.sourceforge.net) — man-страницы `snmpd.conf`, `snmpwalk`, поиск: *net-snmp snmpd.conf rocommunity agentAddress*.
- **Postfix** (postfix.org) — BASIC_CONFIGURATION_README, поиск: *postfix basic configuration local only internet site*.
- Номера версий Zabbix и точные имена пакетов/файлов релиза меняются — сверяй на `zabbix.com/download` перед установкой.
