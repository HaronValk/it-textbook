# Disaster Recovery и бэкапы: Velero, Restic, Kopia, регулярные учения по восстановлению

## 1. Введение (зачем это нужно в реальной работе)

09:12, понедельник. Алексей Сидоров со склада открывает портал и пишет в поддержку:

> «Склад / портал заказов на k8s / Вчера всё работало, а сегодня список заказов пустой и на каждой странице 500. Заказы за последнюю неделю пропали, у нас отгрузки стоят!»

Дальше выясняется: в пятницу вечером коллега «почистил» namespace `shop` перед релизом — `kubectl delete ns shop` показался быстрым способом убить зависший под. Namespace пересоздался скриптом деплоя за две минуты, поды поднялись зелёными, а данные в `PersistentVolume` пришли новые и пустые. Ни бэкапов, ни проверенного сценария восстановления нет.

Это самая дорогая и самая частая авария в Kubernetes: **не падение кластера, а потеря данных внутри живого кластера**. Кластер сам по себе — расходник: его можно пересобрать за час. Данные — нет.

После этой главы читатель должен уметь: считать RPO/RTO для сервиса и подбирать под них инструменты; бэкапить Kubernetes-манифесты и данные томов через Velero; поднимать независимые репозитории Restic и Kopia; делать снапшот etcd; и — главное — **регулярно проводить учения по восстановлению**, потому что бэкап, который никогда не восстанавливали, равен отсутствию бэкапа.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Сначала два термина, вокруг которых всё крутится:

- **RPO (Recovery Point Objective)** — сколько данных мы готовы потерять. RPO 24 часа = «при аварии откатываемся на суточный бэкап».
- **RTO (Recovery Time Objective)** — за сколько обязаны подняться. RTO 1 час = «через час сервис снова работает». RTO измеряется только учениями, а не обещаниями.

Классическая формула стратегии — **3-2-1 (расширенно 3-2-1-1-0)**: 3 копии данных, 2 разных носителя, 1 копия вне площадки, 1 копия неизменяемая/офлайн, 0 ошибок при проверке восстановления.

### Что вообще бэкапить в Kubernetes

1. **etcd** — состояние всего кластера (объекты API). Единственная копия «правды» о кластере.
2. **Объекты API** (манифесты: Deployment, Service, ConfigMap, Secret, CRD, RBAC) — Velero забирает их через API-сервер.
3. **Данные PersistentVolume** — то, что лежит в файлах и БД.
4. **Внешние по отношению к кластеру вещи** — DNS-зоны, сертификаты, реестр образов HARBOR01, Git-репозитории GIT01, секреты.

### Как устроен Velero

Velero — это оператор DR для Kubernetes с двумя частями:

- **velero server** — Deployment в namespace `velero`, общается с API-сервером (TCP 6443) и с объектным хранилищем по S3 API. Хранит бэкапы как набор JSON/YAML-описаний в бакете.
- **node-agent** — DaemonSet на каждой ноде, читает файловые системы подов и складывает их данные в хранилище в формате **Kopia** (раньше — Restic). Это называется **fs-backup**.

Что Velero **не** делает: не снимает консистентный дамп БД. Он копирует файлы работающего пода, а PostgreSQL/MySQL в этот момент может писать. Для БД нужен либо `pg_dump`/`mysqldump` в CronJob, либо хуки бэкапа (`pre`/`post` annotations, `fsfreeze`), либо снапшот диска средствами CSI.

Ключевые CRD: `Backup`, `Restore`, `Schedule`, `BackupStorageLocation` (куда лить), `VolumeSnapshotLocation`.

