#!/usr/bin/env python3
"""Verify that a wheel contains the exact backend project source payload."""
from __future__ import annotations

import email.parser
import hashlib
import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def verify(wheel_dir: Path, output: Path) -> bool:
    project = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    expected_name = project.get("project", {}).get("name")
    expected_version = project.get("project", {}).get("version")
    source_root = ROOT / "backend"
    source_files = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in sorted((source_root / "fwrouter_api").rglob("*.py"))
    }
    wheels = sorted(wheel_dir.glob("*.whl"))
    observed = []
    errors = []
    for wheel in wheels:
        try:
            with zipfile.ZipFile(wheel) as archive:
                metadata_paths = [name for name in archive.namelist()
                                  if name.endswith(".dist-info/METADATA")]
                if len(metadata_paths) != 1:
                    raise ValueError("wheel must contain exactly one dist-info/METADATA")
                metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_paths[0]))
                payload = sorted(name for name in archive.namelist()
                                 if name.startswith("fwrouter_api/") and name.endswith(".py"))
                payload_bytes = {name: archive.read(name) for name in payload}
                matching_source = payload == sorted(source_files) and all(
                    payload_bytes[name] == source_files[name] for name in payload
                )
                observed.append({
                    "filename": wheel.name,
                    "name": metadata.get("Name"),
                    "version": metadata.get("Version"),
                    "fwrouter_api_python_files": [
                        {"path": name, "sha256": hashlib.sha256(payload_bytes[name]).hexdigest()}
                        for name in payload
                    ],
                    "matches_current_source": matching_source,
                })
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            errors.append(f"{wheel.name}: {type(error).__name__}")

    summary = {
        "schema": "fwrouter-backend-wheel-audit/v1",
        "expected_name": expected_name,
        "expected_version": expected_version,
        "source_python_files": sorted(source_files),
        "wheels": observed,
        "errors": errors,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    valid = (
        isinstance(expected_name, str)
        and re.sub(r"[-_.]+", "-", expected_name).lower() == "fwrouter-api"
        and isinstance(expected_version, str)
        and len(source_files) > 0
        and len(observed) == 1
        and not errors
        and re.sub(r"[-_.]+", "-", observed[0]["name"] or "").lower()
        == re.sub(r"[-_.]+", "-", expected_name).lower()
        and observed[0]["version"] == expected_version
        and "fwrouter_api/__init__.py" in [row["path"] for row in observed[0]["fwrouter_api_python_files"]]
        and observed[0]["matches_current_source"] is True
    )
    if not valid:
        print("wheel identity or fwrouter_api source payload mismatch", file=sys.stderr)
    return valid


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_backend_wheel.py WHEEL_DIR OUTPUT_JSON", file=sys.stderr)
        return 2
    return 0 if verify(Path(sys.argv[1]), Path(sys.argv[2])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
