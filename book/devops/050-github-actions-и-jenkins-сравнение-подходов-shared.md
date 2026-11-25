# GitHub Actions и Jenkins: сравнение подходов, shared libraries, Jenkinsfile, self-hosted runners

## 1. Введение (зачем это нужно в реальной работе)

> **Тикет #4127.** Автор: Сидоров (склад). Тема: «Сборка не выходит на тестовый стенд».
> «Здравствуйте! Вчера просили добавить в отчёт поле "номер рейса". Сегодня на тесте всё ещё старая версия. Разработчик говорит: "джоба в Jenkins красная, лог на сто экранов, я в нём ничего не понимаю". Посмотрите, пожалуйста. Срочность: средняя, отчёт нужен к пятнице.»

За этой заявкой стоит типичная ситуация: CI-сервер в компании есть, но им никто не владеет. Джобы создавались годами «как получилось», конфигурация живёт в веб-интерфейсе, а не в Git, половина пайплайнов скопирована друг у друга с правками, плагины не обновлялись с 2021 года. И когда что-то падает, непонятно, где искать причину. Именно здесь появляется выбор инструмента и, что важнее, дисциплина его использования.

В этой главе мы разберём два главных подхода к CI в индустрии. Jenkins — классический self-hosted контроллер с плагинами и агентами, который в РФ стоит почти в каждой второй компании. GitHub Actions — облачный, «всё в YAML рядом с кодом», с готовыми действиями из маркетплейса. Мы сравним их архитектурно, разберём Jenkinsfile (декларативный и скриптовый), shared libraries (общие библиотеки шагов) и то, что объединяет оба мира — self-hosted runners: свои машины-исполнители.

После главы ты должен уметь: поднять Jenkins в контейнере на стенде, хранить пайплайн как код в репозитории, вынести повторяющиеся шаги в shared library, подключить собственного агента (аналог self-hosted runner) и аргументированно объяснить на собеседовании, почему выбрал Jenkins или GitHub Actions.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Оба инструмента решают одну задачу: получить событие из Git, запустить набор шагов на машине-исполнителе и вернуть результат. Отличается архитектура.

**Jenkins.** Это Java-приложение (WAR/контейнер), состоящее из **controller** (веб-UI, очередь задач, планировщик, хранилище конфигурации в `$JENKINS_HOME`) и **агентов (nodes)** — машин, где реально выполняются шаги. Агент бывает: постоянный (SSH или JNLP-подключение), облачный (Docker/Kubernetes plugin создаёт контейнер на время сборки) или встроенный (`built-in node`, он же master — использовать его для сборок в проде считается плохой практикой). Каждая сборка получает **executor** (слот) и **workspace** (рабочий каталог на агенте).

Функциональность Jenkins почти целиком строится на **плагинах**: Git, Pipeline, Credentials, SSH Build Agents, JUnit, Workspace Cleanup. Плагины — одновременно сила (гибкость) и главный риск (CVE, конфликты зависимостей, «плагин не обновлялся 4 года»).

**GitHub Actions.** Исполнители (**runners**) бывают двух видов: `ubuntu-latest`/`windows-latest`/`macos-latest` — облачные машины GitHub (для публичных репозиториев бесплатно, для приватных — минуты по тарифу), и **self-hosted runner** — твой собственный сервер. Runner — это агент, который сам **ходит наружу на `github.com` по HTTPS/443** и забирает задания (long-poll). Открывать входящие порты не нужно — это ключевое архитектурное отличие от классического JNLP-агента Jenkins.

Конфигурация GitHub Actions — файлы `.github/workflows/*.yml` в репозитории (Workflow → Job → Step). Переиспользование — через **composite actions**, **reusable workflows** (`workflow_call`) и actions из Marketplace.

Схема нашего стенда после этой главы:

```text
                 ┌──────────────────────────────────────┐
   git push ────▶ │ GitLab CE  GIT01  10.10.10.50        │
                 │  vektor/app            (код + Jenkinsfile)
                 │  vektor/jenkins-shared-lib  (библиотека)
                 └───────────────┬──────────────────────┘
                                 │ HTTP :80  (webhook / SCM poll)
                                 ▼
                 ┌──────────────────────────────────────┐
                 │ Jenkins controller (контейнер)        │
                 │ GIT01:8081                            │
                 │ $JENKINS_HOME → volume jenkins_home   │
                 │ очередь, UI, плагины, credentials     │
                 └───────────────┬──────────────────────┘
                                 │ SSH :22  (SSH Build Agent)
                                 │ + Java на агенте
                                 ▼
                 ┌──────────────────────────────────────┐
                 │ agent «ubu01»  10.10.10.30            │
                 │ user jenkins, /home/jenkins/agent     │
                 │ тут реально бегут sh-шаги             │
                 └──────────────────────────────────────┘
```

Порты, которые надо помнить: Jenkins UI — **8080** внутри контейнера (у нас проброшен на **8081**), **50000/TCP** — только для JNLP-агентов (нам не нужен, у нас SSH), **22/TCP** — SSH-агенты, GitLab — 80/443, self-hosted runner GitHub — исходящий **443**.

Jenkinsfile бывает двух видов: **declarative** (`pipeline { agent ... stages { ... } post { ... } }` — строгий синтаксис, «из коробки» есть `post`, `options`, `when`, `matrix`) и **scripted** (`node { stage('x') { ... } }` — чистый Groovy, максимум гибкости, минимум проверок). В 2026 году по умолчанию пишут declarative.

Shared library — это отдельный Git-репозиторий с тремя каталогами:

```text
jenkins-shared-lib/
├── vars/        # глобальные переменные: buildApp.groovy → buildApp(...)
├── src/         # Groovy-классы: org/vektor/Utils.groovy
└── resources/   # статические файлы (шаблоны, скрипты), доступны через libraryResource()
```

## 3. Аналогия из жизни

Jenkins — это **своя мастерская в гараже**. Ты покупаешь станки сам (плагины), расставляешь их как хочешь, можешь сделать под свой процесс что угодно. Но электрику, вентиляцию, техобслуживание станков и пожарную безопасность обеспечиваешь тоже сам. Если один станок устарел и задымил — это твоя проблема, и хорошо бы иметь журнал, когда ты его последний раз проверял.

GitHub Actions — это **аренда рабочего места в технопарке**. Помещение, электричество и часть станков уже включены, новые берёшь из каталога с инструкциями (Marketplace), за простой платишь по счётчику. Быстро стартовать, но планировка — по правилам арендодателя: свои тяжёлые станки тоже можно привезти (self-hosted runner), только придётся подписать правила техники безопасности и пускать туда не кого попало.

Аналогия ломается в одном: в реальности «гараж» легко поставить в контейнер и получить почти облачный опыт, а «технопарк» можно развернуть у себя (GitLab/Gitea + runners) — граница между подходами проходит не по «облако/не облако», а по тому, кто отвечает за обновления и изоляцию исполнителей.

## 4. Практика и команды

### 4.1. Jenkins в контейнере (на GIT01)

```bash
docker volume create jenkins_home
docker run -d --name jenkins --restart unless-stopped \
  -p 8081:8080 \
  -v jenkins_home:/var/jenkins_home \
  jenkins/jenkins:lts
```
`-p 8081:8080` — проброс порта: снаружи 8081, внутри контейнера стандартный 8080 (на GIT01 80/443 заняты GitLab). `-v jenkins_home:/var/jenkins_home` — именованный том, иначе все джобы исчезнут при пересоздании контейнера. `--restart unless-stopped` — переживает перезагрузку ВМ. Тег `lts` — стабильная ветка; для конкретной версии Java смотри теги `lts-jdk17` / `lts-jdk21` `[ПРОВЕРИТЬ под свою версию]`.

### 4.2. Первичный пароль и логи

