# Проект №1: Полный CI/CD — приложение на GitLab → сборка → Harbor → деплой в Kubernetes через ArgoCD

## 1. Введение (зачем это нужно в реальной работе)

**Тикет #4412.** Очередь DevOps. Автор — тимлид команды «Платежи» (в GitLab у него роль Maintainer в группе проекта «Вектор»).

> «Мы выкатываем релиз руками: собрал образ на своей машине, `docker push`, потом по SSH зашёл на k8s-cp01 и `kubectl apply`. В пятницу вечером забыли, что пушили, и полчаса искали, какая версия в кластере. Хотим: `git push` → через 10 минут новая версия работает, и всегда видно, из какого коммита она собрана».

Это самая частая задача, которую в реальной работе отдают джуну-девопсу в первую неделю. CI/CD — не «модный термин», а способ убрать человека из процесса выкатки: человек принимает решение (мержит merge request), а сборку, публикацию образа и доставку в кластер делает автоматика. После этой главы ты будешь уметь: собирать образ в GitLab CI, публиковать его в Harbor с неизменяемым тегом, описывать приложение Helm-чартом в отдельном git-репозитории и заставлять Argo CD приводить кластер к состоянию из git — без единого `kubectl apply` руками.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Классический CI/CD — это **push-модель**: пайплайн собрал образ и сам «толкнул» его в кластер. GitOps — **pull-модель**: в кластере живёт агент (Argo CD), который читает git-репозиторий с описанием желаемого состояния и сам приводит кластер к нему. Разница принципиальная: у пайплайна нет доступа к Kubernetes API (нечего красть), а любое изменение в кластере либо есть в git, либо будет затёрто агентом.

Компоненты и порты:

```text
┌──────────────────────────┐        ┌────────────────────────────┐
│ GIT01 10.10.10.50        │        │ HARBOR01 10.10.10.55       │
│ GitLab CE  HTTPS 443     │        │ Harbor  HTTPS 443          │
│  ├─ repo vektor-app      │        │  project: vektor           │
│  ├─ repo vektor-deploy   │        │  robot$vektor+ci (push/pull)│
│  └─ GitLab Runner        │        └─────────────▲──────────────┘
└───────────┬──────────────┘                      │ docker push (443)
            │ CI job                               │
            │ 1. clone                             │
            │ 2. docker build ─────────────────────┘
            │ 3. git push (новый тег в values.yaml)
            ▼
   repo vektor-deploy (желаемое состояние)
            │
            │ HTTPS 443 (pull, каждые ~3 мин)
            ▼
┌───────────────────────────────────────────────────────────────┐
│ k8s-cp01 10.10.10.61  + k8s-w01 .62 + k8s-w02 .63             │
│  Kubernetes API 6443 │ Argo CD (ns argocd) │ ns vektor         │
│  kubelet/containerd тянет образ из Harbor по 443              │
└───────────────────────────────────────────────────────────────┘
```

Всё общение — L7 (HTTP/HTTPS) поверх TLS: это важно, потому что Harbor на стенде с самоподписанным сертификатом, и доверие к нему придётся руками прописать в трёх местах — в CI-джобе, в containerd на нодах и в Argo CD. Ещё один слой — **теги образов**: тег должен быть неизменяемым (например, короткий SHA коммита), иначе `latest` превращает откат в гадание. Сканирование образов на уязвимости и подпись — тема урока 120, здесь мы только готовим для неё место в пайплайне.

## 3. Аналогия из жизни

Представь кондитерскую. Повар (GitLab CI) печёт торт по рецепту из книги рецептов (git-репозиторий приложения). Готовый торт он ставит в холодильник (Harbor), и на каждой коробке — номер партии, а не надпись «торт» (неизменяемый тег). На витрине (Kubernetes) должно лежать ровно столько тортов, сколько написано в накладной — а накладную составляет не повар, а кладовщик (Argo CD): он ходит в бухгалтерию (репозиторий `vektor-deploy`), читает накладную и сам выставляет витрину. Если кто-то стащил торт с витрины, кладовщик в следующем обходе заметит расхождение и положит новый.

