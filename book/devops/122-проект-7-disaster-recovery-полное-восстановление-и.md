# Проект №7: Disaster Recovery — полное восстановление инфраструктуры из кода и бэкапов

## 1. Введение (зачем это нужно в реальной работе)

**Тикет 08:47.** «Пользователь: ivanov (бухгалтерия). ВМ: WIN11-CL01. Что написал: „Портал заказов не открывается, белая страница. У нас отгрузки, клиенты ждут“. Срочность: высокий». Дежурный открывает Grafana — дашборд пустой. Заходит по SSH на `k8s-cp01` — `kubectl` не отвечает. Ещё через десять минут выясняется, что и `GIT01` недоступен. Компания «Вектор» только что потеряла и кластер, и Git, и реестр образов. Вот тут и выясняется, чего стоит вся предыдущая работа.

Disaster Recovery (DR, аварийное восстановление) — это не «мы делаем бэкапы». Это дисциплина, которая отвечает на вопрос: «если завтра в 8 утра у нас не останется ничего, кроме бэкапов, за сколько часов мы вернёмся в строй и сколько данных потеряем?» Бэкап, который ни разу не восстанавливали, — это не бэкап, а надежда. В DevOps-мире у нас есть козырь: инфраструктура описана кодом в Git, значит часть «железа» можно поднять заново — но только если код, секреты и образы тоже сохранены. После этой главы вы будете уметь: считать RTO/RPO для сервисов, строить бэкап-схему по правилу 3-2-1, восстанавливать etcd, GitLab, Harbor и Vault, пересобирать кластер с нуля из кода и — главное — проводить учебную тревогу и писать по ней отчёт.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Три вещи, которые новички путают, а интервьюер различает мгновенно:

- **HA (высокая доступность)** — переживаем отказ *одного компонента*: упал узел — под переехал. Не спасает от «ударили не ту команду» или «потеряли всю площадку».
- **Бэкап** — копия данных на момент времени. Сам по себе ничего не восстанавливает.
- **DR** — процесс и план: кто, что и в каком порядке поднимает после катастрофы.

Две метрики, вокруг которых строится вся глава:

```text
         последняя успешная          МОМЕНТ СБОЯ            сервис снова работает
              копия                     |                            |
                |                       |                            |
   <--- RPO --->|                       |<---------- RTO ------------>|
   (сколько данных                      |      (сколько длится простой)
    мы готовы потерять)                 |
```

**RPO (Recovery Point Objective)** — сколько данных допустимо потерять: если бэкап раз в сутки, RPO = 24 часа. **RTO (Recovery Time Objective)** — сколько времени допустимо лежать. RTO в 1 час при восстановлении GitLab «на глазок» по памяти — это фантастика; RTO подтверждается только замером на тренировке.

**Правило 3-2-1 (и его современное расширение 3-2-1-1-0):** 3 копии данных, 2 разных носителя, 1 копия вне основной площадки, 1 копия неизменяемая (immutable/object lock — защита от шифровальщика), 0 ошибок при проверке восстановления.

**Порядок восстановления важнее скорости.** Зависимости образуют граф: без секретов приложение не стартует, без реестра не тянет образ, без Git нечем управлять кластером.

```text
 1. Сеть, DNS, точное время (chrony)         <- без этого не работает ничего
        |
 2. Секреты: Vault (raft snapshot) + unseal keys в офлайн-сейфе
        |
 3. Исходники: GitLab (код, манифесты, пайплайны) + gitlab-secrets.json
        |
 4. Реестр образов: Harbor (Postgres + /data/registry)
        |
 5. Кластер: etcd-снапшот ИЛИ kubeadm init заново + ArgoCD bootstrap
        |
 6. Приложения: ArgoCD sync из Git (декларативное состояние)
        |
 7. Ingress + сертификаты (cert-manager), внешний DNS
        |
 8. Наблюдаемость: MON01, Prometheus/Grafana/Loki — чтобы видеть, что всё ожило
```

