# Стратегии деплоя: Rolling, Blue/Green, Canary, Feature Flags, A/B, автоматический откат

## 1. Введение (зачем это нужно в реальной работе)

**Заявка в поддержку «Вектора»:**

> Пользователь: ivanov (Иван Иванов, бухгалтерия)
> ВМ: WIN11-CL01
> Что написал: «Открываю портал — у меня старая версия, у Петровой новая. У неё кнопка «Оплатить» есть, у меня нет. И иногда страница просто не грузится, потом сама оживает.»
> Срочность: Высокая

Это классический след неудачного деплоя: обновили половину подов, у части пользователей новая версия, у части старая, а в момент замены кто-то попал на под, который ещё не готов принимать трафик. Ровно то, что решают стратегии деплоя.

Деплой — это не «положить новый образ в кластер». Это управляемое изменение работающей системы: сколько пользователей одновременно затронуто, как быстро мы узнаём о проблеме, за сколько секунд можем вернуться назад. В DevOps-е практике вы будете делать это каждую неделю, а в компаниях поспокойнее — каждый спринт. После этой главы вы должны уметь: выбрать стратегию под задачу (rolling / blue-green / canary / feature flag / A/B), описать её манифестом в Git, настроить readiness-пробы так, чтобы Kubernetes не пускал трафик в неготовый под, и сделать автоматический откат в пайплайне, который срабатывает без человека.

Важно понять с самого начала: стратегия деплоя — это про **риск**, а не про удобство. Каждая из них обменивает сложность инфраструктуры на скорость и дешевизну ошибки.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Начнём с того, что показывает Kubernetes. У `Deployment` есть поле `spec.strategy` с двумя вариантами: `Recreate` (сначала убить всё, потом поднять новое — простой неизбежен) и `RollingUpdate` (по умолчанию). RollingUpdate управляется двумя числами: `maxUnavailable` (сколько подов можно потерять одновременно) и `maxSurge` (сколько лишних подов можно создать сверх желаемого количества).

Ключевой элемент, без которого rolling-обновление превращается в генератор 502-х ошибок, — **readiness probe**. Это HTTP/TCP/exec-проверка, которую kubelet (агент на каждой ноде, порт 10250) выполняет периодически. Под, не прошедший проверку, не попадает в `Endpoints` сервиса, то есть трафик на него не идёт. Пока новый под не готов — старый не убивается. Именно поэтому `maxUnavailable: 0` + readiness probe = обновление без простоя.

Где это живёт в сети: клиент → Ingress (80/443, L7) → Service (ClusterIP, L4 внутри кластера) → под. Service выбирает поды по label-селектору, и это **единственный** механизм переключения трафика в «чистом» Kubernetes. Значит, любую стратегию мы строим либо на изменении количества подов с нужными метками, либо на изменении селектора сервиса.

```text
                 ┌──────────────────────────────────────────┐
   клиент ──────►│ Ingress (nginx)  :80/443     L7-маршрутизация
                 └───────────────┬──────────────────────────┘
                                 │  (canary-by-header / weight)
                                 ▼
                 ┌──────────────────────────────────────────┐
                 │ Service webapp   ClusterIP   :80          │
                 │ selector: app=webapp, version=blue        │
                 └───────┬───────────────────────┬──────────┘
                         │                       │
              ┌──────────▼────────┐   ┌──────────▼──────────┐
              │ Deploy webapp-blue│   │ Deploy webapp-green │
              │  replicas: 4      │   │  replicas: 0→4      │
              │  nginx:1.25       │   │  nginx:1.26         │
              └───────────────────┘   └─────────────────────┘
                         ▲
                         │ kubectl patch service (переключение селектора)
```

Стратегии в одной таблице:

| Стратегия | Что переключаем | Цена ошибки | Нужна доп. инфраструктура |
|---|---|---|---|
| Rolling | Поды по одному | Средняя (смесь версий) | Нет, встроено |
| Blue/Green | Селектор сервиса целиком | Низкая, откат мгновенный | ×2 ресурсов |
| Canary | Долю подов или вес в ingress | Низкая, но грубая | Иногда ingress/mesh |
| Feature flag | Код внутри приложения | Нулевая (без деплоя) | Сервис флагов |
| A/B | Группу пользователей | Нулевая | Ingress/mesh + аналитика |