Где аналогия ломается: кладовщик не умеет печь и не умеет лечить рецепт. Если в накладной ошибка или в образе баг, Argo CD честно и аккуратно воспроизведёт ошибку во всём кластере — и с той же скоростью.

## 4. Практика и команды

**1. Проверить, что стенд жив и доверяет самоподписанным сертификатам.**

```bash
# на UBU01
cat >> /etc/hosts <<'EOF'
10.10.10.50 gitlab.vektor.lab
10.10.10.55 harbor.vektor.lab
EOF
curl -k -s -o /dev/null -w '%{http_code}\n' https://gitlab.vektor.lab
curl -k -s -o /dev/null -w '%{http_code}\n' https://harbor.vektor.lab
```

`-k` — игнорировать проверку сертификата (только для быстрой проверки!), `-o /dev/null` — выбросить тело, `-w` — напечатать код ответа. Ожидаем `200` или `302`. Если `000` — не резолвится имя или сервис лежит. Те же строки в `/etc/hosts` добавь на GIT01, HARBOR01, k8s-cp01, k8s-w01, k8s-w02.

**2. Забрать CA Harbor и положить его в доверенные на нодах.**

```bash
# на k8s-cp01, k8s-w01, k8s-w02
mkdir -p /usr/local/share/ca-certificates
cp harbor-ca.crt /usr/local/share/ca-certificates/harbor-vektor.crt
update-ca-certificates
```

Файл `harbor-ca.crt` копируется с HARBOR01 (в Harbor он лежит в каталоге, куда ты его положил при установке — `/data/cert/` или аналогично). `update-ca-certificates` пересобирает `/etc/ssl/certs/ca-certificates.crt`. В выводе должно быть `1 added`.

**3. Настроить containerd на доверие к Harbor (это не то же самое, что системный CA).**

```bash
# на каждой ноде
mkdir -p /etc/containerd/certs.d/harbor.vektor.lab
cp harbor-ca.crt /etc/containerd/certs.d/harbor.vektor.lab/ca.crt
cat > /etc/containerd/certs.d/harbor.vektor.lab/hosts.toml <<'EOF'
server = "https://harbor.vektor.lab"
[host."https://harbor.vektor.lab"]
  ca = "/etc/containerd/certs.d/harbor.vektor.lab/ca.crt"
EOF
```

В `/etc/containerd/config.toml` должен быть указан каталог с этими настройками:

```toml
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
```

На containerd 2.x имя плагина другое (`io.containerd.cri.v1.images`) — сверься с версией: `containerd --version` `[ПРОВЕРИТЬ под свою версию]`. Затем `systemctl restart containerd` и проверка: `crictl pull harbor.vektor.lab/vektor/vektor-app:test` — должно скачаться без `x509: certificate signed by unknown authority`.

**4. Собрать образ локально и вручную опубликовать — baseline до автоматизации.**

```bash
docker login harbor.vektor.lab -u 'robot$vektor+ci' --password-stdin
docker build -t harbor.vektor.lab/vektor/vektor-app:manual-1 .
docker push harbor.vektor.lab/vektor/vektor-app:manual-1
```

`--password-stdin` — пароль не попадает в историю shell. Формат логина робота в Harbor 2.x: `robot$<проект>+<имя>` `[ПРОВЕРИТЬ в своей версии Harbor]`. `docker build -t` задаёт полное имя образа: реестр/проект/репозиторий:тег. Если `push` падает с `denied` — у робота нет прав push в проект `vektor`.

**5. Создать imagePullSecret в кластере.**

```bash
kubectl create namespace vektor
kubectl -n vektor create secret docker-registry harbor-cred \
  --docker-server=harbor.vektor.lab \
  --docker-username='robot$vektor+ci' \
  --docker-password='<пароль робота>'
```

Секрет — это объект типа `kubernetes.io/dockerconfigjson`. Проверка: `kubectl -n vektor get secret harbor-cred`. В `deployment` он подключается через `imagePullSecrets: [{name: harbor-cred}]`.

