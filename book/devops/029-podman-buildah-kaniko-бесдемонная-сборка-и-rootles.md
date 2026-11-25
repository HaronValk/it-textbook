# Podman, Buildah, Kaniko: бесдемонная сборка и rootless-контейнеры

## 1. Введение (зачем это нужно в реальной работе)

Тикет в общий чат IT от Ивана Иванова (учётка `ivanov`, на DevOps-стенде он гоняет сборки на `UBU01`):

> «Поставил на UBU01 podman, хочу собрать образ нашего сервиса проверки связи с WEB01. А он пишет `permission denied` на `/var/lib/containers` и не собирает. Почему нельзя просто поставить docker, как в интернете учат? И почему в комментариях пишут, что сборка без демона — это правильный подход для CI?»

Ситуация живая: в компании вы не можете ставить `dockerd` на каждый билд-агент, потому что демон работает от root и является одной большой дырой в безопасности и точкой отказа. В Kubernetes ставить Docker внутрь пода вообще нельзя (и с версии 1.24 Docker как runtime там не используется). Поэтому в реальной работе вы постоянно встречаете: `podman` на серверах и в CI-агентах, `buildah` в скриптах сборки, `kaniko` в пайплайнах Kubernetes, `skopeo` для переноса образов между реестрами. После этой главы вы должны уметь: запускать контейнеры под обычным пользователем, понимать, как работает user namespace и `/etc/subuid`, собирать образы тремя разными инструментами и объяснить на собеседовании, зачем это всё нужно и где ломается.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Начнём с того, что все участники говорят на одном языке — **OCI** (Open Container Initiative): спецификации *image* (как упакован образ), *runtime* (как запускать) и *distribution* (как отдавать образы по HTTP/Registry API). Поэтому образ, собранный Docker, спокойно запустится в Podman и наоборот.

### Docker: демон как единственный посредник

```text
docker CLI  --(unix socket /var/run/docker.sock)-->  dockerd (root)
                                                        |
                                                    containerd
                                                        |
                                          containerd-shim + runc (root)
                                                        |
                                                     контейнер
```

Ключевое: `dockerd` — долгоживущий процесс от root. CLI только разговаривает с ним по сокету. Упал демон — встали все контейнеры, которые он обслуживал; доступ к сокету = доступ к root на хосте.

### Podman: fork-exec и conmon

```text
podman run (ваш uid, без демона)
    |
    +-- conmon (монитор контейнера: stdio, логи, exit-код)
            |
            +-- crun/runc --> namespaces + cgroups --> процесс
```

`podman` не имеет сервера. Он сам создаёт namespaces, cgroups и запускает контейнер через OCI-runtime (`crun` — по умолчанию в Podman, либо `runc`). Между вами и контейнером стоит `conmon` («container monitor»): он держит stdin/stdout, пишет лог, ждёт завершения и остаётся жить после выхода `podman`. Именно поэтому «бесдемонный» — не значит «без фоновых процессов»: conmon остаётся, а вот единого привилегированного сервера нет.

Побочные следствия архитектуры: Podman умеет **поды** (`podman pod create` — общий network namespace для нескольких контейнеров, как в Kubernetes), умеет генерировать systemd-юниты и k8s-манифесты (`podman kube generate`), и его логи по умолчанию идут в journald.

### Rootless: user namespace и subuid

```text
хост (uid 1000 = вы)                  контейнер (выглядит как root)
  uid 1000      ------------------->   uid 0
  uid 100000    ------------------->   uid 1     (диапазон из /etc/subuid)
  ...
  uid 165535    ------------------->   uid 65536
```

При `podman run` без sudo Podman просит ядро создать **user namespace** и раскладывает uid'ы по карте. Ваш собственный uid снаружи становится uid 0 внутри (поэтому `id` в контейнере показывает `root`), а «остальной root» внутри — это диапазон 100000+ снаружи, выделенный вам в `/etc/subuid` и `/etc/subgid`. Запись в эти файлы делает пакет `uidmap` (`newuidmap`/`newgidmap` — setuid-хелперы, которые ядро пускает к записи uid_map).

