# Менеджеры артефактов: Nexus, Artifactory, GitLab Package Registry — Maven, npm, PyPI, Docker, Helm

## 1. Введение (зачем это нужно в реальной работе)

**Заявка #412. От:** команда разработки «Вектор» (на стенде её роль играешь ты сам — `adm.student`, отдельных учёток разработчиков в стенде нет).
**Что написали:** «Мы третий раз собираем один и тот же jar на разных машинах и получаем разные файлы. `npm install` периодически падает — registry.npmjs.org то недоступен, то отдаёт 502. И главное: если завтра из Maven Central удалят версию библиотеки, мы не сможем пересобрать релиз полугодовой давности. Нужен внутренний склад пакетов».
**Срочность:** средняя, но блокирует релиз.

Это типовая задача DevOps-инженера. Пока артефакты лежат «где-то на билд-агентах» и тянутся напрямую из интернета, у вас нет ни воспроизводимости (нельзя собрать вчерашний релиз сегодня), ни скорости (каждый раннер качает гигабайты заново), ни устойчивости (упал upstream — встал CI), ни контроля (кто и что опубликовал, можно ли это удалить, есть ли контрольная сумма).

После этой главы ты будешь уметь: поднять Nexus, создать в нём hosted/proxy/group-репозитории под пять форматов, публиковать туда Maven, npm, PyPI, Docker-образы и Helm-чарты, делать то же самое через GitLab Package Registry без установки чего-либо, и объяснить на собеседовании, чем Nexus отличается от Artifactory и Harbor.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

**Артефакт** — это результат сборки: `.jar`, `.tgz`, `.whl`, Docker-образ, Helm-чарт, `tar.gz` с бинарником. Исходники живут в Git, артефакты — в менеджере артефактов (artifact repository manager). Путать их — классическая ошибка новичка: Git не предназначен для хранения бинарников, а артефакт-репозиторий не заменяет Git.

**Три роли репозитория** (термины немного различаются у вендоров, но смысл один):

| Роль | Nexus | Artifactory | Что делает |
|------|-------|-------------|-----------|
| Proxy / remote | `proxy` | `remote` | Кэширует пакеты из внешнего мира (Maven Central, npmjs, PyPI, Docker Hub). Первый запрос идёт в интернет, дальше отдаётся из кэша |
| Hosted / local | `hosted` | `local` | Ваш собственный «склад»: сюда публикуете свои сборки |
| Group / virtual | `group` | `virtual` | Виртуальный репозиторий-агрегатор: клиент обращается к одному URL, менеджер сам решает, в hosted или proxy искать |

Схема потока:

```text
 dev / git push
      |
      v
 [ CI на GIT01 ]  --publish-->  +---------------------------+
      |                         |   МЕНЕДЖЕР АРТЕФАКТОВ     |
      |                         |                           |
      |                         |  hosted  (наш код)        |
      |                         |  proxy   (Maven Central,  |<--- internet
      |                         |           npmjs, PyPI,    |
      |                         |           Docker Hub)     |
      |                         |  group   = hosted + proxy |
      |                         +---------------------------+
      |                                     ^
      v                                     | pull
 [ k8s / стенд ]  -------------------------+
```

**Как устроен каждый формат** (это спрашивают на собеседовании):

- **Maven** — иерархия каталогов `/group/artifact/version/artifact-version.jar` + `.pom` + `maven-metadata.xml` (в нём список версий). Клиент — `mvn`, авторизация — Basic Auth из `~/.m2/settings.xml`. Разделение `releases` (неизменяемые версии) и `snapshots` (перезаписываемые) — обязательное.
- **npm** — HTTP-реестр по спецификации npm Registry API: `GET /<package>` отдаёт JSON с метаданными, `GET /<package>/-/<package>-<version>.tgz` — сам tarball. Публикация — `PUT`. Авторизация: либо Basic (`_auth` = base64 от `user:pass`), либо Bearer-токен.
- **PyPI** — «simple index» по PEP 503: `GET /simple/<name>/` отдаёт HTML-страницу со ссылками на файлы. Загрузка — POST multipart (её делает `twine`).
- **Docker** — Registry HTTP API v2: `GET /v2/` (проверка), `GET /v2/<name>/manifests/<tag>`, слои по digest `sha256:...`. Образ — это манифест + набор слоёв, каждый слой адресуется по хешу содержимого (content-addressable storage). Отсюда дедупликация: два образа с общим базовым слоем занимают место один раз.
- **Helm** — «классический» вариант: HTTP-репозиторий с `index.yaml` и `.tgz`-чартами. Современный вариант: OCI-артефакт в Docker-реестре (`oci://host/project/chart:version`) — так работают Harbor и GitLab.