**6. CI-файл приложения (`.gitlab-ci.yml` в `vektor-app`).**

```yaml
stages: [build, bump]

variables:
  IMAGE: harbor.vektor.lab/vektor/vektor-app
  TAG: $CI_COMMIT_SHORT_SHA
  DOCKER_HOST: tcp://docker:2376
  DOCKER_TLS_CERTDIR: "/certs"
  DOCKER_TLS_VERIFY: 1
  DOCKER_CERT_PATH: "/certs/client"

default:
  image: docker:27-cli        # версию фиксируй под своё окружение [ПРОВЕРИТЬ]
  services:
    - name: docker:27-dind
      alias: docker

build-and-push:
  stage: build
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
  before_script:
    - mkdir -p /etc/docker/certs.d/harbor.vektor.lab
    - echo "$HARBOR_CA_B64" | base64 -d > /etc/docker/certs.d/harbor.vektor.lab/ca.crt
    - echo "$HARBOR_PASS" | docker login harbor.vektor.lab -u "$HARBOR_USER" --password-stdin
  script:
    - docker build -t "$IMAGE:$TAG" .
    - docker push "$IMAGE:$TAG"
  artifacts:
    reports: {}
```

`$CI_COMMIT_SHORT_SHA` — предопределённая переменная GitLab, первые 8 символов SHA коммита. `services: docker:dind` поднимает демон Docker рядом с джобой (нужен `privileged = true` у раннера). `$HARBOR_CA_B64` — твой CA в base64, положенный в Settings → CI/CD → Variables (тип Variable, флаг Masked и Protected). `rules` не даёт собирать образ из каждой ветки.

**7. Джоба, которая «пишет накладную» — обновляет тег в репозитории деплоя.**

```yaml
bump-manifests:
  stage: bump
  image: alpine/git:latest   # тег зафиксируй [ПРОВЕРИТЬ]
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
  script:
    - git clone https://oauth2:${DEPLOY_TOKEN}@gitlab.vektor.lab/vektor/vektor-deploy.git
    - cd vektor-deploy
    - sed -i "s|^  tag:.*|  tag: \"${CI_COMMIT_SHORT_SHA}\"|" charts/vektor-app/values.yaml
    - git -c user.email=ci@vektor.lab -c user.name=ci-robot commit -am "deploy ${CI_COMMIT_SHORT_SHA}"
    - git push origin main
```

`DEPLOY_TOKEN` — GitLab Deploy Token с правом `write_repository` на `vektor-deploy`, хранится как Masked+Protected переменная. `sed -i` правит ровно одну строку `tag:` в `values.yaml` — это и есть контракт между CI и Argo CD.

**8. Установить Argo CD и получить пароль администратора.**

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd get pods
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

Первая команда создаёт namespace, вторая — ставит все манифесты (это официальный способ из документации Argo CD). `jsonpath` вытаскивает поле пароля из секрета, `base64 -d` декодирует. Логин по умолчанию — `admin`.

**9. Дать Argo CD доступ к репозиторию и зарегистрировать приложение.**

```bash
kubectl -n argocd patch svc argocd-server -p '{"spec":{"type":"NodePort"}}'
kubectl -n argocd get svc argocd-server -o jsonpath='{.spec.ports[?(@.name=="https")].nodePort}'; echo

argocd login 10.10.10.61:<nodePort> --username admin --password '<пароль>' --insecure
argocd repo add https://gitlab.vektor.lab/vektor/vektor-deploy.git \
  --username deploy-argocd --password '<токен>' --insecure-skip-server-verification
argocd app create vektor-app \
  --repo https://gitlab.vektor.lab/vektor/vektor-deploy.git \
  --path charts/vektor-app \
  --dest-server https://kubernetes.default.svc \
  --dest-namespace vektor \
  --sync-policy automated --auto-prune --self-heal
```

