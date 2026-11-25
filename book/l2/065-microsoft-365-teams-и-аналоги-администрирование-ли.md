# Microsoft 365 / Teams и аналоги: администрирование, лицензии, доступ в РФ-реалиях

## 1. Введение (зачем это нужно в реальной работе)

Тикет вhelpdesk «Вектора»: **Пользователь:** ivanov (бухгалтерия, WIN11-CL01). **Что написал:** «Здравствуйте! В Teams не открывается канал "Бухгалтерия" — пишет "У вас нет доступа к этой команде". С телефона при этом всё видно. И ещё Outlook каждые 10 минут просит пароль. Работа стоит, отчётность за квартал». **Срочность:** высокая.

Дальше выясняется классика: у бухгалтера лицензия Business Basic без Exchange Online (почта отдельно на другом сервисе), канал — приватный, доступ через группу, а на ноутбуке слетело состояние устройства в Entra ID. Три разные причины в одном тикете.

Microsoft 365 — это не «почта» и не «Teams», это подписка на десяток облачных сервисов поверх одной системы идентификации. Инженер L2 в Москве 2026 года почти гарантированно встретит M365 хотя бы в двух видах: как остаток у компании, которая ещё не переехала, и как «то, что надо вывезти» при миграции на Яндекс 360 / VK WorkSpace / МойОфис (это урок 64). После главы ты должен уметь: разобрать лицензионный состав тенанта, объяснить разницу SKU, найти пользователя и его права, проверить состояние устройства и сети до облака, прочитать логи клиента, отличить «сломалось у нас» от «сломалось у вендора» и построить план миграции.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Тенант** — изолированный экземпляр облака Microsoft, привязанный к организации. Внутри тенанта живут пользователи, группы, домены, лицензии и настройки сервисов. У тенанта всегда есть технический домен вида `vektor.onmicrosoft.com`, а сверху навешиваются публичные домены. Важно: локальный `corp.vektor.lab` для облака **не годится** — `.lab` не публичная зона, письма туда не придут и проверку владения доменом ты не пройдёшь. В учебном тенанте адреса будут вида `ivanov@vektor.onmicrosoft.com` или на домене, который ты реально контролируешь.

**Ключевая идея: identity-центричность.** Любой сервис M365 сначала спрашивает Entra ID (бывший Azure AD) «кто это и что ему можно», и только потом отдаёт данные. Отсюда все «не могу войти» лечатся в первую очередь на уровне идентичности.

```text
                     ТЕНАНТ (подписка организации)
   Клиенты (MUA)     ┌──────────────────────────────────────────────┐
 Outlook, Teams,     │  Entra ID — вход, MFA, Conditional Access     │
 браузер, мобильное  │      │                                       │
      │              │      ├── Exchange Online   (почта, календарь) │
      │  HTTPS 443   │      ├── Teams            (чаты, звонки)     │
      └──────────────┼──►   ├── SharePoint Online (файлы каналов)  │
        UDP 3478-3481│      └── OneDrive         (личные файлы)     │
        (медиа Teams)│                                              │
                     └──────────────────────────────────────────────┘
                                    ▲
                                    │ Entra Connect (синхронизация)
   Офис: DC01 (AD DS, corp.vektor.lab) ─┘
   FS01, WIN11-CL01, WIN10-CL02, UBU01, ASTRA01
```

**Слои и протоколы.** Всё прикладного уровня (L7 по OSI): HTTPS 443 для админки, почты (MAPI/HTTPS, IMAP 993), файлов; для медиа Teams — UDP 3478–3481 (STUN/TURN) `[ПРОВЕРИТЬ актуальный диапазон в документации Microsoft]`. Типовые имена, которые надо уметь пинговать и резолвить: `login.microsoftonline.com`, `outlook.office365.com`, `teams.microsoft.com`, `*.sharepoint.com`.

