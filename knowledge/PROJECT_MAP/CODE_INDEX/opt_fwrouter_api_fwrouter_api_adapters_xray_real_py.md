# `/opt/fwrouter-api/fwrouter/api/adapters/xray/real.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- Docker Compose subprocesses use `/run/fwrouter-v2/docker-cli` as Docker CLI state so API hardening does not depend on `/root/.docker`.
- Binding/client reconciliation remembers the active config before replacing it.
  If the candidate reload fails, it writes and reloads that last-good config
  before returning failure.
- Explicit per-client Direct, Disabled, and legacy Selective-block directives
  are inserted before per-client VPN binding rules so a stale overlapping
  handoff cannot override a deny/direct choice. Mode rules are scoped to the
  VLESS inbound and exact client email; unrelated traffic retains its existing
  routing behavior.
- Mutating adapter entrypoints use the shared process/thread writer guard.
