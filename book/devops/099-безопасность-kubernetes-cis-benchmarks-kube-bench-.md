# Безопасность Kubernetes: CIS Benchmarks, kube-bench, Pod Security, NetworkPolicy, runtime-защита

## 1. Введение (зачем это нужно в реальной работе)

Тикет **INC-2026-099**. От: ivanov (бухгалтерия, WIN11-CL01). Текст: «Коллеги, к нам приходил аудитор по платёжному сервису. Он сказал, что в кластере наши контейнеры работают от root, между собой видят всё и никто не проверяет, что в них происходит. У меня из-за этого 1С не отвалится? Срочность: средняя, но лучше сегодня».

Кластер Kubernetes — это уже не «ещё одна машина», это десятки процессов, каждый со своими правами, сетью и файловой системой. И проверять его «на глазок» нельзя: нужны формальные чек-листы, механизмы, которые не дают развернуть опасную конфигурацию, сегментация сети и детектор подозрительного поведения в рантайме. Именно эти четыре слоя мы разбираем: соответствие эталону (**CIS Benchmark** + **kube-bench**), допуск подов (**Pod Security Admission**), сеть (**NetworkPolicy**) и поведение в момент работы (**Falco**, seccomp, capabilities).

После главы ты сможешь: прогнать кластер по CIS-эталону и объяснить отчёт, повесить на namespace политику restricted и не сломать при этом kube-system, написать default-deny NetworkPolicy с точечными разрешениями, поставить runtime-детектор и отличить настоящий инцидент от шума. И всё это — в виде YAML в Git, а не «руками подкрутил и забыл».

Тема почти всегда приходит в работу не как «сделай безопасно», а как «аудит сказал», «клиент требует соответствия», «после инцидента». Уметь разговаривать с аудитом и с разработчиками на одном языке — половина этой работы.

## 2. Теория «под капотом»

Kubernetes защищают на четырёх независимых уровнях. Уровни не заменяют друг друга: дыра в одном не закрывается другим.

```text
┌────────────────── Уровни защиты кластера ──────────────────┐

 1. КОНФИГ УЗЛА И CONTROL PLANE
    файлы /etc/kubernetes/*.conf (права 600), флаги kube-apiserver,
    kubelet (--anonymous-auth), etcd, шифрование секретов в etcd
    → проверяется CIS Benchmark, автоматизируется kube-bench (Go, читает
      файлы через hostPath и сверяет с чек-листом)

 2. ДОПУСК ПОДА (admission)
    kube-apiserver → PodSecurity (встроен, GA с 1.25)
    профили: privileged → baseline → restricted
    → либо под создаётся, либо «Forbidden: violates PodSecurity»

 3. СЕТЬ
    CNI (Calico / Cilium / ...) реализует NetworkPolicy
    L3/L4: кто с кем и на какой порт. По умолчанию — всё разрешено!

 4. РАНТАЙМ
    containerd → seccomp (syscalls) / AppArmor / capabilities
    Falco (eBPF или kernel module) слушает события ядра:
    exec в контейнере, чтение /etc/shadow, открытие сокета наружу
```

**CIS Benchmark** — эталон безопасной конфигурации от Center for Internet Security. Для Kubernetes он разбит на разделы: control plane, etcd, kubelet, политики, managed-сервисы. Внутри проверки с уровнями: **L1** — базовый, не ломает приложения; **L2** — жёсткий, требует переделки приложений. Часть проверок **scored** (оценивается), часть **not scored** (контекст, например «есть ли отдельный API-сервер у managed-кластера»).

**kube-bench** — утилита Aqua Security, которая читает конфиги нод и сравнивает их с CIS. Она **ничего не исправляет**, только показывает PASS/FAIL/WARN и путь к файлу/флагу. Запускается либо как бинарь на ноде, либо как Job в кластере (тогда монтирует hostPath `/etc/kubernetes`, `/var/lib/kubelet`, `/etc/systemd`).

**Pod Security Admission (PSA)** — встроенный (с 1.25 стабильный) admission-контроллер на уровне namespace. Включается метками `pod-security.kubernetes.io/enforce|warn|audit=<профиль>` и `...-version=<v1.xx>`. Заменил собой **PodSecurityPolicy**, который объявили deprecated в 1.21 и выпилили в 1.25.

