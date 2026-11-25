# Контейнеры в РФ-реалиях: зеркала Docker Hub, Yandex Container Registry, VK Cloud, локальные прокси-кэши

## 1. Введение (зачем это нужно в реальной работе)

Тикет #4712. Пользователь: `adm.student` (IT). ВМ: UBU01. Текст: «Собираю образ по Dockerfile, на строке `FROM ubuntu:24.04` — `failed to resolve source metadata ... i/o timeout` на `registry-1.docker.io`. Вчера то же самое работало. Очень нужно к вечеру». Срочность: высокая.

Это не «сломался Docker». Это с июня 2024 года Docker Inc. ограничил доступ к своим сервисам для российских IP `[ПРОВЕРИТЬ точную дату и формулировку в блоге Docker]`, плюс у Docker Hub всегда были лимиты на число скачиваний. В итоге `docker pull nginx` из московского офиса может не работать вообще — а без образов не собирается CI, не поднимается кластер и не обновляется прод.

После этой главы ты должен уметь: объяснить, почему pull падает и чем это отличается от «нет интернета»; поднять локальный прокси-кэш (pull-through cache) на Harbor и на голом `registry:2`; настроить демон Docker и containerd так, чтобы они ходили через зеркало; уметь перенести образ в изолированный контур через `skopeo` и `docker save`; и не превратить всё это в дыру в безопасности.

## 2. Теория «под капотом» (как это работает: архитектура, протоколы)

Registry (реестр образов) — это HTTP-сервис, реализующий **OCI Distribution Spec** (бывший Docker Registry API v2). Всё общение идёт по HTTPS на 443, по путям `/v2/...`. Скачивание образа — это три запроса:

1. `GET /v2/` → в ответ `401 Unauthorized` с заголовком `WWW-Authenticate: Bearer realm="https://auth.docker.io/token",service="registry.docker.io"`.
2. Клиент идёт на `auth.docker.io`, получает токен (анонимный или по логину/паролю).
3. С токеном запрашивает **manifest** (`/v2/library/nginx/manifests/1.27`) — JSON-описание образа: список слоёв и их digest'ы. Затем тянет **blobs** (слои) с CDN.

Ключевая идея: **образ адресуется digest'ом** — хешем sha256 от содержимого. Поэтому любое зеркало обязано отдавать байт-в-байт тот же manifest, иначе digest не сойдётся и клиент откажется. Это твоя главная защита от подмены.

Схема того, что мы строим:

```text
                    ┌──────────────── интернет ────────────────┐
                    │  registry-1.docker.io / auth.docker.io    │
                    │  (может быть недоступен из РФ)           │
                    └───────────────▲──────────────────────────┘
                                    │ HTTPS 443 (один раз на образ)
                    ┌───────────────┴──────────────┐
                    │  HARBOR01 10.10.10.55        │
                    │  проект-прокси dockerhub-proxy│  ← «перевалочная база»
                    │  (pull-through cache)        │
                    └───────────────▲──────────────┘
                                    │ HTTPS 443 / HTTP 5000 (в лабе)
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
   UBU01 10.10.10.30          GIT01 10.10.10.50           k8s-* 10.10.10.61-63
   docker pull                GitLab CI: image:            containerd
                              harbor.../dockerhub-proxy/…   hosts.toml
```

Три способа закрыть проблему, и они не взаимоисключающие:

| Способ | Что это | Плюсы | Минусы |
|---|---|---|---|
| Публичное зеркало облака | сервис провайдера, отдающий образы Docker Hub под своим префиксом | 5 минут на старте, ничего не администрируешь | зависишь от провайдера и его биллинга, часто без гарантий |
| Свой pull-through cache | Harbor / Nexus / `registry:2`, который кэширует по требованию | контроль, свой аудит, работает как единая точка | нужен диск, обслуживание, TLS/CA |
| Оффлайн-перенос | `skopeo copy` / `docker save` + доставка файла | единственный путь в air-gap и на закрытые объекты | ручная работа, нет автообновлений |

Отдельно — механизм **registry-mirrors** у демона Docker и **hosts.toml** у containerd: это подмена адреса, по которому клиент ищет `docker.io`. Работает только для Docker Hub, для `cr.yandex` или `harbor` его не применяют.