Ключевая идея DevOps-DR: **кластер — расходник, Git — источник истины**. Всё, что описано манифестами (Deployment, Service, Ingress, NetworkPolicy), после катастрофы воспроизводится из Git. Данные (Postgres, PVC, Git-репозитории, образы в реестре) и секреты — воспроизводятся только из бэкапа. Отсюда два разных инструмента: **декларативное восстановление** (ArgoCD/Kustomize/Helm) и **восстановление состояния** (etcd-снапшот, `pg_dump`, restic, Velero).

Важный нюанс: **etcd — это состояние всего кластера Kubernetes**. Потеря etcd = потеря всех объектов API (включая PVC и Secret'ов в etcd). Поэтому снапшот etcd делают на каждом control plane по расписанию, а не «когда вспомним».

## 3. Аналогия из жизни

Представьте склад компании «Вектор». Есть **план эвакуации на стене** (runbook) и **огнетушители** (бэкапы). Пока пожара не было, всё выглядит нормально. Но проверить план можно только одним способом — учебной тревогой: вывести людей, пересчитать по списку, засечь время. Если при тревоге выясняется, что пожарный выход завален коробками, вы узнаете об этом в спокойный вторник, а не в ночь, когда горит. Бэкап без восстановления — это план эвакуации, нарисованный от руки. И ещё: план должен висеть на стене у каждого, а не лежать в голове у одного уборщика (bus factor).

Аналогия ломается в одном: в реальной жизни переезд после пожара — это недели и потерянные вещи, а в IT мы можем *отрепетировать* восстановление на копии, не останавливая продакшен.

## 4. Практика и команды

### 4.1. Снапшот etcd (ядро DR для Kubernetes)

```bash
sudo ETCDCTL_API=3 etcdctl snapshot save /srv/backup/etcd-$(date +%F).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key
```

`ETCDCTL_API=3` — версия API (в etcd 3.4+ по умолчанию 3, но привычка полезна). `--endpoints` — локальный API etcd. Три сертификата обязательны: etcd слушает только TLS. Типичный вывод:

```text
{"level":"info","ts":...,"msg":"fetched snapshot","size":"2.4 MB","took":"now"}
{"level":"info","ts":...,"msg":"saved","path":"/srv/backup/etcd-2026-04-14.db"}
```

Смотрим на `size` (снапшот в сотни байт — подозрительно) и на отсутствие ошибок.

### 4.2. Проверка снапшота

```bash
sudo ETCDCTL_API=3 etcdctl snapshot status /srv/backup/etcd-2026-04-14.db -w table
```

```text
+----------+----------+------------+------------+
|   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE |
+----------+----------+------------+------------+
| 9f3c1a02 |   184223 |       1421 |     2.4 MB |
+----------+----------+------------+------------+
```

`TOTAL KEYS` — сколько объектов API внутри. Ноль ключей = вы сохранили пустоту. `-w table` — табличный вывод.

### 4.3. Восстановление etcd из снапшота

```bash
# 1. Останавливаем статические поды control plane
sudo mv /etc/kubernetes/manifests /etc/kubernetes/manifests.off
sudo systemctl stop kubelet
# 2. Восстанавливаем данные в новый каталог
sudo ETCDCTL_API=3 etcdctl snapshot restore /srv/backup/etcd-2026-04-14.db \
  --data-dir=/var/lib/etcd-restored
# 3. Указываем новый каталог в манифесте и возвращаем поды
sudo sed -i 's#/var/lib/etcd#/var/lib/etcd-restored#' /etc/kubernetes/manifests.off/etcd.yaml
sudo mv /etc/kubernetes/manifests.off /etc/kubernetes/manifests
sudo systemctl start kubelet
```

`--data-dir` — куда распаковать; **не восстанавливайте поверх живого каталога**. В манифесте `etcd.yaml` надо поправить оба места: `hostPath` и аргумент `--data-dir` (иначе etcd стартует со старыми данными и вы решите, что восстановление не сработало).

### 4.4. Бэкап и восстановление GitLab

```bash
sudo gitlab-backup create
sudo ls -lh /var/opt/gitlab/backups/
sudo tar czf /srv/backup/gitlab-config-$(date +%F).tar.gz /etc/gitlab/gitlab.rb /etc/gitlab/gitlab-secrets.json
```

```text
1744612800_2026_04_14_17.2.0_gitlab_backup.tar
```

Имя = `<timestamp>_<дата>_<версия>_gitlab_backup.tar`. **Отдельно** копируйте `/etc/gitlab/gitlab-secrets.json` — без него после восстановления не расшифруются CI-переменные и runner-токены. Восстановление:

```bash
sudo gitlab-backup restore BACKUP=1744612800_2026_04_14_17.2.0
sudo gitlab-ctl reconfigure && sudo gitlab-ctl restart
sudo gitlab-rake gitlab:check SANITIZE=true
```

Восстанавливать нужно в **ту же версию** GitLab, иначе restore откажется.

### 4.5. Бэкап Harbor

```bash
docker exec harbor-db pg_dumpall -U postgres > /srv/backup/harbor-db-$(date +%F).sql
sudo rsync -aHAX --numeric-ids /data/registry/ /srv/backup/harbor-registry/
sudo tar czf /srv/backup/harbor-config-$(date +%F).tar.gz /data/*.yml /data/cert/
```

`pg_dumpall` — все базы разом (Harbor держит `registry`, `notary_signer` и др.). `rsync -aHAX` — архивный режим с сохранением ACL (`-A`), xattr (`-X`) и жёстких ссылок (`-H`), `--numeric-ids` — без пересчёта UID/GID (иначе образы станут «ничьими»). Точный официальный сценарий бэкапа Harbor смотрите в документации — детали различаются между версиями `[ПРОВЕРИТЬ]`.

### 4.6. Vault: снапшот Raft и распечатывание

```bash
vault operator raft snapshot save /srv/backup/vault-$(date +%F).snap
vault operator raft snapshot restore /srv/backup/vault-2026-04-14.snap
vault status
vault operator unseal    # три раза, ключами 1..3 из офлайн-сейфа
```

`vault status` показывает `Sealed true/false`: после рестарта Vault **всегда** возвращается запечатанным. Пока не распечатали — все секреты недоступны, и приложение падает. Ключи unseal хранятся не в Git и не на том же сервере.

### 4.7. Универсальный бэкап файлов через restic

```bash
restic -r sftp:backup@10.10.10.30:/srv/backup/restic init
restic -r sftp:backup@10.10.10.30:/srv/backup/restic backup /etc/gitlab /var/opt/gitlab/backups --tag git01
restic snapshots
restic check
restic restore latest --target /tmp/verify
```

`-r` — репозиторий (здесь SFTP на `UBU01`), `backup` — что копируем, `--tag` — метка для фильтрации, `check` — проверка целостности структуры репозитория (делать еженедельно, а не «когда-нибудь»), `restore --target` — восстановление в отдельный каталог, чтобы не затереть живое. Репозиторий шифруется ключом, который выводится один раз при `init` — потеряете пароль, потеряете бэкап.

### 4.8. Автоматизация: systemd-таймер

```ini
# /etc/systemd/system/backup-etcd.service
[Unit]
Description=etcd snapshot
[Service]
Type=oneshot
ExecStart=/usr/local/bin/backup-etcd.sh
```

```ini
# /etc/systemd/system/backup-etcd.timer
[Unit]
Description=etcd snapshot every 6 hours
[Timer]
OnCalendar=*-*-* 00/6:15:00
Persistent=true
[Install]
WantedBy=timers.target
```

`OnCalendar` — расписание в формате systemd (каждые 6 часов в :15). `Persistent=true` — догнать пропущенный запуск, если сервер был выключен. Проверка: `systemctl list-timers backup-etcd.timer`.

### 4.9. Полная пересборка кластера: `kubeadm`

```bash
sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --control-plane-endpoint=10.10.10.61
sudo kubeadm token create --print-join-command
# на k8s-w01 / k8s-w02:
sudo kubeadm join 10.10.10.61:6443 --token <token> --discovery-token-ca-cert-hash sha256:<hash>
```

`--print-join-command` выдаёт готовую строку подключения с живым токеном (токен по умолчанию живёт 24 часа). Пересборка «с нуля» — законный путь DR, если etcd-снапшот не годится (несовместимая версия, повреждён).

### 4.10. Bootstrap GitOps: ArgoCD из Git

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl apply -f argocd/root-app.yaml
argocd app list
argocd app sync root
```

`root-app.yaml` — «app of apps»: один Application, который тянет из Git все остальные. Это и есть восстановление «из кода»: кластер пустой, а через 10 минут в нём весь продакшен. В боевой среде фиксируйте версию ArgoCD тегом, а не `stable`.

### 4.11. Velero — бэкап объектов и томов Kubernetes

```bash
velero backup create full-2026-04-14 --include-namespaces shop,vault --snapshot-volumes
velero backup describe full-2026-04-14 --details
velero restore create --from-backup full-2026-04-14
```

Velero сохраняет объекты API и (через CSI/Kopia) содержимое PV. Удобно, когда etcd-снапшот недоступен или нужно восстановить один namespace. Требует object storage — на стенде это может быть MinIO `[ПРОВЕРИТЬ]` по версии Velero.

### 4.12. Контроль целостности бэкапа

```bash
sha256sum /srv/backup/etcd-2026-04-14.db > /srv/backup/etcd-2026-04-14.db.sha256
sha256sum -c /srv/backup/etcd-2026-04-14.db.sha256
```

```text
/srv/backup/etcd-2026-04-14.db: OK
```

Контрольная сумма фиксируется сразу после создания копии и проверяется перед восстановлением: так вы ловите битый файл до того, как начнёте им «спасать» компанию.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

Готовы: `UBU01` (10.10.10.30) — он же бэкап-хост, `WEB01` (10.10.20.10) и `MON01` (10.10.10.40), `GIT01` (10.10.10.50) с GitLab CE + runner, `HARBOR01` (10.10.10.55) с Harbor, кластер `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63) с ArgoCD, Vault (namespace `vault`) и приложением портала `portal.corp.vektor.lab` (namespace `shop`) — всё это вы делали в уроках 116–121. Windows-часть стенда не нужна.

