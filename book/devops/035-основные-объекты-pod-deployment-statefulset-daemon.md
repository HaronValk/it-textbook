# Основные объекты Kubernetes: Pod, Deployment, StatefulSet, DaemonSet, Job, CronJob, Service, Ingress

## 1. Введение (зачем это нужно в реальной работе)

Тикет в чате поддержки: «**Пользователь:** Сидоров (склад). **ВМ:** рабочее место в офисе. **Что написал:** "Открываю отчёт по отгрузкам — то 502 Bad Gateway, то старая версия за январь. И почему-то только у меня, у Петровой нормально". **Срочность:** средняя, но отчёт нужен к 15:00».

Ты приходишь в кластер `k8s-cp01` и видишь: три реплики приложения, один под в `CrashLoopBackOff`, другой ещё не готов, Service раскидывает трафик по всем трём, а часть пользователей попадает на «старую версию», потому что Deployment застрял на середине rolling update. Всё это — восемь объектов из названия главы, и 90 % инцидентов в Kubernetes на L1/L2 читаются именно через них.

После главы ты должен уметь: прочитать любой манифест и понять, что он делает; объяснить разницу между Deployment и StatefulSet и не перепутать их на проде; найти, почему Service не доставляет трафик до подов; запустить разовую и регулярную задачу; и внятно ответить на собеседовании, зачем вообще нужен отдельный объект Service, если у пода уже есть IP.

Дальше по модулю эти объекты будут только углубляться: конфиги (урок 36), сеть (37), Ingress (38), хранилище (39), ресурсы и планирование (40).

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Pod** — минимальная единица развёртывания в Kubernetes, а не контейнер. Под — это «песочница» из одного или нескольких контейнеров, которые делят между собой: сетевой namespace (один IP на под, контейнеры видят друг друга по `localhost`), IPC-namespace и список volume. У каждого пода есть служебный контейнер `pause`, который держит сеть и namespace, даже если прикладной контейнер упал. Под эфемерен: удалили — создался новый, с другим IP и новым именем.

Ты почти никогда не создаёшь поды напрямую. Ты создаёшь **контроллер** — объект, который следит, чтобы подов было столько, сколько надо, и таких, каких надо. Контроллеры живут в `kube-controller-manager` на control plane и работают по принципу reconciliation loop: сравнивают *желаемое* состояние (поле `spec`) с *фактическим* (поле `status`) и приводят второе к первому.

| Объект | API-группа | Что гарантирует | Типичная роль |
|---|---|---|---|
| Pod | `v1` | «этот контейнер запущен» | отладка, одноразовые задачи |
| Deployment | `apps/v1` | N одинаковых взаимозаменяемых подов + rolling update | stateless-приложения |
| StatefulSet | `apps/v1` | стабильные имена `name-0..N-1`, порядок запуска, свой диск на под | БД, брокеры, ZooKeeper/etcd |
| DaemonSet | `apps/v1` | ровно один под на каждую подходящую ноду | агенты: node-exporter, CNI, лог-шиппер |
| Job | `batch/v1` | задача выполнилась K раз и завершилась | миграции БД, бэкап |
| CronJob | `batch/v1` | создаёт Job по расписанию cron | ночные отчёты, чистки |
| Service | `v1` | стабильный IP/DNS + балансировка на поды | «точка входа» к подам |
| Ingress | `networking.k8s.io/v1` | L7-маршрутизация по Host/Path | публикация снаружи |

**Ключевая мысль про Service.** IP пода меняется при каждом пересоздании, поэтому приложения не ходят друг к другу по IP подов. Service — это виртуальный IP (ClusterIP, из диапазона, который задаётся в `--service-cluster-ip-range` при init кластера) плюс selector по меткам. Селектор находит поды, kube-controller-manager пишет их IP в объект **EndpointSlice** (раньше — `Endpoints`), а kube-proxy на каждой ноде превращает это в правила iptables/IPVS, которые подменяют адрес назначения на конкретный под. Service — это L4 (TCP/UDP), он не понимает HTTP-заголовки.

**Ingress** — это L7: правила «Host: web.vektor.lab, Path: / → сервис web:80». Сам по себе объект Ingress **ничего не делает** — его читает ingress-контроллер (nginx-ingress, Traefik), который сам является Deployment/DaemonSet с Service типа LoadBalancer/NodePort. Об этом подробно в уроке 38.

```text
                    ┌──────────────────────────────────────────┐
   браузер  ───────►│ Ingress (правила Host/Path, L7)          │
                    │   читает ingress-controller (Pod)        │
                    └───────────────────┬──────────────────────┘
                                        │  по имени сервиса
                    ┌───────────────────▼──────────────────────┐
                    │ Service web (ClusterIP 10.96.x.x, L4)    │
                    │ selector: app=web  → EndpointSlice       │
                    └───────────────────┬──────────────────────┘
                                        │ iptables/IPVS (kube-proxy)
                    ┌───────────────────▼──────────────────────┐
                    │ Deployment web → ReplicaSet web-7d9f…    │
                    │   ├── Pod web-7d9f…-a1  (10.244.1.7)     │
                    │   ├── Pod web-7d9f…-b2  (10.244.2.5)     │
                    │   └── Pod web-7d9f…-c3  (10.244.2.9)     │
                    └──────────────────────────────────────────┘

   StatefulSet pg:  pg-0 (свой PVC), pg-1, pg-2 + headless Service pg-headless
   DaemonSet agent: по одному поду на k8s-w01 и k8s-w02 (на cp01 — только с toleration)
```

