# Linux в домене: ввод Astra Linux/ALT/РЕД ОС в AD (FreeIPA, Samba, realmd)

## 1. Введение (зачем это нужно в реальной работе)

Заявка в учебную очередь `C:\Support\Tickets`:

> **Пользователь:** Иван Иванов (бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Здравствуйте! Нам на бухгалтерию поставили новую машину с Astra Linux (ASTRA01, 10.10.10.31). Раньше на ней работал только локальный пользователь, а теперь надо, чтобы сотрудники заходили туда под своими доменными учётками, как на Windows. И чтобы пароль был тот же, что в домене. Сейчас при входе пишет "неверное имя пользователя или пароль". Помогите, пожалуйста, до конца недели — надо сдавать отчёт.»
> **Срочность:** средняя

В Москве в 2026 году это уже не экзотика. Госсектор, банки, крупные корпорации по требованиям импортозамещения переводят часть рабочих мест на Astra Linux, РЕД ОС, ALT Linux. При этом домен Active Directory почти везде остаётся — на Windows Server. И у L1 возникает задача: «Linux-машина должна видеть доменных пользователей». Это не «поставить пакет и забыть»: нужно понимать, как Linux вообще узнаёт про пользователей, что такое SSSD, Winbind, realmd, Kerberos, LDAP, и почему одна и та же команда на Astra и на ALT может выглядеть по-разному.

После этой главы ты будешь уметь: объяснить, как Linux-машина входит в AD; выполнить ввод ASTRA01 в домен `corp.vektor.lab`; проверить, что доменный пользователь реально может войти; диагностировать типовые ошибки (время, DNS, Kerberos, права sudo) и грамотно ответить на собеседовании, почему это не «просто apt install».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Linux по своей природе ничего не знает про Active Directory. AD — это набор служб Microsoft: Kerberos (аутентификация), LDAP (каталог), DNS (SRV-записи), SMB (файлы и политики). Чтобы Linux-машина «стала доменной», на ней должны работать несколько компонентов, которые умеют говорить с этими службами.

Схема (упрощённо):

```text
   ASTRA01 (10.10.10.31)                    DC01 (10.10.10.10)
   ┌──────────────────────┐                 ┌──────────────────────┐
   │  login / ssh / sudo  │                 │  AD DS               │
   │        │             │                 │  ├─ Kerberos (88)    │
   │        ▼             │   DNS (53)      │  ├─ LDAP   (389/636) │
   │  NSS  +  PAM         │◄───────────────►│  ├─ DNS    (53)      │
   │        │             │   Kerberos(88)  │  └─ SMB    (445)     │
   │        ▼             │   LDAP (389)    │                      │
   │  SSSD (или winbind)  │                 │  зона corp.vektor.lab│
   │        │             │                 │  (AD-интегрированная)│
   │        ▼             │                 └──────────────────────┘
   │  /etc/sssd/sssd.conf │
   │  /etc/krb5.conf      │
   │  /etc/nsswitch.conf  │
   └──────────────────────┘
```

Что здесь что:

- **NSS (Name Service Switch)** — механизм, через который Linux отвечает на вопросы «кто такой ivanov?», «какая у него группа?». Файл `/etc/nsswitch.conf`. Чтобы доменные пользователи «появились» в системе, в строки `passwd`, `group`, `shadow` нужно добавить источник `sss` (SSSD) или `winbind`.
- **PAM (Pluggable Authentication Modules)** — механизм проверки пароля при входе, `su`, `sudo`, `ssh`. Файлы в `/etc/pam.d/`. Чтобы доменный пароль проверялся через Kerberos, в PAM добавляют модуль `pam_sss.so`.
- **Kerberos** — протокол аутентификации по билетам (ticket). Клиент получает у контроллера домена билет TGT (Ticket Granting Ticket), потом предъявляет его службам. Порт 88 TCP/UDP. Критично: **время на клиенте и на DC должно расходиться не более чем на 5 минут** (по умолчанию), иначе билет не выдаётся.
- **LDAP** — протокол чтения каталога (пользователи, группы, OU). Порт 389 (или 636 для LDAPS). Через LDAP Linux узнаёт список пользователей и их группы.
- **DNS** — Linux должен резолвить SRV-записи вида `_ldap._tcp.corp.vektor.lab`, `_kerberos._tcp.corp.vektor.lab`. Если DNS-сервер на клиенте не указывает на DC01 (10.10.10.10), ввод в домен провалится с ошибкой «Cannot resolve KDC» или «SRV record not found».
- **SSSD (System Security Services Daemon)** — современный «клей»: одна служба, которая объединяет Kerberos + LDAP + кэш и отдаёт результат в NSS и PAM. Сегодня это стандарт для Linux в AD.
- **Winbind** — старый компонент Samba, делает то же самое через SMB/RPC. Ещё встречается на старых дистрибутивах и в некоторых сборках ALT/РЕД ОС.
- **realmd** — утилита-обёртка, которая автоматически настраивает SSSD/Winbind и Kerberos. Команда `realm join` — самый быстрый способ ввести машину в домен.
- **Samba (как клиент домена)** — набор утилит `net ads join`, `wbinfo`, `smbclient`. Используется, когда нужен доступ к SMB-ресурсам (`\\FS01\Buh`) или когда realmd/SSSD недоступны.
- **FreeIPA** — это **не** Active Directory. Это отдельный продукт (Red Hat), свой LDAP + Kerberos + DNS + CA. В нашей компании домен — AD, поэтому FreeIPA здесь только для сравнения: в задачах L1 «ввести Linux в AD» FreeIPA не используется, это отдельный сервер идентичности. Если компания выбрала FreeIPA — это архитектурное решение, а не задача L1.

Итого: чтобы Linux «стал доменным», нужно (1) правильный DNS, (2) синхронное время, (3) Kerberos-конфиг, (4) SSSD или Winbind, (5) правки NSS и PAM, (6) учётная запись компьютера в AD. realmd делает шаги 3–6 за одну команду.

## 3. Аналогия из жизни

Представь большой бизнес-центр с единой пропускной системой (это AD). У каждого сотрудника — бейдж с фотографией и чипом (это доменная учётка). На входе стоит турникет с ридером (это Kerberos: он проверяет чип и выдаёт талон на посещение). Внутри здания есть справочная, где можно узнать, кто в каком отделе работает (это LDAP). Есть служба охраны, которая знает, кому куда можно (это группы AD).

Теперь ты открываешь новый офис в этом же бизнес-центре (это Linux-машина). Чтобы твои сотрудники могли войти по общему бейджу, тебе нужно: провести в офис кабель к той же пропускной системе (DNS на DC01), синхронизировать часы на турникете с главными часами здания (Kerberos-время), поставить у себя турникет и ридер (SSSD), подключить справочную (LDAP) и зарегистрировать сам офис в управляющей компании (учётная запись компьютера в AD). realmd — это «мастер подключения офиса к бизнес-центру за один звонок».

Аналогия перестаёт работать в одном месте: в бизнес-центре офис либо подключён, либо нет, а в Linux можно «подключить» только аутентификацию, но забыть про домашнюю папку, sudo и группы — и пользователь войдёт, но работать не сможет.

## 4. Практика и команды

Все команды — на ASTRA01 под `adm.student` (sudo), если не указано иное. На DC01 — в PowerShell от `adm.student`.

**1. Проверить DNS и время до ввода в домен.** Это первое, что делает L1 при любой проблеме с доменом.

```bash
# DNS должен указывать на DC01
cat /etc/resolv.conf
# Проверить SRV-записи Kerberos и LDAP
sudo apt install -y dnsutils    # если ещё нет
dig +short SRV _kerberos._tcp.corp.vektor.lab
dig +short SRV _ldap._tcp.corp.vektor.lab
# Время
timedatectl
```

Смотрим: в `resolv.conf` — `nameserver 10.10.10.10`; `dig` возвращает строки вида `0 100 88 dc01.corp.vektor.lab.` (примерно); `timedatectl` показывает `System clock synchronized: yes` и часовой пояс `Europe/Moscow`. Если SRV-записей нет — либо DNS не тот, либо DC01 не отвечает.

**2. Установить пакеты для ввода в домен.** На Astra Linux CE набор может отличаться от Ubuntu, но базовые пакеты те же.

```bash
sudo apt update
sudo apt install -y realmd sssd sssd-tools libnss-sss libpam-sss \
                    adcli samba-common-bin oddjob oddjob-mkhomedir \
                    packagekit krb5-user
```

`realmd` — утилита `realm`; `sssd` — служба; `adcli` — утилита для работы с AD; `oddjob-mkhomedir` — создаёт домашнюю папку при первом входе; `krb5-user` — клиент Kerberos и `kinit`/`klist`. На ALT/РЕД ОС имена пакетов могут отличаться [ПРОВЕРИТЬ в конкретной версии].

**3. Найти домен и посмотреть, что видит realmd.**

```bash
realm discover corp.vektor.lab
```

Вывод примерно:

```text
corp.vektor.lab
  type: kerberos
  realm-name: CORP.VEKTOR.LAB
  domain-name: corp.vektor.lab
  configured: no
  server-software: active-directory
  client-software: sssd
  required-package: sssd
  ...
```

Если `server-software: active-directory` — realmd нашёл DC. Если пусто или ошибка — проблема в DNS.

**4. Ввести машину в домен.**

```bash
sudo realm join -U adm.student corp.vektor.lab
```

`-U adm.student` — от чьего имени выполнять (нужны права создать объект компьютера в AD; `adm.student` — доменный админ, у него есть). Пароль спросит интерактивно. Успех — тишина или строка `Successfully enrolled machine in realm`. Ошибки: `Couldn't find kerberos realm` (DNS), `Clock skew too great` (время), `Insufficient access` (нет прав в AD).

**5. Проверить, что учётка компьютера появилась в AD.** На DC01:

```powershell
Get-ADComputer -Filter 'Name -like "ASTRA01*"' | Select Name, DistinguishedName
```

Должна появиться запись `ASTRA01` (имя может быть в верхнем регистре). Если её нет — join не прошёл, даже если команда не выдала ошибку.

**6. Проверить Kerberos-билет доменного пользователя.**

```bash
kinit ivanov@CORP.VEKTOR.LAB
klist
```

`kinit` запросит пароль `ivanov` и выдаст TGT. `klist` покажет строку с `krbtgt/CORP.VEKTOR.LAB@CORP.VEKTOR.LAB`. Если `kinit` падает с `Preauthentication failed` — неверный пароль; `Clock skew` — время; `Cannot contact any KDC` — DNS/сеть.

**7. Проверить, что SSSD видит пользователя.**

```bash
id ivanov@corp.vektor.lab
getent passwd ivanov@corp.vektor.lab
```

`id` покажет UID/GID и группы (примерно `uid=1234567(ivanov) gid=1234567(domain users) groups=...`). `getent passwd` — строку из NSS. Если пусто — SSSD не настроен или `nsswitch.conf` не содержит `sss`.

**8. Проверить вход доменного пользователя.**

```bash
su - ivanov@corp.vektor.lab
```

Спросит пароль, должен пустить в домашнюю папку `/home/corp.vektor.lab/ivanov` (или `/home/ivanov@corp.vektor.lab` — зависит от настроек SSSD [ПРОВЕРИТЬ на Astra]). Если пускает, но домашней папки нет — не установлен `oddjob-mkhomedir` или не включён в PAM.

**9. Разрешить доменным админам sudo (осторожно!).** По умолчанию доменный пользователь — обычный. Чтобы `adm.student` мог `sudo`, добавляют правило:

```bash
sudo visudo -f /etc/sudoers.d/domain-admins
```

Содержимое:

```text
%domain\ admins@corp.vektor.lab ALL=(ALL) ALL
```

Это учебная настройка. В боевой среде так делать нельзя без согласования с ИБ — обычно sudo дают через группы AD и централизованно.

**10. Выйти из домена (если что-то пошло не так).**

```bash
sudo realm leave corp.vektor.lab
```

Убирает конфиг SSSD/Kerberos и (по умолчанию) учётку компьютера из AD. Если нужно оставить учётку — `realm leave --remove` [ПРОВЕРИТЬ флаг в твоей версии].

**11. Посмотреть логи SSSD при проблемах.**

```bash
sudo journalctl -u sssd -n 100 --no-pager
sudo tail -n 100 /var/log/sssd/sssd_corp.vektor.lab.log
```

Ищем `Authentication failure`, `Cannot contact any KDC`, `SRV lookup failed`.

**12. Проверить, что машина «в домене» с точки зрения realmd.**

```bash
realm list
```

Вывод примерно:

```text
corp.vektor.lab
  type: kerberos
  realm-name: CORP.VEKTOR.LAB
  domain-name: corp.vektor.lab
  configured: kerberos-member
  ...
```

`configured: kerberos-member` — машина в домене.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- DC01 (10.10.10.10) — Windows Server 2022, домен `corp.vektor.lab` развёрнут (L1 №42), OU и пользователи `ivanov`, `petrova`, `sidorov`, `adm.student` созданы (L1 №43), DNS-зона AD-интегрированная, DC01 — DNS-сервер для клиентов.
- ASTRA01 (10.10.10.31) — Astra Linux CE, SSH включён, sudo-пользователь `adm.student`, статический адрес, DNS указывает на 10.10.10.10 (по хронологии L1 №30 и настройкам сети).
- WIN11-CL01 и WIN10-CL02 — в домене (L1 №44), для проверки, что домен жив, но в лабораторной не участвуют.
- Пароль стенда: `Lab#2026-vektor`.

### Снапшот

В VMware Workstation на ASTRA01 и DC01: `before-l1-050`. На DC01 — обязательно, потому что join создаёт объект компьютера в AD.

### Шаги

1. **На DC01** проверь, что домен и DNS живы. В PowerShell от `adm.student`:

    ```powershell
    Get-ADDomain | Select DNSRoot, NetBIOSName
    Resolve-DnsName -Name _kerberos._tcp.corp.vektor.lab -Type SRV
    ```

    Ожидаемо: `DNSRoot corp.vektor.lab`, `NetBIOSName VEKTOR`, SRV-запись указывает на `dc01.corp.vektor.lab` порт 88.

2. **На ASTRA01** проверь DNS и время:

    ```bash
    cat /etc/resolv.conf
    timedatectl
    ```

    Ожидаемо: `nameserver 10.10.10.10`, `System clock synchronized: yes`, часовой пояс `Europe/Moscow`. Если время «плывёт» — синхронизируй (`sudo timedatectl set-ntp true` или вручную `sudo date -s "..."`).

3. **На ASTRA01** проверь SRV-записи:

    ```bash
    sudo apt install -y dnsutils
    dig +short SRV _kerberos._tcp.corp.vektor.lab
    dig +short SRV _ldap._tcp.corp.vektor.lab
    ```

    Ожидаемо: строки с `dc01.corp.vektor.lab.` и портами 88 и 389. Если пусто — DNS не тот, дальше идти нет смысла.

4. **На ASTRA01** установи пакеты:

    ```bash
    sudo apt update
    sudo apt install -y realmd sssd sssd-tools libnss-sss libpam-sss \
                        adcli samba-common-bin oddjob oddjob-mkhomedir \
                        packagekit krb5-user
    ```

    Ожидаемо: пакеты установлены без ошибок. На Astra CE имена могут отличаться [ПРОВЕРИТЬ].

5. **На ASTRA01** найди домен:

    ```bash
    realm discover corp.vektor.lab
    ```

    Ожидаемо: блок с `server-software: active-directory`, `client-software: sssd`, `configured: no`.

6. **На ASTRA01** введи машину в домен:

    ```bash
    sudo realm join -U adm.student corp.vektor.lab
    ```

    Введи пароль `Lab#2026-vektor`. Ожидаемо: `Successfully enrolled machine in realm` или тишина. Если ошибка — смотри шаг 11.

7. **На DC01** проверь, что учётка компьютера появилась:

    ```powershell
    Get-ADComputer -Filter 'Name -like "ASTRA01*"' | Select Name, DistinguishedName
    ```

    Ожидаемо: одна запись `ASTRA01` (или `ASTRA01$` в некоторых отображениях). Если её нет — join не прошёл.

8. **На ASTRA01** проверь Kerberos и SSSD:

    ```bash
    kinit ivanov@CORP.VEKTOR.LAB
    klist
    id ivanov@corp.vektor.lab
    ```

    Ожидаемо: `kinit` спрашивает пароль `ivanov` (`Lab#2026-vektor`), `klist` показывает TGT, `id` — UID/GID и группы, включая `domain users` и `GG-Buh` (если `ivanov` в этой группе по L1 №43).

9. **На ASTRA01** проверь вход доменного пользователя:

    ```bash
    su - ivanov@corp.vektor.lab
    pwd
    exit
    ```

    Ожидаемо: пускает, `pwd` показывает домашнюю папку (`/home/corp.vektor.lab/ivanov` или похожую [ПРОВЕРИТЬ]). Если папки нет — `oddjob-mkhomedir` не включён в PAM.

10. **На ASTRA01** проверь `realm list`:

    ```bash
    realm list
    ```

    Ожидаемо: `configured: kerberos-member`.

11. **Если join упал** — посмотри логи:

    ```bash
    sudo journalctl -u sssd -n 100 --no-pager
    sudo tail -n 100 /var/log/sssd/sssd_corp.vektor.lab.log
    ```

    Типовые строки: `Clock skew too great` (время), `Cannot contact any KDC` (DNS/сеть), `Preauthentication failed` (пароль), `Insufficient access` (права в AD).

12. **Запиши в журнал** `C:\Lab\vektor\stand-log.md`: «ASTRA01 введена в домен corp.vektor.lab, учётка компьютера создана, доменный вход ivanov проверен».

### Проверка

- [ ] `realm discover corp.vektor.lab` показывает `server-software: active-directory`.
- [ ] `realm list` показывает `configured: kerberos-member`.
- [ ] На DC01 `Get-ADComputer` находит `ASTRA01`.
- [ ] `kinit ivanov@CORP.VEKTOR.LAB` и `klist` дают TGT.
- [ ] `id ivanov@corp.vektor.lab` показывает UID и группы.
- [ ] `su - ivanov@corp.vektor.lab` пускает в домашнюю папку.
- [ ] В `stand-log.md` есть запись об изменении.

### Сломай и почини

**Заявка:**

> **Пользователь:** Иван Иванов (бухгалтерия), ВМ ASTRA01
> **Что написал:** «После обеда перестал пускать на ASTRA01 под доменной учёткой. Пишет "Authentication failure". Пароль точно правильный — на Windows (WIN11-CL01) вхожу. Срочно, отчёт!»
> **Срочность:** высокая

**Как воспроизвести поломку.** На ASTRA01 (под `adm.student`) сдвинь системное время на 10 минут вперёд:

```bash
sudo timedatectl set-ntp false
sudo date -s "+10 minutes"
```

После этого `kinit ivanov@CORP.VEKTOR.LAB` начнёт падать с `Clock skew too great` — Kerberos не выдаст билет, SSSD не пустит пользователя. Это ровно то, что видит пользователь.

**Диагностика по шагам:**

1. На ASTRA01: `timedatectl` — видим, что время ушло вперёд на 10 минут относительно реального.
2. Сравни с DC01: на DC01 в PowerShell `Get-Date` — время правильное.
3. Проверь `kinit ivanov@CORP.VEKTOR.LAB` — ошибка `Clock skew too great` подтверждает диагноз.
4. Проверь `journalctl -u sssd -n 50` — там будут строки про Kerberos preauthentication / clock skew.

**Решение:**

```bash
sudo timedatectl set-ntp true
# если NTP недоступен — вручную:
sudo date -s "2026-01-15 14:30:00"   # подставь реальное время
sudo systemctl restart sssd
```

После этого `kinit` и `su -` работают.

**Ответ пользователю:** «Иван, добрый день. Причина — рассинхронизация времени на ASTRA01 с контроллером домена: Kerberos не выдаёт билет, если разница больше 5 минут. Время синхронизировал, вход под доменной учёткой восстановлен. Проверьте, пожалуйста, и напишите, если ещё что-то не так.»

**Закрытие заявки:** категория «Аутентификация / AD», решение «синхронизация времени на Linux-клиенте», примечание «проверить, почему NTP отключился — возможно, ручное вмешательство или сбой chrony/systemd-timesyncd».

### Что изменилось на стенде

- ASTRA01 введена в домен `corp.vektor.lab`, в AD создана учётная запись компьютера `ASTRA01`.
- На ASTRA01 установлены пакеты `realmd`, `sssd`, `adcli`, `krb5-user`, `oddjob-mkhomedir` и др.
- На ASTRA01 настроены `/etc/sssd/sssd.conf`, `/etc/krb5.conf`, `/etc/nsswitch.conf`, PAM.
- Доменный пользователь `ivanov` может входить на ASTRA01.
- В `C:\Lab\vektor\stand-log.md` добавлена запись.

### Откат

1. На ASTRA01: `sudo realm leave corp.vektor.lab` (убирает конфиг и учётку компьютера).
2. Если нужно чисто — восстанови снапшот `before-l1-050` на ASTRA01 и DC01 в VMware Workstation: правый клик по ВМ → Snapshot Manager → выбрать `before-l1-050` → Restore.
3. Удали запись из `stand-log.md` или добавь строку «откат к before-l1-050».

## 6. Ошибки новичков и безопасность

1. **Неверный DNS на клиенте.** Ошибка → `realm discover` не находит домен, `kinit` не находит KDC. Правильно: перед join всегда проверять `/etc/resolv.conf` и `dig SRV`.
2. **Рассинхронизация времени.** Ошибка → `Clock skew too great`, пользователь не входит. Правильно: `timedatectl` до и после join, NTP включён.
3. **`realm join` без прав в AD.** Ошибка → `Insufficient access`. Правильно: использовать учётку с правами создать объект компьютера (в учебном стенде — `adm.student`), в боевой — заранее согласованную сервисную учётку.
4. **Забыть `oddjob-mkhomedir`.** Ошибка → пользователь входит, но без домашней папки, приложения падают. Правильно: ставить пакет и включать `pam_mkhomedir` в PAM.
5. **Путать FreeIPA и AD.** Ошибка → пытаются «ввести в AD» через `ipa-client-install`, ничего не работает. Правильно: FreeIPA — отдельный продукт, для AD используют realmd/SSSD или Samba/winbind.
6. **Дать доменному пользователю sudo «на всякий случай».** Ошибка → любой доменный пользователь — админ на Linux-машине. Правильно: только конкретная группа AD, только через `/etc/sudoers.d/`, согласовано с ИБ.
7. **Не проверить вход после join.** Ошибка → «вроде в домене», а пользователь не входит. Правильно: всегда `kinit` + `id` + `su -` после join.
8. **Игнорировать логи SSSD.** Ошибка → гадание вместо диагностики. Правильно: `journalctl -u sssd` и `/var/log/sssd/`.

### Безопасность

- В боевой среде нельзя вводить Linux-машину в домен под личной доменной админской учёткой — для этого выделяют сервисную учётку с минимальными правами (создание объектов компьютеров в конкретном OU).
- Нельзя давать доменным пользователям sudo без согласования: это обход модели доступа AD.
- Нельзя отключать SELinux/AppArmor и файрвол «чтобы заработало» — это маскирует настоящую причину (обычно DNS или порт).
- Пароли стенда (`Lab#2026-vektor`) — только учебные. В реальной среде такие пароли недопустимы.
- Логи SSSD и Kerberos могут содержать имена пользователей и служебную информацию — не выкладывай их в публичные чаты.

## 7. Вопросы на собеседовании

**Вопрос 1. «Как ввести Linux-машину в Active Directory?»**

Ответ: «Есть два основных пути. Первый — через `realmd` и SSSD: `realm discover`, `realm join -U <учётка> <домен>`, потом проверка `realm list`, `kinit`, `id`, `su -`. Второй — через Samba/winbind: `net ads join -U <учётка>`, настройка `smb.conf`, `wbinfo -t`, `wbinfo -u`. Перед этим обязательно проверить DNS (SRV-записи Kerberos и LDAP должны резолвиться на DC) и синхронизацию времени — иначе Kerberos не выдаст билет. После join проверить, что учётка компьютера появилась в AD, и что доменный пользователь реально входит.»

*Что хочет услышать интервьюер:* что ты знаешь про DNS, время, Kerberos и SSSD, а не только одну команду.
*Красный флаг:* «просто ставлю пакет и всё работает» без упоминания DNS и времени.

**Вопрос 2. «Почему пользователь не может войти на Linux-машине, хотя в домене он есть и пароль верный?»**

Ответ: «Самые частые причины: рассинхронизация времени больше 5 минут (Kerberos не выдаёт билет), неверный DNS на клиенте (не резолвятся SRV-записи), SSSD не запущен или не настроен, `nsswitch.conf` не содержит `sss`, PAM не настроен. Диагностика: `timedatectl`, `dig SRV _kerberos._tcp.<домен>`, `kinit <user>@<REALM>`, `journalctl -u sssd`. По ошибке `kinit` обычно сразу понятно, в чём дело.»

*Что хочет услышать интервьюер:* системную диагностику, а не «перезагрузите».
*Красный флаг:* «надо переустановить Linux».

**Вопрос 3. «Чем FreeIPA отличается от Active Directory и когда что используют?»**

Ответ: «FreeIPA — это отдельный продукт на Linux (LDAP + Kerberos + DNS + CA), часто используется в чисто Linux-инфраструктуре. AD — это Microsoft-домен, стандарт в корпоративной среде с Windows. Если компания уже на AD, Linux-машины вводят в AD через SSSD/realmd или Samba/winbind, а не через FreeIPA. FreeIPA выбирают, когда строят идентичность с нуля на Linux или нужна интеграция с Red Hat-стеком. Смешивать их в одном домене без отдельного проекта доверия — плохая идея.»

*Что хочет услышать интервьюер:* что ты не путаешь продукты и понимаешь контекст.
*Красный флаг:* «FreeIPA — это то же самое, что AD».

**Вопрос 4. «Что такое SSSD и зачем он нужен?»**

Ответ: «SSSD — служба, которая объединяет Kerberos, LDAP и кэш и отдаёт результат в NSS и PAM. Через неё Linux узнаёт доменных пользователей и проверяет их пароли. Настраивается в `/etc/sssd/sssd.conf`, обычно автоматически через `realm join`. Плюс — кэш: если DC временно недоступен, пользователь может войти по закэшированным данным. Логи — `journalctl -u sssd` и `/var/log/sssd/`.»

*Что хочет услышать интервьюер:* понимание, что SSSD — это «клей» между Linux и AD.
*Красный флаг:* «SSSD — это DNS-сервер».

**Вопрос 5. «Что проверить в первую очередь, если `realm join` падает с ошибкой?»**

Ответ: «Сначала DNS: `dig SRV _kerberos._tcp.<домен>` и `dig SRV _ldap._tcp.<домен>` должны возвращать DC. Потом время: `timedatectl`, разница с DC не больше 5 минут. Потом права: учётка должна иметь право создать объект компьютера в нужном OU. Потом сеть: порты 88, 389, 445, 53 до DC открыты. И только потом смотреть логи `journalctl -u sssd` и `/var/log/sssd/`.»

*Что хочет услышать интервьюер:* порядок проверок и понимание, что 90% проблем — DNS и время.
*Красный флаг:* «сразу переустанавливаю пакеты».

## 8. Источники (официальная документация)

- **Red Hat Documentation** (docs.redhat.com) — раздел «RHEL: Integrating RHEL systems directly with Windows Active Directory», поиск: `RHEL integrating with Active Directory realmd sssd`.
- **Red Hat Documentation** — «Managing certifiates / SSSD» [ПРОВЕРИТЬ точное название раздела], поиск: `RHEL SSSD configuration ad provider`.
- **Samba Project** (samba.org) — раздел «Samba as a Domain Member», поиск: `samba net ads join winbind domain member`.
- **Microsoft Learn** (learn.microsoft.com) — раздел «Active Directory Domain Services», поиск: `AD DS Kerberos LDAP integration Linux`.
- **Astra Linux** (astralinux.ru) — официальная документация по Astra Linux Common Edition, раздел про аутентификацию и домен [ПРОВЕРИТЬ точное название раздела и версию], поиск: `Astra Linux ввод в домен Active Directory`.
- **ALT Linux** (altlinux.org) — документация по вводу в домен AD через `system-auth` / `sssd` [ПРОВЕРИТЬ], поиск: `ALT Linux Active Directory join`.
- **РЕД ОС** (redos.red-soft.ru) — документация по интеграции с AD [ПРОВЕРИТЬ], поиск: `РЕД ОС ввод в домен Active Directory`.
- **FreeIPA** (freeipa.org) — официальный сайт, раздел «Documentation», поиск: `FreeIPA client install documentation` (для сравнения, не для ввода в AD).
