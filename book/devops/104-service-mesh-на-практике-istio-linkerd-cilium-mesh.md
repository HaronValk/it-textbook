# Service Mesh на практике: Istio, Linkerd и Cilium Mesh — трафик, наблюдаемость, безопасность

## 1. Введение (зачем это нужно в реальной работе)

В GitLab на GIT01, в проекте `vektor/payments`, приходит issue от команды разработки платёжного сервиса:

> «После релиза 2.14 у части клиентов 502 на оплате. Логи у нас одинаковые, метрик по хендлерам нет — не понимаем, где ломается. Просим: (1) раскатывать новые версии не на всех сразу, а на 5–10% трафика, (2) видеть latency и error rate по каждому сервису отдельно, (3) шифровать трафик между `checkout` и `payments` — на аудите спросили про защиту канала. Срок — до конца спринта.»

Ровно этот набор требований (управление трафиком + наблюдаемость + шифрование между сервисами) в 2026 году закрывают service mesh'ем. Это не «модная игрушка»: в крупных компаниях mesh стоит на сотнях подов и держит канареечные релизы, таймауты, ретраи и mTLS. В маленьких командах его часто сознательно **не** ставят — и это тоже нормальное инженерное решение, которое надо уметь объяснить.

На уровне L1/L2 ты с этим столкнёшься косвенно: «в приложении 503, а сеть в порядке». На DevOps-уровне ты сам отвечаешь за mesh: ставишь control plane, описываешь политики как код, объясняешь разработчикам, почему их Job не завершается, и разбираешь TLS-ошибки между подами.

После главы ты будешь уметь: поставить Istio и Linkerd в кластер, включить sidecar-инъекцию, сделать канареечный сплит 90/10, включить строгий mTLS и точечную авторизацию, снять метрики и трейсы, диагностировать типовой `503`/`connection reset` и осознанно выбрать между Istio, Linkerd и Cilium.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Service mesh — это слой инфраструктуры, который перехватывает **весь** сетевой трафик подов и применяет к нему единые правила: маршрутизация, шифрование, ретраи, таймауты, метрики. Приложение про mesh ничего не знает и не содержит mesh-кода.

Mesh делится на две части:

- **Data plane** — то, что реально гоняет пакеты. У Istio и Linkerd это sidecar-прокси (Envoy и linkerd2-proxy соответственно), который запускается рядом с контейнером приложения в том же поде. У Cilium — eBPF-программа в ядре Linux плюс Envoy только там, где нужен L7.
- **Control plane** — раздаёт data plane'у конфигурацию и сертификаты. У Istio это `istiod`, у Linkerd — набор сервисов в namespace `linkerd` (destination, identity, proxy-injector, policy), у Cilium — агент `cilium` на каждой ноде + оператор.

```text
        ┌─────────────────────────── CONTROL PLANE ───────────────────────────┐
        │  istiod (ns istio-system на k8s-cp01)                               │
        │  Pilot: xDS-конфиг    Citadel (встроен): выдача SPIFFE-сертификатов │
        │  :15010 xDS (plain)   :15012 xDS+CA (TLS)   :15017 webhook          │
        └───────────────┬─────────────────────────────────┬───────────────────┘
                        │ xDS (gRPC/TLS)                  │ CSR / SVID (TLS)
        ┌───────────────▼─────────────────┐   ┌───────────▼──────────────────┐
        │ POD checkout-xxxx (ns shop)     │   │ POD payments-yyyy (ns shop)  │
        │ ┌────────────┐ ┌──────────────┐ │   │ ┌────────────┐ ┌───────────┐ │
        │ │ app :9898  │ │ Envoy        │ │   │ │ app :9898  │ │ Envoy     │ │
        │ │            │ │ :15001 out   │─┼───┼─│            │ │ :15006 in │ │
        │ └────────────┘ │ :15006 in    │ │   │ └────────────┘ │ mTLS      │ │
        │                │ :15000 admin │ │   │                │ :15000    │ │
        │                │ :15020 stats │ │   │                │ :15020    │ │
        │                └──────────────┘ │   │                └───────────┘ │
        └─────────────────────────────────┘   └──────────────────────────────┘
          iptables/CNI заворачивает весь in/out трафик пода в Envoy
```

Ключевые порты (Istio, их полезно помнить наизусть для диагностики): `15000` — админка Envoy, `15001` — outbound-перехват, `15006` — inbound-перехват, `15020` — склейка метрик и health, `15021` — health-пробы, `15090` — метрики самого Envoy, `15053` — DNS-прокси. У `istiod`: `15010`/`15012` — xDS, `15014` — служебные метрики, `15017` — валидирующий webhook.

