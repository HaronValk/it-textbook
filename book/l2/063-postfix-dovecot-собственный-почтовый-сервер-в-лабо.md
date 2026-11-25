# Postfix + Dovecot: собственный почтовый сервер в лаборатории

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку: **Пользователь:** ivanov / **ВМ:** WIN11-CL01 / **Что написал:** «Мне для тестовой задачи нужен ящик `ivanov@corp.vektor.lab`, чтобы принимать письма от робота склада и отвечать на них. В Exchange-то у нас ничего нет, а на публичные почты отправлять тестовые письма запрещено. Сделайте внутренний почтовый сервер, я проверю в Thunderbird». **Срочность:** средняя.

Такая заявка — классика. Внутренняя почта нужна и для уведомлений от 1С, и для мониторинга (Zabbix любит отправлять алерты по SMTP), и для сканеров с функцией Scan-to-Email, и для тестовых стендов. Не каждая компания тянет Exchange или платный облачный ящик для каждой мелочи: сервер на Postfix + Dovecot — это бесплатно, ставится за час и полностью под твоим контролем.

После главы ты будешь уметь: поставить Postfix как MTA и Dovecot как MDA, настроить виртуальные ящики без системных пользователей, включить SMTP-аутентификацию и TLS, читать очередь Postfix по письмам и находить причину «отправил, а не дошло», проверять IMAP-доступ через `doveadm` и объяснять на собеседовании, где заканчивается MTA и начинается MDA.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

В L1 и уроке 61 ты уже разбирал общую схему: MTA (Mail Transfer Agent) — тот, кто передаёт письмо по SMTP между серверами и принимает его от клиента, MDA (Mail Delivery Agent) — тот, кто раскладывает письмо в ящик, MUA (Mail User Agent) — программа пользователя (Thunderbird, Outlook, веб-клиент). Postfix — MTA. Dovecot — MDA + IMAP/POP3-сервер + сервер аутентификации для Postfix.

Почему их ставят парой: Postfix очень хорош в SMTP-логике (очереди, ретраи, проверки, TLS, антиспам-хуки), но класть письмо в ящик пользователя он умеет только примитивно. Dovecot же умеет Maildir, индексы для быстрого IMAP, квоты, поиск, LMTP и хранение паролей. Поэтому стандартная связка: Postfix принимает → передаёт письмо Dovecot по LMTP → Dovecot кладёт в Maildir → пользователь ходит в Dovecot по IMAP и забирает.

```text
        MUA: Thunderbird на WIN11-CL01 (ivanov)
        │  отправка: SMTP 587 + STARTTLS + AUTH
        │  получение: IMAP 993 (TLS)
        │
   ┌────▼────────────────────────┐          ┌────────────────────────────┐
   │ Postfix — MTA (UBU01)       │          │ Dovecot — IMAP/POP3        │
   │  :25  smtpd (приём извне)   │          │  :143 imap (STARTTLS)      │
   │  :587 smtpd (submission)    │          │  :993 imaps (TLS)          │
   │  cleanup → qmgr → lmtp      │          │  passdb: /etc/dovecot/users│
   └────┬────────────────────────┘          └───────────▲────────────────┘
        │  LMTP через unix-сокет                        │
        │  /var/spool/postfix/private/dovecot-lmtp      │
        └──────────────────────► Dovecot LMTP ──────────┘
                                 (MDA)
                                     │
                                     ▼
                        Maildir: /var/vmail/corp.vektor.lab/ivanov/Maildir
                        (cur/ new/ tmp/ — один файл = одно письмо)
```

| Порт | Протокол | Кто слушает | Зачем |
|------|----------|-------------|-------|
| 25 | SMTP | Postfix | приём почты от других серверов (MX) |
| 587 | SMTP submission | Postfix | отправка от клиентов, обязателен AUTH |
| 143 | IMAP | Dovecot | почта, желательно STARTTLS |
| 993 | IMAPS | Dovecot | IMAP сразу в TLS |
| 110/995 | POP3/POP3S | Dovecot | редко нужен, в корпоративе почти не используется |
| 4190 | ManageSieve | Dovecot | серверные фильтры (опционально) |

Ключевые внутренние сущности Postfix: `/etc/postfix/main.cf` (параметры), `/etc/postfix/master.cf` (какие сервисы вообще запущены и на каких портах), очередь в `/var/spool/postfix/` — подкаталоги `incoming`, `active`, `deferred`, `hold`, `corrupt`. Письмо, попавшее в `deferred`, живёт там до истечения `maximal_queue_lifetime` (по умолчанию 5 дней), Postfix пытается доставить его повторно по расписанию и в итоге присылает отправителю bounce.

Второй слой — DNS. Чтобы письмо `ivanov@corp.vektor.lab` вообще нашло дорогу, для домена `corp.vektor.lab` нужна MX-запись, указывающая на `mail.corp.vektor.lab`, и A-запись этого имени на IP сервера. У нас зона `corp.vektor.lab` уже обслуживается DNS на DC01, туда и добавим. SPF/DKIM/DMARC и PTR нужны для отправки во внешний мир — в лаборатории мы их не настраиваем, но помним об этом (подробно было в уроке 61).

