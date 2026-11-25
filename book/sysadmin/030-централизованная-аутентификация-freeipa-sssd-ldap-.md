# Централизованная аутентификация: FreeIPA, SSSD, LDAP, Kerberos, интеграция с AD

## 1. Введение (зачем это нужно в реальной работе)

Тикет #4127, автор — Сидоров (склад): «Алексей, у нас на складе новый кладовщик, и он же будет ходить на UBU01 смотреть выгрузки. Сейчас вы заводите отдельные учётки на каждом сервере Linux, у него уже три пароля, и он их путает. В Windows у него одна учётка. Сделайте так же, пожалуйста. Срочность: средняя, до конца недели».

Это самая частая причина, по которой на предприятии вообще появляется централизованная аутентификация. Пока сервер один, локальные пользователи в `/etc/passwd` работают. Как только серверов пять, а людей пятьдесят, начинается ад: уволенный сотрудник остаётся в трёх системах, пароли меняются вручную, никто не помнит, кто где заведён, а аудит требует показать список тех, у кого есть доступ к конкретной машине. Централизация решает это одним движением: учётка одна, живёт в каталоге, доступ выдаётся группой, пароль меняется в одном месте, а увольнение отключает человека везде сразу.

В этой главе ты разберёшься, как устроены три кита корпоративной аутентификации в Linux — LDAP, Kerberos и SSSD, — и как из них собирается FreeIPA. Ты научишься поднимать собственный домен FreeIPA, включать Linux-хост в домен и, главное, делать кросс-доменное доверие с Active Directory, чтобы `ivanov` из `corp.vektor.lab` заходил на UBU01 тем же паролем, что и на WIN11-CL01. Это ровно та задача, которую в РФ просят на собеседовании на позицию сисадмина в компании с Windows-инфраструктурой и растущим парком Linux.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**LDAP** (Lightweight Directory Access Protocol, RFC 4511) — протокол доступа к иерархическому каталогу. Каталог — это дерево объектов, у каждого есть уникальное имя DN (Distinguished Name), например `uid=ivanov,cn=users,cn=accounts,dc=ipa,dc=vektor,dc=lab`. LDAP — это **не протокол аутентификации** в строгом смысле: он умеет проверять пароль (bind), но не умеет выдавать билеты и Single Sign-On. Историческая ошибка новичка — «сделать вход по LDAP» и удивиться, что пароль летит по сети (simple bind) и что каждый сервис спрашивает пароль заново.

**Kerberos** (RFC 4120, версия 5, реализация MIT) — протокол аутентификации на билетах. Клиент один раз аутентифицируется у KDC (Key Distribution Center) и получает **TGT** (Ticket-Granting Ticket) — пропуск, который предъявляет, когда нужен доступ к конкретному сервису. KDC выдаёт **service ticket** для нужного сервиса, сервис проверяет билет своим ключом (он лежит в keytab-файле) и верит клиенту без передачи пароля. Критично: билеты содержат метки времени, поэтому расхождение часов больше ~5 минут ломает всё (это будет твоя поломка в лабе).

**SSSD** (System Security Services Daemon) — демон на клиентской машине. Он сидит между приложениями (через PAM и NSS) и внешним источником учёток (IPA, AD, plain LDAP), умеет кэшировать данные, обновлять тикеты в фоне, применять правила доступа (HBAC) и маппить идентификаторы. Без SSSD подключать LDAP к системе можно только через legacy-схему `nss-pam-ldapd`/NIS, которую в 2026 году в новой инфраструктуре не ставят.

**FreeIPA** — не отдельный протокол, а дистрибутив-сборка из готовых компонентов: 389 Directory Server (LDAP-каталог), MIT KDC, BIND (DNS), Dogtag CA (сертификаты), Apache + REST/JSON API, `ipa` CLI. Плюс своя схема, группы, HBAC-правила, sudo-правила, SSH-ключи пользователей, автоматическая выдача сертификатов. Upstream проекта — freeipa.org; коммерческая версия — Red Hat Identity Management. В РФ похожие задачи решают ALD Pro (Astra Linux Directory) и РЕД АДМ — это отдельные продукты, но идея та же.

Как выглядит путь входа по SSH паролем на UBU01 в домене IPA:

