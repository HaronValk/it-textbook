# Обновление и обслуживание кластера: версии, drain/cordon, бэкап etcd, DR

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Портал согласования платежей с обеда отвечает через раз, половина кнопок отдаёт 502. Раньше работало!»
> **Срочность:** Высокая

Иван не знает, что за порталом стоит кластер Kubernetes (k8s-cp01 + два воркера), и что сегодня по плану — обновление с v1.31 на v1.32. Ты уже выкатил `apt upgrade` на одном воркере и ушёл пить чай. Итог: одна нода в `NotReady`, поды висят в `Pending`, PDB не даёт выселить остатки, а бэкапа etcd нет — потому что «это же кластер, он сам себя лечит».

Тема «обслуживание кластера» в реальной работе встречается постоянно, а не раз в год:

- Kubernetes выпускает новый minor примерно каждые 4 месяца, и поддерживаются только 3 последних minor-версии [ПРОВЕРИТЬ актуальную политику поддержки]. Значит, обновляться нужно регулярно, а не «когда-нибудь».
- Managed-кластеры (Яндекс, VK Cloud, Cloud.ru — урок 47) обновляют control plane за тебя, но **воркеры, приложения, PDB и бэкапы — твоя зона ответственности**.
- Плановые работы (drain ноды, замена диска, расширение пула) — рутина DevOps.
- Disaster recovery (DR) — то, о чём вспоминают, когда уже поздно.

После этой главы ты должен уметь: объяснить version skew policy, обновить кластер на kubeadm без простоя, осознанно пользоваться `cordon`/`drain`, снимать и проверять снапшот etcd, восстановить кластер из бэкапа и написать минимальный DR-runbook.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Ключевая идея:** в Kubernetes нельзя обновить «всё сразу». Компоненты общаются по сети API-запросами, и у них есть правила совместимости — **version skew policy**.

Основные правила (для свежих версий, проверяй в доках под свою версию):

| Компонент | Правило |
|---|---|
| kube-apiserver | никогда не новее остальных компонентов control plane; может быть на 1 minor новее kube-controller-manager / kube-scheduler / cloud-controller-manager |
| kubelet | не новее kube-apiserver и не старее его более чем на 3 minor-версии [ПРОВЕРИТЬ: раньше было 2] |
| kube-proxy | то же, что kubelet |
| kubectl | ±1 minor от kube-apiserver |

Отсюда железный порядок: **сначала control plane, потом воркеры**, и **по одному minor за раз** (нельзя прыгнуть 1.29 → 1.32).

**Что происходит при `cordon` и `drain`:**

- `cordon` ставит ноде `spec.unschedulable=true` — новые поды сюда не планируются, старые живут.
- `drain` = `cordon` + выселение подов через **Eviction API** (`policy/v1 Eviction`), а не через `delete`. Eviction API уважает **PodDisruptionBudget (PDB)** — если PDB разрешает 0 прерываний, выселение вернёт 429 и `drain` будет ждать/упадёт по таймауту. Это не баг, это защита.
- DaemonSet-поды не выселяются (их место прибито к ноде) — нужен `--ignore-daemonsets`.
- Поды с `emptyDir` теряют данные при выселении — нужен `--delete-emptydir-data`.
- Поды без контроллера (голый Pod) не пересоздадутся нигде — нужен `--force`, и это осознанное решение.

```text
                 ПОРЯДОК ОБНОВЛЕНИЯ (kubeadm, 1 minor)

  ┌──────────────────────────────────────────────────────────┐
  │ 1. k8s-cp01: kubeadm upgrade plan → apply                │
  │    (control plane первым: apiserver, controller, sched,  │
  │     etcd, kube-proxy, CoreDNS)                           │
  └───────────────────────┬──────────────────────────────────┘
                          │ apiserver теперь N+1
        ┌─────────────────┴──────────────────┐
        ▼                                    ▼
  ┌───────────────┐                    ┌───────────────┐
  │ k8s-w01       │                    │ k8s-w02       │
  │ drain →       │  по одной ноде,    │ drain →       │
  │ kubeadm       │  не параллельно    │ kubeadm       │
  │ upgrade node →│                    │ upgrade node →│
  │ kubelet →     │                    │ kubelet →     │
  │ uncordon      │                    │ uncordon      │
  └───────────────┘                    └───────────────┘

  etcd: снапшот ДО любых работ, лежит ВНЕ кластера
```

