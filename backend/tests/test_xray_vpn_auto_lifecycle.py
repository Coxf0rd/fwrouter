from __future__ import annotations

import json
import hashlib
import os
import stat
from typing import Any
import pytest

from _test_support import database_table_snapshot as _database_snapshot
from fwrouter_api.db.connection import db_session
from fwrouter_api.services import subject_policy as subject_policy_service
from fwrouter_api.services.subscription_profiles import list_desired_subscription_xray_clients, promote_runtime_verified_subscription_nodes
from pathlib import Path

from _test_support import configure_test_state_dir
from fwrouter_api.adapters.xray import RealXrayAdapter
from fwrouter_api.db.connection import initialize_database
from fwrouter_api.services import xray_subscription_service
from test_xray import (
    _configure_env,
    _run_profile_candidate_validation_case,
    _seed_server,
    _seed_routing_state,
    _seed_subscription_identity,
    _FakeRunner,
    _build_adapter,
    _enable_xray_module,
    _patch_runtime,
    _patch_xray_adapters,
    _write_xray_config,
    _xray_paths,
)


class _PythonComponentXrayAdapter:
    """Use the real config adapter while keeping native generation checks separate."""

    def __init__(self, component: RealXrayAdapter) -> None:
        self._component = component

    def __getattr__(self, name: str):
        if name == "stage_subscription_generation":
            raise AttributeError(name)
        return getattr(self._component, name)


def _install_runtime_boundaries(monkeypatch, adapter: _PythonComponentXrayAdapter) -> None:
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: adapter)
    monkeypatch.setattr(
        xray_subscription_service,
        "_materialize_xray_runtime_bindings",
        lambda **_kwargs: {"ok": True, "status": "applied"},
    )
    monkeypatch.setattr(
        "fwrouter_api.services.mihomo_config.reconcile_mihomo_runtime",
        lambda **_kwargs: {"ok": True, "reconcile_action": "none"},
    )


def test_vpn_auto_reconcile_removes_stale_identity_and_recreates_one_stable_identity(
    monkeypatch, tmp_path: Path,
) -> None:
    configure_test_state_dir(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()

    stale_email = xray_subscription_service._vpn_auto_xray_client_email("server-gone")
    config_path, _ = _xray_paths()
    _write_xray_config(config_path, [
        {"id": "stale-uuid", "email": stale_email},
        {"id": "manual-uuid", "email": "manual@example.test"},
    ])
    runner = _FakeRunner()
    component = _build_adapter(tmp_path, runner=runner)
    _patch_xray_adapters(monkeypatch, component)
    adapter = _PythonComponentXrayAdapter(component)
    _install_runtime_boundaries(monkeypatch, adapter)

    removed = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")

    assert removed["ok"] is True
    assert removed["deleted_count"] == 1
    clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]
    assert stale_email not in {client["email"] for client in clients}
    assert "manual@example.test" in {client["email"] for client in clients}
    virtual_email = xray_subscription_service._vpn_auto_xray_client_email(
        xray_subscription_service.VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
    )
    first_virtual = next(client for client in clients if client["email"] == virtual_email)

    first_return = xray_subscription_service.reconcile_xray_vpn_auto_subscription(requested_by="pytest")
    second_clients = json.loads(config_path.read_text(encoding="utf-8"))["inbounds"][0]["settings"]["clients"]
    second_virtual = next(client for client in second_clients if client["email"] == virtual_email)

    assert first_return["ok"] is True
    assert first_return["created_count"] == 0
    assert first_return["deleted_count"] == 0
    assert second_virtual["id"] == first_virtual["id"]
    assert len([client for client in second_clients if client["email"] == virtual_email]) == 1


