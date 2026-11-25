# DevOps-инженер: оглавление


## МОДУЛЬ 0. Что такое DevOps и как им стать

- [1. Кто такой DevOps-инженер в 2026 году: обязанности, зарплатная вилка в Москве, отличие от сисадмина, SRE, Platform Engineer и разработчика](001-кто-такой-devops-инженер-в-2026-году-обязанности-з.md)
- [2. Культура и философия DevOps: CALMS, DORA-метрики, жизненный цикл поставки ПО, shift-left](002-культура-и-философия-devops-calms-dora-метрики-жиз.md)
- [3. Стенд для курса: схема лаборатории (VMware/Proxmox, GitLab, Kubernetes-кластер, мониторинг, реестр образов, облако) и план её поэтапного развёртывания](003-стенд-для-курса-схема-лаборатории-vmware-proxmox-g.md)
- [4. Рабочее место DevOps: Linux/WSL2, терминал, zsh, tmux, VS Code/Neovim, SSH-конфиги, менеджеры паролей](004-рабочее-место-devops-linux-wsl2-терминал-zsh-tmux-.md)
- [5. Как учиться DevOps без коммерческого опыта: pet-проекты, документация в Git, портфолио на GitHub/GitLab](005-как-учиться-devops-без-коммерческого-опыта-pet-про.md)

## МОДУЛЬ 1. Фундамент: Linux, сети, протоколы