**etcd.** Это key-value хранилище, где лежит **всё состояние кластера**: объекты, секреты, ServiceAccount-токены, конфиги. etcd работает по протоколу **Raft** (консенсус): запись считается успешной, когда её подтвердило большинство узлов — **кворум = (N/2)+1**. Для 3 узлов кворум 2, для 5 — 3. Если кворум потерян, кластер etcd только читает, а kube-apiserver фактически мёртв. У нас на стенде etcd **stacked** — один узел, работает как static pod на k8s-cp01, слушает 2379 (клиенты) и 2380 (peer).

Снапшот etcd (`etcdctl snapshot save`) — это консистентная копия всей БД на момент времени. **Что снапшот etcd НЕ содержит:** сами данные в PersistentVolume (это отдельные диски), содержимое внешних систем (БД 1С, GitLab на GIT01), образы в реестре HARBOR01. Поэтому «бэкап etcd» ≠ «DR-план».

**DR (Disaster Recovery)** — это не инструмент, а план: что бэкапим, куда, как часто, сколько времени на восстановление допускаем. Две метрики, которые спросят на любом собеседовании:

- **RTO** (Recovery Time Objective) — сколько времени кластер лежит, пока мы его поднимаем.
- **RPO** (Recovery Point Objective) — сколько данных (по времени) мы готовы потерять. RPO = 24 часа → бэкап раз в сутки.

**Что бэкапить в кластере:**

1. etcd snapshot (состояние API).
2. `/etc/kubernetes/pki` — сертификаты и ключи CA (без них etcd-снапшот бесполезен: секреты в нём зашифрованы ключом encryption config, а кластер needs CA).
3. `/etc/kubernetes/*.conf`, `kubeadm-config`, манифесты static pods.
4. Манифесты приложений — но они уже должны быть в Git (GIT01) через Helm/Kustomize (уроки 42–43). Это и есть «инфраструктура как код».
5. PersistentVolume — через CSI-снапшоты или Velero + restic/kopia.
6. Внешние зависимости: DNS, реестр образов, балансировщик.

**Velero** — стандартный инструмент бэкапа k8s-объектов и PV. Он ставится в кластер, ходит в S3-совместимое хранилище (MinIO в лаборатории) и умеет бэкапить по namespace, по label, по расписанию.

## 3. Аналогия из жизни

Представь девятиэтажку, в которой живут люди (поды), а **etcd — это архивная комната в подвале**, где лежат все документы на квартиры, договоры и печати. `cordon` — это табличка «новые жильцы не заселяются». `drain` — это расселение: ты обходишь квартиры и просишь жильцов переехать, но **управляющая компания (PDB) запрещает выселять больше двух квартир одновременно** — иначе дом формально пуст, а сервис не работает. Бэкап etcd — это вывезти всю архивную комнату в другое здание. А DR — это инструкция на стене: «при пожаре звонить сюда, архив лежит там, ключи у такого-то, собраться нужно за 4 часа».

Где аналогия ломается: жильцы переезжают сами и забирают мебель, а поды данные не забирают — `emptyDir` умирает вместе с подом, а состояние в PVC остаётся на диске и требует отдельного бэкапа.

## 4. Практика и команды

### 4.1. Инвентаризация: что и какой версии

```bash
kubectl get nodes -o wide
kubectl version --output=yaml
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.nodeInfo.kubeletVersion}{"\t"}{.status.nodeInfo.osImage}{"\n"}{end}'
```

```text
NAME       STATUS   ROLES           AGE   VERSION   INTERNAL-IP
k8s-cp01   Ready    control-plane   41d   v1.31.4   10.10.10.61
k8s-w01    Ready    <none>          41d   v1.31.4   10.10.10.62
k8s-w02    Ready    <none>          41d   v1.31.4   10.10.10.63
```

Смотрим: версии нод должны совпадать или отличаться не больше разрешённого skew. `kubectl version --output=yaml` покажет `clientVersion` и `serverVersion` — если разница больше одного minor, ставь kubectl из репозитория pkgs.k8s.io.

### 4.2. План обновления

```bash
sudo kubeadm upgrade plan
```

