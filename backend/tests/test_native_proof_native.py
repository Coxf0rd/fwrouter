from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_PINNED_MIHOMO_SHA256 = "12d97b7b7fa22cb4456e62c6e35db4be952dbb1c53eacaa9ef437c95d5068a9d"


def test_native_proof_collectors_against_pinned_engines(tmp_path: Path) -> None:
    if os.environ.get("FWROUTER_QUALIFIED_CHILD_PROFILE") != "/run/fwrouter-acceptance/qualified-child-profile.json":
        pytest.skip("requires the qualified child-process profile with the pinned Mihomo binary")
    configured_binary = os.environ.get("FWROUTER_TEST_MIHOMO_BINARY")
    binary = Path(configured_binary) if configured_binary else next((candidate for candidate in (
        Path("/opt/fwrouter-test/bin/mihomo"), Path("/tmp/fwrouter-mihomo-v1.19.31/mihomo"),
    ) if candidate.is_file()), Path("/opt/fwrouter-test/bin/mihomo"))
    if not binary.is_file() or binary.is_symlink():
        pytest.fail("qualified pinned Mihomo binary is unavailable")
    if hashlib.sha256(binary.read_bytes()).hexdigest() != _PINNED_MIHOMO_SHA256:
        pytest.fail("qualified Mihomo binary digest does not match v1.19.31 pin")

    script = r'''
import hashlib, http.server, json, os, pathlib, socket, subprocess, sys, tempfile, threading, time, urllib.request

binary = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])
version = subprocess.run([str(binary), "-v"], check=True, capture_output=True, text=True, timeout=5).stdout
if "v1.19.31" not in version:
    raise SystemExit("pinned Mihomo version mismatch")
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(204)
        self.end_headers()
    def log_message(self, *_args):
        pass
health_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
health_thread.start()
health_url = f"http://127.0.0.1:{health_server.server_address[1]}/generate_204"
config = "\n".join([
    "mode: rule", "log-level: silent", "allow-lan: false", "bind-address: 127.0.0.1",
    f"external-controller: 127.0.0.1:{port}", "proxies: []", "proxy-groups:",
    "  - name: vpn-auto", "    type: select", "    proxies: [REJECT]",
    "  - name: vpn-global", "    type: select", "    proxies: [REJECT, vpn-auto]",
    "rules:", "  - MATCH,vpn-global", "  - NETWORK,udp,REJECT", "sub-rules:",
    "  fwrouter-transparent:", "    - MATCH,vpn-global", "    - NETWORK,udp,REJECT",
    "  fwrouter-full-vpn:", "    - MATCH,vpn-global", "    - NETWORK,udp,REJECT", "",
])
config_path = root / "config.yaml"
config_path.write_text(config, encoding="utf-8")
config_path.chmod(0o600)
subprocess.run([str(binary), "-t", "-d", str(root), "-f", str(config_path)], check=True,
               capture_output=True, text=True, timeout=8)
process = subprocess.Popen([str(binary), "-d", str(root), "-f", str(config_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True, close_fds=True)
try:
    deadline = time.monotonic() + 8
    proxies = rules = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/proxies", timeout=0.4) as response:
                proxies = json.load(response).get("proxies")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/rules", timeout=0.4) as response:
                rules = json.load(response).get("rules")
            break
        except Exception:
            if process.poll() is not None:
                raise RuntimeError("pinned Mihomo exited before native readback")
            time.sleep(0.1)
    if not isinstance(proxies, dict) or not isinstance(rules, list):
        raise RuntimeError("pinned Mihomo native endpoints unavailable")
    sys.path.insert(0, sys.argv[3])
    for key in tuple(os.environ):
        if key.startswith("FWROUTER_"):
            os.environ.pop(key, None)
    os.environ["FWROUTER_STATE_DIR"] = str(root / "fwrouter-state")
    from fwrouter_api.core.config import Settings, get_settings
    Settings.model_config["env_file"] = None
    settings = get_settings()
    if not settings.paths.state_dir.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("fixture FWRouter state path escaped disposable root")
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import servers, xray_bindings, xray_handoff
    from fwrouter_api.services import mihomo_config, mihomo_config_proxies, mihomo_config_status, mihomo_reconcile_fingerprint
    from fwrouter_api.services import native_proof
    from fwrouter_api.services.mihomo_config_yaml import hash_mihomo_yaml_file
    adapter = MihomoHttpAdapter(base_url=f"http://127.0.0.1:{port}", config_path=config_path, timeout_seconds=1)
    parsed = MihomoHttpAdapter._parse_vpn_udp_policy_config(config)
    routing = {"server_mode": "auto", "active_auto_server_id": "synthetic-server-id"}
    db = {"routing": routing, "selection": {"revision": 9, "active_server_id": "synthetic-server-id"}}
    mihomo_config.build_mihomo_config = lambda *_args, **_kwargs: parsed
    xray_bindings.collect_xray_runtime_bindings = lambda: []
    xray_handoff.build_xray_handoff_assignments = lambda _bindings: []
    servers.get_routing_global_state = lambda **_kwargs: routing
    # Keep the real canonical fingerprint and receipt writer/reader. Replace
    # only their input collectors with stable, fixture-owned values so the
    # receipt binds this disposable engine/config without opening application DB.
    mihomo_reconcile_fingerprint._table_fingerprint = lambda: {"fixture_tables": []}
    mihomo_reconcile_fingerprint._input_files_fingerprint = lambda: {"fixture_files": []}
    mihomo_reconcile_fingerprint._state_path = lambda: root / "reconcile-state.json"
    mihomo_reconcile_fingerprint._resolved_candidate_config_path = lambda: root / "config.next.yaml"
    mihomo_reconcile_fingerprint.active_config_hash = lambda: hash_mihomo_yaml_file(config_path)
    mihomo_config_status._resolve_active_auto_runtime_target = lambda _server_id, *, expected_targets: (
        "logical-fixture" if "logical-fixture" in expected_targets else None
    )
    def runtime_identity():
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        return {"image_digest": "sha256:" + digest,
                "image_reference_sha256": hashlib.sha256(b"mihomo-fixture").hexdigest(),
                "version_sha256": hashlib.sha256(version.encode()).hexdigest(),
                "incarnation_sha256": hashlib.sha256(f"{process.pid}:{process.args}".encode()).hexdigest()}
    mihomo_reconcile_fingerprint._current_runtime_incarnation = lambda: runtime_identity()["incarnation_sha256"]
    # Add a fixture-only HTTP endpoint backed by the local 204 health responder.
    # The real pinned fallback group can select it without external traffic.
    parsed["proxies"] = [{"name": "server-fixture", "type": "http", "server": "127.0.0.1",
                           "port": health_server.server_address[1]}]
    logical_group = {"name": "logical-fixture", "type": "fallback", "proxies": ["server-fixture"],
                     "url": health_url, "interval": 300}
    parsed["proxy-groups"].insert(0, logical_group)
    parsed_groups = {group["name"]: group for group in parsed["proxy-groups"]}
    parsed_groups["vpn-auto"]["proxies"] = ["logical-fixture", "REJECT"]
    parsed_groups["vpn-global"]["proxies"] = ["REJECT", "vpn-auto"]
    mihomo_config_proxies._logical_profile_groups = lambda **_kwargs: [logical_group]
    config = "\n".join([line for line in config.splitlines() if not line.startswith("proxies:") and not line.startswith("proxy-groups:") and not line.startswith("  - name: vpn-auto") and not line.startswith("    type: select") and not line.startswith("    proxies: [REJECT]") and not line.startswith("  - name: vpn-global") and not line.startswith("    proxies: [REJECT, vpn-auto]")])
    config += "\n" + "\n".join([
        "proxies:", "  - name: server-fixture", "    type: http", "    server: 127.0.0.1",
        f"    port: {health_server.server_address[1]}",
        "proxy-groups:", "  - name: logical-fixture", "    type: fallback", "    proxies: [server-fixture]",
        f"    url: {health_url}", "    interval: 300",
        "  - name: vpn-auto", "    type: select", "    proxies: [logical-fixture, REJECT]",
        "  - name: vpn-global", "    type: select", "    proxies: [REJECT, vpn-auto]", "",
    ])
    config_path.write_text(config, encoding="utf-8")
    config_path.chmod(0o600)
    subprocess.run([str(binary), "-t", "-d", str(root), "-f", str(config_path)], check=True,
                   capture_output=True, text=True, timeout=8)
    # Restart on the final fixture that includes the selector member.
    process.terminate()
    process.wait(timeout=3)
    process = subprocess.Popen([str(binary), "-d", str(root), "-f", str(config_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/proxies", timeout=0.4) as response:
                proxies = json.load(response).get("proxies")
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/rules", timeout=0.4) as response:
                rules = json.load(response).get("rules")
            logical_group_state = proxies.get("logical-fixture") if isinstance(proxies, dict) else None
            if (
                isinstance(logical_group_state, dict)
                and str(logical_group_state.get("type") or "").lower() == "fallback"
                and logical_group_state.get("now") == "server-fixture"
            ):
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("fixture selector member unavailable")
    logical_readback = proxies.get("logical-fixture")
    if not isinstance(logical_readback, dict):
        raise RuntimeError("fixture logical fallback group unavailable")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/proxies/vpn-global", data=b'{"name":"vpn-auto"}',
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(request, timeout=1) as response:
        if response.status not in {200, 204}:
            raise RuntimeError("fixture global selector did not accept its intended member")
    input_fingerprint = mihomo_reconcile_fingerprint.current_mihomo_input_fingerprint(routing)
    native_udp_receipt = MihomoHttpAdapter._vpn_udp_policy_readback(
        config, adapter._proxies(timeout_seconds=2.0),
        adapter._get_json("/rules", timeout_seconds=2.0).get("rules"),
        _parsed_config=parsed,
    )
    if mihomo_reconcile_fingerprint.write_mihomo_reconcile_fingerprint_state(
        fingerprint=input_fingerprint,
        result={"reconcile_reason": "native_proof_disposable_fixture"},
        native_proof=native_udp_receipt,
        expected_active_hash=hash_mihomo_yaml_file(config_path),
        expected_runtime_incarnation=runtime_identity()["incarnation_sha256"],
    ) is not True:
        raise RuntimeError("fixture Mihomo receipt writer rejected the disposable native proof")
    proof = native_proof._mihomo_readback(db, adapter=adapter, runtime_identity_reader=runtime_identity)
    # Exercise a real native selector drift through Mihomo's controller API.
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/proxies/vpn-auto", data=b'{"name":"REJECT"}',
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(request, timeout=1) as response:
        if response.status not in {200, 204}:
            raise RuntimeError("fixture auto selector did not accept its drift target")
    try:
        native_proof._mihomo_readback(db, adapter=adapter, runtime_identity_reader=runtime_identity)
        bad_selector_reason = "unexpected-pass"
    except ValueError as exc:
        bad_selector_reason = str(exc)
    # Restore only the disposable engine to its expected state before testing
    # the readback parser's reaction to a synthetic altered /rules response.
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/proxies/vpn-auto", data=b'{"name":"logical-fixture"}',
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(request, timeout=1) as response:
        if response.status not in {200, 204}:
            raise RuntimeError("fixture auto selector could not be restored")
    real_get_json = adapter._get_json
    def changed_rules(path, **kwargs):
        result = real_get_json(path, **kwargs)
        if path == "/rules":
            result = dict(result)
            result["rules"] = [dict(result["rules"][0], proxy="DIRECT"), *result["rules"][1:]]
        return result
    adapter._get_json = changed_rules
    try:
        native_proof._mihomo_readback(db, adapter=adapter, runtime_identity_reader=runtime_identity)
        bad_rules_reason = "unexpected-pass"
    except ValueError as exc:
        bad_rules_reason = str(exc)
    print(json.dumps({"version_sha256": hashlib.sha256(version.encode()).hexdigest(),
                      "native_rule_count": proof["native_rule_count"],
                      "selector_count": proof["selector_count"], "proof": proof,
                      "logical_group": {"type": logical_readback.get("type"),
                                        "all": logical_readback.get("all"),
                                        "now": logical_readback.get("now")},
                      "bad_rules_reason": bad_rules_reason, "bad_rules_source": "synthetic_readback_response",
                      "bad_selector_reason": bad_selector_reason, "bad_selector_source": "native_controller_api"}, sort_keys=True))
finally:
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    health_server.shutdown()
    health_server.server_close()
    health_thread.join(timeout=2)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, str(binary), str(tmp_path), str(Path(__file__).resolve().parents[1])],
        check=True, capture_output=True, text=True, timeout=25,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["native_rule_count"] == 2
    assert result["proof"]["native_top_level_rules_match"] is True
    assert result["proof"]["selector_membership_matches"] is True
    assert result["proof"]["guarded_rules_match"] is True
    assert result["proof"]["selection_target_matches_durable_state"] is True
    assert str(result["logical_group"]["type"]).lower() == "fallback"
    assert result["logical_group"]["all"] == ["server-fixture"]
    assert result["logical_group"]["now"] == "server-fixture"
    assert result["bad_rules_reason"] == "MIHOMO_NATIVE_POLICY_MISMATCH"
    assert result["bad_selector_reason"] == "MIHOMO_SELECTION_TARGET_MISMATCH"
    assert result["bad_selector_source"] == "native_controller_api"
    assert result["bad_rules_source"] == "synthetic_readback_response"
