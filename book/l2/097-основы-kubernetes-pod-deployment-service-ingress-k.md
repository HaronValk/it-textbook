# Основы Kubernetes: pod, deployment, service, ingress, kubectl, minikube/k3s в лаборатории

## 1. Введение (зачем это нужно в реальной работе)

Тикет в очереди L2:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия, WIN11-CL01)
> **Что написал:** «Открываю тестовый портал `http://portal.vektor.lab` — белая страница с надписью 503 Service Unavailable. Вчера работало. Бухгалтерия ждёт, срочно».
> **Срочность:** средняя, но с обещанным дедлайном.

Ещё пять лет назад такой тикет в компании на 50 человек был экзотикой. В 2026-м это рутина: даже небольшой бизнес ставит внутренние сервисы в Kubernetes — потому что «поставил один раз кластер, а дальше приложение само перезапускается, масштабируется и обновляется без простоя». А значит, поддержка L1/L2 обязана уметь: посмотреть, живы ли поды, понять, почему сервис не отдаёт трафик, и отличить «упало приложение» от «сломался роутинг».

Важно сразу снять страх: **Kubernetes для L2 — это не «написать свой оператор»**. Это 15 команд `kubectl`, умение читать `describe` и `logs`, и понимание цепочки «Ingress → Service → Endpoints → Pod → контейнер». Именно эту цепочку мы сегодня и разберём.

После главы вы будете: поднимать одноузловой кластер k3s на UBU01, запускать pod и deployment, публиковать приложение через Service и Ingress, диагностировать 503/ImagePullBackOff/CrashLoopBackOff и понимать, где кончается «контейнеры» (урок 96) и начинается «оркестрация».

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Kubernetes — **декларативная** система. Вы не говорите «запусти контейнер», вы говорите «я хочу, чтобы всегда было 2 копии nginx с меткой `app: vektor-portal`». Дальше работает **control loop**: контроллер сравнивает желаемое состояние (в базе) с фактическим (что реально запущено) и подгоняет одно под другое. Это принципиально отличается от Docker из урока 96, где вы запускали контейнер руками императивно.

### Компоненты

```text
                        ┌────────────────────────────── UBU01 (10.10.10.30) ──────────────────────────────┐
                        │                                                                                 │
  kubectl (клиент) ────►│  CONTROL PLANE (в k3s всё в одном процессе k3s server)                          │
   TLS :6443            │  ┌───────────────┐   ┌───────────┐   ┌────────────────────┐   ┌──────────────┐  │
                        │  │ kube-apiserver│──►│ scheduler │   │ controller-manager │──►│ хранилище    │  │
                        │  └───────┬───────┘   └───────────┘   └────────────────────┘   │ SQLite (kine)│  │
                        │          │                                                   └──────────────┘  │
                        │          ▼                                                                      │
                        │  NODE (эта же ВМ)                                                               │
                        │  ┌─────────┐   ┌────────────┐   ┌──────────┐   ┌──────────────┐                  │
                        │  │ kubelet │──►│ containerd │──►│  Pod     │   │ kube-proxy / │                  │
                        │  │ :10250  │   │ (рантайм)  │   │ container│   │ iptables     │                  │
                        │  └─────────┘   └────────────┘   └──────────┘   └──────────────┘                  │
                        │        ▲              ▲                                                        │
                        │        │ CNI flannel (VXLAN UDP/8472) — сеть подов 10.42.0.0/16                  │
                        │        │ Service CIDR 10.43.0.0/16                                              │
                        │  ServiceLB (klipper) :80/:443 ──► Traefik (Ingress controller)                   │
                        └─────────────────────────────────────────────────────────────────────────────────┘
```

Порты, которые надо помнить:

| Порт | Что | Зачем |
|------|-----|-------|
| TCP 6443 | kube-apiserver | единственная точка входа для `kubectl`, TLS + аутентификация |
| TCP 10250 | kubelet | API самого узла, тут же `logs` и `exec` |
| UDP 8472 | flannel VXLAN | overlay-сеть подов (нужна только в многоузловом кластере) |
| TCP 30000–32767 | NodePort | публикация сервиса наружу через порт узла |
| TCP 80/443 | Traefik | входной контроллер (в k3s включён по умолчанию) |

