from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from fwrouter_api.services import diagnostics
from fwrouter_api.services.event_contract import sanitize_value


router = APIRouter()


@router.get("/diagnose")
def get_diagnose_endpoint(
    view: Literal["full", "summary"] = Query(default="full"),
) -> dict[str, object]:
    report = (
        diagnostics.build_diagnostic_report(include_history=False)
        if view == "summary"
        else diagnostics.build_diagnostic_report()
    ).model_dump(mode="json")
    if view == "summary":
        sections = {}
        for name, section in report["sections"].items():
            sections[name] = {
                key: section.get(key)
                for key in (
                    "status", "reason", "reason_code", "affected_entity_count",
                    "last_observation", "overall_impact", "failed", "drift",
                    "connections_total", "connections_enabled", "connections_stale",
                )
                if key in section
            }
        return sanitize_value({
            "status": report["status"],
            "generated_at": report["generated_at"],
            "sections": sections,
        })
    return sanitize_value(report)
