# Безопасность Linux: SSH-hardening, fail2ban, firewalld/ufw, SELinux/AppArmor

## 1. Введение (зачем это нужно в реальной работе)

Утро, тикет в очереди L2:

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Мне по инструкции надо заливать выгрузку на UBU01 через WinSCP. Вчера всё работало, сегодня — „Connection refused“. Пробовал перезагрузить ноутбук, не помогло.»
> **Срочность:** средняя (отчёт нужен до 15:00)

Ты приходишь на UBU01, а там SSH вообще не отвечает — при этом сам сервер пингуется и `ss -tlnp` показывает, что порт 22 слушается. Значит, трафик режется **до** демона: либо файрвол, либо fail2ban забанил IP бухгалтера. Это классика: одну и ту же заявку «не подключается» дают пять разных причин, и надо уметь отличить их за минуту.

Linux-сервер без базовой защиты в интернете или даже в большой локалке живёт до первого сканера. После главы ты должен уметь: закрыть SSH от перебора (ключи вместо паролей, ограничения в конфиге), настроить host-firewall (ufw/firewalld поверх nftables), поставить fail2ban и понимать, где он пишет баны, а также объяснить разницу между правами доступа и мандатным контролем (SELinux/AppArmor) и найти в логах отказ LSM, из-за которого сервис «не видит файл».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Безопасность Linux-хоста — это слои. Снизу вверх:

```text
   WIN11-CL01 10.10.10.100
        │  TCP/22, ssh-клиент
        ▼
 ┌───────────────────────── UBU01 10.10.10.30 ─────────────────────────┐
 │ 1. netfilter в ядре (L3/L4)                                          │
 │      ufw / firewalld → это фронтенды, генерирующие правила nftables  │
 │ 2. fail2ban (демон, читает журналы и правит те же правила)           │
 │ 3. sshd :22 (L7) — кто ты: ключ/пароль, AllowGroups, MaxAuthTries    │
 │ 4. PAM / NSS / AD — проверка учётной записи                          │
 │ 5. LSM: AppArmor (Ubuntu) или SELinux (RHEL/часть отечественных ОС)  │
 │      — что РАЗРЕШЕНО процессу после того, как он уже запущен        │
 └──────────────────────────────────────────────────────────────────────┘
```

**SSH.** Протокол прикладного уровня (L7) поверх TCP/22. Клиент и сервер договариваются о шифровании, проверяют **host key** сервера (то, что оседает в `known_hosts`), затем аутентифицируют пользователя: паролем, публичным ключом или через Kerberos/GSSAPI (пригодится в уроке 58). Ключ ed25519 — пара файлов: приватный остаётся у клиента, публичный лежит на сервере в `~/.ssh/authorized_keys`. SSH **очень** придирчив к правам: если домашний каталог или `~/.ssh` доступны на запись группе/всем, вход по ключу будет отклонён.

**Host firewall.** В ядре Linux фильтрацией занимается `netfilter`, управляют им через `nftables` (современный инструмент) или `iptables` (легаси-синтаксис, на современных дистрибутивах обычно поверх того же nftables). `ufw` (Ubuntu/Debian) и `firewalld` (RHEL-семейство, Debian/Ubuntu тоже ставятся) — это **надстройки**, которые удобнее для человека, но под капотом те же правила ядра. Держать включёнными два фронтенда одновременно нельзя — они перетирают друг друга.

**fail2ban.** Демон на Python: следит за журналом (`/var/log/auth.log` или journald), регулярками ищет строки вида «Failed password for … from <IP>», считает попытки в окне `findtime` и при превышении `maxretry` добавляет правило бана в файрвол на `bantime`. Это не «антихакер», а защита от автоматического перебора: он не спасает от слабого пароля у одной попытки и не заменяет ключи.

