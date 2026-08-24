from __future__ import annotations

from fwrouter_api.services.apply_orchestrator_constants import INTENT_APPLY_MANUAL_RULES, LOCK_APPLY, LOCK_RULES_APPLY


def _lock_for_intent(intent: str) -> str:
    if intent == INTENT_APPLY_MANUAL_RULES:
        return LOCK_RULES_APPLY
    return LOCK_APPLY

