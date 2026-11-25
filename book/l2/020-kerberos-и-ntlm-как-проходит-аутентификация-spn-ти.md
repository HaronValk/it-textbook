# Kerberos и NTLM: как проходит аутентификация, SPN, типовые ошибки, klist

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в поддержку «Вектора»:**

> Пользователь: Иван Иванов (бухгалтерия)
> ВМ: WIN11-CL01
> Что написал: «С обеда не открывается \\FS01\Buh — пишет "Не удается получить доступ к сетевой папке. Целевое имя участника неверно". У Петровой та же ерунда. Пароль не менял, вчера всё работало. Закрытие месяца, помогите!»
> Срочность: высокая

Это классика. Пароль верный, учётка активна, сеть работает, папка на FS01 на месте — но ломается аутентификация. В AD за вход отвечают два протокола: современный Kerberos (по умолчанию везде, где можно) и унаследованный NTLM (когда Kerberos невозможен или сломан). L1 в такой ситуации обычно перезагружает компьютер и переспрашивает пароль. L2 обязан открыть `klist`, посмотреть, есть ли тикет на нужный сервис, проверить журналы DC и найти корневую причину за 10 минут вместо трёх часов перебора.

После этой главы ты будешь понимать, как именно проходят AS- и TGS-обмены, что такое SPN и почему дубликат SPN валит доступ ко всему файловому серверу, как отличить «Kerberos сломался» от «упал в NTLM», какие события (4768/4769/4771/4624/11) искать и как всё это увидеть своими глазами на стенде.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

### Kerberos: билетная система

Kerberos — сетевой протокол аутентификации, в котором пароль **никогда не передаётся по сети**. Вместо этого KDC (Key Distribution Center, центр распределения ключей) выдаёт клиенту зашифрованные «билеты» (tickets). В домене Active Directory KDC — это служба на контроллере домена, состоящая из двух логических частей: **AS** (Authentication Service — выдаёт TGT) и **TGS** (Ticket Granting Service — выдаёт билеты на конкретные сервисы).

Ключевые понятия:

- **Realm** — область Kerberos, в AD совпадает с именем домена: `CORP.VEKTOR.LAB`.
- **Principal** — участник: пользователь (`ivanov@CORP.VEKTOR.LAB`) или сервис (`cifs/fs01.corp.vektor.lab`).
- **TGT** (Ticket Granting Ticket) — «пропуск в здание», зашифрован ключом служебной учётки `krbtgt`. Живёт по умолчанию 10 часов, продлевается до 7 дней.
- **Service ticket** — билет на конкретный сервис, зашифрован ключом (хешем пароля) того аккаунта, под которым работает сервис.
- **Session key** — симметричный ключ сессии, выдаётся клиенту вместе с билетом и используется для проверки подлинности уже напрямую с сервером.
- **SPN** (Service Principal Name) — уникальное имя сервиса в каталоге, по которому KDC находит, чьим ключом шифровать билет. Формат: `класс/хост:порт/имя`, например `HTTP/web01.corp.vektor.lab`, `MSSQLSvc/srv1c01.corp.vektor.lab:1433`.
- **Pre-authentication** — клиент доказывает, что знает пароль, отправив в AS-REQ метку времени, зашифрованную ключом из пароля. Именно этот шаг падает при неверном пароле (событие 4771).
- **SPNEGO («Negotiate»)** — обёртка, которая позволяет клиенту и серверу договориться: сначала пробуем Kerberos, не вышло — откатываемся на NTLM.

```text
КЛИЕНТ (WIN11-CL01, ivanov)                     KDC = DC01 (AS + TGS)
      |                                                |
      | 1) AS-REQ:   «я ivanov, дай TGT»               |
      |    + pre-auth: время, шифрованное ключом пароля |
      |----------------------------------------------->|
      | 2) AS-REP:   TGT (шифр krbtgt) + session key    |
      |<-----------------------------------------------|
      | 3) TGS-REQ:  TGT + «дай билет на cifs/fs01»     |
      |----------------------------------------------->|
      | 4) TGS-REP:  service ticket (шифр ключа FS01)   |
      |<-----------------------------------------------|
      | 5) AP-REQ:   service ticket + authenticator     |
      |              (шифр session key)                 |
      |----------------------------------------------->| FS01
      | 6) AP-REP:   взаимная проверка (опционально)    |
      |<-----------------------------------------------|
```