```text
SSH-сервер (PAM)
      │  pam_unix → pam_sss
      ▼
SSSD (на UBU01)  ──── LDAP 389/636 ────►  389-ds на IPA01   (данные: uid, gid, группы, HBAC)
      │  ──── Kerberos 88/464 ─────────►  MIT KDC на IPA01  (проверка пароля, билеты)
      │
      └── локальный кэш /var/lib/sss/db  (работает, если IPA01 недоступен)

IPA01 ──── cross-realm trust (Kerberos 88, SMB 445) ────► DC01 (corp.vektor.lab)
```

Порты, которые нужно открыть в сторону сервера идентификации:

| Порт | Протокол | Зачем |
|------|----------|-------|
| 53/tcp+udp | DNS | SRV-записи `_kerberos._tcp`, `_ldap._tcp` — по ним клиент находит сервер |
| 88/tcp+udp | Kerberos | выдача TGT и service ticket |
| 464/tcp+udp | kpasswd | смена пароля |
| 389/tcp | LDAP | каталог |
| 636/tcp | LDAPS | каталог по TLS (в IPA включён по умолчанию) |
| 123/udp | NTP | синхронизация времени (обязательна для Kerberos) |
| 443/tcp | HTTPS | веб-интерфейс IPA, REST API, Dogtag |
| 445/tcp | SMB | нужен только для доверия с AD (`ipa-adtrust-install`) |

Две вещи, которые чаще всего ломаются: **DNS** (клиент не находит сервер по SRV, потому что в `resolv.conf` стоит публичный резолвер) и **время** (chrony разъехался). Если запомнишь только это — уже сэкономишь себе день.

## 3. Аналогия из жизни

Представь бизнес-центр с бюро пропусков. **LDAP** — это картотека бюро: там записано, что Иванов работает в «Векторе», в каком отделе, какой у него пропуск и в какие кабинеты ему можно. **Kerberos** — сама система пропусков: ты один раз показываешь паспорт на входе, получаешь бумажный пропуск в здание на весь день (это TGT), и дальше по нему тебе выдают разовые талоны в конкретные кабинеты (service ticket), причём охрана кабинета не звонит в бюро — она смотрит на печать и верит талону. **SSSD** — вахтёр на входе в твой офис: он проверяет талон, сверяет с картотекой и, чтобы не звонить в бюро каждый раз, держит у себя список сотрудников и их пропусков — поэтому, если бюро закрылось на обед (сервер упал), люди уже вошедшие продолжают работать. **FreeIPA** — само здание бюро пропусков со всеми службами; **доверие с AD** — межведомственное соглашение, по которому пропуск, выданный в соседнем бизнес-центре, признают и здесь.

Аналогия перестаёт работать в одном месте: настоящий пропуск нельзя подделать, а Kerberos-билет технически можно — если украсть keytab сервиса или хост-машины, поэтому keytab защищают как пароль root и не хранят в git.

## 4. Практика и команды

**1) Билеты Kerberos: `kinit`, `klist`, `kdestroy`.**
```bash
kinit admin@IPA.VEKTOR.LAB        # получить TGT (спросит пароль)
klist                             # показать билеты
kdestroy -A                       # снести все билеты разом
```
Вывод `klist` (сокращённо):
```text
Default principal: admin@IPA.VEKTOR.LAB
Valid starting       Expires              Service principal
01.03.2026 10:12:44  02.03.2026 10:12:40  krbtgt/IPA.VEKTOR.LAB@IPA.VEKTOR.LAB
```
Смотрим на `Default principal` и срок жизни. Если `kinit` отвечает `Clock skew too great` — время, если `Cannot find KDC for realm` — DNS.

**2) Машинная учётка через keytab.**
```bash
sudo klist -k /etc/krb5.keytab
sudo kinit -kt /etc/krb5.keytab host/ubu01.ipa.vektor.lab@IPA.VEKTOR.LAB
sudo kvno host/ubu01.ipa.vektor.lab@IPA.VEKTOR.LAB
```
Так проверяют, что у машины есть рабочая идентичность, — это нужно для NFS, автоматического enroll и диагностики, когда «человек входит, а сервис нет».

