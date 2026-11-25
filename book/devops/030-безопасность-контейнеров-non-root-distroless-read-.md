# Безопасность контейнеров: non-root, distroless, read-only FS, capabilities, seccomp, Trivy/Grype

## 1. Введение (зачем это нужно в реальной работе)

Тикет #INC-4471, автор — Алексей Сидоров (`sidorov`, склад): «Добрый день! Мне переслали письмо от сервиса сканирования — пишут, что в нашем GitLab-проекте `webapp` образ содержит 47 уязвимостей уровня HIGH и что контейнер работает от root. Начальник склада спрашивает, могут ли через это утечь данные поставщиков. Разберитесь, пожалуйста». Срочность: средняя.

Такие заявки приходят постоянно, как только в компании появляется хоть один контейнер. Причина простая: контейнер — это не «маленькая виртуалка», а обычный процесс на хосте, только в изоляции. Ядро у всех контейнеров общее. Значит, «сломанный забор» в одном контейнере — это потенциальная дырка в ядро и в соседей. ИБ-аудитор или сканер из CI однажды спросит: «а почему `nginx` у вас от root, с полным набором capabilities и записываемым корнем?» — и надо будет не покраснеть.

После этой главы вы сможете: запускать контейнеры от непривилегированного пользователя, собирать образы на distroless/scratch, переводить корневую ФС в read-only, отрезать capabilities, включать seccomp-профиль, сканировать образы Trivy и Grype и встраивать это в CI так, чтобы пайплайн падал на критичных CVE. Это ровно тот набор, который на собеседовании называют «container hardening» и «shift left security».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Безопасность контейнера — это не одна кнопка, а слои. Их удобно делить на «что внутри образа» (supply chain) и «как он запущен» (runtime).

```text
 ┌──────────────────── границы доверия ────────────────────┐
 │                                                          │
 │  ОБРАЗ (что внутри)          RUNTIME (как запущен)       │
 │  ├─ базовый слой             ├─ UID/GID, userns-remap    │
 │  │   scratch / distroless    ├─ capabilities (cap-drop)  │
 │  ├─ пакеты → CVE (Trivy)     ├─ seccomp (какие syscall)  │
 │  ├─ секреты в слоях?         ├─ AppArmor / SELinux       │
 │  └─ USER nonroot             ├─ rootfs read-only         │
 │                              ├─ no-new-privileges        │
 │                              └─ limits: cpu/mem/pids     │
 │                                                          │
 │  ОБЩЕЕ ЯДРО ХОСТА — единая точка отказа для всех         │
 │  контейнеров: namespaces + cgroups + LSM                 │
 └──────────────────────────────────────────────────────────┘
```

Разберём слои по порядку, снизу вверх по «глубине» защиты.

**Ядро и namespaces.** Контейнер изолирован пространствами имён (урок 26): PID, NET, MNT, UTS, IPC, USER. Изоляция — это не запрет, а «свой вид на систему». Если процесс внутри — root (UID 0), то в рамках своего namespace он почти всё может: менять владельцев файлов, вызывать `mount`, открывать raw-сокеты. Namespace не спасает от эксплойта ядра. Отсюда правило №1 — **non-root**: `USER 10001` в Dockerfile или `--user 10001:10001` в `docker run`. Тогда даже при RCE атакующий получает бесправного пользователя.

**Capabilities.** В Linux привилегии root разбиты на ~40 capabilities (`CAP_NET_BIND_SERVICE`, `CAP_SYS_ADMIN`, `CAP_CHOWN`, `CAP_NET_RAW`...). Docker по умолчанию даёт контейнеру урезанный, но всё же заметный набор (порядка 14 штук). `CAP_SYS_ADMIN` — это фактически «новый root»: монтирование, `unshare`, часть работы с namespace. Принцип: `--cap-drop=ALL`, потом добавить ровно одну-две нужные.

**seccomp** (secure computing mode) — фильтр системных вызовов на уровне ядра. Программа говорит ядру: «этот syscall разрешён, этот вернёт EPERM, а на этом — убить процесс». Docker по умолчанию применяет довольно большой deny-list профиль: блокируются `reboot`, `kexec_load`, `mount` без прав, `ptrace` в ряде случаев и т.п. `seccomp` сужает поверхность атаки на ядро: если в приложении дыра, но нужного syscall в белом списке нет — эксплойт не сработает.

