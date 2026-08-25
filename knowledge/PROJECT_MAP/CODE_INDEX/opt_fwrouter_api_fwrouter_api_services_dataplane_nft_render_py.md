# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft_render.py`

## Purpose

Owns `render_owned_table_candidate()`, the manifest-to-nft text renderer for
the FWRouter owned table. It composes constants, set builders and chain
builders, including core-bypass rendering, counters, selective/default routing,
scoped subject steering and transparent Mihomo handoff contracts.

## Guardrails

- Renderer output is a runtime contract; compare candidate text when refactoring.
- Keep the public compatibility wrapper in `dataplane_nft.py` so legacy imports
  and monkeypatch hooks keep working.
- Do not write artifacts here; artifact lifecycle belongs in
  `dataplane_nft_artifacts.py`.
