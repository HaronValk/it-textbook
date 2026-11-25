# Диагностика Kubernetes: kubectl debug, события, логи, CrashLoopBackOff, ImagePullBackOff, OOMKilled

## 1. Введение (зачем это нужно в реальной работе)

8:47 утра, портал заявок «Вектора» лежит. Тикет в портале техподдержки от Иванова (бухгалтерия, WIN11-CL01):

> «Добрый день! Не открывается портал согласования заявок — белая страница, иногда 502. У всей бухгалтерии то же самое. Началось вчера после обеда. Очень срочно, у нас платежи!»

Дальше ты открываешь терминал, а не Zabbix с надеждой. Потому что Zabbix уже сказал «HTTP 502», а это значит «nginx в порядке, а вот бэкенд за ним — нет». И дальше начинается работа, которую DevOps делает каждый день: `kubectl get pods`, `describe`, `logs`, `get events` — и через пять минут ты знаешь, что под висит в `ImagePullBackOff`, потому что в деплой прилетел тег образа, которого нет в Harbor.

Эта глава — про то, как читать Kubernetes, когда он сломался. После неё ты должен уметь: по статусу пода сузить круг причин до двух-трёх; отличить `CrashLoopBackOff` от `ImagePullBackOff` и `OOMKilled` за один взгляд; достать логи упавшего контейнера, которого уже нет; зайти внутрь пода, даже если в образе нет `bash` и даже если контейнер не стартует; и объяснить разработчику словами, что именно у него не так, а не «ну, поды не поднимаются».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Диагностика в Kubernetes — это чтение состояния по слоям. Сломалось всегда на одном из них, и важно не прыгать через слои.

```text
kubectl apply / rollout
        │  HTTPS 6443
        ▼
   kube-apiserver ──────► etcd (единственный источник правды: spec и status)
        │
        ├──► kube-scheduler ──► Pending / FailedScheduling  → СОБЫТИЕ
        │
        ▼
   kubelet на ноде (HTTPS 10250)
        │
        ├──► containerd (CRI) ──► pull образа
        │        └─ ошибка ──► ErrImagePull → ImagePullBackOff → СОБЫТИЕ
        │
        ├──► create/start контейнера
        │        ├─ конфиг битый ──► CreateContainerConfigError → СОБЫТИЕ
        │        └─ процесс упал ──► CrashLoopBackOff → СОБЫТИЕ + ЛОГИ (stdout/stderr)
        │
        └──► cgroup v2 memory.max ──► ядро убивает процесс
                 └─ exit 137 + Reason: OOMKilled → СОБЫТИЕ + status
```

Ключевые вещи, которые надо держать в голове:

**Статус пода — это не одно поле.** У пода есть `status.phase` (`Pending`, `Running`, `Succeeded`, `Failed`, `Unknown`) и есть `status.containerStatuses[].state` (`Running`, `Waiting`, `Terminated`). То, что ты видишь в колонке STATUS у `kubectl get pods`, — это чаще всего `Reason` из `Waiting`: `CrashLoopBackOff`, `ImagePullBackOff`, `CreateContainerConfigError`. Пока контейнер в `Waiting`, никаких логов у него нет — есть только события.

**События (Events) — это объекты в etcd, которые создают apiserver, scheduler, kubelet и контроллеры.** Они не вечны: у `kube-apiserver` есть флаг `--event-ttl`, по умолчанию 1 час. Поэтому «а что было вчера» из событий не достать — нужен централизованный сбор логов и метрики (это уроки про наблюдаемость).

**Логи контейнера** kubelet складывает на ноду в `/var/log/pods/<namespace>_<pod>_<uid>/<container>/0.log`. `kubectl logs` идёт через apiserver → kubelet → CRI. Если под перезапустился, текущий лог пустой или свежий, а причина смерти — в предыдущем инстансе: `kubectl logs --previous`.

**Коды выхода говорят о причине больше, чем статус:**

| Код | Что значит |
|-----|-----------|
| 0 | успешное завершение (для Job — норма) |
| 1 | приложение само упало с ошибкой |
| 126 / 127 | не найдена команда / нет прав на исполнение (опечатка в `command`) |
| 137 | 128 + 9 (SIGKILL) — почти всегда OOMKilled или `kubectl delete` |
| 143 | 128 + 15 (SIGTERM) — штатная остановка, graceful shutdown |

**Backoff.** kubelet перезапускает упавший контейнер с нарастающей паузой: 10s, 20s, 40s, 80s, 160s, дальше потолок 5 минут. Пока пауза идёт — статус `CrashLoopBackOff`, в событиях `Back-off restarting failed container`. Это защита ноды от штормов, а не отдельная поломка.