## 3. Аналогия из жизни

Postfix — это сортировочный центр почты. Он принимает любой конверт, проверяет, правильный ли адрес и есть ли марка (аутентификация), сортирует по направлениям и, если адресат в другом городе, договаривается с чужим центром о передаче. Если центр-получатель недоступен, письмо не выбрасывают: оно лежит в мешке «повторить позже» (очередь `deferred`) и ездит по попыткам, пока не истечёт срок.

Dovecot — это уже само отделение с абонентскими ящиками. Он не занимается междугородней логистикой, он хранит конверты в персональных ячейках и выдаёт их владельцу по предъявлении пропуска (логин и пароль). Maildir — это когда каждое письмо лежит отдельным файлом в папке `new/`, а не одним толстым архивным ящиком: так проще бэкапить, индексировать и не терять всё при сбое.

Аналогия ломается на переадресации: в реальном отделении нельзя по щелчку сделать так, чтобы все письма на имя «Иванов» физически падали в ящик «Петровой», а Postfix это делает одной строчкой в карте алиасов.

## 4. Практика и команды

Дальше всё считается выполненным на UBU01 (10.10.10.30), где мы и развернём почту. Логи — `/var/log/mail.log`.

**1. Что реально изменено в конфиге**

```bash
postconf -n            # только не-дефолтные параметры
postconf -d mynetworks # значение по умолчанию конкретного параметра
```

`-n` — самый полезный ключ: 90% проблем ищутся сравнением «что я думаю, что настроил» и «что Postfix реально загрузил». Пример вывода:

```text
alias_maps = hash:/etc/aliases
inet_interfaces = all
myhostname = mail.corp.vektor.lab
mydomain = corp.vektor.lab
myorigin = $mydomain
smtpd_sasl_auth_enable = yes
smtpd_sasl_path = private/auth
smtpd_tls_cert_file = /etc/ssl/certs/mail.crt
virtual_mailbox_domains = corp.vektor.lab
virtual_transport = lmtp:unix:private/dovecot-lmtp
```

**2. Какие сервисы вообще запущены**

```bash
postconf -M | grep -E '^\s*(smtp|submission|smtps)\s'
postconf -P "submission/inet/smtpd_tls_security_level"
```

`-M` показывает master.cf в нормализованном виде, `-P` — параметры отдельного сервиса. Самая частая причина «клиент не может отправить» — сервис `submission` в master.cf закомментирован (в дефолтном файле он есть, но выключен).

**3. Проверка конфига и перезагрузка**

```bash
postfix check          # синтаксис и права на файлы
systemctl reload postfix
systemctl status postfix
```

`postfix check` ловит опечатки в путях и неправильные права до `reload`. После `reload` в логе будет строка `postfix/postfix-script: refreshing the Postfix mail system`.

**4. Очередь: что там лежит и почему**

```bash
postqueue -p           # то же, что mailq
postqueue -j           # JSON-вывод, удобно парсить скриптом
```

Пример (сокращённо):

```text
-Queue ID-  --Size-- ----Arrival Time---- -Sender/Recipient-------
4Yq2pL3xKz1Bq     512 Mon Mar  2 10:14:05  petrova@corp.vektor.lab
(connect to private/dovecot-lmtp: No such file or directory)
                                         ivanov@corp.vektor.lab
```

В скобках — **причина** задержки. Именно её читаем первой, а не гадаем.

**5. Посмотреть само письмо**

```bash
postcat -q 4Yq2pL3xKz1Bq     # по ID из очереди
postcat -q 4Yq2pL3xKz1Bq -h  # только заголовки
```

Видно конверт (MAIL FROM/RCPT TO), заголовки `Received`, `Message-ID`. Это нужно, чтобы понять, на каком хопе письмо застряло и не подменён ли адрес алиасом.

**6. Действия с очередью**

```bash
postqueue -f                 # форсировать повторную попытку для всей очереди
postqueue -i 4Yq2pL3xKz1Bq   # только для одного письма
postsuper -r ALL             # переставить письма в очередь заново (после правки конфига)
postsuper -d 4Yq2pL3xKz1Bq   # удалить одно письмо
postsuper -h ALL             # отправить всю очередь в hold, чтобы не улетело
```

`postsuper -d ALL` — крайняя мера. Сначала читаем причину, потом чиним, потом `postqueue -f`. Удаление очереди без разбора — это выброшенные письма пользователей.

**7. Логи**

```bash
grep -E 'postfix/(smtpd|qmgr|lmtp)' /var/log/mail.log | tail -30
journalctl -u postfix@- --since "30 min ago"
```

Что искать: `NOQUEUE: reject` (отказ на этапе SMTP), `status=sent` (доставлено), `status=deferred` (задержка), `warning:` (нехватка места, таймауты). Пример строки отказа:

```text
postfix/smtpd[2143]: NOQUEUE: reject: RCPT from unknown[10.10.10.101]:
 554 5.7.1 <ivanov@corp.vektor.lab>: Relay access denied;
 from:<petrova@corp.vektor.lab> to:<ivanov@corp.vektor.lab> proto=ESMTP
```

**8. Кто на каких портах слушает**

```bash
ss -tlnp | grep -E ':(25|587|993|143)\b'
ss -lx | grep -i dovecot        # unix-сокеты LMTP и SASL
```

Оба сокета — `/var/spool/postfix/private/auth` и `.../dovecot-lmtp` — являются точкой стыка двух сервисов. Нет сокета — нет доставки.

**9. Проверка аутентификации в Dovecot**

```bash
doveadm auth test ivanov@corp.vektor.lab
```

Спросит пароль и ответит `auth succeeded` либо `auth failed`. Так проверяется вход по IMAP, не открывая почтовый клиент.

**10. Что в ящике и сколько места**

```bash
doveadm mailbox list -u ivanov@corp.vektor.lab
doveadm mailbox status -u ivanov@corp.vektor.lab messages vsize INBOX
doveadm quota get -u ivanov@corp.vektor.lab   # если включены квоты
```

Если письмо в логе помечено `status=sent`, а пользователь его «не видит» — проверяем этим, вдруг оно в другой папке или в другом ящике (частая история с алиасами).

**11. Пароль для Dovecot**

```bash
doveadm pw -s SHA512-CRYPT
```

Генерирует хеш для файла `/etc/dovecot/users`. Ключ `-p 'пароль'` подставлять в командную строку не стоит: пароль останется в истории shell.

**12. Тестовая отправка письма с аутентификацией**

```bash
swaks --to ivanov@corp.vektor.lab --from petrova@corp.vektor.lab \
      --server 10.10.10.30 --port 587 --tls --auth LOGIN \
      --auth-user petrova@corp.vektor.lab \
      --header "Subject: probe from swaks" --body "test"
```

`swaks` — «швейцарский нож» для SMTP: показывает весь диалог с сервером, коды ответов и результат аутентификации. По умолчанию сертификат не проверяет (в лаборатории это удобно), в боевой проверке добавляют `--tls-verify` [ПРОВЕРИТЬ синтаксис ключа в вашей версии].

**13. Ручной диалог с SMTP и проверка TLS**

```bash
nc 10.10.10.30 25
220 mail.corp.vektor.lab ESMTP Postfix
EHLO win11-cl01
250 2.1.0 Ok
MAIL FROM:<petrova@corp.vektor.lab>
250 2.1.0 Ok
RCPT TO:<ivanov@corp.vektor.lab>
250 2.1.5 Ok
DATA
354 End data with <CR><LF>.<CR><LF>
Subject: hello
test
.
250 2.0.0 Ok: queued as 4Yq2pL3xKz1Bq
```

И проверка сертификата на IMAPS:

```bash
openssl s_client -connect 10.10.10.30:993 -servername mail.corp.vektor.lab
```

Смотрим строки `subject=`, `issuer=`, даты, и что соединение не падает с ошибкой.

**14. Со стороны Windows**

```powershell
Resolve-DnsName corp.vektor.lab -Type MX
Test-NetConnection 10.10.10.30 -Port 587
Test-NetConnection 10.10.10.30 -Port 993
```

`Resolve-DnsName -Type MX` покажет, куда Windows вообще собирается слать почту для домена (спросит у DC01). `Test-NetConnection` даёт `TcpTestSucceeded : True` — быстрый ответ на вопрос «сеть и порт живы?».

**15. Проверка таблиц-карт**

```bash
postmap -q ivanov@corp.vektor.lab hash:/etc/postfix/vmailbox
doveconf -n
```

`postmap -q` показывает, что Postfix увидит в карте. Если вернулась пустая строка — вы забыли `postmap /etc/postfix/vmailbox` после правки файла (правки в `.db` сами не попадут), и ящик будет считаться несуществующим. `doveconf -n` — аналог `postconf -n` для Dovecot.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Почтовый сервер ставим на **UBU01 (10.10.10.30)** — отдельную ВМ под почту на треке L2 мы не заводим, а в реальной работе этот сервер жил бы в DMZ за GW01 (10.10.20.0/24) с пробросом портов. Сейчас он в офисной сети, и это осознанное упрощение лаборатории.

### Предусловия

- Стенд L2 готов: DC01 (AD DS + DNS + DHCP, 10.10.10.10), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01, домен `corp.vektor.lab`.
- UBU01 доступен по SSH, есть пользователь с sudo, интернет через NAT VMnet8 работает (`apt update` проходит).
- WIN11-CL01 в сети, пользователь `ivanov` может войти.
- На DC01 работает оснастка DNS, зона `corp.vektor.lab` есть.
- Важно не путать: учётные записи AD (`ivanov` в домене VEKTOR) и почтовые ящики `ivanov@corp.vektor.lab` — это **разные** базы пользователей. На UBU01 свой файл паролей. В боевой среде их связывают через LDAP/Kerberos (подход из урока 58), но в этой лаборатории — отдельно.

