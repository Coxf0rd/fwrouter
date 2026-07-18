# FWRouter Knowledge

Эта папка начинается с пользовательских инструкций: установка, API, внешнее управление и диагностика.

## Пользовательские инструкции

1. [INSTALL_AND_DEPLOY.md](/knowledge/INSTALL_AND_DEPLOY.md) - установка, деплой и синхронизация в live.
2. [API_AND_CLI.md](/knowledge/API_AND_CLI.md) - основные API группы, CLI entrypoints и operational endpoints.
3. [EXTERNAL_MANAGEMENT.md](/knowledge/EXTERNAL_MANAGEMENT.md) - формат внешнего управления через API, attribution и ошибки валидации.
4. [EXTERNAL_INGRESS.md](/knowledge/EXTERNAL_INGRESS.md) - managed external ingress clients, сейчас Tailscale exit-node/LAN-like модель.
5. [TROUBLESHOOTING.md](/knowledge/TROUBLESHOOTING.md) - диагностика типовых проблем.

## Работа с проектом

Для разработки и для Codex/AI-агентов есть отдельная техническая карта: [PROJECT_MAP/README.md](/knowledge/PROJECT_MAP/README.md).

Архитектурная карта - это сжатое описание того, как устроены backend, dataplane, systemd, nftables, routing, mihomo/xray, UI и persistent state. Она нужна перед изменениями, чтобы не ломать инварианты системы.

`CODE_INDEX` - это навигационный индекс по важным файлам. Его используют, когда нужно быстро понять, какой service/route/script отвечает за конкретную часть поведения, не читая весь проект подряд.

Правило сопровождения: если меняется код, конфиг, API, install/deploy, systemd, nftables, policy routing, mihomo/xray, boot behavior или UI, точечно обновляй соответствующие документы в [PROJECT_MAP](/knowledge/PROJECT_MAP/) и, если изменение видно пользователю, один из файлов выше.