**requests/limits и QoS** (были в уроке 40) здесь критичны: `limits.memory` — это и есть `memory.max` в cgroup. Превысил — ядро убивает процесс внутри контейнера, под получает `OOMKilled` и exit 137, **даже если на ноде свободной памяти вагон**. Это самый частый источник непонимания.

## 3. Аналогия из жизни

Представь приёмное отделение больницы. Пациент (под) либо ещё в коридоре и ждёт койку (`Pending` — врач-распорядитель, scheduler, не нашёл места), либо его привезли в палату (нода), либо он в палате, но с ним что-то не так.

Дальше три разных сценария, и путать их нельзя. Первый: пациента не могут принять, потому что не привезли его карту и лекарства — `ImagePullBackOff`, он вообще не начал лечение. Второй: приняли, начали процедуру, через минуту он встал и ушёл, и так по кругу — `CrashLoopBackOff`, процесс стартует и падает. Третий: у него в палате закончился кислород, и дежурный врач принял решение отключить — `OOMKilled`, внешнее решение, а не болезнь.

События — это журнал приёмного отделения: кто, когда, что сказал. Логи — это дневник самого пациента, который он вёл до потери сознания. `kubectl debug` — это прийти к нему в палату и поговорить лично. А `describe pod` — это карта, где всё написано сразу: диагноз, предыдущий эпизод, лимиты, кто лечащий врач.

Аналогия перестаёт работать в одном: пациент умер — это трагедия, а под «умер» — это норма. Мы его удаляем, контроллер создаёт нового за секунды, и в этом весь смысл.

## 4. Практика и команды

Все команды — с рабочей машины, где настроен `KUBECONFIG` на кластер `k8s-cp01` (10.10.10.61). Namespace для лабораторной — `diag`.

**1. Обзор: кто вообще жив**

```bash
kubectl get pods -A -o wide --field-selector=status.phase!=Running
```

`-A` — все namespace, `-o wide` — добавляет IP и ноду, `--field-selector` фильтрует на стороне apiserver (для подов поддерживаются `status.phase`, `spec.nodeName`, `metadata.name`). `!=` тоже работает.

```text
NAMESPACE   NAME         READY   STATUS             RESTARTS   AGE   IP          NODE
diag        crash-demo   0/1     CrashLoopBackOff   5          6m    10.244.2.7  k8s-w01
diag        oom-demo     0/1     OOMKilled          2          3m    10.244.2.9  k8s-w01
```

Смотрим: `RESTARTS` и `AGE` вместе. 5 рестартов за 6 минут — это быстрый цикл, классический CrashLoop.

**2. Слежение в реальном времени**

```bash
kubectl get pods -n diag -w
kubectl get events -n diag -w --sort-by=.lastTimestamp
```

`-w` = `--watch`, держит соединение и печатает изменения. Второй вариант — лучший способ поймать причину «прямо сейчас».

**3. Главная команда диагностики — describe**

```bash
kubectl describe pod -n diag oom-demo
```

Читать надо три блока: `Containers` (образ, `Limits`, `Requests`, `Last State`), `Conditions` (`Ready: False` с причиной), `Events` (внизу, самый свежий — последний).

```text
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
    Limits:
      memory:  16Mi
    Requests:
      memory:  8Mi
```

`OOMKilled` + `Exit Code: 137` + `Limits.memory: 16Mi` — диагноз готов, дальше арифметика.

**4. Логи: текущие, прошлые, всех контейнеров**

```bash
kubectl logs -n diag crash-demo --tail=50
kubectl logs -n diag crash-demo --previous
kubectl logs -n diag oom-demo --previous --timestamps
kubectl logs -n diag -l app=shop-api --all-containers --prefix --since=15m --max-log-requests=10
kubectl logs -n diag deploy/shop-api -f
```

`--tail` — последние N строк (не грузи гигабайты), `--previous`/`-p` — логи прошлого инстанса контейнера, `--since` — окно по времени, `-l` — по селектору, `--prefix` — приписывает имя пода к каждой строке, иначе каша из нескольких. `--max-log-requests` поднимает лимит (по умолчанию 5) — на 30 подах без него получишь ошибку.

**5. События: самый недооценённый источник**

```bash
kubectl get events -n diag --sort-by=.lastTimestamp
kubectl get events -n diag --field-selector involvedObject.name=crash-demo,involvedObject.kind=Pod
```

