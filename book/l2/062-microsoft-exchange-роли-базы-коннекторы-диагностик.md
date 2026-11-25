# Microsoft Exchange: роли, базы, коннекторы, диагностика очередей и недоставки

## 1. Введение (зачем это нужно в реальной работе)

**Заявка №4417.** Пользователь: ivanov. ВМ: WIN11-CL01. Что написал: «Отправил в бухгалтерию письмо с актами — не дошло. Отправил повторно — вернулось: "Delivery has failed… Remote Server returned '550 5.1.10 RESOLVER.ADR.RecipientNotFound'". А два письма контрагентам висят в "Исходящих" и не уходят уже час. Срочно, у нас закрытие месяца». Срочность: высокая.

Почта — сервис, у которого нет «перезагрузить и подождать». Если она не работает — останавливается работа компании, и звонок идёт сразу тебе. Внутри темы три разные проблемы, которые новичок видит как одну: неверный адрес получателя (5.1.10), застрявшая очередь (письма не уходят) и неверная маршрутизация (коннектор). После этой главы ты будешь:

- понимать, какие службы на сервере Exchange отвечают за приём, маршрутизацию и доставку письма;
- знать, из чего состоит база данных ящиков и почему её логи нельзя трогать руками;
- уметь читать очереди и видеть причину остановки;
- находить конкретное письмо по журналу отслеживания и объяснять NDR человеческим языком;
- отличать «сломался Exchange» от «сломался DNS/адрес/клиент» — за 10 минут, а не за два дня.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Роли.** Начиная с Exchange 2013 Microsoft оставила две серверные роли: **Mailbox (сервер почтовых ящиков)** и **Edge Transport (пограничный транспорт, в DMZ)**. Пять старых ролей (CAS, Hub Transport, Mailbox, UM, Edge) слились в Mailbox. В Exchange 2019 роль Unified Messaging удалена вообще. Внутри Mailbox-роли живут фактически четыре сервиса:

| Сервис | Что делает |
|---|---|
| Client Access (IIS) | OWA, ECP, MAPI/HTTP, EWS, ActiveSync, Exchange PowerShell; порт 443 |
| Front End Transport | принимает SMTP извне/от клиентов на 25 и 587, фильтрует по receive-коннекторам |
| Transport (Hub) | маршрутизация, транспортные правила, антиспам, очереди |
| Mailbox Transport | забор письма из базы (Submission) и раскладка в базу (Delivery) |
| Store (ESE) | сама база данных и почтовые ящики |

**Путь письма** (упрощённо, но ровно так, как ты будешь его диагностировать):

```text
 Отправитель (WIN11-CL01, Outlook / OWA)
      |  MAPI over HTTP 443  или  SMTP 587 (клиентская отправка)
      v
 +------------------- EX01 (роль Mailbox) --------------------+
 | Front End Transport  <-- Receive connector: Default(25),   |
 |                          Client(587), Proxy(25 от Exchange) |
 |        |  локальный SMTP 25                                 |
 |        v                                                    |
 | Transport (Hub)      --> транспортные правила, агенты       |
 |        |  очередь Submission --> очередь Delivery           |
 |        v                                                    |
 | Mailbox Transport (Submission / Delivery)                   |
 |        |                                                    |
 |        v                                                    |
 | Store: база .edb + транзакционные логи .log                 |
 +-------------------------------------------------------------+
 Исходящее: Store -> Submission -> Transport -> Send connector
            -> DNS/MX получателя  ИЛИ  smarthost -> интернет
```

Порты, которые надо помнить: **25** — SMTP между серверами, **587** — клиентская отправка, **443** — всё клиентское (OWA/ECP/MAPI/EWS), **143/993** — IMAP, **110/995** — POP. В Exchange 2016/2019 службы IMAP4 и POP3 установлены, но **остановлены** по умолчанию — включать их надо осознанно.

**Коннекторы.** *Receive connector* — «входное окно»: кто и с каких IP может подключиться, какая аутентификация, какой размер письма. *Send connector* — «выходная дверь»: куда отправлять (по MX-записям DNS или через smarthost), для каких доменов, с какой аутентификацией. Если внутренняя почта ходит, а внешняя нет — почти всегда смотреть Send connector.

**Базы данных.** Один файл `.edb` (Extensible Storage Engine, ESE) — это и есть ящики, папки, письма. Все изменения сначала пишутся в **транзакционные логи** (`.log`), потом «мягко» попадают в `.edb`. Логи удаляются (truncate) только после успешного **VSS-бэкапа** базы. Отсюда два следствия, которые надо выучить наизусть: (1) нет бэкапа — логи растут до заполнения диска; (2) удалить логи руками = потерять почту и убить базу. Есть ещё файл `.chk` (checkpoint — докуда логи уже применены). В DAG (Database Availability Group) база реплицируется на несколько серверов, и у каждой копии свой статус.

