# Конфигурация и секреты: ConfigMap, Secret, Sealed Secrets, External Secrets, SOPS

## 1. Введение (зачем это нужно в реальной работе)

**Тикет в поддержку «Вектора»:** «Пользователь: petrova / ВМ: WIN10-CL02 / Что написала: "Портал платежей отдаёт 502, вчера всё работало, я ничего не трогала" / Срочность: высокая».

Ты открываешь кластер, а поды приложения висят в `CreateContainerConfigError`. Образ тот же, код тот же, деплой никто не менял. Поменялся **конфиг**: кто-то переименовал ключ в Secret, откатил ConfigMap, или вообще удалил секрет «почистить». В контейнерном мире приложение отделено от образа, и всё, что отличает dev от prod — строки подключения, пароли, флаги, лимиты — живёт в объектах конфигурации. По статистике, большая часть инцидентов в Kubernetes после «упал под» — это не сеть и не диск, а именно конфигурация: её нет, она устарела, она не та.

Отдельная боль — секреты в Git. Классическая история: разработчик закоммитил `secret.yaml` с боевым паролем БД, через месяц репозиторий утёк, пароль меняют в пожарном порядке. Поэтому в зрелых командах секреты **никогда** не лежат в репозитории открытым текстом: их либо шифруют (SOPS), либо «запечатывают» под ключ кластера (Sealed Secrets), либо вообще не хранят в Git, а тянут из внешнего хранилища (External Secrets + Vault / Yandex Lockbox).

После этой главы ты должен уметь: отличать ConfigMap от Secret и понимать, что base64 — не шифрование; безопасно монтировать конфиг в под и знать, когда он обновится сам, а когда нужен rollout; хранить секреты в Git так, чтобы это не стоило тебе работы; и объяснить на собеседовании, чем отличаются три подхода к секретам и когда какой выбирать.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**ConfigMap и Secret — это обычные объекты API.** Они лежат в `etcd` точно так же, как Deployment или Service. Никакой магии: `kube-apiserver` (порт 6443) принимает YAML, валидирует, пишет в etcd (2379/2380 на control plane). `kubelet` на ноде (порт 10250) получает от API описание пода, видит ссылки на ConfigMap/Secret, забирает их содержимое и материализует в контейнер — либо в переменные окружения, либо в файлы внутри тома.

**Ключевое различие.** ConfigMap — открытые данные, Secret — данные, которые Kubernetes старается не показывать: в `kubectl describe` их не видно, в логах скрываются, размер ограничен 1 МиБ. Но по умолчанию **Secret в etcd хранится в base64, а это кодирование, не шифрование**. `echo 'Lab#2026-vektor' | base64` расшифрует любой, у кого есть доступ к etcd или право `get secrets` в RBAC. Поэтому в продакшене отдельно включают шифрование etcd at rest (`EncryptionConfiguration` с провайдером `aescbc`/`secretbox`/`kms`), отдельно урезают RBAC, отдельно включают audit-log.

**Три способа доставки секрета в под:**

| Способ | Обновляется на лету | Видно в `kubectl describe pod` | Когда использовать |
|---|---|---|---|
| `env` / `envFrom` | Нет, только после рестарта пода | Нет | короткие параметры, пароли |
| том (volume) | Да, с задержкой (период синхронизации kubelet, обычно до ~2 минут) | Нет | конфиг-файлы, сертификаты |
| `subPath` | Нет | Нет | единичный файл, который нельзя перезаписывать |

`immutable: true` (доступно для ConfigMap и Secret с 1.21) запрещает изменения объекта: меньше нагрузки на API-сервер и защита от случайной правки. Цена — обновлять конфиг можно только созданием нового объекта.

**Как секреты попадают в кластер из Git:**

```text
              GIT01 (10.10.10.50, GitLab)                     k8s-cp01 (10.10.10.61)
 ┌──────────────────────────────────────────┐      ┌──────────────────────────────────────┐
 │ configmap.yaml          (открыто)         │      │  kube-apiserver :6443                │
 │ secret.enc.yaml         (SOPS + age)      │─────►│        │                             │
 │ sealedsecret.yaml       (RSA, Bitnami)    │ apply│        ▼                             │
 │ externalsecret.yaml     (только ссылка)   │      │      etcd :2379  (шифрование at rest)│
 └──────────────────────────────────────────┘      │        │                             │
                                                    │        ▼                             │
   sops -d  ─────────► plaintext ──────────────┐    │   kubelet :10250                     │
   kubeseal ─► SealedSecret ─► controller ─────┼───►│        │                             │
                              (private key)    │    │        ▼                             │
   ESO ─► Vault / Lockbox / другой namespace ──┘    │   Pod: env + volume                  │
                                                    └──────────────────────────────────────┘
```

