from __future__ import annotations

import json
import sys
import types

from fwrouter_api import cli


def _run_supersession_cli(
    monkeypatch, capsys, service_result: dict[str, object]
) -> dict[str, object]:
    service_module = types.ModuleType("fwrouter_api.services.xray_subscription_service")
    service_module.reconcile_xray_subscription_profile_nodes = lambda **_kwargs: service_result
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)

    exit_code = cli.main([
        "xray", "generation", "supersede",
        "--checkpoint-sha256", "a" * 64,
        "--migration-plan-sha256", "b" * 64,
        "--apply",
    ])

    assert exit_code == 1
    return json.loads(capsys.readouterr().out)


def test_generation_supersession_cli_exposes_bounded_failure_codes(monkeypatch, capsys) -> None:
    output = _run_supersession_cli(monkeypatch, capsys, {
        "ok": False,
        "status": "failed",
        "stage": "generation_apply",
        "error_code": "XRAY_IDENTITY_INTENT_COMPENSATION_UNVERIFIED",
        "failed_operation": "MIHOMO_XRAY_HANDOFF_TARGET_MISSING",
        "compensation_reason": "handoff_target_missing",
    })

    assert output["failed_operation"] == "MIHOMO_XRAY_HANDOFF_TARGET_MISSING"
    assert output["compensation_reason"] == "handoff_target_missing"
    assert output["error_code"] == "XRAY_IDENTITY_INTENT_COMPENSATION_UNVERIFIED"


def test_generation_supersession_cli_omits_untrusted_failure_details(monkeypatch, capsys) -> None:
    private_value = "11111111-1111-4111-8111-111111111111 token=secret-value"
    output = _run_supersession_cli(monkeypatch, capsys, {
        "ok": False,
        "status": "failed",
        "failed_operation": private_value,
        "compensation_reason": "Compensation failed: " + private_value,
    })

    captured = json.dumps(output)
    assert output["failed_operation"] is None
    assert output["compensation_reason"] is None
    assert private_value not in captured
    assert "secret-value" not in captured


def test_generation_supersession_cli_bounds_failure_codes(monkeypatch, capsys) -> None:
    output = _run_supersession_cli(monkeypatch, capsys, {
        "ok": False,
        "status": "failed",
        "failed_operation": "A" * 97,
        "compensation_reason": "a" * 97,
    })

    assert output["failed_operation"] is None
    assert output["compensation_reason"] is None
