# YAML, JSON, TOML, HCL, Jsonnet: форматы конфигураций и работа с ними (yq, jq, kustomize)

## 1. Введение (зачем это нужно в реальной работе)

Тикет в поддержку «Вектора»:

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Открываю форму выставления счёта на внутреннем портале — кнопки “Печать” нет, интерфейс как неделю назад. У Петровой то же самое. Кэш чистил, браузер менял, на телефоне тоже старое.»
> **Срочность:** средняя (работаем, но неудобно)

Дальше выясняется: портал никто не ломал, приложение работает, браузер Иванова ни при чём. Сломался **конфиг**. В GitLab на GIT01 пайплайн собрал манифест для кластера, в котором тег образа остался старым — потому что в overlay-патче ключ был написан с опечаткой, а сборщик конфигурации такое молча проглотил. В итоге в прод уехала предыдущая версия приложения. Пользователь видит «старый интерфейс», инженер видит YAML.

Форматы конфигураций — это язык, на котором DevOps-инженер разговаривает со всей инфраструктурой: Ansible — YAML, Kubernetes — YAML/JSON, Terraform — HCL, Python-проекты — TOML, GitLab CI — YAML, Grafana-дашборды — JSON, kube-prometheus — Jsonnet. За день вы десятки раз читаете, правите, конвертируете и валидируете такие файлы, и почти каждый инцидент «ничего не задеплоилось» или «задеплоилось не то» упирается в один символ отступа. После главы вы будете: читать и править конфиги из терминала одним движением (`jq`, `yq`), понимать, где формат подставит вам подножку (YAML-типы, дубли ключей, отсутствие комментариев в JSON), собирать конфигурацию из базы и оверлеев через kustomize и прогонять её через валидаторы до того, как она уедет в прод.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Сетевых портов и уровней OSI у форматов нет — это уровень представления данных (presentation layer в терминах OSI), то есть «как записать структуру на диск и как её прочитать обратно». Общая картина одна для всех:

```text
   файл на диске (текст)
          │
          ▼
   [ парсер формата ]  ← YAML-парсер, JSON-парсер, TOML-парсер, HCL-парсер
          │
          ▼
   дерево данных (map / list / строка / число / bool / null)
          │
          ├──► инструмент-потребитель (kubectl, ansible, terraform, docker compose)
          └──► инструмент-преобразователь (yq, jq, jsonnet, kustomize) ──► новый текст
```

Ключевая идея: **формат — это только синтаксис**. Одно и то же дерево можно записать как JSON, YAML или TOML, и инструменты конвертируют их друг в друга. Именно поэтому `yq` умеет читать JSON, а `jq` — нет (jq работает только с JSON, YAML ему подсовывают через `yq -o=json`).

**JSON** (RFC 8259) — самый строгий и самый машинный: объекты `{}`, массивы `[]`, строки только в двойных кавычках, чисел два вида, `null` есть. Комментариев нет, висячих запятых нет, одинарных кавычек нет. Это транспортный формат: Kubernetes API говорит **только JSON**, а YAML в манифестах — это удобство для человека, `kubectl` конвертирует YAML→JSON на клиенте перед отправкой. Важное следствие: всё, что вы пишете в YAML для k8s, в какой-то момент станет JSON, и многодокументный файл через `---` станет списком объектов.

**YAML** (YAML 1.2, yaml.org) — надмножество JSON, оптимизированное под человека: отступы вместо скобок, комментарии `#`, несколько документов в файле через `---`, якоря `&a` и ссылки `*a` для переиспользования блоков. Плата — неоднозначность типов. Значение `yes`/`no`/`on`/`off` в YAML 1.1 читается как boolean (классическая «норвежская проблема»: код страны `NO` превращается в `false`), в YAML 1.2 core schema — только `true`/`false`. Библиотеки расходятся: PyYAML ведёт себя как 1.1, современные Go-парсеры ближе к 1.2 — проверяйте на своём инструменте. Числа с ведущим нулём (`0755`) в YAML 1.1 — восьмеричные, в 1.2 — `0o755`. `1.10` — это число `1.1`, а не строка версии. Отступы — **только пробелы**, табуляция запрещена.

**TOML** (v1.0.0, toml.io) — типизированный конфиг для приложений: секции `[project]`, вложенность через точки (`tool.mypy.overrides`), массивы таблиц `[[...]]`, настоящие даты, явные строки. Нет `null` — это надо помнить. Стандарт де-факто для `pyproject.toml` (PEP 621), `Cargo.toml`, конфигов Hugo и Traefik.

**HCL** (HashiCorp Configuration Language, HCL2) — не просто данные, а язык описания с блоками, аргументами, ссылками (`var.env`, `local.name`), выражениями `for`, интерполяцией `"${...}"` и функциями. Синтаксис задаёт HashiCorp, а смысл (`resource`, `variable`, `module`) — уже Terraform/Nomad/Packer. Есть JSON-вариант (`.tf.json`), но на практике пишут нативно.

