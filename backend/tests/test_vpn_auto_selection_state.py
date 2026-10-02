from __future__ import annotations

import json

import pytest

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.vpn_auto_selection_state import (
    SELECTION_PROVENANCE_KEY,
    SELECTION_REVISION_KEY,
    advance_selection_revision,
    commit_active_selection,
    read_selection_revision,
    selection_pool_signature,
)


def test_revision_only_advances_on_committed_transition_and_cas_is_atomic() -> None:
    with db_session() as connection:
        connection.executemany(
            "INSERT INTO servers (server_id, server_name) VALUES (?, ?)",
            [("old", "Old"), ("new", "New")],
        )
        connection.execute(
            "INSERT INTO routing_global_state (id, server_mode, active_auto_server_id, apply_state) VALUES (1, 'auto', 'old', 'clean')"
        )
        assert read_selection_revision(connection) == 0

        # Read-only snapshots and confirmed no-ops do not reserve/bump a revision.
        assert read_selection_revision(connection) == 0

        assert advance_selection_revision(connection, expected_revision=0) == 1
    with db_session() as connection:
        assert read_selection_revision(connection) == 1
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?)",
            (SELECTION_PROVENANCE_KEY, json.dumps({"decision_id": "old-decision"})),
        )
        assert commit_active_selection(
            connection,
            expected_revision=1,
            expected_active_server_id="old",
            expected_provenance_decision_id="old-decision",
            server_id="new",
            provenance={"decision_id": "new-decision"},
        ) == 2

    with db_session() as connection:
        row = connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()
        assert row["active_auto_server_id"] == "new"
        assert read_selection_revision(connection) == 2
        provenance = connection.execute("SELECT value_json FROM settings WHERE key=?", (SELECTION_PROVENANCE_KEY,)).fetchone()
        assert json.loads(provenance["value_json"]) == {"decision_id": "new-decision"}


def test_cas_rejects_stale_revision_active_state_and_provenance() -> None:
    with db_session() as connection:
        connection.executemany(
            "INSERT INTO servers (server_id, server_name) VALUES (?, ?)",
            [("same", "Same"), ("other", "Other"), ("target", "Target")],
        )
        connection.execute(
            "INSERT INTO routing_global_state (id, server_mode, active_auto_server_id, apply_state) VALUES (1, 'auto', 'same', 'clean')"
        )
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?)",
            (SELECTION_PROVENANCE_KEY, json.dumps({"decision_id": "newer"})),
        )
        assert advance_selection_revision(connection) == 1
    with db_session() as connection:
        assert commit_active_selection(
            connection,
            expected_revision=0,
            expected_active_server_id="same",
            expected_provenance_decision_id="older",
            server_id="target",
            provenance={"decision_id": "stale"},
        ) is None
    with db_session() as connection:
        assert commit_active_selection(
            connection,
            expected_revision=1,
            expected_active_server_id="other",
            expected_provenance_decision_id="newer",
            server_id="target",
            provenance={"decision_id": "stale"},
        ) is None
        assert commit_active_selection(
            connection,
            expected_revision=1,
            expected_active_server_id="same",
            expected_provenance_decision_id="older",
            server_id="target",
            provenance={"decision_id": "stale"},
        ) is None
        assert connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()[0] == "same"
        assert read_selection_revision(connection) == 1


def test_cas_fails_closed_for_missing_routing_or_nonobject_provenance() -> None:
    with db_session() as connection:
        assert advance_selection_revision(connection) == 1
        assert commit_active_selection(
            connection,
            expected_revision=1,
            expected_active_server_id=None,
            expected_provenance_decision_id=None,
            server_id="target",
            provenance={"decision_id": "new"},
        ) is None
        connection.execute(
            "INSERT INTO servers (server_id, server_name) VALUES ('target', 'Target')"
        )
        connection.execute(
            "INSERT INTO routing_global_state (id, server_mode, active_auto_server_id, apply_state) VALUES (1, 'auto', NULL, 'clean')"
        )
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, '[]')",
            (SELECTION_PROVENANCE_KEY,),
        )
        assert commit_active_selection(
            connection,
            expected_revision=1,
            expected_active_server_id=None,
            expected_provenance_decision_id=None,
            server_id="target",
            provenance={"decision_id": "new"},
        ) is None
        assert connection.execute("SELECT active_auto_server_id FROM routing_global_state WHERE id=1").fetchone()[0] is None
        assert read_selection_revision(connection) == 1


