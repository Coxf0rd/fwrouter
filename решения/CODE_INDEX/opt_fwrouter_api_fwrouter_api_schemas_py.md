# `/opt/fwrouter-api/fwrouter_api/schemas.py`

## Назначение

Общие Pydantic schemas для API.

## Важные функции

- `ApiResponse`
  Единый envelope `/api/v2`: `ok`, `data`, `error`.

## Внешние зависимости

- Pydantic

## Runtime/persistent state

State не читает и не пишет.

## Boot persistence relevance

Средняя. Это стабильный API contract для UI/scripts.

## Нюансы

- Route handlers должны возвращать данные внутри `data`, а не произвольный top-level payload.