**NetworkPolicy** — объект API `networking.k8s.io/v1`. Сам по себе он ничего не делает: его реализует CNI-плагин. Calico и Cilium умеют, «голый» Flannel — нет (трафик будет ходить, сколько политик ни пиши). Логика важная: как только на под действует **хотя бы одна** политика с `policyTypes: Ingress`, весь входящий трафик, не описанный в правилах, отбрасывается. Классическая схема — «default deny в namespace + точечные allow».

**Runtime-защита** начинается с `securityContext` пода: `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem`, `seccompProfile: RuntimeDefault`, `appArmor`. А заканчивается детектором поведения — чаще всего **Falco**: он собирает системные вызовы и матчит их на YAML-правила, например «в контейнере запустили shell» или «запись в /etc».

## 3. Аналогия из жизни

Кластер — это жилой дом, который вы сдаёте жильцам (подам).

**CIS Benchmark** — строительные нормы и правила: какая толщина стен, где должны висеть огнетушители, какой замок на подвале. **kube-bench** — приёмщик с чек-листом и фонариком: он ходит по дому, сверяет с нормами и пишет акт «пункт 4.2.1 — не выполнено», но сам замок не меняет — это делаешь ты. **Pod Security** — правила заселения: квартиру нельзя сдавать под склад пиротехники (privileged), у жильца не должно быть ключей от всего дома (root), входная дверь должна закрываться на замок, а не на палочку. Пропуска выдают на входе (admission) — если не проходишь, тебя не пустят в квартиру вообще. **NetworkPolicy** — домофон и внутренние пропуска: по умолчанию из своей квартиры ты не можешь зайти в чужую, и только консьерж явно перечислил, кому к тебе можно. **Falco** — тот же консьерж плюс камеры: он не мешает тебе жить, но если в три ночи в квартире кто-то ломает стену — звонит в полицию.

Где аналогия ломается: настоящий дом не проверяет личность каждого жильца при каждом переходе между комнатами — а в Kubernetes именно это делает mTLS и service mesh (следующая глава), и NetworkPolicy их не заменяет.

## 4. Практика и команды

Все примеры — для кластера на `k8s-cp01` / `k8s-w01` / `k8s-w02` (Ubuntu Server, kubeadm). Версию своего кластера проверяй сам: `kubectl version`.

**1. Посмотреть кластер и CNI — есть ли вообще чем реализовывать политики.**

```bash
kubectl get nodes -o wide
kubectl get pods -n kube-system -o wide
kubectl get daemonsets -n kube-system
```

В выводе ищи `calico-node`, `cilium`, `kube-flannel`. Если последнее — NetworkPolicy работать не будет, нужен Calico/Cilium. Смотри `READY` и `NODE` у DaemonSet: один под на каждую ноду.

**2. Прогнать kube-bench прямо на control-plane (без Job).**

```bash
sudo /usr/local/bin/kube-bench run --targets master
sudo /usr/local/bin/kube-bench run --targets node
```

`--targets` выбирает набор проверок: `master`, `node`, `etcd`, `policies`, `controlplane`. Бинарь берётся из релизов Aqua Security `[ПРОВЕРИТЬ актуальную версию]`.

Типичный (сокращённый) вывод:

```text
[INFO] 1 Control Plane Security Configuration
[FAIL] 1.2.1 Ensure that the --anonymous-auth argument is set to false
[PASS] 1.2.9 Ensure that the --profiling argument is set to false
[WARN] 1.3.2 Ensure that the --profiling argument is set to false (etcd)
== Summary master ==
43 checks PASS
9 checks FAIL
5 checks WARN
```

Смотреть надо на строки `[FAIL]`: это конкретные файлы и флаги, которые можно поправить.

**3. Запустить kube-bench как Job — так удобнее собирать отчёт централизованно.**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: kube-bench
  namespace: kube-system