**Очереди.** Письмо, которое нельзя отдать дальше, встаёт в очередь и повторяет попытки. Типы очередей: **Submission** (только что принято, ждёт обработки), **Delivery** (по одной на каждый целевой домен/сервер), **Poison** (письмо, которое роняет транспорт — изолируется, чтобы не циклиться), **Shadow** (копии для shadow redundancy — гарантии, что письмо не потеряется при падении сервера), **Unreachable** (не найден маршрут). Состояния: **Active** (работает), **Ready** (ждёт), **Retry** (была ошибка, ждёт повтора), **Suspended** (приостановлена вручную).

**NDR** — это отчёт о недоставке, письмо от `postmaster`. Ключ — расширенный код статуса по RFC 3463: `5.x.x` — постоянная ошибка (повторять бессмысленно), `4.x.x` — временная (Retry). Типовые: `550 5.1.1`/`5.1.10` — получатель не найден, `550 5.7.1` — нет прав на релей, `550 5.2.1` — ящик отключён, `452 4.2.2` — ящик переполнен, `451 4.4.0` — проблема с DNS/маршрутом.

## 3. Аналогия из жизни

Exchange — это большой сортировочный центр курьерской компании. **Receive connector** — приёмное окно: кого пускать, с какими документами. **Transport service** — цех сортировки: тут решают, в какой город поедет посылка (маршрутизация) и выбрасывают запрещённые вложения (транспортные правила). **Send connector** — ворота на выезд: либо сами везём по адресу (DNS/MX), либо отдаём партнёру-перевозчику (smarthost). **База `.edb`** — стеллажи с ячейками, где лежат посылки. **Транзакционные логи** — журнал приёмки: кладовщик сначала пишет в журнал «принял посылку №123», потом несёт её на стеллаж; если журнал сжечь, никто не докажет, что посылка вообще была. **Очередь** — поддон у ворот, на котором копятся посылки, которые некуда везти; на каждой наклейка с последней ошибкой. **NDR** — извещение «не вручено» с причиной.

Аналогия перестаёт работать в одном месте: посылку на поддоне нельзя «переотправить бесплатно», а Exchange хранит несколько копий письма (shadow redundancy) и умеет переигрывать логи — то есть у него есть память о том, чего курьерская служба не помнит.

## 4. Практика и команды

Всё ниже — в **Exchange Management Shell (EMS)** на EX01 (или с рабочей станции с установленными инструментами). Запускать от учётки с правами Organization Management.

**1. Кто вообще жив и что установлено**

```powershell
Get-ExchangeServer | Format-Table Name,Edition,AdminDisplayVersion,ServerRole
Get-ServerHealth -Identity EX01 | Where-Object {$_.AlertValue -ne "Healthy"} | Format-Table Name,AlertValue
Test-ServiceHealth -Server EX01
```
`Get-ExchangeServer` показывает версию и роль; `Get-ServerHealth` — состояние «щупов» (мониторов) сервера, фильтр оставляет только не-Healthy; `Test-ServiceHealth` проверяет, что все обязательные службы и пулы приложений запущены. Начинать разбор «сервер вообще не работает» надо отсюда, а не с перезагрузки.

**2. Состояние баз данных**

```powershell
Get-MailboxDatabase -Status | Format-List Name,Server,EdbFilePath,LogFolderPath,Mounted,DatabaseSize,AvailableNewMailboxSpace
```
Смотрим: `Mounted = True` (иначе ящики недоступны), где физически `.edb` и логи, сколько места осталось. `AvailableNewMailboxSpace` — «мусор» внутри базы, освобождается только офлайн-дефрагментацией, а не удалением писем.

**3. Состояние копий в DAG**

```powershell
Get-MailboxDatabaseCopyStatus -Identity DB01 | Format-Table Name,Status,CopyQueueLength,ReplayQueueLength
```
`Status = Mounted` у активной копии, `Healthy` у пассивной. Растущие `CopyQueueLength`/`ReplayQueueLength` — репликация отстаёт: копия «не догоняет», при переключении потеряются письма.

**4. Очереди: сводка по всей организации**