**AppArmor / SELinux** — мандатный контроль доступа (MAC): не «какие syscall», а «к каким файлам и операциям процесс вообще имеет право». На Ubuntu 24.04 по умолчанию AppArmor; Docker навешивает профиль `docker-default` [ПРОВЕРИТЬ имя профиля в вашей версии]. Это ортогонально seccomp, они дополняют друг друга.

**Read-only rootfs.** `--read-only` монтирует корень контейнера только для чтения. Приложение не сможет дописать веб-шелл, подменить бинарник, положить в `/tmp` второй стейдж. Всё, что реально нужно писать, выносится в явные `--tmpfs` или volume.

**no-new-privileges** запрещает повышение прав через setuid/setgid-бинарники (`sudo`, `su`, `ping`). Одна строчка, которая закрывает целый класс атак.

**Distroless / scratch.** В обычном `ubuntu:24.04` внутри лежат shell, apt, coreutils, curl — идеальный конструктор для атакующего после RCE. Distroless (образы Google) содержит только рантайм приложения (glibc/CA-сертификаты) и ничего больше; `scratch` — вообще пустой слой, годится для статически слинкованного Go-бинарника (урок 22). Меньше файлов → меньше CVE → меньше что делать внутри.

**Сканеры.** Trivy (Aqua Security) и Grype (Anchore) читают слои образа, составляют список пакетов (SBOM) и сверяют с базами уязвимостей (NVD, дистрибутивные трекеры, GitHub Advisory). Оба умеют сканировать по SBOM (`cyclonedx`, `spdx`) — это правильный «shift left»: сканируем не образ, а артефакт сборки, ещё до пуша в реестр.

Где всё это живёт по портам/сокетам: Docker CLI общается с демоном через `/var/run/docker.sock` (Unix-сокет, не TCP) — и это тоже часть темы безопасности, потому что доступ к сокету = root на хосте. containerd — через `/run/containerd/containerd.sock`. Сканеры ходят в интернет за базами: Trivy тянет БД из `ghcr.io` [ПРОВЕРИТЬ актуальный источник], Grype — с endpoint'ов Anchore [ПРОВЕРИТЬ]. Из РФ доступность этих источников не guaranteed — про зеркала и прокси-кэш будет урок 32.

## 3. Аналогия из жизни

Представьте съёмную квартиру в большом доме. Дом — это ядро Linux, квартиры — контейнеры, стены — namespaces. **Non-root** — жилец не владелец квартиры: мебель стоит, но перепланировку не согласует. **Capabilities** — не связка ключей от всего дома, а отдельные ключи: от подъезда, от почтового ящика, от кладовки. `--privileged` — это когда выдали ключ от подвала, крыши и щитовой «на всякий случай». **seccomp** — список разрешённых инструментов: молоток и отвёртка можно, болгарку и сварочник нельзя, даже если они в квартире лежат. **AppArmor** — правила ТСЖ: «к газовой плите доступ есть, к электрощитку нет». **Read-only FS** — квартира без права переставлять мебель: жить можно, но баррикаду из шкафов не построишь. **Distroless** — квартира без кладовки с инструментами: нечего схватить, когда ворвался чужой. **Trivy/Grype** — досмотр багажа на входе: до того, как жилец въехал, проверяем, что он не принёс.

Аналогия ломается в одном месте: стены между квартирами здесь бумажные — все квартиры стоят на одном фундаменте-ядре, и трещина в фундаменте (эксплойт ядра) обрушивает весь дом, а не одну квартиру.

## 4. Практика и команды

Все команды — на `UBU01` (10.10.10.30) с Docker. Аналоги на Podman почти дословные (урок 29).

**1. Проверяем, от кого работает контейнер.**

```bash
docker run --rm nginx:1.27 id
docker run --rm --user 10001:10001 nginx:1.27 id
```
Первый вывод: `uid=0(root) gid=0(root)` — вот вам и «47 HIGH в отчёте + root сверху». Второй: `uid=10001 gid=10001 groups=10001`. Ключ `--user UID:GID` переопределяет `USER` из образа. Смотреть на `uid`: ноль — плохо.

