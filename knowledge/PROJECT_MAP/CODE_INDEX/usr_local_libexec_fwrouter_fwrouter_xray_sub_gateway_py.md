# `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`

## Purpose

Generated code-index entry for `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

The response allowlist forwards protocol/cache headers and excludes FWRouter diagnostic headers and token echoes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.

## Stage 6.1 / S61-F12 — HTTP contract

The actual handler is exercised against fixture-owned ephemeral loopback HTTP
servers in `installer/tests/test_xray_sub_gateway_http.py`. This standalone host
script suite deliberately runs outside the backend test process, whose network
capability denial remains unchanged. The catalog maps Gateway source changes to
the host-contract suite and its existing installer-startup shard. No live API,
production port, Docker or systemd is used.

- GET preserves valid capability/legacy paths and query strings, fixed-upstream
  requests, forwarded-host/proto/for handling, status/body and the existing five
  protocol/cache response headers. Non-allowlisted request/response headers stay
  excluded. Header trust policy is unchanged.
- HEAD retains the existing GET-to-upstream contract and omits the downstream
  body on both success and failure. It is not changed into a new API HEAD call.
- Upstream HTTP error statuses/bodies remain protocol responses. Transport,
  timeout and malformed-response failures produce 502 with the fixed body
  `upstream unavailable\n` for GET; exception text is never reflected. This
  intentionally tightens the previous raw exception error body.
- Production timeout remains 180 seconds. A handler subclass supplies a short
  timeout only in disposable fixtures; no deployment/environment flag is added.
- Existing method/status-only access/error logging remains path, capability,
  query, header, body and client-address redacted.

Tests cover forwarding, capability-path rejection, GET/HEAD, errors/refusal/
timeout/disconnect and log redaction with bounded socket/thread teardown. They
are HTTP contract evidence, not public proxy exposure or external VPN E2E.