### Иерархия объектов

```text
Deployment  (сколько копий и какой образ — «план»)
   └── ReplicaSet (следит, чтобы подов было ровно N)
          └── Pod  (1..n контейнеров + общая сеть и volume)
                 └── container

Service (ClusterIP) ──selector──► Endpoints ──► Pod IP:port
   └── NodePort / LoadBalancer (публикация наружу)
          └── Ingress (HTTP-маршрутизация по Host и Path) ──► Service
```

Три ключевых мысли, без которых дальше никак:

1. **Pod — минимальная единица, а не контейнер.** В поде может быть несколько контейнеров, они делят `localhost` и сетевой namespace. IP у пода один.
2. **Service не «проксирует» трафик в привычном смысле.** Он находит поды по **меткам (labels)** через selector и наполняет объект **Endpoints** их IP. На узле kube-proxy (в k3s — через iptables) заводит правила: пакет на ClusterIP переписывается на IP одного из подов. Если подов с нужной меткой нет — Endpoints пустой, и вы получаете 503.
3. **Ingress — это правило L7**, а не железка. Само правило бесполезно без **Ingress controller** (в k3s это Traefik), который слушает 80/443 и превращает правила в свой конфиг.

По модели OSI: Ingress — L7 (HTTP Host/Path), Service — L4 (IP:port), CNI/overlay — L2/L3, `kubectl` — прикладной уровень поверх HTTPS.

## 3. Аналогия из жизни

Представьте жилой комплекс. **Pod** — квартира: в ней живут (контейнеры), у неё один адрес (IP), и если жильцов выселить, квартира сносится — новая будет уже с другим адресом.

**Deployment** — застройщик с планом: «в комплексе всегда должно быть 2 заселённые квартиры нашего типа». Если одну квартиру сносят (под упал), застройщик тут же сдаёт новую, с другим адресом. Вам не нужно звонить застройщику — он сам сверяет план с фактом (reconciliation loop).

**Service** — единый справочный телефон комплекса плюс диспетчер. Вы звоните на один неизменный номер (ClusterIP), а диспетчер смотрит в свой список «кто сейчас проживает по признаку `app: vektor-portal`» (Endpoints) и переключает на актуальную квартиру. Квартиры меняются — номер остаётся.

**Ingress** — вахтёр на входе: «на `portal.vektor.lab` — в бухгалтерский корпус, на `wiki.vektor.lab` — в другой». Он не работает с квартирами напрямую, только через диспетчера (Service).

**kubectl** — планшет прораба: посмотреть, что реально построено, и подать заявку на изменение плана.

Аналогия ломается на сети: «диспетчер» физически не сидит на телефоне — правила iptables на узле просто переписывают адрес пакета, и снаружи поды вообще не видны, их IP (10.42.x.x) существуют только внутри кластера.

## 4. Практика и команды

Всё выполняем на **UBU01 (10.10.10.30)**, где после урока 96 уже стоит Docker и вы умеете читать `docker ps`. k3s — дистрибутив Kubernetes «в одном бинарнике», для лаборатории идеален: 2 vCPU / 4 ГБ RAM хватает.

**1. Установка k3s и проверка сервиса**

```bash
curl -sfL https://get.k3s.io | sh -
systemctl status k3s --no-pager
sudo journalctl -u k3s -n 50 --no-pager
```
Скрипт скачивает бинарник и ставит systemd-юнит. В `status` смотрим `active (running)`. Если `curl` не проходит (GitHub из РФ периодически недоступен) — легальная альтернатива: скачать релиз `k3s` + `k3s-airgap-images-amd64.tar` со страницы Releases проекта k3s на GitHub с другого канала и установить в air-gap режиме (`INSTALL_K3S_SKIP_DOWNLOAD=true`). Точные имена файлов текущего релиза — `[ПРОВЕРИТЬ]` на странице релиза.

**2. Доступ к кластеру из-под своего пользователя**

```bash
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $USER:$USER ~/.kube/config
chmod 600 ~/.kube/config
echo 'export KUBECONFIG=$HOME/.kube/config' >> ~/.bashrc && source ~/.bashrc
kubectl get nodes -o wide
```
`k3s.yaml` — это **kubeconfig**: адрес API, сертификат CA и клиентский сертификат. Кто владеет этим файлом — тот фактически root кластера, поэтому `chmod 600`. Типичный вывод:

