# Active Directory Federation Services, Certificate Services и NPS: единая аутентификация и сертификаты

## 1. Введение (зачем это нужно в реальной работе)

> **Тикет #4712**
> **Пользователь:** ivanov (WIN11-CL01)
> **Что написал:** «Не могу подключиться к корпоративному Wi-Fi в переговорке — пишет "Windows не удалось проверить подлинность сервера". Утром работало. И ещё: захожу в личный кабинет перевозчика — снова требует логин и пароль, хотя я уже вошёл в Windows. Коллеги из HR говорят то же самое».
> **Срочность:** высокая, человек не может работать.

В этом тикете сразу две разные системы, и обе — про доверие. Первая (Wi-Fi) — это NPS и сертификаты: точка доступа спрашивает у RADIUS-сервера, можно ли пустить этого пользователя, и обе стороны проверяют сертификаты друг друга. Вторая (кабинет перевозчика) — это федерация: AD FS выдаёт внешнему сервису подписанное утверждение «это действительно ivanov из «Вектора»», и пароль наружу не уходит.

В работе системного админа эта тройка встречается постоянно: 802.1X для Wi-Fi и проводной сети, VPN с проверкой по сертификату, SSO в десяток SaaS-сервисов, вход по смарт-карте, шифрование трафика внутри периметра, подпись документов, интеграция с 1С и внешними платформами. После этой главы вы будете понимать, как устроена цепочка доверия от корневого CA до токена в браузере, уметь разворачивать двухуровневый PKI (пусть и в упрощённом лабе), выпускать сертификаты через шаблоны и автоенроллмент, поднимать AD FS и NPS и — главное — диагностировать, на каком именно шаге цепочка порвалась.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Три сервиса решают одну задачу разными инструментами: **доказать, что субъект — это он, не передавая пароль по сети**.

**AD CS (Active Directory Certificate Services)** — это PKI, инфраструктура открытых ключей. Выдаёт X.509-сертификаты: документ, в котором открытый ключ субъекта подписан закрытым ключом удостоверяющего центра (CA). Ключевые понятия:

- **Корневой CA (root)** — самоподписанный, его сертификат лежит в храме «Доверенные корневые центры сертификации» у всех. Его закрытый ключ — самое ценное, что у вас есть, поэтому в норме корневой CA выключен и физически изолирован (offline root).
- **Выдающий CA (issuing / subordinate)** — подчинён корневому, работает онлайн, выпускает сертификаты пользователям и серверам.
- **Шаблон (template)** — правило: кто может запросить, какие EKU (Enhanced Key Usage — назначение ключа: Server Authentication, Client Authentication, Smart Card Logon), срок жизни, нужно ли одобрение руководителя. Права на шаблон = права выдавать себе удостоверения, поэтому шаблоны — объект атаки.
- **Автоенроллмент** — механизм, при котором клиент сам, по GPO, забирает нужные сертификаты и продлевает их. Работает через RPC/DCOM к CA (порты 135/tcp + динамика) и LDAP к AD.
- **CRL (Certificate Revocation List)** — список отозванных сертификатов. Публикуется по расписанию (по умолчанию раз в неделю) в точку распространения CDP, обычно `ldap:///CN=...CDP...` и файл/HTTP. **OCSP** — онлайн-проверка отзыва по HTTP, быстрее и свежее CRL. **AIA** — где скачать сам сертификат CA, чтобы достроить цепочку.

**AD FS (Active Directory Federation Services)** — служба токенов (STS, Security Token Service). Пользователь аутентифицируется в AD FS (Kerberos через встроенную аутентификацию браузера, форма логина, сертификат), AD FS выпускает **claim** — утверждение вида «user.UPN = ivanov@corp.vektor.lab», «role = Бухгалтерия» — и подписывает его своим **token-signing** сертификатом. Внешнее приложение (**relying party trust**, доверяющая сторона) проверяет подпись по метаданным федерации. Протоколы: SAML 2.0, WS-Federation, WS-Trust, а с 2016 — ещё OAuth2/OpenID Connect. Порт — 443/tcp, плюс 49443 для аутентификации по сертификату пользователя `[ПРОВЕРИТЬ на своей сборке]`. В DMZ ставится **WAP (Web Application Proxy)** — он публикует AD FS наружу, не пуская интернет напрямую к домену.