**Идентичность и mTLS.** Каждому поду control plane выдаёт короткоживущий X.509-сертификат (по умолчанию на 24 часа, ротация автоматическая) с идентичностью вида `spiffe://cluster.local/ns/shop/sa/payments`. Это и есть mTLS «из коробки»: сертификат привязан к ServiceAccount, а не к IP. В Istio по умолчанию режим `PERMISSIVE` (пускает и шифрованный, и открытый трафик) — это сделано для миграции. В Linkerd mTLS включён по умолчанию. В Cilium mutual authentication есть, но помечен как beta `[ПРОВЕРИТЬ актуальный статус]`.

**Трафик.** Основные CRD Istio: `Gateway` (вход снаружи), `VirtualService` (правила маршрутизации: хосты, матчи по заголовкам, веса, ретраи, таймауты, fault injection), `DestinationRule` (сабсеты по версиям, политики балансировки, настройки TLS к апстриму). В Linkerd исторически `ServiceProfile`, сейчас курс на стандартный Gateway API (`HTTPRoute`) `[ПРОВЕРИТЬ]`. В Cilium — Gateway API и `CiliumEnvoyConfig`.

**Безопасность.** `PeerAuthentication` — как принимать трафик (`STRICT`/`PERMISSIVE`/`DISABLE`). `AuthorizationPolicy` — кто что может: allow/deny по namespace, ServiceAccount, JWT, методу и пути HTTP. Это L7-авторизация; она **не заменяет** `NetworkPolicy` — та работает на L3/L4 и остаётся первой линией.

**Наблюдаемость.** Прокси сам считает метрики: `istio_requests_total`, `istio_request_duration_milliseconds`, плюс TCP-метрики. Трейсинг — через заголовки `x-b3-*` / W3C `traceparent`. Логи доступа — опционально (`meshConfig.accessLogFile`). Kiali рисует граф сервисов поверх этих данных.

Сравнение по существу:

| | Istio | Linkerd | Cilium Mesh |
|---|---|---|---|
| Data plane | Envoy-sidecar (или ambient: ztunnel + waypoint) | Rust-прокси `linkerd2-proxy` | eBPF в ядре + Envoy для L7 |
| mTLS по умолчанию | нет, включается политикой | да | настраивается, beta |
| L7: canary/retry/fault | очень богато (VirtualService) | проще (HTTPRoute/ServiceProfile) | Gateway API, CiliumEnvoyConfig |
| Цена по ресурсам | высокая: sidecar на каждый под | низкая | низкая, агент на ноду |
| Порог входа | высокий | низкий | средний, нужен Cilium как CNI |

Правило выбора простое: нужен богатый L7 и вы готовы это эксплуатировать — Istio; нужны mTLS, метрики и ретраи без боли — Linkerd; Cilium уже стоит как CNI и хочется eBPF-наблюдаемости — Cilium Mesh.

## 3. Аналогия из жизни

Представь бизнес-центр, где сидят десятки фирм. Раньше каждая фирма сама проверяла пропуска у своих посетителей: у кого-то охранник, у кого-то консьерж, у кого-то вообще никого — и все по-разному. Service mesh — это когда в **каждый** кабинет поставили одинакового обученного охранника (sidecar). Он проверяет пропуск (mTLS), записывает в журнал, кто во сколько вошёл (метрики), может отправить посетителя в другой кабинет (маршрутизация) и умеет сказать «подождите 2 секунды и попробуйте снова» (ретраи). Инструкции все охранники получают из единого центра охраны (control plane), поэтому политика одна на весь центр. Cilium — вариант, где охранников нет в кабинетах, а в коридорах стоят турникеты, которые читают тот же пропуск и пишут тот же журнал.

Где аналогия ломается: охранник не понимает **смысла** документа — он проверяет, что пропуск валиден, но не то, имеет ли этот сотрудник право переводить деньги. Бизнес-логика авторизации остаётся в приложении.

## 4. Практика и команды

Все команды — с `k8s-cp01` (10.10.10.61), где лежит `kubeconfig` администратора кластера.

**1. Проверить, что кластер жив и чем он управляется.**

```bash
kubectl get nodes -o wide
kubectl version --short
helm version --short
istioctl version --remote=false
```

Смотрим: три ноды `k8s-cp01`, `k8s-w01`, `k8s-w02` в статусе `Ready`. Если `istioctl` не установлен — ставим CLI отдельно (бинарник в `~/.local/bin`), на сам кластер он ничего не ставит.