```text
[upgrade/versions] Cluster version: v1.31.4
[upgrade/versions] kubeadm version: v1.32.0
Components that must be upgraded manually after you have upgraded the control plane:
COMPONENT   CURRENT      TARGET
kubelet     3 x v1.31.4  v1.32.0

Upgrade to the latest stable version:
COMPONENT                 CURRENT    TARGET
kube-apiserver            v1.31.4    v1.32.0
kube-controller-manager   v1.31.4    v1.32.0
kube-scheduler            v1.31.4    v1.32.0
kube-proxy                v1.31.4    v1.32.0
CoreDNS                   v1.11.1    v1.12.0
etcd                      3.5.15-0   3.5.16-0
```

Вывод примерный, цифры зависят от версии — `[ПРОВЕРИТЬ]` под свой релиз. Главное: команда **ничего не меняет**, она показывает путь и предупреждения.

### 4.3. Пакеты: hold / unhold

```bash
sudo apt-mark showhold
sudo apt-mark unhold kubeadm kubelet kubectl
sudo apt-get update
sudo apt-cache madison kubeadm | head
sudo apt-get install -y kubeadm='1.32.0-1.1'
sudo apt-mark hold kubeadm kubelet kubectl
```

`apt-mark hold` запрещает apt обновлять пакет — иначе `apt upgrade` однажды притащит kubelet новее apiserver и сломает ноду. Суффикс `-1.1` — формат пакетов из репозитория pkgs.k8s.io `[ПРОВЕРИТЬ актуальный суффикс через apt-cache madison]`.

### 4.4. Обновление control plane

```bash
sudo kubeadm upgrade apply v1.32.0
```

```text
[upgrade/successful] SUCCESS! Your cluster was upgraded to "v1.32.0". Enjoy!
[upgrade/kubelet-config] Kubelet configuration is now at v1beta1
```

`apply` обновляет static pods (apiserver, controller-manager, scheduler, etcd), kube-proxy, CoreDNS, продлевает сертификаты control plane (если не отключено) и обновляет kubelet-config. Флаг `-y` убирает интерактивное подтверждение — в скриптах нормально, руками лучше видеть вопросы.

### 4.5. Обновление воркера: `kubeadm upgrade node`

```bash
# на k8s-w01
sudo kubeadm upgrade node
```

На воркере и на «втором и последующих» control plane используется `kubeadm upgrade node` (не `apply`!). Она обновляет kubelet-config и kube-proxy для этой ноды. Полный цикл на воркере: `drain` → пакет `kubeadm` → `kubeadm upgrade node` → пакеты `kubelet`/`kubectl` → `systemctl daemon-reload && systemctl restart kubelet` → `uncordon`.

### 4.6. `cordon` и `drain`

```bash
kubectl cordon k8s-w01
kubectl drain k8s-w01 --ignore-daemonsets --delete-emptydir-data --timeout=180s
kubectl uncordon k8s-w01
```

```text
node/k8s-w01 cordoned
evicting pod prod/web-7c9f8b6d4-2xk9p
evicting pod kube-system/kube-proxy-x7lqd  (skipped: daemonset)
node/k8s-w01 drained
```

Ключи: `--ignore-daemonsets` — не пытаться выселить DaemonSet; `--delete-emptydir-data` — разрешить удалять поды с emptyDir (данные пропадут); `--timeout` — не висеть вечно; `--dry-run=client` — посмотреть, кого выселим, ничего не делая; `--force` — удалить поды без контроллера (опасно). `uncordon` возвращает ноду в строй.

### 4.7. Кто где живёт и что блокирует выселение

```bash
kubectl get pods -A -o wide --field-selector spec.nodeName=k8s-w01
kubectl get pdb -A
```

```text
NAMESPACE   NAME      MIN AVAILABLE   MAX UNAVAILABLE   ALLOWED DISRUPTIONS   AGE
prod        web-pdb   2               N/A               1                     12d
```

Если `ALLOWED DISRUPTIONS = 0` — `drain` не сможет выселить ни один под этого приложения. Лечится не через `--force`, а через ожидание (под перезапустился) или временное изменение PDB/добавление реплик.

### 4.8. Снапшот etcd и проверка

```bash
sudo mkdir -p /var/backups/etcd
sudo ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /var/backups/etcd/etcd-$(date +%F-%H%M).db

sudo ETCDCTL_API=3 etcdctl --write-out=table \
  snapshot status /var/backups/etcd/etcd-2026-05-01-1015.db
```