### Снапшот

В VMware Workstation на **UBU01**, **DC01** и **WIN11-CL01**: меню `VM → Snapshot → Take Snapshot`, имя `before-l2-063`. Или из командной строки хоста, если установлен `vmrun`:

```text
vmrun snapshot "C:\VMs\UBU01\UBU01.vmx" before-l2-063
```

### Шаги

1. **UBU01.** Проверяем имя и обновляем пакеты:
   ```bash
   hostnamectl
   sudo apt update
   ```
   Ожидаемо: имя `ubu01`, список пакетов обновился без ошибок.

2. **UBU01.** Ставим пакеты:
   ```bash
   sudo apt install postfix dovecot-core dovecot-imapd dovecot-lmtpd mailutils swaks
   ```
   В интерактивном окне Postfix выбираем `Internet Site`, системное имя почты — `corp.vektor.lab`. Ожидаемо: сервисы `postfix` и `dovecot` запущены (`systemctl status postfix dovecot`).

3. **UBU01.** Создаём владельца ящиков и каталог:
   ```bash
   sudo groupadd -g 5000 vmail
   sudo useradd -u 5000 -g vmail -d /var/vmail -m -s /usr/sbin/nologin vmail
   sudo mkdir -p /var/vmail && sudo chown -R vmail:vmail /var/vmail
   ```
   Ожидаемо: `id vmail` показывает `uid=5000(vmail) gid=5000(vmail)`. Отдельный пользователь без shell — так ящики не превращаются в системные учётки.

4. **DC01.** В `DNS → Forward Lookup Zones → corp.vektor.lab` создаём узел: `New Host (A or AAAA)` → имя `mail`, IP `10.10.10.30`. Затем правый клик по зоне → `New Mail Exchanger (MX)` → `mail.corp.vektor.lab`, preference `10`. Ожидаемо: `nslookup mail.corp.vektor.lab` с WIN11-CL01 отдаёт 10.10.10.30. [ПРОВЕРИТЬ: создание MX на apex зоны через `Add-DnsServerResourceRecordMX` в PowerShell — в GUI это пункт «New Mail Exchanger» у корня зоны.]

5. **UBU01.** Базовая настройка Postfix:
   ```bash
   sudo postconf -e 'myhostname = mail.corp.vektor.lab'
   sudo postconf -e 'mydomain = corp.vektor.lab'
   sudo postconf -e 'myorigin = $mydomain'
   sudo postconf -e 'inet_interfaces = all'
   sudo postconf -e 'mydestination = $myhostname, localhost.$mydomain, localhost'
   sudo postconf -e 'virtual_mailbox_domains = corp.vektor.lab'
   sudo postconf -e 'virtual_mailbox_maps = hash:/etc/postfix/vmailbox'
   sudo postconf -e 'virtual_transport = lmtp:unix:private/dovecot-lmtp'
   sudo postconf -e 'smtpd_sasl_type = dovecot'
   sudo postconf -e 'smtpd_sasl_path = private/auth'
   sudo postconf -e 'smtpd_sasl_auth_enable = yes'
   sudo postconf -e 'smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated reject_unauth_destination'
   ```
   Ключевое: домен `corp.vektor.lab` **не** входит в `mydestination` (иначе Postfix будет искать локальных юникс-пользователей), а `virtual_transport` отправляет письма в Dovecot по LMTP.

6. **UBU01.** Список ящиков и карта:
   ```bash
   sudo tee /etc/postfix/vmailbox >/dev/null <<'EOF'
   ivanov@corp.vektor.lab    corp.vektor.lab/ivanov/
   petrova@corp.vektor.lab   corp.vektor.lab/petrova/
   sidorov@corp.vektor.lab   corp.vektor.lab/sidorov/
   postmaster@corp.vektor.lab corp.vektor.lab/postmaster/
   EOF
   sudo postmap /etc/postfix/vmailbox
   ```
   Ожидаемо: появился `/etc/postfix/vmailbox.db`, а `postmap -q ivanov@corp.vektor.lab hash:/etc/postfix/vmailbox` возвращает путь. При LMTP-доставке этот путь не используется для записи — карта нужна Postfix только как ответ «такой ящик существует».

7. **UBU01.** Включаем submission в master.cf:
   ```bash
   sudo postconf -M submission/inet="submission inet n - y - - smtpd"
   sudo postconf -P "submission/inet/syslog_name=postfix/submission"
   sudo postconf -P "submission/inet/smtpd_tls_security_level=encrypt"
   sudo postconf -P "submission/inet/smtpd_sasl_auth_enable=yes"
   sudo postconf -P "submission/inet/smtpd_client_restrictions=permit_sasl_authenticated,reject"
   ```
   Ожидаемо: `postconf -M | grep submission` показывает активный сервис. Клиенты отправляют только через 587 и только после AUTH — порт 25 остаётся для приёма почты от других серверов.