Порты, которые пригодятся: API-сервер `6443`, kubelet `10250`, диапазон NodePort по умолчанию `30000–32767`, DNS-сервис кластера (CoreDNS) — ClusterIP `10.96.0.10` в дефолтной конфигурации kubeadm `[ПРОВЕРИТЬ для своего кластера: kubectl -n kube-system get svc kube-dns]`.

## 3. Аналогия из жизни

Представь сеть кофеен. **Pod** — это одна точка с бариста: конкретное помещение, конкретный адрес, закрылась — всё, адреса больше нет. **Deployment** — это управляющий сети: ему неважно, *какая именно* точка работает, важно, чтобы в районе их было ровно три и все с одинаковым меню; точка сгорела — он открывает новую, но по другому адресу. **StatefulSet** — это, скажем, три нумерованных сейфа в головном офисе: сейф №0, №1, №2, у каждого своё содержимое, и открывать их нужно строго по порядку, а если сейф №1 сломали — новый поставят на то же место и с тем же номером, иначе часть документов просто потеряется. **DaemonSet** — это камера видеонаблюдения: по одной на каждую точку, не больше и не меньше, и без камеры точка не считается открытой. **Job** — разовый вызов клинера: пришёл, убрал, ушёл. **CronJob** — тот же клинер, но по расписанию «каждую ночь в 3:00». **Service** — единый телефон колл-центра: клиент звонит на один номер и не знает, какой бариста сегодня на смене. **Ingress** — девушка на ресепшене, которая слушает, *что именно* просит клиент, и переводит звонок в нужный отдел.

Аналогия перестаёт работать на масштабировании: управляющий сети не может открыть точку «за 30 секунд на любом свободном углу по усмотрению планировщика», а именно это и делает Deployment с подом.

## 4. Практика и команды

Все команды — с `kubectl` на `k8s-cp01` (кластер из урока 34), namespace `vektor`.

**1. Осмотреться: что вообще есть в кластере.**
```bash
kubectl get nodes -o wide
kubectl get all -n vektor
kubectl api-resources | grep -Ei 'deployment|statefulset|daemonset|job|cronjob|ingress'
```
`-o wide` добавляет IP нод, версию ядра и container runtime. `get all` — не «вообще всё», а только самые частые типы (pods, services, deployments, replicasets, statefulsets, daemonsets, jobs, cronjobs). Типичный вывод:
```text
NAME       READY   STATUS    RESTARTS   AGE
pod/web-7d9f8c6b4-aaa11   1/1   Running   0   3m
NAME               TYPE        CLUSTER-IP     PORT(S)   AGE
service/web        ClusterIP   10.96.14.22    80/TCP    3m
```
Смотреть: `READY 0/1` — контейнер не проходит readiness-пробу; `RESTARTS` растёт — приложение падает.

**2. Под: создать и посмотреть.**
```bash
kubectl run ping-test -n vektor --image=10.10.10.55/vektor/busybox:1.36 \
  --restart=Never -- sleep 3600
kubectl get pod ping-test -n vektor -o yaml | head -40
```
`--restart=Never` создаёт именно Pod, а не Deployment (в новых версиях kubectl это поведение по умолчанию). В `-o yaml` смотри `spec.containers[].image` и `status.podIP`.

**3. Разобрать под по событиям — главный инструмент диагностики.**
```bash
kubectl describe pod ping-test -n vektor
```
В конце — блок `Events:` с `Scheduled → Pulled → Created → Started`. Если видишь `Failed to pull image` — проблема с образом/реестром, если `0/3 nodes are available` — планировщик не может найти ноду (урок 40).

**4. Логи и вход внутрь.**
```bash
kubectl logs ping-test -n vektor
kubectl logs -f deploy/web -n vektor --tail=50
kubectl logs web-7d9f8c6b4-aaa11 -n vektor -c nginx --previous
kubectl exec -it ping-test -n vektor -- sh
```
`-f` — потоком, `--tail` — последние N строк, `-c` — конкретный контейнер (если их в поде несколько), `--previous` — логи **предыдущего** (упавшего) контейнера, это спасает при `CrashLoopBackOff`. `--` отделяет команду kubectl от команды внутри контейнера.