**2. Non-root в Dockerfile (правильный способ, а не флагом в рантайме).**

```dockerfile
FROM debian:12-slim
RUN groupadd -g 10001 app && useradd -u 10001 -g app -M -s /usr/sbin/nologin app
COPY --chown=10001:10001 ./webapp /usr/local/bin/webapp
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/webapp"]
```
`groupadd/useradd` создают учётку без домашнего каталога (`-M`) и без shell (`-s /usr/sbin/nologin`) — заходить внутрь некуда. `--chown` в `COPY` сразу ставит правильного владельца. `USER` действует на все следующие слои и на запуск.

**3. Read-only корень + явные записываемые места.**

```bash
docker run -d --name web-nginx \
  --read-only \
  --tmpfs /var/cache/nginx:rw,noexec,nosuid,size=32m \
  --tmpfs /var/run:rw,noexec,nosuid,size=8m \
  -p 8080:80 nginx:1.27
```
`--read-only` — корень только для чтения. `--tmpfs` монтирует tmpfs в память с ограничением размера; `noexec` запрещает исполнять оттуда файлы, `nosuid` игнорирует setuid-биты. Для nginx эти два каталога обязательны, иначе он не стартует (см. раздел «Сломай и почини»).

**4. Capabilities: отрезать всё, вернуть нужное.**

```bash
docker run --rm --cap-drop=ALL nginx:1.27 sh -c 'grep -E "^Cap" /proc/self/status'
docker run --rm --cap-drop=ALL --cap-add=NET_BIND_SERVICE nginx:1.27 sh -c 'grep CapEff /proc/self/status'
```
Смотрим `CapEff` (effective set) — это hex-битовая маска. С `--cap-drop=ALL` она `0000000000000000`, с добавленной capability — уже не ноль. Расшифровать маску: `capsh --decode=0000000000000400` (пакет `libcap2-bin`).

**5. Список capabilities живого контейнера.**

```bash
docker run -d --name c1 nginx:1.27
docker exec c1 sh -c 'apt-get update -qq && apt-get install -y -qq libcap2-bin && getpcaps 1'
```
`getpcaps <pid>` печатает человекочитаемый набор. У дефолтного Docker-контейнера вы увидите что-то вроде `chown, dac_override, fowner, setgid, setuid, net_bind_service, net_raw, sys_chroot, mknod, audit_write, setfcap`. `net_raw` и `mknod` приложению почти никогда не нужны.

**6. no-new-privileges.**

```bash
docker run --rm --security-opt no-new-privileges:true debian:12-slim \
  sh -c 'ls -l /usr/bin/su && su -s /bin/sh nobody -c id || echo "su заблокирован"'
```
Флаг запрещает любые переходы прав через setuid. В рабочей среде это дефолт для всех контейнеров.

**7. seccomp: свой чёрный список.**

```json
{
  "defaultAction": "SCMP_ACT_ALLOW",
  "architectures": ["SCMP_ARCH_X86_64"],
  "syscalls": [
    { "names": ["mkdir", "mkdirat"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1 }
  ]
}
```
```bash
docker run --rm --security-opt seccomp=./no-mkdir.json alpine:3.20 sh -c 'mkdir /tmp/x; echo "код выхода: $?"'
```
`defaultAction: SCMP_ACT_ALLOW` — разрешено всё, кроме перечисленного; `SCMP_ACT_ERRNO` с `errnoRet: 1` (EPERM) — вернуть «Operation not permitted»; `SCMP_ACT_KILL_PROCESS` — убить. Проверить, что демон вообще умеет seccomp: `docker info | grep -i seccomp` → `seccomp: Security Options`. `[ПРОВЕРИТЬ]` точное имя ключа `errnoRet` в вашей версии Docker — формат профиля менялся, сверьтесь с документацией Moby.

**8. Ресурсные лимиты — тоже безопасность (DoS изнутри).**

```bash
docker run -d --name limited \
  --memory 128m --memory-swap 128m --cpus 0.5 \
  --pids-limit 100 --ulimit nofile=1024:1024 nginx:1.27
```
`--pids-limit` защищает от fork-бомбы, `--memory` — от «съел всю память хоста».

**9. Rootless / userns-remap на уровне демона.**