8. **UBU01.** Самоподписанный сертификат для лаборатории:
   ```bash
   sudo openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
     -keyout /etc/ssl/private/mail.key -out /etc/ssl/certs/mail.crt \
     -subj "/CN=mail.corp.vektor.lab"
   sudo chmod 640 /etc/ssl/private/mail.key
   sudo chown root:postfix /etc/ssl/private/mail.key
   sudo postconf -e 'smtpd_tls_cert_file = /etc/ssl/certs/mail.crt'
   sudo postconf -e 'smtpd_tls_key_file = /etc/ssl/private/mail.key'
   sudo postconf -e 'smtpd_tls_security_level = may'
   ```
   В боевой среде — только сертификат от удостоверяющего центра; самоподписанный там означает «пользователи увидят предупреждение и привыкнут его игнорировать», а это прямой путь к MITM.

9. **UBU01.** Конфиг Dovecot. Правим `/etc/dovecot/local.conf` (Ubuntu подключает его последним, переопределяя остальное):
   ```text
   protocols = imap lmtp
   mail_location = maildir:~/Maildir
   disable_plaintext_auth = yes
   ssl = yes
   ssl_cert = </etc/ssl/certs/mail.crt
   ssl_key = </etc/ssl/private/mail.key

   passdb {
     driver = passwd-file
     args = /etc/dovecot/users
   }
   userdb {
     driver = static
     args = uid=vmail gid=vmail home=/var/vmail/%d/%n
   }

   service auth {
     unix_listener /var/spool/postfix/private/auth {
       mode = 0660
       user = postfix
       group = postfix
     }
   }
   service lmtp {
     unix_listener /var/spool/postfix/private/dovecot-lmtp {
       mode = 0600
       user = postfix
       group = postfix
     }
   }
   protocol lmtp {
     postmaster_address = postmaster@corp.vektor.lab
   }
   ```
   Затем пользователи:
   ```bash
   sudo doveadm pw -s SHA512-CRYPT           # сгенерировать хеш пароля стенда
   sudo tee /etc/dovecot/users >/dev/null <<'EOF'
   ivanov@corp.vektor.lab:{SHA512-CRYPT}$6$...вставьте_свой_хеш...
   petrova@corp.vektor.lab:{SHA512-CRYPT}$6$...вставьте_свой_хеш...
   EOF
   sudo chown root:dovecot /etc/dovecot/users
   sudo chmod 640 /etc/dovecot/users
   ```
   Ожидаемо: `doveconf -n` выводит наши директивы без ошибок, `doveadm auth test ivanov@corp.vektor.lab` отвечает `auth succeeded`.

10. **UBU01.** Файрвол и перезапуск:
    ```bash
    sudo ufw allow from 10.10.10.0/24 to any port 25,587,993,143 proto tcp
    sudo postfix check && sudo systemctl restart postfix dovecot
    ss -tlnp | grep -E ':(25|587|993)'
    ls -l /var/spool/postfix/private/dovecot-lmtp /var/spool/postfix/private/auth
    ```
    Ожидаемо: три порта в LISTEN, оба сокета существуют и принадлежат `postfix`.

11. **UBU01.** Тестовая отправка и доставка:
    ```bash
    swaks --to ivanov@corp.vektor.lab --from petrova@corp.vektor.lab \
          --server 10.10.10.30 --port 587 --tls --auth LOGIN \
          --auth-user petrova@corp.vektor.lab
    sudo tail -5 /var/log/mail.log
    sudo doveadm mailbox status -u ivanov@corp.vektor.lab messages INBOX
    ```
    Ожидаемо: в логе `postfix/lmtp ... status=sent (250 2.0.0 <ivanov@...> ...)`, а `messages=1`. Заголовок `Received` в письме показывает цепочку `by mail.corp.vektor.lab ... for <ivanov@corp.vektor.lab>`.

12. **WIN11-CL01.** Настраиваем Thunderbird: `Меню (≡) → Настройки → Учётные записи → Действия с учётной записью → Добавить учётную запись почты`. Имя `Иван Иванов`, адрес `ivanov@corp.vektor.lab`, пароль стенда, вводим вручную: входящая IMAP `mail.corp.vektor.lab` порт 993 SSL/TLS, исходящая SMTP `mail.corp.vektor.lab` порт 587 STARTTLS, имя пользователя — полный адрес. На предупреждение о самоподписанном сертификате в лаборатории жмём «Подтвердить исключение».
    Ожидаемо: ящик открывается, входящее письмо от `petrova` видно, ответ уходит без ошибок.

### Проверка