Если Vault вы ещё не разворачивали — поставьте на `k8s-cp01` минимальный `vault-0` (Dev-режим не подойдёт: нужен Raft integrated storage, иначе нет `operator raft snapshot`).

### Снапшот

VMware Workstation → ВМ → `VM` → `Snapshot` → `Take Snapshot`, имя `before-devops-122` на всех затрагиваемых: **k8s-cp01, k8s-w01, k8s-w02, GIT01, HARBOR01, UBU01**. Удобно сделать это скриптом `vmrun`, если установлен VMware Workstation Pro. В `stand-log.md` добавьте строку «снапшот before-devops-122 снят на 6 ВМ».

### Шаги

1. **UBU01.** Готовим бэкап-хост: `sudo useradd -m -s /bin/bash backup`, `sudo mkdir -p /srv/backup/{restic,etcd,gitlab,harbor}`, `sudo chown -R backup:backup /srv/backup`. Ожидаемо: каталоги существуют, `df -h /srv` показывает запас места (не меньше 30 ГБ).
2. **Все узлы.** Настраиваем беспарольный доступ: `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_backup` и `ssh-copy-id -i ~/.ssh/id_backup.pub backup@10.10.10.30`. Ожидаемо: `ssh backup@10.10.10.30 hostname` → `UBU01`.
3. **UBU01.** Инициализируем репозиторий: `restic -r sftp:backup@10.10.10.30:/srv/backup/restic init`. **Сохраните пароль репозитория вне стенда** (в менеджере паролей). Ожидаемо: `created restic repository ...` и предупреждение о пароле.
4. **GIT01.** Создаём скрипт `/usr/local/bin/backup-gitlab.sh` (содержимое — раздел 4.4 + строка `restic ... backup /var/opt/gitlab/backups --tag git01`), `chmod +x`, запускаем вручную. Ожидаемо: новый `.tar` в `/var/opt/gitlab/backups` и `snapshot ... saved` в выводе restic.
5. **HARBOR01.** Снимаем дамп БД и реестр (раздел 4.5), затем `restic ... backup /srv/backup/harbor-registry /srv/backup/harbor-db-*.sql --tag harbor01`. Ожидаемо: в логе restic — `Files: 1204 new, ...`, размер гигабайты-сотни мегабайт.
6. **k8s-cp01.** Скрипт `/usr/local/bin/backup-etcd.sh` из раздела 4.1 + `etcdctl snapshot status`, ставим таймер из 4.8 (`systemctl enable --now backup-etcd.timer`). Ожидаемо: `systemctl list-timers` показывает следующий запуск.
7. **k8s-cp01.** Сохраняем состояние Vault: `kubectl -n vault exec vault-0 -- vault operator raft snapshot save /tmp/v.snap`, затем `kubectl -n vault cp vault-0:/tmp/v.snap /srv/backup/vault-$(date +%F).snap` и в restic. Ожидаемо: файл десятки килобайт, `vault operator raft snapshot inspect` не нужен — достаточно успешного exit code.
8. **UBU01.** Фиксируем в Git план DR: в репозитории `devops/infra` создаём `dr/dr-plan.md` с таблицей сервисов (GitLab — RTO 4 ч / RPO 24 ч; Harbor — RTO 2 ч / RPO 24 ч; k8s — RTO 1 ч / RPO 15 мин; Vault — RTO 30 мин / RPO 24 ч). Коммит и пуш. Ожидаемо: `git log --oneline -1` в GIT01/GitLab показывает ваш коммит.
9. **UBU01.** Тренировочная проверка целостности: `restic check`, `restic snapshots`, `sha256sum -c` для etcd-снапшота, `tar -tzf gitlab-config-*.tar.gz | head`. Ожидаемо: `no errors were found`, список снапшотов, `OK`.
10. **ОБЪЯВЛЯЕМ КАТАСТРОФУ.** Засекаем время в `stand-log.md`. На k8s-cp01: `sudo mv /etc/kubernetes/manifests /etc/kubernetes/manifests.off && sudo systemctl stop kubelet`. Ожидаемо: `kubectl get nodes` → `The connection to the server 10.10.10.61:6443 was refused`, `curl -I https://portal.corp.vektor.lab` → 503/таймаут.
11. **k8s-cp01.** Восстанавливаем etcd по разделу 4.3. Ожидаемо: через 1–3 минуты `kubectl get nodes` → все три узла `Ready`.
12. **k8s-cp01.** Проверяем, что состояние вернулось: `kubectl -n shop get pods`, `kubectl -n argocd get applications`. Ожидаемо: поды `shop` поднимаются, приложения в ArgoCD в статусе `Synced`.
13. **k8s-cp01.** Vault после рестарта запечатан: `kubectl -n vault exec vault-0 -- vault status` → `Sealed true`. Распечатываем тремя ключами (`vault operator unseal` ×3), затем `kubectl -n shop rollout restart deploy/shop-api`. Ожидаемо: `Sealed false`, поды `Running 1/1`.
14. **HARBOR01.** Тренировочное восстановление реестра в отдельный каталог: `docker compose down`, `docker compose up -d`, `docker exec -i harbor-db psql -U postgres < /srv/backup/harbor-db-*.sql`, вернуть `/data/registry` из restic в `/tmp/verify` и сравнить размер. Ожидаемо: `docker login harbor.corp.vektor.lab` проходит, `docker pull` учебного образа работает.
15. **UBU01.** Фиксируем время окончания, считаем RTO = конец − начало, пишем `dr/dr-report-2026-04-14.md`: что сломалось, что восстановили, сколько заняло, что делали не по плану. Коммит. Ожидаемо: отчёт в GitLab, RTO измерен в минутах.