```text
NAME    STATUS   ROLES                  AGE   VERSION        INTERNAL-IP
ubu01   Ready    control-plane,master   3m    v1.3x.x+k3s1   10.10.10.30
```
`Ready` + `control-plane,master` = единственный узел, и он же управляющий.

**3. Обзор того, что уже работает**

```bash
kubectl get pods -A -o wide
kubectl get svc -A
```
`-A` (`--all-namespaces`) — по всем namespace. В `kube-system` увидите `traefik`, `coredns`, `local-path-provisioner`, `metrics-server`, `svclb-traefik`. `svclb-traefik` — это ServiceLB, который «занимает» порты 80/443 на узле.

**4. Быстрый pod одной командой**

```bash
kubectl run nettest --image=busybox:1.36 --restart=Never --command -- sleep 3600
kubectl get pod nettest -o wide
kubectl exec -it nettest -- sh
# внутри: nslookup kubernetes.default ; wget -qO- http://vektor-portal-svc ; exit
```
`--restart=Never` создаёт «голый» pod без контроллера (умрёт — не пересоздастся). `exec -it` — вход в контейнер. `nslookup` проверяет кластерный DNS: имя `kubernetes.default` должно резолвиться в 10.43.0.1 — это ClusterIP сервиса API.

**5. Pod из манифеста**

```bash
kubectl apply -f pod.yaml
kubectl get pod myapp -o yaml | head -40
kubectl delete pod myapp
```
`apply` — «привести к описанному состоянию», идемпотентно. `delete pod` для пода без контроллера = окончательное удаление; для пода под Deployment — «убей, застройщик построит новый».

**6. Describe — главный инструмент диагностики**

```bash
kubectl describe pod <имя>
```
Внизу секция `Events` (сортировка по времени). Ищем `Failed to pull image`, `Back-off restarting`, `Readiness probe failed`, `OOMKilled` в `Last State`. Если pod только что создан — `kubectl get events --sort-by=.lastTimestamp`.

**7. Логи**

```bash
kubectl logs <pod> --tail=100
kubectl logs -f <pod> -c nginx          # follow, конкретный контейнер
kubectl logs <pod> --previous           # логи предыдущего (упавшего) запуска
```
`--previous` — то, без чего не разобрать `CrashLoopBackOff`: контейнер упал, перезапустился, текущие логи пустые, а причина — в логах прошлого инстанса.

**8. Масштабирование и раскатка**

```bash
kubectl scale deployment vektor-portal --replicas=3
kubectl rollout status deployment/vektor-portal
kubectl rollout history deployment/vektor-portal
kubectl rollout undo deployment/vektor-portal
```
`rollout undo` без номера — откат на предыдущую ревизию. Историю хранит ReplicaSet.

**9. Проброс порта без NodePort**

```bash
kubectl port-forward svc/vektor-portal-svc 8080:80
# в другом терминале: curl -I http://127.0.0.1:8080
```
Удобно для проверки «а работает ли сервис вообще», не трогая firewall и NodePort. В бою так делают для отладки, но не для публикации.

**10. Ресурсы и состояние**

```bash
kubectl top nodes
kubectl top pods
kubectl get endpoints vektor-portal-svc
kubectl explain deployment.spec.strategy
```
`top` работает благодаря metrics-server (в k3s включён по умолчанию; если `error: Metrics API not available` — смотрим поды в `kube-system`). `get endpoints` — самый быстрый способ понять, видит ли Service свои поды.

**11. Kubernetes «изнутри» узла**

```bash
sudo k3s crictl ps
sudo k3s crictl images
journalctl -u k3s | grep -i error
```
k3s использует containerd, не Docker — поэтому `docker ps` **не покажет** поды. `crictl` — тот же `docker ps`, но для CRI.

**12. С рабочей станции Windows (WIN11-CL01)**

```powershell
# kubectl.exe с официального сайта kubernetes.io, kubeconfig скопирован в C:\Users\ivanov\.kube\config
kubectl --kubeconfig C:\Users\ivanov\.kube\config get pods -A
```
Для этого на UBU01 должен быть открыт 6443, а в kubeconfig — адрес `https://10.10.10.30:6443` вместо `127.0.0.1`. Это учебный сценарий: в бою доступ к API дают только с админских рабочих мест через VPN + RBAC.

