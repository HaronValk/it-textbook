# Архитектура приложений для DevOps: монолит vs микросервисы, 12-factor app, API gateway, event-driven

## 1. Введение (зачем это нужно в реальной работе)

Тикет в GitLab на GIT01, проект `vektor/payment-service`, автор — тимлид разработки платёжного сервиса:

> «Коллеги, мы выкатили новую версию payment-api в кластер. Локально у разработчика всё работает, на стенде — 502 на части запросов и остатки склада в orders-api отстают на час. Логи в контейнере пустые, приложение пишет в файл внутри пода. Конфиг БД зашит в образ. Помогите разобраться, у нас через два дня демо заказчику».

Заявка как будто про Kubernetes, а на самом деле — про архитектуру. Приложение, которое пишет логи в файл, хранит настройки в коде и требует ручного порядка запуска, нельзя нормально эксплуатировать ни в k8s, ни в облаке, ни на виртуалках: оно будет ломаться на каждом деплое.

DevOps-инженер не проектирует бизнес-логику, но он первым получает удар, когда архитектура не готова к эксплуатации. Он отвечает за то, чтобы сервис собирался воспроизводимо, конфигурировался без пересборки, масштабировался горизонтально, отдавал метрики и логи в общую систему, переживал падение отдельного пода и не терял события при ретраях. Эта глава — про язык, на котором DevOps разговаривает с разработкой: монолит и микросервисы, 12 факторов, API gateway и событийная архитектура.

После главы ты будешь: понимать, чем отличается «модно нарезать на микросервисы» от «эксплуатируемо»; проверять любой сервис по чек-листу 12-factor и говорить разработчику конкретно, что не так; настраивать ingress как точку входа с маршрутизацией и лимитами; читать лаг консьюмеров в брокере и понимать, где теряются события.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Монолит.** Одно приложение, один процесс, один артефакт деплоя, одна база. Плюсы: простота, транзакции в одной БД работают «из коробки», отладка в одном логе, дешёвый старт. Минусы проявляются на масштабе: любое изменение требует пересборки и перезапуска всего приложения, масштабируется только целиком (хотя узкое место — один отчёт), а связность кода растёт, пока релиз не начинает занимать недели. Хорошая середина — **модульный монолит**: один деплой, но внутри жёсткие границы модулей, и при необходимости модуль можно вынести.

**Микросервисы.** Система разбита по бизнес-возможностям (bounded context из Domain-Driven Design): платежи, заказы, уведомления. Каждый сервис — отдельный деплой, отдельная команда, **своя база данных**. Остальные ходят к нему только через API или события. Плюсы: независимые релизы, точечное масштабирование и отказоустойчивость. Минусы: сеть становится ненадёжной частью системы, появляются распределённые транзакции, версионирование API, распределённая трассировка, и счёт за инфраструктуру растёт. Типичная ошибка — **распределённый монолит**: сервисов десять, но они деплоятся только вместе, ходят в одну общую БД и синхронно вызывают друг друга цепочкой из пяти звеньев. Это худшее из двух миров.

**12-factor app** — набор правил, превращающих приложение из «работает у разработчика» в «эксплуатируется где угодно». Все двенадцать: I Codebase (один репозиторий — много деплоев), II Dependencies (явно объявлены и изолированы), III Config (настройки в окружении, не в коде), IV Backing services (БД, брокер, кэш — подключаемые ресурсы по URL), V Build/Release/Run (стадии строго разделены), VI Processes (процессы stateless, состояние — в БД), VII Port binding (сервис сам слушает порт), VIII Concurrency (масштабирование процессами), IX Disposability (быстрый старт, корректное завершение по SIGTERM), X Dev/prod parity (окружения похожи), XI Logs (лог — поток событий в stdout, а не файл), XII Admin processes (миграции и разовые задачи — отдельные процессы). Для DevOps критичны III, VI, IX, XI: именно на них ломается 80% деплоев в Kubernetes.

