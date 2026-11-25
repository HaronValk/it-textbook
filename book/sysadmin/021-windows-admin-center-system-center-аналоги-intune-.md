# Windows Admin Center, System Center-аналоги и Intune-подход: централизованное управление парком

## 1. Введение (зачем это нужно в реальной работе)

Тикет в конце месяца:

> **Пользователь:** petrova
> **ВМ:** WIN10-CL02
> **Что написала:** «Мария, добрый день. Опять не печатает принтер в HR, и ещё выскочило обновление Windows, которое просит перезагрузку, а у меня отчёт. Когда вы подойдёте? Я жду уже сорок минут.»
> **Срочность:** средняя

Пока парк — это пять машин, можно ходить ногами. Когда их пятьдесят, а ты один, ноги заканчиваются на третьем этаже. Дальше начинается то, из-за чего сисадмин среднего уровня превращается в senior: у него есть **плоскость управления** (management plane) — один или несколько инструментов, через которые он видит весь парк, ставит обновления, собирает инвентаризацию и делает это удалённо, не вставая с кресла.

После этой главы вы будете понимать: чем Windows Admin Center (WAC) отличается от System Center и от Intune-подхода, когда что применять, какие порты и протоколы нужны для удалённого управления, как построить «бедный Intune» из бесплатных средств на своём стенде и как ограничить эту самую мощную в мире дверь в парк, чтобы она не стала входом для атакующего.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Любая инфраструктура делится на две плоскости:

- **Data plane** — то, чем пользуются люди: файлы, печать, 1С, почта.
- **Management plane** — то, чем пользуешься ты: подключиться, посмотреть, поставить, забрать логи. В идеале management plane изолирован от data plane (см. урок 18 про Tiering и PAW).

**Windows Admin Center** — бесплатный (без лицензии) веб-инструмент Microsoft. Это **gateway**: ты ставишь службу на одну машину (у нас — FS01, в бою — выделенный jump-host), заходишь браузером на `https://fs01.corp.vektor.lab` и оттуда подключаешься к другим узлам. Сам WAC — это ASP.NET-приложение + локальная служба, которая **не ставит агентов** на управляемые машины: она ходит к ним по штатным протоколам Windows.

Протоколы, на которых всё держится:

| Что | Порт | Зачем |
|---|---|---|
| WinRM / WS-Man (HTTP) | 5985 | основной канал: PowerShell Remoting, большинство «плиток» WAC |
| WinRM / WS-Man (HTTPS) | 5986 | то же, но в TLS — так надо делать в бою |
| SMB | 445 | файлы, реестр, часть инструментов, шаринг расширений |
| RPC / WMI | 135 + динамические 49152–65535 | старые WMI-запросы, инвентаризация, часть инструментов |
| ICMP | — | статус «онлайн/офлайн» в списке подключений |
| HTTPS (сам gateway) | 443 | веб-интерфейс WAC |

WinRM — это SOAP поверх HTTP, поэтому «управляемость» узла ломается ровно так же, как любой веб-сервис: служба выключена, правило файрвола закрыто, сертификат просрочен, DNS не резолвится.

Схема стенда после этой главы:

```text
        браузер админа (WIN11-CL01, Edge)
                    │ HTTPS 443
                    ▼
      ┌───────────────────────────────────┐
      │  FS01  10.10.10.20                │
      │  роль: WAC gateway + файлы/печать │
      └───────┬───────────────┬───────────┘
     WinRM 5985│        5985   │  5985
              ▼               ▼            ▼
        ┌──────────┐   ┌────────────┐  ┌────────────┐
        │  DC01    │   │ WIN11-CL01 │  │ WIN10-CL02 │
        │ .10      │   │ DHCP       │  │ DHCP       │
        └──────────┘   └────────────┘  └────────────┘
```

**System Center** — это не один продукт, а семейство тяжёлых серверных решений Microsoft, каждое со своей базой SQL:

- **MECM (бывший SCCM)** — развёртывание ОС и ПО, патчи, инвентаризация, удалённое управление. Про развёртывание образов — урок 22.
- **SCOM** — мониторинг (аналог на стенде — Zabbix на MON01).
- **SCVMM** — управление гипервизорами Hyper-V (аналог — PVE01, только он про Proxmox).
- **SCDPM** — резервное копирование.
- **SCORCH / SCSM** — автоматизация и service desk.

