# Helm: чарты, values, шаблоны, репозитории, тестирование, helmfile

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (бухгалтерия, WIN11-CL01)
> **Что написал:** «Портал согласования заявок снова лежит. В прошлый раз админ руками правил что-то в кластере, после перезапуска опять всё отвалилось. Сделайте нормально, третий раз за месяц.»
> **Срочность:** высокая

Классическая ситуация: приложение в Kubernetes разворачивали `kubectl apply` по десятку YAML-файлов, половину параметров правили через `kubectl edit`, картинки в манифестах обновляли руками, а «версия» — это дата в голове у того, кто деплоил. Понять, что именно сейчас крутится в кластере, чем оно отличается от Git и как откатиться на вчерашнее, — невозможно.

Helm — это пакетный менеджер для Kubernetes. Он превращает набор манифестов в версионируемый пакет (чарт) с параметрами (values), хранит историю каждого развёртывания (релизы) и умеет откатываться на предыдущую ревизию одной командой. После этой главы ты будешь уметь: читать и собирать чарты, управлять values и их приоритетом, шаблонизировать манифесты под разные окружения, подключать репозитории и OCI-реестр, прогонять линт и тесты чарта в CI, а также описывать весь кластер декларативно через `helmfile`. Это база для следующего урока (Kustomize) и для работы с GitOps (Argo CD / Flux), которую ты встретишь на любой DevOps-вакансии.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Три сущности, которые надо различать раз и навсегда:

- **Chart (чарт)** — архив `*.tgz` с манифестами-шаблонами, `Chart.yaml` (метаданные), `values.yaml` (значения по умолчанию) и вспомогательными файлами. Аналог `.deb`/`.rpm`, только для Kubernetes.
- **Values** — параметры, которые подставляются в шаблоны. Один чарт → много разных развёртываний.
- **Release (релиз)** — конкретный экземпляр чарта, установленный в кластер под именем в конкретном namespace. `vektor-web` в namespace `vektor-web` — это релиз.

```
                 ┌───────────────────────────────────────┐
  values.yaml    │  helm (CLI на UBU01)                  │
  -f prod.yaml   │                                       │
  --set k=v ────►│  1. слить values (приоритет)          │
                 │  2. отрендерить Go-шаблоны + Sprig    │
                 │  3. отсортировать манифесты по kind   │
                 │  4. three-way merge с живым кластером │
                 └──────────────┬────────────────────────┘
                                │ HTTPS :6443 + kubeconfig
                                ▼
                     kube-apiserver на k8s-cp01
                                │
                ┌───────────────┴─────────────────┐
                ▼                                 ▼
      Deployment / Service / ...        Secret sh.helm.release.v1.
      (реальные объекты в etcd)         <release>.v<revision>
                                        (история релиза)
```

**Структура чарта** (то, что генерирует `helm create`):

```text
vektor-web/
├── Chart.yaml          # apiVersion: v2, name, version, appVersion, dependencies
├── values.yaml         # значения по умолчанию
├── charts/             # сабчарты (зависимости), скачиваются helm dependency update
├── crds/               # CRD, ставятся до остальных манифестов
├── templates/
│   ├── _helpers.tpl    # именованные шаблоны (define/include), не рендерится в манифест
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── serviceaccount.yaml
│   ├── ingress.yaml
│   ├── NOTES.txt       # текст, который печатается после install/upgrade
│   └── tests/test-connection.yaml
└── .helmignore
```

**Шаблонизация.** Внутри `templates/` работает язык шаблонов Go плюс библиотека функций Sprig. Доступны объекты: `.Values` (values), `.Chart` (из `Chart.yaml`), `.Release` (`.Name`, `.Namespace`, `.IsInstall`, `.IsUpgrade`, `.Revision`), `.Capabilities` (`.KubeVersion`, `.APIVersions`), `.Files`, `.Template`. Функции, которые нужны каждый день: `include`, `required`, `default`, `quote`, `toYaml`, `indent`/`nindent`, `trunc`, `trimSuffix`, `tpl`, `semverCompare`.

**Приоритет values** (от слабого к сильному): `values.yaml` чарта → values родительского чарта (для сабчартов) → файлы `-f` в порядке перечисления (последний побеждает) → `--set`/`--set-string`/`--set-file`. Секция `global:` прокидывается во все сабчарты.