spec:
  template:
    spec:
      hostPID: true
      nodeSelector:
        node-role.kubernetes.io/control-plane: ""
      tolerations:
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
        effect: NoSchedule
      containers:
      - name: kube-bench
        image: 10.10.10.55/library/kube-bench:v0.9.0   # зеркало в Harbor [ПРОВЕРИТЬ тег]
        command: ["kube-bench", "run", "--targets", "master"]
        volumeMounts:
        - {name: etc-k8s,  mountPath: /etc/kubernetes, readOnly: true}
        - {name: lib-kubelet, mountPath: /var/lib/kubelet, readOnly: true}
        - {name: etc-systemd, mountPath: /etc/systemd, readOnly: true}
        - {name: lib-systemd, mountPath: /lib/systemd, readOnly: true}
      restartPolicy: Never
      volumes:
      - {name: etc-k8s,     hostPath: {path: /etc/kubernetes}}
      - {name: lib-kubelet, hostPath: {path: /var/lib/kubelet}}
      - {name: etc-systemd, hostPath: {path: /etc/systemd}}
      - {name: lib-systemd, hostPath: {path: /lib/systemd}}
```

```bash
kubectl apply -f kube-bench-job.yaml
kubectl logs -n kube-system job/kube-bench > kube-bench-$(date +%F).txt
```

**4. Проверить права на ключевые файлы — самая частая находка CIS.**

```bash
sudo stat -c '%a %U %G %n' /etc/kubernetes/admin.conf \
  /etc/kubernetes/kubelet.conf /etc/kubernetes/controller-manager.conf
sudo grep -E 'anonymous|authorization' /var/lib/kubelet/config.yaml
```

Ожидаемо: права `600` и владелец `root:root`; в kubelet — `anonymous: {enabled: false}` и `authorization: {mode: Webhook}`. Если `admin.conf` читает весь мир — это фактически root кластера для любого пользователя ноды.

**5. Создать namespace с жёстким профилем Pod Security.**

```bash
kubectl create namespace payment
kubectl label --dry-run=server --overwrite namespace payment \
  pod-security.kubernetes.io/enforce=restricted
kubectl label namespace payment \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/warn=restricted \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/enforce-version=latest
kubectl get ns payment -o jsonpath='{.metadata.labels}' | tr ',' '\n'
```

`--dry-run=server` — очень полезный трюк: сервер покажет предупреждения для уже существующих подов, но метку не поставит. `enforce` блокирует, `warn` пишет предупреждение клиенту, `audit` сыплет записи в audit-лог. `-version` фиксирует версию профиля, чтобы обновление кластера не сломало прод неожиданно.

**6. Правильный securityContext — эталон пода в namespace `payment`.**

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 101
    runAsGroup: 101
    seccompProfile:
      type: RuntimeDefault
  containers:
  - name: web
    image: 10.10.10.55/library/nginx-unprivileged:1.27   # слушает 8080, не 80 [ПРОВЕРИТЬ тег]
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
    volumeMounts:
    - {name: cache, mountPath: /var/cache/nginx}
    - {name: tmp,   mountPath: /tmp}
```

`drop: [ALL]` убирает даже `NET_RAW` и `CHOWN`; `readOnlyRootFilesystem` требует emptyDir для всего, куда приложение пишет.

**7. Убедиться, что плохой под не пролезет.**

```bash
kubectl run bad --image=10.10.10.55/library/busybox:1.36 -n payment \
  --overrides='{"spec":{"containers":[{"name":"bad","image":"10.10.10.55/library/busybox:1.36","securityContext":{"privileged":true}}]}}' \
  -- sleep 3600
```

Ожидаемо:

```text
Error from server (Forbidden): pods "bad" is forbidden: violates PodSecurity
"restricted:latest": privileged (container "bad" must not set
securityContext.privileged=true), allowPrivilegeEscalation != false ...
```

**8. Default-deny: закрыть весь трафик в namespace.**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: payment
spec:
  podSelector: {}
  policyTypes: ["Ingress", "Egress"]
```

`podSelector: {}` = «все поды namespace», пустой список правил = «ничего не разрешено».

**9. Разрешить DNS — иначе всё умрёт на резолве имён.**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: payment
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
    ports:
    - {protocol: UDP, port: 53}
    - {protocol: TCP, port: 53}
```

Метка `kubernetes.io/metadata.name` автоматически ставится на каждый namespace — на неё удобно ссылаться.

**10. Точечное разрешение: к API ходит только фронтенд.**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-allow-frontend
  namespace: payment
spec:
  podSelector:
    matchLabels:
      app: payment-api
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: payment-web
    ports:
    - {protocol: TCP, port: 8080}