```text
+----------+----------+------------+------------+
|   HASH   | REVISION | TOTAL KEYS | TOTAL SIZE |
+----------+----------+------------+------------+
| 7a3c1f0e |    48213 |       1120 |     4.1 MB |
+----------+----------+------------+------------+
```

`snapshot status` — **обязательный шаг**: снапшот без проверки не считается бэкапом. Если команда падает — файл битый. Сертификаты нужны, потому что клиентский порт etcd закрыт TLS (mTLS).

### 4.9. Здоровье etcd

```bash
sudo ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  endpoint health --cluster

sudo ... endpoint status --cluster -w table
```

```text
+------------------------+------------------+---------+---------+-----------+------------+
|        ENDPOINT        |        ID        | VERSION | DB SIZE | IS LEADER | IS LEARNER |
+------------------------+------------------+---------+---------+-----------+------------+
| https://127.0.0.1:2379 | 8e9e05c52164694d |  3.5.15 |  4.1 MB |      true |      false |
+------------------------+------------------+---------+---------+-----------+------------+
```

Смотреть: `DB SIZE` (рост → деградация, нужна дефрагментация), `IS LEADER`, `VERSION`.

### 4.10. Сертификаты и join-токен

```bash
sudo kubeadm certs check-expiration
sudo kubeadm certs renew all
kubeadm token create --print-join-command
```

Сертификаты kubeadm живут **1 год** (CA — 10 лет). `kubeadm upgrade apply` продлевает их автоматически, но проверить нужно уметь. Join-команда нужна, если восстанавливаешь ноду через `kubeadm reset` + повторный ввод в кластер.

### 4.11. Бэкап объектов и PV: Velero

```bash
velero backup create daily-2026-05-01 --include-namespaces prod --snapshot-volumes
velero backup describe daily-2026-05-01 --details
velero get backups
velero restore create --from-backup daily-2026-05-01
```

Velero не заменяет снапшот etcd: он восстанавливает объекты и PV, но не сам кластер и не RBAC-ядро. Используются вместе.

### 4.12. Восстановление из снапшота etcd (kubeadm, sketch)

Полная процедура описана в доке, порядок такой: остановить kube-apiserver (убрать манифест из `/etc/kubernetes/manifests`), остановить etcd, восстановить данные:

```bash
sudo etcdutl snapshot restore /var/backups/etcd/etcd-2026-05-01-1015.db \
  --data-dir=/var/lib/etcd-restore
```

В etcd 3.5+ утилита восстановления называется `etcdutl` (`etcdctl snapshot restore` устарел) `[ПРОВЕРИТЬ под свою версию etcd]`. Затем `--data-dir` в манифесте etcd (`/etc/kubernetes/manifests/etcd.yaml`) и hostPath тома указывают на новый каталог, возвращают манифесты apiserver, ждут поднятия. На стенде из одного узла это занимает минуты.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **k8s-cp01** (10.10.10.61) — control plane, развёрнут через kubeadm, etcd stacked. Требуется урок 34.
- **k8s-w01** (10.10.10.62), **k8s-w02** (10.10.10.63) — воркеры, `Ready`.
- **UBU01** (10.10.10.30) — внешнее хранилище бэкапов (не в кластере!). SSH работает.
- **GIT01** (10.10.10.50) — GitLab, репозиторий `vektor/k8s-ops`.
- Приложение `web` (3 реплики) в namespace `prod` + PDB `web-pdb` (`minAvailable: 2`) — из урока 40. Если PDB нет, создай: без него лабораторная теряет смысл.
- На всех нодах пакеты kubeadm/kubelet/kubectl стоят из pkgs.k8s.io и **не** на `hold` (или проверь `apt-mark showhold`).

Если кластера нет — сначала урок 34. Если нет PDB — урок 40 или минимальный манифест по образцу ниже.

### Снапшот

В VMware Workstation на каждой ВМ (`k8s-cp01`, `k8s-w01`, `k8s-w02`) — **VM → Snapshot → Take Snapshot**, имя `before-devops-046`. Через CLI с хоста:

```bash
vmrun -T ws snapshot "/home/user/vmware/k8s-cp01/k8s-cp01.vmx" before-devops-046
vmrun -T ws snapshot "/home/user/vmware/k8s-w01/k8s-w01.vmx"   before-devops-046
vmrun -T ws snapshot "/home/user/vmware/k8s-w02/k8s-w02.vmx"   before-devops-046
```