**3) Поиск в каталоге: `ldapsearch`.**
```bash
# по билету (SASL GSSAPI), без пароля в командной строке
ldapsearch -H ldaps://ipa01.ipa.vektor.lab -Y GSSAPI \
  -b "cn=users,cn=accounts,dc=ipa,dc=vektor,dc=lab" "(uid=admin)" uid cn mail

# простой bind под сервисной учёткой (пароль запросит интерактивно)
ldapsearch -H ldaps://ipa01.ipa.vektor.lab -x -D "uid=admin,cn=users,cn=accounts,dc=ipa,dc=vektor,dc=lab" -W \
  -b "cn=groups,cn=accounts,dc=ipa,dc=vektor,dc=lab" "(cn=ipausers)"
```
`-H` — URI сервера, `-Y GSSAPI` — аутентификация билетом, `-x` — простой bind, `-D` — DN пользователя, `-W` — запросить пароль, `-b` — база поиска. Анонимный поиск в 389-ds по умолчанию закрыт — это правильно, не открывай.

**4) Найти сервер по DNS: `realm discover`.**
```bash
realm discover ipa.vektor.lab
```
```text
ipa.vektor.lab
  type: kerberos
  realm-name: IPA.VEKTOR.LAB
  domain-name: ipa.vektor.lab
  configured: no
  server-software: ipa
  client-software: sssd
```
Если «ничего не найдено» — SRV-записи не резолвятся, смотри `dig`.

**5) Управление IPA через CLI.**
```bash
ipactl status                  # состояние всех служб IPA (KDC, LDAP, DNS, CA)
ipa user-find --all            # пользователи
ipa trust-find                 # настроенные доверия
ipa hbacrule-find              # правила доступа
```
`ipactl restart` перезапускает всё разом — полезно, но на боевом сервере это простой на десятки секунд, предупреждай.

**6) Диагностика SSSD: `sssctl`.**
```bash
sudo sssctl domain-status ipa.vektor.lab
sudo sssctl domain-status corp.vektor.lab
sudo sssctl user-checks 'ivanov@corp.vektor.lab'
```
```text
Online status: Online
Active servers:
IPA: ipa01.ipa.vektor.lab
```
Для каждого домена — свой статус. `user-checks` показывает, что именно ответила PAM (acct, auth, password) — самый быстрый способ понять, «человека не пускают» или «пароль не тот».

**7) Проверка глазами системы.**
```bash
getent passwd 'ivanov@corp.vektor.lab'
id 'ivanov@corp.vektor.lab'
grep -E '^(passwd|group|shadow)' /etc/nsswitch.conf
```
В `nsswitch.conf` должно быть `passwd: compat sss` и `group: compat sss`. Если SSSD не прописан — система о домене вообще не знает.

**8) Сброс кэша.**
```bash
sudo sssctl cache-expire -E     # очистить всё
sudo sssctl cache-expire -u 'ivanov@corp.vektor.lab'
```
Кэш живёт в `/var/lib/sss/db`. Классика: «я изменил группу, а на клиенте не видно» — потому что кэш, а не потому что IPA сломался.

**9) Логи SSSD и их детализация.**
```bash
sudo journalctl -u sssd -n 100 --no-pager
sudo sssctl debug-level 6        # временно, не оставляй на бою
sudo sssctl debug-level 0
```
Уровень 6 — это очень много текста и заметная нагрузка; включай на 5 минут и выключай.

**10) Проверка TLS/LDAPS.**
```bash
openssl s_client -connect ipa01.ipa.vektor.lab:636 -servername ipa01.ipa.vektor.lab </dev/null
```
Смотрим `Verify return code: 0 (ok)` и цепочку. Сертификат IPA выдан собственным Dogtag CA, поэтому на клиентах он должен быть в доверенных (`/etc/ipa/ca.crt`, ставится `ipa-client-install`).

**11) Время.**
```bash
timedatectl
chronyc tracking
chronyc sources -v
```
`Leap status: Normal`, `System time: 0.0000xx seconds fast` — хорошо. Уход в секунды и больше — плохо.

**12) Подключение к AD напрямую, без FreeIPA (частый боевой вариант).**
```bash
sudo apt install -y sssd-ad sssd-tools realmd adcli
sudo realm join --user=adm.student corp.vektor.lab
```
Ставит `id_provider = ad` в `/etc/sssd/sssd.conf`. Ручной конфиг выглядит так:
```ini
[domain/corp.vektor.lab]
id_provider = ad
auth_provider = ad
access_provider = ad
ad_domain = corp.vektor.lab
krb5_realm = CORP.VEKTOR.LAB
cache_credentials = true
use_fully_qualified_names = false
fallback_homedir = /home/%u
```
Это дешевле, чем FreeIPA, но не даёт HBAC, sudo-правил и единого управления Linux-парком.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Стенд L2 готов: DC01 (10.10.10.10, AD DS + DNS + DHCP, домен `corp.vektor.lab`), UBU01 (10.10.10.30, Ubuntu Server 24.04 LTS, SSH), учётки `adm.student`, `ivanov`, `petrova`, `sidorov`, группы `GG-Buh/HR/Sklad/IT`.

