# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft_artifacts.py`

## Purpose

Owns generated dataplane artifact paths and atomic writes/promotions for
candidate, current, applied and last-good nft/manifest files.

## Guardrails

- Keep manifest copies as atomic copies from canonical JSON where possible.
- Do not render nft policy here except through the injected/default renderer.
- Persistent business intent remains SQLite plus generated artifacts; this
  module does not own routing decisions.