**Порты и адреса на стенде:** Nexus — `8081` (UI + Maven/npm/PyPI/Helm) и отдельный порт на каждый Docker-репозиторий (у нас `8082`). Harbor — `443`/`80`, registry API по пути `/v2/`. GitLab — `443` (Package Registry живёт по путям `/api/v4/projects/<id>/packages/...`), встроенный Container Registry — отдельный порт `5050`.

**Важные свойства «взрослого» менеджера артефактов:** immutable-релизы (нельзя перезаписать версию), retention/cleanup-политики (что и когда удалять), контрольные суммы, RBAC (кто может писать в `prod`-репозиторий), аудит, дедупликация блобов, HA и бэкапы. Nexus Community Edition и GitLab Package Registry — бесплатны; Artifactory — коммерческий продукт.

## 3. Аналогия из жизни

Менеджер артефактов — это склад при заводе. `hosted` — стеллажи с вашей готовой продукцией. `proxy` — приёмная, куда курьер из внешнего мира привозит чужие запчасти, и они остаются лежать на складе, даже если поставщик закрылся. `group` — окошко выдачи, где кладовщик сам смотрит, что у вас своё, а что заказать у поставщика: сборщику не надо бегать в пять разных мест. А retention-политика — это нормы списания: без неё склад забивается просроченным товаром и однажды вы просто не сможете завезти новую партию. **Аналогия перестаёт работать в одном месте:** на реальном складе коробку можно открыть и подменить содержимое, а хороший менеджер артефактов хранит неизменяемые версии и сверяет контрольные суммы — подменить нельзя.

## 4. Практика и команды

**1. Запуск Nexus контейнером (на стенде — на GIT01)**

```bash
sudo mkdir -p /srv/nexus-data
sudo chown -R 200:200 /srv/nexus-data     # в образе Nexus работает UID 200
sudo docker run -d --name nexus \
  --restart unless-stopped \
  -p 8081:8081 -p 8082:8082 \
  -v /srv/nexus-data:/nexus-data \
  sonatype/nexus3
```

`-p 8081` — UI и все «HTTP-форматы», `-p 8082` — Docker-коннектор, `-v` — данные переживают пересоздание контейнера. Актуальный тег смотри на hub.docker.com `[ПРОВЕРИТЬ]`. Первый старт — 1–2 минуты.

**2. Первичный пароль администратора**

```bash
sudo docker exec nexus cat /nexus-data/admin.password
```

Начиная с Nexus 3.17 пароль генерируется случайно и лежит в этом файле; после первого входа Nexus попросит его сменить.

**3. Проверка через REST API**

```bash
curl -s -u admin:'Пароль' \
  "http://10.10.10.50:8081/service/rest/v1/search?repository=npm-hosted&name=vektor-web" | jq .
```

Параметры `repository`, `name`, `version`, `group` — фильтры поиска. В выводе `items[].assets[].downloadUrl` — прямые ссылки на файлы. Это удобно для скриптов и для проверки «а точно ли залилось».

**4. Maven: `~/.m2/settings.xml` (на UBU01)**

```xml
<settings>
  <mirrors>
    <mirror>
      <id>nexus-public</id>
      <mirrorOf>*</mirrorOf>
      <url>http://10.10.10.50:8081/repository/maven-group/</url>
    </mirror>
  </mirrors>
  <servers>
    <server>
      <id>nexus-releases</id>
      <username>admin</username>
      <password>Пароль</password>
    </server>
  </servers>
</settings>
```

`<mirrorOf>*</mirrorOf>` — «всё качать через Nexus» (включая Maven Central). `<id>` сервера обязан совпадать с `<id>` репозитория в `pom.xml`, иначе Maven не подставит пароль.

**5. Maven: публикация**

```xml
<!-- pom.xml -->
<distributionManagement>
  <repository>
    <id>nexus-releases</id>
    <url>http://10.10.10.50:8081/repository/maven-releases/</url>
  </repository>
</distributionManagement>
```

