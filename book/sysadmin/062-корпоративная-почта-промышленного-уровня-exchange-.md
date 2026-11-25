# Корпоративная почта промышленного уровня: Exchange, Postfix/Dovecot, Zimbra, российские решения (CommuniGate, МойОфис, VK WorkSpace)

## 1. Введение (зачем это нужно в реальной работе)

Тикет в 9:15: **Пользователь:** petrova (Мария Петрова, HR) / **ВМ:** WIN10-CL02 / **Что написала:** «Настраиваю почту на новом ноутбуке как в инструкции, а она пишет „Не удалось подключиться к серверу“. Мне сегодня до 15:00 надо разослать приказ по отпускам, помогите!» / **Срочность:** высокая.

Почта — это сервис, который в компании замечают только когда он лёг. Пятьдесят сотрудников пишут по 30–80 писем в день, через почту идут приказы, счета, переписка с клиентами и перевозчиками, а у логистики и платежей ещё и уведомления от систем. При этом почта — самая атакуемая служба в сети: 90% атак начинаются с письма. Поэтому «промышленная» почта — это не «поставил Postfix, работает», а набор свойств: отказоустойчивость (письмо не теряется, если сервер упал), аутентификация пользователей, TLS, журналирование и архив, бэкап, антиспам (урок 63), мониторинг (MON01, Zabbix) и понятный регламент миграции.

После этой главы ты будешь понимать, из каких блоков состоит любой почтовый сервер (MTA, MDA, хранилище, каталог), чем Exchange отличается от Postfix/Dovecot и Zimbra, как выбрать решение под требования 152-ФЗ и реестра отечественного ПО, и сможешь поднять рабочий почтовый сервер на стенде «Вектор» — с DNS, TLS, аутентификацией и разбором реальной поломки.

## 2. Теория «под капотом»

Почта живёт на трёх прикладных протоколах и одном принципе — **store-and-forward** («накопил и передал»): сервер принимает письмо, кладёт его в очередь на диск и только потом пытается доставить. Поэтому отправитель никогда не ждёт получателя — он ждёт, пока *его* сервер принял ответственность за письмо.

**Роли в цепочке:**

- **MUA** (Mail User Agent) — клиент: Outlook, Thunderbird, мобильное приложение, веб-почта.
- **MTA** (Mail Transfer Agent) — транспорт: Postfix, Exchange Transport Service, CommuniGate SMTP-модуль. Общается SMTP.
- **MDA/LDA** (Mail Delivery Agent) — раскладывает письмо в ящик: Dovecot LMTP, Exchange Store.
- **Хранилище** — Maildir/mbox (файлы на диске) или база ESE (.edb) в Exchange, собственная БД в CommuniGate/Zimbra.
- **Каталог** — откуда берутся пользователи: AD (Exchange), OpenLDAP (Zimbra), внутренний каталог (CommuniGate), файл/БД (Postfix+Dovecot).

```text
                corp.vektor.lab (10.10.10.0/24)
                      ┌───────────────────────────┐
   Thunderbird        │  MAIL01  10.10.10.35      │
   WIN11-CL01 ───────►│  Postfix :25  (MTA, приём)│
   (ivanov)           │  Postfix :587 (submission,│
        ▲             │          AUTH + STARTTLS)  │
        │  :993 IMAPS │  Dovecot :143/993 IMAP    │
        └─────────────│  Dovecot :110/995 POP3    │
                      │  Dovecot LMTP ──► /var/vmail (Maildir)
                      └─────────────┬─────────────┘
                                    │ DNS: MX, A, (SPF/DKIM/DMARC — урок 63)
                      ┌─────────────▼─────────────┐
                      │  DC01 10.10.10.10 (DNS)   │
                      └───────────────────────────┘
```

**Порты, которые надо знать наизусть:**

| Порт | Протокол | Что это |
|---|---|---|
| 25 | SMTP | приём от других серверов, **без** аутентификации (MTA↔MTA) |
| 587 | SMTP submission | отправка от клиента, обязательны AUTH + STARTTLS |
| 465 | SMTPS | то же, но TLS с первого байта (implicit TLS) |
| 143 / 993 | IMAP / IMAPS | ящик на сервере, папки синхронизируются |
| 110 / 995 | POP3 / POP3S | скачивание на клиент (в корпоративной среде уходит в прошлое) |
| 443 | HTTPS | веб-почта (OWA), ActiveSync, EWS, MAPI/HTTP, Autodiscover |
| 389 / 636 | LDAP / LDAPS | каталог пользователей |
| 3268 / 3269 | Global Catalog | поиск получателей в лесу AD |

**Exchange** (Windows Server + AD). С Exchange 2013 роли схлопнулись: основная роль — **Mailbox** (внутри неё фронтенд-сервисы Client Access: OWA, EWS, ActiveSync, MAPI/HTTP, Autodiscover) и отдельно устанавливаемая роль **Edge Transport** для периметра. Пользователи и получатели живут в AD (Exchange не хранит свой список юзеров). Отказоустойчивость — **DAG** (Database Availability Group): до 16 серверов, несколько копий каждой базы, «свидетель» (file share witness), автоматическое переключение при падении. Письма ищутся через message tracking log. Важно для 2026 года: срок поддержки Exchange Server 2019 истёк 14.10.2025 `[ПРОВЕРИТЬ]`, ему на смену пришёл Exchange Server SE (Subscription Edition) `[ПРОВЕРИТЬ]`; кроме того, доступность продуктов Microsoft в РФ ограничена с 2022 года — поэтому в российском контуре чаще смотрят на Linux-решения.

