# `/opt/fwrouter-api/fwrouter_api/services/runtime_convergence.py`

## Назначение

Отдельный runtime self-heal слой для selective/VPN dataplane contract. Не занимается выбором VPN-сервера и не анализирует трафик.

## Важные функции

- `run_runtime_convergence_check(...)`
  Проверяет, нужен ли active selective/VPN runtime contract. Если нужен, вызывает штатные repair entrypoints:
  - `reconcile_dnsmasq_rules()` для managed dnsmasq config, DNS capture и nftset materialization;
  - `reconcile_current_routing_if_drift()` для live `inet fwrouter_v2` marker/artifact drift.
  Результат кэшируется коротким TTL и сохраняется как last status для других сервисов.

- `get_last_runtime_convergence_status(...)`
  Read-only API для потребителей вроде watchdog. Не чинит runtime, только возвращает последний известный статус или `not_checked`.

## Внешние зависимости

- `services/dnsmasq.py`
- `services/apply_orchestrator.py`
- routing state в SQLite
- `subject_policy` для scoped `lan`/`tailscale_node` VPN/selective subjects
- operational/technical logs

## Runtime/persistent state

- держит last result в памяти backend process;
- live repairs идут только через существующие dnsmasq/apply-orchestrator paths;
- SQLite routing intent напрямую не меняет, кроме изменений, которые выполняет обычный apply pipeline при подтвержденном drift.

## Boot persistence relevance

Средняя. Scheduler стартует внутри backend lifecycle и закрывает runtime drift быстрее ежедневного maintenance.

## Нюансы

- Это граница ответственности для runtime repair. Watchdog не должен напрямую вызывать `reconcile_dnsmasq_rules()` или drift reapply.
- `not_checked` не считается hard failure: после backend startup watchdog не должен ломать auto logic только потому, что первый convergence tick еще не успел записать результат.
- Ошибки self-heal логируются здесь, а не в watchdog.