```bash
mvn -B clean deploy -DskipTests
```

Типичный вывод: `Uploaded to nexus-releases: http://10.10.10.50:8081/repository/maven-releases/... (12 kB at 45 kB/s)`. Если видишь `401 Unauthorized` — не совпал `<id>`; `400 Bad Request` — версия оканчивается на `-SNAPSHOT`, а репозиторий объявлен как release.

**6. npm: конфиг и публикация**

```bash
NEXUS=http://10.10.10.50:8081
AUTH=$(printf 'admin:%s' 'Пароль' | base64 -w0)
cat > ~/.npmrc <<EOF
registry=$NEXUS/repository/npm-hosted/
$NEXUS/repository/npm-hosted/:_auth=$AUTH
email=adm.student@corp.vektor.lab
EOF
npm publish
```

`_auth` — это **base64, а не шифрование**: любой, кто прочитает файл, получит пароль. Строка-ключ должна точно совпадать с URL реестра публикации (вплоть до завершающего слеша), иначе npm её не найдёт.

**7. PyPI: `~/.pypirc` + twine**

```ini
[distutils]
index-servers = nexus

[nexus]
repository = http://10.10.10.50:8081/repository/pypi-hosted/
username = admin
password = Пароль
```

```bash
chmod 600 ~/.pypirc
python -m build                       # создаёт dist/*.whl и dist/*.tar.gz
twine upload -r nexus dist/*
```

Проверка установки: `pip install --index-url http://10.10.10.50:8081/repository/pypi-group/simple/ --trusted-host 10.10.10.50 vektor-report`. Флаг `--trusted-host` нужен только потому, что у нас HTTP без TLS; в проде так делать нельзя.

**8. Docker: разрешаем «небезопасный» реестр (только для лабы)**

```json
{
  "insecure-registries": ["10.10.10.50:8082", "10.10.10.55"]
}
```

Файл `/etc/docker/daemon.json`, затем `sudo systemctl restart docker`. **В боевой среде так нельзя** — только TLS с корпоративным CA; `insecure-registries` означает, что трафик можно подменить.

**9. Docker: login / tag / push в Nexus**

```bash
docker login 10.10.10.50:8082 -u admin -p 'Пароль'
docker tag nginx:alpine 10.10.10.50:8082/vektor-web:1.0.0
docker push 10.10.10.50:8082/vektor-web:1.0.0
```

Ключевое отличие от Harbor: **в Nexus имя репозитория не входит в путь образа**, за него отвечает порт коннектора. У Harbor наоборот: `10.10.10.55/vektor/web:1.0.0`, где `vektor` — проект.

**10. Helm: публикация чарта в Nexus**

```bash
helm create vektor-web
helm package vektor-web -d dist/
helm plugin install https://github.com/chartmuseum/helm-push
helm repo add nexus --username admin --password 'Пароль' \
  http://10.10.10.50:8081/repository/helm-hosted/
helm cm-push dist/vektor-web-0.1.0.tgz nexus
helm search repo nexus
```

Nexus не понимает `helm push` (это OCI-команда), для него нужен плагин `cm-push`.

**11. Helm OCI: публикация в Harbor**

```bash
helm registry login 10.10.10.55 -u admin -p 'Пароль'
helm push dist/vektor-web-0.1.0.tgz oci://10.10.10.55/vektor
helm pull oci://10.10.10.55/vektor/vektor-web --version 0.1.0
```

Для HTTP-реестра нужен флаг `--plain-http` (в старых версиях Helm его нет) `[ПРОВЕРИТЬ версию]`; правильный путь — работать по HTTPS с доверенным сертификатом.

**12. GitLab Package Registry из пайплайна (без Nexus вообще)**

```yaml
publish:npm:
  stage: publish
  image: node:20
  script:
    - echo "//${CI_SERVER_HOST}/api/v4/projects/${CI_PROJECT_ID}/packages/npm/:_authToken=${CI_JOB_TOKEN}" >> .npmrc
    - echo "@vektor:registry=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/packages/npm/" >> .npmrc
    - npm publish
  rules:
    - if: $CI_COMMIT_TAG
```

