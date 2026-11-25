# Linux в домене: ввод Astra Linux/ALT/РЕД ОС в AD (FreeIPA, Samba, realmd)

## 1. Введение (зачем это нужно в реальной работе)

Заявка в Service Desk:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** ASTRA01 (10.10.10.31), терминал в бухгалтерии
> **Что написал:** «Поставили на ASTRA01 новую бухгалтерскую программу. Раньше я заходил туда под локальной учёткой, а теперь нужно под доменной — иначе программа не открывает `\\FS01\Buh`. Сделайте, чтобы я входил своим обычным паролем от Windows и сразу видел общую папку.»
> **Срочность:** средняя, до конца дня.

В Москве Linux в корпоративной сети — это не экзотика: файловые серверы, 1С-серверы, веб-прокси, системы видеонаблюдения, терминалы в бухгалтерии, Astra Linux на рабочих местах по импортозамещению. И почти всегда возникает один и тот же вопрос: «а можно, чтобы на этой Linux-машине работали те же логины и пароли, что в Windows?» Потому что держать вторую базу пользователей — это двойная работа и вечные расхождения: уволили сотрудника в AD, а на Linux he остался.

После этой главы ты должен уметь: объяснить, что происходит при вводе Linux в домен; проверить DNS и время (две причины 80% провалов); выполнить ввод через `realmd` и через Samba; проверить, что доменная учётка реально логинится; ограничить вход только нужными группами; и диагностировать «вчера работало, сегодня не пускает».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Главное, что нужно понять: **в Linux нет «домена» как встроенного понятия**. Есть два места, куда операционная система смотрит, когда ей нужно ответить на вопрос «кто этот пользователь?» и «пускать ли его?».

- **NSS** (Name Service Switch) — отвечает на вопрос «кто это». Через него работают `getent`, `id`, `ls -l`, `ps -u`. Настройка — `/etc/nsswitch.conf`. Чтобы доменные пользователи появились в системе, туда добавляется источник `sss` (или `winbind`).
- **PAM** (Pluggable Authentication Modules) — отвечает на вопрос «правильный ли пароль и что делать после входа» (создать домашний каталог, применить лимиты). Настройка — `/etc/pam.d/`. Именно сюда подключается проверка пароля через Kerberos.

Дальше — кто эти источники наполняет:

```text
   ЛОГИН (login, su, sshd, gdm)
        |                \
        | PAM             \ NSS  (getent, id, ls -l)
        v                  v
   +------------------------------------+
   |  SSSD  (демон sssd)                |   кэш пользователей и групп,
   |  nss + pam + домены                |   оффлайн-вход
   +------------------------------------+
        |  Kerberos 88 (билеты)   |  LDAP 389/636 (атрибуты, группы)
        |  DNS 53 (SRV-записи)    |  SMB 445 (смена пароля, GPO)
        v                         v
   +--------------------------------------------+
   |  DC01  10.10.10.10  corp.vektor.lab        |
   |  KDC (Kerberos) + LDAP + DNS + SYSVOL      |
   |  объект компьютера UBU01 в AD              |
   +--------------------------------------------+
        ^
        |  одноразовая операция «ввод в домен»:
        |  realmd + adcli  ИЛИ  samba (net ads join)
        |  результат: /etc/krb5.keytab + запись в AD
```

Роли инструментов:

| Инструмент | Что делает | Где живёт |
|---|---|---|
| `realmd` | «мастер настройки»: находит домен, ставит нужные пакеты, вызывает join | Ubuntu, Debian, РЕД ОС [ПРОВЕРИТЬ], Astra [ПРОВЕРИТЬ] |
| `adcli` | реально создаёт объект компьютера в AD и получает keytab | Debian/Ubuntu |
| `SSSD` | постоянная работа: логин, группы, кэш, оффлайн | везде |
| `winbind` + `net ads join` | альтернатива из мира Samba: то же самое, но «руками» | Samba, Astra (`astra-winbind`), ALT |
| `FreeIPA` | **не** AD, а отдельный каталог (389-ds + Kerberos + CA) | RHEL-семейство, отдельный сервер |
| ALD Pro / РЕД ИДМ | российские аналоги FreeIPA | Astra / РЕД ОС [ПРОВЕРИТЬ] |