```json
{
  "userns-remap": "default"
}
```
в `/etc/docker/daemon.json`, затем `sudo systemctl restart docker`. UID 0 внутри контейнера маппится в непривилегированный UID на хосте (обычно из диапазона 100000+). Проверить: `docker run --rm alpine id` покажет `uid=0(root)`, а `ps -o uid,cmd -C nginx` на хосте — большой UID. Альтернатива без демона — Podman rootless (урок 29).

**10. Ищем секреты в слоях — их не спрятать.**

```bash
docker history --no-trunc 10.10.10.55/vektor/webapp:1.0.0 | head -20
```
`--no-trunc` показывает полные команды слоёв. Если в истории видно `ENV DB_PASSWORD=...` или `COPY .env /app/` — пароль уже в образе, и `docker save` его достанет, даже если следующим слоем файл удалили. Секреты — только в переменные окружения рантайма или в secret-хранилище.

**11. Trivy: сканирование образа, IaC и SBOM.**

```bash
trivy image --severity HIGH,CRITICAL nginx:1.27
trivy image --scanners vuln,secret,misconfig 10.10.10.55/vektor/webapp:1.0.0
trivy config ./Dockerfile
trivy image --format cyclonedx --output sbom.cdx.json 10.10.10.55/vektor/webapp:1.0.0
```
Первая команда печатает таблицу `Пакет | Уязвимость | Severity | Установленная версия | Исправлено в`. Ключевое: колонка «Fixed Version» — если там пусто, официального патча нет. `--exit-code 1` заставит команду вернуть ненулевой код (нужно в CI). Флаг `--scanners` в разных версиях назывался по-разному (`--security-checks`) — `[ПРОВЕРИТЬ]` под свою версию.

**12. Grype: второй мнение и работа по SBOM.**

```bash
grype nginx:1.27
grype sbom:./sbom.cdx.json --fail-on high
```
`--fail-on high` — аналог `--exit-code` у Trivy: падает, если найдено HIGH и выше. Сканирование по SBOM удобнее в CI: SBOM собран один раз на этапе сборки, сканировать его можно многократно и без Docker-демона.

**13. Kubernetes: то же самое, но декларативно (пригодится, когда поднимем кластер).**

```yaml
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: webapp
      image: registry.vektor.lab/vektor/webapp:1.0.0
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: { drop: ["ALL"] }
      volumeMounts: [{ name: tmp, mountPath: "/tmp" }]
  volumes:
    - name: tmp
      emptyDir: { sizeLimit: 16Mi }
```
Маппинг один-в-один: `readOnlyRootFilesystem` ↔ `--read-only`, `capabilities.drop` ↔ `--cap-drop`, `allowPrivilegeEscalation: false` ↔ `no-new-privileges`, `seccompProfile` ↔ `--security-opt seccomp`. Не ссылайтесь на `HARBOR01` в этом манифесте до урока 31 — реестр появится там.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- `UBU01` (10.10.10.30) — Ubuntu Server 24.04 LTS, установлен Docker Engine, есть доступ в интернет (или к локальному зеркалу образов — тема урока 32).
- `GIT01` (10.10.10.50) — GitLab CE + runner, проект `vektor/webapp` с `Dockerfile` из урока 27 и `.gitlab-ci.yml`.
- Образ `vektor/webapp:1.0.0` собран локально на `UBU01` (Docker Hub из РФ может быть недоступен; если так — возьмите зеркало, см. урок 32, и пометьте это в `stand-log.md`).
- `HARBOR01` пока **не нужен** — реестр ставим в уроке 31. Кластер Kubernetes тоже появится позже, поэтому всё делаем на Docker, а манифест из п.4.13 сохраняем на будущее.

### Снапшот