GUI: в k3s его нет, но есть **Lens / OpenLens** (десктопный клиент) и веб-панель **Headlamp** — ставятся отдельно, для лабы не обязательны.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд L2 готов: DC01 (10.10.10.10), FS01 (10.10.10.20), WIN11-CL01, WIN10-CL02, UBU01 (10.10.10.30), ASTRA01 (10.10.10.31), домен `corp.vektor.lab`.
- UBU01: Ubuntu Server 24.04, вход по SSH под `adm.student` (или вашим пользователем с sudo), Docker из урока 96 установлен и **не мешает** (k3s живёт отдельно на containerd).
- UBU01 выделено **не менее 4 ГБ RAM и 2 vCPU**, диск 30+ ГБ. Если меньше — выключите ВМ, добавьте ресурсы в VMware Workstation (VM → Settings).
- Полноценный многоузловой кластер (`k8s-cp01`, `k8s-w01`, `k8s-w02`) **на этом треке не создаётся** — он появится на треке DevOps. Сегодня строим одноузловой учебный кластер, этого достаточно для всей диагностики.
- Заведён файл `stand-log.md`.

### Снапшот

В VMware Workstation на UBU01 (и на WIN11-CL01, если будете ставить kubectl.exe): VM → Snapshot → Take Snapshot, имя `before-l2-097`. Остальные ВМ (DC01, FS01, WIN10-CL02, ASTRA01) не затрагиваются — снапшот им не нужен.

### Шаги

1. **UBU01.** `nproc`, `free -h`, `df -h /`. Ожидаемо: 2 CPU, ~3.8 GiB total, ≥15 GB свободно. Если RAM меньше 2 ГБ — k3s и Traefik будут падать по OOM, добавляем память.
2. **UBU01.** `sudo apt update && sudo apt -y install curl ca-certificates`. Ожидаемо: `curl` уже есть, apt не падает.
3. **UBU01.** `curl -sfL https://get.k3s.io | sh -`. Ожидаемо: в конце `[INFO] systemd: Starting k3s`, через 30–60 секунд сервис активен. Проверка: `systemctl is-active k3s` → `active`.
4. **UBU01.** Настраиваем kubeconfig (команды из раздела 4, п.2) и выполняем `kubectl get nodes -o wide`. Ожидаемо: один узел `ubu01` в статусе `Ready`.
5. **UBU01.** `kubectl get pods -A`. Ожидаемо: в `kube-system` поды `traefik-*`, `coredns-*`, `local-path-provisioner-*`, `metrics-server-*`, `svclb-traefik-*` — все `Running` (coredns может быть `Running` с 1/1). `kubectl get svc -A` — у `traefik` тип `LoadBalancer` с EXTERNAL-IP `10.10.10.30`.
6. **UBU01.** Создаём `~/k8s/deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vektor-portal
  labels:
    app: vektor-portal
spec:
  replicas: 2
  selector:
    matchLabels:
      app: vektor-portal
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: vektor-portal
    spec:
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports:
            - containerPort: 80
          readinessProbe:
            httpGet:
              path: /
              port: 80
            initialDelaySeconds: 3
            periodSeconds: 5
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 128Mi
```
`kubectl apply -f ~/k8s/deployment.yaml`, затем `kubectl get deploy,rs,pods --show-labels`. Ожидаемо: `deploy` 2/2, один ReplicaSet, два пода `Running` с меткой `app=vektor-portal`, IP из 10.42.0.0/16.
7. **UBU01.** Публикуем сервис: `~/k8s/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: vektor-portal-svc
spec:
  type: NodePort
  selector:
    app: vektor-portal
  ports:
    - port: 80
      targetPort: 80
      nodePort: 30080
```
`kubectl apply -f ~/k8s/service.yaml`, затем `kubectl get svc vektor-portal-svc` и `kubectl get endpoints vektor-portal-svc`. Ожидаемо: два IP пода в Endpoints, `PORT` = 80:30080/TCP.
8. **UBU01.** Открываем порт: `sudo ufw status`, при активном ufw — `sudo ufw allow 30080/tcp` (и `sudo ufw allow 80,443/tcp` для Traefik). Проверка: `curl -I http://10.10.10.30:30080` → `HTTP/1.1 200 OK`, `Server: nginx`.
9. **WIN11-CL01.** В браузере `http://10.10.10.30:30080` — страница `Welcome to nginx!`. Это и есть публикация через NodePort.
10. **UBU01.** Создаём `~/k8s/ingress.yaml`:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: vektor-portal
spec:
  ingressClassName: traefik
  rules:
    - host: portal.vektor.lab
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: vektor-portal-svc
                port:
                  number: 80
