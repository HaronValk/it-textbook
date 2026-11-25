# Яндекс Облако: Compute, VPC, Managed Kubernetes, Object Storage, IAM, Managed DB

## 1. Введение (зачем это нужно в реальной работе)

Тикет в helpdesk «Вектора»:

> **Пользователь:** ivanov (бухгалтерия)
> **ВМ:** WIN11-CL01
> **Что написал:** «Открываю по закладке дашборд отчётов, который вы на прошлой неделе поднимали в облаке — `ERR_CONNECTION_TIMED_OUT`. У Петровой открывается, я проверил. Срочность: средняя, но отчёт нужен к вечеру».

Классика: «у меня не работает, а у неё работает». Дальше выясняется, что вы ограничили доступ к облачной ВМ по адресу офиса, а ivanov сидит за другим NAT-шлюзом — и вся «поломка» решается одной строкой в списке правил безопасности. Ровно поэтому облачные темы спрашивают на собеседованиях: они проверяют, умеете ли вы думать про сеть, доступ и ответственность одновременно.

В работе DevOps облако — это не «кнопка создать сервер», а набор взаимосвязанных слоёв: сеть (VPC), доступ (IAM), вычисления (Compute и Managed Kubernetes), хранение (Object Storage) и базы данных как сервис (Managed DB). Русские облака при этом нужны почти в каждой вакансии 2026 года: требование хранить персональные данные в РФ (152-ФЗ) и импортозамещение выталкивают компании в Yandex Cloud, VK Cloud, Selectel, Cloud.ru.

После этой главы вы сможете: поднять с нуля каталог в Yandex Cloud, собрать сеть с приватными подсетями и NAT-шлюзом, запустить ВМ и управляемый кластер Kubernetes, положить артефакт в S3-совместимый бакет, создать управляемый PostgreSQL, выдать сервисному аккаунту минимальные роли и — главное — диагностировать типовую облачную аварию за 5 минут, а не за час.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Иерархия ресурсов.** Всё в Yandex Cloud живёт в дереве, и права наследуются сверху вниз:

```text
Организация (Yandex Cloud Organization)
└── Облако (cloud)  vektor-cloud
    ├── Каталог (folder)  vektor-dev          <- сюда кладём лабораторные ресурсы
    │   ├── VPC: сеть vektor-net
    │   │   └── подсеть vektor-subnet-a  10.20.0.0/24  (ru-central1-a)
    │   ├── Compute: vektor-app01
    │   ├── Managed Kubernetes: k8s-vektor
    │   ├── Object Storage: бакет vektor-artifacts-2026
    │   └── Managed PostgreSQL: pg-vektor
    └── Каталог (folder)  vektor-prod
```

Каталог (folder) — это «папка проекта»: единица биллинга, изоляции и выдачи прав. Роль, выданная на облако, действует во всех каталогах; роль на каталог — только в нём.

**Зоны доступности.** Регион `ru-central1` (данные физически в РФ — Москва, Владимир, Калуга, Рязань `[ПРОВЕРИТЬ актуальный список площадок]`) делится на зоны: `ru-central1-a`, `ru-central1-b`, `ru-central1-d`. Зона — это независимая площадка с электричеством и сетью. Правило: один хост = одна зона, отказоустойчивость = минимум две зоны. Устаревшую `ru-central1-c` для новых ресурсов не используем `[ПРОВЕРИТЬ]`.

**VPC (Virtual Private Cloud).** Сеть → подсети (каждая привязана к одной зоне, своя CIDR-подсеть) → группы безопасности (security group, SG). SG — это stateful-фильтр на уровне L3/L4: проверяет IP, протокол, порт, помнит соединение (обратный трафик разрешать не надо). SG навешивается на *сетевой интерфейс* конкретной ВМ/ноды, а не на подсеть. Внутри одной SG есть «магическое» правило: по умолчанию разрешён весь трафик между интерфейсами с этой SG `[ПРОВЕРИТЬ поведение default-sg]`.

Два способа дать выход в интернет:

```text
Вариант 1 (быстро, дороже): у сетевого интерфейса есть публичный IPv4
  ВМ 10.20.0.1  --[nat-ip-version=ipv4]-->  интернет

Вариант 2 (правильно, дешевле): NAT-шлюз + таблица маршрутов на подсети
  ВМ 10.20.0.1 (без публичного IP)
        | маршрут 0.0.0.0/0
        v
  NAT-шлюз vektor-nat-gw --> публичный IP шлюза --> интернет
```

Входящий трафик из интернета ко второй схеме не идёт вообще — именно так строят приватные подсети для нод Kubernetes и баз данных.

**IAM (Identity and Access Management).** Три сущности, которые надо различать:

- **аккаунт пользователя** (вы, `adm.student`-аналог в облаке) — логин/пароль + федерация;
- **сервисный аккаунт (SA)** — «робот», под которым работает Terraform, CI или приложение;
- **роль** — набор разрешений (`viewer`, `editor`, `vpc.admin`, `compute.admin`, `k8s.editor`, `storage.editor`, `mdb.admin`).

Ключи бывают двух видов, и это любимый вопрос на собеседовании:
- **авторизованный ключ** (`yc iam key create`) — RSA-пара, из неё `yc`/Terraform/SDK получают короткоживущий IAM-токен (JWT). Для Compute, VPC, Kubernetes.
- **статический ключ доступа** (`yc iam access-key create`) — пара `key_id`/`secret` в стиле AWS. Только для S3-совместимых API (Object Storage, Container Registry).

**Сервисы, которые надо знать по именам.** Compute Cloud (IaaS, ВМ с дисками `network-ssd`/`network-hdd`, прерываемые ВМ); Object Storage (S3 API, endpoint `https://storage.yandexcloud.net`, классы `standard`/`cold`/`ice`, версионирование, lifecycle); Managed Service for Kubernetes (мастер-плоскость управляет провайдер, ноды — ваши ВМ в вашем VPC); Managed Service for PostgreSQL/MySQL/ClickHouse/MongoDB/Redis (PaaS: бэкапы, реплики, обновления — на стороне провайдера); Container Registry, Lockbox (секреты), KMS.

**Кто за что отвечает в Managed Kubernetes:**

```text
[ вы ]      приложение, манифесты, политики, ноды (их размер, версия, SG)
[ провайдер] control plane: apiserver, etcd, резервные копии, патчи, SLA
[ вы ]      при kubectl -> HTTPS 443 -> api.yandex.cloud -> ваш кластер
```

Порты, которые вам понадобятся: 22 (SSH), 80/443 (HTTP/HTTPS и API кластера), 6432 (пулер Managed PostgreSQL), 5432 (прямое подключение), 443 (Object Storage).

**Модель ответственности.** Compute — IaaS: ОС, патчи, sshd — ваше. Managed Kubernetes — CaaS: ноды и поды — ваше, мастер — провайдера. Managed DB — PaaS: схема, пользователи, индексы — ваше; бэкапы и железо — провайдера. Object Storage — почти SaaS: только доступ и lifecycle-политики. Подробно про IaaS/PaaS/SaaS и FinOps — урок 68.

## 3. Аналогия из жизни

Представьте бизнес-центр. **VPC** — это этаж с коридорами и стенами: вы арендуете помещение, но планировку коридоров задаёт арендодатель, а свою дверь и замок (security group) ставите вы. **Compute** — арендованный кабинет с мебелью: ключи у вас, уборку и ремонт розеток делаете сами. **Managed Kubernetes** — «офис под ключ с управляющим»: управляющий (мастер-плоскость) следит за светом и лифтами, но сотрудников (поды) нанимаете, обучаете и увольняете вы. **Object Storage** — склад самообслуживания с ячейками: платите за занятый объём, доступ по пропуску. **IAM** — пропускная система: пропуск выдаётся на конкретный этаж и конкретную дверь, а не «на весь бизнес-центр». **Managed DB** — арендованный сейф с дежурным администратором: он делает копии и меняет замки, но что именно лежит в ячейках, знаете только вы.

Аналогия ломается в одном месте: в бизнес-центре аренда заканчивается, когда вы съехали, а в облаке забытая ВМ или мастер Kubernetes продолжают списывать деньги с вашего счёта каждый час.

## 4. Практика и команды

Всё выполняем на **UBU01** (10.10.10.30) — это ваша рабочая Linux-машина с доступом в интернет. По ходу лабораторной понадобятся `jq` и `aws-cli` (`sudo apt install -y jq awscli`), `kubectl` — по инструкции kubernetes.io.

**1. Установка и инициализация CLI**

```bash
curl -sSL https://storage.yandexcloud.net/yandexcloud-yc/install.sh | bash
exec -l $SHELL
yc init          # интерактивно: токен из браузера, облако, каталог, зона по умолчанию
yc config list
```

`yc init` спрашивает OAuth-токен (даёт ссылку), затем просит выбрать облако/каталог и зону. Вывод `yc config list`:

```text
user-account:         aje********@yandex.ru
cloud-id:             b1g********
folder-id:            b1g********
compute-default-zone: ru-central1-a
```

Смотрим: `folder-id` — куда по умолчанию пойдут ресурсы, `compute-default-zone` — зона. Конфиг лежит в `~/.config/yandex-cloud/config.yaml`, там же хранятся ключи — в Git не коммитим.

**2. Каталог и сеть**

