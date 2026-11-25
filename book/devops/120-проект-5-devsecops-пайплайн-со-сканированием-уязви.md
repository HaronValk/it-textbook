# Проект №5: DevSecOps — пайплайн со сканированием уязвимостей, Vault, подписью образов, политиками безопасности

## 1. Введение (зачем это нужно в реальной работе)

**Заявка SD-6120.** Пользователь: `sidorov` (склад). ВМ: рабочая почта. Текст: «Добрый день. Партнёр прислал письмо: у нас на сайте оплаты браузер ругается, а в приложенном отчёте их сканера наш сервис `payments` помечен как critical. Они просят до конца недели подтвердить, что мы устранили. Кто этим занимается?» Срочность: средняя.

Вот так DevSecOps и приходит в жизнь: не «мы решили внедрить безопасность», а «партнёр/аудитор/клиент прислал отчёт, и надо ответить фактами». В маленькой компании «Вектор» нет отдельной команды AppSec — значит, безопасность встраивает в пайплайн тот самый человек, который этот пайплайн и собрал. Вы уже сделали CI/CD (урок 116), production-grade кластер (118) и наблюдаемость (119). Теперь вы должны уметь: находить секреты и уязвимости до сборки, сканировать образы, подписывать их так, чтобы подпись нельзя было подделать, хранить ключи и пароли в Vault, и запрещать кластеру запускать что попало через admission-политики. После главы вы сможете собрать пайплайн, который **сам** не пускает небезопасный артефакт в прод, и объяснить на собеседовании, почему подписывают digest, а не тег.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**DevSecOps** — это когда проверки безопасности автоматизированы и стоят в том же пайплайне, что и сборка (принцип *shift left*: чем раньше найдена проблема, тем дешевле). Есть три уровня контроля:

1. **Код и зависимости (до сборки).** SAST (*Static Application Security Testing*) — анализ исходников без запуска: Semgrep, Bandit. SCA (*Software Composition Analysis*) — проверка сторонних библиотек по базам CVE: Trivy, Grype. Secrets scanning — поиск паролей/токенов в git: Gitleaks. SBOM (*Software Bill of Materials*) — машиночитаемый список компонентов (формат CycloneDX или SPDX): Syft.
2. **Артефакт (образ).** Сканирование слоёв образа тем же Trivy; в Harbor есть встроенный сканер и политика «не давать запускать образ с уязвимостями выше порога». Плюс **подпись** — cosign (проект Sigstore) кладёт подпись рядом с образом в реестре как отдельный OCI-артефакт.
3. **Runtime (кластер).** Admission-контроль: API-сервер Kubernetes перед созданием объекта вызывает validating webhook. Kyverno как раз такой webhook: он умеет проверять подпись образа (`verifyImages`), запрещать тег `latest`, требовать `resources.limits`, запрещать `privileged`. Альтернатива — OPA Gatekeeper с политиками на языке Rego.

**Ключевая идея подписи.** Тег — это ярлык, его можно перевесить: `payments:1.4.2` сегодня указывает на один образ, завтра на другой. **Digest** (`sha256:...`) — хеш содержимого, он уникален. Подпись всегда считается по digest. Если вы подписали тег, а потом перезалили тот же тег — подпись осталась на старом digest, и проверка честно провалится.

**Два режима cosign:** keyless (сертификат от OIDC-провайдера + прозрачный лог Rekor, нужен интернет) и ключевой (пара ключей). На стенде без выхода в интернет берём ключевой режим, но приватный ключ не кладём в CI-переменную, а держим в **HashiCorp Vault**, в secrets engine **Transit** — «криптография как сервис»: Vault сам считает подпись, ключ из Vault не выходит. Второй engine, **KV v2**, хранит обычные секреты (пароли БД, токены) с версионированием.