```powershell
Get-QueueDigest -GroupBy DeliveryType -DetailsLevel Normal
Get-QueueDigest -Server EX01 -Filter "Status -eq 'Retry'" -DetailsLevel Verbose
```
`-GroupBy` группирует (`DeliveryType`, `NextHopDomain`, `Server`, `Queue`), `-Filter` — серверный фильтр. Быстро отвечает на вопрос «где копится»: если одна очередь на 5000 писем, а остальные пустые — проблема в одном направлении.

**5. Очереди и письма внутри них**

```powershell
Get-Queue -Server EX01 | Format-Table Identity,DeliveryType,Status,MessageCount,NextHopDomain,LastError
Get-Message -Queue "EX01\contoso.com" | Format-List Identity,FromAddress,Subject,Status,RetryCount,LastError,DateReceived
```
`Identity` очереди = `<сервер>\<имя>`. `LastError` — самое важное поле: там текст ошибки от удалённой стороны. `RetryCount` растёт — значит попытки идут и падают.

**6. Управление очередями**

```powershell
Retry-Queue -Identity "EX01\contoso.com" -Resubmit $true
Suspend-Queue -Identity "EX01\contoso.com"
Resume-Queue -Identity "EX01\contoso.com"
Remove-Message -Identity "EX01\contoso.com\1234" -WithNDR $false
```
`Retry-Queue -Resubmit $true` немедленно повторяет попытку. `Remove-Message -WithNDR $false` удаляет письмо **без** уведомления отправителю — так делают с мусором и петлями, но не с письмами живых пользователей. В GUI: **EAC → Mail flow → Queues** (или Exchange Toolbox → Queue Viewer).

**7. Журнал отслеживания сообщений** — главный инструмент «куда делось письмо»

```powershell
Get-MessageTrackingLog -Server EX01 -Sender ivanov@corp.vektor.lab `
  -Start "01.03.2026 08:00" -End "01.03.2026 12:00" `
  | Format-Table Timestamp,EventId,Sender,Recipients,MessageSubject,RecipientStatus -AutoSize
```
`EventId`: `RECEIVE` (принято), `SUBMIT` (клиент отправил), `SEND` (уехало наружу), `DELIVER` (положено в ящик), `FAIL` (не доставлено), `DEFER` (отложено), `EXPAND` (развёрнута группа), `TRANSFER` (передано другому серверу), `REDIRECT`. `RecipientStatus` содержит расширенный код ошибки. Поиск по `-MessageSubject` и `-MessageId` тоже работает.

**8. Коннекторы**

```powershell
Get-ReceiveConnector -Server EX01 | Format-List Name,Enabled,TransportRole,Bindings,RemoteIPRanges,PermissionGroups,AuthMechanism
Get-SendConnector | Format-List Name,Enabled,AddressSpaces,SmartHosts,DNSRoutingEnabled,SourceTransportServers,MaxMessageSize
```
`PermissionGroups` у receive-коннектора — это и есть ответ на «кому можно релеить». `DNSRoutingEnabled = True` — отправка по MX; `False` + `SmartHosts` — через шлюз. `AddressSpaces` — для каких доменов действует коннектор.

**9. Проверка потока и SMTP-связности**

```powershell
Test-Mailflow -Identity EX01 -TargetMailbox ivanov@corp.vektor.lab -TargetEmailAddress ivanov@corp.vektor.lab
Test-SmtpConnectivity -Identity EX01
```
`Test-Mailflow` реально отправляет тестовое письмо и возвращает `TestMailflowResult` — самый честный тест «почта ходит». `Test-SmtpConnectivity` проверяет, что каждый receive-коннектор принимает соединение.

**10. Протокольные логи коннекторов**

```powershell
Set-ReceiveConnector -Identity "EX01\Default EX01" -ProtocolLoggingLevel Verbose
Set-SendConnector -Identity "Lab-Out" -ProtocolLoggingLevel Verbose
Get-TransportService -Identity EX01 | Format-List ReceiveProtocolLogPath,SendProtocolLogPath
```
Это «сырой» SMTP-диалог: видно HELO, MAIL FROM, RCPT TO и код ответа на каждом шаге. Незаменимо, когда сервер говорит «5.7.1 denied», а непонятно кто именно. В бою `Verbose` включают на время, иначе логи съедят диск.

**11. Здоровье базы и ящиков**

```powershell
Get-MailboxStatistics -Identity ivanov | Format-List DisplayName,TotalItemSize,ItemCount,LastLogonTime,Database
Get-MoveRequestStatistics -Identity ivanov | Format-List Status,PercentComplete,BytesTransferred,BadItemsEncountered
```
Переполненный ящик — `TotalItemSize` у лимита, и тогда отправка будет падать с `4.2.2`. Перемещение ящика между базами (`New-MoveRequest`) — штатный способ «подвинуть» почту без простоя.

