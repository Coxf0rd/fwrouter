# FWRouter Fix Log

## 2026-06-29

- `install-server-tree.sh`: added source existence checks, included `fwrouter-xray` and `fwrouter-ui`, excluded `.env`/`.venv`/cache/db junk from installs, and enforced executable bits for helper scripts.
- `scripts/export-clean-tree.sh`: added clean export flow that omits runtime junk, secrets, caches, `.git`, and containerd directories.
- `dataplane-check.sh`: VPN candidate validation now requires explicit `fwrouter tproxy handoff tcp:` and `fwrouter tproxy handoff udp:` markers in addition to generic `tproxy` rules.
- `dataplane_nft.py`: `render_owned_table_candidate()` no longer performs Docker or DNS discovery; it now uses manifest-provided lists plus static secure-DNS fallback data only.
- `core_bypass.py`: bypass state changes now clear live probe caches so runtime/system summaries reflect `bypass_direct_safe` immediately.
- `system_subjects.py`: builtin `fwrouter:global` reconciliation no longer overwrites imported `applied_mode`.
- `control_plane_transfer.py`: normalized import now drops `fwrouter:global` server overrides instead of re-inserting null rows.
- `tailscale.py`: offline peers are no longer counted as importable just because they expose `TailscaleIPs`.
- `main.py`: added `create_app(enable_startup_tasks=False)` for API/unit tests that must avoid startup schedulers and runtime mutation.