```text
git push
   │
   ├─ 1. gitleaks        (секреты)          ─┐
   ├─ 2. semgrep         (SAST)              │ security gates:
   ├─ 3. trivy fs + syft (SCA + SBOM)        │ пайплайн красный
   └─ 4. build → push в Harbor              ─┘  = дальше не идём
              │
              ├─ 5. trivy image (HIGH/CRITICAL)
              ├─ 6. cosign sign  ← Vault Transit (:8200 внутри кластера)
              └─ 7. cosign attest (SBOM)
                        │
                        ▼
             Harbor 10.10.10.55 (443) — immutable tags, scan on push
                        │
                  ArgoCD (деплой)
                        │
                        ▼
             k8s API 10.10.10.61:6443
                        │
        ┌───────────────┴────────────────┐
        │ admission chain                │
        │  Kyverno: подписано? latest?   │
        │  limits? privileged?           │
        └───────────────┬────────────────┘
                        ▼
                Pod в namespace payments
```

Порты, которые встретятся: Harbor 443, Vault 8200 внутри пода (на стенде NodePort 30820), Kubernetes API 6443, GitLab 443.

## 3. Аналогия из жизни

Аэропорт. На входе в терминал у вас проверяют паспорт и билет — это **secrets scanning и SAST**: ещё ничего не произошло, а нарушителя уже отсекли. Дальше багаж просвечивают — **сканирование зависимостей и образа**. Затем на сумку вешают пломбу с уникальным номером — **подпись по digest**: пломба привязана к конкретной сумке, переклеить её на другую нельзя. На гейте стюардесса смотрит на пломбу и сверяет с номером рейса — это **Kyverno verifyImages**: нет пломбы или номер не тот — на борт не пустят, даже если сумка красивая. Ключ от пломбиратора лежит в сейфе у начальника смены, а не в кармане грузчика — это **Vault Transit**.

Аналогия перестаёт работать в одном месте: сумку можно вскрыть и закрыть так, что пломба внешне цела, а у образа любое изменение содержимого меняет digest — подделать незаметно нельзя, можно только подписать заново, а для этого нужен доступ к ключу.

## 4. Практика и команды

**1. Поиск секретов в репозитории (Gitleaks).**
```bash
gitleaks detect --source . --no-git --redact -v
```
`--no-git` — сканировать файлы, а не историю коммитов (быстрее, для CI); `--redact` — не печатать найденный секрет в лог (иначе сами утечёте его в лог GitLab); `-v` — подробно. Вывод: `Finding: AWS Access Key ... File: config/db.env`, `leaks found: 1`. Смотрим путь файла и правило. В новых версиях команда называется `gitleaks dir` `[ПРОВЕРИТЬ]`.

**2. SAST (Semgrep).**
```bash
semgrep scan --config=auto --error --metrics=off .
```
`--config=auto` — подтянуть правила под язык проекта; `--error` — вернуть код 1 при находках (для CI); `--metrics=off` — не отправлять телеметрию. Вывод: `python.lang.security.audit.sqli` с номером строки. Смотрим severity и `confidence`.

**3. SCA по зависимостям (Trivy).**
```bash
trivy fs --scanners vuln,secret --ignore-unfixed \
  --severity HIGH,CRITICAL --exit-code 1 .
```
`--ignore-unfixed` — не падать на CVE, для которых ещё нет патча (иначе пайплайн вечно красный); `--exit-code 1` — упасть при находках. Вывод: таблица `LIBRARY / VULNERABILITY / SEVERITY / FIXED VERSION`. Смотрим колонку `FIXED VERSION` — есть ли куда обновиться.

**4. SBOM (Syft).**
```bash
syft dir:. -o cyclonedx-json=sbom.cdx.json
```
Формат `cyclonedx-json` понимают Harbor, Grype, Dependency-Track. Дальше SBOM можно сканировать отдельно: `trivy sbom sbom.cdx.json` — так проверяют образы, которые нельзя запустить локально.

**5. Сканирование образа.**
```bash
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
  harbor.vektor.lab/vektor/payments:1.4.2
```
Первый запуск качает базу CVE (нужен интернет или локенное зеркало). Смотрим `Total: 3 (HIGH: 2, CRITICAL: 1)` и есть ли `fixed`.