`CI_JOB_TOKEN` — временный токен раннера, живёт только на время джобы. Вне пайплайна нужен Personal/Deploy Token с правом `write_package_registry`.

**13. GitLab: PyPI и generic-пакеты**

```bash
TWINE_USERNAME=gitlab-ci-token TWINE_PASSWORD=${CI_JOB_TOKEN} \
  twine upload --repository-url ${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/packages/pypi dist/*

curl --header "JOB-TOKEN: ${CI_JOB_TOKEN}" \
     --upload-file dist/app.tar.gz \
     "${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/packages/generic/vektor-app/1.0.0/app.tar.gz"
```

Generic-эндпоинт — универсальный «склад файлов» для того, что не подходит ни под один формат.

**14. Artifactory — справочно (на стенде не разворачиваем: платный)**

```bash
export ARTIFACTORY_HOST='<ваш-хост>'      # подставь свой
npm config set registry https://${ARTIFACTORY_HOST}/artifactory/api/npm/npm-virtual/
docker login ${ARTIFACTORY_HOST}
docker tag nginx:alpine ${ARTIFACTORY_HOST}/docker-local/vektor-web:1.0.0
curl -u admin:'Пароль' "https://${ARTIFACTORY_HOST}/artifactory/api/search/artifact?name=*.jar&repos=maven-local"
```

Ключевые отличия: в URL всегда есть ключ репозитория `/artifactory/<repo-key>/`, «виртуальный» репозиторий собирает local+remote, а UI в 7.x на порту `8082`, сервис — `8081` `[ПРОВЕРИТЬ для вашей инсталляции]`. Лицензия — платная; есть триал/облачный free-tier с лимитами `[ПРОВЕРИТЬ актуальные условия]`.

## 5. Лабораторная работа

### Предусловия

- **GIT01 (10.10.10.50)** — GitLab CE + runner, установлен Docker (нужен для docker-executor из урока 49). Здесь поднимаем Nexus. Проверь свободную память: `free -h`. Если меньше 8 ГБ — ставь Nexus на **UBU01 (10.10.10.30)** и везде ниже замени `10.10.10.50` на `10.10.10.30` (запиши это в журнал стенда).
- **HARBOR01 (10.10.10.55)** — Harbor с созданным проектом `vektor`, `docker login` работает.
- **UBU01 (10.10.10.30)** — «клиентская» машина: `openjdk-17-jdk`, `maven`, `nodejs`+`npm`, `python3-venv`, `helm`, `docker`.
- **k8s-узлы** в этой лабе не трогаем — они понадобятся в уроке 55.
- Отдельной ВМ под Nexus в таблице стенда нет — это осознанный компромисс учебного стенда. В реальной работе под Nexus/Artifactory выделяют отдельный хост с диском под блобы.

### Снапшот

В VMware Workstation на **GIT01**, **UBU01**, **HARBOR01** (при остановленных ВМ): `VM → Snapshot → Take Snapshot`, имя — `before-devops-052`. Если снимаешь при включённой ВМ, выбери «Snapshot the virtual machine's memory» только при необходимости — иначе снапшот будет тяжёлым.

### Шаги