**2. Установить Istio (профиль `demo` — только для стенда).**

```bash
istioctl install --set profile=demo -y
istioctl verify-install
kubectl -n istio-system get deploy,pods
```

`profile=demo` ставит `istiod` и ingress-gateway с расширенным трейсингом и большим логом — в проде так нельзя, там `default` или `minimal` + аддоны отдельно. `verify-install` сверяет живой кластер с манифестом.

**3. Включить инъекцию sidecar на namespace и перезапустить поды.**

```bash
kubectl label namespace shop istio-injection=enabled --overwrite
kubectl rollout restart deploy -n shop
kubectl get pods -n shop
```

В колонке `READY` должно стать `2/2` (приложение + `istio-proxy`). Альтернатива метке namespace — аннотация на поде `sidecar.istio.io/inject: "true"`.

**4. Проверить, что прокси синхронизированы с control plane.**

```bash
istioctl proxy-status
```

Вывод — таблица `NAME / CDS / LDS / EDS / RDS / ISTIOD / VERSION`. Все колонки должны быть `SYNCED`; если `STALE` — прокси не получил конфиг, смотрим `istioctl proxy-config` и логи `istiod`.

**5. Статический анализ конфигурации (делать до применения!).**

```bash
istioctl analyze -n shop
kubectl apply --dry-run=server -f canary.yaml
```

`analyze` ловит классику: VirtualService ссылается на несуществующий сабсет, Gateway не привязан, порт не объявлен в Service (`name: http` обязателен для L7-правил).

**6. Посмотреть, что реально в конфиге Envoy.**

```bash
istioctl proxy-config routes deploy/checkout -n shop
istioctl proxy-config cluster deploy/checkout -n shop | grep payments
istioctl proxy-config listener deploy/payments -n shop | grep 9898
```

Это «рентген» mesh'а: если маршрута нет в `routes` — проблема в VirtualService, а не в приложении.

**7. Канареечный сплит 90/10 (YAML в Git).**

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata: { name: checkout, namespace: shop }
spec:
  host: checkout
  subsets:
    - { name: v1, labels: { version: v1 } }
    - { name: v2, labels: { version: v2 } }
---
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata: { name: checkout, namespace: shop }
spec:
  hosts: [checkout]
  http:
    - route:
        - destination: { host: checkout, subset: v1 }
          weight: 90
        - destination: { host: checkout, subset: v2 }
          weight: 10
      retries: { attempts: 2, perTryTimeout: 1s, retryOn: 5xx }
      timeout: 3s
```

`subsets` матчатся по лейблам пода (`version: v1`). `weight` — проценты, сумма 100. `retryOn` перечисляет коды/условия, по которым повторять.

**8. Строгий mTLS и проверка сертификата.**

```yaml
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata: { name: default, namespace: shop }
spec:
  mtls: { mode: STRICT }
```

```bash
kubectl apply -f peer-auth.yaml
istioctl proxy-config secret deploy/payments -n shop
```

В секретах увидишь `default` (сертификат пода) и `ROOTCA`; в `VALID` — `true` и время до истечения. Сертификат на 24 часа, ротация автоматическая.

**9. Точечная авторизация: только `checkout` и только GET.**

```yaml
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata: { name: payments-allow-checkout, namespace: shop }
spec:
  selector: { matchLabels: { app: payments } }
  action: ALLOW
  rules:
    - from:
        - source: { principals: ["cluster.local/ns/shop/sa/checkout"] }
      to:
        - operation: { methods: ["GET"], paths: ["/version", "/api/*"] }
```

`principals` — SPIFFE-идентичность, а не IP. Как только появляется ALLOW-политика на workload, весь остальной трафик к нему запрещается (default deny для этого селектора).

**10. Метрики: PromQL по данным прокси.**

```promql
sum(rate(istio_requests_total{reporter="destination",
  destination_service="payments.shop.svc.cluster.local",
  response_code!~"2.."}[5m])) by (response_code)

histogram_quantile(0.99, sum(rate(istio_request_duration_milliseconds_bucket{
  destination_service="payments.shop.svc.cluster.local"}[5m])) by (le))