```bash
docker exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword
docker logs --tail 100 -f jenkins
```
Первый пароль нужен один раз на экране разблокировки. В `docker logs` смотрим старт: `Jenkins is fully up and running` — значит поднялся; `java.lang.OutOfMemoryError` — мало памяти.

### 4.3. Jenkins CLI (когда UI недоступен или нужна автоматизация)

```bash
curl -O http://10.10.10.50:8081/jnlpJars/jenkins-cli.jar
java -jar jenkins-cli.jar -s http://10.10.10.50:8081/ \
  -auth adm.student:'Lab#2026-vektor' list-jobs
java -jar jenkins-cli.jar -s http://10.10.10.50:8081/ \
  -auth adm.student:'Lab#2026-vektor' build app-ci -s
```
`list-jobs` — список джоб, `build <job> -s` — запустить и дождаться завершения (`-s` = synchronous). Пароль в командной строке виден в истории — в реальной среде используют API-токен и переменные окружения.

### 4.4. Jenkinsfile (declarative)

```groovy
pipeline {
  agent { label 'linux' }
  options {
    timestamps()
    timeout(time: 15, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }
  environment { IMAGE_TAG = "1.0.${BUILD_NUMBER}" }
  stages {
    stage('Checkout') { steps { checkout scm } }
    stage('Lint')     { steps { sh 'echo "lint ok"' } }
    stage('Test')     { steps { sh 'echo "tests ok"' } }
    stage('Build') {
      when { branch 'main' }
      steps { sh "echo build image vektor/app:${IMAGE_TAG}" }
    }
  }
  post {
    success { echo 'сборка зелёная' }
    failure { echo 'сборка красная' }
    always  { cleanWs() }
  }
}
```
`agent { label 'linux' }` — где выполняем (метка агента). `options` — таймаут (чтобы зависшая сборка не держала executor сутками) и ротация логов (`buildDiscarder` — защита от переполнения диска, см. раздел 6). `when { branch 'main' }` — шаг только для основной ветки. `post` — действия после сборки, `cleanWs()` очищает workspace.

### 4.5. Шаг из shared library — `vars/buildApp.groovy`

```groovy
def call(Map cfg = [:]) {
  def image = cfg.image ?: 'app'
  def tag   = cfg.tag ?: env.BUILD_NUMBER
  pipeline {
    agent { label cfg.agent ?: 'linux' }
    stages {
      stage('Build') { steps { sh "docker build -t ${image}:${tag} ." } }
      stage('Smoke') { steps { sh "echo smoke ${image}:${tag}" } }
    }
  }
}
```
Файл `vars/buildApp.groovy` автоматически становится глобальной функцией `buildApp(...)` — именно потому, что лежит в `vars/`.

### 4.6. Класс из shared library и его подключение

```groovy
// src/org/vektor/Utils.groovy
package org.vektor
class Utils implements Serializable {
  static String slug(String s) { s.toLowerCase().replaceAll(/[^a-z0-9]+/, '-') }
}
```

```groovy
// Jenkinsfile
@Library('vektor-lib@main') _
import org.vektor.Utils
buildApp(image: 'vektor/app', tag: Utils.slug(env.BRANCH_NAME))
```
`@Library('имя@версия') _` — подключает библиотеку (подчёркивание обязательно, это аннотация на «пустом» выражении). `implements Serializable` — обязательное требование к классам: пайплайн должен переживать перезапуск контроллера.

### 4.7. SSH-ключ и агент на UBU01

```bash
# на UBU01
sudo useradd -m -s /bin/bash jenkins
sudo apt install -y openjdk-21-jre-headless    # агенту нужна Java
sudo -u jenkins ssh-keygen -t ed25519 -N '' -f /home/jenkins/.ssh/id_ed25519
sudo -u jenkins cat /home/jenkins/.ssh/id_ed25519.pub
sudo -u jenkins mkdir -p /home/jenkins/agent
```
`-N ''` — ключ без пароля (иначе агент не подключится неинтерактивно). Публичную часть добавляем в `/home/jenkins/.ssh/authorized_keys`, приватную — в Jenkins как credential типа «SSH Username with private key».