`--dest-server https://kubernetes.default.svc` — «в тот кластер, где живёт сам Argo CD». `--sync-policy automated` включает автосинк, `--auto-prune` удаляет объекты, убранные из git, `--self-heal` возвращает ручные правки. Флаг `--insecure-skip-server-verification` — опять же костыль под самоподписанный сертификат `[ПРОВЕРИТЬ точное имя флага в своей версии CLI]`. NodePort — временное решение для лабы; нормальный вход через Ingress будет в уроке 118.

**10. Посмотреть состояние и заставить синхронизироваться.**

```bash
argocd app get vektor-app
argocd app sync vektor-app
argocd app wait vektor-app --timeout 180
kubectl -n vektor get pods,svc
kubectl -n vektor rollout status deploy/vektor-app
```

`argocd app get` печатает `Sync Status` (Synced/OutOfSync) и `Health Status` (Healthy/Degraded/Progressing). `rollout status` ждёт завершения раскатки и печатает `deployment "vektor-app" successfully rolled out`.

**11. Проверить приложение и «свежесть» версии.**

```bash
kubectl -n vektor port-forward svc/vektor-app 8080:80
curl -s http://127.0.0.1:8080/version
```

`port-forward` пробрасывает порт локально, не открывая сервис наружу. Ответ приложения должен содержать SHA коммита — так ты за секунду понимаешь, какая версия живёт в кластере.

**12. Откат.**

```bash
argocd app history vektor-app
argocd app rollback vektor-app <ID>     # [ПРОВЕРИТЬ наличие команды в твоей версии CLI]
```

Правильный GitOps-откат — `git revert` коммита в `vektor-deploy`: история остаётся линейной и честной. `argocd app rollback` быстрее, но живёт только в Argo CD и расходится с git.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

Имя снапшота: **`before-devops-116`**.

### Предусловия

- **GIT01** (10.10.10.50): GitLab CE работает, HTTPS на 443, раннер зарегистрирован, в `/etc/gitlab-runner/config.toml` у `[runners.docker]` стоит `privileged = true`.
- **HARBOR01** (10.10.10.55): Harbor работает, HTTPS на 443, есть `harbor-ca.crt`.
- **k8s-cp01 / k8s-w01 / k8s-w02** (10.10.10.61–.63): кластер из предыдущих глав в состоянии `Ready` (`kubectl get nodes`).
- **UBU01** (10.10.10.30): рабочая станция с `kubectl` (kubeconfig на кластер), `helm`, `argocd` CLI, `docker`, `git`.
- Имена `gitlab.vektor.lab` и `harbor.vektor.lab` резолвятся на всех перечисленных машинах.

Если Argo CD и приложения ещё нет — это нормально, ставим здесь. Если кластер не поднят — вернись к главам про установку Kubernetes на треке DevOps.

### Снапшот