В VMware Workstation на `UBU01` и `GIT01`: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-030`. (Если хост позволяет — обе ВМ выклюшенными, так снапшот компактнее.)

### Шаги

1. **UBU01.** Ставим сканеры. `sudo apt-get update && sudo apt-get install -y wget gnupg` затем Trivy официальным скриптом `curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sudo sh -s -- -b /usr/local/bin` и Grype `curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sudo sh -s -- -b /usr/local/bin`. Ожидаемо: `trivy --version` и `grype version` печатают версии. Если GitHub недоступен — ставьте из apt-пакета/локального зеркала `[ПРОВЕРИТЬ]` и зафиксируйте это в журнале.

2. **UBU01.** Базовая линия. `trivy image --severity HIGH,CRITICAL nginx:1.27 | tee baseline-nginx.txt`. Ожидаемо: таблица с десятками CVE, часть — с заполненной «Fixed Version». Запишите число в журнал: это точка отсчёта.

3. **UBU01.** Сканируем свой образ. `trivy image --scanners vuln,secret,misconfig vektor/webapp:1.0.0 | tee baseline-webapp.txt`. Ожидаемо: список CVE + возможные находки по секретам и по misconfig в Dockerfile. Если секретов нет — Trivy так и напишет.

4. **UBU01.** Воспроизводим проблему из тикета. `docker run --rm vektor/webapp:1.0.0 id`. Ожидаемо: `uid=0(root)`. Делаем скриншот в отчёт.

5. **UBU01.** Собираем hardened-версию. В каталоге проекта создаём `Dockerfile.hardened` на базе distroless (`gcr.io/distroless/static-debian12:nonroot` для Go-бинарника; `[ПРОВЕРИТЬ]` доступность `gcr.io` из вашей сети — если недоступно, используйте `scratch` + `CGO_ENABLED=0`). Сборка: `docker build -f Dockerfile.hardened -t vektor/webapp:1.1.0-hardened .`. Ожидаемо: образ собирается, размер заметно меньше исходного (`docker images | grep webapp`).

6. **UBU01.** Проверяем, что внутрь не зайти. `docker run --rm -it --entrypoint sh vektor/webapp:1.1.0-hardened`. Ожидаемо: ошибка вида `exec: "sh": executable file not found` — shell отсутствует, это и есть цель.

7. **UBU01.** Запускаем по всем правилам. Команда из п.4.3 + `--user 65532:65532 --cap-drop=ALL --security-opt no-new-privileges --pids-limit 100 --memory 128m`. Ожидаемо: контейнер в состоянии `Up`, `docker exec ... id` (для отладки) — `uid=65532`.

8. **UBU01.** Проверяем read-only: `docker exec webapp-hardened touch /test 2>&1`. Ожидаемо: `Read-only file system`. Запись в `/tmp` при этом проходит.

9. **UBU01.** Проверяем seccomp-профиль из п.4.7: `docker run --rm --security-opt seccomp=./no-mkdir.json alpine:3.20 mkdir /tmp/x`. Ожидаемо: `mkdir: can't create directory '/tmp/x': Operation not permitted`.

10. **GIT01.** Встраиваем сканер в CI. В `.gitlab-ci.yml` добавляем job:

```yaml
container-scan:
  stage: test
  image: aquasec/trivy:latest
  variables:
    TRIVY_NO_PROGRESS: "true"
  script:
    - trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA"
  allow_failure: false
```
Коммитим, пушим. Ожидаемо: пайплайн зелёный на `1.1.0-hardened` и красный на старой ветке — это видно в GitLab → CI/CD → Pipelines.

11. **UBU01.** `stand-log.md`: записываем новые теги, установленные сканеры, номера CVE до/после, имя профиля seccomp.

### Проверка

- [ ] `trivy --version` и `grype version` работают на `UBU01`.
- [ ] `baseline-nginx.txt` и `baseline-webapp.txt` сохранены, числа зафиксированы в журнале.
- [ ] `docker run --rm vektor/webapp:1.1.0-hardened id` → не `uid=0`.
- [ ] `--entrypoint sh` в hardened-образе падает (shell отсутствует).
- [ ] `docker exec ... touch /test` → `Read-only file system`.
- [ ] `--cap-drop=ALL` даёт `CapEff: 0000000000000000`.
- [ ] seccomp-профиль блокирует `mkdir` с EPERM.
- [ ] Пайплайн на `GIT01` падает при HIGH/CRITICAL и проходит на hardened-образе.
- [ ] В `stand-log.md` появилась запись про урок 030.

### Сломай и почини

**Пользователь:** Алексей Сидоров (`sidorov`, склад). **ВМ:** `UBU01` (сервис `web-nginx` на порту 8080). **Что написал:** «Открываю тестовый портал — "сайт недоступен". Вчера работало, мы поставщику в 15:00 каталог показываем». **Срочность:** Высокая.

