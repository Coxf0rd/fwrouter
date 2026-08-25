# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft_sets.py`

## Purpose

Builds nft set definitions and deferred element commands for the owned
dataplane table. It also resolves manifest-provided infrastructure and
secure-DNS IPv4 lists, groups scoped VPN matchers into compact shared sets, and
chooses the canonical effective-rules artifact when the manifest does not carry
the full `rules_effective` payload.

## Guardrails

- Keep DNS runtime sets as timeout sets, not interval/auto-merge sets.
- Use manifest data or the provided effective-rules loader only; no runtime
  Docker/DNS/system discovery in the renderer path.
- Preserve facade monkeypatch compatibility by passing loaders from
  `dataplane_nft.py`.
