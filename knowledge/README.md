# FWRouter Knowledge Map

Эта папка хранит постоянную карту проекта `fwrouter` для будущих Codex/AI-агентов и людей, которые будут менять control-plane, dataplane и boot-подъем системы.

Что читать в первую очередь:

1. [QUICK_START_FOR_AGENTS.md](/knowledge/QUICK_START_FOR_AGENTS.md)
2. [ARCHITECTURE.md](/knowledge/ARCHITECTURE.md)
3. [BOOT_FLOW.md](/knowledge/BOOT_FLOW.md)
4. [DATABASE_SCHEMA.md](/knowledge/DATABASE_SCHEMA.md)
5. [NETWORK_MODEL.md](/knowledge/NETWORK_MODEL.md)
6. [UI.md](/knowledge/UI.md), если меняется frontend
7. нужные файлы в [CODE_INDEX/README.md](/knowledge/CODE_INDEX/README.md)

Покрытие этой карты:

- control-plane backend `FastAPI` в `/opt/fwrouter-api`
- `systemd`-оркестрация сервисов и таймеров
- `mihomo` и `xray`
- static UI в `/opt/fwrouter-ui`
- `nftables`, `ip rule`, `ip route`, `sysctl`
- install/bootstrap/diagnostics scripts
- persistent config, generated artifacts, runtime state
- boot persistence и post-reboot проверки

Не включено подробно:

- содержимое `.venv`, кэш, бинарники, старые debug-артефакты
- тесты как дерево исполнения, кроме упоминания в обзорных документах
- устаревшие ad-hoc заметки, backup indexes, prompt drafts и временные логи

`knowledge/` считается канонической картой проекта, а не архивом переписки или снапшотов. Исторические `log-*`, backup indexes, prompt drafts и старые requirements drafts удаляются из этой папки, если их содержание уже перенесено в архитектуру, ADR, `CODE_INDEX` или troubleshooting-документы.