**NPS (Network Policy Server)** — RADIUS-сервер. Реализует RFC 2865/2866: UDP 1812 (аутентификация) и 1813 (аккаунтинг), устаревшие 1645/1646. Применяется для 802.1X (Wi-Fi, коммутаторы), VPN, аутентификации RAS. Внутри — **EAP-методы**: PEAP-MSCHAPv2 (пароль внутри TLS-туннеля, серверу нужен сертификат, клиенту — доверие к CA) и EAP-TLS (двусторонние сертификаты, пароля нет вообще). Логика решения: сначала **Connection Request Policy** (обрабатываем локально или проксируем), затем **Network Policy** (условия → ограничения → «разрешить/запретить», первая совпавшая политика выигрывает), затем Dial-in свойства учётной записи в AD.

Схема стенда в этой теме:

```text
        ① вход в Wi-Fi/VPN (802.1X)                     ② вход в SaaS (SSO)
 WIN11-CL01 ──EAP-TLS──► DC01:NPS ──LDAP 389/636──► AD DS   браузер ──HTTPS 443──► FS01:AD FS ──SAML──► SaaS
                            │                                                    │
                            └────────── проверка цепочки / CRL ──────────────────┘
                                                    │
                                    DC02: AD CS (Vektor-Root-CA)
                                    шаблоны · выдача · CRL · AIA
```

## 3. Аналогия из жизни

Представьте поездку в другую страну. **Паспортный стол** (корневой CA) выдаёт вам **загранпаспорт** — книжку с фото и защитой, которую невозможно подделать, но которую легко проверить. Паспорт не вы изучаете заново на каждой границе: его просто сверяют с образцом. **Выдающий CA** — это МФЦ, который печатает паспорт по шаблону: обычный, служебный, дипломатический. **CRL** — стоп-лист аннулированных паспортов, который пограничник иногда обновляет. **AD FS** — консульство: по вашему паспорту оно выдаёт **визу** (токен) в конкретную страну (SaaS), и страна верит визе, потому что знает печать консульства. **NPS** — охранник на входе в офис: смотрит паспорт, сверяет со стоп-листом и пускает только тех, кому по пропуску положено.

Аналогия ломается в одном: пограничник верит своим глазам и бумаге, а здесь вся проверка — математика (подпись проверяется за микросекунды без человека), и именно поэтому один и тот же «паспорт» можно предъявить миллион раз, а отзыв работает не мгновенно — нужен онлайн-канал (OCSP) или свежий стоп-лист.

## 4. Практика и команды

**1. Установка роли CA и настройка Enterprise Root CA (DC02)**

```powershell
Install-WindowsFeature ADCS-Cert-Authority -IncludeManagementTools
Install-AdcsCertificationAuthority -CAType EnterpriseRootCA `
  -CACommonName "Vektor-Root-CA" `
  -CryptoProviderName "RSA#Microsoft Software Key Storage Provider" `
  -KeyLength 4096 -HashAlgorithmName SHA256 `
  -ValidityPeriod Years -ValidityPeriodUnits 20 -Force
```

`-CAType EnterpriseRootCA` — CA интегрирован в AD (шаблоны, публикация в LDAP); `-KeyLength 4096` — длина ключа; `-ValidityPeriod Years -ValidityPeriodUnits 20` — срок жизни самого CA. В продакшене так **не делают**: корневой CA должен быть Standalone и offline.

**2. Проверка состояния CA**

```cmd
certutil -CAInfo
certutil -crl
```

`-CAInfo` показывает имя, тип CA, срок, количество выданных. `-crl` публикует CRL немедленно (не ждать неделю). Смотреть на `CRL published successfully` и на дату `Next CRL Publish`.

**3. Просмотр выданных сертификатов**

```cmd
certutil -view -restrict "Disposition=20" -out "RequestID,RequesterName,CommonName,NotAfter"
```