### Проверка

- [ ] Снапшот `before-devops-122` есть на всех шести ВМ
- [ ] `restic snapshots` показывает минимум 4 снапшота с тегами `git01`, `harbor01` и т.д.
- [ ] `etcdctl snapshot status` показывает `TOTAL KEYS` больше нуля
- [ ] `gitlab-backup create` завершился без ошибок, `.tar` лежит в `/var/opt/gitlab/backups`
- [ ] `gitlab-secrets.json` скопирован отдельно от общего бэкапа
- [ ] После восстановления etcd все три узла `Ready`, поды `shop` в `Running`
- [ ] `vault status` → `Sealed false`
- [ ] `argocd app list` → все приложения `Synced / Healthy`
- [ ] `curl -I https://portal.corp.vektor.lab` → `HTTP/2 200`
- [ ] RTO посчитан и записан в `dr-report`, план закоммичен в Git

### Сломай и почини

**Пользователь:** ivanov (бухгалтерия) / ВМ: WIN11-CL01 (заявка в чате поддержки) / **Что написал:** «Портал опять 503, после ваших ночных работ вообще ничего не грузится, у нас отгрузки» / **Срочность:** высокий.

**Как воспроизвести поломку (намеренно):** на `k8s-cp01` выполните `kubectl -n vault delete pod vault-0` — Vault перезапустится запечатанным, ровно как после реального DR. Приложение не сможет получить секреты.