**LSM (Linux Security Modules).** Обычные права (`rwx`, владелец, группа) — это **DAC**, дискреционный контроль: владелец сам решает, кому дать доступ. LSM добавляет **MAC**, мандатный контроль: политика задаётся администратором и процесс её обойти не может. `SELinux` вешает на каждый объект метку контекста и проверяет правила «процессу типа A можно читать файлы типа B». `AppArmor` работает по путям: «программе `/usr/sbin/nginx` разрешено читать `/var/www/**`, `/etc/nginx/**` и больше ничего». На Ubuntu 24.04 по умолчанию включён AppArmor, SELinux отсутствует. На Astra Linux своя история — там в защищённых редакциях используется собственная подсистема мандатного контроля, детали разберём в уроке 59 `[ПРОВЕРИТЬ]`.

## 3. Аналогия из жизни

Офис компании «Вектор». **SSH** — единственная входная дверь с кодовым замком. **Харденинг** — это заменить кодовый замок на замок с ключом (ключ не подберёшь перебором) и вывесить список тех, кому вообще можно входить в эту дверь. **Файрвол** — турникет перед дверью: он вообще не подпускает к ней тех, кто пришёл не из нашего бизнес-центра. **fail2ban** — охранник с журналом: он смотрит, кто три раза подряд ошибся кодом, и вычёркивает этого человека из списка входящих на час. **AppArmor/SELinux** — внутренний пропускной режим: даже если ты вошёл в здание, в бухгалтерию тебя пустят только с пропуском, где написано «этаж 3, кабинет 12». Аналогия перестаёт работать в одном месте: охранник (fail2ban) видит только то, что написано в журнале, — если дверь не логирует попытки, охранник просто спит.

## 4. Практика и команды

**1. Что вообще слушает и с какими настройками**

```bash
sudo ss -tlnp | grep ':22'
sudo sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|maxauthtries|allowgroups)'
```

`ss -tlnp` — сокеты: `-t` TCP, `-l` слушающие, `-n` без резолва, `-p` процесс. Вывод: `LISTEN 0 4096 0.0.0.0:22 users:(("sshd",pid=812,fd=3))`. Если слышишь `0.0.0.0:22` — слушаем все интерфейсы. `sshd -T` (нужен root) печатает **действующий** конфиг после разбора всех `Include` — это важнее, чем смотреть глазами в файл.

**2. Проверка конфига перед перезапуском**

```bash
sudo sshd -t && echo CONFIG_OK
```

`-t` — только проверка синтаксиса, без запуска. Всегда прогоняй перед `systemctl restart ssh`, иначе рискуешь остаться без демона и без входа.

**3. Ключи**

```bash
ssh-keygen -t ed25519 -C "adm.student@WIN11-CL01"
ssh-copy-id -i ~/.ssh/id_ed25519.pub adm.student@10.10.10.30
```

`-t ed25519` — современный быстрый тип ключа, `-C` — комментарий-метка. `ssh-copy-id` копирует публичную часть в `~/.ssh/authorized_keys` и выставляет права. На Windows `ssh-copy-id` нет — копируй вручную (см. лабораторную).

**4. Права на ключи**

```bash
chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && ls -ld ~ ~/.ssh
```

`drwx------` для домашнего каталога и `.ssh`, `-rw-------` для `authorized_keys`. Если в правах есть `w` для группы — sshd откажет в ключевом входе и напишет в лог `Authentication refused: bad ownership or modes`.

**5. ufw: состояние и профили**

```bash
sudo ufw status verbose
sudo ufw app list
```

Вывод `Status: active`, `Default: deny (incoming), allow (outgoing)`, далее список правил. `ufw app list` показывает готовые профили приложений (`Nginx Full`, `OpenSSH`), чтобы не писать порты руками.

**6. ufw: правила**

```bash
sudo ufw default deny incoming
sudo ufw allow from 10.10.10.0/24 to any port 22 proto tcp comment 'SSH office'
sudo ufw limit 22/tcp
sudo ufw enable
```