**Jsonnet** (jsonnet.org) — язык *генерации* конфигов: JSON плюс функции, импорты, наследование объектов (`+`, `self`, `super`), стандартная библиотека `std.*`, параметры верхнего уровня (TLA) для разных окружений. Применяют там, где руками писать больно: дашборды Grafana (grafonnet), kube-prometheus, Databricks, Tanka.

**kustomize** (kustomize.io, встроен в `kubectl`) — подход «база + оверлеи» без шаблонизации: есть `kustomization.yaml`, в нём список ресурсов и модификаторы (`namePrefix`, `namespace`, `labels`, `images`, `replicas`, `configMapGenerator`, `patches`). Патчи бывают стратегические (YAML-фрагмент, накладывается по ключам) и JSON Patch (RFC 6902, операции `add`/`remove`/`replace` по путям). kustomize **не создаёт** объекты в кластере — он только рендерит итоговый YAML, поэтому его можно гонять без кластера и валидировать в CI.

Шкала «жёсткость → гибкость»: JSON → YAML → TOML → HCL → Jsonnet. Чем правее, тем больше можно выразить и тем больше шансов, что конфиг станет программой, которую никто, кроме автора, не читает.

## 3. Аналогия из жизни

Представьте, что «Вектор» издаёт один и тот же приказ по складу, но в пяти канцеляриях его оформляют по-своему.

**JSON** — телеграмма: всё максимально коротко и формально, каждая строка в кавычках, ни одного лишнего символа, ни намёка на комментарий. Прочитать можно, но глазами больно.

**YAML** — блокнот с отступами: красиво, по-человечески, можно подписать на полях «это для склада». Но если секретарь сдвинет строку на один пробел, приказ прочитается как совсем другой документ, а если напишет «NO» вместо «Норвегия» — получит «нет».

**TOML** — типовая анкета с пронумерованными графами: «Раздел 1. Отправитель», «Раздел 2. Получатель». Всё разложено, типы заданы, шаг влево запрещён.

**HCL** — та же анкета, но с формулами внутри: в графе «количество» не цифра, а «= сколько_на_складе * 2», и есть правила вычисления.

**Jsonnet** — не анкета, а машина, которая печатает анкеты: вы задаёте «склад=Москва», и она выкатывает готовый приказ для Москвы, для Питера и для архива, все согласованные между собой.

Где аналогия ломается: бумажный документ можно понять «по смыслу», а конфиг — нет. Парсер либо разобрал файл, либо нет, и никакой человеческой логики, которая «догадается, что имелось в виду», там не предусмотрено.

## 4. Практика и команды

### 4.1. jq — работа с JSON

Создадим тестовый инвентарь (потом пригодится в лабе):

```bash
cat > stand.json <<'EOF'
{
  "company": "vektor",
  "servers": [
    { "name": "DC01",    "ip": "10.10.10.10", "net": "vmnet8", "role": "ad" },
    { "name": "UBU01",   "ip": "10.10.10.30", "net": "vmnet8", "role": "linux" },
    { "name": "GIT01",   "ip": "10.10.10.50", "net": "vmnet8", "role": "ci" },
    { "name": "WEB01",   "ip": "10.10.20.10", "net": "vmnet10", "role": "web" }
  ]
}
EOF
```

**1. Прочитать и отфильтровать.**

```bash
jq -r '.servers[] | select(.net == "vmnet10") | "\(.name)\t\(.ip)"' stand.json
```

```text
WEB01	10.10.20.10
```

`-r` — raw output, без кавычек вокруг строк (обязательно для shell-скриптов). `.servers[]` — развернуть массив в поток объектов. `select(cond)` — фильтр. `"\(...)"` — интерполяция строк. Смотрим: попали ли в выборку только нужные объекты.

**2. Компактно, с TSV и с проверкой на пустоту.**

```bash
jq -c '.servers[] | select(.role == "ci")' stand.json
jq -r '.servers[] | [.name, .ip, .role] | @tsv' stand.json
jq -e '.servers | length > 0' stand.json; echo "exit=$?"
```

```text
{"name":"GIT01","ip":"10.10.10.50","net":"vmnet8","role":"ci"}
DC01	10.10.10.10	ad
UBU01	10.10.10.30	linux
...
exit=0
```

`-c` — compact (одна строка, удобно для логов и `grep`). `@tsv` — формат вывода в таблицу. `-e` — код возврата 1, если результат `false`/`null`. Это стандартный приём: `jq -e` как проверка в CI-скрипте.

**3. Изменить структуру и передать переменные.**

```bash
jq --arg env prod '.servers |= map(.env = $env)' stand.json | head -n 12
jq -n --argjson n 3 '{replicas: $n, tier: "prod"}'
jq 'paths(scalars) | join(".")' stand.json | sort -u
```

`|=` — обновить значение по пути, `map()` — применить к каждому элементу. `--arg` передаёт строку, `--argjson` — уже готовый JSON-фрагмент, `-n` — не читать stdin (генерируем объект с нуля). `paths(scalars)` — карта всех «листовых» путей файла: лучший способ разобраться в чужом огромном JSON.