**Sealed Secrets (Bitnami).** В кластер ставится контроллер, который при старте генерирует пару RSA-ключей; приватный ключ лежит в Secret внутри кластера, публичный отдаётся наружу. Утилита `kubeseal` шифрует обычный Secret публичным ключом и выдаёт CRD `SealedSecret` (`bitnami.com/v1alpha1`), который **можно безопасно коммитить**: расшифровать его может только этот кластер. По умолчанию (`strict` scope) SealedSecret привязан к имени и namespace — в другой namespace его не переиспользуешь. Потеря приватного ключа = потеря всех запечатанных секретов, поэтому ключ бэкапят.

**External Secrets Operator (ESO).** Секреты вообще не едут через Git — в Git лежит только ссылка. ESO — оператор с CRD `SecretStore` / `ClusterSecretStore` (где брать), `ExternalSecret` (что взять и куда положить) и `PushSecret`. Он ходит в Vault, Yandex Lockbox, VK Cloud, AWS Secrets Manager, 1С-совместимые хранилища и раз в `refreshInterval` синхронизирует значения в обычный Secret. Плюс — ротация пароля происходит сама, без коммита. Минус — нужен живой внешний сервис и аутентификация (ServiceAccount / Kubernetes Auth в Vault).

**SOPS (getsops/sops).** Шифрует файл целиком или только значения (по регулярке), ключами age, GPG, AWS KMS, GCP KMS, Azure Key Vault или Vault Transit. Расшифровка — `sops -d` в CI или нативная поддержка в Flux/ArgoCD. Работает без кластера и без оператора, поэтому удобен для GitOps и для «холодного» восстановления.

## 3. Аналогия из жизни

Представь офис «Вектора». **ConfigMap** — это доска объявлений в коридоре: на ней расписание, номера кабинетов, телефон вахты. Все видят, ничего секретного, поменять может любой, у кого есть маркер.

**Secret** — конверт с паролем от сейфа, который лежит в том же шкафу, но подписан «не читать». Пока шкаф в серверной, всё нормально. Но если шкаф вынесли в коридор (не настроили RBAC и шифрование etcd) — конверт вскрывают за пять секунд, потому что «печать» на нём это base64, а не сургуч.

**Sealed Secrets** — тот же конверт, но залитый сургучом с оттиском печати кладовщика: нести по улице и даже опубликовать можно, а вскрыть — только кладовщику (контроллеру) у себя в кабинете. Потерял кладовщика (ключ) — все конверты мусор.

**External Secrets** — ты вообще не держишь конверт у себя. У тебя пропуск в кассу (Vault), и ты каждый час ходишь и спрашиваешь актуальный пароль. Начальство меняет пароль — ты просто получаешь новый, ничего не перепечатывая.

**SOPS** — ты пишешь письмо, сам зашифровываешь его своим ключом и отправляешь по обычной почте. Прочитать сможет тот, у кого есть твой ключ (ты или CI).

Аналогия перестаёт работать в одном месте: в реальном Kubernetes «кладовщик» и «касса» — это сервисы с правами, и если ты неправильно настроил RBAC, любой под в кластере может просто попросить API отдать секрет напрямую, минуя все сургучи.

## 4. Практика и команды

Все команды — с `k8s-cp01` (там `kubectl` и админский kubeconfig) или с `UBU01` (там утилиты `kubeseal`, `sops`, `age`). Namespace приложения — `vektor-config`.

**1. ConfigMap из литералов и файла**

```bash
kubectl create namespace vektor-config
kubectl create configmap payments-config -n vektor-config \
  --from-literal=APP_MODE=prod \
  --from-literal=DB_HOST=payments-db.vektor-config.svc.cluster.local \
  --from-file=app.properties
```

`--from-literal` — пара ключ=значение; `--from-file` — целый файл, имя файла становится ключом. Вывод: `configmap/payments-config created`. Проверка: `kubectl get cm payments-config -n vektor-config -o yaml`. Смотри `data:` — ровно то, что ожидал.

**2. ConfigMap как YAML с `immutable`**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: payments-config
  namespace: vektor-config