В VMware Workstation: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-116`. Сделать на **GIT01, HARBOR01, k8s-cp01, k8s-w01, k8s-w02, UBU01**. Снапшот делать при выключенной или хотя бы «спокойной» ВМ — консистентность диска важнее скорости.

### Шаги

1. **UBU01.** Добавь записи в `/etc/hosts` (команда из п.4.1), продублируй на GIT01, HARBOR01 и все три k8s-узла. Проверь `curl -k https://harbor.vektor.lab` → код 200/302. Ожидаемый результат: имена резолвятся, сервисы отвечают.
2. **HARBOR01 (веб-интерфейс).** Projects → New Project → имя `vektor`, access level private. Затем в проекте: Robots → New Robot → имя `ci`, права push+pull на репозитории. Скопируй логин и секрет — секрет показывается один раз.
3. **k8s-узлы.** Настрой доверие containerd (п.4.3), перезапусти `containerd`. Проверь `crictl pull harbor.vektor.lab/vektor/vektor-app:manual-1` после шага 6 — должно пройти без ошибки сертификата.
4. **GIT01.** Создай группу `vektor` и два проекта: `vektor-app` (приложение + Dockerfile + `.gitlab-ci.yml`) и `vektor-deploy` (Helm-чарт). Запушь начальный код.
5. **GIT01 → Settings → CI/CD → Variables.** Добавь `HARBOR_USER`, `HARBOR_PASS`, `HARBOR_CA_B64` (Masked/Protected), `DEPLOY_TOKEN` (Deploy Token на `vektor-deploy` с `write_repository`). Ожидаемый результат: переменные видны в списке, значения скрыты.
6. **UBU01.** Ручной baseline: `docker build`, `docker login`, `docker push` с тегом `manual-1` (п.4.4). Проверь в Harbor UI: в проекте `vektor` появился артефакт `manual-1`. Ожидаемый результат: образ в Harbor.
7. **k8s-cp01.** Создай namespace и `imagePullSecret` (п.4.5). Ожидаемый результат: `kubectl -n vektor get secret harbor-cred`.
8. **GIT01 (`vektor-deploy`).** Положи минимальный чарт: `Chart.yaml`, `values.yaml` (с `image.repository`, `image.tag`), `templates/deployment.yaml`, `templates/service.yaml`. В deployment — `imagePullSecrets: [{name: harbor-cred}]` и `resources.requests`. Закоммить.
9. **k8s-cp01.** Установи Argo CD (п.4.8), дождись `Running` всех подов в `argocd`. Ожидаемый результат: 7–8 подов, все `Running`.
10. **UBU01.** `argocd login`, `argocd repo add` (креды `vektor-deploy`), `argocd app create` (п.4.9). `argocd app get vektor-app` → `Synced`/`Healthy`. Проверь `curl` через `port-forward` — приложение отвечает.
11. **GIT01 (`vektor-app`).** Измени что-нибудь в приложении (например, вывод `/version`), закоммить в `main`, запушь. Смотри CI/CD → Pipelines: джоба `build-and-push` зелёная, затем `bump-manifests` зелёная.
12. **k8s-cp01.** Подожди 1–3 минуты (интервал опроса Argo CD) и проверь: `kubectl -n vektor get pods` — новый ReplicaSet, `curl …/version` отдаёт новый SHA. Ожидаемый результат: версия в кластере = SHA последнего коммита.
13. **Проверка self-heal.** `kubectl -n vektor scale deploy/vektor-app --replicas=1`, подожди минуту. Argo CD вернёт количество реплик из git. Затем `kubectl -n vektor delete pod -l app=vektor-app` — под пересоздастся.
14. **Запись в `stand-log.md`** (см. раздел «Что изменилось на стенде»).

### Проверка

- [ ] `curl -k https://harbor.vektor.lab/api/v2.0/health` возвращает `{"status":"healthy"}` `[ПРОВЕРИТЬ путь в своей версии]`
- [ ] В Harbor есть проект `vektor`, робот `ci` с правами push/pull, артефакт с тегом = SHA коммита
- [ ] `crictl pull` на каждой ноде проходит без `x509`
- [ ] Пайплайн в `vektor-app` зелёный, в `vektor-deploy` появился коммит от `ci-robot` с новым тегом
- [ ] `argocd app get vektor-app`: `Sync Status: Synced`, `Health Status: Healthy`
- [ ] `kubectl -n vektor get pods` — все `Running`, образ с тегом = SHA
- [ ] `curl http://127.0.0.1:8080/version` через port-forward отдаёт тот же SHA
- [ ] Ручной `kubectl scale` откатывается Argo CD
- [ ] В `stand-log.md` записано: Argo CD в ns `argocd`, приложение в ns `vektor`, имена репозиториев и роботов

### Сломай и почини

**Пользователь / ВМ:** тимлид команды «Платежи» / стенд GIT01 + k8s-cp01
**Что написал:** «Пайплайн зелёный, а в кластере ничего не поменялось. Поды в `ImagePullBackOff`, релиз стоит.»
**Срочность:** средняя (успеваем до конца дня).

**Как воспроизвести поломку (намеренно):** в `vektor-deploy` поправь `values.yaml` так, чтобы `tag` был `deadbeef` — тега с таким именем в Harbor нет. Закоммить в `main`. Argo CD через пару минут сделает синк и создаст поды с несуществующим образом.