**Хранение состояния.** Helm 3 (в отличие от Helm 2 с Tiller'ом) не имеет серверной части: он ходит в kube-apiserver как обычный клиент. Каждая ревизия релиза сохраняется объектом **Secret** `sh.helm.release.v1.<release>.v<revision>` в namespace релиза, с метками `owner=helm`, `name=<release>`, `status=deployed|superseded|failed|pending-upgrade`. Данные там gzip+base64, **но не шифруются** — это важно для безопасности. Драйвер меняется переменной `HELM_DRIVER` (`secret` по умолчанию, `configmap`, `sql`).

**Сети и порты.** `helm` → kube-apiserver по HTTPS/6443 с kubeconfig. `helm repo add` → HTTPS GET `index.yaml` с HTTP-репозитория чартов. OCI-режим (Helm 3.8+) → HTTPS к реестру (Harbor, 443), чарт хранится как OCI-артефакт.

**Хуки и тесты.** Любой манифест с аннотацией `helm.sh/hook: pre-install|post-install|pre-upgrade|post-upgrade|pre-rollback|pre-delete|post-delete|test` выполняется в нужной фазе; `helm.sh/hook-weight` задаёт порядок, `helm.sh/hook-delete-policy` — когда удалять. `helm test` запускает поды с хуком `test`.

**helmfile** — надстройка над Helm: один YAML описывает все релизы кластера, окружения и порядок установки (`needs`). Это «Helm-часть» декларативного GitOps без установки Argo CD.

## 3. Аналогия из жизни

Helm — это IKEA. **Чарт** — коробка: инструкция, список деталей, крепёж. **values.yaml** — базовая комплектация, а `-f prod.yaml` и `--set` — опции, которые ты дозаказываешь на кассе: другой цвет фасада, петли под левую дверь. **Релиз** — уже собранный шкаф, стоящий в конкретной комнате (namespace) под конкретным именем. **Ревизии** — фотографии каждой сборки на антресоли: если перепутал полки, достаёшь фото предыдущей версии и пересобираешь по ней (`helm rollback`). А `helm get manifest` — это «а что у меня реально прикручено к стене прямо сейчас», потому что кто-то мог по-тихому вкрутить свой саморез после сборки.

Аналогия ломается в одном: IKEA не смотрит, что ты уже сделал со шкафом, а Helm перед каждой установкой делает **three-way merge** — сравнивает старый манифест из своей истории, новый из шаблонов и живое состояние в кластере, и точечно патчит разницу.

## 4. Практика и команды

Все команды — с UBU01, где настроен `kubectl` (кластер из урока 34). GUI у Helm нет — только терминал.

**1. Версия и окружение**

```bash
helm version --short
helm env
```

`helm env` печатает служебные переменные: `HELM_REPOSITORY_CONFIG` (`~/.config/helm/repositories.yaml`), `HELM_REPOSITORY_CACHE`, `HELM_NAMESPACE`, `KUBECONFIG`. Если репозитории «пропали» — смотри сюда.

**2. Подключение репозитория**

```bash
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
helm repo list
helm repo update
```

`repo add` скачивает `index.yaml` в кэш. `repo update` обновляет индексы всех репозиториев — без него `helm search` и `install` видят устаревшие версии чартов.

**3. Поиск и изучение чарта**

```bash
helm search repo metrics-server
helm show chart metrics-server/metrics-server
helm show values metrics-server/metrics-server | head -40
```

Смотреть в выводе: колонки `CHART VERSION` (версия чарта) и `APP VERSION` (версия самого приложения — это разные вещи!), а в `values.yaml` — какие параметры вообще существуют. Никогда не ставь чарт, не прочитав его values.

**4. Установка с предпросмотром**

```bash
helm install metrics-server metrics-server/metrics-server \
  -n kube-system --version <версия> \
  --set args={--kubelet-insecure-tls} \
  --dry-run=client --debug
```

`--dry-run=client` рендерит манифесты без отправки в кластер, `--debug` показывает их. `--set args={...}` передаёт список. Флаг `--kubelet-insecure-tls` отключает проверку сертификата kubelet — на стенде допустимо, в боевой среде это дыра, там ставят нормальные сертификаты.

**5. Список и статус релизов**

```bash
helm list -A
helm status vektor-web -n vektor-web
```

`helm list -A` показывает `NAME / NAMESPACE / REVISION / STATUS / CHART / APP VERSION`. Статус `deployed` — всё ок, `failed` / `pending-upgrade` — надо разбираться (см. лабораторную).

**6. Что реально применено**

```bash
helm get values vektor-web -n vektor-web        # только пользовательские values
helm get values vektor-web -n vektor-web --all  # вместе с values.yaml чарта
helm get manifest vektor-web -n vektor-web
```

`get manifest` — единственный честный способ увидеть YAML, который Helm отправил в кластер.

**7. Обновление**

```bash
helm upgrade --install vektor-web ./vektor-web -n vektor-web \
  -f values-prod.yaml --set image.tag="1.27" \
  --atomic --wait --timeout 5m
```

`--install` — создать, если релиза нет (идемпотентный деплой). `--atomic` — при ошибке автоматически откатить на предыдущую ревизию, `--wait` — ждать готовности подов (вместе они и работают). Это дефолт для CI.

**8. История и откат**

```bash
helm history vektor-web -n vektor-web
helm rollback vektor-web 2 -n vektor-web
```

```
REVISION  STATUS      CHART              DESCRIPTION
1         superseded  vektor-web-0.1.0   Install complete
2         deployed    vektor-web-0.1.1   Upgrade complete
3         failed      vektor-web-0.1.2   Upgrade failed: timed out waiting for the condition
```

`helm rollback <rel> 0` (или без номера) откатывает на предыдущую ревизию. Откат **создаёт новую ревизию**, история не переписывается.

**9. Шаблоны: локальная проверка**

```bash
helm lint ./vektor-web --strict
helm template vektor-web ./vektor-web -f values-prod.yaml --debug | less
```

`helm lint` ловит синтаксис шаблонов, отсутствующие обязательные поля, кривой `Chart.yaml`. `helm template` рендерит **без кластера** — это то, что должно гоняться в CI на каждый коммит.

**10. Фрагмент шаблона**

```yaml
# templates/deployment.yaml (фрагмент)
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      {{- include "vektor-web.selectorLabels" . | nindent 6 }}
  template:
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ required "image.tag обязателен" .Values.image.tag }}"
          ports:
            - containerPort: {{ .Values.service.port }}
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
```

`include` подставляет именованный шаблон из `_helpers.tpl`, `nindent 12` — перевод строки + отступ 12 пробелов (иначе YAML поедет). `required` роняет рендер с понятным сообщением, если параметр не задан, — лучше упасть на линте, чем в проде.

**11. Зависимости**

```bash
helm dependency list ./vektor-web
helm dependency update ./vektor-web
```

Читает `dependencies:` из `Chart.yaml`, скачивает сабчарты в `charts/` и создаёт `Chart.lock` (аналог lock-файла, фиксирует версии).

**12. Пакет и OCI-реестр**

```bash
helm package ./vektor-web -d dist/
helm registry login 10.10.10.55
helm push dist/vektor-web-0.1.0.tgz oci://10.10.10.55/charts
helm install vektor-web oci://10.10.10.55/charts/vektor-web --version 0.1.0 -n vektor-web
```

`helm push` требует Helm 3.8+ и OCI-совместимый реестр (Harbor 2.x подходит). Если Harbor без TLS, нужен флаг `--plain-http` `[ПРОВЕРИТЬ]` для своей версии Helm — в боевой среде реестр обязан быть с TLS.

**13. Тесты чарта**

```bash
helm test vektor-web -n vektor-web --logs
```

Запускает поды с аннотацией `helm.sh/hook: test`. `--logs` печатает их вывод. Тест должен проверять, что приложение реально отвечает, а не просто «под создался».

**14. helmfile**

```bash
helm plugin list                     # нужен плагин helm-diff для diff
helmfile -e dev list                 # что вообще описано
helmfile -e dev diff                 # план изменений
helmfile -e dev apply                # применить только отличия
helmfile -e dev sync                 # применить всё безусловно
helmfile -e dev template > all.yaml  # отрендерить всё в один файл
```

`helm plugin install https://github.com/databus23/helm-diff` — без плагина `helmfile diff` не работает `[ПРОВЕРИТЬ актуальный URL]`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63) — в состоянии Ready.
- `UBU01` (10.10.10.30) — рабочая станция админа: установлен `kubectl`, kubeconfig от кластера лежит в `~/.kube/config` и работает (`kubectl get nodes` отвечает).
- `HARBOR01` (10.10.10.55) — Harbor из модуля про реестр, доступен, создан проект `charts` (Settings → Projects → New Project, можно private).
- `GIT01` (10.10.10.50) — GitLab, есть проект `devops/vektor-charts`.
- Если `kubectl` на UBU01 ещё не настроен: скопируй `/etc/kubernetes/admin.conf` с `k8s-cp01` в `~/.kube/config` и поправь `server:` на `https://10.10.10.61:6443`.

