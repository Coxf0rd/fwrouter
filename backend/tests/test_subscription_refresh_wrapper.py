from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


def _load_wrapper():
    script_path = Path(__file__).resolve().parents[2] / "host" / "sbin" / "fwrouter-subscription-refresh-job"
    loader = SourceFileLoader("fwrouter_subscription_refresh_job", str(script_path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def _running(job_id: str = "job-1") -> dict:
    return {"ok": True, "data": {"job": {"job_id": job_id, "status": "running"}}, "error": None}


def _success(job_id: str = "job-1") -> dict:
    return {"ok": True, "data": {"job": {"job_id": job_id, "status": "success"}}, "error": None}


def test_subscription_refresh_wrapper_waits_beyond_previous_systemd_timeout(monkeypatch) -> None:
    wrapper = _load_wrapper()
    clock = {"value": 0.0}
    calls = {"count": 0}

    monkeypatch.delenv("FWROUTER_SUBSCRIPTION_REFRESH_WAIT_SECONDS", raising=False)
    monkeypatch.setattr(wrapper.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(wrapper.time, "sleep", lambda seconds: clock.__setitem__("value", clock["value"] + seconds))

    def _api_json(path: str = "", **kwargs):
        calls["count"] += 1
        return _success() if clock["value"] >= 400 else _running()

    monkeypatch.setattr(wrapper, "api_json", _api_json)

    result = wrapper.wait_for_terminal_job(_running())

    assert result["data"]["job"]["status"] == "success"
    assert calls["count"] > 120
    assert wrapper.DEFAULT_WAIT_SECONDS == 600


def test_subscription_refresh_systemd_timeout_exceeds_wrapper_wait() -> None:
    wrapper = _load_wrapper()
    unit_path = Path(__file__).resolve().parents[2] / "host" / "systemd" / "fwrouter-subscription-refresh.service"
    timeout_line = next(line for line in unit_path.read_text().splitlines() if line.startswith("TimeoutStartSec="))
    timeout_seconds = int(timeout_line.split("=", 1)[1])

    assert timeout_seconds == 660
    assert timeout_seconds > wrapper.DEFAULT_WAIT_SECONDS
