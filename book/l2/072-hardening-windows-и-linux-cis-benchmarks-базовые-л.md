# Hardening Windows и Linux: CIS Benchmarks, базовые линии безопасности

## 1. Введение (зачем это нужно в реальной работе)

Понедельник, 09:12. В поддержку падает тикет:

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Мне сказали, что у нас теперь «безопасность». После обеда не смогла сохранить файл в `\\FS01\HR` — писало про подпись SMB. А потом вообще три раза вводила пароль и меня заблокировало. Я ничего не меняла!»
> **Срочность:** средняя (работа стоит, но не критично)

Что тут произошло на самом деле: ночью мы включили базовую линию безопасности — обязательную подпись SMB на FS01 и политику блокировки учётных записей в домене. Один старый сетевой диск на WIN10-CL02 не умел подпись, а пароль у Марии остался старый, и после смены она трижды промахнулась. Заявка выглядит как «у меня всё сломалось», а на деле это последствия hardening.

Hardening (ужесточение конфигурации) — это не «поставить антивирус». Это планомерное уменьшение поверхности атаки: выключить то, что не используется, потребовать то, что должно быть, и записать всё это так, чтобы через полгода можно было проверить и повторить. В работе L2 это встречается постоянно: внедрение политик, разбор «после ваших обновлений всё упало», проверка «а не отключили ли у нас снова SMB-подпись на новом сервере», подготовка к аудиту по требованиям ФСТЭК.

После этой главы вы будете уметь: читать CIS Benchmark и не применять его вслепую; собирать из него собственную базовую линию; применять её через GPO, `secedit`, `ufw`, `sysctl`; проверять состояние и ловить «дрейф» конфигурации; и главное — понимать, что и как ломается, и заранее готовить откат.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**CIS Benchmark** (Center for Internet Security) — это опубликованный набор рекомендаций по настройке конкретной ОС или продукта: Windows Server 2022, Windows 11, Ubuntu 24.04, nginx, Chrome и т.д. Каждая рекомендация имеет номер, описание, команду проверки и команду исправления. Скачивание бесплатное, но требует регистрации на cisecurity.org.

Внутри бенчмарка есть две оси, которые новички путают:

- **Уровень 1 (L1)** — «базовая гигиена», минимальное влияние на удобство и совместимость. Применяется практически везде.
- **Уровень 2 (L2)** — «защита в глубину» для систем с повышенными требованиями. Может сломать совместимость (например, отключение NTLM целиком, запрет локальных входов). На обычном рабочем месте бухгалтера это делать нельзя.

Плюс пометки **Scored / Not Scored** — влияет ли пункт на итоговый балл при автоматической проверке.

**Базовая линия (baseline)** — это НЕ бенчмарк. Бенчмарк — источник рекомендаций. Базовая линия — ваш собственный документ: «из 300 пунктов CIS для Windows Server 2022 мы берём 240, вот 60 отклонений, вот почему (1С, старый сканер, Kaspersky), версия 1.0, дата утверждения, кто владелец». Базовая линия должна быть в системе контроля версий и проходить через change management.

Где hardening физически живёт в системе — по слоям:

```text
┌──────────────────────────────────────────────────────────────┐
│ Приложения/сервисы  SSH, SMB, печать, IIS, 1С               │  ← конфиги сервисов
├──────────────────────────────────────────────────────────────┤
│ Аутентификация      LSA/NTLM/Kerberos (Windows)              │  ← пароли, блокировки,
│                     PAM/sshd/sudo (Linux)                       MFA, права
├──────────────────────────────────────────────────────────────┤
│ Сеть в ОС           Windows Firewall / ufw / nftables,        │  ← какие порты слушаем
│                     sysctl (net.ipv4.*)                        и куда ходим
├──────────────────────────────────────────────────────────────┤
│ Ядро и драйверы     ASLR, dmesg_restrict, Device Guard        │  ← защита от эксплойтов
├──────────────────────────────────────────────────────────────┤
│ Аудит и журналы     auditpol, Event Log, auditd, journald     │  ← чтобы было что разбирать
└──────────────────────────────────────────────────────────────┘
```

Жизненный цикл базовой линии:

```text
CIS Benchmark (источник)
      │  выбрать профиль L1/L2, выписать исключения
      ▼
Базовая линия «Вектор» v1.0  (файлы .inf / GPO / Ansible / скрипт)
      │  применить: сначала тест → потом пилот → потом все
      ▼
Контроль дрейфа: периодическая проверка (secedit /analyze, oscap, lynis)
      │
      └── расхождение → тикет → исправить или оформить исключение
```

