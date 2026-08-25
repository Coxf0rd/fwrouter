# `/opt/fwrouter-api/fwrouter_api/services/mihomo_config_status.py`

## Назначение

Status/read-only helpers для Mihomo config: structural fingerprints, config file status summaries и cheap runtime satisfaction check.

## Runtime/persistent state

- читает base/candidate config paths
- читает Mihomo controller health через adapter
- state не пишет

## Нюансы

- `mihomo_runtime_satisfies_routing` намеренно дешевый: проверяет metadata, selectors и transparent listener readiness без генерации большого candidate YAML.
- При любой неопределенности возвращает `ok=False`, чтобы caller мог уйти в полный reconcile path.