`Disposition=20` — «выпущен» (21 — отозван). Смотрим, кто что получил: если сертификат с EKU Client Authentication запросил не тот, кто должен, — вы нашли дыру в правах шаблона.

**4. Публикация и проверка точек CDP/AIA**

```powershell
Get-CACrlDistributionPoint
Get-CAAuthorityInformationAccess
certutil -verify -urlfetch "C:\certs\fs01.cer"
```

`-urlfetch` пытается реально скачать CRL и цепочку по всем URL из сертификата. Если видите `Unable to download` — внешние клиенты этот сертификат не проверят.

**5. Локальные хранилища и запрос сертификата**

```powershell
Get-ChildItem Cert:\LocalMachine\My | Format-Table Subject, NotAfter, Thumbprint
certutil -pulse
```

`Cert:\LocalMachine\My` — личное хранилище компьютера. `certutil -pulse` вручную запускает автоенроллмент (то же, что ждать 8 часов по расписанию GPO). Путь в GUI: `certlm.msc` → Personal → All Tasks → Request New Certificate.

**6. Автоенроллмент через GPO**

```powershell
gpupdate /force
gpresult /h C:\Temp\gpresult.html
```

Настройка: `Computer Configuration → Policies → Windows Settings → Security Settings → Public Key Policies → Certificate Services Client - Auto-Enrollment`. Включаем для компьютеров и пользователей, ставим галочки «Renew expired» и «Update certificates». `gpresult` покажет, какая политика реально применилась.

**7. Установка и конфигурация AD FS (FS01)**

```powershell
Install-WindowsFeature ADFS-Federation -IncludeManagementTools
Install-AdfsFarm -CertificateThumbprint <thumbprint> `
  -FederationServiceName "adfs.corp.vektor.lab" `
  -FederationServiceDisplayName "Vektor SSO" `
  -GroupServiceAccountIdentifier "VEKTOR\gmsa_adfs$"
Get-AdfsProperties | Select-Object HttpsPort, Identifier, HostName
```

Сертификат (`-CertificateThumbprint`) должен лежать в `LocalMachine\My` с приватным ключом и EKU Server Authentication, а его CN — совпадать с `-FederationServiceName`. `-GroupServiceAccountIdentifier` — gMSA (групповая управляемая учётная запись: пароль управляется AD автоматически).

**8. Проверка TLS-эндпоинта с Linux (UBU01)**

```bash
openssl s_client -connect adfs.corp.vektor.lab:443 \
  -servername adfs.corp.vektor.lab -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null
openssl x509 -in /tmp/adfs.cer -noout -subject -issuer -dates -ext subjectAltName
```

Смотрим `Verify return code: 0 (ok)` и соответствие CN/SAN имени. `Verify return code: 20` — не найден издатель (корневой CA не в доверенных), `21` — не удалось проверить цепочку, `10` — сертификат просрочен.

**9. NPS: установка, регистрация в AD, резервная копия конфигурации**

```powershell
Install-WindowsFeature NPAS -IncludeManagementTools
netsh nps export filename="C:\nps-backup.xml" exportPSK=YES
```

Регистрация в AD — GUI: `nps.msc` → правый клик на «NPS (Local)» → **Register server in Active Directory**. Без этого NPS не прочитает Dial-in свойства из AD. `exportPSK=YES` сохраняет общие секреты — файл считать секретным.

**10. Тест RADIUS с клиента (GW01, Debian 12)**

```bash
apt install freeradius-utils
radtest ivanov 'Lab#2026-vektor' 10.10.10.10 0 'LabRadius#2026'
```

Аргументы: логин, пароль, адрес RADIUS-сервера, номер NAS-порта, общий секрет. Ответ `Received Access-Accept` — политика разрешила; `Access-Reject` — проверяйте условия политики и группу в AD.

**11. Отзыв сертификата**

```cmd
certutil -revoke 7b1f2a3c4d5e6f708192a3b4c5d6e7f8 6
certutil -crl
```

Первый аргумент — серийный номер, второй — причина (6 = CertificateHold, временная; 1 = KeyCompromise, окончательная). После `-crl` новый CRL уходит в CDP. Снятие с удержания — только через GUI `certsrv.msc` → Revoked Certificates → All Tasks → Unrevoke, и только для причины 6.

**12. Журнал NPS**

```powershell
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=6272,6273} -MaxEvents 20
```

6272 — доступ предоставлен, 6273 — отказано (внутри — reason code и имя политики). Это первое, что смотрят по любому тикету «не пускает в Wi-Fi».

## 5. Лабораторная работа

### Предусловия

Стенд уровня L2 собран: `DC01` (AD DS, DNS, DHCP), `DC02` (второй контроллер домена, урок 15), `FS01` (файлы/печать), `WIN11-CL01`, `WIN10-CL02`, `UBU01`, `ASTRA01`, `GW01` (Debian 12, 10.10.10.3 / 10.10.20.1), `WEB01`. Домен `corp.vektor.lab`, учётка `adm.student`, пароль `Lab#2026-vektor`. AD CS, AD FS, NPS не установлены.