### 4.8. Тот же пайплайн на GitHub Actions

```yaml
name: ci
on:
  push: { branches: [main] }
  pull_request:
permissions:
  contents: read
jobs:
  build:
    runs-on: [self-hosted, linux, ubu01]
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: echo "lint ok"
      - name: Test
        run: echo "tests ok"
      - name: Build
        run: echo "build vektor/app:${GITHUB_RUN_NUMBER}"
```
`runs-on: [self-hosted, linux, ubu01]` — три метки, раннер должен иметь их все. `permissions` урезает права `GITHUB_TOKEN` до чтения — базовая гигиена.

### 4.9. Self-hosted runner GitHub: установка

```bash
# на UBU01, ссылку и токен берём на странице Settings → Actions → Runners
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L "<URL_СО_СТРАНИЦЫ>"
tar xzf runner.tar.gz
./config.sh --url https://github.com/<owner>/<repo> --token <REGISTRATION_TOKEN>
./run.sh                       # интерактивный запуск, для проверки
sudo ./svc.sh install && sudo ./svc.sh start   # как сервис
```

### 4.10. Токен регистрации через API

```bash
curl -X POST -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/actions/runners/registration-token
```
Токен живёт около часа — это правильный способ автоматизировать регистрацию раннеров в IaC-стиле.

### 4.11. Линтер workflow-файлов

```bash
actionlint .github/workflows/ci.yml
```
Ловит опечатки в YAML, несуществующие контексты и выражения до пуша. Ставится бинарником с релизов проекта `rhysd/actionlint`.

### 4.12. Jenkins как код (JCasC)

```yaml
# jenkins.yaml — Configuration as Code plugin
jenkins:
  systemMessage: "Vektor CI (lab)"
  numExecutors: 0
  nodes:
    - permanent:
        name: "ubu01"
        labelString: "linux ubu01"
        remoteFS: "/home/jenkins/agent"
        launcher:
          ssh:
            host: "10.10.10.30"
            port: 22
            credentialsId: "ubu01-jenkins-ssh"
```
`numExecutors: 0` — запрещаем сборки на контроллере. Точную вложенность полей `launcher.ssh` сверяй с документацией плагина `[ПРОВЕРИТЬ]`.

## 5. Лабораторная работа

### Предусловия

Готовы: **GIT01** (`10.10.10.50`, GitLab CE, Docker) и **UBU01** (`10.10.10.30`, SSH). На GIT01 в GitLab из урока 49 есть проект `vektor/app` (если назвал иначе — используй своё имя) и у тебя есть личный access token GitLab со scope `read_repository`. Jenkins ещё нет — ставим здесь. Windows-часть стенда не нужна. Доступ в интернет с GIT01 нужен для установки плагинов; если его нет — плагины ставятся вручную через «Advanced → Deploy Plugin» `[ПРОВЕРИТЬ: список зависимостей для офлайн-установки]`.

### Снапшот

В VMware Workstation: останови GIT01 и UBU01 (`sudo shutdown -h now`) → правый клик по ВМ → **Snapshot → Take Snapshot** → имя `before-devops-050`. Сделай на **обеих** ВМ, затем включи их.

### Шаги