1. **GIT01.** Проверь, что порты свободны: `ss -lntp | grep -E '8081|8082'`. Ожидаемо — пусто.
2. **GIT01.** `sudo mkdir -p /srv/nexus-data && sudo chown -R 200:200 /srv/nexus-data`. Ожидаемо — каталог принадлежит `200:200`.
3. **GIT01.** Запусти контейнер командой из п.4.1. `docker ps` → контейнер `nexus` в статусе `Up`.
4. **GIT01.** `sudo docker exec nexus cat /nexus-data/admin.password` → строка пароля. Открой в браузере `http://10.10.10.50:8081`, войди как `admin`, смени пароль. Ожидаемо — визард настройки и главный экран Nexus.
5. **GIT01 (UI Nexus).** Создай репозитории: `maven-releases` (maven2, hosted, Release policy), `maven-proxy` (maven2, proxy → `https://repo.maven.apache.org/maven2/`), `maven-group` (maven2, group, в него `maven-releases` + `maven-proxy`), `npm-hosted`, `npm-proxy` (→ `https://registry.npmjs.org`), `npm-group`, `pypi-hosted`, `helm-hosted`, `docker-hosted` (HTTP, connector port `8082`). Ожидаемо — 9 репозиториев в списке. Сразу зайди в **Administration → Security → Realms** и активируй **npm Bearer Token Realm**.
6. **UBU01.** Установи клиентов: `sudo apt update && sudo apt install -y maven nodejs npm python3-venv docker.io`. Для Python-сборки: `python3 -m venv ~/pyenv && ~/pyenv/bin/pip install build twine`.
7. **UBU01.** Настрой `~/.m2/settings.xml` и `pom.xml` (п.4.4–4.5), выполни `mvn -B clean deploy -DskipTests`. Ожидаемо — финальная строка `BUILD SUCCESS` и `Uploaded to nexus-releases: ...`.
8. **UBU01.** Настрой `~/.npmrc` (п.4.6), создай пакет `npm init -y`, поправь `name` на `vektor-web`, `version` на `1.0.0`, выполни `npm publish`. Ожидаемо — `+ vektor-web@1.0.0`.
9. **UBU01.** В каталоге Python-проекта сделай `vektor_report` с `pyproject.toml`, собери и залей: `python -m build && twine upload -r nexus dist/*`. Ожидаемо — `Uploading distributions to ...` без ошибок.
10. **UBU01.** Настрой `/etc/docker/daemon.json` с `insecure-registries`, перезапусти docker, залогинься, сделай `tag` и `push` (п.4.9). Ожидаемо: `1.0.0: digest: sha256:... size: ...`.
11. **UBU01.** Собери чарт, поставь плагин, залей в Nexus и в Harbor (п.4.10–4.11). `helm search repo nexus` → строка `nexus/vektor-web  0.1.0`.
12. **GIT01.** В тестовом проекте GitLab закоммить `.gitlab-ci.yml` с джобой `publish:npm` (п.4.12) и тегом `v1.0.0`. Ожидаемо — зелёный пайплайн, пакет появляется в **Deploy → Package Registry** проекта.
13. **Проверка «от обратного»:** на UBU01 удали локальный кэш `rm -rf ~/.m2/repository/ru/vektor` и `rm -rf ~/.npm`, повтори сборку — всё должно скачаться из Nexus. Это и есть доказательство, что склад работает.
14. **GIT01.** Запиши в `stand-log.md`: Nexus запущен контейнером на GIT01, порт 8081/8082, том `/srv/nexus-data`, список репозиториев, что пароль админа изменён (сам пароль — не в файл!).

### Проверка

- [ ] `docker ps` на GIT01 показывает контейнер `nexus` в статусе `Up`
- [ ] `http://10.10.10.50:8081` открывается, вход под `admin` с новым паролем работает
- [ ] В Nexus есть `maven-releases`, `npm-hosted`, `pypi-hosted`, `helm-hosted`, `docker-hosted`, `*-group`
- [ ] `mvn deploy` завершился `BUILD SUCCESS`, артефакт виден в UI
- [ ] `npm publish` вернул `+ vektor-web@1.0.0`
- [ ] `twine upload` прошёл без ошибок, пакет качается через `pip download`
- [ ] `docker pull 10.10.10.50:8082/vektor-web:1.0.0` работает после `docker rmi` локальной копии
- [ ] `helm search repo nexus` находит `vektor-web`
- [ ] В GitLab в Package Registry проекта лежит npm-пакет, собранный из пайплайна
- [ ] Повторная сборка с очищенными локальными кэшами проходит без интернета к upstream (проверь, отключив NAT-адаптер на минуту)

### Сломай и почини

> **Пользователь:** тимлид разработки (роль играешь ты сам) / **ВМ:** UBU01
> **Что написал:** «Публикую пакет — `npm publish` падает: `npm ERR! code E401 npm ERR! Unable to authenticate, need: Basic realm="Sonatype Nexus Repository Manager"`. Вчера всё работало, я ничего не трогал».
> **Срочность:** высокая, блокирует релиз.

**Как воспроизвести поломку.** На GIT01 в UI Nexus: **Administration → Security → Realms**, убери `npm Bearer Token Realm` из активных и сохрани. Либо (второй вариант) смени пароль `admin` в Nexus, оставив в `~/.npmrc` старый `_auth`.

**Диагностика по порядку:**