```bash
yc resource-manager cloud list
yc resource-manager folder create --name vektor-dev
yc config set folder-id $(yc resource-manager folder get --name vektor-dev --format json | jq -r .id)

yc vpc network create --name vektor-net
yc vpc subnet create --name vektor-subnet-a \
  --network-name vektor-net --zone ru-central1-a --range 10.20.0.0/24
yc vpc subnet list
```

`--range` — CIDR подсети; она не должна пересекаться с 10.10.10.0/24 и 10.10.20.0/24 вашего стенда, иначе получите «интересную» жизнь при VPN-связке. В выводе `list` смотрим колонки `NAME / NETWORK / ZONE / RANGE`.

**3. Группа безопасности**

```bash
SG=$(yc vpc security-group create --name vektor-sg-app --network-name vektor-net \
      --format json | jq -r .id)

yc vpc security-group update-rules $SG \
  --add-rule "direction=ingress,port=22,protocol=tcp,v4-cidrs=[203.0.113.10/32],description=ssh-office"
yc vpc security-group update-rules $SG \
  --add-rule "direction=ingress,port=80,protocol=tcp,v4-cidrs=[0.0.0.0/0],description=http-public"
yc vpc security-group get $SG
```

`direction=ingress|egress`, `protocol=tcp|udp|icmp|any`, `port` — один порт или диапазон `8000-8100`, `v4-cidrs=[...]` — список источников. `203.0.113.10` — учебный адрес из диапазона TEST-NET-3, подставьте свой внешний IP (узнать: `curl -s ifconfig.me`). Важно: из облака вы видите **внешний** IP офиса, а не 10.10.10.0/24 — трафик проходит через NAT.

**4. NAT-шлюз и таблица маршрутов**

```bash
yc vpc gateway create --name vektor-nat-gw
GW=$(yc vpc gateway get --name vektor-nat-gw --format json | jq -r .id)

yc vpc route-table create --name vektor-rt-nat --network-name vektor-net \
  --route destination=0.0.0.0/0,gateway-id=$GW
yc vpc subnet update --name vektor-subnet-a --route-table-name vektor-rt-nat
```

Синтаксис `--route` с `gateway-id` сверяйте по `yc vpc route-table create --help` `[ПРОВЕРИТЬ]`. Проверка: `yc vpc subnet get --name vektor-subnet-a` — в поле `route_table_id` должен появиться ID таблицы.

**5. Виртуальная машина**

```bash
cat > cloud-init.yaml <<'EOF'
#cloud-config
package_update: true
packages: [nginx]
write_files:
  - path: /var/www/html/index.html
    content: "vektor cloud app01 ok\n"
runcmd:
  - systemctl enable --now nginx
EOF

yc compute instance create --name vektor-app01 \
  --zone ru-central1-a \
  --network-interface subnet-name=vektor-subnet-a,nat-ip-version=ipv4,security-group-ids=$SG \
  --create-boot-disk image-folder-id=standard-images,image-family=ubuntu-2404-lts,size=20,type=network-ssd \
  --cores 2 --memory 2 \
  --metadata-from-file user-data=cloud-init.yaml \
  --ssh-key ~/.ssh/id_ed25519.pub
```

Разбор: `image-folder-id=standard-images` — публичный каталог образов, `image-family` — «последняя версия семейства» (список: `yc compute image list --folder-id standard-images`), `nat-ip-version=ipv4` — публичный IP, `--metadata-from-file user-data=` — cloud-init (урок 63). `ubuntu-2404-lts` `[ПРОВЕРИТЬ наличие в standard-images]`.

**6. Инвентаризация и вход**

```bash
yc compute instance list
yc compute instance get --name vektor-app01 --format json | jq -r '.network_interfaces[0].primary_v4_address.one_to_one_nat.address'
ssh ubuntu@<полученный-IP>
curl -s http://<IP>/
```

`list` показывает `NAME / ZONE / STATUS / PUBLIC IP`. `STATUS=RUNNING`, страница `vektor cloud app01 ok` — ВМ жива. Пользователь по умолчанию в образах Ubuntu — `ubuntu`.

**7. Сервисный аккаунт и ключи**

```bash
yc iam service-account create --name sa-vektor-tf
SA_ID=$(yc iam service-account get --name sa-vektor-tf --format json | jq -r .id)

for R in vpc.admin compute.admin k8s.admin storage.admin mdb.admin; do
  yc resource-manager folder add-access-binding $(yc config get folder-id) \
    --role $R --service-account-id $SA_ID
done

yc iam key create --service-account-id $SA_ID --output sa-key.json
chmod 600 sa-key.json
yc config profile create tf
yc config profile set tf service-account-key sa-key.json
yc config profile activate tf && yc config list
```