## 3. Аналогия из жизни

Представь, что у тебя небольшая пекарня, а муку ты берёшь у заграничного поставщика, фура которого перестала заезжать в страну. Публичное зеркало облака — это посредник, у которого есть свой склад: он возит от поставщика и перепродаёт тебе под своим артикулом. Pull-through cache — это собственный склад у тебя во дворе: первый заказ всё равно приезжает от посредника, но дальше ты берёшь мешки со своего стеллажа и никого не ждёшь; при этом на мешке тот же заводской номер партии (digest), и подменить муку по дороге нельзя. Оффлайн-перенос — это когда ты сам едешь на границу с прицепом и везёшь мешки через блокпост.

Аналогия ломается в одном: у склада ограниченный объём, и старые мешки иногда выбрасывают (сборка мусора в кэше) — «я это уже качал» не гарантирует, что оно лежит на полке сейчас.

## 4. Практика и команды

**1. Посмотреть, какие зеркала уже настроены у демона Docker.**

```bash
docker info | sed -n '/Registry Mirrors/,+3p'
```

Типичный вывод:

```text
 Registry Mirrors:
  http://127.0.0.1:5000/
```

Если список пуст — демон ходит напрямую в Docker Hub. Именно эту секцию мы будем менять.

**2. Проверить, жив ли вообще Docker Hub с твоей машины.**

```bash
curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' https://registry-1.docker.io/v2/
curl -sS -o /dev/null -w '%{http_code}\n' https://auth.docker.io/token
```

Ожидаемо `401` — это норма (значит, дошли и получили запрос токена). Таймаут или `000` — сеть/блокировка. Важно: `401` != «всё хорошо», лимиты и запреты могут отдаваться и на `429`/`403`.

**3. Конфиг зеркала у демона Docker.**

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": ["http://127.0.0.1:5000"],
  "insecure-registries": ["127.0.0.1:5000", "10.10.10.55"],
  "max-concurrent-downloads": 5
}
EOF
sudo systemctl restart docker && docker info | grep -A2 'Registry Mirrors'
```

`registry-mirrors` — список URL, которые демон пробует вместо `docker.io` (по порядку). Требуется **рестарт** демона, не reload. `insecure-registries` разрешает HTTP и самоподписанный TLS — только для стенда.

**4. Поднять простейший pull-through cache на `registry:2`.**

```bash
docker run -d --name registry-mirror --restart unless-stopped \
  -p 5000:5000 \
  -e REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io \
  -e REGISTRY_PROXY_USERNAME= -e REGISTRY_PROXY_PASSWORD= \
  -v /srv/registry-mirror:/var/lib/registry \
  registry:2
docker logs --tail 20 registry-mirror
```

`REGISTRY_PROXY_REMOTEURL` превращает обычный registry в прокси: первый запрос уходит наверх, ответ кладётся на диск и отдаётся всем следующим. Логи покажут `response completed` с полем `remote_addr` — по нему видно, ходил ли он в интернет или отдал из кэша.

**5. Тянуть образ через префикс проекта Harbor.**

```bash
docker login 10.10.10.55 -u admin
docker pull 10.10.10.55/dockerhub-proxy/library/nginx:1.27
docker images | grep nginx
```

Формат: `<registry>/<project>/<repo>:<tag>`. Для Docker Hub-образов официальные (`library/nginx`) сохраняют префикс `library/`. Если тег не указан — берётся `latest`, так лучше не делать.

**6. Создать проект-прокси в Harbor через API (то же самое, что руками в UI).**

```bash
curl -sS -u admin:'Lab#2026-vektor' -X POST 'https://10.10.10.55/api/v2.0/projects' \
  -H 'Content-Type: application/json' -d '{
    "project_name": "dockerhub-proxy",
    "public": false,
    "registry_id": 1,
    "storage_limit": 53687091200
  }' -k
```

`registry_id: 1` — это заранее добавленный в Harbor внешний endpoint `https://registry-1.docker.io` (в UI: Administration → Registries → New Endpoint, provider «Docker Hub»). `storage_limit` в байтах — защита от «кэш съел весь диск».

**7. Сверить digest'ы: не подменили ли образ.**