Ключевые протоколы и механизмы, которые нам понадобятся: **SMB signing** (подпись пакетов SMB2/3, защита от MITM и релея), **NTLM level** (`LmCompatibilityLevel`, запрет LM/NTLMv1), **Kerberos** (основной протокол в домене), **SSH** (порт 22, аутентификация по ключам), **PAM** (pluggable authentication modules — «слой аутентификации» в Linux, через него работают `sudo`, `ssh`, блокировки `pam_faillock`), **auditd/Event Log** (источники для SIEM, урок 75).

## 3. Аналогия из жизни

Представьте, что вы сдаёте квартиру. Есть строительные нормы и правила (CIS Benchmark) — они толстые, общие и написаны для всех домов сразу. Есть ваш договор аренды и опись имущества (базовая линия) — тонкая бумага, где написано: «замок на двери меняем раз в год, балконная дверь закрывается на щеколду, у жильца один комплект ключей, вторую связку хранит управляющий». Опись подписана, лежит в папке, и раз в квартал вы приходите и сверяете: замок тот же? щеколда на месте? не поставил ли жилец дополнительный незарегистрированный ключ?

Если жилец поставил свой замок без спроса — это дрейф конфигурации. Если вы поставили бронированную дверь, а у жильца остался старый ключ — он ночует на улице. Ровно это и случилось с Марией Петровой: дверь новую поставили, а ключ ей не поменяли и не предупредили.

Аналогия перестаёт работать в одном месте: конфигурацию можно менять централизованно и мгновенно на сотне машин сразу, а замки в ста квартирах — нет; поэтому в ИТ цена «забыли про пилот» гораздо выше.

## 4. Практика и команды

### 4.1. Прочитать текущую политику Windows и сравнить с эталоном

`secedit` — встроенная утилита работы с локальной политикой безопасности.

```powershell
mkdir C:\Baseline -Force
secedit /export /cfg C:\Baseline\fs01-before.inf
notepad C:\Baseline\fs01-before.inf
secedit /analyze /db C:\Baseline\analyze.sdb /cfg C:\Baseline\vektor-baseline.inf /log C:\Baseline\analyze.log
```

- `/export /cfg` — выгрузить текущие настройки в текстовый INF.
- `/analyze` — сравнить текущее состояние с эталоном и записать расхождения в лог (`/log`). Это и есть контроль дрейфа «на минималках».
- Что смотреть в выводе: в `analyze.log` блоки `Mismatch` — именно они показывают, где реальность уехала от базовой линии.

### 4.2. Применить базовую линию локальной политики

Файл `C:\Baseline\vektor-baseline.inf`:

```ini
[Unicode]
Unicode=yes

[Version]
signature="$CHICAGO$"

[System Access]
MinimumPasswordLength = 14
PasswordComplexity = 1
PasswordHistorySize = 24
LockoutBadCount = 5
LockoutDuration = 15
ResetLockoutCount = 15

[Registry Values]
MACHINE\System\CurrentControlSet\Control\Lsa\LmCompatibilityLevel=4,5
MACHINE\System\CurrentControlSet\Control\Lsa\LimitBlankPasswordUse=4,1
MACHINE\System\CurrentControlSet\Control\Lsa\RestrictAnonymous=4,1
MACHINE\System\CurrentControlSet\Control\Lsa\RestrictAnonymousSAM=4,1
MACHINE\System\CurrentControlSet\Control\Lsa\MSV1_0\NTLMMinClientSec=4,537395200
MACHINE\System\CurrentControlSet\Services\LanManServer\Parameters\SMB1=4,0
MACHINE\System\CurrentControlSet\Services\LanManServer\Parameters\RequireSecuritySignature=4,1
```

```powershell
secedit /validate C:\Baseline\vektor-baseline.inf
secedit /configure /db C:\Windows\security\local.sdb /cfg C:\Baseline\vektor-baseline.inf /areas SECURITYPOLICY /log C:\Baseline\apply.log
```

- `/validate` — проверить синтаксис INF ДО применения. Всегда делайте это первым.
- `/configure` — применить; `/areas SECURITYPOLICY` — только политики безопасности, не трогая остальное.
- `LmCompatibilityLevel=5` — разрешён только NTLMv2, LM и NTLMv1 отклоняются. `RestrictAnonymous` — запрет анонимного перечисления.
- Важно: на контроллере домена парольная политика задаётся **не так**, а через GPO (`Default Domain Policy` → Computer Configuration → Policies → Windows Settings → Security Settings → Account Policies). Локальный `secedit` на DC01 политику доменных учёток не заменит.