def test_identity_migration_marker_blocks_durable_credentials_before_registration(
    monkeypatch, tmp_path: Path,
) -> None:
    """An unresolved server marker must stop preparation before identity writes."""
    from fwrouter_api.adapters.xray_common import xray_writer_guard
    from fwrouter_api.services.subscription_node_identity import IdentityConflict
    from fwrouter_api.services.subscription_profiles import _subscription_servers

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    server_id = "profile-requires-reviewed-identity-map"
    _seed_server(server_id, raw={"_fwrouter_identity_migration_required": True})
    _seed_subscription_identity(slug="identity-marker", token="identity-marker-token")

    # Use the real list helper to cover its dictionary projection and ensure the
    # safety check consults persisted server metadata by ID.
    servers = _subscription_servers()
    assert any(server.get("server_id") == server_id for server in servers)
    assert all(isinstance(server, dict) for server in servers)

    identity_tables = (
        "subscription_node_identities",
        "subscription_node_aliases",
        "subscription_node_credentials",
        "subscription_node_credential_history",
        "subscription_node_identity_transitions",
    )
    with db_session() as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in identity_tables
        }
    assert before == {table: 0 for table in identity_tables}

    with xray_writer_guard(timeout_seconds=5.0):
        with pytest.raises(IdentityConflict, match="IDENTITY_MIGRATION_REQUIRED"):
            xray_subscription_service._prepare_durable_profile_credentials(include_vpn_auto=False)

    with db_session() as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in identity_tables
        }
        row = connection.execute("SELECT raw_json FROM servers WHERE server_id=?", (server_id,)).fetchone()
        assert row is not None
        assert json.loads(row["raw_json"])["_fwrouter_identity_migration_required"] is True
    assert after == before


