from __future__ import annotations

import io
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


def test_subscription_gateway_logs_method_and_status_without_request_data(capsys) -> None:
    gateway = _load_gateway()
    handler = object.__new__(gateway.Handler)
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.log_date_time_string = lambda: "15/Oct/2026:12:00:00 +0000"
    handler.send_response = lambda code, message=None: handler.log_request(code)
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()

    bearer = "synthetic-bearer-fixture"
    legacy_id = "123e4567-e89b-42d3-a456-426614174000"
    requestline = (
        f"GET /s/{bearer}?token={bearer} HTTP/1.1"
        f"; legacy=/api/v2/xray/clients/{legacy_id}/subscription.txt"
    )
    client_address = "203.0.113.55"

    handler.log_request(200, size=12)
    handler.send_error(404, message=requestline)
    handler.log_message('"%s" %s %s', requestline, 404, client_address)
    handler.command = "GET /s/untrusted-method-data"
    handler.log_request("200 untrusted status data")

    output = capsys.readouterr().err
    assert "access method=GET status=200" in output
    assert "error method=GET status=404" in output
    assert "access method=GET status=404" in output
    assert "message method=GET status=-" in output
    assert "access method=OTHER status=-" in output
    assert bearer not in output
    assert legacy_id not in output
    assert client_address not in output
    assert "/s/" not in output
    assert "token=" not in output
    assert "/s/untrusted-method-data" not in output
