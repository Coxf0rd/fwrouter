# `/opt/fwrouter-api/fwrouter_api/routes/subjects.py`

## Назначение

API для subject inventory, details, alias updates, mode changes и `ui/whoami`.

## Важные endpoints

- `GET /api/v2/subjects`
- `GET /api/v2/subjects/{subject_id}`
- `GET /api/v2/ui/whoami`
  Определяет текущий LAN/Tailscale subject по IP и возвращает subject уже с `effective_state`. User UI использует этот endpoint вместо полного `/ui/clients`, чтобы не ждать тяжелую агрегацию всех клиентов.
- `PATCH /api/v2/subjects/{subject_id}/alias`
- `POST /api/v2/subjects/{subject_id}/mode`
  При `subject_id = xray-subscription:*` разворачивает синтетический UI aggregate ID в список реальных Xray `subject_id` и передает batch payload в apply mutation.
- `POST /api/v2/subjects/sync`

## Внешние зависимости

- subject policy
- subject group resolver для синтетических `xray-subscription:*` UI IDs
- subjects service
- job manager / apply mutation

## Runtime/persistent state

- mode changes и alias updates меняют persistent state
- sync endpoint создает inventory job

## Boot persistence relevance

Высокая. Subjects и их mode semantics должны переживать reboot.