**6. Harbor: политики проекта.** GUI: *Projects → vektor → Configuration → Deployment security*: включить «Prevent vulnerable images from running», порог `High`; там же *Tag immutability* — правило для `1.*` (запрет перезаписи релизных тегов). Через API:
```bash
curl -sk -u admin:'Lab#2026-vektor' \
 "https://harbor.vektor.lab/api/v2.0/projects/vektor/repositories/payments/artifacts?with_scan_overview=true&with_tag=true"
```
`with_scan_overview=true` — вернуть сводку сканирования по каждому артефакту. Точный набор query-параметров `[ПРОВЕРИТЬ]` по версии Harbor (у нас 2.x).

**7. Vault: включаем Transit и создаём ключ.**
```bash
export VAULT_ADDR=http://10.10.10.62:30820
vault secrets enable transit
vault write -f transit/keys/cosign-sign type=ecdsa-p256
```
`-f` — без тела запроса; `type=ecdsa-p256` — алгоритм, который понимает cosign. Ключ создан внутри Vault, приватной части на диске нет.

**8. Vault: KV v2 для секретов CI.**
```bash
vault secrets enable -path=kv kv-v2
vault kv put kv/ci/payments db_password='Lab#2026-vektor' registry_token='...'
vault kv get -field=db_password kv/ci/payments
```
`-field` — достать одно поле (удобно в скриптах). `vault kv metadata get kv/ci/payments` покажет версии — можно откатиться.

**9. Публичный ключ cosign из Vault.**
```bash
cosign public-key --key hashivault://cosign-sign > cosign.pub
```
Нужны переменные окружения `VAULT_ADDR` и `VAULT_TOKEN` `[ПРОВЕРИТЬ точный набор env для Vault KMS в вашей версии cosign]`. Запасной вариант без Vault: `cosign generate-key-pair` (создаст `cosign.key`/`cosign.pub`, пароль берётся из `COSIGN_PASSWORD`).

**10. Подпись и проверка по digest.**
```bash
TAG=1.4.2; REPO=harbor.vektor.lab/vektor/payments
DIGEST=$(crane digest "$REPO:$TAG")          # или: skopeo inspect --format '{{.Digest}}' docker://$REPO:$TAG
cosign sign --key hashivault://cosign-sign --tlog-upload=false "$REPO@$DIGEST"
cosign verify --key cosign.pub --insecure-ignore-tlog "$REPO@$DIGEST"
```
`--tlog-upload=false` и `--insecure-ignore-tlog` нужны потому, что на стенде нет доступа к Rekor `[ПРОВЕРИТЬ имя флага в вашей версии cosign]`. В `verify` смотрим `"critical": {"image": {"docker-manifest-digest": "sha256:..."}}` — digest должен совпасть.

**11. Аттестация SBOM.**
```bash
cosign attest --key hashivault://cosign-sign --tlog-upload=false \
  --predicate sbom.cdx.json --type cyclonedx "$REPO@$DIGEST"
cosign verify-attestation --key cosign.pub --type cyclonedx "$REPO@$DIGEST"
```
Аттестация — «подписанное утверждение об образе». Отвечает на вопрос аудитора «покажите состав и подтвердите, что он не менялся».

**12. Kyverno: ставим и проверяем политику.**
```bash
helm repo add kyverno https://kyverno.github.io/kyverno/ && helm repo update
helm install kyverno kyverno/kyverno -n kyverno --create-namespace
kubectl get clusterpolicy
kubectl run probe --image=harbor.vektor.lab/vektor/payments:latest -n payments
```
Ожидаемо: `Error from server: admission webhook "validate.kyverno.svc-fail" denied the request: ...:latest is not allowed`. Отчёты по политикам — `kubectl get policyreport -A`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия
Стенд DevOps: `GIT01` (10.10.10.50, GitLab CE + runner, проект `vektor/payments` из урока 116), `HARBOR01` (10.10.10.55, проект `vektor`), кластер `k8s-cp01`/`k8s-w01`/`k8s-w02` (10.10.10.61–63) с namespace `payments` и ArgoCD из уроков 116/118. Runner на GIT01 должен уметь собирать образы (`docker` доступен). Vault отдельной ВМ не имеет — ставим его подом в кластер на `k8s-w01`, это явное расширение стенда, фиксируем в `stand-log.md`. Все действия — только с учебным паролем `Lab#2026-vektor`.

