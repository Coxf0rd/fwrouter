# `/opt/fwrouter-api/fwrouter_api_main.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_main.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

The FastAPI app includes read-only operational/diagnostic endpoints for
`/api/v2/reconcile`, `/api/v2/events/recent`, and `/api/v2/diagnose`.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.

## Public subscription response header boundary

The request-context middleware creates and resets internal caller and server
request IDs for every request. It preserves `X-Request-ID` but removes only
`X-FWRouter-Request-ID` from `/s` and `/s/...` responses. Ordinary API responses
retain the server-attribution header. This policy also covers public namespace
errors, redirects and HEAD/method-rejection responses; it neither adds HEAD
routing nor changes status/body/error handling or authentication.

The six existing public subscription format/header assertions and two focused
header/context tests are exact routine overrides in the existing profiled Xray
test file. Qualified native/process ownership remains separate.

## Application lifetime ownership

`create_app()` builds HTTP routing and request middleware. It does not create a
separate JobManager, settings cache, scheduler set, cache namespace, or runtime
state root. The native API supports one process lifetime over its configured
state root; overlapping production app lifespans or changing state-root
environment variables during active work are not an isolation mechanism.
`ApplicationLifecycle` coordinates the existing owners. The fixed host
maintenance lease remains outside its injectable collaborators and is held
through shutdown. Startup preserves database admission before log cleanup,
handler registration, workers, and prewarm. Shutdown signals all schedulers
first, then shares the existing 14-second drain budget across schedulers,
prewarm, and the same JobManager instance.
