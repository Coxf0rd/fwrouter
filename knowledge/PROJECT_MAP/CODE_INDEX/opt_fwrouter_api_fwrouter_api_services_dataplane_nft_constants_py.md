# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft_constants.py`

## Purpose

Small constant/helper module for nftables rendering. It owns the stable
FWRouter table name, required chain names, static secure-DNS bypass baseline,
control-plane input ports, root UID exemption and fwmark derivation helpers.

## Guardrails

- Do not change `inet fwrouter_v2`, `0x100`, `0x200`, table `100` related
  contracts partially.
- Keep constants import-only; rendering and artifact writes belong in sibling
  modules.