```

`reporter="destination"` — точка зрения сервера, `"source"` — клиента. Расхождение между ними сразу показывает, где теряется запрос.

**11. Открыть дашборды Kiali/Grafana/Prometheus.**

```bash
istioctl dashboard kiali
istioctl dashboard grafana
kubectl -n istio-system port-forward svc/istio-ingressgateway 8080:80
```

`dashboard` поднимает локальный port-forward и открывает браузер. В Kiali смотрим граф `shop` и «красные» рёбра — это error rate.

**12. Linkerd — то же самое, но проще.**

```bash
linkerd install --crds | kubectl apply -f -
linkerd install | kubectl apply -f -
linkerd check
linkerd viz install | kubectl apply -f -
linkerd inject deploy.yaml | kubectl apply -f -
linkerd viz stat deploy -n shop
linkerd viz tap deploy/checkout -n shop
```

`check` — обязательный шаг, проверяет предпосылки (версия k8s, права, сертификаты). `tap` — живой поток запросов, удобнее, чем `tcpdump`, когда нужно понять, кто кому отвечает 500.

**13. Cilium: проверка и наблюдаемость.**

```bash
cilium status --wait
cilium connectivity test
hubble observe --namespace shop --verdict DROPPED
```

Cilium Service Mesh требует, чтобы Cilium был **CNI** кластера. Менять CNI на живом кластере — простой, поэтому на нашем стенде это отдельная работа (см. урок 109 про eBPF), а не шаг этой лабораторной.

**14. Fault injection и таймауты — проверка устойчивости.**

```yaml
    - fault:
        abort: { percentage: { value: 10 }, httpStatus: 503 }
      route:
        - destination: { host: checkout, subset: v1 }
