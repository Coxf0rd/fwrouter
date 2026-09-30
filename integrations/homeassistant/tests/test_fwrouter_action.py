from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import fwrouter_action  # noqa: E402


def _selector(outcome: str, *, changed: bool, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "applied": outcome == "selected",
        "selection_outcome": outcome,
        "selected_server_id": "logical-id",
        "selected_server_name": "Norway",
        "active_after": "logical-id",
        "effective_route": {"changed": changed, "fixed_server_id": None},
        "auto_transition": {
            "outcome": outcome,
            "changed": changed,
            "active_after_id": "logical-id",
            "active_after_name": "Norway",
            "selected_server_name": "Norway",
            "reason_code": "api_controlled_switch",
            "origin": "api",
            "actor_attribution": "caller_supplied_unverified",
        },
    }


def _install_confirmed_readback(monkeypatch) -> None:
    monkeypatch.setattr(
        fwrouter_action,
        "vpn_auto_state",
        lambda: {"vpn_auto": {"active_auto_server_id": "logical-id", "active_auto_server_valid": True, "config_consistent": True}},
    )
    monkeypatch.setattr(
        fwrouter_action,
        "router_summary",
        lambda: {"server_mode": "auto", "current_server_id": "logical-id"},
    )
    monkeypatch.setattr(
        fwrouter_action,
        "wait_until",
        lambda _label, predicate: predicate() is True or pytest.fail("selector readback did not confirm expected state"),
    )


def test_switch_best_reports_confirmed_change_and_logical_label(monkeypatch, capsys):
    _install_confirmed_readback(monkeypatch)
    monkeypatch.setattr(fwrouter_action, "request_json", lambda *_args, **_kwargs: {"data": {"selector": _selector("selected", changed=True)}})

    fwrouter_action.switch_best()

    output = capsys.readouterr().out
    assert "auto_transition=selected" in output
    assert "effective_route_change=changed" in output
    assert "logical_server=Norway" in output


def test_switch_best_reports_noop_without_claiming_a_change(monkeypatch, capsys):
    _install_confirmed_readback(monkeypatch)
    monkeypatch.setattr(fwrouter_action, "request_json", lambda *_args, **_kwargs: {"data": {"selector": _selector("noop", changed=False)}})

    fwrouter_action.switch_best()

    output = capsys.readouterr().out
    assert "auto_transition=noop" in output
    assert "effective_route_change=unchanged" in output


def test_switch_best_rejects_unconfirmed_and_contradictory_outcomes(monkeypatch):
    _install_confirmed_readback(monkeypatch)
    monkeypatch.setattr(fwrouter_action, "request_json", lambda *_args, **_kwargs: {"data": {"selector": _selector("unconfirmed", changed=False, ok=False)}})

    with pytest.raises(RuntimeError, match="best-server switch failed"):
        fwrouter_action.switch_best()

    monkeypatch.setattr(fwrouter_action, "request_json", lambda *_args, **_kwargs: {"data": {"selector": _selector("selected", changed=True, ok=False)}})
    with pytest.raises(RuntimeError, match="best-server switch failed"):
        fwrouter_action.switch_best()
