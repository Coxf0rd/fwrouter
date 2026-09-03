from __future__ import annotations

from fastapi import APIRouter

from fwrouter_api.services.reconcile import build_reconcile_response


router = APIRouter()


@router.get("/reconcile")
def get_reconcile_endpoint() -> dict[str, object]:
    return build_reconcile_response().model_dump(mode="json")