### Снапшот
В VMware Workstation: `VM → Snapshot → Take Snapshot`, имя `before-devops-120` на **GIT01**, **HARBOR01**, **k8s-cp01**, **k8s-w01**. Для консистентности — при остановленном runner и без активных деплоев.

### Шаги

1. **k8s-w01.** Ставим Vault из Helm (standalone, не dev — dev-режим стирает данные при рестарте):
```yaml
# vault-values.yaml
server:
  standalone:
    enabled: true
    config: |
      ui = true
      listener "tcp" {
        address = "0.0.0.0:8200"
        tls_disable = 1        # ТОЛЬКО стенд, в проде TLS обязателен
      }
      storage "file" { path = "/vault/data" }
  service:
    type: NodePort
    nodePort: 30820
ui:
  enabled: true
```
`helm install vault hashicorp/vault -n vault --create-namespace -f vault-values.yaml`. Ожидаемо: под `vault-0` в статусе `Running`, но `0/1` — он «запечатан».

2. **k8s-w01.** Инициализация и распечатывание:
```bash
kubectl exec -n vault vault-0 -- vault operator init -key-shares=1 -key-threshold=1
kubectl exec -n vault vault-0 -- vault operator unseal <UNSEAL_KEY>
```
Ключ и root-токен сохраняем **вне git** (файл на хосте, в KeePass). Ожидаемо: `Sealed false`.

3. **k8s-w01.** Включаем Transit, создаём ключ, включаем KV и кладём секрет (команды 7–8 из раздела 4). Ожидаемо: `Success! Enabled the transit secrets engine`, `vault kv get` возвращает значение.

4. **k8s-w01.** Ставим Kyverno (команда 12). Ожидаемо: 4–5 подов в `kyverno` в статусе `Running`.

5. **GIT01.** Добавляем security-стадии в `.gitlab-ci.yml`:
```yaml
stages: [secrets, sast, sca, build, scan, sign]
variables:
  REPO: harbor.vektor.lab/vektor/payments
  TAG: $CI_COMMIT_SHORT_SHA

gitleaks:
  stage: secrets
  script: [gitleaks detect --source . --no-git --redact -v]

semgrep:
  stage: sast
  script: [semgrep scan --config=auto --error --metrics=off .]

trivy-fs:
  stage: sca
  script:
    - syft dir:. -o cyclonedx-json=sbom.cdx.json
    - trivy fs --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 .
  artifacts: {paths: [sbom.cdx.json]}

build:
  stage: build
  script:
    - docker build -t $REPO:$TAG .
    - docker push $REPO:$TAG

scan-image:
  stage: scan
  needs: [build]
  script:
    - trivy image --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 $REPO:$TAG

sign-image:
  stage: sign
  needs: [build, scan-image, trivy-fs]
  script:
    - export VAULT_ADDR=http://10.10.10.62:30820
    - DIGEST=$(crane digest $REPO:$TAG)
    - cosign sign --key hashivault://cosign-sign --tlog-upload=false $REPO@$DIGEST
    - cosign attest --key hashivault://cosign-sign --tlog-upload=false --predicate sbom.cdx.json --type cyclonedx $REPO@$DIGEST
```
`VAULT_TOKEN` кладём в *Settings → CI/CD → Variables* как masked/protected, не в файл. Ожидаемо: коммит запускает 6 job'ов, все зелёные.

6. **GIT01.** Проверяем, что гейты реально работают: в тестовой ветке добавляем в `app/config.py` строку `AWS_SECRET = "AKIAIOSFODNN7EXAMPLE"` и пуш. Ожидаемо: job `gitleaks` **падает**, дальше стадии не идут. Убираем строку, пушим снова.

7. **GIT01.** Добавляем уязвимую зависимость (`requests==2.19.0` в `requirements.txt`), пушим. Ожидаемо: `trivy-fs` или `scan-image` падает с HIGH/CRITICAL, `sign-image` не запускается.