1. Прочитай текст ошибки: `401` — это «кто ты», `403` — «тебе нельзя». `E401` = проблема аутентификации, права ни при чём.
2. Проверь, что Nexus жив: `curl -s -o /dev/null -w '%{http_code}\n' http://10.10.10.50:8081/service/rest/v1/status` → ждём `200`.
3. Проверь, что сам пароль валиден: `curl -u admin:'Пароль' http://10.10.10.50:8081/service/rest/v1/status` → `200`. Если `401` — пароль неверный, и `.npmrc` тут ни при чём.
4. Посмотри `~/.npmrc`: совпадает ли ключ `//10.10.10.50:8081/repository/npm-hosted/:_auth=...` с URL публикации **посимвольно** (частый баг — забытый `/` в конце).
5. Раскодируй `_auth`: `echo '<значение>' | base64 -d` — увидишь `admin:Пароль`. Если пароль другой — вот и причина.
6. Проверь realms в Nexus (шаг 5 лабораторной). Именно отключённый `npm Bearer Token Realm` даёт 401 при валидном пароле — это самая неочевидная причина, и её надо знать.

**Решение:** пересоздать `_auth` и/или включить realm обратно:

```bash
AUTH=$(printf 'admin:%s' 'НовыйПароль' | base64 -w0)
sed -i "s|^.*npm-hosted/:_auth=.*|http://10.10.10.50:8081/repository/npm-hosted/:_auth=$AUTH|" ~/.npmrc
npm publish
```

**Ответ пользователю:** «Причина найдена: на Nexus был отключён realm для npm-токенов (следствие изменения настроек безопасности), из-за этого реестр отвечал 401 даже на верный пароль. Realm включён, проверьте публикацию — у меня `vektor-web@1.0.0` залился. На будущее: для CI мы выдадим deploy-токен только на `npm-hosted`, а не пароль администратора».
**Закрытие заявки:** тип «конфигурация», причина в описании, в базу знаний — заметка «E401 при валидном пароле → проверить Realms».

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** запущен контейнер `nexus` (образ `sonatype/nexus3`), порты `8081`, `8082`, том `/srv/nexus-data`. Пароль `admin` изменён (хранится вне Git).
- **Nexus:** репозитории `maven-releases`, `maven-proxy`, `maven-group`, `npm-hosted`, `npm-proxy`, `npm-group`, `pypi-hosted`, `helm-hosted`, `docker-hosted` (connector `8082`). Активирован realm `npm Bearer Token Realm`.
- **UBU01 (10.10.10.30):** установлены `maven`, `nodejs`/`npm`, `docker.io`, venv `~/pyenv` с `build` и `twine`; настроены `~/.m2/settings.xml`, `~/.npmrc`, `~/.pypirc`; в `/etc/docker/daemon.json` добавлены `insecure-registries`.
- **HARBOR01 (10.10.10.55):** в проекте `vektor` появились образ `web:1.0.0` и OCI-чарт `vektor-web:0.1.0`.
- **GitLab:** в тестовом проекте включён Package Registry, есть npm-пакет из пайплайна.
- **Ограничение стенда:** Nexus делит хост с GitLab — по памяти это «на грани». В проде — отдельная ВМ.

### Откат

1. Останови и удали контейнер: `docker stop nexus && docker rm nexus`.
2. Если нужно вернуть всё как было — выключи ВМ и в VMware Workstation: `VM → Snapshot → Snapshot Manager` → выбери `before-devops-052` → **Restore**. Данные на диске вернутся к моменту снапшота, каталог `/srv/nexus-data` исчезнет.
3. Не забудь откатить **все три** ВМ (GIT01, UBU01, HARBOR01), иначе получишь рассинхрон: клиент настроен на Nexus, которого нет.
4. Запиши факт откатa в `stand-log.md` — следующая глава должна знать реальное состояние.

## 6. Ошибки новичков и безопасность