**5. Deployment: создать, обновить, откатить.**
```bash
kubectl create deployment web -n vektor --image=10.10.10.55/vektor/nginx:1.27 --replicas=3
kubectl rollout status deployment/web -n vektor
kubectl set image deployment/web -n vektor nginx=10.10.10.55/vektor/nginx:1.27.1
kubectl rollout history deployment/web -n vektor
kubectl rollout undo deployment/web -n vektor --to-revision=1
kubectl scale deployment/web -n vektor --replicas=5
```
`set image <deployment> <имя_контейнера>=<образ>` — имя контейнера в Deployment по умолчанию совпадает с именем deployment (`web`), но у нас после `create deployment --image` контейнер называется `web`, а не `nginx` — проверь через `kubectl get deploy web -o jsonpath='{.spec.template.spec.containers[*].name}'` `[ПРОВЕРИТЬ на своём выводе]`. `rollout undo --to-revision` работает только если не задан `revisionHistoryLimit: 0`.

**6. Правки через apply, а не через императивные команды.**
```bash
kubectl diff -f web-deploy.yaml
kubectl apply -f web-deploy.yaml
```
`diff` показывает, что изменится, до применения. Всё, что делается руками в консоли, для DevOps-трека — временное: источник истины в Git (GIT01).

**7. Service: опубликовать и проверить, что за ним есть эндпоинты.**
```bash
kubectl expose deployment web -n vektor --name=web --type=NodePort \
  --port=80 --target-port=80
kubectl get svc web -n vektor
kubectl get endpointslices -n vektor
kubectl get endpointslices -n vektor -o jsonpath='{.items[*].endpoints[*].addresses[*]}'
```
`--port` — порт самого Service, `--target-port` — порт контейнера. `NodePort` открывает порт из диапазона 30000–32767 на **каждой** ноде. Если в `endpointslices` пусто (`.endpoints` отсутствует) — selector Service не совпадает с метками подов, и трафик уходить некуда.

**8. Проверка с Linux-машины и локальный туннель.**
```bash
# на UBU01 (10.10.10.30)
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.62:30080/

# на k8s-cp01, без открытия портов наружу
kubectl port-forward svc/web 8080:80 -n vektor
curl -I http://127.0.0.1:8080/
```
`port-forward` — туннель через API-сервер, только для отладки: в бою так не публикуют сервисы.

**9. StatefulSet и headless Service.**
```bash
kubectl get statefulset,po -n vektor -l app=pg
kubectl get pvc -n vektor
kubectl delete pod pg-1 -n vektor && kubectl get pod pg-1 -n vektor -w
```
Удалённый `pg-1` пересоздастся **с тем же именем** и тем же PVC. У Deployment так не бывает — там имя всегда новое.

**10. Job и CronJob.**
```bash
kubectl get jobs -n vektor
kubectl create job manual-export -n vektor --from=cronjob/report-export
kubectl logs job/manual-export -n vektor
kubectl delete job manual-export -n vektor
```
`--from=cronjob` — самый частый способ «дёрнуть ночную задачу руками прямо сейчас», не дожидаясь расписания.

**11. События кластера по времени.**
```bash
kubectl get events -n vektor --sort-by=.lastTimestamp | tail -20
```
`--sort-by` с `.lastTimestamp` — быстрый способ увидеть, что происходило последние минуты. В новых версиях есть `.eventTime` `[ПРОВЕРИТЬ]`.

**12. Справка прямо в кластере.**
```bash
kubectl explain statefulset.spec.volumeClaimTemplates
kubectl explain cronjob.spec.concurrencyPolicy
```
`kubectl explain` берёт документацию из той версии API, что реально стоит в кластере, — не надо гуглить схемы под чужую версию.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34 работает: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63). `kubectl get nodes` показывает все три в `Ready`.
- На `k8s-cp01` настроен `kubectl` (kubeconfig администратора из урока 34).
- `HARBOR01` (10.10.10.55) доступен, проект `vektor` создан в уроке 31, в нём лежат зеркала `busybox:1.36`, `nginx:1.27`, `postgres:16-alpine` `[ПРОВЕРИТЬ теги в своём Harbor]`. Если зеркал нет — либо сделай их по уроку 31/32, либо подставь те образы, которые у тебя точно тянутся (проверь `docker pull` с UBU01).
- `UBU01` (10.10.10.30) — с него будем проверять доступность NodePort.
- Если у тебя кластер не kubeadm, а k3s/kind — набор объектов тот же, но имя taint на control-plane и диапазон NodePort могут отличаться `[ПРОВЕРИТЬ: kubectl describe node k8s-cp01 | grep -i taint]`.
- Если в кластере нет default StorageClass, шаг 6 (StatefulSet) даст `Pending` на PVC — это нормально и разбирается в уроке 39. Для этой лабы временно замени `volumeClaimTemplates` на `emptyDir` и запиши это в `stand-log.md` как долг.

### Снапшот

Снапшоты **на всех трёх узлах** (`k8s-cp01`, `k8s-w01`, `k8s-w02`) с именем `before-devops-035`.

В VMware Workstation: выделить ВМ → правый клик → **Snapshot → Take Snapshot** → имя `before-devops-035`.

