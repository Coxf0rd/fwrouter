# `/opt/fwrouter-api/fwrouter_api/adapters/subscription.py`

## Purpose

Downloads, classifies, and parses provider subscription payloads into semantic
`SubscriptionServer` entries.

## Important Functions And Types

- `SubscriptionRequestProfile`
  Describes source-aware HTTP request headers for subscription fetches.
  Current profiles include `legacy_flclash` and `client_compatible`.
- `HttpMihomoSubscriptionAdapter.refresh(url, ...)`
  Downloads subscription payloads, detects response format, rejects provider
  placeholders, and dispatches to the appropriate parser.
- payload detection
  Classifies `clash_yaml`, `base64_subscription`, `plain_uri_lines`,
  `json_profile`, empty, and unsupported/placeholder responses.
- URI/base64/plain parser
  Flat subscriptions use the contract `1 distinct exact URI = 1 server`.
  Exact duplicate URI entries deduplicate.
- Clash/Mihomo YAML parser
  Imports top-level `proxies` as selectable proxy nodes. Display name is not
  identity.
- structured JSON parser
  Imports user-visible logical profiles/groups, stores internal VLESS endpoints
  and service outbounds in topology metadata, and does not expose every internal
  outbound as a UI server.

## Runtime/Persistent State

This adapter does not write SQLite directly. Persistence, membership sync, and
inventory lifecycle are owned by `services/subscription.py`.

## Phase 2 Identity Contract

- Flat URI identity: `sub:<sha256(exact trimmed URI)>`.
- Structured YAML/JSON identity: `sub:<sha256(canonical raw semantic object)>`.
- `server_name` is display text only.
- Duplicate display names are valid.
- Mihomo runtime names are deterministic unique names stored in raw metadata.
- Diagnostics classify unsupported, invalid, exact duplicate, service, and
  internal endpoint entries without logging credentials.

## Live Regression Cases

- `cdn.mainboss.net` full/client-compatible response is a 23-entry flat
  URI/base64 subscription and includes `🇩🇪Auto Server🔋 - NEW` as a VLESS
  Reality xhttp node.
- `sub.proxen.app` client-compatible response is a structured JSON profile:
  logical profiles are user-visible servers, while internal VLESS endpoints and
  service outbounds remain topology metadata.

## Runtime Impact

Medium/high. Parser output drives server inventory, Mihomo config generation,
selector choices, and UI/API server projections.
