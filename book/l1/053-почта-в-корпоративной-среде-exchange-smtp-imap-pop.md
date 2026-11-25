# Почта в корпоративной среде: Exchange, SMTP/IMAP/POP3, SPF/DKIM/DMARC, диагностика недоставки

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №1471.** Пользователь: `ivanov` (бухгалтерия). ВМ: WIN11-CL01. Что написал: «Отправил контрагенту акт сверки, мне пришло письмо "Undelivered Mail Returned to Sender" с каким-то кодом 5.7.1. Контрагент говорит, что ничего не получал. Срочно, до конца дня надо». Срочность: высокая.

Почта — это первый по частоте источник тикетов после «не работает принтер» и «не открывается папка». Причина простая: почта — единственный сервис, где сбой виден сразу двум сторонам, и обе звонят в поддержку. При этом она выглядит как чёрный ящик: пользователь нажал «Отправить», письмо куда-то ушло, а куда именно — непонятно.

После этой главы ты должен уметь: объяснить на пальцах путь письма от Outlook до ящика получателя; назвать порты SMTP/IMAP/POP3 и сказать, зачем их два разных для отправки; прочитать NDR (уведомление о недоставке) и перевести код ошибки на человеческий язык; проверить MX, SPF, DMARC через `nslookup`/`dig`; посмотреть очередь и логи почтового сервера; отличить «проблему у нас» от «проблемы у получателя» — и написать пользователю внятный ответ вместо «попробуйте ещё раз».

Плюс ты познакомишься с Exchange — не потому, что L1 его администрирует, а потому, что в 80% российских контор на Windows почта исторически Exchange или его облачные аналоги, и на собеседовании про это спросят.

## 2. Теория «под капотом»

Почта — это набор протоколов прикладного уровня (L7 модели OSI) поверх TCP. Каждый протокол отвечает за свой этап:

```text
  [Отправитель]                                                  [Получатель]
  Outlook/Thunderbird  --- (1) --->  MTA отправителя  --- (2) --->  MTA получателя  --- (3) --->  Dovecot/MDA  --- (4) ---> Outlook
        MUA                            Postfix/Exchange           Postfix/Exchange          хранилище ящика         MUA
                                   (очередь, MX-поиск)          (фильтры, SPF/DKIM/DMARC)

 (1) SMTP submission: 587 (с авторизацией) или 465 (TLS сразу)
 (2) SMTP relay:      25 — сервер-сервер, БЕЗ авторизации, только по MX
 (3) доставка в ящик: Postfix local / Exchange Store
 (4) IMAP 143/993 или POP3 110/995 — ЗАБРАТЬ почту из ящика
```

**Кто есть кто.** MUA (Mail User Agent) — клиент (Outlook). MTA (Mail Transfer Agent) — почтовый сервер, который передаёт письмо (Postfix, Exchange Transport). MDA (Mail Delivery Agent) — тот, кто кладёт письмо в конкретный ящик (Dovecot, `postfix/local`). Хранилище — Maildir/`/var/mail` в Linux или база Exchange (.edb).

**Как сервер находит сервер получателя.** По DNS-записи MX (Mail eXchanger) домена получателя: «письма для `contoso.ru` принимает `mx.contoso.ru`, приоритет 10». Если MX нет — отправитель попробует A-запись домена (устаревшее поведение, но всё ещё работает). Именно поэтому «у нас пропала почта с одного адреса» часто означает «у них сломалась DNS-зона».

**SMTP vs IMAP vs POP3 — самая частая путаница у новичков.** SMTP (`25`, `587`, `465`) — только отправка и передача между серверами. IMAP (`143`, `993`) — доступ к ящику на сервере, письма хранятся на сервере, папки синхронизируются, письмо читается с любого устройства. POP3 (`110`, `995`) — «скачать и обычно удалить с сервера», один ящик — одно устройство. В корпоративной среде стандарт — IMAP; POP3 встречается у старых конфигураций и это отдельный источник тикетов «письма пришли на домашний ноутбук, а на рабочем их нет».

**Exchange.** Продукт Microsoft: транспортная служба (Transport Service) на Hub Transport / Mailbox-роли, база почтовых ящиков, свои коннекторы отправки/приёма (Send/Receive connectors) вместо `main.cf`. Управление — веб-консоль EAC (Exchange admin center) и PowerShell (Exchange Management Shell). Для L1 из Exchange важны три вещи: OWA (веб-почта `https://сервер/owa`), трассировка сообщений (message trace / `Get-MessageTrackingLog`) и очередь (`Get-Queue`). В 2026 году в РФ доступность облачных сервисов Microsoft для организаций ограничена и меняется — перед советом «переезжайте в Exchange Online» смотри актуальные условия `[ПРОВЕРИТЬ]`; легальные альтернативы — Яндекс 360, VK WorkSpace, Mail.ru для бизнеса (детали в уроке 54).