Тема требует сервера, которого в таблице стенда нет, — добавляем его явно: **IPA01**, Ubuntu Server 24.04 LTS, `10.10.10.35`, FQDN `ipa01.ipa.vektor.lab`, домен IPA `ipa.vektor.lab`, realm `IPA.VEKTOR.LAB`, 2 vCPU / 2 ГБ RAM / 20 ГБ диск. Адрес .35 в стенде свободен (заняты .3, .10, .11, .20, .25, .30, .31, .40, .50, .55, .61–.63, .70).

Важно: домен IPA **отдельный** от домена AD. Так делают в жизни: два независимых каталога, связанные доверием, а не один «допиленный» домен. Обратную зону `10.10.10.0/24` оставляем за DC01 — поэтому при установке IPA используем `--no-reverse`.

Если пакеты FreeIPA в Ubuntu 24.04 вызовут проблемы (на Debian-семействе это community-поддержка, а не родная платформа) [ПРОВЕРИТЬ] — легальная альтернатива: развернуть IPA01 на AlmaLinux или Rocky Linux 9, там FreeIPA — родная связка [ПРОВЕРИТЬ актуальную версию]. Дистрибутивы свободные, проблем с доступностью из РФ нет.

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-sysadmin-030`. Или из консоли хоста:

```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "C:\VMs\UBU01\UBU01.vmx" before-sysadmin-030
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "C:\VMs\DC01\DC01.vmx" before-sysadmin-030
```

IPA01 создаётся уже **после** снапшота и снапшотом не покрывается — её состояние откатывается удалением ВМ.

### Шаги

1. **Создать ВМ IPA01.** VMware: Ubuntu Server 24.04 LTS, 2 vCPU, 2 ГБ RAM, 20 ГБ, сеть VMnet8. Установить систему, задать имя `ipa01`. Ожидаемо: система загрузилась, есть консоль и SSH.

2. **Сеть и имя на IPA01.** Проверить интерфейс (`ip -br a`, обычно `ens33`) и настроить netplan `/etc/netplan/50-cloud-init.yaml` (или свой файл):
```yaml
network:
  version: 2
  ethernets:
    ens33:
      dhcp4: false
      addresses: [10.10.10.35/24]
      routes:
        - to: default
          via: 10.10.10.2
      nameservers:
        addresses: [10.10.10.10]
```
```bash
sudo netplan apply
sudo hostnamectl set-hostname ipa01.ipa.vektor.lab
echo "10.10.10.35 ipa01.ipa.vektor.lab ipa01" | sudo tee -a /etc/hosts
ping -c2 dc01.corp.vektor.lab
```
Ожидаемо: `ping` до DC01 идёт, `hostname -f` выдаёт `ipa01.ipa.vektor.lab`.

3. **Время на IPA01.** В `/etc/chrony/chrony.conf` закомментировать `pool ...` и добавить `server 10.10.10.10 iburst`, затем:
```bash
sudo systemctl restart chrony
chronyc tracking
```
Ожидаемо: `Leap status: Normal`, источник — 10.10.10.10. Без этого Kerberos работать не будет.

4. **Установка FreeIPA.**
```bash
sudo apt update && sudo apt install -y freeipa-server freeipa-server-dns
sudo ipa-server-install \
  --domain=ipa.vektor.lab \
  --realm=IPA.VEKTOR.LAB \
  --hostname=ipa01.ipa.vektor.lab \
  --ip-address=10.10.10.35 \
  --setup-dns --forwarder=10.10.10.10 --no-reverse \
  --no-ntp \
  --ds-password='Lab#2026-vektor' \
  --admin-password='Lab#2026-vektor' \
  --unattended