Если `DC02` нет — сначала пройдите урок 15: Enterprise CA требует AD. Если нет `UBU01` — сойдёт `ASTRA01` (там тоже есть `openssl`).

Два сознательных отступления от продакшена, о которых вы должны помнить: **CA ставим на контроллер домена** (нет свободной ВМ, а компрометация DC = компрометация PKI) и **AD FS — на файловый сервер** (AD FS на DC не ставится [ПРОВЕРИТЬ формулировку ограничения в вашей версии]). В реальной компании под PKI выделяют отдельный сервер, корневой CA держат offline, а AD FS — на выделенном члене домена + WAP в DMZ.

### Снапшот

На хосте, в VMware Workstation: VM → Snapshot → Take Snapshot, имя `before-sysadmin-016`. Для `DC01`, `DC02`, `FS01`, `WIN11-CL01`, `GW01`. Или через `vmrun` (VMware Workstation должен быть установлен, пути — свои):

```cmd
vmrun -T ws snapshot "C:\VMs\DC02\DC02.vmx" before-sysadmin-016
```

### Шаги

1. **DC02 — установить роль AD CS.** `Install-WindowsFeature ADCS-Cert-Authority -IncludeManagementTools`. Ожидаемо: `Success: True`, перезагрузка не требуется.
2. **DC02 — сконфигурировать CA** командой `Install-AdcsCertificationAuthority` из п.1 раздела 4. Ожидаемо: `CA setup completed successfully`.
3. **DC02 — убедиться, что CA жив и CRL опубликован:** `certutil -CAInfo`, затем `certutil -crl`. Ожидаемо: `CRL published successfully`, в `pkiview.msc` зелёные галочки, кроме CDP/AIA по HTTP (их нет — это нормально для лабы, разберём в «Ошибках»).
4. **DC02 — создать шаблоны.** `certsrv.msc` → Certificate Templates → Manage → правый клик на **Web Server** → Duplicate Template → имя `Vektor-WebServer` → Security: `FS01$` (Read + Enroll), `Authenticated Users` — только Read. Потом то же для **RAS and IAS Server** → `Vektor-NPS`, Enroll для `DC01$`. В `certsrv.msc` → Certificate Templates → New → Certificate Template to Issue — опубликовать оба.
5. **DC01 — включить автоенроллмент GPO.** Создать GPO `PKI-Autoenrollment`, привязать к домену, включить Computer и User Auto-Enrollment. Затем на `FS01`, `DC01`, `WIN11-CL01`: `gpupdate /force`, `certutil -pulse`. Ожидаемо: на `WIN11-CL01` в `certlm.msc` → Personal появился сертификат пользователя/компьютера от `Vektor-Root-CA`.
6. **DC01 — DNS-запись для AD FS:** `Add-DnsServerResourceRecordA -Name "adfs" -ZoneName "corp.vektor.lab" -IPv4Address "10.10.10.20"`. Проверка: `nslookup adfs.corp.vektor.lab`.
7. **FS01 — запросить TLS-сертификат.** `certlm.msc` → Personal → All Tasks → Request New Certificate → `Vektor-WebServer` → в свойствах указать Subject name `adfs.corp.vektor.lab`, DNS SAN `adfs.corp.vektor.lab`, приватный ключ экспортируемый — нет. Ожидаемо: сертификат со статусом «This certificate is valid».
8. **DC01 — подготовить gMSA для AD FS:** `Add-KdsRootKey -EffectiveTime ((Get-Date).AddHours(-10))`, затем `New-ADServiceAccount -Name "gmsa_adfs" -DNSHostName "adfs.corp.vektor.lab" -PrincipalsAllowedToRetrieveManagedPassword "FS01$"`. Ожидаемо: объект создан, ошибок KDS нет.
9. **FS01 — установить и сконфигурировать AD FS** командами из п.7 раздела 4 (thumbprint — из п.7 шага 7). Ожидаемо: `The configuration of the first federation server in the farm completed successfully`.
10. **UBU01 — проверить TLS:** `openssl s_client` из п.8 раздела 4. Ожидаемо: `Verify return code: 0 (ok)`. Если 20 — на UBU01 не установлен корневой CA: `sudo cp vektor-root-ca.cer /usr/local/share/ca-certificates/ && sudo update-ca-certificates`.
11. **DC01 — поднять NPS.** `Install-WindowsFeature NPAS -IncludeManagementTools`, зарегистрировать сервер в AD, в `certlm.msc` запросить сертификат по шаблону `Vektor-NPS`. В `nps.msc` → RADIUS Clients → New: `GW01`, адрес `10.10.10.3`, общий секрет `LabRadius#2026`. Network Policies → New: условие `User Groups = VEKTOR\GG-Buh`, Grant Access, Authentication Methods: MS-CHAPv2 **и** PAP (только для теста, потом убрать).
12. **GW01 — тест:** `radtest` из п.10 раздела 4 с `ivanov` → `Access-Accept`; с `petrova` (она в `GG-HR`) → `Access-Reject`. Ожидаемо: ровно так.
13. **DC01 — убрать PAP** из политики, оставить только EAP/MS-CHAPv2, экспортировать конфиг через `netsh nps export`.