```

10% запросов начнут отдавать 503 — так проверяют, что клиентские ретраи и алерты вообще работают.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер Kubernetes из предыдущих модулей: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), все `Ready`, CNI работает.
- `GIT01` (10.10.10.50) с GitLab, проект `vektor/platform/mesh` создан, есть `runner`.
- `HARBOR01` (10.10.10.55), проект `vektor` (имя проекта сверь с тем, что реально создано в Harbor). Образы `podinfo` версии 6.x зазеркалены: `10.10.10.55/vektor/podinfo:<тег>` `[ПРОВЕРИТЬ тег, который реально лежит в Harbor]`. Если образа нет — сначала `docker pull`/`skopeo copy` на машине с интернетом и `push` в Harbor.
- На `k8s-cp01`: `kubectl`, `helm`, `istioctl`, `git`.
- Namespace `shop` ещё не существует — создаём в лабе.

### Снапшот

На каждой затрагиваемой ВМ — `k8s-cp01`, `k8s-w01`, `k8s-w02` (и `GIT01`, если будешь менять настройки GitLab):

VMware Workstation → правый клик по ВМ → **Snapshot → Take Snapshot** → имя `before-devops-104`.

Дополнительно, потому что снапшот ВМ не гарантирует консистентность etcd:

```bash
sudo ETCDCTL_API=3 etcdctl snapshot save /var/backups/etcd-before-104.db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key
```

Пути к сертификатам сверь со своим кластером `[ПРОВЕРИТЬ]`.

### Шаги

1. **k8s-cp01.** `kubectl get nodes` — три ноды `Ready`. Ожидаемо: все `Ready`, `AGE` — с момента создания кластера.
2. **k8s-cp01.** Создать namespace и рабочие каталоги в репозитории:
   ```bash
   git clone git@10.10.10.50:vektor/platform/mesh.git && cd mesh
   mkdir -p apps policies traffic observability
   kubectl create namespace shop
   ```
   Ожидаемо: namespace `shop` в статусе `Active`.
3. **k8s-cp01.** Описать приложения в `apps/` — `checkout` двух версий (`version: v1` и `version: v2`, разные `PODINFO_UI_COLOR`), `payments` одной версии, плюс `ServiceAccount` для каждого и `Service` с **именованным** портом:
   ```yaml
   ports: [{ name: http, port: 9898, targetPort: 9898 }]
   ```
   `kubectl apply -f apps/`. Ожидаемо: 3 деплоймента, все поды `1/1`.
4. **k8s-cp01.** `istioctl install --set profile=demo -y` и `istioctl verify-install`. Ожидаемо: `Istio is installed successfully` / `Installation is complete`.
5. **k8s-cp01.** `kubectl label namespace shop istio-injection=enabled --overwrite && kubectl rollout restart deploy -n shop`. Ожидаемо: поды `2/2`, в `kubectl get pods -n shop` появились контейнеры `istio-proxy`.
6. **k8s-cp01.** `istioctl proxy-status` и `istioctl analyze -n shop`. Ожидаемо: все прокси `SYNCED`, `analyze` — `No validation issues found`.
7. **k8s-cp01.** Применить `traffic/destinationrule.yaml` и `traffic/virtualservice.yaml` из шага 7 раздела 4 (90/10 между `v1` и `v2`). Проверить сплит:
   ```bash
   kubectl exec -n shop deploy/payments -c app -- \
     sh -c 'for i in $(seq 1 40); do curl -s checkout:9898/version; echo; done' | sort | uniq -c
   ```
   Ожидаемо: примерно 36 против 4 (погрешность на малой выборке нормальна).
8. **k8s-cp01.** Включить STRICT mTLS (`policies/peer-auth.yaml`), затем проверить, что открытый трафик из **него**шего namespace не проходит:
   ```bash
   kubectl run curl-plain --image=curlimages/curl --restart=Never -- \
     curl -sS -m 3 http://payments.shop.svc.cluster.local:9898/version
   kubectl logs curl-plain
   ```
   Ожидаемо: `curl: (56) ... connection reset by peer` `[ПРОВЕРИТЬ точный текст curl в твоей версии]`. Из пода `checkout` (с sidecar) тот же запрос проходит.
9. **k8s-cp01.** Применить `policies/authz.yaml` (шаг 9 раздела 4: GET от `sa/checkout`). Проверить:
   ```bash
   kubectl exec -n shop deploy/checkout -c app -- curl -s -o /dev/null -w '%{http_code}\n' payments:9898/version   # 200
   kubectl exec -n shop deploy/checkout -c app -- curl -s -o /dev/null -w '%{http_code}\n' -X POST payments:9898/version  # 403
   ```
   Ожидаемо: `200` и `403` соответственно.
10. **k8s-cp01.** Развернуть аддоны наблюдаемости из архива релиза Istio (`samples/addons/prometheus.yaml`, `kiali.yaml`, `grafana.yaml`; в офлайне картинки предварительно зеркалятся в HARBOR01 `[ПРОВЕРИТЬ]`). `istioctl dashboard kiali` → граф `shop`, 3 узла, рёбра с mTLS-замком.
11. **k8s-cp01.** В Prometheus выполнить оба PromQL-запроса из шага 10 раздела 4. Ожидаемо: error rate ноль, p99 в пределах таймаута 3s.
12. **k8s-cp01.** Включить fault injection (10% 503) на минуту, посмотреть в Kiali всплеск, убрать. Ожидаемо: `istio_requests_total{response_code="503"}` ненулевой, после отката возвращается к нулю.
13. **k8s-cp01.** `git add . && git commit -m "mesh: namespace shop, canary 90/10, STRICT mTLS, authz" && git push`. В `.gitlab-ci.yml` добавить джоб `istio-analyze` с образом `istioctl` и командой `istioctl analyze -A --use-kube=false -f .` `[ПРОВЕРИТЬ ключи офлайн-анализа]`.
14. **Записать в `stand-log.md`** изменения (см. следующий подраздел).

### Проверка

- [ ] `kubectl get pods -n shop` — все поды `2/2` и `Running`.
- [ ] `istioctl proxy-status` — все `SYNCED`.
- [ ] `istioctl analyze -n shop` — нет issues.
- [ ] Тест из шага 7 даёт примерно 90/10.
- [ ] `curl-plain` из немишированного namespace получает reset.
- [ ] `POST` от `checkout` → `403`, `GET` → `200`.
- [ ] `istioctl proxy-config secret deploy/payments -n shop` показывает `default` и `ROOTCA`, `VALID = true`.
- [ ] Kiali показывает граф с mTLS на рёбрах.
- [ ] Конфиги лежат в GitLab `vektor/platform/mesh`, CI-джоб зелёный.
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

**Пользователь:** разработчик сервиса `payments` (issue в GitLab `vektor/payments`)
**ВМ / где:** под `payments-report-cron` в namespace `payments-jobs` на кластере (k8s-cp01)
**Что написал:** «После ваших изменений вчера наш ночной отчёт падает: `curl: (56) Recv failure: Connection reset by peer`. Раньше работало. До утра нужно починить, отчёт уходит в бухгалтерию.»
**Срочность:** высокая (до 09:00).

**Как воспроизвести поломку намеренно:**

```bash
kubectl create namespace payments-jobs      # метку инъекции НЕ ставим
kubectl -n payments-jobs create deploy report --image=curlimages/curl -- \
  sh -c 'while true; do curl -sS -m 3 http://payments.shop.svc.cluster.local:9898/version || sleep 5; done'