```text
LAST SEEN   TYPE      REASON    OBJECT            MESSAGE
4m          Normal    Scheduled pod/crash-demo    Successfully assigned diag/crash-demo to k8s-w01
4m          Normal    Pulled    pod/crash-demo    Container image "busybox:1.36" already present on machine
3m          Warning   BackOff   pod/crash-demo    Back-off restarting failed container crash in pod crash-demo_diag
```

`involvedObject.name` + `involvedObject.kind` — единственный способ отфильтровать события одного объекта. И помни про TTL 1 час.

**6. `kubectl debug` — эфемерный контейнер в живой под**

```bash
kubectl debug -it -n diag shop-api-7d9f8c6b5-4xz2p \
  --image=busybox:1.36 --target=api -- sh
```

`--target` — разделить PID-namespace с указанным контейнером (видишь его процессы через `ps`), `-it` — интерактивно. Внутри пода появляется **ephemeral container** — он не входит в `PodSpec.containers`, не может иметь resources, его нельзя удалить без пересоздания пода, и он не переживает рестарт пода. В актуальных версиях (1.25+) это стабильная функциональность.

**7. `kubectl debug --copy-to` — копия пода для отладки**

```bash
kubectl debug -n diag crash-demo --copy-to=crash-debug \
  --image=busybox:1.36 --share-processes -- sh
```

Создаёт **новый** под по образцу старого, но с другим образом/командой; оригинал не трогается. `--share-processes` (по умолчанию `true` для копии) включает общий namespace процессов. Незаменимо, когда контейнер падает так быстро, что `exec` не успеть.

**8. `kubectl debug node/...` — зайти на ноду как в под**

```bash
kubectl debug node/k8s-w01 -it --image=busybox:1.36 -n diag
```

Создаёт под с `hostPID`, `hostNetwork`, `hostIPC`; корень ноды смонтирован в `/host`. Внутри: `chroot /host` — и ты в системе ноды. Мощно и опасно, см. раздел 6.

**9. `kubectl exec` — когда контейнер жив**

```bash
kubectl exec -it -n diag shop-api-7d9f8c6b5-4xz2p -- sh
kubectl exec -n diag shop-api-7d9f8c6b5-4xz2p -- env | grep -i -E 'db|secret'
```

Всё после `--` выполняется внутри контейнера. `env` часто показывает, что ConfigMap подставился не тот, или Secret пустой.

**10. Метрики: `top`**

```bash
kubectl top pod -n diag --containers
kubectl top node k8s-w01
```

```text
POD         NAME   CPU(cores)   MEMORY(bytes)
oom-demo    hog    3m           15Mi
```

Требует установленного metrics-server (ставился в уроке 34). `15Mi` при лимите `16Mi` — вот тебе и OOM.

**11. Точечно вытащить поле из YAML**

```bash
kubectl get pod -n diag oom-demo -o jsonpath='{.status.containerStatuses[*].lastState.terminated.reason}{"\n"}'
kubectl get pod -n diag oom-demo -o jsonpath='{.spec.containers[*].resources}{"\n"}'
kubectl explain pod.spec.containers.lifecycle
```

`jsonpath` — для скриптов и дежурных однострочников (`-o yaml` слишком многословен). `kubectl explain` — встроенная документация по любому полю, лучше гуглить сюда.

**12. Уровень ноды, когда kubectl бессилен**

```bash
# на k8s-w01 через SSH (пользователь стенда)
sudo systemctl status kubelet containerd
sudo journalctl -u kubelet -f --no-pager
sudo crictl ps -a | grep -i oom-demo
sudo crictl logs <CONTAINER_ID> --tail 50
sudo dmesg -T | grep -i -E 'oom|killed process'
```

`crictl` — CLI к CRI-рантайму (containerd), конфиг по умолчанию `/etc/crictl.yaml`, сокет `unix:///run/containerd/containerd.sock`. `dmesg` покажет, кого именно убил ядро — контейнер или вообще весь сервис на ноде.

## 5. Лабораторная работа

### Предусловия

Кластер из уроков 33–44 жив и здоров: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), metrics-server установлен, `kubectl` настроен на рабочей машине. На `HARBOR01` (10.10.10.55) опубликован реестр — предполагаем, что его адрес `harbor.corp.vektor.lab`; **если у тебя в стенде он заведён под другим именем, подставь своё**. Образы `busybox:1.36` и `nginx:1.27` должны быть доступны (в крайнем случае — через зеркало в проекте `mirror` на HARBOR01, потому что docker.io из РФ бывает недоступен).