**12. Проверка с Linux (UBU01)**

```bash
nc -vz 10.10.10.26 25
swaks --to ivanov@corp.vektor.lab --from petrova@corp.vektor.lab --server 10.10.10.26
```
`nc -vz` проверяет, что порт открыт и отвечает. `swaks` (Perl-скрипт для теста SMTP) проходит полный SMTP-диалог вручную — видно точный код ответа.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 готов: DC01 (AD DS, DNS, DHCP), FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01, домен `corp.vektor.lab`, учётки `ivanov`, `petrova`, `sidorov`, `adm.student`.

**Важно:** в таблице стенда почтового сервера нет, поэтому эта глава **явно добавляет новую ВМ `EX01`** — иначе тему не на чем делать, а ставить Exchange на DC01 нельзя категорически. Параметры: Windows Server 2022 Evaluation, **10.10.10.26**, статический IP, шлюз 10.10.10.2, DNS 10.10.10.10, 4 vCPU, 10–12 ГБ RAM (минимально допустимые 8 ГБ — ниже сервер будет «жевать»), диск 100 ГБ. Если хосту не хватает RAM — выключи MON01 и SRV1C01 на время лабы.

Дистрибутив: Exchange Server 2019 **Evaluation** (180 дней) или Exchange Server Subscription Edition [ПРОВЕРИТЬ актуальность триала в 2026]. Отдельно зафиксируй в голове: поддержка Exchange 2016/2019 закончилась 14.10.2025, для новой боевой установки в 2026 берут Exchange SE с подпиской [ПРОВЕРИТЬ сроки]. Из РФ скачать дистрибутив Microsoft может быть проблемой — для учёбы бери Evaluation, для боевой почты смотри урок 64 про российские решения.

Введи EX01 в домен (учёткой `VEKTOR\adm.student`) до установки Exchange.

### Снапшот

В VMware Workstation выключи/приостанови ВМ и сделай снапшот с именем **`before-l2-062`** на: **DC01**, **EX01** (пока чистый Windows), **WIN11-CL01**, **UBU01**. DC01 — обязательно: установка Exchange меняет схему AD.

### Шаги

1. **EX01, PowerShell (от админа).** Проверь имя, IP, членство в домене: `Get-ComputerInfo | Select CsName,CsDomain` и `ipconfig /all`. Ожидаемо: `EX01`, домен `corp.vektor.lab`, DNS = 10.10.10.10.

2. **EX01.** Поставь предварительные компоненты: `Install-WindowsFeature NET-Framework-45-Features,RPC-over-HTTP-proxy,RSAT-Clustering,RSAT-Clustering-CmdInterface,Web-Mgmt-Console,WAS-Process-Model,RSAT-Web-Server,Web-Basic-Auth,Web-Client-Auth,Web-Digest-Auth,Web-Dir-Browsing,Web-Http-Errors,Web-Http-Logging,Web-Http-Redirect,Web-Http-Tracing,Web-ISAPI-Ext,Web-ISAPI-Filter,Web-Lgport,Web-Metabase,Web-Mgmt-Service,Web-Net-Ext45,Web-Request-Monitor,Web-Server,Web-Stat-Compression,Web-Static-Content,Web-Windows-Auth,Web-WMI,Windows-Identity-Foundation,RSAT-ADDS`. Ожидаемо: `Success: True`. Плюс отдельно установи .NET Framework 4.8 и UCMA [ПРОВЕРИТЬ актуальный список в документе Microsoft «Exchange Server prerequisites»].

3. **DC01 (одна учётка, одна команда).** Из папки дистрибутива разверни схему и подготовь AD:
```powershell
.\Setup.exe /PrepareSchema /IAcceptExchangeServerLicenseTerms_DiagnosticDataON
.\Setup.exe /PrepareAD /OrganizationName:VEKTOR /IAcceptExchangeServerLicenseTerms_DiagnosticDataON
```
Ожидаемо: «Setup completed successfully» дважды. Это необратимо для AD — поэтому снапшот DC01 и был нужен.

4. **EX01.** Установи роль:
```powershell
.\Setup.exe /Mode:Install /Role:Mailbox /OrganizationName:VEKTOR /IAcceptExchangeServerLicenseTerms_DiagnosticDataON
```
Ожидаемо: 15–20 км транзакций установки (реально 30–60 минут) и сообщение об успехе. Затем `https://10.10.10.26/ecp` открывается с учёткой `adm.student`.

