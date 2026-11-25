# Российские Kubernetes-платформы: Deckhouse, Яндекс Managed Kubernetes, VK Cloud Containers, Cloud.ru — особенности и выбор

## 1. Введение (зачем это нужно в реальной работе)

Тикет в трекере «Вектора»:

> **Автор:** adm.student (системный администратор)
> **Тема:** Переезд платёжного шлюза на новый кластер — какой платформе отдаём предпочтение?
> **Текст:** «Коллеги, мы выкатили пилот платёжного API на k8s-cp01, но руководство спрашивает: остаёмся на самоподнимаемом кластере или уходим в российское облако? Юристы говорят про 152-ФЗ и реестр отечественного ПО, ИБ просит ФСТЭК, а мне нужно до пятницы обоснование на одну страницу. Помогите с критериями и хотя бы черновиком сравнения.»
> **Срочность:** Высокая, до конца недели.

В 2026 году российский DevOps почти никогда не «просто ставит kubeadm» — он выбирает между платформой в облаке (управляемый control plane) и дистрибутивом на своём железе. Выбор влияет на бюджет, на то, кто дежурит в три ночи, и на то, пройдёт ли система сертификацию. После этой главы ты будешь: понимать модель ответственности managed vs self-managed; знать, чем Deckhouse отличается от «голого» Kubernetes; уметь сравнивать Яндекс Managed Kubernetes, VK Cloud Containers и Cloud.ru Managed Kubernetes по техническим и юридическим критериям; уметь оформить решение как ADR (Architecture Decision Record) в Git и проверить совместимость стенда скриптом. Мы не будем заново разбирать устройство кластера — это было в уроках 33–46.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Все четыре варианта дают тебе один и тот же API Kubernetes на порту 6443, но **граница ответственности** проходит в разных местах.

```text
СЛОИ ПЛАТФОРМЫ И КТО ИХ ОБСЛУЖИВАЕТ

┌──────────────────────────────────────────────────────────────┐
│ 4. Приложение (Deployment, Service, Ingress)   ВСЕГДА ВЫ      │
├──────────────────────────────────────────────────────────────┤
│ 3. Платформенные «батарейки»                                  │
│    (ingress, cert-manager, monitoring, CSI, backup, Vault)    │
│    managed-облако: частично вы │ Deckhouse: готовые модули    │
├──────────────────────────────────────────────────────────────┤
│ 2. Worker-ноды (ОС, kubelet, containerd, CNI, обновления)     │
│    managed: вы (группы узлов) │ Deckhouse: вы (модуль node-manager)│
├──────────────────────────────────────────────────────────────┤
│ 1. Control plane (api-server, etcd, scheduler, controller)    │
│    managed (Яндекс/VK/Cloud.ru): ПРОВАЙДЕР                    │
│    Deckhouse: ВЫ, но обновлениями управляет платформа         │
└──────────────────────────────────────────────────────────────┘
```

Разберём по существу.

**Managed Kubernetes (Яндекс, VK Cloud Containers, Cloud.ru).** Провайдер поднимает control plane в своей зоне, даёт тебе endpoint `https://<id>.k8s.<cloud>/` и kubeconfig. Master-ноды тебе не видны: SSH туда нет, etcd не потрогать, `kubectl get nodes` показывает только воркеры. Ты управляешь группами узлов (node groups), их размером, типом диска, зоной. Провайдер отвечает за доступность API, обновления минорных версий control plane и SLA. Есть «зональный» мастер (одна зона, дешевле) и «региональный/HA» (три зоны, дороже) — [ПРОВЕРИТЬ] актуальные названия у каждого вендора.

**Дистрибутив (Deckhouse).** Ты ставишь кластер на свои машины (`dhctl bootstrap`), control plane физически твой, но обновления, конфигурация и набор модулей управляются платформой. Ключевое отличие от «голого» Kubernetes: Deckhouse — это фреймворк модулей, где ingress-nginx, cert-manager, prometheus, loki, csi, node-manager, user-authn включаются декларативно (CR `ModuleConfig`), а не ставятся руками через Helm в каждом кластере. Плюс поддержка отечественных ОС (Astra Linux, РЕД ОС, ALT) и сертифицированная редакция для КИИ — [ПРОВЕРИТЬ] точный состав редакций и наличие сертификата ФСТЭК.