data:
  APP_MODE: "prod"
  app.properties: |
    log.level=info
    feature.newflow=false
immutable: true
```

`immutable: true` — объект нельзя изменить, только удалить и создать заново. В выводе `kubectl get cm` появится пометка `Immutable`. Для часто меняющихся конфигов так не делают.

**3. Secret: генерируем манифест, не создаём «в лоб»**

```bash
kubectl create secret generic payments-db -n vektor-config \
  --from-literal=DB_USER=payments \
  --from-literal=DB_PASSWORD='Lab#2026-vektor' \
  --dry-run=client -o yaml > payments-db.secret.yaml
```

`--dry-run=client -o yaml` — ничего не отправляет в кластер, только печатает манифест. Это правильный паттерн: манифест попадает в Git (потом шифруется), а не создаётся руками.

**4. `stringData` вместо `data`**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: payments-report
  namespace: vektor-config
type: Opaque
stringData:
  REPORT_TOKEN: "token-uchebnyy-123"
```

`stringData` принимает открытый текст и при записи сам переводит его в `data` (base64). При чтении `data` и `stringData` мержатся, приоритет у `data`. Удобно для читаемости манифеста — но именно поэтому такой файл нельзя коммитить без шифрования.

**5. Расшифровать секрет обратно (и понять, что base64 — не защита)**

```bash
kubectl get secret payments-db -n vektor-config \
  -o jsonpath='{.data.DB_PASSWORD}' | base64 -d; echo
```

Вывод: `Lab#2026-vektor`. `jsonpath` вытаскивает одно поле, `base64 -d` декодирует. Ровно это же сделает любой, у кого есть право `get secrets`.

**6. Монтирование в Deployment: env + volume**

```yaml
spec:
  template:
    spec:
      containers:
        - name: api
          image: 10.10.10.55/vektor/payments-api:1.0
          envFrom:
            - configMapRef:
                name: payments-config
            - secretRef:
                name: payments-db
          env:
            - name: REPORT_TOKEN
              valueFrom:
                secretKeyRef:
                  name: payments-report
                  key: REPORT_TOKEN
          volumeMounts:
            - name: cfg
              mountPath: /etc/app
              readOnly: true
      volumes:
        - name: cfg
          configMap:
            name: payments-config
            items:
              - key: app.properties
                path: app.properties
                mode: 0444
```

`envFrom` забирает **все** ключи объекта в переменные; `secretKeyRef` — один конкретный ключ (`optional: false` по умолчанию: нет ключа — под не стартует). `items` в томе ограничивает набор файлов и задаёт права (`0444` — только чтение).

**7. Secret для приватного registry (Harbor)**

```bash
kubectl create secret docker-registry harbor-pull -n vektor-config \
  --docker-server=10.10.10.55 \
  --docker-username=robot_vektor \
  --docker-password='Lab#2026-vektor'
```

Тип `kubernetes.io/dockerconfigjson`. Дальше в поде: `imagePullSecrets: [{name: harbor-pull}]`. Без этого `kubelet` получит `401` и под уйдёт в `ImagePullBackOff`.

**8. Включено ли шифрование etcd (проверка на control plane)**

```bash
sudo grep -i encryption /etc/kubernetes/manifests/kube-apiserver.yaml || echo "НЕ ВКЛЮЧЕНО"
sudo ETCDCTL_API=3 etcdctl \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/secrets/vektor-config/payments-db | head -c 300
```

Если в выводе `etcdctl` видно `k8s:enc:aescbc:v1:...` — шифрование работает. Если видно `k8s:enc:...` отсутствует, а в тексте читается `payments-db` и `DB_PASSWORD` — данные лежат открыто.

**9. Sealed Secrets: запечатать и применить**

```bash
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm install sealed-secrets sealed-secrets/sealed-secrets -n kube-system

kubeseal --fetch-cert \
  --controller-name sealed-secrets-controller \
  --controller-namespace kube-system > pub-cert.pem

kubeseal --cert pub-cert.pem --format yaml \
  < payments-report.secret.yaml > payments-report.sealed.yaml

kubectl apply -f payments-report.sealed.yaml
kubectl get sealedsecret -n vektor-config
kubectl get secret payments-report -n vektor-config
```

`--fetch-cert` забирает публичный ключ; `--cert` позволяет печатать SealedSecret даже без доступа к кластеру (важно для CI). После `apply` контроллер в течение секунд создаёт обычный Secret. Проверь `kubectl logs -n kube-system deploy/sealed-secrets-controller` — там будет строка про unseal.