`default deny incoming` — вся входящая по умолчанию запрещена; `allow from ... to any port 22` — разрешить только из офисной подсети; `limit` — разрешить, но с rate-limit (примерно 6 соединений за 30 секунд), мягкая защита от перебора. `enable` включает файрвол и правила применяются сразу.

**7. Что под капотом у ufw**

```bash
sudo nft list ruleset | head -40
sudo iptables -S | head -20
```

Тут видно реальные цепочки: `chain ufw-user-input`, `ufw-before-input` и правила `accept`/`drop`. Смысл: когда ufw «не работает», смотри сюда — правило могло не создаться или его перетёр другой менеджер (Docker, libvirt).

**8. firewalld (если в компании RHEL/Астра/ALT)**

```bash
sudo firewall-cmd --state
sudo firewall-cmd --get-default-zone
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-all
```

Логика другая: есть **зоны** (`public`, `internal`, `dmz`) и **сервисы** (готовые наборы портов). `--permanent` пишет в конфиг, но не применяет — применяет `--reload`. Без `--permanent` правило живёт до перезагрузки.

**9. fail2ban: статус и управление**

```bash
sudo fail2ban-client status
sudo fail2ban-client status sshd
sudo fail2ban-client -t
sudo fail2ban-client set sshd unbanip 10.10.10.100
```

Первая команда — список включённых «тюрем» (jail). Вторая: `Filter: sshd`, `Currently failed: 2`, `Total failed: 15`, `Currently banned: 1`, `Banned IP list: 10.10.10.100`. `-t` — проверка конфига. `set sshd unbanip` — снять бан немедленно (частая операция в реальной поддержке).

**10. fail2ban: почему не банит**

```bash
sudo fail2ban-regex /var/log/auth.log /etc/fail2ban/filter.d/sshd.conf
sudo journalctl -u ssh -n 30 --no-pager
```

`fail2ban-regex` прогоняет фильтр по логу и показывает, сколько строк совпало (`Lines matched: 4`). Если 0 — либо не тот файл (на части установок `/var/log/auth.log` нет, и нужен `backend = systemd`), либо не тот формат строк.

**11. AppArmor**

```bash
sudo aa-status
sudo aa-complain usr.bin.man
sudo aa-enforce usr.bin.man
sudo journalctl -k | grep -i 'apparmor.*DENIED' | tail
```

`aa-status` — режим (`apparmor module is loaded`), сколько профилей в enforce/complain. `aa-complain` переводит профиль в режим «только логировать», `aa-enforce` возвращает блокировку. Отказы ищутся в ядерном журнале строкой `apparmor="DENIED" ... profile="/usr/sbin/..." name="/var/www/..."`.

**12. SELinux (теория + команды на будущее)**

```bash
getenforce            # Enforcing / Permissive / Disabled
sestatus
ls -Z /var/www/html   # контекст: unconfined_u:object_r:httpd_sys_content_t:s0
sudo ausearch -m AVC -ts recent
sudo restorecon -Rv /var/www/html
sudo semanage port -l | grep http
```

`Enforcing` — блокирует и логирует, `Permissive` — только логирует. `ls -Z`/`ps -eZ` показывают метки. `ausearch -m AVC` вытаскивает отказы из auditd. `restorecon` возвращает файлам «правильную» метку по политике (главное лекарство после копирования файлов через `cp`/`scp`). `semanage port -l` — какие порты разрешены сервису.

**13. Кто и как пытался зайти**

```bash
sudo lastb | head
sudo last -n 10
sudo journalctl -u ssh --since "1 hour ago" | grep -i 'failed\|invalid'
```