### Снапшот

В VMware Workstation для каждой затрагиваемой ВМ: правый клик по ВМ → **Snapshot → Take Snapshot**, имя `before-devops-042`.

Затрагиваем: `UBU01`, `GIT01`, `HARBOR01`, `k8s-cp01`. Рабочие узлы `k8s-w01`/`k8s-w02` не меняются конфигурационно (только поды), их снапшот не обязателен — но если места на диске хватает, сделай и на них.

### Шаги

1. **UBU01. Установить Helm.**
   ```bash
   curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
   helm version --short
   ```
   Ожидаемо: `v3.x.y+g...`. Если GitHub из твоей сети недоступен — скачай архив `helm-vX.Y.Z-linux-amd64.tar.gz` с официального `get.helm.sh` `[ПРОВЕРИТЬ актуальную версию]` и распакуй бинарник в `/usr/local/bin`.

2. **UBU01. Подключить репозиторий и изучить чарт.**
   ```bash
   helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/
   helm repo update
   helm search repo metrics-server
   helm show values metrics-server/metrics-server | head -40
   ```
   Ожидаемо: в поиске минимум одна строка с `CHART VERSION` и `APP VERSION`. Запиши в блокнот обе цифры — они различаются, и на собеседовании про это спрашивают.

3. **UBU01. Поставить metrics-server.**
   ```bash
   helm install metrics-server metrics-server/metrics-server -n kube-system \
     --set args={--kubelet-insecure-tls}
   kubectl -n kube-system get pods -l app.kubernetes.io/name=metrics-server
   ```
   Ожидаемо: под `Running`. Через минуту `kubectl top nodes` начинает отдавать цифры.