### Проверка

- [ ] `certutil -CAInfo` на DC02 показывает `Vektor-Root-CA`, `Enterprise Root CA`.
- [ ] В `pkiview.msc` нет красных крестов, кроме HTTP-CDP/AIA (осознанно).
- [ ] `certutil -view -restrict "Disposition=20"` показывает сертификаты для `FS01`, `DC01`, `WIN11-CL01`.
- [ ] `Get-ChildItem Cert:\LocalMachine\My` на FS01 содержит сертификат с CN `adfs.corp.vektor.lab` и приватным ключом.
- [ ] `nslookup adfs.corp.vektor.lab` с WIN11-CL01 отвечает `10.10.10.20`.
- [ ] `Get-AdfsProperties` на FS01 отдаёт `HttpsPort 443`.
- [ ] `openssl s_client` с UBU01: `Verify return code: 0 (ok)`.
- [ ] `radtest` для `ivanov` — Accept, для `petrova` — Reject.
- [ ] В Security на DC01 есть события 6272/6273 с вашими тестами.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** ivanov / WIN11-CL01
> **Что написал:** «Опять Wi-Fi: "Windows не удалось проверить подлинность сервера". Утром пускало, сейчас нет. Переговорка, гости, срочно».
> **Срочность:** высокая

**Воспроизводим поломку.** На DC02 отзываем сертификат NPS-сервера с причиной CertificateHold и публикуем CRL:

```cmd
certutil -view -restrict "RequesterName=DC01$" -out "SerialNumber,CommonName"
certutil -revoke <серийный номер> 6
certutil -crl
```

**Диагностика по порядку** (от клиента к CA — так быстрее отсекать):

1. `Get-Service IAS` на DC01 — служба NPS вообще жива? (Если упала — это не сертификат.)
2. Событие 6273 на DC01: `Get-WinEvent -FilterHashtable @{LogName='Security'; Id=6273} -MaxEvents 5`. В тексте — reason code. Код, связанный с цепочкой/отзывом, сразу сужает поиск.
3. На DC01: `Get-ChildItem Cert:\LocalMachine\My | fl Subject,NotAfter,Thumbprint` — сертификат на месте, не истёк?
4. `certutil -verify -urlfetch <путь к .cer>` на DC01 — цепочка и CRL скачиваются? Если `Revocation status: revoked` — вы нашли причину.
5. На DC01 в `certsrv.msc` → Issued Certificates → найти сертификат DC01 → Status = Revoked. Подтверждение.
6. Проверить, что клиент вообще видит корневой CA: на WIN11-CL01 `certlm.msc` → Trusted Root Certification Authorities — есть ли `Vektor-Root-CA`.

