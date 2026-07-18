# `/opt/fwrouter-api/fwrouter_api/routes/rules.py`

## Назначение

API для manual/effective rules, validation и full update jobs.

## Важные endpoints

- `GET /api/v2/rules`
- `GET /api/v2/rules/summary`
- `GET /api/v2/rules/effective`
- `POST /api/v2/rules/manual/validate`
- `POST /api/v2/rules/manual`
- `POST /api/v2/rules/manual/apply`
- `POST /api/v2/rules/full-update`
- `GET /api/v2/rules/jobs/{job_id}`

## Внешние зависимости

- rules service
- runtime enforcement state
- apply orchestrator
- jobs state

## Runtime/persistent state

- может менять manual rules draft/active state и запускать full update

## Boot persistence relevance

Высокая. Effective rules artifact влияет на selective/VPN enforcement после reboot.

## Нюансы

- `GET /rules` остается полным diagnostic endpoint и читает большие active/effective artifacts.
- `GET /rules/summary` предназначен для UI settings rules pane: возвращает `state`, `rules_metadata`, configured sources и manual draft/active text без чтения `big_vpn_text` и `effective-rules.json`.