```text
        ┌──────────────────────── k8s-cp01 / k8s-w01 / k8s-w02 ────────────────────────┐
        │                                                                              │
        │  namespace shop                    namespace velero                          │
        │  ┌───────────────┐   k8s API      ┌──────────────────┐                       │
        │  │ deploy shop   │◄───6443───────►│ velero server    │                       │
        │  │ PVC 5Gi       │                │ (Deployment)     │                       │
        │  └───────┬───────┘                └───────┬──────────┘                       │
        │          │ node-agent (DaemonSet)         │ S3 API :30900                     │
        │          ▼  читает файлы пода             ▼                                   │
        │  ┌───────────────────┐            ┌───────────────────────┐                   │
        │  │ node-agent (kopia)│───────────►│ MinIO backup-system   │                   │
        │  └───────────────────┘   S3 :9000  │ (лабораторный таргет) │                   │
        │                                   └───────────────────────┘                   │
        └──────────────────────────────────────────────────────────────────────────────┘
                          ▲                                            ▲
                          │ etcd snapshot (:2379)                      │ restic / kopia CLI
                    /var/backups на k8s-cp01                     UBU01 10.10.10.30
```

### Restic и Kopia отдельно

Это самостоятельные CLI-инструменты бэкапа в объектное хранилище/локальный диск. Общее: **шифрование на клиенте** (пароль репозитория), **дедупликация** по чанкам, версионные снапшоты, монтирование репозитория как ФС.

- **Restic** — старый, простой, стабильный, репозиторий в формате restic, есть везде в пакетных менеджерах.
- **Kopia** — новее, дедуп лучше на больших объёмах, гибкие политики хранения, параллелизм, есть сервер и GUI, формат репозитория новый. Начиная с Velero 1.14 Kopia используется по умолчанию как uploader для файловых бэкапов, Restic помечен как устаревающий `[ПРОВЕРИТЬ статус в release notes актуальной версии]`.

Оба хороши как **вторая независимая копия** критичных данных — например, дампов БД, которые Velero физически не умеет делать.

## 3. Аналогия из жизни

Представьте, что вы страхуете квартиру. Velero — это бригада грузчиков с описью: они знают, что кровать стояла у стены, шкаф был двухдверный (манифесты), а внутри шкафа лежали паспорта (данные в томе). Restic и Kopia — это сейф в другом городе у родственников: туда вы раз в сутки кладёте копию самых важных документов, и даже если шкаф сгорит вместе с квартирой, паспорта остались. А **учения по восстановлению** — это тренировка МЧС: вы раз в квартал по-настоящему собираете тревожный чемодан и замеряете время, а не читаете инструкцию на холодильнике.

Где аналогия ломается: испорченную квартиру нельзя «откатить», а бэкап — можно; зато бэкап не спасает от того, что вы сами каждую ночь аккуратно копируете уже испорченные данные.

## 4. Практика и команды

Все команды выполняются с **UBU01 (10.10.10.30)** — там лежит `kubectl` и `kubeconfig` к кластеру (проверка: `kubectl get nodes` показывает k8s-cp01, k8s-w01, k8s-w02).

### 4.1. Установка Velero CLI

```bash
# официальный релиз с GitHub, версия под linux/amd64
wget https://github.com/vmware-tanzu/velero/releases/download/v1.14.0/velero-v1.14.0-linux-amd64.tar.gz   # [ПРОВЕРИТЬ актуальную версию]
tar -xzf velero-v1.14.0-linux-amd64.tar.gz
sudo mv velero-v1.14.0-linux-amd64/velero /usr/local/bin/
velero version --client-only
```

`velero version --client-only` печатает версию клиента. Если есть доступ к кластеру, `velero version` покажет и сервер — они должны совпадать по мажору.

### 4.2. Установка сервера Velero в кластер

```bash
cat > credentials-velero <<'EOF'
[default]
aws_access_key_id = vektor
aws_secret_access_key = Lab#2026-vektor
EOF
chmod 600 credentials-velero

velero install \
  --provider aws \
  --plugins velero/velero-plugin-for-aws:v1.9.0 \
  --bucket velero \
  --backup-location-config region=minio,s3ForcePathStyle=true,s3Url=http://10.10.10.61:30900 \
  --secret-file ./credentials-velero \
  --use-node-agent \
  --default-volumes-to-fs-backup \
  --uploader-type kopia
```