Проверить: `vmrun -T ws listSnapshots "/home/user/vmware/k8s-cp01/k8s-cp01.vmx"`.

### Шаги

1. **k8s-cp01.** Инвентаризация: `kubectl get nodes -o wide`, `kubectl version --output=yaml`, `kubectl get pods -A -o wide`. Ожидаемо: 3 ноды `Ready`, версии одинаковые, все поды `Running`. Запиши версии в `stand-log.md`.
2. **k8s-cp01.** Проверь PDB и приложение: `kubectl -n prod get deploy,pods,pdb`. Ожидаемо: 3/3 пода `Running`, `ALLOWED DISRUPTIONS = 1`.
3. **k8s-cp01.** Снапшот etcd по команде из 4.8 в `/var/backups/etcd/`. Ожидаемо: файл создан, `snapshot status` показывает `TOTAL KEYS > 0`.
4. **k8s-cp01.** Скопируй бэкап и PKI на UBU01: `sudo tar czf /var/backups/etcd/pki-$(date +%F).tgz /etc/kubernetes/pki /etc/kubernetes/*.conf` и `scp /var/backups/etcd/* adm.student@10.10.10.30:/srv/backup/k8s/`. Ожидаемо: файлы на UBU01, `ls -lh /srv/backup/k8s/` показывает размер. (Если на UBU01 другая учётка — подставь свою.)
5. **k8s-cp01.** `sudo kubeadm upgrade plan`. Ожидаемо: отчёт с текущей и целевой версией, предупреждений про «version skew» и «unsupported» нет.
6. **k8s-cp01.** Обнови kubeadm: `sudo apt-mark unhold kubeadm && sudo apt-get update && sudo apt-get install -y kubeadm='1.32.0-1.1'`, затем `sudo kubeadm upgrade plan` ещё раз (теперь `kubeadm version` = целевой). Ожидаемо: план без ошибок «kubeadm version too old».
7. **k8s-cp01.** `sudo kubeadm upgrade apply v1.32.0` (отвечай `y`). Ожидаемо: `SUCCESS! Your cluster was upgraded`, поды kube-system перезапустились, `kubectl get nodes` показывает cp01 уже с новой версией kubelet? Нет — kubelet ещё старый; обнови его: `sudo apt-mark unhold kubelet kubectl && sudo apt-get install -y kubelet='1.32.0-1.1' kubectl='1.32.0-1.1' && sudo apt-mark hold kubeadm kubelet kubectl && sudo systemctl daemon-reload && sudo systemctl restart kubelet`.
8. **k8s-w01.** Выселение: `kubectl drain k8s-w01 --ignore-daemonsets --delete-emptydir-data --timeout=180s`. Ожидаемо: `node/k8s-w01 drained`, на cp01 `kubectl -n prod get pods -o wide` показывает 3 пода на w02 (или 2 + 1 в `Pending`, если ресурсов мало — это сигнал, что реплик больше, чем мощности).
9. **k8s-w01.** `sudo apt-mark unhold kubeadm && sudo apt-get install -y kubeadm='1.32.0-1.1' && sudo kubeadm upgrade node && sudo apt-mark unhold kubelet kubectl && sudo apt-get install -y kubelet='1.32.0-1.1' kubectl='1.32.0-1.1' && sudo apt-mark hold kubeadm kubelet kubectl && sudo systemctl daemon-reload && sudo systemctl restart kubelet`. Ожидаемо: `kubeadm upgrade node` завершился без ошибок.
10. **k8s-cp01.** `kubectl uncordon k8s-w01 && kubectl get nodes -o wide`. Ожидаемо: `k8s-w01 Ready` с новой версией, поды постепенно распределяются обратно.
11. **Повтори шаги 8–10 для k8s-w02.** Ожидаемо: обе ноды `Ready` на целевой версии.
12. **k8s-cp01.** Итоговая проверка: `kubectl get nodes`, `kubectl -n prod get pods -o wide`, `kubectl -n kube-system get pods`, `sudo kubeadm certs check-expiration`. Ожидаемо: все `Running`, сертификаты валидны минимум год.
13. **GIT01 / k8s-cp01.** Положи скрипт бэкапа etcd в репозиторий `vektor/k8s-ops` (`scripts/backup-etcd.sh`), закоммить и запушь. Ожидаемо: коммит в GitLab, скрипт версионирован.

Скрипт для шага 13 (запускать по cron на cp01):