**API gateway** — единая точка входа снаружи внутрь (traffic north-south). Делает TLS-терминацию, маршрутизацию по путям и хостам, аутентификацию (проверка JWT), rate limiting, кэширование, версионирование, канареечный сплит, access-логи и заголовки трассировки. В k8s эту роль чаще всего играет Ingress-контроллер (ingress-nginx, Traefik) или более функциональный Kong/APISIX; современная замена Ingress — Gateway API. Важно не путать: gateway — про вход снаружи, **service mesh** (глава 104) — про трафик между сервисами внутри (east-west): mTLS, ретраи, таймауты, канареечные веса.

**Event-driven.** Сервис не вызывает другого синхронно, а публикует факт («заказ создан») в брокер (Kafka, RabbitMQ, NATS — глава 105), а заинтересованные стороны читают его в своём темпе. Это даёт слабую связность и устойчивость к недоступности потребителя, но приносит **eventual consistency** (данные сходятся не мгновенно), доставку **at-least-once** (событие может прийти дважды) и необходимость схем и их версий. Ключевые паттерны: **идемпотентный консьюмер** (по уникальному `event_id`), **транзакционный outbox** (пишем событие в БД в той же транзакции, отдельный воркер отправляет), **dead letter queue** (DLQ) для «ядовитых» сообщений, **saga** вместо распределённой транзакции.

```text
                ┌───────────────────────────────────────────────┐
 Клиент         │            ВНЕШНИЙ КОНТУР                     │
 (браузер, 1С)  │   DNS api.vektor.lab → адрес кластера         │
      │         │                                               │
      ▼         │   API GATEWAY (Ingress / Kong)                │
   :443 TLS ───►│   • TLS-терминация   • маршрутизация по пути  │
                │   • JWT / rate limit • access-log, trace-id   │
                └──────────────────┬────────────────────────────┘
                                   │  north-south → east-west
                ┌──────────────────▼────────────────────────────┐
                │  k8s-cp01 (10.10.10.61), k8s-w01 (.62), w02   │
                │   ┌─────────────┐        ┌─────────────┐      │
                │   │ payment-api │◄─sync──│ orders-api  │      │
                │   │ :8080       │ HTTP   │ :8080       │      │
                │   └──────┬──────┘        └──────┬──────┘      │
                │          │ publish              │ subscribe   │
                │          ▼                      ▼             │
                │   ┌────────────────────────────────────┐      │
                │   │ BROKER (Kafka :9092 / Rabbit :5672)│      │
                │   │ topic orders.created, DLQ          │      │
                │   └────────────────────────────────────┘      │
                └───────────────────────────────────────────────┘
   Порты: 80/443 — вход; 8080 — приложение; 9092 — Kafka;
          5672/15672 — RabbitMQ; 4222 — NATS; 5432 — PostgreSQL
```

## 3. Аналогия из жизни

Фудкорт в торговом центре. Монолит — это один большой ресторан: одна кухня, один склад, один шеф. Заказов мало — идеально: всё под рукой, повара видят друг друга. Заказов стало в десять раз больше — расширять приходится весь ресторан сразу, а если сломался один гриль, стоит вся кухня.

Микросервисы — фудкорт: отдельная точка с пиццей, отдельная с суши, у каждой своя кухня, свой склад и свой управляющий. Пиццерия может переехать на новое меню, не спрашивая суши. Но теперь нужен **хостес и общее меню на входе** (API gateway): гость не бегает по кухням, он делает заказ в одной точке. Нужна **доска заказов, куда точки кидают готовые позиции** (брокер событий): суши-бар не звонит пиццерии, он просто выставляет «роллы готовы», а курьер забирает. И нужны **санитарные правила и стандартная планировка кухни** (12-factor): любая точка должна открыться в любом городе по одному и тому же регламенту, а не «у шефа своя плита и свой порядок».

