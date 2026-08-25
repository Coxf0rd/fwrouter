# `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft_chains.py`

## Purpose

Builds nft chain lines for classify/direct/VPN branches, prerouting,
prerouting NAT, output, output NAT and disabled-subject guards. It keeps the
transparent TCP redirect and UDP TProxy marker comments close to the chain
logic that emits them.

## Guardrails

- Immunity/protected/infrastructure guards stay before subject/global capture.
- `fwrouter_classify` decides the branch; terminal direct/VPN chains keep their
  own semantics.
- Disabled subjects must continue to be blocked in classify, forward/output,
  listener input guards and non-root `meta skuid` egress where applicable.
