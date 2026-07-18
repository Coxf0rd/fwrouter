# `/opt/fwrouter-api/fwrouter_api/adapters/scripts.py`

## Назначение

Allowlisted shell/script runner для backend. Устраняет произвольный shell execution и задает контракт именованных script actions.

## Важные классы

- `ScriptSpec`
- `ScriptResult`
- `ScriptRunner`

## Важные функции

- `run(script_name, extra_args=...)`
  Запускает только allowlisted script.

## Внешние зависимости

- shell scripts в `/usr/local/libexec/fwrouter`
- `host-services.py`
- `systemctl`

## Runtime/persistent state

- не хранит state, но запускает операции с сильными side effects

## Boot persistence relevance

Высокая. Startup recovery вызывает `dataplane_check`.

## Нюансы

- не расширять allowlist без понимания security и idempotency
- `dataplane_apply`/`dataplane_rollback` имеют увеличенный timeout, потому что full `nft -f` на больших domain/IP rulesets может занимать существенно дольше 20 секунд; короткий timeout опасен тем, что процесс можно оборвать после удаления owned table, но до успешного применения candidate