def test_selection_pool_signature_ignores_probe_telemetry_but_keeps_connection_material() -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO servers (server_id, server_name, raw_json) VALUES ('node', 'Node', ?)",
            (json.dumps({"name": "node", "type": "vless", "server": "vpn.example", "port": 443, "history": [{"delay": 30}], "alive": True}),),
        )
        connection.execute("INSERT INTO server_preferences (server_id, vpn_auto) VALUES ('node', 1)")
        initial = selection_pool_signature(connection)
        connection.execute(
            "UPDATE servers SET raw_json=? WHERE server_id='node'",
            (json.dumps({"name": "node", "type": "vless", "server": "vpn.example", "port": 443, "history": [{"delay": 990}], "alive": False, "last-test": "now"}),),
        )
        assert selection_pool_signature(connection) == initial
        connection.execute(
            "UPDATE servers SET raw_json=? WHERE server_id='node'",
            (json.dumps({"name": "node", "type": "vless", "server": "different.example", "port": 443, "history": [{"delay": 990}], "alive": False}),),
        )
        assert selection_pool_signature(connection) != initial


@pytest.mark.parametrize("raw", ["-1", "true", "1.5", "null", '"1"', "not-json"])
def test_malformed_revision_fails_closed_without_resetting_epoch(raw: str) -> None:
    with db_session() as connection:
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?)",
            (SELECTION_REVISION_KEY, raw),
        )
    with db_session() as connection:
        with pytest.raises(ValueError):
            read_selection_revision(connection)


def test_watchdog_fence_captures_evidence_time_revision_and_rejects_aba() -> None:
    from fwrouter_api.services.vpn_runtime_control import VpnRuntimeController

    with db_session() as connection:
        connection.executemany(
            "INSERT INTO servers (server_id, server_name) VALUES (?, ?)",
            [("a", "A"), ("b", "B")],
        )
        connection.execute(
            "INSERT INTO routing_global_state (id, server_mode, active_auto_server_id, apply_state) VALUES (1, 'auto', 'a', 'clean')"
        )
        connection.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?)",
            (SELECTION_PROVENANCE_KEY, json.dumps({"decision_id": "d0", "selected_server_id": "a"})),
        )
    controller = VpnRuntimeController(vpn_adapter={"ready": True})
    controller.capture_selection_fence()  # watchdog captures before collecting failure evidence
    assert controller.selection_fence_args() == {
        "expected_selection_revision": 0, "expected_active_server_id": "a",
    }
    with db_session() as connection:
        assert commit_active_selection(
            connection, expected_revision=0, expected_active_server_id="a",
            expected_provenance_decision_id="d0", server_id="b",
            provenance={"decision_id": "d1", "selected_server_id": "b"},
        ) == 1
    with db_session() as connection:
        assert commit_active_selection(
            connection, expected_revision=1, expected_active_server_id="b",
            expected_provenance_decision_id="d1", server_id="a",
            provenance={"decision_id": "d2", "selected_server_id": "a"},
        ) == 2

    # Value equality (A again) cannot make evidence from revision 0 current.
    with db_session() as connection:
        assert commit_active_selection(
            connection,
            expected_revision=controller.selection_revision_snapshot,
            expected_active_server_id=controller.selection_active_snapshot,
            expected_provenance_decision_id="d0", server_id="b",
            provenance={"decision_id": "stale-watchdog", "selected_server_id": "b"},
        ) is None
    assert controller.selection_fence_args()["expected_selection_revision"] == 0


def test_recovery_without_evidence_context_fails_closed() -> None:
    from fwrouter_api.services.vpn_runtime_control import VpnRuntimeController

    controller = VpnRuntimeController(vpn_adapter={"ready": True})
    assert controller.selection_fence_args() == {
        "expected_selection_revision": -1, "expected_active_server_id": None,
    }