**Диагностика по порядку:**
1. `curl -I https://portal.corp.vektor.lab` → `503`. Значит, Ingress жив, а бэкенда нет — проблема не в сети и не в DNS.
2. `kubectl -n shop get pods` → `shop-api-... 0/1 CrashLoopBackOff`.
3. `kubectl -n shop describe pod shop-api-...` → секция `Events`, смотрим `Back-off restarting failed container` и exit code.
4. `kubectl -n shop logs deploy/shop-api --previous` → в логе `failed to read secret: connection refused` или `vault is sealed`. Вот и корень.
5. `kubectl -n vault get pods` → `vault-0 0/1 Running` (не Ready).
6. `kubectl -n vault exec vault-0 -- vault status` → `Sealed true`.
7. `kubectl -n vault exec -it vault-0 -- vault operator unseal` — три раза разными ключами → `Sealed false`.
8. `kubectl -n shop get externalsecret` → статус `SecretSynced`; затем `kubectl -n shop rollout restart deploy/shop-api` и `kubectl -n shop get pods -w` → `Running 1/1`.
9. `curl -I https://portal.corp.vektor.lab` → `200`. Записываем в `stand-log.md`: «Vault после DR требует ручного unseal; в прод — auto-unseal через KMS/HSM».

**Ответ пользователю:** «Иван, портал снова работает, проблема была на стороне сервиса секретов после восстановления, данные не пострадали. Проверьте, пожалуйста, что заказы открываются, и напишите, если что-то ещё ведёт себя странно». **Закрытие заявки:** категория «инфраструктура», привязка к инциденту DR, в решении — ссылка на `dr-report` и задача «настроить auto-unseal», чтобы такое не повторялось.