Что ещё меняется в rootless-режиме:
- **Хранилище** — `~/.local/share/containers/storage`, а не `/var/lib/containers`. Образы, скачанные rootless и root, — это два разных стора.
- **overlayfs** — в user namespace работает (ядро 5.11+), иначе Podman откатывается на `fuse-overlayfs` (медленнее).
- **Сеть** — нельзя лезть в netfilter хоста, поэтому исходящий трафик идёт через userspace-стек: `slirp4netns` или `pasta`. Публикация порта `-p 8080:80` работает (это проксирование), а вот занять на хосте порт <1024 без `sysctl net.ipv4.ip_unprivileged_port_start` — нет.
- **cgroups** — через вашу user-сессию systemd, поэтому для «живучести» после логаута нужен `loginctl enable-linger`.

### Buildah и Kaniko: два разных способа собрать образ

**Buildah** — «сборщик без runtime»: он умеет и декларативно (`buildah build` по Dockerfile), и императивно (`from` → `run` → `copy` → `config` → `commit`). Работает в том же rootless-хранилище, что и Podman (`podman build` внутри — это фактически Buildah).

**Kaniko** — не сборщик на хосте, а *контейнер*, который собирает образ **изнутри себя**, без демона, без privileged-режима и без доступа к Docker-сокету:

```text
pod  [ kaniko executor container ]
       |-- распаковывает base image в свой собственный /
       |-- выполняет каждую инструкцию Dockerfile (RUN — в userspace)
       |-- после каждого шага делает снапшот ФС -> слой
       +-- push в registry  или  --tar-path (локальный tar)
```

Именно поэтому Kaniko любят в Kubernetes: узлу не нужно ничего, кроме возможности запустить под. Цена: скорость (снапшоты ФС), ограниченная поддержка BuildKit-синтаксиса и необходимость монтировать credentials в `/kaniko/.docker/config.json`.

## 3. Аналогия из жизни

Docker — это ресторан с одним метрдотелем (`dockerd`). Все заказы идут через него: он принимает, он же несёт на кухню, он же хранит ключи от всех дверей. Удобно, пока метрдотель на месте; заболел — ресторан закрыт, а кто угодно, кто перехватил его рацию, получает доступ ко всему зданию.

Podman — самообслуживание: каждый гость сам идёт на кухню и готовит себе сам, а рядом дежурит один официант (`conmon`), который следит за вашим столиком и записывает, что было. Один гость ушёл — остальные продолжают есть.

Rootless — это ещё и нумерация квартир в отеле: для вас ваш номер — «0», самый главный, но снаружи, на табличке дома, он числится как 100000. Вы можете быть «root» у себя, но в подъезде вы всё равно жилец с ограниченными правами.

Kaniko — передвижная кухня-фургон: приезжает внутрь вашего двора (пода), сама разгружается и готовит, ничего не требуя от дома, кроме места для парковки.

Где аналогия ломается: кухня-то у всех всё равно общая — одно ядро Linux, одни и те же порты и сеть. Rootless снижает риск «сбежать из контейнера до root хоста», но не отменяет уязвимостей ядра и ошибок в самих приложениях.

## 4. Практика и команды

Все команды — на `UBU01` (Ubuntu Server 24.04) под вашей обычной учётной записью, кроме строк с `sudo`.

**1. Установка набора инструментов**

```bash
sudo apt update
sudo apt install -y podman buildah uidmap skopeo
podman --version && buildah --version && skopeo --version
```

`uidmap` критичен: без `newuidmap`/`newgidmap` rootless не заведётся. Версии в Ubuntu 24.04 — 4.x/1.3x, точные номера `[ПРОВЕРИТЬ]`.

**2. Проверка, что режим действительно rootless**

```bash
podman info --format '{{.Host.Security.Rootless}}'
podman info | grep -Ei 'rootless|graphRoot|networkBackend|overlay'
```