Если кластера нет — эту лабораторную делать не на чем; пройди уроки 33–34.

### Снапшот

На **k8s-cp01, k8s-w01, k8s-w02** (и на HARBOR01, если будешь менять там проект):

VM → Snapshot → Take Snapshot → имя `before-devops-045` → описание «начало урока 45, диагностика».

Правило про горячие снапшоты: файловая система etcd на cp01 от горячего снапшота может оказаться неконсистентной. Для лабы это некритично, но перед важными работами узел лучше погасить (`sudo systemctl stop kubelet`) или делать снапшот etcd через `etcdctl snapshot save` (будет в уроке 46).

### Шаги

1. **k8s-cp01 / рабочая машина.** Проверь кластер: `kubectl get nodes -o wide` → все три `Ready`. Затем `kubectl get pods -A | grep -v -E 'Running|Completed'` → пусто. Если что-то не так — сначала лечи кластер, не начинай лабу.
2. **Создай namespace.** `kubectl create namespace diag` → `namespace/diag created`.
3. **Сценарий A: CrashLoopBackOff.** Запусти под, который сразу падает:
   ```bash
   kubectl run crash-demo -n diag --image=busybox:1.36 -- sh -c 'echo starting; exit 1'
   ```
   Ожидаемо: через ~20 секунд `kubectl get pod -n diag crash-demo` показывает `CrashLoopBackOff`, `RESTARTS` растёт.
4. **Разбери его.** `kubectl describe pod -n diag crash-demo` → в `Last State: Terminated Reason: Error Exit Code: 1`; в `Events` — `Back-off restarting failed container`. Затем `kubectl logs -n diag crash-demo --previous` → строка `starting`. Вывод: контейнер стартовал нормально, но приложение завершилось с кодом 1 само.
5. **Почини.** `kubectl delete pod -n diag crash-demo` и запусти с рабочей командой:
   ```bash
   kubectl run crash-demo -n diag --image=busybox:1.36 -- sh -c 'echo starting; sleep 3600'
   ```
   Ожидаемо: `Running`, `RESTARTS 0`.
6. **Сценарий B: ImagePullBackOff.** Примени YAML:
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: imagepull-demo
     namespace: diag
   spec:
     containers:
       - name: api
         image: harbor.corp.vektor.lab/vektor/does-not-exist:0.0.1
   ```
   `kubectl apply -f imagepull-demo.yaml` → статус сначала `ErrImagePull`, через несколько секунд `ImagePullBackOff`.
7. **Прочитай текст ошибки в Events.** `kubectl describe pod -n diag imagepull-demo | tail -n 15`. Текст решает всё:
   - `unauthorized` / `401` → нет или неверный `imagePullSecret`;
   - `manifest unknown` / `404` → тег или репозиторий не существует;
   - `no such host` → DNS/имя реестра;
   - `x509: certificate signed by unknown authority` → CA Harbor не в доверии у ноды;
   - `i/o timeout` → сеть/файрвол до 10.10.10.55.
   Ожидаемо у тебя: либо `unauthorized`, либо `manifest unknown`.
8. **Добавь pull secret** (логин и пароль робота берём в Harbor UI: Project `vektor` → Robot Accounts — **не выдумывай их**):
   ```bash
   kubectl create secret docker-registry harbor-pull -n diag \
     --docker-server=harbor.corp.vektor.lab \
     --docker-username='robot$vektor+ci' \
     --docker-password='<пароль робота>'
   ```
   И добавь в манифест `spec.imagePullSecrets: [{name: harbor-pull}]`, перепримени. Ожидаемо: ошибка сменилась на `manifest unknown` — значит авторизация пройдена, дело в теге. Замени тег на существующий (например `1.0.0` своего `shop-api` или `nginx:1.27` из Harbor) → `Running`.
9. **Сценарий C: OOMKilled.** Примени:
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: oom-demo
     namespace: diag
   spec:
     containers:
       - name: hog
         image: busybox:1.36
         command: ["sh", "-c"]
         args:
           - 'x=""; while true; do x="$x xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"; done'
         resources:
           requests: { memory: "8Mi", cpu: "50m" }
           limits:   { memory: "16Mi", cpu: "200m" }
   ```
   Ожидаемо: под доходит до `Running`, через несколько секунд рестартует, и через 2–3 итерации — `OOMKilled` / `CrashLoopBackOff`.