Разница между canary и A/B важна на собеседованиях: **canary** — это про технический риск (выкатываем на случайную долю трафика и смотрим на ошибки/латентность), **A/B** — про продуктовую гипотезу (делим пользователей по признаку: когорте, региону, заголовку, и сравниваем метрику конверсии). Технически оба могут делаться одним механизмом, но цели и критерии успеха разные.

Наконец, **автоматический откат** — это не функция Kubernetes, а функция пайплайна. `kubectl rollout status` возвращает ненулевой код, если деплой не завершился за таймаут (`progressDeadlineSeconds`, по умолчанию 600 с). Пайплайн ловит этот код и вызывает `kubectl rollout undo`. Отсюда правило: **деплой-джоба не считается успешной, пока `rollout status` не вернул 0**.

## 3. Аналогия из жизни

Представьте, что вы меняете старый деревянный мост через реку на новый бетонный.

**Rolling** — разбираете и меняете пролёты по одному, движение идёт по остальным. Дешево, но какое-то время мост наполовину деревянный, и это заметно водителям.

**Blue/Green** — строите новый мост рядом, полностью готовый. Утром перекрываете въезд на старый и открываете новый. Если новый провалился — за минуту возвращаете знаки на старый. Дорого: два моста стоят одновременно.

**Canary** — пускаете по новому мосту сначала одну машину из десяти и смотрите, доедет ли. Не доехала — перекрываете.

**Feature flag** — мост тот же, но на нём есть шлагбаум на новой полосе, и вы открываете её не стройкой, а рубильником: сегодня для сотрудников, завтра для всех, послезавтра закрыли обратно.

**A/B** — половине водителей выдаёте навигатор с новым маршрутом и сравниваете среднее время в пути. Вопрос не «сломалось ли», а «стало ли лучше».

Аналогия перестаёт работать на одном важном месте: мост нельзя «наполовину откатить» — а приложение с изменённой схемой БД можно, и это самая опасная часть, потому что код откатывается, а миграция базы — нет.

## 4. Практика и команды

Все примеры — для неймспейса `prod` в кластере `k8s-cp01/k8s-w01/k8s-w02`, образы тянем из Harbor `10.10.10.55/vektor/webapp`.

**1. Rolling update: посмотреть и задать параметры**

```bash
kubectl -n prod get deploy webapp -o jsonpath='{.spec.strategy}'
kubectl -n prod patch deploy webapp -p '{"spec":{"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}}'
```
`maxSurge: 1` — разрешено поднять 1 лишний под; `maxUnavailable: 0` — ни один готовый под не может быть удалён. Типичный вывод jsonpath: `{"rollingUpdate":{"maxSurge":1,"maxUnavailable":0},"type":"RollingUpdate"}`. Смотрим: если `type: Recreate` — любой деплой даст простой, и это надо чинить до релиза.

**2. Смена образа (собственно деплой)**

```bash
kubectl -n prod set image deploy/webapp webapp=10.10.10.55/vektor/webapp:1.26.0
kubectl -n prod annotate deploy/webapp kubernetes.io/change-cause="release 1.26.0, ticket DEVOPS-54" --overwrite
```
`set image` меняет только строку образа — Kubernetes сам создаст новый ReplicaSet. Аннотация `change-cause` нужна, чтобы в истории ревизий было по-человечески написано, что это был за релиз (ключ `--record` в современных версиях kubectl устарел, не используйте его).

**3. Наблюдение за прогрессом**

```bash
kubectl -n prod rollout status deploy/webapp --timeout=180s
kubectl -n prod get pods -l app=webapp -w
```
Вывод: `Waiting for deployment "webapp" rollout to finish: 2 out of 4 new replicas have been updated...` → `deployment "webapp" successfully rolled out`. Смотрим на соотношение `updated / available` и на то, не зависает ли счётчик — зависание означает, что новые поды не проходят readiness.

**4. История ревизий**