**Intune-подход** — идеологически другая вещь: не «сервер, к которому ты подключаешься», а **облако, в котором машина регистрируется сама** (MDM-enrollment), получает политики (configuration profiles, compliance policies) и обновления по «кольцам» (update rings), а доступ к корпоративным ресурсам выдаётся по условиям (Conditional Access). Ключевая разница: **push-модель с состоянием** («машина должна быть такой») вместо **pull-модели с командой** («выполни вот это»).

Технически «состояние» на Windows описывается: GPO (урок 19), PowerShell DSC, а в облаке — профилями Intune. На стенде мы соберём суррогат: **WAC + GPO + WSUS + PowerShell + Zabbix**. Это ровно то, что делают в компаниях, где Intune недоступен или запрещён.

## 3. Аналогия из жизни

Представьте жилой комплекс. **WAC** — это приложение на телефоне: видишь каждый прибор по отдельности, можешь щёлкнуть выключателем конкретной лампочки в конкретной квартире. Удобно, пока приборов десяток. **System Center** — диспетчерская управляющей компании: операторы, датчики на каждом стояке, график ремонтов, журнал заявок; мощно, дорого и требует отдельной комнаты. **Intune-подход** — «умный дом»: ты не жмёшь кнопки, ты один раз пишешь правило «если холодно — включить отопление, но не выше 22», и система сама держит состояние, а жильцы могут быть в другом городе.

Аналогия перестаёт работать в одном месте: в умном доме все устройства одного вендора и говорят на одном протоколе, а реальный парк — это Windows, Astra Linux, сетевые железки и 1С-сервер, у каждого свой язык. Единой кнопки «сделать хорошо» не существует, поэтому и держат несколько инструментов сразу.

## 4. Практика и команды

**1. Установка WAC в тихом режиме (FS01, PowerShell от админа).** Скачайте `WindowsAdminCenter.msi` с сайта Microsoft в `C:\temp`.

```powershell
msiexec /i C:\temp\WindowsAdminCenter.msi /qn /L*v C:\temp\wac-install.log SME_PORT=443 SSL_CERTIFICATE_OPTION=generate
```
`/qn` — без интерфейса, `/L*v` — подробный лог, `SME_PORT` — порт gateway, `SSL_CERTIFICATE_OPTION=generate` — сделать самоподписанный сертификат. Смотреть в логе: строки `Installation success` и отсутствие `Error 1603`.

**2. Проверка, что gateway поднялся.**

```powershell
Get-Service -Name ServerManagementGateway   # имя службы уточнить: [ПРОВЕРИТЬ]
Get-NetTCPConnection -LocalPort 443 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess
```
Ожидаем `Status : Running` и `LocalAddress 0.0.0.0, LocalPort 443`. Если порт слушает только `127.0.0.1` — вы поставили WAC с привязкой к localhost.

**3. Проверка управляемости узла (с FS01).**

```powershell
Test-NetConnection WIN11-CL01 -Port 5985
Test-WSMan WIN11-CL01
```
Первый должен дать `TcpTestSucceeded : True`, второй — вернуть блок `wsmid` с `ProtocolVersion`. Ошибка `Test-WSMan : ...не удалось` = WinRM выключен или закрыт файрвол.

**4. Включение WinRM на управляемой машине (локально, в консоли ВМ).**

```powershell
Enable-PSRemoting -SkipNetworkProfileCheck -Force
Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -RemoteAddress 10.10.10.0/24 -Profile Domain
```
`-SkipNetworkProfileCheck` разрешает работу в профиле «Общедоступная сеть» (на стенде удобно, в бою — нет). Второй командой мы **сужаем** правило до офисной подсети — так и надо делать.

**5. Удалённая сессия и инвентаризация парка.**

```powershell
$cred = Get-Credential VEKTOR\adm.student
Invoke-Command -ComputerName DC01,WIN11-CL01,WIN10-CL02 -Credential $cred -ScriptBlock {
    Get-CimInstance Win32_OperatingSystem |
      Select-Object PSComputerName, Caption, Version, LastBootUpTime
} | Export-Csv C:\temp\inventory.csv -NoTypeInformation -Encoding UTF8
```
`Invoke-Command` выполняет блок на всех узлах параллельно, `PSComputerName` добавляется автоматически. Если узел недоступен — в потоке ошибок будет `Connecting to remote server ... failed`, а данные по остальным всё равно соберутся (`-ErrorAction SilentlyContinue` по вкусу).