**SPF, DKIM, DMARC — защита от подделки отправителя.** Это три DNS-записи и три проверки на стороне получателя:

| Что | Где лежит | Что проверяет | Пример значения |
|---|---|---|---|
| SPF | TXT на домене | имеет ли право IP отправителя слать почту от этого домена | `v=spf1 a:mail.corp.vektor.lab -all` |
| DKIM | TXT на `селектор._domainkey.домен` | криптоподпись письма; тело и часть заголовков не подменили | `v=DKIM1; k=rsa; p=MIIBIjAN...` |
| DMARC | TXT на `_dmarc.домен` | что делать, если SPF/DKIM не сошлись, и куда слать отчёты | `v=DMARC1; p=none; rua=mailto:postmaster@corp.vektor.lab` |

`-all` в SPF означает «всё, что не перечислено, — не наш IP, reject». `p=none` в DMARC — режим наблюдения (ничего не блокируем, только собираем отчёты), `p=quarantine` — в спам, `p=reject` — отклонять. Результаты проверок получатель пишет в заголовки письма: `Authentication-Results: spf=pass dkim=pass dmarc=pass`. Отсюда правило L1: **прежде чем звонить в чужую поддержку, посмотри заголовки письма**. Если там `spf=fail` — проблема на нашей стороне, а не у них.

## 3. Аналогия из жизни

Корпоративная почта — это городская почтовая сеть. `ivanov` пишет письмо и опускает в ящик у себя в отделении (SMTP submission, порт 587 — «окошко приёма с авторизацией»: без паспорта не примут). Сортировочный центр (MTA) смотрит в справочник (DNS, запись MX): «письма в город Петровск принимает вот это отделение». Дальше письмо едет поездом между сортировочными центрами (SMTP relay, порт 25 — «служебный канал между отделениями», частным лицам туда нельзя). В Петровске письмо кладут в абонементный ящик № petrova (MDA). Петрова приходит и забирает — либо сама ходит за корреспонденцией в отделение и видит все ящики и папки (IMAP), либо почтальон приносит ей домой всю пачку и оставляет расписку (POP3).

SPF — это список «кто имеет право отправлять письма от имени нашего города». DKIM — сургучная печать на конверте: если её вскрыли в пути, получатель это увидит. DMARC — инструкция на конверте: «если печати нет или отправитель не из списка — выбрось в мусор и напиши нам отчёт». Аналогия перестаёт работать в одном месте: у письма нет «оригинального» экземпляра — получатель видит только копию, и если копию подменили до отправки, никакая печать этого не покажет.

## 4. Практика и команды

Примеры вывода условные, но структура настоящая.

**1. MX-запись домена.**
```powershell
# Windows
nslookup -type=mx corp.vektor.lab
Resolve-DnsName -Name corp.vektor.lab -Type MX
```
```bash
# Linux (если нет dig: sudo apt install bind9-dnsutils)
dig +short MX corp.vektor.lab
```
```text
corp.vektor.lab MX preference = 10, mail exchanger = mail.corp.vektor.lab
```
`-type=MX` — тип записи. Смотрим: запись вообще есть? указывает на живой хост? один приоритет или несколько? Если MX пусто — внешняя почта в домен не пойдёт вообще.

**2. SPF и DMARC в DNS.**
```powershell
nslookup -type=txt corp.vektor.lab
nslookup -type=txt _dmarc.corp.vektor.lab
```
```bash
dig +short TXT corp.vektor.lab
dig +short TXT _dmarc.corp.vektor.lab
```
```text
"v=spf1 a:mail.corp.vektor.lab -all"
"v=DMARC1; p=none; rua=mailto:postmaster@corp.vektor.lab"
```
Ключевое: SPF должен быть **один** (два SPF-файла = fail по RFC 7208). Ищем `-all` / `~all` и понимаем политику.

**3. DKIM-запись по селектору.**
```bash
dig +short TXT mail._domainkey.corp.vektor.lab
```
```text
"v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A..."
```
Селектор (`mail.`) задаётся на почтовом сервере; если в письме `s=mail`, а записи нет — `dkim=none/fail`.