**FreeIPA — это не «ввод в AD».** FreeIPA строит свой собственный домен (`IPA.VEKTOR.LAB`), со своим Kerberos и своими пользователями. Доверие между FreeIPA и AD (`ipa trust-add`) — задача уровня L2+. На L1 достаточно знать разницу: «ввести Linux в существующий AD» ≠ «развернуть FreeIPA».

**Порты**, которые нужны от Linux-клиента до DC01:

| Порт | Протокол | Зачем |
|---|---|---|
| 53 TCP/UDP | DNS | найти DC по SRV-записям `_ldap._tcp`, `_kerberos._tcp` |
| 88 TCP/UDP | Kerberos | получить билет (ticket) на вход |
| 464 TCP/UDP | kpasswd | смена пароля |
| 389 / 636 | LDAP | читать пользователей, группы, OU |
| 3268 / 3269 | Global Catalog | поиск в масштабе леса |
| 445 TCP | SMB | SYSVOL, GPO, смена пароля через AD |
| 123 UDP | NTP | синхронизация времени (критично для Kerberos) |

**Kerberos в двух словах:** пароль пользователя никогда не идёт по сети. Клиент просит у KDC (это DC01) «билет» — зашифрованный блок, который доказывает: «этот пользователь действительно тот, за кого себя выдаёт». Билет живёт ограниченное время и содержит метку времени. Поэтому **если часы на Linux и на DC расходятся больше чем на 5 минут, вход не работает** — это самая частая поломка после DNS. У машины (не человека) тоже есть свой «билет» — машинный аккаунт, его ключ хранится в файле `/etc/krb5.keytab`. Этот файл — фактически пароль компьютера: кто его скопировал, тот может представляться этой машиной в домене.

## 3. Аналогия из жизни

Представь завод с бюро пропусков. В бюро (это **DC01 / AD**) лежит база сотрудников и список, кому куда можно. На входе стоит турникет с вахтёром (**KDC**): сотрудник показывает паспорт, вахтёр сверяет с базой и выдаёт бумажный пропуск на сегодня (**Kerberos-билет**).

Теперь в цеху открыли новый участок (**Linux-машина**). У него свой отдельный турникет, и он ничего не знает про заводскую базу. Чтобы всё заработало, нужно: (1) поставить на участке вахтёра, который умеет звонить в бюро пропусков — это **SSSD**; (2) оформить сам участок как объект в бюро пропусков и выдать ему служебный пропуск — это **ввод в домен** и файл `keytab`; (3) наладить телефонную линию до бюро — это **DNS и сетевые порты**.

**realmd** — это монтажник, который приходит один раз, делает всё по чек-листу и уходит. **Samba/winbind** — тот же результат, но собранный вручную из готовых деталей.

Где аналогия ломается: у вахтёра на участке нет своих часов, он смотрит на заводские. Если часы на участке убежали на десять минут, вахтёр считает любой пропуск просроченным — и человек с правильным паспортом не проходит.

## 4. Практика и команды

**1. Проверка DNS — всегда первым делом.**

```bash
cat /etc/resolv.conf
resolvectl status | grep -A3 "DNS Servers"
dig +short SRV _ldap._tcp.corp.vektor.lab @10.10.10.10
```

Ожидаемый вывод последней команды: `0 100 389 dc01.corp.vektor.lab.` В `resolv.conf` должен быть `nameserver 10.10.10.10`. Если там 8.8.8.8 или адрес шлюза VMware — домен не найдётся вообще, и `realm discover` скажет «not found». Смотреть: есть ли SRV-запись и указывает ли она на DC01.

**2. Время.**

```bash
timedatectl
```

Смотреть строку `System clock synchronized: yes` и `NTP service: active`. Сравнить с DC01 (PowerShell на DC01):

```powershell
w32tm /stripchart /computer:10.10.10.30 /samples:3
```

В выводе `o:` — смещение (offset) в секундах. Больше ±5 минут — вход не заработает. Важно: сравнивается **UTC-время**, а не «часовой пояс на экране».

**3. Поиск домена (discovery).**

```bash
sudo apt update
sudo apt install -y realmd sssd sssd-tools adcli krb5-user \
    samba-common-bin oddjob oddjob-mkhomedir packagekit
realm discover corp.vektor.lab
```

Типичный (сокращённый) вывод:

```text
corp.vektor.lab
  type: kerberos
  realm-name: CORP.VEKTOR.LAB
  domain-name: corp.vektor.lab
  configured: no
  server-software: active-directory
  client-software: sssd
  required-package: sssd-tools
  required-package: adcli
  login-formats: %U
```

Смотреть: `server-software: active-directory` (значит, нашли именно AD, а не чужой Kerberos) и `configured: no` до ввода в домен.

**4. Проверка Kerberos до ввода.**

```bash
kinit adm.student@CORP.VEKTOR.LAB
klist
```

`klist` покажет выданный билет `krbtgt/CORP.VEKTOR.LAB@CORP.VEKTOR.LAB`. Если `kinit` падает с `Clock skew too great` — проблема во времени, если `Cannot find KDC` — в DNS. После проверки: `kdestroy`.

**5. Ввод в домен через realmd.**

```bash
sudo realm join --user=adm.student corp.vektor.lab
sudo realm list
```

`realm join` спросит пароль (`Lab#2026-vektor`), создаст объект компьютера в AD, положит ключи в `/etc/krb5.keytab` и сгенерирует `/etc/sssd/sssd.conf`. `configured: kerberos-member` в `realm list` = машина в домене. Полезный ключ: `--computer-ou="OU=Компьютеры,OU=Company,DC=corp,DC=vektor,DC=lab"` — чтобы объект попал в нужное OU, а не в контейнер `CN=Computers`.

**6. Настройка SSSD.**

```bash
sudo nano /etc/sssd/sssd.conf
```

```ini
[sssd]
domains = corp.vektor.lab
config_file_version = 2
services = nss, pam

[domain/corp.vektor.lab]
ad_domain = corp.vektor.lab
krb5_realm = CORP.VEKTOR.LAB
realmd_tags = manages-system joined-with-adcli
cache_credentials = True
id_provider = ad
krb5_store_password_if_offline = True
default_shell = /bin/bash
ldap_id_mapping = True
use_fully_qualified_names = False
fallback_homedir = /home/%u@%d
access_provider = ad
```

`use_fully_qualified_names = False` — чтобы писать `ivanov`, а не `ivanov@corp.vektor.lab`. `ldap_id_mapping = True` — AD не хранит Linux-UID, SSSD вычисляет их сам из SID. `access_provider = ad` — фильтр «кому можно входить». После правки: `sudo chmod 600 /etc/sssd/sssd.conf && sudo systemctl restart sssd` (SSSD не запустится с правами шире 0600).

**7. Ограничение входа по группам.**

```bash
sudo realm deny --all
sudo realm permit --groups GG-Buh
sudo realm permit --groups GG-IT
```

Теперь ivanov (GG-Buh) входит, а petrova (GG-HR) — нет. Это правильная модель: «разрешено только тем, кому надо», а не «пускаем весь домен» [ПРОВЕРИТЬ точное имя опции `--groups` в вашей версии realmd].

**8. Проверка, что учётка видна системе.**

```bash
getent passwd ivanov
id ivanov@corp.vektor.lab
sssctl user-checks ivanov
```

`getent passwd` вернёт строку вида `ivanov:*:1234567:1234567:Иван Иванов:/home/ivanov@corp.vektor.lab:/bin/bash`. Пусто — значит NSS не подключён или SSSD не запустился.

**9. Состояние домена и кэша.**

```bash
sssctl domain-status corp.vektor.lab
sudo sssctl cache-expire -a
sudo systemctl restart sssd
```

`domain-status` показывает `Online status: Online` и активный сервер. Если `Offline` — проверяй сеть/DNS. `cache-expire` нужен, когда группу в AD поменяли, а Linux «не видит»: SSSD держит кэш.

**10. Домашний каталог при первом входе.**

```bash
sudo pam-auth-update --enable mkhomedir
grep mkhomedir /etc/pam.d/common-session
```

Альтернатива, если профиля нет в списке — добавить руками в `/etc/pam.d/common-session` строку:
`session required pam_mkhomedir.so skel=/etc/skel umask=0077`.

**11. Samba-путь (так делают на Astra Linux и ALT).**

```bash
sudo apt install -y samba winbind libnss-winbind libpam-winbind
sudo nano /etc/samba/smb.conf
```