**Postfix + Dovecot** — «конструктор». Postfix — только транспорт (SMTP, очередь, политики), Dovecot — IMAP/POP3/LMTP и аутентификация (SASL), хранилище — Maildir на диске. Плюсы: бесплатно, минимальные требования, всё конфигурируется текстом. Минусы: HA и веб-интерфейс делаешь сам, мониторинг и бэкап — тоже.

**Zimbra** — готовый «комбайн» на Linux: Postfix как MTA, mailboxd (Java) как хранилище и веб-интерфейс, OpenLDAP как каталог, MariaDB для метаданных, Amavis/ClamAV/SpamAssassin для фильтрации, nginx как фронт, Lucene для поиска, админка на 7071. Ставится одним инсталлятором, но и требует больше ресурсов. Есть платная Network Edition и открытая сборка — состав редакций менялся, проверяй актуальный `[ПРОВЕРИТЬ]`.

**Российские решения.** **CommuniGate Pro** — один многопоточный сервер-«монолит» с собственным хранилищем и каталогом, модули SMTP/IMAP/WebMail/CalDAV/XMPP, кластеризация, ставится на Linux и Windows, популярен в госсегменте. **МойОфис** (почтовый сервер + клиенты, Linux, позиционируется для госсектора, в реестре отечественного ПО). **VK WorkSpace** — облако и on-premise-версия: почта, календарь, мессенджер VK Teams, звонки, документы. Плюс облачные варианты (Яндекс 360 для бизнеса) и «Astra Linux / РЕД ОС + Postfix/Dovecot» как бюджетный вариант. Наличие в реестре и текущий статус проверяй на `reestr.digital.gov.ru` `[ПРОВЕРИТЬ]`, а не по презентациям вендора.

Требования «промышленного уровня», которые проверяют на собеседовании: HA (DAG/кластер), антиспам и антивирус, TLS везде, журналирование и архив (для 152-ФЗ и разбора инцидентов), бэкап и восстановление отдельного письма (урок 61), квоты, мониторинг очереди и ящиков, регламент миграции.

## 3. Аналогия из жизни

Почтовый сервер — это сортировочный центр с приёмным окном. Курьер (MUA) приносит посылку в окно **submission (587)**: там просят пропуск (аутентификацию) и проверяют, что ты не голый (TLS). Принятую посылку **никогда не отдают обратно** — её ставят на стеллаж «очередь» и дают квитанцию с номером (`250 Ok: queued as 4Xy2`). Дальше кладовщик (MTA) смотрит в справочник адресов (DNS/MX), звонит в следующий центр и передаёт. Если тот недоступен — посылка лежит на стеллаже и попытки повторяются (deferred), а если пролежит слишком долго — возвращается отправителю с извинениями (bounce). Внутри своего города посылку несут прямо в шкафчик получателя (LMTP → Maildir), и шкафчик открывается ключом получателя по запросу (IMAP).

Аналогия ломается на каталоге: в реальном сортировочном центре адрес — это просто надпись на коробке, а в Exchange и Zimbra получатель — объект в AD/LDAP, у которого есть права, группы рассылки, квоты и политики, и именно каталог, а не письмо, определяет, куда и кому оно в итоге уйдёт.

## 4. Практика и команды

**1. Что реально настроено в Postfix (а не что в дефолтах).**
```bash
sudo postconf -n
# myhostname = mail.corp.vektor.lab
# mydestination = $myhostname, localhost.$mydomain, localhost
# virtual_mailbox_domains = corp.vektor.lab
# virtual_transport = lmtp:unix:private/dovecot-lmtp
# smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated reject_unauth_destination
```
`-n` — только параметры, отличающиеся от умолчаний. Смотреть на `myhostname`, `mydestination`, `virtual_transport` и `smtpd_relay_restrictions` — последний и есть защита от открытого релея.

**2. Дефолты и лимиты размера письма.**
```bash
sudo postconf -d | grep -E 'message_size_limit|mailbox_size_limit'
# message_size_limit = 10240000      (10 МБ)
# mailbox_size_limit = 51200000      (50 МБ)
```
`-d` показывает умолчания. В бою `message_size_limit` поднимают до 25–50 МБ осознанно, иначе Exchange/1С начнут отбивать вложения.

**3. Очередь: главный инструмент диагностики.**
```bash
postqueue -p            # то же, что mailq
# -Queue ID-  --Size-- ----Arrival Time---- -Sender/Recipient-------
# 4Xy2abcd     1843 Mon Mar  2 10:14:03  ivanov@corp.vektor.lab
#                          petrova@corp.vektor.lab (mailbox permission denied)
postqueue -f            # принудительно переотправить всё
postsuper -r ALL        # вернуть в очередь (requeue) после правки конфига
postsuper -d 4Xy2abcd   # удалить конкретное письмо
postcat -q 4Xy2abcd     # посмотреть тело/заголовки письма из очереди
```