Разбор: `--backup-location-config` — параметры S3; `s3ForcePathStyle=true` обязателен для MinIO (адресация вида `host/bucket`, а не `bucket.host`); `--use-node-agent` ставит DaemonSet для файловых бэкапов; `--default-volumes-to-fs-backup` включает их по умолчанию для всех томов; `--uploader-type kopia` выбирает движок. Пароль в файле содержит `#` — в shell его всегда берите в кавычки.

### 4.3. Создание бэкапа

```bash
velero backup create shop-092 --include-namespaces shop --default-volumes-to-fs-backup --wait
velero backup get
```

```text
NAME       STATUS      ERRORS  WARNINGS  CREATED              EXPIRES   STORAGE LOCATION   SELECTOR
shop-092   Completed   0       1         2026-03-02 10:15:03  29d       default            <none>
```

Смотрим `STATUS`: `Completed` — хорошо, `PartiallyFailed` — уже авария (что-то не забралось), `Failed` — бэкап не создан. `WARNINGS=1` типично для подов с `emptyDir`.

### 4.4. Разбор бэкапа и его логов

```bash
velero backup describe shop-092 --details
velero backup logs shop-092 | tail -n 30
```

В выводе `describe` критичны блоки `Phase`, `Included namespaces`, `Resource List` (что именно забрано) и `Restic/Kopia Backups` (сколько томов и их статус). В логах ищите `error` и `timeout`.

### 4.5. Расписание

```bash
velero schedule create shop-daily \
  --schedule="0 2 * * *" \
  --include-namespaces shop \
  --default-volumes-to-fs-backup \
  --ttl 168h
velero schedule get
```

`--schedule` — cron в UTC (2:00 UTC = 5:00 МСК, учитывайте при разборе инцидентов). `--ttl 168h` — хранить 7 дней; `0` означает «не удалять никогда» и через полгода забитый бакет.

### 4.6. Восстановление

```bash
velero restore create --from-backup shop-092 --include-namespaces shop --wait
velero restore describe <имя-restore>      # имя печатается в выводе предыдущей команды
velero restore logs <имя-restore> | grep -i error
```

Восстановление в уже существующий namespace конфликтует с живыми объектами. Для учений правильнее лить в копию: `--namespace-mappings shop:shop-drill`.

### 4.7. Состояние хранилища бэкапов

```bash
kubectl -n velero get backupstoragelocation
kubectl -n velero get pods
kubectl -n velero logs deploy/velero --tail=50
```

`backupstoragelocation` в фазе `Available` — Velero видит бакет. Если `Unavailable` — почти всегда неверные ключи, `s3Url` или сеть до MinIO; смотрите логи `deploy/velero`.

### 4.8. Снапшот etcd (k8s-cp01)

```bash
sudo ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /var/backups/etcd-$(date +%F-%H%M).db

sudo ETCDCTL_API=3 etcdctl snapshot status /var/backups/etcd-*.db -w table
```

`snapshot status` показывает `hash`, `revision`, `totalKey`, `totalSize`. Растущий `totalSize` и `revision` — признак живого кластера. Без `--cacert/--cert/--key` etcd откажет: у него mTLS.

### 4.9. Restic: репозиторий и бэкап

```bash
export RESTIC_REPOSITORY="s3:http://10.10.10.61:30900/restic"
export RESTIC_PASSWORD='Lab#2026-vektor'
restic init
restic backup /srv/payments-dump --tag daily
restic snapshots --compact
```

```text
ID        Time                 Host    Tags   Paths
-------------------------------------------------------------
a1b2c3d4  2026-03-02 10:20:11  ubu01   daily  /srv/payments-dump
```

`restic init` создаёт зашифрованный репозиторий. **Пароль потерян — данные потеряны навсегда**, восстановления нет by design.

### 4.10. Restic: проверка, ротация, восстановление

```bash
restic check --read-data-subset=2%
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
restic restore latest --target /tmp/restore-shop
```

`--read-data-subset=2%` читает 2% данных и сверяет хеши — это единственный способ узнать, что бэкап не битый. `forget --prune` применяет политику хранения и физически чистит репозиторий. `restore latest` разворачивает последний снапшот в указанную папку.

### 4.11. Kopia: репозиторий, снапшот, политика