```ini
[global]
   workgroup = VEKTOR
   realm = CORP.VEKTOR.LAB
   security = ads
   idmap config * : backend = tdb
   idmap config * : range = 3000-7999
   idmap config VEKTOR : backend = rid
   idmap config VEKTOR : range = 10000-99999
   template shell = /bin/bash
   template homedir = /home/%U
   winbind use default domain = yes
```

```bash
sudo net ads join -U adm.student
sudo systemctl restart smbd winbind
sudo net ads testjoin      # ожидаем: Join is OK
sudo wbinfo -t             # ожидаем: checking the trust secret ... succeeded
sudo wbinfo -u             # список доменных пользователей
```

На Astra Linux Common Edition есть пакет `astra-winbind`, который делает часть этой настройки за тебя, и графический модуль в «Центре управления системой» → раздел аутентификации/домена [ПРОВЕРИТЬ название модуля в вашей версии]. На ALT Linux аналогичный путь — метапакет `task-auth-ad-sssd` и утилита `system-auth write ad ...` в `alterator-auth` [ПРОВЕРИТЬ]. В РЕД ОС штатный пакетный менеджер — `dnf`, пакеты `realmd`/`sssd`/`adcli` [ПРОВЕРИТЬ наличие в репозитории] и модуль в «Центре управления».

**12. Проверка доступа к файловому ресурсу под доменной учёткой.**

```bash
smbclient -L //fs01.corp.vektor.lab -U 'ivanov@corp.vektor.lab'
smbclient //fs01.corp.vektor.lab/Buh -U 'ivanov@corp.vektor.lab'
```

Внутри `smbclient` — `ls`, затем `exit`. Если пускает в `Obshchee`, но не в `Buh` — это права NTFS/шары на FS01 (тема урока 46), а не проблема ввода в домен.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **DC01** (10.10.10.10): AD DS развёрнут (урок 42), есть OU `Company` → `Бухгалтерия`, `HR`, `Склад`, `IT`, `Компьютеры`, `Серверы`; пользователи `ivanov`, `petrova`, `sidorov`; группы `GG-Buh`, `GG-HR`, `GG-Sklad`, `GG-IT`; учётка `adm.student` — администратор домена.
- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — доступ по SSH/консоли, есть sudo.
- **ASTRA01** (10.10.10.31, Astra Linux Common Edition) — доступ по консоли.
- **FS01** (10.10.10.20) с шарой `\\FS01\Buh` — понадобится только на шаге 10.
- Обе Linux-машины имеют выход в сеть VMnet8; DC01 — единственный DNS.
- Если FS01 ещё не поднят, пройди уроки 46–47, шаг 10 можно пропустить.

### Снапшот

В VMware Workstation на **DC01, UBU01 и ASTRA01**: меню `VM` → `Snapshot` → `Take Snapshot…`, имя — `before-l1-050`. (Или `VM` → `Snapshot Manager` → `Take`.) Снапшот контроллера домена в лабе допустим, потому что DC один и реплик нет; в боевой среде откат снапшота DC — плохая практика (USN rollback).

### Шаги