**4. Проверка доступности портов почты.**
```powershell
Test-NetConnection -ComputerName 10.10.10.30 -Port 25
Test-NetConnection -ComputerName 10.10.10.30 -Port 143
```
```bash
nc -vz 10.10.10.30 25
nc -vz 10.10.10.30 143
```
```text
Connection to 10.10.10.30 25 port [tcp/smtp] succeeded!
```
`-vz` — verbose, только проверка соединения без передачи данных. «Succeeded» — сеть и порт в порядке, проблема выше (авторизация, конфиг, отказ по политике).

**5. Живой SMTP-диалог (проверяем, что сервер вообще отвечает и что за баннер).**
```bash
openssl s_client -starttls smtp -connect 10.10.10.30:25 -quiet
```
```text
220 mail.corp.vektor.lab ESMTP Postfix (Ubuntu)
EHLO test.local
250-mail.corp.vektor.lab
250-SIZE 10240000
250 STARTTLS
...
```
`-starttls smtp` — поднять TLS внутри SMTP-сессии. По баннеру и списку `250-` видно версию сервера и какие расширения (STARTTLS, AUTH) он предлагает.

**6. Тестовое письмо в обход Outlook.**
```bash
sudo apt install swaks          # если не установлен
swaks --to petrova@corp.vektor.lab --from ivanov@corp.vektor.lab --server 10.10.10.30
```
Отправляет письмо по SMTP напрямую. Это главный трюк L1: **если через swaks письмо доходит, а через Outlook нет — проблема в клиенте, а не в сервере**.

**7. Очередь сообщений Postfix.**
```bash
postqueue -p        # то же, что mailq
sudo postqueue -f   # принудительно отправить всё из очереди
```
```text
-Queue ID-  --Size-- ----Arrival Time---- -Sender/Recipient-------
A1B2C3D4E5*     512 Mon Mar  2 10:11:12  ivanov@corp.vektor.lab
                                         petrova@corp.vektor.lab (connect to 10.10.10.30[10.10.10.30]:25: ...)
```
`*` — письмо в процессе отправки, без `*` — лежит и ждёт. В скобках Postfix пишет причину задержки. Пустая очередь = письма ушли дальше.

**8. Логи почтового сервера.**
```bash
sudo tail -f /var/log/mail.log
sudo journalctl -t postfix/smtpd -n 50 --no-pager
```
```text
Mar  2 10:11:12 mail postfix/smtpd[2211]: connect from unknown[10.10.10.101]
Mar  2 10:11:12 mail postfix/smtpd[2211]: NOQUEUE: reject: RCPT from unknown[10.10.10.101]: 554 5.7.1 <petrova@corp.vektor.lab>: Relay access denied; from:<ivanov@corp.vektor.lab>
Mar  2 10:11:13 mail postfix/local[2213]: 9F2A: to=<petrova@corp.vektor.lab>, relay=local, status=sent
```
`status=sent` — доставлено в ящик, `status=bounced` / `deferred` — нет. `NOQUEUE: reject` — сервер отказал ещё до постановки в очередь, письмо не ушло никуда.

**9. Конфиг Postfix «как есть» и проверка синтаксиса.**
```bash
postconf -n
postfix check
```
```text
mydestination = $myhostname, corp.vektor.lab, localhost, localhost.localdomain
myhostname = mail.corp.vektor.lab
mynetworks = 127.0.0.0/8 10.10.10.0/24
home_mailbox = Maildir/
```
`-n` — только изменённые относительно дефолта параметры. В первую очередь смотрим `mydestination` (за какие домены сервер отвечает), `mynetworks` (кому разрешён relay) и `myhostname`.

**10. Заголовки письма в Outlook (классический клиент).**
Путь: открыть письмо → **Файл → Свойства → Internet headers**. Ищем строки `Received:` (путь письма, сверху вниз — от последнего сервера к первому), `Authentication-Results:` (spf/dkim/dmarc), `Return-Path`. Это то, что просят приложить к тикету.

**11. Трассировка в Exchange (на нашем стенде нет — нужен Exchange).**
```powershell
Get-MessageTrackingLog -Sender ivanov@corp.vektor.lab -Start (Get-Date).AddHours(-2) |
  Format-Table Timestamp,EventId,Source,RecipientStatus
Get-Queue
```
`EventId` DSN/DELIVER/SEND — что произошло с письмом; `RecipientStatus` — почему не доставлено. GUI-аналог: Exchange admin center → **Mail flow → Message trace**. В Microsoft 365 — тот же Message trace в EAC.

