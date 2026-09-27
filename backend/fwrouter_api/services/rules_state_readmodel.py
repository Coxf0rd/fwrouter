from __future__ import annotations

import hashlib
from typing import Any

from fwrouter_api.services import rules as rules_service
from fwrouter_api.services.rules_state_files import get_manual_rules_texts
from fwrouter_api.services.rules_state_metadata import (
    _repair_stale_running_rules_state,
    list_rules_metadata,
)
from fwrouter_api.services.rules_state_store import (
    _default_rules_paths,
    _read_json_if_exists,
    _read_text_if_exists,
    _rules_state_with_updates,
    get_rules_state,
)


def get_rules_overview() -> dict[str, Any]:
    state = _repair_stale_running_rules_state(get_rules_state())
    texts = get_manual_rules_texts()
    metadata = texts["metadata"] if isinstance(texts["metadata"], dict) else {}
    return {
        "state": state,
        "metadata": list_rules_metadata(),
        "sources": {
            "configured": rules_service._configured_rules_sources(),
            "last_effective": {
                "versions": metadata.get("versions", {}),
                "source_urls": metadata.get("source_urls", {}),
                "fetch_summary": metadata.get("fetch_summary", {}),
            },
        },
        "manual": {
            "draft_text": texts["draft_text"] or None,
            "active_text": texts["active_text"] or None,
            "draft_validation": rules_service.validate_manual_rules(texts["draft_text"] or ""),
            "active_validation": rules_service.validate_manual_rules(texts["active_text"] or ""),
            "effective": texts["effective"],
        },
        "lists": {
            "static_direct_text": texts["static_direct_text"] or "",
            "big_direct_text": texts["big_direct_text"] or "",
            "big_vpn_text": texts["big_vpn_text"] or "",
        },
        "artifacts": {
            "effective_text": texts["effective_text"],
            "metadata": texts["metadata"],
            "candidate_json_path": str(_default_rules_paths()["effective_candidate_json_path"]),
            "candidate_text_path": str(_default_rules_paths()["effective_candidate_text_path"]),
        },
    }


def get_rules_summary() -> dict[str, Any]:
    state = _repair_stale_running_rules_state(get_rules_state())
    defaults = _default_rules_paths()
    metadata_file = _read_json_if_exists(defaults["metadata_path"])
    metadata = metadata_file if isinstance(metadata_file, dict) else {}
    draft_text = _read_text_if_exists(defaults["manual_draft_path"]) or ""
    active_text = _read_text_if_exists(defaults["manual_active_path"]) or ""

    return {
        "state": state,
        "metadata": list_rules_metadata(),
        "sources": {
            "configured": rules_service._configured_rules_sources(),
            "last_effective": {
                "versions": metadata.get("versions", {}),
                "source_urls": metadata.get("source_urls", {}),
                "fetch_summary": metadata.get("fetch_summary", {}),
            },
        },
        "manual": {
            "draft_text": draft_text or None,
            "active_text": active_text or None,
            "draft_validation": rules_service.validate_manual_rules(draft_text),
            "active_validation": rules_service.validate_manual_rules(active_text),
        },
    }


def _manual_rules_audit_summary(text: str) -> dict[str, Any]:
    validation = rules_service.validate_manual_rules(text)
    rules = validation.get("rules") if isinstance(validation.get("rules"), list) else []
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "rule_count": len(rules),
        "valid": bool(validation.get("valid")),
    }


def save_manual_draft(text: str, *, requested_by: str = "api") -> dict[str, Any]:
    defaults = _default_rules_paths()
    current = get_manual_rules_texts()
    previous_text = str(current.get("draft_text") or "")
    changed = previous_text != text
    validation = rules_service.validate_manual_rules(text)
    if not changed:
        overview = get_rules_overview()
        overview["manual"]["draft_validation"] = validation
        return overview

    rules_service.atomic_write_text(defaults["manual_draft_path"], text)
    with rules_service.db_session() as connection:
        _rules_state_with_updates(
            connection=connection,
            manual_draft_path=str(defaults["manual_draft_path"]),
            manual_active_path=str(defaults["manual_active_path"]),
            static_direct_path=str(defaults["static_direct_path"]),
            big_direct_path=str(defaults["big_direct_path"]),
            big_vpn_path=str(defaults["big_vpn_path"]),
            effective_json_path=str(defaults["effective_json_path"]),
            effective_text_path=str(defaults["effective_text_path"]),
            metadata_path=str(defaults["metadata_path"]),
            status="pending",
            error_code=None,
            error_message=None,
        )
        from fwrouter_api.services.event_contract import current_event_context
        from fwrouter_api.services.events import create_event_context, write_audit_event

        entity_id = "manual-draft"
        request_id = current_event_context().get("request_id")
        write_audit_event(
            actor=requested_by,
            actor_attribution="caller_supplied",
            source="rules_admin_api",
            action="manual_rules_draft_changed",
            event_code="rules.manual_draft_changed",
            legacy_event_type="rules.manual_draft_changed",
            entity_type="rules",
            entity_id=entity_id,
            previous_value=_manual_rules_audit_summary(previous_text),
            new_value=_manual_rules_audit_summary(text),
            context=create_event_context(request_id=request_id, entity_id=entity_id),
            details={"outcome": "intent_committed"},
            connection=connection,
        )
    overview = get_rules_overview()
    overview["manual"]["draft_validation"] = validation
    return overview


def get_effective_rules() -> dict[str, Any]:
    texts = get_manual_rules_texts()
    return {
        "effective": texts["effective"],
        "effective_text": texts["effective_text"],
        "metadata": texts["metadata"],
        "paths": {
            "effective_json_path": str(texts["effective_json_path"]),
            "effective_text_path": str(texts["effective_text_path"]),
            "metadata_path": str(texts["metadata_path"]),
        },
    }