`lastb` — неудачные входы (читает `/var/log/btmp`, нужен root), `last` — удачные. Это первое, что смотрят при разборе «сервер тормозит» и «нас, кажется, ломают».

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — включён, есть учётная запись с `sudo` (в примерах `adm.student`; если в L1 ты назвал её иначе — подставляй своё имя), установлен `openssh-server`, вход по паролю работает.
- **WIN11-CL01** — включён, получил адрес по DHCP от DC01 (проверь `ipconfig`), есть встроенный клиент OpenSSH (`ssh -V` в PowerShell).
- **DC01, FS01** — включены (нужны для DHCP/DNS, менять их не будем).
- Машины `WEB01`, `GW01`, `MON01` не трогаем: firewall на шлюзе и веб-сервере настраивается в своих уроках.

### Снапшот

В VMware Workstation на **UBU01** и **WIN11-CL01**: `VM → Snapshot → Take Snapshot`, имя `before-l2-055`. Оба снапшота делай при выключенном или хотя бы «спокойном» состоянии гостя.

### Шаги

1. **UBU01, консоль VMware.** Инвентаризация:
   ```bash
   sudo ss -tlnp | grep ':22'
   sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin|allowgroups|maxauthtries)'
   sudo ufw status verbose
   sudo aa-status | head -5
   sudo systemctl status ssh.socket --no-pager
   ```
   Ожидаемо: sshd слушает `0.0.0.0:22`, `passwordauthentication yes`, ufw `inactive`, AppArmor `loaded`. Если `ssh.socket` в состоянии `active (listening)` — SSH запускается через сокет-активацию systemd; запомни это, ниже пригодится `[ПРОВЕРИТЬ]`.

2. **UBU01.** Готовим группу доступа:
   ```bash
   sudo groupadd ssh-users
   sudo usermod -aG ssh-users adm.student
   id -nG adm.student
   ```
   В выводе должна появиться `ssh-users`. Без этого шага следующий пункт отрежет вход.

3. **WIN11-CL01, PowerShell.** Генерируем ключ и кладём публичную часть на сервер:
   ```powershell
   ssh-keygen -t ed25519 -C "adm.student@WIN11-CL01"
   $pub = Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
   ssh adm.student@10.10.10.30 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '%s\n' '$pub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```
   На вопрос о пароле — вводим учебный `Lab#2026-vektor`. Ожидаемо: `authorized_keys` создан.

4. **WIN11-CL01.** Проверяем вход по ключу (пароль не должен спрашиваться):
   ```powershell
   ssh -i "$env:USERPROFILE\.ssh\id_ed25519" adm.student@10.10.10.30 hostname
   ```
   Ожидаемо: `UBU01`. Сессию не закрываем до шага 6.

5. **UBU01, консоль.** Включаем харденинг отдельным drop-in-файлом:
   ```bash
   ls /etc/ssh/sshd_config.d/
   sudo tee /etc/ssh/sshd_config.d/00-hardening.conf >/dev/null <<'EOF'
   PermitRootLogin no
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PubkeyAuthentication yes
   PermitEmptyPasswords no
   MaxAuthTries 3
   LoginGraceTime 30
   AllowGroups ssh-users
   X11Forwarding no
   ClientAliveInterval 300
   ClientAliveCountMax 2
   EOF
   ```
   Обрати внимание: имя начинается с `00-`, потому что sshd берёт **первое** встреченное значение параметра, а файлы из `sshd_config.d` подключаются в начале основного конфига и читаются по алфавиту. Если там лежит `50-cloud-init.conf` с `PasswordAuthentication yes`, он перебьёт твой файл, названный `99-*`.

6. **UBU01.** Проверяем и применяем:
   ```bash
   sudo sshd -t && echo CONFIG_OK
   sudo sshd -T | grep -Ei '^(passwordauthentication|permitrootlogin|allowgroups)'
   sudo systemctl restart ssh
   ```
   Ожидаемо: `CONFIG_OK`, `passwordauthentication no`, `permitrootlogin no`, `allowgroups ssh-users`. Проверка с WIN11-CL01: `ssh adm.student@10.10.10.30 hostname` — входит по ключу; `ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password adm.student@10.10.10.30` — `Permission denied (publickey)`. Если менял порт — на ssh.socket это не подействует без правки самого юнита `[ПРОВЕРИТЬ]`.