kubectl -n payments-jobs logs deploy/report --tail=5
```

**Диагностика по порядку:**

1. Что видит клиент: `kubectl -n payments-jobs logs deploy/report` → `Connection reset by peer`. Значит TCP-соединение установилось, но было сброшено — это не DNS и не маршрутизация.
2. Кто клиент: `kubectl -n payments-jobs get pods -o jsonpath='{.items[0].spec.containers[*].name}'` → один контейнер `report`. Sidecar'а нет → трафик идёт открытым текстом.
3. Что на сервере: `kubectl get peerauthentication -n shop -o yaml` → `mode: STRICT`. Сервер принимает только mTLS.
4. Лог sidecar сервера: `kubectl logs deploy/payments -c istio-proxy -n shop --tail=20` → ошибка TLS-хендшейка `[ПРОВЕРИТЬ точную формулировку в своей версии]`.
5. Подтверждение гипотезы: `kubectl get ns payments-jobs --show-labels` → метки `istio-injection` нет.

**Решение (правильное):** клиент — наш собственный код, значит проще всего дать ему sidecar:

```bash
kubectl label namespace payments-jobs istio-injection=enabled --overwrite
kubectl -n payments-jobs rollout restart deploy/report
```

Альтернатива, если клиент сторонний и не может жить с sidecar: точечное исключение через `portLevelMtls` в `PeerAuthentication` (`PERMISSIVE` на конкретном порту) или вынести вызов через egress/ingress-gateway, который сам умеет mTLS. Ослаблять весь namespace до `PERMISSIVE` — плохо.

**Ответ пользователю:** «Причина — включение обязательного mTLS в `shop`. Ваш namespace `payments-jobs` не входил в mesh, поэтому вызов шёл открытым текстом и сбрасывался на входе. Добавил namespace в mesh и перезапустил деплой; отчёт проверен, 200 OK. Если в будущем появятся внешние клиенты, которых нельзя добавить в mesh, — приходите, сделаем исключение через гейтвей, а не отключением шифрования.»

**Закрытие заявки:** в issue — что сделано, какая команда, ссылка на коммит в `vektor/platform/mesh`, и запись в `stand-log.md` о новом namespace в mesh.

### Что изменилось на стенде

- Кластер k8s: namespace `istio-system` (istiod, `istio-ingressgateway`, Prometheus, Grafana, Kiali), namespace `shop` (SA `checkout`/`payments`, деплойменты `checkout` v1 и v2, `payments`, сервисы), namespace `payments-jobs` (деплоймент `report`, в mesh).
- Политики: `PeerAuthentication/default` в `shop` — `STRICT`; `AuthorizationPolicy/payments-allow-checkout`.
- Трафик: `DestinationRule/checkout` (сабсеты v1/v2), `VirtualService/checkout` (90/10, retries, timeout 3s).
- Репозиторий GitLab `vektor/platform/mesh` с каталогами `apps/`, `policies/`, `traffic/`, `observability/` и CI-джобом анализа.
- Harbor: в проекте `vektor` образ `podinfo` (тег зафиксирован в `apps/`).
- `istioctl` установлен на `k8s-cp01`; etcd-бэкап в `/var/backups/etcd-before-104.db`.

### Откат

**Вариант А (полный, чистый).** На `k8s-cp01`, `k8s-w01`, `k8s-w02`: выключить ВМ → VMware Workstation → **Snapshot Manager → before-devops-104 → Revert**. После старта подождать 2–3 минуты: часы и etcd на снапшоте могут разъехаться, проверь `kubectl get nodes` и `date`.

**Вариант Б (только кластер, без отката ВМ).**

```bash
kubectl delete ns shop payments-jobs
istioctl uninstall --purge -y
kubectl delete ns istio-system --ignore-not-found
```

`--purge` удаляет и CRD Istio. Cluster-wide ресурсы (webhook'и) проверь: `kubectl get validatingwebhookconfiguration | grep istio`.

**Вариант В (только конфиги).** `git revert <commit>` и `kubectl apply -f` — самый частый путь в реальной работе; политики живут в Git, значит откат политики = откат коммита.

После любого отката — запись в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Ставить mesh «на всякий случай» на 5 сервисов.** → Двойной рост сложности, sidecar'ы съедают CPU/RAM, диагностика становится двухслойной. → Считай: sidecar — это ~50–100 МБ и несколько процентов CPU на каждый под `[ПРОВЕРИТЬ по своим замерам]`. Mesh оправдан, когда сервисов десятки и есть команда, которая его эксплуатирует.

2. **Оставить `PERMISSIVE` «пока не разберёмся».** → Полгода спустя половина трафика открыта, а ты думаешь, что mTLS работает. → Миграция: `PERMISSIVE` → собрать список клиентов без sidecar по метрикам `istio_requests_total{connection_security_policy="none"}` → добавить их в mesh → `STRICT`.

3. **Думать, что AuthorizationPolicy заменяет NetworkPolicy.** → L7-политика не мешает поду достучаться до чужого пода по L4, если sidecar не встал (например, под в hostNetwork). → Оставляем `NetworkPolicy` как базу (урок 99), mesh — как второй слой.

4. **Забыть про sidecar в Job/CronJob.** → Под с sidecar никогда не завершается, Job висит `Running`, ночные задачи копятся. → Либо не включать инъекцию для таких namespace, либо настроить корректный выход прокси `[ПРОВЕРИТЬ актуальный способ: EXIT_ON_ZERO_ACTIVE_CONNECTIONS / нативная поддержка sidecar-контейнеров в k8s]`.

5. **Менять CNI на Cilium «заодно» на рабочем кластере.** → Смена CNI — пересоздание сетевого слоя: все поды теряют связность, Ingress падает. → Отдельное окно, отдельный кластер, снапшот etcd, план отката.

6. **Смотреть только `istioctl analyze`.** → Он ловит конфигурацию, но не рантайм. → Всегда добавляй `proxy-status`, `proxy-config routes/cluster` и логи `istio-proxy`.

7. **Игнорировать именованные порты в Service.** → `port: 9898` без `name: http` — Istio не поймёт, что это HTTP, и L7-правила не применятся. → Всегда `name: http`/`grpc`/`tcp`.

8. **Путать mesh-mTLS с TLS «наружу».** → Трафик между подами шифруется, но клиент с улицы приходит по HTTP и уходит в лог открытым. → На периметре — TLS на ingress-gateway/балансировщике, это отдельная задача.

### Безопасность

В боевой среде нельзя: ставить `profile=demo` (лишние аддоны и расширенное логирование, включая потенциально чувствительные заголовки); хранить `kubeconfig` кластера на своей рабочей машине без ограничений; выдавать `AuthorizationPolicy` с `action: ALLOW` и пустым списком правил «чтобы проверить» (это разрешает **всё**); отключать `PeerAuthentication` целиком ради одного клиента; открывать `istioctl dashboard` на `0.0.0.0` — он делает port-forward без аутентификации, слушай только `127.0.0.1`. И помни: сертификаты mesh короткоживущие, но компрометация пода = компрометация его SPIFFE-идентичности до 24 часов, поэтому RBAC на `ServiceAccount` и `PodSecurity` остаются обязательными.

## 7. Вопросы на собеседовании

**1. Чем service mesh отличается от ingress-контроллера и API gateway?**

**Ответ.** Ingress контролирует трафик **снаружи внутрь** кластера: терминирует TLS, маршрутизирует по хосту и пути, часто умеет WAF и rate limiting. Service mesh контролирует трафик **между сервисами внутри**: mTLS с идентичностью на каждый под, ретраи, таймауты, канареечные сплиты, детальные метрики по каждой паре «источник-назначение». Они дополняют друг друга: ingress-gateway у Istio — это тот же Envoy из mesh, но на периметре; API gateway дополнительно занимается контрактами API, ключами, квотами для внешних потребителей.

**Что хочет услышать интервьюер:** чёткое разделение «восток-запад» (service-to-service) vs «север-юг» (клиент-кластер) и понимание, что mesh не отменяет ingress.

**Красный флаг:** «mesh заменяет ingress, мы nginx выкинули».

**2. Как в Istio работает mTLS и кто ротирует сертификаты?**

**Ответ.** `istiod` (встроенный CA) по CSR выдаёт каждому поду X.509-сертификат с SAN вида `spiffe://cluster.local/ns/<ns>/sa/<serviceaccount>` на 24 часа по умолчанию. Прокси сам запрашивает продление примерно за половину срока; ручная ротация не нужна. Режим приёма задаётся `PeerAuthentication` (`PERMISSIVE`/`STRICT`/`DISABLE`), проверка — `istioctl proxy-config secret`. Доверие строится на корневом CA; для мультикластера его обменивают или используют общий внешний CA.