### 4.3. Аудит: включить то, что потом будем читать

```cmd
auditpol /list /subcategory:* /v
auditpol /get /category:*
auditpol /set /subcategory:"Logon" /success:enable /failure:enable
auditpol /set /subcategory:"Account Lockout" /success:enable /failure:enable
```

- `/get` — показать текущие категории аудита; `/set` — изменить.
- **Ловушка РФ:** на русской Windows имена подкатегорий локализованы. Надёжнее брать английские имена или GUID из `auditpol /list /subcategory:* /v`.
- Смотреть в выводе: `Logon` должен быть `Success and Failure`, иначе инциденты (урок 79) будете разбирать по показаниям пользователей.

### 4.4. SMB: выключить SMB1, включить подпись

```powershell
Get-SmbServerConfiguration | Select EnableSMB1Protocol, RequireSecuritySignature, EnableSecuritySignature
Set-SmbServerConfiguration -EnableSMB1Protocol $false -RequireSecuritySignature $true -EnableSecuritySignature $true -Confirm:$false
Get-SmbClientConfiguration | Select RequireSecuritySignature
```

`RequireSecuritySignature=$true` — сервер откажется работать с клиентом без подписи. На современных Windows это по умолчанию; в смешанной сети со старыми NAS/МФУ — источник тикетов.

### 4.5. Кто локальный администратор на сервере

```powershell
Get-LocalGroup -SID S-1-5-32-544 | Get-LocalGroupMember
Get-LocalUser | Where-Object Enabled | Select Name, LastLogon, PasswordLastSet
```

- `S-1-5-32-544` — SID группы Administrators. Используйте SID, а не имя: на русской ОС группа называется «Администраторы», и скрипт с `-Group "Administrators"` упадёт.
- Смотреть: не должно быть «левых» учёток и включённого встроенного `Администратор`/`Administrator`.

### 4.6. Службы, которым не место на сервере

```powershell
Get-Service Spooler | Select Name, Status, StartType
Stop-Service Spooler -Force
Set-Service Spooler -StartupType Disabled
```

Печать на контроллере домена не нужна (атаки класса PrintNightmare). На FS01, который и есть сервер печати, наоборот — нужна. Проверяйте зависимости: `Get-Service Spooler | Select -Expand DependentServices`.

### 4.7. Быстрый срез журнала безопасности

```powershell
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4740; StartTime=(Get-Date).AddHours(-24)} |
  Select-Object TimeCreated, @{n='User';e={$_.Properties[0].Value}}
```

`4740` — блокировка учётной записи. Это первый запрос при разборе «меня заблокировало».

### 4.8. Linux: что реально слушает порты

```bash
sudo ss -tulpn
sudo systemctl list-unit-files --state=enabled
```

`ss -tulpn`: `-t` TCP, `-u` UDP, `-l` слушающие, `-p` процесс, `-n` без резолва имён. Всё, что слушает `0.0.0.0`, доступно снаружи — это и есть поверхность атаки. На UBU01 в норме: 22 (ssh). Если увидели 631 (cups), 5353 (avahi) — либо обосновать, либо выключить: `sudo systemctl disable --now avahi-daemon`.

### 4.9. Linux: эффективная конфигурация SSH

```bash
sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication|maxauthtries|allowusers'
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
LoginGraceTime 20
X11Forwarding no
EOF
sudo sshd -t && sudo systemctl reload ssh
```

- `sshd -T` — вывести **итоговую** конфигурацию после всех include-файлов. Именно так проверяют, что настройка применилась.
- `sshd -t` — только проверка синтаксиса. Всегда перед `reload`, иначе можно потерять доступ.
- Файл в `sshd_config.d/` с префиксом `99-` читается последним и перекрывает ранние. На Ubuntu 24.04 директива `Include /etc/ssh/sshd_config.d/*.conf` стоит в начале `sshd_config`, поэтому осторожно с порядком `[ПРОВЕРИТЬ]` на конкретной сборке.

### 4.10. Linux: параметры ядра

```bash
sudo tee /etc/sysctl.d/99-vektor-hardening.conf >/dev/null <<'EOF'
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 2
kernel.randomize_va_space = 2
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
EOF
sudo sysctl --system
sysctl kernel.randomize_va_space net.ipv4.tcp_syncookies
```

`randomize_va_space=2` — полный ASLR (рандомизация адресного пространства), усложняет эксплуатацию переполнений. `accept_redirects=0` — защита от подмены маршрута.

### 4.11. Linux: файрвол и обновления

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 10.10.10.0/24 to any port 22 proto tcp
sudo ufw --force enable
sudo ufw status verbose