**10. SOPS + age: шифруем только значения**

```bash
age-keygen -o ~/.config/sops/age/keys.txt      # публичный ключ печатается в stderr
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt
cat > .sops.yaml <<'EOF'
creation_rules:
  - path_regex: ^secrets/.*\.enc\.yaml$
    encrypted_regex: ^(data|stringData)$
    age: age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
EOF

sops --encrypt payments-db.secret.yaml > secrets/payments-db.enc.yaml
sops --decrypt secrets/payments-db.enc.yaml | kubectl apply -f -
```

`encrypted_regex` оставляет структуру манифеста читаемой, а шифрует только блок `data`/`stringData` — в Git видно, что за объект и как называется, но не видно значений. Без `.sops.yaml` передавай `--encrypted-regex` флагом явно.

**11. External Secrets: ссылка вместо значения**

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: k8s-store
spec:
  provider:
    kubernetes:
      remoteNamespace: eso-source
      server:
        url: https://kubernetes.default.svc
        caProvider:
          type: ConfigMap
          name: kube-root-ca.crt
          key: ca.crt
      auth:
        serviceAccount:
          name: eso-store
          namespace: eso-source
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: payments-vault
  namespace: vektor-config
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: k8s-store
    kind: ClusterSecretStore
  target:
    name: payments-vault
    creationPolicy: Owner
  data:
    - secretKey: VAULT_TOKEN
      remoteRef:
        key: payments-vault-source
        property: token
```

Провайдер `kubernetes` здесь — учебная замена Vault: ESO читает секрет из namespace `eso-source` и создаёт `payments-vault` в `vektor-config`. Для настоящего Vault меняется только блок `provider`. ⚠️ Версии API у ESO менялись (`v1beta1`, затем `v1`) — проверь `kubectl api-resources | grep external-secrets` и подставь ту, что поддерживает установленный релиз `[ПРОВЕРИТЬ под свою версию]`.

**12. Диагностика «конфиг не доехал»**

```bash
kubectl get pods -n vektor-config
kubectl describe pod -n vektor-config -l app=payments-api | tail -n 25
kubectl get events -n vektor-config --sort-by=.lastTimestamp | tail -n 20
kubectl exec -it deploy/payments-api -n vektor-config -- env | sort
```

В `describe` ищи `Events`: `CreateContainerConfigError` + `secret "payments-db" not found` — это почти всегда секрет. `env` внутри пода покажет, что реально прилетело.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Кластер из урока 34 работает: `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63), `kubectl` на `k8s-cp01`.
- `UBU01` (10.10.10.30) — рабочая станция админа, есть SSH на `k8s-cp01`.
- `GIT01` (10.10.10.50) — репозиторий `vektor/platform`.
- `HARBOR01` (10.10.10.55) — оттуда берём образ. Если своего образа `payments-api` ещё нет, возьми любой рабочий из урока 32 (`busybox`, `nginx`) и подставь его имя — логика лабы не изменится.
- Установлены `helm` (на `k8s-cp01`) и `kubeseal`, `sops`, `age` (на `UBU01`). Дистрибутивы качаются с официальных релизов; если из РФ это неудобно — скачай на ПК и перенеси по SCP, это законно и нормально.

### Снапшот

В VMware Workstation на каждой ВМ: **VM → Snapshot → Take Snapshot**, имя `before-devops-036`.

- `k8s-cp01` — обязательно (там etcd и весь state кластера)
- `k8s-w01`, `k8s-w02`
- `GIT01` — там появятся зашифрованные файлы
- `UBU01` — там появятся ключи `age` и `pub-cert.pem`

### Шаги