4. **UBU01. Посмотреть, где Helm хранит историю.**
   ```bash
   kubectl -n kube-system get secret -l owner=helm
   helm get values metrics-server -n kube-system
   ```
   Ожидаемо: Secret `sh.helm.release.v1.metrics-server.v1` со статусом `deployed` и values, ровно те, что ты передал.

5. **UBU01. Создать свой чарт.**
   ```bash
   mkdir -p ~/devops && cd ~/devops
   helm create vektor-web
   find vektor-web -maxdepth 2 -type f | sort
   ```
   Ожидаемо: структура из раздела 2. Открой `values.yaml` и `templates/deployment.yaml` — убедись, что шаблон читает `.Values.image.repository` и `.Values.replicaCount`.

6. **UBU01. Подогнать values под себя.**
   ```yaml
   # vektor-web/values.yaml (ключевые строки)
   replicaCount: 2
   image:
     repository: nginx
     tag: "1.27"        # [ПРОВЕРИТЬ] актуальный тег
   service:
     type: ClusterIP
     port: 80
   resources:
     requests: {cpu: 50m, memory: 64Mi}
     limits:   {cpu: 200m, memory: 128Mi}
   ingress:
     enabled: false
   ```
   Ожидаемо: `--set` больше не нужен, всё в файле.

7. **UBU01. Линт и рендер без кластера.**
   ```bash
   helm lint ./vektor-web --strict
   helm template vektor-web ./vektor-web --debug | head -40
   ```
   Ожидаемо: `1 chart(s) linted, 0 chart(s) failed`; в выводе — готовый Deployment с `replicas: 2` и `image: nginx:1.27`.

8. **UBU01. Установить релиз.**
   ```bash
   helm install vektor-web ./vektor-web -n vektor-web --create-namespace --wait
   helm list -n vektor-web
   kubectl -n vektor-web get pods,svc
   ```
   Ожидаемо: `STATUS: deployed`, два пода `Running`, сервис `vektor-web` с ClusterIP.