**12. Отправка из Linux-консоли, если SMTP открыт наружу (проверка анонимного relay).**
```bash
printf 'EHLO test\r\nMAIL FROM:<a@b.ru>\r\nRCPT TO:<test@example.com>\r\nQUIT\r\n' | nc 10.10.10.30 25
```
Если сервер принял `RCPT TO` на чужой домен — он **открытый relay**. В нормальной среде так быть не должно.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
- **DC01** (10.10.10.10): AD DS + DNS установлены, зона `corp.vektor.lab` есть, ты умеешь входить как `VEKTOR\adm.student`.
- **UBU01** (10.10.10.30, Ubuntu Server 24.04): доступ по SSH, интернет для `apt` (или локальное зеркало). На стенде **отдельной почтовой ВМ нет** — поэтому почтовую роль (Postfix + Dovecot) поднимаем прямо на UBU01. В реальной компании под это дают отдельный сервер; здесь мы сознательно смешиваем роли.
- **WIN11-CL01**: введён в домен, вход `VEKTOR\ivanov`, установлен классический Outlook (из урока 52), открывается.
- **Exchange мы не устанавливаем**: для него нужна лицензия и отдельная тема уровня L2. Команды из п.4.11 и таблицу NDR отрабатываем «на бумаге» по реальным ошибкам, которые гуглятся и показываются на собеседовании.
- Если домен ещё не поднят — сначала пройди модуль Active Directory, без DNS-зоны почта работать не будет.

### Снапшот
В VMware Workstation на **всех трёх** ВМ (DC01, UBU01, WIN11-CL01): правый клик по ВМ → **Snapshot → Take Snapshot** → имя `before-l1-053`. Для выключенной ВМ снапшот тоже снимается, но лучше с остановленными сервисами. Если нужна консоль — `vmrun snapshot "<путь к .vmx>" before-l1-053`.

### Шаги

1. **UBU01.** Устанавливаем пакеты:
   ```bash
   sudo apt update
   sudo apt install -y postfix dovecot-imapd dovecot-pop3d
   ```
   В диалоге Postfix выбираем **Internet Site**, System mail name: `mail.corp.vektor.lab`. Ожидаемо: пакеты установлены, `systemctl status postfix` — `active (running)`.

2. **UBU01.** Создаём «ящики» — локальных пользователей (упрощение: в реальной компании ящики берутся из AD, а не из `/etc/passwd`):
   ```bash
   sudo useradd -m -s /bin/bash ivanov
   sudo useradd -m -s /bin/bash petrova
   echo 'ivanov:Lab#2026-vektor' | sudo chpasswd
   echo 'petrova:Lab#2026-vektor' | sudo chpasswd
   ```
   Ожидаемо: `id ivanov` показывает uid/gid, домашние каталоги `/home/ivanov`, `/home/petrova` созданы.

3. **UBU01.** Правим `main.cf` через `postconf -e`. **Одинарные кавычки обязательны** — иначе `$myhostname` подставит shell, а не Postfix:
   ```bash
   sudo postconf -e 'myhostname = mail.corp.vektor.lab'
   sudo postconf -e 'mydomain = corp.vektor.lab'
   sudo postconf -e 'myorigin = $mydomain'
   sudo postconf -e 'mydestination = $myhostname, corp.vektor.lab, localhost, localhost.localdomain'
   sudo postconf -e 'inet_interfaces = all'
   sudo postconf -e 'mynetworks = 127.0.0.0/8 10.10.10.0/24'
   sudo postconf -e 'home_mailbox = Maildir/'
   sudo systemctl reload postfix && postconf -n
   ```
   Ожидаемо: `postconf -n` выводит семь строк, `mydestination` содержит `corp.vektor.lab`.

4. **UBU01.** Настраиваем Dovecot (IMAP/POP3). Для лабы отключаем TLS и разрешаем plaintext — **это небезопасно**, в боевой среде так нельзя:
   ```bash
   sudo sed -i 's/^ssl = .*/ssl = no/' /etc/dovecot/conf.d/10-ssl.conf
   sudo sed -i 's/^disable_plaintext_auth = .*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf
   sudo sed -i 's|^mail_location = .*|mail_location = maildir:~/Maildir|' /etc/dovecot/conf.d/10-mail.conf
   sudo systemctl restart dovecot
   sudo ss -lntp | grep -E ':(25|110|143)\b'
   ```
   Ожидаемо: слушают `:25` (master), `:143` (imap), `:110` (pop3). Если UFW включён — `sudo ufw allow 25,110,143/tcp`.

