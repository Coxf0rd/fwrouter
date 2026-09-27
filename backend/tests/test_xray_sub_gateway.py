from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


def _load_gateway():
    script_path = Path(__file__).resolve().parents[2] / "host" / "libexec" / "fwrouter" / "fwrouter-xray-sub-gateway.py"
    loader = SourceFileLoader("fwrouter_xray_sub_gateway", str(script_path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_subscription_gateway_forwards_protocol_headers_without_fwrouter_diagnostics() -> None:
    gateway = _load_gateway()

    assert "content-type" in gateway.PASSTHROUGH_HEADERS
    assert "profile-update-interval" in gateway.PASSTHROUGH_HEADERS
    assert "cache-control" in gateway.PASSTHROUGH_HEADERS
    assert not any(name.startswith("x-fwrouter-") for name in gateway.PASSTHROUGH_HEADERS)