1. **UBU01.** Проверь адрес и DNS: `ip -br a`, `resolvectl status`. Ожидаем: адрес из пула 10.10.10.100–150, в DNS-серверах — `10.10.10.10`. Если не так — поправь `/etc/netplan/*.yaml` (`nameservers: addresses: [10.10.10.10]`), затем `sudo netplan apply`.
2. **UBU01.** `dig +short SRV _ldap._tcp.corp.vektor.lab @10.10.10.10` → одна строка `0 100 389 dc01.corp.vektor.lab.`
3. **UBU01 + DC01.** Сверь время: `timedatectl` на UBU01, `w32tm /query /status` и `w32tm /stripchart /computer:10.10.10.30 /samples:3` на DC01. Ожидаем: `System clock synchronized: yes`, смещение — секунды.
4. **UBU01.** Установи пакеты (команда из п.4.3) и выполни `realm discover corp.vektor.lab`. Ожидаем `server-software: active-directory`, `configured: no`.
5. **UBU01.** `kinit adm.student@CORP.VEKTOR.LAB` (пароль `Lab#2026-vektor`), затем `klist`. Ожидаем билет `krbtgt/CORP.VEKTOR.LAB@CORP.VEKTOR.LAB`. Выполни `kdestroy`.
6. **UBU01.** `sudo realm join --user=adm.student corp.vektor.lab`. Ожидаем: запрос пароля, затем тишину и код возврата 0 (`echo $?`). Проверь: `realm list` → `configured: kerberos-member`, `login-formats: %U`.
7. **DC01.** В PowerShell: `Get-ADComputer -Filter "Name -like 'UBU*'" | Select-Object Name, DistinguishedName, Enabled`. Ожидаем: `UBU01` в `CN=Computers,DC=corp,DC=vektor,DC=lab`, `Enabled: True`.
8. **UBU01.** Приведи `/etc/sssd/sssd.conf` к виду из п.4.6, `sudo chmod 600 /etc/sssd/sssd.conf`, `sudo systemctl restart sssd`, `systemctl status sssd` → `active (running)`.
9. **UBU01.** `sudo realm deny --all`, затем `sudo realm permit --groups GG-Buh` и `--groups GG-IT`. Включи создание домашних каталогов (п.4.10).
10. **UBU01.** Проверки: `id ivanov@corp.vektor.lab` → группа `gg-buh@corp.vektor.lab` в списке; `su - ivanov` (пароль `Lab#2026-vektor`) → приглашение шелла и путь `/home/ivanov@corp.vektor.lab`; `exit`. Затем `su - petrova` → ожидаем отказ (не в разрешённых группах). Под ivanov: `smbclient //fs01.corp.vektor.lab/Buh -U 'ivanov@corp.vektor.lab'` → `ls` показывает файлы, `exit`.
11. **ASTRA01.** Ввод через Samba: `sudo apt install -y astra-winbind` (или набор пакетов из п.4.11) [ПРОВЕРИТЬ состав пакета в вашей версии], правка `/etc/samba/smb.conf` по образцу, `sudo net ads join -U adm.student`, `sudo systemctl restart smbd winbind`, `sudo net ads testjoin` → `Join is OK`, `sudo wbinfo -t` → `succeeded`, `sudo wbinfo -u` → список доменных пользователей.
12. **ASTRA01.** `getent passwd ivanov` и `sudo su - ivanov` → вход работает, домашний каталог создаётся (если нет — включи `pam_mkhomedir` по п.4.10). Запиши результат в `stand-log.md`.

### Проверка

- [ ] `dig +short SRV _ldap._tcp.corp.vektor.lab @10.10.10.10` возвращает запись DC01
- [ ] `realm list` на UBU01: `configured: kerberos-member`, `login-formats: %U`
- [ ] `/etc/krb5.keytab` существует, права `600 root:root`
- [ ] `getent passwd ivanov` возвращает строку с `/bin/bash`
- [ ] `su - ivanov` на UBU01 проходит с паролем `Lab#2026-vektor`
- [ ] `su - petrova` на UBU01 отклоняется (ограничение по группам)
- [ ] В AD есть объект компьютера `UBU01`, в свойствах `OperatingSystem` = Ubuntu [ПРОВЕРИТЬ, заполняет ли adcli это поле]
- [ ] На ASTRA01 `net ads testjoin` → `Join is OK`
- [ ] ivanov открывает `\\FS01\Buh` с Linux-машины
- [ ] Все изменения записаны в `stand-log.md`

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** UBU01
> **Что написал:** «Утром не могу зайти на терминал под своим доменным паролем. Пишет `Authentication failure`, пароль точно правильный — в Windows на WIN11-CL01 он работает.»
> **Срочность:** высокая, работа стоит.

**Как воспроизвести поломку.** На UBU01 от имени root:

```bash
sudo timedatectl set-ntp false
sudo date -s "+11 minutes"
sudo systemctl restart sssd
```

Теперь `su - ivanov` падает, хотя пароль верный.

**Диагностика по шагам (именно в этом порядке):**