sudo apt install unattended-upgrades apt-listchanges
sudo dpkg-reconfigure -plow unattended-upgrades
apt-config dump APT::Periodic
```

`ufw` — надстройка над nftables, «файрвол для людей». Правило `allow from 10.10.10.0/24` вместо `allow 22` — принципиально: SSH открыт только из офисной сети, не из интернета.

### 4.12. Права на файлы и поиск «дыр»

```bash
sudo stat -c '%a %U:%G %n' /etc/shadow /etc/passwd /etc/ssh/sshd_config
sudo find / -xdev -type f -perm -0002 -not -path '/proc/*' -not -path '/sys/*'
sudo awk -F: '($3==0){print $1}' /etc/passwd
```

- `/etc/shadow` в норме `640 root:shadow` (на части дистрибутивов `000`).
- `-perm -0002` — файлы, доступные на запись всем (world-writable). Их не должно быть в системных каталогах.
- `$3==0` — все учётки с UID 0 (root-эквиваленты). Должна быть только `root`.

### 4.13. Автоматическая оценка соответствия

```bash
sudo lynis audit system          # отчёт: /var/log/lynis.log, hardening index
sudo oscap info /usr/share/xml/scap/ssg/content/ssg-ubuntu2404-ds.xml   # [ПРОВЕРИТЬ имя файла]
```

Lynis — бесплатный аудитор, ставится из репозитория Ubuntu. OpenSCAP + SCAP Security Guide позволяют прогнать формальную проверку по профилю CIS и получить XML-отчёт. Точные имена пакетов и профилей для 24.04 проверяйте на месте — они меняются от релиза к релизу.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- DC01 (10.10.10.10) — AD DS, DNS, DHCP, домен `corp.vektor.lab` работает, вы входите как `VEKTOR\adm.student`.
- FS01 (10.10.10.20) — в домене, шара `\\FS01\HR` доступна для `GG-HR`, есть принтер.
- WIN10-CL02 — рабочее место `petrova` (DHCP), вход доменной учёткой.
- UBU01 (10.10.10.30) — Ubuntu Server 24.04, SSH работает, у вас есть учётка с `sudo` и настроен вход по ключу. **Если ключа нет** — шаг с `PasswordAuthentication no` пропустите и запишите это как осознанное отклонение от базовой линии.
- Учётные записи и пароли — только со стенда: домен `VEKTOR`, учебный пароль `Lab#2026-vektor`.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-l2-072`. Сделайте для **DC01, FS01, WIN10-CL02, UBU01**. Если предпочитаете CLI: `vmrun snapshot "C:\VMs\DC01\DC01.vmx" before-l2-072`.

### Шаги

1. **DC01.** Откройте `gpmc.msc` → домен `corp.vektor.lab` → ПКМ на `Default Domain Policy` → **Edit**. Путь: Computer Configuration → Policies → Windows Settings → Security Settings → Account Policies → Account Lockout Policy. Задайте: **Account lockout threshold = 5**, **Account lockout duration = 15 minutes**, **Reset account lockout counter after = 15 minutes**. Ожидаемо: в домене появилась блокировка после 5 неудач (раньше было «Never»).
2. **DC01.** Там же: Account Policies → Password Policy → **Minimum password length = 14**, **Enforce password history = 24**, **Maximum password age = 60**. Затем `gpupdate /force` и на DC01 проверьте `Get-ADDefaultDomainPasswordPolicy`. Ожидаемо: `MinPasswordLength : 14`, `LockoutThreshold : 5`.
3. **DC01 и FS01.** Включите аудит входа:
   ```cmd
   auditpol /set /subcategory:"Logon" /success:enable /failure:enable
   auditpol /set /subcategory:"Account Lockout" /success:enable /failure:enable
   auditpol /get /subcategory:"Logon"
   ```
   Ожидаемо: `Logon` → `Success and Failure`. (На русской локали имена подкатегорий другие — сверьтесь по `auditpol /list /subcategory:* /v`.)