### Что изменилось на стенде

- `UBU01`: создан пользователь `backup`, каталог `/srv/backup` с подкаталогами, restic-репозиторий `/srv/backup/restic`
- `GIT01`: скрипт `/usr/local/bin/backup-gitlab.sh`, бэкапы в `/var/opt/gitlab/backups`, конфиг-архив в `/srv/backup`
- `HARBOR01`: скрипт бэкапа, дампы `harbor-db-*.sql` и копия `/data/registry`
- `k8s-cp01`: скрипт и таймер `backup-etcd.timer`, снапшоты etcd, `vault-0` пересоздавался, etcd восстановлен из снапшота
- Git (`devops/infra`): каталог `dr/` с `dr-plan.md` и `dr-report-2026-04-14.md`
- Новые учётки: `backup` на UBU01 (SSH-ключи с GIT01, HARBOR01, k8s-cp01)
- Метрики: измеренный RTO восстановления etcd — записан в отчёт

### Откат

VMware Workstation → нужная ВМ → `VM` → `Snapshot` → `Snapshot Manager` → выбрать `before-devops-122` → `Go To`. Откатывайте **все** затронутые ВМ (k8s-cp01, w01, w02, GIT01, HARBOR01, UBU01), иначе состояние разъедется: например, Git будет считать, что бэкап есть, а UBU01 — что нет. После отката обновите `stand-log.md` строкой «откат к before-devops-122, причина: …».

## 6. Ошибки новичков и безопасность