```
Ожидаемо: длинная установка (10–20 минут), в конце `The IPA Server is configured and the ipa service is running`. Если установщик ругается на прямое/обратное разрешение имени — добавь `--no-host-dns` [ПРОВЕРИТЬ наличие ключа].

5. **Проверить сервер.**
```bash
sudo ipactl status
kinit admin
klist
```
Ожидаемо: все службы `RUNNING`, `klist` показывает билет `krbtgt/IPA.VEKTOR.LAB@IPA.VEKTOR.LAB`. Веб-интерфейс: `https://ipa01.ipa.vektor.lab/ipa/ui` (сертификат самоподписанный — предупреждение браузера нормально).

6. **DNS между доменами.** На DC01 добавить условный форвардер для зоны IPA:
```powershell
Add-DnsServerConditionalForwarderZone -Name "ipa.vektor.lab" -MasterServers 10.10.10.35
Resolve-DnsName ipa01.ipa.vektor.lab
```
На IPA01 проверить обратное направление:
```bash
dig +short _kerberos._tcp.corp.vektor.lab SRV
dig +short dc01.corp.vektor.lab
```
Ожидаемо: DC01 резолвит IPA01, IPA01 видит SRV-записи Kerberos AD. Если нет — доверие не поднимется вообще.

7. **Подготовить IPA к доверию.**
```bash
sudo ipa-adtrust-install --unattended
```
Ожидаемо: устанавливается Samba-часть, создаётся `cn=ad,cn=trusts,...`, перезапускаются службы. Это обязательный шаг: без него `ipa trust-add` скажет, что доверие не поддерживается.

8. **Установить доверие с AD.**
```bash
kinit admin
ipa trust-add corp.vektor.lab --admin=adm.student --password
```
Пароль вводится интерактивно — **не** пиши его в командной строке, он попадёт в `history` и в список процессов. Ожидаемо: `Added Active Directory trust for realm "corp.vektor.lab"`, затем `ipa trust-find` показывает `corp.vektor.lab` в состоянии `Enabled`.

9. **Ввести UBU01 в домен IPA.**
```bash
sudo apt install -y freeipa-client
sudo ipa-client-install \
  --domain=ipa.vektor.lab \
  --server=ipa01.ipa.vektor.lab \
  --realm=IPA.VEKTOR.LAB \
  --principal=admin --password='Lab#2026-vektor' \
  --mkhomedir --no-ntp --unattended
```
Ожидаемо: `Client configuration complete`, в `/etc/sssd/sssd.conf` появился домен `ipa.vektor.lab`, в `/etc/krb5.keytab` — ключи хоста.

10. **Проверить домены на клиенте.**
```bash
sudo systemctl restart sssd
sudo sssctl domain-status ipa.vektor.lab
sudo sssctl domain-status corp.vektor.lab
getent passwd 'ivanov@corp.vektor.lab'
id 'ivanov@corp.vektor.lab'
```
Ожидаемо: оба домена `Online`, `getent` возвращает строку вида `ivanov@corp.vektor.lab:*:2000001:2000001:Иван Иванов:/home/corp.vektor.lab/ivanov:/bin/bash` [ПРОВЕРИТЬ точный диапазон ID]. Если UID в районе 2000000 — ID-маппинг из SID работает.

11. **Реальный вход по SSH.** С WIN11-CL01 (в Windows 11 есть встроенный OpenSSH-клиент):
```powershell
ssh -l 'ivanov@corp.vektor.lab' 10.10.10.30
```
Ожидаемо: запрос пароля AD, после ввода — домашний каталог создан автоматически (`--mkhomedir`), `id` показывает группу из AD.

12. **Записать в `stand-log.md`** что изменилось (см. следующий подраздел).

### Проверка

- [ ] `ipactl status` на IPA01 — все службы `RUNNING`
- [ ] `kinit admin` + `klist` — TGT сроком 24 часа (по умолчанию)
- [ ] `dig +short _kerberos._tcp.corp.vektor.lab SRV` на IPA01 — непустой ответ
- [ ] `Resolve-DnsName ipa01.ipa.vektor.lab` на DC01 — 10.10.10.35
- [ ] `ipa trust-find` — доверие `corp.vektor.lab` в состоянии Enabled
- [ ] `sssctl domain-status ipa.vektor.lab` и `... corp.vektor.lab` на UBU01 — `Online`
- [ ] `id 'ivanov@corp.vektor.lab'` на UBU01 — UID/GID и группа выданы
- [ ] Вход по SSH под `ivanov@corp.vektor.lab` работает, домашний каталог создаётся
- [ ] `getent passwd 'sidorov@corp.vektor.lab'` тоже резолвится

### Сломай и почини