1. **`k8s-cp01`.** Проверь кластер: `kubectl get nodes -o wide`, `kubectl version`. Ожидаемо: три ноды в статусе `Ready`, версии client/server совпадают по мажору.
2. **`k8s-cp01`.** Создай namespace и ConfigMap: `kubectl create namespace vektor-config`, затем команду из п.4.1 с `--from-file=app.properties` (файл создай заранее: `printf 'log.level=info\nfeature.newflow=false\n' > app.properties`). Ожидаемо: `configmap/payments-config created`.
3. **`k8s-cp01`.** Сгенерируй манифест секрета через `--dry-run=client -o yaml > payments-db.secret.yaml` (п.4.3). Файл пока лежит на диске в открытом виде — это временно, в Git он в таком виде не поедет.
4. **`UBU01`.** Установи `sops` и `age`, сгенерируй ключ: `age-keygen -o ~/.config/sops/age/keys.txt`. Скопируй публичный ключ (`age1...`) — он понадобится в `.sops.yaml`. Приватный ключ не уходит никуда, кроме твоего ПК и менеджера секретов.
5. **`GIT01`/`UBU01`.** Склонируй репозиторий, положи туда `payments-config.yaml`, `payments-db.secret.yaml`, `.sops.yaml` с правилом из п.4.10, зашифруй: `sops --encrypt payments-db.secret.yaml > secrets/payments-db.enc.yaml`. Открой `secrets/payments-db.enc.yaml` глазами: структура читается, `DB_PASSWORD: ENC[AES256_GCM,...]` — значения нет. Закоммить **только** `.enc.yaml`.
6. **`k8s-cp01`.** Примени конфиг: `kubectl apply -f payments-config.yaml` и `sops --decrypt secrets/payments-db.enc.yaml | kubectl apply -f -`. Ожидаемо: `configmap/... created`, `secret/payments-db created`.
7. **`k8s-cp01`.** Установи контроллер Sealed Secrets (п.4.9), дождись `kubectl -n kube-system rollout status deploy/sealed-secrets-controller`. Затем **`UBU01`**: `kubeseal --fetch-cert ... > pub-cert.pem`, запечатай `payments-report.secret.yaml` (секрет с ключом `REPORT_TOKEN`), верни `payments-report.sealed.yaml` в репозиторий и закоммить. Ожидаемо: в Git лежит SealedSecret, читать нечего.
8. **`k8s-cp01`.** `kubectl apply -f payments-report.sealed.yaml`, затем `kubectl get sealedsecret,secret -n vektor-config`. Ожидаемо: через несколько секунд рядом с `SealedSecret` появится обычный `Secret payments-report` со статусом `Synced`.
9. **`k8s-cp01`.** Разверни тестовый Deployment (п.4.6) с тремя источниками: `envFrom` из `payments-config` и `payments-db`, том из `payments-config`, `secretKeyRef` из `payments-report`. `kubectl apply -f payments-deploy.yaml`, `kubectl rollout status deploy/payments-api -n vektor-config`. Ожидаемо: под `Running`, в `kubectl exec ... -- env` видны `APP_MODE=prod`, `DB_USER`, `REPORT_TOKEN`.
10. **`k8s-cp01`.** Проверь обновление на лету: `kubectl edit cm payments-config` → поменяй `log.level=debug`. Подожди 1–2 минуты, `kubectl exec ... -- cat /etc/app/app.properties`. Ожидаемо: файл обновился, **а переменная окружения `APP_MODE`/`log.level` — нет**. Сделай `kubectl rollout restart deploy/payments-api -n vektor-config` и убедись, что env обновился.
11. **`k8s-cp01`.** Установи ESO через Helm (`helm repo add external-secrets https://charts.external-secrets.io`, `helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace`), создай namespace `eso-source`, в нём Secret `payments-vault-source` с ключом `token`, ServiceAccount `eso-store` и Role/RoleBinding на `get/list/watch secrets`. Примени `ClusterSecretStore` + `ExternalSecret` из п.4.11. Ожидаемо: `kubectl describe externalsecret payments-vault -n vektor-config` → условие `Ready=True`, причина `SecretSynced`.
12. **`GIT01`.** Закоммить всё, что получилось: `payments-config.yaml`, `payments-report.sealed.yaml`, `secrets/payments-db.enc.yaml`, `externalsecret.yaml`, `.sops.yaml`. Открой `stand-log.md` и допиши изменения.

### Проверка

- [ ] `kubectl get cm,secret -n vektor-config` показывает `payments-config`, `payments-db`, `payments-report`, `payments-vault`
- [ ] В Git **нет** ни одного файла с открытым паролем: `git grep -i 'Lab#2026'` в репозитории ничего не находит
- [ ] `git grep -c 'ENC\['` находит `secrets/payments-db.enc.yaml` — SOPS реально зашифровал
- [ ] `kubectl get sealedsecret payments-report -n vektor-config` в статусе `Synced`
- [ ] `kubectl get externalsecret payments-vault -n vektor-config` → `Ready=True / SecretSynced`
- [ ] Под `payments-api` в `Running`, `kubectl exec ... -- env` содержит все ожидаемые переменные
- [ ] `kubectl exec ... -- cat /etc/app/app.properties` показывает актуальный `log.level`
- [ ] `sudo grep encryption /etc/kubernetes/manifests/kube-apiserver.yaml` — ты понимаешь, включено у тебя шифрование etcd или нет (по умолчанию в kubeadm — нет)