**Диагностика по порядку:**

1. `argocd app get vektor-app` → смотрим `Sync Status` (будет `Synced` — git и кластер согласованы!) и `Health Status` (`Degraded`/`Progressing`). Вывод: проблема не в Argo CD, а в образе.
2. `kubectl -n vektor get pods` → `ImagePullBackOff` или `ErrImagePull`. `kubectl -n vektor describe pod <pod>` → раздел `Events`. Ищем `Failed to pull image` и текст ошибки: `manifest unknown` (нет тега) vs `unauthorized` (права) vs `x509` (сертификат).
3. `kubectl -n vektor get deploy vektor-app -o jsonpath='{.spec.template.spec.containers[0].image}'` — какой именно образ просят.
4. На ноде: `crictl pull harbor.vektor.lab/vektor/vektor-app:deadbeef` → `manifest unknown`. Значит, тега нет.
5. В Harbor UI (или через API `curl -u robot … /api/v2.0/projects/vektor/repositories/vektor-app/artifacts`) проверяем список тегов. Тега `deadbeef` нет — подтверждено.
6. Смотрим историю `vektor-deploy`: `git log --oneline -5` — кто и когда поставил этот тег.

**Решение:** `git revert <коммит>` в `vektor-deploy` (или новый коммит с правильным SHA) → `git push`. Argo CD в течение пары минут сделает синк, поды поднимутся. Если нужно срочно — `argocd app sync vektor-app` после пуша, чтобы не ждать опроса.

**Ответ пользователю:** «Причина — в репозиторий деплоя попал тег образа, которого нет в Harbor, поэтому kubelet не смог скачать образ. Argo CD отработал корректно, кластер привёл себя ровно к тому, что было в git. Откатили коммит, версия вернулась, поды в Running. Чтобы не повторилось: сборка и деплой теперь связаны через CI — тег в `values.yaml` меняет только джоба `bump-manifests`, ручные правки в этом файле запрещены (включим protected branch на `main` в `vektor-deploy`).»

**Закрытие заявки:** тип «инцидент», причина «невалидный тег образа в репозитории деплоя», действие — revert, профилактика — protected branch + ревью MR в `vektor-deploy`.

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** группа `vektor`, проекты `vektor-app` и `vektor-deploy`; Deploy Token на `vektor-deploy`; CI/CD-переменные `HARBOR_USER`, `HARBOR_PASS`, `HARBOR_CA_B64`, `DEPLOY_TOKEN`; раннер с `privileged = true`.
- **HARBOR01 (10.10.10.55):** проект `vektor` (private), robot-аккаунт `robot$vektor+ci` (push/pull), артефакты `vektor-app` с тегами SHA.
- **k8s-cp01/w01/w02:** в `/etc/containerd/certs.d/harbor.vektor.lab/` — `hosts.toml` и `ca.crt`; CA добавлен в системное хранилище; `config_path` в `config.toml`; перезапуск containerd.
- **Кластер:** namespace `argocd` (Argo CD, svc `argocd-server` типа NodePort), namespace `vektor` (secret `harbor-cred`, deployment и service `vektor-app`), Application `vektor-app` в Argo CD.
- **UBU01:** `/etc/hosts` с именами GitLab и Harbor; локальный образ `manual-1`.

### Откат

VMware Workstation → правый клик по ВМ → **Snapshot → Snapshot Manager** → выбрать `before-devops-116` → **Restore**. Восстанавливать нужно **все** машины из списка снапшота, иначе получишь половинчатое состояние (например, Argo CD остался, а репозитория с чартом нет). Помни: восстановление снапшота стирает всё, что сделано после него, включая коммиты в GitLab и артефакты в Harbor. Если хочется сохранить работу — откатывай точечно: `argocd app delete vektor-app`, `kubectl delete ns argocd vektor`, откат конфигов containerd.

## 6. Ошибки новичков и безопасность

