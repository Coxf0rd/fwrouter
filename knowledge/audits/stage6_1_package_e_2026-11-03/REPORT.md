# Stage 6.1 Package E — Schema Lifecycle Retirement

Status: historical source acceptance; no merge or deployment is implied. Current PR qualification and remaining gates are tracked in the owner-local canonical roadmap.

Schema support is exactly version 27. Existing files are validated without mutation before writer setup; only canonical and two exact known legacy-27 profiles are supported. Unsupported, corrupt, or unknown structures fail closed without repair. Fresh initialization is absent-file-only and no-replace; historical migration execution is retired. See [database schema contract](../../PROJECT_MAP/DATABASE_SCHEMA.md) and [canonical SQL](../../../backend/fwrouter_api/db/schema.sql).

The [schema-open benchmark method](benchmark_schema_open.py) and [paired measurements](schema-open-benchmark.json) preserve the reproducible source evidence. Historical migration behavior and exact legacy-profile limits remain recorded in Git history and the project map; this summary is navigation, not a new acceptance receipt.