```bash
kubectl -n prod rollout history deploy/webapp
kubectl -n prod rollout history deploy/webapp --revision=3
```
Вывод:
```text
REVISION  CHANGE-CAUSE
2         release 1.25.0, ticket DEVOPS-51
3         release 1.26.0, ticket DEVOPS-54
```
Смотрим на `CHANGE-CAUSE` и на количество ревизий (`revisionHistoryLimit`, по умолчанию 10). Пустой `CHANGE-CAUSE` — значит, кто-то деплоил без аннотации, и разбираться в инциденте будет тяжело.

**5. Откат**

```bash
kubectl -n prod rollout undo deploy/webapp                 # на предыдущую
kubectl -n prod rollout undo deploy/webapp --to-revision=2 # на конкретную
```
Вывод: `deployment.apps/webapp rolled back`. Важно понимать: откат — это **новая** ревизия с параметрами старой, счётчик ревизий растёт, `--to-revision` указывает на старую.

**6. Пауза и возобновление (ручной «полу-canary»)**

```bash
kubectl -n prod rollout pause deploy/webapp
kubectl -n prod get rs -l app=webapp
kubectl -n prod rollout resume deploy/webapp
```
Пока деплой на паузе, вы можете поднять всего один новый под, посмотреть на него и только потом продолжить. Вывод `get rs` покажет два ReplicaSet с разным количеством реплик — это и есть визуализация rolling-процесса.

**7. Canary на репликах (без дополнительных инструментов)**

```bash
kubectl -n prod scale deploy/webapp-stable --replicas=9
kubectl -n prod scale deploy/webapp-canary --replicas=1
for i in $(seq 1 20); do curl -s http://10.10.10.61:30080/version; done | sort | uniq -c
```
Оба деплоймента имеют общую метку `app=webapp`, сервис селектит её — трафик делится примерно 9:1. Вывод вида `18 1.26.0` / `2 1.25.0` — это и есть подтверждение. Важно: распределение **приблизительное**, по числу подов, а не по весу запросов; точный процент — задача Argo Rollouts/Flagger (урок 56).

**8. Blue/Green: переключение селектором**

```bash
kubectl -n prod patch svc webapp -p '{"spec":{"selector":{"app":"webapp","version":"green"}}}'
kubectl -n prod get endpoints webapp
```
`get endpoints` покажет IP-адреса подов, на которые сейчас идёт трафик — после патча там должны быть адреса зелёных подов. Откат — тот же патч со значением `blue`, и он занимает секунды.

**9. Feature flag через ConfigMap**

```bash
kubectl -n prod create configmap webapp-flags --from-literal=FEATURE_NEW_CHECKOUT=false
kubectl -n prod set env deploy/webapp --from=configmap/webapp-flags
kubectl -n prod exec deploy/webapp -- printenv FEATURE_NEW_CHECKOUT
```
Честное ограничение: переменные окружения из ConfigMap применяются только при пересоздании пода. Если смонтировать ConfigMap как файл (volume), kubelet обновит его в контейнере с задержкой `[ПРОВЕРИТЬ: до ~1 минуты + кэш приложения]`, но приложение должно само перечитывать файл. Для настоящих «рубильников» в проде берут Unleash или Flagsmith (open-source, self-hosted).

**10. A/B через ingress-nginx (канареечные аннотации)**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: webapp-ab
  namespace: prod
  annotations:
    nginx.ingress.kubernetes.io/canary: "true"
    nginx.ingress.kubernetes.io/canary-by-header: "x-ab-group"
    nginx.ingress.kubernetes.io/canary-by-header-value: "b"
spec:
  ingressClassName: nginx
  rules:
    - host: portal.vektor.lab
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service: {name: webapp-b, port: {number: 80}}
```
Проверка: `curl -H 'x-ab-group: b' http://portal.vektor.lab/version` попадает на B, без заголовка — на A. Набор аннотаций в вашей версии ingress-nginx `[ПРОВЕРИТЬ]` по официальной странице «Canary Deployments».

**11. Helm с атомарным откатом**