Где аналогия ломается: в фудкорте гость может подойти к любой точке напрямую, а в системе gateway — единственный вход, и обойти его нельзя.

## 4. Практика и команды

Дальше работаем из Linux (UBU01 как рабочая станция инженера или GIT01), Windows-часть на DevOps-стенде не нужна.

**1. Карта того, что задеплоено.** Первый шаг в любой заявке «сервис не работает».

```bash
kubectl -n arch-demo get deploy,svc,ingress -o wide
kubectl -n arch-demo get pods --show-labels
```
`-o wide` добавляет IP и образы, `--show-labels` показывает метки. Смотреть: совпадают ли метки подов с `spec.selector` сервиса и есть ли у Ingress адрес.

**2. Проверка, что за сервисом стоят живые поды.**

```bash
kubectl -n arch-demo get endpointslices
# либо классическое:
kubectl -n arch-demo get endpoints payment-api
```
Если в `ENDPOINTS` пусто (`<none>`) — сервис никого не нашёл. Это причина 503 от gateway в 90% случаев.

**3. API gateway: маршрутизация по путям.**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: api-gateway
  namespace: arch-demo
  annotations:
    nginx.ingress.kubernetes.io/use-regex: "true"          # [ПРОВЕРИТЬ] для вашей версии контроллера
    nginx.ingress.kubernetes.io/rewrite-target: /$2
    nginx.ingress.kubernetes.io/limit-rps: "20"