1. Повторить проблему и посмотреть точный текст: `su - ivanov`. Часто там прямо написано `Authentication failure`.
2. Проверить, жив ли SSSD: `systemctl status sssd` → `active (running)`. Упавший демон сразу объясняет всё.
3. Проверить домен: `sssctl domain-status corp.vektor.lab`. `Online` — сеть и DNS живы, проблема не в них.
4. **Проверить время** — `timedatectl`. Если `NTP service: inactive` и время «уехало», это оно. Сверить с DC01: `w32tm /stripchart /computer:10.10.10.30 /samples:3` — смещение в сотни секунд.
5. Посмотреть лог SSSD: `sudo tail -n 50 /var/log/sssd/sssd_corp.vektor.lab.log` — ищем `Clock skew too great` / `KRB5KRB_AP_ERR_SKEW`. Это подтверждает диагноз.
6. На всякий случай исключить вторую частую причину — удалённый/отключённый объект компьютера: на DC01 `Get-ADComputer UBU01 | Select Enabled`. Если `False` — учётку включить (`Enable-ADAccount`) и перезайти.
7. Проверить, что вход режет не группа: `sssctl user-checks ivanov` — если access provider даёт `denied`, дело в `realm permit`, а не во времени.

**Решение.**

```bash
sudo timedatectl set-ntp true
sudo systemctl restart systemd-timesyncd
timedatectl            # ждём System clock synchronized: yes
sudo systemctl restart sssd
su - ivanov            # проверка
```

**Ответ пользователю:** «Иван, добрый день. Причина — рассинхронизация времени на терминале после ночного обслуживания: контроллер домена отклонял вход из-за разницы часов. Время синхронизировал, вход под доменной учёткой проверил — работает. Если повторится, сразу пишите: это одна минута на исправление».

**Закрытие заявки:** категория «Аутентификация / домен», в решении — «восстановлена синхронизация времени (NTP), SSSD перезапущен, вход проверен», привязка к конфигурационной единице UBU01.

### Что изменилось на стенде

- **UBU01**: введён в домен `corp.vektor.lab`; установлены `realmd`, `sssd`, `sssd-tools`, `adcli`, `krb5-user`, `samba-common-bin`, `oddjob`, `oddjob-mkhomedir`, `packagekit`; создан `/etc/krb5.keytab`; настроен `/etc/sssd/sssd.conf` (`use_fully_qualified_names = False`, `fallback_homedir = /home/%u@%d`); вход разрешён группам `GG-Buh`, `GG-IT`; включён `pam_mkhomedir`.
- **ASTRA01**: введён в домен через Samba/winbind (workgroup `VEKTOR`, realm `CORP.VEKTOR.LAB`, idmap rid 10000–99999), `winbind` работает, доменные пользователи видны через `getent`.
- **DC01 (AD)**: появились объекты компьютеров `UBU01` и `ASTRA01` (по умолчанию `CN=Computers`).
- **Проверено**: ivanov входит на UBU01 и открывает `\\FS01\Buh`; petrova на UBU01 не входит.
- Учебная особенность: пароль доменного администратора вводился вручную в консоли лабы; в боевой среде так не делают (см. раздел 6).

### Откат