```bash
helm upgrade --install webapp ./chart -n prod \
  --set image.tag=1.26.0 --atomic --wait --timeout 5m
```
`--atomic` — если релиз не стал `deployed` за таймаут, Helm сам откатит на предыдущий рабочий релиз. Самая дешёвая автоматика из существующих.

**12. Автоматический откат в GitLab CI**

```yaml
deploy:prod:
  stage: deploy
  environment:
    name: prod
  script:
    - kubectl -n prod set image deploy/webapp webapp=$IMAGE
    - kubectl -n prod annotate deploy/webapp kubernetes.io/change-cause="$CI_COMMIT_SHORT_SHA" --overwrite
    - kubectl -n prod rollout status deploy/webapp --timeout=180s ||
      { kubectl -n prod rollout undo deploy/webapp; echo "ROLLBACK"; exit 1; }
```
Если `rollout status` не уложился в 180 с — джоба откатывает деплой и падает красным. Обратите внимание: `exit 1` обязателен, иначе GitLab посчитает джобу успешной.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Готовый кластер: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63) — из уроков 40–47.
- `GIT01` (10.10.10.50) с GitLab CE и зарегистрированным runner, `HARBOR01` (10.10.10.55) с проектом `vektor`. Проект `vektor/webapp` и пайплайн сборки вы делали в уроках 49–53.
- Если своего приложения нет — берём `nginx:1.25-alpine` и `nginx:1.26-alpine` как «v1» и «v2», скопированные в Harbor. Это законно (BSD-подобная лицензия) и достаточно для демонстрации.
- `kubectl` на UBU01 настроен на кластер (`~/.kube/config`).
- Имя снапшота: `before-devops-054`.

### Снапшот

В VMware Workstation: выделить ВМ → `VM` → `Snapshot` → `Take Snapshot` → имя `before-devops-054`. Сделать для `k8s-cp01`, `k8s-w01`, `k8s-w02`, `GIT01`.

Или из командной строки хоста (пути подставьте свои):
```cmd
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\k8s-cp01\k8s-cp01.vmx" before-devops-054
```
`[ПРОВЕРИТЬ]` точный путь к `vmrun.exe` на вашей версии Workstation.

### Шаги

1. **На UBU01.** Проверить кластер и создать неймспейс:
   `kubectl get nodes` → три ноды в `Ready`. Затем `kubectl create namespace prod` → `namespace/prod created`.

2. **На UBU01.** Создать в Git каталог `k8s/` и файл `deployment.yaml` для «v1»: `replicas: 4`, образ `10.10.10.55/vektor/webapp:1.25.0`, `strategy` с `maxSurge: 1 / maxUnavailable: 0`, readiness probe `httpGet / port 80`, `initialDelaySeconds: 3`, `periodSeconds: 5`. Применить: `kubectl -n prod apply -f k8s/deployment.yaml`. Ожидаемо: `deployment.apps/webapp created`.

3. **На UBU01.** Создать `service.yaml` (ClusterIP, `selector: {app: webapp, version: blue}`) и применить. Проверить: `kubectl -n prod get endpoints webapp` → 4 IP-адреса подов.

4. **На UBU01.** Запустить наблюдение в отдельном терминале: `kubectl -n prod get pods -l app=webapp -w`. Во втором терминале выполнить `kubectl -n prod set image deploy/webapp webapp=10.10.10.55/vektor/webapp:1.26.0`. Ожидаемо: поды появляются по одному, старые исчезают только после `1/1 Running` у новых; в первом терминале видно одновременное существование 5 подов.

5. **На UBU01.** Проверить, что простоя не было: `kubectl -n prod rollout status deploy/webapp --timeout=180s` → `successfully rolled out`. Затем `kubectl -n prod rollout history deploy/webapp` → две ревизии с разными `CHANGE-CAUSE`.

6. **На UBU01.** Смоделировать поломку: `kubectl -n prod set image deploy/webapp webapp=10.10.10.55/vektor/webapp:1.27.0` (тега нет). Ожидаемо: `rollout status` зависает, `kubectl -n prod get pods` показывает `ErrImagePull`/`ImagePullBackOff` у нового ReplicaSet, но **старые 4 пода живы** — сервис работает.