**Сетевые и прочие порты, которые важны на практике.** API — 6443/TCP (наружу или только в VPC); kubelet — 10250/TCP (между control plane и воркерами, внутри сети кластера); etcd — 2379/2380 (только между master, в managed-облаке тебе недоступен); реестр образов — 443/TCP; при mirror-режиме Deckhouse добавляется ещё один контур: твой внутренний Harbor на 443/TCP.

**Юридический слой — это тоже «под капотом».** В РФ есть реестр российского ПО (reestr.digital.gov.ru), требования 152-ФЗ о локализации персональных данных, приказы ФСТЭК №17 (ГИС) и №239 (КИИ). Для госзаказчика или КИИ выбор managed-кластера у провайдера без нужных аттестаций может быть заблокирован — это не «бюрократия», а реальный фильтр на этапе проектирования. Все конкретные номера и статусы проверяй по первоисточнику: они меняются.

**Модель выбора.** Нет «лучшей платформы», есть фильтр по требованиям: где физически лежат данные → какие требования ИБ → managed или on-prem → какие ОС и железо → бюджет → экосистема.

## 3. Аналогия из жизни

Представь, что тебе нужно жильё под бизнес.

**Managed Kubernetes** — съёмная квартира с управляющей компанией. Вода, отопление, крыша — не твоя головная боль, за это платишь ежемесячно. Но перепланировку согласовывать, УК решает, когда менять стояки, а переезжать придётся по её правилам.

**Self-managed на kubeadm** — дом, построенный своими руками на своём участке. Полная свобода, включая свободу затопить соседей в три ночи и обнаружить, что фундамент треснул.

**Deckhouse** — дом под ключ от застройщика: стены твои, участок твой, но электрика, сантехника и вентиляция уже разведены по проекту, обслуживаются по регламенту и обновляются централизованно. За «под ключ» платишь лицензией (или живёшь в бесплатной редакции с ограничениями — [ПРОВЕРИТЬ] текущие условия).

Где аналогия ломается: в отличие от УК, провайдер managed-кластера может сам обновить control plane до минорной версии, и твои устаревшие манифесты однажды утром просто перестанут применяться — арендодатель не спрашивает.

## 4. Практика и команды

### 4.1. Переключение между кластерами

```bash
kubectl config get-contexts
```
Типичный вывод:
```text
CURRENT   NAME                          CLUSTER
*         vektor-lab                    k8s-cp01.corp.vektor.lab
          yc-vektor-k8s-prod            https://<id>.k8s.yandexcloud.net
```
Смотрим: какой контекст активен, нет ли двух контекстов с одинаковым именем кластера (частая причина «применил не туда»).

```bash
kubectl config use-context yc-vektor-k8s-prod
kubectl config current-context
```
`use-context` меняет активный контекст, `current-context` его печатает. В боевой работе контекст в `PS1`/`starship` — не роскошь, а защита от `kubectl delete` не в том кластере.

### 4.2. Создание кластера в Яндекс Managed Kubernetes

```bash
yc managed-kubernetes cluster create \
  --name vektor-k8s-prod \
  --network-name vektor-net \
  --subnet-name vektor-subnet-a \
  --zone ru-central1-a \
  --release-channel regular \
  --version 1.30 \
  --public-ip
```
`--release-channel` задаёт скорость доставки обновлений (`rapid`, `regular`, `stable` — [ПРОВЕРИТЬ]), `--version` фиксирует минорную версию, `--public-ip` выдаёт публичный адрес API (в проде лучше без него + доступ через VPC/allowlist). Точный список флагов и актуальные версии — `yc managed-kubernetes cluster create --help`.

### 4.3. Группа узлов

```bash
yc managed-kubernetes node-group create \
  --name vektor-ng-app \
  --cluster-name vektor-k8s-prod \
  --zone ru-central1-a \
  --platform standard-v3 \
  --cores 4 --memory 8 \
  --disk-type network-ssd --disk-size 64 \
  --fixed-size 2
```
`--fixed-size 2` — ровно две ноды (автоскейл задаётся иначе, через `--min`/`--max`). `--preemptible` даёт дешёвые прерываемые ноды — для прода не надо. Детали флагов — [ПРОВЕРИТЬ].

### 4.4. Получение kubeconfig

```bash
yc managed-kubernetes cluster get-credentials vektor-k8s-prod --external
```
`--external` — конфиг для доступа через публичный endpoint; без него — для внутреннего. Важно: в Яндекс Облаке kubeconfig может опираться на IAM-токен с ограниченным сроком жизни (до 12 часов — [ПРОВЕРИТЬ]). Симптом «вчера работало, сегодня `Unauthorized`» — почти всегда истёкший токен, а не сломанный кластер.