spec:
  ingressClassName: nginx
  rules:
  - host: api.vektor.lab
    http:
      paths:
      - path: /payments(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: payment-api
            port: { number: 80 }
      - path: /orders(/|$)(.*)
        pathType: ImplementationSpecific
        backend:
          service:
            name: orders-api
            port: { number: 80 }
```
`rewrite-target` срезает префикс перед отправкой в под, `limit-rps` ограничивает запросы в секунду с одного IP. Проверка без DNS:

```bash
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80 &
curl -s -H "Host: api.vektor.lab" http://127.0.0.1:8080/payments/healthz
```
Ожидаемо: `ok`. Если `502` — gateway дозвонился до сервиса, но под не ответил; если `503` — у сервиса нет endpoints.

**4. Rate limit в деле.**

```bash
for i in $(seq 1 40); do
  curl -s -o /dev/null -w "%{http_code}\n" -H "Host: api.vektor.lab" http://127.0.0.1:8080/payments/
done | sort | uniq -c
```
Ожидаемо: часть `200`, часть `503` (ingress-nginx отдаёт 503 при срабатывании лимита) [ПРОВЕРИТЬ код для вашей версии]. Смысл — увидеть, что защита работает, до того как её увидит заказчик.

**5. 12-factor III: конфиг в окружении, а не в образе.**

```bash
kubectl -n arch-demo create configmap payment-config --from-literal=LOG_LEVEL=info
kubectl -n arch-demo create secret generic payment-secret \
  --from-literal=DB_PASSWORD='Lab#2026-vektor'   # только учебный стенд!
kubectl -n arch-demo exec deploy/payment-api -- env | sort | grep -E 'LOG_LEVEL|SERVICE_NAME'
```
Смотреть: переменные пришли из ConfigMap/Secret, а не зашиты в Dockerfile. Секреты в проде — из Vault/Secret Manager, а не `--from-literal` в истории shell.

**6. Пробы — это и есть disposability (фактор IX).**

```yaml
        readinessProbe:
          httpGet: { path: /ready, port: 8080 }
          initialDelaySeconds: 3
          periodSeconds: 5
        livenessProbe:
          httpGet: { path: /healthz, port: 8080 }
          periodSeconds: 10
        terminationGracePeriodSeconds: 20
```
`readinessProbe` убирает под из балансировки, `livenessProbe` перезапускает зависший, grace period даёт время доработать запросы после SIGTERM.

**7. Логи как поток (фактор XI).**

```bash
kubectl -n arch-demo logs -f deploy/payment-api --tail=50
kubectl -n arch-demo logs deploy/payment-api --previous   # логи упавшего контейнера
```
Если логи пустые — приложение пишет в файл. Это баг архитектуры, а не «kubectl сломался».

**8. Релиз без пересборки (факторы V и III).**

```bash
kubectl -n arch-demo set env deploy/payment-api LOG_LEVEL=debug
kubectl -n arch-demo rollout status deploy/payment-api
kubectl -n arch-demo rollout restart deploy/payment-api
```
`set env` меняет переменную и триггерит новый ReplicaSet — это «release», а не «build».

**9. RabbitMQ: где копятся сообщения.**

```bash
kubectl -n brokers exec -it rabbitmq-0 -- \
  rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers
```
`messages_ready` растёт, `consumers` = 0 → консьюмер упал или не подписался. `messages_unacknowledged` растёт → консьюмер взял и не подтвердил (завис или долго обрабатывает).

**10. Kafka: лаг консьюмер-группы.**

```bash
kubectl -n brokers exec -it kafka-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group payment-consumer
```
Смотреть `LAG` = `LOG-END-OFFSET` − `CURRENT-OFFSET`. Растущий лаг = потребитель не успевает; лаг, застывший на одном offset = консьюмер залип на «ядовитом» сообщении.

**11. NATS: быстрая проверка pub/sub.**

```bash
nats pub orders.created '{"event_id":"e-1","order":42}' &
nats sub 'orders.created'
```
Удобно для мгновенной проверки, что брокер жив и подписка работает, не поднимая тяжёлый стек.

**12. Аудит «конфиг в коде» перед ревью.**

```bash
git grep -nEi '(password|secret|token|api_key)\s*=' -- '*.py' '*.go' '*.yaml'
```
Быстрый способ показать разработке, что секреты лежат в репозитории — до того, как это найдёт сканер безопасности в CI.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
Готовый кластер `k8s-cp01` (10.10.10.61), `k8s-w01` (.62), `k8s-w02` (.63); GIT01 (10.10.10.50) с GitLab; HARBOR01 (10.10.10.55) с проектом `vektor`; на кластере установлен ingress-nginx (если нет — ставим из главы 104/103 по вашей методике). Docker на GIT01. Если RabbitMQ/Kafka ещё не подняты — шаг 11 пропускаем, остальное не зависит.

### Снапшот
В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot, имя `before-devops-108`. Снять на **GIT01, HARBOR01, k8s-cp01, k8s-w01, k8s-w02**.

### Шаги

1. **k8s-cp01.** Проверяем кластер и создаём namespace.
   ```bash
   kubectl get nodes
   kubectl create namespace arch-demo
   ```
   Ожидаемо: три ноды в статусе `Ready`, namespace создан.

2. **GIT01.** Создаём репозиторий `vektor/arch-demo` в GitLab и в нём файлы: `app.py`, `Dockerfile`, `k8s/deployment.yaml`, `k8s/service.yaml`, `k8s/ingress.yaml`, `k8s/config.yaml`.

3. **GIT01.** Пишем минимальное приложение, которое живёт по 12-factor: порт из окружения, логи в stdout, `/healthz` и `/ready`, корректный SIGTERM.
   ```python
   # app.py
   import json, os, signal, sys, time
   from http.server import BaseHTTPRequestHandler, HTTPServer

   SERVICE = os.getenv("SERVICE_NAME", "unknown")
   PORT = int(os.getenv("PORT", "8080"))
   START = time.time()

   class H(BaseHTTPRequestHandler):
       def do_GET(self):
           if self.path in ("/healthz", "/ready"):
               self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
           body = json.dumps({"service": SERVICE, "path": self.path,
                              "uptime": round(time.time()-START, 1)}).encode()
           self.send_response(200)
           self.send_header("Content-Type", "application/json"); self.end_headers()
           self.wfile.write(body)
       def log_message(self, fmt, *args):
           sys.stdout.write("%s %s\n" % (SERVICE, fmt % args))   # логи в stdout

   signal.signal(signal.SIGTERM, lambda *a: (print(SERVICE, "SIGTERM, drain", flush=True), sys.exit(0)))
   print(f"{SERVICE}: listening on :{PORT}", flush=True)
   HTTPServer(("0.0.0.0", PORT), H).serve_forever()
   ```
   И `Dockerfile`:
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY app.py .
   ENV PORT=8080
   EXPOSE 8080
   CMD ["python", "app.py"]
   ```

4. **GIT01.** Собираем и пушим образ в Harbor.
   ```bash
   docker build -t 10.10.10.55/vektor/arch-demo:0.1.0 .
   docker login 10.10.10.55 -u admin
   docker push 10.10.10.55/vektor/arch-demo:0.1.0
   ```
   Ожидаемо: `digest: sha256:...`. Если Harbor по HTTP — добавьте адрес в `insecure-registries` в `/etc/docker/daemon.json` и на нодах k8s [ПРОВЕРИТЬ конфиг вашего containerd].

5. **k8s-cp01.** Создаём конфиг и секрет (фактор III).
   ```bash
   kubectl -n arch-demo create configmap app-config --from-literal=LOG_LEVEL=info
   kubectl -n arch-demo create secret generic app-secret --from-literal=DB_PASSWORD='Lab#2026-vektor'
   ```

6. **k8s-cp01.** Деплоим ОДИН образ дважды с разным окружением (фактор I: одна кодовая база — много деплоев).
   ```yaml
   # k8s/deployment.yaml (фрагмент, применяем дважды: payment-api и orders-api)
   apiVersion: apps/v1
   kind: Deployment
   metadata: { name: payment-api, namespace: arch-demo }
   spec:
     replicas: 2
     selector: { matchLabels: { app: payment-api } }
     template:
       metadata: { labels: { app: payment-api } }
       spec:
         containers:
         - name: app
           image: 10.10.10.55/vektor/arch-demo:0.1.0
           ports: [{ containerPort: 8080 }]
           env:
           - { name: SERVICE_NAME, value: payment-api }
           envFrom:
           - configMapRef: { name: app-config }
           - secretRef:    { name: app-secret }
           readinessProbe: { httpGet: { path: /ready, port: 8080 }, periodSeconds: 5 }
           livenessProbe:  { httpGet: { path: /healthz, port: 8080 }, periodSeconds: 10 }
           resources: { requests: { cpu: 50m, memory: 64Mi }, limits: { memory: 128Mi } }
   ```
   Ожидаемо: `kubectl -n arch-demo get pods` — 4 пода `Running`, по 2 на сервис.

7. **k8s-cp01.** Создаём Service для каждого и Ingress из раздела 4, шаг 3.
   Ожидаемо: `kubectl -n arch-demo get endpoints payment-api` показывает два IP:8080.

8. **k8s-cp01.** Проверяем маршрутизацию через gateway.
   ```bash
   kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 8080:80 &
   curl -s -H "Host: api.vektor.lab" http://127.0.0.1:8080/payments/ | jq
   curl -s -H "Host: api.vektor.lab" http://127.0.0.1:8080/orders/   | jq
   ```
   Ожидаемо: в первом ответе `"service": "payment-api"`, во втором — `orders-api`. Один вход, два бэкенда.

9. **k8s-cp01.** Меняем конфиг без пересборки образа: правим ConfigMap и перезапускаем rollout. Ожидаемо: новые поды подхватили значение, старые погашены.

10. **k8s-cp01.** Проверяем disposability: `kubectl -n arch-demo delete pod -l app=payment-api`. Ожидаемо: поды пересоздались, `curl` из шага 8 продолжает отвечать 200.

11. **Брокер (если поднят в уроке 105).** Публикуем событие и убеждаемся, что консьюмер его забрал; проверяем лаг командой из раздела 4, шаг 10.

12. **GIT01.** Коммитим манифесты и пишем в `stand-log.md` строку: `108: namespace arch-demo, образ 10.10.10.55/vektor/arch-demo:0.1.0, ingress api.vektor.lab → payment-api/orders-api`.

### Проверка
- [ ] `kubectl -n arch-demo get pods` — все поды `Running`, `READY 1/1`
- [ ] `kubectl -n arch-demo get endpoints payment-api` содержит два адреса, не `<none>`
- [ ] `curl` на `/payments/` возвращает `payment-api`, на `/orders/` — `orders-api`
- [ ] При 40 быстрых запросах часть отдаёт 503 (rate limit работает)
- [ ] `kubectl -n arch-demo exec deploy/payment-api -- env` показывает `LOG_LEVEL` и `SERVICE_NAME`
- [ ] `kubectl -n arch-demo logs deploy/payment-api` непустой (логи в stdout)
- [ ] После `delete pod` сервис не «упал»: curl продолжает отвечать 200
- [ ] Все манифесты в Git, руками в кластер ничего не набирали

### Сломай и почини
**Пользователь:** тимлид разработки платёжного сервиса. **ВМ/сервис:** k8s-w01, `payment-api`. **Что написал:** «После вашего деплоя на `api.vektor.lab/payments` стабильно 502, `/orders` при этом работает. Демо через два дня». **Срочность:** высокая.

**Воспроизводим поломку:** правим `selector` в Service `payment-api` на несуществующую метку:
```bash
kubectl -n arch-demo patch svc payment-api -p '{"spec":{"selector":{"app":"payment-apii"}}}'
```
**Диагностика по порядку:**
1. `curl -H "Host: api.vektor.lab" .../payments/` — видим 503.
2. `kubectl -n arch-demo describe ingress api-gateway` — бэкенд указан верно, значит gateway не при чём.
3. `kubectl -n arch-demo get endpoints payment-api` — `<none>`. Вот и причина.
4. `kubectl -n arch-demo get pods --show-labels` — поды живы, метка `app=payment-api`.
5. `kubectl -n arch-demo get svc payment-api -o yaml | grep -A3 selector` — видим опечатку `payment-apii`.

**Решение:** откатываем селектор (`kubectl -n arch-demo patch svc payment-api -p '{"spec":{"selector":{"app":"payment-api"}}}'`), ждём 5–10 секунд, endpoints наполняются, curl отдаёт 200.

**Ответ пользователю:** «Причина — опечатка в selector сервиса после ручного патча, из-за этого у сервиса не было endpoints. Исправлено, `/payments` отвечает 200. Патчи в обход Git больше не применяем — только через манифесты в репозитории». **Закрытие:** тип «конфигурация», причина «ручное изменение в обход Git», профилактика — запретить `kubectl edit` в кластере, включить GitOps-синхронизацию (глава 103).

### Что изменилось на стенде
- Namespace `arch-demo` на кластере k8s (cp01 + w01 + w02).
- Репозиторий `vektor/arch-demo` на GIT01 с манифестами.
- Образ `10.10.10.55/vektor/arch-demo:0.1.0` в Harbor, проект `vektor`.
- Deployment/Service `payment-api` (2 реплики) и `orders-api` (2 реплики).
- ConfigMap `app-config`, Secret `app-secret` (учебный пароль).
- Ingress `api-gateway`, хост `api.vektor.lab` → `/payments`, `/orders`.
- Строка в `stand-log.md` про урок 108.

### Откат
```bash
kubectl delete namespace arch-demo
```
либо Snapshot Manager в VMware → выбрать `before-devops-108` → Restore на всех пяти ВМ. Образ в Harbor удаляется вручную в веб-интерфейсе HARBOR01 (проект `vektor` → репозиторий → Delete), если нужно вернуть стенд в исходное состояние полностью.

## 6. Ошибки новичков и безопасность

- **Дробить на микросервисы «потому что модно»** → 5 сервисов на 200 строк кода, сетевые таймауты, распределённые баги и рост стоимости инфраструктуры. Правильно: сначала модульный монолит, выделение сервиса — только когда есть команда, нагрузка и граница домена.
- **Общая БД у нескольких сервисов** → это распределённый монолит: изменение схемы ломает всех, деплой снова становится совместным. Правильно: у сервиса своя БД, обмен — через API или события.
- **Синхронные цепочки вызовов A→B→C→D** → падение последнего звена валит всю цепочку, таймауты умножаются. Правильно: минимум синхронных хопов, асинхронность, таймауты, circuit breaker.
- **Логи в файл внутри контейнера** → после пересоздания пода логи исчезли, разбирать инцидент нечем. Правильно: stdout/stderr, сбор через Loki/ELK.
- **Конфиг и секреты в образе или в Git** → пересборка на каждое изменение, утечка паролей. Правильно: ConfigMap/Secret, внешний Vault, ротация.
- **`replicas: 1` и отсутствие readinessProbe** → при rolling update есть окно, когда трафик идёт в неготовый под, и пользователь видит 502. Правильно: минимум 2 реплики, пробы, PodDisruptionBudget.
- **Игнорирование идемпотентности** → брокер доставил событие дважды, деньги списались дважды. Правильно: уникальный `event_id`, проверка уже обработанных, outbox.
- **Версионирование API «когда-нибудь потом»** → обновил payment-api, отвалился orders-api. Правильно: `/v1/`, обратная совместимость, контрактные тесты.

### Безопасность
В боевой среде нельзя: держать секреты в ConfigMap (ConfigMap — не секрет, он читается без шифрования), оставлять gateway без rate limiting и WAF (глава 107), разрешать подам ходить друг к другу без mTLS (глава 100). Отдельно для РФ: если сервис обрабатывает ПДн, по 152-ФЗ данные должны храниться и обрабатываться в РФ, а по 187-ФЗ критическая инфраструктура требует сертифицированных средств защиты — это прямо влияет на выбор gateway, брокера и схемы шифрования (глава 101). Учебный пароль `Lab#2026-vektor` в этой главе использован только как демонстрация механизма Secret; в реальном кластере Secret в etcd должен быть зашифрован (EncryptionConfiguration) или заменён внешним хранилищем секретов.

## 7. Вопросы на собеседовании

**1. «Чем микросервисы отличаются от монолита, и когда вы бы не стали дробить систему?»**
Ответ: микросервисы — независимые деплои, свои данные, общение по сети; монолит — один артефакт и одна БД, простые транзакции. Не стал бы дробить при маленькой команде, неясных границах домена, отсутствии зрелой инфраструктуры (CI/CD, наблюдаемость, gateway) — получится распределённый монолит дороже исходного. Нормальный путь: модульный монолит → выделение узких мест.
*Что хочет услышать интервьюер:* инженерное суждение, а не хайп; понимание цены распределённости.
*Красный флаг:* «микросервисы всегда лучше, это современно».

**2. «Что такое 12-factor app и какие факторы чаще всего нарушают?»**
Ответ: методология из 12 правил для эксплуатируемых приложений (12factor.net). Чаще всего нарушают III (конфиг в коде), VI (хранение состояния в процессе), IX (нет graceful shutdown и быстрого старта), XI (логи в файлы вместо stdout). Проверяю любой сервис именно по этим четырём, потому что они ломают деплой в Kubernetes.
*Что хочет услышать интервьюер:* что ты применяешь это на практике и знаешь факторы по номерам.
*Красный флаг:* перечисление «код в гите, зависимости, конфиг» и молчание про logs/disposability.

**3. «Зачем нужен API gateway, если в Kubernetes уже есть Service и Ingress?»**
Ответ: Service — внутренняя балансировка L4 внутри кластера, Ingress — L7-маршрутизация снаружи. Gateway добавляет то, что нужно на входе: TLS-терминацию, аутентификацию/JWT, rate limiting, версионирование, канареечный сплит, единые access-логи и трассировку. Ingress-контроллер — простейший gateway; Kong/APISIX/Gateway API — когда нужны политики. Service mesh — это про east-west между сервисами, не про вход.
*Что хочет услышать интервьюер:* чёткое разделение north-south и east-west, L4 vs L7.
*Красный флаг:* «gateway и mesh — одно и то же».

**4. «Брокер гарантирует at-least-once. Как не списать деньги дважды?»**
Ответ: идемпотентность консьюмера — в событии есть `event_id`, обработчик хранит таблицу обработанных id и в одной транзакции с бизнес-изменением пишет отметку; повторная доставка отбрасывается. Плюс транзакционный outbox на стороне продюсера, DLQ для «ядовитых» сообщений, мониторинг лага и алерт на рост `messages_unacknowledged`. Exactly-once на уровне бизнес-операции достигается идемпотентностью, а не настройкой брокера.
*Что хочет услышать интервьюер:* понимание, что гарантии доставки ≠ гарантии бизнес-операции.
*Красный флаг:* «включим exactly-once в Kafka и всё».

**5. «Как понять, что у нас распределённый монолит, и что делать?»**
Ответ: признаки — сервисы деплоятся только вместе, общая БД, длинные синхронные цепочки, изменение одного контракта ломает всех, релиз требует согласования всех команд. Лечение: выделить реальные границы домена, дать каждому сервису свои данные, заменить часть синхронных вызовов событиями, ввести версионирование API и контрактные тесты, навести порядок в CI/CD. Иногда честный ответ — схлопнуть часть сервисов обратно в модульный монолит.
*Что хочет услышать интервьюер:* готовность откатывать архитектурные решения назад, а не защищать их.
*Красный флаг:* «надо просто переписать на Kafka».

## 8. Источники (официальная документация)

- **12factor.net** — первоисточник методологии: разделы «The Twelve Factors», «Config», «Logs», «Disposability». Поиск: `12 factor app config logs disposability`.
- **kubernetes.io** — разделы «Ingress», «Gateway API», «Service», «Configure Liveness, Readiness and Startup Probes». Поиск: `kubernetes ingress path types`, `kubernetes Gateway API`.
- **kubernetes.github.io** (документация ingress-nginx) — раздел «Annotations»: rewrite-target, limit-rps, use-regex. Поиск: `ingress-nginx annotations rewrite-target limit-rps`. [ПРОВЕРИТЬ актуальный домен — документация контроллера переезжала]
- **microservices.io** (Chris Richardson) — паттерны «Database per service», «Transactional outbox», «Saga», «API Gateway». Поиск: `microservices.io transactional outbox saga`.
- **kafka.apache.org** — раздел «Operations» и «Basic Kafka Operations» про consumer groups и lag. Поиск: `kafka consumer groups describe lag`.
- **rabbitmq.com** — раздел «Management Command Line Tool» / `rabbitmqctl list_queues`. Поиск: `rabbitmqctl list_queues messages_ready consumers`.
- **nats.io** — раздел «NATS CLI»: `nats pub`, `nats sub`, JetStream. Поиск: `nats cli pub sub jetstream`.
- **docs.docker.com** — раздел «Dockerfile reference» (multi-stage builds, ENV, EXPOSE). Поиск: `dockerfile reference multi-stage build`.
- **martinfowler.com** — статьи «Microservices» и «MonolithFirst». Поиск: `martinfowler monolithfirst microservices`.
- **learn.microsoft.com** — раздел «.NET Microservices: Architecture for Containerized .NET Applications», глава про API Gateway pattern (полезно как общая теория независимо от стека). Поиск: `dotnet microservices architecture api gateway pattern`. [ПРОВЕРИТЬ точное название главы в текущей версии руководства]
