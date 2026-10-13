from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

from fwrouter_api.adapters.xray_common import XrayAdapterError, XrayApplyResult
from fwrouter_api.adapters import xray_real
from fwrouter_api.adapters.xray_real import RealXrayAdapter


def test_loaded_client_readback_against_isolated_xray_26_2_6(monkeypatch, tmp_path) -> None:
    image = os.environ.get("FWROUTER_XRAY_TEST_IMAGE")
    if not image:
        pytest.skip("set FWROUTER_XRAY_TEST_IMAGE to an already-present pinned Xray 26.2.6 image")
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    if not image.startswith("sha256:"):
        pytest.fail("FWROUTER_XRAY_TEST_IMAGE must be an immutable local image ID, not a mutable tag")

    version = subprocess.run(
        ["docker", "run", "--pull=never", "--rm", "--network", "none", image, "version"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    assert "Xray 26.2.6" in version

    expected = ("11111111-1111-4111-8111-111111111111", "fixture@example.test")
    run_id = os.environ.get("FWROUTER_XRAY_TEST_RUN_ID")
    if run_id and not re.fullmatch(r"[0-9a-f]{32}", run_id):
        pytest.fail("FWROUTER_XRAY_TEST_RUN_ID must be a lowercase 128-bit nonce")
    container = f"native-xray-readback-test-{run_id[:12]}" if run_id else f"native-xray-readback-test-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="fwrouter-xray-readback-", dir=tmp_path) as temp_dir:
        root = pathlib.Path(temp_dir)
        root.chmod(0o755)
        monkeypatch.setattr(xray_real, "DOCKER_CLI_STATE_DIR", root / "docker-cli")
        config_path = root / "config.json"
        config_path.write_text(json.dumps({
            "log": {"loglevel": "none"},
            "api": {"tag": "fwrouter-api", "services": ["StatsService", "HandlerService"]},
            "stats": {},
            "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
            "inbounds": [
                {"listen": "127.0.0.1", "port": 10085, "protocol": "dokodemo-door",
                 "tag": "fwrouter-api", "settings": {"address": "127.0.0.1"}},
                {"listen": "127.0.0.1", "port": 5300, "protocol": "vless", "tag": "vless-ws",
                 "settings": {"clients": [{"id": expected[0], "email": expected[1]}], "decryption": "none"},
                 "streamSettings": {"network": "ws", "wsSettings": {"path": "/vless"}}},
            ],
            "outbounds": [
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "freedom", "tag": "fwrouter-api"},
            ],
            "routing": {"rules": [
                {"type": "field", "inboundTag": ["fwrouter-api"], "outboundTag": "fwrouter-api"},
            ]},
        }), encoding="utf-8")
        config_path.chmod(0o644)
        replacement = json.loads(config_path.read_text(encoding="utf-8"))
        replacement["inbounds"][1]["settings"]["clients"] = [{
            "id": "22222222-2222-4222-8222-222222222222", "email": "replacement@example.test",
        }]
        replacement_identity = (
            "22222222-2222-4222-8222-222222222222", "replacement@example.test",
        )

        ownership_labels = (["--label", "io.fwrouter.acceptance.owner=fwrouter-test-harness-v1",
                             "--label", f"io.fwrouter.acceptance.run={run_id}"] if run_id else [])
        started = subprocess.run(
            ["docker", "run", "--pull=never", "--detach", "--rm", "--network", "none",
             "--cpus=0.5", "--memory=128m", "--pids-limit=64", "--cap-drop=ALL",
             "--security-opt=no-new-privileges", "--user", "65534:65534", *ownership_labels,
             "--name", container, "-v", f"{config_path}:/etc/xray/config.json:ro", image,
             "run", "-config", "/etc/xray/config.json"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        assert started.stdout.strip()
        try:
            deadline = time.monotonic() + 10
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    adapter = RealXrayAdapter(
                        config_path=config_path,
                        compose_path=root / "unused-compose.yml",
                        runner=lambda action, payload: _isolated_runner(container, action, payload),
                    )
                    isolated_runner = lambda action, payload: _isolated_runner(container, action, payload)
                    adapter._runner = lambda action, payload: (
                        adapter._default_runner(action, payload)
                        if action == "runtime_config_archive"
                        else isolated_runner(action, payload)
                    )
                    identities = adapter.list_loaded_client_identities()
                    break
                except Exception as exc:
                    last_error = type(exc).__name__
                    time.sleep(0.2)
            else:
                pytest.fail(f"isolated Xray HandlerService did not become ready ({last_error})")
            assert identities == [expected]
            assert adapter.get_runtime_config_sha256() == hashlib.sha256(config_path.read_bytes()).hexdigest()

            old_incarnation = adapter.get_runtime_incarnation()
            staged_path = root / "config.next.json"
            staged_path.write_text(json.dumps(replacement), encoding="utf-8")
            staged_path.chmod(0o644)
            os.replace(staged_path, config_path)
            expected_host_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
            restarted = subprocess.run(
                ["docker", "restart", container], check=True, capture_output=True,
                text=True, timeout=10,
            )
            assert restarted.stdout.strip() == container
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    identities = adapter.list_loaded_client_identities()
                    if identities == [replacement_identity]:
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            else:
                pytest.fail("restarted isolated Xray did not load the atomically replaced identity")
            assert adapter.get_runtime_incarnation() != old_incarnation
            copied = root / "mounted-config.json"
            subprocess.run(
                ["docker", "cp", f"{container}:/etc/xray/config.json", str(copied)],
                check=True, capture_output=True, timeout=8,
            )
            assert hashlib.sha256(copied.read_bytes()).hexdigest() == expected_host_sha
            assert adapter.get_runtime_config_sha256() == expected_host_sha
            assert identities == [replacement_identity]

            empty_config = json.loads(config_path.read_text(encoding="utf-8"))
            empty_config["inbounds"][1]["settings"]["clients"] = []
            empty_path = root / "config.empty.json"
            empty_path.write_text(json.dumps(empty_config), encoding="utf-8")
            empty_path.chmod(0o644)
            os.replace(empty_path, config_path)
            empty_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
            restarted = subprocess.run(
                ["docker", "restart", container], check=True, capture_output=True,
                text=True, timeout=10,
            )
            assert restarted.stdout.strip() == container
            deadline = time.monotonic() + 10
            last_error = None
            while time.monotonic() < deadline:
                try:
                    identities = adapter.list_loaded_client_identities()
                    if identities == []:
                        break
                except XrayAdapterError as exc:
                    if exc.code not in {"XRAY_API_READBACK_FAILED", "XRAY_API_READBACK_TIMEOUT"}:
                        raise
                    last_error = exc.code
                time.sleep(0.2)
            else:
                pytest.fail(f"restarted isolated Xray did not return an empty HandlerService user list ({last_error})")
            assert adapter.get_runtime_config_sha256() == empty_sha
        finally:
            subprocess.run(["docker", "stop", container], check=False, capture_output=True, timeout=8)


def _isolated_runner(
    container: str, action: str, payload: dict[str, object],
) -> XrayApplyResult:
    if action == "api_inbound_users":
        assert payload == {"tag": "vless-ws"}
        command = ["docker", "exec", container, "xray", "api", "inbounduser", "--server=127.0.0.1:10085",
                   "-timeout=3", "-tag=vless-ws"]
    elif action == "runtime_container_id":
        assert payload == {}
        command = ["docker", "inspect", "--format", "{{.Id}}", container]
    elif action == "runtime_inspect":
        command = ["docker", "inspect", "--format", "{{.Id}}|{{.State.Running}}|{{.State.StartedAt}}", container]
    elif action == "runtime_config_archive":
        command = ["docker", "cp", f"{container}:/etc/xray/config.json", "-"]
    else:
        raise AssertionError(action)
    completed = subprocess.run(
        command,
        check=False, capture_output=True, text=action != "runtime_config_archive", timeout=8,
    )
    if action == "runtime_config_archive":
        return XrayApplyResult(
            ok=completed.returncode == 0, message="isolated mounted config archive",
            error_code=None if completed.returncode == 0 else "XRAY_RUNTIME_CONFIG_READ_FAILED",
            details={"archive_bytes": completed.stdout if isinstance(completed.stdout, bytes) else b""},
        )
    return XrayApplyResult(
        ok=completed.returncode == 0,
        message="isolated native API readback",
        error_code=(None if completed.returncode == 0 else
                    "XRAY_API_READBACK_FAILED" if action == "api_inbound_users" else
                    f"ISOLATED_DOCKER_EXIT_{completed.returncode}"),
        details={"stdout": completed.stdout},
    )