5. **EX01, EMS.** Создай ящики пользователям стенда:
```powershell
Enable-Mailbox -Identity corp\ivanov -Database DB01
Enable-Mailbox -Identity corp\petrova -Database DB01
Get-Mailbox | Format-Table Name,Database,PrimarySmtpAddress
```
Ожидаемо: у ivanov и petrova есть SMTP-адреса вида `ivanov@corp.vektor.lab`. Если `DB01` не существует — посмотри `Get-MailboxDatabase` и подставь реальное имя.

6. **WIN11-CL01, браузер.** Зайди в `https://10.10.10.26/owa` как `VEKTOR\ivanov`, отправь письмо на `petrova@corp.vektor.lab` с темой `test-l2-062`. Ожидаемо: письмо появилось в «Отправленных», у petrova (OWA в другом окне) — во «Входящих».

7. **EX01, EMS.** Найди это письмо в журнале:
```powershell
Get-MessageTrackingLog -Server EX01 -MessageSubject "test-l2-062" | Format-Table Timestamp,EventId,Sender,Recipients
```
Ожидаемо: цепочка `SUBMIT → RECEIVE → DELIVER` (внутри одного сервера — короткая). Запомни этот «след»: по нему ты всегда докажешь, что письмо было и куда делось.

8. **EX01, EMS.** Посмотри базу и логи:
```powershell
Get-MailboxDatabase -Status | Format-List Name,EdbFilePath,LogFolderPath,Mounted,DatabaseSize
(Get-ChildItem (Get-MailboxDatabase DB01).LogFolderPath -Filter *.log | Measure-Object).Count
```
Ожидаемо: `Mounted = True`, размер базы в мегабайтах, логов десятки-сотни — они **не удаляются**, потому что бэкапа не было.

9. **EX01, EMS.** Сделай «плохой» Send connector для внешних адресов:
```powershell
New-SendConnector -Name "Lab-Out" -AddressSpaces "smtp:*" -DNSRoutingEnabled $false `
  -SmartHosts 10.10.20.99 -SourceTransportServers EX01