```bash
export KOPIA_PASSWORD='Lab#2026-vektor'
kopia repository create s3 \
  --bucket=kopia-shop \
  --endpoint=10.10.10.61:30900 \
  --access-key=vektor \
  --secret-access-key='Lab#2026-vektor' \
  --disable-tls        # [ПРОВЕРИТЬ имя флага для http в вашей версии]

kopia snapshot create /srv/payments-dump
kopia snapshot list
kopia policy set --global --keep-latest 10 --keep-daily 7 --keep-weekly 4
```

`kopia snapshot list` печатает дерево снапшотов с размерами и дедуплицированным объёмом. Политика хранения в Kopia задаётся на репозиторий/источник, а не в момент бэкапа, как в Restic.

### 4.12. Kopia: проверка и восстановление

```bash
kopia snapshot verify --verify-files-percent=5
kopia restore k0123456789abcdef /tmp/restore-kopia
```

`verify` пересчитывает хеши указанного процента файлов. `restore` принимает ID снапшота (берётся из `snapshot list`) и целевую папку; восстанавливать можно отдельный файл, указав путь после ID.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер **k8s-cp01 (10.10.10.61), k8s-w01 (.62), k8s-w02 (.63)** живой, `kubectl get nodes` — 3 узла `Ready`, есть StorageClass по умолчанию (создавали в главах про storage).
- Namespace `shop` с Deployment и PVC (сервис платежей/заказов) уже развёрнут.
- **UBU01 (10.10.10.30)** — SSH-доступ, `kubectl`, `restic`, `kopia` (`apt install restic`, kopia — из релиза на GitHub `[ПРОВЕРИТЬ пакет в репозитории Ubuntu]`).
- Windows-часть стенда не нужна.
- Если namespace `shop` ещё нет — создайте минимальный: `kubectl create ns shop` и PVC 1Gi + под, пишущий файл в том.

Таргет для бэкапов — **MinIO внутри кластера**. Это осознанный компромисс лаборатории: в реальной работе бакет обязан быть вне кластера (Yandex Object Storage, VK Cloud, Selectel — все доступны из РФ легально).

### Снапшот

В VMware Workstation на каждой затрагиваемой ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-092`.

ВМ: `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01`. Пока идёт снапшот, ВМ лучше не трогать.

### Шаги

1. **k8s-cp01.** Развернуть MinIO в namespace `backup-system` (манифест ниже). Ожидаемо: `kubectl -n backup-system get pods` → `minio-... 1/1 Running`.
2. **k8s-cp01.** Создать бакеты `velero`, `restic`, `kopia-shop` через веб-консоль MinIO (`http://10.10.10.61:30901`) или `kubectl exec` с `mc`. Ожидаемо: три бакета видны в UI.
3. **UBU01.** Установить Velero CLI (п. 4.1). Ожидаемо: `velero version --client-only` печатает версию.
4. **UBU01.** Выполнить `velero install` из п. 4.2. Ожидаемо: `kubectl -n velero get pods` → `velero-...` и по одному `node-agent-...` на каждой ноде в `Running`.
5. **UBU01.** Записать тестовые данные в сервис: открыть приложение `shop` и создать 5 заказов, либо `kubectl -n shop exec deploy/shop -- sh -c 'echo order-1 >> /data/orders.txt'`. Ожидаемо: файл/записи существуют, зафиксируйте количество в `stand-log.md`.
6. **UBU01.** Создать бэкап `velero backup create shop-092 --include-namespaces shop --default-volumes-to-fs-backup --wait`. Ожидаемо: `STATUS=Completed`, в `describe --details` блок Kopia Backups показывает 1 том `Completed`.
7. **k8s-cp01.** Снять снапшот etcd (п. 4.8) и проверить `snapshot status`. Ожидаемо: таблица с `totalKey` > 0, файл в `/var/backups/`.
8. **UBU01.** Настроить независимую копию в Restic: `restic init`, `restic backup /srv/payments-dump` (создайте папку с тестовым дампом). Ожидаемо: `restic snapshots` показывает один снапшот.
9. **UBU01.** То же в Kopia (п. 4.11). Ожидаемо: `kopia snapshot list` показывает снапшот с ненулевым размером.
10. **UBU01. Имитация аварии:** `kubectl delete ns shop`. Ожидаемо: `kubectl get ns shop` → `NotFound`, портал отдаёт 404/500.
11. **UBU01.** Восстановить: `velero restore create --from-backup shop-092 --include-namespaces shop --wait`. Ожидаемо: `kubectl -n shop get pods` — поды `Running`, манифесты и Service вернулись.
12. **UBU01. Учебное восстановление в копию:** `velero restore create drill-092 --from-backup shop-092 --namespace-mappings shop:shop-drill --include-namespaces shop --wait`. Замерьте время: от запуска до `Running` — это ваш фактический RTO. Ожидаемо: работают оба namespace одновременно, прод не тронут.
13. **UBU01.** Проверить целостность независимых копий: `restic check --read-data-subset=2%` и `kopia snapshot verify --verify-files-percent=5`. Ожидаемо: `no errors were found`.
14. **UBU01.** Записать в `stand-log.md` результаты: дата учения, имя бэкапа, измеренный RTO, фактический RPO (когда был последний бэкап до «аварии»).

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: backup-system
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: minio-data
  namespace: backup-system
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 20Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: minio
  namespace: backup-system