1. **GIT01.** `docker volume create jenkins_home` → `docker run` из п.4.1 → `docker ps`. Ожидаемо: контейнер `jenkins` в статусе `Up`, порт `0.0.0.0:8081->8080/tcp`.
2. **GIT01.** `docker exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword`. Ожидаемо: 32-символьная строка.
3. **С твоего ПК.** Открой `http://10.10.10.50:8081`, введи пароль → «Install suggested plugins» (это долго, 5–10 минут) → создай админа `adm.student` / `Lab#2026-vektor`. Ожидаемо: главная страница Jenkins.
4. **GIT01, GitLab.** В `vektor/app` создай файл `Jenkinsfile` с содержимым из п.4.4 (пока оставь `agent any` — агента ещё нет), коммит в `main`. Ожидаемо: файл в репозитории.
5. **GIT01, GitLab.** Preferences → Access Tokens → создай токен, scope `read_repository`, скопируй.
6. **Jenkins → Manage Jenkins → Credentials → System → Global credentials → Add Credentials.** Kind: *Username with password*; Username: твоя учётка GitLab; Password: токен; ID: `gitlab-adm`. Ожидаемо: запись в списке.
7. **Jenkins → New Item → Pipeline → имя `app-ci`** → Pipeline script from SCM → Git → Repository URL `http://10.10.10.50/vektor/app.git` → Credentials `gitlab-adm` → Branch `*/main` → Script Path `Jenkinsfile` → Save → **Build Now**. Ожидаемо: стадии Checkout/Lint/Test/Build зелёные в Blue Ocean или в консоли.
8. **UBU01.** Выполни п.4.7 целиком, добавь публичный ключ в `/home/jenkins/.ssh/authorized_keys` с правами `600`. Проверь: `sudo -u jenkins ssh -o BatchMode=yes jenkins@10.10.10.30 hostname`. Ожидаемо: `UBU01`.
9. **Jenkins → Manage Jenkins → Nodes → New Node.** Имя `ubu01`, Number of executors `1`, Remote root directory `/home/jenkins/agent`, Labels `linux ubu01`, Launch method *Launch agents via SSH*, Host `10.10.10.30`, Credentials — новый *SSH Username with private key* (username `jenkins`, приватный ключ). Save → открой узел → **Launch agent**. Ожидаемо: «Agent is online», в логе `Agent successfully connected and online`.
10. **GIT01, GitLab.** В `Jenkinsfile` замени `agent any` на `agent { label 'linux' }`, добавь `stage('Whoami') { steps { sh 'hostname; whoami' } }`. Коммит → **Build Now**. Ожидаемо: в консоли `UBU01` и `jenkins` — шаги ушли на агента.
11. **GIT01, GitLab.** Создай проект `vektor/jenkins-shared-lib`, положи туда `vars/buildApp.groovy` (п.4.5), `src/org/vektor/Utils.groovy` (п.4.6) и README. Ожидаемо: репозиторий с тремя каталогами.
12. **Jenkins → Manage Jenkins → System → Global Pipeline Libraries.** Name `vektor-lib`, Default version `main`, *Modern SCM* → Git → URL `http://10.10.10.50/vektor/jenkins-shared-lib.git` + те же credentials. Save.
13. **GIT01, GitLab.** Замени тело `Jenkinsfile` на `@Library('vektor-lib@main') _` + `buildApp(image: 'vektor/app')` (п.4.6). Коммит → Build Now. Ожидаемо: стадии Build/Smoke, в консоли путь `…/jenkins-shared-lib@main/vars/buildApp.groovy`.
14. **Сравнение.** Добавь в `vektor/app` файл `.github/workflows/ci.yml` (п.4.8) и прогони на UBU01 `actionlint`, если бинарник доступен. Ожидаемо: линтер молчит, файл лежит рядом с Jenkinsfile — оба подхода описаны кодом.

### Проверка

- [ ] `docker ps` на GIT01 показывает `jenkins` в статусе `Up`, `docker volume ls` — том `jenkins_home`.
- [ ] `http://10.10.10.50:8081` открывается, вход под `adm.student`.
- [ ] Джоба `app-ci` зелёная, в консоли видно `Checking out Revision …` из GitLab.
- [ ] Узел `ubu01` в Nodes в статусе online, у него метка `linux`.
- [ ] В консоли сборки есть `BUILD_NUMBER` и вывод `hostname` = `UBU01`.
- [ ] После shared library в консоли есть строка про загрузку `jenkins-shared-lib@main`.
- [ ] В `vektor/app` есть `Jenkinsfile` и `.github/workflows/ci.yml`.
- [ ] В `stand-log.md` записано: контейнер Jenkins, том, админ, узел, библиотека.