Ожидаемо: `true`, `graphRoot` в вашем `$HOME`, backend `netavark`, сеть `slirp4netns` или `pasta` `[ПРОВЕРИТЬ]`. Если `false` — вы случайно под sudo или сломана карта uid.

**3. Диапазоны subuid/subgid**

```bash
cat /etc/subuid; cat /etc/subgid
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
sudo podman system migrate     # пересоздать хранилище с новой картой
```

Первая строка показывает `<логин>:100000:65536` — база и размер диапазона. Если вашей строки нет — rootless упадёт.

**4. Запуск контейнера без sudo**

```bash
podman run --rm -it alpine:3.20 id
podman unshare cat /proc/self/uid_map
```

Внутри — `uid=0(root)`, а `uid_map` снаружи покажет `0 <ваш uid> 1` и затем `1 100000 65536`. Это и есть магия user namespace.

**5. Порт и keep-id**

```bash
podman run -d --name web -p 8080:80 nginx:alpine
curl -sI http://localhost:8080 | head -1
podman run --rm --userns=keep-id -v "$PWD":/src:Z alpine:3.20 ls -ln /src
```

`-p 8080:80` — публикация на хосте (порт >1024, поэтому rootless не мешает). `--userns=keep-id` нужен, когда контейнер должен видеть файлы тома от вашего uid, а не от «root». `:Z` актуален на SELinux-системах (Astra, РЕД ОС); на Ubuntu безвреден.

**6. Сборка по Dockerfile (Podman = Buildah внутри)**

```bash
mkdir -p ~/vektor-tool && cd ~/vektor-tool
cat > Dockerfile <<'EOF'
FROM alpine:3.20
RUN apk add --no-cache curl
COPY check.sh /usr/local/bin/check.sh
ENTRYPOINT ["/usr/local/bin/check.sh"]
EOF
printf '#!/bin/sh\ncurl -sS -o /dev/null -w "%%{http_code}\\n" http://10.10.20.10\n' > check.sh
podman build --layers -t localhost/vektor-tool:1.0 .
```

`--layers` включает кэш промежуточных слоёв (в Buildah-режиме кэш по умолчанию не так агрессивен, как в Docker) `[ПРОВЕРИТЬ]`. Тег с префиксом `localhost/` — иначе Podman считает имя неполным и спрашивает реестр.

**7. Императивная сборка Buildah**

```bash
ctr=$(buildah from alpine:3.20)
buildah run "$ctr" -- apk add --no-cache curl
buildah copy "$ctr" check.sh /usr/local/bin/check.sh
buildah config --entrypoint '["/usr/local/bin/check.sh"]' "$ctr"
buildah commit "$ctr" localhost/vektor-tool:buildah
buildah rm "$ctr"
buildah images
```

`from` создаёт «рабочий контейнер», `run` выполняет команду в его rootfs (без запуска полноценного контейнера с изоляцией как у Podman), `config` пишет метаданные, `commit` фиксирует слой. Удобно, когда Dockerfile не нужен — например в скрипте, который дёргает `dnf install` в базе.

**8. Перенос образа без реестра**

```bash
podman save -o vektor-tool.tar localhost/vektor-tool:1.0
podman load -i vektor-tool.tar
skopeo inspect docker://docker.io/library/alpine:3.20 | head -20
```

`save/load` — транспорт `docker-archive`. `skopeo` умеет смотреть/копировать образы вообще без скачивания в локальное хранилище (`skopeo copy docker://... oci-archive:...`) — в РФ-реалиях это основной способ таскать образы через зеркала.

**9. Kaniko без демона и без кластера**

```bash
cd ~/vektor-tool
podman run --rm -v "$PWD":/workspace:Z \
  gcr.io/kaniko-project/executor:latest \
  --context=dir:///workspace \
  --dockerfile=/workspace/Dockerfile \
  --destination=localhost/vektor-tool:kaniko \
  --no-push --tar-path=/workspace/kaniko-out.tar \
  --snapshot-mode=redo
podman load -i kaniko-out.tar
podman images | grep vektor-tool
```