```
`kubectl apply -f ~/k8s/ingress.yaml`, `kubectl get ingress` (ADDRESS = 10.10.10.30). Проверка без правки DNS: `curl -H "Host: portal.vektor.lab" http://10.10.10.30/` → 200. Затем на WIN11-CL01 добавьте строку `10.10.10.30 portal.vektor.lab` в `C:\Windows\System32\drivers\etc\hosts` (блокнот от имени администратора) и откройте `http://portal.vektor.lab`.
11. **UBU01.** Проверяем самовосстановление: `kubectl get pods`, затем `kubectl delete pod <один из подов>`, сразу `kubectl get pods -w`. Ожидаемо: удалённый под уходит в `Terminating`, появляется новый `ContainerCreating` → `Running`. Сервис всё это время доступен — на этом и строится «нулевой простой».
12. **UBU01.** Проверяем откат: `kubectl set image deployment/vektor-portal nginx=nginx:1.27-broken`, `kubectl rollout status deployment/vektor-portal`. Ожидаемо: раскатка виснет, новые поды в `ImagePullBackOff`, старые (2) продолжают работать — стратегия `maxUnavailable: 0`. Затем `kubectl rollout undo deployment/vektor-portal` и снова 200.
13. **UBU01.** `kubectl top pods`, `sudo k3s crictl ps | head`. Записываем в `stand-log.md`.

### Проверка

- [ ] `kubectl get nodes` → `ubu01` в статусе `Ready`.
- [ ] `kubectl get deploy vektor-portal` → `2/2` в колонке READY.
- [ ] `kubectl get endpoints vektor-portal-svc` содержит два IP подов (не `<none>`).
- [ ] `curl -I http://10.10.10.30:30080` с UBU01 → 200.
- [ ] С WIN11-CL01 `http://portal.vektor.lab` открывается (после правки hosts).
- [ ] Удалённый вручную pod пересоздался автоматически, сайт не «упал».
- [ ] `kubectl rollout history deployment/vektor-portal` показывает ≥2 ревизии.
- [ ] `kubectl top pods` отдаёт цифры CPU/памяти, а не ошибку.
- [ ] В `stand-log.md` появилась запись про k3s, namespace/объекты и открытые порты.

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия, ВМ WIN11-CL01)
> **Что написал:** «Портал `http://portal.vektor.lab` открывается, но вместо страницы — `503 Service Unavailable`. Вчера всё было нормально, мы ничего не трогали».
> **Срочность:** средняя (закрытие месяца).

**Как воспроизвести поломку (на UBU01, от себя, «как будто коллега правил сервис»):**

```bash
kubectl patch svc vektor-portal-svc -p '{"spec":{"selector":{"app":"vektor-portal-v2"}}}'
```
Метка в selector сервиса перестала совпадать с метками подов — Service больше не находит поды. (Второй классический способ сломать то же самое: `kubectl set image deployment/vektor-portal nginx=nginx:1.27-broken` — тогда пропадут сами поды.)

**Диагностика по шагам — от внешнего к внутреннему:**

1. Проверяем сам факт: `curl -I -H "Host: portal.vektor.lab" http://10.10.10.30/` → `503`. Значит, Traefik жив и правило нашёл, но за ним «пусто». Уже это сужает круг: не DNS, не firewall, не сеть.
2. `kubectl get ingress vektor-portal` и `kubectl describe ingress vektor-portal` — backend указан верно, на `vektor-portal-svc:80`. Значит, Ingress не при чём.
3. `kubectl get svc vektor-portal-svc -o yaml | grep -A3 selector` → `app: vektor-portal-v2`. **Вот и причина.**
4. Подтверждаем: `kubectl get endpoints vektor-portal-svc` → `<none>`. Пусто.
5. Сверяем с реальностью: `kubectl get pods --show-labels` → поды живы, `Running`, метка `app=vektor-portal`. Приложение в порядке, «сломана проводка».
6. На всякий случай проверяем, что поды реально отвечают: `kubectl port-forward svc/... ` не сработает (сервис битый), поэтому `kubectl port-forward pod/<имя> 8080:80` + `curl -I 127.0.0.1:8080` → 200. Окончательно снимаем подозрение с nginx.

