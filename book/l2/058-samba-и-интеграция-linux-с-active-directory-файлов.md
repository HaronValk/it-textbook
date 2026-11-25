# Samba и интеграция Linux с Active Directory: файловые шары, ввод в домен, Kerberos

## 1. Введение (зачем это нужно в реальной работе)

Тикет #4712. Пользователь: ivanov. ВМ: WIN11-CL01. Что написал: «Мне сказали, что сканы для 1С теперь лежат на `\\UBU01\Buh-Linux`. Открываю — не пускает, просит логин-пароль. Логин-пароль доменный вводил, не помогает. Срочность: средняя, но отчёт в пятницу».

За этим тикетом стоит типовая задача любого L2 в Москве: в компании есть Windows-домен на Active Directory, но часть сервисов крутится на Linux. И эти Linux-серверы должны отдавать файлы доменным пользователям так, чтобы админ не заводил на каждом сервере «локального ivanov» и не сбрасывал ему пароль вручную. Плюс обратная задача — сам Linux-сервер должен уметь ходить в шары на Windows-файловом сервере (FS01) под доменной учёткой, без хранения пароля в скрипте.

В РФ 2026 года это встречается постоянно: 1С на Linux, Astra Linux на рабочих местах с доступом к AD, шлюзы и прокси на Debian, которые должны авторизовывать пользователей через домен, СЭД и сканеры, пишущие по SMB. Умение ввести Linux в AD, настроить Samba как файловый сервер и разобраться, почему Kerberos «не пускает», — это водораздел между «нажимаю кнопки по инструкции» и «понимаю, что происходит».

После главы ты должен уметь: проверить готовность Linux к вводу в домен (DNS, время), ввести машину через `realmd`/`sssd`, читать `klist` и понимать, что такое keytab, настроить SMB-шару с правами по доменным группам, смонтировать шару FS01 через CIFS/Kerberos и диагностировать типовые отказы доступа по логам.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**SMB** (Server Message Block) — прикладной протокол доступа к файлам и принтерам, седьмой уровень OSI. Работает поверх TCP **445** (современный вариант) и NetBIOS over TCP/IP — UDP **137** (служба имён, `nmbd`), UDP **138** (дейтаграммы), TCP **139** (сессии). Старое имя CIFS — это диалект SMB1, в 2026 году его включают только под древний сканер и сразу жалеют об этом.

Роли Samba:
- **standalone** — локальная база пользователей (`smbpasswd`), никакого AD;
- **member server** — машина введена в домен, но контроллером не является; учётки берёт из AD (наш случай для UBU01);
- **AD DC** — Samba 4 сама может быть контроллером домена. В РФ на этом построен **Astra Linux Directory (ALD)** — форк Samba 4 для Astra Linux `[ПРОВЕРИТЬ актуальную версию ALD]`.

**Kerberos** — протокол аутентификации на тикетах. В домене AD контроллер домена (DC01) одновременно является **KDC** (Key Distribution Center). Порты: **88/TCP+UDP** (Kerberos), **464** (смена пароля), **389/636** (LDAP/LDAPS), **135 + динамические RPC** (службы Windows), **53** (DNS), **123/UDP** (NTP).

Схема потока:

```text
┌────────────────┐  1. kinit ivanov + пароль        ┌──────────────────────┐
│ UBU01          │ ───────────────────────────────► │ DC01                 │
│ (клиент)       │ ◄─────────────────────────────── │ KDC + DNS + LDAP     │
└───────┬────────┘  2. TGT (билет-пропуск)          │ corp.vektor.lab      │
        │                                           └──────────▲───────────┘
        │ 3. «дай билет на cifs/ubu01.corp.vektor.lab»         │
        └──────────────────────────────────────────────────────┘
        │ 4. service ticket, зашифрованный ключом машины UBU01
        ▼
   smbd на UBU01 ── берёт ключ из /etc/krb5.keytab ── расшифровал ──► доступ
        │
        └─ SID пользователя из AD ──► idmap ──► UID/GID Linux ──► права на файлы
```