**6. Кто в домене «мёртвый» (гигиена парка).**

```powershell
Get-ADComputer -Filter * -Properties LastLogonDate,OperatingSystem |
  Where-Object LastLogonDate -lt (Get-Date).AddDays(-60) |
  Select-Object Name,OperatingSystem,LastLogonDate | Sort-Object LastLogonDate
```
`LastLogonDate` — реплицируемый атрибут (не путать с `LastLogonTimestamp`). 60 дней без входа — кандидат на вывод из домена; так же находят машины, которые «есть в AD, но не существуют».

**7. Уровень патчей по парку.**

```powershell
Invoke-Command -ComputerName DC01,FS01,WIN11-CL01 -Credential $cred -ScriptBlock {
    Get-CimInstance Win32_QuickFixEngineering |
      Sort-Object InstalledOn -Descending | Select-Object -First 1 PSComputerName,HotFixID,InstalledOn
}
```
`Win32_QuickFixEngineering` даёт список установленных обновлений; сортировка по `InstalledOn` показывает свежесть. Учтите: некоторые обновления (например, SSuS/Defender-подписи) в этот класс не попадают.

**8. Свободное место в одном запросе.**

```powershell
Invoke-Command -ComputerName DC01,FS01 -Credential $cred -ScriptBlock {
    Get-Volume | Where-Object DriveLetter | Select-Object PSComputerName,DriveLetter,@{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}}
}
```
Классика: FS01 с забитым диском роняет шары `\\FS01\Buh` быстрее, чем ты успеваешь ответить на тикет.

**9. Массовая перезагрузка с проверкой (после обновлений).**

```powershell
Restart-Computer -ComputerName WIN10-CL02 -Credential $cred -Protocol WSMan -Wait -For PowerShell
Test-WSMan WIN10-CL02
```
`-Wait -For PowerShell` ждёт, пока на узле снова поднимется PowerShell. Без `-Protocol WSMan` на старых сборках бывают сюрпризы.

**10. Смена сертификата gateway на выданный от AD CS (если поднимали PKI в уроке 16).**

```powershell
$cert = New-SelfSignedCertificate -DnsName "fs01.corp.vektor.lab" -CertStoreLocation Cert:\LocalMachine\My
$cert.Thumbprint
msiexec /i C:\temp\WindowsAdminCenter.msi /qn SME_PORT=443 SSL_CERTIFICATE_OPTION=installed SME_THUMBPRINT=<отпечаток>   # точные имена параметров: [ПРОВЕРИТЬ]
```
Самоподписанный сертификат — только для лаборатории; в бою выпускают от внутреннего CA (или ставят `SME_CERTIFICATE` от корпоративного) `[ПРОВЕРИТЬ]`.

**11. PowerShell-модуль WAC (управление подключениями из консоли).**

```powershell
Install-Module -Name WindowsAdminCenter -Scope AllUsers   # [ПРОВЕРИТЬ наличие модуля и имена командлетов]
Get-WACConnection
```
Позволяет добавлять узлы пачками: `New-WACConnection -ComputerName ...` `[ПРОВЕРИТЬ]`. Даже если модуль недоступен, полезно знать, что WAC автоматизируется, а не только «мышкой».

**12. Проверка «целевого состояния» (Intune-подход в миниатюре).**

```powershell
gpresult /r /scope:computer
dsregcmd /status
```
`gpresult` показывает, какие GPO применились (это и есть desired state на стенде), `dsregcmd /status` — состояние join: `AzureAdJoined : NO` на стенде нормально, а в гибридной схеме (урок 17) там будет `YES` и `DomainJoined : YES`.