7. **На UBU01.** Откат: `kubectl -n prod rollout undo deploy/webapp` → `rolled back`. Проверить `kubectl -n prod get pods` → снова 4 `Running`.

8. **На UBU01.** Blue/Green: `kubectl -n prod apply -f k8s/deployment-green.yaml` (метка `version: green`, образ 1.26.0, `replicas: 4`). Дождаться `Ready`. Переключить: `kubectl -n prod patch svc webapp -p '{"spec":{"selector":{"app":"webapp","version":"green"}}}'`. Ожидаемо: `kubectl -n prod get endpoints webapp` показывает новые адреса, `curl` возвращает версию 1.26.0.

9. **На UBU01.** Canary на репликах: создать `deployment-canary.yaml` (метка `app: webapp`, без `version`, 1 реплика) — но тогда сервис её не увидит, потому что селектор требует `version: green`. Поэтому для канарейки создать **отдельный** сервис `webapp-canary` с селектором `track: canary` и прогнать 20 запросов через NodePort. Ожидаемо: часть ответов — версия канарейки. Записать наблюдение в `stand-log.md`: «канарейка на репликах требует общего селектора, иначе не работает».

10. **На UBU01.** Feature flag: `kubectl -n prod create configmap webapp-flags --from-literal=FEATURE_NEW_CHECKOUT=false`, подключить через `kubectl -n prod set env deploy/webapp-green --from=configmap/webapp-flags`. Проверить `kubectl -n prod exec deploy/webapp-green -- printenv FEATURE_NEW_CHECKOUT` → `false`. Поменять на `true` через `kubectl -n prod create configmap ... --dry-run=client -o yaml | kubectl apply -f -` и перезапустить поды: `kubectl -n prod rollout restart deploy/webapp-green`. Ожидаемо: значение изменилось только после рестарта — это и есть вывод лабораторной.

11. **На GIT01.** Добавить в `.gitlab-ci.yml` джобу `deploy:prod` из раздела 4 (пункт 12), закоммитить. Запустить пайплайн вручную на ветке `main`. Ожидаемо: зелёная джоба, в логах `deployment "webapp" successfully rolled out`.

12. **На UBU01.** Записать изменения в `stand-log.md` на хосте (неймспейс `prod`, деплойменты, сервисы, ConfigMap, новая джоба CI).

### Проверка

- [ ] `kubectl get nodes` — все три ноды `Ready`
- [ ] `kubectl -n prod get deploy` — `webapp-blue` и `webapp-green` существуют
- [ ] В ходе шага 4 одновременно было 5 подов (maxSurge работает)
- [ ] `kubectl -n prod rollout history deploy/webapp` показывает ≥2 ревизии с заполненным `CHANGE-CAUSE`
- [ ] При битом теге (шаг 6) старые поды остались `Running` и сервис отвечал
- [ ] `rollout undo` вернул 4 рабочих пода
- [ ] `get endpoints webapp` после патча содержит адреса зелёных подов
- [ ] `printenv FEATURE_NEW_CHECKOUT` меняется только после `rollout restart`
- [ ] Пайплайн `deploy:prod` зелёный, в логе есть `successfully rolled out`
- [ ] В `stand-log.md` появилась запись про урок 54

### Сломай и почини

> **Пользователь:** petrova (Мария Петрова, HR)
> **ВМ:** WIN10-CL02
> **Что написал:** «Портал вообще не открывается, у всей HR то же самое. Началось минут 15 назад, до этого работало.»
> **Срочность:** Критическая

**Как воспроизвести поломку.** На UBU01:
```bash
kubectl -n prod set image deploy/webapp webapp=10.10.10.55/vektor/webapp:1.26.0-broken
kubectl -n prod get pods
```
Если в манифесте `maxUnavailable: 1` и нет readiness probe (уберите её), Kubernetes начнёт убивать старые поды до того, как новые станут готовы. Результат: часть подов в `ImagePullBackOff`, часть — уже удалена, сервис отдаёт 502 всем.

**Диагностика по порядку:**