### 4.5. Terraform для VK Cloud Containers (скелет)

```hcl
terraform {
  required_providers {
    vkcs = {
      source = "vk-cs/vkcs"   # [ПРОВЕРИТЬ] namespace провайдера в реестре
    }
  }
}

resource "vkcs_kubernetes_cluster" "vektor" {
  name                = "vektor-k8s"
  cluster_template_id = data.vkcs_kubernetes_cluster_template.tpl.id
  master_flavor       = data.vkcs_kubernetes_cluster_template.tpl.master_flavors[0].id
  network_id          = vkcs_networking_network.vektor.id
  subnet_id           = vkcs_networking_subnet.vektor.id
}
```
Имена ресурсов и аргументов у провайдера меняются между версиями — сверяйся с документацией провайдера. Проверка без создания ресурсов:
```bash
terraform init -backend=false
terraform fmt -check
terraform validate
```
`-backend=false` не требует настроенного S3/локального стейта, `validate` проверяет синтаксис и типы без обращения к API и без кредов. Смотреть на `Success! The configuration is valid.`

### 4.6. Cloud.ru — через API/Terraform

У Cloud.ru управляемый Kubernetes доступен через портал, API и Terraform-провайдер [ПРОВЕРИТЬ имя провайдера и namespace; исторически платформа была OpenStack-совместимой, поэтому иногда используют провайдер `openstack`]. Практический приём: сначала `terraform providers schema -json` на инициализированном проекте — и ты видишь реальные аргументы ресурса, а не догадки из блогов.

### 4.7. Установка Deckhouse CLI

```bash
# [ПРОВЕРИТЬ] URL бинарника в текущей документации deckhouse.ru
curl -fsSL -o d8 https://deckhouse.ru/downloads/deckhouse-cli/d8-linux-amd64
chmod +x d8 && sudo install -m 0755 d8 /usr/local/bin/d8
d8 version
```
`d8` — CLI платформы (обёртка над kubectl, работа с mirror и релизами). Ключевое: версия CLI должна соответствовать версии платформы, иначе часть подкоманд откажется работать.

### 4.8. Конфиг кластера Deckhouse (подготовка, без запуска)

```yaml
apiVersion: deckhouse.io/v1
kind: ClusterConfiguration
clusterType: Static
podSubnetCIDR: 10.111.0.0/16
serviceSubnetCIDR: 10.222.0.0/16
kubernetesVersion: "1.30"
clusterDomain: cluster.local
---
apiVersion: deckhouse.io/v1
kind: InitConfiguration
deckhouse:
  releaseChannel: Stable
  configOverrides:
    global:
      modules:
        publicDomainTemplate: "%s.k8s.vektor.lab"
```
`clusterType: Static` — своё железо (для облака `Cloud`). `releaseChannel` — `Stable`/`RockSolid`/`EarlyAccess`/`Beta`/`Alpha` [ПРОВЕРИТЬ актуальный набор]. Bootstrap выполняется так (в лаборатории — только читаем, не запускаем):
```bash
dhctl bootstrap --ssh-host=10.10.10.61 --ssh-user=adm.student \
  --ssh-agent-private-keys=/home/adm/.ssh/id_ed25519 \
  --config=config.yaml --resources=resources.yaml
```
Точные имена флагов проверяй через `dhctl bootstrap --help` — они менялись.

### 4.9. Проверка состояния Deckhouse-кластера

```bash
kubectl -n d8-system get pods
kubectl -n d8-system get deckhousereleases
kubectl get moduleconfigs
```
Первое — живы ли системные поды платформы (`deckhouse`, `prometheus`, `cni-cilium` и т.д.). Второе — в каком статусе релиз (`Deployed`, `Pending`, `Superseded`). Третье — список модулей и их состояние.

### 4.10. Включение модуля

```bash
kubectl apply -f - <<'EOF'
apiVersion: deckhouse.io/v1alpha1
kind: ModuleConfig
metadata:
  name: ingress-nginx
spec:
  enabled: true
EOF
kubectl get moduleconfig ingress-nginx -o yaml | grep -A2 status
```
`apiVersion` и структура `spec` — [ПРОВЕРИТЬ] для твоей версии платформы. Ожидаемый результат: в статусе появляется `enabled: true`, в `d8-system` появляются поды ingress.