**Решение:**

```bash
kubectl patch svc vektor-portal-svc -p '{"spec":{"selector":{"app":"vektor-portal"}}}'
kubectl get endpoints vektor-portal-svc        # снова два IP
curl -I -H "Host: portal.vektor.lab" http://10.10.10.30/   # 200
```
Если бы причина была в образе (`ImagePullBackOff`), лечили бы так: `kubectl rollout undo deployment/vektor-portal` или `kubectl set image` на существующий тег.

**Ответ пользователю:** «Портал снова доступен, проверил: `http://portal.vektor.lab` отдаёт страницу, ошибок нет. Причина — ошибка в конфигурации маршрутизации сервиса (сервис перестал видеть свои экземпляры приложения), данные не пострадали. Обновление браузера (Ctrl+F5) может понадобиться. Если повторится — сразу пишите, у нас есть алерт на такие случаи [в реальной среде]».

**Закрытие заявки:** категория «веб-сервис / конфигурация», причина — «несоответствие label selector сервиса меткам подов», решение — «исправлен selector, проверено 200 OK», время восстановления, ссылка на запись в `stand-log.md`. В реальной среде — плюс запись постмортема (урок 92) и заведение change-request (урок 93).

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен k3s (single-node, control-plane+worker), рантайм containerd, встроенные Traefik (Ingress), CoreDNS, metrics-server, local-path-provisioner, ServiceLB.
- **Порты на UBU01:** 6443 (API), 80/443 (Traefik), 30080 (NodePort). Открыты в ufw.
- **Кластерные объекты:** Deployment `vektor-portal` (2 реплики, nginx:1.27-alpine, readinessProbe, requests/limits), Service `vektor-portal-svc` (NodePort 30080), Ingress `vektor-portal` (`portal.vektor.lab`).
- **Сеть:** pod CIDR 10.42.0.0/16, service CIDR 10.43.0.0/16 — существует только внутри UBU01.
- **WIN11-CL01:** в `hosts` добавлена строка `10.10.10.30 portal.vektor.lab`; при желании — `kubectl.exe` и kubeconfig пользователя.
- **Файлы:** `~/k8s/{deployment,service,ingress}.yaml`, `~/.kube/config` (chmod 600), `stand-log.md` дополнен.

### Откат

VMware Workstation → UBU01 → Snapshot Manager → снапшот `before-l2-097` → Revert. WIN11-CL01 — удалить строку из `hosts` (или откатить её снапшот тоже, если делали). Если нужно откатить только объекты, без снапшота: `kubectl delete -f ~/k8s/ingress.yaml -f ~/k8s/service.yaml -f ~/k8s/deployment.yaml`. Полное удаление k3s (если совсем не нужен): `/usr/local/bin/k3s-uninstall.sh`.

## 6. Ошибки новичков и безопасность