**Админ-центры** разделены по сервисам, и это первая ловушка новичка:
- `admin.microsoft.com` — пользователи, лицензии, биллинг, состояние сервисов (Health → Service health);
- `entra.microsoft.com` — роли, MFA, Conditional Access, устройства;
- `admin.teams.microsoft.com` (TAC) — политики собраний, внешний доступ, гостевой доступ;
- `admin.exchange.microsoft.com` — почтовые ящики, транспорт (детали в уроке 62);
- `purview.microsoft.com` — аудит, удержание, eDiscovery.

**Лицензии (SKU).** Лицензия — это набор «включённых сервисов» на пользователя. Ориентиры (цены меняются, проверяй у партнёра): Business Basic — веб- и мобильные приложения Office, Exchange, Teams; Business Standard — плюс десктопный Office; Business Premium — плюс Intune и расширенная защита; E3 — корпоративный набор с большими лимитами архива; E5 — плюс расширенный аудит, Defender for Office 365 P2 и Teams Phone-опции `[ПРОВЕРИТЬ актуальные цены и состав SKU]`. Отдельно стоят F1/F3 для «сменников» без рабочего ПК, Microsoft Teams Essentials и Teams Phone. Назначать лицензии массово правильно **через группы** (group-based licensing), но для этого нужна Entra ID P1 (входит в E3/Business Premium) `[ПРОВЕРИТЬ]`.

**Роли.** Глобальный администратор — «бог», его не должно быть много и он не должен быть повседневной учёткой. Рабочие роли: User Administrator, Helpdesk Administrator (сброс паролей), License Administrator, Teams Administrator, Exchange Administrator, Reports Reader.

**Гибридная идентичность.** Entra Connect синхронизирует объекты из AD DS в Entra ID. Три способа аутентификации: Password Hash Sync (хэш пароля в облаке), Pass-Through Auth (проверка пароля на локальном агенте), ADFS (федерация, самая тяжёлая). Синхронизируются не все подряд — обычно фильтруют по OU (у нас, например, только `Company` и `Серверы`).

**РФ-реалии 2026.** Microsoft приостановила новые продажи в РФ в марте 2022 года; оплата и продление существующих подписок из России сильно затруднены или невозможны, часть организаций потеряла доступ `[ПРОВЕРИТЬ актуальный статус на момент чтения — ситуация меняется]`. Практические следствия: (1) планировать миграцию, а не «переждать»; (2) учитывать 152-ФЗ (персональные данные россиян должны храниться в РФ) и требования по импортозамещению/реестру российского ПО; (3) для госсектора и поднадзорных организаций использование иностранных мессенджеров для рабочих коммуникаций ограничено законом `[ПРОВЕРИТЬ формулировки 149-ФЗ]`. Легальный путь — учебный/developer-тенант или партнёрский trial для практики, а в проде — российский аналог.

| Что в M365 | Российский аналог |
|---|---|
| Exchange Online | Яндекс 360, VK WorkSpace, CommuniGate, МойОфис |
| Teams | VK Teams (Squadus), TrueConf, IVA MCU, Яндекс 360 |
| SharePoint/OneDrive | Яндекс 360 Диск, VK WorkSpace, МойОфис |
| Entra ID | домен AD DS + SSO российского вендора |

## 3. Аналогия из жизни

Microsoft 365 — это аренда офиса в большом бизнес-центре. Здание — тенант. Рабочее место в нём — лицензия: есть место со столом и розеткой (Business Basic), есть с личным шкафом и своим принтером (Business Standard), есть с личным охранником (Business Premium/E5). Служба охраны на входе — Entra ID: она проверяет пропуск и может запретить вход с чужого ноутбука (Conditional Access). Управляющий зданием — админ-центр: там ты арендуешь места, выдаёшь пропуска и читаешь объявления «сегодня лифт не работает» (Service health). А российский аналог — свой дом: дороже по времени, зато ключи у тебя и выселить не могут. Аналогия ломается в одном: из съёмного офиса мебель можно физически вынести, а данные из облака — только через экспорт, который надо готовить заранее.