Важные зависимости Kerberos:
- **DNS.** Клиент ищет KDC по SRV-записям `_kerberos._tcp.corp.vektor.lab` (урок 19). Нет DNS — нет Kerberos.
- **Время.** Допустимое расхождение часов (`MaxClockSkew`) — **5 минут**. Больше — KDC отвечает ошибкой `KRB_AP_ERR_SKEW` (код 0x25).
- **Имя.** Kerberos работает по имени, потому что имя → SPN. По IP-адресу SPN не сформировать.

### NTLM: вызов-ответ

NTLM — протокол challenge-response. Сервер присылает случайное число (challenge), клиент возвращает ответ, вычисленный из хеша пароля (в NTLMv2 — с меткой времени и своим случайным числом). Пароль по сети не идёт, но и сервер клиента не доказывает ничего. NTLM используется внутри SMB, RPC, HTTP там, где Kerberos невозможен.

Главная проблема NTLM для безопасности: перехваченный ответ можно «переиграть» (pass-the-hash, NTLM relay). Поэтому в 2026 году идёт постепенный отказ от NTLM: в Windows Server 2025+ появились механизмы аудита и ограничения NTLM, а Microsoft объявила курс на его вывод [ПРОВЕРИТЬ актуальный статус на learn.microsoft.com]. На стенде мы NTLM не отключаем — иначе половина лабораторных не пройдёт.

### Порты и место в модели OSI

| Что | Протокол / порт | Уровень |
|-----|-----------------|---------|
| Kerberos (AS, TGS) | TCP 88, UDP 88 | L5–L7 (прикладной) |
| Смена пароля (kpasswd) | TCP 464, UDP 464 | прикладной |
| LDAP к каталогу | TCP 389, 636 | прикладной |
| Глобальный каталог | TCP 3268, 3269 | прикладной |
| SMB (файлы, NTLM внутри) | TCP 445 | прикладной |
| RPC (Netlogon, NTLM внутри) | TCP 135 + динамические | прикладной |
| DNS | TCP/UDP 53 | прикладной |
| Аутентификация в целом | — | «слой 5» по OSI: сеансовый, потому что устанавливает сессию доверия перед передачей данных |

## 3. Аналогия из жизни

Представь бизнес-центр, где сидит **администратор на ресепшн** — это KDC (DC01). Ты — сотрудник (ivanov), у тебя есть паспорт (пароль). Ты приходишь на ресепшн и говоришь: «Я Иванов, вот паспорт» — администратор проверяет и выдаёт тебе **пропуск в здание** (TGT). В пропуске написано, что ты Иванов, и он заламинирован так, что подделать нельзя.

Дальше ты идёшь к **турникету на конкретном этаже** — это сервис (файловый сервер FS01). Турникет не умеет проверять паспорт, он умеет только читать пропуск на этаж. Поэтому ты снова на ресепшн: «вот пропуск в здание, дай пропуск на 3-й этаж, в офис 312». Администратор выдаёт **разовый пропуск на этаж** (service ticket), заламинированный ключом именно этого офиса. Офис принимает только свой формат пропуска — и в этом фокус: ресепшн знает ключи всех офисов, но не наоборот.

**SPN** — это табличка с точным названием офиса на двери («Офис 312, FS01»). Если на двух дверях висит одна и та же табличка, администратор не понимает, чей это офис, и выдаёт пропуск с ключом не той двери — а дверь такой пропуск не откроет.

**NTLM** в этой метафоре — охранник, у которого нет ресепшна: он просто спрашивает «назови кодовое слово», и если ты назвал правильную производную от паспорта — пускает. Кодовое слово не несёт информации, кто ты и куда идёшь.

Где аналогия ломается: в реальном бизнес-центре пропуск на этаж не содержит зашифрованного письма от администратора, а в Kerberos service ticket — это буквально зашифрованная структура, которую сервис расшифровывает своим ключом, и офис на самом деле проверяет не человека, а валидность билета.

## 4. Практика и команды

### 4.1. klist — главный инструмент

```powershell
klist
klist tgt
klist tickets
klist purge
klist sessions
klist -li 0x1a2b3c purge
```

Разбор: `klist` без параметров показывает кеш билетов текущего входа. `tgt` — только TGT с деталями, `tickets` — все билеты. `purge` очищает кеш (нужно после смены пароля или починки SPN). `sessions` — список сеансов входа (LUID), `-li` — работать с кешем конкретного сеанса (её используют с `0x3e7` — сеанс SYSTEM).

Сокращённый вывод:

```text
Current LogonId is 0:0x1a2b3c
Cached Tickets: (2)

#0>     Client: ivanov @ CORP.VEKTOR.LAB
        Server: krbtgt/CORP.VEKTOR.LAB @ CORP.VEKTOR.LAB
        KerbTicket Encryption Type: AES-256-CTS-HMAC-SHA1-96
        Ticket Flags 0x40e10000 -> forwardable renewable initial pre_authent
        Start Time: 2/10/2026 9:12:44 (local)
        End Time:   2/10/2026 19:12:44 (local)
        Renew Time: 2/17/2026 9:12:44 (local)
        Kdc Called: DC01.corp.vektor.lab

#1>     Client: ivanov @ CORP.VEKTOR.LAB
        Server: cifs/fs01.corp.vektor.lab @ CORP.VEKTOR.LAB
        KerbTicket Encryption Type: AES-256-CTS-HMAC-SHA1-96
        End Time:   2/10/2026 19:12:44 (local)
```

Что смотреть: есть ли `krbtgt` (значит, вход в домен прошёл по Kerberos), есть ли билет на **конкретный** сервис (`cifs/fs01...`), тип шифрования (AES — норма; RC4 — повод разбираться), время окончания (просроченный билет = падение в NTLM), и `Kdc Called` — на какой контроллер домена клиент ходил.

Получить билет на конкретный сервис принудительно, чтобы проверить, находится ли SPN и выдаётся ли билет:

```powershell
klist get cifs/fs01.corp.vektor.lab
```

Параметр `get` в Windows-версии `klist` есть, но его поведение отличается от Linux-версии [ПРОВЕРИТЬ в актуальной документации на learn.microsoft.com].

### 4.2. setspn — работа с именами сервисов

```powershell
setspn -L FS01
setspn -Q HOST/fs01.corp.vektor.lab
setspn -X
setspn -S HOST/fs01.corp.vektor.lab VEKTOR\SRV1C01$
setspn -D HOST/fs01.corp.vektor.lab VEKTOR\SRV1C01$
setspn -A HTTP/alias.corp.vektor.lab VEKTOR\SRV1C01$
```

- `-L <аккаунт>` — список SPN аккаунта. У компьютерной учётки FS01 по умолчанию есть `HOST/FS01`, `HOST/fs01.corp.vektor.lab`, `RestrictedKrbHost/...` — этого хватает для SMB и большинства служб.
- `-Q <SPN>` — «у кого зарегистрирован этот SPN».
- `-X` — поиск **дубликатов** SPN по лесу (можно ограничить `-T corp.vektor.lab`) [ПРОВЕРИТЬ синтаксис `-T`].
- `-S` — добавить SPN **с проверкой** на дубликат. Если дубликат есть — операция отменяется.
- `-A` — добавить **без проверки**. Именно этой опцией и создают себе аварию.
- `-D` — удалить SPN.

Типичный вывод `setspn -L FS01`:

```text
Registered ServicePrincipalNames for CN=FS01,OU=Серверы,DC=corp,DC=vektor,DC=lab:
        HOST/FS01
        HOST/fs01.corp.vektor.lab
        RestrictedKrbHost/FS01
        RestrictedKrbHost/fs01.corp.vektor.lab
```

Если пользователи обращаются к серверу по DNS-алиасу (CNAME), SPN на алиас нет, и Kerberos не сработает — лечится добавлением `HOST/<алиас>` на аккаунт сервера (`setspn -S HOST/alias <аккаунт>`).

### 4.3. Аудит Kerberos на контроллере домена

```powershell
auditpol /list /subcategory:* /v
auditpol /set /subcategory:"Kerberos Authentication Service" /success:enable /failure:enable
auditpol /set /subcategory:"Kerberos Service Ticket Operations" /success:enable /failure:enable
```

В русской локали имена подкатегорий другие [ПРОВЕРИТЬ: «Служба проверки подлинности Kerberos», «Операции службы билетов Kerberos»] — сначала посмотри их через `/list`. Где смотреть события: **Event Viewer → Windows Logs → Security**, фильтр по ID:

- **4768** — запрошен TGT (успех/провал предварительной аутентификации);
- **4769** — запрошен service ticket;
- **4770** — TGT продлён;
- **4771** — провал pre-auth (неверный пароль, отключённая учётка, расхождение времени);
- **4740** — блокировка учётной записи.

В 4771 смотри поле **Failure Code**: `0x18` — неверный пароль, `0x12` — учётка отключена/истекла/ограничение по времени входа, `0x17` — истёк пароль, `0x25` — расхождение времени, `0x6` — неизвестный клиент, `0x7` — неизвестный сервис (SPN не найден).

### 4.4. Событие 11 на DC: дубликат SPN

В **Event Viewer → Windows Logs → System** на DC01 ищи источник `Kerberos-Key-Distribution-Center`, **Event ID 11**: «The KDC encountered duplicate names while processing a Kerberos authentication request». Это прямой указатель на дубликат SPN. Быстрее всего:

```powershell
Get-WinEvent -LogName System -MaxEvents 200 |
  Where-Object { $_.Id -eq 11 -and $_.ProviderName -like '*Kerberos*' } |
  Select-Object TimeCreated, Message
```

### 4.5. Кто на самом деле аутентифицировался: событие 4624

```powershell
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4624; StartTime=(Get-Date).AddHours(-1)} |
  Where-Object { $_.Message -match '10\.10\.10\.' } |
  Select-Object TimeCreated, @{n='Msg';e={($_.Message -split "`n" | Select-String 'Authentication Package|Account Name|Source Network Address') -join ' | '}}
```

В тексте 4624 есть строка **Authentication Package**: `Kerberos` или `NTLM`. Это самый честный способ узнать, чем реально пустили пользователя. Включён ли аудит входа: `auditpol /get /subcategory:"Logon"`.

### 4.6. Время: w32tm

```powershell
w32tm /monitor
w32tm /stripchart /computer:DC01.corp.vektor.lab /samples:5
w32tm /resync /rediscover
```

`/monitor` показывает смещение локальных часов относительно DC и Stratum. `/stripchart` рисует график расхождения в реальном времени, `/samples` — число замеров. `/resync /rediscover` — принудительная пересинхронизация с переобнаружением источника. Оффсет больше ±5 минут = Kerberos не работает.

### 4.7. Проверка доверия и поиск DC: nltest

```powershell
nltest /sc_query:corp.vektor.lab
nltest /dsgetdc:corp.vektor.lab
```

`/sc_query` показывает состояние защищённого канала с доменом (используется Netlogon, работает на NTLM и часто «маскирует» проблемы Kerberos: канал жив, а Kerberos — нет). `/dsgetdc` показывает, какой DC выбрал клиент по DC Locator — если он ходит не туда, где зарегистрирован SPN или где расходятся часы, будет «плавающая» проблема.

### 4.8. Подробное логирование Kerberos на клиенте

```powershell
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa\Kerberos\Parameters' `
  -Name LogLevel -Value 1 -PropertyType DWord -Force
```

После перезапуска (или перелогина) в **System** появляются события источника `Kerberos` с деталями ошибок клиента — полезно, когда пользователь говорит «не пускает», а на DC ничего нет. Не забудь потом вернуть `LogLevel` в 0, иначе журнал засорится.

### 4.9. Инвентаризация SPN через PowerShell

```powershell
Get-ADUser -Filter 'servicePrincipalName -like "*"' -Properties servicePrincipalName |
  Select-Object Name, servicePrincipalName
Get-ADComputer -Filter * -Properties servicePrincipalName |
  Where-Object servicePrincipalName | Select-Object Name, servicePrincipalName
```

Плюс поиск дубликатов без setspn:

```powershell
Get-ADObject -LDAPFilter '(servicePrincipalName=*)' -Properties servicePrincipalName |
  ForEach-Object { $_.servicePrincipalName } | Group-Object | Where-Object Count -gt 1
```

### 4.10. NTLM: аудит и ограничение

Путь в GUI: **secpol.msc → Local Policies → Security Options → Network security: Restrict NTLM: Audit Incoming NTLM Traffic** (и парная политика для исходящего трафика). В домене это раздаётся GPO на FS01/DC01. Реестровые значения живут в `HKLM\SYSTEM\CurrentControlSet\Control\Lsa\MSV1_0` (`RestrictSendingNTLMTraffic`, `RestrictReceivingNTLMTraffic`) [ПРОВЕРИТЬ точные числовые значения для receiving].

Логи: **Event Viewer → Applications and Services Logs → Microsoft → Windows → NTLM → Operational**, события 8001–8004 (заблокировано/аудит, входящее/исходящее) [ПРОВЕРИТЬ номера].

### 4.11. Linux-сторона: kinit, klist, kvno

```bash
sudo apt install -y krb5-user
kinit adm.student@CORP.VEKTOR.LAB
klist -e
kvno cifs/fs01.corp.vektor.lab
kdestroy
```

`kinit` получает TGT (спросит пароль), `klist -e` показывает билеты с типами шифрования, `kvno` запрашивает service ticket на указанный SPN — идеальный тест «а SPN-то на месте?». `kdestroy` чистит кеш (по умолчанию `/tmp/krb5cc_<uid>`). На UBU01 нужен `/etc/krb5.conf` с realm `CORP.VEKTOR.LAB` и KDC `10.10.10.10`, а в `/etc/resolv.conf` — DNS `10.10.10.10`.

## 5. Лабораторная работа

### Предусловия

Нужны: **DC01** (AD DS, DNS, DHCP, `adm.student`), **FS01** (папки `Obshchee`, `Buh`, `HR`), **WIN11-CL01** (ivanov), **WIN10-CL02** (petrova). Все в домене, время синхронизировано. Опционально **SRV1C01** — только как «чужой» аккаунт для проверки `setspn -S`; если его ещё нет, шаг 6 пропусти или используй любой другой компьютерный аккаунт. **UBU01** — для шага 11. На DC01 понадобятся права администратора домена.

### Снапшот

В VMware Workstation: **VM → Snapshot → Take Snapshot**, имя `before-l2-020`, на DC01, FS01, WIN11-CL01, WIN10-CL02 (и SRV1C01, если используешь). Через командную строку хоста:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\DC01\DC01.vmx" before-l2-020
```