### 4.11. Проверка узла на соответствие требованиям Deckhouse master

```bash
nproc; free -g | awk '/Mem:/{print $2" GB RAM"}'; df -h /var/lib | tail -1; \
lscpu | grep -Ei 'model name|virtualization'
```
Смотрим: ядра (обычно ≥4 для master), RAM (≥8 ГБ), свободное место (≥40 ГБ), включённую виртуализацию. Пороговые значения — [ПРОВЕРИТЬ] в актуальной документации.

### 4.12. Проверка доступа к реестру с узла

```bash
sudo crictl pull HARBOR01.corp.vektor.lab/deckhouse/kubernetes:latest
sudo journalctl -u containerd -n 30 --no-pager
```
Первое — реальная попытка скачать образ тем же путём, что использует kubelet. Второе — увидеть, что именно ответил containerd: `unauthorized`, `certificate signed by unknown authority`, `no such host`. Это базовый инструмент диагностики `ImagePullBackOff` (урок 45) в приватном контуре.

## 5. Лабораторная работа

### Предусловия
Стенд DevOps в состоянии после уроков 33–46: кластер `k8s-cp01` (10.10.10.61), `k8s-w01` (10.10.10.62), `k8s-w02` (10.10.10.63) развёрнут и работает; `HARBOR01` (10.10.10.55) — реестр образов; `GIT01` (10.10.10.50) — GitLab; `UBU01` (10.10.10.30) — рабочая Linux-станция с SSH. Windows-часть стенда не нужна. Публичный доступ к облакам не обязателен: если интернета нет или не хочется платить — шаги 8–9 выполняются в режиме `terraform validate` без создания ресурсов. Регистрировать аккаунты в облаках не требуется.

### Снапшот
На всех затрагиваемых ВМ — `GIT01`, `HARBOR01`, `UBU01`, `k8s-cp01`:
```text
VMware Workstation → ПКМ по ВМ → Snapshot → Take Snapshot
Имя: before-devops-047
```
Или из командной строки:
```bash
vmrun -T ws snapshot "/path/GIT01.vmx" before-devops-047
vmrun -T ws snapshot "/path/HARBOR01.vmx" before-devops-047
vmrun -T ws snapshot "/path/UBU01.vmx" before-devops-047
vmrun -T ws snapshot "/path/k8s-cp01.vmx" before-devops-047
```
Пути подставь свои. Снапшот делается на выключенной или работающей ВМ — VMware позволяет и так и так, но для консистентности лучше выключить.

### Шаги

1. **UBU01.** Собери инвентарь доступных ресурсов: `nproc`, `free -h`, `df -h /`, `lsblk`. Ожидаемо: 2–4 vCPU, 4–8 ГБ RAM, свободный корень. Запиши значения — они понадобятся для проверки требований Deckhouse.

2. **GIT01.** В GitLab создай проект `vektor/platform-choice` (Private), склонируй на UBU01:
   ```bash
   git clone git@GIT01.corp.vektor.lab:adm.student/platform-choice.git
   cd platform-choice && mkdir -p docs adr scripts terraform
   ```
   Ожидаемо: пустой репозиторий с четырьмя папками.

3. **UBU01.** Создай `docs/requirements.md` — требования «Вектора»: данные физлиц (платежи) хранятся в РФ (152-ФЗ); целевой уровень — 3 кластера (dev/stage/prod); on-prem предпочтителен, но допустим РФ-облак; сертификация ФСТЭК желательна для платёжного контура; команда 3 человека → managed предпочтительнее; GPU не нужен; бюджет ограничен. Ожидаемо: 10–12 строк конкретных, проверяемых требований. Требования без цифр и владельца — не требования.

4. **UBU01.** Создай `docs/platform-matrix.md` — таблица сравнения 4 платформ по 10 критериям: модель (managed/дистрибутив), где control plane, где данные, поддержка отечественных ОС, сертификация, набор «батареек», мультиоблачность/lock-in, SLA и поддержка, ориентир по стоимости, сложность эксплуатации. Заполни честно, где не знаешь — ставь `[ПРОВЕРИТЬ]`. Ожидаемо: файл на 40+ строк, без пустых ячеек.