1. На UBU01 и ASTRA01 (если хочешь чистые машины): `sudo realm leave --user=adm.student corp.vektor.lab` или `sudo net ads leave -U adm.student`, затем в VMware `VM` → `Snapshot Manager` → выбрать `before-l1-050` → `Go To`.
2. На DC01 откат снапшота уберёт и объекты компьютеров. Если DC01 не откатываешь — удали объекты вручную: `Remove-ADComputer -Identity UBU01` (аналогично `ASTRA01`).
3. После любого отката почисти кэш SSSD (`sudo sssctl cache-expire -a`), чтобы не остались «призрачные» пользователи, и обнови `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Не проверили DNS → `realm discover` ничего не находит.** Классика: в `/etc/resolv.conf` остался DNS провайдера или шлюз VMware. → Всегда начинай с `dig SRV _ldap._tcp.corp.vektor.lab @10.10.10.10` и только потом вводи в домен.
2. **Разное время → «Clock skew too great».** Проверяют пароли, группы, перезагружают — а дело в часах. → Перед вводом всегда `timedatectl` + сверка с DC01; в боевой среде NTP обязателен на всех серверах.
3. **Ввод в домен под своей админской учёткой.** В лабе — `adm.student`, ок. В бою для этого заводят отдельную сервисную учётку с правом создавать объекты только в конкретном OU. → Не используй пароль доменного администратора в скриптах и не оставляй его в истории bash (`HISTIGNORE`, `history -c`).
4. **Права на `/etc/sssd/sssd.conf` шире 0600.** SSSD просто не стартует с «подозрительным» файлом. → `sudo chmod 600 /etc/sssd/sssd.conf && sudo systemctl restart sssd`.
5. **Забыли `use_fully_qualified_names = False`.** Все команды требуют `ivanov@corp.vektor.lab`, пользователь злится, скрипты падают. → Решить заранее и одинаково на всех машинах.
6. **Пустой фильтр входа (`access_provider` без ограничений).** В систему может зайти любой доменный аккаунт, включая сервисные и админские. → `realm deny --all` + `realm permit --groups GG-...` или `simple_allow_groups`.
7. **Кэш SSSD после смены групп.** Пользователя добавили в `GG-Buh`, а Linux «не видит» доступ. → `sudo sssctl cache-expire -a` и повторный вход; кэш — это фича (оффлайн-вход), но про него забывают.
8. **Ручная правка `/etc/nsswitch.conf` и `/etc/pam.d/` без бэкапа.** Ломается локальный вход, и ты остаёшься без root на машине. → Держи вторую консоль root открытой, копируй файлы в `.bak`, проверяй `getent passwd` до перезагрузки.

**Безопасность.** Файл `/etc/krb5.keytab` — это пароль машины: нельзя копировать его на другие хосты и хранить в git. Доменных пользователей не надо автоматически добавлять в `sudo` — вход в систему и административные права это разные вещи. Не открывай порты 88/389/445 с Linux-клиента наружу периметра. На Astra Linux в режиме защищённой ОС дополнительно работает мандатный контроль доступа — там ввод в домен и настройку прав согласовывают с ИБ, а не делают «по статье из интернета». В лабе мы, для простоты, один раз отключаем ограничения (`realm deny --all` → `permit` по группам) и работаем учебным паролем `Lab#2026-vektor` — в реальной компании такой пароль недопустим, а список разрешённых групп должен быть минимальным.

## 7. Вопросы на собеседовании (3–5 вопросов с ответами)

**1. Как ввести Linux-машину в домен Active Directory? Какие компоненты участвуют?**

*Ответ:* Проверяю DNS (клиент должен использовать DC как DNS-сервер, иначе не найдутся SRV-записи) и синхронизацию времени. Устанавливаю `realmd`, `sssd`, `adcli`, `krb5-user`, `samba-common-bin`. Выполняю `realm discover corp.vektor.lab` — убеждаюсь, что найден именно AD, затем `realm join --user=<учётка> corp.vektor.lab`. Команда создаёт объект компьютера в AD и файл `/etc/krb5.keytab`, генерирует `/etc/sssd/sssd.conf`. Дальше правлю конфиг SSSD, включаю создание домашних каталогов через PAM, ограничиваю вход по группам и проверяю `getent passwd`, `id`, `su -`. Альтернативный путь — Samba/winbind: `net ads join`, `wbinfo -t`.
*Что хочет услышать интервьюер:* порядок «DNS → время → discover → join → sssd.conf → проверка», понимание, что SSSD закрывает и NSS, и PAM.
*Красный флаг:* «ставлю winbind, правлю smb.conf и всё» без диагностики DNS и времени; или «ставлю FreeIPA» как ответ на вопрос про ввод в существующий AD.

**2. Пользователь не может войти на Linux-сервер под доменной учёткой. Ваши действия?**

*Ответ:* Сначала точный текст ошибки и когда это началось. Дальше по цепочке: 1) `systemctl status sssd` — работает ли демон; 2) `sssctl domain-status corp.vektor.lab` — домен в сети или offline; 3) `timedatectl` и сверка с DC (`w32tm /stripchart`) — расхождение часов; 4) `getent passwd <user>` — видит ли система учётку; 5) `sssctl user-checks <user>` — не режет ли access provider или группы; 6) лог `/var/log/sssd/sssd_<домен>.log`; 7) на DC — включён ли объект компьютера и не заблокирована ли учётка. 8) Кэш: `sssctl cache-expire -a`.
*Что хочет услышать интервьюер:* что ты идёшь по слоям (сервис → домен → время → права → AD), а не перезагружаешь наугад.
*Красный флаг:* «переустановлю SSSD и заново введу в домен» без единой проверки; «почищу keytab».

**3. Чем SSSD отличается от winbind и что выбрать?**