Или из командной строки хоста (пути подставь свои):
```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VM\k8s-cp01\k8s-cp01.vmx" before-devops-035
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VM\k8s-w01\k8s-w01.vmx" before-devops-035
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VM\k8s-w02\k8s-w02.vmx" before-devops-035
```
Важно: снапшот живой ВМ — crash-consistent. etcd при этом может требовать восстановления, если откатывать только cp01. Для учебной лабы это допустимо; правильный бэкап etcd — урок 46.

### Шаги

1. **k8s-cp01.** Создай namespace и проверь, что кластер здоров:
   ```bash
   kubectl create namespace vektor
   kubectl get nodes
   ```
   Ожидаемо: три ноды `Ready`, namespace `vektor` создан.

2. **k8s-cp01.** Создай файл `pod-ping.yaml`:
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: ping-test
     namespace: vektor
     labels:
       app: ping
   spec:
     containers:
     - name: shell
       image: 10.10.10.55/vektor/busybox:1.36
       command: ["/bin/sh", "-c", "sleep 3600"]
   ```
   Примени: `kubectl apply -f pod-ping.yaml` → `kubectl get pod ping-test -n vektor -o wide`. Ожидаемо: `Running`, есть `podIP` вида `10.244.x.x` и колонка `NODE` с одной из нод.

3. **k8s-cp01.** Зайди в под и посмотри сеть:
   ```bash
   kubectl exec -it ping-test -n vektor -- sh
   # внутри:
   hostname; ip addr show; nslookup kubernetes.default
   ```
   Ожидаемо: hostname = имя пода, один интерфейс, DNS резолвит `kubernetes.default`. Выйти — `exit`.

4. **k8s-cp01.** Создай `web-deploy.yaml` (Deployment + Service NodePort):
   ```yaml
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: web
     namespace: vektor
   spec:
     replicas: 3
     selector:
       matchLabels:
         app: web
     strategy:
       type: RollingUpdate
       rollingUpdate:
         maxSurge: 1
         maxUnavailable: 0
     template:
       metadata:
         labels:
           app: web
       spec:
         containers:
         - name: web
           image: 10.10.10.55/vektor/nginx:1.27
           ports:
           - containerPort: 80
           readinessProbe:
             httpGet:
               path: /
               port: 80
             initialDelaySeconds: 3
             periodSeconds: 5
   ---
   apiVersion: v1
   kind: Service
   metadata:
     name: web
     namespace: vektor
   spec:
     type: NodePort
     selector:
       app: web
     ports:
     - port: 80
       targetPort: 80
       nodePort: 30080
   ```
   `kubectl apply -f web-deploy.yaml` → `kubectl rollout status deployment/web -n vektor`. Ожидаемо: `successfully rolled out`, 3 пода `Running 1/1`.

5. **k8s-cp01 и UBU01.** Проверь эндпоинты и доступ снаружи:
   ```bash
   kubectl get endpointslices -n vektor
   ```
   Ожидаемо: три адреса. Затем на `UBU01`:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.62:30080/
   curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.63:30080/
   ```
   Ожидаемо: оба `200` — NodePort открыт на каждой ноде, даже если под на ней не живёт.

6. **k8s-cp01.** Rolling update и откат:
   ```bash
   kubectl set image deployment/web web=10.10.10.55/vektor/nginx:1.27.1 -n vektor
   kubectl rollout status deployment/web -n vektor
   kubectl rollout history deployment/web -n vektor
   kubectl rollout undo deployment/web -n vektor --to-revision=1
   ```
   Ожидаемо: в процессе обновления поды создаются по одному и только после `Ready` удаляется старый (это эффект `maxUnavailable: 0`). Проверь `kubectl get pod -n vektor -w` в втором терминале.

7. **k8s-cp01.** StatefulSet + headless Service:
   ```yaml
   apiVersion: v1
   kind: Service
   metadata:
     name: pg-headless
     namespace: vektor
   spec:
     clusterIP: None
     selector:
       app: pg
     ports:
     - port: 5432
       name: pg
   ---
   apiVersion: apps/v1
   kind: StatefulSet
   metadata:
     name: pg
     namespace: vektor
   spec:
     serviceName: pg-headless
     replicas: 3
     selector:
       matchLabels:
         app: pg
     template:
       metadata:
         labels:
           app: pg
       spec:
         containers:
         - name: pg
           image: 10.10.10.55/vektor/postgres:16-alpine
           ports:
           - containerPort: 5432
           env:
           - name: POSTGRES_PASSWORD
             value: "Lab#2026-vektor"
           - name: PGDATA
             value: /var/lib/postgresql/data/pgdata
           volumeMounts:
           - name: data
             mountPath: /var/lib/postgresql/data
     volumeClaimTemplates:
     - metadata:
         name: data
       spec:
       accessModes: ["ReadWriteOnce"]
       resources:
         requests:
           storage: 1Gi
   ```
   Внимание: `clusterIP: None` — это и есть headless Service, он не балансирует, а даёт DNS-имена подам. Пароль в открытом виде — **только для лабы**, в реальности Secret из урока 36. Примени, дождись `pg-0`, `pg-1`, `pg-2` по порядку.