1. **Тег `latest`.** → Невозможно понять, что развёрнуто, и невозможно откатиться: `latest` в кластере и `latest` в Harbor — разные образы. → Тег = `$CI_COMMIT_SHORT_SHA`, плюс `imagePullPolicy: IfNotPresent`.
2. **Пароли в `.gitlab-ci.yml`.** → Секреты в истории git навсегда, даже после удаления файла. → Только CI/CD Variables с флагами Masked и Protected; в уроке 120 — Vault.
3. **Деплой из пайплайна через `kubectl apply`.** → У CI-джобы есть kubeconfig с правами на кластер; утечка токена = полный доступ. → GitOps: пайплайн пишет только в git, в кластер ходит Argo CD.
4. **Ручные правки в кластере «по-быстрому».** → `selfHeal` затрёт их в случайный момент, а «быстрый фикс» не попадёт в git. → Правим `vektor-deploy`, не кластер. Аварийный `kubectl edit` — только с последующим переносом в git.
5. **`--insecure` / `-k` навсегда.** → Привычка игнорировать TLS приводит к MITM и к тому, что настоящая ошибка сертификата маскируется. → Правильный CA в `/etc/docker/certs.d`, в `certs.d` containerd и в настройках Argo CD.
6. **Привилегированный раннер без ограничений.** → Docker-in-Docker с `privileged = true` — это фактически root на хосте раннера. → Отдельный раннер только для сборки, теги раннеров, `protected`-ветки, в идеале — kaniko или buildah без демона.
7. **Нет `resources.requests/limits` в чарте.** → Один образ «съедает» ноду и роняет соседей. → Задать requests/limits сразу, до первого релиза.
8. **Нет protected branch на `main` в `vektor-deploy`.** → Любой участник может поменять тег руками, и никто этого не заметит. → Protected branch + MR-ревью даже для «одной строчки».

**Безопасность (что нельзя в боевой среде):** хранить пароль робота Harbor в переменной без флагов Protected (её увидят джобы из чужих веток); давать роботу права `system admin` вместо push/pull в один проект; оставлять `argocd-server` доступным по NodePort в интернет без Ingress и SSO (нормальный вход — урок 118); деплоить образы без сканирования (урок 120). В лабе мы ставим NodePort и `--insecure` — только потому, что это изолированный стенд на твоём ПК.

## 7. Вопросы на собеседовании

**1. Чем GitOps отличается от обычного CI/CD с деплоем из пайплайна?**
Ответ: в push-модели CI-джоба имеет доступ к Kubernetes API и сама применяет манифесты; в pull-модели агент в кластере (Argo CD/Flux) периодически сравнивает git с фактическим состоянием и сам синхронизирует. Плюсы GitOps: у CI нет доступа к кластеру (меньше поверхность атаки), git — единственный источник правды и аудит-лог, автоматически лечится drift, откат = revert коммита. Минусы: задержка синка, нужен агент с сетевой доступностью до git, сложнее с секретами и с «императивными» операциями (миграции).
*Что хочет услышать:* понимание drift и того, что при GitOps пайплайн перестаёт быть точкой отказа при деплое.
*Красный флаг:* «GitOps — это когда есть CI/CD», без понимания разницы pull/push.

**2. Argo CD показывает OutOfSync, хотя вы ничего не меняли. Ваши действия?**
Ответ: сначала `argocd app diff vektor-app` — увидеть, какие поля разъехались. Типичные причины: ручной `kubectl edit/scale`, HPA меняет `replicas`, мутирующий admission webhook дописывает поля (sidecar injection), controller типа cert-manager меняет Secret. Дальше: если изменение легитимное — перенести его в git; если это «шум» контроллера — настроить `ignoreDifferences` в Application или `ServerSideApply`. Проверить, кто именно сделал ручное изменение — по audit log кластера.
*Что хочет услышать:* сначала `diff`, потом решение; понимание конфликта selfHeal с HPA.
*Красный флаг:* «нажму Sync, всё станет Synced» — лечит симптом, а не причину.

