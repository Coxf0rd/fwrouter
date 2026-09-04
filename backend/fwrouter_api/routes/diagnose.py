from __future__ import annotations

from fastapi import APIRouter

from fwrouter_api.services import diagnostics


router = APIRouter()


@router.get("/diagnose")
def get_diagnose_endpoint() -> dict[str, object]:
    return diagnostics.build_diagnostic_report().model_dump(mode="json")