spec:
  replicas: 1
  selector:
    matchLabels: {app: minio}
  template:
    metadata:
      labels: {app: minio}
    spec:
      containers:
        - name: minio
          image: quay.io/minio/minio:latest   # [ПРОВЕРИТЬ актуальный тег]
          args: ["server", "/data", "--console-address", ":9001"]
          env:
            - {name: MINIO_ROOT_USER, value: vektor}
            - {name: MINIO_ROOT_PASSWORD, value: "Lab#2026-vektor"}
          ports:
            - {containerPort: 9000}
            - {containerPort: 9001}
          volumeMounts:
            - {name: data, mountPath: /data}
      volumes:
        - name: data
          persistentVolumeClaim: {claimName: minio-data}
---
apiVersion: v1
kind: Service
metadata:
  name: minio
  namespace: backup-system
spec:
  type: NodePort
  selector: {app: minio}
  ports:
    - {name: api, port: 9000, targetPort: 9000, nodePort: 30900}
    - {name: console, port: 9001, targetPort: 9001, nodePort: 30901}
```

### Проверка

- [ ] `kubectl -n velero get pods` — velero и все `node-agent` в `Running`, рестартов нет.
- [ ] `kubectl -n velero get backupstoragelocation` — `default` в фазе `Available`.
- [ ] `velero backup get` — `shop-092` в статусе `Completed`, `ERRORS=0`.
- [ ] В MinIO (консоль `:30901`) в бакете `velero` лежат каталоги `backups/`, `kopia/`.
- [ ] Снапшот etcd существует, `snapshot status` возвращает `totalKey > 0`.
- [ ] `restic snapshots` и `kopia snapshot list` показывают по снапшоту, `check`/`verify` без ошибок.
- [ ] После `delete ns shop` и restore заказы на месте (количество совпадает с записанным в шаге 5).
- [ ] Учебное восстановление в `shop-drill` прошло, время зафиксировано в `stand-log.md`.
- [ ] В `stand-log.md` есть строка вида: «092: Velero+Kopia, MinIO 10.10.10.61:30900, RPO 24ч, RTO 4 мин (drill-092)».

### Сломай и почини

> **Пользователь:** Алексей Сидоров (`sidorov`, склад)
> **ВМ / сервис:** портал заказов в namespace `shop` на k8s-w01
> **Что написал:** «Портал открывается, но заказов нет вообще, хотя вчера было 30 штук. Ошибок не пишет, просто пустая таблица. Отгрузки встали.»
> **Срочность:** Высокая (остановлена работа склада)

**Как воспроизвести.** Смоделируйте «неудачный бэкап»: удалите namespace и восстановите его из бэкапа, созданного **без** файловых данных:

```bash
kubectl delete ns shop
velero backup create shop-broken --include-namespaces shop --wait   # без --default-volumes-to-fs-backup
velero restore create --from-backup shop-broken --include-namespaces shop --wait
```

Манифесты вернутся, поды поднимутся, а файлы в томе будут пустыми — ровно то, что увидел пользователь.

**Диагностика по порядку:**

1. Спросите «когда последний раз видели данные» — это определяет целевой бэкап и RPO.
2. `kubectl -n shop exec deploy/shop -- ls -la /data` — папка существует, но пустая → проблема в данных, а не в приложении.
3. `velero restore describe <restore> --details` — смотрите, был ли восстановлен том. Строка про `pod volume restores` отсутствует → бэкап делался без fs-backup.
4. `velero backup describe shop-broken --details` — подтверждает: в бэкапе нет Kopia-данных, только манифесты.
5. Проверяем вторую линию защиты: `restic snapshots` на UBU01 → есть ли актуальный снапшот `/srv/payments-dump`.
6. `restic restore latest --target /tmp/restore-shop` и смотрим содержимое: заказы на месте.
7. `kubectl -n shop exec deploy/shop -- ls -la /data` до копирования — фиксируем, что том смонтирован и доступен на запись.

**Решение.** Возвращаем данные из независимой копии:

```bash
restic restore latest --target /tmp/restore-shop
kubectl -n shop cp /tmp/restore-shop/srv/payments-dump/orders.txt deploy/shop:/data/orders.txt
kubectl -n shop exec deploy/shop -- wc -l /data/orders.txt
```

Затем — обязательно — переделываем расписание правильно:

```bash
velero schedule delete shop-daily --confirm
velero schedule create shop-daily --schedule="0 2 * * *" \
  --include-namespaces shop --default-volumes-to-fs-backup --ttl 168h