@pytest.mark.parametrize("recorded_digest", [True, False], ids=["changed-active-config", "missing-transition-proof"])
def test_transition_recovery_requires_recorded_config_digest_before_native_mutation(
    monkeypatch, tmp_path: Path, recorded_digest: bool,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _enable_xray_module()
    adapter = _build_adapter(tmp_path, runner=_FakeRunner())
    _patch_xray_adapters(monkeypatch, adapter)
    xray_path, _ = _xray_paths()
    _write_xray_config(xray_path, [])
    from fwrouter_api.services import mihomo_config, mihomo_runtime
    mihomo_path = Path(mihomo_config._resolved_base_config_path())
    mihomo_path.parent.mkdir(parents=True, exist_ok=True)
    mihomo_path.write_text("owned-transition", encoding="utf-8")
    transition_hash = hashlib.sha256(mihomo_path.read_bytes()).hexdigest()
    validation = {"xray": {"expected_client_identities": []}}
    if recorded_digest:
        validation["transition"] = {"candidate_sha256": transition_hash}
    monkeypatch.setattr(mihomo_runtime, "get_mihomo_runtime_incarnation", lambda: "owned-incarnation")
    checkpoint = xray_subscription_service._write_xray_generation_checkpoint(
        adapter=adapter, generation_id="transition-proof-fixture", tokens=set(), phase="xray_applied",
        source_fingerprint=xray_subscription_service._generation_source_fingerprint(),
        staged_generation={"native_validation": validation}, managed_email_prefixes=["sub-"],
    )
    stored = json.loads(checkpoint.read_text())
    assert stored["staged_generation"]["mihomo_transition_sha256"] == (transition_hash if recorded_digest else None)
    if recorded_digest:
        mihomo_path.write_text("foreign-config-same-incarnation", encoding="utf-8")
    before = (xray_path.read_bytes(), mihomo_path.read_bytes(), checkpoint.read_bytes())
    revisions_before = _database_snapshot()
    def unexpected_native_read_or_write(*_args, **_kwargs):
        raise AssertionError("unproven transition must be refused before native mutation")
    monkeypatch.setattr(adapter, "reload", unexpected_native_read_or_write)
    monkeypatch.setattr(adapter, "test_config", unexpected_native_read_or_write)
    result = xray_subscription_service._restore_xray_generation_checkpoint(adapter, checkpoint)
    assert result == {"ok": False, "recovered": "stale_checkpoint_declined", "reason": (
        "active_mihomo_generation_changed" if recorded_digest else "mihomo_transition_digest_unavailable"
    )}
    assert (xray_path.read_bytes(), mihomo_path.read_bytes(), checkpoint.read_bytes()) == before
    assert _database_snapshot() == revisions_before


def test_unmapped_inventory_replacement_does_not_inherit_or_publish_identity(monkeypatch, tmp_path: Path) -> None:
    """A replacement keeps a fresh derived identity until a reviewed map is installed."""
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    tokens = [f"inventory-delta-{index}" for index in range(7)]
    for token in tokens:
        _seed_subscription_identity(slug=token, token=token)
    from fwrouter_api.adapters.subscription import parse_subscription_payload

    def member(index):
        return {
            "tag": f"owned-member-{index}", "protocol": "vless",
            "settings": {"vnext": [{"address": f"member-{index}.example.invalid", "port": 443,
                                      "users": [{"id": f"owned-fixture-id-{index}"}]}]},
            "streamSettings": {"network": "tcp", "security": "tls",
                               "tlsSettings": {"serverName": f"member-{index}.example.invalid"}},
        }

    service_outbounds = [{"tag": "direct", "protocol": "freedom"},
                         {"tag": "reject", "protocol": "blackhole"},
                         {"tag": "dns", "protocol": "dns"}]
    original_profile = {"remarks": "owned-growth-profile",
                        "outbounds": [*(member(index) for index in range(7)), *service_outbounds]}
    changed_profile = json.loads(json.dumps(original_profile))
    changed_profile["outbounds"] = [*changed_profile["outbounds"][:7],
                                    *(member(index) for index in range(7, 10)), *service_outbounds]
    old_result = parse_subscription_payload(json.dumps(original_profile))
    new_result = parse_subscription_payload(json.dumps(changed_profile))
    assert old_result.ok and new_result.ok
    assert len(old_result.servers) == len(new_result.servers) == 1
    retiring = old_result.servers[0]
    replacement = new_result.servers[0]
    assert retiring.server_name == replacement.server_name
    assert retiring.host == replacement.host
    assert retiring.server_id != replacement.server_id
    assert len(retiring.raw["_fwrouter_topology"]["endpoints"]) == 7
    assert len(replacement.raw["_fwrouter_topology"]["endpoints"]) == 10
    assert original_profile["outbounds"][:7] == changed_profile["outbounds"][:7]
    for profile, server in ((original_profile, retiring), (changed_profile, replacement)):
        canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert server.server_id == "sub:" + hashlib.sha256(canonical.encode()).hexdigest()
    _seed_server("server-retained")
    _seed_server(retiring.server_id, server_name=retiring.server_name, raw=retiring.raw)
    original = {token: list_desired_subscription_xray_clients(token) for token in tokens}
    for nodes in original.values():
        promote_runtime_verified_subscription_nodes(nodes)
    from uuid import NAMESPACE_DNS, uuid5
    snapshots_before = _database_snapshot()["subscription_profile_snapshots"]
    _seed_server(replacement.server_id, server_name=replacement.server_name, raw=replacement.raw)
    with db_session() as connection:
        connection.execute("UPDATE servers SET inventory_state = 'missing' WHERE server_id = ?", (retiring.server_id,))
        assert connection.execute(
            "SELECT 1 FROM subscription_node_aliases WHERE server_id=?", (replacement.server_id,),
        ).fetchone() is None
    for token in tokens:
        old = {node["server_id"]: node for node in original[token]}
        new = {node["server_id"]: node for node in list_desired_subscription_xray_clients(token)}
        assert retiring.server_id not in new and replacement.server_id in new
        assert new["server-retained"]["client_uuid"] == old["server-retained"]["client_uuid"]
        assert new["server-retained"]["client_email"] == old["server-retained"]["client_email"]
        assert new[replacement.server_id]["client_uuid"] == str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{replacement.server_id}"))
        assert new[replacement.server_id]["client_uuid"] != old[retiring.server_id]["client_uuid"]
        published = next(row for row in snapshots_before if row["token"] == token)
        published_ids = {node["client_uuid"] for node in json.loads(published["nodes_json"])}
        assert old[retiring.server_id]["client_uuid"] in published_ids
        assert new[replacement.server_id]["client_uuid"] not in published_ids
    assert _database_snapshot()["subscription_profile_snapshots"] == snapshots_before


def test_reviewed_profile_identity_mapping_preserves_credentials_without_publishing(monkeypatch, tmp_path: Path) -> None:
    """A reviewed map updates intent while native and public state await generation proof."""
    from uuid import NAMESPACE_DNS, uuid5

    from fwrouter_api.adapters.subscription import parse_subscription_payload
    from fwrouter_api.adapters.xray import NoopXrayAdapter
    from fwrouter_api.services.subscription_profiles import _subscription_email
    from fwrouter_api.services.subscription_node_identity import canonical_digest
    from fwrouter_api.services import subscription as subscription_service
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_revision

    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    # The identity migration derives its checkpoint location from the adapter
    # config path. Give the no-op adapter a test-owned path so this contract
    # test can prove that no checkpoint exists without touching host paths or
    # invoking any native Xray operations.
    test_adapter = NoopXrayAdapter()
    test_adapter.config_path = tmp_path / "xray" / "config.json"
    monkeypatch.setattr(xray_subscription_service, "_xray_adapter", lambda: test_adapter)
    tokens = [f"reviewed-map-{index}" for index in range(7)]
    for token in tokens:
        _seed_subscription_identity(slug=token, token=token)

    def member(index: int) -> dict[str, Any]:
        return {
            "tag": f"reviewed-member-{index}", "protocol": "vless",
            "settings": {"vnext": [{"address": f"member-{index}.example.invalid", "port": 443,
                                      "users": [{"id": f"reviewed-fixture-id-{index}"}]}]},
            "streamSettings": {"network": "tcp", "security": "tls",
                               "tlsSettings": {"serverName": f"member-{index}.example.invalid"}},
        }

    services = [{"tag": "direct", "protocol": "freedom"},
                {"tag": "reject", "protocol": "blackhole"},
                {"tag": "dns", "protocol": "dns"}]
    previous = {"remarks": "reviewed-profile-before", "outbounds": [member(0), *services]}
    reviewed = {"remarks": "reviewed-profile-after-policy-review", "outbounds": [member(0), *services]}
    old_server = parse_subscription_payload(json.dumps(previous)).servers[0]
    new_server = parse_subscription_payload(json.dumps(reviewed)).servers[0]
    _seed_server(old_server.server_id, server_name=old_server.server_name, raw=old_server.raw)
    # Emulate an existing schema-24 parser row that has complete canonical raw
    # provenance fields but predates the durable profile witness.
    legacy_target_raw = dict(new_server.raw)
    legacy_target_raw.pop("_fwrouter_profile_provenance", None)
    _seed_server(new_server.server_id, server_name=new_server.server_name, raw=legacy_target_raw)
    source_url = "https://reviewed.example.invalid/subscription"
    source_id = subscription_service._source_id(source_url)

    persisted: dict[str, str] = {}
    with db_session() as connection:
        connection.execute(
            """INSERT INTO subscription_server_memberships
               (source_id, server_id, source_url, entry_identity_hash, parser_format, display_name, is_active)
               VALUES (?, ?, ?, 'old-profile', 'json', ?, 0)""",
            (source_id, old_server.server_id, source_url, old_server.server_name),
        )
        connection.execute(
            """INSERT INTO subscription_server_memberships
               (source_id, server_id, source_url, entry_identity_hash, parser_format, display_name, is_active)
               VALUES (?, ?, ?, 'new-profile', 'json', ?, 1)""",
            (source_id, new_server.server_id, source_url, new_server.server_name),
        )
        for token in tokens:
            email = _subscription_email(token, old_server.server_id)
            client_uuid = str(uuid5(NAMESPACE_DNS, f"reviewed-public:{token}"))
            persisted[email] = client_uuid
            connection.execute(
                """INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind,
                   stable_key, display_name, desired_mode, runtime_state, is_active, is_deleted, metadata_json)
                   VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', 'active', 1, 0, ?)""",
                (f"xray:{client_uuid}", f"xray:{client_uuid}", email,
                 json.dumps({"detail": {"email": email, "client_uuid": client_uuid}})),
            )
            connection.execute(
                "INSERT INTO subscription_profile_snapshots (token, nodes_json) VALUES (?, ?) ",
                (token, json.dumps([{"server_id": old_server.server_id, "client_email": email,
                                     "client_uuid": client_uuid}], sort_keys=True)),
            )
        internal_email = xray_subscription_service._vpn_auto_xray_client_email(old_server.server_id)
        internal_uuid = str(uuid5(NAMESPACE_DNS, "reviewed-internal"))
        persisted[internal_email] = internal_uuid
        connection.execute(
            """INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind,
               stable_key, display_name, desired_mode, runtime_state, is_active, is_deleted, metadata_json)
               VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', 'active', 1, 0, ?)""",
            (f"xray:{internal_uuid}", f"xray:{internal_uuid}", internal_email,
             json.dumps({"detail": {"email": internal_email, "client_uuid": internal_uuid}})),
        )
        selection_revision = read_selection_revision(connection)

    snapshots_before = _database_snapshot()["subscription_profile_snapshots"]
    manifest = [{
        "old_server_id": old_server.server_id,
        "new_server_id": new_server.server_id,
        "old_profile": previous,
        "new_profile": reviewed,
        "expected_identity_revision": 1,
    }]
    identity_digest = canonical_digest(sorted((value, key) for key, value in persisted.items()))

    with pytest.raises(RuntimeError, match="IDENTITY_MIGRATION_SELECTION_STALE"):
        xray_subscription_service.migrate_subscription_profile_identities(
            manifest,
            expected_selection_revision=selection_revision + 1,
            expected_identity_digest=identity_digest,
        )
    assert _database_snapshot()["subscription_profile_snapshots"] == snapshots_before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM subscription_node_aliases WHERE server_id=?", (new_server.server_id,),
        ).fetchone() is None

    for corrupt_field in ("_fwrouter_raw_identity_sha256", "_fwrouter_topology"):
        corrupted = json.loads(json.dumps(legacy_target_raw))
        if corrupt_field == "_fwrouter_raw_identity_sha256":
            corrupted[corrupt_field] = "0" * 64
        else:
            corrupted[corrupt_field]["endpoints"][0]["host"] = "tampered.invalid"
        with db_session() as connection:
            connection.execute("UPDATE servers SET raw_json=? WHERE server_id=?",
                               (json.dumps(corrupted, sort_keys=True), new_server.server_id))
        with pytest.raises(RuntimeError, match="IDENTITY_MIGRATION_STORED_WITNESS_MISMATCH"):
            xray_subscription_service.migrate_subscription_profile_identities(
                manifest, expected_selection_revision=selection_revision,
                expected_identity_digest=identity_digest,
            )
        assert _database_snapshot()["subscription_profile_snapshots"] == snapshots_before
        with db_session() as connection:
            assert connection.execute(
                "SELECT 1 FROM subscription_node_aliases WHERE server_id=?", (new_server.server_id,),
            ).fetchone() is None
            connection.execute("UPDATE servers SET raw_json=? WHERE server_id=?",
                               (json.dumps(legacy_target_raw, sort_keys=True), new_server.server_id))

    preview = xray_subscription_service.migrate_subscription_profile_identities(
        manifest,
        expected_selection_revision=selection_revision,
        expected_identity_digest=identity_digest,
    )
    assert preview["ok"] is True and preview["status"] == "dry_run"
    assert preview["native_applied"] is False
    assert preview["fresh_generation_required"] is True
    assert _database_snapshot()["subscription_profile_snapshots"] == snapshots_before
    with db_session() as connection:
        assert connection.execute(
            "SELECT 1 FROM subscription_node_aliases WHERE server_id=?", (new_server.server_id,),
        ).fetchone() is None
        dry_run_raw = json.loads(connection.execute(
            "SELECT raw_json FROM servers WHERE server_id=?", (new_server.server_id,),
        ).fetchone()[0])
        assert "_fwrouter_profile_provenance" not in dry_run_raw

    result = xray_subscription_service.migrate_subscription_profile_identities(
        manifest,
        expected_selection_revision=selection_revision,
        expected_identity_digest=identity_digest,
        apply=True,
        expected_plan_sha256=preview["plan_sha256"],
    )
    assert result["ok"] is True and result["status"] == "applied"
    assert result["native_applied"] is False
    assert result["fresh_generation_required"] is True
    assert _database_snapshot()["subscription_profile_snapshots"] == snapshots_before
    with db_session() as connection:
        stored_witness = json.loads(connection.execute(
            "SELECT raw_json FROM servers WHERE server_id=?", (new_server.server_id,),
        ).fetchone()[0])["_fwrouter_profile_provenance"]
        assert stored_witness.get("content") == new_server.server_id.removeprefix("sub:")
        assert connection.execute(
            "SELECT current_server_id FROM subscription_node_identities WHERE origin_server_id=?",
            (old_server.server_id,),
        ).fetchone()[0] == new_server.server_id
        for scope in [*(f"subscription:{token}" for token in tokens), "vpn-auto"]:
            row = connection.execute(
                """SELECT c.client_uuid FROM subscription_node_credentials c
                   JOIN subscription_node_aliases a USING (identity_id)
                   WHERE c.scope=? AND a.server_id=?""",
                (scope, new_server.server_id),
            ).fetchone()
            assert row is not None