8. **k8s-cp01.** Пишем политики Kyverno:
```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata: {name: payments-guardrails}
spec:
  validationFailureAction: Audit   # сначала Audit! Enforce — после проверки
  background: false
  rules:
    - name: disallow-latest
      match: {any: [{resources: {kinds: [Pod], namespaces: [payments]}}]}
      validate:
        message: "Тег latest запрещён, укажите версию или digest"
        pattern: {spec: {containers: [{image: "!*:latest"}]}}
    - name: require-limits
      match: {any: [{resources: {kinds: [Pod], namespaces: [payments]}}]}
      validate:
        message: "Нужны resources.requests/limits"
        pattern: {spec: {containers: [{resources: {limits: {memory: "?*", cpu: "?*"}}}]}}
    - name: verify-signature
      match: {any: [{resources: {kinds: [Pod], namespaces: [payments]}}]}
      verifyImages:
        - imageReferences: ["harbor.vektor.lab/vektor/*"]
          attestors:
            - entries:
                - keys:
                    publicKeys: |-
                      -----BEGIN PUBLIC KEY-----
                      <содержимое cosign.pub>
                      -----END PUBLIC KEY-----
```
`kubectl apply -f payments-guardrails.yaml`. Имя поля для action зависит от версии Kyverno (`spec.validationFailureAction` vs `rules[].validate.failureAction`) `[ПРОВЕРИТЬ]`. Ожидаемо: `kubectl get clusterpolicy` → `payments-guardrails`, `READY=True`.

9. **k8s-cp01.** Проверка в Audit: `kubectl run probe --image=harbor.vektor.lab/vektor/payments:latest -n payments` — под создастся, но в `kubectl get policyreport -n payments` появится `FAIL`. Меняем на `Enforce`, повторяем — теперь `denied the request`. Удаляем `probe`.

10. **k8s-cp01.** Деплой через ArgoCD: в манифесте deployment указываем `image: harbor.vektor.lab/vektor/payments@sha256:<digest подписанного образа>`, синкаем. Ожидаемо: под `Running`, сервис отвечает.

11. **GIT01/`stand-log.md`.** Записываем изменения (см. блок ниже) и коммитим политики в отдельный репозиторий `vektor/policies`.

### Проверка
- [ ] `kubectl get pods -n vault` — `vault-0` в `Running`, `vault status` → `Sealed false`.
- [ ] `vault list transit/keys` показывает `cosign-sign`.
- [ ] `cosign verify --key cosign.pub --insecure-ignore-tlog harbor.vektor.lab/vektor/payments@sha256:<digest>` возвращает `Verified OK`.
- [ ] `cosign verify-attestation --key cosign.pub --type cyclonedx <образ>` показывает SBOM.
- [ ] Пайплайн с секретом в коде падает на `gitleaks`, с уязвимой библиотекой — на `trivy`.
- [ ] `kubectl run` с тегом `latest` в namespace `payments` отклоняется webhook'ом.
- [ ] `kubectl run` с неподписанным образом (например, `nginx:1.27` из другого реестра) отклоняется.
- [ ] В Harbor у образа `payments` видна подпись и результат сканирования, тег `1.*` неизменяем.
- [ ] В `stand-log.md` появилась запись про Vault, Kyverno, cosign и политики.

### Сломай и почини
**Пользователь:** `ivanov`. **ВМ:** WIN11-CL01. **Что написал:** «Открываю тестовый магазин на 10.10.10.62 — при оплате ошибка 502. Вчера работало. Разработчик говорит "деплой не прошёл". Почините до вечера». **Срочность:** средняя.

**Как воспроизвести поломку (намеренно):** в проекте `payments` перезапустить пайплайн для уже существующего тега `1.4.2` после правки кода — образ перезалился тем же тегом, digest стал новым, а подпись осталась на старом. ArgoCD синкает манифест, где deployment переименован, старый ReplicaSet удаляется по prune, новый pod отклоняется Kyverno `verifyImages` → endpoints пустой → 502 на ingress.