1. `kubectl -n prod get deploy webapp` → смотрим колонки `READY` (например `1/4`) и `AVAILABLE`. Это первое, что показывает масштаб.
2. `kubectl -n prod get pods -l app=webapp` → видим `ImagePullBackOff`/`ErrImagePull`. Уже понятно, что проблема не в приложении, а в образе.
3. `kubectl -n prod describe pod <имя>` → секция `Events`: `Failed to pull image ... manifest unknown`. Подтверждаем, что тега нет в Harbor.
4. `kubectl -n prod rollout history deploy/webapp` → находим последнюю рабочую ревизию.
5. `kubectl -n prod rollout undo deploy/webapp --to-revision=<N>` → `rolled back`.
6. `kubectl -n prod rollout status deploy/webapp --timeout=120s` → ждём `successfully rolled out`.
7. `kubectl -n prod get endpoints webapp` → адреса вернулись, `curl` отдаёт 200.

**Решение.** Немедленно откатить (`rollout undo`), потом чинить пайплайн: добавить в CI шаг проверки существования тега в Harbor (`skopeo inspect docker://10.10.10.55/vektor/webapp:$TAG` `[ПРОВЕРИТЬ: точный синтаксис в вашей версии skopeo]`), вернуть readiness probe и `maxUnavailable: 0`.

**Что написать пользователю:** «Мария, проблема на нашей стороне, обновление сервиса прошло с ошибкой. Откатили на предыдущую версию, портал работает. Проверьте, пожалуйста, вход; если что-то ещё не открывается — напишите, я на связи. Приносим извинения за неудобства.»

**Закрытие заявки:** категория «Инфраструктура / деплой», в комментарии — причина (несуществующий тег образа), время восстановления, ссылка на задачу в трекере на доработку пайплайна. Заявку закрывать после подтверждения от пользователя.

### Что изменилось на стенде

- Кластер `k8s-cp01/w01/w02`: создан неймспейс `prod`.
- Деплойменты `webapp` (rolling), `webapp-blue`, `webapp-green`, `webapp-canary`.
- Сервисы `webapp` (ClusterIP, селектор `version=green`), `webapp-canary`.
- ConfigMap `webapp-flags` с ключом `FEATURE_NEW_CHECKOUT`.
- `GIT01`: в проекте `vektor/webapp` появились каталог `k8s/` и джоба `deploy:prod` в `.gitlab-ci.yml`.
- `HARBOR01`: используются образы `vektor/webapp:1.25.0` и `:1.26.0`.
- На хосте обновлён `stand-log.md`.

### Откат

Если лабораторная развалилась и хочется начать с чистого листа — сначала быстро:

```bash
kubectl delete namespace prod
```

Это удалит все объекты урока, кластер останется рабочим. Если сломалось что-то на уровне нод — в VMware Workstation: `VM` → `Snapshot` → `Snapshot Manager` → выбрать `before-devops-054` → `Go to`. Восстанавливать **все** узлы кластера одновременно, иначе etcd разъедется и кластер придётся поднимать заново (про это был урок 46).

## 6. Ошибки новичков и безопасность

1. **Тег `latest` в деплое → невоспроизводимость.** Вы не знаете, какой код работает, откат `rollout undo` возвращает «тот же latest». Правильно: иммутабельные теги (`1.26.0`, git-sha), `latest` запрещён политикой.

2. **Нет readiness probe → трафик в неготовый под.** Пользователи получают 502 в момент старта приложения. Правильно: readiness probe на реальный эндпоинт, который проверяет и БД, и зависимости; `initialDelaySeconds` с запасом.

3. **`maxUnavailable: 1` при малом числе реплик → простой.** При 2 репликах это 50% ёмкости. Правильно: `maxUnavailable: 0` + `maxSurge: 1`, а реплик держать минимум 3.

4. **Blue/Green без совместимости схемы БД.** Зелёный деплой пишет в базу по-новому, синий падает, откат кода не помогает — данные уже изменены. Правильно: паттерн expand-contract (сначала добавить колонку, потом писать в обе, потом убрать старую) и обратная совместимость на 1 релиз вперёд.

