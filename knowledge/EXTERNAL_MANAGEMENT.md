# External Management Contract

FWRouter API supports external management clients: local automation, voice bridges, bots, dashboards, and CLI wrappers. The backend does not know integration-specific details; it accepts only a generic attribution model.

## Base Fields

Mutating requests may include:

- `requested_by`
  Opaque source string. Recommended format for external clients: `external_client:<client_name>`.
- `management_context`
  Object with additional attribution fields:
  - `source_type`: source type, usually `external_client`
  - `client_name`: management client name
  - `channel`: invocation channel, for example `local_api`, `voice`, `bot`, `webhook`
  - `actor`: user/subject, if known
  - `action`: concrete client action
  - `request_id`: correlation id, if the client can provide one

For `external_client`, `client_name` and `action` are required. If they are missing, the endpoint returns an error before executing the action.

## Incomplete Context Error

```json
{
  "ok": false,
  "data": {
    "management_attribution": {
      "requested_by": "external_client",
      "source_type": null,
      "client_name": null,
      "channel": null,
      "actor": null,
      "action": null,
      "request_id": null,
      "attribution_complete": false,
      "attribution_missing": ["client_name", "action"]
    }
  },
  "error": {
    "code": "MANAGEMENT_ATTRIBUTION_INCOMPLETE",
    "message": "External management request is missing required attribution fields.",
    "missing_fields": ["client_name", "action"]
  }
}
```

## Example: Select The Best `vpn-auto` Server

```bash
curl -sS -X POST http://127.0.0.1:5500/api/v2/selector/vpn-auto/switch   -H 'Content-Type: application/json'   -d '{
    "confirm_switch": true,
    "requested_by": "external_client:my-automation",
    "management_context": {
      "source_type": "external_client",
      "client_name": "my-automation",
      "channel": "local_api",
      "action": "switch_best_vpn_auto_server",
      "actor": "operator"
    }
  }'
```

## Example: Select A Global Fixed Server

```bash
curl -sS -X POST http://127.0.0.1:5500/api/v2/routing/global/fixed-server   -H 'Content-Type: application/json'   -d '{
    "server_id": "server-id-or-name-from-inventory",
    "confirm_switch": true,
    "requested_by": "external_client:my-automation",
    "management_context": {
      "source_type": "external_client",
      "client_name": "my-automation",
      "channel": "voice",
      "action": "set_global_fixed_server",
      "actor": "operator"
    }
  }'
```

Global fixed server has a backend TTL of 24 hours. The TTL is stored in FWRouter state, not in the external client.

## Example: Reset Global Fixed Server To Auto

The `DELETE` endpoint accepts context through query parameters:

```bash
curl -sS -X DELETE   'http://127.0.0.1:5500/api/v2/routing/global/fixed-server?confirm_switch=true&requested_by=external_client:my-automation&management_client_name=my-automation&management_channel=local_api&management_action=clear_global_fixed_server&management_actor=operator'
```

## Example: Change Global Mode

```bash
curl -sS -X POST http://127.0.0.1:5500/api/v2/routing/global   -H 'Content-Type: application/json'   -d '{
    "mode": "selective",
    "requested_by": "external_client:my-automation",
    "management_context": {
      "source_type": "external_client",
      "client_name": "my-automation",
      "channel": "bot",
      "action": "set_global_mode:selective",
      "actor": "operator"
    }
  }'
```

## Logging

Successful external management actions are written to operational logs with:

- `requested_by`
- normalized `management_attribution`
- selected server or mode
- `active_before` / `active_after`, where applicable
- ping details, where applicable

The UI displays these events in the normal operator action journal.