- [ ] `postconf -n` показывает `virtual_transport = lmtp:unix:private/dovecot-lmtp` и `mydestination` без `corp.vektor.lab`
- [ ] На DC01 есть A-запись `mail.corp.vektor.lab` → 10.10.10.30 и MX для `corp.vektor.lab`
- [ ] `ss -tlnp` на UBU01: порты 25, 587, 993 слушают
- [ ] Оба сокета в `/var/spool/postfix/private/` существуют, владелец `postfix`
- [ ] `doveadm auth test ivanov@corp.vektor.lab` → `auth succeeded`
- [ ] `swaks` через 587 с AUTH проходит, письмо доставлено (`status=sent`)
- [ ] В `/var/vmail/corp.vektor.lab/ivanov/Maildir/new` лежит файл письма
- [ ] Thunderbird на WIN11-CL01 отправляет и принимает почту от имени `ivanov@corp.vektor.lab`
- [ ] Отправка без пароля с порта 587 отклоняется (`SASL authentication required`)
- [ ] `stand-log.md` дополнен

### Сломай и почини

**Пользователь:** ivanov / **ВМ:** WIN11-CL01 / **Что написал:** «Отправил Марии письмо из Thunderbird — написало «отправлено», но у неё ничего нет уже 20 минут. И письмо от робота склада вернулось с ошибкой. Раньше работало». / **Срочность:** средняя.

**Как воспроизвести поломку (на UBU01):**

```bash
sudo systemctl stop dovecot
swaks --to ivanov@corp.vektor.lab --from petrova@corp.vektor.lab \
      --server 10.10.10.30 --port 587 --tls --auth LOGIN \
      --auth-user petrova@corp.vektor.lab
```

Swaks на этапе `--auth` уже упадёт (SASL обслуживает Dovecot), а если письма шли изнутри через pickup, они просто лягут в `deferred` с причиной «connect to private/dovecot-lmtp: No such file or directory».

**Диагностика по порядку:**

1. `postqueue -p` — есть ли письма и какая причина в скобках. Если видим `No such file or directory` про `dovecot-lmtp` — проблема на стыке Postfix↔Dovecot, дальше не гадаем.
2. `postcat -q <QID> -h` — убедиться, что адресат именно `ivanov@corp.vektor.lab` и письмо не ушло в другой ящик через алиас.
3. `grep -E 'postfix/(lmtp|qmgr|smtpd)' /var/log/mail.log | tail -30` — цепочка от приёма до попытки доставки.
4. `ls -l /var/spool/postfix/private/` и `ss -lx | grep dovecot` — сокетов нет? Значит, Dovecot не работает.
5. `systemctl status dovecot` и `grep -i dovecot /var/log/mail.log | tail` — почему не работает: падает конфиг, нет прав, кончилось место.
6. `df -h /var/vmail` и `ls -ld /var/vmail` — место и владелец `vmail:vmail`.
7. `doveconf -n` — конфиг читается без ошибок.

**Решение:** `sudo systemctl start dovecot`, затем `sudo postqueue -f` — очередь разошлется. Проверяем: `postqueue -p` → `Mail queue is empty`, `doveadm mailbox status -u ivanov@corp.vektor.lab messages INBOX` → счётчик вырос.

**Ответ пользователю:** «Добрый день! Сбой был на почтовом сервере — останавливалась служба доставки. Письма не потерялись, они лежали в очереди; после восстановления службы все доставлены, включая письмо робота склада. Проверьте, пожалуйста, ящик и подтвердите. При повторении пришлите время отправки».

**Закрытие заявки:** тип «Инцидент», категория «Почта», приоритет средний, в решении — «Остановлена служба Dovecot; запущена, очередь разобрана, доставка восстановлена. Рекомендация: включить мониторинг порта 993 и сервиса dovecot на MON01».

### Что изменилось на стенде

- **UBU01 (10.10.10.30)** — роль «почтовый сервер»: Postfix (MTA, порты 25 и 587), Dovecot (IMAP 143/993, LMTP), сертификат `/etc/ssl/certs/mail.crt`.
- **UBU01** — системный пользователь `vmail` (uid/gid 5000), каталог `/var/vmail`, ящики Maildir.
- **UBU01** — конфиги `/etc/postfix/main.cf`, `/etc/postfix/master.cf` (включён `submission`), `/etc/postfix/vmailbox(.db)`, `/etc/dovecot/local.conf`, `/etc/dovecot/users`.
- **UBU01** — правила ufw: 25/587/993/143 из 10.10.10.0/24.
- **DC01** — DNS: A `mail.corp.vektor.lab` → 10.10.10.30, MX `corp.vektor.lab` → `mail.corp.vektor.lab` (pref 10).
- **Почтовые ящики:** `ivanov@`, `petrova@`, `sidorov@`, `postmaster@corp.vektor.lab` (учётки почтового сервера, не AD).
- **WIN11-CL01** — профиль Thunderbird для `ivanov@corp.vektor.lab`.
- Записи для `stand-log.md`:
  ```text
  [l2-063] UBU01: Postfix+Dovecot, ящики корпоративной почты corp.vektor.lab, vmail uid 5000
  [l2-063] DC01: DNS A mail=10.10.10.30, MX corp.vektor.lab -> mail.corp.vektor.lab
  [l2-063] UBU01: smtp 25/587, imap 993, LMTP unix:/var/spool/postfix/private/dovecot-lmtp
  ```