**Решение.** Снять с удержания: `certsrv.msc` → Revoked Certificates → правый клик → All Tasks → Unrevoke → `certutil -crl`. Если причина была не CertificateHold (например, скомпрометированный ключ) — перевыпустить сертификат на DC01 и заново выбрать его в EAP-профиле NPS (при замене сертификата профиль иногда держит старый отпечаток `[ПРОВЕРИТЬ на своей сборке]`).

**Ответ пользователю:** «Иван, проблема была на стороне сервера аутентификации — отозванный сертификат, мы его восстановили. Переподключитесь к Wi-Fi: нажмите "Забыть сеть" и подключитесь заново. Если попросит — перезагрузите ноутбук. Через 10 минут проверю, что у вас всё работает». **Закрытие:** указать в тикете корневую причину (отзыв сертификата NPS), действие (unrevoke + публикация CRL) и профилактику — алерт в MON01 на «сертификат NPS истекает/отозван».

### Что изменилось на стенде

- **DC02:** роль AD CS, Enterprise Root CA `Vektor-Root-CA` (RSA 4096, SHA256), шаблоны `Vektor-WebServer`, `Vektor-NPS`, опубликован CRL в LDAP-CDP.
- **DC01:** GPO `PKI-Autoenrollment` на домене; DNS A-запись `adfs` → 10.10.10.20; роль NPS (`NPAS`); NPS зарегистрирован в AD; RADIUS-клиент `GW01` (10.10.10.3); сертификат по шаблону `Vektor-NPS`; сетевые политики `Vektor-Buh-WiFi`.
- **FS01:** ферма AD FS (`adfs.corp.vektor.lab`), TLS-сертификат на CN `adfs.corp.vektor.lab`, работает под gMSA.
- **Новый объект AD:** gMSA `gmsa_adfs$`, корневой ключ KDS на DC01.
- **GW01:** пакет `freeradius-utils`, RADIUS-клиент с секретом `LabRadius#2026` (учебный, в журнал секретов стенда).
- **WIN11-CL01 / WIN10-CL02:** автоенроллнутые сертификаты, корневой CA в доверенных.
- **Пароли/секреты стенда:** добавлен `LabRadius#2026` — только для лаборатории.

### Откат

VMware Workstation → VM → Snapshot Manager → выбрать `before-sysadmin-016` → **Revert**. Или:

```cmd
vmrun -T ws revertToSnapshot "C:\VMs\DC02\DC02.vmx" before-sysadmin-016
```

**Важно:** откатывать надо **все** затронутые ВМ (DC01, DC02, FS01, WIN11-CL01, GW01), и делать это до того, как домен «разъехался». Откат контроллера домена на снапшот в реальной среде — почти гарантированный USN rollback (см. ниже). В лабе — допустимо, но после отката проверьте `repadmin /replsummary`.

## 6. Ошибки новичков и безопасность