7. **UBU01.** Включаем ufw:
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow from 10.10.10.0/24 to any port 22 proto tcp comment 'SSH office'
   sudo ufw enable
   sudo ufw status verbose
   ```
   Ожидаемо: `Status: active`, правило `22/tcp ALLOW 10.10.10.0/24`. Существующая SSH-сессия не рвётся: ufw по умолчанию разрешает established-соединения.

8. **UBU01.** Ставим fail2ban:
   ```bash
   sudo apt update && sudo apt install -y fail2ban
   sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
   [DEFAULT]
   bantime  = 1h
   findtime = 10m
   maxretry = 5
   banaction = ufw

   [sshd]
   enabled = true
   backend = systemd
   EOF
   sudo fail2ban-client -t
   sudo systemctl enable --now fail2ban
   sudo fail2ban-client status sshd
   ```
   Ожидаемо: `-t` без ошибок, статус с `Currently failed: 0`, `Banned IP list:` пустой. `banaction = ufw` важен: баны попадут в цепочки ufw, а не в отдельные правила iptables, которые ufw-правила могут перекрыть `[ПРОВЕРИТЬ]`.

9. **WIN11-CL01, PowerShell.** Имитируем перебор — 6 неудачных попыток под несуществующим пользователем:
   ```powershell
   ipconfig | Select-String 'IPv4'
   1..6 | ForEach-Object {
     ssh -i "$env:USERPROFILE\.ssh\id_ed25519" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile="$env:TEMP\kh55" nosuchuser@10.10.10.30
   }
   ```
   Каждая попытка мгновенно даёт `Permission denied`. На UBU01:
   ```bash
   sudo fail2ban-client status sshd
   ```
   Ожидаемо: `Total failed: 6`, `Currently banned: 1`, в `Banned IP list` — адрес WIN11-CL01 (например `10.10.10.100`). Снимаем бан:
   ```bash
   sudo fail2ban-client set sshd unbanip 10.10.10.100
   ```
   и убеждаемся, что вход по ключу снова работает.

10. **UBU01.** Смотрим на AppArmor:
    ```bash
    sudo aa-status
    sudo journalctl -k | grep -i 'apparmor.*DENIED' | tail -5
    sudo apt install -y apparmor-utils
    sudo aa-complain usr.bin.man ; sudo aa-enforce usr.bin.man
    ```
    Ожидаемо: список профилей, переключение `man` в complain и обратно без ошибок. Отказов может не быть — это нормально, важен сам приём их чтения.

11. **Хост.** Записываем в `stand-log.md`: харденинг SSH на UBU01 (drop-in `00-hardening.conf`, ключи ed25519 у adm.student, группа `ssh-users`), ufw включён с правилом 22/tcp из 10.10.10.0/24, fail2ban с `jail.local`.

### Проверка

- [ ] `sudo sshd -T | grep passwordauthentication` → `no`
- [ ] Вход с WIN11-CL01 по ключу работает, по паролю — `Permission denied (publickey)`
- [ ] `sudo ufw status verbose` → `Status: active`, `Default: deny (incoming)`, правило на 22 из офисной подсети
- [ ] `sudo fail2ban-client status sshd` → jail активен, бан и разбан работают
- [ ] `sudo aa-status` → модуль загружен, профили в enforce
- [ ] В `stand-log.md` появилась запись про эту лабораторную

### Сломай и почини

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «WinSCP к 10.10.10.30 пишет „Connection refused“, вчера работало. Ноутбук перезагружал. Отчёт нужен до 15:00!»
> **Срочность:** средняя

**Воспроизводим.** На UBU01: `sudo fail2ban-client set sshd banip 10.10.10.100`. С WIN11-CL01 `ssh adm.student@10.10.10.30 hostname` — получаем `Connection refused` (при `banaction = ufw` возможен и таймаут — это нормально, бан может быть DROP, а не REJECT).

**Диагностика по порядку:**

1. `ping 10.10.10.30` с WIN11-CL01 — отвечает → сеть и маршрут живы, проблема выше L3.
2. На UBU01 `sudo ss -tlnp | grep ':22'` — слушает → демон жив.
3. `sudo journalctl -u ssh --since "15 min ago"` — **пусто, ни одной попытки подключения от 10.10.10.100**. Ключевой признак: трафик отсекается до sshd → файрвол/бан.
4. `sudo ufw status numbered` — видим deny/reject на 10.10.10.100 от fail2ban.
5. `sudo fail2ban-client status sshd` — подтверждаем: `Banned IP list: 10.10.10.100`.
6. `sudo journalctl -u ssh --since "2 hours ago" | grep 10.10.10.100` — находим причину: серия `Failed publickey for invalid user nosuchuser from 10.10.10.100` (у него в WinSCP сохранён старый профиль с неверным логином, клиент долбит сервер).

**Решение.** `sudo fail2ban-client set sshd unbanip 10.10.10.100`, затем поправить у пользователя сохранённую сессию (логин и ключ). Для себя: если такие «долбёжки» регулярны — увеличить `findtime`, поднять `maxretry`, либо добавить `ignoreip = 10.10.10.0/24` для офисной подсети (осознанно, понимая, что тогда перебор изнутри не блокируется).

**Ответ пользователю:** «Доступ восстановлен, попробуйте сейчас. Причина: на сервере сработала защита от перебора — в вашем WinSCP сохранён профиль со старым именем пользователя, из-за него шли неудачные попытки входа. Я поправил настройки сессии; если снова увидите „Connection refused“ — напишите, не пытайтесь входить много раз подряд, это снова включит блокировку на час.»

**Закрытие заявки:** категория «Доступы / SSH», в решении указать причину (fail2ban ban), действие (unban + правка профиля клиента) и профилактику (ignoreip или инструкция пользователю).

### Что изменилось на стенде

- **UBU01:** SSH только по ключам, root-вход запрещён, доступ ограничен группой `ssh-users`; drop-in `/etc/ssh/sshd_config.d/00-hardening.conf`; группа `ssh-users`, в ней `adm.student`.
- **UBU01:** ufw активен, default deny incoming, разрешён TCP/22 из 10.10.10.0/24.
- **UBU01:** установлен и включён fail2ban, конфиг `/etc/fail2ban/jail.local`, баны через ufw.
- **WIN11-CL01:** создан ключ `~/.ssh/id_ed25519`, публичная часть в `authorized_keys` на UBU01.
- **Важно для следующих глав:** на UBU01 теперь нельзя войти по паролю; с адресов вне 10.10.10.0/24 (например из DMZ 10.10.20.0/24) SSH закрыт — при необходимости добавляй правило ufw.

### Откат

VM → Snapshot Manager → выбрать `before-l2-055` на UBU01 (и на WIN11-CL01, если правил ключи) → **Restore Snapshot**. После отката не забудь вернуть в `stand-log.md` пометку, что изменения отменены. Точечный откат без снапшота: `sudo rm /etc/ssh/sshd_config.d/00-hardening.conf && sudo systemctl restart ssh`, `sudo ufw disable`, `sudo systemctl disable --now fail2ban` — но снапшот надёжнее.

## 6. Ошибки новичков и безопасность

- **Отключил пароли, не проверив ключ** → лок-аут, лечится только консолью VMware (в реале — IPMI/коворкинг с руках). Правильно: сначала проверяем вход по ключу **в отдельной сессии**, только потом `restart ssh`, старую сессию держим открытой.
- **`AllowGroups` без добавления себя в группу** → тот же лок-аут. Правильно: `id -nG` перед перезапуском.
- **`ufw enable` по SSH без разрешения 22** → доступ пропадает сразу после подтверждения. Правильно: сначала `ufw allow`, потом `enable`.
- **Меняешь `Port` в sshd_config, а порт не меняется** → на части современных Ubuntu SSH поднимается через `ssh.socket`, параметр `Port` в конфиге игнорируется `[ПРОВЕРИТЬ]`. Правильно: проверять `systemctl status ssh.socket` и править `ListenStream` через `systemctl edit ssh.socket`, либо отключать сокет-активацию.
- **Два файрвола одновременно** (ufw + firewalld, или ufw + правила Docker) → правила перетираются, диагностика превращается в квест. Правильно: один менеджер, остальное — только чтение через `nft list ruleset`.
- **fail2ban «не работает»** → бэкенд читает несуществующий `/var/log/auth.log` (только journald) или фильтр не совпадает. Правильно: `fail2ban-regex` по реальному логу и `backend = systemd`.
- **Забанил сам себя или 127.0.0.1** тестовыми попытками. Правильно: `ignoreip` для localhost, тесты — с отдельной ВМ, разбан через `fail2ban-client set sshd unbanip`.
- **Отключил AppArmor/SELinux целиком**, потому что «сервис не работает» → снимаешь целый слой защиты вместо разбора одной политики. Правильно: `aa-complain`/`Permissive` как временная мера на 10 минут, затем точечное правило.

**Безопасность.** В боевой среде нельзя: держать `PermitRootLogin yes` и общий пароль root, разрешать пароли на SSH-порту, открытом в интернет, хранить приватные ключи без парольной фразы на общих машинах, отключать LSM «навсегда», писать `ufw allow 22/tcp` без ограничения источника, если сервер виден снаружи. Смена порта SSH — не защита, это только шум в логах, сканеры находят SSH по баннеру. Все изменения подобного рода проходят через change management и сначала обкатываются на тестовом стенде.

## 7. Вопросы на собеседовании

**1. Чем ufw отличается от iptables и nftables?**

Ответ: iptables и nftables — инструменты управления netfilter в ядре (iptables — легаси-синтаксис, на современных дистрибутивах работает поверх nftables; nftables — актуальный). ufw — надстройка над ними, «простой файрвол» с профилями приложений, понятными правилами и логом. Под капотом ufw всё равно создаёт цепочки nftables/iptables. firewalld — аналогичная надстройка, но с зонами и сервисами, чаще в RHEL-семействе.

*Что хочет услышать интервьюер:* понимание, что «файрвол» — это один механизм в ядре, а разные команды — разные интерфейсы к нему.

*Красный флаг:* «ufw безопаснее, чем iptables» или «это разные файрволы, которые работают независимо».

**2. Как вы будете защищать SSH на новом сервере и как не потерять доступ?**

Ответ: по порядку — создать отдельную группу доступа, разложить публичные ключи и проверить вход по ключу в новой сессии; затем drop-in-файл: `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `MaxAuthTries 3`, `LoginGraceTime 30`, `AllowGroups`; `sshd -t`, `sshd -T` для проверки эффективного конфига, только потом `restart ssh`. Старую сессию не закрываю до успешной проверки, на стенде делаю снапшот, на боевом сервере заранее проверяю доступ через консоль/IPMI.