1. **Править объекты через `kubectl edit` и забывать про манифесты.** → Кластер и файлы в репозитории расходятся, при следующем `apply` ваши правки молча затираются. → Правильно: единственный источник правды — YAML (в идеале в Git, см. уроки 98–99), правки только через `apply`.
2. **Тег `latest` в образе.** → `rollout` подтягивает другой код, `rollout undo` не помогает (тег-то тот же), поведение непредсказуемо. → Всегда фиксированный тег/дайджест: `nginx:1.27-alpine`, лучше `@sha256:...`.
3. **Pod без `requests`/`limits`.** → Один под съедает память узла, OOM-killer убивает соседей (`kube-system` в первую очередь — кластер «ложится»). → Всегда указывать requests и limits (в лабе мы это сделали).
4. **Путать уровни: лезть в под напрямую вместо Service.** → «У меня `curl` по IP пода работает, значит всё хорошо» — а сервис не работает, потому что selector не совпадает. → Проверять цепочку сверху вниз: Ingress → Service → Endpoints → Pod.
5. **Нет readinessProbe.** → Kubernetes считает под готовым сразу после старта контейнера и льёт на него трафик; пользователь ловит 502 на старте. → `readinessProbe` для приёма трафика, `livenessProbe` для перезапуска зависших.
6. **Секреты в манифесте открытым текстом.** → Пароль от БД уезжает в Git и в `kubectl describe`. → Secret + внешний менеджер (Vault, урок 102), манифесты — только ссылки на секреты.
7. **Работа без namespace.** → `kubectl delete -f .` в `default` сносит не то, что планировали; в командах забывают `-n`. → Отдельные namespace по сервисам/командам, `kubens`/`kubectx` для переключения, всегда проверять `kubectl config view --minify | grep namespace`.
8. **Диагностика по `docker ps`.** → В k3s контейнеров Docker нет, вывод пустой, новичок делает вывод «кластер пустой». → `kubectl get pods`, а на узле — `sudo k3s crictl ps`.

### Безопасность

- **kubeconfig = root кластера.** В нашем k3s сертификат `adm.student` — админский. В боевой среде так нельзя: каждому свой ServiceAccount с RBAC-ролью, доступ к API — только через VPN с админских машин, 6443 не публикуется наружу.
- **6443 в интернет — это приглашение.** Открытый API-сервер сканируется ботами в течение часов; при слабом пароле/утекшем kubeconfig кластер ваш только до первого криптомайнера.
- **`ufw allow 30080` наружу** мы делаем только внутри лабораторной сети. В бою NodePort публикуют лишь для внутренних нужд или не используют вовсе — трафик заводят через Ingress/LoadBalancer с TLS.
- **Тестовый стенд vs прод:** в главе мы намеренно ломаем селектор сервиса и ставим «битый» тег образа. На проде такие эксперименты делают только на staging — и никогда в пятницу вечером без change-request.
- **Образы:** `nginx:1.27-alpine` тянется из Docker Hub. В российских реалиях доступ к Docker Hub может быть нестабильным — легальные варианты: зеркало/реестр внутри компании (Harbor, урок 102) или отечественные managed-платформы (например, Deckhouse Kubernetes Platform — `[ПРОВЕРИТЬ]` актуальный статус в реестре российского ПО).

## 7. Вопросы на собеседовании

**Вопрос 1. Чем Pod отличается от контейнера и почему минимальная единица — Pod, а не контейнер?**
Ответ: Pod — обёртка над одним или несколькими контейнерами, которые делят сетевой namespace (общий IP и `localhost`) и общие volume. Pod — единица планирования: scheduler ставит под целиком на узел, kubelet запускает его как единицу. Несколько контейнеров в одном поде нужны для паттерна sidecar (прокси, сборщик логов, init-контейнеры для подготовки).
*Что хочет услышать интервьюер:* понимание, что под — не «контейнер плюс обвязка», а единица атомарного размещения и общей сети.
*Красный флаг:* «Pod — это то же самое, что контейнер, просто другое слово».

**Вопрос 2. Как Service находит поды и почему пользователь получает 503 при живом приложении?**
Ответ: Service находит поды по label selector. Список актуальных «подходящих» подов хранится в объекте Endpoints (в новых версиях — EndpointSlice). Если поды не Ready или метки не совпадают с selector — Endpoints пустой, и ingress controller/L4-балансировщик отдаёт 503 (нет доступных бэкендов). Диагностика: `kubectl get endpoints <svc>`, `kubectl get pods --show-labels`.
*Что хочет услышать интервьюер:* что вы сразу смотрите Endpoints, а не перезапускаете поды наугад.
*Красный флаг:* «503 — значит, приложение упало, надо перезапустить deployment».