4. **FS01.** Выгрузите текущее состояние: `secedit /export /cfg C:\Baseline\fs01-before.inf` и просмотрите файл. Ожидаемо: увидите `LmCompatibilityLevel`, `RestrictAnonymous` и прочие секции `[Registry Values]` — это ваш «до».
5. **FS01.** Создайте `C:\Baseline\vektor-baseline.inf` из примера 4.2, прогоните `secedit /validate`, затем `secedit /configure ... /areas SECURITYPOLICY`. Ожидаемо: код возврата 0, в `C:\Baseline\apply.log` — «Задача выполнена успешно» / «The task has completed successfully».
6. **FS01.** Проверьте дрейф: `secedit /analyze /db C:\Baseline\analyze.sdb /cfg C:\Baseline\vektor-baseline.inf /log C:\Baseline\analyze.log`, затем `Select-String -Path C:\Baseline\analyze.log -Pattern "Mismatch"`. Ожидаемо: список расхождений пуст или содержит только те пункты, которые вы сознательно исключили.
7. **FS01.** SMB: `Set-SmbServerConfiguration -EnableSMB1Protocol $false -RequireSecuritySignature $true -EnableSecuritySignature $true -Confirm:$false`, затем `Get-SmbServerConfiguration | Select EnableSMB1Protocol, RequireSecuritySignature`. Ожидаемо: `False` и `True`. Проверьте доступ к шаре с WIN10-CL02: `net use Z: \\FS01\HR` под `petrova`.
8. **DC01.** Отключите службу печати: `Stop-Service Spooler -Force; Set-Service Spooler -StartupType Disabled; Get-Service Spooler`. Ожидаемо: `Status: Stopped, StartType: Disabled`. Печать при этом продолжает работать через FS01 — проверьте с WIN10-CL02.
9. **FS01.** Ревизия админов: `Get-LocalGroup -SID S-1-5-32-544 | Get-LocalGroupMember`. Ожидаемо: только `VEKTOR\Domain Admins` и, возможно, локальный `Administrator` (выключенный). Всё лишнее — удалить и записать в журнал.
10. **UBU01.** Подключитесь по SSH (сессию с `sudo` **не закрывайте**). Соберите «до»: `sudo ss -tulpn` и `sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication'`. Ожидаемо: видите, что слушает и какие настройки SSH реально применяются.
11. **UBU01.** Создайте `/etc/ssh/sshd_config.d/99-hardening.conf` (пример 4.9), выполните `sudo sshd -t`, затем `sudo systemctl reload ssh`. В **новой** сессии проверьте вход по ключу. Ожидаемо: вход по ключу работает, `sshd -T` показывает `permitrootlogin no`, `passwordauthentication no`.
12. **UBU01.** Примените `sysctl` (пример 4.10) и `ufw` (пример 4.11). Проверьте `sudo ufw status verbose` — ожидаемо `Status: active`, одно правило для 22 из 10.10.10.0/24. С WIN10-CL01 проверьте: `Test-NetConnection 10.10.10.30 -Port 22` → `TcpTestSucceeded : True`.
13. **UBU01.** Настройте автообновления безопасности (пример 4.11) и запустите `sudo lynis audit system`. Ожидаемо: отчёт с `Hardening index` в районе 60–75 (до настройки был ниже); зафиксируйте число в журнале как метрику «до/после».
14. **Все.** Запишите результат в `stand-log.md`: базовые линии `C:\Baseline\vektor-baseline.inf` (FS01) и `/etc/ssh/sshd_config.d/99-hardening.conf`, `/etc/sysctl.d/99-vektor-hardening.conf` (UBU01), GPO `Default Domain Policy` изменён, Hardening index = N.

### Проверка

- [ ] `Get-ADDefaultDomainPasswordPolicy` на DC01 показывает `LockoutThreshold 5`, `MinPasswordLength 14`
- [ ] `auditpol /get /subcategory:"Logon"` → `Success and Failure` на DC01 и FS01
- [ ] `secedit /analyze` по `vektor-baseline.inf` на FS01 не даёт `Mismatch` (кроме оформленных исключений)
- [ ] `Get-SmbServerConfiguration` на FS01: `EnableSMB1Protocol False`, `RequireSecuritySignature True`
- [ ] Шара `\\FS01\HR` открывается с WIN10-CL02 под `petrova`
- [ ] `Get-Service Spooler` на DC01: `Stopped / Disabled`; печать с WIN10-CL02 через FS01 работает
- [ ] `sshd -T` на UBU01: `permitrootlogin no`, `passwordauthentication no`
- [ ] `ufw status verbose` на UBU01: `active`, 22 только из `10.10.10.0/24`
- [ ] `sysctl kernel.randomize_va_space` → `2`
- [ ] `stand-log.md` обновлён, числа и имена файлов зафиксированы

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Не могу войти, пишет «учётная запись заблокирована, обратитесь к администратору». Пароль я помню, просто утром тыкал раскладку. И так уже второй раз за неделю!»
> **Срочность:** высокая (человек без рабочего места)

**Как воспроизвести.** С WIN11-CL01 сымитируйте серию неверных паролей (только на стенде!):