- **Пароль в `~/.npmrc` / `settings.xml` закоммичен в Git** → утечка учётки с правом записи в репозиторий, через который собирается прод (классический вектор supply-chain-атаки) → в CI использовать `CI_JOB_TOKEN`/deploy-токены, в GitLab — masked/protected CI-переменные, локально — файлы с `chmod 600` вне репозитория.
- **Публикация релизов в snapshot-репозиторий или перезапись версии** → «одинаковый» `1.0.0` у разных людей означает разное, воспроизводимость потеряна → разделять `releases`/`snapshots`, включить immutable-политику (в Artifactory — «prohibit overwrite», в Nexus — release-политика репозитория).
- **Нет retention/cleanup** → диск забит старыми снапшотами и слоями образов, Nexus падает с `No space left on device` ровно в день релиза → заранее настроить cleanup-политики и мониторинг свободного места.
- **Анонимный доступ на запись или публикация UI в интернет** → кто угодно кладёт свой пакет с вашим именем, CI его подхватывает → закрыть анонимную запись, доступ только из офисной сети/VPN, отдельные креды на чтение и запись.
- **`insecure-registries` «на минутку» в проде** → MitM и подмена образа на лету → TLS с корпоративным CA, сертификат в `/etc/docker/certs.d/<host>/ca.crt`.
- **Dependency confusion:** внутренний пакет `vektor-utils` без scope, а в публичном npm кто-то публикует пакет с тем же именем и версией повыше → сборка тянет чужой код → использовать scoped-имена (`@vektor/...`), привязывать scope к внутреннему реестру, для Python — `--index-url` только на внутренний индекс с явным `--extra-index-url` политикой.
- **Секреты внутри артефакта** (`.env`, ключи в jar/wheel) → они навсегда остаются в репозитории вместе с историей версий → секреты только через переменные окружения/Vault, в CI — secret scanning (тема урока 53).

**Безопасность.** В лабе мы сознательно работаем по HTTP и с `insecure-registries`, храним пароль администратора Nexus в открытом виде в домашнем каталоге и даём `admin` права на все репозитории. В боевой среде это недопустимо: только TLS, отдельные сервисные учётки с минимальными правами (публикация в `hosted`, чтение из `group`), ротация токенов, аудит публикаций и обязательная проверка того, что в прод-репозиторий может писать только защищённая ветка/тег.

## 7. Вопросы на собеседовании

**1. Чем proxy-репозиторий отличается от hosted и зачем нужен group/virtual?**
**Ответ:** hosted — собственные артефакты, которые мы публикуем; proxy (Artifactory: remote) — кэш внешнего источника: первый запрос уходит в интернет, ответ сохраняется локально, дальше отдаётся из кэша. Это даёт скорость, устойчивость при падении upstream и возможность собрать релиз, даже если пакет удалили из Maven Central. Group (virtual) — агрегатор: один URL для клиента, внутри порядок опроса hosted → proxy. Клиенту не нужно знать, откуда именно взялся пакет, а конфиг сборки остаётся одинаковым для всех.
**Что хочет услышать интервьюер:** что вы понимаете кэширование и воспроизводимость, а не просто «нажимал кнопки в UI».
**Красный флаг:** «proxy — это когда репозиторий проксирует запросы» без единого слова про кэш и отказоустойчивость.

**2. Почему у Nexus у каждого Docker-репозитория свой порт, а у Harbor — путь с проектом?**
**Ответ:** Docker Registry API v2 не включает имя репозитория в URL: клиент обращается к `/v2/<image>/manifests/<tag>`, и «репозиторий-менеджер» никак не может узнать, в какой из репозиториев Nexus целится. Поэтому Nexus разводит их по портам (connector), а путь образа начинается сразу с имени образа. Harbor использует другую модель: проект — это часть имени образа (`host/vektor/web`), а внутри проекта уже работает RBAC, квоты и сканирование уязвимостей. Плюс Harbor — это registry, а Nexus — мультиформатный менеджер; они не взаимоисключают друг друга.
**Что хочет услышать интервьюер:** понимание Registry API и того, что имя образа — часть адресации.
**Красный флаг:** «просто так исторически сложилось».

**3. Как пайплайн должен аутентифицироваться в реестре и почему не личным паролем разработчика?**
**Ответ:** через короткоживущие машинные креды: `CI_JOB_TOKEN` в GitLab, deploy-токен с правом `write_package_registry`, сервисная учётка в Nexus/Artifactory, OIDC-федерация в облаках. Причины: личный пароль не отзывается при увольнении сотрудника, он даёт доступ ко всем репозиториям сразу, он попадает в логи и в артефакты, его невозможно ротировать без остановки пайплайна. Плюс машинные креды позволяют разграничить права: прод-репозиторий пишет только защищённая ветка/тег.
**Что хочет услышать интервьюер:** принцип наименьших привилегий и сменяемость секретов.
**Красный флаг:** «положим пароль админа в переменную CI и всё».