5. **Canary «на глаз» без метрик.** Кто-то посмотрел `curl` три раза и решил, что всё ок. Правильно: критерий успеха заранее (p95 латентности, доля 5xx, бизнес-метрика), автоматический анализ — тема урока 56.

6. **Feature flags без владельца и TTL → «флаговый долг».** Через год в коде 200 флагов, половина мертва, и непонятно, что можно выключить. Правильно: у каждого флага владелец, дата истечения и задача на удаление.

7. **Автоматический откат без `exit 1`.** Пайплайн зелёный, деплой на самом деле откатился, никто не заметил. Правильно: откат всегда завершает джобу ошибкой + алерт.

8. **`kubectl apply` руками с ноутбука мимо Git.** Нет истории, нет ревью, «кто это сделал» — неизвестно. Правильно: только через пайплайн из Git (переход к GitOps — урок 55).

### Безопасность

- **Не давайте CI-раннеру `cluster-admin`.** Если раннер скомпрометируют, злоумышленник получит весь кластер. В норме — ServiceAccount с правами только на свой неймспейс и только на `deployments`/`services`.
- **Не деплойте образы из непроверенных источников.** В Harbor включайте сканирование уязвимостей и подпись образов (Cosign/Notation, урок 53), а в кластере — политику «только подписанные образы».
- **A/B по заголовку — это не авторизация.** `canary-by-header: x-ab-group` любой клиент подделает. Не используйте этот механизм, чтобы «пустить VIP-клиентов на новую версию».
- **Не отключайте пробы и NetworkPolicy «на время деплоя».** Именно в момент деплоя система наименее устойчива; ослабление защиты тут — худшее время.
- **Секреты в ConfigMap.** Флаги можно в ConfigMap, пароли и токены — только в Secret (а лучше во внешнем хранилище секретов, урок 57).

## 7. Вопросы на собеседовании

**1. Чем rolling update отличается от blue/green и когда вы выберете что?**

Rolling меняет поды постепенно внутри одного Deployment: старые заменяются новыми, часть пользователей какое-то время работает на новой версии, часть на старой. Ресурсов нужно мало (`maxSurge` штук), откат — `rollout undo`, но требует обратной совместимости версий и схемы БД. Blue/green держит два полных окружения и переключает трафик селектором сервиса или весом в ingress: всё или ничего, откат — секунды, но нужно ×2 ресурсов и умение работать с миграциями, совместимыми с обеими версиями. Rolling — дефолт для большинства stateless-сервисов; blue/green — когда цена ошибки высока и есть запас ресурсов; для БД и stateful — ни то, ни другое без отдельного плана.

*Что хочет услышать интервьюер:* понимание компромисса «ресурсы ↔ риск», упоминание совместимости схемы БД, конкретные поля `maxSurge`/`maxUnavailable`.
*Красный флаг:* «blue/green всегда лучше, потому что безопаснее» без упоминания стоимости и миграций.

**2. Как сделать canary-деплой в Kubernetes без Argo Rollouts и почему это неточная канарейка?**

Держим два Deployment с общей меткой `app=webapp` и разным числом реплик (9 и 1), сервис селектит общую метку — трафик делится примерно по числу подов. Неточность в том, что распределение идёт по подам, а не по запросам: долговременные соединения (keep-alive, WebSocket) залипают на одном поде, и реальный процент может сильно отличаться от 10%. Для точного веса нужен ingress-nginx (`canary-weight`), service mesh или Argo Rollouts/Flagger, которые управляют весом и анализируют метрики.

*Что хочет услышать интервьюер:* что вы знаете ограничения кустарного подхода и назовёте инструмент для точного.
*Красный флаг:* «девять подов на один — значит ровно 10% трафика».

**3. Как настроить автоматический откат в пайплайне?**

Деплой-джоба делает `kubectl set image`, ставит аннотацию `change-cause`, затем `kubectl rollout status --timeout=180s`. Если команда вернула ненулевой код — выполняется `kubectl rollout undo` и джоба завершается с `exit 1`, чтобы это было видно. Для Helm — `helm upgrade --install --atomic --wait --timeout 5m`. Обязательные условия: readiness probe (иначе rollout «успешен» для мёртвого приложения), ограниченный `progressDeadlineSeconds` и алерт на факт отката.