**4. Логи доставки.**
```bash
sudo tail -f /var/log/mail.log
# postfix/smtpd[812]: connect from unknown[10.10.10.101]
# postfix/smtpd[812]: NOQUEUE: reject: RCPT ... Relay access denied
# postfix/lmtp[829]: 4Xy2abcd: to=<petrova@corp.vektor.lab>, relay=mail.corp.vektor.lab[private/dovecot-lmtp], status=sent (250 2.0.0 Saved)
sudo journalctl -u postfix@- -n 50 --no-pager
```
Ищи `status=sent` (доставлено), `status=deferred` (лежит в очереди), `status=bounced`, `NOQUEUE: reject` (не пустили на входе).

**5. Хеш пароля для Dovecot.**
```bash
sudo doveadm pw -s SHA512-CRYPT
# Enter new password: ****
# {SHA512-CRYPT}$6$Xk2.../...
```
Пароль вводить интерактивно, не ключом `-p` — иначе он попадёт в историю bash.

**6. Проверка аутентификации без клиента.**
```bash
sudo doveadm auth test ivanov@corp.vektor.lab
# passdb: ivanov@corp.vektor.lab auth succeeded
```

**7. Список ящиков пользователя.**
```bash
sudo doveadm mailbox list -u petrova@corp.vektor.lab
# INBOX
# Sent
# Drafts
```

**8. TLS глазами клиента.**
```bash
openssl s_client -connect mail.corp.vektor.lab:993 -servername mail.corp.vektor.lab -CAfile /etc/ssl/vektor/mail.crt
# Verify return code: 0 (ok)
openssl s_client -starttls smtp -connect mail.corp.vektor.lab:587
```
Смотрим `Verify return code` и версию протокола (должен быть TLS 1.2/1.3). На Windows то же по смыслу: `Test-NetConnection mail.corp.vektor.lab -Port 993`.

**9. Тестовая отправка письма из консоли (swaks).**
```bash
swaks --server mail.corp.vektor.lab --port 587 -tls \
      --auth-user ivanov@corp.vektor.lab --auth-password 'Lab#2026-vektor' \
      -f ivanov@corp.vektor.lab -t petrova@corp.vektor.lab \
      --header "Subject: test-62" --body "hello from MAIL01"
# <-  235 2.7.0 Authentication successful
# ->  250 2.0.0 Ok: queued as 4Xy2abcd
```

**10. Кто слушает порты.**
```bash
ss -tlnp | grep -E ':(25|110|143|587|993|995)\b'
# LISTEN 0 100 0.0.0.0:25  ... users:(("master",pid=901,fd=12))
```

**11. Проверка DNS (на Linux и Windows).**
```bash
dig MX corp.vektor.lab +short      # 10 mail.corp.vektor.lab.
dig +short mail.corp.vektor.lab    # 10.10.10.35
```
```powershell
Resolve-DnsName -Name corp.vektor.lab -Type MX
Resolve-DnsName -Name mail.corp.vektor.lab
```
`Resolve-DnsName` запускать на WIN11-CL01: клиент должен видеть те же записи, что и сервер.

**12. Где физически лежит письмо.**
```bash
sudo ls -lt /var/vmail/corp.vektor.lab/petrova/Maildir/new/ | head
sudo find /var/vmail -type f -newermt '-15 min' | head
```

**13. Exchange Management Shell (справочно, если поднимешь Exchange отдельно).**
```powershell
Get-Queue | Format-Table Identity,Status,MessageCount
Get-MessageTrackingLog -Sender ivanov@corp.vektor.lab -Start (Get-Date).AddHours(-3) |
  Format-Table Timestamp,EventId,Source,RecipientStatus
Get-MailboxDatabase -Status | Format-Table Name,Mounted,DatabaseSize,AvailableNewMailboxSpace
Get-MailboxDatabaseCopyStatus | Format-Table Name,Status,CopyQueueLength
Test-ServiceHealth
Get-DatabaseAvailabilityGroup | Format-List Name,Status,WitnessServer
```
Логика та же, что в Postfix: очередь → трекинг → состояние баз → состояние копий DAG. Разница только в инструменте.

**GUI.** Windows-клиент: `Панель управления → Mail (Microsoft Outlook) → Show Profiles → E-mail Accounts → New`. Thunderbird: `Account Settings → Server Settings` (IMAP, порт 993, SSL/TLS, Normal password) и `Outgoing Server (SMTP)` (587, STARTTLS).

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Готовый стенд L2: DC01 (10.10.10.10, AD DS + DNS + DHCP), WIN11-CL01 (ящик ivanov), WIN10-CL02 (ящик petrova), FS01, UBU01, ASTRA01, GW01, WEB01, MON01, PVE01. Домен `corp.vektor.lab`, пароль стенда `Lab#2026-vektor`.

