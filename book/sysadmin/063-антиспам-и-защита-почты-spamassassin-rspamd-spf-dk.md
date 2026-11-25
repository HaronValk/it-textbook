# Антиспам и защита почты: SpamAssassin, Rspamd, SPF/DKIM/DMARC, карантин, DLP

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди: **Пользователь:** ivanov / **ВМ:** WIN11-CL01 / **Что написал:** «Отправил в бухгалтерию акт сверки с вложением — мне вернулось: `550 5.7.1 Message rejected, spam score too high`. До обеда всё работало, я ничего не менял. Срочно, у нас закрытие месяца». / **Срочность:** высокая.

Почта — единственный сервис, через который в компанию приходит **весь** внешний нефильтрованный контент: фишинг, малварь во вложениях, вишинг с поддельного домена, письма «от директора» с просьбой оплатить счёт. Одновременно это же и канал утечки: сотрудник отправляет базу клиентов «на свою почту». Поэтому антиспам и защита почты — это не «поставить галочку», а постоянная работа: правила, пороги, карантин, разбор ложных срабатываний, журналы и регламент.

Для компании «Вектор» это ещё и регуляторика: логистика + платёжные сервисы означает 152-ФЗ (персональные данные) и, если инфраструктура попадает под 187-ФЗ, требования приказов ФСТЭК №17 и ГОСТ Р 57580 к защите каналов. После главы вы будете уметь: настроить SPF/DKIM/DMARC и проверить их по DNS, поднять Rspamd и подключить его к Postfix через milter, читать символы и пороги, сделать карантин с разбором, объяснить разницу между SpamAssassin и Rspamd и почему DLP на уровне почтового шлюза — только часть задачи.

## 2. Теория «под капотом»

**Два отправителя в одном письме.** В SMTP есть *envelope* (конверт) — то, что видно в `MAIL FROM:` и `RCPT TO:` в диалоге с сервером, и *headers* (заголовки) — `From:`, `To:`, `Subject:`. Они не обязаны совпадать. Именно на этом расхождении строится и подделка (спуфинг), и вся защита: SPF проверяет конверт, DKIM подписывает заголовки и тело, DMARC требует, чтобы результат SPF или DKIM **совпал по домену** с `From:`.

**Порты:** 25/tcp — приём почты между серверами (MTA-to-MTA), 587/tcp — submission от клиента (с STARTTLS и аутентификацией), 465/tcp — SMTPS (implicit TLS), 993/tcp — IMAPS (Dovecot), 11332/tcp — локальный milter-сокет Rspamd.

**SPF (Sender Policy Framework)** — TXT-запись в DNS домена: список IP/хостов, которым разрешено отправлять от имени домена. Механизмы: `ip4:`, `a`, `mx`, `include:`, `exists:`, `redirect=`; квалификаторы `+` (pass, по умолчанию), `-` (fail), `~` (softfail), `?` (neutral). Важное ограничение: **не более 10 DNS-запросов** на проверку записи — «include-ад» из десятка чужих SPF ломает проверку молча.

**DKIM (DomainKeys Identified Mail)** — сервер отправителя считает хеш тела и выбранных заголовков, подписывает приватным ключом и добавляет `DKIM-Signature:` с селектором. Получатель берёт публичный ключ из `<selector>._domainkey.<domain>` (TXT) и проверяет. Если письмо изменили в пути — подпись ломается. Ключ обычно 2048 бит RSA; селектор нужен, чтобы можно было ротировать ключи без простоя (старый и новый живут в DNS одновременно).

**DMARC (RFC 7489)** — TXT в `_dmarc.<domain>`: что делать получателю, если ни SPF, ни DKIM не дали **alignment** (совпадения домена) с `From:`. Политики: `p=none` (только отчёты), `p=quarantine` (в спам/карантин), `p=reject` (отклонить). Дополнительно: `sp=` для поддоменов, `adkim=`/`aspf=` (strict `s` / relaxed `r`), `pct=` (доля писем под политикой), `rua=` (агрегированные XML-отчёты), `ruf=` (форензик). Правильный rollout: `p=none` + сбор `rua` → 2–4 недели → `p=quarantine` → `p=reject`.

**ARC (RFC 8617)** — «страховка» для пересылки: промежуточный сервер (рассылка, форвард) добавляет свою цепочку подписей, и получатель может принять решение по последнему доверенному звену. Нужен потому, что при форварде SPF исходного домена почти всегда ломается.

**Фильтрация контента.** SpamAssassin — классический MTA-фильтр: набор правил (regexp, DNSBL, байесовский классификатор, сетевые тесты через Pyzor/Razor), каждое правило даёт балл, при превышении `required_score` письмо помечается. Rspamd — современный движок на C/Lua: сотни символьных правил, свои модули (`spf`, `dkim`, `dmarc`, `multimap`, `regexp`, `fuzzy`, `ratelimit`, `bayes`), состояние в Redis, встроенная статистика. Дополнительно: **DNSBL** (проверка IP отправителя по `zen.spamhaus.org` и подобным), **greylisting** (временный отказ 4xx — «переотправь через 5 минут», спам-боты не переотправляют), **fuzzy-хеши** (поиск похожих писем по базе), **rate limit** (сколько писем в час с одного отправителя).