5. **k8s-cp01.** Создай на UBU01 скрипт `scripts/check-node-requirements.sh`, который проверяет vCPU ≥ 4, RAM ≥ 8 ГБ, диск ≥ 40 ГБ и печатает `PASS`/`FAIL` по каждому пункту с ненулевым кодом возврата при провале. Скопируй на `k8s-cp01` (`scp`) и запусти:
   ```bash
   scp scripts/check-node-requirements.sh adm.student@10.10.10.61:/tmp/
   ssh adm.student@10.10.10.61 'bash /tmp/check-node-requirements.sh; echo "exit=$?"'
   ```
   Ожидаемо: три строки PASS/FAIL и код возврата. Если FAIL по RAM — значит, `k8s-cp01` в текущей конфигурации Deckhouse-single-node не потянет: зафиксируй это в ADR как факт, а не как «ну ладно».

6. **UBU01.** Установи `d8` CLI (по официальной инструкции, URL сверь) и проверь `d8 version`. Ожидаемо: вывод версии CLI. Если установка невозможна (нет интернета) — сохрани в `docs/` вывод `curl -I` к домену загрузки и пометь шаг как заблокированный.

7. **UBU01.** Создай `adr/0001-deckhouse-config-draft.yaml` с `ClusterConfiguration` и `InitConfiguration` из раздела 4.8, подставив `publicDomainTemplate: "%s.k8s.vektor.lab"`. Проверь YAML:
   ```bash
   python3 -c "import yaml,sys; list(yaml.safe_load_all(open('adr/0001-deckhouse-config-draft.yaml'))); print('YAML OK')"
   ```
   Ожидаемо: `YAML OK`. Это дешёвая проверка до того, как `dhctl` потратит 20 минут.

8. **UBU01.** В `terraform/yandex/`, `terraform/vkcs/`, `terraform/cloudru/` создай по минимальному `main.tf` с блоком `required_providers` (ресурсы не создаём). Для каждого:
   ```bash
   cd terraform/vkcs && terraform init -backend=false && terraform validate && cd ../..
   ```
   Ожидаемо: `Success! The configuration is valid.` Если провайдер не найден — это тоже результат: зафиксируй в матрице и ищи актуальное имя в документации вендора.

9. **HARBOR01.** Через веб-интерфейс создай проект `deckhouse` (Private, scan on push). На `UBU01` проверь доступ:
   ```bash
   curl -sS -o /dev/null -w "%{http_code}\n" -u adm.student:'Lab#2026-vektor' https://HARBOR01.corp.vektor.lab/v2/
   ```
   Ожидаемо: `200`. Это точка входа для mirror-режима: в закрытом контуре Deckhouse не может ходить в интернет за образами и тянет их из твоего Harbor.

10. **UBU01.** Заполни `adr/0001-platform-choice.md` по шаблону ADR: Контекст → Рассмотренные варианты → Решение → Последствия (включая риски и что делать, если требования изменятся). Решение должно быть однозначным: одна платформа + одна фраза «почему не остальные». Ожидаемо: 1 страница.

11. **UBU01.** Закоммить и поставь тег:
    ```bash
    git add -A && git commit -m "ADR-0001: выбор k8s-платформы, матрица сравнения, чек-скрипт"
    git tag -a v0.1-platform-choice -m "platform selection draft" && git push --follow-tags
    ```
    Ожидаемо: коммит и тег видны в GitLab.

12. **UBU01.** Запиши изменения в `stand-log.md`: новый проект в GitLab, проект `deckhouse` в Harbor, установленный `d8` на UBU01, созданные каталоги terraform.

### Проверка
- [ ] На GIT01 есть проект `vektor/platform-choice` с тегом `v0.1-platform-choice`.
- [ ] В `docs/requirements.md` не менее 10 проверяемых требований с владельцем.
- [ ] В `docs/platform-matrix.md` все 10 критериев заполнены по 4 платформам, неопределённости помечены `[ПРОВЕРИТЬ]`.
- [ ] `scripts/check-node-requirements.sh` на `k8s-cp01` возвращает код 0 или явный FAIL с причиной.
- [ ] `d8 version` отрабатывает либо шаг задокументирован как заблокированный.
- [ ] YAML-конфиг Deckhouse проходит `yaml.safe_load_all`.
- [ ] Хотя бы один `terraform validate` вернул `Success!`.
- [ ] `curl ... /v2/` к HARBOR01 вернул `200`.
- [ ] В `adr/0001-platform-choice.md` решение сформулировано одной платформой, а не «зависит от ситуации».
- [ ] Запись в `stand-log.md` сделана.

### Сломай и почини

