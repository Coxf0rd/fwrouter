# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft_render.py`

## Purpose

Owns `render_owned_table_candidate()`, the manifest-to-nft text renderer for
the FWRouter owned table. It composes constants, set builders and chain
builders, including core-bypass rendering, counters, selective/default routing,
scoped subject steering and transparent Mihomo handoff contracts.

Docker immunity is derived per active inventory record: host/FWRouter and
exactly identified managed Xray/Mihomo runtime addresses stay protected, while
resolved ordinary Docker `direct`, `vpn`, and `blocked` paths reach classify.
The renderer independently rejects a Docker VPN matcher that overlaps a
protected address/CIDR or is not ready for fail-closed classification.

## Guardrails

- Renderer output is a runtime contract; compare candidate text when refactoring.
- Keep the public compatibility wrapper in `dataplane_nft.py` so legacy imports
  and monkeypatch hooks keep working.
- Do not write artifacts here; artifact lifecycle belongs in
  `dataplane_nft_artifacts.py`.