```bash
skopeo inspect --raw docker://docker.io/library/nginx:1.27 | sha256sum
skopeo inspect --raw --tls-verify=false docker://10.10.10.55/dockerhub-proxy/library/nginx:1.27 | sha256sum
```

Два одинаковых хеша = байт-в-байт тот же manifest. Это правильный способ проверки, потому что digest манифеста и есть sha256 от его содержимого.

**8. Перенести образ в изолированный контур.**

```bash
skopeo copy --all docker://docker.io/library/postgres:16 docker-archive:/tmp/postgres16.tar
skopeo copy docker-archive:/tmp/postgres16.tar docker://10.10.10.50:5050/vektor/postgres:16
```

`--all` тащит все архитектуры (нужно, если в контуре есть и amd64, и arm64). Без `--all` — только текущую платформу, и на другой железке образ не запустится.

**9. Классика без skopeo — save/load.**

```bash
docker pull nginx:1.27 && docker save nginx:1.27 -o /tmp/nginx.tar
docker load -i /tmp/nginx.tar && docker images nginx
```

Просто, работает везде, но не сохраняет multi-arch и плохо переносится между разными версиями движка.

**10. Настройка containerd на нодах Kubernetes.**

```toml
# /etc/containerd/certs.d/docker.io/hosts.toml
server = "https://registry-1.docker.io"

[host."https://10.10.10.55/v2/dockerhub-proxy"]
  capabilities = ["pull", "resolve"]
```

Каталог `certs.d/<host>/hosts.toml` — штатный механизм containerd (1.5+) для зеркал и приватных реестров. Важная тонкость: у Harbor прокси живёт по пути `/v2/dockerhub-proxy`, поэтому в `host` указывается URL **с префиксом** — именно так Harbor подставляется как зеркало для containerd. Для демона Docker такой трюк не работает: у `registry-mirrors` нет понятия «префикс проекта», поэтому там либо `registry:2`, либо явный префикс в имени образа.

**11. Облачный реестр в Yandex Cloud.**

```bash
yc container registry create --name vektor-registry
yc container registry configure-docker          # настраивает credential helper для cr.yandex
docker tag nginx:1.27 cr.yandex/<registry-id>/nginx:1.27
docker push cr.yandex/<registry-id>/nginx:1.27
```

`configure-docker` прописывает в `~/.docker/config.json` helper `yc`, и `docker login` больше не нужен — токен берётся из CLI. Плюс: у Yandex Cloud есть **публичное зеркало Docker Hub** с префиксом `cr.yandex/mirror/...` `[ПРОВЕРИТЬ точный формат пути и актуальность в документации Yandex Cloud]` — им можно пользоваться вообще без своего реестра.

**12. Реестр в VK Cloud.**

```bash
docker login <адрес-реестра-из-панели> -u <логин> -p <пароль>
docker pull <адрес-реестра-из-панели>/vektor/nginx:1.27
```

Адрес и учётка выдаются в панели VK Cloud (раздел Container Registry) `[ПРОВЕРИТЬ актуальный вид домена реестра в документации VK Cloud]`. У VK Cloud есть и собственное зеркало Docker Hub `[ПРОВЕРИТЬ наличие и адрес]`. Смысл один: тот же OCI-реестр, просто чужой, с биллингом и SLA.

**13. Диагностика: что реально происходит при pull.**

```bash
docker pull nginx:1.27 2>&1 | tail -5
journalctl -u docker --since '-5 min' --no-pager | tail -30
docker logs --tail 50 registry-mirror | grep -i 'proxy\|error'
```

Смотри на текст ошибки: `i/o timeout` / `connection refused` — сеть; `429 Too Many Requests` — лимиты; `x509: certificate signed by unknown authority` — TLS/CA; `denied` — авторизация.

**14. Проверить digest локального образа.**

```bash
docker image inspect nginx:1.27 --format '{{index .RepoDigests 0}}'
```

Даст `nginx@sha256:...`. Сравнивай его со `sha256sum` от `skopeo inspect --raw` — это и есть твой контроль целостности в CI.

## 5. Лабораторная работа (пошаговый сценарий на виртуалке)

### Предусловия