### Сломай и почини

> **Пользователь:** petrova / **ВМ:** WIN10-CL02 / **Что написала:** «Портал платежей не открывается, страница с ошибкой 502, начала новую заявку — то же самое. Работа стоит» / **Срочность:** высокая

**Ломаем намеренно (на `k8s-cp01`):**

```bash
kubectl delete secret payments-db -n vektor-config
kubectl delete pod -n vektor-config -l app=payments-api
```

**Диагностика по шагам:**

1. Что видит пользователь: `kubectl get ingress -n vektor-config` — Ingress есть, значит 502 идёт от бэкенда, а не от DNS.
2. Есть ли поды: `kubectl get pods -n vektor-config` → `payments-api-xxxx   0/1   CreateContainerConfigError`. Запомни: это **не** `CrashLoopBackOff` — контейнер даже не пытался стартовать.
3. Почему: `kubectl describe pod -n vektor-config <pod>` → в `Events` строка `secret "payments-db" not found`. Это прямая улика.
4. Сверь ссылки: `kubectl get deploy payments-api -n vektor-config -o yaml | grep -A3 -i secretRef`. Видно, что деплой ждёт `payments-db`.
5. Проверь, что секрета правда нет: `kubectl get secret -n vektor-config`. Есть `payments-report` и `payments-vault`, а `payments-db` отсутствует.
6. Не спеши создавать руками — сначала посмотри, что говорит Git: в репозитории `vektor/platform` лежит `secrets/payments-db.enc.yaml`. Это источник истины.

**Решение:**

```bash
cd ~/platform
sops --decrypt secrets/payments-db.enc.yaml | kubectl apply -f -
kubectl get secret payments-db -n vektor-config
kubectl rollout status deploy/payments-api -n vektor-config
```

**Ответ пользователю:** «Мария, добрый день. Портал недоступен из-за отсутствующего секрета конфигурации в кластере. Секрет восстановлен из защищённого хранилища, приложение перезапущено. Проверьте, пожалуйста, открывается ли портал, и напишите мне — я закрою заявку.»

**Закрытие заявки:** категория «конфигурация кластера», root cause «секрет удалён вручную, манифест не был под управлением GitOps», профилактика — включить автосинхронизацию (ArgoCD/Flux) и запретить ручные `kubectl delete` в прод-namespace через RBAC.

### Что изменилось на стенде

- Кластер: namespace `vektor-config` (ConfigMap `payments-config`, Secrets `payments-db`, `payments-report`, `payments-vault`), Deployment `payments-api`; namespace `eso-source` (Secret `payments-vault-source`, ServiceAccount `eso-store`).
- Установлены: контроллер **Sealed Secrets** в `kube-system`, **External Secrets Operator** в `external-secrets`; CRD `SealedSecret`, `SecretStore`, `ClusterSecretStore`, `ExternalSecret`.
- `k8s-cp01`: в `kube-system` появился Secret с приватным ключом Sealed Secrets — **его нужно забэкапить**, без него все SealedSecret бесполезны.
- `UBU01`: ключ age `~/.config/sops/age/keys.txt`, файл `pub-cert.pem`, установлены `sops`, `age`, `kubeseal`.
- `GIT01`, репозиторий `vektor/platform`: `.sops.yaml`, `payments-config.yaml`, `secrets/payments-db.enc.yaml`, `payments-report.sealed.yaml`, `externalsecret.yaml`.
- Реестр `HARBOR01`: при необходимости создан robot-аккаунт `robot_vektor` и Secret `harbor-pull` для pull из приватного проекта.

### Откат

Если что-то пошло не так — в VMware Workstation на каждой ВМ: **VM → Snapshot → Snapshot Manager → `before-devops-036` → Restore**. Поминай: откат `k8s-cp01` вернёт и etcd, то есть состояние кластера на момент снапшота. Не откатывай только одну ноду из трёх — рассинхронизируешь etcd; откатывай весь набор. Если откат ВМ нежелателен, минимальный «мягкий» откат — `kubectl delete namespace vektor-config eso-source`, `helm uninstall sealed-secrets -n kube-system`, `helm uninstall external-secrets -n external-secrets`; CRD Sealed Secrets удаляются вместе с чартом, CRD ESO после `helm uninstall` иногда остаются — проверь `kubectl get crd | grep external-secrets`. Ключи на `UBU01` и файлы в Git оставь: они пригодятся в следующих главах про Helm и GitOps.