```bash
#!/usr/bin/env bash
set -euo pipefail
DEST=/var/backups/etcd
TS=$(date +%F-%H%M)
export ETCDCTL_API=3
etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save "$DEST/etcd-$TS.db"
etcdctl snapshot status --write-out=table "$DEST/etcd-$TS.db"
find "$DEST" -name 'etcd-*.db' -mtime +7 -delete
```

### Проверка

- [ ] `kubectl get nodes` — все три ноды `Ready`, версии совпадают.
- [ ] `kubectl -n prod get pods` — 3/3 `Running`, `RESTARTS` не растут бесконечно.
- [ ] `kubectl get pdb -A` — PDB существует и `ALLOWED DISRUPTIONS` соответствует ожиданию.
- [ ] `kubectl get nodes -o jsonpath=...` — нет `SchedulingDisabled` ни на одной ноде.
- [ ] `/var/backups/etcd/etcd-*.db` существует, `snapshot status` показывает ключи.
- [ ] Копия бэкапа и `pki-*.tgz` лежат на UBU01 в `/srv/backup/k8s/`.
- [ ] `sudo kubeadm certs check-expiration` — без `EXPIRED`.
- [ ] Скрипт `backup-etcd.sh` в GitLab, `stand-log.md` обновлён.

### Сломай и почини

> **Пользователь:** ivanov (через дежурного)
> **ВМ:** WIN11-CL01, портал «Вектор»
> **Что написал:** «Портал вообще не открывается, в браузере 503. Вы же что-то обновляли?»
> **Срочность:** Критическая

**Воспроизводим поломку** (на стенде, после успешного обновления):

1. На **k8s-w01**: `sudo systemctl stop kubelet && sudo systemctl disable kubelet`.
2. На **k8s-cp01**: `kubectl cordon k8s-w02` (как будто «забыли вернуть в строй»).
3. Подождать 2–3 минуты: k8s-w01 уйдёт в `NotReady`, планировать станет некуда, часть подов уйдёт в `Pending`.

**Диагностика по порядку (не перескакивай):**

```bash
# 1. Общая картина
kubectl get nodes
kubectl get pods -A -o wide | grep -v -E 'Running|Completed'

# 2. Почему под не едет
kubectl -n prod describe pod <pending-pod> | tail -20
# ожидаемо в Events: "0/3 nodes are available: 3 node(s) had untolerated taint..."

# 3. Не блокирует ли PDB
kubectl get pdb -A
# ALLOWED DISRUPTIONS 0 при 2 доступных подах из 3 — PDB работает как задумано

# 4. Живой ли kubelet на «плохой» ноде
# на k8s-w01:
systemctl status kubelet --no-pager
journalctl -u kubelet -n 50 --no-pager

# 5. Что говорит сама нода
kubectl describe node k8s-w01 | tail -15
```

**Решение:** на k8s-w01 — `sudo systemctl enable --now kubelet`, дождаться `Ready` (`kubectl get nodes -w`), затем `kubectl uncordon k8s-w02`, затем `kubectl -n prod rollout status deploy/web`. Поды сами переедут, PDB разблокируется по мере роста доступных реплик.

**Ответ пользователю:**

> Иван, добрый день! Проблема на стороне кластера портала — после планового обновления один из серверов не вернулся в работу. Мы его восстановили, портал работает штатно, проверил вход и раздел платежей. Приносим извинения за неудобства, если что-то ещё не открывается — напишите, посмотрим.

**Закрытие заявки:** категория «Инфраструктура / Kubernetes», в решении указать root cause («нода не вернулась после maintenance, kubelet disabled»), добавить в runbook пункт «после обновления проверить `kubectl get nodes` и отсутствие `SchedulingDisabled`».

### Что изменилось на стенде

- **k8s-cp01, k8s-w01, k8s-w02** обновлены до целевой версии (запиши конкретную, например v1.32.x); пакеты `kubeadm/kubelet/kubectl` на `hold`.
- На **k8s-cp01** появился каталог `/var/backups/etcd/` со снапшотами.
- На **UBU01** — каталог `/srv/backup/k8s/` с копиями etcd-снапшотов и `pki-*.tgz`.
- В **GIT01** репозиторий `vektor/k8s-ops` содержит `scripts/backup-etcd.sh`.
- В namespace `prod` — Deployment `web` (3 реплики) и PDB `web-pdb` (`minAvailable: 2`).
- В `stand-log.md` запись: дата, версии до/после, путь бэкапов, результат проверки сертификатов.