- **HARBOR01** (10.10.10.55) — Harbor установлен в уроке 31, доступен по HTTPS, есть админ `admin` / `Lab#2026-vektor`.
- **UBU01** (10.10.10.30) — Ubuntu 24.04, Docker Engine, SSH.
- **GIT01** (10.10.10.50) — GitLab CE + runner из модуля CI/CD. Если ещё не поднят — шаг 8 пропусти.
- **k8s-cp01/w01/w02** — если кластер из модуля Kubernetes ещё не собран, шаг с `hosts.toml` делаешь «на вырост» на UBU01 в `/etc/containerd/certs.d/` без запуска пода.
- Windows-часть стенда не нужна.

Если в уроке 31 Harbor ставился на самоподписанном сертификате — на UBU01 в `/etc/docker/daemon.json` уже должен быть `insecure-registries` с `10.10.10.55`. Если нет — добавь.

### Снапшот

В VMware Workstation: правый клик по ВМ → Snapshot → Take Snapshot, имя `before-devops-032`. Сделать на **HARBOR01**, **UBU01**, **GIT01** (и на k8s-узлах, если трогаешь containerd).

```text
HARBOR01  → before-devops-032
UBU01     → before-devops-032
GIT01     → before-devops-032
```

### Шаги

1. **UBU01.** Зафиксируй исходное состояние: `docker info | sed -n '/Registry Mirrors/,+2p'` и `curl -sS -o /dev/null -w '%{http_code}\n' https://registry-1.docker.io/v2/`. Ожидаемо: список зеркал пуст, код `401` **или** таймаут (если из твоей сети Docker Hub недоступен). Запиши результат — это точка «до».

2. **UBU01.** Попробуй `docker pull nginx:1.27`. Ожидаемо: либо успех (тогда дальше ты строишь кэш «на будущее» и ради скорости), либо ошибка таймаута/`429`. В любом случае запомни текст ошибки.

3. **HARBOR01** (браузер, `https://10.10.10.55`). Administration → Registries → New Endpoint: Provider `Docker Hub`, URL `https://registry-1.docker.io`, без кредов (аноним). Ожидаемо: статус `Healthy`.

4. **HARBOR01.** Projects → New Project: имя `dockerhub-proxy`, Access Level `Private`, галочка `Proxy Cache` + endpoint из шага 3, quota 50 GiB. Ожидаемо: проект создан, в списке у него иконка прокси.

5. **UBU01.** `docker login 10.10.10.55 -u admin` (пароль `Lab#2026-vektor`). Ожидаемо: `Login Succeeded`.

6. **UBU01.** `docker pull 10.10.10.55/dockerhub-proxy/library/nginx:1.27`. Ожидаемо: слои качаются (первый раз — с Docker Hub через Harbor), образ появляется локально. В Harbor UI в проекте `dockerhub-proxy` появляется репозиторий `library/nginx` с артефактом `1.27`.

7. **UBU01.** Проверь кэш: `docker rmi 10.10.10.55/dockerhub-proxy/library/nginx:1.27` и снова pull. Ожидаемо: второй pull заметно быстрее (десятки секунд против минут), в логах Harbor (`/var/log/harbor/proxy_cache.log` внутри контейнера `harbor-core` или в `docker logs harbor-core`) видно, что upstream не дёргался `[ПРОВЕРИТЬ точное имя файла лога в вашей версии Harbor]`.

8. **GIT01.** В `.gitlab-ci.yml` проекта замени базовый образ:
   ```yaml
   image: 10.10.10.55/dockerhub-proxy/library/python:3.12-slim
   ```
   Закоммить и запусти pipeline. Ожидаемо: job стартует, образ тянется с Harbor. Если runner не доверяет самоподписанному сертификату — добавь CA в `/etc/docker/certs.d/10.10.10.55/ca.crt` на GIT01.

9. **UBU01.** Подними локальный pull-through на `registry:2` (команда из п.4 раздела 4), добавь `http://127.0.0.1:5000` в `registry-mirrors`, перезапусти Docker. Ожидаемо: `docker info` показывает зеркало.

10. **UBU01.** `docker rmi nginx:1.27 && docker pull nginx:1.27`. Ожидаемо: Docker пошёл не в интернет, а на `127.0.0.1:5000`; в `docker logs registry-mirror` запись о проксировании. Теперь `docker pull` работает даже при полностью недоступном Docker Hub (пока образ есть в кэше).

