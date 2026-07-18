# `/opt/fwrouter-api/fwrouter_api/services/subject_inventory.py`

## Назначение

Собирает inventory subjects из Docker, Tailscale, host services, Xray и дополнительных источников, затем синхронизирует их в DB.

## Важные классы

- `SubjectInventoryRecord`
  Нормализованная запись inventory subject.

## Важные функции

- `_extract_docker_records(result)`
  Строит `docker:*` subjects из `docker ps`.

- `_tailscale_peer_records(payload, include_all_peers=...)`
  Строит `tailscale_node:*` subjects.

- inventory extract/sync functions
  Преобразуют stdout script adapters в canonical subject records.

- `sync_subject_inventory(...)`
  Главный orchestration entrypoint.

## Внешние зависимости

- script runner (`docker_ps`, host/tailscale collectors)
- Xray adapter
- DB

## Runtime/persistent state

- обновляет subject tables и detail tables

## Boot persistence relevance

Средняя. Не обязателен для самого boot, но влияет на корректную post-boot subject model.

## Нюансы

- `tailscale_node` это canonical subject type, legacy `tailscale` нормализуется отдельно
- стабильные subject IDs нельзя ломать без миграционного плана
- docker compose identity нормализуется как `docker:<project>:<service>`, а не через склейку `project-service`
- по умолчанию auto-import Tailscale subjects берёт только routed peers; overlay-only peers добавляются только при `include_all_tailscale_peers=true`
- inventory refresh не должен перетирать persisted `subjects.desired_mode`: default mode допустим только на initial insert, иначе LAN/Tailscale client после resync silently откатывается из user-selected `selective` обратно в `global`
- inventory refresh не должен перетирать ручной `subjects.alias`: discovery может обновлять `display_name`/detail, но alias сохраняется, чтобы переименование LAN/Tailscale клиентов переживало последующие sync