```

`podSelector` внутри `from` без `namespaceSelector` = «поды в этом же namespace».

**11. Проверить политику с временного пода.**

```bash
kubectl run nettest -n payment --rm -it --restart=Never \
  --image=10.10.10.55/library/netshoot:latest -- \
  curl -m 3 -sS http://payment-api:8080/healthz
kubectl get networkpolicy -n payment
kubectl describe networkpolicy default-deny-all -n payment
```

Таймаут вместо ответа — это и есть работающая политика. `describe` показывает отрендеренные селекторы — первое, что смотрят при разборе «а почему не ходит».

**12. Поставить Falco и увидеть алерт.**

```bash
helm repo add falcosecurity https://falcosecurity.github.io/charts
helm repo update
helm install falco falcosecurity/falco -n falco --create-namespace \
  --set driver.kind=modern_ebpf --set tty=true
kubectl -n falco get pods
kubectl -n falco logs daemonset/falco -f
```

В соседнем терминале:

```bash
kubectl exec -it deploy/payment-api -n payment -- sh
```

В логах Falco в течение секунды появляется правило `Terminal shell in container` с полями `k8s.ns.name=payment`, `proc.name=sh`. `driver.kind` зависит от ядра: `ebpf`, `modern_ebpf` или `kmod` `[ПРОВЕРИТЬ под свою версию ядра Ubuntu]`.

**13. Включить аудит API-сервера (коротко).**

```text
# /etc/kubernetes/audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
- level: Metadata
  namespaces: ["payment"]
- level: RequestResponse
  verbs: ["create","delete"]
  resources: [{group: "", resources: ["pods"]}]
```

Флаг `--audit-policy-file` и `--audit-log-path` добавляются в манифест kube-apiserver в `/etc/kubernetes/manifests/`. После правки static pod перезапускается сам, control plane живёт 30–60 секунд [ПРОВЕРИТЬ на своём стенде, что API успел подняться].

## 5. Лабораторная работа

### Предусловия

- Готовый кластер с уроков модуля: `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63), Ubuntu Server, kubeadm, рабочий `kubectl` с `k8s-cp01`.
- CNI с поддержкой NetworkPolicy (Calico или Cilium). Если стоит Flannel — сначала поставь Calico, иначе шаги 6–8 не дадут эффекта.
- `HARBOR01` (10.10.10.55) доступен, в нём есть проект `library` с образами (см. уроки по реестру).
- `GIT01` (10.10.10.50) с репозиторием `devops/k8s-security`.
- `helm` на `k8s-cp01`.

### Снапшот

В VMware Workstation на **каждой** из ВМ кластера (`k8s-cp01`, `k8s-w01`, `k8s-w02`): меню **VM → Snapshot → Take Snapshot**, имя `before-devops-099`, описание «до лабораторной по безопасности k8s». Одного снапшота на control-plane недостаточно: Falco ставится DaemonSet'ом на все ноды.

### Шаги