### Откат

GUI: выбери ВМ → **VM → Snapshot → Snapshot Manager** → снапшот `before-devops-046` → **Go To**. CLI:

```bash
vmrun -T ws revertToSnapshot "/home/user/vmware/k8s-w01/k8s-w01.vmx" before-devops-046
vmrun -T ws start "/home/user/vmware/k8s-w01/k8s-w01.vmx"
```

Откатывай все три ВМ вместе: если откатить только воркер, он вернётся со старой версией kubelet, а apiserver останется новым — это допустимый skew, но лучше держать стенд консистентным.

## 6. Ошибки новичков и безопасность

1. **Обновлять все ноды параллельно.** → Двойной простой, потеря кворума PDB, апдейт «наполовину». → Всегда по одной ноде, `drain` → обновление → `uncordon` → дождаться `Ready`.
2. **`apt upgrade` без `hold`.** → kubelet обновился до версии новее apiserver → нода `NotReady`, поды не стартуют. → `apt-mark hold kubeadm kubelet kubectl`, обновление только явными версиями.
3. **`drain --force --delete-emptydir-data` «чтобы прошло».** → Убивает поды без контроллера и данные в emptyDir; PDB обходится, сервис падает. → Сначала `kubectl get pdb -A`, `describe pod`, разбираться, кто держит eviction.
4. **Забыть `uncordon`.** → Нода навсегда `SchedulingDisabled`, кластер теряет ёмкость, поды в `Pending`. → Проверка `kubectl get nodes` в чек-листе после каждой ноды.
5. **Прыжок через minor (1.29 → 1.32).** → `kubeadm upgrade apply` откажется, а если обходить — несовместимые API и миграции etcd. → Только по одному minor, `kubeadm upgrade plan` в помощь.
6. **«Бэкап» etcd без проверки.** → Файл есть, но битый или снят не с того endpoint — при аварии восстанавливать нечего. → Всегда `snapshot status`, раз в квартал — **тестовое восстановление** на отдельной ВМ.
7. **Хранить снапшот etcd на той же ВМ.** → Умер диск cp01 — умер и бэкап. → Правило 3-2-1: три копии, два носителя, одна вне площадки (в лабе — UBU01).
8. **Считать бэкап etcd полным DR-планом.** → Восстановил API, а PV пустые, образы в HARBOR01 не те, секреты шифровались другим ключом. → Бэкапить PKI + encryption config + PV + IaC из Git, писать runbook с RTO/RPO.

**Безопасность.** Снапшот etcd содержит **все Secrets в открытом или слабо зашифрованном виде** — это фактически мастер-пароль от кластера. В боевой среде: шифрование бэкапов (age/GPG/Vault), отдельное хранилище с ограниченным доступом, ротация ключей encryption-at-rest, аудит доступа к бэкапам. `kubectl drain --force` и `--ignore-daemonsets` в продакшене — только с обоснованием и в окно работ. `kubeadm certs renew` в неправильном порядке может уронить apiserver — на проде это делают по runbook. И никогда не храни `pki`-архив в том же Git-репозитории, что и манифесты.

## 7. Вопросы на собеседовании (5 вопросов с ответами)

**1. Как обновить кластер kubeadm с 1.31 до 1.32? Опиши порядок и почему он такой.**

Ответ: снапшот etcd и PKI, `kubeadm upgrade plan` на control plane, обновление пакета kubeadm, `kubeadm upgrade apply v1.32.0` (обновляет static pods, CoreDNS, kube-proxy, kubelet-config, сертификаты), затем kubelet/kubectl на cp. Дальше по одному воркеру: `drain` → kubeadm → `kubeadm upgrade node` → kubelet/kubectl → `restart kubelet` → `uncordon`. Порядок такой из-за version skew: apiserver может быть на 1 minor новее controller-manager/scheduler, kubelet — до 3 minor старше apiserver, но не новее. Только по одному minor за раз.
*Что хочет услышать:* что кандидат называет снапшот, план, порядок control plane → workers и умеет объяснить skew, а не просто пересказать команды.
*Красный флаг:* «делаю `apt upgrade` на всех нодах и перезагружаю», «сразу 1.29 → 1.32».

