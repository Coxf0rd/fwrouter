"""Reproduce the isolated old-source/current-source binding-file profile.

Run from any working directory with the FWRouter project Python environment:

    cd /srv/fwrouter && /opt/fwrouter-api/.venv/bin/python knowledge/audits/residual_runtime_2026-10-07/scoped_egress_binding_file_ab.py

Only a temporary state directory and synthetic JSON file are used. The script
prints aggregate counts, a digest, and timings; it does not print subject data
or filesystem paths.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any


BASELINE = "8d137b7"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SUBJECT_COUNT = 315
EXPLICIT_COUNT = 131
ROUNDS = 4


def _load_baseline_module(directory: Path) -> ModuleType:
    source = subprocess.check_output(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "show",
            f"{BASELINE}:backend/fwrouter_api/services/scoped_egress.py",
        ],
        text=True,
    )
    source_path = directory / "scoped_egress_baseline.py"
    source_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("scoped_egress_baseline", source_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load baseline source module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_fixture() -> list[tuple[dict[str, Any], str, str | None, str | None]]:
    subjects = []
    for index in range(SUBJECT_COUNT):
        if index < EXPLICIT_COUNT:
            subject_id = f"xray-{index}"
            if index < 110:
                active, path, target, source = True, "direct", None, None
            elif index < 120:
                active, path, target, source = False, "vpn", "server-fixed", "global_fixed"
            elif index < 129:
                active, path, target, source = True, "vpn", None, None
            elif index == 129:
                active, path, target, source = True, "vpn", "server-fixed", "global_fixed"
            else:
                active, path, target, source = True, "vpn", None, "vpn_auto"
            subject = {
                "subject_id": subject_id,
                "subject_type": "explicit_external_client",
                "implementation_kind": "xray",
                "is_active": active,
            }
        else:
            subject = {
                "subject_id": f"lan-{index}",
                "subject_type": "lan",
                "is_active": True,
                "detail": {"ip_address": f"10.20.{index // 254}.{index % 254 + 1}"},
            }
            path, target, source = "vpn", "server-fixed", "global_fixed"
        subjects.append((subject, path, target, source))
    return subjects


def _profile(module: ModuleType, bindings_path: Path, subjects: list[tuple[dict[str, Any], str, str | None, str | None]]) -> dict[str, Any]:
    module._xray_bindings_path = lambda: bindings_path
    counts = {"read_text": 0, "json_parse": 0}
    original_read_text = Path.read_text
    original_json_loads = json.loads

    def counted_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == bindings_path:
            counts["read_text"] += 1
        return original_read_text(path, *args, **kwargs)

    def counted_json_loads(*args: Any, **kwargs: Any) -> Any:
        counts["json_parse"] += 1
        return original_json_loads(*args, **kwargs)

    Path.read_text = counted_read_text
    json.loads = counted_json_loads
    outputs = []
    timings_ms = []
    try:
        for _ in range(ROUNDS):
            started = time.perf_counter()
            round_output = [
                module.build_scoped_subject_runtime(
                    subject,
                    dataplane_path=path,
                    selected_server_id=target,
                    selected_server_source=source,
                    server_override=None,
                    vpn_supported=True,
                    bypass_enabled=False,
                )
                for subject, path, target, source in subjects
            ]
            timings_ms.append((time.perf_counter() - started) * 1000)
            outputs.extend(round_output)
    finally:
        Path.read_text = original_read_text
        json.loads = original_json_loads

    digest = hashlib.sha256(
        json.dumps(outputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "binding_file_read_text_calls": counts["read_text"],
        "binding_json_parse_calls": counts["json_parse"],
        "last_round_wall_ms": round(timings_ms[-1], 3),
        "output_sha256": digest,
    }


def main() -> None:
    sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
    from fwrouter_api.services import scoped_egress as current

    with tempfile.TemporaryDirectory(prefix="fwrouter-binding-ab-") as temp_root:
        root = Path(temp_root)
        state_dir = root / "state"
        bindings_path = state_dir / "xray" / "fwrouter-bindings.json"
        bindings_path.parent.mkdir(parents=True)
        entries = [
            {
                "subject_id": f"xray-{index}",
                "status": "applied",
                "selected_server_id": "server-fixed" if index == 129 else "vpn-global",
            }
            for index in range(EXPLICIT_COUNT)
        ]
        bindings_path.write_text(
            json.dumps({"bindings": entries}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.environ["FWROUTER_STATE_DIR"] = str(state_dir)
        baseline = _load_baseline_module(root)
        fixture = _make_fixture()
        result = {
            "fixture_subjects": SUBJECT_COUNT,
            "explicit_clients": EXPLICIT_COUNT,
            "eligible_explicit_clients_per_round": 2,
            "binding_file_bytes": bindings_path.stat().st_size,
            "rounds": ROUNDS,
            "baseline": _profile(baseline, bindings_path, fixture),
            "after": _profile(current, bindings_path, fixture),
        }
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