> **Пользователь:** adm.student (владелец стенда)
> **ВМ:** k8s-w01
> **Что написал:** «После того как завёл проект `deckhouse` в Harbor и прописал mirror в containerd — новые поды висят в `ImagePullBackOff`. Старые работают. Демо через час.»
> **Срочность:** Высокая

**Как воспроизвести поломку.** На `k8s-w01` в `/etc/containerd/config.toml` добавь в `[plugins."io.containerd.grpc.v1.cri".registry.mirrors]` секцию `endpoint = ["https://HARBOR01.corp.vektor.lab"]`, но **не добавь** Harbor в `registry.configs."HARBOR01.corp.vektor.lab".tls.insecure_skip_verify` и не положи CA-сертификат. Перезапусти `containerd`.

**Диагностика по порядку.**
1. `kubectl describe pod <pod> -n <ns>` — смотрим точный текст события: `x509: certificate signed by unknown authority` или `401 Unauthorized`.
2. `kubectl get events -n <ns> --sort-by=.lastTimestamp | tail -20` — когда началось и на каких нодах (одна или все).
3. На `k8s-w01`: `sudo crictl pull HARBOR01.corp.vektor.lab/deckhouse/kubernetes:latest` — воспроизводим руками, получаем ту же ошибку.
4. `sudo journalctl -u containerd -n 50 --no-pager | grep -i -E 'harbor|x509|unauthorized'` — подтверждаем причину в логах демона.
5. `curl -sS -o /dev/null -w "%{http_code}\n" https://HARBOR01.corp.vektor.lab/v2/` с ноды — проверяем, что сеть и DNS в порядке и проблема именно в доверии к сертификату.
6. `sudo openssl s_client -connect HARBOR01.corp.vektor.lab:443 -showcerts </dev/null | head -30` — кто и чем подписал сертификат.

**Решение.** Скопировать CA-сертификат Harbor на ноду (например, `/usr/local/share/ca-certificates/vektor-harbor-ca.crt`), выполнить `sudo update-ca-certificates`, в `config.toml` прописать путь к CA в `registry.configs."HARBOR01.corp.vektor.lab".tls.ca_file`, перезапустить `containerd`, повторить `crictl pull`. `insecure_skip_verify = true` — только как временная мера на стенде с комментарием, в боевой среде так нельзя.

**Ответ пользователю:** «Причина — нода не доверяет сертификату Harbor после включения mirror. Добавил корневой CA в доверенные и прописал его в конфиге containerd, перезапустил демон. Образ тянется, поды поднялись. Закреплю CA в конфигурации нод через Ansible/cloud-init, чтобы не повторилось на новых узлах.»

**Закрытие заявки:** статус Resolved, в описании — причина, фикс, ссылка на коммит с конфигом. Отдельной задачей — «автоматизировать доставку CA на все ноды».

### Что изменилось на стенде
- **GIT01 (10.10.10.50):** новый проект `adm.student/platform-choice`, тег `v0.1-platform-choice`; каталоги `docs/`, `adr/`, `scripts/`, `terraform/`.
- **HARBOR01 (10.10.10.55):** новый приватный проект `deckhouse` (scan on push).
- **UBU01 (10.10.10.30):** установлен `d8` CLI (если шаг прошёл); локальная копия репозитория `~/platform-choice`.
- **k8s-cp01 (10.10.10.61):** запуск `check-node-requirements.sh` (только чтение, изменений нет); зафиксирован результат проверки ресурсов.
- **k8s-w01 (10.10.10.62):** изменён `/etc/containerd/config.toml` (mirror + CA для HARBOR01), добавлен CA-сертификат в системное хранилище; `containerd` перезапущен.
- **Документы:** ADR-0001, матрица сравнения, требования — источник истины для следующих уроков модуля.

### Откат
Через GUI VMware Workstation: ПКМ по ВМ → Snapshots → `before-devops-047` → Restore. Или:
```bash
vmrun -T ws revertToSnapshot "/path/k8s-w01.vmx" before-devops-047
vmrun -T ws revertToSnapshot "/path/HARBOR01.vmx" before-devops-047
vmrun -T ws revertToSnapshot "/path/UBU01.vmx" before-devops-047
vmrun -T ws revertToSnapshot "/path/GIT01.vmx" before-devops-047
vmrun -T ws start "/path/k8s-w01.vmx"
```
Помни: откат вернёт и состояние диска, и содержимое Git — незакоммиченные изменения пропадут.

## 6. Ошибки новичков и безопасность