11. **UBU01.** Сверь digest: `sha256sum` от `skopeo inspect --raw` для `docker.io/library/nginx:1.27` и для копии из Harbor. Ожидаемо: хеши совпадают.

12. **k8s-w01** (если кластер есть). Создай `/etc/containerd/certs.d/docker.io/hosts.toml` из п.10 раздела 4, `systemctl restart containerd`, затем `kubectl run test --image=nginx:1.27 --restart=Never` и `kubectl describe pod test`. Ожидаемо: pod в `Running`, в событиях нет `Failed to pull image`.

13. **UBU01.** Допиши в `stand-log.md`: какие реестры, проекты, зеркала и адреса появились.

### Проверка

- [ ] `docker info` на UBU01 показывает `Registry Mirrors: http://127.0.0.1:5000/`
- [ ] `docker pull 10.10.10.55/dockerhub-proxy/library/nginx:1.27` проходит
- [ ] Повторный pull того же образа заметно быстрее первого
- [ ] В Harbor UI проект `dockerhub-proxy` содержит `library/nginx`
- [ ] Digest образа из Harbor совпадает с digest'ом из Docker Hub
- [ ] Pipeline на GIT01 собирается с базовым образом из Harbor
- [ ] Pod на k8s (если есть) стартует без ошибок pull
- [ ] Запись в `stand-log.md` сделана

### Сломай и почини

> **Пользователь:** `adm.student` (IT)
> **ВМ:** UBU01
> **Что написал:** «После обеда `docker pull nginx` стал падать: `Error response from daemon: Get "http://127.0.0.1:5000/v2/": dial tcp 127.0.0.1:5000: connect: connection refused`. До обеда работало. Ничего не трогал.»
> **Срочность:** высокая (не собирается CI)

**Как воспроизвести:** на UBU01 выполни `docker stop registry-mirror && docker rm registry-mirror`. Демон Docker остался с `registry-mirrors: ["http://127.0.0.1:5000"]`, но зеркала больше нет.

**Диагностика по порядку:**

1. `docker info | sed -n '/Registry Mirrors/,+2p'` — убеждаемся, что зеркало вообще настроено (да, `127.0.0.1:5000`). Значит, демон ходит в мир через локальный адрес.
2. `curl -v http://127.0.0.1:5000/v2/` — `connection refused`. Сервиса нет.
3. `docker ps -a --filter name=registry-mirror` — контейнера нет (или `Exited (1)`).
4. Если контейнер есть, но `Exited`: `docker logs registry-mirror` — чаще всего `permission denied` на volume `/srv/registry-mirror` или занятый порт 5000 (`ss -tlnp | grep 5000`).
5. Проверяем, что дело не в сети: `curl -sS -o /dev/null -w '%{http_code}\n' https://registry-1.docker.io/v2/` — если тоже плохо, проблема шире кэша.

**Решение:** поднять контейнер заново (`docker run -d ... registry:2` из п.4) с `--restart unless-stopped` и вернуть volume `/srv/registry-mirror`. Проверить `docker pull nginx:1.27`. Хорошая практика — не полагаться на единственное зеркало: добавить в `registry-mirrors` вторым адрес Harbor.

**Ответ пользователю:** «Причина — остановился локальный прокси-кэш реестра на UBU01, демон Docker был настроен ходить только через него. Поднял контейнер заново, добавил автозапуск и резервное зеркало на Harbor. Проверь сборку — должно проходить; если повторится, скажи, посмотрю логи и место на диске.»

**Закрытие заявки:** тип «сбой сервиса», root cause «контейнер registry-mirror без политики рестарта», preventive action «--restart unless-stopped + второй mirror + алерт на `docker ps` раз в 5 минут».

### Что изменилось на стенде