**2. Чем `cordon` отличается от `drain` и зачем флаги `--ignore-daemonsets` и `--delete-emptydir-data`?**

Ответ: `cordon` только помечает ноду `unschedulable`; `drain` дополнительно выселяет поды через Eviction API с учётом PDB и grace period. DaemonSet-поды привязаны к ноде и не могут быть выселены — без `--ignore-daemonsets` drain упадёт. Поды с emptyDir потеряют локальные данные при удалении, поэтому их удаление требует явного `--delete-emptydir-data`. `--force` нужен для подов без контроллера — и это осознанный риск.
*Что хочет услышать:* упоминание Eviction API и PDB — это отличает понимание от заучивания.
*Красный флаг:* «drain просто удаляет поды», «PDB не влияет на drain».

**3. Как сделать и проверить бэкап etcd? Что ещё нужно, кроме него?**

Ответ: `etcdctl snapshot save` с `--endpoints`, `--cacert`, `--cert`, `--key`; проверка `snapshot status` (revision, keys, size). Хранить вне кластера, шифровать. Кроме etcd: `/etc/kubernetes/pki`, конфиги, манифесты static pods, IaC из Git, PV через CSI-снапшоты/Velero, внешние зависимости (DNS, реестр). Восстановление — `etcdutl snapshot restore --data-dir=...` с правкой манифеста etcd и hostPath.
*Что хочет услышать:* что бэкап без проверки восстановлением — не бэкап, и что etcd ≠ всё состояние системы.
*Красный флаг:* «etcd бэкапится сам, он же реплицируется», «бэкап кладу рядом с etcd-данными».

**4. Что такое RTO и RPO и как ты построишь DR-план для кластера?**

Ответ: RTO — целевое время восстановления, RPO — допустимая потеря данных по времени. План: инвентаризация компонентов и их RTO/RPO, расписание бэкапов (etcd по cron, Velero по namespace), 3-2-1, шифрование, документированный runbook с шагами восстановления, ответственные, регулярные учения (game day) с замером фактического времени. Отдельно — сценарии: потеря ноды, потеря control plane, потеря всего кластера, потеря площадки.
*Что хочет услышать:* цифры, регулярные учения, конкретика про «восстановление проверено, а не нарисовано».
*Красный флаг:* «бэкапы есть, план в голове», «RTO не считаем».

**5. Можно ли обновить воркеры раньше control plane? Что произойдёт?**

Ответ: нет. Kubelet не может быть новее apiserver — kubelet просто не сможет зарегистрироваться/работать с более старым API, нода уйдёт в `NotReady`. Обратное допустимо: ноды могут отставать от apiserver в пределах 3 minor. Поэтому control plane обновляется первым, и на managed-кластерах (Яндекс, VK Cloud) сначала обновляют control plane, потом вручную воркеры.
*Что хочет услышать:* понимание направления skew и практический вывод.
*Красный флаг:* «без разницы, главное версии совпали в итоге».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Upgrading kubeadm clusters» (и подстраницы «Upgrading kubeadm clusters from v1.31 to v1.32»). Поиск: `kubernetes upgrading kubeadm clusters`.
- **kubernetes.io** — «Version Skew Policy» (страница в Concepts / Cluster Administration). Поиск: `kubernetes version skew policy`. `[ПРОВЕРИТЬ актуальные значения для твоей версии]`
- **kubernetes.io** — «Safely Drain a Node». Поиск: `kubernetes safely drain a node`.
- **kubernetes.io** — «Operating etcd clusters for Kubernetes» и «Backing up an etcd cluster». Поиск: `kubernetes operating etcd back up etcd cluster`.
- **kubernetes.io** — «Disruptions» и «Specifying a Disruption Budget for your Application». Поиск: `kubernetes disruptions poddisruptionbudget`.
- **kubernetes.io** — «kubeadm certs». Поиск: `kubeadm certs check-expiration renew`.
- **etcd.io** — «Disaster recovery» и «etcdutl». Поиск: `etcd disaster recovery snapshot restore etcdutl`.
- **velero.io** — «Documentation» → Backup / Restore / Schedule. Поиск: `velero backup restore documentation`.
- **kubernetes.io** — «Releases» / поддержка версий. Поиск: `kubernetes releases supported versions`.
- **vmware.com** — документация VMware Workstation, утилита `vmrun` (команды `snapshot`, `revertToSnapshot`). Поиск: `vmrun command line reference snapshot`.