## 4. Практика и команды

Все облачные команды выполняются в **PowerShell** под своей учётной записью с нужными правами. Модули ставятся на клиентскую ВМ, не на DC01.

**1. Подключение к Graph и инвентаризация лицензий.** Microsoft Graph PowerShell — современная замена MSOnline/AzureAD.

```powershell
Install-Module Microsoft.Graph -Scope CurrentUser
Connect-MgGraph -Scopes "Organization.Read.All","User.Read.All","Directory.Read.All"
Get-MgSubscribedSku | Select-Object SkuPartNumber, ConsumedUnits, @{n='Enabled';e={$_.PrepaidUnits.Enabled}}
```
`-Scopes` — какие разрешения просим; `ConsumedUnits` — сколько занято, `Enabled` — сколько куплено. Смотри на SKU, где `ConsumedUnits` близко к `Enabled`: это кандидаты на докупку или на отзыв лицензий у уволенных.

**2. Пользователь и его лицензии.**

```powershell
Get-MgUser -UserId ivanov@vektor.onmicrosoft.com | Format-List DisplayName,AccountEnabled,SignInActivity
Get-MgUserLicenseDetail -UserId ivanov@vektor.onmicrosoft.com | Select-Object SkuPartNumber
```
Пустой список лицензий — уже ответ на половину тикетов «у меня нет доступа».

**3. Назначение/снятие лицензии.**

```powershell
$sku = (Get-MgSubscribedSku | Where-Object SkuPartNumber -eq 'O365_BUSINESS_ESSENTIALS').SkuId
Set-MgUserLicense -UserId ivanov@vektor.onmicrosoft.com -AddLicenses @(@{SkuId=$sku}) -RemoveLicenses @()
```
Обязательно `-RemoveLicenses @()` — иначе командлет ругнётся. Массово так не делают: берут группу и групповое лицензирование.

**4. Exchange Online — только диагностика (глубина в уроке 62).**

```powershell
Connect-ExchangeOnline -UserPrincipalName adm.student@vektor.onmicrosoft.com
Get-Mailbox -Identity ivanov | Format-List DisplayName,RecipientTypeDetails,ProhibitSendQuota
Get-MailboxStatistics -Identity ivanov | Select-Object TotalItemSize,ItemCount
```
`RecipientTypeDetails` покажет `SharedMailbox`/`RoomMailbox` — сразу видно, настоящий это ящик или общий.

**5. Политики Teams.**

```powershell
Connect-MicrosoftTeams
Get-CsTeamsMeetingPolicy -Identity Global | Format-List AllowRecording,AllowTranscription,ScreenSharingMode
Grant-CsTeamsMeetingPolicy -Identity petrova@vektor.onmicrosoft.com -PolicyName "MeetingPolicy-HR"
```
Политики бывают `Global`, `Tag:*` (свои наборы) и применяются к пользователю через `Grant-*`. `Get-CsOnlineUser -Identity ivanov | Select TeamsUpgradeEffectiveMode` покажет режим сосуществования с Skype for Business `[ПРОВЕРИТЬ актуальность модуля MicrosoftTeams — Microsoft переводит часть командлетов на Graph]`.

**6. Состояние устройства (Windows).** Главный инструмент по «не пускает в Teams».

```cmd
dsregcmd /status
```
Смотри три поля: `AzureAdJoined : YES` — устройство в Entra ID; `DomainJoined : YES` — в локальном домене; `AzureAdPrt : YES` — есть primary refresh token для SSO. Комбинация «DomainJoined YES, AzureAdJoined NO, AzureAdPrt NO» = гибридного join нет, условный доступ устройство не узнает.

**7. Сеть до облака.**

