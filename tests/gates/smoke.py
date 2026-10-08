#!/usr/bin/env python3
"""Bounded isolated and explicitly opt-in live-readonly FWRouter smoke checks."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SCHEMA = BACKEND / "fwrouter_api/db/schema.sql"
INSTALLER = ROOT / "installer/install.sh"
MAX_TIMEOUT = 30
MAX_OUTPUT = 16_384
MAX_HTTP_BODY = 32_768


class SmokeError(ValueError):
    pass


def _execute(argv: Sequence[str], *, timeout: int, env: Mapping[str, str], cwd: Path,
             runner: Callable[..., Any]) -> tuple[str, int | None, str]:
    if not argv or timeout < 1 or timeout > MAX_TIMEOUT:
        raise SmokeError("invalid bounded command")
    if runner is subprocess.run:
        # Reuse gate.py's bounded pipe reader and process-group termination.
        spec = importlib.util.spec_from_file_location("_fwrouter_smoke_gate", ROOT / "tests/gates/gate.py")
        if spec is None or spec.loader is None:
            return "unavailable", None, ""
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        code, output, exceeded = module.run_process(list(argv), timeout, MAX_OUTPUT, cwd=cwd, env=dict(env))
        if code == 124:
            return "timeout", code, ""
        if code == 125 or exceeded:
            return "output_limit", code, ""
        return ("passed" if code == 0 else "failed"), code, output[:MAX_OUTPUT]
    try:
        proc = runner(list(argv), cwd=cwd, env=dict(env), timeout=timeout,
                      capture_output=True, text=True, check=False)
    except subprocess.TimeoutExpired:
        return "timeout", None, ""
    except (OSError, ValueError):
        return "unavailable", None, ""
    output = str(getattr(proc, "stdout", "") or "")[:MAX_OUTPUT]
    code = getattr(proc, "returncode", 1)
    status = ("blocked_not_verified" if code == 0 else "failed")
    return status, int(code), output


def _safe_run(argv: Sequence[str], *, timeout: int, env: Mapping[str, str], cwd: Path,
              runner: Callable[..., Any]) -> dict[str, Any]:
    status, code, _ = _execute(argv, timeout=timeout, env=env, cwd=cwd, runner=runner)
    return {"status": status, "returncode": code,
            "evidence_mode": "bounded_local_process" if runner is subprocess.run else "injected_test_runner"}


def _create_current_schema(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def _expected_schema_version() -> str:
    source = (BACKEND / "fwrouter_api/db/schema_state.py").read_text(encoding="utf-8")
    match = re.search(r'^EXPECTED_SCHEMA_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if match is None:
        raise SmokeError("canonical schema version constant is unavailable")
    return match.group(1)


def _readonly_db(db_path: Path) -> dict[str, Any]:
    expected_version = _expected_schema_version()
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()[0] == 1
        tables = int(connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0])
        try:
            row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            version = str(row[0]) if row else None
        except sqlite3.OperationalError:
            version = None
        valid = query_only and tables > 0 and version == expected_version
        return {"status": "passed" if valid else "failed", "query_only": query_only,
                "user_table_count": tables, "schema_version_valid": version == expected_version}
    finally:
        connection.close()


def _api_payload_check(result: Mapping[str, Any], kind: str) -> bool:
    if result.get("status_code") != 200 or result.get("api_ok") is not True:
        return False
    fields = result.get("fields")
    if not isinstance(fields, Mapping):
        return False
    if kind == "health":
        return (fields.get("service") == "fwrouter-api" and fields.get("status") == "healthy"
                and fields.get("database_status") == "healthy" and fields.get("schema_ok") is True)
    return fields.get("critical_state_present") is True


def _default_http_get(url: str, timeout: int, kind: str) -> Mapping[str, Any]:
    """Perform GET only, reject redirects, parse only allowlisted response fields."""
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                             headers: Any, newurl: str) -> None:
            return None
    try:
        response = build_opener(NoRedirect).open(Request(url, method="GET"), timeout=timeout)
    except (HTTPError, URLError, OSError):
        return {"status_code": None, "api_ok": False, "fields": {}}
    try:
        raw = response.read(MAX_HTTP_BODY + 1)
        if len(raw) > MAX_HTTP_BODY or response.status != 200:
            return {"status_code": response.status, "api_ok": False, "fields": {}}
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return {"status_code": getattr(response, "status", None), "api_ok": False, "fields": {}}
    finally:
        response.close()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return {"status_code": 200, "api_ok": False, "fields": {}}
    data = payload["data"]
    if kind == "health":
        db = data.get("database") if isinstance(data.get("database"), dict) else {}
        schema = db.get("schema") if isinstance(db.get("schema"), dict) else {}
        fields = {"service": data.get("service"), "status": data.get("status"),
                  "database_status": db.get("status"), "schema_ok": schema.get("ok") is True}
    else:
        fields = {"critical_state_present": isinstance(data.get("state"), dict)}
    return {"status_code": 200, "api_ok": payload.get("ok") is True, "fields": fields}


def _http_check(adapter: Callable[..., Mapping[str, Any]] | None, url: str, timeout: int,
                kind: str) -> dict[str, Any]:
    if adapter is None:
        result = _default_http_get(url, timeout, kind)
    else:
        try:
            result = adapter(url, timeout)
        except Exception:
            result = {"status_code": None, "api_ok": False, "fields": {}}
    return {"status": "passed" if _api_payload_check(result, kind) else "failed",
            "status_code": result.get("status_code") if isinstance(result.get("status_code"), int) else None,
            "contract": "health_schema_and_database" if kind == "health" else "critical_state_projection"}


def _isolated_fastapi_checks(env: Mapping[str, str], *, runner: Callable[..., Any],
                             timeout: int) -> list[dict[str, Any]]:
    """Run real read-only routes in a fresh child after the isolation bootstrap."""
    blocked = [
        {"name": "health_api", "status": "blocked_not_verified",
         "reason": "guarded_fastapi_child_unavailable"},
        {"name": "critical_state_api", "status": "blocked_not_verified",
         "reason": "guarded_fastapi_child_unavailable"},
    ]
    # Injected command runners exist for unit contracts; they are not evidence
    # that an application child was launched or isolated.
    if runner is not subprocess.run:
        return blocked
    child = r'''
import importlib.util, json, os, sqlite3, sys
from pathlib import Path
sys.dont_write_bytecode = True
bootstrap_path, backend_path, schema_path = map(Path, sys.argv[1:4])
spec = importlib.util.spec_from_file_location("fwrouter_smoke_isolation", bootstrap_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
owned_root, state_root = module.configure_test_process()
sys.path.insert(0, str(backend_path))
checks = []
try:
    from fwrouter_api.core.config import Settings, get_settings
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from fwrouter_api.main import create_app
    from fwrouter_api.routes import state as state_routes
    from fwrouter_api.routes import system as system_routes
    db_path = state_root / "fwrouter-smoke.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()

    def readonly_schema():
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            from fwrouter_api.db.schema_state import inspect_database_schema
            return inspect_database_schema(connection)
        finally:
            connection.close()

    # This fixture projection intentionally remains partial L4 evidence.
    from unittest.mock import patch
    with patch.object(system_routes, "get_cached_schema_state", side_effect=readonly_schema), \
         patch.object(state_routes, "build_system_state_projection",
                      side_effect=lambda: {"schema": {"ok": True}}):
        with TestClient(create_app(enable_startup_tasks=False)) as client:
            health = client.get("/api/v2/health", follow_redirects=False)
            state = client.get("/api/v2/state/system", follow_redirects=False)

    def projection(response, kind):
        try:
            payload = response.json()
        except Exception:
            payload = {}
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if kind == "health":
            db = data.get("database", {}) if isinstance(data, dict) else {}
            schema = db.get("schema", {}) if isinstance(db, dict) else {}
            fields = {"service": data.get("service"), "status": data.get("status"),
                      "database_status": db.get("status"), "schema_ok": schema.get("ok") is True}
        else:
            value = data.get("state") if isinstance(data, dict) else None
            fields = {"critical_state_present": isinstance(value, dict)}
        return {"status_code": response.status_code,
                "api_ok": isinstance(payload, dict) and payload.get("ok") is True,
                "fields": fields}

    checks = [
        {"name": "health_api", "status": "pending", "evidence_mode": "guarded_fastapi_testclient"},
        {"name": "critical_state_api", "status": "pending",
         "evidence_mode": "guarded_fastapi_testclient_fixture_projection"},
    ]
    health_result = projection(health, "health")
    health_fields = health_result["fields"]
    checks[0]["status"] = "passed" if (
        health_result["status_code"] == 200 and health_result["api_ok"] is True
        and health_fields.get("service") == "fwrouter-api"
        and health_fields.get("status") == "healthy"
        and health_fields.get("database_status") == "healthy"
        and health_fields.get("schema_ok") is True
    ) else "failed"
    state_result = projection(state, "state")
    checks[1]["status"] = "passed" if (
        state_result["status_code"] == 200 and state_result["api_ok"] is True
        and state_result["fields"].get("critical_state_present") is True
    ) else "failed"
except ImportError:
    checks = [{"name": name, "status": "blocked_not_verified",
               "reason": "fastapi_testclient_dependencies_unavailable"}
              for name in ("health_api", "critical_state_api")]
except Exception as exc:
    checks = [{"name": name, "status": "failed", "reason": type(exc).__name__}
              for name in ("health_api", "critical_state_api")]
finally:
    module.cleanup_owned_root()
    cleanup_ok = not owned_root.exists()
print(json.dumps({"checks": checks, "cleanup_ok": cleanup_ok,
                  "owned_root": str(owned_root)}, sort_keys=True))
'''
    env_clean = {key: value for key, value in env.items()
                 if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"}}
    command = [sys.executable, "-I", "-c", child, str(BACKEND / "tests/_isolation_bootstrap.py"),
               str(BACKEND), str(SCHEMA)]
    state, _code, output = _execute(command, timeout=timeout, env=env_clean,
                                    cwd=ROOT, runner=runner)
    if state != "passed" or len(output.encode("utf-8", "replace")) > MAX_OUTPUT:
        return blocked
    try:
        payload = json.loads(output)
        checks = payload["checks"]
        if not isinstance(checks, list) or {row.get("name") for row in checks} != {"health_api", "critical_state_api"}:
            return blocked
        owned_root = payload.get("owned_root")
        if (payload.get("cleanup_ok") is not True or not isinstance(owned_root, str)
                or Path(owned_root).exists()):
            return [{"name": row["name"], "status": "failed",
                     "reason": "guarded_child_cleanup_failed"} for row in blocked]
        return checks
    except (KeyError, TypeError, json.JSONDecodeError):
        return blocked


def _native_checks(validators: Sequence[Mapping[str, str]] | None, runner: Callable[..., Any],
                   env: Mapping[str, str], scratch: Path, *, require_parity: bool = False) -> list[dict[str, Any]]:
    rows = list(validators or [])
    found = {row.get("kind") for row in rows}
    checks: list[dict[str, Any]] = []
    for missing in ("mihomo", "xray"):
        if missing not in found:
            checks.append({"name": f"native_{missing}", "status": "blocked_not_verified",
                           "reason": "explicit_pinned_binary_and_config_required"})
    if not rows:
        return checks
    for row in rows:
        binary, config = Path(row.get("binary", "")), Path(row.get("config", ""))
        expected_hash, kind = row.get("sha256", "").lower(), row.get("kind")
        name = f"native_{kind}" if kind in {"mihomo", "xray"} else "native_unknown"
        if kind not in {"mihomo", "xray"} or not binary.is_absolute() or not config.is_absolute() or not expected_hash:
            checks.append({"name": name, "status": "blocked_not_verified", "reason": "unsupported_or_incomplete_validator"})
            continue
        try:
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        except OSError:
            checks.append({"name": name, "status": "blocked_not_verified", "reason": "binary_unavailable"})
            continue
        if digest != expected_hash or not config.is_file():
            checks.append({"name": name, "status": "blocked_not_verified", "reason": "hash_or_config_mismatch"})
            continue
        if require_parity:
            generated, mounted = Path(row.get("generated_path", "")), Path(row.get("mounted_path", ""))
            if not generated.is_absolute() or not mounted.is_absolute() or not generated.is_file() or not mounted.is_file():
                checks.append({"name": name, "status": "blocked_not_verified", "reason": "generated_or_mounted_config_missing"})
                continue
            try:
                parity = hashlib.sha256(generated.read_bytes()).digest() == hashlib.sha256(mounted.read_bytes()).digest()
            except OSError:
                parity = False
            if not parity or generated.resolve() != config.resolve():
                checks.append({"name": name, "status": "failed", "reason": "generated_mounted_hash_parity_failed"})
                continue
        argv = ([str(binary), "-t", "-d", str(scratch), "-f", str(config)] if kind == "mihomo" else
                [str(binary), "run", "-test", "-config", str(config)])
        checks.append({"name": name, **_safe_run(argv, timeout=20, env=env, cwd=scratch, runner=runner),
                       "binary_sha256_match": True, "scratch_owned": True})
    return checks


def _report(profile: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {row.get("status") for row in checks}
    known = {"passed", "not_requested", "blocked_not_verified", "unavailable", "failed", "timeout", "output_limit"}
    if not statuses or not statuses <= known:
        status = "failed"
    elif statuses & {"failed", "timeout", "output_limit"}:
        status = "failed"
    elif statuses & {"blocked_not_verified", "unavailable"}:
        status = "blocked_not_verified"
    else:
        status = "passed"
    return {"schema_version": 1, "profile": profile, "status": status, "checks": checks}


def run_smoke(profile: str, *, reason: str | None = None, opt_in: bool = False,
              db_path: str | Path | None = None, api_base_url: str | None = None,
              http_get: Callable[..., Mapping[str, Any]] | None = None,
              validators: Sequence[Mapping[str, str]] | None = None,
              failed_units_allowlist: Sequence[str] | None = None,
              require_native: bool = False,
              command_runner: Callable[..., Any] = subprocess.run,
              timeout_seconds: int = 20) -> dict[str, Any]:
    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT:
        raise SmokeError("timeout_seconds must be between 1 and 30")
    if profile == "isolated":
        with tempfile.TemporaryDirectory(prefix="fwrouter-smoke-", dir="/tmp") as temp:
            owned = Path(temp).resolve()
            target, state = owned / "target", owned / "state"
            target.mkdir(mode=0o700); state.mkdir(mode=0o700)
            db = state / "fwrouter.db"
            _create_current_schema(db)
            env = {"PATH": os.defpath, "HOME": str(owned), "TMPDIR": str(owned),
                   "LC_ALL": "C.UTF-8", "FWROUTER_STATE_DIR": str(state),
                   "FWROUTER_STARTUP_TASKS_ENABLED": "0", "FWROUTER_WATCHDOG_SCHEDULER_ENABLED": "0",
                   "FWROUTER_MAINTENANCE_SCHEDULER_ENABLED": "0", "FWROUTER_MEMBER_PROBE_SCHEDULER_ENABLED": "0",
                   "FWROUTER_ACTIVE_OBSERVATION_SCHEDULER_ENABLED": "0", "FWROUTER_SUBJECT_INVENTORY_SCHEDULER_ENABLED": "0",
                   "FWROUTER_EXTERNAL_COLLECTOR_SCHEDULER_ENABLED": "0", "FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED": "0"}
            install = _safe_run(["sh", str(INSTALLER), "--deploy", "--component", "backend", "--target", str(target)],
                                timeout=timeout_seconds, env=env, cwd=ROOT, runner=command_runner)
            checks = [{"name": "installer_deploy_to_owned_temp_target", **install},
                      {"name": "temporary_schema_readonly", **_readonly_db(db)}]
            if http_get is None:
                checks.extend(_isolated_fastapi_checks(env, runner=command_runner,
                                                      timeout=timeout_seconds))
                api_mode = ("guarded_child_fastapi_testclient" if command_runner is subprocess.run
                            else "blocked_injected_process_runner")
            else:
                checks.extend([{"name": "health_api", **_http_check(http_get, "http://127.0.0.1:5000/api/v2/health", timeout_seconds, "health"), "evidence_mode": "injected_test_adapter"},
                               {"name": "critical_state_api", **_http_check(http_get, "http://127.0.0.1:5000/api/v2/state/system", timeout_seconds, "state"), "evidence_mode": "injected_test_adapter"}])
                api_mode = "injected_test_adapter"
            if require_native:
                checks.extend(_native_checks(validators, command_runner, {**env, "HOME": str(owned)}, owned))
            else:
                checks.append({"name": "native_validation", "status": "not_requested",
                               "scope": "component_l4_only"})
            report = _report(profile, checks)
            report.update({"api_evidence_mode": api_mode,
                           "isolation": {"target_owned_temp": True, "database_owned_temp": True,
                                          "production_env_imported": False, "api_mutations": False,
                                          "provider_calls": False, "public_network": False}})
            return report
    if profile != "live-readonly":
        raise SmokeError("profile must be isolated or live-readonly")
    if not opt_in or not reason or not reason.strip():
        return _report(profile, [{"name": "authorization", "status": "blocked_not_verified",
                                 "reason": "live_readonly_requires_explicit_opt_in_and_reason"}])
    if not db_path or not api_base_url:
        raise SmokeError("live-readonly requires explicit api_base_url and db_path")
    parsed = urlsplit(api_base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise SmokeError("live API URL must be bare loopback HTTP")
    database = Path(db_path).resolve(strict=True)
    if not database.is_file():
        raise SmokeError("explicit db_path must name a file")
    checks = [{"name": "sqlite_readonly_allowlisted_metadata", **_readonly_db(database)},
              {"name": "health_api", **_http_check(http_get, api_base_url.rstrip("/") + "/api/v2/health", timeout_seconds, "health")},
              {"name": "critical_state_api", **_http_check(http_get, api_base_url.rstrip("/") + "/api/v2/state/system", timeout_seconds, "state")}]
    with tempfile.TemporaryDirectory(prefix="fwrouter-smoke-native-", dir="/tmp") as temp:
        scratch = Path(temp).resolve()
        env = {"PATH": os.defpath, "HOME": str(scratch), "TMPDIR": str(scratch), "LC_ALL": "C.UTF-8"}
        checks.extend(_native_checks(validators, command_runner, env, scratch, require_parity=True))
        if failed_units_allowlist is None:
            checks.append({"name": "failed_units_review", "status": "blocked_not_verified",
                           "reason": "explicit_exact_failed_unit_allowlist_required"})
        else:
            if any(not isinstance(unit, str) or not unit.endswith((".service", ".timer", ".socket")) or "/" in unit for unit in failed_units_allowlist):
                raise SmokeError("failed unit allowlist must contain exact systemd unit names")
            unit_argv = ["systemctl", "--failed", "--no-legend", "--plain", "--type=service"]
            state, code, output = _execute(unit_argv, timeout=10, env=env, cwd=scratch, runner=command_runner)
            if state != "passed":
                checks.append({"name": "failed_units_review", "status": "blocked_not_verified", "reason": "systemd_readonly_query_unavailable",
                               "evidence_mode": "injected_test_runner" if command_runner is not subprocess.run else "bounded_local_process"})
            else:
                observed = {line.split()[0] for line in output.splitlines() if line.split()}
                unexpected = observed - set(failed_units_allowlist)
                checks.append({"name": "failed_units_review", "status": "failed" if unexpected else "passed",
                               "failed_count": len(observed), "unexpected_failed_count": len(unexpected),
                               "allowlist_exact": True})
    checks.append({"name": "mutation_guard", "status": "passed", "http_methods": ["GET"],
                   "redirects": "rejected", "provider_calls": False,
                   "installer_or_restart": False, "api_mutations": False})
    report = _report(profile, checks)
    report["authorization"] = {"opt_in": True, "reason_recorded": True,
                               "api_host_class": "loopback", "database_path_explicit": True}
    return report


def _load_cli_json(path_value: str | None, *, kind: str) -> Any:
    if path_value is None:
        return None
    path = Path(path_value).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 32_768:
        raise SmokeError(f"{kind} JSON file missing or exceeds size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"invalid {kind} JSON") from exc
    if kind == "validators":
        if not isinstance(value, list):
            raise SmokeError("validators JSON must be a list")
        for row in value:
            if not isinstance(row, dict) or row.get("kind") not in {"mihomo", "xray"}:
                raise SmokeError("validator row must use a supported kind")
            for key in ("binary", "config", "sha256"):
                if not isinstance(row.get(key), str) or not row[key]:
                    raise SmokeError("validator row is missing a required value")
            if not Path(row["binary"]).is_absolute() or not Path(row["config"]).is_absolute():
                raise SmokeError("validator binary/config paths must be absolute")
            if len(row["sha256"]) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in row["sha256"]):
                raise SmokeError("validator sha256 must be a 64-character digest")
        return value
    if kind == "failed-units-allowlist":
        if not isinstance(value, list) or any(not isinstance(unit, str) or
            not unit.endswith((".service", ".timer", ".socket")) or "/" in unit for unit in value):
            raise SmokeError("failed-units allowlist JSON must be an exact unit-name list")
        return value
    raise SmokeError("unsupported JSON argument kind")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("isolated", "live-readonly"), required=True)
    parser.add_argument("--reason"); parser.add_argument("--opt-in", action="store_true")
    parser.add_argument("--api-base-url"); parser.add_argument("--db-path")
    parser.add_argument("--validators-json", help="explicit pinned native validator JSON file")
    parser.add_argument("--complete-native", action="store_true", help="require both pinned Mihomo and Xray checks")
    parser.add_argument("--failed-units-allowlist-json", help="explicit reviewed exact failed-unit JSON list")
    args = parser.parse_args(argv)
    try:
        validators = _load_cli_json(args.validators_json, kind="validators")
        failed_units = _load_cli_json(args.failed_units_allowlist_json, kind="failed-units-allowlist")
        report = run_smoke(args.profile, reason=args.reason, opt_in=args.opt_in,
                           api_base_url=args.api_base_url, db_path=args.db_path,
                           validators=validators, failed_units_allowlist=failed_units,
                           require_native=args.complete_native)
    except (SmokeError, OSError, sqlite3.Error) as exc:
        report = {"schema_version": 1, "profile": args.profile, "status": "failed", "error": type(exc).__name__}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