10. **Подтверди гипотезу тремя командами:** `kubectl describe pod -n diag oom-demo | grep -A3 'Last State'` → `Reason: OOMKilled, Exit Code: 137`; `kubectl top pod -n diag oom-demo` → `MEMORY` у потолка; `kubectl logs -n diag oom-demo --previous` → пусто или обрыв (ядро не даёт попрощаться).
11. **Почини.** Правь лимит на `memory: 128Mi` (requests `64Mi`), `kubectl apply -f oom-demo.yaml` — **обязательно с `kubectl delete pod` или через Deployment**, потому что `resources` у существующего пода не меняются: `kubectl delete pod -n diag oom-demo` → пересоздание → `Running`, `RESTARTS 0`.
12. **Сценарий D: Pending.** `kubectl create deployment pending-demo -n diag --image=nginx:1.27 --replicas=1` и сразу `kubectl set resources deployment/pending-demo -n diag --limits=memory=64Gi --requests=memory=64Gi`. Ожидаемо: под в `Pending`, в событиях `FailedScheduling: 0/3 nodes are available: 3 Insufficient memory`.
13. **`kubectl debug` в живом поде.** `kubectl debug -it -n diag <pod с nginx> --image=busybox:1.36 --target=nginx -- sh`, внутри: `ps aux`, `wget -qO- localhost:80 | head`. Ожидаемо: видишь процессы nginx и получаешь HTML — значит сеть и приложение живы, проблема была бы снаружи.
14. **`kubectl debug node/k8s-w01`.** `kubectl debug node/k8s-w01 -it --image=busybox:1.36 -n diag`, внутри `ls /host/var/log/pods | head`. Ожидаемо: список каталогов логов подов. Выход — `exit`; под отладки останется, удали его руками: `kubectl delete pod -n diag node-debugger-k8s-w01-xxxxx`.
15. **Запись в `stand-log.md`.** Внеси всё из раздела «Что изменилось на стенде» ниже.

### Проверка

- [ ] `kubectl get nodes` — три узла `Ready`
- [ ] `crash-demo` после починки `Running`, `RESTARTS 0`
- [ ] Ты можешь по `kubectl logs --previous` показать строку `starting` из упавшего инстанса
- [ ] `imagepull-demo` проходил обе стадии ошибок и в итоге `Running` (или удалён после разбора)
- [ ] Ты различаешь `unauthorized` и `manifest unknown` по тексту события
- [ ] В `kubectl describe pod oom-demo` был `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`
- [ ] `kubectl top pod -n diag` показывает метрики (значит metrics-server жив)
- [ ] `pending-demo` дал событие `FailedScheduling` с `Insufficient memory`
- [ ] Эфемерный контейнер запускался, `ps` внутри него видел процессы целевого контейнера
- [ ] `stand-log.md` обновлён

### Сломай и почини

**Пользователь:** ivanov (бухгалтерия, WIN11-CL01), заявка через портал
**ВМ:** кластер `k8s-cp01`/`k8s-w01`/`k8s-w02`, namespace `shop`
**Что написал:** «Портал согласования заявок отдаёт 502 Bad Gateway. Вчера работал. У всех в бухгалтерии. Платежи стоят.»
**Срочность:** Высокая

**Как воспроизвести поломку.** В `shop` есть Deployment `shop-api`, который до этого работал:

```bash
kubectl -n shop set image deployment/shop-api api=harbor.corp.vektor.lab/vektor/shop-api:9.9.9
kubectl -n shop patch deployment shop-api --type=json -p='[{"op":"remove","path":"/spec/template/spec/imagePullSecrets"}]'
```

Тег `9.9.9` в Harbor не существует, pull secret убран. Результат: поды `ImagePullBackOff`, у Service `shop-api` — пустой `Endpoints`, Ingress отдаёт 502.

**Диагностика по порядку (не перескакивай):**

1. `kubectl -n shop get pods` → новые поды `ImagePullBackOff`, `READY 0/1`. Старых уже нет (RollingUpdate прошёл, потому что старый ReplicaSet был заменён).
2. `kubectl -n shop get endpoints shop-api` → `ENDPOINTS <none>`. Подтверждает: 502 не от nginx, а от отсутствия бэкенда. Дальше копаем только поды.
3. `kubectl -n shop describe pod <pod>` → в `Events` внизу: `Failed to pull image ...: unauthorized: authentication required` **или** `manifest unknown`. Читаем буквально.
4. `kubectl -n shop get deployment shop-api -o jsonpath='{.spec.template.spec.imagePullSecrets}{"\n"}{.spec.template.spec.containers[*].image}{"\n"}'` → видим, что секрета нет и тег `9.9.9`.
5. `kubectl -n shop get secret | grep harbor` → секрета `harbor-pull` в namespace нет (или он есть, но не подключён).
6. `kubectl -n shop describe secret harbor-pull` → если есть, проверь, что `.dockerconfigjson` не пустой.
7. `kubectl -n shop rollout history deployment/shop-api` → видим предыдущую ревизию с рабочим тегом.