Почтового сервера на стенде ещё нет — **эта глава добавляет новую ВМ MAIL01**: Ubuntu Server 24.04 LTS, 2 vCPU, 2 ГБ RAM, 20 ГБ диск, адаптер в VMnet8, адрес **10.10.10.35**, FQDN `mail.corp.vektor.lab`. Записываем её в `stand-log.md` как постоянную часть стенда.

Exchange в лабораторной **не поднимаем**: ему нужна отдельная ВМ с Windows Server, 8+ ГБ RAM, вступление в домен и долгая установка. Теория и команды по нему — в разделах 2 и 4; на собеседовании этого достаточно, а руками потрогать промышленную почту дешевле на Linux.

### Снапшот

Снапшот с именем `before-sysadmin-062` делаем так, как позволяет VMware:

- **DC01, WIN11-CL01, WIN10-CL02** — снапшот `before-sysadmin-062` прямо сейчас (VM → Snapshot → Take Snapshot).
- **MAIL01** ещё не существует, поэтому: создаём ВМ, ставим чистую ОС, настраиваем сеть (п. 1), выключаем — и **после этого** снимаем `before-sysadmin-062`. Дальше все изменения можно откатить одним движением.

### Шаги

1. **MAIL01 — создание ВМ.** VMware Workstation → New Virtual Machine → ISO Ubuntu Server 24.04 LTS → 2 vCPU / 2048 МБ / 20 ГБ → сеть VMnet8 (NAT). После установки:
```bash
sudo hostnamectl set-hostname mail
echo "10.10.10.35 mail.corp.vektor.lab mail" | sudo tee -a /etc/hosts
hostname -f          # ожидаем: mail.corp.vektor.lab
ip -4 a              # ожидаем: 10.10.10.35/24
```
Сетевые настройки (netplan): адрес 10.10.10.35/24, шлюз 10.10.10.2, DNS 10.10.10.10. **Ожидаемо:** `ping 10.10.10.10` отвечает, `ping ya.ru` — тоже (через NAT).

2. **DC01 — DNS-записи.** `Server Manager → Tools → DNS → Forward Lookup Zones → corp.vektor.lab`:
   - ПКМ → New Host (A) → Name `mail`, IP `10.10.10.35` → Add Host.
   - ПКМ по зоне → New Mail Exchanger (MX) → поле «Host or child domain» **оставить пустым** (MX для самой зоны), Mail server `mail.corp.vektor.lab`, Preference `10`.
   **Ожидаемо:** на DC01 `Resolve-DnsName mail.corp.vektor.lab` → 10.10.10.35; `Resolve-DnsName corp.vektor.lab -Type MX` → `mail.corp.vektor.lab`.

3. **MAIL01 — пакеты.**
```bash
sudo apt update
sudo apt install -y postfix mailutils dovecot-core dovecot-imapd dovecot-pop3d dovecot-lmtpd swaks
```
В диалоге Postfix: **Internet Site**, System mail name: `mail.corp.vektor.lab`. **Ожидаемо:** сервисы `postfix` и `dovecot` в состоянии active (`systemctl status postfix dovecot`).

4. **MAIL01 — пользователь и хранилище писем.**
```bash
sudo groupadd -g 5000 vmail
sudo useradd -g vmail -u 5000 -d /var/vmail -m -s /usr/sbin/nologin vmail
sudo mkdir -p /var/vmail/corp.vektor.lab/{ivanov,petrova}/Maildir
sudo chown -R vmail:vmail /var/vmail
sudo chmod -R 700 /var/vmail
```
**Ожидаемо:** `ls -ld /var/vmail` → владелец `vmail vmail`.

5. **MAIL01 — TLS-сертификат.** В бою берём из корпоративного УЦ (урок 57); на стенде — самоподписанный:
```bash
sudo mkdir -p /etc/ssl/vektor
sudo openssl req -new -x509 -days 825 -nodes -newkey rsa:2048 \
  -keyout /etc/ssl/vektor/mail.key -out /etc/ssl/vektor/mail.crt \
  -subj "/CN=mail.corp.vektor.lab" \
  -addext "subjectAltName=DNS:mail.corp.vektor.lab,DNS:corp.vektor.lab"
sudo chmod 600 /etc/ssl/vektor/mail.key
```
**Ожидаемо:** оба файла на месте, `mail.crt` читается.

6. **MAIL01 — файл пользователей Dovecot.** Хеши получаем по одному:
```bash
sudo doveadm pw -s SHA512-CRYPT     # для ivanov, скопировать строку
sudo doveadm pw -s SHA512-CRYPT     # для petrova
sudo tee /etc/dovecot/users >/dev/null <<'EOF'
ivanov@corp.vektor.lab:{SHA512-CRYPT}$6$ВСТАВИТЬ_ХЕШ:5000:5000::/var/vmail/corp.vektor.lab/ivanov::
petrova@corp.vektor.lab:{SHA512-CRYPT}$6$ВСТАВИТЬ_ХЕШ:5000:5000::/var/vmail/corp.vektor.lab/petrova::
EOF
sudo chown root:dovecot /etc/dovecot/users && sudo chmod 640 /etc/dovecot/users
```
**Ожидаемо:** `sudo doveadm auth test ivanov@corp.vektor.lab` → `auth succeeded`.