*Что хочет услышать интервьюер:* что откат привязан к коду выхода команды и что джоба обязана падать.
*Красный флаг:* «откатим руками, когда увидим проблему».

**4. Feature flag и деплой — в чём разница и какие риски у флагов?**

Деплой доставляет код в прод, флаг решает, будет ли этот код выполняться и для кого. Это позволяет отделить релиз от выкатки: код уже в проде, но выключен, включаем по расписанию или для конкретных когорт, выключаем мгновенно без отката образа. Риски: флаговый долг (мёртвые флаги живут годами), комбинаторный взрыв состояний при вложенных флагах, флаги как способ скрыть незаконченную работу вместо нормальных веток разработки, и требование к отказоустойчивости самого сервиса флагов — если он упал, приложение не должно падать вместе с ним (кэш последнего известного значения).

*Что хочет услышать интервьюер:* разделение «деплой ≠ релиз», понимание TTL флагов и деградации сервиса флагов.
*Красный флаг:* «флаги — это просто if в коде, никаких проблем».

**5. Что делать с миграцией БД, если релиз откатывается?**

Откат кода не откатывает данные. Поэтому миграции делаются по expand-contract: сначала добавляем новые структуры, совместимые со старой версией (expand), релиз пишет и читает так, чтобы обе версии работали; только после того, как новая версия стабильна, удаляем старое (contract). Деструктивные изменения (drop column, смена типа) выносятся в отдельный релиз на 1–2 итерации позже. Плюс: бэкап перед миграцией и проверка, что миграция идемпотентна и умеет откатываться на уровне схемы, если это возможно.

*Что хочет услышать интервьюер:* «откат приложения ≠ откат БД» и знание expand-contract.
*Красный флаг:* «всегда можно откатить миграцию, у нас же есть down-скрипты» — без оговорок про потерянные данные.

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Deployments» (стратегии, `maxSurge`, `maxUnavailable`, `progressDeadlineSeconds`); задачи «Perform a Rolling Update on a Deployment», «Rolling Back a Deployment», «Configure Liveness, Readiness and Startup Probes». Поиск: `kubernetes deployments rolling update`, `kubernetes rollout undo`.
- **kubernetes.github.io/ingress-nginx** — раздел «Canary Deployments» (аннотации `canary-by-header`, `canary-weight`). Поиск: `ingress-nginx canary deployments annotations`. `[ПРОВЕРИТЬ]` актуальный набор аннотаций для вашей версии.
- **docs.gitlab.com** — разделы «CI/CD YAML reference» (`environment`, `script`), «Environments and deployments». Поиск: `gitlab ci environments deployment`.
- **helm.sh** — раздел «Helm Upgrade» (`--atomic`, `--wait`, `--timeout`). Поиск: `helm upgrade atomic wait`.
- **goharbor.io** — раздел о тегах, иммутабельности и сканировании образов. Поиск: `harbor tag immutability vulnerability scanning`.
- **getunleash.io** и **flagsmith.com** — open-source сервисы feature-флагов, разделы «Feature flag best practices / flag lifecycle». Поиск: `feature flag lifecycle technical debt`.
- **martinfowler.com** — статьи Danilo Sato «BlueGreenDeployment» и «CanaryRelease»; также «Feature Toggles (aka Feature Flags)» Pete Hodgson. Поиск: `martinfowler canary release blue green deployment`.
- **12factor.net** — манифест, раздел «Build, release, run» (полезно для понимания, где в жизненном цикле находится деплой). Поиск: `12 factor build release run`.

Все дистрибутивы в главе — бесплатные или с открытым кодом (Kubernetes, nginx, GitLab CE, Harbor, Helm, Unleash/Flagsmith). Платные enterprise-редакции не требуются; если в вашей организации нужен коммерческий feature-flag сервис, легальные варианты есть у LaunchDarkly и российских облачных провайдеров — проверяйте доступность оплаты из РФ.
