# `/opt/fwrouter-api/fwrouter_api/services/apply_hot_swap.py`

## Purpose

Fast hot-swap helpers for replacing only the `fwrouter_classify` nft chain.

## Main Responsibilities

- Detect eligible subject-only fast apply contexts.
- Detect eligible global mode hot-swap contexts.
- Extract candidate classify-chain rules from rendered nft artifacts.
- Apply atomic `nft -f` classify-chain replacements.
- Verify live classify-chain comments and subject markers after hot-swap.

## Runtime Impact

Can execute `nft -f` through `subprocess` when called by `run_apply_pipeline()`.
It does not own rollback or manifest promotion; those remain in `apply.py`.

## Guardrails

- Do not hot-swap when core bypass, missing chains, or VPN policy routing prerequisites require a full apply.
- Preserve live marker verification; hot-swap success without marker verification is unsafe.
- Keep dnsmasq reconcile skipped only for classify-chain-only swaps.