### Откат

```text
VM → Snapshot → Snapshot Manager → before-l2-063 → Restore
```

Или `vmrun revertToSnapshot "C:\VMs\UBU01\UBU01.vmx" before-l2-063` (аналогично для DC01 и WIN11-CL01). Откат нужен, если испортил `main.cf` так, что `postfix check` не спасает: быстрее вернуть снапшот, чем разбирать синтаксис. Перед откатом допиши в `stand-log.md` строку «откатился на before-l2-063, причина такая-то».

## 6. Ошибки новичков и безопасность

1. **Домен в `mydestination` вместе с виртуальными ящиками** → Postfix ищет `ivanov` среди юникс-пользователей, письмо отбивается `User unknown in local recipient table` → виртуальный домен должен быть только в `virtual_mailbox_domains`.
2. **Забыли `postmap` после правки `vmailbox`** → ящик «не существует», письма отбиваются на этапе SMTP → после любой правки хешируемых файлов выполняем `postmap` и `postfix reload`.
3. **Правили конфиг, но не перезагрузили сервис** → полчаса диагностики вслепую → `postconf -n` + `systemctl reload postfix`, а лучше сначала `postfix check`.
4. **Клиентам дали порт 25 вместо 587** → нет гарантии шифрования и единых правил → отправка только через submission 587 с AUTH и `smtpd_tls_security_level=encrypt`.
5. **`postsuper -d ALL` вместо разбора причины** → письма пользователей уничтожены безвозвратно → читаем `postqueue -p`, причину в скобках, логи, и только потом чистим по одному ID.
6. **`chmod -R 777 /var/vmail`** → Dovecot отказывается работать с такими правами (защита от подмены), а если заработает — любой процесс читает чужую почту → только `vmail:vmail` и 0700 на домашние каталоги.
7. **Очередь растёт, а её никто не смотрит** → через 5 дней (`maximal_queue_lifetime`) письма отбиваются bounce'ами, пользователи узнают о проблеме позже всех → мониторинг размера `/var/spool/postfix/deferred` и алерт на рост (MON01 из трека L2).
8. **Ставили на UBU01 и забыли про PTR/SPF/DKIM** → письма во внешний мир уходят в спам или отклоняются чужими MX → для внутренней почты это не нужно, для внешней — обязательно, иначе сервер бессмысленно выпускать наружу.

### Безопасность

- **Open relay.** Никогда не ставьте `mynetworks = 0.0.0.0/0` и не убирайте `reject_unauth_destination`, чтобы «быстро заработало». Через открытый релей за сутки рассылают сотни тысяч спам-писем, ваш IP попадает в чёрные списки, а компания — в разбор с провайдером. В лаборатории мы сознательно оставили `mynetworks` по умолчанию (только localhost) — это и есть правильная настройка.
- **Самоподписанный сертификат** допустим только в лаборатории. В боевой среде — сертификат от УЦ, иначе пользователей приучают игнорировать предупреждения TLS.
- **Пароли в файле виртуальных пользователей** храним только хешем (`doveadm pw`), файл — `root:dovecot 640`. Пароль стенда `Lab#2026-vektor` общий только потому, что это учебный стенд; в реальной работе так нельзя.
- **Наружу SMTP-сервер без антиспама и антивируса не выставляют.** Реальный почтовый сервер ставится в DMZ (за GW01), с фильтрацией, проверкой вложений и ограничением размеров писем; порт 25 наружу многие провайдеры вообще ограничивают.

## 7. Вопросы на собеседовании

**1. Чем MTA отличается от MDA и как это делится между Postfix и Dovecot?**

Ответ: MTA (Postfix) принимает и передаёт почту по SMTP, держит очередь, делает ретраи и генерирует bounce. MDA (Dovecot LMTP/LDA) кладёт конкретное письмо в конкретный ящик пользователя, плюс отвечает за IMAP-доступ, индексы и квоты. В связке Postfix по `virtual_transport = lmtp:unix:private/dovecot-lmtp` передаёт письмо Dovecot через unix-сокет, Dovecot пишет в Maildir и отдаёт по IMAP.

*Что хочет услышать интервьюер:* что кандидат понимает разделение ответственности и знает точку стыка — сокет LMTP.
*Красный флаг:* «Postfix — это почтовый сервер, Dovecot — его веб-интерфейс» или путаница MUA/MTA.

**2. Пользователь говорит «письмо отправлено, но не дошло». Ваши действия?**

Ответ: иду по цепочке. Проверяю `postqueue -p` — есть ли письмо и какая причина в скобках (`deferred`, `connect ... No such file or directory`, `Connection timed out`). Смотрю `postcat -q <ID> -h` — правильные ли адреса. Дальше логи `/var/log/mail.log` или `journalctl -u postfix@-`: есть ли `status=sent` и куда. Если `sent` — проверяю ящик на стороне MDA через `doveadm mailbox status`, смотрю правильный ли ящик (алиасы). Если `deferred` — смотрю сокет LMTP, состояние Dovecot, место на диске, DNS и доступность внешнего MX.

