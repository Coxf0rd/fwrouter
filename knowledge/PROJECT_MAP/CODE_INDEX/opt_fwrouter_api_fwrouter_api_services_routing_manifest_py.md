# `/opt/fwrouter-api/fwrouter_api_services_routing_manifest.py`

## Purpose

Builds the bounded dataplane manifest from SQLite intent, effective subject
state and runtime preflight. Subject entries include effective routing state,
scoped matchers, bounded `network_listeners`, and bounded process UID metadata
derived from subject details.

`build_dataplane_manifest_from_state(...)` re-normalizes subjects against the
planned runtime preflight before nft rendering. When apply passes a staged
subject state, the builder must preserve embedded `user_override` and
`server_override` data from `effective_state`; otherwise user mode changes can
be rendered from stale/global state before the DB commit.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

The manifest is the only input used by nft rendering. Listener metadata for
disabled Docker/host services and process UID metadata for host-network Docker
egress must be snapshotted here; renderers must not probe Docker, systemd,
`/proc` or live sockets directly.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