**Решение:**

```bash
kubectl -n shop create secret docker-registry harbor-pull \
  --docker-server=harbor.corp.vektor.lab \
  --docker-username='robot$vektor+ci' --docker-password='<пароль робота>'

kubectl -n shop patch deployment shop-api --type=merge \
  -p '{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"harbor-pull"}]}}}}'

kubectl -n shop set image deployment/shop-api api=harbor.corp.vektor.lab/vektor/shop-api:1.4.2
kubectl -n shop rollout status deployment/shop-api --timeout=180s
kubectl -n shop get endpoints shop-api
```

Если тег `1.4.2` неизвестен и торопишься вернуть сервис — `kubectl -n shop rollout undo deployment/shop-api` вернёт предыдущую ревизию за секунды. Это правильный «быстрый откат, потом разбор».

**Ответ пользователю:**

> Иван, добрый день. Портал восстановлен, проверьте, пожалуйста, доступ. Причина: при обновлении сервиса был указан несуществующий тег образа, из-за чего новые экземпляры сервиса не запускались, и портал отдавал 502. Мы вернули рабочую версию, а в процесс сборки добавили проверку тега, чтобы такое не повторялось. Если увидите ошибку снова — сразу пишите в этот тикет.

**Закрытие заявки:** категория «Kubernetes / деплой», решение — «исправлен imagePullSecret и тег образа, выполнен успешный rollout», корневая причина — «в CI передавался тег, не публиковавшийся в Harbor; отсутствовал `imagePullSecrets` в манифесте». Поставь себе задачу: добавить в CI шаг проверки существования тега (`crane manifest` / `skopeo inspect`) — это и есть переход от «тушим» к «не горит».

### Что изменилось на стенде

- Namespace `diag` создан.
- В `diag` живут поды `crash-demo` (working), `imagepull-demo` (рабочий тег), `oom-demo` (лимит 128Mi), `pending-demo` (Deployment, висит в Pending — можно удалить).
- В `diag` создан Secret `harbor-pull` типа `kubernetes.io/dockerconfigjson` для `harbor.corp.vektor.lab`.
- Namespace `shop`: Deployment `shop-api` с подключённым `imagePullSecrets: harbor-pull` и рабочим тегом `1.4.2`; секрет `harbor-pull` также скопирован в `shop`.
- Узлы кластера не менялись. Ручных правок на нодах нет.

### Откат

1. VMware Workstation → выбери ВМ `k8s-cp01` → Snapshot Manager → `before-devops-045` → **Revert**.
2. Повтори на `k8s-w01`, `k8s-w02` (и `HARBOR01`, если трогал).
3. Запусти ВМ, дождись загрузки, проверь: `kubectl get nodes` — `Ready`, `kubectl get ns` — `diag` отсутствует.
4. В `stand-log.md` допиши строку: «45: откат к before-devops-045, стенд в состоянии до лабораторной».

## 6. Ошибки новичков и безопасность

**Ошибка 1. Начинать с `kubectl delete pod`.** → Под исчезает вместе со всеми уликами: событиями, `lastState`, логами прошлого инстанса. → Сначала `describe` + `logs --previous` + `get events`, и только потом пересоздание.

**Ошибка 2. Смотреть только логи.** → Пустой лог у `ImagePullBackOff`, `CreateContainerConfigError` и `Pending` — это норма, контейнер не стартовал, писать некому. → Сначала события, логи только когда контейнер хотя бы раз запускался.

**Ошибка 3. Забыть про `--previous`.** → В `CrashLoopBackOff` текущий лог либо пустой, либо содержит `Back-off restarting failed container`, и причина смерти не видна. → Всегда `kubectl logs <pod> --previous` при рестартах.

**Ошибка 4. Лечить `OOMKilled` поднятием лимита без разбора.** → Утечка памяти съест и новый лимит, а на ноде кончится память и начнут падать соседние поды. → Сначала `kubectl top pod`, потом решение: лимит мал (поднять) или утечка (задача разработке). Помни про JVM: без `-XX:MaxRAMPercentage=75` куча может не знать про cgroup-лимит.