```powershell
Test-NetConnection login.microsoftonline.com -Port 443
Test-NetConnection teams.microsoft.com -Port 443
Resolve-DnsName login.microsoftonline.com
```
`TcpTestSucceeded : True` — порт открыт. Если DNS резолвится в странный IP или сертификат подменён — почти наверняка DPI/прокси.

**8. То же с Linux (UBU01) — TLS и цепочка сертификатов.**

```bash
dig +short login.microsoftonline.com
curl -sI https://login.microsoftonline.com | head -n 3
openssl s_client -connect login.microsoftonline.com:443 -servername login.microsoftonline.com </dev/null 2>/dev/null | openssl x509 -noout -issuer -dates
ss -tunp | grep -E ':443|:3478'
```
`issuer` должен быть публичным УЦ; если это корпоративный прокси — значит трафик перехватывают, и клиент может ругаться на сертификат.

**9. Версия Office на рабочем месте.**

```powershell
Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Office\ClickToRun\Configuration" |
  Select-Object ProductReleaseIds, VersionToReport, CDNBaseUrl
```
`ProductReleaseIds` = что установлено (например, `O365BusinessRetail`), `CDNBaseUrl` = канал обновлений. Несовпадение с тем, что «куплено», — частая причина «функция есть у коллеги, но не у меня».

**10. Аудит действий в облаке.**

```powershell
Connect-ExchangeOnline
Search-UnifiedAuditLog -StartDate (Get-Date).AddDays(-2) -EndDate (Get-Date) -Operations "Add member to role.","Change user license." -ResultSize 50
```
Кто выдал лишние права и когда — вопрос, который задают после любого инцидента.

**11. Поиск писем по трейсу (когда «письмо не дошло»).**

```powershell
Get-MessageTrace -SenderAddress ivanov@vektor.onmicrosoft.com -StartDate (Get-Date).AddDays(-2) -EndDate (Get-Date) | Select Received,RecipientAddress,Status,Subject
```
`Status: Delivered/Failed/Quarantined` — сразу развилка диагностики `[ПРОВЕРИТЬ: в новых тенантах используется Get-MessageTraceV2]`.

**12. GUI-пути (L1 часто делает именно так).**
- Лицензия: `admin.microsoft.com` → Users → Active users → выбрать пользователя → Licenses and apps.
- Политика собраний: `admin.teams.microsoft.com` → Meetings → Meeting policies.
- MFA/доступ: `entra.microsoft.com` → Protection → Conditional Access / Authentication methods.
- Авария у вендора: `admin.microsoft.com` → Health → Service health (проверять **до** ковыряния в клиенте).
- Логи клиента Teams: `%appdata%\Microsoft\Teams\logs.txt` (классический) и каталог `%localappdata%\Packages\MSTeams_*\LocalCache\Microsoft\MSTeams\Logs` (новый) `[ПРОВЕРИТЬ точный путь для текущей версии]`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Тема облачная, а стенд изолирован, поэтому лабораторная честно делится на две части: **всё, что можно проверить на стенде без тенанта** (identity, сеть, лицензионная матрица, права) и **локальный аналог Teams** на UBU01, чтобы руками пощупать модель доступа «команда / приватный канал / гость». Если у тебя есть легальный доступ к учебному тенанту (developer-программа или партнёрский trial) — прогони команды из раздела 4 в параллель и запиши вывод в `stand-log.md`.

### Предусловия

Стенд L2 в полном состоянии: DC01 (AD DS, DNS, DHCP), FS01, WIN11-CL01 (ivanov), WIN10-CL02 (petrova), UBU01 (10.10.10.30, SSH), ASTRA01. Домен `corp.vektor.lab`, пользователи `ivanov`, `petrova`, `sidorov`, админ `adm.student`, группы `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT`. На UBU01 должен быть установлен Docker (если нет — тема 63/58 подскажет, как ставить пакеты; интернет через NAT VMnet8 работает).

### Снапшот