7. **MAIL01 — конфигурация Dovecot** (файлы в `/etc/dovecot/`):
```text
# dovecot.conf
protocols = imap pop3 lmtp

# conf.d/10-auth.conf
disable_plaintext_auth = yes
auth_mechanisms = plain login
!include auth-passwdfile.conf.ext

# conf.d/auth-passwdfile.conf.ext
passdb {
  driver = passwd-file
  args = scheme=SHA512-CRYPT username_format=%u /etc/dovecot/users
}
userdb {
  driver = passwd-file
  args = username_format=%u /etc/dovecot/users
  default_fields = uid=vmail gid=vmail home=/var/vmail/%d/%n
}

# conf.d/10-mail.conf
mail_location = maildir:/var/vmail/%d/%n/Maildir

# conf.d/10-ssl.conf
ssl = required
ssl_cert = </etc/ssl/vektor/mail.crt
ssl_key  = </etc/ssl/vektor/mail.key
```
В `conf.d/10-master.conf` добавить сокеты для Postfix:
```text
service lmtp {
  unix_listener /var/spool/postfix/private/dovecot-lmtp { mode = 0600 user = postfix group = postfix }
}
service auth {
  unix_listener /var/spool/postfix/private/auth { mode = 0660 user = postfix group = postfix }
}
```
```bash
sudo systemctl restart dovecot && sudo doveconf -n | head -30
```
**Ожидаемо:** сервис active, `doveconf -n` показывает твои строки. На Ubuntu 24.04 обычно Dovecot 2.3.x; если у тебя 2.4, имена директив SSL другие `[ПРОВЕРИТЬ]`.

8. **MAIL01 — конфигурация Postfix.**
```bash
sudo postconf -e 'myhostname = mail.corp.vektor.lab'
sudo postconf -e 'mydomain = corp.vektor.lab'
sudo postconf -e 'myorigin = $mydomain'
sudo postconf -e 'mydestination = $myhostname, localhost.$mydomain, localhost'
sudo postconf -e 'mynetworks = 127.0.0.0/8'
sudo postconf -e 'virtual_mailbox_domains = corp.vektor.lab'
sudo postconf -e 'virtual_mailbox_base = /var/vmail'
sudo postconf -e 'virtual_mailbox_maps = hash:/etc/postfix/vmailbox'
sudo postconf -e 'virtual_transport = lmtp:unix:private/dovecot-lmtp'
sudo postconf -e 'smtpd_sasl_type = dovecot'
sudo postconf -e 'smtpd_sasl_path = private/auth'
sudo postconf -e 'smtpd_sasl_auth_enable = yes'
sudo postconf -e 'smtpd_tls_cert_file = /etc/ssl/vektor/mail.crt'
sudo postconf -e 'smtpd_tls_key_file = /etc/ssl/vektor/mail.key'
sudo postconf -e 'smtpd_tls_security_level = may'
sudo postconf -e 'smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated reject_unauth_destination'
printf 'ivanov@corp.vektor.lab corp.vektor.lab/ivanov/\npetrova@corp.vektor.lab corp.vektor.lab/petrova/\n' | sudo tee /etc/postfix/vmailbox
sudo postmap /etc/postfix/vmailbox
sudo postfix check && sudo systemctl reload postfix
```
В `/etc/postfix/master.cf` раскомментировать блок `submission inet n - y - - smtpd` и в нём `-o smtpd_tls_security_level=encrypt`, `-o smtpd_sasl_auth_enable=yes`, `-o smtpd_client_restrictions=permit_sasl_authenticated,reject`.
**Ожидаемо:** `postconf -n` содержит `virtual_transport = lmtp:unix:private/dovecot-lmtp`.

9. **MAIL01 — файрвол (учебно).**
```bash
sudo ufw allow 25,587,993,143/tcp
sudo ufw enable
```
**Ожидаемо:** `sudo ufw status` показывает 4 разрешённых порта. В боевой среде 25 открывают только для релея/антиспам-шлюза, а не «для всех» — см. раздел 6.

10. **MAIL01 — самопроверка до клиента.**
```bash
ss -tlnp | grep -E ':(25|143|587|993)\b'
swaks --server mail.corp.vektor.lab --port 587 -tls \
      --auth-user ivanov@corp.vektor.lab --auth-password 'Lab#2026-vektor' \
      -f ivanov@corp.vektor.lab -t petrova@corp.vektor.lab --header "Subject: test-62"
```
**Ожидаемо:** `235 Authentication successful` и `250 Ok: queued as ...`; в `mail.log` строка `status=sent`.

11. **WIN11-CL01 — клиент ivanov.** Установить Mozilla Thunderbird (free), аккаунт вручную: IMAP `mail.corp.vektor.lab`, 993, SSL/TLS, Normal password, логин `ivanov@corp.vektor.lab`; SMTP `mail.corp.vektor.lab`, 587, STARTTLS. Самоподписанному сертификату — исключение (на стенде допустимо). Отправить письмо на `petrova@corp.vektor.lab` с темой «Тест 62». **Ожидаемо:** письмо уходит без ошибок, в логах MAIL01 — `status=sent`.