9. **UBU01. Обновить и посмотреть историю.**
   ```bash
   helm upgrade vektor-web ./vektor-web -n vektor-web --set replicaCount=3 --atomic --wait
   helm history vektor-web -n vektor-web
   ```
   Ожидаемо: ревизия 2, `STATUS: deployed`, три пода.

10. **UBU01. Добавить тест-хук и прогнать его.**
    ```bash
    # templates/tests/test-connection.yaml — взят из helm create, проверь наличие
    helm test vektor-web -n vektor-web --logs
    ```
    Ожидаемо: `Phase: Succeeded`, в логах `wget`/`curl` получил HTTP 200 от сервиса.

11. **UBU01. Упаковать и запушить в Harbor (OCI).**
    ```bash
    helm package ./vektor-web -d dist/
    helm registry login 10.10.10.55
    helm push dist/vektor-web-0.1.0.tgz oci://10.10.10.55/charts
    ```
    Ожидаемо: `Pushed: 10.10.10.55/charts/vektor-web:0.1.0`. В Harbor в проекте `charts` появился артефакт. Если TLS самоподписанный — `--insecure-skip-tls-verify` (только на стенде!).

12. **UBU01. Коммит чарта в GitLab.**
    ```bash
    cd ~/devops && git init && git add . && git commit -m "vektor-web chart v0.1.0"
    git remote add origin git@10.10.10.50:devops/vektor-charts.git
    git push -u origin master
    ```
    Ожидаемо: чарт в Git, история изменений версионируется.

13. **UBU01. Описать оба релиза через helmfile.**
    ```yaml
    # ~/devops/helmfile.yaml
    repositories:
      - name: metrics-server
        url: https://kubernetes-sigs.github.io/metrics-server/

    environments:
      dev:
        values: [env/dev.yaml]
      prod:
        values: [env/prod.yaml]
    ---
    releases:
      - name: metrics-server
        namespace: kube-system
        chart: metrics-server/metrics-server
        version: <версия из шага 2>   # пинить обязательно
        values:
          - args: [--kubelet-insecure-tls]

      - name: vektor-web
        namespace: vektor-web
        chart: ./vektor-web
        values:
          - env/{{ .Environment.Name }}.yaml
        needs: [kube-system/metrics-server]
    ```
    ```bash
    mkdir -p env && printf 'replicaCount: 1\n' > env/dev.yaml && printf 'replicaCount: 3\n' > env/prod.yaml
    helmfile -e dev list
    helmfile -e dev diff
    helmfile -e prod template | grep -c "replicas: 3"
    ```
    Ожидаемо: `list` показывает два релиза, `diff` — изменения только для `vektor-web` (metrics-server уже стоит), `template` для prod даёт `replicas: 3`.

### Проверка

- [ ] `helm version --short` отдаёт версию 3.x
- [ ] `helm repo list` содержит `metrics-server`
- [ ] `helm list -A` показывает `metrics-server` (kube-system) и `vektor-web` (vektor-web) в статусе `deployed`
- [ ] `kubectl -n kube-system get secret -l owner=helm` содержит секрет релиза metrics-server
- [ ] `helm history vektor-web -n vektor-web` содержит минимум 2 ревизии, последняя `deployed`
- [ ] `helm test vektor-web -n vektor-web` завершается `Succeeded`
- [ ] В Harbor в проекте `charts` лежит артефакт `vektor-web` версии `0.1.0`
- [ ] В GitLab проект `devops/vektor-charts` содержит `Chart.yaml`, `values.yaml`, `templates/`
- [ ] `helmfile -e prod template | grep -c "replicas: 3"` возвращает не ноль
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

> **Пользователь:** petrova (HR)
> **ВМ:** WIN10-CL02
> **Что написала:** «Внутренний портал vektor-web не открывается, вчера работал. Браузер пишет 503.»
> **Срочность:** средняя

**Как воспроизвести поломку** (на UBU01, от лица «того самого админа»):

```bash
helm upgrade vektor-web ./vektor-web -n vektor-web --set replicaCount=5 --timeout 30s
# дождись начала раскатки и нажми Ctrl+C ровно в момент ожидания
```