### Шаги

1. **DC01**, PowerShell от `VEKTOR\adm.student`: включаем аудит Kerberos (`auditpol /set ...` из п. 4.3). Проверяем: `auditpol /get /subcategory:"Kerberos Service Ticket Operations"` → `Success and Failure`.
2. **WIN11-CL01**, вход `VEKTOR\ivanov`: `klist`. Ожидаемо: один билет `krbtgt/CORP.VEKTOR.LAB`, AES-256, `Kdc Called: DC01.corp.vektor.lab`.
3. **WIN11-CL01**: `klist tgt` → видим Start/End/Renew Time и Ticket Flags. Затем `klist purge` и снова `klist` → «Cached Tickets: (0)».
4. **WIN11-CL01**: открываем в проводнике `\\FS01\Obshchee`, закрываем окно, выполняем `klist` → появился второй билет `cifs/fs01.corp.vektor.lab`. Фиксируем: именно он и есть «пропуск на этаж».
5. **WIN11-CL01**: `klist sessions` → список сеансов с LUID. Ожидаемо: минимум один интерактивный сеанс ivanov.
6. **DC01**: `setspn -L FS01` → четыре записи HOST/RestrictedKrbHost. Затем `setspn -Q HOST/fs01.corp.vektor.lab` → FS01. Затем (если SRV1C01 есть) `setspn -S HOST/fs01.corp.vektor.lab VEKTOR\SRV1C01$` → ошибка «Duplicate SPN found, aborting operation!» и запись НЕ добавляется. Это демонстрация защиты.
7. **DC01**: `setspn -X` → «No duplicate SPNs found» (на стенде из 6 машин выполняется быстро).
8. **WIN11-CL01**: `klist purge`, затем открываем `\\10.10.10.20\Obshchee` — по IP. Снова `klist` → билета `cifs/...` **нет**. На DC01 в Security ищем 4624 за последние минуты → `Authentication Package: NTLM`. Вывод: по IP Kerberos невозможен, был откат в NTLM.
9. **FS01**: включаем аудит входящего NTLM (secpol, п. 4.10). Повторяем шаг 8 → в логе NTLM Operational появляются события аудита.
10. **WIN10-CL02** (petrova): отключаем синхронизацию времени с хостом (**VM → Settings → Options → VMware Tools → снять Time synchronization**), затем в PowerShell от администратора: `net stop w32time`, `Set-Date -Adjust (New-TimeSpan -Minutes 10)`. Открываем `\\FS01\HR`. Ожидаемо: либо ошибка доступа, либо доступ прошёл по NTLM — но на **DC01** в Security появляется **4771 с Failure Code 0x25**, а на клиенте в System — событие Kerberos о `KRB_AP_ERR_SKEW`.
11. **WIN10-CL02**: `w32tm /resync /rediscover`, `w32tm /stripchart /computer:DC01.corp.vektor.lab /samples:5` (оффсет в пределах секунд), `klist purge`, повторный вход в `\\FS01\HR` → билет в `klist` есть, доступ по Kerberos.
12. **UBU01** (опционально): создаём `/etc/krb5.conf` с realm `CORP.VEKTOR.LAB` и `kdc = 10.10.10.10`, затем `kinit adm.student@CORP.VEKTOR.LAB`, `klist -e`, `kvno cifs/fs01.corp.vektor.lab` → `kvno = N`. Записываем результат в `stand-log.md`.

### Проверка