**Пользователь:** ivanov / UBU01 (заходит с WIN11-CL01) / **«Не могу зайти на UBU01, вчера всё работало. Пишет „Permission denied, please try again“. Пароль точно правильный, в Windows заходит.»** / **Срочность: высокая, до конца дня.**

**Воспроизводим поломку** (на UBU01, под root):
```bash
sudo timedatectl set-ntp false
sudo date -s "$(date -d '+7 minutes' '+%Y-%m-%d %H:%M:%S')"
```

**Диагностика по порядку:**

1. Смотрим на время — это первое, что проверяют при любых проблемах Kerberos:
```bash
date; timedatectl; chronyc tracking
```
Видим, что NTP выключен, а время ушло вперёд на 7 минут.
2. Подтверждаем гипотезу ручным `kinit` под доменной учёткой:
```bash
kinit 'ivanov@CORP.VEKTOR.LAB'
```
Ожидаемый ответ: `kinit: Clock skew too great while getting initial credentials`. Диагноз закрыт.
3. Для полноты убеждаемся, что дело не в сети и не в HBAC:
```bash
sudo sssctl domain-status corp.vektor.lab     # Online — значит связь с IPA/AD есть
getent passwd 'ivanov@corp.vektor.lab'        # строка есть — значит каталог отвечает
sudo journalctl -u sssd -n 50 --no-pager      # видим те же сообщения про clock skew
```

**Решение** (на UBU01):
```bash
sudo timedatectl set-ntp true
sudo systemctl restart chrony
sudo chronyc makestep
date
kdestroy -A
sudo sssctl cache-expire -E
sudo systemctl restart sssd
```
Проверяем: `kinit 'ivanov@CORP.VEKTOR.LAB'` проходит, SSH-вход работает.

**Ответ пользователю:** «Иван, добрый день. Проблема была не в пароле: на сервере UBU01 сбились часы (разошлись с контроллером домена на 7 минут), а Kerberos не выдаёт билеты при таком расхождении — это защита от повторного использования перехваченных билетов. Синхронизацию времени восстановил, вход работает. Проверьте, пожалуйста, и напишите, если что-то ещё не так. Чтобы такое не повторялось, мы отдельно проверим службу синхронизации на этом сервере».

**Закрытие заявки:** категория «Аутентификация / Kerberos», причина «расхождение системного времени (NTP) на хосте», решение «восстановлена синхронизация chrony, сброшен кэш SSSD». В `stand-log.md` — запись о поломке и фиксе.

### Что изменилось на стенде

- Новая ВМ **IPA01**: Ubuntu Server 24.04 LTS, `10.10.10.35`, FQDN `ipa01.ipa.vektor.lab`, домен `ipa.vektor.lab`, realm `IPA.VEKTOR.LAB`.
- На IPA01 работает: 389-ds (LDAP), MIT KDC, BIND (зона `ipa.vektor.lab`, форвардер на 10.10.10.10), Dogtag CA, Apache/Web UI, Samba для доверия.
- Учётные записи IPA: `admin` (администратор IPA, пароль стенда).
- На DC01 добавлена **условная пересылка** зоны `ipa.vektor.lab` → 10.10.10.35.
- Настроено **одностороннее доверие** `ipa.vektor.lab` → `corp.vektor.lab`.
- **UBU01** введён в домен IPA (`ipa-client-install`), в `/etc/sssd/sssd.conf` два домена, включён `mkhomedir`, `use_fully_qualified_names = True`.
- На UBU01 доменные пользователи AD входят как `ivanov@corp.vektor.lab`, домашние каталоги в `/home/corp.vektor.lab/`.
- Chrony на IPA01 и UBU01 синхронизируется с DC01 (10.10.10.10).

### Откат

Если что-то пошло не так: выключить IPA01 и удалить её из инвентаря; на UBU01 — **VM → Snapshot → Revert to snapshot `before-sysadmin-030`**; на DC01 — тем же способом. Альтернатива без отката всей ВМ: на DC01 удалить зону (`Remove-DnsServerZone -Name ipa.vektor.lab -Force`), на UBU01 снять клиента (`sudo ipa-client-install --uninstall`). Помни: откат DC01 откатит и то, что делали в этой лабе, — запиши в `stand-log.md`, что именно вернул.

## 6. Ошибки новичков и безопасность