В VMware Workstation: выделить ВМ → правый клик → Snapshot → имя `before-l2-065`. Сделать на **UBU01, DC01, WIN11-CL01, WIN10-CL02**.

### Шаги

1. **WIN11-CL01.** `dsregcmd /status` в cmd. Ожидаемо: `DomainJoined : YES`, `AzureAdJoined : NO`, `AzureAdPrt : NO`. Запиши вывод — это «портрет» машины, которая в облако не вписана.
2. **UBU01.** `dig +short login.microsoftonline.com` и `curl -sI https://login.microsoftonline.com`. Ожидаемо: IP-адреса и HTTP-ответ (301/302) либо таймаут, если провайдер режет. Зафиксируй что получилось — оба результата валидны для журнала.
3. **UBU01.** `openssl s_client -connect login.microsoftonline.com:443 -servername login.microsoftonline.com </dev/null 2>/dev/null | openssl x509 -noout -issuer -dates`. Ожидаемо: issuer — публичный УЦ, даты в будущем. Если issuer «свой» — трафик перехвачен прокси.
4. **DC01.** Аудит атрибутов для синхронизации: 
   ```powershell
   Get-ADUser -Filter * -Properties mail,UserPrincipalName |
     Select-Object Name,SamAccountName,UserPrincipalName,mail | Format-Table -AutoSize
   ```
   Ожидаемо: у `ivanov` UPN вида `ivanov@corp.vektor.lab` — для облака не годится. Сделай вывод: какой UPN-суффикс нужно добавить в AD Domains and Trusts, чтобы синхронизация прошла корректно.
5. **DC01.** Состав групп (это будут «лицензионные группы»): 
   ```powershell
   Get-ADGroupMember GG-Buh | Select-Object Name,SamAccountName
   Get-ADGroupMember GG-HR  | Select-Object Name,SamAccountName
   ```
   Ожидаемо: в `GG-Buh` — `ivanov`, в `GG-HR` — `petrova`. Зафиксируй: GG-Buh → лицензия с Exchange, GG-HR → лицензия Business Basic.
6. **UBU01.** Поднять локальный аналог Teams (Mattermost Team Edition в контейнере):
   ```bash
   docker run -d --name mm -p 8065:8065 --restart unless-stopped \
     mattermost/mattermost-team-edition:latest
   docker ps
   ```
   Ожидаемо: контейнер `mm` в состоянии `Up`, порт `0.0.0.0:8065->8065/tcp`. Если Docker Hub недоступен из РФ — возьми официальный `.deb`-пакет Mattermost или зеркало реестра `[ПРОВЕРИТЬ актуальную доступность]`.
7. **WIN11-CL01.** Открыть `http://10.10.10.30:8065`, создать первую учётку администратора (`adm.student`), команду «Вектор», каналы `Buh`, `HR`, `Obshchee`. Ожидаемо: интерфейс чата, три канала в списке.
8. **В админке аналога** (`System Console` → User Management): создать пользователей `ivanov`, `petrova`, `sidorov` с учебным паролем стенда. Ожидаемо: три учётки в списке.
9. **WIN11-CL01 (ivanov).** Войти, отправить сообщение в `Buh`. **WIN10-CL02 (petrova).** Войти, убедиться, что `Buh` ей не виден (канал приватный, доступ выдан только ivanov и adm.student). Ожидаемо: у petrova в списке только `HR` и `Obshchee`. Это ровно та модель, что в Teams даёт приватный канал поверх SharePoint-сайта.
10. **UBU01.** Диагностика сервиса: 
    ```bash
    ss -tlnp | grep 8065
    docker logs --tail 30 mm
    ```
    Ожидаемо: порт слушается, в логах записи о старте и о подключениях клиентов.