**Как воспроизвести поломку.** Намеренно «ужесточаем» nginx и забываем про каталоги, куда он пишет:

```bash
docker rm -f web-nginx
docker run -d --name web-nginx --read-only -p 8080:80 nginx:1.27
```

**Диагностика по шагам.**

1. `docker ps -a --filter name=web-nginx` — STATUS `Restarting (1)` или `Exited (1)`. Значит, процесс падает на старте, а не «сеть отвалилась».
2. `docker logs --tail 20 web-nginx` — ключевая строка: `mkdir() "/var/cache/nginx/client_temp" failed (30: Read-only file system)`. Диагноз готов.
3. `docker inspect web-nginx --format '{{.HostConfig.ReadonlyRootfs}} {{json .HostConfig.Tmpfs}}'` → `true map[]`. Подтверждаем: корень ro, tmpfs не смонтированы.
4. `curl -I http://localhost:8080` → `Connection refused`. Это следствие, а не причина — не начинайте с него.
5. Сверяемся с `stand-log.md`: вчера контейнер запускали без `--read-only`.

**Решение.**

```bash
docker rm -f web-nginx
docker run -d --name web-nginx \
  --read-only \
  --tmpfs /var/cache/nginx:rw,noexec,nosuid,size=32m \
  --tmpfs /var/run:rw,noexec,nosuid,size=8m \
  --cap-drop=ALL --cap-add=NET_BIND_SERVICE \
  --security-opt no-new-privileges \
  -p 8080:80 nginx:1.27
```
Проверка: `curl -I http://localhost:8080` → `HTTP/1.1 200 OK` (или `301`).

**Ответ пользователю:** «Алексей, добрый день! Причина — мои изменения в конфигурации контейнера: я включил режим "только для чтения", а nginx нужно два служебных каталога для временных файлов. Добавил их как временные тома, сервис поднят, портал открывается. К 15:00 всё будет работать. Если увидите ошибку — пишите сразу».

**Закрытие заявки:** в комментарии — причина, решение, команда восстановления, ссылка на коммит/изменение конфигурации; тип — «Change related», не «инцидент по вине пользователя».

### Что изменилось на стенде

- `UBU01`: установлены `trivy` и `grype` в `/usr/local/bin`.
- `UBU01`: собран образ `vektor/webapp:1.1.0-hardened` (distroless/scratch, non-root UID 65532, без shell).
- `UBU01`: в проекте появился `Dockerfile.hardened` и файл профиля `no-mkdir.json`.
- `UBU01`: работают контейнеры `web-nginx` (read-only + tmpfs + cap-drop) и `webapp-hardened`.
- `GIT01`: в `.gitlab-ci.yml` добавлен job `container-scan` (Trivy, `--exit-code 1`, HIGH/CRITICAL).
- `GIT01`: сохранены артефакты сканирования `baseline-*.txt`.

### Откат

VMware Workstation → правый клик по `UBU01` → **Snapshots** → `before-devops-030` → **Revert**. То же на `GIT01`. Быстрая уборка без отката:

```bash
docker rm -f web-nginx webapp-hardened limited c1 2>/dev/null
docker rmi vektor/webapp:1.1.0-hardened
sudo rm -f /usr/local/bin/trivy /usr/local/bin/grype
```
На `GIT01`: revert коммита с `container-scan` (`git revert <sha>`) и пуш.

## 6. Ошибки новичков и безопасность