1. **CA на контроллере домена / на файловом сервере «потому что так проще»** → компрометация сервера = компрометация всего PKI: атакующий выпускает себе сертификат на любое имя и живёт в сети годами. Правильно: отдельный сервер (в идеале — offline root + онлайн issuing + HSM).
2. **Корневой CA работает онлайн и стоит в домене** → один фишинг админа и у вас чужой рут. Правильно: Standalone offline root, выключен, ключ на защищённом носителе/HSM, церемония подписи с двумя людьми.
3. **Дубликат шаблона с Enroll для `Authenticated Users` и возможностью указать произвольный SAN** → любой сотрудник выпускает себе сертификат на `adfs.corp.vektor.lab` или на имя коллеги. Правильно: Enroll только для конкретных групп/компьютеров, SAN — только из AD, «Supply in the request» запрещён.
4. **CDP/AIA только по LDAP** → внешние партнёры и гости не проверят сертификат, корпоративный портал на стороннем хостинге не заработает. Правильно: HTTP-точка CDP/AIA в DMZ (у нас — `WEB01`, отдаёт CRL через nginx; трафик из LAN идёт через `GW01`, не забудьте правило в nftables).
5. **Просроченный CRL** → клиенты отвергают даже валидные сертификаты, падает весь 802.1X. Правильно: мониторинг срока CRL в Zabbix (MON01) и алерт за 48 часов до истечения.
6. **Забыли убрать PAP из тестовой политики NPS** → пароли уходят по RADIUS открытым текстом. Правильно: тестовая политика живёт минуты, потом удаляется.
7. **Самоподписанный сертификат на AD FS «чтобы быстрее»** → предупреждения в браузерах, пользователи приучаются игнорировать ошибки TLS. Правильно: только сертификат от своего PKI.
8. **Откат снапшота DC после работы** → USN rollback: домен тихо ломается, репликация встаёт, часть учёток не аутентифицируется. Правильно: снапшоты ВМ — только для лабораторий, в бою — резервная копия состояния системы (System State / Bare Metal Recovery) и проверенный план восстановления.

**Безопасность.** Никогда не отключайте на клиентах проверку сертификата сервера («Не проверять сертификат сервера» в свойствах Wi-Fi) — это и есть основная защита от поддельной точки доступа. Не выдавайте права Enroll на шаблоны через группу `Domain Users`. Не храните экспорт `netsh nps export exportPSK=YES` на общем диске. В РФ-реалиях помните про импортозамещение: ГОСТ-криптография — это КриптоПро CSP и КриптоПро УЦ `[ПРОВЕРИТЬ актуальную линейку версий]`, а в Astra Linux роль связки «AD CS + NPS» частично закрывают ALD Pro/FreeIPA и связка с КриптоПро. Это платные продукты, учебных версий обычно нет — планируйте бюджет.

## 7. Вопросы на собеседовании

**1. Чем аутентификация по сертификату лучше пароля и что нужно, чтобы заработал EAP-TLS?**
Ответ: пароль можно подобрать, перехватить, передать коллеге; ключ в защищённом хранилище (TPM) не копируется, и утечка базы хешей бесполезна. Для EAP-TLS нужны: рабочий PKI, серверный сертификат на NPS с EKU Server Authentication, клиентские сертификаты с EKU Client Authentication (обычно автоенроллментом через GPO), доверие клиента к издателю серверного сертификата, доступность CRL/OCSP с обеих сторон и опубликованный root в NTAuth (для сценариев входа по сертификату).
*Что хочет услышать:* что вы понимаете двусторонность проверки и что «сертификаты» без автоенроллмента, CRL и доверия — не работают.
*Красный флаг:* «EAP-TLS — это когда по сертификату в VPN» без единого слова о CRL и шаблонах.

**2. Что такое CRL и OCSP, что будет, если точка публикации недоступна?**
Ответ: CRL — подписанный список отозванных сертификатов, публикуется по расписанию; OCSP — онлайн-запрос статуса по HTTP. Если клиент не смог получить CRL или OCSP-ответ и при этом не может проверить, что список не устарел, — по RFC 5280 проверка цепочки формально считается неуспешной (в Windows поведение настраивается политикой «Revocation»). На практике: сертификат валиден, но аутентификация падает, и в журнале будет что-то про цепочку. Лечится тем, что точка CDP/AIA вынесена в DMZ, доступна и мониторится.
*Что хочет услышать:* связь «недоступная CRL = отказ в доступе» и понимание, что это надо мониторить.
*Красный флаг:* «CRL — это когда отзывают, к доступности отношения не имеет».