`add-access-binding` добавляет роль к каталогу; `--role editor` не выдаём — это «почти админ» на всё. Файл `sa-key.json` — секрет уровня «ключ от квартиры»: в Git не попадает, только SOPS/Lockbox (урок 66).

**8. Object Storage через S3 API**

```bash
yc iam access-key create --service-account-name sa-vektor-tf --format json
# из вывода берём access_key.key_id и access_key.secret

export AWS_ACCESS_KEY_ID=<key_id>
export AWS_SECRET_ACCESS_KEY=<secret>
EP=https://storage.yandexcloud.net

aws --endpoint-url=$EP s3 mb s3://vektor-artifacts-2026-$RANDOM
aws --endpoint-url=$EP s3 cp ./cloud-init.yaml s3://<bucket>/builds/
aws --endpoint-url=$EP s3 ls s3://<bucket>/builds/
```

Имя бакета глобально уникально в Yandex Cloud, поэтому суффикс. `--endpoint-url` обязателен: обычный `aws s3` по умолчанию идёт в Amazon.

**9. Managed Kubernetes**

```bash
yc managed-kubernetes cluster create --name k8s-vektor \
  --network-name vektor-net --subnet-name vektor-subnet-a --zone ru-central1-a \
  --service-account-id $SA_ID --node-service-account-id $SA_ID \
  --release-channel regular --security-group-ids $SG
# создание мастера занимает порядка 10-15 минут

yc managed-kubernetes node-group create --name ng-vektor --cluster-name k8s-vektor \
  --cores 2 --memory 4 --disk-size 32 --disk-type network-ssd \
  --fixed-size 1 --preemptible --container-runtime containerd
# сетевые параметры нод (подсеть, SG, отсутствие публичного IP) задаются
# флагом --network-interface ... [ПРОВЕРИТЬ синтаксис в вашей версии CLI]

yc managed-kubernetes cluster get-credentials --name k8s-vektor --external
kubectl get nodes -o wide
```

`--preemptible` — прерываемая нода: дешевле, но провайдер может выключить её; для продакшена так не делают. `get-credentials` пишет kubeconfig в `~/.kube/config`; `--external` — подключаться к публичному эндпоинту мастера.

**10. Managed PostgreSQL**

```bash
yc managed-postgresql cluster create --name pg-vektor \
  --environment PRODUCTION --network-name vektor-net --security-group-ids $SG \
  --host zone=ru-central1-a,subnet-name=vektor-subnet-a \
  --resource-preset s2.micro --disk-size 10 --disk-type network-ssd \
  --postgresql-version 16 \
  --user name=vektor_app,password='Lab#2026-vektor' \
  --database name=vektor_app
```

Дальше берём FQDN:

```bash
yc managed-postgresql cluster list-hosts --name pg-vektor
psql "host=<fqdn> port=6432 dbname=vektor_app user=vektor_app sslmode=require"
```

`6432` — пулер соединений (рекомендуемый путь), `5432` — прямое подключение. `sslmode=require` — минимум для лабораторной; для `verify-full` нужен CA-сертификат облака `[ПРОВЕРИТЬ ссылку на CA.pem в документации]`.

**11. То же самое кодом (Terraform, урок 61-62)**

```hcl
terraform {
  required_providers {
    yandex = { source = "yandex-cloud/yandex" }
  }
}

provider "yandex" {
  zone = "ru-central1-a"
}

resource "yandex_vpc_network" "vektor" {
  name = "vektor-net"
}

resource "yandex_vpc_subnet" "a" {
  name           = "vektor-subnet-a"
  zone           = "ru-central1-a"
  network_id     = yandex_vpc_network.vektor.id
  v4_cidr_blocks = ["10.20.0.0/24"]
}
```

Провайдер авторизуется через `service_account_key_file`, а `cloud_id`/`folder_id` удобно передавать переменными окружения `YC_CLOUD_ID`, `YC_FOLDER_ID`.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- Стенд трека DevOps: **UBU01** (10.10.10.30) включён, есть интернет-выход (напрямую через VMnet8 или через `GW01`, если вы его делали на L2), установлены `jq`, `awscli`, `kubectl`, есть SSH-ключ `~/.ssh/id_ed25519`.
- Аккаунт в Yandex Cloud. Новым аккаунтам обычно дают стартовый грант (в 2024 это было 4000 ₽ на 60 дней) `[ПРОВЕРИТЬ актуальные условия и требования к карте на 2026]`. Если завести аккаунт нельзя — пройдите шаги 1–2 и 5–6 в режиме чтения документации, а кластер Kubernetes отработайте локально на `k8s-cp01`–`k8s-w02`; к облаку вернитесь, когда аккаунт появится.
- В облаке заранее должен быть создан платёжный аккаунт и каталог — это делается в консоли.

