from __future__ import annotations

from fastapi import APIRouter

from fwrouter_api.services import diagnostics
from fwrouter_api.services.event_contract import sanitize_value


router = APIRouter()


@router.get("/diagnose")
def get_diagnose_endpoint() -> dict[str, object]:
    return sanitize_value(diagnostics.build_diagnostic_report().model_dump(mode="json"))
