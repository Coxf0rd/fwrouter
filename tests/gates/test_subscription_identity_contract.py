"""Pure SQLite identity contracts: no application bootstrap or native execution."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


identity = load("identity_contract_subject", "backend/fwrouter_api/services/subscription_node_identity.py")
schema = load("identity_contract_schema", "backend/fwrouter_api/db/subscription_identity_schema.py")


class DurableIdentityContract(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("CREATE TABLE servers(server_id TEXT PRIMARY KEY, inventory_state TEXT, raw_json TEXT)")
        self.connection.execute("CREATE TABLE subscription_server_memberships(source_id TEXT, server_id TEXT, is_active INTEGER DEFAULT 1)")
        schema.create_identity_schema(self.connection)
        self.old = {"remarks": "owned.test", "outbounds": [
            {"tag": str(i), "protocol": "vless", "address": f"{i}.invalid"} for i in range(7)
        ] + [{"protocol": "freedom"}, {"protocol": "blackhole"}, {"protocol": "dns"}]}
        self.new = json.loads(json.dumps(self.old))
        self.new["outbounds"][7:7] = [{"tag": str(i), "protocol": "vless", "address": f"{i}.invalid"}
                                      for i in range(7, 10)]
        self.old_id = self.store(self.old)
        self.new_id = self.store(self.new)
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def store(self, profile, source="owned-source"):
        witness = identity.profile_provenance(profile)
        server_id = "sub:" + witness["content"]
        self.connection.execute("INSERT OR IGNORE INTO servers VALUES (?, 'active', ?)",
                                (server_id, json.dumps({"_fwrouter_profile_provenance": witness})))
        self.connection.execute("INSERT INTO subscription_server_memberships (source_id, server_id, is_active) VALUES (?, ?, 1)", (source, server_id))
        return server_id

    def migrate(self, revision=1):
        # Refresh ownership has moved to the canonical replacement before
        # reviewed identity continuity is installed.
        self.connection.execute(
            "UPDATE subscription_server_memberships SET is_active=0 WHERE server_id=?", (self.old_id,)
        )
        return identity.migrate_profile_identity(
            self.connection, old_server_id=self.old_id, new_server_id=self.new_id,
            old_profile=self.old, new_profile=self.new, expected_revision=revision,
        )

    def seed(self):
        # 70 unrelated + seven public profile credentials + one internal =78.
        for i in range(70):
            identity.retain_credentials(self.connection, scope=f"subscription:token-{i % 7}",
                server_id=f"unchanged-{i}", client_uuid=str(uuid5(NAMESPACE_DNS, f"owned-{i}")),
                client_email=f"sub-owned-{i}@fwrouter.local")
        for i in range(7):
            identity.retain_credentials(self.connection, scope=f"subscription:token-{i}",
                server_id=self.old_id, client_uuid=str(uuid5(NAMESPACE_DNS, f"affected-{i}")),
                client_email=f"sub-affected-{i}@fwrouter.local")
        identity.retain_credentials(self.connection, scope="vpn-auto", server_id=self.old_id,
            client_uuid=str(uuid5(NAMESPACE_DNS, "internal")), client_email="vpn-auto-owned@fwrouter.local")

    def rows(self):
        return [tuple(row) for row in self.connection.execute(
            "SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id")]

    def identity_snapshot(self):
        return {
            table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in (
                "subscription_node_identities", "subscription_node_aliases",
                "subscription_node_credentials", "subscription_node_identity_transitions",
            )
        }

    def test_all_78_survive_migration_refresh_replay_and_reopen(self):
        self.seed()
        before = self.rows()
        stable = self.migrate()
        self.assertEqual(stable, self.migrate())
        self.assertEqual(before, self.rows())
        self.assertEqual(78, len(before))
        for i in range(7):
            credentials = identity.credentials_for_scope(self.connection, f"subscription:token-{i}")
            self.assertEqual(credentials[self.old_id]["client_uuid"], credentials[self.new_id]["client_uuid"])
        self.assertEqual(2, self.connection.execute("SELECT revision FROM subscription_node_identities WHERE identity_id=?", (stable,)).fetchone()[0])
        self.connection.commit()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite"
            reopened = sqlite3.connect(path)
            self.connection.backup(reopened)
            reopened.close()
            reopened = sqlite3.connect(path)
            reopened.row_factory = sqlite3.Row
            self.assertEqual(before, [tuple(row) for row in reopened.execute(
                "SELECT * FROM subscription_node_credentials ORDER BY scope, identity_id")])
            self.assertEqual(stable, reopened.execute(
                "SELECT identity_id FROM subscription_node_aliases WHERE server_id=?",
                (self.new_id,),
            ).fetchone()[0])
            self.assertEqual(self.new_id, reopened.execute(
                "SELECT current_server_id FROM subscription_node_identities WHERE identity_id=?",
                (stable,),
            ).fetchone()[0])
            reopened.close()

    def test_refresh_admits_only_ordered_profile_growth_and_keeps_existing_credentials(self):
        self.seed()
        self.connection.execute(
            "UPDATE subscription_server_memberships SET is_active=0 WHERE server_id=?", (self.old_id,)
        )
        incoming = SimpleNamespace(
            server_id=self.new_id,
            raw={"_fwrouter_profile_provenance": identity.profile_provenance(self.new)},
        )
        before = self.rows()
        identity.inherit_profile_growth(self.connection, [incoming])
        aliases = self.connection.execute(
            "SELECT identity_id FROM subscription_node_aliases WHERE server_id=?", (self.new_id,)
        ).fetchone()
        self.assertIsNotNone(aliases)
        self.assertEqual(before, self.rows())
        for scope in [*(f"subscription:token-{i}" for i in range(7)), "vpn-auto"]:
            by_server = identity.credentials_for_scope(self.connection, scope)
            self.assertEqual(by_server[self.old_id]["client_uuid"], by_server[self.new_id]["client_uuid"])

    def test_conflict_rolls_back_entire_transaction(self):
        self.seed()
        identity.register_node(self.connection, self.new_id)
        self.connection.commit()
        before = self.identity_snapshot()
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_TARGET_CONFLICT"):
            with self.connection:
                self.migrate()
        self.assertEqual(before, self.identity_snapshot())

    def test_caller_rollback_restores_credentials_aliases_and_revision(self):
        self.seed()
        # Make the seeded durable state the transaction boundary. Otherwise a
        # rollback correctly undoes both the migration and this fixture setup.
        self.connection.commit()
        before = self.identity_snapshot()
        with self.assertRaisesRegex(RuntimeError, "simulated publication boundary"):
            with self.connection:
                self.migrate()
                raise RuntimeError("simulated publication boundary")
        self.assertEqual(before, self.identity_snapshot())

    def test_source_conflict_and_stale_revision_are_refused(self):
        self.seed()
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_REVISION_STALE"):
            self.migrate(99)
        self.connection.execute("UPDATE subscription_server_memberships SET source_id='foreign' WHERE server_id=?", (self.new_id,))
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_SOURCE_CONFLICT"):
            self.migrate()

    def test_source_cannot_own_old_and_new_profiles_as_active_at_once(self):
        self.seed()
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_ACTIVE_ALIAS_CONFLICT"):
            identity.migrate_profile_identity(
                self.connection, old_server_id=self.old_id, new_server_id=self.new_id,
                old_profile=self.old, new_profile=self.new, expected_revision=1,
            )

    def test_rotation_is_explicit_idempotent_and_fenced(self):
        self.seed()
        stable = self.migrate()
        before = identity.credentials_for_scope(self.connection, "vpn-auto")[self.new_id]
        rotated = identity.rotate_credentials(self.connection, scope="vpn-auto", identity_id=stable,
            expected_epoch=1, operation_id="owned-rotation")
        self.assertNotEqual(before["client_uuid"], rotated["client_uuid"])
        self.assertEqual(before["client_email"], rotated["client_email"])
        self.assertEqual(rotated["client_uuid"], identity.rotate_credentials(self.connection,
            scope="vpn-auto", identity_id=stable, expected_epoch=1, operation_id="owned-rotation")["client_uuid"])
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_ROTATION_EPOCH_STALE"):
            identity.rotate_credentials(self.connection, scope="vpn-auto", identity_id=stable,
                expected_epoch=1, operation_id="competing-rotation")

        # A retired UUID remains reserved even after its active credential has
        # rotated. It cannot be assigned to another server/scope on refresh.
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_CREDENTIAL_OWNERSHIP_CONFLICT"):
            identity.retain_credentials(
                self.connection, scope="subscription:other-client", server_id=self.new_id,
                client_uuid=before["client_uuid"], client_email="sub-retired@fwrouter.local",
            )
        self.assertEqual(1, self.connection.execute(
            "SELECT COUNT(*) FROM subscription_node_credential_history WHERE client_uuid=?",
            (before["client_uuid"],),
        ).fetchone()[0])

    def test_deleted_server_and_revoked_credentials_cannot_be_adopted(self):
        self.seed()
        self.connection.execute("UPDATE servers SET inventory_state='deleted' WHERE server_id=?", (self.old_id,))
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_SERVER_UNAVAILABLE"):
            self.migrate()
        identity_id = identity.register_node(self.connection, self.old_id)
        revoked = identity.revoke_credentials(self.connection, scope="vpn-auto", identity_id=identity_id,
            expected_epoch=1)
        self.assertEqual(1, revoked["revoked"])
        self.assertEqual(2, revoked["epoch"])
        self.assertEqual(revoked, identity.revoke_credentials(self.connection, scope="vpn-auto",
            identity_id=identity_id, expected_epoch=1))
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_CREDENTIAL_REVOKED"):
            identity.retain_credentials(self.connection, scope="vpn-auto", server_id=self.old_id,
                client_uuid=str(uuid5(NAMESPACE_DNS, "internal")), client_email="vpn-auto-owned@fwrouter.local")

    def test_scope_ownership_is_idempotent_and_rejects_credential_reassignment(self):
        self.seed()
        original = identity.credentials_for_scope(self.connection, "vpn-auto")[self.old_id]
        same = identity.retain_credentials(self.connection, scope="vpn-auto", server_id=self.old_id,
            client_uuid=original["client_uuid"], client_email=original["client_email"])
        self.assertEqual(original["client_uuid"], same["client_uuid"])
        self.assertEqual(original["client_email"], same["client_email"])
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_CREDENTIAL_CONFLICT"):
            identity.retain_credentials(self.connection, scope="vpn-auto", server_id=self.old_id,
                client_uuid=str(uuid5(NAMESPACE_DNS, "replacement")), client_email=original["client_email"])
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_.*OWNERSHIP_CONFLICT"):
            identity.retain_credentials(self.connection, scope="another-scope", server_id=self.old_id,
                client_uuid=original["client_uuid"], client_email="another@fwrouter.local")
        self.assertEqual(78, len(self.rows()))

    def test_migration_rejects_unreviewed_identity_and_keeps_historical_replay_idempotent(self):
        self.seed()
        wrong_id = "sub:" + "0" * 64
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_WITNESS_MISMATCH"):
            identity.migrate_profile_identity(
                self.connection, old_server_id=self.old_id, new_server_id=wrong_id,
                old_profile=self.old, new_profile=self.new, expected_revision=1,
            )
        stable = self.migrate()
        # Exact proof replay is safe and does not advance the logical revision.
        self.assertEqual(stable, self.migrate())
        self.assertEqual(2, self.connection.execute(
            "SELECT revision FROM subscription_node_identities WHERE identity_id=?", (stable,)
        ).fetchone()[0])
        # A committed identical proof may be replayed after a caller restart,
        # even if the caller retained the pre-commit expected revision.
        self.assertEqual(stable, identity.migrate_profile_identity(
            self.connection, old_server_id=self.old_id, new_server_id=self.new_id,
            old_profile=self.old, new_profile=self.new, expected_revision=1,
        ))
        third = json.loads(json.dumps(self.new))
        third["outbounds"].insert(0, {"tag": "added-before", "protocol": "vless", "address": "extra.invalid"})
        third_id = self.store(third)
        self.connection.execute(
            "UPDATE subscription_server_memberships SET is_active=0 WHERE server_id=?", (self.new_id,)
        )
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_REVISION_STALE"):
            identity.migrate_profile_identity(
                self.connection, old_server_id=self.new_id, new_server_id=third_id,
                old_profile=self.new, new_profile=third, expected_revision=1,
            )
        self.assertEqual(stable, identity.migrate_profile_identity(
            self.connection, old_server_id=self.new_id, new_server_id=third_id,
            old_profile=self.new, new_profile=third, expected_revision=2,
        ))
        # Historical transitions remain auditable but are not replayable once
        # the identity has advanced beyond their recorded target revision.
        before_stale_replay = self.identity_snapshot()
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_REVISION_STALE"):
            identity.migrate_profile_identity(
                self.connection, old_server_id=self.old_id, new_server_id=self.new_id,
                old_profile=self.old, new_profile=self.new, expected_revision=1,
            )
        self.assertEqual(before_stale_replay, self.identity_snapshot())

    def test_name_or_primary_endpoint_is_not_sufficient_provenance(self):
        changed = json.loads(json.dumps(self.old))
        changed["outbounds"] = changed["outbounds"][:1] + self.new["outbounds"][7:]
        self.assertFalse(identity.compatible_profile_growth(identity.profile_provenance(self.old), identity.profile_provenance(changed)))
        changed = json.loads(json.dumps(self.new))
        changed["routing"] = {"policy": "different"}
        self.assertFalse(identity.compatible_profile_growth(identity.profile_provenance(self.old), identity.profile_provenance(changed)))

    def test_reviewed_mapping_can_accept_policy_change_without_enabling_automatic_guessing(self):
        self.seed()
        changed = json.loads(json.dumps(self.new))
        changed["routing"] = {"policy": "operator-reviewed"}
        changed_id = self.store(changed)
        self.connection.execute(
            "UPDATE subscription_server_memberships SET is_active=0 WHERE server_id=?", (self.old_id,)
        )
        before = self.rows()
        with self.assertRaisesRegex(identity.IdentityConflict, "IDENTITY_MIGRATION_WITNESS_MISMATCH"):
            identity.migrate_profile_identity(
                self.connection, old_server_id=self.old_id, new_server_id=changed_id,
                old_profile=self.old, new_profile=changed, expected_revision=1,
            )
        stable = identity.migrate_profile_identity(
            self.connection, old_server_id=self.old_id, new_server_id=changed_id,
            old_profile=self.old, new_profile=changed, expected_revision=1,
            allow_profile_change=True,
        )
        self.assertEqual(78, len(before))
        self.assertEqual(before, self.rows())
        for scope in [*(f"subscription:token-{i}" for i in range(7)), "vpn-auto"]:
            by_server = identity.credentials_for_scope(self.connection, scope)
            self.assertEqual(by_server[self.old_id]["client_uuid"], by_server[changed_id]["client_uuid"])
        self.assertEqual(changed_id, self.connection.execute(
            "SELECT current_server_id FROM subscription_node_identities WHERE identity_id=?", (stable,)
        ).fetchone()[0])