### Снапшот

В VMware Workstation на **UBU01** (и на **GIT01**, если храните IaC там): меню VM → Snapshot → Take Snapshot, имя `before-devops-069`.

Плюс бэкап локального конфига CLI:

```bash
cp -r ~/.config/yandex-cloud ~/yc-config-backup-069
```

Сразу проговорим: снапшот VMware **не откатывает облако**. Облачные ресурсы удаляются только командами из раздела «Откат».

### Шаги

1. **UBU01.** `yc --version` и `yc init`, затем `yc config list`. Ожидаем: в выводе заполнены `cloud-id`, `folder-id`, `compute-default-zone: ru-central1-a`.
2. **UBU01.** В консоли облака (billing) выставьте бюджет с уведомлением на 500 ₽ — например, на весь каталог. Ожидаем: в разделе «Бюджеты» появилась запись. Это ваша страховка от «забыл удалить кластер».
3. **UBU01.** Создайте каталог `vektor-dev`, сеть `vektor-net`, подсеть `vektor-subnet-a` 10.20.0.0/24 в `ru-central1-a` (команды из п. 2 раздела 4). Ожидаем: `yc vpc subnet list` показывает одну подсеть в статусе `ACTIVE`.
4. **UBU01.** Создайте SG `vektor-sg-app` и правила: ingress 22 с вашего внешнего IP (`curl -s ifconfig.me` + `/32`), ingress 80 с `0.0.0.0/0`. Ожидаем: `yc vpc security-group get` показывает два ingress-правила.
5. **UBU01.** Создайте NAT-шлюз и таблицу маршрутов, привяжите её к подсети. Ожидаем: `yc vpc subnet get --name vektor-subnet-a` содержит непустой `route_table_id`.
6. **UBU01.** Создайте ВМ `vektor-app01` с cloud-init (nginx + index.html), `--ssh-key`. Ожидаем: через 30–60 секунд `yc compute instance list` показывает `RUNNING`, а `curl http://<публичный-IP>/` возвращает `vektor cloud app01 ok`.
7. **UBU01.** Зайдите по SSH (`ssh ubuntu@<IP>`), проверьте `systemctl status nginx`, выйдите. Ожидаем: сервис `active (running)`.
8. **UBU01.** Создайте сервисный аккаунт `sa-vektor-tf`, выдайте роли `vpc.admin`, `compute.admin`, `storage.admin`, `k8s.admin`, `mdb.admin`, выпустите авторизованный ключ и профиль `tf`. Ожидаем: `yc config list` в профиле `tf` без `user-account`, но с `service-account-key`.
9. **UBU01.** Выпустите статический ключ, создайте бакет `vektor-artifacts-2026-<суффикс>`, положите туда `cloud-init.yaml`, выведите список. Ожидаем: `aws ... s3 ls` показывает объект `builds/cloud-init.yaml` с размером.
10. **UBU01.** Создайте кластер `k8s-vektor` и группу нод `ng-vektor` (1 прерываемая нода, 2 vCPU / 4 ГБ), получите kubeconfig, выполните `kubectl get nodes`. Ожидаем: одна нода в статусе `Ready`. Затем разверните тестовое приложение: `kubectl create deployment web --image=nginx` и `kubectl get pods` → `Running`.
11. **UBU01.** Создайте `pg-vektor` (один хост, `s2.micro`, 10 ГБ), получите FQDN через `list-hosts`, подключитесь `psql` с `sslmode=require`, выполните `CREATE TABLE t(id serial primary key);` и `\dt`. Ожидаем: таблица создана, соединение по TLS установлено.
12. **UBU01.** Уберите за собой: удалите приложение, кластер с нодами, БД, ВМ, бакет и сеть (команды в «Откате»). Ожидаем: `yc compute instance list`, `yc managed-kubernetes cluster list`, `yc managed-postgresql cluster list` — пустые списки.
13. **UBU01.** Запишите в `stand-log.md` всё созданное (см. следующий подраздел).

### Проверка

- [ ] `yc config list` показывает нужные `cloud-id` и `folder-id`, зона `ru-central1-a`.
- [ ] `yc vpc subnet list` — одна подсеть 10.20.0.0/24, `route_table_id` заполнен.
- [ ] `yc vpc security-group get vektor-sg-app` — ровно два ingress-правила, SSH не открыт в `0.0.0.0/0`.
- [ ] `curl http://<IP>/` с UBU01 возвращает страницу, `ssh ubuntu@<IP>` работает.
- [ ] `aws --endpoint-url=$EP s3 ls s3://<bucket>/builds/` показывает файл.
- [ ] `kubectl get nodes` — нода `Ready`, `kubectl get pods` — под `Running`.
- [ ] `psql ... sslmode=require` подключается, `\dt` показывает созданную таблицу.
- [ ] Бюджет с уведомлением создан, после уборки в биллинге нет активных ресурсов.
- [ ] В `sa-key.json` и в истории bash нет секретов, добавленных в Git.