```powershell
1..6 | ForEach-Object { net use \\DC01\IPC$ /user:VEKTOR\ivanov "WrongPass#2026" 2>&1 | Out-Null }
```

Через 5 попыток учётка блокируется.

**Диагностика по порядку:**

1. Уточнить у пользователя: когда началось, что делал, один компьютер или несколько. «Второй раз за неделю» — красный флаг: возможно, старый пароль сохранён где-то ещё (телефон, задача планировщика, сетевой диск).
2. На DC01: `Get-ADUser ivanov -Properties LockedOut, BadLogonCount, LastBadPasswordAttempt | Select Name, LockedOut, BadLogonCount, LastBadPasswordAttempt`. Ожидаемо: `LockedOut: True`.
3. На DC01 найти источник неудач — событие 4740 (блокировка) и 4625 (неудачный вход):
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4740; StartTime=(Get-Date).AddHours(-2)} |
     Select TimeCreated, @{n='Target';e={$_.Properties[0].Value}}, @{n='Caller';e={$_.Properties[1].Value}}
   ```
   Смотрим поле «Caller computer» — откуда долбятся. Если это WIN11-CL01, а пользователь там не был — ищем сохранённые креды.
4. Разблокировать: `Unlock-ADAccount -Identity ivanov`.
5. Проверить, не «утекает» ли пароль: на WIN11-CL01 → **Панель управления → Диспетчер учётных данных → Учётные данные Windows** — удалить записи для FS01/DC01; проверить `Планировщик заданий` и подключённые телефоны (Exchange ActiveSync, если настроен).

**Ответ пользователю:** «Иван, заблокировал вас я — точнее, наша новая политика безопасности. Теперь после 5 неверных вводов пароля учётная запись блокируется на 15 минут, и это правильная защита: так же блокируются и злоумышленники, которые пытаются подобрать пароль. Я вас разблокировал, заходите. Учтите: пароль придётся сменить, новый — минимум 14 символов. Если блокировка повторится без вашего участия — напишите мне сразу, будем искать, где сохранён старый пароль.»

**Закрытие заявки:** категория «Учётные записи → Блокировка», в решении — причина (политика lockout 5/15), действие (разблокировано, креды проверены), рекомендация (смена пароля ≥14). Отдельным тикетом — задача «проинформировать сотрудников о новой парольной политике» (иначе такие заявки будут пачками).

### Что изменилось на стенде

- **DC01:** изменена `Default Domain Policy` — lockout 5/15/15, пароль ≥14, история 24, срок 60 дней; включён аудит `Logon` и `Account Lockout`; служба `Spooler` остановлена и отключена.
- **FS01:** применена локальная базовая линия «Вектор v1.0» (`C:\Baseline\vektor-baseline.inf`); SMB1 выключен, подпись SMB обязательна; включён аудит `Logon`; проведена ревизия локальных администраторов.
- **UBU01:** включён `ufw` (22 только из 10.10.10.0/24); `/etc/sysctl.d/99-vektor-hardening.conf`; `/etc/ssh/sshd_config.d/99-hardening.conf` (`PermitRootLogin no`, пароли отключены при наличии ключа); настроен `unattended-upgrades`; зафиксирован `Hardening index` от Lynis.
- **WIN11-CL01 / WIN10-CL02:** конфигурация не менялась (только проверки), учётка `ivanov` разблокирована.
- Новые артефакты: каталог `C:\Baseline` на FS01, GPO-политика домена (изменённая), запись в `stand-log.md`.

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot Manager** → выбрать `before-l2-072` → **Go to**. Откатывать нужно **согласованно**: сначала DC01 и FS01 (иначе доменные политики разъедутся), потом UBU01 и клиент. После отката в `stand-log.md` добавьте строку «откат к before-l2-072, причина: …». Если откатывать всё не хочется — точечно: на FS01 применить `fs01-before.inf` через `secedit /configure`, на DC01 вернуть значения в `Default Domain Policy`, на UBU01 удалить drop-in-файлы и выполнить `sudo systemctl reload ssh` / `sudo sysctl --system` / `sudo ufw disable`.

## 6. Ошибки новичков и безопасность

1. **Применить CIS Level 2 целиком «потому что безопасно».** → Ломается совместимость: отключается NTLM, локальные входы, старые приложения 1С и МФУ перестают работать, бизнес встаёт. → Правильно: Level 1 как база, Level 2 — точечно и с обоснованием.
2. **Жёсткая настройка без пилота и без снапшота.** → Массовый отказ, откатывать нечего, виноват «админ, который всё сломал». → Правильно: сначала тест на одной ВМ, потом пилотная группа 2–3 человека, потом все; снапшот/бэкап до изменений.
3. **Настройка контроллера домена локальной политикой.** → Думаете, что задали парольную политику, а доменные учётки живут по другим правилам. → Правильно: парольная политика домена — только через GPO (Default Domain Policy или отдельная GPO на домен).
4. **Отключение SSH-пароля до настройки ключей.** → Потеря доступа к серверу, лечится только консолью гипервизора. → Правильно: сначала проверить вход по ключу в отдельной сессии, только потом `PasswordAuthentication no`.
5. **Включение файрвола без правила для SSH.** → `ufw enable` — и вы вне сервера. → Правильно: сначала `ufw allow from 10.10.10.0/24 ... port 22`, потом `enable`.
6. **Hardening без документации.** → Через полгода никто не знает, почему SMB1 выключен и что будет, если включить. → Правильно: базовая линия = файл + версия + список исключений + запись в `stand-log.md`.
7. **Одноразовый hardening.** → Новая ВМ развёрнута из шаблона без политики, дрейф вернулся. → Правильно: контроль дрейфа по расписанию (`secedit /analyze`, `oscap`, `lynis`) + регулярная сверка.
8. **Скрипт hardening «из интернета» от неизвестного автора.** → Может выключить нужное, добавить бэкдор или сломать загрузку. → Правильно: официальные источники (Microsoft SCT, CIS, Ubuntu Security Guide) и построчный разбор перед запуском.

### Безопасность

- **Нельзя** отключать UAC, Windows Firewall, SELinux/AppArmor и `ufw` «ради того, чтобы заработало». В боевой среде это оформляется как исключение с компенсирующими мерами (сегментация, EDR) и сроком.
- **Нельзя** держать пароли и ключи в hardening-скриптах и в шарах вроде `\\FS01\Obshchee`. Секреты — отдельная тема (урок 76).
- **Нельзя** ставить «пароль не истекает» и `MinimumPasswordLength = 4` как способ закрыть тикет. Это прямое нарушение и повод для замечания на аудите.
- На стенде мы отключаем парольный вход по SSH и держим учебный пароль `Lab#2026-vektor` в открытом виде — **только** потому, что это изолированная лаборатория на вашем ПК. В реальной сети такой пароль недопустим, а вход по ключу должен быть у каждого админа свой, персональный.