1. **На `k8s-cp01`.** `kubectl get nodes -o wide` и `kubectl get ds -n kube-system`. Ожидаемо: три ноды `Ready`, среди DaemonSet'ов есть `calico-node` или `cilium` с `DESIRED=3, READY=3`. Если CNI не умеет политики — стоп, сначала меняем CNI.
2. **На `k8s-cp01`.** Применяем `kube-bench-job.yaml` из раздела 4 и снимаем лог: `kubectl logs -n kube-system job/kube-bench > ~/kube-bench-before.txt`. Ожидаемо: файл с секцией `== Summary master ==` и списком `[FAIL]`. Записываем число FAIL в `stand-log.md`.
3. **На `k8s-cp01`.** `sudo stat -c '%a %U %G %n' /etc/kubernetes/admin.conf` — если не `600 root root`, выполняем `sudo chmod 600 /etc/kubernetes/admin.conf`. Ожидаемо: повторный `stat` показывает `600`.
4. **На `k8s-cp01`.** В `/var/lib/kubelet/config.yaml` выставляем `anonymous: {enabled: false}` и `authorization: {mode: Webhook}` (если отличается), затем `sudo systemctl restart kubelet` на каждой ноде (`k8s-cp01`, `k8s-w01`, `k8s-w02`). Ожидаемо: `kubectl get nodes` через минуту снова `Ready`.
5. **На `k8s-cp01`.** Повторяем kube-bench: `kubectl delete job kube-bench -n kube-system && kubectl apply -f kube-bench-job.yaml`, лог в `~/kube-bench-after.txt`, `diff` двух файлов. Ожидаемо: число `[FAIL]` уменьшилось хотя бы на 2–3, в diff видны строки `PASS` там, где были `FAIL`.
6. **На `k8s-cp01`.** `kubectl create namespace payment` и навешиваем метки PSA `enforce/warn/audit=restricted` (команды из п.5 раздела 4). Ожидаемо: `kubectl get ns payment --show-labels` показывает три метки.
7. **На `k8s-cp01`.** Разворачиваем `payment-web` и `payment-api` (nginx-unprivileged) с полным securityContext из п.6. Ожидаемо: `kubectl get pods -n payment` — `Running`, `kubectl get deploy -n payment` — `READY 1/1`.
8. **На `k8s-cp01`.** Пытаемся запустить privileged-под (команда из п.7). Ожидаемо: `Error from server (Forbidden) ... violates PodSecurity "restricted:latest"`. Сохраняем текст ошибки в `stand-log.md`.
9. **На `k8s-cp01`.** Применяем `default-deny-all` и `allow-dns-egress`, затем `api-allow-frontend`. Ожидаемо: `kubectl get netpol -n payment` — три объекта.
10. **На `k8s-cp01`.** Проверяем: из временного пода в namespace `default` — `curl` до `payment-api` даёт таймаут; из пода с меткой `app=payment-web` в `payment` — отвечает 200/404. Ожидаемо: поведение различается, это и есть сегментация.
11. **На `k8s-cp01`.** Ставим Falco через helm (п.12), ждём `Running` на всех трёх нодах. Ожидаемо: `kubectl -n falco get pods` — три пода в статусе `Running`.
12. **На `k8s-cp01`.** Открываем `kubectl -n falco logs daemonset/falco -f`, во втором терминале — `kubectl exec -it deploy/payment-api -n payment -- sh`. Ожидаемо: алерт про `Terminal shell in container` с namespace `payment`.
13. **На `k8s-cp01`.** Складываем всё в Git: манифесты, job, отчёты kube-bench, скриншот/лог алерта Falco — в репозиторий `devops/k8s-security` на `GIT01`, коммит `lab-099: k8s hardening + netpol + falco`.

### Проверка

- [ ] `kubectl get nodes` — все три ноды `Ready`, поды kube-system без CrashLoopBackOff.
- [ ] В `~/kube-bench-after.txt` строк `[FAIL]` меньше, чем в `~/kube-bench-before.txt`.
- [ ] `kubectl get ns payment --show-labels` содержит `pod-security.kubernetes.io/enforce=restricted`.
- [ ] `payment-web` и `payment-api` в статусе `Running` и отвечают друг другу.
- [ ] Privileged-под получает отказ admission, текст ошибки сохранён.
- [ ] `kubectl get netpol -n payment` — три политики; curl из чужого namespace не проходит.
- [ ] Falco в статусе `Running` на трёх нодах и выдал хотя бы один алерт `Terminal shell in container`.
- [ ] В `stand-log.md` и в Git на `GIT01` есть запись о том, что изменилось.

### Сломай и почини

**Пользователь:** ivanov
**ВМ:** WIN11-CL01
**Что написал:** «После ваших вчерашних работ платёжный сервис в кластере не открывается — страница думает 30 секунд и отдаёт 504. До этого работало. У нас отчёт, срочно».
**Срочность:** высокая.

**Как воспроизвести поломку.** На `k8s-cp01` удалить разрешающую политику и оставить только `default-deny`:

```bash
kubectl delete networkpolicy api-allow-frontend -n payment
kubectl get ingress -A          # находим ingress-controller (обычно ns ingress-nginx)
```

Дополнительно можно добавить политику, блокирующую ingress в namespace ingress-controller — тогда 504 получат все сервисы сразу, что делает поломку правдоподобной.

**Диагностика по шагам.**