## 6. Ошибки новичков и безопасность

- **«Secret зашифрован, там base64»** → любой, у кого есть `get secrets`, читает пароли за секунду → включай шифрование etcd, урезай RBAC, для прода — внешнее хранилище.
- **Коммит `secret.yaml` открытым текстом** → утечка при первом же клонировании репозитория → в Git только `*.enc.yaml` или `SealedSecret`; добавь pre-commit hook с grep по `password|secret|token`.
- **Правка ConfigMap «на живую» через `kubectl edit` и ожидание, что всё перезапустится** → env не обновляется, поды крутят старый конфиг → `kubectl rollout restart` или, лучше, GitOps-синхронизация.
- **`envFrom` всего Secret целиком** → в переменные попадают лишние ключи, они светятся в `kubectl exec -- env` и в логах → бери конкретные ключи через `secretKeyRef`.
- **Секреты в аргументах командной строки пода** (`command: ["app","--password=..."]`) → видны в `ps` и в `kubectl describe pod` → только env или файл.
- **`optional: true` без понимания** → под стартует с пустым паролем и падает позже, в непонятном месте → для критичных ключей `optional` не ставить.
- **Потеря приватного ключа Sealed Secrets** → все `SealedSecret` в Git превращаются в мусор → бэкап Secret с ключом вне кластера, ротация ключей контроллера с `kubeseal --re-encrypt` `[ПРОВЕРИТЬ флаг в вашей версии]`.
- **Класть в ConfigMap конфиг на 3 МБ** → объект не сохранится или раздуется etcd → большие файлы в PVC или объектном хранилище.

### Безопасность

В боевой среде **нельзя**: выдавать `cluster-admin` «на посмотреть», давать всем разработчикам `get secrets` на весь кластер, оставлять `--authorization-mode=AlwaysAllow`, хранить секреты в аннотациях (аннотации видны в `kubectl describe`), коммитить `.sops.yaml` с приватным ключом (там только публичный), оставлять age-ключ в CI-переменной без маскировки. В норме: доступ к секретам через RBAC по namespace, `kubectl auth can-i get secrets --as=system:serviceaccount:vektor-config:default` как регулярная проверка, аудит-лог на `get secrets`, ротация паролей по графику. На стенде пароль `Lab#2026-vektor` учебный — в реальной работе такой пароль на всех системах недопустим.

## 7. Вопросы на собеседовании

**1. Чем Secret отличается от ConfigMap и почему Secret нельзя считать безопасным по умолчанию?**

Ответ: технически это похожие объекты API, оба хранятся в etcd и монтируются в под. Secret отличается типом, ограничением размера (1 МиБ) и поведением клиентов: `kubectl describe` и логи не печатают содержимое. По умолчанию в etcd Secret лежит **в base64**, то есть любой с доступом к etcd-бэкапу или с RBAC-правом `get secrets` его прочитает. Безопасным он становится только когда включено шифрование etcd at rest (`EncryptionConfiguration`, провайдер `aescbc`/`kms`), настроен минимальный RBAC, ведутся аудит-логи и есть практика ротации.

*Что хочет услышать интервьюер:* что кандидат понимает разницу между «скрыт» и «зашифрован», и знает про encryption at rest.
*Красный флаг:* «Secret зашифрован, там используются ключи Kubernetes» — путает кодирование с шифрованием.

**2. Как секрет из Git попадает в кластер так, чтобы не утечь? Назовите подходы и trade-off.**

Ответ: три основных. **Sealed Secrets** — асимметричное шифрование публичным ключом кластера, в Git `SealedSecret`, расшифровка контроллером; плюс — просто, без внешних сервисов; минус — привязка к кластеру и критичный приватный ключ. **SOPS** — шифрование age/GPG/KMS, в Git зашифрованы только значения; плюс — гибко, работает с Flux/ArgoCD и обычным CI; минус — ключ расшифровки должен быть в CI. **External Secrets** — в Git только `ExternalSecret`, значения живут в Vault/Lockbox; плюс — авто-ротация и единый источник истины; минус — нужен внешний сервис, аутентификация и резервный доступ на случай, если хранилище недоступно.