- **HARBOR01:** добавлен внешний endpoint `docker-hub` → `https://registry-1.docker.io`; создан проект-прокси `dockerhub-proxy` (private, quota 50 GiB); в нём кэшируется `library/nginx:1.27`.
- **UBU01:** в `/etc/docker/daemon.json` появились `registry-mirrors: ["http://127.0.0.1:5000"]` и `insecure-registries`; запущен контейнер `registry-mirror` (`registry:2`), данные в `/srv/registry-mirror`; выполнен `docker login 10.10.10.55`.
- **GIT01:** `.gitlab-ci.yml` использует базовый образ из Harbor; при необходимости добавлен CA в `/etc/docker/certs.d/10.10.10.55/`.
- **k8s-w01 (если есть):** `/etc/containerd/certs.d/docker.io/hosts.toml` с зеркалом на Harbor.
- Новых ВМ, адресов и учёток не появилось.

### Откат

VMware Workstation → правый клик по ВМ → Snapshot Manager → выбрать `before-devops-032` → **Revert**. Сделать на HARBOR01, UBU01, GIT01 (и k8s-узлах). После отката убедиться, что `docker info` на UBU01 снова без зеркал, а Harbor не содержит проекта `dockerhub-proxy`.

## 6. Ошибки новичков и безопасность

1. **`insecure-registries` на весь прод.** → любой, кто подменит DNS, отдаст тебе «свой» образ по HTTP. → Правильно: свой CA, `certs.d`, никакого HTTP вне стенда.
2. **Логин и пароль реестра прямо в `daemon.json` или в `.gitlab-ci.yml`.** → утечка через Git и через `docker info` коллегам. → Правильно: credential helper (`docker-credential-pass`, `yc`), CI-переменные masked/protected, robot-аккаунты с правами только на pull.
3. **`latest` вместо фиксированного тега и digest.** → из-за кэша и пересборок upstream'а у тебя на стенде и в проде разные бинарники. → Правильно: `nginx:1.27` в теге + digest в проде, обновление через PR.
4. **Один кэш на весь кластер без квот.** → Harbor/registry съедает диск, падает вместе с остальными сервисами. → Правильно: `storage_limit`, garbage collection по расписанию, мониторинг места на MON01.
5. **Доверие любому публичному зеркалу из интернета.** → неизвестный владелец может отдавать пересобранные образы (digest не сойдётся с upstream, но ты не проверяешь). → Правильно: только провайдеры с договором, проверка digest, Cosign-подпись из урока 31.
6. **Считать, что кэш «всегда спасёт».** → после GC или смены тега образ придётся тянуть из недоступного источника. → Правильно: заранее заливать в реестр критичные образы явным `skopeo copy`, а не надеяться на ленивый кэш.
7. **Анонимные pull в CI.** → лимиты Docker Hub и отсутствие аудита, кто что тянет. → Правильно: свой реестр как единственный источник, наружу — только через прокси с сервисной учёткой.

**Безопасность.** В боевой среде нельзя: держать реестр без TLS и без аутентификации; открывать `registry:2` наружу (у него нет ни RBAC, ни аудита); хранить в Git пароли от облачных реестров; отключать проверку подписи ради «чтобы работало». Всё, что мы делаем на стенде с `--tls-verify=false` и `insecure-registries`, — исключительно учебная мера.

## 7. Вопросы на собеседовании

**1. «Docker Hub из нашей сети недоступен. Как вы обеспечите доставку образов?»**
Ответ: три уровня. Быстро — публичное зеркало российского облака (Yandex Cloud `cr.yandex/mirror/...`, VK Cloud) или собственный реестр у провайдера. Правильно — свой pull-through cache (Harbor proxy cache или Nexus docker proxy) как единая точка входа, кэш наполняется по требованию, критичные образы предзаливаются `skopeo copy`. Для закрытого контура — оффлайн-перенос `skopeo copy docker-archive` с `--all` и проверкой digest. Плюс настраиваю клиентов: `registry-mirrors` для Docker, `hosts.toml` для containerd.
*Что хочет услышать:* что ты знаешь разницу между зеркалом-репликой, прокси-кэшем и оффлайн-переносом, и что у тебя есть план для air-gap.
*Красный флаг:* «настрою VPN и буду тянуть напрямую» — не решает задачу, нарушает политику и не масштабируется на CI.

