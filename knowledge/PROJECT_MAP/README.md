# Project Map

Техническая карта проекта для разработки и AI-агентов. Пользовательские инструкции лежат уровнем выше, в `/knowledge`.

## Что это такое

- Архитектурная карта описывает устройство системы и ее инварианты: backend, database, runtime state, dataplane, policy routing, nftables, systemd, mihomo/xray и UI.
- `CODE_INDEX` связывает важные файлы с их ответственностью. Это быстрый индекс для поиска нужного route/service/script перед изменением кода.
- `DECISIONS` хранит ADR: почему были выбраны ключевые архитектурные решения.

## Что читать перед изменениями

1. [QUICK_START_FOR_AGENTS.md](QUICK_START_FOR_AGENTS.md)
2. [ARCHITECTURE.md](ARCHITECTURE.md)
3. [BOOT_FLOW.md](BOOT_FLOW.md)
4. [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)
5. [NETWORK_MODEL.md](NETWORK_MODEL.md)
6. нужные файлы в [CODE_INDEX/README.md](CODE_INDEX/README.md)

## Правило обновления

Если меняется код, конфиг, systemd unit, nftables logic, policy routing, install script, API, CLI, mihomo/xray integration, UI или boot behavior, обновляй только затронутые документы в этой папке. Если изменение видно пользователю или внешнему интегратору, обновляй также соответствующий файл в `/knowledge`.
