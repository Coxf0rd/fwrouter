# `/opt/fwrouter-api/fwrouter/api/adapters/xray/common.py`

## Purpose

Extracted module from the apply/Xray split. Keep this card concise and update the shared project map separately when responsibilities change.

## Notes

- Keep facade import compatibility stable.
- Preserve monkeypatch-compatible facade paths used by tests and integration code.
- Owns the shared reentrant thread/process writer guard for Xray config/client
  mutations. Lock files use the configured runtime directory and restrictive
  mode; callers acquire the guard before DB transactions or runtime I/O.