8. **k8s-cp01.** Проверь стабильность имён и DNS:
   ```bash
   kubectl delete pod pg-1 -n vektor
   kubectl get pod -n vektor -l app=pg -w      # pg-1 вернётся с тем же именем
   kubectl run dnstest --image=10.10.10.55/vektor/busybox:1.36 -n vektor \
     --restart=Never --rm -it -- nslookup pg-0.pg-headless
   ```
   Ожидаемо: `pg-1` пересоздан, имя то же, PVC тот же; `nslookup` возвращает ClusterIP/адрес `pg-0`.

9. **k8s-cp01.** DaemonSet:
   ```yaml
   apiVersion: apps/v1
   kind: DaemonSet
   metadata:
     name: node-agent
     namespace: vektor
   spec:
     selector:
       matchLabels:
         app: node-agent
     template:
       metadata:
         labels:
           app: node-agent
       spec:
         tolerations:
         - key: node-role.kubernetes.io/control-plane
           operator: Exists
           effect: NoSchedule
         containers:
         - name: agent
           image: 10.10.10.55/vektor/busybox:1.36
           command: ["/bin/sh","-c","while true; do echo agent on $NODE_NAME; sleep 30; done"]
           env:
           - name: NODE_NAME
             valueFrom:
               fieldRef:
                 fieldPath: spec.nodeName
   ```
   Примени. Ожидаемо: ровно 3 пода (по одному на каждую ноду) — потому что мы добавили toleration. Убери блок `tolerations` и перепримени — подов станет 2, на `k8s-cp01` агент не запустится. Это и есть ответ «почему DaemonSet не всегда на всех нодах».

10. **k8s-cp01.** Job и CronJob:
    ```yaml
    apiVersion: batch/v1
    kind: Job
    metadata:
      name: db-migrate
      namespace: vektor
    spec:
      backoffLimit: 3
      ttlSecondsAfterFinished: 600
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: migrate
            image: 10.10.10.55/vektor/busybox:1.36
            command: ["/bin/sh","-c","echo migrate start; sleep 5; echo migrate done"]
    ---
    apiVersion: batch/v1
    kind: CronJob
    metadata:
      name: report-export
      namespace: vektor
    spec:
      schedule: "*/3 * * * *"
      concurrencyPolicy: Forbid
      successfulJobsHistoryLimit: 2
      failedJobsHistoryLimit: 1
      jobTemplate:
        spec:
          template:
            spec:
              restartPolicy: OnFailure
              containers:
              - name: export
                image: 10.10.10.55/vektor/busybox:1.36
                command: ["/bin/sh","-c","date; echo export ok"]
    ```
    Ожидаемо: Job `Completed 1/1`; CronJob создаёт новый Job каждые 3 минуты, старые чистятся по `successfulJobsHistoryLimit`. Проверь: `kubectl get jobs -n vektor`, `kubectl get cronjob -n vektor`.

11. **k8s-cp01.** Ingress-объект (пока только объект):
    ```yaml
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: web
      namespace: vektor
    spec:
      ingressClassName: nginx
      rules:
      - host: web.vektor.lab
        http:
          paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: web
                port:
                  number: 80
    ```
    `kubectl apply -f web-ingress.yaml` → `kubectl get ingress -n vektor`. Ожидаемо: объект создан, колонка `ADDRESS` **пустая** — контроллера Ingress в кластере ещё нет. Это нормально: ставим его в уроке 38.

12. **Хост.** Запиши в `stand-log.md`: создан namespace `vektor`; Deployment/Service `web` (NodePort 30080); StatefulSet `pg` + headless `pg-headless`; DaemonSet `node-agent`; Job `db-migrate`; CronJob `report-export`; Ingress `web` без контроллера.

### Проверка

- [ ] `kubectl get nodes` — 3 ноды `Ready`.
- [ ] `kubectl get pod -n vektor` — `ping-test`, 3 пода `web` (или меньше в момент обновления), `pg-0..2`, 3 пода `node-agent`.
- [ ] `kubectl get endpointslices -n vektor` — у сервиса `web` ровно 3 адреса.
- [ ] `curl` с UBU01 на `http://10.10.10.62:30080/` и `http://10.10.10.63:30080/` возвращает `200`.
- [ ] `kubectl rollout history deployment/web -n vektor` — минимум 2 ревизии, откат на 1 отработал.
- [ ] `pg-1` после удаления вернулся с тем же именем; `nslookup pg-0.pg-headless` отвечает.
- [ ] `kubectl get job db-migrate -n vektor` — `COMPLETIONS 1/1`.
- [ ] `kubectl get cronjob report-export -n vektor` — `SCHEDULE */3 * * * *`, `LAST SCHEDULE` обновляется.
- [ ] `kubectl get ingress web -n vektor` — адрес пустой (ожидаемое поведение без контроллера).
- [ ] В `stand-log.md` появилась запись про урок 035.

### Сломай и почини

> **Пользователь:** Сидоров (склад)
> **ВМ / где:** обращается в чат поддержки, работает через браузер
> **Что написал:** «Отчёт по отгрузкам не открывается вообще. Вчера работал, сегодня браузер пишет "сайт недоступен", коллеги тоже попробовали — у всех то же самое. Срочность: высокая, отчёт нужен к 15:00».
> **Срочность:** высокая