Разбор: `--context` — что монтируем как контекст; `--dockerfile`; `--destination` — имя будущего образа; `--no-push` + `--tar-path` — не пушить, а положить tar; `--snapshot-mode=redo` — более честный подсчёт изменений ФС. Тег `:latest` лучше зафиксировать на конкретный `v1.x` `[ПРОВЕРИТЬ]`. Образ исторически лежит в `gcr.io`; из РФ он может быть недоступен — тяните его через локальное зеркало или реестр компании (легально, без «левых» сборок).

**10. Автозапуск: systemd + Quadlet**

```ini
# ~/.config/containers/systemd/web.container
[Unit]
Description=Vektor web (rootless podman)

[Container]
Image=localhost/vektor-tool:1.0
PublishPort=8080:80

[Install]
WantedBy=default.target
```

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user start web.service
systemctl --user status web.service --no-pager
```

Quadlet — генератор юнитов из `.container`-файлов (Podman 4.4+; в старых статьях вы увидите `podman generate systemd`, он объявлен устаревшим). `enable-linger` нужен, чтобы контейнер жил без вашей SSH-сессии. Точные пути и имя генератора `[ПРОВЕРИТЬ]`.

**11. Поды и диагностика**

```bash
podman pod create --name vektor -p 8080:80
podman ps -a --pod
podman logs web --tail 20
podman inspect web --format '{{.State.Pid}} {{.HostConfig.NetworkMode}}'
```

Один под = общий network namespace; по такому же принципу устроены поды в Kubernetes.

GUI в Podman есть (`podman-desktop`), но на сервере без графики L1-админ всё делает через CLI — запомните команды, а не кнопки.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Готов `UBU01` (10.10.10.30, Ubuntu Server 24.04), доступ по SSH под вашей обычной учётной записью с sudo.
- Доступен `WEB01` (10.10.20.10, nginx) — на нём проверяем, что собранный образ реально что-то делает.
- Windows-часть стенда (DC01, FS01) в DevOps-треке может отсутствовать — она здесь не нужна.
- Кластер `k8s-cp01/k8s-w01/k8s-w02` на этом уроке **не используется** (он появится в модуле про Kubernetes); Kaniko проверяем локально в Podman.
- `GIT01` и `HARBOR01` тоже пока не трогаем: реестр — урок 31.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-029`. Сделать на `UBU01`. Если план сломается настолько, что проще откатиться — снапшот вернёт систему без podman и без ваших образов.

### Шаги

