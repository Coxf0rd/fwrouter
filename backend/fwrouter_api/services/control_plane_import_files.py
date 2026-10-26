"""Prepare import-owned rules without modifying any currently published path.

Publication belongs to the existing rules_state row and its SQLite transaction.
No recovery worker, runtime owner, or second activation pointer is introduced.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.database_backup import _check_directory_chain, _fsync_directory


RULE_FIELDS = {
    "manual_draft_path": ("manual_draft_text", "manual.draft.txt"),
    "manual_active_path": ("manual_active_text", "manual.active.txt"),
    "static_direct_path": ("static_direct_text", "static-direct.active.txt"),
    "big_direct_path": ("big_direct_text", "big-direct.active.txt"),
    "big_vpn_path": ("big_vpn_text", "big-vpn.active.txt"),
    "effective_text_path": ("effective_text", "effective-rules.txt"),
    "effective_json_path": ("effective_json", "effective-rules.json"),
    "metadata_path": ("metadata_json", "metadata.json"),
}
RECEIPT_PREFIX = "maintenance.import.receipt."


def prepare_rules_files(rules_snapshot: dict[str, Any]) -> dict[str, str]:
    state_dir = get_settings().paths.state_dir
    _check_directory_chain(state_dir)
    parent = state_dir / "import-rules"
    parent.mkdir(mode=0o700, exist_ok=True)
    _check_directory_chain(parent)
    _fsync_directory(state_dir)
    directory = Path(tempfile.mkdtemp(prefix="release-", dir=parent))
    content = rules_snapshot.get("content")
    content = content if isinstance(content, dict) else {}
    paths: dict[str, str] = {}
    try:
        for field, (source, name) in RULE_FIELDS.items():
            value = content.get(source)
            if source in {"effective_json", "metadata_json"}:
                text = json.dumps(value if isinstance(value, dict) else {},
                                  ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            else:
                text = str(value or "")
            path = directory / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                remaining = memoryview(text.encode("utf-8"))
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("IMPORT_RULES_SHORT_WRITE")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if path.read_bytes() != text.encode("utf-8"):
                raise OSError("IMPORT_RULES_PREPARATION_READBACK_FAILED")
            paths[field] = str(path)
        _fsync_directory(directory)
        _fsync_directory(parent)
        return paths
    except BaseException:
        # Only this unpublished, uniquely created directory belongs to us.
        shutil.rmtree(directory)
        _fsync_directory(parent)
        raise