1. Что вообще изменилось: `git log --oneline -5` в `devops/k8s-security` на `GIT01` и `cat stand-log.md`. Почти всегда ответ уже там — «вчера применяли default-deny».
2. Живы ли поды: `kubectl get pods -n payment -o wide`. Если `Running`, значит проблема не в приложении, а в сети.
3. Список политик в namespace: `kubectl get netpol -n payment` и `kubectl describe netpol default-deny-all -n payment` — смотрим `Allowing ingress traffic: <none>`. Это приговор.
4. Проверка с временного пода **внутри** namespace: `kubectl run nettest -n payment --rm -it --restart=Never --image=10.10.10.55/library/netshoot:latest -- curl -m 3 -v http://payment-api:8080`. Если изнутри отвечает, а снаружи нет — режем ingress на границе.
5. Логи ingress-controller: `kubectl logs -n ingress-nginx deploy/ingress-nginx-controller --tail=50` — там будут `upstream timed out`, что подтверждает: пакет ушёл, ответа нет.
6. Только после этого — правка: вернуть `api-allow-frontend` и добавить разрешение от namespace ingress-controller.

**Решение.**

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-allow-ingress-controller
  namespace: payment
spec:
  podSelector:
    matchLabels:
      app: payment-api
  policyTypes: ["Ingress"]
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: ingress-nginx
    ports:
    - {protocol: TCP, port: 8080}
```

```bash
kubectl apply -f api-allow-ingress-controller.yaml
kubectl run nettest -n payment --rm -it --restart=Never \
  --image=10.10.10.55/library/netshoot:latest -- curl -m 3 -sS http://payment-api:8080