**2. «Чем pull-through cache отличается от обычного зеркала?»**
Ответ: зеркало-реплика содержит заранее выбранный набор образов и синхронизируется по расписанию/триггеру (репликация Harbor, `skopeo sync`); ты явно решаешь, что там лежит. Pull-through cache ничего не хранит заранее: он прозрачно проксирует запрос наверх и кэширует ответ; набор образов определяется тем, что попросили. Плюс — не надо планировать; минус — первый pull зависит от доступности upstream, и кэш можно «вымыть» сборкой мусора.
*Что хочет услышать:* понимание, что кэш не гарантирует наличие образа.
*Красный флаг:* «это одно и то же».

**3. «Как убедиться, что зеркало не подсунуло другой образ?»**
Ответ: OCI-образ адресуется digest'ом — sha256 от манифеста. Сверяю `sha256sum` от `skopeo inspect --raw` для upstream и для зеркала: значения обязаны совпадать. В проде деплою по digest, а не по тегу, и дополнительно проверяю подпись Cosign (из урока 31). Если digest отличается — образ не мой, разбираюсь.
*Что хочет услышать:* что digest — это контракт, и что тег `latest` ему не равен.
*Красный флаг:* «сравню размер образа» / «посмотрю на название».

**4. «Как настроить Kubernetes-кластер на зеркало и приватный реестр?»**
Ответ: containerd конфигурируется через `/etc/containerd/certs.d/<registry-host>/hosts.toml`: `server` — основной адрес, `[host."..."]` — зеркала с `capabilities = ["pull", "resolve"]`; для Harbor-прокси указываю URL с префиксом проекта. Для приватного реестра — `imagePullSecrets` в namespace или ServiceAccount, либо авторизация на уровне ноды в `hosts.toml`. Сертификат своего CA кладу рядом в `ca.crt` и перезапускаю containerd.
*Что хочет услышать:* что ты знаешь, что в современных кластерах нет `dockershim`, и работаешь с containerd напрямую.
*Красный флаг:* «пропишу в `/etc/docker/daemon.json`» — на нодах без Docker это не работает.

**5. «Полностью изолированный контур, интернета нет вообще. Что делаете?»**
Ответ: строю пайплайн «на земле»: список нужных образов в Git, `skopeo copy --all` в `docker-archive` или `oci-archive`, перенос носителем, `skopeo copy` в локальный Harbor, подпись Cosign и проверка политики в кластере. Регулярно (раз в N недель) обновляю набор и фиксирую digest'ы в репозитории, чтобы обновление было воспроизводимым и ревьюируемым. Отдельно — зеркало баз Trivy и пакетов, иначе сканер в изоляции бесполезен.
*Что хочет услышать:* воспроизводимость, версионирование набора образов, а не «руками занёс».
*Красный флаг:* «поставлю прокси наружу» — в air-gap это невозможно по определению.

## 8. Источники (официальная документация)

- **docs.docker.com** — раздел «Registry» → статьи *Registry as a pull through cache* и *Registry mirror* (`registry-mirrors` в `dockerd`). Поиск: `docker registry as a pull through cache`, `dockerd registry-mirrors`.
- **docs.docker.com** — *Docker Hub* → *Usage and limits* (актуальные лимиты на pull). Поиск: `docker hub rate limits pull`.
- **goharbor.io** — *Administration* → *Proxy Cache* и раздел про репликацию. Поиск: `harbor proxy cache configuration`.
- **containerd.io** (github.com/containerd/containerd, docs/hosts.md) — конфигурация реестров через `certs.d/hosts.toml`. Поиск: `containerd hosts.toml registry mirrors`.
- **github.com/containers/skopeo** — README, разделы `skopeo copy` и `skopeo sync`. Поиск: `skopeo copy docker-archive`.
- **yandex.cloud/ru/docs** — *Container Registry*, раздел про зеркало Docker Hub и `yc container registry configure-docker`. Поиск: `yandex cloud container registry docker hub mirror`.
- **cloud.vk.com/docs** — *Container Registry* (адрес реестра, аутентификация, зеркало). Поиск: `vk cloud container registry docker login` `[ПРОВЕРИТЬ актуальный путь раздела]`.
- **opencontainers.org** — спецификация *OCI Distribution Specification*. Поиск: `OCI distribution spec v2 API`.

Всё, что касается точных адресов зеркал, лимитов и дат блокировок, проверяй по документации провайдера на день использования — эта часть рынка меняется быстрее, чем выходят учебники.