*Что хочет услышать интервьюер:* процедуру с проверкой и «страховкой», а не список директив.

*Красный флаг:* «поставлю Port 2222 и всё» или «сразу отключу пароли, потом проверю».

**3. Как работает fail2ban и почему он может не банить?**

Ответ: демон читает лог (`/var/log/auth.log` или journald через `backend = systemd`), регулярками фильтра ищет неудачные попытки, считает их в окне `findtime` и при `maxretry` добавляет бан в файрвол на `bantime`. Баны видны в `fail2ban-client status sshd`. Не банит, если: неверный бэкенд/путь к логу, фильтр не совпадает с форматом строк (проверяется `fail2ban-regex`), сервис не перезапущен после правки `jail.local`, IP в `ignoreip`, или правило бана перекрывается другим файрволом (например, ufw-правила стоят раньше вставленных fail2ban).

*Что хочет услышать интервьюер:* что первым делом надо проверить лог и совпадение фильтра, а не «переустановить fail2ban».

*Красный флаг:* «fail2ban защищает от брутфорса на уровне приложения» (нет, он работает на уровне файрвола по логам).

**4. SELinux и AppArmor — в чём разница и что из них на Ubuntu?**

Ответ: оба — LSM, добавляют мандатный контроль поверх обычных прав. SELinux использует метки (контексты) на файлах и процессах и правила типов — гибко, но сложно; AppArmor — профили по путям, проще и грубее. На Ubuntu по умолчанию AppArmor, SELinux — на RHEL/CentOS-семействе и части отечественных ОС. Отказы: AppArmor — `journalctl -k | grep apparmor`, SELinux — `ausearch -m AVC`, лечится `restorecon`/`semanage`/правкой профиля, а не отключением.