12. **WIN10-CL02 — клиент petrova.** То же самое для `petrova@corp.vektor.lab`. **Ожидаемо:** письмо видно во «Входящих», отправка обратно ivanov тоже проходит. Проверить на MAIL01:
```bash
sudo ls -lt /var/vmail/corp.vektor.lab/petrova/Maildir/new/ | head -3
```

13. **Журнал стенда.** Внести в `stand-log.md`: MAIL01 создан, адрес, DNS-записи, ящики, сертификат, где конфиги.

### Проверка

- [ ] `hostname -f` на MAIL01 → `mail.corp.vektor.lab`, `ip -4 a` → 10.10.10.35.
- [ ] `Resolve-DnsName corp.vektor.lab -Type MX` на DC01 и на клиентах даёт `mail.corp.vektor.lab` (pref 10).
- [ ] `ss -tlnp` показывает LISTEN на 25, 587, 143, 993.
- [ ] `doveadm auth test` проходит для обоих пользователей.
- [ ] `swaks` отправляет письмо и получает `250 Ok: queued as ...`.
- [ ] В `/var/log/mail.log` есть `status=sent`, в очереди (`postqueue -p`) пусто.
- [ ] Письмо реально доходит с WIN11-CL01 на WIN10-CL02 и обратно.
- [ ] `openssl s_client` показывает `Verify return code: 0 (ok)`.
- [ ] `stand-log.md` обновлён.

### Сломай и почини

**Заявка.** Пользователь: petrova / ВМ: WIN10-CL02 / Что написала: «С утра открываю почту, а она пишет: „Не удалось открыть папку Входящие: Permission denied“. Иванову при этом всё приходит. У меня внутри письма от перевозчика, приказ до 15:00!» / Срочность: высокая.

**Воспроизводим поломку** (имитируем коллегу, который «поправил права»):
```bash
sudo chown -R root:root /var/vmail/corp.vektor.lab/petrova
sudo systemctl restart dovecot
```
Теперь пишем petrova письмо с клиента ivanov — Postfix примет его, а LMTP не сможет записать.

**Диагностика по слоям, сверху вниз:**
1. `Test-NetConnection mail.corp.vektor.lab -Port 993` на WIN10-CL02 → открыт. Значит, сеть и TLS не при чём, проблема на стороне ящика.
2. `sudo systemctl status dovecot postfix` на MAIL01 → оба active. Сервис жив.
3. `sudo tail -50 /var/log/mail.log` → `status=deferred (mailbox permission denied)` для petrova; письма ivanov идут `status=sent`.
4. `postqueue -p` → в очереди одно письмо на petrova, уходит в deferred. Значит, письмо не потеряно — уже плюс.
5. `sudo journalctl -u dovecot -n 40 --no-pager` → `open(.../petrova/Maildir/dovecot.index) failed: Permission denied`.
6. `ls -ld /var/vmail/corp.vektor.lab/petrova /var/vmail/corp.vektor.lab/petrova/Maildir` → владелец `root root`. Нашли причину.
7. `sudo doveadm auth test petrova@corp.vektor.lab` → аутентификация проходит, значит дело не в пароле.

**Решение:**
```bash
sudo chown -R vmail:vmail /var/vmail/corp.vektor.lab/petrova
sudo find /var/vmail -not -user vmail -ls     # проверяем, что больше нигде не «натекло»
sudo postqueue -f                              # выталкиваем застрявшее письмо
```
Через минуту в логе `status=sent`, у petrova письмо появляется.

**Ответ пользователю:** «Мария, добрый день! Проблема была в правах доступа к вашему почтовому ящику на сервере, письма при этом не потерялись — они доставятся автоматически. Сейчас всё исправлено, закройте и откройте Thunderbird. Если папка «Входящие» ещё ругается — напишите, посмотрю заново. Приказ можете отправлять, задержек не будет».

**Закрытие заявки:** категория «Почта → доступ к ящику», в решении — «нарушены права на каталог Maildir, восстановлены через chown, очередь разогнана postqueue -f, письма доставлены, потерь нет». Отдельным пунктом — завести задачу: «запретить ручные изменения прав на /var/vmail, зафиксировать владельца в конфиг-менеджменте».

### Что изменилось на стенде

- Новая ВМ **MAIL01**: Ubuntu Server 24.04 LTS, 10.10.10.35, FQDN `mail.corp.vektor.lab`, 2 vCPU / 2 ГБ / 20 ГБ.
- На DC01 в зоне `corp.vektor.lab`: A-запись `mail` → 10.10.10.35, MX зоны `corp.vektor.lab` → `mail.corp.vektor.lab` (pref 10).
- На MAIL01: Postfix (25, 587) + Dovecot (143, 993, LMTP), виртуальный домен `corp.vektor.lab`, хранилище `/var/vmail` (владелец `vmail`, uid/gid 5000), файл пользователей `/etc/dovecot/users`.
- Ящики: `ivanov@corp.vektor.lab`, `petrova@corp.vektor.lab` (пароль стенда `Lab#2026-vektor`).
- Сертификат `/etc/ssl/vektor/mail.crt|key` (CN `mail.corp.vektor.lab`, SAN с именем зоны) — самоподписанный, только для стенда.
- Клиенты WIN11-CL01 и WIN10-CL02 настроены по IMAP/993 + SMTP/587.