⚠️ jq хранит числа как IEEE-754 double. 64-битные ID (например, `id` из GitLab API) теряют точность — берите их строкой (`-r '.id | tostring'`) или через `--arg`.

### 4.2. yq — работа с YAML (и мост к JSON)

Важно: под именем `yq` живут два разных инструмента. Мы используем **mikefarah/yq v4** (Go). Тёзка `kislyuk/yq` — это обёртка над jq с другим синтаксисом; сниппеты из интернета могут не подойти.

**4. Прочитать поле и список полей.**

```bash
yq '.kind' base/deployment.yaml
yq '.spec.template.spec.containers[].image' base/deployment.yaml
yq 'select(.kind == "Deployment") | .metadata.name' overlays/prod/manifest.yaml
```

```text
Deployment
nginx:1.27
vektor-web
```

`.a.b` — путь, `[]` — все элементы массива, `select()` работает и по документам, разделённым `---`. Вторая команда — самый частый вопрос при разборе инцидента: «какой образ реально в манифесте?». `-N` убирает разделители `---` из вывода.

**5. Изменить файл на месте (главная рабочая лошадка).**

```bash
yq -i '.spec.replicas = 3' overlays/prod/deployment.yaml
yq -i '.spec.template.spec.containers[0].image = "nginx:1.27.1"' overlays/prod/deployment.yaml
git diff -- overlays/prod/deployment.yaml
```

`-i` — in-place. **Всегда** смотрите `git diff` после `-i`: yq переписывает файл целиком и может переформатировать кавычки и отступы, раздувая диф. В нормальной работе такие правки идут через merge request, а не руками на сервере.

**6. Конвертация форматов и слияние.**

```bash
yq -o=json '.' base/deployment.yaml > deployment.json      # YAML → JSON
yq -p=json -o=yaml '.' stand.json > stand.yaml             # JSON → YAML
yq ea 'select(fi == 0) * select(fi == 1)' base.yaml override.yaml   # глубокий merge
```

`-o`/`-p` задают выходной и входной формат. `ea` (eval-all) обрабатывает все файлы сразу, `fi` — индекс файла, `*` — рекурсивное слияние объектов. Так делают «дефолты + локальные переопределения» без kustomize.

**7. Аналитика по манифестам.**

```bash
kustomize build overlays/prod | yq 'select(.kind) | [.kind, .metadata.name] | @tsv'
kustomize build overlays/prod | yq '[.kind] | group_by(.) | map({(.[0]): length}) | add'
```

```text
Deployment	prod-vektor-web
Service	prod-vektor-web
{"Deployment": 1, "Service": 1}
```

Быстрый способ увидеть, что вообще уедет в кластер, и не забыли ли вы, например, Service.

### 4.3. TOML

**8. Разобрать `pyproject.toml` без установки чего-либо.** В Python 3.11+ есть встроенный `tomllib` (только чтение):

```bash
python3 -c 'import tomllib, json, sys; print(json.dumps(tomllib.load(open(sys.argv[1],"rb")), indent=2, ensure_ascii=False))' pyproject.toml | head -n 20
```

```text
{
  "project": {
    "name": "vektor-tools",
    "version": "1.2.0",
    "requires-python": ">=3.11"
  },
  ...
}
```

Для записи нужен `tomli_w`/`tomlkit`. Линтер и форматтер TOML — `taplo` (`taplo fmt --check`, `taplo get -f pyproject.toml project.version`) [ПРОВЕРИТЬ актуальный синтаксис флагов taplo].

### 4.4. HCL

**9. Форматирование и проверка.**

```bash
terraform fmt -check -diff
terraform validate
```

`fmt` работает без `init` и без провайдеров — годится как pre-commit хук для любого HCL-репозитория. `validate` требует инициализированного каталога (`terraform init`) [ПРОВЕРИТЬ поведение на конфиге без провайдеров]. Выражения вроде `local.replicas[var.env]` и `for`-циклы проверяются только на `plan`, так что HCL-файл, который «валиден», ещё не значит «разумен».

### 4.5. Jsonnet

**10. Отрендерить конфиг с параметром окружения.**

```bash
jsonnet app.jsonnet --tla-str env=prod | yq -p=json '.spec.replicas'
jsonnetfmt -i app.jsonnet
```

`--tla-str` передаёт параметр верхнего уровня строкой (`--tla-code` — кодом, например `--tla-code 'n=3'`). `jsonnetfmt` — форматтер из пакета go-jsonnet. `-y` печатает YAML-поток из массива объектов, `-S` ожидает на выходе строку — удобно, когда используете `std.manifestYamlDoc`.

### 4.6. kustomize

**11. Собрать оверлей и сравнить окружения.**

```bash
kustomize build overlays/prod
diff <(kustomize build overlays/dev) <(kustomize build overlays/prod)
kustomize build overlays/prod > /tmp/prod.yaml
```