```

**Ответ пользователю:** «Данные заказов восстановлены по состоянию на <дата/время>, проверьте пожалуйста список — должно быть 30 записей. Причина: в резервное копирование не попадали файлы томов; расписание исправлено, добавлена ежедневная проверка целостности и ежемесячные учения по восстановлению. Приносим извинения за простой, потери — N заказов за период с <время> (RPO), восстановление заняло M минут (RTO).»

**Закрытие заявки:** тип «инцидент», severity High, причина в постмортеме (см. урок 88) — не «виноват Сидоров», а «в процессе бэкапа отсутствовала валидация восстановления; добавлен шаг автотеста восстановления в пайплайн».

### Что изменилось на стенде

- Новый namespace `backup-system` на кластере: Deployment `minio` + PVC `minio-data` 20Gi + Service NodePort 30900/30901.
- Бакеты объектного хранилища: `velero`, `restic`, `kopia-shop`.
- Namespace `velero`: сервер Velero, DaemonSet `node-agent`, `BackupStorageLocation` `default` → MinIO `http://10.10.10.61:30900`.
- Расписание `shop-daily` (02:00 UTC, TTL 168h) с файловыми бэкапами.
- Бэкапы `shop-092`, `shop-broken`, тестовый restore `drill-092`, namespace `shop-drill`.
- На UBU01: `/etc/restic/password` (или переменные окружения в профиле), репозиторий Restic и Kopia в MinIO, локальная папка `/srv/payments-dump`.
- На k8s-cp01: каталог `/var/backups/` со снапшотами etcd.
- В `stand-log.md`: запись об учении, RPO/RTO.

### Откат

В VMware Workstation: **VM → Snapshot → Snapshot Manager** → выбрать `before-devops-092` → **Go to**. Повторить на `k8s-cp01`, `k8s-w01`, `k8s-w02`, `UBU01`. Если удаляли namespace `shop` и снапшот откатывать не хочется — достаточно `velero restore create --from-backup <последний-рабочий-бэкап>` и заново создать расписание.

## 6. Ошибки новичков и безопасность