11. **WIN11-CL01.** Проверить, что «нет доступа» ≠ «нет сети»: `Test-NetConnection 10.10.10.30 -Port 8065`. Ожидаемо: `TcpTestSucceeded : True`. Запомни порядок проверки: сервис на сервере → порт → клиент.
12. **Собрать артефакт.** Создать файл `m365-license-matrix.md` со таблицей: пользователь / OU / группа / нужные сервисы / SKU / цена за месяц `[ПРОВЕРИТЬ]` / примечание. Заполнить на 4 учётки стенда и дописать строку «50 сотрудников → экстраполяция». Это то, что реально просят на входе в проект миграции.
13. **`stand-log.md`.** Записать: на UBU01 поднят контейнер `mm` (порт 8065), созданы учётки `ivanov/petrova/sidorov` в локальном аналоге, артефакт `m365-license-matrix.md` создан.

### Проверка

- [ ] `dsregcmd /status` на WIN11-CL01 сохранён в журнал, поля `AzureAdJoined`/`AzureAdPrt` объяснены своими словами.
- [ ] С UBU01 снят отпечаток TLS-сертификата облачного эндпоинта (или зафиксирован таймаут как факт).
- [ ] На DC01 выведены атрибуты `mail`/`UserPrincipalName` всех пользователей и состав `GG-Buh`/`GG-HR`.
- [ ] На UBU01 работает контейнер `mm`, порт 8065 слушается.
- [ ] В локальном аналоге есть команда «Вектор» и три канала.
- [ ] `petrova` не видит канал `Buh`.
- [ ] `docker logs mm` читается без ошибок.
- [ ] Файл `m365-license-matrix.md` создан и заполнен.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Заявка.** Пользователь: ivanov (бухгалтерия) / ВМ: WIN11-CL01 / Что написал: «Не открывается рабочий чат: браузер пишет "сайт недоступен", до обеда всё работало. У Петровой открывается, я спрашивал. Срочность: средняя, но у меня отчёт».

**Как воспроизвести поломку.** На UBU01: `docker stop mm`. Внешне — ровно «у одного не работает», хотя на самом деле не работает у всех, просто Петрова не проверяла.

**Диагностика по шагам.**
1. С WIN11-CL01: `ping 10.10.10.30` — хост отвечает, значит сеть и ВМ живы (L3 в порядке).
2. `Test-NetConnection 10.10.10.30 -Port 8065` → `TcpTestSucceeded : False`. Локализуем: проблема на сервере, не на клиенте.
3. На UBU01: `ss -tlnp | grep 8065` — порт не слушается.
4. `docker ps -a` → контейнер `mm` в состоянии `Exited (0)`. Причина найдена: сервис остановлен.
5. `docker logs --tail 50 mm` — убедиться, что не было падения из-за нехватки памяти/диска (`df -h`, `free -m`).

**Решение.** `docker start mm`, подождать 10–20 секунд, повторить `Test-NetConnection` с клиента, открыть чат под ivanov, проверить отправку сообщения. Дополнительно включить автозапуск: `docker update --restart unless-stopped mm`.

**Ответ пользователю.** «Иван, добрый день. Проблема была на стороне сервиса чата — он был остановлен, сейчас поднят и работает. Проверил вход под вашей учётной записью, сообщения отправляются. Если снова увидите "сайт недоступен" — напишите, пожалуйста, время и с какого устройства, я проверю логи. Заявку закрываю, при повторении — сразу звоните».

**Закрытие заявки.** Категория «Коллаборация / недоступность сервиса», решение — «сервис остановлен, перезапущен, включён автозапуск», корневая причина — «контейнер не имел политики автозапуска, остановлен при обслуживании», профилактика — «restart policy + мониторинг доступности порта».

### Что изменилось на стенде

- UBU01: запущен контейнер `mm` (Mattermost Team Edition), порт 8065, restart policy `unless-stopped`.
- UBU01: локальные учётки `adm.student` (админ аналога), `ivanov`, `petrova`, `sidorov`.
- Локальный аналог: команда «Вектор», каналы `Buh` (приватный), `HR`, `Obshchee`.
- WIN11-CL01 и WIN10-CL02: настройки браузера/сессии к `http://10.10.10.30:8065`.
- Новый артефакт на хосте: `m365-license-matrix.md`.
- DC01: изменения только в чтении, конфигурация AD не менялась.

