#!/usr/bin/env python3
"""Run the actual schema-preservation acceptance body on disposable local state.

This probe uses the real application `_prepare_db` and the real acceptance test
function. Only worker process, native identity reads, and HTTP response
transport are replaced; it does not start Compose, Xray, Mihomo, or a service.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

from application_acceptance import conftest  # noqa: E402
from application_acceptance import test_database_application as acceptance_test  # noqa: E402


class _StoppedWorker:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: float) -> None:
        assert self.terminated and timeout == 5


def _main() -> None:
    with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-schema27-probe-") as directory:
        state = Path(directory) / "state"
        state.mkdir(mode=0o700)
        conftest._prepare_db(state)

        native = object()
        identity_reads = 0

        def loaded_identities(owner: object) -> list[tuple[str, str]]:
            nonlocal identity_reads
            assert owner is native
            identity_reads += 1
            return [("fixture-native-client-id", "fixture-native-client@example.invalid")]

        def http_json(url: str) -> tuple[int, dict[str, bool]]:
            assert url.endswith("/xray/clients")
            return 200, {"ok": True}

        acceptance_test.loaded_identities = loaded_identities
        acceptance_test.http_json = http_json
        stack = {
            "state": state,
            "native": native,
            "worker": _StoppedWorker(),
            "api": "http://owned-fixture.invalid/api/v2",
            "start_worker": lambda: (_StoppedWorker(), "http://owned-fixture.invalid/api/v2"),
        }
        acceptance_test.test_current_schema_initialization_preserves_control_plane_and_native_state(
            stack,
        )
        assert identity_reads == 2
        print(
            "PASS: actual schema-preservation acceptance body on _prepare_db state; "
            "worker/native/HTTP boundaries stubbed only; no Compose or native runtime started",
        )


if __name__ == "__main__":
    _main()