**Milter** (mail filter) — API Sendmail, который поддерживают Postfix и Rspamd: MTA отдаёт письмо фильтру на этапе SMTP-сессии и получает вердикт — принять, добавить заголовки, отложить, отклонить.

```text
        Интернет
           │  SMTP 25
           ▼
   ┌───────────────┐   milter 11332   ┌──────────────┐
   │ MAIL01 Postfix│◄────────────────►│   Rspamd     │
   │  (MTA)        │                  │ spf/dkim/    │
   └──────┬────────┘                  │ dmarc/bayes  │
          │                           └──────┬───────┘
          │ LMTP/Maildir                     │
          ▼                                  ▼
   ┌───────────────┐                  ┌──────────────┐
   │ Dovecot IMAP  │                  │ Redis        │
   │ + Sieve       │                  │ (bayes,      │
   │ (Junk/Quar.)  │                  │  ratelimit)  │
   └──────┬────────┘                  └──────────────┘
          │ IMAPS 993
          ▼
   WIN11-CL01 / WIN10-CL02 (Outlook, Thunderbird, веб-клиент)
          │
          ▼
   DC01 (10.10.10.10): DNS — A, MX, TXT (SPF, DKIM, DMARC)
```

## 3. Аналогия из жизни

Представьте КПП на въезде в бизнес-центр. **SPF** — это список машин, которым выдан пропуск на въезд: охранник смотрит на номер и сверяет со списком, который прислала компания-арендатор. **DKIM** — пломба на фургоне: если пломба цела и номер пломбы совпадает с тем, что записан у компании, значит груз не вскрывали в пути. **DMARC** — инструкция для охраны: «если машина без пропуска и без пломбы — не пускай / пусти, но только на грузовую площадку / запиши в журнал и пусти». А **DLP** — это уже досмотр на выезде: проверяем, не вывозят ли со склада то, что вывозить нельзя.

Аналогия ломается на пересылке: когда груз перегружают в другой фургон (форвард через промежуточный сервер), старая пломба и старый пропуск относятся уже не к тому, кто реально привёз — ровно для этого придумали ARC.

## 4. Практика и команды

**1. Проверка SPF/DKIM/DMARC в DNS (Linux).**
```bash
dig +short TXT corp.vektor.lab
dig +short TXT mail._domainkey.corp.vektor.lab
dig +short TXT _dmarc.corp.vektor.lab
dig +short MX corp.vektor.lab
```
`+short` — только ответ без секции authority. Смотрим: SPF должен начинаться с `v=spf1` и **быть единственным** TXT с этой строкой; DKIM — `v=DKIM1; k=rsa; p=<base64>`; DMARC — `v=DMARC1; p=...`.

**2. То же из Windows.**
```powershell
Resolve-DnsName -Name corp.vektor.lab -Type TXT -Server 10.10.10.10
nslookup -type=TXT _dmarc.corp.vektor.lab 10.10.10.10
```

**3. Создание записей в зоне на DC01 (PowerShell, от adm.student).**
```powershell
Add-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name "@" -Txt `
  -DescriptiveText "v=spf1 ip4:10.10.10.35 -all"
Add-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name mail -Txt `
  -DescriptiveText "v=DKIM1; k=rsa; p=MIIBIjANBg..."
Add-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name _dmarc -Txt `
  -DescriptiveText "v=DMARC1; p=quarantine; adkim=s; aspf=s; pct=100; rua=mailto:dmarc@corp.vektor.lab"
Get-DnsServerResourceRecord -ZoneName corp.vektor.lab -RRType Txt
```
GUI-путь: `dnsmgmt.msc` → Forward Lookup Zones → `corp.vektor.lab` → правый клик → New Record → **Text (TXT)**. Для DKIM имя записи — `mail._domainkey` (селектор `mail`).

**4. Генерация ключа DKIM и вывод готовой TXT-записи.**
```bash
sudo mkdir -p /var/lib/rspamd/dkim
sudo rspamadm dkim_keygen -s mail -d corp.vektor.lab \
     -k /var/lib/rspamd/dkim/corp.vektor.lab.mail.key > /root/dkim.txt