**3. Как сделать так, чтобы от коммита до работающего пода можно было проследить цепочку?**
Ответ: три уровня. Образ: тег = короткий SHA коммита, неизменяемость включена в Harbor (tag immutability), никогда не перезаписываем тег. Код: SHA попадает в `values.yaml` в `vektor-deploy` коммитом от CI-бота, в deployment — в аннотацию `app.kubernetes.io/version` и `gitlab.com/commit`. Рантайм: `argocd app history` и метки ревизий Argo CD, `kubectl get pod -o jsonpath` → образ с тегом. Плюс в CI — `--build-arg GIT_SHA` и эндпоинт `/version`, чтобы видеть версию без доступа к кластеру.
*Что хочет услышать:* неизменяемые теги и связка «тег = коммит», а не «мы посмотрели в чат».
*Красный флаг:* `latest` или «спросим у разработчика, что он пушил».

**4. Релиз сломался в проде. Как откатить за две минуты и что делать с миграциями БД?**
Ответ: откат — `git revert` коммита с тегом в `vektor-deploy` (или `argocd app rollback` как аварийный вариант), Argo CD применит предыдущий ReplicaSet. Если нужен мгновенный откат без ожидания синка — `kubectl -n vektor rollout undo deploy/vektor-app`, но это drift, который надо сразу закрыть коммитом. Миграции БД откатить нельзя, поэтому применять схему expand/contract: сначала обратно-совместимое изменение схемы, потом код, удаление старого — отдельным релизом позже; миграции — отдельной PreSync-джобой, чтобы они не накатывались вместе с каждой ревизией.
*Что хочет услышать:* понимание, что образ откатить легко, а данные — нет.
*Красный флаг:* «откатим и образ, и БД тем же коммитом».

**5. Как вы храните секреты в пайплайне и в кластере?**
Ответ: в CI — CI/CD Variables с Masked и Protected (доступны только джобам из protected-веток), deploy-токены с минимальными правами на конкретный проект, отдельный раннер с тегами для прод-джоб. В кластере — минимум `imagePullSecret` с ограниченным роботом; дальше по возрастанию: SOPS/Sealed Secrets (шифрованный секрет живёт в git), либо Vault с External Secrets Operator и короткоживущими токенами. Принципы: секрет никогда не в git в открытом виде, у каждого секрета свой срок жизни и своя область действия.
*Что хочет услышать:* «least privilege + не в git + ротация», а не названия инструментов.
*Красный флаг:* «секреты в приватном репозитории, туда же никто не смотрит».

## 8. Источники (официальная документация)

- **docs.gitlab.com** — разделы «CI/CD pipelines», «Predefined CI/CD variables», «GitLab Runner executors / Docker», «Deploy tokens». Поисковая фраза: `gitlab ci docker in docker dind`.
- **goharbor.io** — разделы «Harbor docs», «Working with projects», «Robot accounts», «Configuring HTTPS». Поиск: `harbor robot account push pull`.
- **argo-cd.readthedocs.io** — разделы «Getting Started», «Declarative Setup», «Private Repositories», «Sync Options», «Diffing / ignoreDifferences». Поиск: `argo cd declarative application manifest`.
- **kubernetes.io** — разделы «Pull an Image from a Private Registry», «Pod Lifecycle / Images», «Deployments». Поиск: `kubernetes image pull secret private registry`.
- **helm.sh** — «Charts» / «Chart Template Guide». Поиск: `helm chart deployment template values`.
- **containerd.io** — раздел про конфигурацию реестров (`hosts.toml`, `certs.d`), «Hosts configuration». Поиск: `containerd registry hosts toml certs.d`. Название секции изменилось между 1.x и 2.x — сверься со своей версией `[ПРОВЕРИТЬ]`.
- **docs.docker.com** — «Registry authentication», «docker build/push», «Verify repository client with certificates». Поиск: `docker registry self signed certificate certs.d`.
- **github.com/argoproj/argo-cd** — каталог `manifests/` (install.yaml). Поиск: `argocd install.yaml manifest`.