**Как воспроизвести поломку (делаем намеренно).** На `k8s-cp01` «случайно» меняем селектор у Service (типичная ошибка при копировании манифеста из другой среды):

```bash
kubectl patch svc web -n vektor --type=merge \
  -p '{"spec":{"selector":{"app":"web-v2"}}}'
```

Через несколько секунд трафик до подов прекращается: EndpointSlice опустеет.

**Диагностика по порядку (не перескакивай):**

1. Проверяем, что Service вообще существует и какой у него тип/порт:
   ```bash
   kubectl get svc web -n vektor -o wide
   ```
   Ожидаемо: `NodePort 80:30080/TCP`, IP на месте — значит, объект не удалён.

2. Проверяем, есть ли за сервисом поды:
   ```bash
   kubectl get endpointslices -n vektor -l kubernetes.io/service-name=web
   ```
   Пусто → проблема в сопоставлении selector ↔ labels, а не в nginx.

3. Сравниваем метки:
   ```bash
   kubectl get svc web -n vektor -o jsonpath='{.spec.selector}'; echo
   kubectl get pods -n vektor --show-labels | grep web
   ```
   Видим `{"app":"web-v2"}` против `app=web` у подов. Причина найдена.

4. Убеждаемся, что сами поды здоровы (чтобы не чинить два раза):
   ```bash
   kubectl get deploy web -n vektor
   kubectl logs -n vektor deploy/web --tail=20
   ```
   `3/3 READY` — приложение работает, виновата только маршрутизация.

5. Проверяем, что дело не в нодах/сети: с `UBU01` `curl -v http://10.10.10.62:30080/` — получаем `Connection refused` (iptables-правило есть, но DNAT некуда делать).

**Решение:**
```bash
kubectl patch svc web -n vektor --type=merge \
  -p '{"spec":{"selector":{"app":"web"}}}'
kubectl get endpointslices -n vektor -w      # адреса вернулись
```
Правильнее — вернуть файл `web-deploy.yaml` из Git (исправленный) и применить `kubectl apply -f`, чтобы кластер снова совпадал с репозиторием.

**Что написать пользователю:** «Добрый день! Проблема была на стороне сервиса маршрутизации: после изменения конфигурации трафик перестал попадать на работающие серверы приложения. Конфигурацию вернули, проверили доступность из офисной сети — отчёт открывается, данные в актуальной версии. Пожалуйста, обновите страницу с Ctrl+F5. Если повторится — напишите, заведём отдельную заявку на разбор».

**Закрытие заявки:** тип — «ошибка конфигурации», приоритет high, время до восстановления — по факту; в описании зафиксировать: `Service/web selector app=web-v2 → Endpoints пуст → исправлено на app=web, проверено curl 200 с двух нод`. Добавить в `stand-log.md` заметку «изменения Service только через Git + `kubectl diff`».

### Что изменилось на стенде

- На `k8s-cp01`/`k8s-w01`/`k8s-w02` существует namespace `vektor`.
- Deployment `web` (3 реплики, образ из HARBOR01) + Service `web` типа NodePort на порту `30080` на всех нодах.
- StatefulSet `pg` (3 реплики) + headless Service `pg-headless`; PVC на каждый под (если есть StorageClass).
- DaemonSet `node-agent` — по поду на каждую ноду, включая control-plane (благодаря toleration).
- Job `db-migrate` (завершён), CronJob `report-export`, расписание `*/3 * * * *`.
- Ingress `web` (`host: web.vektor.lab`) создан, но без контроллера — активируется в уроке 38.
- В кластере поселились тестовые объекты (`ping-test`, `dnstest`) — по желанию удалить.

### Откат

Если что-то развалилось и починить быстро не выходит:

1. `kubectl delete namespace vektor` — сносит **все** объекты лабы разом. Namespace-удаление асинхронное, дождись `kubectl get ns vektor` → `NotFound`.
2. Если проблема на уровне нод (не поднимается kubelet, слетел etcd): в VMware Workstation выбрать ВМ → **Snapshot Manager** → снапшот `before-devops-035` → **Restore**. Для `k8s-cp01` после отката проверь `kubectl get nodes`: если API-сервер не отвечает, восстанови только cp01 и дождись, пока воркеры переподключатся.
3. В `stand-log.md` добавь строку об откате с причиной — это часть твоей будущей «истории инцидентов» для портфолио.

## 6. Ошибки новичков и безопасность