### Откат

- **MAIL01:** VM → Snapshot Manager → `before-sysadmin-062` → Revert. Вернётся чистая Ubuntu с сетью; конфиги почты, ящики и сертификат исчезнут — повторяем шаги 3–10.
- **DC01:** Revert `before-sysadmin-062` уберёт A- и MX-записи. Если откатывать DC01 не хочешь (на нём могли быть другие изменения) — удали записи руками в DNS Manager.
- **WIN11-CL01 / WIN10-CL02:** Revert или просто удалить аккаунт в Thunderbird (Account Settings → Remove).
- Запись в `stand-log.md` про откат не удаляем, а дописываем: «откат выполнен, причина».

## 6. Ошибки новичков и безопасность

1. **Открытый релей.** Ошибка: добавить `10.10.10.0/24` в `mynetworks`, «чтобы всем было удобно отправлять». → Через сутки сервер в чёрных списках, с него рассылают спам. → Правильно: `mynetworks = 127.0.0.0/8`, отправка только через 587 с аутентификацией, `smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated reject_unauth_destination`.
2. **Аутентификация без TLS.** Ошибка: `disable_plaintext_auth = no` и `smtpd_tls_security_level = may` «на время тестов». → Пароли ходят открытым текстом по офисной сети. → Правильно: `disable_plaintext_auth = yes`, для submission `smtpd_tls_security_level = encrypt`.
3. **Самоподписанный сертификат в бою.** → Пользователи привыкают жать «Принять риск», и это же делают при атаке MITM. → Правильно: сертификат из корпоративного УЦ (урок 57), внутренний корень раскатан через GPO.
4. **Почта на контроллере домена.** Ошибка: «поставлю Exchange/Postfix на DC01, чтобы не плодить ВМ». → Компромисс почты = компромисс домена; плюс конфликт портов и производительности. → Правильно: отдельный сервер в отдельном сегменте.
5. **Нет квот.** → Один ящик с 40 ГБ видео кладёт диск и весь сервис. → Правильно: квоты на ящик и на домен, алерт в Zabbix (MON01) на 80% заполнения.
6. **POP3 «чтобы почта не занимала место».** → Письма существуют в единственном экземпляре на ноутбуке; ноутбук уехал в ремонт — переписки нет. → Правильно: IMAP + серверный архив/журналирование.
7. **Нет бэкапа и нет проверки восстановления.** → Сбой массива — и вся история компании потеряна. → Правильно: бэкап по уроку 61 + регулярное восстановление отдельного письма как учение.
8. **Правки руками и «по памяти».** → Через месяц никто не помнит, почему в `main.cf` стоит именно это. → Правильно: конфиги в Git, изменения через Ansible, регламент в вики.

### Безопасность

В боевой среде почтовый сервер **никогда** не публикуют напрямую в интернет: снаружи открыт только релей/антиспам-шлюз (порт 25), сам почтовый сервер смотрит в него и в локалку. На стенде мы разрешили 25/587/993 «для всех» через ufw — это учебное упрощение. Пароль `Lab#2026-vektor` хранится в файле и одинаков для всех — в продакшене так нельзя: пароли уникальные, хеши только вида SHA512-CRYPT/bcrypt, доступ к `/etc/dovecot/users` — root:dovecot 640. Отдельно: содержимое почты почти всегда содержит персональные данные (152-ФЗ), поэтому доступ администратора к ящикам должен быть регламентирован и логируем (в идеале — только через PAM-решение из урока 56), а журналирование включено.

## 7. Вопросы на собеседовании

**1. «Чем MTA отличается от MDA и где в этой цепочке Exchange?»**
Ответ: MTA — транспорт, принимает и передаёт по SMTP (Postfix, Exchange Transport). MDA — доставляет в конкретный ящик (Dovecot LMTP, Exchange Store). MUA — клиент. В Exchange роли исторически разделялись, с 2013 всё внутри роли Mailbox: фронтенд-транспорт принимает соединение, транспортная служба маршрутизирует, Store раскладывает по базам; отдельно ставится только Edge Transport на периметр.
*Что хочет услышать:* что ты понимаешь разделение «транспорт ≠ хранилище» и умеешь искать проблему на нужном слое.
*Красный флаг:* «Exchange — это просто сервер почты, как Postfix, только от Microsoft».

**2. «Проведи письмо от ivanov к petrova: что происходит по шагам и какие порты задействованы?»**
Ответ: клиент подключается к 587 с AUTH + STARTTLS → Postfix проверяет получателя по `virtual_mailbox_maps` и кладёт письмо в очередь (файл в `/var/spool/postfix`) → отвечает `250 queued as <ID>` → транспорт ищет маршрут: для своего домена — локальная доставка через LMTP в Maildir, для внешнего — DNS MX получателя и соединение по 25 → лог `status=sent`; при неудаче — `deferred` с повторами и в итоге `bounce` отправителю. Клиент получателя забирает письмо по 993 (IMAP).
*Что хочет услышать:* понимание store-and-forward, DNS MX, ролей портов 25 vs 587.
*Красный флаг:* «клиент отправляет напрямую на сервер получателя по 993».