1. **`--privileged` «чтобы заработало».** → Контейнер получает все capabilities, доступ к устройствам, отключённые seccomp/AppArmor-ограничения — это фактически root на хосте. → Правильно: `--cap-drop=ALL`, добавить одну capability, а если чего-то не хватает — разбираться, чего именно, а не снимать все ограничения.
2. **Проброс `/var/run/docker.sock` внутрь контейнера.** → Любой процесс в контейнере может запустить привилегированный контейнер на хосте — полный побег из изоляции. → Правильно: не монтировать сокет; для сборки — Kaniko/Buildah (урок 29).
3. **`--security-opt seccomp=unconfined` при первом же падении.** → Снимается целый слой защиты ядра, часто это «лечение симптома» вместо поиска причины. → Правильно: посмотреть `dmesg`/логи, добавить конкретный syscall в свой профиль.
4. **Секреты в слоях образа.** → `ENV DB_PASSWORD=...`, `COPY .env`, `ARG` с паролем видны в `docker history` и остаются в `docker save`. → Правильно: секреты — в переменные окружения рантайма, Docker secrets или внешний Vault; в `.dockerignore` — `.env`, `.git`, ключи.
5. **`latest` вместо фиксированного тега или digest.** → Непонятно, что именно проверял сканер, и `docker pull` может притащить другую версию. → Правильно: пиновать `nginx:1.27.3` или `@sha256:...`, обновлять осознанно.
6. **`--ignore-unfixed` в CI «чтобы позеленело».** → Уязвимости без патча просто перестают замечаться. → Правильно: отдельный отчёт по unfixed, регулярный пересмотр, а блокирующий гейт — только по тем, где патч есть.
7. **Сканирование только на этапе сборки.** → Образ в реестре живёт месяцами, БД уязвимостей обновляется ежедневно. → Правильно: сканировать и в CI, и периодически в реестре (Harbor умеет, урок 31), и по SBOM.
8. **Игнорирование не-root-пользователя в compose-файлах.** → `docker compose up` плодит контейнеры от root, потому что в Compose никто не прописал `user:`. → Правильно: `user: "10001:10001"`, `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` прямо в `compose.yaml`.

**Безопасность.** В лабораторной мы намеренно отключаем часть ограничений и запускаем «сломанные» контейнеры — на домашнем стенде это допустимо. В боевой среде так нельзя: `--privileged`, `seccomp=unconfined`, монтирование `/` хоста внутрь контейнера и запуск от root — это прямые пункты на отзыв доступов. Отдельно: не сканируйте Trivy/Grype образы с непроверенных registry (сканер поднимает содержимое образа, и старые версии парсеров имели CVE), держите базу уязвимостей обновлённой, не публикуйте отчёты с внутренними именами пакетов в открытый доступ.

## 7. Вопросы на собеседовании

**Вопрос 1. Namespaces же изолируют контейнер — зачем тогда запускать его не от root?**

Ответ: namespaces дают лишь видимость изоляции: внутри своего user namespace процесс с UID 0 по-прежнему имеет root-полномочия в рамках предоставленных capabilities. Если контейнер скомпрометирован, root внутри может менять владельцев файлов на смонтированных томах, использовать setuid-бинарники, обходить часть ограничений, а при удачном эксплойте ядра — атаковать хост. Non-root (а лучше rootless + userns-remap) снижает импакт от RCE до «бесправный процесс». Плюс многие Kubernetes-политики (`restricted` PSS, `runAsNonRoot: true`) просто не дадут запустить root-под.

*Что хочет услышать интервьюер:* понимание, что namespace ≠ привилегии, и знание про userns-remap/rootless. *Красный флаг:* «в контейнере root не страшен, он же в контейнере».

**Вопрос 2. Чем `--cap-drop=ALL` плюс одна capability лучше, чем `--privileged`?**

Ответ: `--privileged` выдаёт все capabilities, доступ ко всем устройствам хоста, снимает seccomp/AppArmor-профили и делает `/sys`, `/proc` почти хостовыми — это путь к побегу. Минимальный набор capabilities реализует принцип наименьших привилегий: приложению нужен привилегированный порт — даём только `CAP_NET_BIND_SERVICE`, и всё. Если приложение не работает без `CAP_SYS_ADMIN`, это повод пересмотреть архитектуру, а не выдавать всё.

*Что хочет услышать интервьюер:* конкретные имена capabilities и понимание, что `CAP_SYS_ADMIN` ≈ root. *Красный флаг:* «privileged — это просто удобный флаг для отладки».

**Вопрос 3. Как работает seccomp и чем отличается от AppArmor/SELinux?**

Ответ: seccomp — фильтр системных вызовов на уровне ядра (BPF): разрешает/запрещает/убивает по номеру syscall, работает по принципу белого или чёрного списка. AppArmor и SELinux — мандатный контроль доступа к объектам (файлы, сокеты, capability-операции) по профилям/меткам. Они решают разные задачи: seccomp сужает поверхность атаки на ядро, AppArmor ограничивает, к каким путям процесс имеет доступ. В Docker по умолчанию включены оба: дефолтный seccomp-профиль и `docker-default` AppArmor.