Без кластера. Именно эту команду ставят в CI, чтобы получить артефакт «что будет применено». `diff` двух рендеров — самый быстрый способ проверить, что оверлеи различаются только тем, чем должны.

**12. Правка kustomization.yaml из CLI.**

```bash
kustomize edit set image nginx=nginx:1.27.1
kustomize edit add resource service.yaml
```

`edit` меняет `kustomization.yaml` в текущем каталоге. Удобно в скриптах, но правило «правки только через PR» остаётся.

### 4.7. Валидация

**13. Синтаксис и схема.**

```bash
yamllint -c .yamllint.yml base/ overlays/
jq empty stand.json && echo "json ok"
kustomize build overlays/prod > /tmp/prod.yaml && kubeconform -strict -summary /tmp/prod.yaml
```

`jq empty` — парсит и молчит; ненулевой код возврата = битый JSON. `yamllint` ловит табы, отступы, дубли ключей, длину строк. `kubeconform` проверяет манифесты по схемам Kubernetes — **обязательный** шаг, потому что `kustomize build` схему не знает и опечатку в имени поля пропустит. Схемы качаются из сети; для офлайна есть `-schema-location` [ПРОВЕРИТЬ актуальные пути зеркал].

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **UBU01** (10.10.10.30, Ubuntu Server 24.04) — включена, SSH работает, есть пользователь с `sudo`, есть доступ в интернет или внутреннее зеркало пакетов.
- **GIT01** (10.10.10.50, GitLab CE) — работает, у вас есть доступ; проект `infra/stand-configs` создаётся по ходу (если его нет — создайте пустой, урок 17 у вас уже был).
- Кластер Kubernetes на этом уроке **не нужен**: kustomize только рендерит YAML. Реальный деплой будет в уроке про Kubernetes.
- Если `terraform` ещё не установлен — шаг с HCL делаем в объёме `cat` + `terraform fmt` (при наличии) или просто фиксируем файл в репозитории и возвращаемся к нему в уроке про Terraform.

### Снапшот

В VMware Workstation: выделить ВМ → **VM → Snapshot → Take Snapshot…**, имя `before-devops-023`. Для UBU01 и GIT01 отдельно.

Из командной строки хоста (путь к `vmrun` и к `.vmx` подставьте свой):

```text
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-023
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws snapshot "D:\VMs\GIT01\GIT01.vmx" before-devops-023
```

### Шаги

1. **Вход и рабочая папка (UBU01).**

   ```bash
   ssh adm.student@10.10.10.30     # учётку подставьте свою
   mkdir -p ~/devops/23-formats && cd ~/devops/23-formats
   git init -q
   ```
   Ожидаемо: пустой git-репозиторий, `git status` показывает текущую ветку.

2. **Установка инструментов.**

   ```bash
   sudo apt update && sudo apt install -y jq yamllint jsonnet
   wget -q https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O yq
   sudo install -m 0755 yq /usr/local/bin/yq
   curl -s "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" | bash
   sudo install -m 0755 ./kustomize /usr/local/bin/kustomize
   jq --version; yq --version; kustomize version; jsonnet --version
   ```
   Ожидаемо: четыре строки с версиями (`jq-1.7.1` [ПРОВЕРИТЬ], `yq 4.x`, `kustomize v5.x`, `jsonnet v0.20` [ПРОВЕРИТЬ]). Если GitHub недоступен — ставьте из внутреннего зеркала/`snap`; главное, чтобы `yq` был от mikefarah. Запишите версии в `stand-log.md`.

3. **База: Deployment и Service для `vektor-web`.**

   ```bash
   mkdir -p base overlays/dev overlays/prod
   cat > base/deployment.yaml <<'EOF'
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: vektor-web
     labels:
       app: vektor-web
   spec:
     replicas: 1
     selector:
       matchLabels:
         app: vektor-web
     template:
       metadata:
         labels:
           app: vektor-web
       spec:
         containers:
           - name: web
             image: nginx:1.27
             ports:
               - containerPort: 80
   EOF
   cat > base/service.yaml <<'EOF'
   apiVersion: v1
   kind: Service
   metadata:
     name: vektor-web
   spec:
     selector:
       app: vektor-web
     ports:
       - port: 80
         targetPort: 80
   EOF
   ```
   Ожидаемо: два файла в `base/`. Тег `nginx:1.27` — учебное значение; в уроке про Harbor мы заменим его на образ из HARBOR01 (10.10.10.55).