*Что хочет услышать интервьюер:* понимание, зачем вообще MAC, если есть права, и умение найти отказ в логе.

*Красный флаг:* «SELinux всегда надо отключить, он только мешает».

**5. Сервис падает с «Permission denied» на файл, а права `rwx` для всех стоят. Куда смотреть?**

Ответ: сначала `namei -l /path` — проверить права на всех уровнях пути, включая каталоги и монтирование; затем LSM: `ls -Z` (контекст SELinux) или `aa-status` и `journalctl -k | grep DENIED` для AppArmor; посмотреть `ausearch -m AVC -ts recent`. Типовые причины: файл скопирован `cp`/`scp` и получил неверную метку → `restorecon -Rv`; нестандартный порт → `semanage port -a`; сервис запущен под systemd с `ProtectSystem`/`PrivateTmp` (юниты разбирали в уроке 54) → путь в другом namespace.

*Что хочет услышать интервьюер:* порядок «права → путь → LSM → sandbox юнита», а не «chmod 777».

*Красный флаг:* «сделаю `chmod 777` и `setenforce 0`».

## 8. Источники (официальная документация)

- **ubuntu.com** — раздел *Ubuntu Server documentation*: страницы «OpenSSH server», «Firewall (UFW)», «AppArmor». Поиск: `ubuntu server openssh ufw apparmor documentation`.
- **openssh.com** — man-страницы `sshd_config(5)`, `ssh-keygen(1)`. Поиск: `sshd_config man page openssh`.
- **fail2ban.org** и **github.com/fail2ban/fail2ban** — wiki, раздел *Jails*, `jail.conf`, фильтр `sshd`. Поиск: `fail2ban jail.local sshd backend systemd`.
- **firewalld.org** — *Documentation*, разделы про зоны и сервисы. Поиск: `firewalld documentation zones services firewall-cmd`.
- **nftables.org** — wiki, раздел *Quickstart* (что реально делает ufw под капотом). Поиск: `nftables wiki quickstart`.
- **docs.redhat.com** — *Using SELinux* (руководство по SELinux, `restorecon`, `ausearch`, `semanage`) `[ПРОВЕРИТЬ точное название раздела]`. Поиск: `red hat using selinux guide restorecon ausearch`.
- **kernel.org** — *The Linux Kernel documentation*, раздел про LSM и AppArmor `[ПРОВЕРИТЬ]`. Поиск: `linux kernel documentation lsm apparmor`.
- **man-страницы дистрибутива** — `man sshd_config`, `man ufw`, `man fail2ban-client`, `man apparmor.d` (актуальны для конкретной версии в твоём стенде).
