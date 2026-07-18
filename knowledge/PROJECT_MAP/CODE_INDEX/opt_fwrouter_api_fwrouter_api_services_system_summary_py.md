# `/opt/fwrouter-api/fwrouter_api/services/system_summary.py`

## Назначение

Строит агрегированное DTO для `/api/v2/system/summary`.

## Важные функции

- `_backend_runtime_status(...)`
  Преобразует module states, warnings и schema status в общий backend status.

- `build_system_summary(schema_state=None)`
  Собирает:
  - core/bypass status
  - subject taxonomy notes и managed external ingress provider contracts
  - backend/database/runtime readiness
  - system subjects summary
  - warnings
  В актуальной реализации работает через short-TTL cache; это защищает UI от повторного тяжелого пересчета `runtime_enforcement` и `scoped_egress` при частом polling.

## Внешние зависимости

- settings
- schema summary
- core bypass
- runtime enforcement
- modules
- scoped egress runtime
- system subjects

## Runtime/persistent state

- только читает runtime/persistent metadata

## Boot persistence relevance

Средняя/высокая как high-level readiness endpoint.

## Нюансы

- warnings тут это уже productized signal для UI/API, а не raw implementation detail
- при анализе latency помнить, что `/api/v2/system/summary` тянет и high-level runtime summary, и backend readiness; без cache это один из самых дорогих API routes