**Ошибка 5. Считать `CrashLoopBackOff` отдельной болезнью.** → Это не причина, а следствие: код выхода 1/137/127 и `Last State` — это и есть причина. → Всегда иди от кода выхода.

**Ошибка 6. Правки через `kubectl exec` и `kubectl edit`.** → Дрейф конфигурации: под пересоздался — правка исчезла, и никто не знает, что было. → Всё через Git + CI (`GIT01`), `kubectl apply` из GitOps-подхода.

**Ошибка 7. `kubectl debug node/...` «по-быстрому на проде».** → Под получает host PID/network и корень ноды, то есть фактический root на узле без RBAC-контроля и без следа в аудите приложения. → Только через одобренный runbook, с ограниченным RBAC и с аудит-логами apiserver.

**Ошибка 8. `--profile=sysadmin` по привычке.** → Privileged-контейнер в поде = обход Pod Security Standards и потенциальный побег на ноду. → По умолчанию профиль `general`; `sysadmin` — только когда реально нужен доступ к устройствам, и только на одноразовом стенде.

**Безопасность.** В реальной среде: (1) не выкладывай `kubectl get secret -o yaml` и `describe` с переменными окружения в тикеты и Slack — секреты утекают именно так; (2) эфемерные контейнеры нельзя удалить без пересоздания пода, и они остаются в спецификации — за этим должен следить процесс; (3) `kubectl logs` может содержать персональные данные и токены — логирование на стенде (`Lab#2026-vektor`) учебное, в проде так нельзя; (4) права на `pods/exec`, `pods/portforward` и `pods/ephemeralcontainers` — это фактически доступ к продовым данным, их выдают точечно и на время (RBAC — урок 41).

## 7. Вопросы на собеседовании

**Вопрос 1. Под в `CrashLoopBackOff`. Твои действия по шагам?**

**Ответ.** Сначала `kubectl get pod -o wide` — смотрю `RESTARTS` и ноду, понимаю скорость цикла. Потом `kubectl describe pod` — блок `Last State`: `Reason` и `Exit Code`. Код 137 → OOMKilled или SIGKILL, проверяю `Limits.memory` и `kubectl top pod`. Код 1 → проблема в приложении, иду в `kubectl logs --previous`. Код 127/126 → опечатка в `command`/`args` или нет прав на файл. Параллельно `kubectl get events --field-selector involvedObject.name=<pod>` — там `Back-off restarting failed container` и всё, что было до старта: монтирование ConfigMap/Secret, ошибки проб. Отдельно проверяю `livenessProbe`: если она бьёт по `/health` с `initialDelaySeconds: 0`, приложение просто не успевает подняться и его убивают.

**Что хочет услышать интервьюер:** системный порядок (статус → describe → exit code → логи → события), знание `--previous` и понимание, что CrashLoop — следствие.

**Красный флаг:** «ну я перезапускаю под, обычно помогает» или сразу `kubectl logs` без `--previous`.

**Вопрос 2. Чем `ImagePullBackOff` отличается от `ErrImagePull`?**

**Ответ.** Это две стадии одного процесса. `ErrImagePull` — kubelet попытался скачать образ и получил ошибку прямо сейчас. Дальше включается backoff, и статус становится `ImagePullBackOff` — «повторю позже с нарастающей паузой». Причины одинаковые: неверный тег/репозиторий (`manifest unknown`), нет прав (`unauthorized`/`401` — не подключён `imagePullSecret` или истёк робот-аккаунт), DNS (`no such host`), TLS (`x509: certificate signed by unknown authority`), сеть/файрвол до реестра (`i/o timeout`). Лечится по тексту события в `describe`, а не догадками.

**Что хочет услышать интервьюер:** понимание, что «PullBackOff» — это ретрай, и умение читать текст ошибки реестра, а не только название статуса.

**Красный флаг:** «надо пересобрать образ» без единого слова про события и `imagePullSecrets`.

**Вопрос 3. Под `OOMKilled`, а на ноде свободной памяти 30 ГБ. Как так?**

**Ответ.** OOMKilled срабатывает на уровне cgroup контейнера, а не ноды. `limits.memory` транслируется в `memory.max` cgroup v2; при превышении ядро убивает процесс внутри контейнера, и под получает `Reason: OOMKilled`, `Exit Code: 137`. Свободная память ноды тут вообще не участвует. Отдельный сценарий — OOM на уровне ноды (kubelet eviction или системный OOM killer), тогда в `dmesg` видно `Killed process`, и страдают все поды ноды. Различаю по `kubectl describe pod` (`OOMKilled` в `Last State`) против `dmesg`/`journalctl` на ноде.