**Диагностика по шагам:**
1. `kubectl get pods -n payments` — подов нет.
2. `kubectl get events -n payments --sort-by=.lastTimestamp | tail` — событие `admission webhook "mutate.kyverno.svc-fail" denied the request: image ... is not signed`.
3. `kubectl get policyreport -n payments -o yaml` — какое правило сработало (`verify-signature`).
4. `crane digest harbor.vektor.lab/vektor/payments:1.4.2` — сравнить с digest, который лежит в манифесте/подписан.
5. `cosign verify --key cosign.pub --insecure-ignore-tlog harbor.vektor.lab/vektor/payments@sha256:<фактический digest>` — `no signatures found` → подтверждаем диагноз.
6. `kubectl logs -n gitlab job-...` (job `sign-image`) — видно, что подпись была на другом digest.

**Решение:** подписать актуальный digest из пайплайна (или пересобрать и прогнать пайплайн целиком), в манифесте зафиксировать digest, включить в Harbor tag immutability для `1.*`, добавить в пайплайн шаг «sign by digest после push» (как в разделе 4, команда 10). Политику **не отключать** — это не она сломалась, а процесс.

**Ответ пользователю:** «Магазин восстановлен, причина — обновление сервиса оплаты не прошло проверку подписи образа на кластере, поэтому новые поды не создались. Пересобрали и подписали корректно, добавили защиту от повторения. Если увидите 502 снова — сразу пишите, посмотрим за минуту».

**Закрытие заявки:** категория «DevOps/Deployment», резолюция с причиной и preventative action, ссылка на пайплайн и коммит с фиксом.

### Что изменилось на стенде
- Новый сервис в кластере: Vault (namespace `vault`, под `vault-0` на `k8s-w01`, NodePort `30820`, file storage, 1 ключ распечатки).
- Vault engines: `transit` (ключ `cosign-sign`, ecdsa-p256), `kv` (v2, секреты `kv/ci/payments`).
- Установлен Kyverno (namespace `kyverno`), политика `payments-guardrails` в режиме `Enforce`.
- В `GIT01`: проект `vektor/payments` получил 6 стадий, CI-переменная `VAULT_TOKEN` (masked); новый проект `vektor/policies` с YAML политик.
- В `HARBOR01`: проект `vektor` — включён «Prevent vulnerable images from running» (порог High), immutable tags для `1.*`; образы несут подпись cosign и аттестацию CycloneDX.
- На рабочей машине читателя: `cosign.pub` и `vault-values.yaml` в репозитории; unseal-ключ и root-токен — вне git.
- Namespace `payments`: deployment использует digest вместо тега.

### Откат
VMware Workstation → *Snapshot Manager* → `before-devops-120` → *Revert* на GIT01, HARBOR01, k8s-cp01, k8s-w01. Если откатывать кластер не хочется: `helm uninstall kyverno -n kyverno`, `helm uninstall vault -n vault` (данные Vault на PVC пропадут — это ожидаемо), `kubectl delete clusterpolicy payments-guardrails`, из `.gitlab-ci.yml` убрать стадии `scan`/`sign`. После любого отката — запись в `stand-log.md`.

## 6. Ошибки новичков и безопасность

1. **Подписывать тег, а не digest.** → После любой перезаливки подпись не совпадает, прод блокируется. → Всегда `crane digest` + подпись по `@sha256:...`.
2. **Сразу включать `Enforce`.** → Кластер перестаёт принимать рабочие деплои, вы «сломали прод политикой». → Сначала `Audit` и policyreport, через день-два — `Enforce`.
3. **`--exit-code 1` без `--ignore-unfixed` и без `--severity`.** → Пайплайн красный на сотнях низких CVE, команда привыкает игнорировать красное. → Порог HIGH/CRITICAL, `--ignore-unfixed`, отдельный job-отчёт для остального.
4. **Приватный ключ cosign в CI-переменной.** → Любой, кто имеет доступ к настройкам проекта, подпишет что угодно от вашего имени. → Vault Transit; ключ не покидает Vault.
5. **Vault в dev-режиме «пока так».** → In-memory storage: рестарт пода — все секреты и ключ подписи потеряны, подписи не проверить. → Standalone/Raft + бэкап unseal-ключей.
6. **`verifyImages` без фиксации digest.** → Между проверкой тега и загрузкой образа тег можно перевесить (TOCTOU). → Kyverno подменяет тег на digest (`mutateDigests`, по умолчанию включено в новых версиях `[ПРОВЕРИТЬ]`), плюс immutable tags в Harbor.
7. **Docker-сокет в runner.** → Job получает root на GIT01 и может влиять на соседние сборки. → Отдельный runner в Kubernetes с Kaniko/Buildah rootless.
8. **Отчёт сканера «зелёный» навсегда.** → База CVE не обновлялась (нет интернета), сканер врёт. → Проверять дату базы, поднять локенное зеркало `trivy` `[ПРОВЕРИТЬ актуальный способ]`.