1. **Ставить IPA в тот же домен, что AD** (`ipa.vektor.lab` = `corp.vektor.lab`) → конфликт DNS-зон и Kerberos-realm, доверие не поднимается, а установка может «съесть» записи. → Всегда отдельный домен для IPA и доверие между доменами.
2. **Забыть про время** → `Clock skew too great`, случайные отказы «то работает, то нет». → chrony на всех хостах, источник — DC01 (или общий NTP), проверять `chronyc tracking` в чек-листе приёма сервера.
3. **Указать в `resolv.conf` публичный DNS (8.8.8.8)** → клиент не находит SRV-записи домена, `realm discover` пустой. → В корпоративной сети клиенты смотрят только на внутренние DNS; внешние — через форвардер.
4. **Править `sssd.conf` и ждать эффекта без перезапуска и сброса кэша** → «изменения не применились». → `systemctl restart sssd` + `sssctl cache-expire -E`, и только потом делать выводы.
5. **Оставить `use_fully_qualified_names = True` и учить пользователей входить как `ivanov`** → «неверный пароль» на ровном месте. → Либо логин `ivanov@corp.vektor.lab`, либо осознанно менять параметр (и следить за коллизиями имён между доменами).
6. **Выключить правило HBAC `allow_all`, не проверив своё** → мгновенно теряется доступ ко всем Linux-хостам домена. → Сначала создаём правило, проверяем на одном хосте, держим открытую root-сессию, только потом отключаем `allow_all`.
7. **Хранить keytab и пароли в git/на share** → компрометация машинной учётки = вход на сервер от имени сервиса. → Keytab только локально, права `0600`, бэкап — в защищённом хранилище; в скриптах — `kinit -kt`, а не пароль в аргументах.
8. **Ставить FreeIPA «на потом» без бэкапа каталога** → потеря 389-ds = потеря всех учёток и хостов. → `ipa-backup` по расписанию [ПРОВЕРИТЬ актуальность утилиты] и регулярная проверка восстановления на тестовом стенде.

**Безопасность.** В бою нельзя: отключать SELinux/AppArmor, чтобы «завёлся SSSD»; открывать 389 без TLS в общую сеть (пароли и данные каталога уходят открытым текстом); давать всем `sudo` через доменную группу «на всякий случай»; использовать `admin` для ежедневной работы и тем более для входа по SSH; разрешать `simple bind` без TLS; оставлять `debug_level = 9` навсегда (логи с PII и рост диска). Учебный пароль `Lab#2026-vektor` годится только для стенда — в реальной среде такой пароль на admin-учётке каталога это инцидент.

## 7. Вопросы на собеседовании

**1. «Как происходит вход AD-пользователя на Linux-сервер, если в инфраструктуре есть FreeIPA с доверием к AD?»**
Ответ: SSH-сервер отдаёт аутентификацию PAM, стек `pam_sss` обращается к SSSD. SSSD по домену `corp.vektor.lab` (он знает о нём через доверие и SRV-записи IPA) делает `kinit` пользователя к KDC AD через cross-realm referral, получает TGT и service ticket на хост-принципал, проверяет пароль и получает из каталога (через IPA/AD) uid, gid, группы, хоум-каталог. Дальше PAM открывает сессию, `oddjob-mkhomedir` создаёт домашний каталог, NSS отдаёт `id`. Билеты кэшируются, пароль по сети в открытом виде не идёт.
*Что хочет услышать интервьюер:* что ты понимаешь цепочку PAM → SSSD → Kerberos/LDAP и что Kerberos — не то же самое, что LDAP.
*Красный флаг:* «SSSD отправляет пароль в LDAP-каталог и сравнивает хэш» — это не так, пароль проверяется Kerberos-обменом.

**2. «Что такое TGT и service ticket, и почему время критично?»**
Ответ: TGT — билет на получение билетов, выдаётся KDC после аутентификации клиента и живёт ограниченное время (обычно 10 часов, продлевается до 7 дней). Service ticket выдаётся по TGT для конкретного сервиса и шифруется ключом сервиса из его keytab. В билетах стоят метки времени и окно допустимого расхождения (по умолчанию 5 минут); при большем расхождении KDC отвергает запрос — это защита от повторного использования перехваченных билетов. Поэтому NTP в домене обязателен.
*Что хочет услышать:* понимание, что время — часть протокола, а не «желательная настройка».
*Красный флаг:* «нужно синхронизировать время, чтобы логи совпадали» — верно, но не это главное.