@pytest.mark.parametrize(
    ("writer", "failure"),
    [
        ("atomic", "write"), ("atomic", "fsync"), ("atomic", "publish"),
        ("archive", "write"), ("archive", "fsync"), ("archive", "publish"),
    ],
    ids=["atomic-write", "atomic-fsync", "atomic-replace", "archive-write", "archive-fsync", "archive-link"],
)
def test_private_checkpoint_writer_failure_preserves_marker_and_cleans_temp(
    monkeypatch, tmp_path: Path, writer: str, failure: str,
) -> None:
    """Private checkpoint write failures retain the previous marker and remove partial files."""
    marker = tmp_path / "generation-checkpoint.json"
    marker.write_text('{"phase":"previous"}', encoding="utf-8")
    marker.chmod(0o600)
    original_bytes = marker.read_bytes()
    real_open, real_close = os.open, os.close
    writer_fds: set[int] = set()
    close_counts: dict[int, int] = {}

    def tracked_open(path, flags, mode=0o777):
        descriptor = real_open(path, flags, mode)
        if str(path).endswith(".tmp"):
            writer_fds.add(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        if descriptor in writer_fds:
            close_counts[descriptor] = close_counts.get(descriptor, 0) + 1
        real_close(descriptor)

    monkeypatch.setattr(xray_subscription_service.os, "open", tracked_open)
    monkeypatch.setattr(xray_subscription_service.os, "close", tracked_close)

    if failure == "write":
        monkeypatch.setattr(
            xray_subscription_service, "_write_all_fd",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected private write failure")),
        )
    elif failure == "fsync":
        monkeypatch.setattr(xray_subscription_service.os, "fsync",
                            lambda _fd: (_ for _ in ()).throw(OSError("injected private fsync failure")))
    elif writer == "atomic":
        monkeypatch.setattr(xray_subscription_service.os, "replace",
                            lambda *_args: (_ for _ in ()).throw(OSError("injected marker replace failure")))
    else:
        monkeypatch.setattr(xray_subscription_service.os, "link",
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected archive link failure")))

    with pytest.raises(OSError):
        if writer == "atomic":
            xray_subscription_service._atomic_write_private_checkpoint(marker, '{"phase":"next"}')
        else:
            digest = hashlib.sha256(original_bytes).hexdigest()
            xray_subscription_service._archive_checkpoint_exact(marker, digest)

    assert marker.read_bytes() == original_bytes
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert writer_fds and set(close_counts) == writer_fds
    assert all(count == 1 for count in close_counts.values())
    assert not list(tmp_path.glob("*.tmp"))
    archive_dir = marker.parent / "checkpoint-archive"
    if writer == "archive":
        assert archive_dir.is_dir()
        assert not list(archive_dir.glob("*.tmp"))
        assert not list(archive_dir.glob("generation-checkpoint-*.json"))


@pytest.mark.parametrize("writer", ["atomic", "archive"], ids=["atomic", "archive"])
def test_private_checkpoint_writer_closes_once_before_publish_even_if_fd_is_reused(
    monkeypatch, tmp_path: Path, writer: str,
) -> None:
    """A closed writer descriptor is never closed again after its number is reused."""
    marker = tmp_path / "generation-checkpoint.json"
    marker.write_text('{"phase":"previous"}', encoding="utf-8")
    marker.chmod(0o600)
    real_open, real_close, real_dup2, real_fstat = os.open, os.close, os.dup2, os.fstat
    real_replace, real_link = os.replace, os.link
    writer_fds: set[int] = set()
    close_counts: dict[int, int] = {}
    reused_fds: set[int] = set()
    publish_observations: list[tuple[int, int]] = []

    def tracked_open(path, flags, mode=0o777):
        descriptor = real_open(path, flags, mode)
        if str(path).endswith(".tmp"):
            writer_fds.add(descriptor)
        return descriptor

    def close_and_reuse(descriptor: int) -> None:
        if descriptor not in writer_fds:
            real_close(descriptor)
            return
        count = close_counts.get(descriptor, 0) + 1
        close_counts[descriptor] = count
        real_close(descriptor)
        if count == 1:
            replacement = real_open(os.devnull, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            if replacement != descriptor:
                real_dup2(replacement, descriptor)
                real_close(replacement)
            reused_fds.add(descriptor)

    def assert_writer_closed_before_publish() -> None:
        for descriptor in writer_fds:
            publish_observations.append((descriptor, close_counts.get(descriptor, 0)))
            assert close_counts.get(descriptor) == 1
            assert descriptor in reused_fds

    def tracked_replace(source, destination) -> None:
        assert_writer_closed_before_publish()
        real_replace(source, destination)

    def tracked_link(source, destination, **kwargs) -> None:
        assert_writer_closed_before_publish()
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(xray_subscription_service.os, "open", tracked_open)
    monkeypatch.setattr(xray_subscription_service.os, "close", close_and_reuse)
    monkeypatch.setattr(xray_subscription_service.os, "replace", tracked_replace)
    monkeypatch.setattr(xray_subscription_service.os, "link", tracked_link)
    try:
        if writer == "atomic":
            xray_subscription_service._atomic_write_private_checkpoint(marker, '{"phase":"next"}')
            assert marker.read_text(encoding="utf-8") == '{"phase":"next"}'
        else:
            original = marker.read_bytes()
            archived = xray_subscription_service._archive_checkpoint_exact(
                marker, hashlib.sha256(original).hexdigest(),
            )
            assert archived.read_bytes() == original
            assert stat.S_IMODE(archived.stat().st_mode) == 0o600
        assert publish_observations and all(count == 1 for _, count in publish_observations)
        assert close_counts and all(count == 1 for count in close_counts.values())
        assert all(real_fstat(descriptor) for descriptor in reused_fds)
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        for descriptor in reused_fds:
            try:
                real_close(descriptor)
            except OSError:
                pass


@pytest.mark.parametrize("client_count", [1, 70])
def test_profile_binding_plan_reuses_owned_enforcement_without_per_client_native_preflight(monkeypatch, tmp_path: Path, client_count: int) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _patch_runtime(monkeypatch)
    _seed_server("profile-plan-server")
    _seed_routing_state(desired_mode="vpn")
    # This component has no native DNS/network namespace. Keep DNS explicitly
    # unavailable rather than discovering the runner host or faking proof.
    monkeypatch.setattr(
        "fwrouter_api.services.dnsmasq.inspect_dnsmasq_selective_status",
        lambda: {"ok": False, "reason": "component_dns_namespace_unavailable"},
    )
    nodes = []
    with db_session() as connection:
        for index in range(client_count):
            client_id = f"owned-profile-client-{index}"
            email = f"sub-owned-plan-{index}@example.test"
            subject_id = f"xray:{client_id}"
            connection.execute(
                """INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind,
                    stable_key, display_name, desired_mode, runtime_state, is_active, is_deleted, metadata_json)
                    VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', 'active', 1, 0, ?)""",
                (subject_id, subject_id, email, json.dumps({"detail": {
                    "client_id": client_id, "client_uuid": client_id, "email": email, "enabled": True,
                }})),
            )
            nodes.append({"client_uuid": client_id, "client_email": email, "server_id": "profile-plan-server", "xray_alias": "owned-profile"})
    from fwrouter_api.services import xray_client_state
    def unexpected_per_client_enrichment(*_args, **_kwargs):
        raise AssertionError("planner must enrich persisted identity with its owned snapshot only")
    monkeypatch.setattr(xray_client_state, "get_subject_with_effective_state", unexpected_per_client_enrichment)
    persisted_subject_reads = []
    read_subject = xray_client_state.get_subject
    def counted_subject_read(subject_id):
        persisted_subject_reads.append(subject_id)
        return read_subject(subject_id)
    monkeypatch.setattr(xray_client_state, "get_subject", counted_subject_read)
    preflight_calls = []
    original = subject_policy_service._default_subject_runtime_enforcement
    def observed_preflight(**kwargs):
        preflight_calls.append(True)
        return original(**kwargs)
    monkeypatch.setattr(subject_policy_service, "_default_subject_runtime_enforcement", observed_preflight)
    before = _database_snapshot()
    bindings, modes, assignments = xray_subscription_service._prospective_profile_bindings(
        nodes, token_prefix="sub-", preserve_existing_overrides=False,
    )
    assert len(persisted_subject_reads) == client_count
    assert len(set(persisted_subject_reads)) == client_count
    assert len(preflight_calls) == 0  # Managed clients are excluded before the discarded mode projection.
    assert len(bindings) == client_count and modes == []
    assert {row["client_uuid"] for row in bindings} == {row["client_uuid"] for row in nodes}
    assert all(row["selected_server_id"] == "profile-plan-server" for row in bindings)
    assert len(assignments) == 1
    assert _database_snapshot() == before
    # Ordinary callers retain the original fresh effective-state path.
    with pytest.raises(AssertionError, match="planner must enrich"):
        xray_client_state._xray_subject_for_client(nodes[0]["client_uuid"])


@pytest.mark.parametrize("case", ["identical", "different-handoffs", "changed-inputs", "copy-corrupted"])
def test_identical_candidate_copy_retains_both_validations_and_rejects_changed_context(monkeypatch, tmp_path: Path, case: str) -> None:
    old = [{"selected_server_id": "retiring-target", "port": 53124}] if case == "different-handoffs" else []
    if case == "copy-corrupted":
        from fwrouter_api.services import artifacts
        original_copy = artifacts.atomic_copy_file
        def corrupt_copy(source, target):
            original_copy(source, target)
            Path(target).write_text("foreign final candidate", encoding="utf-8")
        monkeypatch.setattr(artifacts, "atomic_copy_file", corrupt_copy)
    result, local_calls, native_calls = _run_profile_candidate_validation_case(
        monkeypatch, tmp_path, old_assignments=old,
        assignments=[{"selected_server_id": "owned-target", "port": 53123}],
        image_id=None, input_context_changes=case == "changed-inputs",
    )
    if case == "copy-corrupted":
        assert result["ok"] is False
        assert result["error_code"] == "MIHOMO_STAGE_CANDIDATE_COPY_MISMATCH"
        assert len(local_calls) == 1 and len(native_calls) == 1
        return
    if case == "changed-inputs":
        assert result["ok"] is False
        assert result["error_code"] == "MIHOMO_STAGE_INPUTS_CHANGED_AFTER_VALIDATION"
        assert len(local_calls) == 1 and len(native_calls) == 1
        return
    assert result["ok"] is True
    assert len(local_calls) == 2 and len(native_calls) == 2
    transition = result["native_validation"]["transition"]
    final = result["native_validation"]["final"]
    assert transition["validation"]["candidate_write_reused"] is False
    assert final["validation"]["candidate_write_reused"] is (case == "identical")
    assert final["validation"]["native_validation_reused"] is False
    if case != "different-handoffs":
        assert transition["candidate_sha256"] == final["candidate_sha256"]
    else:
        assert transition["candidate_sha256"] != final["candidate_sha256"]