**4. Что такое dependency confusion и как от него защищаться?**
**Ответ:** атака на цепочку поставок: если внутренние пакеты называются так же, как публичные (или без namespace), злоумышленник публикует в публичный реестр пакет с тем же именем и более высокой версией. Менеджер пакетов по умолчанию предпочитает старшую версию и подтягивает чужой код в вашу сборку. Защита: scoped-имена (`@vektor/...`) с жёсткой привязкой scope к внутреннему реестру, единый внутренний индекс без «extra index» в публичный, проксирование внешних реестров только через контролируемый Nexus/Artifactory с вайтлистом, lock-файлы с проверкой целостности (`integrity` в `package-lock.json`, `--require-hashes` в pip), SCA-сканирование и SBOM.
**Что хочет услышать интервьюер:** что вы знаете атаку по имени и закрываете её архитектурно, а не «мы аккуратные».
**Красный флаг:** «это проблема только для open-source, у нас свой код».

**5. Nexus, Artifactory или GitLab Package Registry — что выберете для компании на 50 человек с GitLab CI?**
**Ответ:** зависит от бюджета и форматов. GitLab Package Registry уже есть — нулевая стоимость внедрения, интеграция с `CI_JOB_TOKEN` и правами проекта, покрывает Maven/npm/PyPI/Helm/generic и Container Registry. Но: нет нормального проксирования внешних реестров с кэшем (это не его роль), слабее retention и дедупликация, привязка к одному инстансу GitLab. Nexus Community Edition бесплатен, даёт proxy+group по всем нужным форматам, зрелые cleanup-политики и REST API — оптимум для 50 человек, если нужен единый кэш Maven/npm/PyPI и офлайн-устойчивость. Artifactory — самый функциональный (метаданные, AQL, продвинутые политики, HA), но платный; оправдан при больших объёмах, жёстких требованиях к безопасности и мультиформатности. Рабочая схема: GitLab — для сборки и CI-артефактов, Nexus — единая точка входа для зависимостей и Docker/Helm, Harbor — если нужны сканирование образов и подпись.
**Что хочет услышать интервьюер:** сравнение по критериям (цена, форматы, прокси, RBAC, retention, HA), а не «берите Artifactory, он крутой».
**Красный флаг:** выбор без вопроса «а сколько это стоит и кто будет сопровождать».

## 8. Источники (официальная документация)

- **Sonatype** — домен `help.sonatype.com`, разделы «Nexus Repository 3» → «Repository Management» (форматы: Maven, npm, PyPI, Docker, Helm) и «Security → Realms». Поиск: `nexus repository 3 npm bearer token realm`, `nexus docker connector port`.
- **JFrog** — домен `jfrog.com`, раздел `jfrog.com/help` → «Artifactory» → «Repository Management» (Local/Remote/Virtual). Поиск: `artifactory npm virtual repository setup`, `artifactory repository layout`.
- **GitLab Docs** — домен `docs.gitlab.com`, раздел «Package Registry» (Maven, npm, PyPI, Helm, Generic) и «Container Registry». Поиск: `gitlab package registry npm CI_JOB_TOKEN`, `gitlab helm chart registry`.
- **Docker Docs** — домен `docs.docker.com`, раздел «Registry» и «Docker Engine → Configure the daemon» (`insecure-registries`). Поиск: `docker registry http api v2 specification`.
- **Helm Docs** — домен `helm.sh/docs`, разделы «Chart Repository Guide» и «Use OCI-based registries». Поиск: `helm push oci registry`, `helm cm-push chartmuseum plugin` `[ПРОВЕРИТЬ актуальность плагина helm-push]`.
- **Apache Maven** — домен `maven.apache.org`, разделы «Guide to deploying 3rd party JARs» и «Settings Reference» (servers, mirrors). Поиск: `maven deploy distributionManagement settings.xml server id`.
- **PyPI / Python Packaging** — домены `packaging.python.org` и `twine.readthedocs.io`, разделы про `.pypirc` и `twine upload`. Поиск: `twine upload custom repository pypirc`, `PEP 503 simple repository api`.