*Что хочет услышать интервьюер:* что кандидат выбирает под контекст, а не «мы всегда делаем X», и знает про судьбу ключей.
*Красный флаг:* «можем просто залить Secret в Git, репозиторий приватный».

**3. Что произойдёт с конфигом в поде, если изменить ConfigMap?**

Ответ: зависит от способа доставки. Если ConfigMap смонтирован томом — kubelet обновит файлы в течение периода синхронизации (обычно до ~2 минут), симлинки в томе переключаются атомарно. Приложение должно само перечитывать файл — иначе оно будет работать по старому содержимому. Если поведение реализовано через `env` или `envFrom` — переменные окружения **не обновляются никогда**, нужен перезапуск пода (`kubectl rollout restart`). Монтирование через `subPath` тоже не обновляется. Отдельно: `immutable: true` запрещает изменения объекта — только удалить и создать.

*Что хочет услышать интервьюер:* конкретику про kubelet sync и про то, что env не обновляется.
*Красный флаг:* «Kubernetes сам перезапускает поды при изменении ConfigMap».

**4. Мы поставили External Secrets. Что будет, если Vault недоступен?**

Ответ: ESO работает по последнему успешному состоянию — существующий Secret остаётся как есть, `ExternalSecret` переходит в `Ready=False` с ошибкой синхронизации, и в `status.conditions` видно причину. Приложение продолжает работать, пока под не перезапустится и не попытается прочитать отсутствующий ключ. Поэтому: алерты на `ExternalSecret` в состоянии не `Ready`, `refreshInterval` не меньше разумного минимума, а для critical-path секретов — план Б (например, Sealed Secrets как резерв) и понимание, что при удалении Secret положить в кластер будет нечего.

*Что хочет услышать интервьюер:* что кандидат думает про failure mode, а не только про happy path.
*Красный флаг:* «Ничего не случится, ESO всё закэширует навсегда».

**5. Почему не давать разработчикам `get secrets` в namespace, где они деплоят?**

Ответ: у права `get secrets` фактически тот же эффект, что у чтения паролей от БД и токенов API. ServiceAccount пода с таким правом можно использовать как точку входа: получил RCE в поде — вычитал все секреты namespace. Практика: разработчики деплоят через CI/ArgoCD с отдельным ServiceAccount, а `get secrets` остаётся у узкого круга; токены ServiceAccount монтируются с `automountServiceAccountToken: false`, где не нужны; доступ проверяется через `kubectl auth can-i`.

*Что хочет услышать интервьюер:* понимание, что Secret — это привилегия, и что RCE в поде превращается в утечку секретов.
*Красный флаг:* «Нам удобно, у нас все свои».

## 8. Источники (официальная документация)

- **kubernetes.io** — раздел «Configure a Pod to Use a ConfigMap», «Distribute Credentials Securely Using Secrets», «Good practices for Kubernetes Secrets», «Encrypting Confidential Data at Rest». Поиск: `kubernetes configmap volume update`, `kubernetes secrets good practices`, `kubernetes encryption at rest`.
- **kubernetes.io** — «Managing Secrets using kubectl», «Managing ConfigMaps», `kubectl create configmap --help`, `kubectl create secret --help`. Поиск: `kubectl create secret generic dry-run yaml`.
- **github.com/bitnami-labs/sealed-secrets** — README, разделы «Installation», «Usage», «Scopes", «Key Renewal». Поиск: `sealed secrets kubeseal scope namespace-wide`, `sealed secrets backup private key`.
- **external-secrets.io** — «Introduction», «Getting started», «API: ExternalSecret», «Stable providers». Поиск: `external secrets operator kubernetes provider ClusterSecretStore`.
- **getsops.io** (Mozilla SOPS) — «SOPS», «Usage», «Encrypting only values», «.sops.yaml creation rules». Поиск: `sops encrypted_regex data stringData`, `sops age key`. Домен проекта менялся, актуальный адрес `[ПРОВЕРИТЬ]`.
- **helm.sh/docs** — установка чартов `sealed-secrets`, `external-secrets`. Поиск: `helm install sealed-secrets chart kube-system`.
- **docs.gitlab.com** — «CI/CD variables», «Masked variables» — как хранить ключ расшифровки SOPS в пайплайне. Поиск: `gitlab ci masked variable`.