**Что хочет услышать интервьюер:** что идентичность привязана к ServiceAccount, а не к IP; что ротация автоматическая; что `PERMISSIVE` — инструмент миграции, а не целевое состояние.

**Красный флаг:** «сертификаты лежат в Secret и мы их руками обновляем раз в год».

**3. Sidecar, ambient и eBPF — в чём разница и что выбрать?**

**Ответ.** Sidecar — отдельный контейнер-прокси в каждом поде: максимальные возможности L7, но накладные расходы по памяти/CPU и latency на каждое соединение. Ambient (Istio) делит на два уровня: `ztunnel` (L4, mTLS и телеметрия на ноду) и waypoint-прокси только там, где нужен L7 — ресурсов меньше, но часть фич и отладка отличаются. Cilium делает L3/L4 в eBPF прямо в ядре без прокси, а Envoy поднимает только под L7-политики: минимальный оверхед, но требуется Cilium как CNI и свой набор ограничений. Выбор: большой парк с богатым L7 — Istio (sidecar или ambient); минимум боли — Linkerd; уже на Cilium — Cilium Mesh.

**Что хочет услышать интервьюер:** понимание компромисса «возможности vs стоимость эксплуатации», а не «Istio самый крутой».

**Красный флаг:** «eBPF лучше, потому что быстрее» без единого слова о том, что это смена CNI и зрелость фич.

