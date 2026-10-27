from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_serializer


class ApiResponse(BaseModel):
    """Common /api/v2 response envelope."""

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _redact_private_attribution(self, handler):
        """Keep host-admin identity snapshots out of ordinary HTTP responses."""
        from fwrouter_api.services.api_client_attribution import redact_http_attribution

        return redact_http_attribution(handler(self))