sudo chown -R _rspamd:_rspamd /var/lib/rspamd/dkim
sudo chmod 600 /var/lib/rspamd/dkim/*
cat /root/dkim.txt
```
`-s` селектор, `-d` домен, `-k` путь к приватному ключу. Команда печатает в stdout готовый текст TXT-записи. `[ПРОВЕРИТЬ]` точный набор ключей `rspamadm dkim_keygen` в вашей версии (`rspamadm dkim_keygen --help`).

**5. Проверка и просмотр конфигурации Rspamd.**
```bash
sudo rspamadm configtest
sudo rspamadm configdump worker
sudo rspamadm configdump dkim_signing
sudo systemctl reload rspamd
```
`configtest` — синтаксис всех подключённых файлов, включая `local.d/*.conf`. Всегда сначала `configtest`, потом reload, иначе рискуете уронить фильтр.

**6. Статистика и «почему это спам».**
```bash
rspamc stat
rspamc symbols < /var/mail/ivanov/cur/1712345678.M12345P678.mail
rspamc < /tmp/test.eml
```
`rspamc stat` — счётчики обработанных писем, обучение Bayes, uptime. `rspamc symbols` печатает список сработавших символов с баллами — это главный инструмент разбора ложных срабатываний. Учтите: `rspamc` обращается к controller-воркеру, по умолчанию `127.0.0.1:11334`; при нестандартном конфиге нужен `-h`.

**7. Отправка тестового письма (swaks).**
```bash
sudo apt install -y swaks
swaks --to petrova@corp.vektor.lab --from ivanov@corp.vektor.lab \
      --server 10.10.10.35 --h-Subject "Тест DKIM" --body "Проверка подписи"
swaks --to petrova@corp.vektor.lab --from ivanov@corp.vektor.lab \
      --server 10.10.10.35 --h-From: 'boss@vektor-pay.ru' --body "Подделка From"
```
Первый вызов — легитимное письмо; второй — имитация спуфинга: конверт от нашего домена, а `From:` чужой. Именно на таких двух письмах вы проверяете, что DMARC-политика реально работает.

**8. Что решил MTA: логи Postfix.**
```bash
sudo grep -E "spf=|dkim=|dmarc=" /var/log/mail.log | tail -20
sudo postqueue -p
sudo postconf -n | grep -i milter
sudo postfix check
```
В логе ищем строки вида `... dkim=pass`, `spf=pass`, `dmarc=fail`. `postqueue -p` — очередь: если писем десятки и они висят («deferred»), смотрим причину в логе. `postconf -n` показывает только не-дефолтные параметры — так быстрее понять, что реально настроено.

**9. SpamAssassin: разбор письма и обучение.**
```bash
sudo apt install -y spamassassin
spamassassin -t < /tmp/test.eml
spamc -c < /tmp/test.eml
sudo sa-update && sudo systemctl restart spamassassin
sudo sa-learn --spam /path/to/spam-dir
sudo sa-learn --ham /path/to/ham-dir
spamassassin --lint
```
`-t` — прогнать письмо через анализатор и напечатать отчёт с баллами по правилам; `spamc -c` — только итоговый вердикт (`True/False`). `sa-learn` обучает байесовский классификатор (для нормальной работы нужно хотя бы по 200 писем каждого класса). `--lint` — проверка синтаксиса конфига.

**10. Скомпилировать и проверить Sieve-правило карантина.**
```bash
sudo sievec /etc/dovecot/sieve/default.sieve
sudo sieve-test -u ivanov /etc/dovecot/sieve/default.svbin /tmp/test.eml
```
`sievec` компилирует `.sieve` в бинарник, `sieve-test` прогоняет письмо через правило от имени конкретного пользователя и показывает, что бы произошло (fileinto/redirect).

**11. DLP-lite на Postfix (блокировка «длинных чисел» в теле).**
```bash
# /etc/postfix/body_checks
# /[0-9]{13,19}/  REJECT Похоже на номер банковской карты. Отправка запрещена политикой ИБ (152-ФЗ).
sudo postmap -q - regexp:/etc/postfix/body_checks < /tmp/test.eml
sudo postfix reload
```
`postmap -q -` проверяет, срабатывает ли правило на конкретном файле, не отправляя письмо. Учтите: `body_checks` — грубая POSIX-regexp-проверка построчно, без контрольной суммы Луна, поэтому она ловит и безобидные длинные числа (номера накладных).

## 5. Лабораторная работа

### Предусловия

Стенд уровня L2 готов: DC01 (10.10.10.10, AD DS + DNS + DHCP), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01, домен `corp.vektor.lab`, пользователи `ivanov`, `petrova`, группы, папки. Есть PVE01 (10.10.10.70).

Для этой темы вводим **новую ВМ, которой раньше на стенде не было**:

| Имя | ОС | Адрес | Роль |
|-----|----|-------|------|
| MAIL01 | Ubuntu Server 24.04 LTS | 10.10.10.35 | почтовый шлюз: Postfix + Dovecot + Rspamd + Redis |

Почтовые платформы мы разбирали в уроке 62; здесь нам нужен собственный MTA, чтобы руками потрогать SPF/DKIM/DMARC и milter. На DC01 у `ivanov` и `petrova` в свойствах учётной записи должно быть заполнено поле **E-mail** (`ivanov@corp.vektor.lab`, `petrova@corp.vektor.lab`) — оно используется как адрес по умолчанию.

> **Лицензии и доступность.** Ubuntu Server, Postfix, Dovecot, Rspamd, Redis, SpamAssassin, swaks — свободное ПО, скачивается без ограничений; каких-то проблем с доступностью из РФ здесь нет. Rspamd из репозитория Ubuntu может быть старее, чем на rspamd.com — для стенда это нормально.

### Снапшот

В VMware Workstation, при выключенной (или приостановленной) ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-sysadmin-063`.

Снимок `before-sysadmin-063` делаем на **DC01** (там меняется DNS-зона) и на **MAIL01** — сразу после установки ОС и настройки сети, но **до** установки Postfix/Rspamd. Так у вас будет чистая точка отката и для «до», и для «после базовой установки».

```bash
# удобно и быстро с хоста, если установлен open-vm-tools / vmrun в PATH VMware Workstation
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\MAIL01\MAIL01.vmx" before-sysadmin-063
```

### Шаги

1. **Создать MAIL01.** VMware Workstation → New VM → Ubuntu Server 24.04 LTS ISO, 2 vCPU, 2 ГБ RAM, 20 ГБ диск, сеть **VMnet8 (NAT)**. При установке задать hostname `mail`, пользователя `adm`, включить OpenSSH.
2. **Сеть на MAIL01.** Netplan-конфиг → статический адрес и **обязательно** DNS = 10.10.10.10 (DC01), иначе Rspamd не сможет проверить SPF/DKIM/DMARC.
   ```bash
   sudo tee /etc/netplan/00-mail01.yaml > /dev/null <<'EOF'
   network:
     version: 2
     ethernets:
       ens33:
         dhcp4: false
         addresses: [10.10.10.35/24]
         routes:
           - to: default
             via: 10.10.10.2
         nameservers:
           addresses: [10.10.10.10]
   EOF
   sudo chmod 600 /etc/netplan/00-mail01.yaml && sudo netplan apply
   hostnamectl set-hostname mail.corp.vektor.lab
   ```
   Ожидаемый результат: `ip a` показывает 10.10.10.35, `resolvectl query corp.vektor.lab` (или `getent hosts dc01.corp.vektor.lab`) отвечает.
3. **Записи в DNS на DC01.** Из PowerShell от `adm.student` добавить A-запись `mail` → 10.10.10.35 и MX для домена:
   ```powershell
   Add-DnsServerResourceRecordA -ZoneName corp.vektor.lab -Name mail -IPv4Address 10.10.10.35
   Add-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name "@" -MX -MailExchange "mail.corp.vektor.lab." -Preference 10
   ```
   Ожидаемый результат: `Resolve-DnsName mail.corp.vektor.lab` отдаёт 10.10.10.35, `Resolve-DnsName corp.vektor.lab -Type MX` отдаёт `mail.corp.vektor.lab`.
4. **Пакеты на MAIL01.**
   ```bash
   sudo apt update
   sudo apt install -y postfix dovecot-imapd dovecot-sieve dovecot-managesieved \
                       rspamd redis-server swaks
   ```
   Вопрос установщика Postfix — «Internet Site», `System mail name: corp.vektor.lab`.
5. **Базовая конфигурация Postfix** — `/etc/postfix/main.cf`:
   ```text
   myhostname = mail.corp.vektor.lab
   mydomain = corp.vektor.lab
   myorigin = $mydomain
   inet_interfaces = all
   inet_protocols = ipv4
   mydestination = $myhostname, localhost.$mydomain, localhost, $mydomain
   mynetworks = 127.0.0.0/8, 10.10.10.0/24
   home_mailbox = Maildir/
   ```
   Применить: `sudo postfix check && sudo systemctl restart postfix`. **Осознанно небезопасно:** `mynetworks = 10.10.10.0/24` разрешает отправку без аутентификации всей офисной сети. В бою так делать нельзя — нужен submission 587 + SASL и узкий `mynetworks`.
6. **Ящики и Dovecot.** Создать системных пользователей и включить Dovecot с Sieve:
   ```bash
   sudo useradd -m -s /bin/bash ivanov && sudo useradd -m -s /bin/bash petrova
   echo 'ivanov:Lab#2026-vektor' | sudo chpasswd
   echo 'petrova:Lab#2026-vektor' | sudo chpasswd
   sudo useradd -m -s /bin/bash quarantine
   # /etc/dovecot/conf.d/10-mail.conf:  mail_location = maildir:~/Maildir
   # /etc/dovecot/conf.d/90-sieve.conf:  sieve_before = /etc/dovecot/sieve/default.sieve
   sudo systemctl restart dovecot
   ```
   Ожидаемый результат: `sudo doveadm auth test ivanov 'Lab#2026-vektor'` → `auth succeeded`.
7. **Ключ DKIM и записи SPF/DKIM/DMARC.** Команды из раздела 4 (пункты 3–4): сгенерировать ключ на MAIL01, скопировать выданный stdout в TXT-запись `mail._domainkey` на DC01, добавить SPF `v=spf1 ip4:10.10.10.35 -all` и DMARC `p=quarantine; adkim=s; aspf=s; rua=mailto:dmarc@corp.vektor.lab`. Проверить: `dig +short TXT _dmarc.corp.vektor.lab`.
8. **Включить подпись и проверки в Rspamd** — файлы в `/etc/rspamd/local.d/`:
   ```text
   # dkim_signing.conf
   enabled = true;
   selector = "mail";
   path = "/var/lib/rspamd/dkim/$domain.$selector.key";
   allow_username_mismatch = true;

   # actions.conf
   reject = 15;
   add_header = 6;
   greylist = 4;

   # milter_headers.conf
   extended_spam_headers = true;

   # redis.conf
   servers = "127.0.0.1:6379";
   ```
   `sudo rspamadm configtest && sudo systemctl restart rspamd && rspamc stat`. `[ПРОВЕРИТЬ]` имя и поведение опции `extended_spam_headers` в вашей версии Rspamd — от неё зависит, появится ли заголовок `X-Spam-Score` и в каком формате.
9. **Подключить Postfix к Rspamd.** В `/etc/postfix/main.cf`:
   ```text
   smtpd_milters = inet:127.0.0.1:11332
   non_smtpd_milters = $smtpd_milters
   milter_default_action = accept
   milter_protocol = 6
   ```
   `sudo postfix reload`. Проверить, что порт слушается: `ss -lntp | grep 11332`. **Небезопасно по-своему:** `milter_default_action = accept` означает «если Rspamd упал — пропускаем всё без фильтрации». Это выбирают, чтобы не останавливать бизнес; в защищённом контуре часто ставят `tempfail`, чтобы почта вставала в очередь, а не проходила грязной.
10. **Карантин через Sieve.** `/etc/dovecot/sieve/default.sieve`:
    ```sieve
    require ["fileinto", "mailbox", "relational", "comparator-i;ascii-numeric"];

    if header :contains "X-Spam" "Yes" {
        if header :value "ge" :comparator "i;ascii-numeric" "X-Spam-Score" "12" {
            redirect "quarantine@corp.vektor.lab";
            stop;
        }
        fileinto :create "Junk";
        stop;
    }
    ```
    `sudo sievec /etc/dovecot/sieve/default.sieve && sudo systemctl restart dovecot`.
11. **Тесты.** Два вызова `swaks` из пункта 7 раздела 4. Затем на MAIL01:
    ```bash
    sudo grep -E "spf=|dkim=|dmarc=" /var/log/mail.log | tail -5
    rspamc symbols < /var/mail/petrova/Maildir/new/<последнее_письмо>
    ```
    Ожидаемый результат: легитимное письмо — `dkim=pass`, `spf=pass`, `dmarc=pass`, `X-Spam: No`; подделка — `dmarc=fail`, высокий балл, письмо уходит в `Junk`/`quarantine`.
12. **DLP-lite.** Добавить `body_checks` (раздел 4, пункт 11), `sudo postmap /etc/postfix/body_checks`, `sudo postfix reload`. Отправить `swaks --body "4111 1111 1111 1111 оплата по счёту"` — ожидаем отказ `550 ... Отправка запрещена политикой ИБ`. Записать эксперимент в `stand-log.md` и **не оставлять это правило включённым** на весь стенд (иначе оно сломает следующие главы) — закомментируйте строку и сделайте `postfix reload`.

### Проверка

- [ ] `dig +short TXT corp.vektor.lab` возвращает ровно одну SPF-запись `v=spf1 ip4:10.10.10.35 -all`
- [ ] `dig +short TXT mail._domainkey.corp.vektor.lab` содержит `v=DKIM1` и `p=`
- [ ] `dig +short TXT _dmarc.corp.vektor.lab` содержит `v=DMARC1; p=quarantine`
- [ ] `rspamadm configtest` завершается без ошибок, `rspamc stat` показывает ненулевой счётчик scanned
- [ ] `postconf -n | grep milter` показывает `inet:127.0.0.1:11332`
- [ ] Легитимное письмо `ivanov → petrova` в логе даёт `dkim=pass`, `spf=pass`, `dmarc=pass`
- [ ] Поддельное письмо с чужим `From:` получает `dmarc=fail` и не попадает во «Входящие»
- [ ] В ящике `petrova` есть папка `Junk`, в ящике `quarantine` — письма с высоким баллом
- [ ] `body_checks` срабатывает на тестовое письмо с номером карты (и потом отключён)
- [ ] Всё записано в `stand-log.md`

### Сломай и почини

> **Пользователь:** ivanov / **ВМ:** WIN11-CL01 / **Что написал:** «Отправил акт сверки в бухгалтерию — вернулось `550 5.7.1 Message rejected as spam`. До этого весь день отправлял нормально. Закрытие месяца, очень срочно!» / **Срочность:** высокая

**Как воспроизвести поломку.** На DC01 заменить SPF-запись на «правильную из интернета»:
```powershell
Get-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name "@" -RRType Txt |
  Where-Object { $_.RecordData.DescriptiveText -like "v=spf1*" } |
  Remove-DnsServerResourceRecord -ZoneName corp.vektor.lab -Force
Add-DnsServerResourceRecord -ZoneName corp.vektor.lab -Name "@" -Txt `
  -DescriptiveText "v=spf1 include:_spf.yandex.ru -all"
```
Теперь SPF для нашего собственного сервера 10.10.10.35 не проходит, а DMARC строгий (`aspf=s`, `adkim=s`, `p=quarantine`).

**Диагностика по порядку:**
1. Спросить пользователя главное: что именно он отправлял, было ли вложение, менялось ли что-то. Зафиксировать в тикете.
2. Проверить, что письмо реально отклонено нашим шлюзом, а не чужим: `sudo grep -i "ivanov" /var/log/mail.log | tail -20` на MAIL01. Ищем `reject` и имя символа.
3. Посмотреть состав баллов: `rspamc symbols < письмо>` (взять из `Maildir` отправителя/из очереди).
4. Если в символах `R_SPF_FAIL`, `DMARC_POLICY_QUARANTINE` — идём в DNS: `dig +short TXT corp.vektor.lab`.
5. Убедиться, что сломан именно SPF, а DKIM жив: `dig +short TXT mail._domainkey.corp.vektor.lab` и в логе строка `dkim=pass`.
6. Проверить, не истёк ли срок чего-то ещё: `rspamc stat` (uptime, версия), `systemctl status rspamd redis-server`.

**Решение.** Вернуть корректную запись `v=spf1 ip4:10.10.10.35 -all`, дождаться истечения TTL (в лабе — `ipconfig /flushdns` на клиенте, `sudo systemd-resolve --flush-caches` на MAIL01 [ПРОВЕРИТЬ имя службы в вашей версии]), отправить тестовое письмо, убедиться в `spf=pass`. Урок на будущее: **любое изменение SPF/DKIM/DMARC проходит через тикет, ревью и тестовую отправку**, а не «поправил в пятницу вечером».

**Что ответить пользователю:** «Иван, добрый день. Проблема была на нашей стороне: в DNS-записи домена оказалось ошибочное правило проверки отправителя, из-за чего сервер считал ваши письма подделкой. Исправил, письмо отправлено повторно и доставлено. Приносим извинения за задержку — если до конца дня увидите ещё отказы, пришлите, пожалуйста, текст ошибки целиком».

**Закрытие заявки:** категория «Почта → отклонение письма», решение — «Исправлена SPF-запись домена», корневая причина — «несогласованное изменение DNS», профилактика — «регламент изменений DNS + мониторинг DMARC-отчётов `rua`».

### Что изменилось на стенде

- **Новая ВМ MAIL01**: Ubuntu Server 24.04 LTS, 10.10.10.35/24, gw 10.10.10.2, DNS 10.10.10.10. Роли: Postfix (MTA, milter → Rspamd), Dovecot (IMAP + Sieve), Rspamd, Redis.
- **DC01**: в зоне `corp.vektor.lab` появились A `mail` → 10.10.10.35, MX → `mail.corp.vektor.lab`, TXT SPF, TXT `mail._domainkey`, TXT `_dmarc`.
- **Ключи**: приватный DKIM `/var/lib/rspamd/dkim/corp.vektor.lab.mail.key`, селектор `mail`.
- **Учётки на MAIL01**: системные пользователи `ivanov`, `petrova`, `quarantine` (пароль стенда `Lab#2026-vektor`), Maildir `~/Maildir`.
- **Правила**: `/etc/dovecot/sieve/default.sieve` (Junk + карантин при балле ≥ 12), `/etc/postfix/body_checks` (ДLП-проверка, **выключена** после опыта), пороги Rspamd `reject=15 / add_header=6 / greylist=4`.
- **Осознанные компромиссы в безопасности**: `mynetworks = 10.10.10.0/24`, `milter_default_action = accept`, отправка без TLS/SASL. В боевой среде — иначе.

### Откат

1. Выключить MAIL01 и DC01 в VMware Workstation.
2. Правый клик по MAIL01 → **Snapshot → Snapshot Manager** → выбрать `before-sysadmin-063` → **Go To**. То же на DC01.
3. Включить ВМ, проверить: `dig +short TXT corp.vektor.lab` на DC01/UBU01, `systemctl status postfix rspamd` на MAIL01.
4. Записать в `stand-log.md` строку «откат к before-sysadmin-063, причина …».
5. Если нужна чистая переустановка — удалить MAIL01 и пройти шаги 1–3 заново; **не** удаляйте DNS-записи «насовсем» без записи в журнал.

## 6. Ошибки новичков и безопасность

1. **Две SPF-записи в одном домене.** → Получатели читают первую попавшуюся, проверка ведёт себя непредсказуемо. → Держите **ровно одну** запись `v=spf1`; всё остальное — через `include:`.
2. **`p=reject` сразу, без периода `p=none`.** → Отваливается легитимная почта (рассылки, 1С, CRM, внешний сервис отправки). → Всегда поэтапно: `none` + `rua` → анализ отчётов → `quarantine` → `reject`.
3. **`include:`-матpёшка на 12 доменов.** → Превышение лимита 10 DNS-запросов → `permerror` → SPF не работает вообще. → Считать запросы (`dig` + сервисы-калькуляторы SPF), объединять через `ip4:`.
4. **Ключ DKIM с правами 644 и владельцем root.** → Rspamd (пользователь `_rspamd`) не может прочитать ключ, подпись не ставится, а в логе — одна строчка. → `chown _rspamd:_rspamd`, `chmod 600`, проверять `rspamc symbols` после каждой правки.
5. **Ротировали DKIM-ключ «в один день».** → Старые письма в очереди и подписчики с кэшем DNS ломают проверку. → Новый селектор → опубликовать TXT → выждать TTL → переключить подпись → старый ключ удалить через 1–2 недели.
6. **Карантин без процесса разбора.** → Папка `quarantine` растёт, письма теряются, пользователи перестают доверять фильтру и просят «отключить антиспам совсем». → Регламент: ежедневный просмотр, SLA на освобождение письма, отчёт ложных срабатываний, дообучение Bayes.
7. **`milter_default_action = accept` без мониторинга Rspamd.** → При падении фильтра почта идёт «как есть», включая фишинг, и никто не замечает. → Алерт на `systemctl status rspamd` и на рост доли `X-Spam: Yes` в логах.
8. **DLP-правило по regexp без тестов.** → Блокируются счета с длинными номерами, начинается «почта не работает». → Сначала режим «только логировать» (`WARN`/доп. заголовок), неделя наблюдения, потом блокировка.

**Безопасность.** В боевой среде нельзя: разрешать релей через `mynetworks` на весь офис (открытый релей — это рассылка спама от вашего домена и попадание в DNSBL за сутки); держать почту без TLS (`smtpd_tls_security_level = may` — минимум, `encrypt` — норма); хранить DKIM-ключ в домашней папке или в git; отключать проверки для «удобства руководителя» (это обход СЗИ и прямое нарушение внутренних регламентов по 152-ФЗ). Все события по карантину и отказам должны попадать в SIEM (Wazuh/ELK из урока 55) — DLP и антиспам без журналов бесполезны при разборе инцидента.

## 7. Вопросы на собеседовании

**1. Объясните, как работает DMARC и что такое alignment. Чем `p=quarantine` отличается от `p=reject`?**
Ответ: DMARC — TXT-политика в `_dmarc.<domain>`. Получатель проверяет SPF (по конверту) и DKIM (по подписи); DMARC применяется только если **хотя бы одна** проверка прошла *и* домен в результате совпал с доменом в `From:` — это и есть alignment (relaxed допускает совпадение организационного домена, strict требует точного). Если совпадения нет — применяется `p=`: `none` — только отчёты, `quarantine` — в спам/карантин, `reject` — отклонить на SMTP-уровне. `pct=` позволяет раскатывать политику долями.
*Что хочет услышать интервьюер:* понимание разницы «проверка прошла» и «проверка прошла и совпала по домену», а также поэтапного rollout.
*Красный флаг:* «DMARC проверяет, что письмо не спам» — нет, DMARC вообще не про содержание.

**2. Компания рассылает письма клиентам через внешнего провайдера, и они попадают в спам. Ваши действия?**
Ответ: выяснить, с каких IP и с какого домена идёт рассылка; добавить провайдера в SPF (`include:` провайдера, следить за лимитом 10 запросов); попросить провайдера включить DKIM-подпись — лучше на **поддомене** (`mail.corp.vektor.lab`) со своим селектором, чтобы не смешивать репутацию с основным доменом; в DMARC выставить `sp=` для поддомена и при необходимости `pct=`; смотреть `rua`-отчёты, чтобы увидеть, что именно не проходит; отдельно — проверить PTR/rDNS, HELO и попадание IP в DNSBL.
*Что хочет услышать интервьюер:* системный подход (SPF → DKIM на поддомене → DMARC-отчёты → репутация IP) и понимание, что «просто добавить `~all`» — не решение.
*Красный флаг:* «поставлю `p=none` и всё заработает» — это не защита, это лог.

**3. Как вы построите карантин и работу с ложными срабатываниями?**
Ответ: три уровня — «пропустить», «в Junk пользователю» (он сам достаёт), «в карантин администратору/ИБ» для высоких баллов и подозрительных вложений. Карантину нужен владелец и SLA (например, разбор в течение рабочего дня). Обязательны: кнопка/адрес `spam@` и `not-spam@` для обратной связи, регулярное дообучение Bayes (`sa-learn`/автообучение Rspamd), раз в неделю — обзор топ-символов и подстройка порогов, отчёт по количеству ложных срабатываний. Для регуляторики — хранение в карантине заданный срок и журнал доступа к нему.
*Что хочет услышать интервьюер:* что карантин — это **процесс**, а не папка; есть метрики и владелец.
*Красный флаг:* «настроил фильтр и забыл» / «всех в карантин, пусть ИБ разбирает вручную».

**4. Пришло письмо от директора с просьбой срочно оплатить счёт. Как отличить реальный спуфинг от сломанного SPF и что проверить?**
Ответ: смотреть `Authentication-Results`/заголовки (`spf=`, `dkim=`, `dmarc=`) и полный `From:` — домен может быть похожим (гомоглифы, `vektor-pay.ru` вместо `corp.vektor.lab`); проверить `Return-Path` vs `From:` (часто расходятся при спуфинге); посмотреть исходный IP и его репутацию; сравнить с историей переписки; при подтверждении спуфинга — блокировка на шлюзе по домену/IP, уведомление ИБ, проверка, не открывал ли пользователь вложения, и обязательное информирование всех сотрудников. Если же `dmarc=fail` только на письмах **своего** домена — это наша поломка SPF/DKIM, чинится в DNS.
*Что хочет услышать интервьюер:* алгоритм «заголовки → домен → IP → действие → коммуникация» и понимание, что почтовый инцидент — это ещё и IR-процесс (урок 60).
*Красный флаг:* «перешлю ИБ и не моё дело» или «заблокирую отправителя по имени «Директор».

**5. Что такое DLP на уровне почты и почему его недостаточно?**
Ответ: DLP (Data Loss Prevention) — контроль содержимого на предмет утечки: регулярные выражения по номерам карт/паспортов/СНИЛС, словари («договор», «выгрузка базы»), проверка вложений, стоп-слова, цифровые отпечатки документов. На MTA это реализуется через `body_checks`/`header_checks`, модули Rspamd, либо шлюзом класса InfoWatch Traffic Monitor, Solar Dozor `[ПРОВЕРИТЬ актуальные названия и вендоров]`. Недостаточно, потому что почта — один канал: данные утекают через мессенджеры, веб-формы, USB, облачные диски, печать. Настоящий DLP ставится как агент на рабочие станции + сетевой шлюз и работает в связке с классификацией данных и регламентом (152-ФЗ: где ПДн, кто оператор, кто имеет доступ).
*Что хочет услышать интервьюер:* понимание, что DLP — организационно-техническая система, а не «правило в Postfix», и знание классов решений.
*Красный флаг:* «DLP = блокировать слова «секретно» в теме письма».

## 8. Источники (официальная документация)

- **rspamd.com** — раздел *Documentation*: «Configuration», «Modules → DKIM signing», «Modules → SPF/DMARC», «Quickstart / Postfix integration». Поиск: `rspamd postfix milter setup`, `rspamd dkim_signing configuration`.
- **postfix.org** — `postconf(5)` (параметры `smtpd_milters`, `milter_default_action`), `header_checks(5)` и `body_checks(5)`, `postmap(1)`. Поиск: `postfix header_checks body_checks man page`.
- **spamassassin.apache.org** — *Documentation*: `Mail::SpamAssassin` config, `sa-learn`, `spamassassin --lint`. Поиск: `spamassassin sa-learn bayes documentation`.
- **dovecot.org** — *Documentation → Sieve*: `sieve_before`, `sievec`, `sieve-test`. Поиск: `dovecot sieve fileinto redirect documentation`.
- **datatracker.ietf.org** — RFC 7208 (SPF), RFC 6376 (DKIM), RFC 7489 (DMARC), RFC 8617 (ARC). Поиск: `RFC 7489 DMARC`.
- **learn.microsoft.com** — *DNS Server cmdlets*: `Add-DnsServerResourceRecord`, `Add-DnsServerResourceRecordA`. Поиск: `Add-DnsServerResourceRecord TXT PowerShell`.
- **docs.redis.io** — базовое администрирование Redis (нужно для состояния Rspamd). Поиск: `redis server configuration basics`.
- Вендоры DLP/почтовых шлюзов РФ: сайты **infowatch.ru**, **solar.ru** (Solar Dozor), **kaspersky.ru** (Kaspersky Secure Mail Gateway) — названия продуктов и их актуальный состав проверяйте на сайте вендора `[ПРОВЕРИТЬ]`. Поиск: `DLP система почта защита от утечек Россия`.