**Безопасность.** На стенде мы слушаем Vault по HTTP (`tls_disable = 1`) и кладём root-токен в файл — в боевой среде так нельзя: только TLS, авторизация приложений через Kubernetes auth или AppRole с короткоживущими токенами, root-токен «запечатан» в сейф, все обращения аудируются (`vault audit enable file`). Пароль `Lab#2026-vektor` один на весь стенд — в реальности у каждого сервиса свой секрет и ротация. Отключать проверки «на минуточку, чтобы додеплоить» — прямой путь к инциденту; легальный обход — задокументированный break-glass: метка-namespace, временное исключение в политике, задача на устранение и запись в журнал.

## 7. Вопросы на собеседовании

**1. Чем SAST отличается от SCA и DAST, и что из этого куда ставить в пайплайне?**
SAST анализирует ваш собственный код без запуска (Semgrep, Bandit) — быстро, но много ложных срабатываний, ставится на этапе до сборки. SCA смотрит сторонние зависимости и образы по базам CVE (Trivy, Grype, Dependency-Track) — на этапе сборки и после push в реестр. DAST тестирует уже запущенное приложение снаружи (OWASP ZAP) — на стейдже после деплоя, потому что ему нужен живой эндпоинт. Все три дают разный класс находок, поэтому нужны вместе, а не «одно вместо другого».
*Что хочет услышать интервьюер:* понимание, что инструменты не взаимозаменяемы, и привязку к стадиям пайплайна.
*Красный флаг:* «Trivy заменяет SAST» или путаница DAST/SAST.

**2. Как работает подпись образа cosign и почему подписывают digest, а не тег?**
cosign генерирует пару ключей (или берёт ключ из KMS/Vault), считает подпись над digest манифеста и кладёт её в тот же реестр отдельным OCI-артефактом, привязанным к этому digest. Проверка — `cosign verify --key cosign.pub repo@sha256:...`: скачивается подпись, проверяется публичным ключом. Тег — изменяемый указатель: `1.4.2` можно перевесить на другой образ, и подпись формально останется «рядом». Digest — хеш содержимого, поэтому подпись по digest гарантирует именно те байты, которые вы собирали. В keyless-режиме дополнительно используется сертификат от OIDC и прозрачный лог Rekor, чтобы подпись нельзя было скрытно отозвать/подменить.
*Что хочет услышать интервьюер:* digest vs tag, OCI-артефакт подписи, роль Rekor, разница keyless/ключевой режим.
*Красный флаг:* «подпись хранится внутри образа» или «cosign шифрует образ».

**3. Что делает Kyverno и почему это admission-контроль, а не просто «скрипт проверки»?**
Kyverno работает как dynamic admission webhook: API-сервер Kubernetes перед созданием/изменением объекта отправляет ему AdmissionReview, Kyverno применяет к нему политику (validate/mutate/generate/verifyImages) и возвращает allow или deny с сообщением. Плюс фоновый режим сканирует уже существующие объекты и пишет PolicyReport. Это значит, что обойти проверку через `kubectl apply` нельзя — она встроена в путь записи в etcd. OPA Gatekeeper решает ту же задачу, но политики пишутся на Rego и он проверяет только то, что описали (плюс ConstraintTemplates), а Kyverno ближе к «YAML без языка программирования» и умеет из коробки проверять подписи образов.
*Что хочет услышать интервьюер:* понимание admission chain, deny на уровне API, разница Kyverno/Gatekeeper, audit vs enforce.
*Красный флаг:* «Kyverno — это сканер образов» или «политики достаточно описать в README».

