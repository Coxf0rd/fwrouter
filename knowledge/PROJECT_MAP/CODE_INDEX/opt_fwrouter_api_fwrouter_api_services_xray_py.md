# `opt_fwrouter_api_fwrouter_api_services_xray_py.md`

## Purpose

Compatibility facade for Xray service; client CRUD, materialization, and subscription flows are split into focused service modules.

## Notes

- Keep old import and monkeypatch paths stable.
- Client delete paths remove scoped Xray `explicit_external_client` projections
  and subject overrides after runtime deletion/sync; audit events remain in
  logs.