Два ключевых момента, на которых ломается большинство внедрений:

1. **DNS.** Клиент ищет KDC по SRV-записям `_kerberos._tcp.corp.vektor.lab`. Если в `/etc/resolv.conf` на UBU01 прописан 8.8.8.8, домен «не находится». На доменном члене DNS-сервером обязан быть DC.
2. **Время.** Kerberos разрешает расхождение часов по умолчанию 5 минут. Разъехались — получаешь `KRB5KRB_AP_ERR_SKEW` и «отказано в доступе» без внятного объяснения.

Третий, менее очевидный: **idmap**. В AD пользователь идентифицируется **SID** (`S-1-5-21-...-1105`), а в Linux — числовым **UID**. Однозначного соответствия нет, поэтому его вычисляют по правилам (backend'ы `rid`, `autorid`, `sss`, `ad`). Если Samba и SSSD считают UID по разным правилам, файл, созданный по SSH, для `smbd` принадлежит «другому» пользователю — и доступ mysteriously пропадает.

## 3. Аналогия из жизни

Представь бизнес-центр с пропускной системой. На ресепшене (KDC) ты один раз показываешь паспорт и получаешь **дневной пропуск в холл** — это TGT. Дальше в холле стоит автомат: показываешь дневной пропуск — получаешь **разовый пропуск в конкретный кабинет** (service ticket). В кабинет тебя пускает не охранник на ресепшене, а замок на двери: он проверяет, что пропуск зашифрован именно ключом этого кабинета. Ресепшн больше не участвует и твой пароль не знает.

Где аналогия перестаёт работать: пропуск в кабинете одноразовый только формально — service ticket живёт часами и кэшируется у тебя в кармане (файл `krb5ccache`), поэтому «выйти из системы» и «уничтожить билет» — это `kdestroy`, отдельное действие, о котором часто забывают.

## 4. Практика и команды

**1. Проверка «видит ли Linux домен»**

```bash
realm discover corp.vektor.lab
```
```text
corp.vektor.lab
  type: kerberos
  realm-name: CORP.VEKTOR.LAB
  domain-name: corp.vektor.lab
  configured: no
  server-software: active-directory
  client-software: sssd
```
`realm` — надстройка над `sssd`/`adcli`. Смотри `server-software` (должно быть `active-directory`) и `configured`. Если «Couldn't find a Kerberos realm» — проблема в DNS, а не в Samba.

**2. Ввод в домен**

```bash
sudo realm join --user=adm.student \
  --computer-ou='OU=Серверы,DC=corp,DC=vektor,DC=lab' \
  -v corp.vektor.lab
realm list
```
`--computer-ou` кладёт компьютерную учётку в нужный OU (точный DN посмотри в ADUC: View → Advanced Features). `-v` — подробный вывод, там видно, на каком шаге упало. `realm list` показывает, что машина в домене, и каким клиентским софтом (`sssd`).

**3. Kerberos вручную**

```bash
kinit adm.student@CORP.VEKTOR.LAB     # realm ОБЯЗАТЕЛЬНО в верхнем регистре
klist                                  # что за билеты и до какого времени
klist -k /etc/krb5.keytab              # ключи машины (service principal)
kdestroy                               # выбросить билеты
```
```text
Valid starting       Expires              Service principal
28.05.2026 10:12:03  28.05.2026 20:12:03  krbtgt/CORP.VEKTOR.LAB@CORP.VEKTOR.LAB
```
Смотри срок: истёк TGT — все SMB/Kerberos-операции отвалятся.

**4. Проверка, что компьютерная учётка жива**

```bash
sudo adcli testjoin -D corp.vektor.lab     # «Successfully validated join»
sudo net ads testjoin                      # альтернатива через Samba
sudo net ads info
```
`net ads info` в конце печатает **KDC time offset** — быстрый способ увидеть расхождение часов.

**5. Видит ли система доменных пользователей**

```bash
getent passwd ivanov
id ivanov
getent group gg-buh
```
```text
ivanov:*:201105:201513:Иван Иванов:/home/ivanov:/bin/bash
```
Если пусто — смотри `sssd`, а не Samba. UID здесь выдал SSSD; запомни его, он должен совпадать с тем, что использует `smbd`.

**6. Диагностика SSSD**

```bash
sudo sssctl domain-status corp.vektor.lab   # online/offline, какой сервер выбран
sudo sssctl user-checks ivanov              # по шагам: разрешён ли вход, какие группы
sudo sss_cache -E                           # сбросить кэш после правок в AD
journalctl -u sssd -n 50 --no-pager
```

**7. Проверка конфига Samba до перезапуска**

```bash
sudo testparm -s
```
Парсит `/etc/samba/smb.conf`, ругается на неизвестные параметры и печатает итоговый конфиг. Никогда не перезапускай `smbd` с битым конфигом на живом сервере.

**8. Просмотр шар снаружи**

```bash
smbclient -L //fs01.corp.vektor.lab -U adm.student
smbclient -k //fs01.corp.vektor.lab/Buh -c 'ls'
```
`-L` — список ресурсов, `-k` — авторизация по уже имеющемуся Kerberos-билету (нужен предварительный `kinit`).

**9. Кто сейчас на шаре**

```bash
sudo smbstatus -b      # кратко: кто подключён, под кем, к какой шаре
sudo smbstatus -L      # блокировки файлов
```
Главный инструмент, когда «файл занят» или «не сохраняется».

**10. Монтирование Windows-шары на Linux**

```bash
sudo apt install -y cifs-utils
sudo kinit ivanov@CORP.VEKTOR.LAB        # от того пользователя, от кого монтируем
sudo mkdir -p /mnt/buh
sudo mount -t cifs //fs01.corp.vektor.lab/Buh /mnt/buh \
  -o sec=krb5,vers=3.1.1,iocharset=utf8
```
`sec=krb5` — аутентификация по Kerberos; `vers=3.1.1` — современный диалект SMB3. Есть ещё `multiuser` (каждый пользователь ходит под своим билетом) и `credentials=/etc/smb-cred` (файл с паролем, `chmod 600`) — второй вариант без Kerberos, но с паролем на диске.

**11. Логи Samba**

```bash
sudo tail -n 30 /var/log/samba/log.smbd
journalctl -u smbd -u nmbd -n 50 --no-pager
```
Повысить детализацию: в `[global]` добавить `log level = 1 auth:5` и перезапустить `smbd`.

**12. Время**

```bash
timedatectl
chronyc tracking      # смотри Last offset и System time
```
`System time` должен идти в ногу с DC01. Это проверяем **до** любых разборок с Kerberos.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **DC01** (10.10.10.10) — AD DS, DNS, домен `corp.vektor.lab` работает; в OU `Серверы` есть место для компьютерной учётки; `ivanov` состоит в `GG-Buh`.
- **FS01** (10.10.10.20) — шары `Obshchee`, `Buh`, `HR` доступны.
- **UBU01** (10.10.10.30, Ubuntu Server 24.04 LTS) — статический IP, SSH работает, DNS-сервер = 10.10.10.10 (это настраивалось в уроке 53; если не сделано — вернись к нему).
- **WIN11-CL01** — рабочая станция `ivanov`, в домене.
- Пароль всего стенда: `Lab#2026-vektor`.

### Снапшот

В VMware Workstation на **DC01** и **UBU01** (состояние «выключена» или «пауза» — так надёжнее): Snapshot → Take Snapshot, имя `before-l2-058`. На FS01 и WIN11-CL01 снапшот не обязателен, но если диск позволяет — сделай и там.

### Шаги

1. **UBU01.** Проверь DNS и домен:
   ```bash
   cat /etc/resolv.conf
   dig -t SRV _ldap._tcp.corp.vektor.lab +short
   ```
   Ожидаемо: `nameserver 10.10.10.10` и строка вида `0 100 389 dc01.corp.vektor.lab.`. Если пусто — правь netplan, `sudo netplan apply`, `sudo resolvectl flush-caches`.

2. **UBU01.** Настрой синхронизацию времени с DC01:
   ```bash
   sudo apt install -y chrony
   echo 'server 10.10.10.10 iburst' | sudo tee -a /etc/chrony/chrony.conf
   sudo systemctl restart chrony
   chronyc tracking
   ```
   Ожидаемо: `Leap status : Normal`, offset — единицы миллисекунд. На DC01 служба времени (`W32Time`) должна работать в режиме сервера `[ПРОВЕРИТЬ: w32tm /query /configuration]`.

3. **UBU01.** Установи пакеты:
   ```bash
   sudo apt update
   sudo apt install -y samba smbclient cifs-utils krb5-user realmd sssd sssd-tools adcli acl attr
   ```
   При установке `krb5-user` спросит realm — вводи `CORP.VEKTOR.LAB` в верхнем регистре.

4. **UBU01.** Найди домен и введи машину:
   ```bash
   sudo realm discover corp.vektor.lab
   sudo realm join --user=adm.student \
     --computer-ou='OU=Серверы,DC=corp,DC=vektor,DC=lab' -v corp.vektor.lab
   realm list
   ```
   Ожидаемо: `realm list` показывает `corp.vektor.lab / kerberos member / configured: kerberos-member`. В ADUC появляется компьютер `UBU01` в OU `Серверы`.

5. **UBU01.** Проверь Kerberos и пользователей:
   ```bash
   sudo kinit adm.student@CORP.VEKTOR.LAB
   klist
   sudo adcli testjoin -D corp.vektor.lab
   getent passwd ivanov
   id ivanov
   ```
   Ожидаемо: билет `krbtgt/CORP.VEKTOR.LAB@CORP.VEKTOR.LAB`, «Successfully validated join», строка пользователя с UID в районе 200000+.

6. **UBU01.** Подправь SSSD под короткие имена и домашние каталоги:
   ```bash
   sudo cp /etc/sssd/sssd.conf /root/sssd.conf.bak
   sudo sed -i 's/^use_fully_qualified_names.*/use_fully_qualified_names = False/' /etc/sssd/sssd.conf
   grep -q fallback_homedir /etc/sssd/sssd.conf || \
     sudo sed -i '/^id_provider/i fallback_homedir = /home/%u' /etc/sssd/sssd.conf
   sudo systemctl restart sssd
   sudo pam-auth-update --enable mkhomedir
   sudo sss_cache -E
   getent passwd ivanov
   getent group gg-buh
   ```
   Ожидаемо: `getent group gg-buh` возвращает GID (если пусто, попробуй `getent group | grep -i gg-buh` — регистр может отличаться).

7. **UBU01.** Создай каталог шары и права:
   ```bash
   sudo mkdir -p /srv/samba/buh
   sudo chgrp gg-buh /srv/samba/buh
   sudo chmod 2770 /srv/samba/buh
   sudo setfacl -m g:gg-buh:rwx /srv/samba/buh
   ls -ldn /srv/samba/buh
   ```
   Ожидаемо: владелец root, группа — GID группы `gg-buh`, права `2770` (`drwxrws---`).

8. **UBU01.** Настрой Samba (`sudo nano /etc/samba/smb.conf`), оставь `[global]` такого вида:
   ```ini
   [global]
      workgroup = VEKTOR
      realm = CORP.VEKTOR.LAB
      security = ads
      kerberos method = secrets and keytab
      server role = member server
      template shell = /bin/bash
      template homedir = /home/%U
      log level = 1 auth:3
      log file = /var/log/samba/log.%m

      idmap config * : backend = tdb
      idmap config * : range = 3000-7999
      idmap config VEKTOR : backend = sss
      idmap config VEKTOR : range = 200000-2000200000

   [Buh-Linux]
      path = /srv/samba/buh
      comment = Buh na Linux
      read only = no
      valid users = @gg-buh
      force group = gg-buh
      create mask = 0660
      directory mask = 2770
   ```
   Затем:
   ```bash
   sudo testparm -s
   sudo systemctl restart smbd nmbd
   sudo systemctl enable smbd nmbd
   sudo smbstatus -b
   ```
   Ожидаемо: `testparm` без ошибок, `smbd` активен, `smbstatus` открывается.

   > Примечание: backend `sss` требует, чтобы `smbd` умел общаться с SSSD через библиотеку `libwbclient-sssd` `[ПРОВЕРИТЬ имя пакета в Ubuntu 24.04]`. Диапазон `200000-2000200000` — это дефолтный диапазон idmap в SSSD; если ты его менял в `sssd.conf` (`ldap_idmap_range_min/max`), поменяй и здесь, иначе UID разъедутся.

9. **UBU01.** Тест снаружи Kerberos-билетом:
   ```bash
   kinit ivanov@CORP.VEKTOR.LAB
   smbclient -k //ubu01.corp.vektor.lab/Buh-Linux -c 'ls'
   ```
   Ожидаемо: листинг (пустой) без запроса пароля.

10. **WIN11-CL01.** Под `ivanov` открой Проводник → `\\UBU01\Buh-Linux`, создай файл `test-ivanov.txt`.

11. **UBU01.** Проверь, что файл принадлежит правильному UID:
    ```bash
    ls -ln /srv/samba/buh
    id -u ivanov
    ```
    Ожидаемо: числовой владелец файла совпадает с `id -u ivanov`. Если нет — проблема в idmap, см. раздел 6.

12. **UBU01.** Смонтируй шару FS01 через Kerberos:
    ```bash
    sudo mkdir -p /mnt/buh
    sudo kinit adm.student@CORP.VEKTOR.LAB
    sudo mount -t cifs //fs01.corp.vektor.lab/Buh /mnt/buh -o sec=krb5,vers=3.1.1,iocharset=utf8
    ls -l /mnt/buh
    df -h /mnt/buh
    ```
    Ожидаемо: содержимое папки `Buh` с FS01 видно. Размонтировать: `sudo umount /mnt/buh`.

13. **Хост.** Допиши в `stand-log.md` изменения (см. следующий подраздел).

### Проверка

- [ ] `realm list` на UBU01 показывает `configured: kerberos-member`.
- [ ] Компьютер `UBU01` виден в ADUC в OU `Серверы`.
- [ ] `klist` после `kinit` показывает действительный `krbtgt`-билет.
- [ ] `sudo adcli testjoin -D corp.vektor.lab` — успех.
- [ ] `getent passwd ivanov` и `id ivanov` возвращают данные.
- [ ] `testparm -s` не выдаёт ошибок.
- [ ] `smbclient -k //ubu01.corp.vektor.lab/Buh-Linux -c 'ls'` работает без пароля.
- [ ] С WIN11-CL01 `ivanov` открывает `\\UBU01\Buh-Linux` и создаёт файл.
- [ ] `ls -ln` показывает числового владельца, совпадающего с `id -u ivanov`.
- [ ] `petrova` (не в `GG-Buh`) получает отказ на `\\UBU01\Buh-Linux`.
- [ ] `/mnt/buh` смонтирован и отдаёт содержимое `\\FS01\Buh`.

### Сломай и почини

> **Пользователь:** ivanov
> **ВМ:** WIN11-CL01
> **Что написал:** «Открываю `\\UBU01\Buh-Linux`, пишет «Отказано в доступе». Вчера всё работало, ничего не трогал. Отчёт в пятницу!»
> **Срочность:** высокая

**Как воспроизвести поломку** (на UBU01):

```bash
sudo timedatectl set-ntp false
sudo date -s "+12 minutes"
sudo systemctl restart smbd
```

**Диагностика по порядку:**

1. `systemctl status smbd nmbd` — службы живы? Если `smbd` в `failed`, смотрим `journalctl -u smbd -n 50`.
2. `sudo smbstatus -b` — есть ли вообще сессии. Пусто → клиент не доходит до аутентификации.
3. `sudo tail -n 40 /var/log/samba/log.smbd` — ищем `Clock skew too great`, `KRB5KRB_AP_ERR_SKEW`, `NT_STATUS_LOGON_FAILURE`, `NT_STATUS_ACCESS_DENIED`. Это разные болезни: первая — время, вторая — пароль/учётка, третья — права.
4. `timedatectl` и `date` на UBU01 против `w32tm /stripchart /computer:10.10.10.10 /samples:3` на DC01 или просто `net time \\dc01` на WIN11-CL01. Расхождение больше пары минут — вот и причина.
5. `chronyc tracking` — синхронизируется ли UBU01 вообще. Если `System time` уплыло, а `set-ntp false` — всё сходится.
6. Дополнительно: `getent passwd ivanov` и `id ivanov` — если тут пусто, дело не в Samba, а в SSSD (тогда `sssctl domain-status corp.vektor.lab`).

**Решение:**

```bash
sudo timedatectl set-ntp true
sudo systemctl restart chrony
sudo chronyc makestep
timedatectl
sudo systemctl restart smbd
```

Проверить: `date` совпадает с DC01, затем с WIN11-CL01 снова открыть шару.

**Ответ пользователю:** «Иван, добрый день. Причина — рассинхронизация часов на файловом сервере: протокол аутентификации Kerberos не пропускает расхождение больше 5 минут, поэтому доменный пароль «не подходил». Время синхронизировал, доступ восстановлен, проверил под вашей учёткой — файл создаётся. Если повторится, сразу пишите, посмотрим на сам сервер синхронизации».

**Закрытие заявки:** категория «Файловые ресурсы → Доступ к шаре», решение «Восстановлена синхронизация времени (chrony) на UBU01, Kerberos-аутентификация работает», привязка к CMDB-объекту UBU01, отметка «корневая причина устранена».

### Что изменилось на стенде

- **UBU01** введён в домен `corp.vektor.lab`; в AD появилась компьютерная учётка `UBU01` в OU `Серверы`.
- На UBU01 создан `/etc/krb5.keytab` (ключи машины) и `/etc/sssd/sssd.conf` (сгенерирован `realmd`, правился вручную; бэкап — `/root/sssd.conf.bak`).
- Установлены: `samba`, `smbclient`, `cifs-utils`, `krb5-user`, `realmd`, `sssd`, `sssd-tools`, `adcli`, `chrony`, `acl`, `attr`.
- В `/etc/chrony/chrony.conf` добавлен сервер `10.10.10.10`.
- В SSSD: `use_fully_qualified_names = False`, `fallback_homedir = /home/%u`; включён `mkhomedir`.
- Создан каталог `/srv/samba/buh` (группа `gg-buh`, права `2770`).
- В `/etc/samba/smb.conf` — `security = ads`, `idmap … backend = sss`, шара `[Buh-Linux]`.
- Точка монтирования `/mnt/buh` (монтируется вручную, в `/etc/fstab` не прописана).
- Доменный вход на UBU01 работает для пользователей AD; `ivanov` имеет доступ к `\\UBU01\Buh-Linux`, `petrova` — нет.

### Откат

1. На UBU01: `sudo umount /mnt/buh` (если смонтировано), затем выключить ВМ.
2. В VMware Workstation: Snapshot Manager → выбрать `before-l2-058` → Revert (сначала на UBU01, потом на DC01).
3. На DC01 после отката компьютерная учётка `UBU01` исчезнет из AD — это нормально, при повторном прохождении лабораторной она создастся заново. Если откатываешь только UBU01, а DC01 оставляешь как есть, удали «осиротевшую» учётку в ADUC вручную и перед повторным `realm join` выполни `sudo realm leave corp.vektor.lab` `[ПРОВЕРИТЬ: команда корректно чистит sssd.conf на вашей версии realmd]`.

## 6. Ошибки новичков и безопасность

1. **DNS указывает на 8.8.8.8.** → `realm discover` не находит домен, `realm join` падает с «Cannot find KDC». → На доменном члене в resolv.conf только DC01 (10.10.10.10), остальные форвардит сам DC.
2. **Не проверили часы.** → `KRB5KRB_AP_ERR_SKEW`, внезапные «отказано в доступе» после ночи. → `chrony` на DC, проверка `chronyc tracking` в чек-листе ввода в домен.
3. **Не указали `kerberos method = secrets and keytab`.** → Через ~30 дней (срок смены пароля компьютерной учётки по умолчанию) шары перестают пускать, лечится перезагрузкой/перегенерацией keytab. → Прописать сразу, до продакшена.
4. **Разные диапазоны idmap у SSSD и Samba.** → Файлы, созданные по SSH, «принадлежат другому» для `smbd`, доступ пропадает. → Диапазон `idmap config VEKTOR : range` обязан совпадать с диапазоном SSSD; проверка — `ls -ln` против `id -u`.
5. **`security = user` вместо `ads`.** → Samba ищет локальных пользователей, доменные учётки «не существуют». → `security = ads` + `realm` в верхнем регистре.
6. **`valid users = @GG-Buh` при том, что SSSD отдаёт имя в нижнем регистре.** → «Группа не найдена», доступ закрыт для всех. → Проверять `getent group | grep -i gg-buh` и писать имя ровно так, как его отдаёт NSS.
7. **`chmod 777` на шару «чтобы точно работало».** → Любой, включая посторонних в домене, пишет и удаляет. → Группа-владелец, `2770`, `valid users = @группа`, при необходимости ACL.
8. **Пароль в `fstab` открытым текстом.** → Утечка при первом же бэкапе или `cat`. → `credentials=/etc/smb-cred` с `chmod 600 root:root` либо `sec=krb5`.

### Безопасность

- **Не включайте `guest ok = yes` на доменном файловом сервере.** Анонимный доступ к шаре в корпоративной сети — это инцидент ИБ, а не «удобно для сканера».
- **Не отключайте подпись и шифрование SMB** (`server signing`, `smb encrypt`) ради совместимости со старым оборудованием. Правильный путь — отдельный сервисный аккаунт и обновление прошивки устройства.
- **`/etc/krb5.keytab` — это пароль машины в файле.** Права `600 root:root`, никаких копий в git и на общих папках.
- **Не логиньтесь доменной админской учёткой (`adm.student`) на файловом сервере для повседневной работы** — только для ввода в домен. Дальше — отдельные сервисные учётки с минимальными правами.
- **Не держите SMB1.** Если древний сканер требует SMB1 — выносите его в отдельный изолированный сегмент, а не открывайте на всём сервере.
- Учебный пароль `Lab#2026-vektor` — только для стенда. В реальной среде такая практика недопустима.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем SMB отличается от CIFS, какие порты используются?**
Ответ: CIFS — это историческое название диалекта SMB1, сегодня официально не используется и считается небезопасным. Современный SMB (2.x/3.x) работает напрямую через TCP 445; старый стек NetBIOS — UDP 137 (служба имён, `nmbd`), UDP 138 (дейтаграммы), TCP 139 (сессии). SMB3 умеет шифрование (`smb encrypt`) и подпись.
*Что хочет услышать интервьюер:* что кандидат не включает SMB1 «по привычке» и знает про 445 и про `nmbd`.
*Красный флаг:* «SMB и CIFS — одно и то же, порт 139» без уточнений.

**Вопрос 2. Как Linux-сервер проверяет доменную учётку при доступе к SMB-шаре? Что такое keytab?**
Ответ: два пути. Kerberos — клиент получает у KDC (контроллера домена) service ticket на `cifs/ubu01.corp.vektor.lab`, `smbd` расшифровывает его ключом машины из `/etc/krb5.keytab` и получает имя пользователя с SID. Если Kerberos невозможен, используется NTLMv2 — тогда `smbd` сам проверяет хэш через LDAP/Netlogon DC. Keytab — файл с ключами service principal'ов машины, по сути «пароль компьютера в домене», поэтому `chmod 600`.
*Что хочет услышать интервьюер:* понимание, что сервер не отправляет пароль в DC на каждый запрос, и что keytab — секрет.
*Красный флаг:* «Samba спрашивает пароль у AD при каждом открытии файла».

**Вопрос 3. Что такое idmap и что будет, если диапазоны разъедутся?**
Ответ: idmap — правила отображения AD-SID в Linux-UID/GID, потому что AD не хранит POSIX-идентификаторы. Backend'ы: `rid` (UID = низ диапазона + RID), `autorid`, `sss` (берёт то, что посчитал SSSD), `ad` (читает атрибуты `uidNumber`). Если Samba и SSSD считают по-разному, один и тот же человек получает разные UID в разных контекстах: файл, созданный по SSH, для `smbd` принадлежит другому владельцу — доступ ломается «на ровном месте». Проверка: `ls -ln` и `id -u`.
*Что хочет услышать интервьюер:* что кандидат знает про SSSD-стек и умеет проверять совпадение UID.
*Красный флаг:* «Samba сама берёт UID из AD».

**Вопрос 4. Пользователь не может открыть шару на Linux-сервере. Ваши действия по шагам?**
Ответ: 1) DNS — резолвится ли `ubu01.corp.vektor.lab` с клиента и KDC с сервера; 2) время — `timedatectl`/`chronyc tracking`, расхождение с DC; 3) `systemctl status smbd nmbd` и `testparm -s`; 4) `sudo smbstatus -b` — доходит ли клиент; 5) `/var/log/samba/log.smbd` — конкретный NTSTATUS; 6) `getent passwd` / `id` для пользователя — жив ли SSSD, тот ли UID; 7) `valid users` и права на каталог (`ls -ldn`, `getfacl`); 8) членство в группе в AD и сброс кэша `sss_cache -E`. Идти от клиента к серверу, а не «перезапустить smbd и посмотреть».
*Что хочет услышать интервьюер:* системный порядок диагностики и понимание, что 80% проблем — DNS, время и маппинг идентификаторов.
*Красный флаг:* «сразу смотрю логи Samba» без проверки DNS и времени.