5. **DC01.** DNS Manager (`dnsmgmt.msc`) → Forward Lookup Zones → `corp.vektor.lab`:
   - New Host (A): имя `mail`, IP `10.10.10.30`.
   - New Mail Exchanger (MX): Mail server `mail.corp.vektor.lab`, preference `10`.
   - New Record → Text (TXT), имя пустое (корень зоны), значение `v=spf1 a:mail.corp.vektor.lab -all`.
   - New Record → Text (TXT), имя `_dmarc`, значение `v=DMARC1; p=none; rua=mailto:postmaster@corp.vektor.lab`.
   Ожидаемо: три новых записи видны в зоне. PowerShell-вариант (синтаксис для корня зоны уточни в справке `[ПРОВЕРИТЬ]`): `Add-DnsServerResourceRecordA -ZoneName corp.vektor.lab -Name mail -IPv4Address 10.10.10.30`.

6. **WIN11-CL01.** Проверяем, что клиент видит записи:
   ```powershell
   Resolve-DnsName -Name corp.vektor.lab -Type MX
   Resolve-DnsName -Name corp.vektor.lab -Type TXT
   Test-NetConnection -ComputerName 10.10.10.30 -Port 25
   ```
   Ожидаемо: MX на `mail.corp.vektor.lab`, TXT со SPF, `TcpTestSucceeded : True`.

7. **WIN11-CL01.** Настраиваем Outlook: **File → Add Account → Manually configure server settings → Internet E-mail**. IMAP: `mail.corp.vektor.lab`, порт `143`, шифрование **None**; SMTP: `mail.corp.vektor.lab`, порт `25`, шифрование **None**, «Outgoing server requires authentication» — **снято**. Учётка `ivanov`, пароль `Lab#2026-vektor`. Ожидаемо: Outlook создаёт профиль, папки Inbox/Drafts/Sent подтягиваются с сервера.

8. **WIN11-CL01.** Отправляем письмо: `petrova@corp.vektor.lab`, тема `Проверка 053`, текст любой. Ожидаемо: письмо уходит из Outbox, ошибок нет.

9. **UBU01.** Проверяем доставку:
   ```bash
   sudo ls -la /home/petrova/Maildir/new/
   sudo tail -n 20 /var/log/mail.log
   postqueue -p
   ```
   Ожидаемо: в `Maildir/new/` лежит файл письма; в логе `status=sent (delivered to maildir)`; `postqueue -p` → `Mail queue is empty`.

10. **UBU01.** Проверяем SMTP руками и забираем письмо по IMAP:
    ```bash
    swaks --to ivanov@corp.vektor.lab --from petrova@corp.vektor.lab --server 10.10.10.30 --body "тест 053"
    sudo ls /home/ivanov/Maildir/new/
    ```
    Ожидаемо: swaks заканчивается `250 2.0.0 Ok: queued as XXXXX`, письмо появляется в Maildir ivanov.

11. **WIN11-CL01.** В Outlook открываем письмо от petrova → **Файл → Свойства → Internet headers**, находим `Return-Path`, `Received`. Фиксируем в блокноте, сколько строк `Received` (по одной на каждый сервер, через который прошло письмо). Ожидаемо: минимум одна.

12. **UBU01.** Записываем изменения в `stand-log.md`: UBU01 получил роли MTA/MDA, открыты 25/110/143, созданы пользователи; DC01 — MX, A `mail`, TXT SPF, TXT `_dmarc`.

### Проверка
- [ ] На DC01 в зоне `corp.vektor.lab` есть A `mail` → 10.10.10.30, MX → `mail.corp.vektor.lab` (10), TXT SPF, TXT `_dmarc`.
- [ ] `Resolve-DnsName -Type MX` с WIN11-CL01 возвращает `mail.corp.vektor.lab`.
- [ ] `Test-NetConnection` на 25 и 143 — успешно.
- [ ] Outlook `ivanov` отправляет письмо, оно доходит до ящика `petrova` (`/home/petrova/Maildir/new/` непустой).
- [ ] `swaks` доставляет письмо с консоли.
- [ ] `postqueue -p` показывает пустую очередь.
- [ ] В `/var/log/mail.log` есть строки `status=sent`, нет `NOQUEUE: reject`.
- [ ] В `stand-log.md` записаны все изменения.

### Сломай и почини