```

**Что написать пользователю:** «Иван, добрый день. Причина — вчерашняя политика сетевой изоляции в namespace payment: она закрыла доступ от ingress-контроллера к сервису. Добавил точечное разрешение только для ingress, остальной трафик остаётся закрытым. Проверил: сервис открывается, отчёт можно сдавать. Если снова 504 — сразу пишите, разберём».

**Закрытие заявки:** в комментарии — что было (NetworkPolicy без allow), что сделали (добавили `api-allow-ingress-controller`), как проверили (curl + логи ingress), ссылка на коммит в GitLab и запись в `stand-log.md`. Тип: change-induced incident, виновник — плановое изменение, не пользователь.

### Что изменилось на стенде

- На `k8s-cp01`, `k8s-w01`, `k8s-w02` исправлены права на `/etc/kubernetes/admin.conf` (600) и параметры kubelet (`anonymous-auth: false`, `authorization: Webhook`).
- В кластере появился namespace `payment` с PSA-метками `restricted`.
- Развёрнуты `Deployment/payment-web` и `Deployment/payment-api` с securityContext restricted.
- В `payment` три NetworkPolicy: `default-deny-all`, `allow-dns-egress`, `api-allow-ingress-controller` (и удалённая в ходе разбора `api-allow-frontend` — вернуть при желании).
- В namespace `falco` стоит Helm-релиз `falco` (DaemonSet на трёх нодах, driver `modern_ebpf`).
- Включён аудит API-сервера, политика `/etc/kubernetes/audit-policy.yaml`.
- В GitLab на `GIT01` появился репозиторий `devops/k8s-security` с манифестами и отчётами `kube-bench-before/after`.
- В `stand-log.md` — запись за `before-devops-099`.

### Откат

Полный: VMware Workstation → **VM → Snapshot Manager** → выбрать `before-devops-099` → **Restore Snapshot** на каждой из трёх ВМ. Кластер вернётся к состоянию до главы целиком, вместе с CNI и отсутствием Falco.

Частичный (если снапшоты трогать не хочется):

```bash
kubectl delete namespace payment
helm uninstall falco -n falco && kubectl delete namespace falco
kubectl delete job kube-bench -n kube-system
```

Правки в `/var/lib/kubelet/config.yaml` и флаги API-сервера откатываются вручную по сделанным заранее копиям: `sudo cp /var/lib/kubelet/config.yaml /var/lib/kubelet/config.yaml.bak` перед изменением — обязательный шаг.

## 6. Ошибки новичков и безопасность

1. **Навесить `enforce=restricted` на `kube-system` или на весь кластер разом** → падают CNI, ingress, coredns, кластер становится неуправляемым. Правильно: только на прикладные namespace, сначала `warn`/`audit`, потом `enforce`, и всегда с `--dry-run=server` перед применением.
2. **Написать NetworkPolicy и не проверить CNI** → политика есть в API, трафик ходит. Правильно: сначала `kubectl get ds -n kube-system`, убедиться, что стоит Calico/Cilium, и только потом писать политики.
3. **Default-deny без разрешения DNS** → приложение «висит» на 30 секунд и отдаёт 500/504, потому что не резолвит имя БД. Правильно: сразу вместе с `default-deny` применять `allow-dns-egress` — это шаблон, а не забывчивость.
4. **Считать kube-bench «инструментом починки»** → отчёт лежит, кластер не меняется. Правильно: kube-bench только диагностирует; каждую находку превращаем в тикет/коммит, а часть проверок (managed-кластер, нерелевантные плагины) помечаем как принятый риск с обоснованием.
5. **Включать `privileged: true` или `hostPath: /` «чтобы работало»** → контейнер получает фактически права root на ноду. Правильно: разбираться, зачем приложению привилегия; в 95% случаев хватает `drop: [ALL]` + нужная capability (`NET_BIND_SERVICE` вместо привилегированного порта).
6. **Поставить Falco и не настроить алертинг** → детектор пишет в лог, который никто не читает. Правильно: Falco → stdout → log-шип в Loki/ELK → алерт в telegram/почту по критичным правилам; и обязательно фильтровать шум, иначе через неделю правила отключат.
7. **Забыть про `readOnlyRootFilesystem`** → приложение пишет временные файлы в слой контейнера, при перезапуске теряет состояние и живёт с лишними правами. Правильно: `readOnlyRootFilesystem: true` + emptyDir для конкретных каталогов.
8. **Хранить секреты в манифестах в Git** → утечка при первом же клонировании репозитория. Правильно: Secret из Vault/External Secrets (урок 96), а в Git — только ссылки.

**Безопасность.** В боевой среде нельзя: запускать kube-bench Job в namespace с широкими правами и оставлять его там (ему нужны `hostPath` и `hostPID` — это потенциальный побег на ноду, удаляйте Job сразу после сбора отчёта); ставить Falco без ограничения прав его ServiceAccount; отключать audit-log «потому что диск забивается» — вместо этого настраивается ротация; выдавать разработчикам `cluster-admin` «на время отладки». И помните: `drop: [ALL]` и `restricted` часто ломают старые образы, которые писали в `/` под root, — это не повод отключать политику, это повод пересобрать образ.

## 7. Вопросы на собеседовании

**1. «У нас Kubernetes 1.28. Как ограничить поды по безопасности — PodSecurityPolicy?»**

Ответ: PSP удалён в 1.25, использовать нельзя. С 1.25 стабилен встроенный **Pod Security Admission** — admission-контроллер, который включается метками на namespace: `pod-security.kubernetes.io/enforce|warn|audit` со значением `privileged|baseline|restricted` и `-version` для фиксации версии профиля. `enforce` блокирует создание пода, `warn` возвращает предупреждение клиенту (`kubectl` его покажет), `audit` пишет в audit-лог. Внедрение делаю поэтапно: сначала `warn`+`audit` на неделю, смотрю, что отваливается, потом `enforce`. Для гранулярных случаев — Validating Admission Policy или Kyverno/Gatekeeper.

*Что хочет услышать:* что PSP мертвы, что PSA встроен и настраивается метками namespace, что внедрение поэтапное.
*Красный флаг:* «поставлю PSP» или «поставлю OPA и всё запрещу сразу на весь кластер».

**2. «Написал NetworkPolicy default-deny, а трафик между подами всё равно ходит. Почему?»**

Ответ: NetworkPolicy — это декларация, её реализует CNI. Если стоит Flannel без Calico, объект создастся, но ничего не сделает. Проверяю `kubectl get ds -n kube-system`. Второй вариант — политика в другом namespace, чем поды, или селектор не совпал (`kubectl describe netpol` покажет отрендеренные селекторы). Третий — трафик идёт не через под, а через hostNetwork или NodePort в обход. И надо помнить: как только на под действует хотя бы одна Ingress-политика, всё, что не разрешено явно, режется; пустой `podSelector: {}` — это все поды namespace.

*Что хочет услышать:* зависимость от CNI, порядок диагностики, понимание «default deny + allow».
*Красный флаг:* «NetworkPolicy всегда работает, это же часть Kubernetes».

**3. «Что такое kube-bench и чем CIS Benchmark отличается от него?»**

Ответ: CIS Kubernetes Benchmark — это документ, эталон конфигурации от Center for Internet Security с проверками и уровнями L1/L2. kube-bench — утилита Aqua, которая этот эталон автоматизирует: читает `/etc/kubernetes/*.conf`, `/var/lib/kubelet/config.yaml`, флаги процессов и выдаёт PASS/FAIL/WARN с указанием конкретного файла. Запускается бинарём на ноде или Job'ом с hostPath. Важно: он только диагностирует, ничего не исправляет, и часть проверок к managed-кластеру неприменима — такие находки фиксирую как принятый риск с обоснованием, а не «закрываю глаза».

*Что хочет услышать:* разницу между эталоном и сканером, L1/L2, что часть проверок нерелевантна.
*Красный флаг:* «kube-bench сам всё чинит» или «прошёл kube-bench — значит безопасно».

**4. «Зачем в securityContext столько полей? Что даёт каждое?»**

Ответ: `runAsNonRoot: true` не даст стартовать контейнеру от UID 0; `allowPrivilegeEscalation: false` запрещает setuid-бинарям поднимать привилегии; `capabilities.drop: [ALL]` убирает весь набор Linux-capabilities, нужные возвращаются точечно; `readOnlyRootFilesystem: true` делает корневую ФС контейнера неизменяемой — малварь не допишет файл; `seccompProfile: RuntimeDefault` отсекает ~300 ненужных приложению системных вызовов, заметно сокращая поверхность атаки ядра. Плюс это ровно то, что требует профиль `restricted`, так что одно другого не заменяет — PSA проверяет, securityContext исполняет.

*Что хочет услышать:* понимание, что это разные механизмы на разных слоях, и практическую пользу каждого поля.
*Красный флаг:* «достаточно `runAsNonRoot`».

**5. «Falco: что это, как работает и что он НЕ заменяет?»**

Ответ: Falco — runtime-детектор: агент (DaemonSet) собирает системные вызовы через eBPF или модуль ядра и матчит их на YAML-правила. Ловит то, чего не видно на этапе admission: запуск shell в контейнере, чтение `/etc/shadow`, обращение к Docker-сокету, `curl` наружу с продакшн-пода. Не заменяет Pod Security (он про допуск, а не про поведение), NetworkPolicy (сеть) и сканеры образов (они про содержимое до запуска). И главное — Falco бесполезен без алертинга: если логи никто не читает, это просто дорогой генератор текста.

*Что хочет услышать:* eBPF/ядро, правила, разграничение слоёв, обязательность алертинга.
*Красный флаг:* «Falco блокирует атаки» — он детектирует, а не блокирует.

## 8. Источники (официальная документация)

- **Center for Internet Security** — домен `cisecurity.org`, раздел «CIS Benchmarks», документ «CIS Kubernetes Benchmark». Поиск: `CIS Kubernetes Benchmark PDF`.
- **kube-bench** — домен `aquasecurity.github.io/kube-bench`, разделы «Installation», «Running kube-bench», «Output». Поиск: `kube-bench aquasecurity documentation`.
- **Kubernetes** — домен `kubernetes.io`, разделы «Pod Security Standards», «Pod Security Admission», «Network Policies», «Configure a Security Context for a Pod or Container», «Audit». Поиск: `kubernetes pod security standards`, `kubernetes network policy default deny`.
- **Falco** — домен `falco.org`, разделы «Installation», «Rules», «Alerts». Поиск: `falco helm chart installation`.
- **Calico / Cilium** — домены `docs.tigera.io` и `docs.cilium.io`, разделы про NetworkPolicy. Поиск: `calico network policy enforcement`.
- **NSA/CISA «Kubernetes Hardening Guidance»** — домен `cisa.gov`. Поиск: `Kubernetes Hardening Guidance CISA NSA` `[ПРОВЕРИТЬ актуальную редакцию]`.

Конкретные номера проверок CIS и теги образов проверяй сам: они меняются с каждой версией Kubernetes, и в тексте они помечены `[ПРОВЕРИТЬ]` не из осторожности, а потому что через полгода будут другими.