**Что хочет услышать интервьюер:** понимание cgroups и разницы между контейнерным и узловым OOM.

**Красный флаг:** «значит ему не хватило памяти на сервере, надо добавить RAM на ноду».

**Вопрос 4. `kubectl exec` или `kubectl debug`? Когда что?**

**Ответ.** `exec` — когда контейнер жив и в образе есть нужный shell/утилиты; это самый быстрый путь. `kubectl debug` нужен в трёх случаях: (1) контейнер не стартует или падает слишком быстро для `exec` — тогда `--copy-to` создаёт отладочную копию пода; (2) в образе нет shell (distroless, scratch) — тогда эфемерный контейнер с `busybox` и `--target` даёт общий PID-namespace с целевым контейнером; (3) нужен доступ к ноде — `kubectl debug node/<node>` монтирует корень в `/host`. Важно: эфемерные контейнеры не удаляются без пересоздания пода и не переживают рестарт, а `--profile=sysadmin` делает их привилегированными — это отдельный риск и отдельное согласование.

**Что хочет услышать интервьюер:** что ты знаешь про distroless и не предлагаешь «поставить bash в прод-образ», и что понимаешь риски `debug node`.

**Красный флаг:** «kubectl debug — это то же самое, что exec, просто новее».

**Вопрос 5. Почему `kubectl get events` не показывает вчерашние события? Где тогда искать?**

**Ответ.** Потому что Events — обычные объекты в etcd с TTL: у `kube-apiserver` есть `--event-ttl`, по умолчанию 1 час, после чего они удаляются. Плюс на больших кластерах их легко потерять в общем потоке. Поэтому «вчерашнее» ищут не в `get events`, а в централизованном хранилище: логи приложений и системных компонентов (Loki/ELK/OpenSearch), метрики (Prometheus + Zabbix на MON01), аудит apiserver (`--audit-policy-file`), и события, экспортированные через event-exporter или `kubectl get events -w` в долгоживущий коллектор. Для постмортема также важны алерты и дашборды — они фиксируют момент деградации.

**Что хочет услышать интервьюер:** понимание, что встроенная диагностика Kubernetes — «здесь и сейчас», а для истории нужен отдельный контур наблюдаемости.

**Красный флаг:** «события хранятся вечно» или «в etcd же всё есть».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Tasks → Monitoring, Logging, and Debugging*: статьи «Debug Pods», «Debug Running Pods», «Determine the Reason for Pod Failure», «Troubleshooting Clusters». Поиск: `kubernetes debug pods failure site:kubernetes.io` `[ПРОВЕРИТЬ актуальные названия статей]`
- **kubernetes.io** — *Concepts → Workloads → Pods → Pod Lifecycle* (статусы `Pending`, `Running`, `CrashLoopBackOff`, `ImagePullBackOff`, restart policy и backoff). Поиск: `kubernetes pod lifecycle container states`
- **kubernetes.io** — *Reference → Command line tool (kubectl)*: `kubectl debug`, `kubectl logs`, `kubectl describe`, `kubectl events`. Поиск: `kubectl debug reference`
- **kubernetes.io** — *Tasks → Configure Pods and Containers → Assign Memory Resources to Containers and Pods* (как работает `limit` и что бывает при превышении). Поиск: `kubernetes assign memory resources oom`
- **kubernetes.io** — *Concepts → Cluster Administration → Node-pressure Eviction* и *Pod Quality of Service Classes*. Поиск: `kubernetes node pressure eviction oom`
- **containerd.io** — документация по `crictl` и конфигурации рантайма. Поиск: `crictl logs containerd`
- **github.com/kubernetes-sigs/cri-tools** — репозиторий `crictl`, раздел про `crictl.yaml` и `--runtime-endpoint` `[ПРОВЕРИТЬ актуальность раздела]`
- **vmware.com** — документация VMware Workstation: раздел про snapshots и Snapshot Manager. Поиск: `VMware Workstation take snapshot documentation`
- **harbor.goharbor.io / goharbor.io** — документация Harbor, разделы «Robot Accounts» и «Pull an image from a private registry». Поиск: `harbor robot account pull secret kubernetes`

Где стоит уточнить: точный адрес, под которым в твоём стенде опубликован Harbor, и точные имена проекта/робот-аккаунта — они зависят от того, как ты настраивал HARBOR01 в уроке про реестр. Эти значения в стенде не зафиксированы, подставь свои и впиши их в `stand-log.md`.