### Сломай и почини

> **Пользователь:** Петрова (HR). **ВМ:** любая, она ходит с WIN10-CL02.
> **Что написала:** «Открываю ссылку на ваш Jenkins (10.10.10.50:8081) — страница не грузится, "сайт недоступен". Вчера всё открывалось, нам показывали сборку. Срочность: высокая, обещали показать руководству.»

**Воспроизводим поломку (на GIT01):**
```bash
docker update --restart=no jenkins
docker stop jenkins
```

**Диагностика по порядку:**
1. `ping 10.10.10.50` — ВМ жива? Если нет, проблема не в Jenkins.
2. `curl -I http://10.10.10.50:8081` — `Connection refused` → слушателя нет.
3. `docker ps -a | grep jenkins` — статус `Exited (0)`. Значит контейнер остановлен, а не упал.
4. `docker logs --tail 50 jenkins` — если лог заканчивается аккуратно и без исключений, это ручная остановка или OOM-killer.
5. `dmesg -T | grep -i -E 'oom|killed process'` — есть ли убийство процесса ядром.
6. `df -h /var/lib/docker` — нет ли переполнения диска (частая причина).
7. `systemctl status docker` — сам Docker жив?

**Решение:**
```bash
docker update --restart=unless-stopped jenkins
docker start jenkins
docker logs --tail 20 jenkins | grep "fully up and running"
curl -s -o /dev/null -w "%{http_code}\n" http://10.10.10.50:8081/login   # ждём 200
```
Затем проверь, что сборки не потерялись: `java -jar jenkins-cli.jar -s http://10.10.10.50:8081/ -auth adm.student:'Lab#2026-vektor' list-jobs`. Если после старта сборка падает с `There are no nodes with the label 'linux'` — отдельно проверь агента: `ssh jenkins@10.10.10.30 hostname` и статус узла в Nodes.

**Ответ пользователю:** «Петрова, здравствуйте! Jenkins был остановлен на сервере (сервис не запущен после работ), данные и сборки целы. Подняли, страница 10.10.10.50:8081 снова доступна, проверили тестовой сборкой — всё зелёное. Если увидите ошибку — пришлите скриншот, разберёмся.»

**Закрытие заявки:** категория «CI/CD → недоступность сервиса», причина «контейнер Jenkins остановлен, автозапуск отключён», действие «включён `--restart=unless-stopped`, добавлен мониторинг HTTP-кода» — и заведи мониторинг на MON01 (Zabbix, урок про наблюдаемость): HTTP 200 на `/login` плюс статус контейнера.

### Что изменилось на стенде

- **GIT01 (10.10.10.50):** Docker-контейнер `jenkins` (образ `jenkins/jenkins:lts`), порт **8081**, том `jenkins_home`, автозапуск `unless-stopped`.
- **Jenkins:** админ `adm.student`, credential `gitlab-adm`, узел `ubu01` (метки `linux ubu01`), credential SSH к UBU01, глобальная библиотека `vektor-lib` (default `main`), джоба `app-ci`.
- **UBU01 (10.10.10.30):** локальный пользователь `jenkins`, JRE, ключ `~/.ssh/id_ed25519`, каталог `/home/jenkins/agent`.
- **GitLab:** проекты `vektor/app` (+`Jenkinsfile`, `.github/workflows/ci.yml`) и `vektor/jenkins-shared-lib`.

### Откат

Если что-то сломалось необратимо: `docker rm -f jenkins && docker volume rm jenkins_home` на GIT01, `sudo userdel -r jenkins` на UBU01, затем в VMware — **Snapshot Manager → before-devops-050 → Restore**. В `stand-log.md` допиши строку «откат к before-devops-050, изменения Jenkins отменены».

## 6. Ошибки новичков и безопасность