**3. «Что будет, если сервер FreeIPA упадёт? Пользователи смогут работать?»**
Ответ: уже вошедшие — да, SSSD держит кэш в `/var/lib/sss/db` и работает офлайн; вход по паролю возможен, если в домене включён `cache_credentials = true` (в IPA по умолчанию включён). Что деградирует: смена пароля, новые пользователи, применение изменений групп и HBAC, выдача сертификатов. Поэтому в продакшене IPA ставят минимум двумя репликами и разводят по хостам/стойкам, а на клиентах проверяют `sssctl domain-status`.
*Что хочет услышать:* осознание, что централизация — это единая точка отказа, и что у неё есть HA-контур.
*Красный флаг:* «всё встанет полностью» без упоминания кэша.

**4. «Как настроить доверие FreeIPA с Active Directory и что обычно ломается?»**
Ответ: порядок такой — отдельные домены и realm, DNS в обе стороны (на DC01 условная пересылка зоны IPA, на IPA форвардер на DNS AD), синхронизация времени, `ipa-adtrust-install` на IPA, затем `ipa trust-add corp.vektor.lab --admin=<админ AD>` с интерактивным вводом пароля, потом перезапуск SSSD на клиентах и проверка `sssctl domain-status corp.vektor.lab` и `getent passwd user@ad.domain`. Ломается чаще всего: DNS/SRV, время, отсутствие `ipa-adtrust-install`, несовпадение криптоалгоритмов Kerberos (RC4/AES) со старым AD, конфликты ID-диапазонов и UID вне зарезервированных.
*Что хочет услышать:* чёткий порядок и «DNS + время» как первые две проверки.
*Красный флаг:* «просто делаю `realm join` к AD и всё» — это другой сценарий, без IPA.

**5. «Почему simple bind по LDAP без TLS — плохо, и что использовать вместо него?»**
Ответ: при simple bind логин и пароль идут по сети в открытом виде (base64 — не шифрование), их видно в дампе трафика; плюс сервер не может проверить, что клиент — тот, за кого себя выдаёт. Правильно: Kerberos/GSSAPI-аутентификация (`-Y GSSAPI`), LDAPS на 636 или StartTLS на 389 с проверкой сертификата, сервисные учётки с минимальными правами, анонимный доступ выключенным. Для приложений, которые умеют только bind, — выделенная сервисная учётка и обязательно TLS.
*Что хочет услышать:* понимание разницы между шифрованием канала и аутентификацией.
*Красный флаг:* «LDAP же и так безопасен, он же внутренний».

## 8. Источники (официальная документация)

- **freeipa.org** — раздел «Documentation» / «FreeIPA Documentation»; поиск: `freeipa server install documentation`, `freeipa trust active directory`.
- **docs.redhat.com** (Red Hat Identity Management, бывший RHEL IdM) — «Installing Identity Management», «Managing smart cards / trusts», «Configuring SSSD»; поиск: `red hat identity management trust active directory`, `red hat idm hbac`.
- **learn.microsoft.com** — «Active Directory Domain Services», «DNS conditional forwarders», «Kerberos authentication overview»; поиск: `active directory conditional forwarder dns`, `kerberos clock skew active directory`.
- **sssd.io** — «SSSD documentation», разделы про `id_provider = ad` / `ipa` и `sssd-ad`; поиск: `sssd ad provider id mapping`, `sssd.conf man page`.
- **web.mit.edu/kerberos** — MIT Kerberos Documentation (концепции TGT, keytab, `kinit`, `klist`); поиск: `mit kerberos documentation keytab`.
- **ietf.org** — RFC 4120 «The Kerberos Network Authentication Service (V5)», RFC 4511 «LDAP: The Protocol»; поиск: `rfc 4120 kerberos`, `rfc 4511 ldap`.
- **ubuntu.com** — «Ubuntu Server documentation», разделы про SSSD и аутентификацию; поиск: `ubuntu server sssd active directory join`.
- **astralinux.ru** — документация Astra Linux Directory (ALD Pro) как отечественная альтернатива FreeIPA; поиск: `ALD Pro документация домен аутентификация` [ПРОВЕРИТЬ актуальный URL раздела].
- **redos.red-soft.ru** — документация РЕД ОС и РЕД АДМ; поиск: `РЕД АДМ документация домен Linux` [ПРОВЕРИТЬ].