Прерванный upgrade оставляет релиз в состоянии `pending-upgrade`, а объекты — в промежуточном состоянии. Дальше любая попытка обновиться упадёт с `another operation (install/upgrade/rollback) is in progress`.

**Диагностика по порядку:**

1. `helm list -n vektor-web` → видим `STATUS: pending-upgrade`. Уже половина ответа.
2. `helm history vektor-web -n vektor-web` → последняя ревизия `pending-upgrade`, предыдущая `deployed`. Запомни номер рабочей.
3. `kubectl -n vektor-web get pods` → поды есть, но `READY 0/1` или `CrashLoopBackOff` — смотрим, что именно сломалось.
4. `kubectl -n vektor-web describe pod <pod>` → секция Events, ищем `OOMKilled`, `ImagePullBackOff`, `Unhealthy`.
5. `kubectl -n vektor-web get secret -l owner=helm,name=vektor-web` → две записи: `v2` (`deployed`) и `v3` (`pending-upgrade`).
6. `kubectl -n vektor-web get endpoints vektor-web` → пусто или неполный список = сервис не за кем стоит, отсюда 503.

**Решение:**

```bash
helm rollback vektor-web 2 -n vektor-web --wait --timeout 3m
helm list -n vektor-web          # STATUS: deployed, REVISION: 4
helm test vektor-web -n vektor-web --logs
```

Если `rollback` тоже ругается на «operation in progress», значит зависшая ревизия действительно блокирует. Тогда (осознанно, только на стенде): удалить секрет именно зависшей ревизии `kubectl -n vektor-web delete secret sh.helm.release.v1.vektor-web.v3` и повторить rollback. **В боевой среде** это делают только после того, как убедились, что процесс helm действительно мёртв, а не просто «подвис» — иначе два параллельных helm начнут драться за одни объекты.

**Ответ пользователю:** «Портал восстановлен, проблема была в прерванном обновлении на стороне деплоя, данные не пострадали. Проверьте, пожалуйста, доступ и напишите, если снова увидите ошибку. Чтобы это не повторялось, переводим обновления на схему с автоматическим откатом.»

**Закрытие заявки:** категория «Kubernetes / деплой», решение — rollback релиза, root cause — прерванный `helm upgrade` без `--atomic`; в журнал изменений добавить пункт «включить `--atomic --wait` в пайплайне деплоя».

### Что изменилось на стенде

- **UBU01 (10.10.10.30):** установлен Helm 3; каталог `~/devops` с чартом `vektor-web` и `helmfile.yaml`; каталог `env/` с `dev.yaml` и `prod.yaml`; настроен `helm repo add metrics-server`.
- **k8s-cp01 / кластер:** namespace `kube-system` — релиз `metrics-server`; namespace `vektor-web` — релиз `vektor-web` (несколько ревизий, последняя `deployed`).
- **HARBOR01 (10.10.10.55):** проект `charts`, OCI-артефакт `vektor-web:0.1.0`.
- **GIT01 (10.10.10.50):** проект `devops/vektor-charts` с чартом.
- **Учётные записи/адреса:** не менялись.

### Откат

1. `helm uninstall vektor-web -n vektor-web` и `helm uninstall metrics-server -n kube-system` (если хочешь чисто убрать релизы).
2. В VMware Workstation: правый клик по ВМ → **Snapshot Manager** → выбрать `before-devops-042` → **Go to**. Повторить для `UBU01`, `GIT01`, `HARBOR01`, `k8s-cp01`.
3. Убедиться, что `kubectl get nodes` и `helm list -A` работают — стенд вернулся в исходное состояние.
4. Записать в `stand-log.md`: «урок 42 откачен снапшотом `before-devops-042`, причина — …».

## 6. Ошибки новичков и безопасность