1. **`agent any` и сборки на контроллере.** → Битый билд роняет контроллер, workspace'ы забивают диск, а вредоносный код из PR получает доступ к `$JENKINS_HOME` с credentials. → `numExecutors: 0`, всё на агентах.
2. **Конфигурация в UI, а не в Git.** → Удалили джобу — восстановить нечем; «документация» — это скриншоты. → Jenkinsfile в репозитории + JCasC для настроек самого Jenkins.
3. **Нет `buildDiscarder`.** → Логи и артефакты съедают диск, Jenkins падает с `No space left on device`. → Ротация (п.4.4) и алерт на `df -h`.
4. **Плагины не обновляются.** → Накопленные CVE; Jenkins исторически самая частая цель ботнетов именно через устаревшие плагины. → Регулярное обновление на тестовом инстансе, фиксация версий в Docker-образе, отключение неиспользуемых плагинов.
5. **Shared library без контроля доступа.** → Библиотеки в Jenkins считаются **trusted**: их код выполняется вне Groovy-песочницы и может достать любые credentials контроллера. → Права на push в репозиторий библиотеки — только у доверенных людей, обязательный code review, версионирование через `@Library('lib@v1.2.0')`.
6. **Self-hosted runner на публичном репозитории.** → Любой, кто пришлёт PR, потенциально выполнит свой код на твоей машине; так сливали секреты и делали криптомайнеров. → Для публичных проектов — только облачные runners, либо изолированные эфемерные раннеры без секретов и с исходящим трафиком под контролем.
7. **`actions/checkout@v4` «как есть» и `pull_request_target` без понимания.** → Тег может быть перезаписан, а `pull_request_target` даёт права на секреты при запуске кода из форка. → Пиннинг по SHA (`uses: actions/checkout@<commit-sha>`), минимальные `permissions`, не использовать `pull_request_target` без явной фильтрации.

**Безопасность (что нельзя в боевой среде):** хранить пароль в `-auth user:password` и в истории команд; монтировать `/var/run/docker.sock` в контейнер Jenkins (это фактически root на хосте) — для сборки образов используют Kaniko/buildah (см. урок 58); класть приватный SSH-ключ агента без пароля куда-либо, кроме Jenkins Credentials; держать Jenkins в открытом интернете без reverse-proxy, TLS и MFA. На стенде пароль `Lab#2026-vektor` учебный — в реальной среде так нельзя.

## 7. Вопросы на собеседовании

**1. Чем архитектурно отличаются Jenkins и GitHub Actions и что выберешь для компании с закрытым контуром?**
Ответ: Jenkins — self-hosted controller + агенты, конфигурация в `$JENKINS_HOME` и в репозитории через Jenkinsfile, функциональность из плагинов, которые ты сам обновляешь; агент может быть «входящим» (JNLP, 50000/TCP). GitHub Actions — SaaS-контроллер, workflows в `.github/workflows`, готовые actions, runner по умолчанию ходит наружу по 443 и не требует входящих портов. Для закрытого контура без доступа к github.com — Jenkins on-prem или GitLab/Gitea self-managed; GitHub Enterprise Server тоже возможен, но покупка и поддержка из РФ затруднены `[ПРОВЕРИТЬ актуальные условия]`, поэтому легальная альтернатива — GitLab CE или Gitea/Forgejo с act_runner.
*Что хочет услышать:* понимание «кто отвечает за обновления и изоляцию» и осознанный выбор, а не «модно/немодно». *Красный флаг:* «Jenkins устарел, всегда GitHub Actions» без учёта контура и данных.

**2. Declarative или scripted pipeline? Что такое Jenkinsfile и почему он в репозитории?**
Ответ: declarative — строгая структура `pipeline { agent / stages / post / options / when }`, валидируется, есть `post` и `matrix`; scripted — свободный Groovy (`node { }`), нужен для нестандартной логики, но легко получить нечитаемый и нестабильный код. Jenkinsfile — Pipeline as Code: версионируется вместе с приложением, проходит code review, одинаково разворачивается на любом контроллере, история изменений видна в Git. Всё, что можно, — declarative, точечно уходим в скриптовый Groovy или в shared library.
*Что хочет услышать:* «конфигурация в Git и проходит ревью». *Красный флаг:* «у нас всё настроено в UI, так удобнее».