### Откат

VMware Workstation → выбрать ВМ → Snapshot Manager → снапшот `before-l2-065` → **Revert**. Сделать на UBU01 (уберёт контейнер и учётки), при необходимости на клиентских ВМ. Файл `m365-license-matrix.md` храни отдельно на хосте — снапшот его не откатит, и это хорошо.

## 6. Ошибки новичков и безопасность

1. **Работать под глобальным администратором постоянно** → одно phishing-письмо = весь тенант. Правильно: повседневная учётка без ролей, отдельная админская, PIM/just-in-time, MFA обязательно.
2. **Путать лицензию и роль** → «я выдал ему E3, а он всё равно не видит канал» (роль не влияет на доступ к контенту) или «дали роль Teams Administrator, чтобы "починил почту"». Правильно: доступ к контенту — членство в группе/команде, права в админке — роль, функциональность — SKU.
3. **Использовать MSOnline и AzureAD-модули** → они выведены из эксплуатации, скрипты падают `[ПРОВЕРИТЬ точные даты вывода]`. Правильно: Microsoft Graph PowerShell SDK.
4. **Удалять уволенного пользователя** → ящик, OneDrive и файлы в Teams уходят в корзину на 30/93 дня, потом необратимо. Правильно: заблокировать вход, снять лицензию, ящик конвертировать в общий, дать доступ руководителю, данные выгрузить.
5. **Копать клиента, не посмотрев Service health** → час диагностики, когда у вендора авария. Правильно: Health → Service health первым делом, всегда.
6. **Синхронизировать весь AD в облако** → лишние объекты, сервисные учётки, утечка структуры. Правильно: фильтр по OU и атрибутам, тестовый пилот.
7. **Считать, что «Teams — это чат, файлы там же»** → файлы канала лежат в SharePoint, личные — в OneDrive; квота и права другие. Правильно: объяснять пользователям и знать самому, где искать.
8. **Игнорировать Conditional Access и MFA** → «пароль утёк, а данных нет» сценарий. Правильно: MFA для всех, блокировка legacy-аутентификации, отдельная политика для админов.

### Безопасность

В боевой среде нельзя: выдавать глобального админа по просьбе «чтобы работало», отключать MFA ради удобства, хранить пароли от сервисных учёток в текстовом файле на FS01, отключать аудит (по нему потом строят разбор инцидента), а также размещать персональные данные сотрудников и клиентов в зарубежном облаке без оценки требований 152-ФЗ. Учебный пароль стенда `Lab#2026-vektor` и открытый порт 8065 без TLS — сознательное упрощение лаборатории; в реальности чат наружу публикуют только через HTTPS и с аутентификацией на домене.

## 7. Вопросы на собеседовании

**1. «Чем Business Standard отличается от E3 и когда вы возьмёте F3?»**
Ответ: Business Standard — веб/десктоп Office + почта + Teams, лимит 300 лицензий на тенант, без полноценного Intune; E3 — корпоративный набор без лимита по количеству, расширенный архив, больший набор соответствия и Entra ID P1/P2 в составе; F3 — для сменного персонала без своего ПК: только браузер/мобильные приложения, без десктопного Office, для касс, склада, водителей. **Что хочет услышать:** что ты выбираешь лицензию по функции и типу сотрудника, а не «по цене». **Красный флаг:** «E5 берём всем, потому что она самая полная» или «назначим Business Standard, а потом посчитаем».