```
Смартхоста 10.10.20.99 не существует. Это учебная поломка: в реальной среде коннекторы на «*» создают только осознанно и с проверенным шлюзом.

10. **WIN11-CL01.** Отправь письмо на несуществующий внешний адрес `test@example.invalid`. Ожидаемо: через несколько минут вернётся NDR с `451 4.4.0`/`Connection refused` [ПРОВЕРИТЬ точный текст для твоей версии].

11. **EX01, EMS.** Найди очередь и причину:
```powershell
Get-Queue -Server EX01 | Format-Table Identity,Status,MessageCount,NextHopDomain,LastError
Get-Message -Queue "EX01\example.invalid" | Format-List Subject,Status,RetryCount,LastError
```
Ожидаемо: очередь `example.invalid` в статусе `Retry`, `MessageCount = 1`, в `LastError` — отказ соединения с 10.10.20.99.

12. **EX01, EMS.** Почини и проверь:
```powershell
Set-SendConnector -Identity "Lab-Out" -SmartHosts 10.10.10.30   # UBU01: порт 25 закрыт -> тоже ошибка, см. шаг 13
Retry-Queue -Identity "EX01\example.invalid" -Resubmit $true
Get-Queue -Server EX01 | Format-Table Identity,Status,MessageCount
```
Так ты увидишь разницу между «хост недоступен» и «хост доступен, но не принимает». После проверки убери коннектор: `Remove-SendConnector -Identity "Lab-Out"` — чтобы не ломать следующие главы.

13. **UBU01.** Проверь SMTP-порт снаружи: `nc -vz 10.10.10.26 25` и `swaks --to ivanov@corp.vektor.lab --from petrova@corp.vektor.lab --server 10.10.10.26`. Ожидаемо: порт открыт, swaks получает `250 2.1.5 Recipient OK`. Если UBU01 не пускает — смотри `RemoteIPRanges` у receive-коннектора.

14. **EX01, EMS.** Включи протокольный лог на Default-коннекторе, повтори шаг 13, найди файл в `ReceiveProtocolLogPath` и открой его: `Set-ReceiveConnector "EX01\Default EX01" -ProtocolLoggingLevel Verbose`. Ожидаемо: в логе видны строки `>> "EHLO"`, `<< "250"`, `>> "RCPT TO:..."`. Выключи обратно: `-ProtocolLoggingLevel None`.

15. **stand-log.md.** Запиши изменения (см. ниже раздел «Что изменилось»).

### Проверка

- [ ] `Get-ExchangeServer` показывает EX01, версию и роль Mailbox.
- [ ] `Get-Mailbox` содержит ящики ivanov и petrova с адресами `@corp.vektor.lab`.
- [ ] `Get-MailboxDatabase -Status` → `Mounted: True`.
- [ ] Письмо `test-l2-062` найдено в `Get-MessageTrackingLog` с событиями `RECEIVE` и `DELIVER`.
- [ ] Ты видел очередь в статусе `Retry` и прочитал `LastError`.
- [ ] `Retry-Queue` изменил состояние очереди, и ты понимаешь, почему после этого письмо всё равно не ушло (UBU01 — не MTA).
- [ ] `nc`/`swaks` с UBU01 получают ответ 250 от EX01.
- [ ] Протокольный лог открыт, прочитан и выключен обратно.
- [ ] Send connector `Lab-Out` удалён.
- [ ] В `stand-log.md` появилась запись про EX01.

### Сломай и почини

> **Пользователь:** ivanov · **ВМ:** WIN11-CL01 · **Что написал:** «С утра не уходят письма в бухгалтерию и внешним контрагентам. В OWA они в "Исходящих", статус не меняется. Одно вернулось: "451 4.4.0, Temporary server error". Мне до обеда отчёт сдавать!» · **Срочность:** высокая.

**Как воспроизвести поломку.** На EX01 в EMS:
```powershell
Set-SendConnector -Identity "Lab-Internal" -SmartHosts 10.10.20.99 -DNSRoutingEnabled $false
```
(если коннектора нет — создай такой же, как в шаге 9). Через 2–3 минуты очередь на внутренний домен уйдёт в `Retry`.

**Диагностика по шагам, в этом порядке:**

1. **Клиент или сервер?** Просим ivanov обновить OWA. Если OWA открывается — Client Access жив, проблема на транспорте. `Test-ServiceHealth -Server EX01` — все службы `True`.
2. **Внутренняя почта ходит?** Отправляем с petrova письмо ivanov. Не дошло → значит, проблема не в конкретном получателе, а в маршруте.
3. **Смотрим очереди:** `Get-Queue -Server EX01 | Format-Table Identity,Status,MessageCount,LastError`. Видим очередь `corp.vektor.lab` в `Retry`, `MessageCount` растёт, `LastError` — отказ соединения с 10.10.20.99.
4. **Смотрим коннекторы:** `Get-SendConnector | Format-List Name,AddressSpaces,SmartHosts,DNSRoutingEnabled`. Находим `SmartHosts = 10.10.20.99`, `DNSRoutingEnabled = False`. Причина найдена: письма шлют на несуществующий узел.
5. **Проверяем, что это не следствие:** `Get-MessageTrackingLog -Server EX01 -EventId FAIL -Start (Get-Date).AddHours(-2)` — видим `RecipientStatus` с 4.4.x, то есть транзиентные ошибки, а не 5.x.x.
6. **Лечим причину, а не очередь:**
```powershell
Set-SendConnector -Identity "Lab-Internal" -DNSRoutingEnabled $true -SmartHosts $null
Retry-Queue -Identity "EX01\corp.vektor.lab" -Resubmit $true
Get-Queue -Server EX01 | Format-Table Identity,Status,MessageCount
```
Ожидаемо: очередь пустеет за секунды, письма доставляются.
7. **Подтверждаем:** `Get-MessageTrackingLog -Sender ivanov@corp.vektor.lab -EventId DELIVER -Start (Get-Date).AddHours(-1)` — есть `DELIVER`.
8. **Проверяем размер ящика** на всякий случай: `Get-MailboxStatistics ivanov | fl TotalItemSize` — не у лимита, иначе следующий тикет будет уже про `4.2.2`.

**Что написать пользователю:**
> Иван, добрый день. Причина найдена: письма уходили через неверно настроенный маршрут на сервере почты, из-за этого они стояли в очереди и повторяли попытки. Маршрут исправлен, очередь разослана — все письма, включая отчёт, уже доставлены. Проверьте, пожалуйста, «Исходящие» и подтвердите. Если хоть одно письмо не дошло — пришлите тему и время, найду по журналу.

**Как закрыть заявку:** категория «Почта / маршрутизация», причина «некорректный Send connector (неверный smarthost)», решение «исправлена маршрутизация, очередь переотправлена», в заметках — идентификатор очереди и время. Статус «Решено», ожидание подтверждения от пользователя.

### Что изменилось на стенде

- Добавлена ВМ **EX01** (Windows Server 2022 Evaluation, Exchange Server 2019/SE, роль Mailbox), **10.10.10.26**, в домене `corp.vektor.lab`.
- В AD развёрнута схема Exchange, создана организация **VEKTOR** (это изменение AD — помнить при откате DC01).
- Создана база **DB01**, включены ящики `ivanov` и `petrova` с адресами `@corp.vektor.lab`.
- Появились receive-коннекторы по умолчанию (`Default EX01`, `Client EX01`, `Proxy`-коннектор), протокольный лог на Default включался и выключен.
- Send connector `Lab-Out` создавался и **удалён**; если ты оставил свой — запиши его имя и параметры.
- Включён доступ к ECP/OWA по `https://10.10.10.26/ecp` и `/owa`.
- Для следующих глав: почта в домене теперь идёт через EX01, `petrova` и `ivanov` — реальные получатели.