1. **`helm upgrade` без `--atomic --wait`.** → Релиз помечен `deployed`, а поды в `CrashLoopBackOff`; пользователи видят 503, а `helm list` врёт, что всё хорошо. → Всегда `--atomic --wait --timeout`, особенно в CI.
2. **Секреты через `--set`.** → Пароль попадает в `~/.bash_history`, в `helm get values`, в Secret релиза и в логи CI. → `values.yaml` в Git без секретов, сами секреты — через External Secrets / SOPS / Sealed Secrets (урок 36).
3. **Правки руками после `helm install`.** → `kubectl edit` меняет Deployment, а Helm об этом не знает; следующий `upgrade` перезапишет правку (three-way merge) или уронит релиз. → Меняй только чарт и values, кластер приводи к состоянию из Git.
4. **Не пинить версию чарта.** → `helm install` без `--version` берёт latest; завтра там новая мажорная версия с другими values — и деплой ломается на ровном месте. → Версия чарта фиксируется в `Chart.yaml`/`helmfile.yaml`/CI.
5. **Один `values.yaml` на dev и prod.** → Прод получает `replicaCount: 1` и `resources` из песочницы. → Отдельные файлы окружений + `helmfile -e <env>`.
6. **`helm create` и деплой как есть.** → В прод уезжает чарт с дефолтным `nginx`-образом и включённым Ingress-шаблоном, который никто не проверял. → `helm create` — это каркас, его вычищают.
7. **`helm uninstall` без понимания, что удалится.** → Удаляются все объекты релиза, включая PVC (если они часть чарта); данные пропадают. → Для БД — отдельные чарты/операторы и `helm uninstall --keep-history` для разбора.
8. **Считать, что `helm rollback` откатывает всё.** → Миграции БД, внешние ресурсы и CRD не откатываются. Откат манифестов ≠ откат данных.

**Безопасность.** В боевой среде: (а) Secret релиза Helm хранит values в base64 — это **не шифрование**; если в values есть пароли, включи шифрование etcd at-rest или не держи секреты в values вообще; (б) `helm install` из чужого репозитория — это запуск произвольного кода в кластере с правами твоей kubeconfig: читай `helm template` перед установкой, пинь версии и, где возможно, проверяй подпись/происхождение чарта `[ПРОВЕРИТЬ актуальность механизма provenance в Helm 3]`; (в) `--kubelet-insecure-tls` и `--insecure-skip-tls-verify` — только для лаборатории, в проде отключают проверку сертификатов и получают MITM; (г) Helm-релизы должен ставить только CI с ограниченной ServiceAccount-ролью, а не человек с cluster-admin; (д) HTTP-репозиторий чартов без TLS = любой в сети может подменить манифесты.

## 7. Вопросы на собеседовании (5 вопросов с ответами)

**1. Чем `helm upgrade` отличается от `kubectl apply -f`?**

Ответ: `kubectl apply` — императивное применение отдельных файлов, без понятия «пакет», «версия» и «релиз». Helm оперирует релизом: рендерит шаблоны с учётом values, сортирует объекты по kind, применяет их и сохраняет ревизию в Secret. За счёт этого есть `helm history`, `helm rollback`, `helm diff` (плагин), удаление всего релиза одной командой. Кроме того, Helm делает three-way merge: сравнивает старый манифест из истории, новый из шаблонов и живое состояние кластера, поэтому корректно обрабатывает поля, удалённые из чарта, — `kubectl apply` с этим справляется хуже.

*Что хочет услышать интервьюер:* понимание, что Helm — про версионирование и жизненный цикл, а не «удобная обёртка над kubectl».
*Красный флаг:* «Helm — это просто шаблонизатор YAML».

**2. Где Helm хранит состояние релиза и что будет, если удалить этот Secret?**

Ответ: в Helm 3 — Secret `sh.helm.release.v1.<release>.v<revision>` в namespace релиза, с метками `owner=helm`, `name`, `status`, `version`. Данные gzip+base64 и не шифруются. Удалишь Secret последней ревизии — Helm «забудет» релиз: `helm list` его не покажет, `helm upgrade` попытается сделать install и упадёт на конфликте существующих объектов (`invalid ownership metadata` / `resource already exists`). Лечится либо восстановлением Secret из бэкапа, либо `helm uninstall` не поможет — придётся импортировать объекты в новый релиз через аннотации `meta.helm.sh/release-name` и `meta.helm.sh/release-namespace` или пересоздавать.

*Что хочет услышать интервьюер:* знание, что состояние лежит в кластере, а не «в helm», и что это надо бэкапить вместе с etcd.
*Красный флаг:* «Helm хранит состояние у себя на диске / в Tiller».

**3. В каком порядке применяются values и как передать секрет в чарт?**