**3. Чем AD FS отличается от обычной аутентификации в AD и зачем он нужен?**
Ответ: AD аутентифицирует пользователя для внутренних ресурсов по Kerberos, и приложение внутри домена само проверяет билет. AD FS нужен, когда приложение **вне домена** и не умеет Kerberos: пользователь аутентифицируется в AD FS (через браузер, Kerberos IWA, форму или сертификат), AD FS выпускает подписанное утверждение (claims) и отдаёт его приложению по SAML 2.0/WS-Fed/OIDC. Приложение ничего не знает о пароле — оно доверяет подписи AD FS. Плюс — единый вход в десятки внешних сервисов и единая точка политики (MFA, правила claim).
*Что хочет услышать:* «федерация = доверие между доменами безопасности, приложение не видит пароль» и знание, что AD FS не заменяет AD, а надстраивается.
*Красный флаг:* «AD FS — это когда AD в облаке».

**4. Как NPS принимает решение, пустить пользователя или нет?**
Ответ: сначала применяются Connection Request Policies (локально или проксировать), затем Network Policies в порядке возрастания number: политика должна совпасть по условиям (группа AD, тип NAS-порта, день недели), затем в ней смотрится «Grant/Deny» и ограничения (время сессии, VLAN через RADIUS-атрибуты, EAP-метод). Первая совпавшая политика выигрывает. Плюс учитывается Dial-in свойство учётной записи в AD («Control access through NPS Network Policy» по умолчанию). Есть ещё «Default» политики, которые часто и срабатывают вместо вашей, — классическая причина «не пускает, а политика правильная».
*Что хочет услышать:* порядок обработки и понимание, что политики не «складываются», а срабатывает первая подходящая.
*Красный флаг:* «NPS смотрит в AD и пускает по паролю».

**5. Как бы вы спроектировали PKI для компании на 500 человек?**
Ответ: двухуровневая иерархия. Корневой CA — Standalone, offline, ключ на HSM или защищённом носителе, срок жизни 15–20 лет, включается только для подписи CSR выдающего CA и обновления CRL. Выдающий CA — Enterprise, в домене, 2 экземпляра (или один + план восстановления), срок жизни 5–10 лет. Точки CDP/AIA на HTTP в DMZ, OCSP-респондер, мониторинг сроков CRL и сертификатов. Шаблоны: минимальные права Enroll, автоенроллмент через GPO для рабочих мест и пользователей, отдельные шаблоны для серверов и для VPN/NPS. Резервные копии CA (ключ + БД через `Backup-CARoleService`) после каждой смены конфигурации. Документация: кто держит ключ, как проходит церемония подписи, что делать при утечке ключа CA.
*Что хочет услышать:* offline root, двухуровневость, бэкапы ключа, мониторинг и регламент — а не «поставлю Enterprise Root CA на DC».
*Красный флаг:* «один CA на DC, зачем сложности».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Active Directory Certificate Services»: статьи «PKI design» / «Certificate Services architecture», «Certificate templates», «Revocation and CRL publishing». Поиск: `AD CS design two-tier PKI learn`.
- **learn.microsoft.com** — «Active Directory Federation Services»: «AD FS design guide», «AD FS deployment», «Claims concepts». Поиск: `AD FS claims relying party trust document`.
- **learn.microsoft.com** — «Network Policy Server»: «Network Policies», «RADIUS clients and EAP», «NPS best practices». Поиск: `NPS network policy processing order learn`.
- **learn.microsoft.com** — «Windows Server deprecated features»: проверьте текущий статус NPS и AD FS в новых версиях `[ПРОВЕРИТЬ]`. Поиск: `Windows Server deprecated features`.
- **ietf.org** (RFC Editor) — RFC 2865/2866 (RADIUS), RFC 5280 (X.509/PKI), RFC 6614 (RadSec). Поиск: `RFC 5280 certificate path validation`.
- **openssl.org** — документация `s_client`, `x509`, `verify`. Поиск: `openssl s_client verify return code`.
- **freeradius.org** — документация по утилитам `radtest`/`radclient`. Поиск: `freeradius radtest usage`.
- **cryptopro.ru** — КриптоПро CSP и КриптоПро УЦ: ГОСТ TLS и сертификаты для РФ-контура `[ПРОВЕРИТЬ актуальные названия продуктов и версий]`.
- **astralinux.ru** — документация Astra Linux и ALD Pro: аналог домена и PKI-интеграции `[ПРОВЕРИТЬ раздел]`.