## 7. Вопросы на собеседовании

**1. Что такое базовая линия безопасности и чем она отличается от CIS Benchmark?**

**Ответ.** CIS Benchmark — публичный набор рекомендаций производителя-сообщества для конкретного продукта: сотни пунктов с уровнями L1/L2. Базовая линия — наш внутренний утверждённый документ: выбранный профиль бенчмарка плюс исключения под нашу среду (1С, старый сканер, Kaspersky), с версией, датой и владельцем. Бенчмарк — источник, базовая линия — то, что мы реально применяем и проверяем. Хранится в Git, изменения — через change management, проверяется автоматически на дрейф.

**Что хочет услышать интервьюер:** что вы не «применяете CIS целиком», а умеете адаптировать и версионировать; что знаете про исключения и контроль дрейфа.

**Красный флаг:** «Скачал бенчмарк и применил всё по списку — так же безопаснее».

**2. Как вы внедрите hardening на 50 машинах Windows?**

**Ответ.** Централизованно через GPO: создаю отдельную GPO (`VEKTOR-Baseline-v1`), а не правлю дефолтные; настройки — из Microsoft Security Baselines / CIS, применяю через Security Compliance Toolkit (LGPO). Раскладка: сначала одна тестовая ВМ, потом пилотная OU с 2–3 пользователями, потом вся OU `Компьютеры`. Обязательно — пилот на совместимость с 1С, печатью и антивирусом. Заранее продумываю откат: GPO отключается/удаляется, снимок ВМ на пилоте, бэкап. После раскатки — `gpresult /h` для отчёта и периодический аудит через `secedit /analyze` или сканер соответствия.

**Что хочет услышать:** порядок «тест → пилот → прод», GPO вместо ручной настройки, план отката, проверка результата.

**Красный флаг:** «Зайду на каждую машину и настрою руками» или «Сделаю скрипт и запущу на всех сразу».

**3. Назовите настройку из CIS Level 2, которую нельзя включать на контроллере домена или на сервере с 1С, и объясните почему.**