1. **Создавать поды напрямую вместо контроллера.** → Под удалили / нода упала — приложение не вернулось, потому что восстанавливать некому. → Для всего, что должно жить, — Deployment/StatefulSet/DaemonSet; голый Pod только для отладки.
2. **Путать `port` и `targetPort` в Service.** → `port` — порт самого сервиса, `targetPort` — порт контейнера. Перепутал — трафик уходит в никуда, а в логах приложения тишина. → Всегда сверяй `containerPort` в поде и `targetPort` в сервисе.
3. **Менять selector у Service «по-быстрому».** → Endpoints обнуляются, приложение живое, но недоступное (наш сценарий из лабы). → Селектор — часть контракта; меняешь только вместе с метками подов и через Git.
4. **`replicas: 1` у StatefulSet «чтобы проще» и попытка работать с ним как с Deployment.** → Ломается логика кворума (PostgreSQL/etcd/ZooKeeper), данные расходятся. → Понимай, что StatefulSet — про идентичность и порядок, и что масштабирование таких систем — отдельная инженерная задача.
5. **CronJob с расписанием чаще, чем длится задача.** → Задачи накладываются друг на друга, жрут CPU, лезут в одну и ту же БД. → `concurrencyPolicy: Forbid` + `startingDeadlineSeconds`, а интервал — больше времени выполнения.
6. **Ждать, что Ingress заработает сам.** → Объект создан, `ADDRESS` пустой, трафика нет. → Нужен ingress-контроллер (урок 38) и, для учебного стенда, запись в `/etc/hosts` или DNS.
7. **Хардкодить пароли и теги `latest` в манифестах.** → Пароль в Git, `latest` даёт неповторяемые деплои. → Secret/Sealed Secrets (урок 36), конкретные теги и digest, желательно через Harbor.
8. **Диагностировать по `kubectl get pods`, игнорируя `describe` и `events`.** → Тратишь час на `CrashLoopBackOff`, хотя в событиях написано `OOMKilled`. → Порядок: `get` → `describe` → `logs --previous` → `get events`.

### Безопасность

- **Не публикуй сервис через `NodePort` «наружу»** в боевой среде: порт открыт на каждой ноде, включая ту, где живёт control plane. Нормально — ClusterIP + Ingress/LoadBalancer с TLS и фильтрацией.
- **`clusterIP: None` (headless) не означает «приватный»** — это просто отсутствие балансировки; доступность регулируется NetworkPolicy (урок 37), а не типом сервиса.
- **Не создавай поды с `hostNetwork: true`, `privileged: true` и монтированием `/var/run/docker.sock`** без острой необходимости: это фактически root на ноде. Pod Security Standards и Kyverno/OPA — урок 41.
- **DaemonSet с широкими tolerations** запускает твой контейнер на control plane. Для агента мониторинга это оправдано, для случайного образа — нет.
- **Job/CronJob с доступом к продовой БД** — это учётка с правами. Отдельный ServiceAccount с минимальными правами (урок 41), а не `default`.
- Пароль `Lab#2026-vektor` в манифестах — учебная условность стенда «Вектор». В любой реальной среде такой файл в Git — инцидент ИБ.

## 7. Вопросы на собеседовании

**Вопрос 1. Чем Deployment отличается от StatefulSet и когда что выбирать?**

Ответ: Deployment управляет взаимозаменяемыми подами: имена генерируются (`web-7d9f8c6b4-a1b2c`), поды могут запускаться в любом порядке и параллельно, при обновлении используется RollingUpdate, у пода нет постоянного диска «из коробки». StatefulSet даёт стабильную идентичность: имена строго `pg-0 … pg-N-1`, порядок создания/удаления по возрастанию/убыванию, привязка к headless Service для DNS (`pg-0.pg-headless`), и через `volumeClaimTemplates` каждому поду выдаётся **свой** PVC, который переживает пересоздание пода. Выбор: stateless API, фронтенды, воркеры — Deployment; базы данных, Kafka, ZooKeeper, etcd, всё, где важна идентичность узла или собственные данные на узел — StatefulSet.
*Что хочет услышать интервьюер:* что кандидат понимает идентичность, порядок и персистентность, а не просто «одно для баз, другое для сайтов».
*Красный флаг:* «StatefulSet нужен для баз данных, потому что он быстрее» или незнание, что PVC у StatefulSet свой на каждый под.

**Вопрос 2. Зачем нужен Service, если у каждого пода уже есть IP? Что такое headless Service?**

Ответ: IP пода эфемерен — при пересоздании он меняется, а количество подов плавает. Service даёт стабильный виртуальный IP и DNS-имя, находит поды по selector, контроллер пишет их адреса в EndpointSlice, kube-proxy реализует балансировку на каждой ноде. Типы: ClusterIP (внутренний), NodePort (порт 30000–32767 на каждой ноде), LoadBalancer (внешний LB через облако/металлLB), ExternalName (CNAME-запись). Headless Service — это `clusterIP: None`: балансировки нет, DNS отдаёт адреса подов напрямую (или по одному имени на под в StatefulSet) — нужно, когда клиент сам должен выбрать узел, например клиент БД или peer-discovery.
*Что хочет услышать интервьюер:* понимание цепочки «service → selector → endpointslice → kube-proxy → pod» и того, что Service работает на L4.
*Красный флаг:* «Service — это балансировщик HTTP» (нет, HTTP-маршрутизация — Ingress) или незнание, что будет, если selector не совпал с метками.

**Вопрос 3. Что произойдёт при `kubectl set image`, как управлять скоростью обновления и как откатиться?**

