# `/opt/fwrouter-api/fwrouter_api/adapters/rules_sources.py`

## Purpose

HTTP/Git adapter for downloading large DIRECT/VPN rule lists.

## Key Functions

- `RulesSourceAdapter.fetch_big_direct_sources()`
- `RulesSourceAdapter.fetch_big_vpn_sources()`
- `RulesSourceAdapter.fetch_big_vpn_source_versions()`
- `_parse_git_source(...)`
- `_fetch_git_source(...)`
- `_normalize_values(...)`
- `RulesSourceFetchError`

## Behavior Notes

- GitHub-backed git sources first resolve the ref through the GitHub commits API, then fetch raw files at the resolved commit.
- `fetch_big_vpn_source_versions()` performs a metadata-only GitHub commit check for git-backed VPN sources. It returns `None` when a safe version-only check is not available, so callers can fall back to the full download path.
- Git source and HTTP source payloads converge to one contract: values, source URLs, version metadata, and per-fetch metadata.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

Medium. Fetch failures break `/rules/full-update`, but do not directly break boot.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Keep `rules_fetch_timeout_seconds` and `rules_fetch_max_bytes` bounds on every network/file payload read.