**4. Зачем Vault Transit, если можно положить ключ cosign в CI-переменную?**
CI-переменная живёт в БД GitLab, доступна администраторам, попадает в бэкапы, может утечь через лог джобы или компрометацию runner'а. В Transit приватный ключ генерируется и хранится внутри Vault, наружу уходит только результат подписи; доступ регулируется политиками Vault, каждый вызов пишется в audit log, ключ можно ротировать и отозвать централизованно, не меняя пайплайн. Тот же Vault через KV v2 отдаёт пароли приложениям, а через Kubernetes auth или Vault Agent Injector — подам, чтобы секреты не лежали в манифестах и не были видны в `kubectl describe`.
*Что хочет услышать интервьюер:* threat model CI, crypto-as-a-service, аудит и ротация, способы доставки секретов в поды.
*Красный флаг:* «Vault — это просто хранилище паролей в YAML» или «ключ в переменной — нормально, она же masked».

**5. Пришёл отчёт: 2 CRITICAL CVE в образе, фикса нет. Что делаете — блокируете деплой или пропускаете?**
Сначала триаж: есть ли уязвимый компонент в реальном пути исполнения (для Java — достижимость метода), есть ли компенсирующие меры (сервис не exposed, NetworkPolicy, read-only FS), какой CVSS и EPSS. Если фикса нет и риск реальный — ищем обход: обновить базовый образ/мажорную версию зависимости, убрать компонент, ограничить сетевой доступ. Деплой не «просто пропускаем»: оформляем документированное исключение (`trivy --ignorefile .trivyignore.yaml` с владельцем и датой истечения) и задачу на устранение со сроком по вашей SLA-политике. В пайплайне порог HIGH/CRITICAL остаётся, иначе исключения станут правилом.
*Что хочет услышать интервьюер:* risk-based подход вместо «бинарно запретить/разрешить», дисциплина исключений с TTL и владельцем.
*Красный флаг:* «просто уберу CRITICAL из порога» или «поставлю `--exit-code 0`, так удобнее».

## 8. Источники (официальная документация)

- **HashiCorp Developer** — `developer.hashicorp.com`: разделы *Vault → Secrets Engines → Transit* и *KV — Versioned Key/Value*; поиск: `vault transit secrets engine sign verify`.
- **Kyverno** — `kyverno.io`: раздел *Policy Types → Verify Images* и *Validate*; поиск: `kyverno verifyImages cosign public key`.
- **Sigstore / cosign** — `docs.sigstore.dev`: разделы *Signing containers → Keyless / with keys*, *Verifying*, *Attestations*; поиск: `cosign sign digest vault kms`.
- **Aqua Trivy** — `trivy.dev`: *Docs → Scanners (vuln, secret, misconfig)*, *Image scanning*, *SBOM*; поиск: `trivy image ignore-unfixed exit-code severity`.
- **Anchore Syft** — `github.com/anchore/syft`: *Supported formats → CycloneDX*; поиск: `syft cyclonedx-json output`.
- **Gitleaks** — `github.com/gitleaks/gitleaks`: *Usage → detect / dir*, *Configuration*; поиск: `gitleaks detect no-git redact`. `[ПРОВЕРИТЬ переименование команд в актуальной версии]`
- **Semgrep** — `semgrep.dev/docs`: *Running Semgrep → semgrep scan*, *Rule packs*; поиск: `semgrep scan config auto metrics off`.
- **Harbor** — `goharbor.io/docs`: *Working with projects → Configure project / Tag immutability*, *Vulnerability scanning*; поиск: `harbor prevent vulnerable images configuration`.
- **Kubernetes** — `kubernetes.io/docs`: *Reference → Access Control → Admission Control / Dynamic Admission Webhooks*; поиск: `kubernetes validating admission webhook`.
- **GitLab Docs** — `docs.gitlab.com`: *CI/CD → YAML reference*, *CI/CD variables*; поиск: `gitlab ci needs artifacts stages`.