1. **Выбор платформы по хайпу, а не по требованиям.** → Через полгода выясняется, что платёжные данные нельзя размещать в выбранном облаке, и переезд стоит дороже, чем выбор. → Сначала `requirements.md` с владельцем и цифрами, потом матрица.

2. **«Managed значит, нам ничего не делать».** → Воркер-ноды, обновления kubelet, PVC, Ingress, лимиты, алерты — на вас; провайдер отвечает только за control plane. → В ADR явно разделите зоны ответственности провайдера и команды.

3. **Bootstrap Deckhouse без проверки ресурсов.** → Установка падает или кластер живёт в постоянном OOM. → Скрипт проверки требований до установки (шаг 5).

4. **Mirror-режим без CA и без проверки `crictl pull`.** → `ImagePullBackOff` на новых узлах, при этом старые поды живы — классическая ловушка. → Проверять `crictl pull` на каждой новой ноде до ввода в строй.

5. **Долгоживущий kubeconfig в Git.** → Утечка токена = полный доступ к кластеру. → Короткоживущие токены (IAM/OIDC), `git-secrets`/gitleaks в CI, `.gitignore` на `*.kubeconfig`.

6. **Игнорирование vendor lock-in.** → Аннотации `service.beta.kubernetes.io/*`, IAM-привязки и CSI конкретного провайдера цементируют приложение в облаке. → Helm-чарты с values-per-cloud, абстракции в манифестах, проверка «сможем ли поднять это на k3s за день».

7. **Публичный API-endpoint без ограничений.** → Массовые сканы и попытки эксплуатации. → Приватный endpoint, allowlist по IP, включённый audit log.

8. **Обновление control plane и воркеров без плана.** → Дрейф версий, несовместимость аддонов, `drain` без PDB. → Канал обновлений + окно + откат (урок 46).

### Безопасность
В боевой среде нельзя: давать `cluster-admin` «на всякий случай» (используйте RBAC и namespace-scoped роли — урок 41); отключать проверку TLS к реестру; хранить секреты в etcd без шифрования и без внешнего хранилища секретов; запускать поды без `securityContext` и Pod Security Standards. Отдельно про лабораторию: единый пароль `Lab#2026-vektor` и `insecure_skip_verify` — учебные послабления. В реальной среде пароли уникальны, сертификаты доверенные, а любое исключение из TLS-проверки требует тикета с обоснованием и сроком.

## 7. Вопросы на собеседовании

**1. Чем managed Kubernetes отличается от self-managed, и что остаётся на вашей зоне ответственности?**
Ответ: в managed провайдер обслуживает control plane (api-server, etcd, scheduler, controller-manager), его доступность и обновления минорных версий; вам остаются воркер-ноды и их ОС, обновления kubelet/containerd, CNI-настройки, CSI и PVC, ingress-контроллер, addon'ы, RBAC, секреты, ресурсные лимиты, мониторинг и алертинг приложений, прикладные манифесты и DR. В self-managed добавляется всё, что выше, плюс etcd (бэкапы!), сертификаты API, HA control plane и обновления самого кластера.
*Что хочет услышать интервьюер:* чёткую границу «провайдер vs мы» и понимание, что managed не отменяет эксплуатацию.
*Красный флаг:* «В managed вообще ничего не надо делать».

**2. Как выбрать между Deckhouse и облачным managed-кластером?**
Ответ: по требованиям. Если данные обязаны лежать on-prem (или в аттестованном контуре), нужна поддержка Astra/РЕД ОС и предсказуемая стоимость при постоянной нагрузке — Deckhouse: свои узлы, готовые модули, единый подход к десятку кластеров. Если команда маленькая, нагрузка переменная, нужна эластичность и не хочется держать дежурство по control plane — managed в РФ-облаке. Гибрид тоже нормален: managed в облаке для клиентских сервисов, Deckhouse on-prem для платёжного контура с требованиями ИБ.
*Что хочет услышать интервьюер:* что решение выводится из требований, а не из личных предпочтений, и что вы знаете про гибрид.
*Красный флаг:* «Deckhouse всегда лучше, потому что российский».

**3. Что такое mirror-режим Deckhouse и зачем он нужен?**
Ответ: режим работы в закрытом контуре (air-gapped), при котором все образы платформы и модулей копируются во внутренний реестр (у нас HARBOR01), а кластер настраивается тянуть их только оттуда. Нужен там, где нет прямого выхода в интернет по требованиям ИБ, либо там, где нужна воспроизводимость и контроль версий образов. Требует места на registry, процедуры обновления зеркала при апгрейде платформы и корректной настройки доверия к CA реестра на всех нодах.
*Что хочет услышать интервьюер:* понимание, что это не «флажок», а процесс с регламентом обновления.
*Красный флаг:* «Просто копируем образы один раз при установке».