*Ответ:* Оба дают доменных пользователей через NSS и PAM. SSSD — отдельный демон с продуманным кэшем и оффлайн-входом, лучше работает с AD (GPO-фильтры, доверия), в связке с realmd/adcli это путь по умолчанию в Debian/Ubuntu и РЕД ОС. Winbind — часть Samba, часто безальтернативен там, где нужен именно Samba-стек (Astra Linux `astra-winbind`, ALT), и хорошо ложится на политику конкретного дистрибутива. В одном домене на одной машине держать оба нельзя.
*Что хочет услышать интервьюер:* понимание, что это конкурирующие клиенты одного и того же, и выбор зависит от дистрибутива и требований, а не «что первое вспомнил».
*Красный флаг:* «winbind — старьё, SSSD — новьё» без понимания, что на Astra/ALT штатно поддерживается winbind.

**4. Что такое keytab и почему к нему особое отношение?**

*Ответ:* `/etc/krb5.keytab` — набор ключей машинного аккаунта, по сути пароль компьютера для Kerberos. С ним машина получает билеты для служб (например, для SMB) без ввода пароля. Если файл утёк, злоумышленник может представляться этой машиной. Поэтому права минимум (0600 root:root), нельзя копировать на другие хосты и в репозитории, при компрометации — перевыпуск: переустановить машинный пароль (переввод в домен / `adcli update`).
*Что хочет услышать интервьюер:* понимание, что keytab = секрет, а не «настройка».
*Красный флаг:* «просто скопирую keytab на второй сервер, чтобы не вводить его в домен».

**5. Что нужно открыть на файрволе между Linux-клиентом и контроллером домена?**

*Ответ:* DNS 53 (TCP/UDP), Kerberos 88, kpasswd 464, LDAP 389 (и 636 для LDAPS), Global Catalog 3268/3269, SMB 445 (SYSVOL, GPO, смена пароля), NTP 123. Обычно этого достаточно и больше ничего не требуется.
*Что хочет услышать интервьюер:* знание, что проблема «не вводится в домен» часто упирается в закрытые порты, и что DNS и время — базовые требования Kerberos.
*Красный флаг:* «откроем всё, чтобы точно работало».

## 8. Источники (официальная документация)

- **Microsoft Learn** (`learn.microsoft.com`) — раздел «Active Directory Domain Services»: обзор AD DS, Kerberos, требования к DNS. Поиск: `Active Directory Domain Services overview`.
- **Ubuntu Server documentation** (`documentation.ubuntu.com`) — раздел про аутентификацию/SSSD и подключение к Active Directory. Поиск: `Ubuntu Server SSSD Active Directory integration`.
- **realmd** (`freedesktop.org`) — man-страницы `realm`, `realmd`: `man realm`, поиск `realmd documentation`. [ПРОВЕРИТЬ текущую страницу проекта]
- **SSSD** (`sssd.io`) — документация по `sssd.conf`, `sssctl`, кэшированию. Поиск: `sssd.conf man page`, `sssctl domain-status`.
- **Samba Wiki** (`wiki.samba.org`) — «Samba as a domain member», настройка `winbind` и idmap. Поиск: `Samba as a domain member idmap config`.
- **FreeIPA** (`freeipa.org`) — разделы «Trusts» и «Client installation». Поиск: `FreeIPA AD trust`.
- **Astra Linux** (`wiki.astralinux.ru`) — руководство администратора, раздел про ввод в домен AD (пакет `astra-winbind`) и про ALD Pro. Поиск: `Astra Linux ввод в домен Active Directory astra-winbind`. [ПРОВЕРИТЬ названия модулей GUI в вашей версии]
- **ALT Linux** (`docs.altlinux.org`) — `alterator-auth`, `system-auth`, `task-auth-ad-sssd`. Поиск: `ALT Linux аутентификация Active Directory alterator`.
- **РЕД ОС** (`redos.red-soft.ru`) — документация администратора, раздел про домен/аутентификацию. Поиск: `РЕД ОС ввод в домен Active Directory`. [ПРОВЕРИТЬ актуальный раздел]

Дистрибутивы бери только официально: Ubuntu — с `ubuntu.com`, Astra Linux Common Edition и РЕД ОС — с сайтов вендоров (есть бесплатные базовые редакции), ALT — `getalt.org`. Скачивание с торрентов «сборок» — прямой путь к претензии от ИБ и к неизвестному содержимому внутри.