4. **kustomization для базы и оверлеев.**

   ```bash
   cat > base/kustomization.yaml <<'EOF'
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   resources:
     - deployment.yaml
     - service.yaml
   EOF
   cat > overlays/prod/kustomization.yaml <<'EOF'
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   namespace: vektor-web-prod
   namePrefix: prod-
   resources:
     - ../../base
   images:
     - name: nginx
       newTag: "1.27.1"
   replicas:
     - name: vektor-web
       count: 3
   labels:
     - pairs:
         app.kubernetes.io/part-of: vektor-web
       includeSelectors: false
   EOF
   sed -e 's/vektor-web-prod/vektor-web-dev/' -e 's/1.27.1/1.27/' overlays/prod/kustomization.yaml > overlays/dev/kustomization.yaml
   ```
   Ожидаемо: три `kustomization.yaml`. `includeSelectors: false` важен: менять селектор у живого Deployment нельзя, он неизменяем.

5. **Рендер и сравнение.**

   ```bash
   kustomize build overlays/dev | head -n 20
   diff <(kustomize build overlays/dev) <(kustomize build overlays/prod)
   ```
   Ожидаемо: в dev `prod-` заменено на `dev-`-префикс (у вас получится `prod-`, пока не поправите `namePrefix` — поправьте), `replicas: 1` и `nginx:1.27`, в prod — `replicas: 3` и `nginx:1.27.1`, плюс разные namespace. `diff` показывает ровно эти строки.

6. **Ловушки YAML: проверяем, как парсер видит типы.**

   ```bash
   cat > gotchas.yaml <<'EOF'
   version: 1.10
   mode: 0755
   country: NO
   flag: yes
   tag: "1.10"
   EOF
   yq -o=json '.' gotchas.yaml
   ```
   Ожидаемо: `version: 1.1` (число!), `mode: 493` (восьмеричное), `country: false` (в YAML 1.1-парсере), `flag: true`, `tag: "1.10"` (строка — потому что в кавычках). Вывод: версии, режимы, коды стран **всегда в кавычках**. Сохраните файл в репозиторий как учебный пример.

7. **jq на реальных данных.**

   ```bash
   yq -p=json -o=yaml '.' /dev/null 2>/dev/null || true
   cat > stand.json <<'EOF'
   {"servers":[{"name":"DC01","ip":"10.10.10.10"},{"name":"UBU01","ip":"10.10.10.30"},{"name":"GIT01","ip":"10.10.10.50"}]}
   EOF
   jq -r '.servers[] | select(.ip | startswith("10.10.10.")) | "\(.name)\t\(.ip)"' stand.json
   jq -c '.servers | map(.name)' stand.json
   jq -e '.servers | length >= 3' stand.json; echo "exit=$?"
   ```
   Ожидаемо: три строки таблицы, `["DC01","UBU01","GIT01"]`, `exit=0`. Поменяйте `>= 3` на `> 5` — получите `exit=1`: так `jq -e` используют в проверках.

8. **TOML и HCL: файлы в репозитории.**

   ```bash
   cat > tools.toml <<'EOF'
   [project]
   name = "vektor-tools"
   version = "1.2.0"
   requires-python = ">=3.11"

   [[tool.mypy.overrides]]
   module = "yaml.*"
   ignore_missing_imports = true
   EOF
   python3 -c 'import tomllib,json,sys;print(json.dumps(tomllib.load(open(sys.argv[1],"rb")),ensure_ascii=False))' tools.toml
   cat > main.tf <<'EOF'
   variable "env" {
     type    = string
     default = "dev"
   }

   locals {
     replicas = { dev = 1, prod = 3 }
     name     = "vektor-web"
   }

   output "summary" {
     value = "${local.name}-${var.env}: ${local.replicas[var.env]} replicas"
   }
   EOF
   terraform fmt -check -diff || true
   ```
   Ожидаемо: TOML разворачивается в JSON с секцией `project` и массивом `tool.mypy.overrides`. HCL: `terraform fmt` либо молчит (файл уже отформатирован), либо показывает диф. Синтаксис HCL — блоки с `{}`, ссылки `var.`/`local.`, индексация `[...]`.

9. **Jsonnet: один шаблон — два окружения.**

   ```bash
   cat > app.jsonnet <<'EOF'
   local envs = {
     dev:  { replicas: 1, image: 'nginx:1.27' },
     prod: { replicas: 3, image: 'nginx:1.27.1' },
   };
   function(env='dev')
     local e = envs[env];
     {
       apiVersion: 'apps/v1',
       kind: 'Deployment',
       metadata: { name: 'vektor-web', labels: { app: 'vektor-web', env: env } },
       spec: {
         replicas: e.replicas,
         selector: { matchLabels: { app: 'vektor-web' } },
         template: {
           metadata: { labels: { app: 'vektor-web' } },
           spec: { containers: [{ name: 'web', image: e.image }] },
         },
       },
     }
   EOF
   jsonnet app.jsonnet --tla-str env=prod | yq -p=json '.spec.replicas, .spec.template.spec.containers[0].image'
   ```
   Ожидаемо: `3` и `nginx:1.27.1`. Ключевая мысль: конфиг стал функцией, а не копипастой.