**2. «Пользователь не может войти в Teams, у коллег работает. Что делаешь?»**
Ответ: сначала Service health (массовое или единичное), затем лицензия (есть ли Teams в SKU, `AccountEnabled`), затем Conditional Access/MFA (политика под устройство или локацию), затем `dsregcmd /status` на устройстве, затем сеть/TLS, затем кэш и логи клиента, потом re-sign-in. **Что хочет услышать:** алгоритм «дешевле-к-дороже», отказ от стрельбы по площадям. **Красный флаг:** «переустановлю Teams» или «пересоздам пользователя» вместо диагностики.

**3. «Как управлять административными правами в M365?»**
Ответ: минимум глобальных админов (2, один break-glass без MFA-политики), остальное — узкие роли (Helpdesk, User, License, Teams), админские учётки отдельно от почтовых, PIM для elevator-доступа, MFA и Conditional Access для админских ролей, аудит всех изменений. **Что хочет услышать:** least privilege и break-glass процедура. **Красный флаг:** «у всех админов одна учётка».

**4. «Что такое гибридная идентичность и чем PHS отличается от PTA?»**
Ответ: синхронизация объектов AD DS в Entra ID через Entra Connect; PHS — хэш пароля (точнее, хэш хэша) в облаке, аутентификация только в облаке, работает при падении локальной сети, но локальные политики паролей не действуют в момент входа; PTA — пароль проверяется агентом локально, пароль в облако не попадает, но нужен агент и его доступность; ADFS — полноценная федерация, сложнее и дороже, нужна только под специфические сценарии. **Что хочет услышать:** понимание зависимости доступности от локальной инфраструктуры. **Красный флаг:** «PHS небезопасен, поэтому только ADFS» без аргументов.

**5. «Как вывезти 50 сотрудников с M365 на Яндекс 360 / VK WorkSpace?»**
Ответ: инвентаризация (пользователи, SKU, объём ящиков, календари, файлы SharePoint/OneDrive, команды Teams, интеграции, мобильные устройства); пилот на 3–5 людях; перенос почты по IMAP/через миграционный API, календари по ICS, файлы — вручную/через API; замена приложения чата; переключение DNS (MX, SPF, DKIM, DMARC) с периода coexist; учётки и SSO; обучение и рассылка «что изменилось»; план откатa и период параллельной работы. **Что хочет услышать:** методология миграции, а не «настрою IMAP и всё». **Красный флаг:** «просто переключим MX в пятницу вечером».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Microsoft 365 admin documentation»; статьи: «Microsoft 365 licensing», «Assign licenses to users», «Microsoft Graph PowerShell SDK overview». Поиск: `Microsoft 365 admin center licensing overview`, `Connect-MgGraph get started`.
- **learn.microsoft.com** — раздел «Microsoft Teams admin documentation»: «Manage meeting policies in Teams», «Teams PowerShell overview». Поиск: `Teams meeting policies powershell`.
- **learn.microsoft.com** — «Microsoft Entra documentation»: «What is Microsoft Entra Connect», «Conditional Access overview», «dsregcmd command reference». Поиск: `dsregcmd status hybrid join`.
- **learn.microsoft.com** — «Exchange Online PowerShell»: `Get-MessageTrace`, `Get-MailboxStatistics`. Поиск: `Get-MessageTrace Exchange Online`.
- **learn.microsoft.com** — «Microsoft 365 network connectivity»: URL/IP-диапазоны и порты. Поиск: `Office 365 URLs and IP address ranges` `[ПРОВЕРИТЬ актуальность таблицы портов медиа Teams]`.
- **mattermost.com / docs.mattermost.com** — «Install Mattermost with Docker», «Deployment guide». Поиск: `mattermost docker install team edition`.
- **docs.docker.com** — «Docker run reference». Поиск: `docker run publish restart policy`.
- **yandex.ru/support/yandex-360** и **vk.workspace** — документация российских аналогов для планирования миграции (подробнее — урок 64). Проверяй актуальный статус M365 для РФ у партнёра и в официальных сообщениях Microsoft `[ПРОВЕРИТЬ]`.