### Откат

1. Выключи EX01.
2. VMware Workstation → снапшот **`before-l2-062`** → Revert на **EX01** и на **DC01** (схема AD откатится вместе с DC01 — это единственный чистый способ).
3. Если откатываешь только EX01, но не DC01, помни: схема Exchange в AD останется, и повторная установка пройдёт иначе (Setup скажет, что организация уже есть).
4. После отката верни снапшоты остальных ВМ (WIN11-CL01, UBU01) и допиши в `stand-log.md` строку «откат к before-l2-062, причина: …».

## 6. Ошибки новичков и безопасность

1. **«Очередь большая — удалю письма».** → Удаляешь письма живых людей и доказательства проблемы. Правильно: сначала `LastError` и коннектор, потом лечение причины, удаление — только для мусора и петель, и лучше с NDR.
2. **Удалить `.log` в папке базы, чтобы освободить диск.** → База уходит в состояние Dirty Shutdown, часть почты теряется безвозвратно. Правильно: настроить VSS-бэкап (он делает truncate логов) или включить circular logging осознанно и на время.
3. **`eseutil /p` при любом испуге.** → `/p` — «хирургия»: база ремонтируется, но могут остаться осиротевшие письма, и в DAG её потом не восстановить копированием. Правильно: сначала `/mh` (посмотреть состояние), затем восстановление из бэкапа или reseed копии.
4. **Перезапуск `MSExchangeTransport` «на всякий случай» первым действием.** → Теряются текущие соединения и часть контекста, а причина не найдена. Правильно: сначала `Get-Queue`, `Get-ServerHealth`, журнал отслеживания.
5. **Правка коннектора «на проде» без записи исходных значений.** → Не помнишь, как было. Правильно: `Get-SendConnector | fl * > connector-before.txt` (или `Export-Clixml`) перед изменением.
6. **Расширение `RemoteIPRanges` до `0.0.0.0-255.255.255.255` «чтобы заработало».** → Открытый релей: через твой сервер любой желающий рассылает спам, домен попадает в чёрные списки. Правильно: только нужные подсети и `PermissionGroups` по минимуму.
7. **Игнорирование того, что Exchange 2019 больше не поддерживается.** → Нет обновлений безопасности, а почта — публичный сервис. Правильно: планировать переход на Exchange SE или российское решение (урок 64).
8. **Одна база на всех и без бэкапа.** → Потеря одного файла = потеря почты всей компании. Правильно: бэкап с VSS, проверка восстановления, при возможности — DAG с копией на другом сервере.

**Безопасность.** Никогда не открывай порт 25 сервера Exchange в интернет напрямую без фильтрации — между интернетом и Exchange ставят Edge Transport или почтовый шлюз. Не давай пользователям права Organization Management «чтобы видели ящики» — для этого есть mailbox permissions и аудит. Включай mailbox audit logging на ящики руководителей и бухгалтерии. Не отправляй с учебного стенда письма на реальные внешние адреса: спам-фильтры получателей и репутация твоего IP в лаборатории тебе не нужны. И помни, что единый пароль стенда `Lab#2026-vektor` — только для лаборатории: в боевой среде это мгновенный компромисс всей почты.

## 7. Вопросы на собеседовании

**1. Какие роли остались в Exchange 2019 и зачем нужна Edge Transport?**
*Ответ:* две роли — Mailbox (в ней CAS-сервисы, Front End Transport, Transport/Hub, Mailbox Transport и Store) и Edge Transport для периметра: антиспам, антивирус, фильтрация SMTP до того, как трафик попадёт во внутреннюю сеть. Edge ставится в DMZ, в домен не вводится, синхронизируется с внутренними серверами через Edge Subscription. В Exchange 2019 UM удалена, IMAP/POP есть, но выключены.
*Что хочет услышать интервьюер:* что ты знаешь консолидацию ролей с 2013 и не рассказываешь про пять ролей из 2010.
*Красный флаг:* «роли Client Access и Hub Transport» как отдельные серверы в 2019.