Ответ: Deployment создаст новый ReplicaSet с новым шаблоном пода, и по стратегии RollingUpdate начнёт постепенно заменять поды, соблюдая `maxSurge` (сколько подов можно держать сверх желаемого) и `maxUnavailable` (сколько может быть недоступно). По умолчанию оба 25 %. `maxUnavailable: 0` + `maxSurge: 1` дают обновление без просадки ёмкости, но требуют readiness-проб — иначе новый под может «стартовать», не будучи готовым. Откатиться: `kubectl rollout undo deployment/web --to-revision=N`, посмотреть историю — `kubectl rollout history`. Нюанс: `--record` устарел; чтобы в аннотации была причина, используют `kubernetes.io/change-cause`. При катастрофе — `kubectl rollout undo` быстрее, чем собирать новый образ.
*Что хочет услышать интервьюер:* связку с readiness-пробами, понимание, почему без проб обновление «просаживает» сервис, и что Deployment хранит ревизии не бесконечно (`revisionHistoryLimit`).
*Красный флаг:* «откатиться можно только пересобрав образ» или непонимание роли readiness-проб.

**Вопрос 4. Как DaemonSet решает, на какие ноды ставить поды, и почему агент не появился на control plane?**

Ответ: DaemonSet по умолчанию запускает по одному поду на каждую ноду, учитывая nodeSelector, nodeAffinity и, главное, taints/tolerations нод. On control plane в kubeadm-кластере стоит taint `node-role.kubernetes.io/control-plane:NoSchedule`, поэтому DaemonSet там не размещается, пока в его шаблоне не появится соответствующая toleration. Планировщик всё равно участвует — DaemonSet-контроллер ставит на поды nodeAffinity по имени ноды, а решение принимает kube-scheduler. Проверять: `kubectl describe node <node> | grep Taint` и `kubectl get ds -n <ns> -o wide`. Практические примеры: kube-proxy, CNI-агент, node-exporter, лог-шиппер.
*Что хочет услышать интервьюер:* что DaemonSet ≠ обход планировщика, и что taints — причина «пропавшего» пода.
*Красный флаг:* «DaemonSet ставит поды на все ноды без исключений» — неверно.

**Вопрос 5. Чем Job отличается от CronJob и что будет, если предыдущая задача ещё не закончилась?**

Ответ: Job — разовая задача, гарантирующая N успешных завершений (`completions`) с ограничением повторов (`backoffLimit`, по умолчанию 6) и `restartPolicy: Never`/`OnFailure`; результат — `Completed`. CronJob — расписание в cron-формате, создающее Job по времени; важные поля: `concurrencyPolicy` (`Allow`/`Forbid`/`Replace`, по умолчанию Allow), `startingDeadlineSeconds`, `successfulJobsHistoryLimit` (по умолчанию 3) и `failedJobsHistoryLimit` (1). Если задача длиннее интервала: при `Allow` они накладываются друг на друга, при `Forbid` новый запуск пропускается, при `Replace` старый убивается. Плюс: если controller-manager лежал, при пропуске более 100 запусков CronJob перестаёт планировать — лечится `startingDeadlineSeconds` и мониторингом `kube_cronjob_*`.
*Что хочет услышать интервьюер:* практический опыт — почему «ночной отчёт» вдруг запускался трижды.
*Красный флаг:* «CronJob гарантирует запуск ровно один раз» — не гарантирует, возможны дубли при `Allow`.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Concepts → Workloads*: статьи «Pods», «Deployments», «StatefulSets», «DaemonSet», «Jobs», «CronJob». Поиск: `kubernetes workloads pods deployments statefulsets`.
- **kubernetes.io** — раздел *Concepts → Services, Load Balancing, and Networking*: «Service», «Service ClusterIP / NodePort», «EndpointSlices», «Ingress». Поиск: `kubernetes service endpointslice headless service`.
- **kubernetes.io** — *Reference → kubectl commands* и *API Reference* (`apps/v1`, `batch/v1`, `networking.k8s.io/v1`). Поиск: `kubernetes api reference deployment spec`.
- **kubernetes.io** — *Tasks → Run Applications*: «Rolling update», «Declarative object management», «Managing CronJobs». Поиск: `kubernetes task rolling update deployment kubectl`.
- **github.com/kubernetes** — репозиторий `kubernetes/kubernetes`, каталог `api/openapi-spec` (актуальные схемы объектов для конкретной версии) `[ПРОВЕРИТЬ путь в текущей ветке]`.
- **nginx.org** и **github.com/kubernetes/ingress-nginx** — раздел про Ingress-контроллер для NGINX; понадобится в уроке 38. Поиск: `ingress-nginx controller installation docs`.
- **kubernetes.io** — *Concepts → Configuration → Managing Resources* (requests/limits) — понадобится в уроке 40. Поиск: `kubernetes resource requests limits qos`.

Все конкретные теги образов, номера версий и диапазоны по умолчанию сверяй командой `kubectl explain` и `kubectl version` в своём кластере — они зависят от установленной версии.