**13. Где GUI:** в WAC путь такой — `Connections → Add → Windows Server / Windows PC → имя или FQDN → Manage as (VEKTOR\adm.student)`. Внутри узла: `Tools` → `Overview`, `Performance monitor`, `Updates`, `Scheduled tasks`, `Services`, `PowerShell`. Инвентаризация в WAC полноценная только через расширения (например, Arc-агент или сторонние extension'ы) — «из коробки» там нет отчётов по парку.

## 5. Лабораторная работа

### Предусловия
- DC01 (AD DS, DNS, DHCP), FS01 (файлы/печать), WIN11-CL01, WIN10-CL02 — все в домене, вход `VEKTOR\adm.student`.
- FS01 доступен по имени `fs01.corp.vektor.lab` с клиентских машин.
- Скачанный `WindowsAdminCenter.msi` (легально, бесплатно, с сайта Microsoft). Доступность загрузки из РФ изредка бывает проблемной — тогда legality-альтернатива: тот же результат собирается на PowerShell Remoting + RSAT (пункты 3–9 раздела 4), а WAC пропускается.

### Снапшот
На FS01, DC01, WIN11-CL01, WIN10-CL02: `VM → Snapshot → Take Snapshot`, имя **`before-sysadmin-021`**.

### Шаги
1. **DC01.** `Resolve-DnsName fs01.corp.vektor.lab` → ожидаем A-запись `10.10.10.20`. Если пусто — сначала чиним DNS (иначе WAC будет «не видеть» узлы).
2. **FS01.** Из консоли: `Enable-PSRemoting -Force`, затем `Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -RemoteAddress 10.10.10.0/24 -Profile Domain` → вывод без ошибок.
3. **FS01.** Установка WAC: `msiexec /i C:\temp\WindowsAdminCenter.msi /qn /L*v C:\temp\wac-install.log SME_PORT=443 SSL_CERTIFICATE_OPTION=generate` → в логе `success`.
4. **FS01.** `Get-Service -Name ServerManagementGateway` [ПРОВЕРИТЬ] и `Get-NetTCPConnection -LocalPort 443 -State Listen` → служба Running, порт слушается.
5. **WIN11-CL01.** В браузере Edge: `https://fs01.corp.vektor.lab` → предупреждение о самоподписанном сертификате (ожидаемо) → «Дополнительно → Перейти». Вход `VEKTOR\adm.student` / `Lab#2026-vektor`. Видим пустой список Connections.
6. **WIN11-CL01 (браузер).** `+ Add → Servers → DC01.corp.vektor.lab`. В диалоге выбрать **Manage as** и ввести `VEKTOR\adm.student` (иначе из браузера на другой машине потребуется настраивать Kerberos constrained delegation). Ожидаем: DC01 в списке, статус Online.
7. **WIN11-CL01 (браузер).** Добавить так же `WIN11-CL01` и `WIN10-CL02`. Если WIN10-CL02 красный — идём в консоль этой ВМ: `Enable-PSRemoting -SkipNetworkProfileCheck -Force` + правило файрвола из раздела 4 (пункт 4). Ожидаем оба узла Online.
8. **WIN11-CL01 (браузер).** Открыть DC01 → `Tools → PowerShell` → выполнить `Get-ADDomain | Select-Object Name,DomainMode`. Ожидаем `corp.vektor.lab`.
9. **WIN11-CL01 (браузер).** Открыть WIN11-CL01 → `Tools → Performance monitor`, добавить `% Processor Time`, затем `Tools → Scheduled tasks` — убедиться, что видно задачи клиента. Это доказательство, что канал WinRM реально работает, а не просто «зелёный статус».
10. **FS01.** Собрать инвентаризацию в файл (раздел 4, пункт 5) → `C:\temp\inventory.csv` открывается, внутри 3 строки с `Caption`.
11. **FS01.** `Get-ADComputer -Filter * -Properties LastLogonDate` → зафиксировать, какие машины давно не логинились.
12. **Хост (Windows).** Записать в `stand-log.md`: адрес gateway, порт, тип сертификата, список узлов, где включали WinRM.

### Проверка
- [ ] `https://fs01.corp.vektor.lab` открывается с WIN11-CL01, вход под `adm.student` проходит.
- [ ] DC01, WIN11-CL01, WIN10-CL02 в WAC в статусе Online.
- [ ] Встроенный PowerShell в WAC на DC01 выполняет команды и возвращает доменные данные.
- [ ] `C:\temp\inventory.csv` на FS01 содержит данные трёх машин.
- [ ] `Test-WSMan` с FS01 успешен для всех трёх узлов.
- [ ] Правило WinRM ограничено `RemoteAddress 10.10.10.0/24`, а не «любой адрес».
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини
> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Иван, добрый день. Вы вчера что-то настраивали на моём компьютере. Теперь вы говорите, что не можете ко мне подключиться удалённо и надо приехать. У меня закрытие месяца, принтер и Excel-надстройка не работают, а вы только завтра. Можно как-то без приезда?»
> **Срочность:** высокая

**Воспроизведение поломки (в консоли WIN11-CL01, «от имени администратора»):**

```powershell
Set-Service WinRM -StartupType Disabled
Stop-Service WinRM -Force
Disable-NetFirewallRule -DisplayGroup "Windows Remote Management"
```

**Диагностика по порядку (с FS01):**
1. `Test-Connection WIN11-CL01` — машина вообще жива? (ICMP — да, значит сеть/DNS в порядке).
2. `Resolve-DnsName WIN11-CL01` — имя резолвится в правильный DHCP-адрес из пула 10.10.10.100–150?
3. `Test-NetConnection WIN11-CL01 -Port 445` — SMB открыт, значит хост-файрвол в целом пропускает.
4. `Test-NetConnection WIN11-CL01 -Port 5985` — **ключевой тест**: `TcpTestSucceeded : False` → канал управления закрыт.
5. `Test-WSMan WIN11-CL01` — та же ошибка, подтверждает WinRM.
6. В WAC: узел WIN11-CL01 → сообщение вида «Connecting to remote server failed» — читаем текст, а не только цвет.
7. На самой ВМ (через консоль VMware): `Get-Service WinRM` → `Stopped`/`Disabled`; `Get-NetFirewallRule -DisplayGroup "Windows Remote Management" | Select DisplayName,Enabled` → `False`.
8. `Get-WinEvent -LogName "Microsoft-Windows-WinRM/Operational" -MaxEvents 20` — подтверждаем, что службу останавливали, а не она упала.

**Решение:**

```powershell
Set-Service WinRM -StartupType Automatic
Start-Service WinRM
Enable-NetFirewallRule -DisplayGroup "Windows Remote Management"
Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -RemoteAddress 10.10.10.0/24 -Profile Domain
Test-WSMan localhost
```
Затем с FS01: `Test-WSMan WIN11-CL01` → успех, в WAC узел снова Online, ivanov может работать.

**Ответ пользователю:** «Иван, добрый день. Проблема была на стороне управляющего канала: на вашем компьютере отключилась служба удалённого управления и закрылось правило в файрволе. Восстановили, к вам уже подключились. Проверьте, пожалуйста, принтер — если печать не идёт, напишите, посмотрим отдельно. Приезжать не нужно.»

**Закрытие заявки:** категория «Управление парком / WinRM», в решении — что включили и каким правилом ограничили, в `stand-log.md` — запись. Профилактика: мониторить `WinRM` в Zabbix (MON01) и добавить проверку в чек-лист после каждой «закалки» клиента.

### Что изменилось на стенде
- **FS01 (10.10.10.20)** — новая роль: WAC gateway, `https://fs01.corp.vektor.lab:443`, самоподписанный сертификат, служба `ServerManagementGateway` [ПРОВЕРИТЬ].
- **FS01** — включён WinRM, правило файрвола ограничено `10.10.10.0/24`; появилась папка `C:\temp` со скриптами и `inventory.csv`.
- **DC01, WIN11-CL01, WIN10-CL02** — включён WinRM, правило «Windows Remote Management» ограничено офисной подсетью.
- **WAC** — сохранены подключения DC01, WIN11-CL01, WIN10-CL02 с «Manage as» `VEKTOR\adm.student`.
- Доменная структура, папки `\\FS01\Obshchee|Buh|HR`, пользователи и группы **не менялись**.

### Откат
VMware Workstation → нужная ВМ → `Snapshot Manager` → `before-sysadmin-021` → `Restore`. Откатывать нужно все четыре ВМ, иначе получите WAC, который ссылается на узлы с другим состоянием. Если нужен «мягкий» откат без снапшота: `msiexec /x C:\temp\WindowsAdminCenter.msi /qn` на FS01 и `Set-Service WinRM -StartupType Disabled` на клиентах (но тогда перестанут работать удалённые проверки из следующих глав — лучше снапшот).

## 6. Ошибки новичков и безопасность

1. **Ставить WAC на контроллер домена «чтобы не плодить ВМ»** → компромисс gateway = компромисс всего домена; на DC ещё и лишняя нагрузка. Правильно: отдельный management-host (FS01 сейчас, в бою — выделенный jump-host/PAW).
2. **Открыть 5985 всему миру** → любой, кто дотянулся до порта и подобрал пароль, получает PowerShell от админа. Правильно: `RemoteAddress` = только подсеть управления, `-Profile Domain`, а лучше — 5986 с сертификатом от внутреннего CA.
3. **Пользоваться «Use my current credentials» из браузера на другой машине без делегирования** → непонятные ошибки авторизации. Правильно: явное «Manage as» для доменной учётки или настроенное constrained delegation.
4. **Ставить WAC на файловый сервер, который уже трещит по диску** → gateway пишет логи и кэш, диск кончается, падают шары. Правильно: отдельный том/машина + алерт по свободному месту.
5. **Считать «зелёный статус в списке» доказательством управляемости** → статус часто живёт на ICMP. Правильно: проверять `Test-WSMan` и выполнять реальную команду.
6. **Оставить самоподписанный сертификат в проде** → пользователи привыкают игнорировать предупреждения браузера, это прямая дорога к MITM. Правильно: сертификат от внутреннего CA (урок 16), потом — только исключения по имени.
7. **Включать `Enable-PSRemoting` без `-SkipNetworkProfileCheck` на клиентах с профилем «Общедоступная сеть»** → WinRM не поднимется, а причина неочевидна. Правильно: сначала привести сетевой профиль в `DomainAuthenticated`, потом включать.
8. **Давать `adm.student` право локального админа на всё и сохранять его в WAC** → это Tier-0 учётка в инструменте Tier-2. Правильно: отдельные учётки для управления клиентами, LAPS для локальных админов (урок 18).

**Безопасность.** Management plane — самая дорогая цель в сети: получив WinRM-доступ, атакующий получает и data plane. В боевой среде нельзя: держать gateway в той же подсети, что и рабочие места пользователей; разрешать управление с клиентских машин всем; хранить доменные админ-пароли в браузере/скриптах; использовать один пароль на весь парк (наш `Lab#2026-vektor` — только стенд). Минимум: изоляция management-подсети, MFA на входе в gateway (через reverse-proxy/SSO), журналирование всех подключений, отдельный «break glass» доступ на случай, если сам gateway лёг.

**Про РФ-реалии 2026.** WAC и PowerShell — бесплатны и остаются рабочим инструментом там, где они уже есть. System Center и Intune — лицензируемые продукты Microsoft; после 2022 года новые поставки и облачные подписки для российских организаций фактически недоступны, поэтому «Intune-подход» чаще реализуют не продуктом, а набором: GPO + WSUS (бесплатные роли Windows Server) + Zabbix (MON01) + скрипты. Для Linux-парка смотрят в сторону ALD Pro (Astra Linux Directory) и средств управления РЕД ОС `[ПРОВЕРИТЬ актуальные названия и версии]`, для инвентаризации — GLPI, Zabbix, TNI; конкретные отечественные MDM/EMM-продукты проверяйте по реестру Минцифры перед выбором.

## 7. Вопросы на собеседовании

**1. «Чем Windows Admin Center отличается от System Center и от Intune? Когда что берут?»**
Ответ: WAC — бесплатный агентless-gateway для точечного и мелкосерийного управления: посмотреть, перезагрузить, зайти в реестр, поставить роль. Без инвентаризации «по расписанию», без отчётности, без развёртывания образов. System Center (MECM/SCOM/SCVMM) — тяжёлая on-prem платформа: агенты, база SQL, роли сервера, зато развёртывание ОС и ПО, патчи по коллекциям, отчёты, мониторинг. Intune — облачная MDM: машина сама регистрируется, получает конфигурационные профили и политики соответствия, управление по состоянию, а не по команде. WAC — «посмотреть и поправить», MECM — «управлять парком в здании», Intune — «управлять парком где угодно, если есть лицензия и облако».
*Что хочет услышать:* понимание границ применимости и того, что инструменты не взаимоисключающие (WAC часто ставят рядом с MECM как «быстрый доступ»).
*Красный флаг:* «WAC заменяет SCCM, там же всё есть».

**2. «Что нужно, чтобы WAC подключился к серверу?»**
Ответ: разрешимое DNS-имя, доменная учётка с правами локального админа на узле, запущенная служба WinRM, открытые 5985 (или 5986 с валидным сертификатом) и, для части инструментов, SMB 445 и RPC/WMI. Плюс корректная авторизация: либо Kerberos-делегирование при входе «моими учётными данными», либо явное «Manage as». Диагностика: `Test-NetConnection -Port 5985`, `Test-WSMan`, лог WinRM/Operational.
*Что хочет услышать:* порты и протоколы, порядок диагностики «сеть → DNS → порт → служба → права».
*Красный флаг:* «просто добавь сервер, оно само».

**3. «Есть 300 ПК и 30 серверов, облако недоступно. Как построите централизованное управление?»**
Ответ: разделить плоскости. Management-подсеть и jump-host с WAC для серверов; MECM (или WSUS + GPO + скрипты, если MECM нет) для патчей и ПО на ПК; Zabbix для мониторинга; инвентаризация по расписанию через `Invoke-Command`/агент и выгрузкой в БД; отдельные учётки Tier-1/Tier-2, LAPS, запрет админ-доступа с пользовательских сегментов. Обязательно: регламент (кто что может), журнал изменений, тестовая группа из 5% машин перед массовым патчем, план отката.
*Что хочет услышать:* структуру и процессы, а не список продуктов; упоминание тестовых колец и отката.
*Красный флаг:* «поставлю один сервер и всё настрою мышкой».

**4. «Что такое Intune-подход и как его эмулировать без Intune?»**
Ответ: это управление по состоянию: машина описана набором политик (конфигурация, соответствие, кольца обновлений), система сама приводит её к целевому виду и сигналит об отклонении. Эмуляция: GPO/DSC как декларация состояния, WSUS с группами-«кольцами», скрипты проверки соответствия по расписанию, сбор результатов в Zabbix, регистрация машин через автоматический join. Ключевое отличие от «руками по SSH/RDP»: результат воспроизводим и проверяем.
*Что хочет услышать:* «desired state» и «compliance», а не только перечисление продуктов.
*Красный флаг:* «Intune — это просто облачный SCCM».

**5. «Как защитить саму систему управления?»**
Ответ: management plane изолируется в отдельный сегмент, доступ только с PAW/jump-host; для управления — отдельные учётки, не Tier-0; WinRM только по 5986 с сертификатом от внутреннего CA и ограничением по IP; MFA на входе в gateway; полное журналирование (кто, когда, к какому узлу); ротация паролей и LAPS; «break glass»-доступ на случай отказа gateway; регулярная проверка, что новые узлы не открыли 5985 в общий сегмент.
*Что хочет услышать:* tiering, ограничение источника, TLS, аудит, план на отказ.
*Красный флаг:* «WAC же внутри сети, значит безопасно».

## 8. Источники (официальная документация)

- **learn.microsoft.com** — раздел «Windows Admin Center»: статьи «Windows Admin Center overview», «Install Windows Admin Center», «Use Windows Admin Center to manage servers», «Windows Admin Center common troubleshooting steps». Поиск: `Windows Admin Center silent install SME_PORT`.
- **learn.microsoft.com** — «Windows Admin Center: managed servers and firewall requirements» (порты и протоколы). Поиск: `Windows Admin Center firewall ports WinRM`.
- **learn.microsoft.com** — «System Center documentation», разделы по Configuration Manager, Operations Manager, Virtual Machine Manager. Поиск: `Microsoft Configuration Manager documentation site:learn.microsoft.com`.
- **learn.microsoft.com** — «Microsoft Intune documentation»: «What is Microsoft Intune», «Configuration profiles», «Compliance policies», «Windows Update rings». Поиск: `Intune compliance policy update rings overview`.
- **learn.microsoft.com** — «Windows Remote Management (WinRM)», `Enable-PSRemoting`, «PowerShell Remoting over HTTPS». Поиск: `about_Remote_Troubleshooting site:learn.microsoft.com`.
- **learn.microsoft.com** — «Azure Arc-enabled servers» (агент `azcmagent`). Поиск: `Azure Arc connected machine agent overview`. Доступность Azure для организаций РФ — `[ПРОВЕРИТЬ]`.
- **learn.microsoft.com** — «Windows Server Update Services (WSUS)»: развёртывание, группы компьютеров, утверждение обновлений. Поиск: `WSUS deploy approve updates computer groups`.
- **zabbix.com** — «Zabbix Documentation», раздел про Windows-агент и мониторинг доступности сервисов `[ПРОВЕРИТЬ актуальную структуру разделов]`.