**2. Что происходит с письмом от нажатия «Отправить» до момента, когда оно в ящике получателя?**
*Ответ:* MUA отправляет по MAPI/HTTP (443) или SMTP (587) → Front End Transport принимает по receive-коннектору → передаёт в Transport service → там маршрутизация, транспортные правила, ставится в Submission-очередь → Mailbox Transport Submission забирает и пишет в Store через ESE → для получателя Mailbox Transport Delivery раскладывает письмо в его ящик, событие `DELIVER`. Наружу — через Send connector по MX или smarthost, событие `SEND`.
*Что хочет услышать:* понимание, что транспорт и Store — разные подсистемы, и что сбой на любом шаге даёт разную симптоматику.
*Красный флаг:* «письмо сразу попадает в базу получателя».

**3. Очередь растёт. Твои действия?**
*Ответ:* `Get-QueueDigest` — где копится; `Get-Queue` — статус, `MessageCount`, `NextHopDomain`, `LastError`; `Get-Message` — конкретные письма, `RetryCount`, `Sender`. Дальше по `LastError`: DNS/маршрут → коннектор; отказ авторизации → права/аутентификация; 4.2.2 → ящик переполнен; 5.7.1 → релей. Проверяю, что коннектор не менялся, смотрю протокольный лог. Только потом `Retry-Queue -Resubmit $true`. Удаление писем — последнее средство.
*Что хочет услышать:* порядок «сводка → очередь → письмо → причина → действие» и отказ от удаления как первого шага.
*Красный флаг:* «перезапущу транспорт».

**4. Пользователь говорит: «письмо не дошло». Как докажешь, что оно было/не было?**
*Ответ:* `Get-MessageTrackingLog` по отправителю, получателю, теме или `MessageId` за нужный интервал; смотрю цепочку `SUBMIT → RECEIVE → SEND/DELIVER` и `RecipientStatus`. Если есть `FAIL`/`DEFER` — читаю расширенный код (5.x.x — постоянно, 4.x.x — временно). Дополнительно смотрю NDR у отправителя и, если письмо ушло наружу, протокольный лог Send connector.
*Что хочет услышать:* что журнал отслеживания — основной инструмент, а не «посмотрю в Outlook».
*Красный флаг:* «в журнале ничего не нашёл, значит Exchange виноват» (журнал хранится ограниченное время, и об этом надо знать).

**5. Что такое транзакционные логи и почему их нельзя удалять?**
*Ответ:* это write-ahead log ESE: все изменения сначала фиксируются в логах, потом применяются к `.edb`. Они обеспечивают целостность и восстановление после сбоя, а также репликацию в DAG. Truncate происходит только после успешного VSS-бэкапа (или при circular logging). Ручное удаление = потеря изменений и почти гарантированное повреждение базы, Dirty Shutdown.
*Что хочет услышать:* связку «логи — бэкап — место на диске» и понимание, что лечится это бэкапом, а не `del *.log`.
*Красный флаг:* «логи можно удалить, Exchange их пересоздаст».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Exchange Server documentation»: статьи «Exchange Server roles», «Mail flow and routing», «Transport pipelines», «Message tracking log», «Queue Viewer», «Exchange Server prerequisites», «Exchange Server supportability matrix». Поиск: `Exchange Server 2019 mail flow site:learn.microsoft.com`.
- **learn.microsoft.com** — «Exchange Server Deployment Assistant» (пошаговый планировщик установки, полезен для проверки prereq). Поиск: `Exchange Deployment Assistant`.
- **learn.microsoft.com** — «Database Availability Group», «Transaction logs and checkpoint file», «Backup, restore, and disaster recovery». Поиск: `Exchange transaction logs truncation site:learn.microsoft.com`.
- **rfc-editor.org** — RFC 3463 «Enhanced Mail System Status Codes» (расшифровка 5.x.x/4.x.x) и RFC 5321 «Simple Mail Transfer Protocol». Поиск: `RFC 3463 enhanced status codes`.
- **learn.microsoft.com** — «Windows Server Backup and Exchange» / «Exchange Server 2019 preferred architecture» — перед настройкой бэкапа проверить актуальную поддерживаемую схему `[ПРОВЕРИТЬ]`.
- **learn.microsoft.com** — «Exchange Server Subscription Edition» — сроки поддержки и лицензирование `[ПРОВЕРИТЬ актуальные даты на 2026 год]`.
