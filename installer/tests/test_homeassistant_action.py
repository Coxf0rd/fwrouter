from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


SCRIPT = Path(__file__).parents[2] / "integrations/homeassistant/scripts/fwrouter_action.py"
SPEC = importlib.util.spec_from_file_location("fwrouter_ha_action", SCRIPT)
assert SPEC and SPEC.loader
ha_action = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ha_action)
PACKAGE = Path(__file__).parents[2] / "integrations/homeassistant/packages/fwrouter_control.yaml"


def selector_result(
    *, outcome: str, changed: bool, server_mode: str = "auto", effective_changed: bool | None = None
) -> dict:
    logical_id = "logical-new"
    return {
        "ok": True,
        "applied": outcome == "selected",
        "selection_outcome": outcome,
        "selected_server_id": logical_id,
        "selected_server_name": "Norway",
        "auto_transition": {
            "outcome": outcome,
            "changed": changed,
            "active_after_id": logical_id,
            "active_after_name": "Norway",
            "selected_server_name": "Norway",
            "reason_code": "api_controlled_switch",
            "origin": "api",
            "actor_attribution": "caller_supplied_unverified",
        },
        "effective_route": {
            "server_mode": server_mode,
            "fixed_server_id": "logical-fixed" if server_mode == "fixed" else None,
            "changed": (
                outcome == "selected" and server_mode == "auto"
                if effective_changed is None
                else effective_changed
            ),
        },
    }


def test_active_server_sensor_uses_safe_name_and_effective_mode_before_retained_id() -> None:
    config = yaml.safe_load(PACKAGE.read_text())
    summary_sensor = next(
        sensor
        for resource in config["rest"]
        for sensor in resource["sensor"]
        if sensor["name"] == "fwrouter_router_summary"
    )
    active_sensor = next(
        sensor
        for group in config["template"]
        for sensor in group.get("sensor", [])
        if sensor["name"] == "fwrouter_active_server_text"
    )
    state_template = active_sensor["state"]
    summary_value = summary_sensor["value_template"]

    assert summary_sensor["json_attributes_path"] == "$.data.router"
    assert "current_server_name" in summary_sensor["json_attributes"]
    assert "current_server_id" in summary_sensor["json_attributes"]
    assert "current_server_name" in summary_value
    assert "current_server_id" not in summary_value
    assert "sub:" not in summary_value
    assert state_template.index("if mode == 'direct'") < state_template.index("elif name and source")
    assert "current_server_name" in state_template
    assert "active_auto_server_id" not in state_template


def run_switch(selector: dict, *, active_id: str = "logical-new", summary: dict | None = None) -> str:
    summary = summary or {
        "global_mode": "VPN",
        "server_mode": "auto",
        "current_server_id": "logical-new",
        "current_server_name": "Norway",
        "current_server_source": "auto",
    }
    state = {
        "vpn_auto": {
            "active_auto_server_id": active_id,
            "active_auto_server_valid": True,
            "config_consistent": True,
        }
    }

    def request(method: str, path: str, payload=None):
        if method == "POST":
            return {"ok": True, "data": {"selector": selector}}
        if path == "/selector/vpn-auto/state":
            return {"ok": True, "data": state}
        if path == "/ui/router-summary":
            return {"ok": True, "data": {"router": summary}}
        raise AssertionError(f"unexpected request: {method} {path}")

    with patch.object(ha_action, "request_json", side_effect=request), patch.object(
        ha_action, "TIMEOUT_SECONDS", 0.1
    ), patch.object(ha_action, "POLL_SECONDS", 0):
        with patch("builtins.print") as output:
            ha_action.switch_best()
    return output.call_args.args[0]


def test_switch_best_confirms_changed_logical_server_and_reports_provenance() -> None:
    message = run_switch(selector_result(outcome="selected", changed=True))

    assert "auto_transition=selected" in message
    assert "effective_change=changed" in message
    assert "logical=Norway" in message
    assert "effective=Norway" in message
    assert "reason=api_controlled_switch" in message
    assert "source=api" in message
    assert "attribution=caller_supplied_unverified" in message


def test_switch_best_keeps_noop_distinct_from_change() -> None:
    message = run_switch(selector_result(outcome="noop", changed=False))

    assert "auto_transition=noop" in message
    assert "effective_change=unchanged" in message


def test_switch_best_verifies_fixed_effective_server_after_auto_selector_change() -> None:
    summary = {
        "global_mode": "VPN",
        "server_mode": "fixed",
        "current_server_id": "logical-fixed",
        "current_server_name": "Sweden",
        "current_server_source": "manual",
    }
    message = run_switch(
        selector_result(outcome="selected", changed=True, server_mode="fixed"),
        summary=summary,
    )

    assert "logical=Norway" in message
    assert "effective=Sweden" in message
    assert "effective_change=unchanged" in message


def test_switch_best_direct_effective_route_does_not_fall_back_to_logical_id() -> None:
    summary = {
        "global_mode": "DIRECT",
        "server_mode": "auto",
        "current_server_id": "logical-new",
        "current_server_name": "Norway",
        "current_server_source": "auto",
    }
    message = run_switch(
        selector_result(outcome="selected", changed=True, effective_changed=False),
        summary=summary,
    )

    assert "effective=direct" in message
    assert "effective_change=unchanged" in message
    assert "sub:" not in message


def test_switch_best_rejects_old_healthy_selection_as_readback() -> None:
    def assert_not_confirmed(label, predicate):
        assert predicate() is False
        raise TimeoutError(f"{label} was not confirmed by FWRouter API")

    with patch.object(
        ha_action,
        "request_json",
        side_effect=[
            {"ok": True, "data": {"selector": selector_result(outcome="selected", changed=True)}},
            {
                "ok": True,
                "data": {
                    "vpn_auto": {
                        "active_auto_server_id": "old-logical",
                        "active_auto_server_valid": True,
                        "config_consistent": True,
                    }
                },
            },
        ],
    ), patch.object(ha_action, "wait_until", side_effect=assert_not_confirmed):
        with pytest.raises(TimeoutError, match="not confirmed"):
            ha_action.switch_best()


def test_switch_best_surfaces_api_operation_error() -> None:
    api_error = {"code": "APPLY_FAILED", "message": "apply rejected"}
    with patch.object(ha_action, "request_json", side_effect=RuntimeError(api_error)):
        with pytest.raises(RuntimeError, match="APPLY_FAILED"):
            ha_action.switch_best()


def test_switch_best_rejects_unconfirmed_outcome_even_when_state_is_healthy() -> None:
    selector = selector_result(outcome="unconfirmed", changed=None)
    with patch.object(
        ha_action, "request_json", return_value={"ok": True, "data": {"selector": selector}}
    ):
        with pytest.raises(RuntimeError, match="not confirmed"):
            ha_action.switch_best()