1. **Бэкап без проверки восстановления** → в нужный момент выясняется, что архив битый или пустой → регламент: раз в квартал обязательная тренировка restore, результат — в отчёте.
2. **Бэкапы на том же диске/хосте, что и данные** → один сбой или шифровальщик убивает и данные, и копии → минимум одна копия вне хоста (на стенде — `UBU01`, в бою — объектное хранилище в другом регионе).
3. **Забыли секреты** → восстановили GitLab без `gitlab-secrets.json` или Vault без unseal-ключей → приложение поднялось, но не работает → чек-лист DR обязан включать секреты отдельным пунктом.
4. **Неверный порядок восстановления** → поднимают приложения раньше реестра и секретов, получают `ImagePullBackOff` и панику → восстанавливаем строго по графу зависимостей (раздел 2).
5. **«Кластер — это и есть бэкап»** → уверенность, что PVC сам себя сохранит → PVC живут на дисках, при потере ноды данные уходят вместе с ней → Velero/CSI-снапшоты для stateful-сервисов.
6. **Нет RTO/RPO** → восстановление «пока не заработает» длится сутки, бизнес не понимает, когда ждать → метрики согласуются с заказчиком **до** аварии.
7. **Bus factor = 1** → процедуру знает один человек и он в отпуске → runbook в Git, ревью, ротация дежурных, тренировка на коллеге.
8. **Бэкап всего подряд без приоритетов** → 4 ТБ мусора, восстановление сутки, а критичные 20 ГБ потерялись в очереди → приоритизация: сначала то, что даёт RTO.

### Безопасность

- **Бэкапы шифровать** (restic делает это по умолчанию) и хранить пароль отдельно от репозитория. Бэкап без шифрования — это копия всей корпоративной базы в открытом виде.
- **Неизменяемость**: включайте object lock / WORM на хранилище бэкапов. Современный шифровальщик первым делом ищет смонтированные бэкап-шары.
- **Доступ**: у учётки `backup` должны быть права только на запись в свой каталог и чтение своих файлов, никакого `sudo` и никакого доступа к прод-данным сверх нужного. На стенде пароль `Lab#2026-vektor` — учебный, в бою так нельзя.
- **Секреты вне Git**: unseal-ключи Vault, root-токены, пароли restic — в сейфе/менеджере паролей, а не в репозитории `infra`. Проверьте историю перед пушем.
- **152-ФЗ и данные**: бэкапы с персональными данными — это те же персональные данные, с теми же требованиями к месту хранения. В РФ предпочтительны хранилища на территории страны; зарубежные облака — риск и юридическая проблема, не только техническая.
- **Учебные «упрощения»**: отключённый файрвол на бэкап-хосте или доступ по паролю вместо ключей — нормально на стенде и грубая ошибка в бою. Помечайте это в `stand-log.md` явно.

## 7. Вопросы на собеседовании

**1. Чем бэкап отличается от Disaster Recovery?**
Бэкап — это копия данных и сам процесс их создания; DR — это способность компании восстановить сервисы после катастрофы: план, порядок действий, роли, RTO/RPO и регулярные тренировки. Бэкап — один из инструментов DR, наравне с IaC и GitOps. Можно иметь идеальные бэкапы и не иметь DR, если никто не знает, в каком порядке их разворачивать.
*Что хочет услышать интервьюер:* понимание, что DR — процесс и организационная практика, а не «мы настроили `pg_dump` в cron».
*Красный флаг:* «бэкап и DR — одно и то же».

**2. Что такое RTO и RPO и как вы их определяете?**
RPO — допустимый объём потери данных, задаёт частоту бэкапов; RTO — допустимое время простоя, задаёт требования к скорости восстановления и к автоматизации. Определяются не инженером в вакууме, а вместе с бизнесом: для портала заказов потеря 15 минут данных может стоить дороже часа простоя, и наоборот. Дальше мы проверяем, что архитектура эти цифры обеспечивает, и подтверждаем замером на тренировке.
*Что хочет услышать интервьюер:* связь метрик с частотой бэкапов и с деньгами бизнеса, а не заученные определения.
*Красный флаг:* «RTO — это время бэкапа» или отсутствие связи с бизнесом.