- [6. Linux для DevOps: процессы, сигналы, права, namespaces, cgroups, файловые дескрипторы](006-linux-для-devops-процессы-сигналы-права-namespaces.md)
- [7. Продвинутая работа в shell: Bash-скрипты, pipefail, trap, getopts, jq, yq, sed, awk](007-продвинутая-работа-в-shell-bash-скрипты-pipefail-t.md)
- [8. systemd и управление сервисами: юниты, таймеры, зависимости, journald, sandboxing](008-systemd-и-управление-сервисами-юниты-таймеры-завис.md)
- [9. Сети для DevOps: TCP/IP, DNS, HTTP/1.1/2/3, TLS, NAT, маршрутизация, диагностика (ss, tcpdump, curl, dig)](009-сети-для-devops-tcp-ip-dns-http-1-1-2-3-tls-nat-ма.md)
- [10. Балансировка и прокси: Nginx, HAProxy, Envoy, L4 vs L7, health checks, sticky sessions](010-балансировка-и-прокси-nginx-haproxy-envoy-l4-vs-l7.md)
- [11. TLS и PKI на практике: сертификаты, цепочки, mTLS, Let's Encrypt, ACME, cert-manager](011-tls-и-pki-на-практике-сертификаты-цепочки-mtls-let.md)
- [12. SSH глубже: ключи, bastion/jump-хосты, туннели, ProxyJump, SSH CA](012-ssh-глубже-ключи-bastion-jump-хосты-туннели-proxyj.md)
- [13. Linux под нагрузкой: профилирование (perf, strace, ltrace), анализ памяти, диска, сети, OOM-killer](013-linux-под-нагрузкой-профилирование-perf-strace-ltr.md)

## МОДУЛЬ 2. Git, процессы разработки, стратегии релизов

- [14. Git глубже: rebase, cherry-pick, reflog, bisect, hooks, submodules, монорепо](014-git-глубже-rebase-cherry-pick-reflog-bisect-hooks-.md)
- [15. Ветвление и релизные процессы: Trunk-Based Development, GitFlow, GitHub Flow, семантическое версионирование](015-ветвление-и-релизные-процессы-trunk-based-developm.md)
- [16. Code review и качество: pull/merge request, линтеры, pre-commit, conventional commits, CODEOWNERS](016-code-review-и-качество-pull-merge-request-линтеры-.md)
- [17. GitLab self-hosted: установка, администрирование, runners, группы, права, бэкапы (актуальный выбор в РФ)](017-gitlab-self-hosted-установка-администрирование-run.md)
- [18. Gitea/Forgejo и российские аналоги: лёгкие самохостинговые Git-решения](018-gitea-forgejo-и-российские-аналоги-лёгкие-самохост.md)
- [19. Agile, Scrum и Kanban для инженера: спринты, эстимейты, работа с продуктовыми командами](019-agile-scrum-и-kanban-для-инженера-спринты-эстимейт.md)

## МОДУЛЬ 3. Языки автоматизации

- [20. Python для DevOps: venv, poetry/uv, работа с файлами, процессами, REST API, парсинг логов](020-python-для-devops-venv-poetry-uv-работа-с-файлами-.md)
- [21. Python: написание CLI-утилит, boto3-подобные SDK, тесты (pytest), упаковка](021-python-написание-cli-утилит-boto3-подобные-sdk-тес.md)
- [22. Go для инфраструктуры: основы языка, горутины, сборка бинарников, написание exporter'ов и операторов](022-go-для-инфраструктуры-основы-языка-горутины-сборка.md)
- [23. YAML, JSON, TOML, HCL, Jsonnet: форматы конфигураций и работа с ними (yq, jq, kustomize)](023-yaml-json-toml-hcl-jsonnet-форматы-конфигураций-и-.md)
- [24. Регулярные выражения, шаблонизация (Jinja2, Go templates), генерация конфигураций](024-регулярные-выражения-шаблонизация-jinja2-go-templa.md)
- [25. Make, Taskfile, Just: автоматизация рутинных операций проекта](025-make-taskfile-just-автоматизация-рутинных-операций.md)

## МОДУЛЬ 4. Контейнеризация

- [26. Контейнеры под капотом: namespaces, cgroups, overlayfs, runc, containerd, OCI](026-контейнеры-под-капотом-namespaces-cgroups-overlayf.md)
- [27. Docker: образы, слои, Dockerfile, multi-stage сборка, кэширование, оптимизация размера](027-docker-образы-слои-dockerfile-multi-stage-сборка-к.md)
- [28. Docker Compose: многоконтейнерные приложения, сети, тома, healthchecks, профили](028-docker-compose-многоконтейнерные-приложения-сети-т.md)
- [29. Podman, Buildah, Kaniko: бесдемонная сборка и rootless-контейнеры](029-podman-buildah-kaniko-бесдемонная-сборка-и-rootles.md)
- [30. Безопасность контейнеров: non-root, distroless, read-only FS, capabilities, seccomp, Trivy/Grype](030-безопасность-контейнеров-non-root-distroless-read-.md)
- [31. Registry образов: Harbor, GitLab Container Registry, Nexus — установка, репликация, политики, подпись (Cosign)](031-registry-образов-harbor-gitlab-container-registry-.md)
- [32. Контейнеры в РФ-реалиях: зеркала Docker Hub, Yandex Container Registry, VK Cloud, локальные прокси-кэши](032-контейнеры-в-рф-реалиях-зеркала-docker-hub-yandex-.md)

## МОДУЛЬ 5. Kubernetes

- [33. Архитектура Kubernetes: control plane, etcd, kubelet, kube-proxy, CRI/CNI/CSI](033-архитектура-kubernetes-control-plane-etcd-kubelet-.md)
- [34. Развёртывание кластера: kubeadm, k3s, kind, minikube, на VMware/Proxmox в лаборатории](034-развёртывание-кластера-kubeadm-k3s-kind-minikube-н.md)
- [35. Основные объекты: Pod, Deployment, StatefulSet, DaemonSet, Job, CronJob, Service, Ingress](035-основные-объекты-pod-deployment-statefulset-daemon.md)
- [36. Конфигурация и секреты: ConfigMap, Secret, Sealed Secrets, External Secrets, SOPS](036-конфигурация-и-секреты-configmap-secret-sealed-sec.md)
- [37. Сети в Kubernetes: CNI (Calico, Cilium, Flannel), NetworkPolicy, Service Mesh (обзор)](037-сети-в-kubernetes-cni-calico-cilium-flannel-networ.md)
- [38. Ingress и Gateway API: Nginx Ingress, Traefik, Envoy Gateway, cert-manager](038-ingress-и-gateway-api-nginx-ingress-traefik-envoy-.md)
- [39. Хранилище: PV/PVC, StorageClass, CSI, Longhorn, Ceph/Rook, NFS](039-хранилище-pv-pvc-storageclass-csi-longhorn-ceph-ro.md)
- [40. Планирование и ресурсы: requests/limits, QoS, affinity, taints/tolerations, HPA/VPA, PodDisruptionBudget](040-планирование-и-ресурсы-requests-limits-qos-affinit.md)
- [41. RBAC и безопасность кластера: ServiceAccount, Pod Security Standards, OPA/Kyverno, Falco](041-rbac-и-безопасность-кластера-serviceaccount-pod-se.md)
- [42. Helm: чарты, values, шаблоны, репозитории, тестирование, helmfile](042-helm-чарты-values-шаблоны-репозитории-тестирование.md)
- [43. Kustomize и управление конфигурациями окружений](043-kustomize-и-управление-конфигурациями-окружений.md)
- [44. Операторы и CRD: принцип работы, kubebuilder/operator-sdk (введение)](044-операторы-и-crd-принцип-работы-kubebuilder-operato.md)
- [45. Диагностика Kubernetes: kubectl debug, события, логи, CrashLoopBackOff, ImagePullBackOff, OOMKilled](045-диагностика-kubernetes-kubectl-debug-события-логи-.md)
- [46. Обновление и обслуживание кластера: версии, drain/cordon, бэкап etcd, DR](046-обновление-и-обслуживание-кластера-версии-drain-co.md)
- [47. Российские Kubernetes-платформы: Deckhouse, Яндекс Managed Kubernetes, VK Cloud Containers, Cloud.ru — особенности и выбор](047-российские-kubernetes-платформы-deckhouse-яндекс-m.md)

## МОДУЛЬ 6. CI/CD

- [48. Принципы CI/CD: непрерывная интеграция, доставка и развёртывание, артефакты, окружения](048-принципы-ci-cd-непрерывная-интеграция-доставка-и-р.md)
- [49. GitLab CI/CD: .gitlab-ci.yml, stages, runners, cache, artifacts, include, rules, environments](049-gitlab-ci-cd-gitlab-ci-yml-stages-runners-cache-ar.md)
- [50. GitHub Actions и Jenkins: сравнение подходов, shared libraries, Jenkinsfile, self-hosted runners](050-github-actions-и-jenkins-сравнение-подходов-shared.md)
- [51. Сборка и тестирование: матрицы, параллелизм, линтеры, unit/integration/e2e в пайплайне](051-сборка-и-тестирование-матрицы-параллелизм-линтеры-.md)
- [52. Менеджеры артефактов: Nexus, Artifactory, GitLab Package Registry — Maven, npm, PyPI, Docker, Helm](052-менеджеры-артефактов-nexus-artifactory-gitlab-pack.md)
- [53. Безопасность пайплайна (DevSecOps): SAST, DAST, SCA, secret scanning, SBOM, подпись артефактов](053-безопасность-пайплайна-devsecops-sast-dast-sca-sec.md)
- [54. Стратегии деплоя: Rolling, Blue/Green, Canary, Feature Flags, A/B, автоматический откат](054-стратегии-деплоя-rolling-blue-green-canary-feature.md)
- [55. GitOps: ArgoCD и Flux — декларативное развёртывание, синхронизация, управление окружениями](055-gitops-argocd-и-flux-декларативное-развёртывание-с.md)
- [56. Progressive Delivery: Argo Rollouts, Flagger, анализ метрик при выкатке](056-progressive-delivery-argo-rollouts-flagger-анализ-.md)
- [57. Мультиокружения: dev/stage/prod, promote-политики, управление конфигурациями и секретами](057-мультиокружения-dev-stage-prod-promote-политики-уп.md)
- [58. Производительность пайплайнов: кэширование, DinD vs Kaniko, ускорение сборок, FinOps CI](058-производительность-пайплайнов-кэширование-dind-vs-.md)

## МОДУЛЬ 7. Infrastructure as Code

- [59. Принципы IaC: декларативный и императивный подходы, идемпотентность, дрейф конфигурации](059-принципы-iac-декларативный-и-императивный-подходы-.md)
- [60. Ansible: инвентари, роли, коллекции, Vault, тестирование (Molecule), AWX/Semaphore](060-ansible-инвентари-роли-коллекции-vault-тестировани.md)
- [61. Terraform/OpenTofu: провайдеры, state, remote backend, модули, workspaces, import](061-terraform-opentofu-провайдеры-state-remote-backend.md)
- [62. Terraform на практике: Yandex Cloud, VK Cloud, Selectel, Proxmox, VMware vSphere](062-terraform-на-практике-yandex-cloud-vk-cloud-select.md)
- [63. Packer и Cloud-Init: golden images, автоматическая сборка образов ВМ](063-packer-и-cloud-init-golden-images-автоматическая-с.md)
- [64. Pulumi и CDK: IaC на языках программирования (обзор)](064-pulumi-и-cdk-iac-на-языках-программирования-обзор.md)
- [65. Тестирование и политика-как-код: terratest, tflint, checkov, OPA/Conftest, Sentinel-подход](065-тестирование-и-политика-как-код-terratest-tflint-c.md)
- [66. Управление секретами в IaC: Vault, SOPS, age, External Secrets Operator, Lockbox](066-управление-секретами-в-iac-vault-sops-age-external.md)
- [67. Crossplane и платформенный подход: управление инфраструктурой через Kubernetes API](067-crossplane-и-платформенный-подход-управление-инфра.md)

## МОДУЛЬ 8. Облака и гибридная инфраструктура

- [68. Основы облаков: IaaS/PaaS/SaaS, регионы, зоны, модель ответственности, FinOps](068-основы-облаков-iaas-paas-saas-регионы-зоны-модель-.md)
- [69. Яндекс Облако: Compute, VPC, Managed Kubernetes, Object Storage, IAM, Managed DB](069-яндекс-облако-compute-vpc-managed-kubernetes-objec.md)
- [70. VK Cloud, Selectel, Cloud.ru: сравнение сервисов, цены, особенности, миграция между провайдерами](070-vk-cloud-selectel-cloud-ru-сравнение-сервисов-цены.md)
- [71. Основы AWS/GCP/Azure: ключевые сервисы и аналоги в российских облаках (для понимания рынка и документации)](071-основы-aws-gcp-azure-ключевые-сервисы-и-аналоги-в-.md)
- [72. Сети в облаке: VPC, подсети, NAT, VPN, peering, Load Balancer, WAF, DDoS-защита](072-сети-в-облаке-vpc-подсети-nat-vpn-peering-load-bal.md)
- [73. Облачное хранение: S3-совместимые хранилища, политики жизненного цикла, шифрование, версионирование](073-облачное-хранение-s3-совместимые-хранилища-политик.md)
- [74. Serverless и функции: Cloud Functions, API Gateway, события, ограничения и применимость](074-serverless-и-функции-cloud-functions-api-gateway-с.md)
- [75. Гибридные и мультиоблачные схемы: on-premise + облако, миграция, отказоустойчивость](075-гибридные-и-мультиоблачные-схемы-on-premise-облако.md)
- [76. FinOps: учёт затрат, теги, бюджеты, оптимизация ресурсов, rightsizing, spot/preemptible](076-finops-учёт-затрат-теги-бюджеты-оптимизация-ресурс.md)

## МОДУЛЬ 9. Мониторинг, логи и наблюдаемость

- [77. Три столпа наблюдаемости: метрики, логи, трейсы, события, SLI/SLO/SLA, error budget](077-три-столпа-наблюдаемости-метрики-логи-трейсы-событ.md)
- [78. Prometheus: архитектура, PromQL, экспортеры, service discovery, recording/alerting rules](078-prometheus-архитектура-promql-экспортеры-service-d.md)
- [79. Grafana: дашборды, переменные, алерты, provisioning как код, Grafana Operator](079-grafana-дашборды-переменные-алерты-provisioning-ка.md)
- [80. Alertmanager и политики оповещений: маршрутизация, подавление, эскалации, on-call (Telegram, PagerDuty-аналоги)](080-alertmanager-и-политики-оповещений-маршрутизация-п.md)
- [81. Масштабирование метрик: VictoriaMetrics, Thanos, Mimir, долгосрочное хранение, федерация](081-масштабирование-метрик-victoriametrics-thanos-mimi.md)
- [82. Централизованные логи: Loki, ELK/OpenSearch, Graylog, Vector/Fluent Bit — сбор, парсинг, ретеншн](082-централизованные-логи-loki-elk-opensearch-graylog-.md)
- [83. Трейсинг: OpenTelemetry, Jaeger, Tempo — инструментирование и анализ задержек](083-трейсинг-opentelemetry-jaeger-tempo-инструментиров.md)
- [84. Мониторинг Kubernetes: kube-state-metrics, node-exporter, cAdvisor, kube-prometheus-stack](084-мониторинг-kubernetes-kube-state-metrics-node-expo.md)
- [85. Блэкбокс-мониторинг и синтетика: blackbox exporter, uptime-проверки, SLO-дашборды](085-блэкбокс-мониторинг-и-синтетика-blackbox-exporter-.md)
- [86. Zabbix в гибридной среде: когда он лучше Prometheus и как их сочетать](086-zabbix-в-гибридной-среде-когда-он-лучше-prometheus.md)

## МОДУЛЬ 10. SRE-практики и надёжность

- [87. Основы SRE: SLI/SLO, error budget, toil, автоматизация, надёжность как продукт](087-основы-sre-sli-slo-error-budget-toil-автоматизация.md)
- [88. Управление инцидентами: роли, severity, коммуникации, war room, постмортемы без поиска виноватых](088-управление-инцидентами-роли-severity-коммуникации-.md)
- [89. On-call: ротации, runbook'и, снижение шума алертов, эскалации, MTTD/MTTR](089-on-call-ротации-runbook-и-снижение-шума-алертов-эс.md)
- [90. Capacity planning и нагрузочное тестирование: k6, Locust, JMeter, поиск узких мест](090-capacity-planning-и-нагрузочное-тестирование-k6-lo.md)
- [91. Chaos Engineering: Chaos Mesh, Litmus, game days, проверка отказоустойчивости](091-chaos-engineering-chaos-mesh-litmus-game-days-пров.md)
- [92. Disaster Recovery и бэкапы: Velero, Restic, Kopia, регулярные учения по восстановлению](092-disaster-recovery-и-бэкапы-velero-restic-kopia-рег.md)
- [93. Отказоустойчивые архитектуры: репликация БД, кворум, split-brain, multi-AZ, graceful degradation](093-отказоустойчивые-архитектуры-репликация-бд-кворум-.md)
- [94. Управление изменениями и релизами: change management, feature flags, freeze-периоды, rollback-планы](094-управление-изменениями-и-релизами-change-managemen.md)

## МОДУЛЬ 11. Безопасность (DevSecOps)

- [95. Безопасность в жизненном цикле разработки: threat modeling, security by design, shift-left](095-безопасность-в-жизненном-цикле-разработки-threat-m.md)
- [96. Секреты и доступы: HashiCorp Vault (и аналоги), динамические секреты, PKI, ротация](096-секреты-и-доступы-hashicorp-vault-и-аналоги-динами.md)
- [97. Supply Chain Security: SBOM, SLSA, подпись Cosign, зависимости, зеркала репозиториев](097-supply-chain-security-sbom-slsa-подпись-cosign-зав.md)
- [98. Сканирование: Trivy, Grype, Semgrep, SonarQube, Gitleaks, OWASP ZAP в пайплайне](098-сканирование-trivy-grype-semgrep-sonarqube-gitleak.md)
- [99. Безопасность Kubernetes: CIS Benchmarks, kube-bench, Pod Security, NetworkPolicy, runtime-защита](099-безопасность-kubernetes-cis-benchmarks-kube-bench-.md)
- [100. Zero Trust, mTLS и Service Mesh: Istio/Linkerd (обзор), идентичность сервисов](100-zero-trust-mtls-и-service-mesh-istio-linkerd-обзор.md)
- [101. Нормативные требования РФ: 152-ФЗ, 187-ФЗ, ФСТЭК, сертификация российских средств, ГОСТ-криптография в CI/CD](101-нормативные-требования-рф-152-фз-187-фз-фстэк-серт.md)
- [102. Импортозамещение в DevOps-стеке: GitLab, Harbor, Vault-аналоги, отечественные облака, Deckhouse, риски и планы миграции](102-импортозамещение-в-devops-стеке-gitlab-harbor-vaul.md)

## МОДУЛЬ 12. Платформенная инженерия и продвинутые темы

- [103. Platform Engineering: внутренняя платформа разработчика (IDP), Backstage, золотые пути, self-service](103-platform-engineering-внутренняя-платформа-разработ.md)
- [104. Service Mesh на практике: Istio/Linkerd/Cilium Mesh — трафик, наблюдаемость, безопасность](104-service-mesh-на-практике-istio-linkerd-cilium-mesh.md)
- [105. Message brokers для DevOps: Kafka, RabbitMQ, NATS — установка, мониторинг, типовые проблемы](105-message-brokers-для-devops-kafka-rabbitmq-nats-уст.md)
- [106. Базы данных в эксплуатации: PostgreSQL (Patroni, pgBouncer), Redis, ClickHouse, MongoDB — бэкапы, репликация, оператор в K8s](106-базы-данных-в-эксплуатации-postgresql-patroni-pgbo.md)
- [107. Edge и CDN: кэширование, WAF, балансировка трафика, Anycast, DDoS-защита](107-edge-и-cdn-кэширование-waf-балансировка-трафика-an.md)
- [108. Архитектура приложений для DevOps: монолит vs микросервисы, 12-factor app, API gateway, event-driven](108-архитектура-приложений-для-devops-монолит-vs-микро.md)
- [109. Основы eBPF: Cilium, Pixie, bpftrace — продвинутая диагностика и наблюдаемость](109-основы-ebpf-cilium-pixie-bpftrace-продвинутая-диаг.md)
- [110. AI/ML в инженерной практике: MLOps основы, LLM-инструменты для DevOps, автоматизация рутины](110-ai-ml-в-инженерной-практике-mlops-основы-llm-инстр.md)

## МОДУЛЬ 13. Soft skills и карьера

- [111. Коммуникация с разработчиками, QA, продуктом и бизнесом: как объяснять и обосновывать технические решения](111-коммуникация-с-разработчиками-qa-продуктом-и-бизне.md)
- [112. Документирование: ADR, runbook'и, диаграммы (C4, Mermaid), онбординг-гайды](112-документирование-adr-runbook-и-диаграммы-c4-mermai.md)
- [113. Управление приоритетами, дежурствами и профилактика выгорания](113-управление-приоритетами-дежурствами-и-профилактика.md)
- [114. Наставничество, ревью, развитие инженерной культуры в команде](114-наставничество-ревью-развитие-инженерной-культуры-.md)
- [115. Английский для DevOps: документация, Stack Overflow, issue'ы, собеседования, технические диалоги](115-английский-для-devops-документация-stack-overflow-.md)

## МОДУЛЬ 14. Комплексные проекты и трудоустройство

- [116. Проект №1: Полный CI/CD — приложение на GitLab → сборка → Harbor → деплой в Kubernetes через ArgoCD](116-проект-1-полный-ci-cd-приложение-на-gitlab-сборка-.md)
- [117. Проект №2: IaC-платформа — Terraform + Ansible + Packer, развёртывание инфраструктуры в облаке и на on-premise одной командой](117-проект-2-iac-платформа-terraform-ansible-packer-ра.md)
- [118. Проект №3: Production-grade Kubernetes — HA-кластер, Ingress, cert-manager, мониторинг, логи, бэкапы, Network Policies](118-проект-3-production-grade-kubernetes-ha-кластер-in.md)
- [119. Проект №4: Наблюдаемость — Prometheus/Grafana/Loki/Tempo, SLO-дашборды, алерты, on-call-процесс](119-проект-4-наблюдаемость-prometheus-grafana-loki-tem.md)
- [120. Проект №5: DevSecOps — пайплайн со сканированием уязвимостей, Vault, подписью образов, политиками безопасности](120-проект-5-devsecops-пайплайн-со-сканированием-уязви.md)
- [121. Проект №6: Миграция монолита в контейнеры и Kubernetes с zero-downtime и canary-выкаткой](121-проект-6-миграция-монолита-в-контейнеры-и-kubernet.md)
- [122. Проект №7: Disaster Recovery — полное восстановление инфраструктуры из кода и бэкапов](122-проект-7-disaster-recovery-полное-восстановление-и.md)
- [123. Резюме и портфолио DevOps: GitHub/GitLab, README проектов, схемы, метрики, кейсы](123-резюме-и-портфолио-devops-github-gitlab-readme-про.md)
- [124. Подготовка к собеседованию: теоретические вопросы, live-coding, system design, troubleshooting-кейсы](124-подготовка-к-собеседованию-теоретические-вопросы-l.md)
- [125. Сборник вопросов с собеседований DevOps/SRE: 200+ вопросов с ответами по всем модулям](125-сборник-вопросов-с-собеседований-devops-sre-200-во.md)
- [126. Сертификации и обучение: CKA/CKAD/CKS, RHCSA/RHCE, Terraform Associate, отечественные курсы — что ценится в РФ](126-сертификации-и-обучение-cka-ckad-cks-rhcsa-rhce-te.md)
- [127. Карьерный трек: Junior DevOps → Middle → Senior → SRE/Platform Engineer → Tech Lead/Architect, план на 3-5 лет](127-карьерный-трек-junior-devops-middle-senior-sre-pla.md)