10. **Валидация до прода.**

    ```bash
    printf 'extends: default\nrules:\n  line-length: {max: 120}\n' > .yamllint.yml
    yamllint -c .yamllint.yml base/ overlays/ || true
    kustomize build overlays/prod > /tmp/prod.yaml
    kubeconform -strict -summary /tmp/prod.yaml || echo "kubeconform: проверьте схему/сеть"
    ```
    Ожидаемо: yamllint без ошибок (или предупреждения о длине строк), kubeconform — `Summary: 2 resources found in 1 file - Valid: 2`. Если kubeconform не установлен — поставьте релизным бинарником с GitHub либо пропустите, но помните: без проверки схемы опечатка в имени поля не ловится.

11. **Коммит и пуш в GIT01.**

    ```bash
    printf '*.rendered.yaml\n/tmp/\n' > .gitignore
    git add -A && git commit -q -m "feat(configs): base+overlays, yq/jq/jsonnet примеры"
    git remote add origin git@10.10.10.50:infra/stand-configs.git
    git push -u origin HEAD
    ```
    Ожидаемо: коммит улетает в GitLab на GIT01, проект виден в вебе. С этого момента вся конфигурация стенда — в Git.

### Проверка

- [ ] `jq --version`, `yq --version`, `kustomize version`, `jsonnet --version` отвечают, версии записаны в `stand-log.md`.
- [ ] `kustomize build overlays/prod` выдаёт `replicas: 3`, `image: nginx:1.27.1`, `namespace: vektor-web-prod`.
- [ ] `diff` рендеров dev и prod показывает только namespace/префикс/реплики/тег.
- [ ] `yq -o=json '.' gotchas.yaml` показывает `version: 1.1` и `mode: 493` — вы понимаете, почему.
- [ ] `jq -e '.servers | length >= 3' stand.json` возвращает 0, а при `> 5` — 1.
- [ ] `python3 -c 'import tomllib...' tools.toml` печатает валидный JSON.
- [ ] `kubeconform` говорит `Valid: 2` (или вы осознанно пропустили шаг и записали это).
- [ ] `git log --oneline` на UBU01 и проект `infra/stand-configs` на GIT01 содержат ваш коммит.

### Сломай и почини

> **Пользователь:** ivanov (Иван Иванов, бухгалтерия), ВМ WIN11-CL01
> **Что написал:** «Форма выставления счёта на внутреннем портале показывает старый интерфейс, кнопки “Печать” нет. У Петровой то же. Кэш и браузер чистил.»
> **Срочность:** средняя

Портала в кластере пока нет — он уедет туда в уроке про Kubernetes. Поэтому ловим причину на том этапе, где она рождается: на рендере конфигурации. В проде этот баг выглядел бы ровно так, как его описал Иванов.

**Воспроизводим.** Коллега «поправил» патч для прода и ошибся в ключе:

```bash
cat > overlays/prod/patch-resources.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vektor-web
spec:
  template:
    spec:
      container:                # <- должно быть containers
        - name: web
          image: nginx:1.27.1
EOF
```

Добавьте патч в `overlays/prod/kustomization.yaml`:

```yaml
patches:
  - path: patch-resources.yaml
    target:
      kind: Deployment
      name: vektor-web
```

**Диагностика по порядку.**

1. Что вообще рендерится: `kustomize build overlays/prod > /tmp/prod.yaml` — команда **завершилась успешно** (код 0). Уже подозрительно: сборка зелёная, а поле не применилось.
2. Смотрим образ и реплики: `yq 'select(.kind=="Deployment") | .spec.template.spec.containers[0].image' /tmp/prod.yaml` → `nginx:1.27` (старый), хотя в `images:` overlay стоит `1.27.1`.
3. Ищем, откуда взялось: `yq 'select(.kind=="Deployment") | .spec.template.spec | keys' /tmp/prod.yaml` → в выводе появляется лишний `container` рядом с `containers`. Вот и след.
4. Сверяем с патчем: `yq '.spec.template.spec | keys' overlays/prod/patch-resources.yaml` — `container`, `volumes`… опечатка видна.
5. Проверяем схемой: `kubeconform -strict -summary /tmp/prod.yaml` → ошибка про неизвестное поле `spec.template.spec.container`. Здесь бы её и поймал CI.

**Решение.** Исправить ключ на `containers`, пересобрать, убедиться, что `yq` показывает `nginx:1.27.1`, прогнать `kubeconform`, закоммитить, добавить в CI-пайплайн GIT01 шаг `kustomize build ... | kubeconform -strict` (и `yamllint`) **перед** деплоем.

**Ответ пользователю:** «Иван, спасибо за детали — браузер и кэш ни при чём. В конфигурации обновления была ошибка, из-за неё портал получал предыдущую версию. Исправили, добавили проверку, которая не пропустит такое в следующий раз. Обновите страницу через 10 минут, если не подхватится — напишите, посмотрим точечно.»

**Закрытие заявки:** категория «конфигурация/деплой», причина «опечатка в патче, отсутствовала валидация схемы», решение «патч исправлен + шаг валидации в пайплайне», профилактика — ссылка на MR.

### Что изменилось на стенде