> **Пользователь:** `ivanov` (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Пишу Петровой в HR — сразу приходит письмо "Undelivered Mail Returned to Sender", внутри `554 5.7.1 <petrova@corp.vektor.lab>: Relay access denied`. До обеда нужно отправить, помогите!»
> **Срочность:** высокая

**Как воспроизвести поломку (на UBU01):**
```bash
sudo postconf -e 'mydestination = $myhostname, localhost, localhost.localdomain'
sudo systemctl reload postfix
```
Мы убрали `corp.vektor.lab` из списка доменов, за которые сервер отвечает. Теперь Postfix считает наш домен «чужим» и отказывает в приёме.

**Диагностика по шагам (именно в этом порядке):**

1. **Читаем NDR целиком**, не только первую строку. Код `5.7.1` = «отказано по политике/правам»; текст `Relay access denied` = «я не принимаю почту для этого домена, а пересылать чужие письма не обязан».
2. **Определяем, кто отказал.** Смотрим в NDR: там указан наш сервер (`mail.corp.vektor.lab`) или чужой. Наш → идём внутрь. Чужой → другая ветка (SPF/blacklist/спам-политика принимающей стороны), и это уже не наша зона.
3. **Проверяем сеть и порт** — исключаем банальности: `Test-NetConnection -ComputerName 10.10.10.30 -Port 25`. Успешно → сеть не при чём, идём дальше.
4. **Проверяем конфиг:** `postconf -n | grep -E 'mydestination|mynetworks|myhostname'`. Видим, что в `mydestination` нет `corp.vektor.lab` — вот и причина.
5. **Подтверждаем логом:** `sudo tail -n 50 /var/log/mail.log` →
   ```text
   NOQUEUE: reject: RCPT from unknown[10.10.10.101]: 554 5.7.1 <petrova@corp.vektor.lab>: Relay access denied; from:<ivanov@corp.vektor.lab> to:<petrova@corp.vektor.lab> proto=ESMTP helo=<WIN11-CL01>
   ```
   `NOQUEUE` = письмо даже не встало в очередь, отказ мгновенный. Совпадает с NDR — диагноз подтверждён.
6. **Проверяем гипотезу «а раньше работало?»** — `git`/история правок нет, но в `stand-log.md` записано исходное значение `mydestination`. Возвращаем его.
7. **Чиним:**
   ```bash
   sudo postconf -e 'mydestination = $myhostname, corp.vektor.lab, localhost, localhost.localdomain'
   sudo postfix check && sudo systemctl reload postfix
   ```
8. **Переотправляем** письмо (из Outlook или `swaks`), убеждаемся: `250 2.0.0 Ok: queued`, в логе `status=sent`, файл в `/home/petrova/Maildir/new/`. Письмо, которое «застряло», при отказе на этапе RCPT не сохраняется — отправитель должен отослать заново; если бы оно было в очереди, помог бы `sudo postqueue -f`.

**Ответ пользователю:**
> Иван, разобрался. Письма блокировались на нашем почтовом сервере из-за ошибки в его настройках: сервер не считал домен `corp.vektor.lab` «своим» и отказывался принимать почту для внутренних адресов. Настройку исправил, проверил тестовой отправкой — Петровой письма доходят. Пожалуйста, отправьте акт ещё раз, я проконтролирую доставку.

**Закрытие заявки:** категория «Почта → недоставка внутренняя», приоритет «Высокий», решение «Исправлен параметр `mydestination` в конфигурации Postfix на UBU01, выполнена проверка доставки», привязка к инциденту-причине (если настройку меняли мы — это Change с последующим разбором).

### Что изменилось на стенде
- **UBU01** (10.10.10.30): установлены `postfix`, `dovecot-imapd`, `dovecot-pop3d`; сервер работает как MTA/MDA для домена `corp.vektor.lab`; открыты TCP 25, 110, 143; TLS для Dovecot **отключён** (учебное упрощение); созданы локальные пользователи `ivanov`, `petrova` с паролем стенда; почта в формате Maildir (`~/Maildir`).
- **DC01** (10.10.10.10): в зоне `corp.vektor.lab` добавлены A `mail` → 10.10.10.30, MX `corp.vektor.lab` → `mail.corp.vektor.lab` (pref 10), TXT SPF, TXT `_dmarc` в режиме `p=none`.
- **WIN11-CL01**: в Outlook добавлен IMAP/SMTP-профиль `ivanov@corp.vektor.lab` (сервер `mail.corp.vektor.lab`, порты 143/25, без шифрования).
- **FS01, WIN10-CL02, ASTRA01** — не изменялись.

### Откат
VMware Workstation → выбрать ВМ → **Snapshot Manager** → снапшот `before-l1-053` → **Revert**. Откатывать нужно **все три** ВМ (DC01, UBU01, WIN11-CL01), иначе получишь рассинхрон: Outlook настроен на сервер, которого уже нет. После откатa верни в `stand-log.md` пометку «урок 053 откачен».

## 6. Ошибки новичков и безопасность

1. **«Перезапущу сервер — вдруг поможет».** → Перезапуск Postfix/Dovecot рвёт активные IMAP-сессии у всех пользователей и не лечит причину. → Сначала `postqueue -p` и `/var/log/mail.log`, потом решение.
2. **Путать порты отправки.** → Настраивают Outlook на 25 «с авторизацией» или на 587 без неё — почта не уходит, а иногда уходит в очередь на сутки. → 587 = submission с AUTH, 25 = сервер-сервер, 465 = TLS сразу, 993/995 = защищённый приём.
3. **Игнорировать `Received`-заголовки.** → Начинают чинить «у нас», когда отказ пришёл с чужого сервера. → Читаем NDR и `Authentication-Results` до любых действий.
4. **Верить пользователю на слово «письмо не дошло».** → На деле оно в «Спаме»/«Нежелательной почте» или в папке другого профиля Outlook. → Проверяем лог доставки: `status=sent` = сервер доставил, дальше вопрос к папкам клиента.
5. **Править SPF наугад.** → Добавляют второй `v=spf1` или лишний пробел → SPF permanent error, письма начинают резаться у всех получателей. → Одна запись на домен, проверка через `dig +short TXT` и внешний валидатор.
6. **Ставить `p=reject` в DMARC сразу.** → Отваливается вся легитимная рассылка (1С, банк, ЭДО), которая ходит через сторонний релей. → Начинают с `p=none`, смотрят отчёты `rua`, потом ужесточают.
7. **Забыть про `mynetworks`.** → Оставили `10.10.10.0/24` и ушли домой — любой заражённый ПК в офисе рассылает спам от имени компании и домен попадает в блок-листы. → Минимально необходимые сети, обязательная авторизация на submission.
8. **Настраивать почту в обход тикета «по-дружески».** → Изменения без следов: следующий админ не понимает, почему конфиг нестандартный. → Любая правка почтового сервера — через заявку/изменение и с записью в `stand-log.md`.

### Безопасность
В этой лабе мы **отключили TLS у Dovecot** и **разрешили relay всей подсети 10.10.10.0/24** — так проще, но так нельзя в бою: пароль уходит по сети открытым текстом, а любой сотрудник может отправить письмо «от лица» компании. В нормальной среде: IMAPS 993 с валидным сертификатом, submission 587 только с AUTH, `mynetworks` — только localhost, DNS-записи SPF/DKIM/DMARC настроены, а почтовый сервер не смотрит в интернет напрямую, а стоит за антиспам-шлюзом. Пароль `Lab#2026-vektor` — только для стенда: в реальной работе пароли не передаются в чатах, не хранятся в открытом виде в конфигах и меняются при компрометации.

## 7. Вопросы на собеседовании

**1. Пользователь говорит: «Письмо не отправилось». Твои действия?**
Ответ: сначала прошу скриншот/текст ошибки (NDR) — код ошибки определяет всю дальнейшую ветку. Дальше проверяю: интернет и доступность почтового сервера (`Test-NetConnection` на 587/25), не заполнен ли ящик, размер вложения, статус в Outlook (`Outbox`/`Отправленные`), затем — по логам сервера, дошло ли письмо. Если NDR с нашего сервера — смотрю `/var/log/mail.log` или `Get-MessageTrackingLog`, если с чужого — читаю причину у них (SPF, blocklist, «user unknown»).
*Что хочет услышать интервьюер:* порядок «симптом → код ошибки → проверка сети → логи сервера → ответ пользователю», и что ты не меняешь конфиг до диагноза.
*Красный флаг:* «перезагружу почтовый сервер» или «попробуйте ещё раз» без единого вопроса.

**2. Чем SMTP отличается от IMAP и POP3?**
Ответ: SMTP — транспорт, отправка и передача между серверами (25 — relay между серверами, 587 — отправка клиентом с авторизацией, 465 — TLS сразу). IMAP (143/993) — доступ к ящику, письма хранятся на сервере и синхронизируются между устройствами. POP3 (110/995) — скачивание с сервера, обычно с удалением, ящик привязан к одному устройству. В компании стандарт — IMAP; POP3 даёт классический тикет «письма есть на ноутбуке, но нет на рабочем ПК».
*Что хочет услышать интервьюер:* что ты различаешь транспорт и доступ и знаешь номера портов.
*Красный флаг:* «POP3 — это старый IMAP» без понимания, где хранятся письма.

**3. Что такое SPF, DKIM и DMARC и зачем они нужны?**
Ответ: три DNS-механизма против подделки отправителя. SPF — TXT-запись со списком IP/хостов, которым разрешено слать почту от домена; `-all` запрещает всё остальное. DKIM — криптоподпись письма, публичный ключ лежит в TXT на `селектор._domainkey.домен`, получатель проверяет, что письмо не подменили. DMARC — политика на `_dmarc`: что делать при провале SPF/DKIM (`none`/`quarantine`/`reject`) и куда слать отчёты (`rua`). Результаты видны в заголовке `Authentication-Results`.
*Что хочет услышать интервьюер:* что SPF проверяет IP, DKIM — подпись, DMARC — сводит их вместе и задаёт политику; что DMARC вводится постепенно.
*Красный флаг:* «SPF — это антивирус/шифрование».

**4. Письмо вернулось с кодом 550 5.1.1. Что это значит?**
Ответ: 5.х.х — постоянная ошибка (в отличие от 4.х.х, временной). `5.1.1` — «такого получателя на сервере нет»: опечатка в адресе, уволенный сотрудник, удалённый ящик. Проверяю адрес, смотрю, есть ли такой получатель в AD (`Get-ADUser`), если внешний — прошу отправителя уточнить адрес у контрагента. Своими силами «доставить» это письмо нельзя.
*Что хочет услышать интервьюер:* различие 4xx/5xx и понимание, что 5.1.1 решается на стороне адреса, а не «настройкой Outlook».
*Красный флаг:* «добавлю адрес в белый список» — белый список не создаёт несуществующий ящик.

**5. Где смотреть, что произошло с письмом в Exchange?**
Ответ: трассировка сообщений — Exchange admin center → Mail flow → Message trace, либо в Exchange Management Shell `Get-MessageTrackingLog` с фильтрами по отправителю/получателю и времени; состояние очереди — `Get-Queue`; заголовки письма — в Outlook через **Файл → Свойства → Internet headers**. В Microsoft 365 трассировка живёт в том же разделе EAC, ограничение по глубине хранения истории зависит от тарифа `[ПРОВЕРИТЬ]`.
*Что хочет услышать интервьюер:* что ты знаешь инструмент трассировки и не лезешь в базы Exchange руками.
*Красный флаг:* «пойду смотреть логи в папке Exchange» — без понимания, что это EAC/EMS.

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Exchange Server»: статьи «Mail flow», «Message tracking», «Get-MessageTrackingLog», «Exchange admin center». Поиск: `Exchange Server message tracking site:learn.microsoft.com`.
- **learn.microsoft.com** — «Microsoft 365 Defender / Exchange admin center message trace». Поиск: `message trace Exchange admin center`.
- **postfix.org** — основная документация: `postconf(5)` (все параметры, в т.ч. `mydestination`, `mynetworks`), `STANDARD_CONFIGURATION_README`, `POSTSCREEN_README`, `BACKSCATTER_README`. Поиск: `postfix standard configuration readme`.
- **dovecot.org** — «Dovecot documentation», разделы «Mail location» и «Authentication». Поиск: `dovecot maildir configuration`.
- **rfc-editor.org** — RFC 5321 (SMTP), RFC 7208 (SPF), RFC 6376 (DKIM), RFC 7489 (DMARC). Поиск: `RFC 7208 SPF`.
- **dmarc.org** — «Overview» и «DMARC deployment guide». Поиск: `DMARC overview deployment guide`.
- **iana.org** — «Service Name and Transport Protocol Port Number Registry» (актуальные номера портов). Поиск: `IANA service names port registry`.
- **ubuntu.com** — Ubuntu Server Guide, раздел «Email services» (Postfix на Ubuntu 24.04). Поиск: `Ubuntu server guide postfix`.
- Инструменты проверки (не официальная документация, но рабочие): MXToolbox, mail-tester, Google Admin Toolbox — проверка SPF/DKIM/DMARC и «спам-оценки» письма. Поиск: `SPF DKIM DMARC checker`.
