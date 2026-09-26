# `backend/fwrouter_api/services/event_contract.py`

## Purpose

Shared event envelope, correlation context, recursive credential sanitization, and bounded-payload metadata used by legacy and typed event writers.

## Important Functions

- `set_event_context(...)` / `reset_event_context(...)` manage request and worker context.
- `sanitize_value(...)` redacts credential-bearing keys, URI userinfo/query secrets, bearer strings, and private-key blocks.
- `normalize_event_details(...)` attaches envelope fields and preserves canonical error/correlation data when truncating large payloads.

## Guardrails

- Keep compatibility wrappers in `services/logs.py` and `services/events.py` as the persistence boundary.
- Job workers must snapshot/restore context explicitly; ContextVar does not cross `Thread` creation.
- Never place event context in `jobs.input_json`.