1. **Бэкап в тот же кластер/на тот же диск.** → Удалили namespace — удалили и бэкап; упал гипервизор — нет ничего. → Таргет всегда вне защищаемой системы, минимум одна копия в другом месте.
2. **«Бэкап есть, значит всё хорошо».** → Восстановление падает через три месяца, когда оно нужно. → Регулярные учения и автотест восстановления в изолированный namespace (не реже раза в квартал, для критичных сервисов — раз в месяц).
3. **Бэкап без данных тома.** → Ровно сценарий из «Сломай и почини». → Проверять в `describe --details`, что тома реально забраны; следить за `STATUS=PartiallyFailed`.
4. **Несогласованный бэкап БД.** → Файлы скопированы посреди транзакции, при восстановлении битые индексы. → `pg_dump`/`mysqldump` в CronJob или Velero-хуки с `fsfreeze`; для managed БД — их нативные снапшоты.
5. **Пароль репозитория в Git или в открытом файле на видном месте.** → Кто угодно читает все бэкапы. → Пароль в менеджере секретов, `chmod 600`, отдельный секрет в CI.
6. **Потерян пароль Restic/Kopia.** → Репозиторий превращается в мусор, восстановления нет. → Две копии пароля в разных местах, вне репозитория, с процедурой передачи.
7. **TTL «на всякий случай побольше».** → Через год бакет забит, счёт растёт, а бэкапов «вчера» нет. → Политика хранения по RPO, алерт на заполнение бакета.
8. **Бэкап любого кластера одним паролем и одной учёткой.** → Компромисс одного стенда раскрывает всё. → Отдельные учётки/бакеты/пароли, минимальные права на запись.

**Безопасность.** Velero-сервер имеет право читать **все** Secrets кластера — доступ к namespace `velero` равен доступу к кластеру, выдавайте его только сервисной учётке CI-джобы бэкапа. Репозитории Restic/Kopia должны быть зашифрованы (это по умолчанию) и по возможности иметь **immutable/object-lock** или офлайн-копию: это единственная защита от шифровальщика и от собственного `restic forget --prune` «не в ту сторону». Ключи MinIO в этой лабе лежат в манифесте прямо в YAML — так делать нельзя даже на стенде, если стенд используется кем-то ещё; в рабочем кластере только Secret/External Secrets. И помните: бэкап не заменяет отказоустойчивость — Velero помогает пережить потерю данных, а не простой сервиса.

## 7. Вопросы на собеседовании

**1. Чем RPO отличается от RTO и как вы их определяете для сервиса?**
RPO — допустимая потеря данных (определяет частоту бэкапов и тип репликации), RTO — допустимое время недоступности (определяет, как быстро вы подниметесь: час на восстановление из S3 или минута на переключение на реплику). Оба задаются бизнесом исходя из стоимости простоя и потери данных, а не инженером в одиночку. Дальше вы выбираете инструменты: RPO 24ч → суточный Velero-бэкап; RPO 5 мин → репликация БД или WAL-архив; RTO 30 мин → тёплый standby, а не «соберём кластер заново».
*Что хочет услышать:* понимание, что RPO/RTO — это требования, а не характеристики инструментов, и что вы связываете их с деньгами и SLA.
*Красный флаг:* «RTO у нас — сколько займёт восстановление» (то есть измерение вместо цели) или путаница терминов местами.

**2. Как Velero бэкапит PersistentVolume и чем fs-backup отличается от CSI-снапшота?**
Два способа. Первый — CSI VolumeSnapshot: средство хранения делает снапшот тома на уровне СХД, быстро, но зависит от CSI-драйвера и привязано к нему при восстановлении. Второй — fs-backup: под node-agent на ноде читает файловую систему пода и заливает данные в объектное хранилище в формате Kopia; медленнее, зато переносимо между кластерами и облаками. Ни один из способов не гарантирует консистентность приложения — для БД нужны хуки (`pre`/`post`) или дампы.
*Что хочет услышать:* понимание разницы «снапшот блока» vs «файлы в S3» и что оба не решают консистентность автоматически.
*Красный флаг:* «Velero делает снапшоты баз данных» — он не знает, что внутри тома.

