from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
import uuid
from typing import Any

from .http_support import http_json


TOP_LEVEL_RULE_COUNT = 199_257
MIN_CONFIG_BYTES = 20_000_000
RSS_LIMIT_KIB = 1_572_864
API_WAVE = (
    "/health",
    "/selector/vpn-auto/state",
    "/mihomo",
    "/runtime",
    "/mihomo",
    "/selector/vpn-auto/state",
)


def _write_scale_config(path: Path) -> dict[str, Any]:
    """Write a large, inert ruleset without retaining the whole document in memory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    config_bytes = 0
    transparent_bytes = 0

    def write(stream, value: str, *, transparent: bool = False) -> None:
        nonlocal config_bytes, transparent_bytes
        encoded = value.encode("ascii")
        stream.write(encoded)
        digest.update(encoded)
        config_bytes += len(encoded)
        if transparent:
            transparent_bytes += len(encoded)

    with path.open("wb") as stream:
        for line in (
            "allow-lan: false\n",
            "bind-address: 127.0.0.1\n",
            "mode: rule\n",
            "log-level: warning\n",
            "external-controller: 127.0.0.1:5200\n",
            "mixed-port: 5201\n",
            "proxies: []\n",
            "proxy-groups:\n",
            "  - name: vpn-auto\n",
            "    type: select\n",
            "    proxies: [REJECT]\n",
            "  - name: vpn-global\n",
            "    type: select\n",
            "    proxies: [vpn-auto, REJECT]\n",
            "rules:\n",
        ):
            write(stream, line)
        def rule_sequence(indent: str, *, transparent: bool = False) -> None:
            write(stream, indent + "DOMAIN,protected.test,DIRECT\n", transparent=transparent)
            for index in range((TOP_LEVEL_RULE_COUNT - 3) // 2):
                domain = f"r{index:06d}.test"
                write(stream, f"{indent}DOMAIN-SUFFIX,{domain},vpn-global\n", transparent=transparent)
                write(stream, f"{indent}AND,((NETWORK,udp),(DOMAIN-SUFFIX,{domain})),REJECT\n", transparent=transparent)
            write(stream, indent + "MATCH,vpn-global\n", transparent=transparent)
            write(stream, indent + "NETWORK,udp,REJECT\n", transparent=transparent)

        rule_sequence("  - ")
        write(stream, "sub-rules:\n  fwrouter-transparent:\n")
        rule_sequence("    - ", transparent=True)
        transparent_count = TOP_LEVEL_RULE_COUNT
        write(stream, "  fwrouter-full-vpn:\n")
        write(stream, "    - MATCH,vpn-global\n")
        write(stream, "    - NETWORK,udp,REJECT\n")
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o600)

    return {
        "config_bytes": config_bytes,
        "config_sha256": digest.hexdigest(),
        "top_level_rule_count": TOP_LEVEL_RULE_COUNT,
        "transparent_subrule_count": transparent_count,
        "transparent_subrule_bytes": transparent_bytes,
        "full_vpn_fallback": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
        "selector_targets": {"vpn-auto": ["REJECT"], "vpn-global": ["vpn-auto", "REJECT"]},
    }


def _read_rss_kib(pid: int, *, field: str = "VmRSS") -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


class _RssSampler:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, name="scale-rss-sampler", daemon=True)
        self.samples: list[int] = []
        self.high_water_samples: list[int] = []

    def _sample(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(0.05)

    def start(self) -> None:
        self._sample_once()
        self._thread.start()

    def _sample_once(self) -> None:
        value = _read_rss_kib(self.pid)
        if value is not None:
            self.samples.append(value)
        high_water = _read_rss_kib(self.pid, field="VmHWM")
        if high_water is not None:
            self.high_water_samples.append(high_water)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sample_once()
        if self._thread.is_alive():
            raise AssertionError("owned API RSS sampler did not stop")

    @property
    def peak_kib(self) -> int | None:
        return max(self.samples) if self.samples else None


def _stop_owned_worker(worker, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    started = time.monotonic()
    if worker.poll() is None:
        worker.terminate()
    try:
        return_code = worker.wait(timeout=timeout_seconds)
    except Exception as exc:
        return {"elapsed_seconds": round(time.monotonic() - started, 4), "return_code": None,
                "graceful": False, "error_type": type(exc).__name__}
    elapsed = time.monotonic() - started
    return {"elapsed_seconds": round(elapsed, 4), "return_code": return_code,
            "graceful": return_code == 0 and elapsed < timeout_seconds}


def _timed_get(api: str, route: str) -> dict[str, Any]:
    started = time.monotonic()
    code, payload = http_json(api + route, timeout=30.0)
    elapsed = time.monotonic() - started
    result: dict[str, Any] = {
        "route": route,
        "status_code": code,
        "ok": bool(payload.get("ok")),
        "elapsed_seconds": round(elapsed, 4),
    }
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if route == "/mihomo":
        mihomo = data.get("mihomo") if isinstance(data.get("mihomo"), dict) else {}
        details = mihomo.get("details") if isinstance(mihomo.get("details"), dict) else {}
        proof = details.get("vpn_udp_policy") if isinstance(details.get("vpn_udp_policy"), dict) else {}
        result["native_proof"] = {
            key: proof.get(key)
            for key in (
                "native_top_level_rules_match", "guarded_rules_match", "wiring_proven",
                "reconcile_receipt_matches", "policy_installed",
            )
        }
    elif route == "/selector/vpn-auto/state":
        selector = data.get("vpn_auto") if isinstance(data.get("vpn_auto"), dict) else {}
        result["selector_state"] = {
            "problem_code": selector.get("problem_code"),
            "active_auto_server_id": selector.get("active_auto_server_id"),
            "enabled_candidates_count": selector.get("enabled_candidates_count"),
            "runtime_vpn_auto_targets": selector.get("runtime_vpn_auto_targets"),
        }
    elif route == "/runtime":
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        result["runtime_state"] = runtime.get("runtime_state")
    return result


def _run_six_client_wave(api: str) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="runtime-scale-client") as pool:
        futures = [pool.submit(_timed_get, api, route) for route in API_WAVE]
        return [future.result() for future in futures]


def _write_metrics(payload: dict[str, Any]) -> None:
    receipt_dir = Path("/tmp/fwrouter-receipts")
    receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if receipt_dir.is_symlink() or not receipt_dir.is_dir() or receipt_dir.stat().st_uid != os.getuid():
        raise AssertionError("runtime scale metrics directory is not an owned directory")
    target = receipt_dir / "runtime-scale-metrics.json"
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)


def test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load(acceptance_stack) -> None:
    stack = acceptance_stack
    native = stack["native"]
    config_path = Path(stack["state"]) / "generated" / "mihomo" / "config.yaml"
    metrics: dict[str, Any] = {
        "schema": "fwrouter-runtime-scale-metrics/v1",
        "scope": "owned-native-mihomo-and-api-worker",
        "status": "started",
        "limitations": [
            "no host systemd or host dataplane claim",
            "periodic RSS begins after readiness; Linux VmHWM includes preceding process startup",
            "native /rules request counters are unavailable in the current worker harness",
            "fixture has no reconciled Mihomo receipt; policy_installed is expected false",
        ],
        "native_rules_request_count": None,
        "rss_scope": "owned API VmRSS plus process-lifetime Linux VmHWM; not aggregate/native memory",
    }
    sampler: _RssSampler | None = None
    active_worker = stack["worker"]
    worker_was_stopped = False
    try:
        initial_stop = _stop_owned_worker(active_worker)
        worker_was_stopped = initial_stop["return_code"] == 0
        metrics["initial_worker_shutdown"] = initial_stop
        assert worker_was_stopped and initial_stop["elapsed_seconds"] < 8.0, initial_stop

        metrics["config"] = _write_scale_config(config_path)
        assert metrics["config"]["config_bytes"] >= MIN_CONFIG_BYTES
        xray_path = Path(stack["state"]) / "xray" / "config.json"
        xray_config = json.loads(xray_path.read_text())
        inbound = next(row for row in xray_config["inbounds"] if row.get("tag") == "vless-ws")
        inbound["settings"]["clients"] = [
            {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"scale-{index}.test")),
             "email": f"scale-{index}@acceptance.test", "level": 0}
            for index in range(78)
        ]
        xray_path.write_text(json.dumps(xray_config))
        native.restart_xray_child()
        from .xray_support import loaded_identities
        native_identities_before = loaded_identities(native)
        assert len(native_identities_before) == 78
        metrics["synthetic_xray_loaded_clients"] = 78
        native.restart_mihomo_child()
        start = time.monotonic()
        active_worker, api = stack["start_worker"](startup_tasks=True)
        metrics["startup_wall_seconds"] = round(time.monotonic() - start, 4)
        stack["worker"] = active_worker
        stack["api"] = api

        sampler = _RssSampler(active_worker.pid)
        sampler.start()
        metrics["rss_hwm_kib_at_readiness"] = _read_rss_kib(active_worker.pid, field="VmHWM")
        waves: list[list[dict[str, Any]]] = []
        metrics["waves"] = waves
        for wave_number in range(4):
            if wave_number:
                # Let the shared read-only health cache expire between waves so
                # each wave includes one fresh large native readback.
                threading.Event().wait(2.1)
            wave = _run_six_client_wave(api)
            waves.append(wave)
            for result in wave:
                assert result["status_code"] == 200 and result["ok"] is True, result
                route = result["route"]
                bound = 5.0 if route == "/health" else 15.0 if route in {"/mihomo", "/selector/vpn-auto/state"} else 20.0
                assert result["elapsed_seconds"] < bound, result
                if route == "/mihomo":
                    proof = result.get("native_proof") or {}
                    assert proof.get("native_top_level_rules_match") is True, proof
                    assert proof.get("guarded_rules_match") is True, proof
                    assert proof.get("wiring_proven") is True, proof
                    assert proof.get("reconcile_receipt_matches") is False, proof
                    assert proof.get("policy_installed") is False, proof
                elif route == "/selector/vpn-auto/state":
                    state = result.get("selector_state") or {}
                    assert state.get("active_auto_server_id") is None, state
                    assert state.get("enabled_candidates_count") == 0, state
                    assert state.get("runtime_vpn_auto_targets") == ["REJECT"], state

        metrics["waves"] = waves
        metrics["rss_peak_kib_post_ready"] = sampler.peak_kib
        lifetime_peak = max(sampler.high_water_samples) if sampler.high_water_samples else None
        metrics["rss_hwm_kib_process_lifetime"] = lifetime_peak
        assert lifetime_peak is not None and lifetime_peak < RSS_LIMIT_KIB, {"rss_hwm_kib": lifetime_peak}
        assert sampler.peak_kib is not None and sampler.peak_kib < RSS_LIMIT_KIB, {
            "rss_peak_kib_post_ready": sampler.peak_kib,
            "limit_kib": RSS_LIMIT_KIB,
        }

        from .http_support import http_json as _http_json

        auto_code, auto_state = _http_json("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3.0)
        assert auto_code == 200 and auto_state.get("now") == "REJECT", {
            "status_code": auto_code, "selected_reject": auto_state.get("now") == "REJECT",
        }
        assert auto_state.get("all") == ["REJECT"], {"auto_targets": auto_state.get("all")}
        assert loaded_identities(native) == native_identities_before
        metrics["synthetic_xray_identities_preserved"] = True
        metrics["native_empty_selector"] = {"status_code": auto_code, "selected_reject": True, "targets": ["REJECT"]}
        metrics["status"] = "passed"
    except BaseException as exc:
        metrics["status"] = "failed"
        metrics["failure_type"] = type(exc).__name__
        raise
    finally:
        if sampler is not None:
            sampler.stop()
            metrics["rss_peak_kib_post_ready"] = sampler.peak_kib
            metrics["rss_hwm_kib_process_lifetime"] = max(sampler.high_water_samples) if sampler.high_water_samples else None
        if active_worker.poll() is None:
            shutdown = _stop_owned_worker(active_worker)
            metrics["final_worker_shutdown"] = shutdown
            if shutdown.get("return_code") != 0 or not shutdown.get("graceful"):
                metrics["status"] = "failed"
                _write_metrics(metrics)
                raise AssertionError(f"restarted API worker failed graceful SIGTERM: {shutdown}")
        _write_metrics(metrics)