**Вопрос 5. SSSD или winbind для ввода Linux в AD?**
Ответ: SSSD — современный выбор для member-сервера: кэширование, офлайн-аутентификация, единая точка для NSS/PAM и SSH, проще в отладке (`sssctl`), не требует запускать `winbindd`. Winbind нужен, если Samba должна сама разрешать пользователей через собственный стек (`idmap backend = rid/ad`), при работе со старыми сценариями или когда SSSD недоступен. В связке «SSSD для входа + Samba для шар» используют `idmap config DOMAIN : backend = sss`, чтобы UID совпадали.
*Что хочет услышать интервьюер:* что выбор осознанный и что кандидат знает про согласование idmap между двумя стеками.
*Красный флаг:* «winbind устарел, всегда SSSD» без понимания, где winbind всё ещё нужен.

## 8. Источники (официальная документация)

- **samba.org** — раздел Wiki: «Setting up Samba as a Domain Member», «Samba & Active Directory», страница про `idmap config` и backend'ы. Поисковая фраза: *samba wiki setting up samba as a domain member idmap*.
- **learn.microsoft.com** — «Kerberos authentication overview», «Server Message Block (SMB) overview», «How to configure authoritative time». Поиск: *kerberos clock skew 5 minutes microsoft*, *smb ports 445 overview*.
- **ubuntu.com** — Server documentation, разделы про Samba и SSSD/realmd. Поиск: *ubuntu server docs samba active directory*, *ubuntu sssd realmd join*.
- **freedesktop.org** — документация SSSD (`sssd-ad`, `sssd.conf(5)`). Поиск: *sssd ad provider ldap_id_mapping documentation*.
- **wiki.astralinux.ru** — раздел про Astra Linux Directory и ввод в домен AD `[ПРОВЕРИТЬ актуальность раздела и версии ALD]`. Поиск: *astra linux directory ald ввод в домен*.
- **redhat.com / access.redhat.com** — «RHEL: Connecting to an AD domain with realmd/SSSD» `[ПРОВЕРИТЬ доступность из РФ — может требовать авторизации; альтернатива: man-страницы `realm(8)`, `sssd-ad(5)`, `smb.conf(5)` на самом стенде]`.
