# `/opt/fwrouter-api/fwrouter_api/services/apply_plan.py`

## Purpose

Apply planning and job-context helpers.

## Main Responsibilities

- Define `ApplyMode`, `ApplyPhaseTimeoutError`, and `ApplyJobAbortedError`.
- Build apply plan DTOs with artifact paths and dataplane capability metadata.
- Resolve generated last-result/last-good manifest paths.
- Validate that an apply job row exists before side effects begin.

## Runtime Impact

Reads settings and job state. Does not apply live dataplane changes.

## Guardrails

- Keep `ApplyMode` import-compatible through `apply.py`.
- Do not create jobs here; apply orchestration owns job lifecycle.
- Keep generated result paths synchronized with installer/runtime expectations.