- [ ] На WIN11-CL01 `klist` показывает TGT с типом AES-256 и `Kdc Called: DC01`.
- [ ] После обращения к `\\FS01\Obshchee` в `klist` появляется билет `cifs/fs01.corp.vektor.lab`.
- [ ] `klist purge` очищает кеш, `klist` показывает 0 билетов.
- [ ] `setspn -L FS01` выдаёт HOST-записи, `setspn -X` не находит дубликатов.
- [ ] `setspn -S` с чужим аккаунтом отказывается добавлять дубликат.
- [ ] Обращение по IP не создаёт cifs-билет, а в 4624 видно `NTLM`.
- [ ] На DC01 есть 4768/4769 (успех) и 4771 с кодом 0x25 после сдвига времени.
- [ ] После `w32tm /resync /rediscover` доступ снова идёт по Kerberos.
- [ ] В `stand-log.md` записано, что изменено.

### Сломай и почини

> **Пользователь:** Мария Петрова (HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Не открывается \\FS01\HR. Пишет "Не удается получить доступ к сетевой папке. Целевое имя участника неверно". Утром открывалось, сейчас через раз. У Иванова та же папка открывается нормально. Срочность: средняя, но мне до обеда нужно.»

**Как воспроизвести поломку** (на DC01, от `adm.student`):

```powershell
setspn -A HOST/fs01.corp.vektor.lab VEKTOR\SRV1C01$
setspn -Q HOST/fs01.corp.vektor.lab
```

Ключ `-A` добавляет SPN без проверки дубликата, и теперь один и тот же SPN висит на двух аккаунтах. На клиенте дополнительно: `klist purge` и переоткрыть папку.

**Диагностика по шагам:**

1. **Клиент (WIN10-CL02).** `klist` → есть ли билет `cifs/fs01.corp.vektor.lab`? Если нет, а доступ иногда работает — это откат в NTLM, а не проблема прав.
2. **Клиент.** Точный текст ошибки и `whoami` — убеждаемся, что пользователь действительно petrova, а не локальная учётка.
3. **DC01, System log.** Ищем **Event ID 11** от `Kerberos-Key-Distribution-Center` — «duplicate names». Это подтверждение гипотезы о дубликате SPN.
4. **DC01.** `setspn -X` → находит дубликат по `HOST/fs01.corp.vektor.lab`. `setspn -Q HOST/fs01.corp.vektor.lab` → показывает, что SPN висит и на FS01, и на SRV1C01.
5. **Проверка времени** (чтобы исключить второй фактор): `w32tm /stripchart /computer:DC01.corp.vektor.lab /samples:3` на клиенте — оффсет в норме.
6. **Проверка DNS:** `nslookup fs01.corp.vektor.lab` с клиента → правильный IP 10.10.10.20.

**Решение:**

```powershell
setspn -D HOST/fs01.corp.vektor.lab VEKTOR\SRV1C01$
setspn -X
```

Затем на клиенте `klist purge`, закрыть все окна проводника (или `net use \\FS01\HR /delete`), открыть папку заново, `klist` → билет `cifs/fs01.corp.vektor.lab` на месте.

**Ответ пользователю:** «Мария, добрый день. Причина найдена: в каталоге домена была ошибочно продублирована запись службы файлового сервера, из-за этого ваш компьютер не мог получить корректный билет доступа. Ошибку я убрал, кеш на вашем компьютере обновил. Пожалуйста, закройте все окна проводника и откройте \\FS01\HR ещё раз — должно работать. Если повторится, напишите, я проверю с другой стороны.»

**Закрытие заявки:** категория «AD / аутентификация», в решении указать: дубликат SPN `HOST/fs01.corp.vektor.lab` на аккаунте SRV1C01, удалён через `setspn -D`, кеш клиента очищен, проверено. Добавить в базу знаний: «при ошибке "целевое имя участника неверно" сначала setspn -X».

### Что изменилось на стенде

- **DC01:** включён аудит подкатегорий «Kerberos Authentication Service» и «Kerberos Service Ticket Operations» (success+failure). В журнале Security появились 4768/4769/4771. Временных SPN не осталось (шаг 6 отменяется самим `setspn -S`).
- **FS01:** включён аудит входящего NTLM-трафика, в логе NTLM Operational появились события 8001–8004.
- **WIN10-CL02:** синхронизация времени с хостом отключена в настройках ВМ (верни обратно, если не нужна), время восстановлено через `w32tm`.
- **UBU01 (если делал шаг 12):** установлен `krb5-user`, создан `/etc/krb5.conf`, кеш билетов очищен через `kdestroy`.
- Кеши билетов на клиентах очищены; повторный вход создал новые билеты.

### Откат

VMware Workstation: **VM → Snapshot Manager → before-l2-020 → Revert**. Или из командной строки хоста:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\WIN10-CL02\WIN10-CL02.vmx" before-l2-020
```

Перед откатом закрой окна проводника с сетевыми папками, иначе Windows будет держать SMB-сессию к откатанному серверу.

## 6. Ошибки новичков и безопасность

1. **«Не пускает — перевведём в домен»** → слетает машинный пароль, проблема маскируется или усугубляется → сначала `klist` и журналы DC, переввод — последним средством.
2. **Тестировать доступ по IP** (`\\10.10.10.20\...`) → Kerberos принципиально невозможен, идёт NTLM, и новичок делает вывод «Kerberos не работает» → всегда проверяй по FQDN.
3. **Добавлять SPN через `setspn -A`** → дубликаты, событие 11, отказ доступа у половины офиса → только `setspn -S` и проверка `setspn -X` после изменений.
4. **Увеличивать `MaxClockSkew` «чтобы не мешало»** → маскирует сломанную синхронизацию времени, ломает керберизацию в других сервисах и снижает безопасность (replay) → чинить `w32tm`, а не политику.
5. **Включать RC4/DES «чтобы заработало»** → деградация шифрования, несовместимость с ужесточёнными политиками 2026 года → разбираться, почему сервис не берёт AES (устаревший сервис, старый пароль, кривой SPN).
6. **Путать «нет доверительных отношений» с ошибкой Kerberos** → это про машинный аккаунт и защищённый канал, а не про билеты; лечение разное (это тема урока 28).
7. **Не включать аудит и потом искать «логи»** → 4768/4769/4771 не пишутся, если подкатегории не включены → `auditpol` на DC до начала диагностики.
8. **Оставлять `LogLevel=1` и кеш билетов «как есть»** → журнал забивается, а старые билеты после смены пароля путают диагностику → `klist purge`, `LogLevel` в 0.

### Безопасность

- NTLM-аутентификация уязвима к **pass-the-hash** и **NTLM relay** (перехват и переигрывание ответа, инструменты вроде Responder). В боевой среде NTLM ограничивают политиками `Restrict NTLM`, включают SMB signing и LDAP signing, а не «разрешают всё, чтобы работало». На стенде мы NTLM разрешаем осознанно — это учебная среда.
- **`klist purge` под SYSTEM (`klist -li 0x3e7 purge`)** на сервере рвёт аутентификацию служб. В проде — только по согласованию и с пониманием, какие службы работают под системными аккаунтами.
- **Хранение пароля в скриптах для `kinit`** — грубая ошибка. В домене для неинтерактивных задач используют keytab-файлы с ограниченными правами (и всё равно это цель атаки). На стенде пароль `Lab#2026-vektor` — только учебный, в реальной работе такой практики нет.
- **SPN и делегирование.** Если сервису выдано право делегирования (нужно для «двойного перехода», например веб-приложение → SQL), скомпрометированный сервис получает доступ к бэкенду от имени пользователя. Выдавать делегирование «на всякий случай» нельзя — это отдельная тема (урок 25).

## 7. Вопросы на собеседовании

**1. Чем Kerberos отличается от NTLM и почему Kerberos предпочтительнее?**

Ответ: Kerberos — протокол с доверенной третьей стороной (KDC). Пароль не передаётся, клиент доказывает знание пароля через pre-auth, получает TGT и затем отдельные билеты на каждый сервис, зашифрованные ключом этого сервиса. Сервер может проверить клиента, клиент — сервер (взаимная аутентификация), поддерживается делегирование и время жизни билетов. NTLM — challenge-response: сервер присылает случайное число, клиент отвечает производной от хеша пароля. Односторонняя аутентификация, нет Delegation, ответ можно перехватить и переиграть (pass-the-hash, relay). Поэтому в AD Kerberos — основной протокол, а NTLM оставлен для совместимости.

*Что хочет услышать интервьюер:* понимание роли KDC, TGT/сервисных билетов и того, что NTLM — это ограниченная и уязвимая замена.

*Красный флаг:* «NTLM и Kerberos — это одно и то же, просто разные версии» или «Kerberos шифрует весь трафик».

**2. Пользователь открывает \\FS01 по имени — всё работает, по IP — просит пароль. Почему?**

Ответ: Kerberos работает по имени, потому что имя превращается в SPN (`cifs/fs01.corp.vektor.lab`), по которому KDC находит аккаунт сервиса и шифрует билет его ключом. По IP-адресу SPN не сформировать, поэтому SPNEGO откатывается на NTLM. Проверяется через `klist` (билета на cifs нет) и событие 4624 с `Authentication Package: NTLM`. Правильное поведение — не «лечить», а объяснить пользователю и при необходимости использовать DNS-алиас с корректным SPN.

*Что хочет услышать интервьюер:* связку «имя → SPN → Kerberos» и понимание отката на NTLM.

*Красный флаг:* «по IP Kerberos не работает, это баг Windows, надо добавить запись в hosts» — без объяснения SPN.

**3. Что такое SPN и что произойдёт при дубликате?**

Ответ: SPN — Service Principal Name, уникальное имя сервиса в каталоге в формате `класс/хост:порт/имя`. Регистрируется на аккаунт (обычно компьютерный) через `setspn -S`. Если один и тот же SPN висит на двух аккаунтах, KDC не понимает, чьим ключом шифровать билет: в System на DC появляется событие 11 (KDC encountered duplicate names), клиент получает ошибку вида «целевое имя участника неверно» или молча откатывается в NTLM — доступ становится медленным и нестабильным. Диагностика: `setspn -X`, `setspn -Q`, удаление через `setspn -D`, затем `klist purge` на клиенте.

*Что хочет услышать интервьюер:* понимание уникальности SPN, конкретные команды и событие 11.

*Красный флаг:* «SPN — это просто алиас, дубликаты не важны».

**4. На контроллере домена в Security есть 4771 с Failure Code 0x25. Что это и что делать?**

Ответ: 4771 — неуспешная предварительная аутентификация Kerberos. Код 0x25 = `KRB_AP_ERR_SKEW`, расхождение времени клиента и KDC больше допустимых 5 минут. Проверяю `w32tm /stripchart /computer:<DC> /samples:5` на клиенте, источник времени (`w32tm /query /source`), состояние `w32time` на DC (`w32tm /monitor`), после чего `w32tm /resync /rediscover` на клиенте и `klist purge`. Если время «уезжает» — ищу причину: сломанный NTP-источник, отключённая синхронизация в виртуалке, неверный часовой пояс.

*Что хочет услышать интервьюер:* знание кода 0x25, допустимого расхождения 5 минут и инструмента w32tm.

*Красный флаг:* «увеличим MaxClockSkew до часа».

**5. Как понять, что пользователь аутентифицировался по NTLM, а не по Kerberos?**

Ответ: три способа. Первый — на клиенте `klist`: если после обращения к сервису нет билета на нужный SPN, Kerberos не применялся. Второй — событие 4624 на сервере или DC: поле `Authentication Package` = NTLM или Kerberos, плюс `Logon Type 3` для сетевого доступа. Третий — аудит NTLM: политика «Restrict NTLM: Audit Incoming NTLM Traffic» и лог `Microsoft-Windows-NTLM/Operational` (8001–8004). Дальше ищу причину: IP вместо имени, отсутствующий или дублированный SPN, DNS-алиас, расхождение времени, клиент вне домена.

*Что хочет услышать интервьюер:* владение `klist` как первым инструментом и понимание, что «тихо работает» ≠ «работает правильно».

*Красный флаг:* «Kerberos и NTLM неразличимы, какая разница».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Server → Identity and Access → Kerberos authentication overview»; статьи «How Kerberos works», «Kerberos authentication troubleshooting». Поиск: `Kerberos authentication overview site:learn.microsoft.com`.
- **learn.microsoft.com** — справочники по командам: «setspn», «klist», «w32tm», «nltest», «auditpol». Поиск: `setspn command reference site:learn.microsoft.com`, `klist command site:learn.microsoft.com`.
- **learn.microsoft.com** — «NTLM overview» и «Network security: Restrict NTLM» (описание политик и настроек реестра). Поиск: `NTLM overview Restrict NTLM settings site:learn.microsoft.com`.
- **learn.microsoft.com** — «Audit Kerberos authentication» и «Windows security audit events» (расшифровка 4768, 4769, 4771, 4624). Поиск: `event 4768 4769 4771 Kerberos audit site:learn.microsoft.com`.
- **MIT Kerberos Documentation** (kerberos.org, web.mit.edu) — «kinit», «klist», «kvno», «krb5.conf». Поиск: `MIT Kerberos documentation kinit klist`.
- **IETF Datatracker** (datatracker.ietf.org) — RFC 4120 «The Kerberos Network Authentication Service (V5)»; RFC 4178 «SPNEGO». Поиск: `RFC 4120 Kerberos V5`.
- **VMware Docs** (techzone.vmware.com / docs.vmware.com) — «Workstation snapshots», «vmrun command reference». Поиск: `vmrun snapshot revertToSnapshot Workstation`.
- Статус отказа Microsoft от NTLM и поддержка RC4 в Kerberos меняются — перед тренировкой сверяйся с актуальными анонсами на learn.microsoft.com. `[ПРОВЕРИТЬ]`