### Сломай и почини

> **Пользователь:** sidorov (склад)
> **ВМ:** — (работает через браузер)
> **Что написал:** «Выгрузка накладных висит и потом отдаёт 502, склад не может закрыть смену. Уже минут сорок.»
> **Срочность:** высокая

**Как воспроизвести поломку (на UBU01):**

```bash
# 1) новый под должен тянуть образ из реестра, поэтому перезапускаем деплой
kubectl rollout restart deployment/web
# 2) отбираем у подсети выход в интернет
yc vpc route-table delete --name vektor-rt-nat
```

Через 1–2 минуты новые поды не смогут скачать образ, потому что у нод нет публичных IP и весь egress шёл через NAT-шлюз.

**Диагностика по порядку:**

1. `kubectl get pods` → под в `ImagePullBackOff` или `ContainerCreating` дольше пары минут. Значит, проблема не в приложении, а в инфраструктуре ноды.
2. `kubectl describe pod <pod>` → в `Events` строки вида `Failed to pull image ... i/o timeout` / `dial tcp ...: connect: connection timed out`. Это уже точно сеть, а не права реестра (иначе был бы `unauthorized`).
3. Проверяем сеть облака: `yc vpc subnet get --name vektor-subnet-a` → поле `route_table_id` пустое. Причина найдена.
4. `yc vpc route-table list` → пусто; `yc vpc gateway list` → шлюз `vektor-nat-gw` на месте (или тоже удалён — тогда пересоздаём).
5. Дополнительно убеждаемся, что дело не в SG: `yc vpc security-group get vektor-sg-app` — egress по умолчанию разрешён, правила не менялись.

**Решение:**

```bash
GW=$(yc vpc gateway get --name vektor-nat-gw --format json | jq -r .id)
yc vpc route-table create --name vektor-rt-nat --network-name vektor-net \
  --route destination=0.0.0.0/0,gateway-id=$GW
yc vpc subnet update --name vektor-subnet-a --route-table-name vektor-rt-nat
kubectl rollout restart deployment/web
kubectl get pods -w
```

**Ответ пользователю:** «Причина — сбой маршрута выхода в интернет у облачных серверов, из-за него перестали запускаться новые экземпляры приложения. Маршрут восстановлен, выгрузка работает, проверьте, пожалуйста, и напишите, если накладные всё ещё не формируются».

**Закрытие заявки:** категория «Облачная инфраструктура / сеть», в решении — «удалена таблица маршрутов подсети, восстановлена», в постмортеме — пункт «запретить ручное удаление route table в prod-каталоге, перенести сеть в Terraform с `prevent_destroy`».

### Что изменилось на стенде

- В Yandex Cloud появился каталог `vektor-dev` с сетью `vektor-net`, подсетью `vektor-subnet-a` 10.20.0.0/24 (`ru-central1-a`), SG `vektor-sg-app`, NAT-шлюзом `vektor-nat-gw` и таблицей маршрутов `vektor-rt-nat` (если не удаляли).
- Сервисный аккаунт `sa-vektor-tf` с ролями `vpc.admin`, `compute.admin`, `storage.admin`, `k8s.admin`, `mdb.admin`; файл `sa-key.json` на UBU01.
- Профиль CLI `tf` в `~/.config/yandex-cloud/config.yaml` на UBU01; бэкап `~/yc-config-backup-069`.
- Ресурсы лабораторной: ВМ `vektor-app01`, кластер `k8s-vektor` + нода `ng-vektor`, БД `pg-vektor`, бакет `vektor-artifacts-2026-<суффикс>` — все удалены на шаге 12 либо помечены как «оставлены до урока 70».
- На UBU01 установлены `jq`, `awscli`, `kubectl`, создан `cloud-init.yaml`.

### Откат

1. Локальная часть: VMware Workstation → VM → Snapshot Manager → выбрать `before-devops-069` → Revert. Либо `cp -r ~/yc-config-backup-069 ~/.config/yandex-cloud`.
2. Облачная часть — вручную, в обратном порядке:

```bash
aws --endpoint-url=https://storage.yandexcloud.net s3 rm s3://<bucket> --recursive
aws --endpoint-url=https://storage.yandexcloud.net s3 rb s3://<bucket>

yc managed-kubernetes node-group delete --name ng-vektor
yc managed-kubernetes cluster delete --name k8s-vektor
yc managed-postgresql cluster delete --name pg-vektor
yc compute instance delete --name vektor-app01

yc vpc subnet update --name vektor-subnet-a --route-table-name ""
yc vpc route-table delete --name vektor-rt-nat
yc vpc gateway delete --name vektor-nat-gw
yc vpc security-group delete --name vektor-sg-app
yc vpc subnet delete --name vektor-subnet-a
yc vpc network delete --name vektor-net
yc iam service-account delete --name sa-vektor-tf
```

3. Проверьте биллинг: в разделе «Мои ресурсы» не должно остаться активных ВМ, кластеров и дисков (диски удаляются вместе с ВМ, если не отключать автоудаление).

## 6. Ошибки новичков и безопасность

1. **Оставлять публичный IP всем ВМ и нодам «чтобы работало»** → любая уязвимость в SSH/nginx сразу доступна из интернета, плюс вы платите за каждый адрес. Правильно: приватная подсеть + NAT-шлюз, публичный IP только у бастиона или балансировщика.
2. **Открывать SG на `0.0.0.0/0` для 22, 5432, 6443** → боты сканируют облачные диапазоны круглосуточно. Правильно: SSH — с конкретного IP или через bastion/VPN; БД — только из SG кластера; API Kubernetes — только через `--external` с ограничением или приватный эндпоинт.
3. **Выдавать сервисному аккаунту `editor` на каталог** → утёкший ключ уносит всю инфраструктуру. Правильно: по одной узкой роли на сервис, отдельные SA для Terraform, CI и приложения.
4. **Коммитить `sa-key.json` или статический ключ в Git** → ключ живёт в истории навсегда. Правильно: SOPS/age, Lockbox, External Secrets Operator (урок 66).
5. **Путать группы безопасности с сетевыми ACL подсети** → правило добавили «на подсеть», а трафика нет. Правильно: SG — на интерфейс; правила проверяем `yc vpc security-group get`.
6. **Считать Managed DB с одним хостом отказоустойчивой** → при обслуживании хоста база недоступна. Правильно: минимум два хоста в разных зонах + понимание, что это отдельные деньги.
7. **Забывать удалить ресурсы после лабораторной** → мастер Kubernetes и хост Managed DB тарифицируются по часам; к концу месяца сюрприз. Правильно: бюджет с уведомлением + чек-лист уборки в `stand-log.md`.
8. **Думать, что снапшот VMware откатит облако** → снапшот защищает только локальные ВМ. Правильно: облачная часть — IaC в Git + явные `delete`-команды.

**Безопасность (что нельзя в боевой среде).** Нельзя использовать пароль `Lab#2026-vektor` и любые стендовые пароли в облаке — секреты в облаке хранят в Lockbox/KMS и подставляют в приложение, а не в манифест. Нельзя выкладывать персональные данные сотрудников в бакет с публичным доступом: даже «тестовый» бакет с реальными ФИО — это инцидент 152-ФЗ. Публичный доступ к бакету (`allUsers`) включается осознанно и на конкретный префикс, а не на весь бакет. Ключи сервисных аккаунтов — с ротацией (раз в 90 дней как минимум), статические ключи доступа — только там, где без них нельзя (S3 API, Container Registry).

## 7. Вопросы на собеседовании

**1. «Чем управляемая база в облаке отличается от PostgreSQL на вашей ВМ — и что вы теряете?»**
Ответ: в Managed DB провайдер отвечает за железо, ОС, установку и патчи СУБД, репликацию, резервные копии и восстановление, мониторинг базовых метрик; вы отвечаете за схему, запросы, индексы, пользователей и права, а также за размер и класс хоста. Теряете: root-доступ к ОС и `postgresql.conf` (частично настраивается через параметры кластера), возможность ставить произвольные расширения (`shared_preload_libraries` — только из списка поддерживаемых), свободу версий (доступны не все мажорные версии), и вы привязаны к провайдеру — миграция стоит времени. Плюсы: HA-переключение за минуты, point-in-time recovery, отсутствие «мы три года не обновляли СУБД».
*Что хочет услышать интервьюер:* чёткое разделение ответственности и понимание цены управляемости.
*Красный флаг:* «managed — это просто база на их сервере, разницы нет».

**2. «Как дать поду в Kubernetes доступ к Object Storage, не вшивая ключи в образ?»**
Ответ: создать сервисный аккаунт с ролью `storage.editor` (или уже), привязать его к поду — через механизм федерации/привязки сервисного аккаунта к service account Kubernetes (в Yandex Cloud это Workload Identity Federation `[ПРОВЕРИТЬ актуальное название и статус фичи]`) и получать временные ключи; альтернатива, работающая всегда — External Secrets Operator, который читает секрет из Lockbox и создаёт `Secret` в кластере; для S3-совместимого клиента из временных ключей собирается сессия. Ключи не попадают ни в образ, ни в Git, ротация — автоматическая.
*Что хочет услышать интервьюер:* вы знаете, что «секрет в ConfigMap» — не решение, и умеете связать IAM облака с IAM Kubernetes.
*Красный флаг:* «положу ключи в Secret — он же зашифрован». Secret в Kubernetes по умолчанию просто base64.

