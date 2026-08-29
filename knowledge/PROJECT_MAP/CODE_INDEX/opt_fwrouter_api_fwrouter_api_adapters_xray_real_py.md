# `/opt/fwrouter-api/fwrouter/api/adapters/xray/real.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- Docker Compose subprocesses use `/run/fwrouter-v2/docker-cli` as Docker CLI state so API hardening does not depend on `/root/.docker`.