1. **UBU01.** Поставьте пакеты: `sudo apt update && sudo apt install -y podman buildah uidmap skopeo`. Ожидаемо: все четыре пакета установлены, `podman --version` печатает версию.
2. **UBU01.** Проверьте карту uid: `cat /etc/subuid`. Ожидаемо: строка с вашим логином и диапазоном `100000:65536`. Если строки нет — `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"` и `sudo podman system migrate`.
3. **UBU01.** `podman info --format '{{.Host.Security.Rootless}}'`. Ожидаемо: `true`. Запишите в `stand-log.md`, какой network backend и storage driver подхватились.
4. **UBU01.** `podman run --rm -it alpine:3.20 sh -c 'id; cat /proc/self/uid_map'`. Ожидаемо: `uid=0(root)` внутри, карта показывает ваш хостовый uid на позиции 0 и `100000` на позиции 1.
5. **UBU01.** Создайте `~/vektor-tool` с `Dockerfile` и `check.sh` из раздела 4 (шаг 6). Соберите: `podman build --layers -t localhost/vektor-tool:1.0 .`. Ожидаемо: сборка проходит без sudo, в конце `Successfully tagged localhost/vektor-tool:1.0`.
6. **UBU01.** Запустите и проверьте: `podman run --rm localhost/vektor-tool:1.0`. Ожидаемо: в stdout код ответа `200` от WEB01. Если WEB01 выключен — сначала поднимите его.
7. **UBU01.** Повторите сборку императивно через Buildah (шаг 7 раздела 4). Ожидаемо: `buildah images` показывает `localhost/vektor-tool:buildah`; размер сопоставим с первым образом.
8. **UBU01.** Соберите тот же Dockerfile через Kaniko командой из шага 9 раздела 4. Ожидаемо: в логе `INFO[...] Taking snapshot of full filesystem...`, затем появление `kaniko-out.tar`; `podman load -i kaniko-out.tar` добавляет образ `localhost/vektor-tool:kaniko`.
9. **UBU01.** Сравните образы: `podman images --format '{{.Repository}}:{{.Tag}} {{.Size}}'` и `podman history localhost/vektor-tool:1.0`. Ожидаемо: у Dockerfile/Podman и Kaniko-варианта одинаковый набор инструкций, но разные digest'ы (слои собирались по-разному).
10. **UBU01.** Настройте автозапуск через Quadlet (шаг 10 раздела 4): `web.container`, `daemon-reload`, `start`, `status`. Ожидаемо: `active (running)`, `curl -I http://localhost:8080` отдаёт ответ. Затем `systemctl --user stop web.service` и `rm ~/.config/containers/systemd/web.container` — сервис нам постоянно не нужен, но навык зафиксирован.
11. **UBU01.** Допишите в `stand-log.md`: установленные пакеты, версии, где лежит storage (`podman info | grep graphRoot`), какие образы созданы.

### Проверка

- [ ] `podman info --format '{{.Host.Security.Rootless}}'` возвращает `true`.
- [ ] В `/etc/subuid` и `/etc/subgid` есть строка с вашим логином.
- [ ] `podman run --rm alpine:3.20 id` работает **без** sudo.
- [ ] `podman images` содержит `localhost/vektor-tool:1.0`, `:buildah` и `:kaniko`.
- [ ] `podman run --rm localhost/vektor-tool:1.0` печатает HTTP-код от WEB01.
- [ ] `ls -l ~/.local/share/containers/storage` существует и принадлежит вам, `/var/lib/containers` не использовался.
- [ ] В логе Kaniko нет слова `error`, а `kaniko-out.tar` загружается в Podman.
- [ ] `stand-log.md` обновлён и содержит дату, версии и перечень образов.

### Сломай и почини

> **Пользователь:** `ivanov`
> **ВМ:** UBU01 (10.10.10.30)
> **Что написал:** «После обеда перестало собираться: `podman run` пишет `newuidmap: write to uid_map failed: Operation not permitted`, и вообще любой контейнер падает. Ничего не трогал!»
> **Срочность:** средняя (сборки стоят, прод не затронут)

**Как воспроизвести поломку.** На UBU01 под root уберите строку своего пользователя из `/etc/subuid` (сохраните её в файл, чтобы вернуть):

```bash
grep "$USER" /etc/subuid | sudo tee /root/subuid.bak
sudo sed -i "/^$USER:/d" /etc/subuid
podman run --rm alpine:3.20 echo hi     # ожидаемо: ошибка про newuidmap/uid_map
```

**Диагностика — по порядку:**

1. Что вообще происходит: `podman --log-level=debug run --rm alpine:3.20 echo hi 2>&1 | tail -30`. Ищем `newuidmap`, `uid_map`, `Operation not permitted`.
2. Карта: `grep "$USER" /etc/subuid /etc/subgid`. Пусто → причина найдена.
3. Хелперы: `which newuidmap newgidmap && ls -l $(which newuidmap)`. Должны быть и должны быть setuid (`rws`). Нет — `sudo apt install --reinstall uidmap`.
4. Ядро/AppArmor: `sysctl kernel.apparmor_restrict_unprivileged_userns 2>/dev/null; sudo dmesg | tail -20`. На Ubuntu 24.04 ограничение непривилегированных user namespaces включено по умолчанию и часть программ из-за него падает с тем же `Operation not permitted` `[ПРОВЕРИТЬ]` — проверяем до правки subuid.
5. Хранилище: `podman info | grep -E 'graphRoot|rootless'` — не должно быть `false`.