*Что хочет услышать интервьюер:* что это не «или-или», а два независимых слоя, и что дефолты уже что-то блокируют. *Красный флаг:* «seccomp и AppArmor — одно и то же».

**Вопрос 4. Зачем distroless, если можно взять alpine — он же маленький?**

Ответ: размер — вторичен, главное — поверхность атаки. В alpine есть shell, apk, busybox — после RCE атакующему есть чем работать: скачать второй стейдж, распаковать, запустить. В distroless нет ни shell, ни пакетного менеджера, ни лишних библиотек — эксплойту нечего использовать, и CVE меньше, потому что меньше пакетов. Минус — сложнее отладка: внутрь не зайти, нужны `kubectl debug` с ephemeral-контейнером или отладочный тег образа. Для статически слинкованного Go-бинарника идеален `scratch`.

*Что хочет услышать интервьюер:* приоритет поверхности атаки над размером и понимание цены — усложнение отладки. *Красный флаг:* «distroless нужен, чтобы образ весил 5 МБ».

**Вопрос 5. Trivy показывает 200 уязвимостей в базовом образе. Ваши действия?**

Ответ: сначала триаж — какие severity, есть ли `Fixed Version`, какой пакет и используется ли он вообще в рантайме. Потом: обновить базовый образ до актуального патч-тега (часто убирает 80% находок), перейти на более тонкий базовый слой (distroless/scratch), удалить ненужные пакеты. В CI — гейт `--exit-code 1 --severity HIGH,CRITICAL` и, при необходимости, `--ignore-unfixed` для блокирующего режима плюс отдельный не блокирующий отчёт по unfixed. Настроить регулярное пересканирование в реестре и хранить SBOM как артефакт сборки.

*Что хочет услышать интервьюер:* процесс (триаж → снижение → гейт → пересканирование), а не «поставлю `--ignore-unfixed` и всё». *Красный флаг:* «просто выключу проверку, чтобы пайплайн был зелёный».

## 8. Источники (официальная документация)

- **Docker Docs** — `docs.docker.com`. Разделы: «Engine security», «Seccomp security profiles for Docker», «Running the daemon with user namespace remapping». Поиск: `docker seccomp security profile`, `docker userns-remap`.
- **Trivy (Aqua Security)** — репозиторий `github.com/aquasecurity/trivy`, документация на `trivy.dev`. Разделы: «Image scanning», «Misconfiguration scanning», «SBOM», «CI integration». Поиск: `trivy image severity exit-code`, `trivy cyclonedx sbom`.
- **Grype (Anchore)** — `github.com/anchore/grype`. Раздел «Usage / Scanning an SBOM». Поиск: `grype fail-on severity`, `grype sbom`.
- **Distroless (GoogleContainerTools)** — `github.com/GoogleContainerTools/distroless`. Раздел «Why distroless / Tags». Поиск: `distroless nonroot tag`. `[ПРОВЕРИТЬ]` актуальные имена тегов и доступность `gcr.io` из вашей сети.
- **Kubernetes Docs** — `kubernetes.io`. Разделы: «Configure a Security Context for a Pod or Container», «Pod Security Standards», «Restrict a Container's Capabilities». Поиск: `kubernetes securityContext readOnlyRootFilesystem`, `pod security standards restricted`.
- **OCI Runtime Spec** — `github.com/opencontainers/runtime-spec`. Раздел «Linux capabilities / seccomp». Поиск: `oci runtime spec seccomp`.
- **Linux man-pages** — `man7.org`. Страницы `capabilities(7)`, `seccomp(2)`, `user_namespaces(7)`. Поиск: `man 7 capabilities`, `man 2 seccomp`.
- **CIS Benchmarks** — `cisecurity.org`. «CIS Docker Benchmark», «CIS Kubernetes Benchmark». Поиск: `CIS Docker Benchmark`.
- **NIST** — `csrc.nist.gov`. «NIST SP 800-190: Application Container Security Guide». Поиск: `NIST SP 800-190 container security`.
- **AppArmor** — `gitlab.com/apparmor/apparmor`, вики на `apparmor.net` `[ПРОВЕРИТЬ актуальный домен]`. Поиск: `docker-default apparmor profile`.