- **UBU01**: установлены `jq`, `yq` (mikefarah v4), `kustomize`, `jsonnet`, `yamllint`, опционально `kubeconform`; каталог `~/devops/23-formats` — git-репозиторий с `base/`, `overlays/dev`, `overlays/prod`, `stand.json`, `gotchas.yaml`, `tools.toml`, `main.tf`, `app.jsonnet`, `.yamllint.yml`.
- **GIT01**: создан проект `infra/stand-configs`, туда запушен первый коммит конфигурации стенда.
- Новых ВМ, адресов и учётных записей не появилось. Кластер k8s не затрагивался.

### Откат

VMware Workstation: выбрать ВМ → **VM → Snapshot → Snapshot Manager** → выбрать `before-devops-023` → **Go To**. Из CLI:

```text
"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" -T ws revertToSnapshot "D:\VMs\UBU01\UBU01.vmx" before-devops-023
```

Если откатывать не хочется — достаточно удалить каталог `~/devops/23-formats` и проект `infra/stand-configs` в GitLab: остальной стенд не затронут.

## 6. Ошибки новичков и безопасность

1. **Табы в YAML.** → `yaml: found character that cannot start any token`, пайплайн красный, деплой стоит. → Только пробелы; настройте редактор (`vim`: `set expandtab`, VS Code: «Insert Spaces»). Проверка: `yamllint`.
2. **Незакавыченные версии и режимы.** → `version: 1.10` становится числом `1.1`, `mode: 0755` — восьмеричным 493, `country: NO` — `false`. Приложение получает не ту версию или падает на парсинге. → Строки со версиями, кодами, режимами, портами-как-строками — в кавычки.
3. **Дубли ключей.** → Go-парсеры (kubectl, kustomize) ругаются `key already set in map`, а PyYAML/некоторые библиотеки молча берут **последнее** значение. Конфиг «работает», но не так, как задумано. → `yamllint` с правилом `key-duplicates`, ревью дифов.
4. **Правка сгенерированного файла.** → Кто-то руками правит отрендеренный `prod.yaml`, следующий `kustomize build` затирает правку. → Правится только источник (`base/`, `overlays/`), артефакт рендера — в `.gitignore`.
5. **`yq -i` без `git diff`.** → Массовая переформатировка файла, диф на 300 строк, ревьюер не видит смысла изменений и пропускает баг. → Мелкие правки, обязательный `git diff`, отдельный коммит «format only».
6. **Опечатка в ключе патча.** → kustomize не знает схемы и молча добавляет мусорное поле; сборка зелёная, изменение не применено (наш кейс из лабы). → Всегда `kubeconform`/`kubectl diff` перед деплоем.
7. **Jsonnet/Helm «на всякий случай».** → Пять микросервисов обёрнуты в самописную библиотеку, которую понимает один человек (он же уволился). → Начинайте с базы + kustomize; генераторы — когда копипасты реально десятки.
8. **Смена селектора у живого Deployment.** → `spec.selector` неизменяем, `kubectl apply` падает `field is immutable`, деплой заблокирован. → Селекторы не трогать; метки добавлять через `labels` с `includeSelectors: false`.

**Безопасность.** Секреты в YAML/TOML/JSON в Git — самая частая утечка в инфраструктуре: `secretGenerator` в kustomize кладёт в манифест **base64, а не шифрование** — это читается одной командой `base64 -d`. Пароли в `values.yaml` Helm, в `terraform.tfvars`, в `app.jsonnet` — то же самое. В рабочей среде: секреты вне репозитория (Vault, SOPS+age, Sealed Secrets, внешний секрет-стор), а в конфиге — ссылка на секрет. Отдельно: не передавайте секреты через `jsonnet --tla-str`/`--ext-str` — аргументы видны в `ps` любому пользователю хоста. И не храните на GIT01 в открытом виде ничего, что жалко потерять, кроме учебных значений стенда.

## 7. Вопросы на собеседовании

**1. Почему Kubernetes работает с YAML, но API у него на JSON? В чём разница?**

YAML — удобство для человека: комментарии, отступы, несколько документов в файле. Сам API-сервер принимает и отдаёт JSON (`Content-Type: application/json`); `kubectl` конвертирует YAML→JSON на клиенте перед отправкой. Отсюда практические следствия: всё, что вы написали, должно корректно сериализоваться в JSON (ключи — строки, дубли ключей — ошибка), а YAML-специфичные штуки вроде якорей разворачиваются на клиенте и до сервера не доезжают. Отдельно: API-сервер работает со схемой, поэтому неизвестное поле в манифесте отклоняется (strict), а тот же мусор в патче kustomize проходит — схему проверяет не он.

*Что хочет услышать интервьюер:* понимание, что формат — это представление, а не отдельная сущность; знание, где именно происходит конвертация.
*Красный флаг:* «k8s читает YAML напрямую, JSON не нужен» или «это одно и то же».

**2. Как безопасно поменять одно поле в 200 манифестах?**