**Решение:**

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
grep "$USER" /etc/subuid
podman system migrate
podman run --rm alpine:3.20 echo ok
```

**Ответ пользователю:** «Причина — пропала запись о вашем пользователе в `/etc/subuid`: без неё ядро не может собрать карту uid'ов для rootless-контейнера, отсюда `Operation not permitted`. Запись восстановил, `podman system migrate` прогнал, тестовый контейнер и сборка `vektor-tool` проходят. Пожалуйста, не редактируйте `/etc/subuid` вручную — если нужно поменять диапазон, пишите в IT. Заявку закрываю, если в течение дня повторится — откройте новую со временем и текстом ошибки».

**Закрытие заявки:** категория «контейнеры / rootless», решение «восстановлен диапазон subuid/subgid», в `stand-log.md` добавляем запись «чинили rootless на UBU01: subuid восстановлен, storage мигрирован».

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлены `podman`, `buildah`, `uidmap`, `skopeo`; для вашего пользователя в `/etc/subuid` и `/etc/subgid` выделен диапазон `100000-165535`; включён linger (`loginctl enable-linger`).
- **UBU01:** создан каталог `~/vektor-tool` с `Dockerfile` и `check.sh` — это будущий артефакт для пайплайна на `GIT01` (урок про CI/CD).
- **UBU01:** локальные образы `localhost/vektor-tool:1.0`, `:buildah`, `:kaniko`; storage rootless в `~/.local/share/containers/storage`.
- **UBU01:** временный Quadlet-юнит `web.container` (создан и удалён в рамках лабы).
- **WEB01 (10.10.20.10):** без изменений, использовался как цель проверки.
- `stand-log.md`: запись о пакетах, версиях, диапазонах subuid и созданных образах.

### Откат

```bash
podman system reset --force         # снести rootless-хранилище, образы, контейнеры, поды
sudo apt remove --purge podman buildah skopeo
rm -rf ~/vektor-tool
```

Если хочется вернуть стенд «как было до главы» целиком — VMware Workstation: правый клик по UBU01 → **Snapshot Manager** → выбрать `before-devops-029` → **Go to**. Все образы и пакеты исчезнут, `stand-log.md` придётся поправить вручную.

## 6. Ошибки новичков и безопасность

1. **Привычка делать всё через `sudo podman`.** → Образы уезжают в root-хранилище `/var/lib/containers`, rootless-режим не проверяется, на собеседовании вы не можете показать главную фишку Podman. → Сначала пробуйте без sudo; `sudo podman` — только осознанно и с комментарием зачем.
2. **Собрал как root, запускаю как пользователь (или наоборот).** → `image not known` / `short-name did not resolve`, потому что это два разных стора. → Держите один режим; проверяйте `podman info | grep graphRoot` перед диагностикой.
3. **Порт 80 напрямую под rootless.** → `bind: permission denied`. → Публикуйте `-p 8080:80` или меняйте `net.ipv4.ip_unprivileged_port_start` через sysctl (это уже изменение политики хоста).
4. **`--privileged` как «лекарство от всех ошибок».** → Контейнер получает практически все права хоста, смысл rootless обнуляется. → Сначала читайте лог; чаще всего нужен `--userns=keep-id`, том с правильными правами или capability (`--cap-add`).
5. **Правка `/etc/subuid` руками, без `podman system migrate`.** → Существующее хранилище привязано к старой карте uid, контейнеры падают на доступе к файлам. → `usermod --add-subuids`, затем `podman system migrate`.
6. **Ожидание, что `podman generate systemd` — современный путь.** → Команда помечена устаревшей, статьи в интернете устарели вместе с ней. → Quadlet (`.container`-файлы) + `systemctl --user`, либо Kubernetes-манифесты.
7. **Kaniko с `--cache` и без `--cache-repo`.** → Кэш молча не работает, каждая сборка полная. → Указывайте `--cache-repo=<registry>/cache` и помните, что Kaniko кэширует не так, как Docker.
8. **Считать Kaniko заменой Dockerfile-синтаксиса целиком.** → `RUN --mount=type=secret`, heredoc'и и прочий BuildKit-синтаксис не поддерживаются — сборка падает на разборе Dockerfile. → Для таких проектов берите BuildKit (rootless `buildkitd`) или `buildah` в CI.

### Безопасность

В боевой среде: никогда не монтируйте `docker.sock` и не запускайте сборку с `--privileged` на общем билд-агенте; не храните credentials реестра в Dockerfile (в Kaniko — только Secret, смонтированный в `/kaniko/.docker/config.json`); помните, что rootless и Kaniko **не** являются границей безопасности для вредоносного кода — `RUN` в Kaniko выполняется с правами root внутри контейнера, а ядро у всех общее. И не тащите образы из случайных «зеркал»: в РФ-реалиях используйте официальные зеркала и локальный реестр (тема урока 32).

## 7. Вопросы на собеседовании

**1. Чем Podman отличается от Docker, кроме отсутствия демона?**

`dockerd` — долгоживущий root-процесс, через который идут все операции; CLI общается с ним по сокету. В Podman клиент сам через fork-exec создаёт namespaces и cgroups и запускает OCI-runtime (`crun`/`runc`), а надзор за контейнером держит `conmon` (stdio, логи, exit-код). Отсюда следствия: нет единой точки отказа и единого сокета с root-правами, работает rootless из коробки, нативно есть поды, интеграция с systemd (Quadlet), логи в journald. Обратная сторона: нет `docker build`-демона с общим кэшем между пользователями, часть экосистемы (Compose, тестовые окружения) требует явной настройки, а «бесдемонность» не значит «нет фоновых процессов».

*Что хочет услышать интервьюер:* понимание архитектуры (podman → conmon → crun) и практических следствий, а не заученное «Podman — это Docker без демона».
*Красный флаг:* «Podman — это форк Docker» или «в Podman вообще нет фоновых процессов, поэтому контейнер умирает после выхода из терминала».

**2. Как работает rootless-контейнер и что нужно на хосте?**

Podman создаёт user namespace и с помощью setuid-хелперов `newuidmap`/`newgidmap` раскладывает uid'ы по карте из `/etc/subuid` и `/etc/subgid`: ваш хостовый uid становится uid 0 внутри, остальные uid'ы контейнера уходят в диапазон 100000+. Хранилище переезжает в `~/.local/share/containers/storage`, overlay собирается в user namespace (или через `fuse-overlayfs`), сеть — userspace-стек `slirp4netns`/`pasta`, cgroups — через user-сессию systemd, поэтому для постоянной работы нужен `loginctl enable-linger`. Требования: пакет `uidmap`, строки в subuid/subgid, ядро с поддержкой user namespaces.

*Что хочет услышать интервьюер:* что вы понимаете маппинг uid и умеете диагностировать `Operation not permitted`.
*Красный флаг:* «rootless-контейнер запускается от root, просто без sudo» или «в rootless нельзя публиковать порты».

**3. Buildah и Podman build — в чём разница и когда что брать?**

`podman build` внутри использует Buildah, так что результат одинаковый. Buildah — самостоятельный инструмент: умеет декларативную сборку по Dockerfile (`buildah build`, ранее `bud`) и императивную (`from` → `run` → `copy` → `config` → `commit`), умеет монтировать rootfs (`buildah mount`, в rootless — внутри `buildah unshare`). Императивный путь удобен, когда сборка — это скрипт без Dockerfile или когда нужно доточить чужой базовый образ; декларативный — когда нужна воспроизводимость и ревью в Git.

*Что хочет услышать интервьюер:* что вы выбираете инструмент под задачу и знаете про общий storage.
*Красный флаг:* «Buildah — отдельный формат образов» (нет, образы OCI/Docker совместимы).

**4. Как Kaniko собирает образ без демона и чего он не умеет?**

Kaniko — контейнер, который распаковывает базовый образ в свою корневую ФС, выполняет инструкции Dockerfile в userspace (RUN — прямо в себе), после каждого шага делает снапшот файловой системы и превращает разницу в слой, затем пушит результат или пишет tar (`--no-push --tar-path`). Демон, privileged-режим и Docker-сокет не нужны, узлу ничего устанавливать не надо — поэтому он идеален для Kubernetes (credentials монтируются в `/kaniko/.docker/config.json`, контекст обычно приходит из init-контейнера с git clone). Не поддерживает BuildKit-синтаксис (`RUN --mount`, heredoc'и), кэширование требует `--cache-repo`, а снапшоты ФС делают сборку медленнее. Плюс стоит проверить актуальный статус поддержки проекта — вокруг Kaniko в последние годы были разговоры о темпе мейнтейна, и альтернативы (rootless BuildKit, Buildah в поде, `ko` для Go) вполне живые `[ПРОВЕРИТЬ]`.

*Что хочет услышать интервьюер:* понимание схемы «снапшот ФС = слой» и ограничений.
*Красный флаг:* «Kaniko — это демон внутри пода» или «Kaniko умеет всё, что Dockerfile».

**5. Как вы запустите контейнер в проде без Docker?**

Rootless Podman + systemd: либо Quadlet (`.container`-файл в `~/.config/containers/systemd/`, `systemctl --user daemon-reload`, `enable-linger`), либо `podman generate kube` → манифесты для Kubernetes. Логи — journald (`journalctl --user -u web`), обновление образов — через `podman auto-update` с `AutoUpdate=registry` или через пайплайн (сборка → реестр → рестарт юнита), конфиги — томами и `--env-file`. Всё это лежит в Git, никаких ручных «поставил и забыл».

*Что хочет услышать интервьюер:* что вы мыслите «контейнер = сервис под systemd/оркестратором», а не «контейнер = docker run в nohup».
*Красный флаг:* `docker run -d` без рестарт-политики и без описания в репозитории.

## 8. Источники (официальная документация)

- **podman.io** — официальный сайт Podman: разделы *Getting Started*, *Docs → podman*(1) man pages. Поиск: `podman rootless tutorial`.
- **docs.podman.io** — документация Podman: *Rootless mode*, *podman-systemd.unit (Quadlet)*, *podman-info*. Поиск: `podman quadlet systemd user unit`.
- **buildah.io** — сайт Buildah: *Getting Started*, *buildah-build*, *buildah-from/run/commit*. Поиск: `buildah build tutorial rootless`.
- **github.com/GoogleContainerTools/kaniko** — README и `docs/` (раздел *Limitations*, *Caching*, *Running Kaniko*). Поиск: `kaniko executor no-push tar-path`.
- **github.com/containers/skopeo** — README, раздел *Copying images*. Поиск: `skopeo copy oci-archive`.
- **kubernetes.io** — раздел *Concepts → Containers* и документация по подам (общая картина, где живёт Kaniko). Поиск: `kubernetes pod container runtime interface`.
- **docs.redhat.com** — *Building, running, and managing containers* (Podman/Buildah, RHEL docs) — хорошее практическое дополнение, терминология совпадает. Поиск: `redhat podman rootless subuid`.
- **ubuntu.com / help.ubuntu.com** — установка пакетов `podman`, `buildah`, `uidmap`; заметки о `kernel.apparmor_restrict_unprivileged_userns` в 24.04 `[ПРОВЕРИТЬ]`.

Всё, что касается конкретных версий, тегов образов и поведения сетевых бэкендов (`slirp4netns` vs `pasta`) в вашей сборке Ubuntu, сверяйте с `podman info` и `man podman` на самом стенде — документация быстро меняется.