Ответ: `values.yaml` чарта → values сабчартов/родителя → файлы `-f` в порядке перечисления (последний побеждает) → `--set`/`--set-string`/`--set-file`. Секреты через `--set` — плохая практика: они попадают в history, `helm get values` и Secret релиза. Правильно: values без секретов в Git, а сами секреты — из внешнего хранилища (External Secrets + Vault, SOPS-шифрованные файлы, Sealed Secrets) либо через смонтированные Secret, созданные вне Helm. `--set-file` годится, чтобы подставить содержимое файла, но не решает проблему хранения.

*Что хочет услышать интервьюер:* чёткий порядок приоритета и осознанное отношение к секретам.
*Красный флаг:* «Передам пароль через `--set`, это же удобно».

**4. Что делает `--atomic` и как откатить неудачный релиз?**

Ответ: `--atomic` автоматически выполняет rollback, если upgrade/install не завершился успешно; он неявно включает `--wait` (ждёт готовности ресурсов) и `--timeout`. Ручной откат: `helm history <rel>` → найти рабочую ревизию → `helm rollback <rel> <N>` (или `0` = предыдущая). Откат создаёт новую ревизию, история не переписывается. Важно: откатываются только Kubernetes-манифесты, не данные и не миграции БД — их откат планируется отдельно.

*Что хочет услышать интервьюер:* что человек понимает границы автоматического отката.
*Красный флаг:* «Rollback решает любую проблему после деплоя».

**5. Helm или Kustomize — что выберете и почему?**

Ответ: Helm — когда нужен параметризованный переиспользуемый пакет, публикация в репозиторий/OCI, версионирование и распространение (сторонние продукты: ingress-nginx, cert-manager, prometheus-стек). Kustomize — когда есть свои манифесты и нужно наложить небольшие отличия под окружения без шаблонизатора (bases/overlays, patch), он встроен в `kubectl`. Часто комбинируют: сторонние чарты ставятся Helm'ом, свои манифесты собираются Kustomize, а сверху — Argo CD/Flux, который умеет и то и другое. Для десятков релизов с окружениями и порядком установки добавляют helmfile.

*Что хочет услышать интервьюер:* отсутствие религии, понимание, что инструмент выбирается под задачу.
*Красный флаг:* «Kustomize — прошлый век, только Helm» или наоборот.

## 8. Источники (официальная документация)

- **Helm** — домен `helm.sh`, разделы: «Docs → Topics → Charts», «Charts → Template Developer Guide», «Charts → Best Practices», «Helm → Commands» (`helm install`, `helm upgrade`, `helm rollback`, `helm dependency`). Поиск: `helm charts template developer guide`, `helm best practices chart`.
- **Helm** — раздел «Topics → The Chart Repository Guide» и «Registries / OCI». Поиск: `helm chart repository index.yaml`, `helm OCI registry push`.
- **Helm** — «Charts → Hooks» и «Charts → Tests». Поиск: `helm chart hooks helm.sh/hook`, `helm test hook`.
- **Kubernetes** — домен `kubernetes.io`, раздел «Concepts → Configuration → Managing Resources» (про declarative object management и strategic merge patch). Поиск: `kubernetes three way strategic merge patch`.
- **helmfile** — домен `helmfile.readthedocs.io` `[ПРОВЕРИТЬ актуальный адрес документации]`, разделы «Configuration», «Environments», «Command Reference». Поиск: `helmfile environments needs`, `helmfile apply vs sync`.
- **Artifact Hub** — домен `artifacthub.io` — публичный каталог чартов, там же смотреть `values.yaml` и версии. Поиск: `artifacthub metrics-server`.
- **Harbor** — домен `goharbor.io`, раздел про OCI-артефакты и Helm. Поиск: `harbor helm OCI push`.
- **GitLab** — домен `docs.gitlab.com`, раздел «CI/CD → Pipeline architecture» и «Package Registry / Container Registry» — для встраивания `helm lint`/`helm template` в пайплайн. Поиск: `gitlab ci helm lint template pipeline`.

Статус проекта Helm (актуальная мажорная версия, наличие Helm 4) и статус helmfile на 2026 год проверяй на `helm.sh` и в репозитории проекта перед стартом `[ПРОВЕРИТЬ]`.