**Вопрос 3. Что делает Deployment и как проходит rolling update с откатом?**
Ответ: Deployment описывает желаемое состояние (образ, число реплик, стратегию) и управляет ReplicaSet'ами: при смене шаблона создаётся новый ReplicaSet, старый постепенно уменьшается. Параметры `maxSurge`/`maxUnavailable` задают темп. История ревизий хранится в старых ReplicaSet'ах, `kubectl rollout undo` переключает Deployment обратно на предыдущий RS. `rollout status` показывает прогресс, при зависании (например, `ImagePullBackOff`) старая версия продолжает обслуживать трафик при `maxUnavailable: 0`.
*Что хочет услышать интервьюер:* понимание, что Deployment ≠ Pods напрямую, между ними ReplicaSet, и что откат — это не «пересборка», а переключение на прежний RS.
*Красный флаг:* «Откат — это заново задеплоить предыдущий образ руками, ничего автоматического нет».

**Вопрос 4. ClusterIP, NodePort, LoadBalancer, Ingress — в чём разница и что выбрать?**
Ответ: ClusterIP — виртуальный IP только внутри кластера, для общения сервисов между собой. NodePort — открывает порт (30000–32767) на **каждом** узле, трафик идёт на Service; грубо, но работает без облака. LoadBalancer — просит у облачного провайдера внешний балансировщик; в k3s эту роль играет ServiceLB (Klipper), подставляя IP узла. Ingress — L7-маршрутизация по Host/Path к разным Service, требует ingress controller (Traefik/nginx). Для внутреннего сервиса — ClusterIP; для доступа людей по именам — Ingress поверх ClusterIP; NodePort — для отладки и в средах без балансировщика.
*Что хочет услышать интервьюер:* что Ingress — это не замена Service, а слой над ним, и что «Ingress без контроллера — просто YAML».
*Красный флаг:* «Ingress сам балансирует трафик на поды, Service не нужен».

**Вопрос 5. Pod в состоянии CrashLoopBackOff. Ваши действия?**
Ответ: `kubectl describe pod` — смотрим `Events`, `Last State`, `Exit Code`, признак `OOMKilled`; `kubectl logs <pod> --previous` — логи упавшего инстанса; `kubectl get pod -o yaml` — правильные ли образ, команда, переменные, монтирования; проверяем probes (падает из-за liveness) и limits (OOM). Дальше — либо `rollout undo`, либо правка конфигурации в манифесте. Если под не стартует вообще (Pending/ImagePullBackOff) — это другая ветка: ресурсы узла, тег образа, доступ к реестру.
*Что хочет услышать интервьюер:* дисциплинированный порядок «describe → logs --previous → события → ресурсы», а не «перезапущу и посмотрю».
*Красный флаг:* «Удалю под, он пересоздастся и заработает» — при CrashLoopBackOff новый под упадёт точно так же.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел *Concepts*: «Pods», «Deployments», «Service», «Ingress», «Namespaces»; отдельно *Tasks → Access Applications in a Cluster* (port-forward, NodePort, ingress) и «kubectl Cheat Sheet». Поиск: `kubernetes concepts deployments services ingress`.
- **kubernetes.io** — *Reference → kubectl commands* и `kubectl explain` как встроенная справка. Поиск: `kubectl cheat sheet`.
- **k3s.io** (документация проекта K3s) — *Quick-Start*, *Configuration options* (`/etc/rancher/k3s/k3s.yaml`), *Air-Gap Install*. Поиск: `k3s air-gap install`.
- **minikube.sigs.k8s.io** — *Documentation → drivers (docker driver)*, если хотите альтернативу k3s на UBU01 или на Windows-хосте. Поиск: `minikube docker driver windows`.
- **traefik.io** — *Traefik & Kubernetes Ingress* (как читать правила Ingress и откуда берётся 503/502). Поиск: `traefik kubernetes ingress 503 service unavailable`.
- **learn.microsoft.com** — обзор Kubernetes и AKS («Kubernetes core concepts»), полезно для понимания managed-кластеров, если в компании облако Microsoft/Azure `[ПРОВЕРИТЬ доступность сервиса из РФ]`.
- **Документация Deckhouse Kubernetes Platform** (отечественная сертифицированная платформа `[ПРОВЕРИТЬ актуальный реестр]`) — как альтернатива зарубежным managed-кластерам в российских реалиях. Поиск: `Deckhouse Kubernetes Platform документация`.
- Все точные номера версий, имена файлов релизов и флаги установки помечены в тексте `[ПРОВЕРИТЬ]` — сверяйте на официальных страницах на дату чтения.