**3. Почему «бэкап успешен» не равно «восстановление возможно» и как вы это проверяете?**
Успешный бэкап значит только то, что данные записались в хранилище. Он не значит, что они читаемы, что хватает прав, что манифесты совместимы с текущей версией кластера и что политика хранения их не удалила. Проверяю так: периодический автоматический restore в отдельный namespace (`--namespace-mappings shop:shop-drill`), проверка бизнес-инварианта (количество записей, HTTP 200 на ключевых ручках), замер времени — это и есть фактический RTO; плюс `restic check`/`kopia snapshot verify` для целостности. Результат учения — запись в runbook, а не «устно проверили».
*Что хочет услышать:* культура регулярных restore drill, автоматизация проверки, измерение RTO.
*Красный флаг:* «Бэкапы проверяются алертом на статус Completed».

**4. Restic или Kopia — что выберете и почему?**
Оба шифруют на клиенте и дедуплицируют. Restic — зрелый, простой, предсказуемый, есть в репозиториях дистрибутивов; минус — медленный `list`/`prune` на очень больших репозиториях и меньше гибкости в политиках. Kopia — новее, лучше параллелизм и дедуп, гибкие политики на источники, встроенный сервер и GUI, и именно Kopia используется в Velero как современный uploader. Мой выбор: Kopia для новых инсталляций и крупных объёмов, Restic — если нужна максимальная простота и совместимость. Главное — что инструмент поддерживается командой и что учения проходят.
*Что хочет услышать:* осознанный выбор по критериям (объём, зрелость, экосистема), а не «то, что больше нравится».
*Красный флаг:* сравнение только по скорости бэкапа без учёта восстановления и проверки целостности.

**5. Что ещё, кроме namespace'ов, нужно включить в DR-план Kubernetes-платформы?**
Снапшоты etcd (база кластера), cluster-scoped объекты: CRD, ClusterRole/Binding, StorageClass, PersistentVolume, namespace'ы по умолчанию; секреты и ключи шифрования etcd; реестр образов (HARBOR01 — иначе восстанавливать нечего запускать) с политикой ретенции; конфигурацию ingress/DNS и TLS-сертификаты; инфраструктуру как код (Terraform/Ansible в GIT01) — чтобы кластер можно было пересобрать; внешние managed-сервисы и их настройки; наконец, документация и контакты on-call. И проверка, что бэкап etcd снимается с той же версии, на которую вы будете восстанавливаться.
*Что хочет услышать:* системное мышление — кластер это не только приложения, и восстановление невозможно без реестра, IaC и DNS.
*Красный флаг:* «Velero бэкапит всё нужное» — etcd и cluster-scoped ресурсы он покрывает не полностью, а реестр образов не покрывает вовсе.

## 8. Источники (официальная документация)

- **Velero** — `velero.io`, разделы «Installation», «Backup Reference», «Restore Reference», «File System Backup», «Resource Filtering». Поиск: `velero file system backup kopia node-agent`, `velero restore namespace mapping`.
- **Kopia** — `kopia.io`, разделы «Repositories» (S3), «Policies», «Snapshots and Restore», «Verification». Поиск: `kopia repository create s3 minio`, `kopia snapshot verify`.
- **Restic** — `restic.net` и `restic.readthedocs.io`, разделы «Preparing a repository», «Working with repositories», «Removing backup data» (`forget --prune`). Поиск: `restic s3 minio repository url`, `restic check read-data-subset`.
- **Kubernetes** — `kubernetes.io`, раздел «Operating etcd clusters for Kubernetes» (снапшоты и восстановление etcd). Поиск: `kubernetes etcdctl snapshot save restore kubeadm`.
- **MinIO** — `min.io`, раздел «MinIO Object Storage for Kubernetes» (Deployment, buckets, `mc`). Поиск: `minio kubernetes operator bucket mc alias`.
- **CNCF / SRE** — `sre.google` (книга Google SRE, главы про data integrity и disaster recovery). Поиск: `google sre book data integrity disaster recovery`.
- Конкретные версии образов, теги плагинов `velero-plugin-for-aws`, флаги `kopia repository create s3 --disable-tls` и текущий статус Restic в Velero — `[ПРОВЕРИТЬ]` по release notes перед лабораторной.
