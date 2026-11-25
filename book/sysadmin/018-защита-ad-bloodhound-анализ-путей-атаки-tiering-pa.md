# Защита Active Directory: BloodHound, анализ путей атаки, Tiering, PAW, Protected Users, LAPS

## 1. Введение (зачем это нужно в реальной работе)

**Заявка (тикет #4471).** Пользователь: `petrova` / ВМ `WIN10-CL02` / Текст: «Мне пришло письмо от имени ИТ: "Подтвердите учётные данные для переезда почты на новый сервер", я перешла по ссылке и ввела пароль. Теперь браузер сам открывает какие-то страницы. Срочность: высокая.»

Дальше начинается то, ради чего написана эта глава. Пароль обычного HR-специалиста утёк — казалось бы, что с него взять? Но у `petrova` есть сессия на `WIN10-CL02`, а на этой же машине неделю назад админ (`adm.student`) подключался к принтеру, и его Domain Admin-билет остался в памяти LSASS. Атакующий с паролем `petrova` заходит на рабочку, поднимает права до локального админа, снимает дамп LSASS — и вот он уже Domain Admin. Никакого взлома «по-хакерски», только легальные права, выданные годами «чтобы работало».

Защита AD — это не про антивирус на контроллере домена. Это про то, чтобы граф связей «кто куда может дойти» был коротким и контролируемым. В этой главе вы научитесь: собирать и читать граф атак (BloodHound), разделять административные полномочия по уровням (Tiering), заводить выделенное рабочее место администратора (PAW), закрывать самые частые способы кражи учётных данных (`Protected Users`) и убирать «одинаковый локальный пароль на всех ПК» через LAPS. Всё — на стенде «Вектор», с реальными командлетами и с честными предупреждениями, где лаборатория расходится с продакшеном.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Путь атаки (attack path)** — это цепочка легальных связей в AD, по которой низкопривилегированная учётка доходит до Tier 0 (контроллеры домена, `Domain Admins`, `krbtgt`, ADCS, Azure/Entra-коннекторы). Каждый шаг — не уязвимость, а ваша же настройка: членство в группе, ACE в ACL, живая сессия, делегирование, SPN.

**Уровни модели tiering (классика Microsoft, сейчас переименована в Enterprise Access Model):**

| Уровень | Что входит | Кто администрирует |
|---|---|---|
| Tier 0 | DC01, DC02, сам домен, `Domain Admins`, `krbtgt`, PKI (в стенде — CA из урока 16), системы управления (PVE01, MON01 как средство управления — спорно, но по умолчанию для сисадмина это Tier 0) | `adm.t0` только с PAW01 |
| Tier 1 | FS01, SRV1C01, файловые/печатные сервисы | `adm.student` с PAW01 или с серверов Tier 1 |
| Tier 2 | WIN11-CL01, WIN10-CL02, рабочие станции, UBU01, ASTRA01 | `adm.student` (helpdesk-часть) |

Правило простое: **учётка Tier N заходит только на машины Tier N и ниже, и никогда — на машины выше.** Учётка Tier 0 не должна вообще касаться рабочих станций.

**Что реально собирает BloodHound (коллектор SharpHound):**

- LDAP к DC (389/636, GC 3268) — пользователи, группы, OU, GPO, ACL (`nTSecurityDescriptor`), SPN, делегирование, `userAccountControl`.
- SMB (445) + RPC (135 + динамика) + WinRM (5985/5986) к каждой машине — локальные группы администраторов, RDP-группы, кто сидит в сессии (`net session`, реестр `HKCU` в профилях), дисковые ресурсы.
- Результат — JSON/ZIP, который заливается в базу BloodHound и превращается в граф.

**Типовые рёбра графа, которые надо знать наизусть:** `MemberOf`, `AdminTo`, `HasSession`, `CanRDP`, `ExecuteDCOM`, `GenericAll`, `WriteDACL`, `Owns`, `ForceChangePassword`, `AddKeyCredentialLink` (shadow credentials), `WriteSPN` (targeted kerberoasting), `AllowedToDelegate`, `DCSync` (права `Replicating Directory Changes All`), `ReadLAPSPassword`.

```text
                  ┌───────────────────────── Tier 0 ─────────────────────────┐
                  │  DC01 (10.10.10.10)   krbtgt   Domain Admins   PKI       │
                  └────────────▲────────────────────────────────────────────┘
                               │ DCSync / GenericAll / сессия DA
                  ┌────────────┴────────── Tier 1 ───────────────────────────┐
                  │  FS01 (10.10.10.20)   SRV1C01                            │
                  └────────────▲────────────────────────────────────────────┘
                               │ AdminTo / HasSession / ACE на OU
                  ┌────────────┴────────── Tier 2 ───────────────────────────┐
   [petrova]──────│──HasSession──► WIN10-CL02 ◄──HasSession──[adm.student]  │
   [ivanov]───────│──AdminTo─────► WIN11-CL01                               │
                  └──────────────────────────────────────────────────────────┘

   Красная цепочка: petrova → WIN10-CL02 (локальный админ) → дамп LSASS
                    → TGT/TGS adm.student → DCSync → весь домен
```

**Protected Users** — группа, члены которой получают жёсткие ограничения на уровне протокола аутентификации: только Kerberos (NTLM, Digest и CredSSP запрещены), запрещён RC4/DES в pre-auth (только AES), запрещено делегирование (любое), TGT живёт максимум 4 часа, кэш учётных данных не сохраняется. Требование — функциональный уровень домена Windows Server 2012 R2 и выше (у нас 2022, порядок).

**Windows LAPS** — встроенный (не путать с legacy-«AdmPwd» от Microsoft) механизм управления паролем локального администратора. Пароль уникален на каждой машине, ротируется по расписанию, хранится в AD в атрибутах `msLAPS-*` на объекте компьютера, читается только тем, кому выдано право на чтение. Плюс режим шифрования через DPAPI-NG: пароль в каталоге лежит зашифрованным и расшифровывается только членами разрешённой группы.

**PAW (Privileged Access Workstation)** — отдельная, максимально урезанная машина, с которой только и делаются админские действия. Не «мой ноутбук, где ещё и почта». На стенде это будет `PAW01`.

## 3. Аналогия из жизни

Аэропорт. `Tier 2` — общий зал ожидания: там толпа, досмотр поверхностный, кто угодно с билетом. `Tier 1` — зона для персонала за турникетом: туда пускают по служебному пропуску, но не в диспетчерскую. `Tier 0` — вышка управления полётами: попасть туда можно только по отдельному пропуску, и человек, который весь день ходил по общему залу в уличной обуви, туда не заходит. PAW — это служебный коридор со шлюзом: перед входом в вышку ты оставляешь верхнюю одежду в шкафчике и надеваешь стерильный халат. LAPS — уникальный ключ от каждой служебной двери вместо одного универсального, который «удобно» носили все и который кто-то однажды сфотографировал. BloodHound — схема аэропорта с нарисованными стрелками: «из общего зала через грузовой лифт можно попасть в вышку, потому что дверь грузового лифта забыли замкнуть».

Где аналогия ломается: в аэропорту зоны разделены стенами физически, а в AD границы задаются только ACL и членством в группах — одна забытая ACE (`GenericAll` на OU, выданная «на недельку» в 2019-м) стирает любую стену, и никто этого не заметит, пока не построишь граф.

## 4. Практика и команды

Все команды — от имени `adm.student` на DC01 (или с PAW01 с модулем RSAT), если не указано иное.

**1. Кто у нас Domain Admin (базовая инвентаризация).**
```powershell
Get-ADGroupMember -Identity "Domain Admins" -Recursive |
    Select-Object SamAccountName, objectClass, distinguishedName
```
`-Recursive` разворачивает вложенные группы — именно так находятся «админы по наследству». Смотреть: есть ли в списке сервисные учётки, уволенные, учётки без человека.

**2. Состав Protected Users.**
```powershell
Get-ADGroupMember -Identity "Protected Users" | Select-Object SamAccountName
```
Пусто — нормальное стартовое состояние стенда.

**3. Компьютеры с неограниченным делегированием (главный подарок атакующему).**
```powershell
Get-ADComputer -LDAPFilter '(userAccountControl:1.2.840.113556.1.4.803:=524288)' `
    -Properties TrustedForDelegation | Select-Object Name, TrustedForDelegation
```
`1.2.840.113556.1.4.803:=524288` — побитовое «И» по флагу `TRUSTED_FOR_DELEGATION`. Любая машина в этом списке позволяет забрать TGT любого, кто к ней подключился. На стенде должно быть пусто.

**4. Учётки с SPN — кандидаты на Kerberoasting.**
```powershell
Get-ADUser -LDAPFilter '(servicePrincipalName=*)' -Properties ServicePrincipalName, PasswordLastSet |
    Select-Object SamAccountName, ServicePrincipalName, PasswordLastSet
```
Если пароль сервисной учётки слабый — его подберут оффлайн по запрошенному TGS. Лечится gMSA или минимум 25+ символами.

**5. Учётки без Kerberos pre-auth — кандидаты на AS-REP roasting.**
```powershell
Get-ADUser -LDAPFilter '(userAccountControl:1.2.840.113556.1.4.803:=4194304)' `
    -Properties DoesNotRequirePreAuth | Select-Object SamAccountName
```
Флаг `DONT_REQ_PREAUTH` (4194304). В норме список пуст.

**6. Кто имеет DCSync-права на домен.**
```cmd
dsacls "DC=corp,DC=vektor,DC=lab" | findstr /i "Replicating"
```
Смотреть ACE с `Replicating Directory Changes` и `Replicating Directory Changes All`. Должны быть только `Domain Controllers`, `Enterprise Domain Controllers` и `Domain Admins`. Любая пользовательская группа здесь — критично.

**7. Сбор графа коллектором SharpHound (с PAW01 или WIN11-CL01).**
```powershell
.\SharpHound.exe -c All,GPOLocalGroup -d corp.vektor.lab --zipfilename vektor-018.zip
```
`-c All` — все методы сбора (`Group`, `ACL`, `Session`, `LoggedOn`, `LocalAdmin`, `RDP`, `DCOM`, `PSRemote`, `Trusts`, `Container`, `CertServices`); `--zipfilename` — имя архива. Сбор `Session`/`LocalAdmin` требует прав администратора на целевых машинах. Полный набор флагов уточняйте в документации версии коллектора — они менялись `[ПРОВЕРИТЬ]`.

**8. Альтернатива с Linux (UBU01): `bloodhound-python`.**
```bash
bloodhound-python -u adm.student -p 'Lab#2026-vektor' -d corp.vektor.lab \
  -ns 10.10.10.10 -c All --zip
```
`-ns` — DNS-сервер (наш DC01), `--zip` — упаковать результат. Удобно, когда под рукой только Linux-админка.

**9. Разворачиваем BloodHound CE на UBU01.**
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2   # [ПРОВЕРИТЬ имена пакетов в 24.04]
mkdir -p ~/bhce && cd ~/bhce
# официальный docker-compose.yml BloodHound CE кладём сюда (см. раздел 8)
sudo docker compose up -d
sudo docker compose logs | grep -i password   # стартовый пароль admin [ПРОВЕРИТЬ]
```
UI слушает порт 8080 `[ПРОВЕРИТЬ]`. Открываем `http://10.10.10.30:8080`, логин `admin`, меняем пароль, загружаем ZIP.

**10. Ищем путь от petrova до Domain Admins (Cypher-запрос в BloodHound → вкладка Explore).**
```cypher
MATCH p=shortestPath((u:User {name:'PETROVA@CORP.VEKTOR.LAB'})-[*1..]->(g:Group {name:'DOMAIN ADMINS@CORP.VEKTOR.LAB'}))
RETURN p
```
Имена узлов в BloodHound — всегда ВЕРХНИМ регистром с доменом. Если путь пустой — отлично; если нет — смотрим каждое ребро и убиваем его.

**11. Включаем Protected Users (осторожно, см. раздел 6).**
```powershell
Add-ADGroupMember -Identity "Protected Users" -Members "adm.student","adm.t0"
Get-ADGroupMember "Protected Users"
```
Проверка эффекта: на DC01 в Security-логе появляются 4625/4771 при попытке NTLM-аутентификации такой учётки.

**12. Готовим LAPS: расширяем схему и раздаём права.**
```powershell
# один раз на лес, нужны Schema Admins + Enterprise Admins, действие необратимо
Update-LapsADSchema -Confirm

# компьютеры в OU Компьютеры получают право писать свой пароль в AD
Set-LapsADComputerSelfPermission -Identity "OU=Компьютеры,DC=corp,DC=vektor,DC=lab"

# читать пароли может только GG-IT
Set-LapsADReadPasswordPermission -Identity "OU=Компьютеры,DC=corp,DC=vektor,DC=lab" `
    -AllowedPrincipals "GG-IT"
```

**13. Читаем локальный пароль машины.**
```powershell
Get-LapsADPassword -Identity WIN11-CL01 -AsPlainText |
    Select-Object Account, Password, ExpirationTimestamp, Source
```
`-AsPlainText` расшифровывает. Каждое чтение логируется в событие 4662 на DC — это ваша доказательная база «кто смотрел пароли».

**14. Проверяем, кто локальный админ на рабочке (с PAW01).**
```powershell
Invoke-Command -ComputerName WIN11-CL01 -ScriptBlock { Get-LocalGroupMember -Group "Администраторы" }
```
Здесь не должно быть `adm.student`, `Domain Admins` и доменных групп Tier 0.

## 5. Лабораторная работа

### Предусловия
Готовый стенд L2: DC01 (AD DS, DNS, DHCP), DC02 (второй контроллер), FS01, WIN11-CL01, WIN10-CL02, UBU01, домиен `corp.vektor.lab`, группы `GG-Buh/GG-HR/GG-Sklad/GG-IT`, папки на FS01. Работаем на уровне sysadmin, поэтому **добавляем одну новую ВМ — `PAW01`** (Windows 11 Enterprise Evaluation, статический адрес **10.10.10.80**, введена в домен, объект в OU `Компьютеры`). Также создаём: глобальную группу `GG-T0-Admins`, учётку `adm.t0` (Tier 0, пароль стенда), OU `Company\IT\PAW` для политик. PVE01 в этой лабораторной не нужен.

### Снапшот
Сначала создаём и настраиваем `PAW01` (адрес, ввод в домен), затем — снапшот `before-sysadmin-018` на **DC01, DC02, FS01, WIN11-CL01, WIN10-CL02, UBU01, PAW01**. В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot → имя `before-sysadmin-018`.

### Шаги

1. **DC01.** Инвентаризация Tier 0: `Get-ADGroupMember "Domain Admins" -Recursive`, `Get-ADGroupMember "Enterprise Admins"`. Ожидаемо: `Administrator`, `adm.student`. Записываем в `stand-log.md`.
2. **DC01.** Проверяем «дыры» командами 3–6 из раздела 4 (делегирование, SPN, pre-auth, DCSync). Ожидаемо на чистом стенде: списки пустые или почти пустые, DCSync — только штатные группы.
3. **UBU01.** Ставим Docker и BloodHound CE (команда 9). Ожидаемо: `docker compose ps` показывает работающие контейнеры, UI открывается на `http://10.10.10.30:8080`.
4. **UBU01.** `sudo ufw allow from 10.10.10.80 to any port 8080 proto tcp && sudo ufw enable`. Ожидаемо: UI доступен только с PAW01.
5. **PAW01.** Копируем SharpHound, запускаем сбор (команда 7) под `adm.student`. Ожидаемо: `vektor-018.zip` рядом с exe, в логе — «SharpHound completed».
6. **UBU01 → BloodHound.** Загружаем ZIP, ждём индексации. Затем в Explore вбиваем `PETROVA` → Search. Ожидаемо: у `petrova` есть ребро `HasSession`/`AdminTo` к `WIN10-CL02` (BloodHound покажет то, что реально было на момент сбора).
7. **PAW01.** Искусственно создаём типовую уязвимость для проверки детекта: логинимся на `WIN10-CL02` под `adm.student` (это ровно то, чего делать нельзя), не выходим, оставляем сессию.
8. **PAW01.** Повторный сбор (команда 7) и загрузка в BloodHound. Ожидаемо: появляется путь `ADM.STUDENT → HasSession → WIN10-CL02 → AdminTo → ...`, запрос из команды 10 даёт непустой граф. Это учебная демонстрация «почему DA не ходит по рабочим станциям». Выходим из сессии на WIN10-CL02.
9. **DC01.** Создаём `GG-T0-Admins`, добавляем `adm.t0`; `adm.student` остаётся DA (иначе стенд потеряет управление). Добавляем обе учётки в `Protected Users` (команда 11). Ожидаемо: команда проходит без ошибок.
10. **DC01.** Tiering-политика: создаём GPO `VEKTOR-SEC-DenyTier0OnTier2` и в `Computer Configuration → Policies → Windows Settings → Security Settings → Local Policies → User Rights Assignment` включаем «Deny log on locally» и «Deny log on through Remote Desktop Services» для `Domain Admins` и `GG-T0-Admins`, линкуем на OU `Компьютеры`. Ожидаемо: после `gpupdate /force` на WIN10-CL02 вход под `adm.student` локально блокируется с сообщением «Вход в систему в данный момент запрещён».
11. **PAW01.** Ограничиваем доступ: GPO `VEKTOR-SEC-PAW01-Logon` на OU `Company\IT\PAW` — «Allow log on locally» только `GG-T0-Admins` и локальные админы PAW01; Windows Firewall входящие — блок всех, кроме DC01/DC02 и порта 8080 к UBU01.
12. **DC01.** LAPS (команды 12). Ожидаемо: `Update-LapsADSchema` завершается со статусом Success; в схеме появляются `msLAPS-*`; на объекте `WIN11-CL01` после GPO-политики `System → LAPS` (Enable Windows LAPS = Enabled, Backup Directory = Active Directory, длина пароля 20+, сложность большая) появляется заполненный атрибут.
13. **DC01.** `Get-LapsADPassword -Identity WIN11-CL01 -AsPlainText`. Ожидаемо: выдаётся пароль и срок жизни; в Security-логе DC01 — событие 4662 на чтение атрибута.
14. **PAW01.** Проверяем, что `Get-LapsADPassword` для `adm.student` работает, а для тестовой учётки без прав — «Access denied». Записываем всё в `stand-log.md`.

### Проверка
- [ ] BloodHound CE отвечает на `http://10.10.10.30:8080`, доступен только с PAW01.
- [ ] В графе есть узлы всех 7 ВМ и учёток домена; путь `ADM.STUDENT → WIN10-CL02` виден.
- [ ] `adm.student` и `adm.t0` состоят в `Protected Users`.
- [ ] На WIN10-CL02 локальный вход `adm.student` заблокирован политикой.
- [ ] На PAW01 `ivanov`/`petrova` не могут войти.
- [ ] `Get-LapsADPassword -Identity WIN11-CL01 -AsPlainText` отдаёт пароль с `ExpirationTimestamp` в будущем.
- [ ] `Get-LapsADPassword -Identity WIN10-CL02` отдаёт **другой** пароль.
- [ ] Чтение пароля фиксируется событием 4662 на DC01.

### Сломай и почини
**Пользователь:** `adm.student` (вы сами) / **ВМ:** PAW01 / **Что написал:** «Открываю `\\10.10.10.20\Buh` — "Отказано в доступе", логин/пароль не принимает. До включения Protected Users всё работало. Срочность: критичная, не могу выдать доступ бухгалтерии.»

**Как воспроизвести:** после шага 9 обратиться к шаре FS01 **по IP** или по короткому имени `\\FS01\Buh` под `adm.student`.

**Диагностика по порядку:**
1. Проверить состав группы: `Get-ADGroupMember "Protected Users"` — учётка там.
2. Проверить, что ошибка именно в NTLM: на DC01 в Security-логе ищем 4776 (проверка NTLM-учётных данных) по `adm.student` в момент попытки; в клиентском `System`-логе — предупреждения Kerberos.
3. `klist` на PAW01 — есть ли TGT/TGS к `cifs/FS01.corp.vektor.lab`.
4. Проверить синтаксис обращения: `\\FS01.corp.vektor.lab\Buh` (FQDN) — если работает, причина подтверждена: по IP Kerberos не может построить SPN, происходит fallback на NTLM, а NTLM для Protected Users запрещён.

**Решение:** работать по FQDN (`\\FS01.corp.vektor.lab\Buh`), а для legacy-приложений, которым NTLM обязателен (MSSQL по NTLM, RADIUS/NPS из урока 16, старые 1С-клиенты), **не** добавлять эти учётки в Protected Users, а закрывать их tiering-политиками и сегментацией. Обратный вариант — вывести `adm.student` из Protected Users и оставить только `adm.t0`, который вообще не ходит на FS01.

**Ответ пользователю:** «Причина — включённая защита учётной записи: для неё разрешён только Kerberos, а обращение по IP заставляет систему использовать NTLM. Используйте `\\FS01.corp.vektor.lab\Buh`; для приложений, которым NTLM критичен, учётку в Protected Users не помещаем — согласуем список таких приложений.»

**Закрытие:** фиксируем в тикете причину, список legacy-приложений с NTLM, и что `adm.t0` используется только с PAW01.

### Что изменилось на стенде
- Новая ВМ **PAW01**: Windows 11 Enterprise Evaluation, 10.10.10.80, в домене, объект в `OU=Компьютеры`, зона Tier 0.
- Новые объекты AD: группа `GG-T0-Admins`, учётка `adm.t0`, OU `Company\IT\PAW`.
- `adm.student` и `adm.t0` — члены `Protected Users`.
- GPO `VEKTOR-SEC-DenyTier0OnTier2` (на `OU=Компьютеры`), `VEKTOR-SEC-PAW01-Logon` (на `OU=Company\IT\PAW`).
- Схема AD расширена под Windows LAPS; LAPS включён для машин в `OU=Компьютеры`, право чтения — `GG-IT`.
- UBU01: установлен Docker, запущен BloodHound CE (порт 8080, доступ только с 10.10.10.80), файрвол ufw включён.
- DC01: включён аудит доступа к атрибутам LAPS (событие 4662).

### Откат
VMware Workstation → правый клик по ВМ → Snapshot Manager → выбрать `before-sysadmin-018` → Revert. Откатывать на DC01, DC02, FS01, WIN11-CL01, WIN10-CL02, UBU01, PAW01. **Важно:** расширение схемы AD откатом ВМ не убирается — атрибуты `msLAPS-*` остаются в каталоге навсегда (они безвредны, но это след). Поэтому шаг 12 делаем осознанно.

## 6. Ошибки новичков и безопасность

1. **«Добавлю всех админов в Protected Users и забуду»** → ломаются NTLM-зависимые приложения: NPS/RADIUS, MSSQL, старые 1С-клиенты, доступ по IP. Правильно: сначала инвентаризация NTLM-зависимостей, потом группа, и обязательно тест на одном администраторе.
2. **Protected Users для сервисных учёток** → сервис не сможет аутентифицироваться (нет кэша, нет NTLM, TGT на 4 часа). Правильно: сервисы — gMSA, а Protected Users — только для людей Tier 0.
3. **«Соберу BloodHound под доменным админом, потом разберусь»** → архив содержит полную карту домена: имена, сессии, ACL. Утечка = подарок атакующему. Правильно: сбор в изолированном сегменте, файл — как секрет, доступ к UI — только с PAW01, и только чтение.
4. **Лечение симптомов вместо рёбер** → удалили одну ACE, а через месяц появилась другая. Правильно: после каждого изменения — повторный сбор и сравнение графа.
5. **Один локальный пароль админа на весь парк** → компрометация одной машины = компрометация всех. Правильно: LAPS, уникальный пароль, ротация, чтение по журналу.
6. **«LAPS включили, но право чтения дали „Все“»** → любой сотрудник читает пароль локального админа любой машины. Правильно: право чтения — только `GG-IT`, лучше с шифрованием DPAPI-NG и отдельной группой расшифровки.
7. **Tiering «на бумаге»** → политика есть, но у `adm.student` остался вход на DC01 по RDP. Правильно: проверять фактически (BloodHound + журналы 4624 type 10 на DC), а не по описанию.
8. **PAW с интернетом и почтой** → вся ценность теряется, фишинг приезжает прямо на машину Tier 0. Правильно: PAW без почты/браузера, обновления через отдельный канал.

### Безопасность
В боевой среде **нельзя**: запускать SharpHound с доменными админ-правами без письменного согласования (в РФ это ещё и вопрос 152-ФЗ/187-ФЗ и внутренних регламентов — сбор графа формально похож на действия злоумышленника); хранить ZIP и пароли LAPS в общедоступных папках вроде `\\FS01\Obshchee`; выдавать право чтения LAPS на весь домен; отключать ufw/брандмауэр «чтобы BloodHound собрал данные». На стенде мы делаем это намеренно упрощённо и на учебном пароле `Lab#2026-vektor` — в реальной работе такой пароль недопустим, как и запись пароля в текстовый файл рядом с утилитой.

## 7. Вопросы на собеседовании

**1. «Расскажите про модель Tiering в AD и зачем она нужна».**
Ответ: три уровня по «ценности» учётных данных. Tier 0 — контроллеры домена, `Domain Admins`, `krbtgt`, PKI, системы управления; Tier 1 — серверы приложений и файловые; Tier 2 — рабочие станции и устройства пользователей. Учётные данные Tier N допускаются только на машины Tier N и ниже; админ Tier 0 никогда не логинится на Tier 2. Смысл — не дать атакующему с рабочей станции собрать хеши привилегированной учётки. Реализуется через отдельные учётки на каждый уровень, GPO с «Deny log on», PAW и сегментацию. Современная версия этой модели у Microsoft — Enterprise Access Model (Control Plane / Management Plane / Workload Plane).
*Что хочет услышать:* что вы понимаете: AD плоский, а защита — про разделение credentials, а не про удаление групп.
*Красный флаг:* «Tiering = установить Windows Firewall и всё».

**2. «Что даёт группа Protected Users и чего она не делает?»**
Ответ: ограничения на уровне аутентификации — только Kerberos (NTLM, Digest, CredSSP запрещены), AES вместо RC4/DES, запрет любого делегирования, TGT не более 4 часов, нет кэширования учётных данных. Нужен функциональный уровень 2012 R2+. Не защищает от кражи TGT/TGS через дамп LSASS на машине, где сессия уже есть, не спасает от Kerberoasting, если учётка не в группе, и ломает NTLM-зависимые приложения.
*Что хочет услышать:* понимание границы возможностей и готовности к побочным эффектам.
*Красный флаг:* «Защищает от Mimikatz полностью».

**3. «Как работает BloodHound и откуда он берёт данные?»**
Ответ: коллектор (SharpHound, AzureHound, bloodhound-python) опрашивает LDAP контроллера домена (пользователи, группы, OU, GPO, ACL, SPN, делегирование) и по SMB/RPC/WinRM собирает локальные группы и сессии на машинах; результат — ZIP с JSON, который загружается в BloodHound CE и превращается в граф узлов и рёбер. Анализ — shortest path к Domain Admins, поиск Kerberoastable, DCSync, делегирования. Ограничение: сбор сессий и локальных админов требует прав на целевых машинах, а графовые данные быстро устаревают.
*Что хочет услышать:* что вы читаете граф, а не просто «нажал кнопку», и понимаете чувствительность данных.
*Красный флаг:* «BloodHound сам что-то эксплуатирует».

**4. «Зачем PAW, если у меня и так чистый ноутбук?»**
Ответ: PAW — машина, на которой нет почты, браузера, мессенджеров и офиса, с узким кругом учёток, зашедших на неё, жёсткой GPO, обновлениями из доверенного источника и сетевым доступом только к Tier 0/1. Цель — исключить путь «фишинг → пользовательский контекст → кража админских билетов». Даже «чистый ноутбук» с корпоративной почтой не PAW.
*Что хочет услышать:* понимание, что PAW — организационная мера + конфигурация, а не установка антивируса.
*Красный флаг:* «PAW = виртуалка админа на его рабочем ПК» (если с этой ВМ ходят в интернет — ценность нулевая).

**5. «Как устроен Windows LAPS и что нужно, чтобы его включить?»**
Ответ: уникальный пароль локального администратора на каждой машине, ротация по расписанию, хранение в AD на объекте компьютера в атрибутах `msLAPS-*`; схему расширяют `Update-LapsADSchema`, право компьютера писать себе пароль дают `Set-LapsADComputerSelfPermission` на OU, право чтения — `Set-LapsADReadPasswordPermission` для конкретной группы; чтение — `Get-LapsADPassword -AsPlainText`, каждое чтение аудируется (4662). Есть режим шифрования DPAPI-NG, когда пароль в каталоге зашифрован для определённой группы. Политики задаются GPO `Administrative Templates → System → LAPS`.
*Что хочет услышать:* понимание, что LAPS — это ещё и модель прав: кто читает, тот и есть главный риск.
*Красный флаг:* «LAPS пароли хранит локально в реестре» или «право чтения дал всем админам домена».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — разделы: «Protected Users Security Group», «Windows LAPS» (обзор, схема AD, политики), «Enterprise Access Model» / «Privileged Access Workstations», «Securing privileged access». Поиск: `Protected Users security group`, `Windows LAPS overview`, `Enterprise access model Microsoft`, `Privileged Access Workstation Microsoft`.
- **learn.microsoft.com** (PowerShell) — справочники `Get-LapsADPassword`, `Update-LapsADSchema`, `Set-LapsADReadPasswordPermission`, `Get-ADGroupMember`, `Get-ADUser` (LDAP-фильтры, `userAccountControl`). Поиск: `Update-LapsADSchema cmdlet reference`.
- **SpecterOps / BloodHound** — официальная документация и репозиторий: `bloodhound.readthedocs.io` `[ПРОВЕРИТЬ актуальный адрес — документация переезжала]`, `github.com/SpecterOps/BloodHound` (раздел «BloodHound Community Edition», docker-compose, SharpHound collection methods). Поиск: `BloodHound CE docker compose install`, `SharpHound collection methods`.
- **specterops.io** — блог-статьи по методологии сбора и по edge-типам графа (ACL abuse, shadow credentials, ADCS ESC-пути). Поиск: `SpecterOps blog attack path management`.
- **ubuntu.com / docs.docker.com** — установка Docker Engine и Compose на Ubuntu 24.04. Поиск: `Docker Engine install Ubuntu`, `docker compose plugin`.
- **Законы РФ** — 152-ФЗ «О персональных данных» и 187-ФЗ «О безопасности КИИ» на официальном портале `pravo.gov.ru`; практические требования удобнее смотреть в методиках ФСТЭК (`fstec.ru`, раздел «Документы»). Поиск: `187-ФЗ безопасность критической информационной инфраструктуры`, `ФСТЭК приказ 239`.