**3. «ВМ в приватной подсети не выходит в интернет. Что проверяете?»**
Ответ: по порядку — есть ли публичный IP у интерфейса (`yc compute instance get`); есть ли NAT-шлюз и таблица маршрутов, и привязана ли она к подсети (`yc vpc subnet get` → `route_table_id`); есть ли маршрут `0.0.0.0/0` на шлюз в таблице; не режет ли egress-правило в SG (по умолчанию egress разрешён, но его могли закрыть); резолвится ли DNS (в Yandex Cloud внутри сети работает резолвер `169.254.169.254` для метаданных, а DNS-сервер задаётся в подсети `[ПРОВЕРИТЬ]`); и последнее — не блокирует ли что-то на стороне получателя.
*Что хочет услышать интервьюер:* системный порядок «интерфейс → маршрут → фильтр → DNS», а не «перезагрузил ВМ».
*Красный флаг:* «наверное, файрвол» без попытки локализовать слой.

**4. «Какие роли вы дадите сервисному аккаунту Terraform и почему не `editor`?»**
Ответ: перечисление конкретных admin/editor-ролей по нужным сервисам — `vpc.admin`, `compute.admin`, `k8s.admin`, `storage.admin`, `mdb.admin` — или ещё уже, если модуль управляет одним сервисом. Принцип минимальных привилегий: `editor` покрывает всё в каталоге, включая сервисы, к которым Terraform не должен прикасаться; при компрометации ключа ущерб ограничен списком ролей. Дополнительно: отдельные SA на разные стейты/окружения, ключ в Vault, ротация, отсутствие ролей на уровне облака.
*Что хочет услышать интервьюер:* понимание least privilege и того, что SA — такая же учётка, которую надо ограничивать.
*Красный флаг:* «дам `editor`, чтобы точно работало».

**5. «Чем VM в Compute Cloud отличается от ноды Managed Kubernetes с точки зрения эксплуатации?»**
Ответ: и то и другое — виртуальная машина в вашем VPC, но в Compute вы владеете ОС целиком: ставите пакеты, обновляете ядро, чините sshd. В Managed Kubernetes нода тоже ваша (вы выбираете размер, версию, тип диска, прерываемость, SG), но её жизненным циклом управляет провайдер: образ обновляется при апгрейде версии кластера, сломанную ноду система пересоздаёт, а приложения описываются манифестами и переезжают на другую ноду сами. То есть в Compute единица эксплуатации — сервер, в Managed Kubernetes — кластер и манифесты; мастер-плоскость вообще не ваша зона ответственности.
*Что хочет услышать интервьюер:* вы не «поднимаете kubectl на ВМ и называете это managed» и понимаете, где заканчивается ваша ответственность.
*Красный флаг:* «managed k8s — это когда я сам ставлю kubeadm, просто в облаке».

## 8. Источники (официальная документация)

- **yandex.cloud** — документация Yandex Cloud. Разделы: «Compute Cloud», «Virtual Private Cloud», «Identity and Access Management», «Object Storage», «Managed Service for Kubernetes», «Managed Service for PostgreSQL», «Yandex Cloud CLI». Поиск: `Yandex Cloud CLI reference compute instance create`.
- **yandex.cloud** — раздел «Цены» и калькулятор стоимости (проверять перед каждой лабораторной, тарифы меняются). Поиск: `Yandex Cloud pricing calculator`.
- **yandex.cloud** — документ о соответствии требованиям 152-ФЗ и размещении данных в РФ `[ПРОВЕРИТЬ точное название раздела]`.
- **registry.terraform.io** — провайдер `yandex-cloud/yandex`, документация ресурсов `yandex_vpc_network`, `yandex_compute_instance`, `yandex_kubernetes_cluster`, `yandex_storage_bucket`, `yandex_mdb_postgresql_cluster`. Поиск: `terraform yandex-cloud provider resources`.
- **kubernetes.io** — разделы «Concepts» и «kubectl Cheat Sheet». Поиск: `kubernetes documentation kubectl cheat sheet`.
- **docs.aws.amazon.com** — Amazon S3 API Reference (общий для S3-совместимых хранилищ). Поиск: `Amazon S3 API Reference list of operations`.
- **cloud.yandex.ru** `[ПРОВЕРИТЬ актуальный домен для входа в консоль]` — веб-консоль управления облаком.