**Ответ.** Например, полный запрет NTLM (`LmCompatibilityLevel` и отключение NTLM через политику ограничения NTLM) — сломает старые приложения и часть оборудования, которым Kerberos недоступен: старые МФУ при сканировании в папку, часть 1С-клиентов, устаревшие системы. Также «интерактивный вход: не отображать имя последнего пользователя» безобиден, но «запрет локального входа» и отключение служебных учёток может сломать службы. На DC отдельно опасны пункты про кэширование учётных данных и отключение служебных учётных записей. Правильный подход: проверить приложение в тесте, при необходимости сделать исключение с компенсацией — сегментация, ограничение по IP, отдельный VLAN.

**Что хочет услышать:** понимание, что hardening конфликтует с legacy; готовность проверять, а не ломать.

**Красный флаг:** «Всё из CIS безопасно, поэтому включаем всё».

**4. Как вы поймёте, что конфигурация «уплыла» от базовой линии?**

**Ответ.** Регулярная сверка: на Windows — `secedit /analyze` с эталонным INF и разбор `Mismatch` в логе, плюс GPO-отчёты (`gpresult`, RSOP) и Microsoft Policy Analyzer/Security Compliance Toolkit; на Linux — `lynis audit system`, `oscap xccdf eval` по профилю CIS, сверка `sshd -T`, `ss -tulpn`, `ufw status`, `sysctl -a`. Отдельно — сравнение с эталонным образом/шаблоном, чтобы новая ВМ не приезжала «чистой». Все расхождения — в тикеты: либо исправить, либо оформить исключение. Идеально — завести это в мониторинг (Zabbix на MON01) и получать алерт, а не узнавать о дрейфе на аудите.

**Что хочет услышать:** что контроль — процесс, автоматизированный и регулярный, а не разовая акция.

**Красный флаг:** «А зачем? Мы же один раз настроили».

**5. Hardening сломал бизнес-приложение. Ваши действия?**

**Ответ.** Сначала восстановить сервис: откатить конкретную настройку (или всю GPO/снапшот), зафиксировать время и что именно откатили. Потом разобраться: какой именно пункт baseline вызвал проблему, что в логах приложения (для 1С — технологический журнал, урок 67), воспроизводится ли на тесте. Затем решение: либо техническое (обновить клиент, настроить Kerberos), либо официальное исключение в baseline с компенсирующими мерами и сроком пересмотра. И обязательно — запись в `stand-log.md`/систему изменений и уведомление пользователей. Главное — не «тихо вернуть как было и забыть», иначе на аудите будет необъяснимое отклонение.

**Что хочет услышать:** приоритет «сначала сервис, потом разбор», документирование и оформление исключения.

**Красный флаг:** «Отключу hardening навсегда, чтобы больше не мешал».

## 8. Источники (официальная документация)

- **CIS (Center for Internet Security)** — cisecurity.org, раздел **CIS Benchmarks**: бесплатные PDF по Windows Server 2022, Windows 11, Ubuntu 24.04. Поисковая фраза: `CIS Benchmark Windows Server 2022 pdf`. Там же бесплатная утилита **CIS-CAT Lite** (требует Java) `[ПРОВЕРИТЬ актуальность версии и условия регистрации]`.
- **Microsoft Learn** — learn.microsoft.com, разделы **Windows security baselines**, **Microsoft Security Compliance Toolkit**, **Security Baselines Guide**. Поиск: `Microsoft Security Compliance Toolkit LGPO`. Там же документация по `secedit`, `auditpol`, SMB signing.
- **Ubuntu** — ubuntu.com, разделы **Ubuntu Security Guide (USG)** и **CIS hardening automation**; документация `ufw`, `unattended-upgrades`, `sshd_config`. Поиск: `Ubuntu Security Guide usg cis_level1_server` `[ПРОВЕРИТЬ актуальные имена профилей и условия Ubuntu Pro]`.
- **OpenSCAP** — open-scap.org, раздел **Documentation**; профили — проект **SCAP Security Guide** (complianceascap.org / GitHub). Поиск: `oscap xccdf eval profile cis ubuntu`.
- **CISOfy / Lynis** — cisofy.com, раздел **Lynis documentation**. Поиск: `lynis audit system hardening index`.
- **NIST** — nist.gov, публикации **SP 800-123** (Guide to General Server Security) и **SP 800-70** (National Checklist Program). Поиск: `NIST SP 800-123 server security guide`.
- **DISA STIGs** — public.cyber.mil, раздел **STIGs** (жёстче CIS, полезно для сравнения). Поиск: `DISA STIG Windows Server 2022`.
- По российской нормативке базовые требования к конфигурации ОС — приказы ФСТЭК №17/21/239; детально разбираем в уроке 78, здесь достаточно знать, что CIS — не единственный источник требований.