**3. «Что такое DAG в Exchange и что нужно, чтобы его построить?»**
Ответ: Database Availability Group — группа до 16 серверов Exchange, между которыми реплицируются копии баз почтовых ящиков; при падении сервера копия активируется на другом. Нужны: одинаковые версии/обновления Exchange, отказоустойчивый свидетель (file share witness вне DAG), сеть с низкой задержкой между узлами, сертификаты на каждом сервере, корректный DNS и балансировка клиентских подключений. Мониторим `Get-MailboxDatabaseCopyStatus`: CopyQueueLength и ContentIndexState, а также отставание lagged-копии.
*Что хочет услышать:* что HA — это не «две копии базы», а ещё и свидетель, сеть, балансировщик и мониторинг отставания.
*Красный флаг:* «DAG — это бэкап» (нет, это HA; бэкап — отдельная история).

**4. «Как переехать с одного почтового сервера на другой без простоя и потери писем?»**
Ответ: сначала инвентаризация (ящики, размеры, алиасы, группы, правила, календари, общие ящики). Пилот на 5–10 пользователей. Синхронизация содержимого по IMAP (imapsync или встроенные миграционные инструменты вендора), затем повторные догоняющие прогоны. За 24–48 часов снижаем TTL MX до 300 с. В час X: переключаем MX на новый сервер, оставляем старый в режиме релея на новый (coexistence), клиентов переводим через Autodiscover/GPO или профиль, через несколько дней отключаем старый. Обязательно: бэкап перед стартом, журнал миграции, чек-лист клиентов (Outlook/мобильные), обратный план отката.
*Что хочет услышать:* про TTL, пилот, догоняющую синхронизацию и план отката.
*Красный флаг:* «в пятницу вечером выключу старый и включу новый».

**5. «Пользователь удалил письмо месяц назад, теперь оно нужно. Как восстановишь?»**
Ответ: сначала смотрю, есть ли архив/журнал (Exchange journaling, архиватор, Veeam/Кибер Бэкап с item-level). В Exchange: восстановление из резервной копии базы в Recovery Database и `New-MailboxRestoreRequest` в отдельную папку пользователя — не поверх рабочего ящика; либо lagged copy DAG, если она настроена. В Postfix/Dovecot: восстановление каталога Maildir из бэкапа в отдельную папку, чтобы не перезаписать текущее состояние. Всегда уточняю дату, отправителя и тему и фиксирую факт выдачи в заявке.
*Что хочет усыншать:* что не льешь бэкап поверх рабочей базы и что есть регламент таких запросов.
*Красный флаг:* «перезапишу ящик из ночного бэкапа».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Exchange Server»: планирование, Mailbox server, Database Availability Group, командлеты Exchange Management Shell. Поиск: `Exchange Server documentation DAG`, `Exchange Server SE lifecycle`.
- **postfix.org** — `Postfix Documentation`, `postconf(5)`, `postfix manual pages`, `Standard Configuration`, `Virtual Domain Hosting`. Поиск: `postfix virtual mailbox dovecot lmtp`.
- **dovecot.org** — `Dovecot Documentation`: `Authentication/Password schemes`, `LDA/LMTP`, `Maildir`, `SSL configuration`. Поиск: `dovecot passwd-file virtual users lmtp postfix`.
- **ubuntu.com/server/docs** — руководство по Ubuntu Server: разделы про Postfix, Dovecot, netplan, ufw. Поиск: `Ubuntu Server docs postfix dovecot`.
- **zimbra.com** — `Zimbra Collaboration Documentation`: installation guide, `zmprov` CLI, `zmcontrol`. Поиск: `Zimbra collaboration installation guide single server`.
- **communigate.com** — `CommuniGate Pro Documentation`: SMTP/IMAP modules, clustering, WebAdmin. Поиск: `CommuniGate Pro manual clustering`.
- **myoffice.ru** — раздел документации по почтовым и серверным продуктам. Поиск: `МойОфис Почта документация администратора` `[ПРОВЕРИТЬ актуальный адрес раздела]`.
- **vk.com / VK WorkSpace** — документация по VK WorkSpace и VK Teams, в том числе on-premise-развёртывание. Поиск: `VK WorkSpace документация администратора` `[ПРОВЕРИТЬ путь к docs]`.
- **reestr.digital.gov.ru** — единый реестр российского ПО: проверка наличия CommuniGate Pro, МойОфис, VK WorkSpace. Поиск: `реестр российского ПО почтовый сервер`.
- **docs.cryptopro.ru** — КриптоПро УЦ и работа с ГОСТ-сертификатами для почтовых серверов (если требуется ГОСТ-TLS). Поиск: `КриптоПро УЦ выпуск сертификата TLS`.