Не руками и не regex по всему репозиторию. Через `yq -i` по списку файлов (`for f in ...; do yq -i '.spec.replicas = 3' "$f"; done`), затем `git diff` глазами, `yamllint`, `kustomize build` (если это оверлеи), `kubeconform` по схемам, и только потом MR. В идеале — вообще не править 200 файлов: заменить копипасту на базу + оверлеи, где значение задаётся в одном `kustomization.yaml`. Проверка результата — не «глазами», а рендером и валидатором в CI.

*Что хочет услышать:* «правка + диф + валидация + ревью», плюс мысль про устранение дублирования.
*Красный флаг:* `sed -i` по всему репо без диффа и без валидации.

**3. Kustomize или Helm? Когда что?**

Helm — шаблонизатор (Go templates) плюс пакетный менеджер: релизы, версии чартов, зависимости, `values.yaml`. Нужен, когда вы отдаёте приложение наружу как продукт или у вас десятки почти одинаковых окружений с логикой (`if`, циклы). Kustomize — «база + точечные патчи», без шаблонов и без логики: проще читать, встроен в `kubectl`, отлично ложится на «наши 5 сервисов, dev/stage/prod». Компромисс: Helm для сторонних чартов + kustomize для своих манифестов и пост-обработки (`helm template | kustomize`). Минус kustomize — в нём нет условий и циклов, и это осознанно.

*Что хочет услышать:* понимание, что выбор определяется числом окружений и потребностью в логике, а не модой.
*Красный флаг:* «kustomize устарел» или «Helm всегда лучше».

**4. Зачем нужен Jsonnet, если есть YAML?**

Когда конфигурация по природе параметрическая: 50 дашбордов Grafana по одному шаблону, многоокруженческие манифесты, конфиги с переиспользованием. Jsonnet даёт функции, импорты, наследование объектов (`+`, `self`, `super`) и TLA-параметры — вы описываете правило, а не 50 копий. Цена: порог входа, слабый тулинг, «программирование вместо конфигурирования». Альтернативы — CUE, Helm, kustomize. Для маленького стенда Jsonnet — оверинжиниринг.

*Что хочет услышать:* что инструмент применяется осознанно, с пониманием цены поддержки.
*Красный флаг:* «Jsonnet умеет всё, везде надо Jsonnet».

**5. Как вы валидируете конфигурацию в CI до деплоя?**

Послойно: (1) синтаксис — `jq empty`, `yaml`-парсер, `taplo check` для TOML, `terraform fmt -check`/`validate` для HCL; (2) стиль и линт — `yamllint` (табы, дубли ключей, длина строк); (3) рендер — `kustomize build`/`helm template` в файл-артефакт; (4) схема — `kubeconform -strict` по получившемуся артефакту; (5) политики — conftest/OPA или Kyverno: «нет latest-тега», «есть requests/limits», «есть labels». Всё это запускается в пайплайне на GIT01 до шага деплоя, а артефакт рендера сохраняется, чтобы понять, что именно уехало.

*Что хочет услышать:* понимание, что `kustomize build` без валидатора схему не проверяет — это и есть главная дыра.
*Красный флаг:* «kubectl apply --dry-run и всё» (без кластера не работает, схемы не знает).

## 8. Источники (официальная документация)

- **yaml.org** — «YAML 1.2 Specification» (разделы про схемы типов и блоки). Поиск: `YAML 1.2 spec`.
- **ietf.org / datatracker.ietf.org** — RFC 8259 «JSON Data Interchange Format», RFC 6902 «JavaScript Object Notation (JSON) Patch», RFC 7386 «JSON Merge Patch». Поиск: `RFC 8259`, `RFC 6902`.
- **json.org** — краткое описание грамматики JSON.
- **toml.io** — «TOML v1.0.0 specification». Поиск: `TOML v1.0.0`.
- **github.com/hashicorp/hcl** — «HCL Native Syntax Specification». Поиск: `HCL native syntax spec`.
- **developer.hashicorp.com** — документация Terraform, разделы про configuration syntax и функции. Поиск: `terraform configuration syntax` [ПРОВЕРИТЬ точное имя раздела].
- **jsonnet.org** — tutorial и language reference (std-библиотека, TLA). Поиск: `jsonnet language reference`.
- **jqlang.github.io/jq** — jq manual (домен перенесён со старого stedolan.github.io) [ПРОВЕРИТЬ]. Поиск: `jq manual`.
- **github.com/mikefarah/yq** — документация и «recipes» (merge, eval-all). Поиск: `mikefarah yq recipes`.
- **kustomize.io** и **kubernetes.io** — «Declarative Management of Kubernetes Objects Using Kustomize», разделы про patches и images. Поиск: `kubernetes kustomize task`, `kustomize patches`.
- **github.com/adrienverge/yamllint** — правила конфигурации линтера. Поиск: `yamllint rules`.
- **github.com/yannh/kubeconform** — README с флагами и источниками схем. Поиск: `kubeconform schema location`.