**3. Как вы проверяете, что бэкапы рабочие?**
Три уровня: автоматическая проверка целостности (`restic check`, `sha256sum -c`, `etcdctl snapshot status`), алерты на «бэкап не создался» (это самый частый сбой), и регулярная тренировка восстановления на отдельной площадке с замером времени и записью в отчёт. Отдельно проверяю, что восстановленные данные консистентны — не просто «файл есть», а «приложение запустилось и отдаёт данные».
*Что хочет услышать интервьюер:* что вы понимаете разницу между «бэкап создан» и «данные восстановимы», и что у вас есть алертинг.
*Красный флаг:* «мы делаем бэкапы ежедневно» без единого слова о тестировании.

**4. Весь код в Git — значит бэкапить ничего не нужно, инфраструктура восстановится сама?**
Нет. Из Git восстановится декларативное состояние: манифесты, Helm-чарты, Terraform, Ansible. Не восстановятся данные (PVC, Postgres), образы в реестре, секреты и — самое неочевидное — сам Git-сервер: его репозитории, CI-переменные, токены runner и `gitlab-secrets.json`. Плюс состояние кластера в etcd, включая Secret'ы, которые в Git специально не кладут. Поэтому GitOps сокращает RTO, но не отменяет бэкапы состояния.
*Что хочет услышать интервьюер:* трезвость — что GitOps это ускоритель DR, а не замена ему, и что вы помните про «бэкап самого Git».
*Красный флаг:* «всё в Git, поэтому бэкапы не нужны».

**5. Полностью потерян кластер, GitLab и реестр образов. С чего начнёте и в каком порядке?**
Сначала фиксирую момент аварии и объявляю инцидент, открываю runbook и засекаю время. Порядок: базовая сеть/DNS/время → Vault из raft-снапшота и unseal-ключи из сейфа → GitLab (restore в ту же версию + `gitlab-secrets.json` + reconfigure) → Harbor (БД + `/data/registry`) → кластер (`kubeadm init` с нуля или etcd-снапшот, затем присоединение воркеров) → ArgoCD bootstrap из Git и sync приложений → Ingress и сертификаты (cert-manager перевыпустит, если сохранён account key issuer'а) → мониторинг → проверка сквозного сценария пользователем. После — постмортем без поиска виноватых и задачи на устранение найденных дыр.
*Что хочет услышать интервьюер:* порядок, основанный на зависимостях, и упоминание постмортема.
*Красный флаг:* начать с приложений или импровизировать без плана.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Operating etcd clusters for Kubernetes», подраздел «Disaster recovery» (снапшоты и restore). Поиск: `kubernetes etcd backup restore`.
- **kubernetes.io** — «kubeadm token» / «Creating a cluster with kubeadm». Поиск: `kubeadm join token create print-join-command`.
- **docs.gitlab.com** — «Back up and restore GitLab» (Omnibus). Поиск: `gitlab backup restore omnibus gitlab-secrets.json`.
- **goharbor.io** — раздел документации по администрированию, тема backup/restore. Поиск: `harbor backup restore registry database` `[ПРОВЕРИТЬ: точное название статьи и наличие официальной утилиты зависит от версии]`.
- **developer.hashicorp.com/vault** — «Integrated Storage» → «Snapshots». Поиск: `vault raft snapshot save restore unseal`.
- **velero.io** — «Backup and Restore» и «Resource filtering». Поиск: `velero backup create restore namespace`.
- **restic.readthedocs.io** — «Backup», «Restoring», «Checking integrity». Поиск: `restic check restore snapshots sftp backend`.
- **docs.aws.amazon.com** — Well-Architected Framework, Reliability Pillar, «Disaster recovery strategies» (backup & restore / pilot light / warm standby / multi-site). Поиск: `aws well-architected disaster recovery strategies RTO RPO`.
- **sre.google** — «Site Reliability Engineering», глава про managing incidents и postmortem culture. Поиск: `google sre book postmortem culture`.
- **docs.ansible.com / developer.hashicorp.com/terraform** — как хранить playbook и конфигурацию восстановления в Git рядом с инфраструктурным кодом. Поиск: `ansible playbook best practices directory layout`.