*Что хочет услышать интервьюер:* методичный порядок «очередь → само письмо → логи → стык с MDA», а не «перезапустил postfix, помогло».
*Красный флаг:* сразу удалять очередь или править конфиг наугад.

**3. Что такое open relay и какими параметрами Postfix от него защищается?**

Ответ: open relay — сервер, который пересылает почту от кого угодно кому угодно без аутентификации. Защита — `smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated reject_unauth_destination` (порядок важен: сначала разрешаем свои сети и тех, кто прошёл AUTH, потом отклоняем всё, что не для нашего домена), плюс отдельный сервис `submission` на 587 с обязательной аутентификацией.

*Что хочет услышать интервьюер:* понимание, что правила читаются по порядку и первое совпадение решает.
*Красный флаг:* «поставил `permit` в конце, чтобы не мешало» или `mynetworks = 0.0.0.0/0`.

**4. Зачем нужны MX, SPF, DKIM, DMARC и PTR, если сервер внутренний?**

Ответ: MX указывает, куда доставлять почту для домена. SPF (TXT-запись) перечисляет, каким серверам разрешено отправлять от имени домена, DKIM подписывает письма криптографически, DMARC говорит получателю, что делать с письмами, не прошедшими SPF/DKIM, и куда слать отчёты. PTR (обратная зона) подтверждает, что IP принадлежит заявленному имени. Для внутренней почты между своими серверами это не критично, но как только сервер выпускает письма в интернет — без этого письма уходят в спам или отбиваются.

*Что хочет услышать интервьюер:* что кандидат не путает внутреннюю доставку с внешней и понимает цену «поставил Postfix и открыл 25 порт наружу».
*Красный флаг:* «SPF ставится на сервере в Postfix» — это DNS-запись, а не настройка MTA.

**5. В очереди 800 писем в `deferred`. Что делаете?**

Ответ: сначала причина, потом действия. `postqueue -p` и группировка причин: таймауты к внешнему MX, отказ аутентификации, переполнение диска, отсутствие сокета LMTP. Смотрю `mail.log` на `warning`/`fatal` за период. Часто это одна массовая причина — например, упавший Dovecot или неверный DNS. Если проблема временная и внешняя — ждём ретраев и, при необходимости, `postqueue -f`; если наши письма отбиваются чужой стороной — разбираемся с PTR/SPF/репутацией IP. Удаляю очередь только после явного согласования и понимания, какие письма уже неактуальны.

*Что хочет услышать интервьюер:* что кандидат ищет корневую причину и умеет группировать ошибки, а не чистит очередь.
*Красный флаг:* «сделаю `postsuper -d ALL`, чтобы очередь не мешала».

## 8. Источники (официальная документация)

- **postfix.org** — раздел `Documentation`: `Postfix Configuration Parameters` (описание всех параметров `main.cf`), `Postfix Standard Configuration Examples`, `Postfix SASL Howto`, `Postfix TLS Support`, `Postfix Queue Management` (`postqueue`, `postsuper`, `postcat`). Поиск: `postfix documentation configuration parameters`, `postfix queue management postqueue postsuper`.
- **dovecot.org** — раздел `Documentation`: страницы `Postfix and Dovecot` (связка через LMTP/LDA и SASL-сокет), `Maildir` (`mail_location`), `Authentication / Password databases` (`passwd-file`), `doveadm`. Поиск: `dovecot postfix lmtp configuration`, `dovecot passwd-file passdb userdb static`.
- **ubuntu.com** — Server documentation, раздел про Postfix и почтовые сервисы (установка и базовая настройка на Ubuntu). Поиск: `ubuntu server postfix dovecot install guide`.
- **learn.microsoft.com** — раздел DNS: типы записей `MX`, `A`, `TXT`, управление зонами в Windows Server DNS. Поиск: `DNS server resource record types MX`, `Add-DnsServerResourceRecordMX`.
- **swaks** — официальная страница инструмента (автор John Jetmore), раздел `Documentation / Options`. Домен не привожу, чтобы не ошибиться: ищите `swaks smtp swiss army knife official documentation`. [ПРОВЕРИТЬ точный домен и набор ключей `--tls-verify` для вашей версии]
- **man-страницы на самом стенде** — самый точный источник под вашу версию: `man 5 postconf`, `man 1 postqueue`, `man 1 postsuper`, `man 1 doveadm`, `man 5 dovecot.conf`. Поиск: `man postconf 5`.
- [ПРОВЕРИТЬ] Точные версии Postfix и Dovecot в Ubuntu Server 24.04 LTS (в 24.04 ожидается Postfix 3.8.x и Dovecot 2.3.x; в Dovecot 2.4 синтаксис конфигурации заметно изменён — сверяйтесь с `doveconf --version` на стенде).