**3. Зачем shared libraries и как они устроены?**
Ответ: убрать копипасту между десятками джоб: один шаг сборки/деплоя, единые стандарты (теги образов, ретраи, нотификации). Репозиторий: `vars/` — глобальные функции, `src/` — Groovy-классы (обязательно `implements Serializable`), `resources/` — статические файлы через `libraryResource`. Подключение: глобально в Manage Jenkins → System → Global Pipeline Libraries, либо на месте `library 'lib@1.4.0'`; в Jenkinsfile — `@Library('lib@main') _`. Версионировать через теги, а не `main`, иначе «библиотека сломала все джобы сразу».
*Что хочет услышать:* версионирование и то, что библиотека — общий актив с владельцем и ревью. *Красный флаг:* «библиотека без версии, всегда тянем main».

**4. Зачем self-hosted runner и какие у него риски?**
Ответ: нужен доступ к внутренней сети, приватному реестру, спецжелезу (GPU, лицензии 1С), либо большие объёмы сборок дешевле держать у себя. Риски: постоянный раннер на публичном репозитории = RCE от любого PR; секреты живут на машине; среда «зарастает» между сборками. Меры: эфемерные раннеры (контейнер/VM на job, автоскейл через ARC/Docker), метки `self-hosted` + свои, минимальные права, отдельный сетевой сегмент, ротация токенов, запрет `pull_request_target` на публичных репо.
*Что хочет услышать:* «self-hosted = ты отвечаешь за безопасность и чистоту окружения». *Красный флаг:* «поставим раннер на прод-сервер, чтобы был доступ к базе».

**5. Как обновлять Jenkins и почему плагины — главная боль?**
Ответ: контроллер и плагины обновляются отдельно; плагины тянут зависимости друг друга, поэтому обновление «всё сразу» ломает совместимость. Практика: зафиксировать версии образа и плагинов (Docker-образ или plugin.txt + `jenkins-plugin-cli`), сначала обновлять на тестовом инстансе с прогоном эталонной джобы, читать changelog и security-адвайзори, держать бэкап `$JENKINS_HOME` и снапшот ВМ, обновлять контроллер и плагины регулярно, а не раз в три года. Для новых установок — минимальный набор плагинов.
*Что хочет услышать:* «тестовый инстанс, фиксация версий, бэкап, регулярность». *Красный флаг:* «нажимаю "Update all" на проде и надеюсь».

## 8. Источники (официальная документация)

- **jenkins.io** — разделы *Pipeline*, *Using a Jenkinsfile*, *Extending with Shared Libraries*, *Managing Nodes* (SSH agents), *Configuration as Code*; поиск: `jenkins shared libraries vars src resources`.
- **jenkins.io/doc/book/installing/docker** — установка Jenkins в Docker, начальный пароль, тома; поиск: `jenkins docker install jenkins/jenkins lts`.
- **plugins.jenkins.io** — страницы плагинов *Git*, *Pipeline*, *SSH Build Agents*, *Credentials Binding*, *Workspace Cleanup*; поиск по имени плагина и разделу *Security*.
- **docs.github.com** — разделы *Actions: Workflow syntax*, *Self-hosted runners*, *Security hardening for GitHub Actions*, *Reusing workflows*; поиск: `github actions workflow syntax runs-on self-hosted`.
- **docs.github.com (REST API)** — *Actions → Self-hosted runners → Create registration token*; поиск: `github rest api actions runners registration-token`.
- **github.com/rhysd/actionlint** — README линтера workflow-файлов; поиск: `actionlint usage shellcheck`.
- **kubernetes.io / gitlab.com** — если пойдёшь в сторону GitLab CI и Kubernetes-исполнителей (для сравнения с Jenkins Kubernetes plugin); поиск: `gitlab runner kubernetes executor` `[ПРОВЕРИТЬ актуальность раздела]`.