**4. Покажи, как сделать канареечный релиз на 5% трафика.**

**Ответ.** Два деплоймента с общим `app: checkout` и разными `version: v1/v2`, `Service` с именованным портом, `DestinationRule` с сабсетами по лейблу `version`, `VirtualService` с весами 95/5. Дальше — наблюдаемость: сравниваем error rate и p99 по `destination_version` в `istio_requests_total` и `istio_request_duration_milliseconds`; если хуже — меняем вес на 0 и откатываем `git revert`. В Linkerd то же самое через `HTTPRoute` (Gateway API) `[ПРОВЕРИТЬ актуальный рекомендуемый способ]`.

**Что хочет услышать интервьюер:** что канарейка — это не только веса, но и метрики по версиям плюс критерий отката, заданный заранее.

**Красный флаг:** «поставил weight 5 и всё» — без п.3 и без плана отката.

**5. Пришёл 503 из mesh. Как диагностируешь?**

**Ответ.** Порядок: (1) `istioctl analyze -n <ns>` — конфигурация; (2) `istioctl proxy-status` — все ли прокси `SYNCED`; (3) `istioctl proxy-config routes/cluster deploy/X` — есть ли маршрут и есть ли эндпоинты в сабсете; (4) логи `-c istio-proxy` — там будет `no healthy upstream`, TLS-ошибка или таймаут; (5) сравнить `istio_requests_total` с `reporter="source"` и `"destination"` — где именно теряется запрос; (6) проверить `PeerAuthentication`/`AuthorizationPolicy` и наличие sidecar у клиента. В 80% случаев это либо пустой сабсет (лейблы не совпали), либо клиент без sidecar при `STRICT`, либо таймаут меньше реального времени ответа.

**Что хочет услышать интервьюер:** системный порядок «конфиг → синхронизация → маршруты → логи → метрики», а не «перезапустил под, заработало».

**Красный флаг:** «сразу отключил mTLS, помогло».

## 8. Источники (официальная документация)

- **istio.io** — разделы «Traffic Management» (VirtualService, DestinationRule, Gateway), «Security» (PeerAuthentication, AuthorizationPolicy, mTLS), «Ambient Mode», «Tasks → Observability» (Kiali, Prometheus, distributed tracing). Поиск: `istio traffic shifting`, `istio mtls strict`, `istio ambient mode architecture`.
- **linkerd.io** — «Getting Started», «Tasks → Traffic Policy», «Authorization Policy», «Debugging with tap». Поиск: `linkerd inject`, `linkerd authorization policy`, `linkerd viz tap`.
- **cilium.io** — «Cilium Service Mesh», «Hubble», «Mutual Authentication», «Gateway API». Поиск: `cilium service mesh architecture`, `hubble observe dropped`, `cilium mutual authentication beta`.
- **kubernetes.io** — «Gateway API» (стандарт маршрутизации, на который переходят и Istio, и Linkerd, и Cilium), «Network Policies». Поиск: `kubernetes gateway api httproute`.
- **prometheus.io** — «Querying basics», `histogram_quantile`. Поиск: `prometheus histogram_quantile p99`.
- **kiali.io** — «Installation» и «Validations». Поиск: `kiali validations istio`.
- **Спецификация SPIFFE** — **spiffe.io**, раздел «SPIFFE ID». Поиск: `spiffe id workload api`.
- **Документация Harbour/Harbor** — **goharbor.io**, раздел «Projects and replication» (для зеркалирования образов в офлайн-стенд). Поиск: `harbor project replication offline`.

Всё, что касается точных версий Istio/Linkerd/Cilium на 2026 год, конкретных имён профилей установки, статуса ambient и mutual authentication в Cilium, а также точных формулировок ошибок TLS в логах Envoy — сверяй по документации на момент выполнения, помечай `[ПРОВЕРИТЬ]` и не переноси мои цифры в прод без проверки.