**4. Как обеспечить требования 152-ФЗ и реестра российского ПО при выборе платформы?**
Ответ: определить категорию данных и где они физически хранятся; проверить, что провайдер размещает ресурсы в РФ-дата-центрах и имеет нужные документы; для госзаказчика и КИИ — проверить наличие ПО в реестре российского ПО и статус сертификации (ФСТЭК) именно той редакции, которую ставите; зафиксировать это в ADR со ссылками на документы и датой проверки, потому что статусы меняются. Технически — шифрование, аудит-логи, разграничение доступа, резервное копирование.
*Что хочет услышать интервьюер:* что вы проверяете первоисточники и фиксируете проверку, а не верите презентации.
*Красный флаг:* уверенные точные номера приказов и «сертификат есть у всех».

**5. Как избежать vendor lock-in в managed Kubernetes?**
Ответ: держать манифесты в Git и поставлять их одним способом (Helm/Kustomize) независимо от облака; вынести облачные различия в values/overlay (класс StorageClass, аннотации LoadBalancer, IAM); не использовать провайдерные CRD в прикладном коде без обёртки; проверить план миграции — «поднимем ли мы это на k3s/Deckhouse за день»; тестировать восстановление из бэкапа в другом окружении; избегать привязки к proprietary-сервисам там, где хватает стандартных примитивов.
*Что хочет услышать интервьюер:* осознание компромисса — 100% переносимости не бывает, но цена переезда должна быть известна заранее.
*Красный флаг:* «Мы за lock-in не переживаем, всё в Git».

## 8. Источники (официальная документация)

- **Deckhouse** — домен `deckhouse.ru`. Разделы: «Deckhouse Kubernetes Platform → Установка», «Требования к ресурсам», «Редакции и лицензирование», «Mirror-режим / работа в закрытом контуре», «Модули (ModuleConfig)». Поисковые фразы: `deckhouse kubernetes platform installation requirements`, `deckhouse mirror mode private registry`, `deckhouse moduleconfig enable module`.
- **Яндекс Облако** — домен `yandex.cloud`. Раздел: «Managed Service for Kubernetes → Создание кластера», «Группы узлов», «Управление доступом (IAM)», «Практические руководства». Поиск: `yandex cloud managed kubernetes cluster create`, `yc managed-kubernetes node-group create`.
- **VK Cloud** — домен `vk.cloud`. Раздел: «Cloud Containers / Контейнеры → Kubernetes», «Terraform-провайдер». Поиск: `vk cloud containers kubernetes terraform provider`, `vkcs_kubernetes_cluster`.
- **Cloud.ru** — домен `cloud.ru`. Раздел: «Managed Kubernetes / Container Cloud», «Terraform-провайдер», «API». Поиск: `cloud.ru managed kubernetes`, `cloudru terraform provider`. Имя провайдера и структуру ресурсов — [ПРОВЕРИТЬ].
- **Реестр российского ПО** — домен `reestr.digital.gov.ru`. Раздел: поиск по реестру. Поиск: `реестр российского ПО Deckhouse`, `реестр российского ПО Kubernetes платформа`.
- **ФСТЭК России** — домен `fstec.ru`. Разделы: приказы №17 и №239, требования к средствам контейнеризации [ПРОВЕРИТЬ точное название документа]. Поиск: `ФСТЭК требования средства контейнеризации`, `fstec приказ 239 кии`.
- **Kubernetes** — домен `kubernetes.io`. Раздел: «Concepts → Cluster Architecture», «Tasks → Administer a Cluster». Поиск: `kubernetes cluster architecture control plane`.
- **Terraform Registry** — домен `registry.terraform.io`. Поиск: `vkcs provider`, `cloudru provider` — для проверки актуальных namespace и версий провайдеров [ПРОВЕРИТЬ].
- **Harbor** — домен `goharbor.io`. Раздел: «Configuring Harbor → Projects», «Registry authentication». Поиск: `harbor project robot account pull`.

Все конкретные версии, цены, названия редакций и статусы сертификации на момент чтения обязательно сверяй по первоисточникам — они меняются несколько раз в год.
