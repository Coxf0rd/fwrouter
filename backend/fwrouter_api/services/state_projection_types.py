from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StateIntentDTO(BaseModel):
    state: str = "unknown"
    mode: str | None = None
    target_id: str | None = None
    source: str = "database"
    updated_at: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StateExecutionDTO(BaseModel):
    state: str = "unknown"
    legacy_apply_state: str | None = None
    applied_mode: str | None = None
    job_id: str | None = None
    apply_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StateObservationDTO(BaseModel):
    state: str = "unknown"
    source: str = "unknown"
    observed_at: str | None = None
    stale: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class StateReconcileDTO(BaseModel):
    state: str = "unknown"
    reason_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StateProjectionDTO(BaseModel):
    state: str = "unknown"
    severity: str = "none"
    message_key: str | None = None
    admin_message_key: str | None = None
    recommended_actions: list[str] = Field(default_factory=list)


class EntityStateProjectionDTO(BaseModel):
    entity: dict[str, Any]
    intent: StateIntentDTO = Field(default_factory=StateIntentDTO)
    execution: StateExecutionDTO = Field(default_factory=StateExecutionDTO)
    observation: StateObservationDTO = Field(default_factory=StateObservationDTO)
    reconcile: StateReconcileDTO = Field(default_factory=StateReconcileDTO)
    projection: StateProjectionDTO = Field(default_factory=StateProjectionDTO)
    legacy: dict[str, Any] = Field(default_factory=dict)
