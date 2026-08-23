# `/opt/fwrouter-api/fwrouter_api/services/apply_manifest.py`

## Purpose

Manifest helper functions for the apply pipeline.

## Main Responsibilities

- Extract requested runtime mode from a manifest.
- Detect core-bypass requests from manifest extras.
- Materialize prebuilt manifests with volatile apply-time fields.
- Build render-failure result payloads for persisted job artifacts.

## Runtime Impact

Pure DTO/manifest manipulation. It does not write artifacts or touch live
dataplane state.

## Guardrails

- Prebuilt manifests must still pass the normal check/apply/verify pipeline.
- Keep failure DTO shape compatible with job artifacts and UI/API consumers.
